#!/usr/bin/env python3
"""
Batch export animation glTF files for all UKX packages that contain
both a MeshAnimation and a SkeletalMesh with parseable RefSkeleton.

Output goes to output/meshes/animations/<package_name>/<anim_name>.gltf
Also generates output/meshes/animations/manifest.json mapping package → animation files.
"""
import os, json, glob, traceback

from vanguard_assets import config
from ue2.package import UE2Package
from scripts.lib.vanguard_meshanim import parse_mesh_animation, find_refskeleton, export_animation_gltf

ROOT = str(config.PROJECT_ROOT)
UKX_DIR = os.path.join(config.ASSETS_PATH, "SkeletalMeshes")
OUT_DIR = os.path.join(ROOT, "output/meshes/animations")
os.makedirs(OUT_DIR, exist_ok=True)

ukx_files = sorted(glob.glob(os.path.join(UKX_DIR, "*.ukx")))
print(f"Found {len(ukx_files)} .ukx files")

manifest = {}  # package_name -> {anims: [{name, path, bones, seqs, clips}], skeleton_bones: N}
total_exported = 0
total_failed = 0
total_no_anim = 0
total_no_skel = 0

for ukx_path in ukx_files:
    pkg_name = os.path.splitext(os.path.basename(ukx_path))[0]
    
    try:
        pkg = UE2Package(ukx_path)
    except Exception:
        continue
    
    # Collect all MeshAnimation and SkeletalMesh exports
    mesh_anims = []
    skel_meshes = []
    for exp in pkg.exports:
        cn = exp.get("class_name", "")
        if cn == "MeshAnimation":
            mesh_anims.append(exp)
        elif cn == "SkeletalMesh":
            skel_meshes.append(exp)
    
    if not mesh_anims:
        total_no_anim += 1
        continue
    
    # Try to get RefSkeleton from any SkeletalMesh (prefer the largest one)
    bind_bones = None
    for sm_exp in sorted(skel_meshes, key=lambda e: e.get("serial_size", 0), reverse=True):
        try:
            bind_bones = find_refskeleton(pkg.get_export_data(sm_exp), pkg.names)
            if bind_bones:
                break
        except Exception:
            continue
    
    if not bind_bones:
        total_no_skel += 1
    
    pkg_out_dir = os.path.join(OUT_DIR, pkg_name)
    os.makedirs(pkg_out_dir, exist_ok=True)
    
    pkg_manifest = {"anims": [], "skeleton_bones": len(bind_bones) if bind_bones else 0}
    
    for ma_exp in mesh_anims:
        anim_name = ma_exp.get("object_name", "unknown")
        try:
            anim_data = parse_mesh_animation(pkg.get_export_data(ma_exp), pkg.names, pkg.version)
        except Exception as e:
            total_failed += 1
            continue
        
        if not anim_data["anim_seqs"]:
            continue
        
        # Use bind_bones if available, otherwise use anim ref_bones as fallback
        bones_for_export = bind_bones if bind_bones else anim_data["ref_bones"]
        
        out_path = os.path.join(pkg_out_dir, f"{anim_name}.gltf")
        try:
            gltf = export_animation_gltf(anim_data, bones_for_export, out_path)
            clip_info = []
            for a in gltf.get("animations", []):
                clip_info.append({"name": a["name"], "channels": len(a["channels"])})
            
            pkg_manifest["anims"].append({
                "name": anim_name,
                "path": f"{pkg_name}/{anim_name}.gltf",
                "bones": len(anim_data["ref_bones"]),
                "clips": clip_info,
            })
            total_exported += 1
        except Exception as e:
            total_failed += 1
            traceback.print_exc()
    
    if pkg_manifest["anims"]:
        manifest[pkg_name] = pkg_manifest

# Write manifest
manifest_path = os.path.join(OUT_DIR, "manifest.json")
with open(manifest_path, "w") as f:
    json.dump(manifest, f, indent=2)

print(f"\n{'='*60}")
print(f"Batch export complete:")
print(f"  Exported:    {total_exported} animation files")
print(f"  Failed:      {total_failed}")
print(f"  No anim:     {total_no_anim} packages had no MeshAnimation")
print(f"  No skeleton: {total_no_skel} packages had no parseable RefSkeleton")
print(f"  Packages with anims: {len(manifest)}")
print(f"  Manifest: {manifest_path}")

# Summary of what we got
total_clips = sum(
    len(clip) 
    for pkg_info in manifest.values() 
    for anim in pkg_info["anims"]
    for clip in [anim["clips"]]
)
print(f"  Total animation clips: {total_clips}")
