#!/usr/bin/env python3
"""
Build legacy shader_to_texture.json compatibility data.

New material consumers should use output/data/material_manifest.json. This script
still supports the older shader_to_texture.json contract for migration and can
project that compatibility map from the canonical manifest.

The legacy scanner follows Shader.Diffuse through Combiner/TexModifier chains to
find one Texture object, then extracts it to PNG.

Uses Unreal-Library batch decompilation (one subprocess per package) and the
native Python texture parser for PNG extraction.

Output:
  - output/data/shader_to_texture.json
      compatibility shader_name → resolved diffuse texture/color mapping.
      Projection entries keep package-qualified texture refs and collision-safe
      emitted asset names from material_manifest.json.
  - output/data/character_shader_materials.json
      parsed character Shader material property refs, including TintAlpha and
      TintPalette targets that are not safe to collapse to diffuse CLR
  - output/textures/<texture_name>.png
      extracted diffuse and optional character helper textures
"""

import os
import sys
import re
import json
import subprocess
import time

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts/lib"))

import config
from ue2 import UE2Package
from ue2.properties import find_property_start, parse_properties
from ue2.texture import Texture as UE2Texture

TEXTURES_DIR = os.path.join(config.ASSETS_PATH, "Textures")
OUTPUT_MAP = os.path.join(config.DATA_DIR, "shader_to_texture.json")
MATERIAL_MANIFEST = os.path.join(config.DATA_DIR, "material_manifest.json")
OUTPUT_SHADER_MATERIALS = os.path.join(
    config.DATA_DIR, "character_shader_materials.json"
)
OUTPUT_TEXTURES_DIR = os.path.join(PROJECT_ROOT, "output", "textures")

# Unreal-Library paths
DOTNET = config.DOTNET
UEL_DLL = config.UNREAL_LIBRARY_DLL


def _texture_ref(ref_path):
    """Return package-qualified texture metadata from a UELib object path."""
    parts = str(ref_path or "").split(".")
    texture_name = parts[-1] if parts else str(ref_path or "")
    package_name = parts[0] if len(parts) > 1 else ""
    return {
        "texture": texture_name,
        "texture_ref": ref_path,
        "texture_package": package_name,
    }


def _project_shader_map_from_manifest(manifest):
    shader_map = {}
    for source_ref, entry in manifest.items():
        source_key = str(source_ref).lower()
        bare_key = source_key.rsplit(".", 1)[-1]
        base_color = entry.get("base_color") or {}
        texture_name = base_color.get("asset_name") or base_color.get("texture_name")
        color_factor = base_color.get("color_factor")
        color_text = None
        if not texture_name and color_factor:
            color_values = list(color_factor)[:3]
            color_text = ",".join(f"{float(value):.6g}" for value in color_values)
            texture_name = f"color:{color_text}"
        if not texture_name:
            continue
        item = {
            "texture": texture_name,
            "texture_ref": base_color.get("texture_ref"),
            "texture_package": base_color.get("texture_package"),
            "texture_name": base_color.get("texture_name"),
            "asset_name": base_color.get("asset_name"),
            "asset_path": base_color.get("asset_path"),
            "source_ref": source_ref,
            "deprecated": True,
        }
        if color_text is not None:
            item["color"] = color_text
        if str(entry.get("alpha_mode") or "").upper() == "MASK":
            item["alpha"] = "mask"
        if entry.get("two_sided"):
            item["two_sided"] = True
        shader_map[source_key] = item
        shader_map.setdefault(bare_key, item)
    return shader_map


def run_uel(pkg_path, cmd):
    """Run Unreal-Library CLI command."""
    try:
        result = subprocess.run(
            [DOTNET, "--roll-forward", "Major", UEL_DLL, pkg_path, cmd],
            capture_output=True,
            text=True,
            input="\n",
            timeout=60,
        )
        return result.stdout
    except (subprocess.TimeoutExpired, Exception):
        return ""


def get_uel_objects(pkg_path):
    """Get all objects in a package via Unreal-Library."""
    stdout = run_uel(pkg_path, "obj list")
    objects = []
    for line in stdout.splitlines():
        m = re.match(r"(\w+)'(.+)'", line.strip())
        if m:
            objects.append((m.group(1), m.group(2)))
    return objects


