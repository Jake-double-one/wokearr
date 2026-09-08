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


def _badge_box(w, h, margin, badge_w, badge_h, position):
    if position == "top-right":
        x1, y0 = w - margin, margin
        x0 = x1 - badge_w
    elif position == "top-left":
        x0, y0 = margin, margin
        x1 = x0 + badge_w
    elif position == "bottom-right":
        x1 = w - margin
        y0 = h - margin - badge_h
        x0 = x1 - badge_w
    else:  # bottom-left
        x0 = margin
        y0 = h - margin - badge_h
        x1 = x0 + badge_w
    return x0, y0, x1, y0 + badge_h


def add_badge(poster_path: str, score: int, out_path: str, position: str = "top-right", label_style: str = "percent"):
    img = Image.open(poster_path).convert("RGBA")
    w, h = img.size
    margin = int(w * 0.035)
    color = score_color(score)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if label_style == "woke":
        # "X% woke" ist deutlich breiter als nur "X%" - passt nicht in einen
        # festen Kreis (wuerde ueberlaufen). Stattdessen eine sich an den Text
        # anpassende Pille, wie sie auch die Web-UI fuer den Badge-Chip nutzt.
        text = f"{score}% woke"
        font = _load_font(int(w * 0.05))
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad_x, pad_y = int(th * 0.85), int(th * 0.5)
        badge_w, badge_h = tw + pad_x * 2, th + pad_y * 2
        radius = badge_h / 2

        x0, y0, x1, y1 = _badge_box(w, h, margin, badge_w, badge_h, position)
        shadow_pad = int(badge_h * 0.08)
        draw.rounded_rectangle(
            [x0 - shadow_pad, y0 - shadow_pad + shadow_pad, x1 + shadow_pad, y1 + shadow_pad + shadow_pad],
            radius=radius, fill=COLOR_SHADOW,
        )
        draw.rounded_rectangle(
            [x0, y0, x1, y1], radius=radius,
            fill=color + (255,), outline=(255, 255, 255, 230), width=max(2, int(badge_h // 18)),
        )
        tx = x0 + (badge_w - tw) / 2 - bbox[0]
        ty = y0 + (badge_h - th) / 2 - bbox[1]
        draw.text((tx, ty), text, font=font, fill=COLOR_WHITE)
    else:
        # Kreis-Badge mit zentrierter Prozentzahl
        diameter = int(w * 0.22)
        x0, y0, x1, y1 = _badge_box(w, h, margin, diameter, diameter, position)

        shadow_pad = int(diameter * 0.06)
        draw.ellipse(
            [x0 - shadow_pad, y0 - shadow_pad + shadow_pad, x1 + shadow_pad, y1 + shadow_pad + shadow_pad],
            fill=COLOR_SHADOW,
        )
        draw.ellipse([x0, y0, x1, y1], fill=color + (255,), outline=(255, 255, 255, 230), width=max(2, diameter // 22))

        text = f"{score}%"
        font = _load_font(int(diameter * 0.34))
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        tx = x0 + (diameter - tw) / 2 - bbox[0]
        ty = y0 + (diameter - th) / 2 - bbox[1]
        draw.text((tx, ty), text, font=font, fill=COLOR_WHITE)

    result = Image.alpha_composite(img, overlay).convert("RGB")
    result.save(out_path, quality=92)
    return out_path
