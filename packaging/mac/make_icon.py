#!/usr/bin/env python3
"""Draws the Mixx Trace app icon (icon-1024.png and MixxTrace.icns). Needs Pillow; only run when the icon changes."""
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SS = 4                                   # draw large, scale down for clean edges
N = 1024 * SS
TILE, RADIUS = 824 * SS, 185 * SS        # Apple's icon grid: an 824 px rounded square on a 1024 px canvas
X0 = (N - TILE) // 2

# The trace from web/icon.svg (32 px grid), as cubic Bezier segments.
SEGS = [((4, 20), (7.5, 20), (8.5, 12), (12, 12)), ((12, 12), (15.5, 12), (15, 23), (18.5, 23)),
        ((18.5, 23), (22, 23), (22, 8), (25, 8)), ((25, 8), (28, 8), (27, 16), (28, 16))]


def curve():
    pts = []
    for p0, p1, p2, p3 in SEGS:
        for i in range(400):
            t = i / 399.0
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
    top, bottom = (0x23, 0x28, 0x30), (0x10, 0x12, 0x16)
    body = Image.merge("RGB", [grad.point(lambda v, a=a, b=b: int(a + (b - a) * v / 255.0)) for a, b in zip(top, bottom)])
    tile.paste(body, (0, 0), mask)
    edge = Image.new("L", (N, N), 0)                                      # a faint lighter rim
    ImageDraw.Draw(edge).rounded_rectangle([X0, X0, X0 + TILE, X0 + TILE], RADIUS, outline=255, width=3 * SS)
    tile.paste((255, 255, 255, 26), (0, 0), ImageChops.multiply(edge, mask))

    line = Image.new("L", (N, N), 0)
    d = ImageDraw.Draw(line)
    k = TILE / 32.0
    r = 2.4 * k / 2.0
    for x, y in curve():
        cx, cy = X0 + x * k, X0 + y * k
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    tile.paste((0xE9, 0xED, 0xF2, 255), (0, 0), line)

    icon = Image.alpha_composite(shadow, tile).resize((1024, 1024), Image.LANCZOS)
    icon.save(os.path.join(HERE, "icon-1024.png"), optimize=True)
    icon.save(os.path.join(HERE, "MixxTrace.icns"), sizes=[(s, s) for s in (16, 32, 64, 128, 256, 512, 1024)])
    print("wrote icon-1024.png and MixxTrace.icns")


if __name__ == "__main__":
    main()
