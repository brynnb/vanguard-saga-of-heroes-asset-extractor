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
from scripts.lib.ue2_tagged_properties import (
    TYPE_BOOL,
    TYPE_STRUCT,
    decode_scalar,
    read_tagged_properties,
)

ASSETS = os.environ.get(
    "VANGUARD_ASSETS",
    os.environ.get("VANGUARD_ASSETS_PATH", config.ASSETS_PATH),
)
UEM_DIR = os.path.join(ASSETS, "Characters", "Meshes")
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output", "meshes", "characters")
TEXTURE_DIR = os.path.join(PROJECT_ROOT, "output", "textures")
MATERIAL_MANIFEST_PATH = os.path.join(PROJECT_ROOT, "output", "data", "material_manifest.json")

# These shipped character meshes are the complete humanoid set whose EMFXMesh
# exports omit `Item Template` entirely. Their sibling race/style exports,
# names, and hair materials all identify them as Hair Top meshes. Keep the
# recovery exact and auditable rather than letting consumers guess from
# arbitrary filenames. The two Idara records are NPC-specific; the rest are
# playable elf/orc styles.
RECOVERED_HAIR_TOP_EXPORTS = {
    "elf_M_char_hair_AB_DK_MidPartedLong1",
    "elf_M_char_hair_AB_DK_MidPartedLong2",
    "elf_M_char_hair_AB_DK_MidPartedLong3",
    "elf_M_char_hair_AB_DK_MidPartedLong4",
    "elf_M_char_hair_AB_DK_PulledBack1",
    "elf_M_char_hair_AB_DK_PulledBack2",
    "elf_M_char_hair_AB_DK_ScalpCutStr1",
    "elf_M_char_hair_AB_HG_Messy1",
    "elf_M_char_hair_AB_HG_Messy2",
    "elf_M_char_hair_AB_HG_Messy3",
    "elf_M_char_hair_AB_HG_MidPartedLong1",
    "elf_M_char_hair_AB_HG_MidPartedLong2",
    "elf_M_char_hair_AB_HG_MidPartedLong3",
    "elf_M_char_hair_AB_HG_MidPartedLong4",
    "elf_M_char_hair_AB_HG_PulledBack1",
    "elf_M_char_hair_AB_HG_ScalpCutStr1",
    "elf_M_char_hair_AB_WD_Messy1",
    "elf_M_char_hair_AB_WD_Messy3",
    "elf_M_char_hair_AB_WD_MidPartedLong3",
    "elf_M_char_hair_AB_WD_MidPartedLong4",
    "elf_M_char_hair_AB_WD_Short1",
    "elf_M_char_hair_AB_WD_WildWavy3",
    "human_F_hair_idara_0_C_0",
    "human_F_hair_idara_0_C_10",
    "orc_F_char_hairORTopLongUnkempt_100_C_0",
    "orc_M_char_hair_AB_Ponytail1",
}


def _hidden_by_layers(pkg, data, export_name=""):
    """Recover an EMFX mesh's authored modular-body occupancy layers."""
    top_properties = {
        prop.name: prop for prop in read_tagged_properties(data, pkg.names)
    }
    item_template = top_properties.get("Item Template")
    if (
        item_template is None
        or item_template.type_id != TYPE_STRUCT
        or item_template.struct_name != "ItemTemplate"
    ):
        return ["Hair Top"] if export_name in RECOVERED_HAIR_TOP_EXPORTS else []
    template_properties = {
        prop.name: prop
        for prop in read_tagged_properties(
            item_template.raw, pkg.names, require_terminator=False
        )
    }
    hidden_by = template_properties.get("Item Hidden By")
    if hidden_by is None:
        return []
    if hidden_by.type_id != TYPE_STRUCT or hidden_by.struct_name != "ItemHiddenBy":
        raise ValueError("Item Hidden By has an unexpected property schema")
    layers = []
    for prop in read_tagged_properties(
        hidden_by.raw, pkg.names, require_terminator=False
    ):
        if prop.type_id != TYPE_BOOL:
            raise ValueError(f"{prop.name} is not a BoolProperty")
        if bool(decode_scalar(prop, pkg.names)):
            layers.append(prop.name.removeprefix("HiddenBy "))
    return layers


def _manifest_entry_for_ref(material_manifest, shader_ref):
    if not material_manifest or not shader_ref:
        return None
    key = str(shader_ref).lower()
    for source_ref, entry in material_manifest.items():
        if str(source_ref).lower() == key:
            return entry
    object_name = key.rsplit(".", 1)[-1]
    matches = [
        entry
        for source_ref, entry in material_manifest.items()
        if str(source_ref).lower().rsplit(".", 1)[-1] == object_name
    ]
    return matches[0] if len(matches) == 1 else None


def _manifest_texture_url(material_manifest, shader_ref):
    entry = _manifest_entry_for_ref(material_manifest, shader_ref)
    if not entry:
        return None
    asset_path = (entry.get("base_color") or {}).get("asset_path")
    if not asset_path:
        return None
    full_path = asset_path if os.path.isabs(asset_path) else os.path.join(PROJECT_ROOT, asset_path)
    if os.path.exists(full_path):
        return "/" + os.path.relpath(full_path, PROJECT_ROOT).replace(os.sep, "/")
    return None


