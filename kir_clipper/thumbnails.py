"""YouTube thumbnails, designed from scratch (not video frames) in one consistent channel theme.

Every thumbnail uses the same system so the channel page looks like a set:
  - complementary red / blue backgrounds (split, spotlight or versus layout) with glow and vignette,
  - 1-4 huge words in a heavy condensed font, thick black outline, one word in the highlight colour,
  - optionally a cut-out photo of the creator (transparent PNGs in brand/thumbnail/person/, named by
    expression: shocked.png, serious.png, pointing.png...) with a white outline so it pops,
  - one accent graphic (arrow, ring, money, check/cross, question mark, rising chart),
  - the channel logo, small, in the same corner every time.
Claude writes the concepts (words, emphasis, layout, expression, accent) aimed at curiosity and emotion;
this module only draws them, so the look never drifts.
"""

from __future__ import annotations

import logging
import math
import os
import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .config import Config

log = logging.getLogger("kir.thumbnails")

W, H = 1280, 720
LAYOUTS = ("split", "spotlight", "versus")
ACCENTS = ("arrow", "ring", "money", "check_x", "question", "chart_up", "none")

DEFAULT_THEME = {
    "primary": "#E11D2E",     # red
    "secondary": "#1D4ED8",   # blue (red/blue: complementary, high contrast)
    "dark": "#0B1026",
    "text": "#FFFFFF",
    "highlight": "#FFD400",   # yellow emphasis word
    "outline": "#000000",
}