def decompile_uel(pkg_path, obj_path):
    """Decompile a single object via Unreal-Library."""
    stdout = run_uel(pkg_path, f"obj decompile {obj_path}")
    lines = stdout.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("// Reference:") or line.strip().startswith(
            "begin object"
        ):
            start = i
            break
    return "\n".join(lines[start:])


def _find_texture_recursive(pkg_path, obj_path, visited=None, depth=0):
    """Recursively follow material chain to find a Texture, skipping ConstantColor dead ends.
    Returns (texture_name, constant_color, texture_ref) where texture_ref preserves
    the package-qualified source texture path when available.
    """
    if visited is None:
        visited = set()
    if obj_path in visited or depth > 10:
        return None, None, None
    visited.add(obj_path)

    dec = decompile_uel(pkg_path, obj_path)
    if not dec:
        return None, None, None

    # Check for ConstantColor — extract RGB as fallback
    if "class=Engine.ConstantColor" in dec:
        color_m = re.search(r"Color=\(R=(\d+),G=(\d+),B=(\d+)", dec)
        if color_m:
            r, g, b = (
                int(color_m.group(1)),
                int(color_m.group(2)),
                int(color_m.group(3)),
            )
            return None, [r / 255.0, g / 255.0, b / 255.0], None
        return None, None, None

    is_combiner = "class=Engine.Combiner" in dec

    if is_combiner:
        # UE2 Combiner is a composed material. The flat shader map can only keep
        # one legacy diffuse name, so use declared property order and preserve
        # the package-qualified texture ref for extraction.
        for mat_ref in (
            re.search(r"Material1=(\w+)'([^']+)'", dec),
            re.search(r"Material2=(\w+)'([^']+)'", dec),
        ):
            if not mat_ref:
                continue
            ref_cls, ref_path = mat_ref.group(1), mat_ref.group(2)
            if ref_cls == "Texture":
                texture_ref = _texture_ref(ref_path)
                return texture_ref["texture"], None, texture_ref
            else:
                tex, color, texture_ref = _find_texture_recursive(
                    pkg_path, ref_path, set(visited), depth + 1
                )
                if tex or color:
                    return tex, color, texture_ref

        return None, None, None
    else:
        # TexModifier/TexScaler/etc — follow Material or Diffuse property
        refs = list(re.finditer(r"(?:Material|Diffuse)=(\w+)'([^']+)'", dec))
        for ref_m in refs:
            ref_cls, ref_path = ref_m.group(1), ref_m.group(2)
            if ref_cls == "Texture":
                texture_ref = _texture_ref(ref_path)
                return texture_ref["texture"], None, texture_ref
            tex, color, texture_ref = _find_texture_recursive(
                pkg_path, ref_path, visited, depth + 1
            )
            if tex:
                return tex, color, texture_ref
        return None, None, None


