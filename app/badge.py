"""
Draws a woke-score badge (circle with percentage) onto a poster.

Five bands, matching the official ones from isitwokeornot.com:
0-19 not woke, 20-39 slightly woke, 40-59 woke, 60-79 very woke,
80-100 super woke.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Debian/Ubuntu (fonts-dejavu-core)
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",            # Alpine (font-dejavu)
]

# Upper bound (inclusive) of each band, in the order of BAND_KEYS. The bands
# themselves are the source's own classification, not a free-form setting -
# only the colors are configurable (see COLOR_SCHEMES).
BAND_KEYS = ("not_woke", "slightly_woke", "woke", "very_woke", "super_woke")
BAND_MAX = (19, 39, 59, 79, 100)

# "standard" mirrors the colors isitwokeornot.com uses themselves (Tailwind
# green-500 / lime-600 / amber-500 / orange-500 / rose-500); "modified" is the
# alternative green -> yellow -> orange -> red -> violet ramp.
COLOR_SCHEMES = {
    "standard": {
        "not_woke":      (0x22, 0xC5, 0x5E),
        "slightly_woke": (0x65, 0xA3, 0x0D),
        "woke":          (0xF5, 0x9E, 0x0B),
        "very_woke":     (0xF9, 0x73, 0x16),
        "super_woke":    (0xF4, 0x3F, 0x5E),
    },
    "modified": {
        "not_woke":      (0x22, 0xC5, 0x5E),
        "slightly_woke": (0xEA, 0xB3, 0x08),
        "woke":          (0xF9, 0x73, 0x16),
        "very_woke":     (0xEF, 0x44, 0x44),
        "super_woke":    (0x8B, 0x5C, 0xF6),
    },
}
DEFAULT_COLOR_SCHEME = "standard"

COLOR_WHITE  = (255, 255, 255)
COLOR_SHADOW = (0, 0, 0, 140)

# Marker burned into every poster we generate (JPEG comment segment) - so
# app.py can reliably recognize "this is one of our own badge uploads",
# independent of whatever Plex/plexapi reports as the poster "provider"
# (that turned out not to be reliable).
BADGE_MARKER = b"wokearr-badge"


def score_band(score: int) -> str:
    """Band key for a score, e.g. 73 -> "very_woke"."""
    for key, upper in zip(BAND_KEYS, BAND_MAX):
        if score <= upper:
            return key
    return BAND_KEYS[-1]


def score_color(score: int, color_scheme: str = DEFAULT_COLOR_SCHEME):
    scheme = COLOR_SCHEMES.get(color_scheme) or COLOR_SCHEMES[DEFAULT_COLOR_SCHEME]
    return scheme[score_band(score)]


def _load_font(size: int):
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    # Fallback in case no DejaVu fonts are installed in the container image
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # older Pillow versions don't know the size parameter on load_default()
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


def add_badge(
    poster_path: str,
    score: int,
    out_path: str,
    position: str = "top-right",
    label_style: str = "percent",
    width_percent: float = 20.0,
    color_scheme: str = DEFAULT_COLOR_SCHEME,
):
    """width_percent controls the badge's size relative to the poster width
    (e.g. 20.0 = 20%). For the circle style ("percent") it applies directly
    to the diameter; for the pill style ("woke") proportionally to the font
    size, so both variants scale consistently with the same knob.
    color_scheme picks the band colors (see COLOR_SCHEMES)."""
    img = Image.open(poster_path).convert("RGBA")
    w, h = img.size
    margin = int(w * 0.035)
    color = score_color(score, color_scheme)
    size_fraction = width_percent / 100

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    if label_style == "woke":
        # "X% woke" is noticeably wider than just "X%" - doesn't fit in a
        # fixed circle (would overflow). Use a pill that adapts to the text
        # instead, the same as the web UI uses for the badge chip.
        text = f"{score}% woke"
        font = _load_font(int(w * size_fraction * 0.25))
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
        # Circle badge with centered percentage
        diameter = int(w * size_fraction)
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
    result.save(out_path, quality=92, comment=BADGE_MARKER)
    return out_path
