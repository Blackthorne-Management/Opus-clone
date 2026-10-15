"""Animated word-by-word captions as an ASS subtitle file, burned in by ffmpeg/libass.

Every style shows a short phrase and animates the word currently being spoken. Styles differ
enough that a re-edit with a different style looks clearly different.
"""

from __future__ import annotations

import re

CAPTION_STYLES = ("hormozi", "bold_pop", "karaoke", "boxed", "minimal")

# Show each phrase a hair before it is spoken: reading takes a moment, so captions that land
# exactly on the audio feel late.
CAPTION_LEAD = 0.04
# A pause shorter than this keeps the previous phrase on screen (no flicker between phrases).
HOLD_GAP = 0.6

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


def group_words(words: list[dict], max_words: int, max_gap: float = 0.45, max_chars: int = 0) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        too_long = max_chars and sum(len(x["w"]) + 1 for x in cur) + len(w["w"]) > max_chars
        if cur and (len(cur) >= max_words or too_long or w["s"] - cur[-1]["e"] > max_gap
                    or cur[-1]["w"][-1:] in ".!?,"):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


def is_emphasis(word: str) -> bool:
    """Numbers, money, percentages and a short list of power words get the accent colour."""
    return bool(_NUMERIC.search(word)) or word.strip(".,!?;:'\"").upper() in _EMPHASIS


def _hormozi_text(word: str) -> str:
    return _esc(word.upper().strip(".,;:\"").replace("\u2019", "'"))


STYLE_PARAMS = {
    #             words, size (at 1920 tall), uppercase, border_style, outline, shadow
    "hormozi":  dict(words=3, size=150, upper=True, border=1, outline=12, shadow=5, chars=13),
    "bold_pop": dict(words=3, size=96, upper=True, border=1, outline=7, shadow=3),
    "karaoke":  dict(words=6, size=74, upper=False, border=1, outline=5, shadow=2),
    "boxed":    dict(words=4, size=78, upper=True, border=3, outline=14, shadow=0),
    "minimal":  dict(words=7, size=64, upper=False, border=1, outline=3, shadow=1),
}