def _shader_map_entry(shader_map, shader_ref):
    if not shader_map or not shader_ref:
        return None
    key = str(shader_ref).lower()
    candidates = [key]
    if "." in key:
        candidates.append(key.rsplit(".", 1)[-1])
    for candidate in candidates:
        entry = shader_map.get(candidate)
        if entry is not None:
            return entry
    return None


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", help="Only export UEMs whose filename contains this string")
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Refresh recovered manifest metadata without rewriting glTF files",
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load shader→texture map for fallback resolution
    shader_map_path = os.path.join(PROJECT_ROOT, "output", "data", "shader_to_texture.json")
    shader_map = {}
    if os.path.exists(shader_map_path):
        with open(shader_map_path) as f:
            shader_map = json.load(f)
        print(f"Loaded shader map: {len(shader_map)} entries")

    material_manifest = {}
    if os.path.exists(MATERIAL_MANIFEST_PATH):
        with open(MATERIAL_MANIFEST_PATH) as f:
            material_manifest = json.load(f)
        print(f"Loaded material manifest: {len(material_manifest)} entries")

    uem_files = sorted(glob.glob(os.path.join(UEM_DIR, "UEM_*.uem")))
    print(f"Found {len(uem_files)} UEM_* files")

    manifest_path = os.path.join(OUTPUT_DIR, "manifest.json")
    if args.metadata_only:
        if not os.path.exists(manifest_path):
            raise FileNotFoundError(
                "--metadata-only requires an existing character manifest"
            )
        with open(manifest_path) as f:
            manifest = json.load(f)
        entries_by_identity = {}
        for entry in manifest:
            if not isinstance(entry, dict):
                continue
            identity = (entry.get("package"), entry.get("export"))
            entries_by_identity.setdefault(identity, []).append(entry)
        refreshed = 0
        for uem_path in uem_files:
            pkg_name = os.path.splitext(os.path.basename(uem_path))[0]
            if args.filter and args.filter not in pkg_name:
                continue
            pkg = UE2Package(uem_path)
            for exp in pkg.get_exports_by_class("EMFXMesh"):
                entries = entries_by_identity.get((pkg_name, exp["object_name"]), [])
                if not entries:
                    continue
                layers = _hidden_by_layers(
                    pkg, pkg.get_export_data(exp), exp["object_name"]
                )
                for entry in entries:
                    prior_layers = entry.get("hidden_by_layers", [])
                    entry["hidden_by_layers"] = list(
                        dict.fromkeys(list(prior_layers) + layers)
                    )
                refreshed += 1
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
        print(f"Refreshed recovered metadata for {refreshed} manifest entries")
        print(f"Manifest: {manifest_path}")
        return

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
                    # Package names are required to decode the authoritative
                    # socket array. Calling without them silently discarded all
                    # weapon, cloak, effect, eye, and mouth attachment points.
                    mesh = parse_emfxmesh_export(data, pkg.names)

                    if not mesh.submeshes:
                        total_skipped += 1
                        continue

                    total_v = sum(sm.num_vertices for sm in mesh.submeshes)
                    total_f = sum(len(sm.faces) for sm in mesh.submeshes)

                    if total_v == 0 or total_f == 0:
                        total_skipped += 1
                        continue

                    # Export to glTF
                    out_dir = os.path.join(OUTPUT_DIR, pkg_name)
                    os.makedirs(out_dir, exist_ok=True)
                    out_path = os.path.join(out_dir, f"{exp_name}.gltf")
                    skins_shaders = extract_skins_shaders(uem_path, exp_name, pkg=pkg)
                    export_gltf(mesh, out_path, texture_dir=TEXTURE_DIR, shader_map=shader_map,
                                pkg_name=pkg_name, skins_shaders=skins_shaders,
                                material_manifest=material_manifest)

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
                                if s_name:
                                    tex_url = _manifest_texture_url(material_manifest, s_name)
                                if s_name and tex_url is None and shader_map:
                                    entry = _shader_map_entry(shader_map, s_name)
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
                        "sockets": [
                            {
                                "alias": socket.attach_alias,
                                "bone": socket.bone_name,
                                "emfx_node": socket.emfx_node,
                                "rotation_degrees": list(socket.rotation),
                                "translation": list(socket.translation),
                                "test_scale": socket.test_scale,
                            }
                            for socket in mesh.sockets
                        ],
                        "uv_sets": mesh.num_uv_sets,
                        "materials": [m.name for m in mesh.materials],
                        "color_variants": color_variants,
                        "hidden_by_layers": _hidden_by_layers(pkg, data, exp_name),
                    })
                    total_exported += 1

                except Exception as e:
                    total_failed += 1

        except Exception as e:
            total_failed += 1

    # Write manifest. Filtered runs are useful for small probes; merge their
    # results into the existing manifest instead of replacing the full export.
    if args.filter and os.path.exists(manifest_path):
        try:
            with open(manifest_path) as f:
                existing_manifest = json.load(f)
        except Exception:
            existing_manifest = []

        replacement_packages = {entry["package"] for entry in manifest}
        if replacement_packages and isinstance(existing_manifest, list):
            manifest = [
                entry for entry in existing_manifest
                if entry.get("package") not in replacement_packages
            ] + manifest
        elif isinstance(existing_manifest, list):
            manifest = existing_manifest

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
