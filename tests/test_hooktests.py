import json
import shutil
from datetime import timedelta
from pathlib import Path

from kir_clipper import db
from kir_clipper.hooktests import evaluate, lessons, winning_hook
from kir_clipper.performance import record
from kir_clipper.pipeline import process_file
from kir_clipper.reedit import run_reedit

from .conftest import FakeLLM, FakeTranscriber, make_video


def auto_clip(cfg, conn, sample_video, llm=None):
    shutil.copy(sample_video, cfg.path("inbox") / "s.mp4")
    process_file(cfg, conn, cfg.path("inbox") / "s.mp4", llm=llm or FakeLLM(scores=(95, 70)),
                 transcriber=FakeTranscriber())
    return conn.execute("SELECT id FROM clips WHERE status='approved' ORDER BY id LIMIT 1").fetchone()[0]


def go_live(conn, post_id, days_ago, views):
    at = db.utcnow() - timedelta(days=days_ago)
    db.update_post(conn, post_id, status="posted", posted_at=db.iso(at))
    if views is not None:
        record(conn, post_id, source="upload_post", at=at + timedelta(days=3), views=views)


def test_trial_and_mirrored_trial_carry_different_hooks(cfg, conn, sample_video):
    cid = auto_clip(cfg, conn, sample_video)
    t = conn.execute("SELECT * FROM hook_tests WHERE clip_id=?", (cid,)).fetchone()
    assert t and t["hook_a"] != t["hook_b"] and t["hook_b"].startswith("alt overlay")
    post_a = conn.execute("SELECT * FROM posts WHERE id=?", (t["post_a"],)).fetchone()
    post_b = conn.execute("SELECT * FROM posts WHERE id=?", (t["post_b"],)).fetchone()
    assert (post_a["step"], post_b["step"]) == ("trial", "mirrored_trial")
    recipe_b = json.loads(Path(post_b["file_path"]).with_suffix(".recipe.json").read_text())
    assert recipe_b["hook_text"] == t["hook_b"] and recipe_b["mirror"] is True


def test_winner_is_decided_and_used(cfg, conn, sample_video):
    cid = auto_clip(cfg, conn, sample_video)
    t = conn.execute("SELECT * FROM hook_tests WHERE clip_id=?", (cid,)).fetchone()
    go_live(conn, t["post_a"], 5, 1000)
    go_live(conn, t["post_b"], 1, 2600)
    assert evaluate(conn, cfg) == 0                       # B hasn't had 3 days yet
    conn.execute("UPDATE posts SET posted_at=? WHERE id=?", (db.iso(db.utcnow() - timedelta(days=4)), t["post_b"]))
    conn.execute("UPDATE performance SET fetched_at=? WHERE post_id=?", (db.iso(db.utcnow() - timedelta(days=1)), t["post_b"]))
    assert evaluate(conn, cfg) == 1
    w = winning_hook(conn, cid)
    assert w["winner"] == t["hook_b"] and w["views_winner"] == 2600
    assert f'WON "{t["hook_b"]}"' in lessons(conn, cfg)

    # New clips: Claude sees the lesson when writing hooks.
    llm = FakeLLM(scores=(95,))
    make_video(cfg.path("inbox") / "s2.mp4", seconds=60, color="smptehdbars")
    process_file(cfg, conn, cfg.path("inbox") / "s2.mp4", llm=llm, transcriber=FakeTranscriber())
    assert any("hook_results" in system and t["hook_b"] in system for system, _ in llm.prompts)

    # The +90 repost is told which hook won.
    conn.execute("UPDATE posts SET status='posted', posted_at=? WHERE clip_id=?",
                 (db.iso(db.utcnow() - timedelta(days=95)), cid))
    conn.execute("UPDATE clips SET status='posted' WHERE id=?", (cid,))
    conn.execute("UPDATE clips SET status='rejected' WHERE id<>? AND status IN ('approved','review','scheduled')", (cid,))
    conn.execute("UPDATE posts SET status='cancelled' WHERE clip_id<>?", (cid,))
    llm2 = FakeLLM(scores=(95,))
    cfg.data["reedit"]["max_reposts_per_week"] = 1
    run_reedit(cfg, conn, llm=llm2)
    assert any("A/B test" in user and t["hook_b"] in user for _, user in llm2.prompts)


def test_close_results_are_a_tie_and_missing_numbers_wait(cfg, conn, sample_video):
    cid = auto_clip(cfg, conn, sample_video)
    t = conn.execute("SELECT * FROM hook_tests WHERE clip_id=?", (cid,)).fetchone()
    go_live(conn, t["post_a"], 6, 1000)
    go_live(conn, t["post_b"], 5, None)
    assert evaluate(conn, cfg) == 0                       # B has no numbers yet
    record(conn, t["post_b"], source="manual", views=1080)
    assert evaluate(conn, cfg) == 1
    assert conn.execute("SELECT winner FROM hook_tests WHERE id=?", (t["id"],)).fetchone()[0] == "tie"
    assert winning_hook(conn, cid) is None
