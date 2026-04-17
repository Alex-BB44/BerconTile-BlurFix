"""
test_blur_fix.py — BerconTile + VRayMultiSubTex Blur-Fix Tester  v0.02
=======================================================================

PROBLEM
-------
BerconTile (TexBerconTile) → VRayMultiSubTex (TexMulti) → VRayBitmap (BitmapBuffer)
rendert in VRay Standalone unscharf (blur), obwohl dasselbe Setup in 3ds Max scharf ist.

URSACHE (bestätigt durch BerconSC.h Quellcode + AppSDK-Rendertest)
-------------------------------------------------------------------
BerconTile überschreibt intern die UV-Koordinaten jeder Tile:
  UVW() → gibt tile-lokale UV zurück (skaliert mit 1/tile_size pro Tile)
  DUVW() → delegiert UNVERÄNDERT an den ursprünglichen Render-Context

Ergebnis: Der MIP-Sampler erhält UV-Ableitungen (ddu/ddv) die zum
gesamten Objekt passen, nicht zur einzelnen Tile. Die Ableitungen sind
~tile_width / tile_size mal zu groß → Sampler wählt ein zu grobes
MIP-Level → Blur.

In 3ds Max bleibt das Problem verborgen, weil der interaktive Viewport
eigene Nachschärfungsfilter hat. VRay Standalone und GPU zeigen den
Fehler unverdeckt. GPU rendert BerconTile oft ohne Textur, weil der
GPU-Pfad TexBerconTile nicht korrekt auflöst.

FIX-STRATEGIE (abhängig vom Texturformat)
------------------------------------------
.tx Dateien (tiled MIP-Pyramide, z.B. via maketx erstellt):
  → UVWGenChannel.duvw_scale = tile_size / tile_width setzen
  → Korrigiert die Ableitungen direkt am Sampler-Eingang
  → VRay wählt danach die korrekte MIP-Stufe aus der .tx Pyramide
  → Saubere Lösung: kein Aliasing, kein Over-Sharpening

.png / .jpg / .exr (kein MIP, flat texture):
  → BitmapBuffer.filter_blur = 0.01 setzen (Fallback)
  → Unterdrückt den übermäßigen Softening-Pass des EWA-Samplers
  → Kein MIP-Effekt, da keine MIP-Pyramide vorhanden

Gemischte Szenen (.tx und .png gleichzeitig):
  → Tool erkennt automatisch pro BitmapBuffer das Format
  → .tx bekommt duvw_scale, .png bekommt filter_blur

BEWEIS
------
AppSDK-Rendertest mit stoff_1 (6x BK1a diffuse, BerconTile tile_width=25):
  A_default       → blur   (Ableitungen zu groß, grobes MIP)
  B_filter_blur=0.01 → scharf (Softening unterdrückt, Fallback-Fix)
  C_duvw_scale=0.04  → scharf (korrekte Ableitungen, primärer Fix)
  D_beide         → scharf (kein Unterschied zu B oder C allein)
Tiled bitmap RAM: 6,01 MiB bei allen Stufen identisch (Dateien zu klein
für messbare MIP-Differenz). Für große .tx (4K+) würde feineres MIP
nachweislich mehr RAM laden.

AUFRUF
------
  python test_blur_fix.py <scene.vrscene> --analyze
  python test_blur_fix.py <scene.vrscene> --fix [--out output/]
  python test_blur_fix.py <scene.vrscene> --compare
  python test_blur_fix.py <scene.vrscene> --blur 0.01 --render

VORAUSSETZUNG
-------------
  VRay AppSDK: VRAY_SDK env oder /usr/Chaos/V-Ray/AppSDK (Linux)
               VRAY_FOR_3DSMAXxxxx_MAIN oder C:\\Program Files\\Chaos (Windows)
"""

from __future__ import annotations
import sys
import os
import re
import shutil
import argparse
from pathlib import Path


# ── AppSDK Loader (aus appsdk_pack.py übernommen) ────────────────────────────

def _python_version_key(p: Path) -> int:
    try:
        return int(p.name.replace("python", ""))
    except ValueError:
        return 0