def follow_diffuse_chain(pkg_path, shader_path):
    """Follow Shader.Diffuse through Combiner/TexModifier chains to find the base Texture.
    Returns (texture_name, constant_color, shader_props) where shader_props is a dict
    with 'blending' and 'opacity' keys extracted from the Shader's OutputBlending/Opacity.
    """
    decompiled = decompile_uel(pkg_path, shader_path)

    # Extract shader-level properties for alpha handling
    shader_props = {}
    ob_m = re.search(r"OutputBlending=(\w+)", decompiled)
    if ob_m:
        shader_props["blending"] = ob_m.group(1)
    else:
        # Check for numeric enum value (e.g. OutputBlending=1 means OB_Masked)
        ob_num = re.search(r"OutputBlending=(\d+)", decompiled)
        if ob_num:
            blending_names = [
                "OB_Normal",
                "OB_Masked",
                "OB_Modulate",
                "OB_Translucent",
                "OB_Invisible",
                "OB_Brighten",
                "OB_Darken",
            ]
            idx = int(ob_num.group(1))
            shader_props["blending"] = (
                blending_names[idx] if idx < len(blending_names) else f"OB_{idx}"
            )

    op_m = re.search(r"Opacity=(\w+)'([^']+)'", decompiled)
    if op_m:
        shader_props["has_opacity"] = True

    ts_m = re.search(r"TwoSided=true", decompiled, re.IGNORECASE)
    if ts_m:
        shader_props["two_sided"] = True

    # Extract Diffuse reference
    m = re.search(r"Diffuse=(\w+)'([^']+)'", decompiled)
    if not m:
        return None, None, shader_props

    diff_cls, diff_path = m.group(1), m.group(2)
    if diff_cls == "Texture":
        texture_ref = _texture_ref(diff_path)
        shader_props.update(texture_ref)
        return texture_ref["texture"], None, shader_props
    if diff_cls == "ConstantColor":
        dec = decompile_uel(pkg_path, diff_path)
        color_m = re.search(r"Color=\(R=(\d+),G=(\d+),B=(\d+)", dec)
        if color_m:
            r, g, b = (
                int(color_m.group(1)),
                int(color_m.group(2)),
                int(color_m.group(3)),
            )
            return None, [r / 255.0, g / 255.0, b / 255.0], shader_props
        return None, None, shader_props

    if diff_cls == "WaterShaderMaterial":
        dec = decompile_uel(pkg_path, diff_path)
        color_m = re.search(r"WaterColor=\(R=(\d+),G=(\d+),B=(\d+)", dec)
        if color_m:
            r, g, b = (
                int(color_m.group(1)),
                int(color_m.group(2)),
                int(color_m.group(3)),
            )
        else:
            r, g, b = 131, 169, 194  # default water teal
        shader_props["is_water"] = True
        # TwoSided=False: all Vanguard Shader wrapper objects have TwoSided=False
        # (confirmed by binary inspection of p0001_water_shaders.utx Shader exports)
        shader_props["two_sided"] = False
        return None, [r / 255.0, g / 255.0, b / 255.0], shader_props

    tex, color, texture_ref = _find_texture_recursive(pkg_path, diff_path)
    if texture_ref:
        shader_props.update(texture_ref)
    return tex, color, shader_props


def _is_soft_alpha_material(shader_name, texture_name):
    classifier = f"{shader_name or ''} {texture_name or ''}".lower()
    compact = classifier.replace("_", "").replace("-", "")
    return "eyelash" in compact


def load_palette_for_texture(pkg, texture_export):
    """Load the 256-entry RGBA palette for a TEXF_P8 (format_id=0) texture export.

    The Palette property stores an object reference (1-byte index into exports).
    Returns a list of 256 (R,G,B,A) tuples, or None if not found/applicable.
    """
    import struct as _struct
    from ue2.properties import read_compact_index as _rci

    try:
        data = pkg.get_export_data(texture_export)
        # Quick check: format byte is in the properties — parse to find the Palette property value
        # The Palette property value is a 1-byte export-table index
        # We already parsed this via UE2Texture.properties['Palette']
        from ue2.texture import Texture as _Tex

        tex_obj = _Tex(data, pkg.names)
        if tex_obj.format_id != 0:
            return None
        palette_ref = tex_obj.properties.get("Palette")
        if palette_ref is None:
            return None
        # palette_ref is a 1-based export index (stored as raw byte value from ObjectProperty)
        # In our property parser ObjectProperty size=1 gives a single byte → export index
        pal_export_idx = int(palette_ref) - 1  # convert 1-based to 0-based
        if not (0 <= pal_export_idx < len(pkg.exports)):
            return None
        pal_exp = pkg.exports[pal_export_idx]
        if pal_exp["class_name"] != "Palette":
            return None
        pal_data = pkg.get_export_data(pal_exp)
        # UPalette: byte 0 = None terminator (no properties), then int32 count (Vanguard ReadLength),
        # then count × 4-byte RGBA entries
        if len(pal_data) < 5:
            return None
        count = _struct.unpack("<i", pal_data[1:5])[0]
        if count != 256 or len(pal_data) < 5 + 256 * 4:
            return None
        palette = []
        for i in range(256):
            off = 5 + i * 4
            r, g, b, a = (
                pal_data[off],
                pal_data[off + 1],
                pal_data[off + 2],
                pal_data[off + 3],
            )
            palette.append((r, g, b, a))
        return palette
    except Exception:
        return None


