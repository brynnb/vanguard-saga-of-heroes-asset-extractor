#!/usr/bin/env python3
"""
Batch export EMotion FX (FXM) animations from .uea packages as glTF files.

For each .uem mesh package, reads the AnimSet property from its _SKELETON
export to discover which .uea animation packages it references. Then parses
each .uea package and exports every EMFXAnim clip as a standalone glTF file
with FXA bone names (matching the mesh skeleton exactly).

Output:
  output/meshes/emfx_animations/<uea_package>/<clip_name>.gltf
  output/meshes/emfx_animations/manifest.json

The manifest maps mesh package names to their animation files.
"""
import os, sys, json, glob, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts", "lib"))

import config
from ue2.package import UE2Package
from ue2_property_reader import BinaryReader, read_ue2_properties, decode_animset_names
from vanguard_emfxanim import parse_emfxanim_export, export_emfxanim_gltf
from vanguard_emfxmesh import parse_emfxmesh_export

MESH_DIR = os.path.join(config.ASSETS_PATH, "Characters", "Meshes")
ANIM_DIR = os.path.join(config.ASSETS_PATH, "Characters", "Animations")
OUT_DIR = os.path.join(ROOT, "output", "meshes", "emfx_animations")
os.makedirs(OUT_DIR, exist_ok=True)


def extract_animset_from_uem(uem_path):
    """Extract AnimSet package names from a .uem file's SKELETON export."""
    try:
        pkg = UE2Package(uem_path)
    except Exception:
        return []

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
                return decode_animset_names(animset_raw, pkg.names)
        except Exception:
            continue

    return []


def extract_mesh_bind_rotations(uem_path):
    """Extract per-bone bind rotations from a .uem mesh package.

    Returns dict {bone_name: (qx, qy, qz, qw)} or None on failure.
    """
    try:
        pkg = UE2Package(uem_path)
    except Exception:
        return None

    for exp in pkg.exports:
        class_name = exp.get("class_name", "")
        if class_name != "EMFXMesh":
            continue
        try:
            data = pkg.get_export_data(exp)
            mesh = parse_emfxmesh_export(data)
            if mesh and mesh.nodes:
                return {node.name: node.rotation for node in mesh.nodes}
        except Exception:
            continue
    return None


def extract_mesh_bind_positions(uem_path):
    """Extract per-bone bind positions from a .uem mesh package.

    Returns dict {bone_name: (px, py, pz)} or None on failure.
    """
    try:
        pkg = UE2Package(uem_path)
    except Exception:
        return None

    for exp in pkg.exports:
        class_name = exp.get("class_name", "")
        if class_name != "EMFXMesh":
            continue
        try:
            data = pkg.get_export_data(exp)
            mesh = parse_emfxmesh_export(data)
            if mesh and mesh.nodes:
                return {node.name: node.position for node in mesh.nodes}
        except Exception:
            continue
    return None


