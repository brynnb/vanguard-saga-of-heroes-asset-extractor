"""
DXT1, DXT3, and DXT5 texture decompression.

Moved here from extract_all_terrain.py so ue2.texture can use them
without a circular / fragile cross-module import.
"""

import struct
from PIL import Image


def decode_dxt3(data, width, height):
    """Decode DXT3 compressed texture to PIL Image.

    DXT3 uses explicit 4-bit alpha per pixel (not interpolated like DXT5).
    Block layout: 8 bytes alpha (4 bits × 16 pixels) + 8 bytes color (same as DXT1/5).
    """
    try:
        pixels = bytearray(width * height * 4)
        blocks_x, blocks_y = width // 4, height // 4

        for block_y in range(blocks_y):
            for block_x in range(blocks_x):
                block_idx = (block_y * blocks_x + block_x) * 16
                if block_idx + 16 > len(data):
                    break

                # Alpha Block (8 bytes) - explicit 4-bit alpha per pixel
                # 16 pixels × 4 bits = 64 bits = 8 bytes
                alpha_bits = struct.unpack("<Q", data[block_idx : block_idx + 8])[0]

                # Color Block (8 bytes) - same as DXT5
                c_idx = block_idx + 8
                c0 = struct.unpack("<H", data[c_idx : c_idx + 2])[0]
                c1 = struct.unpack("<H", data[c_idx + 2 : c_idx + 4])[0]

                def decode565(c):
                    return (
                        ((c >> 11) & 0x1F) * 255 // 31,
                        ((c >> 5) & 0x3F) * 255 // 63,
                        (c & 0x1F) * 255 // 31,
                    )

                r0, g0, b0 = decode565(c0)
                r1, g1, b1 = decode565(c1)

                color_table = [
                    (r0, g0, b0),
                    (r1, g1, b1),
                    ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3),
                    ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3),
                ]

                c_indices = struct.unpack("<I", data[c_idx + 4 : c_idx + 8])[0]

                for py in range(4):
                    y = block_y * 4 + py
                    if y >= height:
                        continue
                    row_offset = (y * width + block_x * 4) * 4
                    for px in range(4):
                        p_idx = py * 4 + px
                        # 4-bit alpha: expand to 8-bit by multiplying by 17 (0x0 -> 0, 0xF -> 255)
                        a4 = (alpha_bits >> (4 * p_idx)) & 0x0F
                        a_val = a4 * 17
                        c_val = color_table[(c_indices >> (2 * p_idx)) & 0x03]
                        pixels[row_offset + px * 4 : row_offset + px * 4 + 4] = bytes(
                            [c_val[0], c_val[1], c_val[2], a_val]
                        )

        return Image.frombytes("RGBA", (width, height), bytes(pixels))
    except Exception:
        return None


