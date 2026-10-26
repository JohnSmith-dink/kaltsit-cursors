#!/usr/bin/env python3
"""Render previews of the existing cursor artwork."""
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from cursorlib import load_cursor

ROOT = Path(__file__).resolve().parent.parent


def render() -> None:
    manifest = json.loads((ROOT / "cursors.json").read_text())
    canvas = Image.new("RGB", (1056, 950), "#f2f4ef")
    draw = ImageDraw.Draw(canvas)
    title = ImageFont.load_default(size=34)
    label = ImageFont.load_default(size=16)
    small = ImageFont.load_default(size=13)
    draw.text((36, 28), "KALTSIT", font=title, fill="#233b35")
    draw.text((36, 78), "17 animated cursors  /  24, 32, 48, 64, 96 px", font=label, fill="#5e756d")
    draw.line((36, 112, 1020, 112), fill="#becfc1", width=2)
    for i, (name, spec) in enumerate(manifest.items()):
        x, y = 36 + (i % 5) * 204, 136 + (i // 5) * 190
        draw.rounded_rectangle((x, y, x + 188, y + 174), radius=12, fill="white")
        frames = load_cursor(ROOT / "assets/windows" / spec["source"], 96)
        for row in range(6):
            for col in range(6):
                fill = "#eaf0e7" if (row + col) % 2 else "#dce6da"
                draw.rectangle((x+46+col*16, y+12+row*16, x+61+col*16, y+27+row*16), fill=fill)
        canvas.paste(frames[0].image, (x + 46, y + 12), frames[0].image)
        draw.text((x + 12, y + 120), name, font=label, fill="#233b35")
        draw.text((x + 12, y + 146), f"{len(frames)} frames / 100 ms", font=small, fill="#5e756d")
    draw.text((36, 920), "Original Windows pixel artwork preserved. Nearest-neighbor scaling.", font=small, fill="#5e756d")
    docs = ROOT / "docs"
    docs.mkdir(exist_ok=True)
    canvas.save(docs / "preview.png")
    animation = []
    frames = load_cursor(ROOT / "assets/windows/Normal.ani", 160)
    for frame in frames:
        image = Image.new("RGB", (192, 192), "#e7eee4")
        image.paste(frame.image, (16, 16), frame.image)
        animation.append(image)
    animation[0].save(docs / "normal.gif", save_all=True, append_images=animation[1:],
                      duration=[f.delay_ms for f in frames], loop=0, optimize=False, disposal=2)
    print("Wrote docs/preview.png and docs/normal.gif")


if __name__ == "__main__":
    render()