def _find_vray_pyd_dir() -> str | None:
    cur_ver = sys.version_info.major * 100 + sys.version_info.minor
    sdk_roots = []

    def _has_vray(d: Path) -> bool:
        return (d / "vray.so").is_file() or (d / "vray.pyd").is_file() \
            or bool(list(d.glob("vray*.so")) + list(d.glob("vray*.pyd")))

    # Linux: VRAY_SDK env oder typische Pfade
    sdk_env = os.environ.get("VRAY_SDK", "")
    if sdk_env and Path(sdk_env).is_dir():
        sdk_roots.append(Path(sdk_env) / "python")
    for p in ["/usr/Chaos/V-Ray/AppSDK/python", "/opt/Chaos/V-Ray/AppSDK/python"]:
        if Path(p).is_dir():
            sdk_roots.append(Path(p))

    # Windows: VRAY_FOR_3DSMAXxxxx_MAIN
    for key, val in os.environ.items():
        if key.startswith("VRAY_FOR_3DSMAX") and key.endswith("_MAIN") and val:
            samples = Path(val).parent / "samples" / "appsdk"
            if samples.is_dir():
                sdk_roots.append(samples)

    # Windows: Program Files
    chaos_vray = Path(r"C:\Program Files\Chaos\V-Ray")
    if chaos_vray.is_dir():
        for max_dir in sorted(chaos_vray.glob("3ds Max *"), reverse=True):
            samples = max_dir / "samples" / "appsdk"
            if samples.is_dir():
                sdk_roots.append(samples)

    for root in sdk_roots:
        candidates = sorted(
            [d for d in root.glob("python3*") if _has_vray(d)],
            key=_python_version_key,
            reverse=True,
        )
        if not candidates:
            if _has_vray(root):
                return str(root)
            continue
        exact = [d for d in candidates if _python_version_key(d) == cur_ver]
        if exact:
            return str(exact[0])
        lower = [d for d in candidates if _python_version_key(d) <= cur_ver]
        if lower:
            return str(lower[0])
        return str(candidates[0])

    return None


def init_appsdk() -> bool:
    pyd_dir = _find_vray_pyd_dir()
    if not pyd_dir:
        print("[warning] vray.pyd nicht gefunden — Render-Modus nicht verfügbar.", file=sys.stderr)
        return False
    if pyd_dir not in sys.path:
        sys.path.insert(0, pyd_dir)
    print(f"[info] AppSDK: {pyd_dir}")
    return True


# ── vrscene Text-Analyse ──────────────────────────────────────────────────────

def flatten_vrscene(vrscene_path: Path, _seen: set | None = None) -> str:
    """Liest vrscene und löst #include Direktiven rekursiv auf."""
    if _seen is None:
        _seen = set()
    vrscene_path = vrscene_path.resolve()
    if vrscene_path in _seen:
        return ""
    _seen.add(vrscene_path)
    text = vrscene_path.read_text(encoding='utf-8', errors='replace')

    def resolve_include(m):
        inc_path_raw = m.group(1)
        # Windows-Backslashes → Forward-Slashes
        inc_path_raw = inc_path_raw.replace("\\", "/")
        inc_path = Path(inc_path_raw)
        if not inc_path.is_absolute():
            inc_path = vrscene_path.parent / inc_path
        if inc_path.is_file():
            return flatten_vrscene(inc_path, _seen)
        # Fallback: nur Dateiname im selben Ordner suchen
        local = vrscene_path.parent / inc_path.name
        if local.is_file():
            return flatten_vrscene(local, _seen)
        print(f"[warn] #include nicht gefunden: {inc_path_raw}", file=sys.stderr)
        return ""

    return re.sub(r'#include\s+"([^"]+)"', resolve_include, text)


def parse_plugin_blocks(text: str) -> dict[str, dict]:
    """
    Parst alle Plugin-Blöcke aus einem vrscene:
      PluginType PluginName { ... }
    Unterstützt mehrzeilige Werte (List(...), ListIntHex(...) etc.)
    """
    plugins: dict[str, dict] = {}

    # Top-Level Blöcke finden — berücksichtigt verschachtelte Klammern
    i = 0
    while i < len(text):
        # Suche nach "Word Word {"
        m = re.search(r'(\w[\w:]*)\s+([\w@.]+)\s*\{', text[i:])
        if not m:
            break
        ptype = m.group(1)
        pname = m.group(2)
        start = i + m.end()  # Position nach "{"

        # Finde das passende schließende "}" (Klammer-Zähler)
        depth = 1
        j = start
        while j < len(text) and depth > 0:
            if text[j] == '{': depth += 1
            elif text[j] == '}': depth -= 1
            j += 1
        body = text[start:j-1]
        i = j

        # Parameter parsen — mehrzeilige Werte werden als ein String zusammengefasst
        params: dict[str, str] = {}
        # Jeden Parameter "key=value;" extrahieren, value kann mehrzeilig sein
        param_pattern = re.compile(r'(\w+)\s*=\s*(List\([^)]*\)|[\w@.()\[\]",:;/\\._\-+]+(?:[\s\S]*?);)', re.DOTALL)
        for pm in param_pattern.finditer(body):
            k = pm.group(1)
            v = pm.group(2).strip().rstrip(';')
            params[k] = v

        # Fallback: einfacher Zeilen-Parser für restliche Parameter
        for line in body.splitlines():
            line = line.strip()
            if '=' in line and not line.startswith('//'):
                k, _, v = line.partition('=')
                k = k.strip()
                if k not in params:
                    params[k] = v.strip().rstrip(';')

        plugins[pname] = {'type': ptype, 'params': params}

    return plugins


