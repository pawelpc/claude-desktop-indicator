"""Generate assets/icon.ico for the indicator's exe and desktop shortcut.

Build-time tool only (requires Pillow); the icon mirrors the indicator's
visual language: teal rounded square (Code background color) with the gold
Fable pentagon and a white bolt of text.

Usage:  pip install pillow && python assets/make_icon.py
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

TEAL = (0x0E, 0x74, 0x90, 255)      # colors.MODE_COLORS["code"]
GOLD = (0xCA, 0x8A, 0x04, 255)      # colors.FAMILY_SHAPES["fable"] fill
SIZE = 256


def pentagon(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    pts = []
    for i in range(5):
        a = math.radians(-90 + i * 72)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def main() -> None:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=48, fill=TEAL)
    d.polygon(pentagon(SIZE / 2, SIZE / 2 + 6, 92), fill=GOLD)
    out = Path(__file__).parent / "icon.ico"
    img.save(out, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
