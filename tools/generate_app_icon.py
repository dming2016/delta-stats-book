#!/usr/bin/env python3
"""Generate desktop and web icon assets from the approved raster master."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
MASTER_SOURCE = ROOT / "tools" / "assets" / "app-icon-master.png"
WEB_ASSETS = ROOT / "web" / "assets"
ARTIFACTS = ROOT / "artifacts"
MASTER_SIZE = 1024
WEB_ICON_VERSION = "v2"


def load_master() -> Image.Image:
    if not MASTER_SOURCE.is_file():
        raise RuntimeError(f"icon master does not exist: {MASTER_SOURCE}")
    image = Image.open(MASTER_SOURCE).convert("RGBA")
    if image.width != image.height:
        raise RuntimeError("icon master must be square")
    if image.getchannel("A").getextrema() != (0, 255):
        raise RuntimeError("icon master must contain transparent and opaque pixels")
    return image.resize((MASTER_SIZE, MASTER_SIZE), Image.Resampling.LANCZOS)


def resized_icon(master: Image.Image, size: int) -> Image.Image:
    icon = master.resize((size, size), Image.Resampling.LANCZOS)
    if size <= 64:
        icon = icon.filter(ImageFilter.UnsharpMask(radius=0.6, percent=150, threshold=2))
    return icon


def create_preview(master: Image.Image) -> Image.Image:
    preview = Image.new("RGB", (1280, 720), (235, 239, 237))
    draw = ImageDraw.Draw(preview)
    large = resized_icon(master, 520)
    preview.paste(large, (60, 88), large)

    sizes = (256, 128, 64, 32, 16)
    x_positions = (640, 942, 1096, 1180, 1230)
    for row, background in enumerate(((255, 255, 255), (24, 28, 31))):
        y = 70 + row * 330
        for icon_size, x in zip(sizes, x_positions, strict=True):
            icon = resized_icon(master, icon_size)
            tile_size = icon_size + 32
            tile = Image.new("RGBA", (tile_size, tile_size), background + (255,))
            tile.alpha_composite(icon, (16, 16))
            preview.paste(tile.convert("RGB"), (x, y))
            label_color = (50, 58, 54) if row == 0 else (230, 235, 232)
            draw.text((x + 3, y + tile_size + 6), f"{icon_size}px", fill=label_color)
    return preview


def main() -> None:
    WEB_ASSETS.mkdir(parents=True, exist_ok=True)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    master = load_master()

    app_icon = resized_icon(master, 512)
    favicon_32 = resized_icon(master, 32)
    favicon_16 = resized_icon(master, 16)

    app_icon.save(WEB_ASSETS / "app-icon.png", optimize=True)
    app_icon.save(WEB_ASSETS / f"app-icon-{WEB_ICON_VERSION}.png", optimize=True)
    favicon_32.save(WEB_ASSETS / "favicon-32.png", optimize=True)
    favicon_32.save(WEB_ASSETS / f"favicon-32-{WEB_ICON_VERSION}.png", optimize=True)
    favicon_16.save(WEB_ASSETS / "favicon-16.png", optimize=True)
    favicon_16.save(WEB_ASSETS / f"favicon-16-{WEB_ICON_VERSION}.png", optimize=True)
    master.save(
        WEB_ASSETS / "app-icon.ico",
        format="ICO",
        sizes=[
            (16, 16),
            (20, 20),
            (24, 24),
            (32, 32),
            (40, 40),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        ],
    )
    create_preview(master).save(ARTIFACTS / "app-icon-preview.png", optimize=True)


if __name__ == "__main__":
    main()