def find_bercon_multi_chains(plugins: dict, text: str = "") -> list[dict]:
    """
    Findet alle Ketten: TexBerconTile → TexMulti → TexBitmap → BitmapBuffer
    Gibt Liste von Dicts zurück mit den gefundenen Plugin-Namen.
    """
    chains = []

    bercon_tiles = {n: p for n, p in plugins.items() if p['type'] == 'TexBerconTile'}
    tex_multis   = {n: p for n, p in plugins.items() if p['type'] == 'TexMulti'}

    for bt_name, bt_plugin in bercon_tiles.items():
        # Finde TexMulti-Plugins die in einem BerconTile-Parameter referenziert werden
        bt_params_str = str(bt_plugin['params'])

        for tm_name, tm_plugin in tex_multis.items():
            if tm_name not in bt_params_str:
                continue

            # Sub-Texturen aus textures_list extrahieren
            # Direkt aus dem Original-Text holen (mehrzeilige Listen)
            tl_match = re.search(
                re.escape(tm_name) + r'[^{]*\{[^}]*textures_list\s*=\s*List\(([^)]*)\)',
                text, re.DOTALL
            )
            if tl_match:
                textures_list_str = tl_match.group(1)
            else:
                textures_list_str = tm_plugin['params'].get('textures_list', '')
            sub_tex_names = re.findall(r'([\w@.]+)', textures_list_str)

            # BitmapBuffer-Namen die zu diesen Sub-Texturen gehören
            bitmap_buffers = []
            for sub_name in sub_tex_names:
                # Sub-Name kann TexCombineColor oder direkt TexBitmap sein
                if sub_name not in plugins:
                    continue
                sub_plugin = plugins[sub_name]
                sub_params = sub_plugin['params']

                if sub_plugin['type'] == 'TexBitmap':
                    buf_name = sub_params.get('bitmap', '').strip(';')
                    if buf_name and buf_name in plugins:
                        bitmap_buffers.append(buf_name)

                elif sub_plugin['type'] == 'TexCombineColor':
                    tex_ref = sub_params.get('texture', '').strip(';')
                    if tex_ref and tex_ref in plugins:
                        inner = plugins[tex_ref]
                        if inner['type'] == 'TexBitmap':
                            buf_name = inner['params'].get('bitmap', '').strip(';')
                            if buf_name and buf_name in plugins:
                                bitmap_buffers.append(buf_name)

            chains.append({
                'bercon_tile':    bt_name,
                'tex_multi':      tm_name,
                'tex_multi_mode': tm_plugin['params'].get('mode', '?'),
                'random_mode':    tm_plugin['params'].get('random_mode', '?'),
                'bitmap_buffers': bitmap_buffers,
            })

    return chains


TX_EXTENSIONS = {'.tx'}

def detect_bitmap_types(plugins: dict) -> dict[str, str]:
    """
    Gibt für jeden BitmapBuffer-Namen das Texturformat zurück: 'tx' oder 'other'.
    .tx hat eine MIP-Pyramide → duvw_scale Fix.
    Alles andere (png/jpg/exr/...) hat kein MIP → filter_blur Fix.

    Extrahiert den Dateinamen robust via Regex aus dem rohen file= Wert,
    da der Parser mehrzeilige Werte manchmal mit Folgezeilen zusammenfasst.
    """
    result = {}
    for name, p in plugins.items():
        if p['type'] != 'BitmapBuffer':
            continue
        file_val = p['params'].get('file', '')
        # Extrahiere nur den eigentlichen Pfad zwischen Anführungszeichen
        m = re.search(r'"([^"]+)"', file_val)
        if m:
            file_val = m.group(1)
        ext = Path(file_val).suffix.lower()
        result[name] = 'tx' if ext in TX_EXTENSIONS else 'other'
    return result


