import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from kir_clipper import db, editor, media, salesvideo
from kir_clipper.render import default_recipe, edit_report, locate_line, render_clip
from kir_clipper.web.app import create_app

INTAKE = {
    "offer": "12-week discipline coaching", "audience": "men 25-40 who keep restarting", "not_for": "quick-fix seekers",
    "placement": "landing", "promise": "You'll wake up at 5am without fighting yourself",
    "pain": "I set three alarms and still sleep through them", "proof": "Marcus: 41 days straight at 5am",
    "method_name": "The Shadow Routine", "method": "Night reset, morning trigger, weekly review",
    "objections": [{"think": "I'm not a morning person", "wrong": "Nobody is at first", "right": "It's a trained habit",
                    "proof": "Marcus hit 41 days"},
                   {"think": "Coaching is too expensive", "wrong": "", "right": "", "proof": ""},
                   {"think": ""}],
    "cta": "Book a free 15-minute call below", "next_step": "You get a plan on the call", "urgency": "", "flaw": "",
}


class SalesLLM:
    def __init__(self, dash_first=True):
        self.calls = []
        self.dash_first = dash_first

    def __call__(self, system, user, schema, max_tokens=16000):
        assert schema is salesvideo.SALES_SCHEMA
        self.calls.append((system, user))
        first = len(self.calls) == 1 and self.dash_first
        L = lambda block, beat, say, show="you, on camera", obj=0: {"block": block, "beat": beat, "objection": obj,
                                                                    "say": say, "show": show}
        return {"title": "Wake up at 5am", "big_idea": "Old way: willpower — new way: a routine that runs itself",
                "missing": ["a proof for objection 2"],
                "lines": [L("INTRO", "promise", "You'll wake up at 5am without fighting yourself."),
                          L("INTRO", "pain", "You set three alarms and still sleep through them."),
                          L("INTRO", "proof", "Marcus did 41 days straight.", "Marcus's streak screenshot"),
                          L("INTRO", "plan", "First how it works, then the questions everyone asks."),
                          L("INTRO", "map", "It takes about four minutes."),
                          L("METHOD", "method", "It's called The Shadow Routine. Night reset, morning trigger, weekly review."),
                          L("OBJECTION", "think", "You think you're not a morning person.", obj=1),
                          L("OBJECTION", "wrong", ("It's not about being a morning person — it's about training."
                                                   if first else "Nobody is at first."), obj=1),
                          L("OBJECTION", "right", "It's a trained habit.", obj=1),
                          L("OBJECTION", "objection_proof", "Marcus hit 41 days.", "Marcus's streak screenshot", obj=1),
                          L("OBJECTION", "think", "You think coaching is too expensive.", obj=2),
                          L("OBJECTION", "objection_proof", "{{a client who made it back}}", "{{proof}}", obj=2),
                          L("CTA", "action", "Book a free 15-minute call below."),
                          L("CTA", "next", "You'll leave the call with a plan.")]}


def test_intake_needs_the_basics():
    with pytest.raises(ValueError, match="promise"):
        salesvideo.clean_intake({**INTAKE, "promise": ""})
    with pytest.raises(ValueError, match="objection"):
        salesvideo.clean_intake({**INTAKE, "objections": [{"think": ""}]})
    clean = salesvideo.clean_intake({**INTAKE, "placement": "nonsense"})
    assert clean["placement"] == "landing" and len(clean["objections"]) == 2


def test_sales_script_uses_owner_words_checks_and_exports(cfg, conn):
    llm = SalesLLM()
    sid = salesvideo.start(cfg, conn, INTAKE, llm=llm, background=False)
    row = conn.execute("SELECT kind, status, error, script_json FROM scripts WHERE id=?", (sid,)).fetchone()
    assert row["kind"] == "sales" and row["status"] == "ready", row["error"]
    s = json.loads(row["script_json"])
    system, user = llm.calls[0]
    assert "I set three alarms and still sleep through them" in user          # their words go in
    assert "never invent" in system.lower()
    assert len(llm.calls) == 2 and s["rewrites"] == 1                         # the "not about X" line got rewritten
    assert "—" not in json.dumps(s["lines"]) and "—" not in s["big_idea"]
    assert s["lines"][1]["start"] > s["lines"][0]["end"]                       # a breath between lines
    assert set(s["blocks"]) == {"INTRO", "METHOD", "OBJECTION", "CTA"}
    assert any("Objection 2 has no real proof" in w for w in s["warnings"])
    assert not any("Objection 1" in w for w in s["warnings"])
    assert "{{a client who made it back}}" in s["placeholders"]
    assert s["proof_lines"] == [2, 9, 11]
    assert "## Intro" in s["markdown"] and "SHOW: Marcus's streak screenshot" in s["markdown"]
    assert s["teleprompter"].count("\n\n") >= len(s["lines"]) and "SHOW" not in s["teleprompter"]
    assert salesvideo.proof_slots(s)[1] == {"line": 9, "say": "Marcus hit 41 days.", "show": "Marcus's streak screenshot"}


def test_long_intro_and_two_ctas_are_flagged():
    raw = SalesLLM(dash_first=False)(None, None, salesvideo.SALES_SCHEMA)
    raw["lines"] = [{"block": "INTRO", "beat": "pain", "objection": 0, "say": " ".join(["word"] * 300), "show": "x"}] + \
                   raw["lines"] + [{"block": "CTA", "beat": "action", "objection": 0, "say": "Or buy now.", "show": "x"}]
    s = salesvideo.assemble(raw, salesvideo.clean_intake(INTAKE), 165)
    text = " ".join(s["warnings"])
    assert "intro runs" in text and "more than one call to action" in text and "Lines 1" in text


