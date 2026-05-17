#!/usr/bin/env python3
"""
Build a shader → diffuse texture mapping for all .utx packages.

For each Shader export, follows the Diffuse property through Combiner/TexModifier
chains to find the actual Texture object, then extracts it to PNG.

Uses the Unreal-Library CLI only to list/decompile material objects. Texture
PNG extraction is handled by this repo's native Python UE2 texture parser.

Output:
  - output/data/shader_to_texture.json  (shader_name → texture_name mapping)
  - output/textures/<texture_name>.png  (extracted diffuse textures)
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
from ue2.texture import Texture as UE2Texture

TEXTURES_DIR = os.path.join(config.ASSETS_PATH, "Textures")
OUTPUT_MAP = os.path.join(config.DATA_DIR, "shader_to_texture.json")
OUTPUT_TEXTURES_DIR = os.path.join(PROJECT_ROOT, "output", "textures")

# Unreal-Library paths
DOTNET = getattr(config, "DOTNET", os.environ.get("DOTNET", "dotnet"))
UEL_DLL = os.path.expanduser(
    os.environ.get(
        "UNREAL_LIBRARY_DLL",
        getattr(config, "UNREAL_LIBRARY_DLL", ""),
    )
)


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
    Returns (texture_name, constant_color) — one or both may be None."""
    if visited is None:
        visited = set()
    if obj_path in visited or depth > 10:
        return None, None
    visited.add(obj_path)

    dec = decompile_uel(pkg_path, obj_path)
    if not dec:
        return None, None

    # Check for ConstantColor — extract RGB as fallback
    if "class=Engine.ConstantColor" in dec:
        color_m = re.search(r"Color=\(R=(\d+),G=(\d+),B=(\d+)", dec)
        if color_m:
            r, g, b = (
                int(color_m.group(1)),
                int(color_m.group(2)),
                int(color_m.group(3)),
            )
            return None, [r / 255.0, g / 255.0, b / 255.0]
        return None, None

    is_combiner = "class=Engine.Combiner" in dec

    if is_combiner:
        # UE2 Combiner blends Material1 and Material2. The nesting is inconsistent —
        # sometimes Material1 is the base surface, sometimes Material2 is.
        # Strategy: resolve BOTH branches, then prefer the non-overlay texture.
        _OVERLAY_KEYWORDS = (
            "overlay",
            "moss",
            "grime",
            "grunge",
            "stain",
            "crack_over",
            "alge_overlay",
            "mosswall",
            "mossy_overlay",
        )

        mat1 = re.search(r"Material1=(\w+)'([^']+)'", dec)
        mat2 = re.search(r"Material2=(\w+)'([^']+)'", dec)

        # Resolve both branches
        results = []  # list of (texture_name, color, source_label)
        for label, mat_ref in [("mat1", mat1), ("mat2", mat2)]:
            if not mat_ref:
                continue
            ref_cls, ref_path = mat_ref.group(1), mat_ref.group(2)
            if ref_cls == "Texture":
                results.append((ref_path.split(".")[-1], None, label))
            else:
                tex, color = _find_texture_recursive(
                    pkg_path, ref_path, set(visited), depth + 1
                )
                if tex or color:
                    results.append((tex, color, label))

        if not results:
            return None, None

        # Filter to texture results (not colors)
        tex_results = [(t, c, l) for t, c, l in results if t]
        if len(tex_results) > 1:
            # Prefer the non-overlay texture
            non_overlay = [
                (t, c, l)
                for t, c, l in tex_results
                if not any(kw in t.lower() for kw in _OVERLAY_KEYWORDS)
            ]
            if non_overlay:
                return non_overlay[0][0], non_overlay[0][1]

        # Return first available result
        return results[0][0], results[0][1]
    else:
        # TexModifier/TexScaler/etc — follow Material or Diffuse property
        refs = list(re.finditer(r"(?:Material|Diffuse)=(\w+)'([^']+)'", dec))
        for ref_m in refs:
            ref_cls, ref_path = ref_m.group(1), ref_m.group(2)
            if ref_cls == "Texture":
                return ref_path.split(".")[-1], None
            tex, color = _find_texture_recursive(pkg_path, ref_path, visited, depth + 1)
            if tex:
                return tex, color
        return None, None


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
        return diff_path.split(".")[-1], None, shader_props
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

    tex, color = _find_texture_recursive(pkg_path, diff_path)
    return tex, color, shader_props


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


def process_package(utx_path, shader_map, extracted_textures):
    """Process a single .utx package: find shaders, resolve diffuse textures, extract PNGs."""
    # Get object list via Unreal-Library
    objects = get_uel_objects(utx_path)
    if not objects:
        return 0, 0

    # Find Shader objects
    shaders = [(cls, path) for cls, path in objects if cls == "Shader"]
    if not shaders:
        return 0, 0

    # Check if any shaders need resolving before doing work
    new_shaders = [
        (cls, path)
        for cls, path in shaders
        if path.split(".")[-1].lower() not in shader_map
    ]
    if not new_shaders:
        return 0, 0

    # Open with native parser for texture extraction
    try:
        pkg = UE2Package(utx_path)
    except:
        return 0, 0

    resolved = 0
    extracted = 0

    for cls, shader_path in new_shaders:
        shader_name = shader_path.split(".")[-1].lower()

        texture_name, constant_color, shader_props = follow_diffuse_chain(
            utx_path, shader_path
        )
        if texture_name:
            # Determine if shader needs alpha masking based on UE2 properties
            needs_alpha = shader_props.get("blending") in (
                "OB_Masked",
                "1",
            ) or shader_props.get("has_opacity")
            if needs_alpha:
                entry = {"texture": texture_name, "alpha": "mask"}
                if shader_props.get("two_sided"):
                    entry["two_sided"] = True
                shader_map[shader_name] = entry
            else:
                shader_map[shader_name] = texture_name
            resolved += 1

            # Extract PNG if not already done
            tex_lower = texture_name.lower()
            if tex_lower not in extracted_textures:
                png_path = extract_texture_png(pkg, texture_name, OUTPUT_TEXTURES_DIR)
                if png_path:
                    extracted_textures[tex_lower] = png_path
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
    args = parser.parse_args()

    if not UEL_DLL or not os.path.exists(UEL_DLL):
        raise SystemExit(
            "Unreal-Library CLI not found. Set UNREAL_LIBRARY_DLL to "
            "Eliot.UELib.CLI.dll before running this script."
        )

    os.makedirs(OUTPUT_TEXTURES_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(OUTPUT_MAP), exist_ok=True)

    # Load existing map — always merge with existing data so partial runs
    # don't wipe previously resolved entries
    shader_map = {}
    extracted_textures = {}
    if os.path.exists(OUTPUT_MAP):
        with open(OUTPUT_MAP) as f:
            shader_map = json.load(f)
        print(
            f"Loaded existing map: {len(shader_map)} shaders"
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

        r, e = process_package(utx_path, shader_map, extracted_textures)
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

    # Final save
    with open(OUTPUT_MAP, "w") as f:
        json.dump(shader_map, f, indent=1)

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"Done in {elapsed:.0f}s")
    print(f"  Shaders resolved: {total_resolved} (total: {len(shader_map)})")
    print(f"  Textures extracted: {total_extracted} (total: {len(extracted_textures)})")
    print(f"  Map: {OUTPUT_MAP}")


if __name__ == "__main__":
    main()