def extract_texture_png(pkg, texture_name, output_dir):
    """Extract a texture from a UE2 package to PNG."""
    for exp in pkg.exports:
        if (
            exp["class_name"] == "Texture"
            and exp["object_name"].lower() == texture_name.lower()
        ):
            try:
                data = pkg.get_export_data(exp)
                palette = load_palette_for_texture(pkg, exp)
                tex = UE2Texture(data, pkg.names, palette=palette)
                if tex.mips:
                    img = tex.get_image(0)
                    if img:
                        out_path = os.path.join(output_dir, f"{exp['object_name']}.png")
                        img.save(out_path)
                        return out_path
            except Exception as e:
                pass
    return None


def _is_char_texture(export):
    name = export.get("object_name", "")
    return (
        export.get("class_name") == "Texture"
        and "_char_" in name
        and re.search(r"_(CLR\d*|TNT|TNTA)$", name, re.IGNORECASE)
    )


def _import_package_path(pkg, import_index):
    """Return import outer-chain object names from root to leaf."""
    names = []
    seen = set()
    idx = import_index
    while 0 <= idx < len(pkg.imports) and idx not in seen:
        seen.add(idx)
        imp = pkg.imports[idx]
        names.append(str(imp.get("object_name", "")))
        outer = int(imp.get("package", 0) or 0)
        if outer < 0:
            idx = -outer - 1
            continue
        break
    return list(reversed([name for name in names if name]))


def _resolve_object_ref(pkg, value):
    try:
        ref = int(value)
    except (TypeError, ValueError):
        return {"raw": value}

    if ref > 0:
        idx = ref - 1
        if 0 <= idx < len(pkg.exports):
            exp = pkg.exports[idx]
            return {
                "raw": ref,
                "kind": "export",
                "class": exp.get("class_name"),
                "name": exp.get("object_name"),
            }
    if ref < 0:
        idx = -ref - 1
        if 0 <= idx < len(pkg.imports):
            imp = pkg.imports[idx]
            path = _import_package_path(pkg, idx)
            package_name = path[0] if path else ""
            return {
                "raw": ref,
                "kind": "import",
                "class": imp.get("class_name"),
                "name": imp.get("object_name"),
                "package_path": path,
                "source_package": package_name,
            }
    return {"raw": ref, "kind": "unresolved"}


def _parse_shader_material(pkg, shader_export):
    name = str(shader_export.get("object_name", ""))
    try:
        data = pkg.get_export_data(shader_export)
        start = find_property_start(data, pkg.names, max_search=2)
        props = parse_properties(data, pkg.names, start if start >= 0 else 0)
    except Exception as exc:
        return {
            "name": name,
            "error": str(exc),
        }

    material = {
        "name": name,
        "properties": [],
    }
    for prop in props:
        prop_name = str(prop.get("name", ""))
        value = prop.get("value")
        entry = {
            "name": prop_name,
            "type": prop.get("type"),
            "value": value,
        }
        if prop.get("type") == "Object":
            entry["target"] = _resolve_object_ref(pkg, value)
        material["properties"].append(entry)

        key_by_prop = {
            "Diffuse": "diffuse",
            "Opacity": "opacity",
            "Specular": "specular",
            "TintAlpha": "tint_alpha",
            "TintPalette": "tint_palette",
        }.get(prop_name)
        if key_by_prop:
            material[key_by_prop] = entry.get("target", {"raw": value})
        elif prop_name in (
            "DiffuseStrength",
            "SpecularStrength",
            "TintScaleA",
            "TintScaleB",
        ):
            material[_camel_to_snake(prop_name)] = value
        elif prop_name in ("OutputBlending", "TwoSided"):
            material[_camel_to_snake(prop_name)] = value
    return material


def _camel_to_snake(value):
    return re.sub(r"(?<!^)([A-Z])", r"_\1", value).lower()


