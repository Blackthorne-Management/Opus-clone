"""Acceptance tests from the build guide, run against a synthetic recording with fake Whisper/Claude."""

import hashlib
import json
import logging
import shutil
from datetime import timedelta

from kir_clipper import db, media
from kir_clipper.config import load_config
from kir_clipper.logging_setup import setup_logging
from kir_clipper.pipeline import process_file
from kir_clipper.reedit import VISUAL_FIELDS, run_reedit, vary_recipe
from kir_clipper.watcher import InboxWatcher

from .conftest import ROOT, FakeLLM, FakeTranscriber


def run_watcher_once(cfg, conn, llm, transcriber):
    handled = []
    w = InboxWatcher(cfg, lambda p: handled.append(process_file(cfg, conn, p, llm=llm, transcriber=transcriber)))
    for _ in range(3):
        for p in w.scan():
            w.on_ready(p)
            w.tracker.mark_done(p)
    return handled


def test_drop_file_get_captioned_vertical_clips_in_queue(cfg, conn, sample_video):
    shutil.copy(sample_video, cfg.path("inbox") / "kir_stream.mp4")
    transcriber = FakeTranscriber()
    run_watcher_once(cfg, conn, FakeLLM(scores=(95, 80, 70, 50)), transcriber)

    stream = conn.execute("SELECT * FROM streams").fetchone()
    assert stream["status"] == "done", stream["error"]
    assert transcriber.calls == 3                                   # 150s in 60s chunks
    assert not any(cfg.path("inbox").iterdir())                     # moved out of the inbox
    assert (cfg.path("processed") / "kir_stream.mp4").exists()      # original kept

    clips = [dict(r) for r in conn.execute("SELECT * FROM clips")]
    statuses = {c["status"] for c in clips}
    assert "review" in statuses and "approved" in statuses
    for c in clips:
        if c["status"] in ("review", "approved"):
            info = media.probe(c["file_path"])
            assert (info["width"], info["height"]) == (270, 480)    # 9:16 (scaled down for test speed)
            assert info["has_audio"]
            recipe = json.loads(c["recipe_json"])
            assert recipe["caption_style"] and recipe["crop_mode"]
        if c["score"] < 60:
            assert c["status"] == "dropped" and c["file_path"] is None
    # Auto-posted clips (score >= 90) were planned on every platform.
    approved = [c for c in clips if c["status"] == "approved"]
    for c in approved:
        plats = {r["platform"] for r in conn.execute("SELECT platform FROM posts WHERE clip_id=?", (c["id"],))}
        assert plats == set(cfg.get("publishing.platforms"))
    assert "Score distribution" in (cfg.data_dir / "notifications.log").read_text()


def test_same_file_twice_is_processed_once(cfg, conn, sample_video):
    llm = FakeLLM()
    shutil.copy(sample_video, cfg.path("inbox") / "a.mp4")
    run_watcher_once(cfg, conn, llm, FakeTranscriber())
    n_calls = len(llm.calls)
    shutil.copy(sample_video, cfg.path("inbox") / "a_copy.mp4")
    run_watcher_once(cfg, conn, llm, FakeTranscriber())
    assert conn.execute("SELECT COUNT(*) FROM streams").fetchone()[0] == 1
    assert len(llm.calls) == n_calls
    assert (cfg.path("processed") / "duplicates" / "a_copy.mp4").exists()


def test_transcript_cache_makes_reruns_free(cfg, conn, sample_video):
    t = FakeTranscriber()
    shutil.copy(sample_video, cfg.path("inbox") / "a.mp4")
    run_watcher_once(cfg, conn, FakeLLM(), t)
    conn.execute("UPDATE streams SET status='failed'")
    from kir_clipper.pipeline import reprocess
    reprocess(cfg, conn, 1, llm=FakeLLM(), transcriber=t)
    assert t.calls == 3


def test_dry_run_scores_everything_posts_nothing(cfg, conn, sample_video):
    cfg.data["review"]["dry_run"] = True
    shutil.copy(sample_video, cfg.path("inbox") / "a.mp4")
    run_watcher_once(cfg, conn, FakeLLM(scores=(95,)), FakeTranscriber())
    assert conn.execute("SELECT COUNT(*) FROM clips WHERE status='dry_run'").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0


