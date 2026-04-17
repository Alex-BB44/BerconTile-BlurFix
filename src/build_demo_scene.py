"""
write_demo_vrscene.py — Schreibt demo.vrscene direkt als Text
=============================================================
Kein AppSDK-Round-Trip — sauberes, lesbares vrscene-Format.
Zwei Planes: links PNG, rechts TX. Kamera schräg von oben.

Aufruf:
  python3 write_demo_vrscene.py --resources /pfad/resources/ --out /pfad/scene/
"""

from __future__ import annotations
import argparse
import math
from pathlib import Path

TILE_SIZE   = 1.0
TILE_WIDTH  = 25.0
TILE_HEIGHT2 = 12.0

PLANE_SIZE = 120.0
PLANE_GAP  = 20.0

TEXTURE_STEMS = [
    "BK1a__01_diffuse",
    "BK1a__02_diffuse",
    "BK1a__03_diffuse",
    "BK1a__04_diffuse",
    "BK1a__05_diffuse",
    "BK1a__06_diffuse",
]


def plane_block(prefix: str, cx: float, resources: Path, ext: str) -> str:
    h = PLANE_SIZE / 2.0
    verts = [
        (cx - h, 0.0,  h),
        (cx + h, 0.0,  h),
        (cx + h, 0.0, -h),
        (cx - h, 0.0, -h),
    ]
    vstr = ",".join(f"Vector({x},{y},{z})" for x,y,z in verts)
    uvstr = "Vector(0,0,0),Vector(1,0,0),Vector(1,1,0),Vector(0,1,0)"

    lines = []

    # Mesh
    lines.append(f"""GeomStaticMesh {prefix}@mesh {{
  vertices=ListVector({vstr});
  faces=ListInt(0,1,2,0,2,3);
  normals=ListVector(Vector(0,1,0),Vector(0,1,0),Vector(0,1,0),Vector(0,1,0));
  faceNormals=ListInt(0,1,2,0,2,3);
  map_channels=List(ListVector({uvstr}));
  map_channels_names=ListInt(1);
}}
""")

    # BitmapBuffer + UVWGen + TexBitmap pro Textur
    for i, stem in enumerate(TEXTURE_STEMS):
        filepath = str(resources / f"{stem}{ext}").replace("\\", "/")
        lines.append(f"""BitmapBuffer {prefix}@buf_{i} {{
  file="{filepath}";
  color_space=1;
  gamma=1.0;
}}
UVWGenChannel {prefix}@uvw_{i} {{
  uvw_channel=1;
}}
TexBitmap {prefix}@bm_{i} {{
  bitmap={prefix}@buf_{i};
  uvwgen={prefix}@uvw_{i};
}}
""")

    # TexMulti
    tex_refs = ",".join(f"{prefix}@bm_{i}" for i in range(len(TEXTURE_STEMS)))
    weights  = ",".join("1.0" for _ in TEXTURE_STEMS)
    lines.append(f"""TexMulti {prefix}@multi {{
  mode=12;
  random_mode=132;
  textures_list=List({tex_refs});
  random_weights=ListFloat({weights});
}}
""")

    # TexBerconTile
    lines.append(f"""UVWGenChannel {prefix}@uvwgen_bercon {{
  uvw_channel=1;
}}
TexBerconTile {prefix}@bercon {{
  uvwgen={prefix}@uvwgen_bercon;
  noise_map1={prefix}@multi;
  noise_color2=Color(0.55,0.52,0.48);
  tile_size={TILE_SIZE};
  tile_width={TILE_WIDTH};
  tile_height2={TILE_HEIGHT2};
  rand_rot=0.0;
  tile_style=0;
}}
""")

    # Material
    lines.append(f"""BRDFVRayMtl {prefix}@brdf {{
  diffuse={prefix}@bercon;
  reflect=AColor(0.03,0.03,0.03,1);
  reflect_glossiness=0.35;
}}
MtlSingleBRDF {prefix}@mtl {{
  brdf={prefix}@brdf;
}}
""")

    # Node (Transform = Identität)
    lines.append(f"""Node {prefix}@node {{
  geometry={prefix}@mesh;
  material={prefix}@mtl;
  transform=Transform(Matrix(Vector(1,0,0),Vector(0,1,0),Vector(0,0,1)),Vector(0,0,0));
  primary_visibility=1;
}}
""")

    return "\n".join(lines)