def get_bercon_tile_params(plugins: dict, bt_name: str, full_text: str = "") -> dict:
    """
    Liest tile_size, tile_width, tile_height aus einem TexBerconTile-Plugin.
    Gibt dict zurück, berechnet duvw_scale = tile_size / tile_width.

    Hintergrund:
      BerconTile skaliert die UV jeder Tile um ~1/tile_size intern.
      tile_width / tile_size gibt die effektive UV-Skalierung an, um die
      die Ableitungen zu groß sind. duvw_scale = tile_size / tile_width
      korrigiert das (Ableitungen werden verkleinert → feineres MIP).

    Liest direkt per Regex aus dem Plugin-Block im full_text, um Parser-
    Artefakte durch Substring-Namen (tile_width vs tile_width2) zu vermeiden.
    """
    tile_size = tile_width = tile_height = None

    if full_text:
        # Finde den exakten Plugin-Block per Regex
        block_m = re.search(
            r'TexBerconTile\s+' + re.escape(bt_name) + r'\s*\{([^}]*)\}',
            full_text, re.DOTALL
        )
        if block_m:
            body = block_m.group(1)
            def _get(key):
                m = re.search(r'\b' + key + r'\s*=\s*([\d.eE+-]+)\s*;', body)
                return float(m.group(1)) if m else None
            tile_size   = _get('tile_size')
            tile_width  = _get('tile_width')
            tile_height = _get('tile_height')

    # Fallback auf geparstes dict
    if tile_size is None or tile_width is None:
        params = plugins.get(bt_name, {}).get('params', {})
        try:
            if tile_size  is None: tile_size  = float(params.get('tile_size',  1.0))
            if tile_width is None: tile_width = float(params.get('tile_width', 1.0))
            if tile_height is None:
                tile_height = float(params.get('tile_height', tile_width))
        except (ValueError, TypeError):
            tile_size = tile_width = tile_height = 1.0

    if tile_height is None:
        tile_height = tile_width
    if tile_width  <= 0: tile_width  = 1.0
    if tile_height <= 0: tile_height = tile_width

    duvw_scale = tile_size / tile_width

    return {
        'tile_size':   tile_size,
        'tile_width':  tile_width,
        'tile_height': tile_height,
        'duvw_scale':  duvw_scale,
    }


def patch_uvwgen_duvw_scale(text: str, duvw_scale: float) -> tuple[str, int]:
    """
    Setzt duvw_scale in allen UVWGenChannel-Blöcken.

    Warum alle UVWGenChannels?
      In einer BerconTile-Kette haben alle Sub-Textur UVWGenChannels
      Identity-Transform (keine eigene UV-Transformation). duvw_scale=1.0
      ist der Default. Da wir nicht zuverlässig tracen können welche
      UVWGenChannels zu welcher BerconTile-Kette gehören (TexCombineColor
      Zwischenschicht, mehrstufige Ketten), patchen wir alle die derzeit
      duvw_scale=1.0 haben oder keinen duvw_scale-Eintrag.

      Risiko: UVWGenChannels die zu anderen Materialien gehören werden
      ebenfalls gepatcht. In reinen BerconTile-Szenen ist das unkritisch.
      Für gemischte Szenen: --fix-uvwgen-only Flag verwenden (zukünftig).

    Gibt (gepatchten Text, Anzahl Patches) zurück.
    """
    count = 0
    scale_str = f'{duvw_scale:.6f}'

    def patch_block(m):
        nonlocal count
        body = m.group(0)
        if 'duvw_scale' in body:
            # Nur patchen wenn noch auf Default (1.0)
            if re.search(r'duvw_scale\s*=\s*1(?:\.0+)?\s*;', body):
                result = re.sub(r'duvw_scale\s*=\s*[^;]+;',
                                f'duvw_scale={scale_str};', body)
                count += 1
                return result
            return body
        else:
            count += 1
            return body.replace('{', f'{{\n  duvw_scale={scale_str};', 1)

    return re.sub(
        r'UVWGenChannel\s+[\w@.]+\s*\{[^}]*\}',
        patch_block, text, flags=re.DOTALL
    ), count