def test_failed_stream_moves_to_failed_and_alerts(cfg, conn, tmp_path):
    bad = cfg.path("inbox") / "broken.mp4"
    bad.write_bytes(b"not a video at all")
    run_watcher_once(cfg, conn, FakeLLM(), FakeTranscriber())
    row = conn.execute("SELECT * FROM streams").fetchone()
    assert row["status"] == "failed"
    assert (cfg.path("failed") / "broken.mp4").exists()
    assert "Processing failed" in (cfg.data_dir / "notifications.log").read_text()


def test_reedit_after_interval_renders_visibly_different_version(cfg, conn, sample_video):
    shutil.copy(sample_video, cfg.path("inbox") / "a.mp4")
    run_watcher_once(cfg, conn, FakeLLM(scores=(95,)), FakeTranscriber())
    clip = conn.execute("SELECT * FROM clips WHERE status='approved' ORDER BY id LIMIT 1").fetchone()
    posted_at = db.utcnow() - timedelta(days=2)
    conn.execute("UPDATE posts SET status='posted', posted_at=? WHERE clip_id=?", (db.iso(posted_at), clip["id"]))
    conn.execute("UPDATE clips SET status='posted' WHERE id=?", (clip["id"],))
    conn.execute("UPDATE posts SET status='cancelled' WHERE clip_id<>?", (clip["id"],))
    conn.execute("UPDATE clips SET status='rejected' WHERE id<>? AND status<>'dropped'", (clip["id"],))

    cfg.data["reedit"]["interval_days"] = 1
    created = run_reedit(cfg, conn, llm=FakeLLM(scores=(95,)))
    assert len(created) == 1
    new = conn.execute("SELECT * FROM clips WHERE id=?", (created[0],)).fetchone()
    assert new["parent_clip_id"] == clip["id"] and new["version"] == 2
    old_r, new_r = json.loads(clip["recipe_json"]), json.loads(new["recipe_json"])
    assert sum(old_r[f] != new_r[f] for f in VISUAL_FIELDS) >= 3
    assert old_r["hook_text"] != new_r["hook_text"]
    digest = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
    assert digest(clip["file_path"]) != digest(new["file_path"])

    # Not eligible again until the new version has run and the interval passes.
    assert run_reedit(cfg, conn, llm=FakeLLM()) == []


def test_reedit_not_before_interval(cfg, conn, sample_video):
    shutil.copy(sample_video, cfg.path("inbox") / "a.mp4")
    run_watcher_once(cfg, conn, FakeLLM(scores=(95,)), FakeTranscriber())
    conn.execute("UPDATE posts SET status='posted', posted_at=?", (db.iso(db.utcnow() - timedelta(days=30)),))
    conn.execute("UPDATE clips SET status='posted' WHERE status='approved'")
    assert run_reedit(cfg, conn, llm=FakeLLM()) == []   # default 90 days


def test_vary_recipe_never_repeats():
    prev = []
    base = {"hook_text": "x"}
    for _ in range(6):
        r = vary_recipe(prev, base)
        assert all(sum(r[f] != p[f] for f in VISUAL_FIELDS) >= 2 for p in prev)
        prev.append(r)


def test_no_api_key_in_logs_or_repo(tmp_path, monkeypatch):
    key = "sk-ant-api03-SECRETSECRETSECRET1234567890"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "up_live_abcdef1234567890")
    cfg = load_config(ROOT / "config.yaml", {"paths": {k: str(tmp_path / k) for k in ("inbox", "processed", "failed", "data")}})
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root._kir_configured = False
    setup_logging(cfg)
    log = logging.getLogger("kir.test")
    log.error("request failed with key %s", key)
    log.error("header Authorization: Apikey up_live_abcdef1234567890")
    log.error(f"inline {key}")
    for h in root.handlers:
        h.flush()
    text = (cfg.data_dir / "logs" / "kir_clipper.log").read_text()
    assert "SECRETSECRET" not in text and "up_live_abcdef" not in text
    assert text.count("***") >= 3
    for h in list(root.handlers):
        root.removeHandler(h)
    root._kir_configured = False

    tracked = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and "data" not in p.parts
               and p.suffix in (".py", ".yaml", ".md", ".toml", ".example", ".ps1", ".html", ".txt")]
    for p in tracked:
        body = p.read_text(encoding="utf-8", errors="ignore")
        assert "sk-ant-api" not in body or p.name == "test_end_to_end.py", p
    assert not (ROOT / ".env").exists() or ".env" in (ROOT / ".gitignore").read_text()
