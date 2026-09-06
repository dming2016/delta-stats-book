"""Draw original abstract location thumbnails without any game artwork."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    "CG": ("cgxg-changgui.png", "cgxg-jimi.png"),
    "ZD": ("lhdb-changgui.png", "lhdb-jimi.png", "lhdb-qianye.png", "lhdb-yongye.png", "lhdb-zhongye.png"),
    "SC": ("htjd-jimi.png", "htjd-juemi.png"),
    "BK": ("bks-changgui.png", "bks-jimi.png", "bks-juemi.png"),
    "AZ": ("az3-changgui.jpg", "az3-jimi.jpg"),
    "MP": ("jq.png", "klddsc.png"),
}
COLORS = ((151, 193, 142), (125, 175, 203), (199, 177, 123), (174, 153, 192), (160, 183, 178), (158, 172, 194))


def main():
    assets = ROOT / "web/assets"
    assets.mkdir(parents=True, exist_ok=True)
    for index, (code, names) in enumerate(GROUPS.items()):
        for variant, name in enumerate(names):
            image = Image.new("RGB", (528, 320), (24, 29, 32))
            draw = ImageDraw.Draw(image)
            accent = COLORS[index]
            for x in range(0, 528, 32):
                draw.line((x, 0, x, 320), fill=(37, 45, 48), width=1)
            for y in range(0, 320, 32):
                draw.line((0, y, 528, y), fill=(37, 45, 48), width=1)
            offset = index * 9 + variant * 5
            draw.polygon([(25, 220), (130, 75 + offset), (240, 150), (355, 50 + offset), (505, 165), (415, 270), (180, 255)], fill=(40, 51, 52), outline=(67, 84, 85), width=3)
            draw.line([(30, 240), (150, 210), (225, 125), (340, 170), (480, 75)], fill=accent, width=5)
            draw.ellipse((243, 139, 285, 181), outline=accent, width=4)
            draw.line((264, 123, 264, 197), fill=accent, width=2)
            draw.line((227, 160, 301, 160), fill=accent, width=2)
            draw.text((22, 20), f"{code}-{variant + 1:02d}", fill=accent, font_size=28)
            draw.text((22, 280), "ABSTRACT LOCATION / NOT A GAME MAP", fill=(168, 180, 184), font_size=13)
            if name.endswith(".jpg"):
                image.save(assets / name, quality=90, optimize=True)
            else:
                image.save(assets / name, optimize=True)
    print("Generated 16 original abstract thumbnails")


if __name__ == "__main__":
    main()