def apply_fix(
    text: str,
    plugins: dict,
    chains: list[dict],
    duvw_scale: float | None = None,
    filter_blur_fallback: float = 0.01,
    filter_type: int = 5,
) -> tuple[str, dict]:
    """
    Wendet den korrekten Fix abhängig vom Texturformat an:
      .tx  → UVWGenChannel.duvw_scale (primärer Fix, MIP-korrekt)
      .png → BitmapBuffer.filter_blur  (Fallback, kein MIP)

    Gibt (gepatchten Text, Report-Dict) zurück.
    Report enthält: tx_count, other_count, duvw_patched, blur_patched
    """
    bitmap_types = detect_bitmap_types(plugins)

    # Ziel-BitmapBuffers aus Ketten — Fallback: alle
    target_buffers: set[str] = set()
    for chain in chains:
        target_buffers.update(chain['bitmap_buffers'])
    if not target_buffers:
        target_buffers = set(bitmap_types.keys())

    tx_buffers    = {n for n in target_buffers if bitmap_types.get(n) == 'tx'}
    other_buffers = {n for n in target_buffers if bitmap_types.get(n) != 'tx'}

    duvw_patched = 0
    blur_patched  = 0

    if tx_buffers and duvw_scale is not None:
        # Primärer Fix: duvw_scale auf UVWGenChannels
        text, duvw_patched = patch_uvwgen_duvw_scale(text, duvw_scale)

    if other_buffers:
        # Fallback: filter_blur auf non-tx BitmapBuffers
        text, blur_patched = patch_bitmap_buffers(
            text, other_buffers, filter_blur_fallback, filter_type
        )

    # Wenn alle Texturen .tx sind aber duvw_scale unbekannt → filter_blur Fallback
    if tx_buffers and duvw_scale is None:
        text, blur_patched = patch_bitmap_buffers(
            text, tx_buffers, filter_blur_fallback, filter_type
        )

    report = {
        'tx_count':     len(tx_buffers),
        'other_count':  len(other_buffers),
        'duvw_patched': duvw_patched,
        'blur_patched': blur_patched,
        'duvw_scale':   duvw_scale,
    }
    return text, report


def analyze_vrscene(vrscene_path: Path) -> None:
    """Analysiert das vrscene und gibt Befunde aus."""
    text = flatten_vrscene(vrscene_path)
    plugins = parse_plugin_blocks(text)
    chains = find_bercon_multi_chains(plugins, text)

    print(f"\n=== Analyse: {vrscene_path.name} ===")
    print(f"Plugins gesamt: {len(plugins)}")

    bercon_count = sum(1 for p in plugins.values() if p['type'] == 'TexBerconTile')
    multi_count  = sum(1 for p in plugins.values() if p['type'] == 'TexMulti')
    bitmap_count = sum(1 for p in plugins.values() if p['type'] == 'BitmapBuffer')
    print(f"TexBerconTile:  {bercon_count}")
    print(f"TexMulti:       {multi_count}")
    print(f"BitmapBuffer:   {bitmap_count}")
    print(f"Ketten gefunden: {len(chains)}")

    for i, chain in enumerate(chains):
        print(f"\n  Kette #{i+1}:")
        print(f"    TexBerconTile : {chain['bercon_tile']}")
        print(f"    TexMulti      : {chain['tex_multi']}")
        print(f"    mode          : {chain['tex_multi_mode']}  random_mode: {chain['random_mode']}")
        print(f"    BitmapBuffers : {len(chain['bitmap_buffers'])} Stück")

        for bb_name in chain['bitmap_buffers'][:3]:
            bb = plugins[bb_name]['params']
            blur = bb.get('filter_blur', '?')
            ftype = bb.get('filter_type', '?')
            fname = bb.get('file', '?')
            print(f"      {bb_name}: filter_blur={blur} filter_type={ftype} [{Path(fname).name}]")
        if len(chain['bitmap_buffers']) > 3:
            print(f"      ... und {len(chain['bitmap_buffers'])-3} weitere")

    # UVWGenChannel Identity-Check
    identity_hex = "0000803F0000000000000000000000000000803F0000000000000000000000000000803F"
    identity_count = sum(
        1 for p in plugins.values()
        if p['type'] == 'UVWGenChannel'
        and identity_hex in p['params'].get('uvw_transform', '')
    )
    print(f"\n  UVWGenChannel mit Identity-Transform: {identity_count}")
    print("  (Identity = Ableitungs-Problem wenn BerconTile UVs intern remappt)")

    if chains:
        bitmap_types = detect_bitmap_types(plugins)
        tx_count    = sum(1 for t in bitmap_types.values() if t == 'tx')
        other_count = sum(1 for t in bitmap_types.values() if t != 'tx')
        print(f"\n  Texturformat: {tx_count}x .tx (MIP)  |  {other_count}x .png/.jpg/andere (kein MIP)")

        # BerconTile-Parameter für duvw_scale Berechnung
        for chain in chains:
            bt_params = get_bercon_tile_params(plugins, chain['bercon_tile'], text)
            print(f"\n  Fix-Strategie für Kette '{chain['bercon_tile']}':")
            print(f"    tile_size={bt_params['tile_size']}  tile_width={bt_params['tile_width']}"
                  f"  tile_height={bt_params['tile_height']}")
            print(f"    → duvw_scale = tile_size / tile_width = {bt_params['duvw_scale']:.6f}")
            if tx_count > 0:
                print(f"    → .tx Texturen: UVWGenChannel.duvw_scale={bt_params['duvw_scale']:.6f}  (primärer Fix)")
            if other_count > 0:
                print(f"    → .png/.jpg:    BitmapBuffer.filter_blur=0.01  (Fallback-Fix)")

        print("\n  DIAGNOSE: BerconTile-Ketten gefunden.")
        print("  Aufruf: python test_blur_fix.py <scene.vrscene> --fix")


