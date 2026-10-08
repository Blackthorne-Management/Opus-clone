"""Animated word-by-word captions as an ASS subtitle file, burned in by ffmpeg/libass.

Every style shows a short phrase and animates the word currently being spoken. Styles differ
enough that a re-edit with a different style looks clearly different.
"""

from __future__ import annotations

CAPTION_STYLES = ("bold_pop", "karaoke", "boxed", "minimal")


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


def group_words(words: list[dict], max_words: int, max_gap: float = 0.45) -> list[list[dict]]:
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur and (len(cur) >= max_words or w["s"] - cur[-1]["e"] > max_gap or cur[-1]["w"][-1:] in ".!?,"):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return groups


STYLE_PARAMS = {
    #             words, size, uppercase, border_style, outline, shadow
    "bold_pop": dict(words=3, size=96, upper=True, border=1, outline=7, shadow=3),
    "karaoke":  dict(words=6, size=74, upper=False, border=1, outline=5, shadow=2),
    "boxed":    dict(words=4, size=78, upper=True, border=3, outline=14, shadow=0),
    "minimal":  dict(words=7, size=64, upper=False, border=1, outline=3, shadow=1),
}


def build_ass(words: list[dict], duration: float, recipe: dict, brand: dict, width: int = 1080,
              height: int = 1920) -> str:
    style_name = recipe.get("caption_style", "bold_pop")
    p = STYLE_PARAMS.get(style_name, STYLE_PARAMS["bold_pop"])
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
Style: Cap,{font},{p['size']},{ass_color(primary)},{ass_color(highlight)},{out_col},{back},-1,0,0,0,100,100,1,0,{p['border']},{p['outline']},{p['shadow']},{align},90,90,{margin_v},1
Style: Hook,{font},84,{ass_color('#000000')},{ass_color(primary)},{ass_color(highlight)},{ass_color(highlight)},-1,0,0,0,100,100,1,0,3,18,0,8,80,80,{int(height * 0.12)},1
Style: Cta,{font},70,{ass_color(primary)},{ass_color(primary)},{ass_color(outline)},{ass_color(outline, 96)},-1,0,0,0,100,100,1,0,1,6,2,8,90,90,{int(height * 0.30)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []
    hl = _inline_color(highlight)
    base = _inline_color(primary)

    for group in group_words(words, p["words"]):
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