def extract_unreferenced_char_textures(
    pkg,
    extracted_textures,
    force_textures=False,
    skip_textures=None,
):
    """Extract exported character material textures even when no Shader references them."""
    skip_textures = skip_textures or set()
    extracted = 0
    for exp in pkg.exports:
        if not _is_char_texture(exp):
            continue
        tex_lower = exp["object_name"].lower()
        if tex_lower in skip_textures:
            continue
        if not force_textures and tex_lower in extracted_textures:
            continue
        png_path = extract_texture_png(pkg, exp["object_name"], OUTPUT_TEXTURES_DIR)
        if png_path:
            extracted_textures[tex_lower] = png_path
            extracted += 1
    return extracted


def _extract_shader_material_textures(
    pkg,
    materials,
    extracted_textures,
    force_textures=False,
):
    """Extract Texture targets referenced by parsed character shader materials.

    This includes imported tint palettes such as
    UTX_gremlin_M_char.Tint.gremlin_M_char_body_0_TNT, which do not live in the
    same package as the Orc/Goblin shader that references them.
    """
    extracted = 0
    opened_packages = {}

    def _pkg_for_target(target):
        if target.get("kind") == "export":
            return pkg
        if target.get("kind") != "import":
            return None
        source_package = str(target.get("source_package", ""))
        if not source_package:
            return None
        if source_package in opened_packages:
            return opened_packages[source_package]
        path = os.path.join(TEXTURES_DIR, source_package + ".utx")
        if not os.path.exists(path):
            opened_packages[source_package] = None
            return None
        try:
            opened = UE2Package(path)
        except Exception:
            opened = None
        opened_packages[source_package] = opened
        return opened

    for material in materials:
        for key in ("diffuse", "opacity", "tint_alpha", "tint_palette"):
            target = material.get(key)
            if not isinstance(target, dict) or target.get("class") != "Texture":
                continue
            texture_name = str(target.get("name", ""))
            if not texture_name:
                continue
            tex_lower = texture_name.lower()
            if not force_textures and tex_lower in extracted_textures:
                continue
            target_pkg = _pkg_for_target(target)
            if target_pkg is None:
                continue
            png_path = extract_texture_png(target_pkg, texture_name, OUTPUT_TEXTURES_DIR)
            if png_path:
                extracted_textures[tex_lower] = png_path
                extracted += 1
    return extracted


def _open_texture_package(package_name, opened_packages):
    if not package_name:
        return None
    if package_name in opened_packages:
        return opened_packages[package_name]
    path = os.path.join(TEXTURES_DIR, package_name + ".utx")
    if not os.path.exists(path):
        opened_packages[package_name] = None
        return None
    try:
        opened = UE2Package(path)
    except Exception:
        opened = None
    opened_packages[package_name] = opened
    return opened