# ── vrscene Text-Patcher ──────────────────────────────────────────────────────

def patch_bitmap_buffers(
    text: str,
    target_names: set[str],
    filter_blur: float,
    filter_type: int | None = None,
) -> tuple[str, int]:
    """
    Patcht filter_blur (und optional filter_type) in den angegebenen BitmapBuffer-Blöcken.
    Gibt (gepatchten Text, Anzahl Patches) zurück.
    """
    count = 0

    for name in target_names:
        # Findet den BitmapBuffer-Block mit diesem Namen
        block_pattern = re.compile(
            r'(BitmapBuffer\s+' + re.escape(name) + r'\s*\{)([^{}]*\})',
            re.DOTALL
        )
        def patch_block(m, fb=filter_blur, ft=filter_type):
            header, body = m.group(1), m.group(2)
            # filter_blur ersetzen oder hinzufügen
            if 'filter_blur=' in body:
                body = re.sub(r'filter_blur\s*=\s*[^;]+;', f'filter_blur={fb};', body)
            else:
                body = body.replace('{', f'{{\n  filter_blur={fb};', 1)
            # filter_type optional
            if ft is not None:
                if 'filter_type=' in body:
                    body = re.sub(r'filter_type\s*=\s*[^;]+;', f'filter_type={ft};', body)
                else:
                    body = body.replace('{', f'{{\n  filter_type={ft};', 1)
            return header + body

        new_text, n = block_pattern.subn(patch_block, text)
        if n:
            text = new_text
            count += n

    return text, count


def create_patched_vrscene(
    vrscene_path: Path,
    output_path: Path,
    filter_blur: float = 0.01,
    filter_type: int = 5,
    use_smart_fix: bool = True,
) -> dict:
    """
    Erstellt eine gepatchte Kopie der vrscene.

    use_smart_fix=True (default):
      Erkennt Texturformat pro BitmapBuffer:
        .tx  → UVWGenChannel.duvw_scale (MIP-korrekt, primärer Fix)
        .png → BitmapBuffer.filter_blur  (kein MIP, Fallback)
      duvw_scale wird aus TexBerconTile.tile_size / tile_width berechnet.

    use_smart_fix=False:
      Nur filter_blur patchen (altes Verhalten, für Vergleichstests).

    Gibt Report-Dict zurück.
    """
    text = flatten_vrscene(vrscene_path)
    plugins = parse_plugin_blocks(text)
    chains = find_bercon_multi_chains(plugins, text)

    if use_smart_fix and chains:
        # duvw_scale aus erster BerconTile-Kette berechnen
        bt_params  = get_bercon_tile_params(plugins, chains[0]['bercon_tile'], text)
        duvw_scale = bt_params['duvw_scale']
        patched_text, report = apply_fix(
            text, plugins, chains,
            duvw_scale=duvw_scale,
            filter_blur_fallback=filter_blur,
            filter_type=filter_type,
        )
        print(f"[info] Smart-Fix: duvw_scale={duvw_scale:.6f} ({report['duvw_patched']} UVWGen)"
              f"  filter_blur={filter_blur} ({report['blur_patched']} BitmapBuffer)")
    else:
        # Fallback: alle BitmapBuffer mit filter_blur patchen
        target_buffers: set[str] = set()
        for chain in chains:
            target_buffers.update(chain['bitmap_buffers'])
        if not target_buffers:
            target_buffers = {n for n, p in plugins.items() if p['type'] == 'BitmapBuffer'}
        patched_text, count = patch_bitmap_buffers(text, target_buffers, filter_blur, filter_type)
        report = {'blur_patched': count, 'duvw_patched': 0, 'duvw_scale': None,
                  'tx_count': 0, 'other_count': count}
        print(f"[info] filter_blur={filter_blur} auf {count} BitmapBuffer gesetzt")

    output_path.write_text(patched_text, encoding='utf-8')
    print(f"[info] Gepatchte vrscene: {output_path}")
    return report


