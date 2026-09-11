"""Generate the Repo Index app icon (AppIcon.icns).

Draws a macOS-style squircle icon — dark navy gradient body, four colored
"data/file" bars (the category palette) and an accent magnifying glass (the
locator motif) — at 1024px with PIL, then builds a multi-resolution .icns via
`sips` + `iconutil` (both ship with macOS).

This is a BUILD-TIME tool (not imported by the package at runtime). Re-run it to
regenerate the committed asset:  python3 make_icon.py  (writes AppIcon.icns +
AppIcon_1024.png next to this file). Requires PIL (the `full`/`app` extra).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CATS = [(0x79, 0xC0, 0xFF), (0xD2, 0xA8, 0xFF), (0x7E, 0xE7, 0x87), (0xFF, 0xA6, 0x57)]
ACC = (0x58, 0xA6, 0xFF)
SS = 2  # supersample factor for smooth edges


def _vgrad(size, top, bot):
    from PIL import Image
    w, h = size
    g = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / (h - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return g.resize((w, h))


def render_png(px: int = 1024):
    """Return a PIL.Image of the icon at ``px`` (rendered supersampled)."""
    from PIL import Image, ImageDraw, ImageFilter
    s = px * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    inset = int(s * 0.085)
    box = [inset, inset, s - inset, s - inset]
    r = int((box[2] - box[0]) * 0.2237)
    sh = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle(
        [box[0], box[1] + int(s * 0.018), box[2], box[3] + int(s * 0.018)], radius=r, fill=(0, 0, 0, 150)
    )
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(s * 0.02)))
    grad = _vgrad((box[2] - box[0], box[3] - box[1]), (0x1D, 0x27, 0x37), (0x0A, 0x0E, 0x15)).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=r, fill=255)
    body = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    body.paste(grad, (box[0], box[1]))
    img.paste(body, (0, 0), mask)
    hl = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle(
        [box[0], box[1], box[2], box[1] + int((box[3] - box[1]) * 0.45)], radius=r, fill=(255, 255, 255, 14)
    )
    # alpha_composite (NOT paste-with-mask): pasting hl through the body `mask`
    # would copy hl's fully-transparent lower region "as is" over the body,
    # erasing the gradient everywhere except the highlight and leaving the
    # squircle see-through. Compositing blends the highlight and leaves the rest.
    img.alpha_composite(hl)

    d = ImageDraw.Draw(img, "RGBA")
    x0, y0, x1, y1 = box
    bw = x1 - x0
    bx = x0 + int(bw * 0.155)
    by = y0 + int(bw * 0.175)
    bh = int(bw * 0.10)
    gap = int(bw * 0.058)
    for i, (c, wf) in enumerate(zip(CATS, [0.58, 0.46, 0.52, 0.38])):
        yy = by + i * (bh + gap)
        d.rounded_rectangle([bx, yy, bx + int(bw * wf), yy + bh], radius=bh // 2, fill=c + (255,))
    cx = x0 + int(bw * 0.635)
    cy = y0 + int(bw * 0.635)
    rr = int(bw * 0.225)
    ring = int(bw * 0.060)
    glow = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=ACC + (120,), width=ring + 8)
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(s * 0.006)))
    # Draw the magnifier on its OWN transparent layer, then alpha_composite it.
    # ImageDraw fills REPLACE (don't blend) the target pixel, so the low-alpha
    # glass tint drawn straight onto img would set the lens to alpha≈46 and the
    # wallpaper would show through it (background-adaptive lens). On its own layer
    # the translucent glass/arc composite cleanly over the opaque body, giving a
    # consistent dark-blue glass lens on every background.
    mg = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    dm = ImageDraw.Draw(mg, "RGBA")
    dm.ellipse([cx - rr + ring // 2, cy - rr + ring // 2, cx + rr - ring // 2, cy + rr - ring // 2], fill=(0x9A, 0xC8, 0xFF, 46))
    dm.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=ACC + (255,), width=ring)
    dm.arc([cx - rr + ring, cy - rr + ring, cx + rr - ring, cy + rr - ring], start=150, end=215, fill=(255, 255, 255, 150), width=max(4, ring // 3))
    hx = cx + int(rr * 0.70)
    hy = cy + int(rr * 0.70)
    ex = x0 + int(bw * 0.875)
    ey = y0 + int(bw * 0.875)
    dm.line([hx, hy, ex, ey], fill=ACC + (255,), width=int(ring * 1.3))
    dm.ellipse([ex - int(ring * 0.65), ey - int(ring * 0.65), ex + int(ring * 0.65), ey + int(ring * 0.65)], fill=ACC + (255,))
    img.alpha_composite(mg)
    return img.resize((px, px), Image.LANCZOS)


def build_icns(out_dir: Path) -> Path:
    """Render the master PNG and build AppIcon.icns in ``out_dir`` (macOS)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    master = render_png(1024)
    png_path = out_dir / "AppIcon_1024.png"
    master.save(png_path)
    icns_path = out_dir / "AppIcon.icns"
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        sizes = [16, 32, 64, 128, 256, 512, 1024]
        for sz in sizes:
            master.resize((sz, sz)).save(iconset / f"icon_{sz}x{sz}.png")
            if sz <= 512:  # @2x variants
                master.resize((sz * 2, sz * 2)).save(iconset / f"icon_{sz}x{sz}@2x.png")
        if shutil.which("iconutil"):
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)], check=True)
        else:  # fallback: single-size icns via sips
            subprocess.run(["sips", "-s", "format", "icns", str(png_path), "--out", str(icns_path)], check=True)
    return icns_path


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    p = build_icns(here)
    print(f"wrote {p}")
