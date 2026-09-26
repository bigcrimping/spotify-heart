"""Build docs/hero.png: the sensor next to the app window.

    python tools/make_hero.py <photo.jpg> [outdir]

The photo crop is fixed to the shot used in the README; pass a different one
and check the framing by eye.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BG = (20, 22, 28)          # the window's own background
EDGE = (42, 46, 57)
PAD = 28
GAP = 28
RADIUS = 12
CROP = (150, 330, 1050, 1230)


def rounded(img: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *[s - 1 for s in img.size]), radius, fill=255)
    out = Image.new("RGBA", img.size, (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def main() -> int:
    photo_path = Path(sys.argv[1])
    outdir = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "docs"
    shot = Image.open(outdir / "window.png").convert("RGBA")

    photo = Image.open(photo_path).convert("RGBA").crop(CROP)
    size = shot.height
    photo = rounded(photo.resize((size, size), Image.LANCZOS), RADIUS)

    w = PAD + photo.width + GAP + shot.width + PAD
    h = PAD + size + PAD
    canvas = Image.new("RGBA", (w, h), BG + (255,))
    canvas.paste(photo, (PAD, PAD), photo)

    # A hairline round the photo, matching the window's card edges.
    ImageDraw.Draw(canvas).rounded_rectangle(
        (PAD, PAD, PAD + photo.width - 1, PAD + photo.height - 1), RADIUS, outline=EDGE, width=1
    )
    canvas.paste(shot, (PAD + photo.width + GAP, PAD), shot)

    out = outdir / "hero.png"
    canvas.convert("RGB").save(out, quality=95)
    print("saved", out, canvas.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