# ── Render via AppSDK ─────────────────────────────────────────────────────────

_MEM_KEYWORDS = ("Peak memory", "Maximum memory usage", "texman", "Tiled bitmap", "Bitmap\"")

def render_vrscene(vrscene_path: Path, output_image: Path, width: int = 800, height: int = 600) -> bool:
    """Rendert eine vrscene, speichert PNG und Textur-Speicher-Report als .txt."""
    try:
        import vray
    except ImportError:
        print("[error] vray Modul nicht verfügbar — kein Render möglich.", file=sys.stderr)
        return False

    errors = []
    mem_lines: list[str] = []

    def on_log(renderer, message, level, instant):
        if level == vray.LOGLEVEL_ERROR:
            errors.append(message)
            print(f"[ERROR] {message}", file=sys.stderr)
        elif level == vray.LOGLEVEL_WARNING:
            print(f"[warn]  {message}", file=sys.stderr)
        elif level == vray.LOGLEVEL_INFO:
            print(f"[info]  {message}")
            if any(kw in message for kw in _MEM_KEYWORDS):
                mem_lines.append(message.strip())

    print(f"[info] Rendere: {vrscene_path.name} → {output_image.name}")

    with vray.VRayRenderer(noRenderLicensePreCheck=True) as renderer:
        renderer.setOnLogMessage(on_log)
        renderer.load(str(vrscene_path))

        renderer.size = (width, height)

        renderer.startSync()
        renderer.waitForRenderEnd()

        if errors:
            print(f"[error] Render fehlgeschlagen: {errors[0]}", file=sys.stderr)
            return False

        img = renderer.getImage()
        if img:
            img.save(str(output_image))
            print(f"[info] Gespeichert: {output_image}")
        else:
            print("[error] Kein Bild empfangen.", file=sys.stderr)
            return False

    # Textur-Speicher-Report speichern
    report_path = output_image.with_suffix('.txt')
    label = output_image.stem
    report_lines = [
        f"Textur-Speicher-Report: {label}",
        f"Szene:      {vrscene_path.name}",
        f"Auflösung:  {width}x{height}",
        "=" * 50,
        *mem_lines,
        "=" * 50,
    ]
    if not mem_lines:
        report_lines.append("(keine Speicher-Daten vom Renderer erhalten)")
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"[info] Speicher-Report: {report_path}")

    return True


# ── Vergleichs-Test ───────────────────────────────────────────────────────────

BLUR_TEST_LEVELS = [1.0, 0.5, 0.1, 0.01]