CONCEPT_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "emphasis": {"type": "string"},
                    "layout": {"type": "string", "enum": list(LAYOUTS)},
                    "expression": {"type": "string"},
                    "accent": {"type": "string", "enum": list(ACCENTS)},
                    "why": {"type": "string"},
                },
                "required": ["text", "emphasis", "layout", "expression", "accent", "why"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["concepts"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- assets

def theme(cfg: Config) -> dict:
    return {**DEFAULT_THEME, **(cfg.get("thumbnails.palette") or {})}


def _rgb(hex_color: str, alpha: int = 255) -> tuple:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)


def font_path(cfg: Config) -> str:
    """Thumbnail font: configured file, bundled Anton, then Windows/Linux heavy fallbacks."""
    candidates = []
    if cfg.get("thumbnails.font"):
        candidates.append(str(cfg.resolve(cfg.get("thumbnails.font"))))
    candidates += [str(p) for p in sorted(cfg.resolve("brand/fonts/thumbnail").glob("*.ttf"))]
    windir = os.environ.get("WINDIR", "C:/Windows")
    candidates += [f"{windir}/Fonts/impact.ttf", f"{windir}/Fonts/arialbd.ttf",
                   "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise FileNotFoundError("No bold font found for thumbnails; put a .ttf in brand/fonts/thumbnail/")


def person_dir(cfg: Config) -> Path:
    return cfg.resolve(cfg.get("thumbnails.person_dir", "brand/thumbnail/person"))


def expressions(cfg: Config) -> list[str]:
    d = person_dir(cfg)
    if not d.exists():
        return []
    return sorted({p.stem.lower() for p in d.iterdir() if p.suffix.lower() in (".png", ".webp", ".jpg", ".jpeg")})


def load_person(cfg: Config, expression: str) -> Image.Image | None:
    """Cut-out of the creator. Transparent PNGs are used as-is; others are cut out with rembg if installed."""
    d = person_dir(cfg)
    if not d.exists():
        return None
    files = [p for p in d.iterdir() if p.suffix.lower() in (".png", ".webp", ".jpg", ".jpeg")]
    if not files:
        return None
    match = [p for p in files if p.stem.lower() == (expression or "").lower()] or files
    path = match[0]
    img = Image.open(path).convert("RGBA")
    if img.getchannel("A").getextrema()[0] == 255:  # no transparency: try to cut the background out
        cached = path.with_suffix(".cutout.png")
        if cached.exists():
            return Image.open(cached).convert("RGBA")
        try:
            from rembg import remove  # optional dependency
            img = remove(img)
            img.save(cached)
        except ImportError:
            log.warning("%s has no transparent background; use a cut-out PNG or install rembg", path.name)
            return None
    bbox = img.getchannel("A").getbbox()
    return img.crop(bbox) if bbox else img


# --------------------------------------------------------------------------- drawing helpers

def _radial(size: tuple[int, int], center: tuple[float, float], radius: float, color: tuple) -> Image.Image:
    """Soft radial glow layer."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    mask = Image.new("L", size, 0)
    md = ImageDraw.Draw(mask)
    cx, cy = center
    steps = 40
    for i in range(steps, 0, -1):
        r = radius * i / steps
        md.ellipse([cx - r, cy - r, cx + r, cy + r], fill=int(255 * (1 - i / steps) ** 1.6))
    layer.paste(Image.new("RGBA", size, color), (0, 0), mask)
    return layer


def _vignette(img: Image.Image, strength: float = 0.55) -> Image.Image:
    mask = Image.new("L", img.size, 0)
    d = ImageDraw.Draw(mask)
    for i in range(30):
        pad = i * 9
        d.rectangle([pad, pad, img.width - pad, img.height - pad], outline=int(255 * strength * (1 - i / 30)), width=9)
    mask = mask.filter(ImageFilter.GaussianBlur(40))
    dark = Image.new("RGBA", img.size, (0, 0, 0, 255))
    return Image.composite(dark, img, mask)


def _noise(img: Image.Image, seed: int, amount: int = 10) -> Image.Image:
    rnd = random.Random(seed)
    small = Image.new("L", (W // 4, H // 4))
    small.putdata([128 + rnd.randint(-amount, amount) for _ in range(small.width * small.height)])
    grain = small.resize(img.size, Image.BILINEAR).convert("RGBA")
    return ImageChops.overlay(img, grain)


def background(layout: str, th: dict, seed: int) -> Image.Image:
    red, blue, dark = _rgb(th["primary"]), _rgb(th["secondary"]), _rgb(th["dark"])
    img = Image.new("RGBA", (W, H), dark)
    d = ImageDraw.Draw(img)
    if layout == "split":
        d.polygon([(0, 0), (int(W * 0.62), 0), (int(W * 0.48), H), (0, H)], fill=blue)
        d.polygon([(int(W * 0.62), 0), (W, 0), (W, H), (int(W * 0.48), H)], fill=red)
        streak = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ImageDraw.Draw(streak).line([(int(W * 0.62), -10), (int(W * 0.48), H + 10)], fill=(255, 255, 255, 220), width=14)
        img.alpha_composite(streak.filter(ImageFilter.GaussianBlur(6)))
        img.alpha_composite(_radial((W, H), (W * 0.78, H * 0.55), 520, (255, 120, 120, 150)))
    elif layout == "versus":
        d.rectangle([0, 0, W // 2, H], fill=red)
        d.rectangle([W // 2, 0, W, H], fill=blue)
        img.alpha_composite(_radial((W, H), (W * 0.25, H * 0.5), 460, (255, 150, 150, 120)))
        img.alpha_composite(_radial((W, H), (W * 0.75, H * 0.5), 460, (150, 180, 255, 120)))
    else:  # spotlight
        d.rectangle([0, 0, W, H], fill=blue)
        img.alpha_composite(_radial((W, H), (W * 0.74, H * 0.6), 620, red))
        rays = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        rd = ImageDraw.Draw(rays)
        for k in range(14):
            a = math.radians(k * 360 / 14 + seed % 30)
            rd.polygon([(W * 0.74, H * 0.6), (W * 0.74 + 1600 * math.cos(a), H * 0.6 + 1600 * math.sin(a)),
                        (W * 0.74 + 1600 * math.cos(a + 0.09), H * 0.6 + 1600 * math.sin(a + 0.09))],
                       fill=(255, 255, 255, 22))
        img.alpha_composite(rays)
    img = _noise(img, seed)
    return _vignette(img)


def _outlined_person(person: Image.Image, height: int) -> Image.Image:
    scale = height / person.height
    p = person.resize((max(1, int(person.width * scale)), height), Image.LANCZOS)
    pad = 24
    canvas = Image.new("RGBA", (p.width + pad * 2, p.height + pad * 2), (0, 0, 0, 0))
    alpha = Image.new("L", canvas.size, 0)
    alpha.paste(p.getchannel("A"), (pad, pad))
    outline = alpha.filter(ImageFilter.MaxFilter(15))
    shadow = alpha.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.GaussianBlur(14))
    out = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    out.paste(Image.new("RGBA", canvas.size, (0, 0, 0, 170)), (10, 10), shadow)
    out.paste(Image.new("RGBA", canvas.size, (255, 255, 255, 255)), (0, 0), outline)
    out.alpha_composite(p, (pad, pad))
    return out


def _fit_lines(words: list[str], font_file: str, max_w: int, max_h: int) -> tuple[list[list[str]], ImageFont.FreeTypeFont]:
    """Split into 1-2 lines and find the biggest size that fits."""
    options = [[words]] + [[words[:i], words[i:]] for i in range(1, len(words))]
    options += [[words[:i], words[i:j], words[j:]] for i in range(1, len(words)) for j in range(i + 1, len(words))]
    best = None
    for lines in options:
        for size in range(230, 40, -6):
            f = ImageFont.truetype(font_file, size)
            widths = [f.getlength(" ".join(l)) for l in lines]
            height = len(lines) * size * 1.02
            if max(widths) <= max_w and height <= max_h:
                if best is None or size > best[1].size:
                    best = (lines, f)
                break
    return best or ([words], ImageFont.truetype(font_file, 60))


def _draw_text(img: Image.Image, text: str, emphasis: str, box: tuple[int, int, int, int], th: dict, font_file: str,
               align: str = "left") -> tuple[int, int, int, int]:
    """Draws the words and returns the bounding box they ended up using."""
    words = text.upper().split()[:4]
    emph = {w.strip(".,!?").upper() for w in emphasis.split()}
    x0, y0, x1, y1 = box
    lines, font = _fit_lines(words, font_file, x1 - x0, y1 - y0)
    size = font.size
    total_h = len(lines) * size * 1.02
    y = y0 + (y1 - y0 - total_h) / 2
    top, right = y, x0
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sd, ld = ImageDraw.Draw(shadow), ImageDraw.Draw(layer)
    stroke = max(6, size // 14)
    for line in lines:
        line_w = font.getlength(" ".join(line))
        x = x0 if align == "left" else x0 + (x1 - x0 - line_w) / 2
        for w in line:
            color = th["highlight"] if w.strip(".,!?") in emph else th["text"]
            sd.text((x + 8, y + 10), w, font=font, fill=(0, 0, 0, 200), stroke_width=stroke, stroke_fill=(0, 0, 0, 200))
            ld.text((x, y), w, font=font, fill=_rgb(color), stroke_width=stroke, stroke_fill=_rgb(th["outline"]))
            x += font.getlength(w + " ")
        right = max(right, x)
        y += size * 1.02
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)))
    img.alpha_composite(layer)
    return int(x0), int(top), int(right), int(y)


def _accent(img: Image.Image, kind: str, at: tuple[int, int], th: dict, font_file: str) -> None:
    if kind == "none":
        return
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x, y = at
    yellow, red, black, white = _rgb(th["highlight"]), _rgb(th["primary"]), (0, 0, 0, 255), (255, 255, 255, 255)
    if kind == "arrow":
        pts = [(x - 120, y + 90), (x - 30, y - 10), (x + 40, y - 40)]
        d.line(pts, fill=black, width=46, joint="curve")
        d.line(pts, fill=yellow, width=30, joint="curve")
        head = [(x + 85, y - 60), (x + 10, y - 95), (x + 30, y + 5)]
        d.polygon(head, fill=yellow, outline=black, width=6)
    elif kind == "ring":
        d.ellipse([x - 110, y - 110, x + 110, y + 110], outline=black, width=26)
        d.ellipse([x - 110, y - 110, x + 110, y + 110], outline=red, width=16)
    elif kind in ("money", "question"):
        glyph = "$" if kind == "money" else "?"
        d.ellipse([x - 95, y - 95, x + 95, y + 95], fill=yellow, outline=black, width=8)
        f = ImageFont.truetype(font_file, 150)
        d.text((x, y + 4), glyph, font=f, fill=black, anchor="mm")
    elif kind == "check_x":
        d.ellipse([x - 150, y - 70, x - 10, y + 70], fill=(220, 30, 40, 255), outline=black, width=6)
        d.line([(x - 115, y - 35), (x - 45, y + 35)], fill=white, width=16)
        d.line([(x - 45, y - 35), (x - 115, y + 35)], fill=white, width=16)
        d.ellipse([x + 10, y - 70, x + 150, y + 70], fill=(30, 170, 80, 255), outline=black, width=6)
        d.line([(x + 40, y + 2), (x + 70, y + 32), (x + 120, y - 28)], fill=white, width=16, joint="curve")
    elif kind == "chart_up":
        pts = [(x - 150, y + 80), (x - 70, y + 20), (x - 10, y + 50), (x + 110, y - 80)]
        d.line(pts, fill=black, width=34, joint="curve")
        d.line(pts, fill=(40, 200, 90, 255), width=20, joint="curve")
        d.polygon([(x + 150, y - 115), (x + 75, y - 100), (x + 125, y - 45)], fill=(40, 200, 90, 255), outline=black, width=5)
    shadow = layer.copy()
    shadow_alpha = shadow.getchannel("A").filter(ImageFilter.GaussianBlur(10))
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sh.paste(Image.new("RGBA", img.size, (0, 0, 0, 150)), (8, 10), shadow_alpha)
    img.alpha_composite(sh)
    img.alpha_composite(layer)


# --------------------------------------------------------------------------- compose

def render(cfg: Config, concept: dict, out: Path, seed: int = 0) -> Path:
    th = theme(cfg)
    font_file = font_path(cfg)
    layout = concept.get("layout") if concept.get("layout") in LAYOUTS else "split"
    img = background(layout, th, seed)
    person = load_person(cfg, concept.get("expression", ""))
    if person is not None:
        cut = _outlined_person(person, int(H * 0.98))
        px = W - cut.width + 40 if layout != "versus" else (W - cut.width) // 2 + 220
        px, py = max(int(W * 0.40), px), H - cut.height + 30
        img.alpha_composite(cut, (px, py))
        face = (px, py, px + cut.width, py + int(cut.height * 0.36))
        text_box = (50, 50, int(W * 0.56), H - 50)
    else:
        text_box = (60, 60, int(W * 0.70), H - 60)
        face = (W, H, W, H)
    tx0, ty0, tx1, ty1 = _draw_text(img, concept.get("text", ""), concept.get("emphasis", ""), text_box, th, font_file)
    # The accent goes in empty space only: right of the words (no photo), else above or below them.
    candidates = [((tx1 + W) // 2, H // 2)] if person is None else []
    candidates += [((tx0 + tx1) // 2, ty0 // 2 + 10), ((tx0 + tx1) // 2, (ty1 + H) // 2),
                   (tx1 + 170, int(H * 0.2)), (tx1 + 170, int(H * 0.8)),
                   (min(tx1 + 170, face[0] - 120), int(H * 0.25)), (face[0] - 130, int(H * 0.75))]
    half = 160  # accent graphics fit in roughly a 320px square

    def clear(c):
        x, y = c
        inside = half <= x <= W - half + 40 and half - 60 <= y <= H - half + 60
        hits_text = x + half > tx0 and x - half < tx1 and y + half > ty0 and y - half < ty1
        hits_face = x + half > face[0] and x - half < face[2] and y + half > face[1] and y - half < face[3]
        return inside and not hits_text and not hits_face

    accent_at = next((c for c in candidates if clear(c)), None)
    if accent_at:
        _accent(img, concept.get("accent", "none"), accent_at, th, font_file)
    logo_cfg = cfg.get("brand.logo")
    logo = cfg.resolve(logo_cfg) if logo_cfg else None
    if logo and logo.exists() and cfg.get("thumbnails.logo", True):
        lg = Image.open(logo).convert("RGBA")
        lg.thumbnail((120, 120))
        img.alpha_composite(lg, (W - lg.width - 24, 20))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out, "JPEG", quality=90, optimize=True)  # YouTube limit is 2 MB
    return out


def concepts(cfg: Config, llm, meta: dict, chapters: list[str], count: int = 3, avoid: list[str] | None = None) -> list[dict]:
    exprs = expressions(cfg)
    expr_line = (f"Available photos of the creator (use exactly one of these names for 'expression'): {', '.join(exprs)}."
                 if exprs else "There is no photo of the creator: set 'expression' to an empty string.")
    avoid_line = f"\nDon't reuse these earlier thumbnail texts: {', '.join(avoid)}" if avoid else ""
    system = (f"You design YouTube thumbnails for {cfg.get('brand.name')}'s channel. Every thumbnail on the channel "
              "uses one consistent theme (red/blue complementary background, huge 1-4 word outlined text with one "
              "yellow word, the creator's cut-out photo, one accent graphic), so viewers recognise the channel at a "
              "glance. Your job is the concept: the words, which word to highlight, the layout, the expression and "
              "the accent.")
    user = f"""Video title: {meta.get('title')}
Description: {meta.get('description', '')[:1500]}
Chapters: {'; '.join(chapters[:15])}

{expr_line}{avoid_line}

Write {count} different thumbnail concepts that maximise click-through while staying honest to the video:
- text: 1 to 4 words, readable on a phone. Don't repeat the title; add curiosity, stakes or emotion
  (a bold claim, a number, a before/after, a question). No hashtags, no emojis.
- emphasis: the single word from text to show in yellow (the one that carries the emotion).
- layout: split (default, red/blue diagonal), spotlight (one big idea, dramatic), versus (a comparison).
- expression: the photo whose emotion matches the text.
- accent: one graphic that supports the idea (arrow points at the key thing, ring circles it, money for
  income/business, check_x for right-vs-wrong, question for curiosity, chart_up for growth, none if cleaner).
- why: one short sentence on why it would get clicked.
Make the {count} concepts genuinely different angles so the owner has a real choice."""
    result = llm(system, user, CONCEPT_SCHEMA)
    out = []
    for c in result.get("concepts", [])[:count]:
        c["text"] = " ".join(c.get("text", "").split()[:4])
        if c.get("expression") and exprs and c["expression"].lower() not in exprs:
            c["expression"] = exprs[0]
        out.append(c)
    return out


def generate(cfg: Config, llm, meta: dict, chapters: list[str], out_dir: Path, stem: str,
             count: int | None = None, avoid: list[str] | None = None) -> list[dict]:
    count = count or int(cfg.get("thumbnails.count", 3))
    made = []
    for i, c in enumerate(concepts(cfg, llm, meta, chapters, count, avoid)):
        path = render(cfg, c, out_dir / f"{stem}_thumb{i + 1}.jpg", seed=hash((stem, i)) & 0xFFFF)
        made.append({**c, "path": str(path)})
    log.info("Generated %d thumbnails for %s", len(made), stem)
    return made