def process_package(
    utx_path,
    shader_map,
    shader_materials,
    extracted_textures,
    force_textures=False,
    extract_char_textures=False,
):
    """Process a single .utx package: find shaders, resolve diffuse textures, extract PNGs."""
    # Get object list via Unreal-Library
    objects = get_uel_objects(utx_path)

    # Find Shader objects
    shaders = [(cls, path) for cls, path in objects if cls == "Shader"]

    # Check if any shaders need resolving before doing work
    new_shaders = shaders if force_textures else [
        (cls, path)
        for cls, path in shaders
        if path.split(".")[-1].lower() not in shader_map
    ]
    if not new_shaders and not extract_char_textures:
        return 0, 0

    # Open with native parser for texture extraction
    try:
        pkg = UE2Package(utx_path)
    except:
        return 0, 0

    resolved = 0
    extracted = 0
    package_shader_textures = set()
    parsed_materials = []
    opened_texture_packages = {}
    current_package = os.path.splitext(os.path.basename(utx_path))[0]

    shader_exports_by_name = {
        str(exp.get("object_name", "")).lower(): exp
        for exp in pkg.exports
        if exp.get("class_name") == "Shader"
    }

    for cls, shader_path in shaders:
        shader_name = shader_path.split(".")[-1].lower()
        if "_char_" not in shader_name:
            continue
        if not force_textures and shader_name in shader_materials:
            existing_material = shader_materials.get(shader_name)
            if isinstance(existing_material, dict):
                parsed_materials.append(existing_material)
            continue
        shader_export = shader_exports_by_name.get(shader_name)
        if not shader_export:
            continue
        material = _parse_shader_material(pkg, shader_export)
        material["source_package"] = os.path.splitext(os.path.basename(utx_path))[0]
        shader_materials[shader_name] = material
        parsed_materials.append(material)

    for cls, shader_path in new_shaders:
        shader_name = shader_path.split(".")[-1].lower()

        texture_name, constant_color, shader_props = follow_diffuse_chain(
            utx_path, shader_path
        )
        if texture_name:
            texture_ref = shader_props.get("texture_ref")
            texture_package = shader_props.get("texture_package")
            # Determine if shader needs alpha masking based on UE2 properties
            needs_alpha = shader_props.get("blending") in (
                "OB_Masked",
                "1",
            ) or shader_props.get("has_opacity")
            needs_structured_entry = bool(
                needs_alpha
                or shader_props.get("two_sided")
                or (
                    texture_package
                    and texture_package.lower() != current_package.lower()
                )
            )
            if needs_structured_entry:
                entry = {"texture": texture_name}
                if texture_ref:
                    entry["texture_ref"] = texture_ref
                if texture_package:
                    entry["texture_package"] = texture_package
            if needs_alpha:
                alpha_mode = (
                    "blend"
                    if _is_soft_alpha_material(shader_name, texture_name)
                    else "mask"
                )
                entry["alpha"] = alpha_mode
                if shader_props.get("two_sided"):
                    entry["two_sided"] = True
                shader_map[shader_name] = entry
            elif needs_structured_entry:
                if shader_props.get("two_sided"):
                    entry["two_sided"] = True
                shader_map[shader_name] = entry
            else:
                shader_map[shader_name] = texture_name
            resolved += 1

            # Extract PNG if not already done
            tex_lower = texture_name.lower()
            if force_textures or tex_lower not in extracted_textures:
                target_pkg = pkg
                if texture_package and texture_package.lower() != current_package.lower():
                    target_pkg = _open_texture_package(
                        texture_package, opened_texture_packages
                    )
                png_path = (
                    extract_texture_png(target_pkg, texture_name, OUTPUT_TEXTURES_DIR)
                    if target_pkg is not None
                    else None
                )
                if png_path:
                    extracted_textures[tex_lower] = png_path
                    package_shader_textures.add(tex_lower)
                    extracted += 1
        elif constant_color:
            if shader_props.get("is_water"):
                # Water shader: store as structured entry so the viewer can render it as water
                shader_map[shader_name] = {
                    "color": f"{constant_color[0]:.3f},{constant_color[1]:.3f},{constant_color[2]:.3f}",
                    "is_water": True,
                    "two_sided": False,
                }
            else:
                # Store as color: prefix so glTF export can use it as baseColorFactor
                shader_map[shader_name] = (
                    f"color:{constant_color[0]:.3f},{constant_color[1]:.3f},{constant_color[2]:.3f}"
                )
            resolved += 1

    if extract_char_textures:
        extracted += _extract_shader_material_textures(
            pkg,
            parsed_materials,
            extracted_textures,
            force_textures=force_textures,
        )
        extracted += extract_unreferenced_char_textures(
            pkg,
            extracted_textures,
            force_textures=force_textures,
            skip_textures=package_shader_textures,
        )

    return resolved, extracted


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Build shader→texture mapping and extract PNGs"
    )
    parser.add_argument(
        "--packages", nargs="*", help="Specific package names (without .utx) to process"
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from existing shader_to_texture.json",
    )
    parser.add_argument(
        "--force-textures",
        action="store_true",
        help="Re-extract texture PNGs for processed packages even if shader map/PNGs already exist",
    )
    parser.add_argument(
        "--extract-char-textures",
        action="store_true",
        help="Also extract exported *_char_*_CLR textures that are not referenced by Shader exports",
    )
    parser.add_argument(
        "--from-material-manifest",
        action="store_true",
        help="Project shader_to_texture.json from material_manifest.json and exit",
    )
    parser.add_argument(
        "--material-manifest",
        default=MATERIAL_MANIFEST,
        help="Manifest path for --from-material-manifest",
    )
    args = parser.parse_args()

    os.makedirs(OUTPUT_TEXTURES_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(OUTPUT_MAP), exist_ok=True)

    if args.from_material_manifest:
        with open(args.material_manifest, "r", encoding="utf-8") as handle:
            material_manifest = json.load(handle)
        shader_map = _project_shader_map_from_manifest(material_manifest)
        with open(OUTPUT_MAP, "w", encoding="utf-8") as handle:
            json.dump(shader_map, handle, indent=1)
            handle.write("\n")
        print(
            f"Projected {len(shader_map)} compatibility entries from {args.material_manifest}"
        )
        print(f"Map: {OUTPUT_MAP}")
        return

    # Load existing map — always merge with existing data so partial runs
    # don't wipe previously resolved entries
    shader_map = {}
    shader_materials = {}
    extracted_textures = {}
    if os.path.exists(OUTPUT_MAP):
        with open(OUTPUT_MAP) as f:
            shader_map = json.load(f)
        print(
            f"Loaded existing map: {len(shader_map)} shaders"
        )
    if os.path.exists(OUTPUT_SHADER_MATERIALS):
        with open(OUTPUT_SHADER_MATERIALS) as f:
            shader_materials = json.load(f)
        print(
            f"Loaded existing character shader material map: {len(shader_materials)} shaders"
        )
    if os.path.exists(OUTPUT_TEXTURES_DIR):
        # Scan existing PNGs
        for png in os.listdir(OUTPUT_TEXTURES_DIR):
            if png.endswith(".png"):
                extracted_textures[png[:-4].lower()] = os.path.join(
                    OUTPUT_TEXTURES_DIR, png
                )
        print(
            f"Resuming: {len(shader_map)} shaders, {len(extracted_textures)} textures already done"
        )

    # Find .utx files
    if args.packages:
        utx_files = []
        for name in args.packages:
            path = os.path.join(
                TEXTURES_DIR, name if name.endswith(".utx") else name + ".utx"
            )
            if os.path.exists(path):
                utx_files.append(path)
            else:
                print(f"  WARNING: {path} not found")
    else:
        utx_files = sorted(
            [
                os.path.join(TEXTURES_DIR, f)
                for f in os.listdir(TEXTURES_DIR)
                if f.endswith(".utx")
            ]
        )

    print(f"Processing {len(utx_files)} .utx packages")
    print(f"Output: {OUTPUT_MAP}")
    print(f"Textures: {OUTPUT_TEXTURES_DIR}")
    print()

    t0 = time.time()
    total_resolved = 0
    total_extracted = 0

    for i, utx_path in enumerate(utx_files):
        pkg_name = os.path.splitext(os.path.basename(utx_path))[0]
        print(f"  [{i+1}/{len(utx_files)}] {pkg_name}", end="", flush=True)

        r, e = process_package(
            utx_path,
            shader_map,
            shader_materials,
            extracted_textures,
            force_textures=args.force_textures,
            extract_char_textures=args.extract_char_textures,
        )
        total_resolved += r
        total_extracted += e

        if r > 0 or e > 0:
            print(f" -> {r} shaders, {e} textures", flush=True)
        else:
            print(f" (no shaders)", flush=True)

        # Save periodically
        if (i + 1) % 20 == 0:
            with open(OUTPUT_MAP, "w") as f:
                json.dump(shader_map, f, indent=1)
            with open(OUTPUT_SHADER_MATERIALS, "w") as f:
                json.dump(shader_materials, f, indent=1)

    # Final save
    with open(OUTPUT_MAP, "w") as f:
        json.dump(shader_map, f, indent=1)
    with open(OUTPUT_SHADER_MATERIALS, "w") as f:
        json.dump(shader_materials, f, indent=1)

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"Done in {elapsed:.0f}s")
    print(f"  Shaders resolved: {total_resolved} (total: {len(shader_map)})")
    print(f"  Character shader materials: {len(shader_materials)}")
    print(f"  Textures extracted: {total_extracted} (total: {len(extracted_textures)})")
    print(f"  Map: {OUTPUT_MAP}")
    print(f"  Material map: {OUTPUT_SHADER_MATERIALS}")


if __name__ == "__main__":
    main()
