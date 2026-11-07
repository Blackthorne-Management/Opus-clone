"""Animated word-by-word captions as an ASS subtitle file, burned in by ffmpeg/libass.

Phrasing follows the caption rules of the master-skills /edit skill (Jake Schincariol, MIT): 1-3 word
phrases that break after . ! ?, after a comma once a phrase has 2 words, on pauses of 0.3 s or more and at a
character limit; a phrase never ends on a weak opener ("the", "to", "a"), a phrase too quick to read grows by a
word, ums are hidden, and each phrase stays up until the next one starts (or a quarter second after a real
pause). The spoken word changes colour only, never size, so the line never shifts while you talk, and every
phrase is sized to fit on one line.
"""

from __future__ import annotations

import re

CAPTION_STYLES = ("hormozi", "bold_pop", "karaoke", "boxed", "minimal")

# Show each phrase a hair before it is spoken: reading takes a moment, so captions that land
# exactly on the audio feel late.
CAPTION_LEAD = 0.04
# A pause up to this keeps the previous phrase on screen (no flicker between phrases).
HOLD_GAP = 0.5
# A phrase stays up at least this long, so it can be read.
MIN_PHRASE = 0.4
FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhm", "er", "erm", "ah", "ahh", "hmm", "hm", "mm", "mmm"}
# Words that open a phrase: a full phrase shouldn't end on one when the sentence carries on.
WEAK_ENDINGS = {"a", "an", "the", "to", "of", "and", "or", "but", "for", "in", "on", "at", "with", "from", "by",
                "into", "my", "your", "our", "their", "his", "her", "its", "if", "as", "is", "are", "was", "were",
                "will", "can"}
# Average width of a heavy uppercase letter as a share of the font size (Montserrat Black measured at ~0.62-0.68).
CHAR_W = 0.68

_EMPHASIS = set("""MONEY MILLION MILLIONS BILLION BILLIONS THOUSAND RICH BROKE WEALTH DEBT NEVER ALWAYS FREE SECRET
STOP MISTAKE MISTAKES WRONG BEST WORST FAST EASY HARD WIN LOSE QUIT FAIL FAILED FAILURE SUCCESS DISCIPLINE
DOLLARS DOLLAR PROFIT CASH INCOME BOSS NOBODY EVERYBODY EVERYONE TRUTH LIE LIES""".split())
_NUMERIC = re.compile(r"[\d$%]")


def ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _inline_color(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _norm(text: str) -> str:
    return re.sub(r"[^\w']+", "", text.lower()).strip("'")


def phrases(words: list[dict], max_words: int = 3, max_chars: int = 18, pause: float = 0.3,
            min_dur: float = MIN_PHRASE) -> list[dict]:
    """Caption phrases [{"start", "end", "words"}] from word timings ({"w", "s", "e"}). Ums are left out."""
    words = [w for w in words if _norm(w["w"]) not in FILLERS and _norm(w["w"])]
    chunks, cur = [], []
    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else None
        cur.append(word)
        text_len = len(" ".join(w["w"] for w in cur))
        hard = word["w"][-1:] in ".!?" or nxt is None or nxt["s"] - word["e"] >= pause
        soft = word["w"][-1:] in ",;:" and len(cur) >= 2
        nxt_len = text_len + 1 + len(nxt["w"]) if nxt else 0
        full = len(cur) >= max_words or (nxt is not None and nxt_len > max_chars)
        if not (hard or soft or full):
            continue
        if (not hard and len(cur) < max_words and nxt is not None and nxt_len <= max_chars + 4
                and nxt["e"] - cur[0]["s"] < 1.2 and word["e"] - cur[0]["s"] < min_dur):
            continue                                   # too quick to read: let it grow by one word
        if full and not hard and len(cur) >= 2 and _norm(word["w"]) in WEAK_ENDINGS:
            carry = cur.pop()                          # "...to" moves to the next phrase
            chunks.append(cur)
            cur = [carry]
            continue
        chunks.append(cur)
        cur = []
    if cur:
        chunks.append(cur)
    out = []
    for i, group in enumerate(chunks):
        start, end = group[0]["s"], group[-1]["e"]
        if i + 1 < len(chunks):
            nxt_start = chunks[i + 1][0]["s"]
            end = nxt_start if nxt_start - end <= HOLD_GAP else end + 0.25
            end = min(max(end, start + min_dur), nxt_start)
        else:
            end = max(end + 0.25, start + min_dur)
        out.append({"start": start, "end": end, "words": group})
    return out


def group_words(words: list[dict], max_words: int, max_gap: float = 0.3, max_chars: int = 18) -> list[list[dict]]:
    return [p["words"] for p in phrases(words, max_words, max_chars or 18, max_gap)]


def is_emphasis(word: str) -> bool:
    """Numbers, money, percentages and a short list of power words get the accent colour."""
    return bool(_NUMERIC.search(word)) or word.strip(".,!?;:'\"").upper() in _EMPHASIS


def _hormozi_text(word: str) -> str:
    return _esc(word.upper().strip(".,;:\"").replace("\u2019", "'"))


STYLE_PARAMS = {
    #             words, size (at 1920 tall), uppercase, border_style, outline, shadow
    "hormozi":  dict(words=3, size=112, upper=True, border=1, outline=10, shadow=4, chars=16),
    "bold_pop": dict(words=3, size=92, upper=True, border=1, outline=7, shadow=3, chars=18),
    "karaoke":  dict(words=6, size=74, upper=False, border=1, outline=5, shadow=2, chars=32),
    "boxed":    dict(words=3, size=78, upper=True, border=3, outline=14, shadow=0, chars=18),
    "minimal":  dict(words=6, size=64, upper=False, border=1, outline=3, shadow=1, chars=34),
}


def build_ass(words: list[dict], duration: float, recipe: dict, brand: dict, width: int = 1080,
              height: int = 1920) -> str:
    style_name = recipe.get("caption_style", "hormozi")
    if style_name not in STYLE_PARAMS:
        style_name = "hormozi"
    p = dict(STYLE_PARAMS[style_name])
    # Your style's tweaks (style.py): size, words at a time, font and colours.
    scale = float(recipe.get("caption_size") or 1.0)
    if abs(scale - 1.0) > 0.01:
        p["size"] = p["size"] * scale
        if p.get("chars"):
            p["chars"] = max(6, round(p["chars"] / scale))   # bigger words, fewer per line
    if recipe.get("caption_words"):
        p["words"] = int(recipe["caption_words"])
    brand = {**brand, **{key: recipe[key] for key in ("primary_color", "highlight_color", "emphasis_color")
                         if recipe.get(key)}}
    if recipe.get("caption_font"):
        brand["font"] = recipe["caption_font"]
    k = height / 1920  # sizes are designed for 1080x1920; scale for other resolutions
    size, outline_w, shadow_w = round(p["size"] * k), round(p["outline"] * k, 1), round(p["shadow"] * k, 1)
    font = brand.get("font", "Arial")
    primary = brand.get("primary_color", "#FFFFFF")
    highlight = brand.get("highlight_color", "#FFD400")
    accent = brand.get("accent_color", "#E63946")
    outline = brand.get("outline_color", "#000000")
    position = recipe.get("caption_position", "lower")
    align, margin_v = (5, 0) if position == "middle" else (2, int(height * 0.27))
    if recipe.get("caption_y"):                     # bottom of the captions at this share of the height
        align, margin_v = 2, int(height * (1 - float(recipe["caption_y"])))

    back = ass_color(accent if style_name == "boxed" else outline, 0 if style_name == "boxed" else 96)
    out_col = ass_color(accent if style_name == "boxed" else outline)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: {2 if style_name in ("hormozi", "bold_pop", "boxed") else 0}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{size},{ass_color(primary)},{ass_color(highlight)},{out_col},{back},-1,0,0,0,100,100,1,0,{p['border']},{outline_w},{shadow_w},{align},{round(70 * k)},{round(70 * k)},{margin_v},1
Style: Hook,{font},84,{ass_color('#000000')},{ass_color(primary)},{ass_color(highlight)},{ass_color(highlight)},-1,0,0,0,100,100,1,0,3,18,0,8,80,80,{int(height * 0.12)},1
Style: Cta,{font},70,{ass_color(primary)},{ass_color(primary)},{ass_color(outline)},{ass_color(outline, 96)},-1,0,0,0,100,100,1,0,1,6,2,8,90,90,{int(height * 0.30)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    hl = _inline_color(highlight)
    base = _inline_color(primary)

    lead = float(recipe.get("caption_lead", CAPTION_LEAD))
    emph = _inline_color(brand.get("emphasis_color", "#2BFF4A"))
    avail = width - 2 * round(70 * k)                 # the line must fit between the side margins
    one_line = style_name in ("hormozi", "bold_pop", "boxed")
    for cue in phrases(words, p["words"], p["chars"]):
        group = cue["words"]
        upper = p["upper"]
        texts = [(_hormozi_text(w["w"]) if style_name == "hormozi" else _esc(w["w"].upper() if upper else w["w"]))
                 for w in group]
        c_start, c_end = cue["start"] - lead, cue["end"] - lead
        # Too wide for one line: shrink this phrase (all of its words alike, so nothing jumps mid-phrase).
        est = CHAR_W * size * len(" ".join(texts))
        fit = f"\\fs{max(12, int(size * avail / est))}" if one_line and est > avail else ""
        if style_name == "karaoke":
            # One event per phrase; \kf sweeps the highlight colour across each word as it is spoken.
            parts, cursor = [], cue["start"]
            for w, t in zip(group, texts):
                gap = max(0, int(round((w["s"] - cursor) * 100)))
                dur = max(1, int(round((w["e"] - w["s"]) * 100)))
                if gap:
                    parts.append(f"{{\\k{gap}}}")
                parts.append(f"{{\\kf{dur}}}{t} ")
                cursor = w["e"]
            swap = f"\\1c{hl}\\2c{base}"         # \kf fills Secondary -> Primary, so spoken words turn highlight
            events.append(f"Dialogue: 0,{_ts(c_start)},{_ts(c_end)},Cap,,0,0,0,,{{\\fad(60,60){swap}}}{''.join(parts).strip()}")
            continue
        if style_name == "minimal":
            events.append(f"Dialogue: 0,{_ts(c_start)},{_ts(c_end)},Cap,,0,0,0,,{{\\fad(80,60)}}{' '.join(texts)}")
            continue
        # hormozi / bold_pop / boxed: the whole phrase shows; the spoken word changes colour (never size).
        for i, w in enumerate(group):
            start = c_start if i == 0 else w["s"] - lead
            end = c_end if i == len(group) - 1 else group[i + 1]["s"] - lead
            if end - start < 0.02:
                continue
            pieces = []
            for j, t in enumerate(texts):
                colour = emph if (style_name == "hormozi" and is_emphasis(group[j]["w"])) else (hl if j == i else base)
                pieces.append(f"{{\\1c{colour}}}{t}{{\\1c{base}}}")
            # The phrase settles in once when it appears (the whole line together), then holds still.
            intro = ("\\fscx94\\fscy94\\t(0,90,\\fscx100\\fscy100)" if i == 0 and style_name in ("hormozi", "bold_pop")
                     else "")
            tags = f"{{{fit}{intro}}}" if (fit or intro) else ""
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{tags}{' '.join(pieces)}")

    hook = (recipe.get("hook_text") or "").strip()
    if recipe.get("hook_overlay") and hook:
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(min(2.3, duration))},Hook,,0,0,0,,{{\\fad(120,200)}}{_esc(hook.upper())}")
    cta = (recipe.get("cta_text") or "").strip()
    if recipe.get("outro_cta") and cta and duration > 8:
        events.append(f"Dialogue: 1,{_ts(duration - 2.6)},{_ts(duration)},Cta,,0,0,0,,{{\\fad(200,0)}}{_esc(cta)}")
    return header + "\n".join(events) + "\n"

