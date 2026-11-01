from kir_clipper import db
from kir_clipper.virality import clean_clip_copy, clean_text, formula_name, polish, say_seconds, score_hook

DRAFT = """In today's fast-paced world, discipline isn't just a buzzword — it's a game-changer. Let's dive in.

Here's the thing: most people don't fail because they lack motivation. It's not about willpower — it's about structure.

I woke up at 5am for 90 days straight. The result? I paid off $12,000 of debt."""


def test_clean_text_leaves_no_broken_punctuation_or_lowercase_starts():
    out, rep = clean_text(DRAFT)
    assert "—" not in out
    assert ".." not in out and ",." not in out
    assert "\nMost people" in out          # "Here's the thing:" deleted, next word capitalised
    assert "game-changer" not in out.lower()
    assert rep["fixed"] >= 4


def test_structural_tells_are_flagged_in_their_common_forms():
    _, rep = clean_text(DRAFT)
    names = " ".join(f["name"] for f in rep["flags"])
    assert "not just" in names            # "isn't just a buzzword - it's"
    assert "It isn't about" in names      # "It's not about willpower - it's about structure"
    assert "Staged reveal" in names       # "The result?" mid-paragraph


def test_deleted_tail_after_dash_does_not_leave_comma_full_stop():
    out, _ = clean_text("Discipline is a game-changer — let's dive in.")
    assert out == "Discipline is a big deal."


def test_clean_text_keeps_urls_hashtags_and_clean_copy_untouched():
    text = "Comment PLAN and I'll send you the list. #discipline #money https://example.com/a—b"
    out, rep = clean_text(text)
    assert out == text.replace("—", "—")  # the URL is protected, nothing else needed fixing
    assert rep["fixed"] == 0


def test_clip_copy_cleanup_skips_spoken_hook_and_hashtags():
    clip = {"hook": "Here's the thing — nobody tells you this", "title": "A game changer — really",
            "caption": "Let's dive in. Discipline wins.", "hashtags": ["#journey"],
            "hook_overlay": "It's a game-changer", "platform_captions": {"tiktok": {"title": "x — y", "caption": "ok"}}}
    rep = clean_clip_copy(clip)
    assert clip["hook"] == "Here's the thing — nobody tells you this"   # the creator's own words
    assert clip["hashtags"] == ["#journey"]
    assert clip["title"] == "A big deal, really"
    assert clip["caption"] == "Discipline wins."
    assert clip["platform_captions"]["tiktok"]["title"] == "x, y"
    assert rep["fixed"] >= 4


def test_hook_score_reports_formula_and_payoff():
    strong = score_hook("I paid off $12k of debt waking up at 5am")
    weak = score_hook("In this video I'm going to talk about some things I've learned")
    assert strong["payoff"] == "LEADS" and strong["payoff_token"] == "$12k"
    assert strong["score"] > 70 > weak["score"]
    assert weak["payoff"] == "MISSING"
    assert score_hook("") is None
    assert formula_name("I saved $400 on flights last month") == "The Payoff"


def test_polish_returns_checks_for_both_hooks():
    clip = {"hook": "so I was broke at 25", "hook_overlay": "BROKE AT 25 — NOW $10K/MONTH",
            "hook_overlay_alt": "Why I quit my job", "title": "t", "caption": "c"}
    checks = polish(clip)
    assert "—" not in clip["hook_overlay"]
    assert checks["hook"]["text"] == clip["hook_overlay"]
    assert checks["hook_alt"]["text"] == "Why I quit my job"
    assert checks["spoken"]["text"] == "so I was broke at 25"
    assert "copy" in checks


def test_checks_round_trip_through_the_database(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    sid = db.insert_stream(conn, tmp_path / "a.mp4", "h1")
    clip = {"start": 0, "end": 30, "score": 80, "hook": "h", "checks": {"hook": {"score": 81.0}}}
    cid = db.insert_clip(conn, sid, clip, "review")
    assert db.clip_dict(db.get_clip(conn, cid))["checks"] == {"hook": {"score": 81.0}}
    db.update_clip(conn, cid, checks={"hook": {"score": 50.0}})
    assert db.clip_dict(db.get_clip(conn, cid))["checks"]["hook"]["score"] == 50.0


def test_old_databases_gain_the_checks_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE clips (id INTEGER PRIMARY KEY, stream_id INTEGER, parent_clip_id INTEGER,
                   kind TEXT NOT NULL DEFAULT 'short', version INTEGER, start REAL, "end" REAL, score INTEGER,
                   hook TEXT, reason TEXT, title TEXT, caption TEXT, hashtags TEXT, platform_captions TEXT,
                   recipe_json TEXT, file_path TEXT, status TEXT, note TEXT, created_at TEXT, updated_at TEXT)""")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert "checks" in {r["name"] for r in conn.execute("PRAGMA table_info(clips)")}


def test_say_seconds_uses_spoken_words():
    assert 2.0 <= say_seconds("I saved four hundred dollars on flights", 170) <= 3.0
    assert say_seconds("$400", 170) > say_seconds("hi", 170)
