"""Niche research, Script studio and Edit a video (the features merged in from the master-skills repo)."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from kir_clipper import db, editor, research, studio
from kir_clipper.highlights import REFINE_SCHEMA
from kir_clipper.web.app import create_app

PASTE = """bigcoach | 2.1M | 5 money rules I wish I knew at 20
bigcoach | 1.8M | Why you're still broke
bigcoach | 2.4M | The 3am habit
bigcoach | 2.0M | Stop saving money
smallcoach | 4,200 | Morning routine for discipline
smallcoach | 3,900 | How I read 50 books
smallcoach | 61,000 | I paid off $12k of debt waking up at 5am | https://www.tiktok.com/@smallcoach/video/1
smallcoach | 4,500 | Gym consistency
thincoach | 900k | Only two posts here
thincoach | 100k | Second one
"""


def test_research_ranks_by_each_creators_own_median(conn):
    got = research.add_posts(conn, PASTE)
    assert got == {"added": 10, "duplicates": 0, "skipped": 0}
    assert research.add_posts(conn, PASTE)["duplicates"] == 10       # pasting twice adds nothing
    rep = research.outliers(conn, min_multiple=2.0, days=None)
    assert [o["creator"] for o in rep["outliers"]] == ["smallcoach"]   # 61k vs ~4.4k median, not bigcoach's 2M
    top = rep["outliers"][0]
    assert top["multiple"] > 13 and top["formula"] == "The Payoff"
    assert {t["creator"] for t in rep["thin"]} == {"thincoach"}


def test_research_supplied_median_and_removal(conn):
    research.add_posts(conn, "creator,views,title,median\n@one,50000,I tried it for 30 days,10000\n")
    rep = research.outliers(conn, min_multiple=2.0, days=None)
    assert rep["outliers"][0]["multiple"] == 5.0                     # a median column counts without 4 posts
    assert research.remove(conn, creator="@one") == 1
    assert research.outliers(conn, days=None)["posts"] == 0


def test_research_fetch_uses_ytdlp_json(conn):
    dump = {"uploader_id": "@shortsguy", "entries": [{"title": f"Clip {i}", "view_count": v, "url": f"https://youtube.com/shorts/{i}"}
                                                     for i, v in enumerate([1000, 1200, 900, 9000])]}
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps(dump), stderr="")

    assert research.fetch_channel(conn, "shortsguy", runner=runner)["added"] == 4
    assert calls[0][-1] == "https://www.youtube.com/@shortsguy/shorts"
    assert "--cookies" not in " ".join(calls[0])                         # never logs in
    assert research.outliers(conn, days=None)["outliers"][0]["views"] == 9000


class ScriptLLM:
    def __init__(self):
        self.calls = []

    def __call__(self, system, user, schema, max_tokens=16000):
        self.calls.append((system, user))
        if "voice_md" in schema["properties"]:
            return {"voice_md": "# voice.md\n\n- **Pace:** 172 wpm\n"}
        first = len(self.calls) == 1
        return {
            "working_title": "Debt at 5am", "why_it_works": "Payoff first, receipt second.",
            "hooks": [{"formula": "The Payoff", "spoken": "I paid off {{your number}} in 90 days of 5am alarms.",
                       "on_screen": "PAID OFF IN 90 DAYS"},
                      {"formula": "The Statistic", "spoken": "I paid off $12,000 by waking up at 5am.",
                       "on_screen": "$12,000. 5AM."},
                      {"formula": "Question", "spoken": "Why do mornings matter so much?", "on_screen": "WHY MORNINGS?"}],
            "beats": [{"label": "PROOF", "say": "Here's the last statement. Zero balance.",
                       "screen": "THE ACTUAL STATEMENT", "visual": "Statement / zero circled"},
                      {"label": "REHOOK", "say": ("It's not about discipline — it's about the alarm across the room."
                                                  if first else "Put the alarm across the room."),
                       "screen": "ALARM ACROSS THE ROOM", "visual": "Alarm / walk / phone face down"},
                      {"label": "CTA", "say": "Comment PLAN and I'll send you my morning checklist.",
                       "screen": 'Comment "PLAN"', "visual": "Face full frame"}],
            "keyword": "plan", "gets": "my morning checklist",
            "caption": "Comment PLAN and I'll send you my morning checklist.\nHow 5am paid my debt.",
            "hashtags": ["#discipline", "debtfree", "#morningroutine"], "cover": "5AM DEBT FREE",
            "title": "How waking up at 5am paid off my debt", "linkedin": ""}


def test_script_studio_writes_checks_and_rewrites_tells(cfg, conn, tmp_path):
    cfg.data.setdefault("studio", {}).update({"voice_file": str(tmp_path / "voice.md"),
                                              "audience_file": str(tmp_path / "audience.md")})
    studio.save_profile(cfg, voice="- **Pace:** 180 wpm\n", audience="Niche: discipline")
    llm = ScriptLLM()
    sid = studio.start_script(cfg, conn, "5am wake-ups paid off my debt", llm=llm, background=False)
    row = conn.execute("SELECT * FROM scripts WHERE id=?", (sid,)).fetchone()
    assert row["status"] == "ready", row["error"]
    s = json.loads(row["script_json"])
    assert len(llm.calls) == 2 and s["rewrites"] == 1                  # the "not about X, it's about Y" line forced one rewrite
    assert "<voice>" in llm.calls[0][0] and "Pace:** 180 wpm" in llm.calls[0][0]
    assert s["wpm"] == 180
    chosen = s["hooks"][s["chosen_hook"]]
    assert chosen["payoff"] == "LEADS" and "$12,000" in chosen["spoken"]  # real number beats the placeholder
    assert s["beats"][0]["label"] == "HOOK" and s["beats"][-1]["label"] == "CTA"
    assert s["beats"][1]["start"] == s["beats"][0]["end"]                # timed back to back
    assert s["keyword"] == "PLAN" and s["hashtags"][1] == "#debtfree"
    assert s["placeholders"] == []                                       # the blank was only in a hook that wasn't picked
    assert "### 1. HOOK" in s["markdown"] and "COVER: 5AM DEBT FREE" in s["markdown"]
    assert "—" not in json.dumps(s)


def test_voice_is_built_from_own_clip_transcripts(cfg, conn, tmp_path):
    cfg.data.setdefault("studio", {}).update({"voice_file": str(tmp_path / "voice.md")})
    sid = db.insert_stream(conn, tmp_path / "s.mp4", "hashvoice")
    words = [{"w": f"word{i}", "s": i * 0.35, "e": i * 0.35 + 0.3} for i in range(200)]
    (cfg.subdir("transcripts") / "hashvoice.json").write_text(json.dumps({"words": words}), encoding="utf-8")
    db.insert_clip(conn, sid, {"start": 0, "end": 30, "score": 90}, "posted")
    llm = ScriptLLM()
    text = studio.build_voice(cfg, conn, llm)
    assert "172 wpm" in text and (tmp_path / "voice.md").read_text(encoding="utf-8") == text
    assert "word1 word2" in llm.calls[0][1]
    assert 160 < studio.measured_wpm(cfg, conn) < 180                    # 1 word / 0.35 s = ~171 wpm


def test_studio_needs_an_idea(cfg, conn):
    with pytest.raises(ValueError):
        studio.start_script(cfg, conn, "  ", llm=ScriptLLM(), background=False)


class CopyLLM:
    def __call__(self, system, user, schema, max_tokens=16000):
        assert schema is REFINE_SCHEMA
        copy = {"title": "t", "caption": "c"}
        return {"start_idx": 0, "end_idx": 0, "score": 77, "hook": "I saved four hundred dollars",
                "hook_overlay": "I SAVED $400 — HERE'S HOW", "hook_overlay_alt": "ONE FREE TOOL", "reason": "payoff first",
                "title": "How I saved $400 on flights", "caption": "Let's dive in. One tool.", "hashtags": ["#travel"],
                "tiktok": copy, "instagram": copy, "youtube": copy, "facebook": copy, "x": copy}


def talking_take(path, seconds=8):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=360x640:r=15:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=200:d={seconds},volume=0.3,volume=enable='between(t,3,4.6)':volume=0",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


class WordsTranscriber:
    def words(self, wav):
        return [{"w": w, "s": round(0.2 + i * 0.3, 2), "e": round(0.45 + i * 0.3, 2), "p": 1.0}
                for i, w in enumerate("I saved four hundred dollars on flights".split())] + \
               [{"w": w, "s": round(4.8 + i * 0.3, 2), "e": round(5.05 + i * 0.3, 2), "p": 1.0}
                for i, w in enumerate("with one free tool".split())]


def test_edit_a_video_becomes_a_review_clip(cfg, conn, tmp_path):
    take = talking_take(tmp_path / "phone take.mp4")
    edit_id = editor.create(cfg, conn, [take], {"pace": "tight", "stopmotion": {"look": "poster", "fps": 10}})
    assert not take.exists()                                              # moved into the data folder
    clip_id = editor.process(cfg, conn, edit_id, llm=CopyLLM(), transcriber=WordsTranscriber())
    clip = db.clip_dict(db.get_clip(conn, clip_id))
    assert clip["status"] == "review" and clip["score"] == 77            # uploads never auto-post
    assert clip["recipe"]["upload"] and clip["recipe"]["stopmotion"] == {"look": "poster", "fps": 10, "seed": 1}
    assert "—" not in clip["recipe"]["hook_text"]                   # Claude's copy was cleaned
    assert clip["caption"] == "One tool."
    assert clip["checks"]["hook"]["payoff"] == "LEADS"
    assert clip["checks"]["edit"]["removed"] > 0.8                       # the 1.6 s pause was cut
    stream = conn.execute("SELECT kind FROM streams WHERE id=?", (clip["stream_id"],)).fetchone()
    assert stream["kind"] == "upload"
    row = conn.execute("SELECT status, clip_id FROM edits WHERE id=?", (edit_id,)).fetchone()
    assert (row["status"], row["clip_id"]) == ("ready", clip_id)


def test_photos_become_stop_motion(cfg, conn, tmp_path):
    photos = []
    for i, colour in enumerate(("red", "green", "blue", "white")):
        p = tmp_path / f"IMG_{i + 1}.png"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x240:d=1",
                        "-frames:v", "1", str(p)], check=True)
        photos.append(p)
    edit_id = editor.create(cfg, conn, photos, {"look": "clay", "fps": 8, "hold": 2, "loop": 2, "title": "Clay test"})
    clip_id = editor.process(cfg, conn, edit_id)
    clip = db.clip_dict(db.get_clip(conn, clip_id))
    assert clip["status"] == "review" and clip["recipe"]["photos"] and clip["title"] == "Clay test"
    assert abs(clip["end"] - 1.0) < 0.1                                  # 4 photos x 2 frames x 2 loops at 16 fps


def test_edit_rejects_mixed_uploads(cfg, conn, tmp_path):
    a, b = tmp_path / "a.mp4", tmp_path / "b.jpg"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    with pytest.raises(ValueError, match="not both"):
        editor.create(cfg, conn, [a, b], {})


def test_failed_edit_can_be_retried_and_restart_requeues(cfg, conn, tmp_path, monkeypatch):
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    edit_id = editor.create(cfg, conn, [bad], {})
    assert editor.drain(cfg) == 1
    row = conn.execute("SELECT status, error FROM edits WHERE id=?", (edit_id,)).fetchone()
    assert row["status"] == "failed" and row["error"]
    editor.retry(conn, edit_id)
    with pytest.raises(ValueError):
        editor.retry(conn, edit_id)                                       # only failed edits
    kicked = []
    monkeypatch.setattr(editor, "kick", lambda c: kicked.append(c))
    db.update_edit(conn, edit_id, status="working")                       # the app stopped mid-render
    editor.resume(cfg, conn)
    assert conn.execute("SELECT status FROM edits WHERE id=?", (edit_id,)).fetchone()["status"] == "queued"
    assert kicked


def test_dashboard_endpoints_for_new_tabs(cfg, conn, tmp_path, monkeypatch):
    cfg.root = tmp_path
    cfg.data.setdefault("studio", {}).update({"voice_file": str(tmp_path / "voice.md"),
                                              "audience_file": str(tmp_path / "audience.md")})
    client = TestClient(create_app(cfg))
    r = client.post("/api/research/add", data={"text": PASTE})
    assert r.status_code == 200 and r.json()["added"] == 10
    d = client.get("/api/research?min=2&days=0").json()
    assert d["outliers"][0]["creator"] == "smallcoach" and "ytdlp" in d
    assert client.post("/api/research/add", data={"text": " "}).status_code == 400
    assert client.post("/api/studio/profile", json={"voice": "v", "audience": "a"}).json()["ok"]
    s = client.get("/api/studio").json()
    assert s["profile"] == {"voice": "v", "audience": "a"} and s["claude"] is False
    assert client.post("/api/studio/scripts", json={"idea": "x"}).status_code == 400   # no API key: friendly error
    sid = studio.start_script(cfg, conn, "idea", llm=ScriptLLM(), background=False)
    md = client.get(f"/api/studio/scripts/{sid}.md")
    assert md.status_code == 200 and md.text.startswith("# Script:")
    e = client.get("/api/edits").json()
    assert e["edits"] == [] and "transcription" in e
    bad = client.post("/api/edits", files=[("files", ("notes.txt", b"hello", "text/plain"))], data={"options": "{}"})
    assert bad.status_code == 400
    assert "Edit a video" in client.get("/").text


class BreakdownLLM:
    def __init__(self):
        self.prompts = []

    def __call__(self, system, user, schema, max_tokens=16000):
        self.prompts.append(user)
        ids = [int(x) for x in __import__("re").findall(r'<winner id="(\d+)"', user)]
        return {"winners": [{"id": i, "format": "talking head", "structure": "hook, proof, CTA", "cta": "comment PLAN",
                             "pace": "35s, fast"} for i in ids],
                "shared": "Every winner opens on a dollar figure.", "how_to_use_it": "Lead with your debt number."}


def test_winner_breakdown_feeds_the_script(cfg, conn):
    research.add_posts(conn, PASTE.replace("https://www.tiktok.com/@smallcoach/video/1", "https://youtu.be/abc123"))
    subs = {"events": [{"segs": [{"utf8": "I paid off twelve grand"}, {"utf8": " waking up at five"}]}]}

    def runner(cmd):
        out = cmd[cmd.index("-o") + 1].replace("%(ext)s", "en.json3")
        __import__("pathlib").Path(out).write_text(json.dumps(subs), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    llm = BreakdownLLM()
    bd = research.breakdown(cfg, conn, llm, runner=runner)
    assert bd["shared"].startswith("Every winner") and bd["winners"][0]["creator"] == "smallcoach"
    assert "transcript: I paid off twelve grand waking up at five" in llm.prompts[0]
    assert "Why you're still broke" in llm.prompts[0]           # ordinary posts given for contrast
    assert research.last_breakdown(cfg)["shared"] == bd["shared"]
    cfg.root = cfg.data_dir
    client = TestClient(create_app(cfg))
    client.app.dependency_overrides = {}
    sid = bd["winners"][0]["id"]
    import kir_clipper.llm as llm_mod
    script_llm = ScriptLLM()
    original = llm_mod.ClaudeJSON
    llm_mod.ClaudeJSON = lambda c: script_llm
    try:
        r = client.post("/api/studio/scripts", json={"research_id": sid, "idea": "my debt story"})
        assert r.status_code == 200
        for _ in range(100):
            row = conn.execute("SELECT status FROM scripts WHERE id=?", (r.json()["id"],)).fetchone()
            if row["status"] != "writing":
                break
            __import__("time").sleep(0.05)
    finally:
        llm_mod.ClaudeJSON = original
    assert "What the winners in this niche share: Every winner opens on a dollar figure." in script_llm.calls[0][1]
    assert "structure hook, proof, CTA" in script_llm.calls[0][1]


def test_breakdown_needs_outliers(cfg, conn):
    with pytest.raises(ValueError):
        research.breakdown(cfg, conn, BreakdownLLM())
