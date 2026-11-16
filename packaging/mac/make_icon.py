#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Xclusive Audio Visual Group (XAVGROUP)
"""Draws the Mixx Trace app icon (icon-1024.png and MixxTrace.icns). Needs Pillow; only run when the icon changes."""
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 4                                   # draw large, scale down for clean edges
N = 1024 * SS
TILE, RADIUS = 824 * SS, 185 * SS        # Apple's icon grid: an 824 px rounded square on a 1024 px canvas
X0 = (N - TILE) // 2

# The MT monogram on a 96 unit grid: three posts and one trace line (see branding/).
STROKE = 9.0
POSTS = [((10, 25), (10, 71)), ((46, 25), (46, 71)), ((67, 25), (67, 71))]
SEGS = [((10, 25), (22, 25), (21, 55), (28, 55)), ((28, 55), (35, 55), (34, 25), (46, 25))]   # the dip of the M
BAR = ((46, 25), (87, 25))                                                                   # then flat: the T
MARK = 614.0 * SS                        # the 96 unit grid is drawn 614 px wide, centred in the tile
INK, RED = (0xFF, 0xFF, 0xFF, 255), (0xE3, 0x10, 0x1C, 255)


def curve():
    pts = []
    for p0, p1, p2, p3 in SEGS:
        for i in range(600):
            t = i / 599.0
            a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t ** 3
            pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


def main():
    mask = Image.new("L", (N, N), 0)
    ImageDraw.Draw(mask).rounded_rectangle([X0, X0, X0 + TILE, X0 + TILE], RADIUS, fill=255)

    shadow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 110), (0, 12 * SS), mask)
    shadow = shadow.filter(ImageFilter.GaussianBlur(14 * SS))

    tile = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    grad = Image.linear_gradient("L").resize((N, N))                      # dark at the top
    top, bottom = (0x1E, 0x1E, 0x1E), (0x00, 0x00, 0x00)
    body = Image.merge("RGB", [grad.point(lambda v, a=a, b=b: int(a + (b - a) * v / 255.0)) for a, b in zip(top, bottom)])
    tile.paste(body, (0, 0), mask)
    edge = Image.new("L", (N, N), 0)                                      # a faint lighter rim
    ImageDraw.Draw(edge).rounded_rectangle([X0, X0, X0 + TILE, X0 + TILE], RADIUS, outline=255, width=3 * SS)
    tile.paste((255, 255, 255, 26), (0, 0), ImageChops.multiply(edge, mask))

    k = MARK / 96.0
    o = (N - MARK) / 2.0
    r = STROKE * k / 2.0

    def stroke(points, colour):
        layer = Image.new("L", (N, N), 0)
        d = ImageDraw.Draw(layer)
        for x, y in points:
            cx, cy = o + x * k, o + y * k
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
        tile.paste(colour, (0, 0), layer)

    def straight(p0, p1, n=600):
        return [(p0[0] + (p1[0] - p0[0]) * i / (n - 1.0), p0[1] + (p1[1] - p0[1]) * i / (n - 1.0)) for i in range(n)]

    stroke([p for a, b in POSTS for p in straight(a, b)], INK)
    stroke(curve() + straight(*BAR), RED)

    icon = Image.alpha_composite(shadow, tile).resize((1024, 1024), Image.LANCZOS)
    icon.save(os.path.join(HERE, "icon-1024.png"), optimize=True)
    icon.save(os.path.join(HERE, "MixxTrace.icns"), sizes=[(s, s) for s in (16, 32, 64, 128, 256, 512, 1024)])
    print("wrote icon-1024.png and MixxTrace.icns")


if __name__ == "__main__":
    main()