def camera_block(cam_dist: float, pitch_deg: float, fov_deg: float) -> str:
    pitch = math.radians(pitch_deg)
    fov   = math.radians(fov_deg)
    # VRay Kamera: Matrix(right, up, -forward_look)
    # Kamera schaut von schräg oben nach unten auf Y=0 Ebene
    # Camera-Z-Achse = Richtung HINTER die Kamera (entgegen Blickrichtung)
    # Blickrichtung = (0, -sin(pitch), sin(pitch)) = schräg nach unten
    # Camera-Z (rückwärts) = -Blickrichtung = (0, sin(pitch), -sin(pitch))... nein
    #
    # Einfachste korrekte Methode: TransformHex aus bekannt funktionierendem vrscene
    # Stattdessen: Kamera direkt senkrecht von oben (pitch=90) für Debug
    # Position: hoch über den Planes
    px, py, pz = 0.0, cam_dist, 0.0
    # Senkrecht von oben: right=X, up=-Z, forward(cam-z)=+Y (wegweisend)
    # In VRay: Matrix columns = (right, up, back) wobei back = entgegen Blick
    # Blick = -Y (nach unten), back = +Y
    # right = +X, up = +Z (oben im Bild)
    return f"""RenderView renderView {{
  transform=Transform(
    Matrix(Vector(1,0,0),Vector(0,0,1),Vector(0,1,0)),
    Vector({px},{py:.4f},{pz})
  );
  fov={fov:.6f};
  focalDistance={cam_dist};
  clipping_near=1.0;
  clipping_far=3000.0;
  orthographic=0;
}}
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--resources", required=True)
    parser.add_argument("--out",       required=True)
    args = parser.parse_args()

    resources = Path(args.resources).resolve()
    out_dir   = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir.parent / "output").mkdir(parents=True, exist_ok=True)

    for stem in TEXTURE_STEMS:
        for ext in (".png", ".tx"):
            if not (resources / f"{stem}{ext}").is_file():
                print(f"[FEHLER] {stem}{ext} nicht gefunden in {resources}")
                return

    offset = (PLANE_SIZE + PLANE_GAP) / 2.0
    out_path = str(out_dir.parent / "output").replace("\\", "/")

    scene = f"""// BerconTile Blur-Fix Demo Scene
// Two planes: left=PNG textures, right=TX (tiled MIP) textures
// Both use BerconTile -> TexMulti -> BitmapBuffer chain
// Render without fix to see blur, apply fix to see sharp result
// See README.md for full explanation

"""

    scene += plane_block("png", -offset, resources, ".png")
    scene += plane_block("tx",  +offset, resources, ".tx")
    scene += camera_block(cam_dist=300.0, pitch_deg=45.0, fov_deg=55.0)

    scene += f"""LightDirect sun {{
  transform=Transform(
    Matrix(Vector(1,0,0),Vector(0,0.6,0.8),Vector(0,-0.8,0.6)),
    Vector(-150,250,150)
  );
  color=AColor(1.0,0.96,0.88,1);
  intensity=3.5;
  shadowSubdivs=12;
  enabled=1;
}}

SettingsEnvironment env {{
  bg_color=AColor(0.82,0.88,1.0,1);
  gi_color=AColor(0.55,0.62,0.78,1);
}}

SettingsGI gi {{
  on=1;
  primary_multiplier=1.0;
  primary_engine=4;
  secondary_engine=3;
}}

SettingsImageSampler sampler {{
  type=3;
  progressive_minSubdivs=1;
  progressive_maxSubdivs=50;
  progressive_noise_threshold=0.005;
  progressive_maxTime=30;
}}

SettingsColorMapping colormap {{
  type=6;
}}

SettingsOutput output {{
  img_width=1920;
  img_height=540;
  img_dir="{out_path}/";
  img_file="render.png";
  img_noAlpha=1;
  frames_per_second=1.0;
  frame_start=0;
  frames=1;
  rgbchannel_consider_for_aa=1;
}}
"""

    scene_path = out_dir / "demo.vrscene"
    scene_path.write_text(scene, encoding="utf-8")
    print(f"[ok] {scene_path}")
    print(f"     Linke Plane:  PNG  — {resources}")
    print(f"     Rechte Plane: TX   — {resources}")
    print(f"     Output:       {out_path}")


if __name__ == "__main__":
    main()