def build_ass(words: list[dict], duration: float, recipe: dict, brand: dict, width: int = 1080,
              height: int = 1920) -> str:
    style_name = recipe.get("caption_style", "hormozi")
    if style_name not in STYLE_PARAMS:
        style_name = "hormozi"
    p = STYLE_PARAMS[style_name]
    k = height / 1920  # sizes are designed for 1080x1920; scale for other resolutions
    size, outline_w, shadow_w = round(p["size"] * k), round(p["outline"] * k, 1), round(p["shadow"] * k, 1)
    font = brand.get("font", "Arial")
    primary = brand.get("primary_color", "#FFFFFF")
    highlight = brand.get("highlight_color", "#FFD400")
    accent = brand.get("accent_color", "#E63946")
    outline = brand.get("outline_color", "#000000")
    position = recipe.get("caption_position", "lower")
    align, margin_v = (5, 0) if position == "middle" else (2, int(height * 0.27))

    back = ass_color(accent if style_name == "boxed" else outline, 0 if style_name == "boxed" else 96)
    out_col = ass_color(accent if style_name == "boxed" else outline)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
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
    groups = group_words(words, p["words"], 0.35 if style_name == "hormozi" else 0.45, p.get("chars", 0))
    if style_name == "hormozi":
        events.extend(_hormozi_events(groups, lead, hl, base, _inline_color(brand.get("emphasis_color", "#2BFF4A")),
                                      size, p["chars"]))
        groups = []
    for group in groups:
        g_start, g_end = group[0]["s"], group[-1]["e"]
        texts = [_esc(w["w"].upper() if p["upper"] else w["w"]) for w in group]
        if style_name == "karaoke":
            # One event per phrase; \kf sweeps the highlight colour across each word as it is spoken.
            parts = []
            cursor = g_start
            for w, t in zip(group, texts):
                lead = max(0, int(round((w["s"] - cursor) * 100)))
                dur = max(1, int(round((w["e"] - w["s"]) * 100)))
                if lead:
                    parts.append(f"{{\\k{lead}}}")
                parts.append(f"{{\\kf{dur}}}{t} ")
                cursor = w["e"]
            # \kf fills from SecondaryColour to PrimaryColour, so swap them: spoken words turn highlight.
            swap = f"\\1c{hl}\\2c{base}"
            events.append(f"Dialogue: 0,{_ts(g_start)},{_ts(g_end + 0.15)},Cap,,0,0,0,,{{\\fad(80,80){swap}}}{''.join(parts).strip()}")
            continue
        if style_name == "minimal":
            events.append(f"Dialogue: 0,{_ts(g_start)},{_ts(g_end + 0.1)},Cap,,0,0,0,,{{\\fad(120,80)}}{' '.join(texts)}")
            continue
        # bold_pop / boxed: one event per word, whole phrase shown, active word coloured (and popped).
        for i, w in enumerate(group):
            w_end = group[i + 1]["s"] if i + 1 < len(group) else g_end + 0.1
            pieces = []
            for j, t in enumerate(texts):
                if j == i:
                    pop = "\\fscx118\\fscy118\\t(0,90,\\fscx100\\fscy100)" if style_name == "bold_pop" else ""
                    pieces.append(f"{{\\1c{hl}{pop}}}{t}{{\\1c{base}\\fscx100\\fscy100}}")
                else:
                    pieces.append(t)
            events.append(f"Dialogue: 0,{_ts(w['s'])},{_ts(w_end)},Cap,,0,0,0,,{' '.join(pieces)}")

    hook = (recipe.get("hook_text") or "").strip()
    if recipe.get("hook_overlay") and hook:
        events.append(f"Dialogue: 1,{_ts(0)},{_ts(min(2.3, duration))},Hook,,0,0,0,,{{\\fad(120,200)}}{_esc(hook.upper())}")
    cta = (recipe.get("cta_text") or "").strip()
    if recipe.get("outro_cta") and cta and duration > 8:
        events.append(f"Dialogue: 1,{_ts(duration - 2.6)},{_ts(duration)},Cta,,0,0,0,,{{\\fad(200,0)}}{_esc(cta)}")
    return header + "\n".join(events) + "\n"


def _hormozi_events(groups: list[list[dict]], lead: float, hl: str, base: str, emph: str,
                    size: int, max_chars: int) -> list[str]:
    """Hormozi style: 1-3 huge uppercase words, the phrase pops in, the spoken word turns yellow and
    grows, money/numbers/power words stay green. Each phrase is held until the next one starts
    (unless there is a real pause) so the screen never flickers between phrases."""
    events = []
    for gi, group in enumerate(groups):
        nxt = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else None
        g_end = group[-1]["e"] + 0.15
        if nxt is not None and nxt - group[-1]["e"] < HOLD_GAP:
            g_end = nxt
        texts = [_hormozi_text(w["w"]) for w in group]
        # One line, always: a phrase that is still too wide (one long word) is shrunk to fit.
        n_chars = len(" ".join(texts))
        fit = f"\\fs{round(size * max_chars / n_chars)}" if n_chars > max_chars else ""
        for i, w in enumerate(group):
            start = w["s"] - lead
            end = (group[i + 1]["s"] if i + 1 < len(group) else g_end) - lead
            if end - start < 0.04:
                continue
            pieces = []
            for j, t in enumerate(texts):
                colour = emph if is_emphasis(group[j]["w"]) else (hl if j == i else base)
                if j == i and i > 0:  # active word pops; the first word rides the phrase pop-in instead
                    pieces.append(f"{{\\1c{colour}\\fscx122\\fscy122\\t(0,110,\\fscx108\\fscy108)}}{t}"
                                  f"{{\\1c{base}\\fscx100\\fscy100}}")
                else:
                    pieces.append(f"{{\\1c{colour}}}{t}{{\\1c{base}}}")
            intro = "{\\fscx70\\fscy70\\t(0,70,\\fscx108\\fscy108)\\t(70,140,\\fscx100\\fscy100)}" if i == 0 else ""
            events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{{{fit}}}{intro}{' '.join(pieces)}"
                          if fit else f"Dialogue: 0,{_ts(start)},{_ts(end)},Cap,,0,0,0,,{intro}{' '.join(pieces)}")
    return events
