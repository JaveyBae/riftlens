"""Draw the RiftLens logo (same shapes as the <symbol id="logo"> in web/static/index.html) into assets/riftlens.ico."""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "assets" / "riftlens.ico"


def draw(size: int = 256) -> Image.Image:
    big = size * 4  # supersample, then downscale for smooth edges
    k = big / 64  # the SVG viewBox is 64x64
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # the 42x42 square rotated 45 degrees: a diamond with half-diagonal 29.7
    d.regular_polygon((32 * k, 32 * k, 29.7 * k), 4, rotation=45, fill="#1b4d50", outline="#e3b04b", width=round(2.5 * k))
    d.line([(17 * k, 47 * k), (47 * k, 17 * k)], fill="#e3b04b", width=round(4 * k))
    for cx, cy, color in ((21.5, 42.5, "#7fc4d6"), (42.5, 21.5, "#e27c6a")):
        d.ellipse([(cx - 3) * k, (cy - 3) * k, (cx + 3) * k, (cy + 3) * k], fill=color)
    lens = Image.new("RGBA", img.size, (0, 0, 0, 0))  # translucent fill has to be composited, not painted
    ImageDraw.Draw(lens).ellipse([28.5 * k, 28.5 * k, 45.5 * k, 45.5 * k], fill=(244, 234, 210, 30))
    img = Image.alpha_composite(img, lens)
    d = ImageDraw.Draw(img)
    d.ellipse([28.5 * k, 28.5 * k, 45.5 * k, 45.5 * k], outline="#f4ead2", width=round(3 * k))
    d.line([(43.5 * k, 43.5 * k), (50 * k, 50 * k)], fill="#f4ead2", width=round(4 * k))
    return img.resize((size, size), Image.LANCZOS)


def main() -> Path:
    OUT.parent.mkdir(exist_ok=True)
    draw(256).save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return OUT


if __name__ == "__main__":
    print(main())