def run_compare(vrscene_path: Path, output_dir: Path, width: int, height: int) -> None:
    """
    Rendert dieselbe Szene mit verschiedenen filter_blur Werten für Vergleich.
    Ergebnis: output_dir/blur_1.00.png, blur_0.10.png etc.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    has_appsdk = init_appsdk()

    text = flatten_vrscene(vrscene_path)
    plugins = parse_plugin_blocks(text)
    chains = find_bercon_multi_chains(plugins, text)
    target_buffers: set[str] = set()
    for chain in chains:
        target_buffers.update(chain['bitmap_buffers'])

    if not target_buffers:
        print("[warning] Keine BerconTile-Ketten erkannt — alle BitmapBuffer werden gepatcht.")
        target_buffers = {n for n, p in plugins.items() if p['type'] == 'BitmapBuffer'}

    bt_params  = get_bercon_tile_params(plugins, chains[0]['bercon_tile'], text) if chains else {}
    duvw_scale = bt_params.get('duvw_scale')
    bitmap_types = detect_bitmap_types(plugins)
    tx_count    = sum(1 for t in bitmap_types.values() if t == 'tx')
    other_count = sum(1 for t in bitmap_types.values() if t != 'tx')

    print(f"\n=== Vergleichs-Test: {len(BLUR_TEST_LEVELS)} Blur-Stufen ===")
    print(f"Ziel-BitmapBuffers: {len(target_buffers)}  ({tx_count}x .tx, {other_count}x andere)")
    if duvw_scale:
        print(f"duvw_scale (berechnet): {duvw_scale:.6f}")

    for blur in BLUR_TEST_LEVELS:
        label = f"blur_{blur:.2f}"
        patched_path = output_dir / f"{vrscene_path.stem}_{label}.vrscene"
        image_path   = output_dir / f"{label}.png"

        # filter_blur only (kein Smart-Fix) für direkten Vergleich
        patched_text, count = patch_bitmap_buffers(text, target_buffers, blur, filter_type=5)
        patched_path.write_text(patched_text, encoding='utf-8')

        print(f"\n  blur={blur:.2f}: {patched_path.name} ({count} BitmapBuffer gepatcht)")

        if has_appsdk:
            render_vrscene(patched_path, image_path, width, height)
        else:
            print(f"  [skip] Kein AppSDK — Render übersprungen. Patched vrscene: {patched_path}")

    # Smart-Fix Render (duvw_scale) zum Vergleich
    if duvw_scale and has_appsdk:
        print(f"\n  smart-fix (duvw_scale={duvw_scale:.4f}):")
        sf_path  = output_dir / f"{vrscene_path.stem}_smart_fix.vrscene"
        sf_image = output_dir / "smart_fix.png"
        sf_text, sf_report = apply_fix(text, plugins, chains, duvw_scale=duvw_scale)
        sf_path.write_text(sf_text, encoding='utf-8')
        render_vrscene(sf_path, sf_image, width, height)

    print(f"\n=== Fertig. Ergebnisse: {output_dir} ===")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="BerconTile Blur-Fix Tester für VRay Standalone",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("vrscene", help="Pfad zur .vrscene Datei")
    parser.add_argument("--fix",    action="store_true",
                        help="Smart-Fix anwenden: duvw_scale für .tx, filter_blur für .png")
    parser.add_argument("--blur",   type=float, default=0.01,
                        help="filter_blur Fallback-Wert (default: 0.01)")
    parser.add_argument("--filter-type", type=int, default=5,
                        help="filter_type Wert (default: 5=EWA; 0=kein Filter)")
    parser.add_argument("--render",  action="store_true",
                        help="Nach dem Patchen rendern (kombinierbar mit --fix)")
    parser.add_argument("--compare", action="store_true",
                        help="Alle Blur-Stufen vergleichend rendern + Smart-Fix")
    parser.add_argument("--analyze", action="store_true",
                        help="Nur analysieren, kein Patch/Render")
    parser.add_argument("--out", default=None,
                        help="Output-Pfad: Ordner (--compare) oder .vrscene Datei (--fix/--blur)")
    parser.add_argument("--width",  type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    args = parser.parse_args()

    vrscene = Path(args.vrscene).resolve()
    if not vrscene.is_file():
        print(f"[error] vrscene nicht gefunden: {vrscene}", file=sys.stderr)
        sys.exit(1)

    # Immer analysieren
    analyze_vrscene(vrscene)

    if args.analyze:
        return

    if args.compare:
        out_dir = Path(args.out).resolve() if args.out else vrscene.parent / "blur_compare"
        run_compare(vrscene, out_dir, args.width, args.height)
        return

    if args.fix:
        # Smart-Fix: duvw_scale für .tx, filter_blur für .png
        out_path = Path(args.out).resolve() if args.out else \
                   vrscene.parent / f"{vrscene.stem}_fixed.vrscene"
        if out_path.is_dir():
            out_path = out_path / f"{vrscene.stem}_fixed.vrscene"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        report = create_patched_vrscene(vrscene, out_path, args.blur, args.filter_type,
                                        use_smart_fix=True)
        if args.render:
            has_appsdk = init_appsdk()
            if has_appsdk:
                image_path = out_path.with_suffix('.png')
                render_vrscene(out_path, image_path, args.width, args.height)
        return

    # Einzel-Patch mit filter_blur (altes Verhalten, für manuelle Tests)
    out_dir = Path(args.out).resolve() if args.out else vrscene.parent / "blur_test_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    label = f"blur_{args.blur:.2f}"
    patched_path = out_dir / f"{vrscene.stem}_{label}.vrscene"

    create_patched_vrscene(vrscene, patched_path, args.blur, args.filter_type,
                           use_smart_fix=False)

    if args.render:
        has_appsdk = init_appsdk()
        if has_appsdk:
            image_path = out_dir / f"{label}.png"
            render_vrscene(patched_path, image_path, args.width, args.height)
        else:
            print("[skip] Kein AppSDK — nur gepatchte vrscene erstellt.")


if __name__ == "__main__":
    main()