def main():
    # --- Phase 1: Scan all .uem files to build mesh → UEA mapping ---
    uem_files = sorted(glob.glob(os.path.join(MESH_DIR, "*.uem")))
    print(f"Scanning {len(uem_files)} .uem files for AnimSet references...")

    mesh_to_uea = {}  # mesh_pkg_name -> [uea_pkg_names]
    all_uea_names = set()

    for uem_path in uem_files:
        pkg_name = os.path.splitext(os.path.basename(uem_path))[0]
        uea_names = extract_animset_from_uem(uem_path)
        if uea_names:
            mesh_to_uea[pkg_name] = uea_names
            all_uea_names.update(uea_names)

    print(f"  {len(mesh_to_uea)} meshes reference {len(all_uea_names)} unique UEA packages")

    # --- Phase 2: Locate .uea files on disk ---
    uea_files_on_disk = {}  # uea_pkg_name -> filepath
    for uea_path in glob.glob(os.path.join(ANIM_DIR, "*.uea")):
        name = os.path.splitext(os.path.basename(uea_path))[0]
        uea_files_on_disk[name] = uea_path

    # Also check MESH_DIR in case some .uea are co-located
    for uea_path in glob.glob(os.path.join(MESH_DIR, "*.uea")):
        name = os.path.splitext(os.path.basename(uea_path))[0]
        uea_files_on_disk.setdefault(name, uea_path)

    found = all_uea_names & set(uea_files_on_disk.keys())
    missing = all_uea_names - found
    print(f"  Found {len(found)}/{len(all_uea_names)} UEA files on disk")
    if missing:
        print(f"  Missing: {sorted(missing)[:10]}{'...' if len(missing) > 10 else ''}")

    # --- Phase 2b: Load mesh bind poses for bind-pose correction ---
    # Map each UEA package to the bind rotations/positions from its first referencing mesh.
    uea_to_bind_rots = {}  # uea_pkg_name -> {bone_name: (qx,qy,qz,qw)}
    uea_to_bind_pos = {}   # uea_pkg_name -> {bone_name: (px,py,pz)}
    mesh_bind_cache = {}   # uem_path -> (rots, pos)

    for mesh_name, uea_names in mesh_to_uea.items():
        uem_path = os.path.join(MESH_DIR, mesh_name + ".uem")
        if uem_path not in mesh_bind_cache:
            mesh_bind_cache[uem_path] = (
                extract_mesh_bind_rotations(uem_path),
                extract_mesh_bind_positions(uem_path),
            )
        bind_rots, bind_pos = mesh_bind_cache[uem_path]
        if bind_rots:
            for uea_name in uea_names:
                uea_to_bind_rots.setdefault(uea_name, bind_rots)
        if bind_pos:
            for uea_name in uea_names:
                uea_to_bind_pos.setdefault(uea_name, bind_pos)

    print(f"  Loaded bind rotations for {len(uea_to_bind_rots)} UEA packages")
    print(f"  Loaded bind positions for {len(uea_to_bind_pos)} UEA packages")

    # --- Phase 3: Parse and export each UEA package ---
    manifest = {}  # mesh_pkg -> {uea_packages: [{name, clips: [{name, path}]}]}
    total_exported = 0
    total_failed = 0
    exported_uea = {}  # uea_name -> [{clip_name, rel_path}]

    for uea_name in sorted(found):
        uea_path = uea_files_on_disk[uea_name]
        try:
            pkg = UE2Package(uea_path)
        except Exception as e:
            print(f"  SKIP {uea_name}: failed to open package: {e}")
            total_failed += 1
            continue

        pkg_out_dir = os.path.join(OUT_DIR, uea_name)
        clip_entries = []
        bind_rots = uea_to_bind_rots.get(uea_name)
        bind_pos = uea_to_bind_pos.get(uea_name)

        for exp in pkg.exports:
            class_name = exp.get("class_name", "")
            if class_name != "EMFXAnim":
                continue

            obj_name = exp.get("object_name", "unknown")
            try:
                data = pkg.get_export_data(exp)
                anim = parse_emfxanim_export(data)
                if not anim.submotions:
                    continue

                out_path = os.path.join(pkg_out_dir, f"{obj_name}.gltf")
                result = export_emfxanim_gltf(anim, obj_name, out_path, mesh_bind_rotations=bind_rots, mesh_bind_positions=bind_pos)
                if result:
                    rel_path = f"{uea_name}/{obj_name}.gltf"
                    clip_entries.append({"name": obj_name, "path": rel_path,
                                        "bones": len(anim.submotions),
                                        "duration": anim.duration})
                    total_exported += 1
                else:
                    total_failed += 1
            except Exception as e:
                total_failed += 1
                traceback.print_exc()

        if clip_entries:
            exported_uea[uea_name] = clip_entries

    # --- Phase 4: Build manifest ---
    for mesh_name, uea_names in mesh_to_uea.items():
        uea_entries = []
        for uea_name in uea_names:
            clips = exported_uea.get(uea_name, [])
            if clips:
                uea_entries.append({"package": uea_name, "clips": clips})
        if uea_entries:
            manifest[mesh_name] = {"uea_packages": uea_entries}

    manifest_path = os.path.join(OUT_DIR, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\n{'='*60}")
    print(f"Batch export complete:")
    print(f"  Exported:       {total_exported} animation clips")
    print(f"  Failed:         {total_failed}")
    print(f"  Meshes mapped:  {len(manifest)}")
    print(f"  UEA packages:   {len(exported_uea)}")
    print(f"  Manifest:       {manifest_path}")


if __name__ == "__main__":
    main()
