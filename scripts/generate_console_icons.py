#!/usr/bin/env python3
"""Generate the console's app icons.

Installing the console as an app needs raster icons, and a committed PNG is not
reviewable. Keeping the drawing here means the icon is a diff instead of an
opaque blob, and it costs no image dependency: zlib and struct are enough.

    python scripts/generate_console_icons.py [--check]

The mark is the three phases Baldr runs, stacked in the same colors the console
uses for planning, execution and review.
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "router" / "src" / "baldr_router" / "console_assets"
SIZES = (192, 512)

BACKGROUND = (27, 36, 48, 255)
BARS = (
    (78, 163, 240, 255),
    (86, 192, 127, 255),
    (224, 161, 69, 255),
)


def _chunk(tag: bytes, payload: bytes) -> bytes:
    body = tag + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


def _png(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    height = len(pixels)
    width = len(pixels[0])
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0: no prediction, so the bytes stay literal
        for red, green, blue, alpha in row:
            raw += bytes((red, green, blue, alpha))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def _rounded(x: int, y: int, left: int, top: int, right: int, bottom: int, radius: int) -> bool:
    if not (left <= x < right and top <= y < bottom):
        return False
    for corner_x, corner_y in (
        (left + radius, top + radius),
        (right - 1 - radius, top + radius),
        (left + radius, bottom - 1 - radius),
        (right - 1 - radius, bottom - 1 - radius),
    ):
        inside_x = x < left + radius or x > right - 1 - radius
        inside_y = y < top + radius or y > bottom - 1 - radius
        if inside_x and inside_y:
            near = abs(x - corner_x) <= radius and abs(y - corner_y) <= radius
            if not near:
                continue
            if (x - corner_x) ** 2 + (y - corner_y) ** 2 > radius**2:
                return False
    return True


def render(size: int) -> bytes:
    unit = size / 512
    # A maskable icon may be cropped to a circle, so the mark stays inside the
    # middle 80% and the background covers the whole canvas.
    bar_left = int(112 * unit)
    bar_right = int(400 * unit)
    bar_height = int(64 * unit)
    gap = int(40 * unit)
    first_top = int(128 * unit)
    bar_radius = max(1, int(bar_height / 2) - 1)
    pixels: list[list[tuple[int, int, int, int]]] = []
    for y in range(size):
        row: list[tuple[int, int, int, int]] = []
        for x in range(size):
            color = BACKGROUND
            for index, bar_color in enumerate(BARS):
                top = first_top + index * (bar_height + gap)
                if _rounded(x, y, bar_left, top, bar_right, top + bar_height, bar_radius):
                    color = bar_color
                    break
            row.append(color)
        pixels.append(row)
    return _png(pixels)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the console app icons")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when a committed icon differs from the generator",
    )
    args = parser.parse_args(argv)
    stale: list[str] = []
    for size in SIZES:
        target = ASSETS / f"icon-{size}.png"
        rendered = render(size)
        if args.check:
            if not target.exists() or target.read_bytes() != rendered:
                stale.append(str(target.relative_to(ROOT)))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(rendered)
        print(f"wrote {target.relative_to(ROOT)} ({len(rendered)} bytes)")
    if stale:
        print("Icons differ from the generator: " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
