"""
Zeichnet eine Ampel-Badge (Kreis mit Prozentzahl) auf ein Poster.
Rot = hoher Woke-Score (Warnung), Gelb = mittel, Gruen = niedrig.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Debian/Ubuntu (fonts-dejavu-core)
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",            # Alpine (font-dejavu)
]

# Schwellenwerte (score in Prozent, 0-100)
THRESH_GREEN_MAX = 33   # 0-33  -> gruen
THRESH_YELLOW_MAX = 66  # 34-66 -> gelb
                        # 67-100 -> rot

COLOR_GREEN  = (46, 160, 67)
COLOR_YELLOW = (240, 173, 13)
COLOR_RED    = (215, 45, 32)
COLOR_WHITE  = (255, 255, 255)
COLOR_SHADOW = (0, 0, 0, 140)


def score_color(score: int):
    if score <= THRESH_GREEN_MAX:
        return COLOR_GREEN
    if score <= THRESH_YELLOW_MAX:
        return COLOR_YELLOW
    return COLOR_RED


def _load_font(size: int):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    # Fallback, falls im Container-Image keine DejaVu-Fonts installiert sind
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # aeltere Pillow-Versionen kennen den size-Parameter bei load_default() nicht
        return ImageFont.load_default()


def add_badge(poster_path: str, score: int, out_path: str, position: str = "top-right", label_style: str = "percent"):
    img = Image.open(poster_path).convert("RGBA")
    w, h = img.size

    # Badge-Groesse relativ zur Posterbreite, damit es bei jeder Aufloesung passt
    diameter = int(w * 0.22)
    margin = int(w * 0.035)

    if position == "top-right":
        x0, y0 = w - diameter - margin, margin
    elif position == "top-left":
        x0, y0 = margin, margin
    elif position == "bottom-right":
        x0, y0 = w - diameter - margin, h - diameter - margin
    else:  # bottom-left
        x0, y0 = margin, h - diameter - margin
    x1, y1 = x0 + diameter, y0 + diameter

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # weicher Schatten hinter dem Kreis fuer Lesbarkeit auf hellen Postern
    shadow_pad = int(diameter * 0.06)
    draw.ellipse(
        [x0 - shadow_pad, y0 - shadow_pad + shadow_pad, x1 + shadow_pad, y1 + shadow_pad + shadow_pad],
        fill=COLOR_SHADOW,
    )
    # farbiger Kreis + weisser Rand
    color = score_color(score)
    draw.ellipse([x0, y0, x1, y1], fill=color + (255,), outline=(255, 255, 255, 230), width=max(2, diameter // 22))

    # Prozentzahl zentriert - "% woke" ist etwas breiter, Schrift dafuer leicht kleiner
    text = f"{score}% woke" if label_style == "woke" else f"{score}%"
    font_scale = 0.24 if label_style == "woke" else 0.34
    font = _load_font(int(diameter * font_scale))
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x0 + (diameter - tw) / 2 - bbox[0]
    ty = y0 + (diameter - th) / 2 - bbox[1]
    draw.text((tx, ty), text, font=font, fill=COLOR_WHITE)

    result = Image.alpha_composite(img, overlay).convert("RGB")
    result.save(out_path, quality=92)
    return out_path
