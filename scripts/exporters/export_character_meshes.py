#!/usr/bin/env python3
"""
Bulk export all UEM character meshes to glTF format for the character viewer.

Exports each EMFXMesh export from every UEM_*.uem file to:
  output/meshes/characters/<package_name>/<export_name>.gltf

Also produces a manifest JSON at output/meshes/characters/manifest.json
listing all exported meshes with metadata.
"""
import os
import sys
import glob
import json
import time

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "lib"))

import config
from ue2.package import UE2Package
from vanguard_emfxmesh import parse_emfxmesh_export, export_gltf, extract_skins_shaders
from vanguard_emfxanim import parse_emfxanim_export
from ue2_property_reader import BinaryReader, read_ue2_properties, decode_animset_names

ASSETS = os.environ.get(
    "VANGUARD_ASSETS",
    os.environ.get("VANGUARD_ASSETS_PATH", config.ASSETS_PATH),
)
UEM_DIR = os.path.join(ASSETS, "Characters", "Meshes")
ANIM_DIR = os.path.join(ASSETS, "Characters", "Animations")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output", "meshes", "characters")
TEXTURE_DIR = os.path.join(PROJECT_ROOT, "output", "textures")


def _load_anim_bind_poses(uem_path):
    """Load animation bind poses for a UEM mesh package.

    Reads the AnimSet property to find the UEA package, loads the first
    animation clip, and returns per-bone bind_pose_rot and bind_pose_pos
    dicts from the FXM submotions.

    Returns (bind_rots, bind_pos) or (None, None).
    """
    try:
        pkg = UE2Package(uem_path)
    except Exception:
        return None, None

    # Find AnimSet references
    uea_names = []
    for exp in pkg.exports:
        obj_name = exp.get("object_name", "")
        if "SKELETON" not in obj_name:
            continue
        try:
            data = pkg.get_export_data(exp)
            reader = BinaryReader(data, 0)
            props = read_ue2_properties(reader, pkg.names)
            animset_raw = props.get("AnimSet")
            if animset_raw and isinstance(animset_raw, (bytes, bytearray)):
                uea_names = decode_animset_names(animset_raw, pkg.names)
                break
        except Exception:
            continue

    if not uea_names:
        return None, None

    # Load first available UEA and extract bind poses from first clip
    for uea_name in uea_names:
        uea_path = os.path.join(ANIM_DIR, uea_name + ".uea")
        if not os.path.exists(uea_path):
            uea_path = os.path.join(UEM_DIR, uea_name + ".uea")
        if not os.path.exists(uea_path):
            continue
        try:
            apkg = UE2Package(uea_path)
            for aexp in apkg.exports:
                if aexp.get("class_name") != "EMFXAnim":
                    continue
                adata = apkg.get_export_data(aexp)
                anim = parse_emfxanim_export(adata)
                if not anim.submotions:
                    continue
                bind_rots = {}
                bind_pos = {}
                for sm in anim.submotions:
                    bind_rots[sm.name] = sm.bind_pose_rot
                    bind_pos[sm.name] = sm.bind_pose_pos
                return bind_rots, bind_pos
        except Exception:
            continue

    return None, None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", help="Only export UEMs whose filename contains this string")
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load shader→texture map for fallback resolution
    shader_map_path = os.path.join(PROJECT_ROOT, "output", "data", "shader_to_texture.json")
    shader_map = {}
    if os.path.exists(shader_map_path):
        with open(shader_map_path) as f:
            shader_map = json.load(f)
        print(f"Loaded shader map: {len(shader_map)} entries")

    uem_files = sorted(glob.glob(os.path.join(UEM_DIR, "UEM_*.uem")))
    print(f"Found {len(uem_files)} UEM_* files")

    # Pre-load animation bind poses for IBM correction
    anim_bind_cache = {}  # uem_path -> (bind_rots, bind_pos)

    manifest = []
    total_exported = 0
    total_skipped = 0
    total_failed = 0
    t0 = time.time()

    for uem_path in uem_files:
        pkg_name = os.path.splitext(os.path.basename(uem_path))[0]
        if args.filter and args.filter not in pkg_name:
            continue
        try:
            pkg = UE2Package(uem_path)
            exports = pkg.get_exports_by_class("EMFXMesh")
            if not exports:
                continue

            for exp in exports:
                exp_name = exp["object_name"]
                try:
                    data = pkg.get_export_data(exp)
                    mesh = parse_emfxmesh_export(data)

                    if not mesh.submeshes:
                        total_skipped += 1
                        continue

                    total_v = sum(sm.num_vertices for sm in mesh.submeshes)
                    total_f = sum(len(sm.faces) for sm in mesh.submeshes)

                    if total_v == 0 or total_f == 0:
                        total_skipped += 1
                        continue

                    # Load animation bind poses for this mesh (cached)
                    if uem_path not in anim_bind_cache:
                        anim_bind_cache[uem_path] = _load_anim_bind_poses(uem_path)
                    anim_rots, anim_pos = anim_bind_cache[uem_path]

                    # Export to glTF
                    out_dir = os.path.join(OUTPUT_DIR, pkg_name)
                    os.makedirs(out_dir, exist_ok=True)
                    out_path = os.path.join(out_dir, f"{exp_name}.gltf")
                    skins_shaders = extract_skins_shaders(uem_path, exp_name, pkg=pkg)
                    export_gltf(mesh, out_path, texture_dir=TEXTURE_DIR, shader_map=shader_map,
                                bind_rot_overrides=anim_rots, bind_pos_overrides=anim_pos,
                                pkg_name=pkg_name, skins_shaders=skins_shaders)

                    # Detect color variants: if skins_shaders lists more shaders than
                    # material slots, the extra entries are texture variants for slot 0.
                    # Build: color_variants[i][j] = texture URL for slot j in variant i.
                    num_slots = len({sm.material_index for sm in mesh.submeshes})
                    color_variants = None
                    if skins_shaders and len(skins_shaders) > num_slots:
                        variants = []
                        for start in range(0, len(skins_shaders), num_slots):
                            var = []
                            for si in range(num_slots):
                                idx = start + si
                                s_name = skins_shaders[idx] if idx < len(skins_shaders) else None
                                tex_url = None
                                if s_name and shader_map:
                                    entry = shader_map.get(s_name.lower())
                                    if entry:
                                        tex = entry.get("texture") if isinstance(entry, dict) else (
                                            entry if isinstance(entry, str) and not entry.startswith("color:") else None)
                                        if tex:
                                            full = os.path.join(TEXTURE_DIR, tex + ".png")
                                            if os.path.exists(full):
                                                tex_url = f"/output/textures/{tex}.png"
                                var.append(tex_url)
                            variants.append(var)
                        if len(variants) > 1 and any(any(v) for v in variants):
                            color_variants = variants

                    manifest.append({
                        "package": pkg_name,
                        "export": exp_name,
                        "path": f"{pkg_name}/{exp_name}.gltf",
                        "vertices": total_v,
                        "faces": total_f,
                        "submeshes": len(mesh.submeshes),
                        "bones": len(mesh.nodes),
                        "uv_sets": mesh.num_uv_sets,
                        "materials": [m.name for m in mesh.materials],
                        "color_variants": color_variants,
                    })
                    total_exported += 1

                except Exception as e:
                    total_failed += 1

        except Exception as e:
            total_failed += 1

    # Write manifest
    manifest_path = os.path.join(OUTPUT_DIR, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.1f}s")
    print(f"  Exported: {total_exported}")
    print(f"  Skipped (no mesh): {total_skipped}")
    print(f"  Failed: {total_failed}")
    print(f"  Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