def decode_dxt5(data, width, height):
    """Decode DXT5 compressed texture to PIL Image."""
    try:
        pixels = bytearray(width * height * 4)
        blocks_x, blocks_y = width // 4, height // 4

        for block_y in range(blocks_y):
            for block_x in range(blocks_x):
                block_idx = (block_y * blocks_x + block_x) * 16
                if block_idx + 16 > len(data):
                    break

                # Alpha Block (8 bytes)
                a0, a1 = data[block_idx], data[block_idx + 1]
                # Read 48 bits of alpha indices (bytes 2-7)
                bits = struct.unpack("<Q", data[block_idx : block_idx + 8])[0] >> 16

                alphas = [a0, a1]
                if a0 > a1:
                    alphas.extend(
                        [((6 - i) * a0 + (i + 1) * a1) // 7 for i in range(6)]
                    )
                else:
                    alphas.extend(
                        [((4 - i) * a0 + (i + 1) * a1) // 5 for i in range(4)]
                    )
                    alphas.extend([0, 255])

                # Color Block (8 bytes)
                c_idx = block_idx + 8
                c0 = struct.unpack("<H", data[c_idx : c_idx + 2])[0]
                c1 = struct.unpack("<H", data[c_idx + 2 : c_idx + 4])[0]

                def decode565(c):
                    return (
                        ((c >> 11) & 0x1F) * 255 // 31,
                        ((c >> 5) & 0x3F) * 255 // 63,
                        (c & 0x1F) * 255 // 31,
                    )

                r0, g0, b0 = decode565(c0)
                r1, g1, b1 = decode565(c1)

                # DXT3/5 always uses 4-color interpolation (no 1-bit alpha in color block)
                color_table = [
                    (r0, g0, b0),
                    (r1, g1, b1),
                    ((2 * r0 + r1) // 3, (2 * g0 + g1) // 3, (2 * b0 + b1) // 3),
                    ((r0 + 2 * r1) // 3, (g0 + 2 * g1) // 3, (b0 + 2 * b1) // 3),
                ]

                c_indices = struct.unpack("<I", data[c_idx + 4 : c_idx + 8])[0]

                for py in range(4):
                    y = block_y * 4 + py
                    if y >= height:
                        continue
                    row_offset = (y * width + block_x * 4) * 4
                    for px in range(4):
                        p_idx = py * 4 + px
                        a_val = alphas[(bits >> (3 * p_idx)) & 0x07]
                        c_val = color_table[(c_indices >> (2 * p_idx)) & 0x03]
                        pixels[row_offset + px * 4 : row_offset + px * 4 + 4] = bytes(
                            [c_val[0], c_val[1], c_val[2], a_val]
                        )

        return Image.frombytes("RGBA", (width, height), bytes(pixels))
    except Exception:
        return None


def decode_dxt1(data, width, height):
    """Decode DXT1 compressed texture to PIL Image."""
    try:
        pixels = bytearray(width * height * 4)
        blocks_x, blocks_y = width // 4, height // 4
        for block_y in range(blocks_y):
            for block_x in range(blocks_x):
                block_idx = (block_y * blocks_x + block_x) * 8
                if block_idx + 8 > len(data):
                    break
                block = data[block_idx : block_idx + 8]
                c0, c1 = (
                    struct.unpack("<H", block[0:2])[0],
                    struct.unpack("<H", block[2:4])[0],
                )
                r0, g0, b0 = (
                    ((c0 >> 11) & 0x1F) * 255 // 31,
                    ((c0 >> 5) & 0x3F) * 255 // 63,
                    (c0 & 0x1F) * 255 // 31,
                )
                r1, g1, b1 = (
                    ((c1 >> 11) & 0x1F) * 255 // 31,
                    ((c1 >> 5) & 0x3F) * 255 // 63,
                    (c1 & 0x1F) * 255 // 31,
                )
                if c0 > c1:
                    colors = [
                        (r0, g0, b0, 255),
                        (r1, g1, b1, 255),
                        (
                            (2 * r0 + r1) // 3,
                            (2 * g0 + g1) // 3,
                            (2 * b0 + b1) // 3,
                            255,
                        ),
                        (
                            (r0 + 2 * r1) // 3,
                            (g0 + 2 * g1) // 3,
                            (b0 + 2 * b1) // 3,
                            255,
                        ),
                    ]
                else:
                    colors = [
                        (r0, g0, b0, 255),
                        (r1, g1, b1, 255),
                        ((r0 + r1) // 2, (g0 + g1) // 2, (b0 + b1) // 2, 255),
                        (0, 0, 0, 0),
                    ]
                indices = struct.unpack("<I", block[4:8])[0]
                for py in range(4):
                    y = block_y * 4 + py
                    if y >= height:
                        continue
                    row_offset = (y * width + block_x * 4) * 4
                    for px in range(4):
                        idx = (indices >> (2 * (py * 4 + px))) & 0x3
                        pixels[row_offset + px * 4 : row_offset + px * 4 + 4] = bytes(
                            colors[idx]
                        )
        return Image.frombytes("RGBA", (width, height), bytes(pixels))
    except Exception:
        return None