def test_locate_line_tolerates_how_people_actually_say_it():
    words = [{"w": w, "s": i * 0.4, "e": i * 0.4 + 0.3} for i, w in
             enumerate("okay so um my client Marcus hit forty one days in a row and then".split())]
    s, e = locate_line("Marcus hit 41 days in a row.", words)
    assert 1.9 <= s <= 2.1 and e > s
    assert locate_line("Something nobody said at all here", words) is None


def landscape_take(path, seconds=6):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s=640x360:r=15:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=200:d={seconds},volume=0.3", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def proof_png(path, colour="red"):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"color=c={colour}:s=320x240:d=1",
                    "-frames:v", "1", str(path)], check=True)
    return path


def test_16_9_render_with_a_proof_card(cfg, tmp_path):
    take = landscape_take(tmp_path / "vsl.mp4")
    words = [{"w": w, "s": 0.3 + i * 0.4, "e": 0.6 + i * 0.4} for i, w in enumerate("Marcus hit forty one days straight".split())]
    recipe = {**default_recipe(cfg, {}), "aspect": "16:9", "jumpcut": "off", "crop_mode": "center",
              "proof_cards": [{"text": "Marcus hit 41 days straight.", "image": str(proof_png(tmp_path / "p.png"))},
                              {"text": "Never said this line", "image": str(tmp_path / "p.png")}]}
    out = render_clip(cfg, take, {"start": 0.0, "end": 6.0}, words, recipe, tmp_path / "out.mp4")
    info = media.probe(out)
    assert info["width"] > info["height"]                                 # 16:9 even though config is 9:16
    rep = edit_report(out)
    assert [c["placed"] for c in rep["proof_cards"]] == [True, False]
    frame = tmp_path / "f.png"
    t = (rep["proof_cards"][0]["start"] + rep["proof_cards"][0]["end"]) / 2
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-ss", f"{t:.2f}", "-i", str(out), "-vf", "crop=8:8:(iw-8)/2:(ih-8)/2",
                    "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", str(frame)], check=True)
    r, g, b = frame.read_bytes()[:3]
    assert r > 100 and g < 80                                             # the red proof card is on screen there


class Words:
    def words(self, wav):
        return [{"w": w, "s": round(0.3 + i * 0.4, 2), "e": round(0.6 + i * 0.4, 2), "p": 1.0}
                for i, w in enumerate("Marcus hit forty one days straight".split())]


def test_sales_edit_places_proof_and_is_not_scheduled(cfg, conn, tmp_path):
    sid = salesvideo.start(cfg, conn, INTAKE, llm=SalesLLM(dash_first=False), background=False)
    take = landscape_take(tmp_path / "phone.mp4")
    proof = proof_png(tmp_path / "streak.png")
    with pytest.raises(ValueError, match="sales video"):
        editor.create(cfg, conn, [landscape_take(tmp_path / "other.mp4")], {}, proofs=[(9, proof_png(tmp_path / "x.png"))])
    edit_id = editor.create(cfg, conn, [take], {"aspect": "16:9", "sales": {"script_id": sid}, "pace": "off"},
                            proofs=[(9, proof)])
    clip_id = editor.process(cfg, conn, edit_id, transcriber=Words())
    clip = db.clip_dict(db.get_clip(conn, clip_id))
    assert clip["recipe"]["sales"] and clip["recipe"]["aspect"] == "16:9" and clip["title"] == "Wake up at 5am"
    assert clip["recipe"]["proof_cards"][0]["text"] == "Marcus hit 41 days."
    assert clip["checks"]["edit"] and edit_report(clip["file_path"])["proof_cards"][0]["placed"]
    assert clip["status"] == "review" and not conn.execute("SELECT 1 FROM posts WHERE clip_id=?", (clip_id,)).fetchone()


def test_sales_endpoints(cfg, conn, tmp_path):
    client = TestClient(create_app(cfg))
    assert client.post("/api/studio/sales", json=INTAKE).status_code == 400          # no Claude key: friendly error
    sid = salesvideo.start(cfg, conn, INTAKE, llm=SalesLLM(dash_first=False), background=False)
    slots = client.get(f"/api/studio/sales/{sid}/proof").json()["slots"]
    assert [s["line"] for s in slots] == [2, 9, 11]
    tp = client.get(f"/api/studio/scripts/{sid}.txt")
    assert tp.status_code == 200 and "Book a free 15-minute call below." in tp.text
    scripts = client.get("/api/studio").json()["scripts"]
    assert scripts[0]["kind"] == "sales"
    take = landscape_take(tmp_path / "t.mp4")
    r = client.post("/api/edits", files=[("files", ("t.mp4", take.read_bytes(), "video/mp4")),
                                          ("proofs", ("p.png", proof_png(tmp_path / "p.png").read_bytes(), "image/png"))],
                    data={"options": json.dumps({"aspect": "16:9", "sales": {"script_id": sid}}), "proof_lines": "[9]"})
    assert r.status_code == 200
    opts = json.loads(conn.execute("SELECT options FROM edits WHERE id=?", (r.json()["id"],)).fetchone()["options"])
    assert opts["sales"]["proof"] == {"9": "proof_009.png"} and opts["aspect"] == "16:9"


def test_old_scripts_table_gains_kind(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("""CREATE TABLE scripts (id INTEGER PRIMARY KEY, idea TEXT NOT NULL, based_on TEXT, status TEXT NOT NULL,
                   script_json TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    raw.execute("INSERT INTO scripts(idea, status, created_at, updated_at) VALUES ('x', 'ready', 'a', 'b')")
    raw.commit()
    raw.close()
    conn = db.connect(path)
    assert conn.execute("SELECT kind FROM scripts").fetchone()["kind"] == "short"
