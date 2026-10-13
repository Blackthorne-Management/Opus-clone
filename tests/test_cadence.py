import shutil
import subprocess
from datetime import timedelta
from zoneinfo import ZoneInfo

import numpy as np

from kir_clipper import db
from kir_clipper.cadence import timeline
from kir_clipper.pipeline import process_file
from kir_clipper.reedit import find_candidates
from kir_clipper.scheduler import mark_posted, publish_tick

from .conftest import FakeLLM, FakeTranscriber
from .test_scheduler import FakePublisher

NY = ZoneInfo("America/New_York")


def auto_clip(cfg, conn, sample_video):
    """Process the sample stream so one clip scores 95 and is auto-approved; return its id."""
    shutil.copy(sample_video, cfg.path("inbox") / "s.mp4")
    process_file(cfg, conn, cfg.path("inbox") / "s.mp4", llm=FakeLLM(scores=(95, 70)), transcriber=FakeTranscriber())
    return conn.execute("SELECT id FROM clips WHERE status='approved' ORDER BY id LIMIT 1").fetchone()[0]


def frame(path, t=3.0, w=96, h=170):
    raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.uint8).reshape(h, w).astype(float)


def posts_by_step(conn, clip_id):
    out = {}
    for r in conn.execute("SELECT * FROM posts WHERE clip_id=? ORDER BY publish_at", (clip_id,)):
        out.setdefault(r["step"], []).append(r)
    return out


def test_five_step_plan_day_one_and_two(cfg, conn, sample_video):
    cid = auto_clip(cfg, conn, sample_video)
    steps = posts_by_step(conn, cid)
    assert set(steps) == {"trial", "feed", "mirrored_trial"}
    trial, mirrored = steps["trial"][0], steps["mirrored_trial"][0]
    assert trial["platform"] == "instagram" and trial["trial"] == 1
    assert mirrored["platform"] == "instagram" and mirrored["trial"] == 1
    assert {r["platform"] for r in steps["feed"]} == {"instagram", "tiktok", "youtube"}
    assert all(r["trial"] == 0 for r in steps["feed"])

    t_trial = db.parse_iso(trial["publish_at"])
    ig_feed = next(r for r in steps["feed"] if r["platform"] == "instagram")
    assert db.parse_iso(ig_feed["publish_at"]) > t_trial                       # step 2 after step 1
    t_mirror = db.parse_iso(mirrored["publish_at"])
    assert t_mirror.astimezone(NY).date() >= t_trial.astimezone(NY).date() + timedelta(days=1)   # step 3: next day

    # Mirrored file: a real, separate render whose picture is the flipped original (top area, away from captions).
    clip = db.get_clip(conn, cid)
    assert mirrored["file_path"] and mirrored["file_path"] != clip["file_path"]
    a, b = frame(clip["file_path"])[:60], frame(mirrored["file_path"])[:60]
    assert np.abs(a[:, ::-1] - b).mean() < np.abs(a - b).mean() * 0.5

    # Dashboard timeline: 3 real steps + 2 projected reposts at +90 and +180 days, numbered 1-5.
    tl = timeline(cfg, conn, cid)
    assert [s["number"] for s in tl] == [1, 2, 3, 4, 5]
    assert [s["label"] for s in tl] == ["Trial Reel", "Feed Reel", "Mirrored Trial Reel",
                                        "Feed Reel +90 days", "Feed Reel +180 days"]
    assert [s["status"] for s in tl][-2:] == ["projected", "projected"]
    first = db.parse_iso(tl[0]["at"])
    assert db.parse_iso(tl[3]["at"]) - first == timedelta(days=90)
    assert db.parse_iso(tl[4]["at"]) - first == timedelta(days=180)


def test_manual_trial_reminds_then_mark_posted(cfg, conn, sample_video):
    cfg.data["cadence"]["trial_mode"] = "manual"
    cid = auto_clip(cfg, conn, sample_video)
    trial = posts_by_step(conn, cid)["trial"][0]
    pub = FakePublisher()
    at = db.parse_iso(trial["publish_at"])
    publish_tick(conn, cfg, pub, now=at - timedelta(minutes=10))
    assert conn.execute("SELECT status FROM posts WHERE id=?", (trial["id"],)).fetchone()[0] == "planned"
    stats = publish_tick(conn, cfg, pub, now=at + timedelta(minutes=1))
    assert stats["manual"] == 1
    assert conn.execute("SELECT status FROM posts WHERE id=?", (trial["id"],)).fetchone()[0] == "manual"
    assert all(s[0] != "instagram" or s[1] != at for s in pub.scheduled)        # never sent to the API
    assert "Trial Reel" in (cfg.data_dir / "notifications.log").read_text()
    mark_posted(conn, trial["id"])
    tl = timeline(cfg, conn, cid)
    assert tl[0]["label"] == "Trial Reel" and tl[0]["status"] == "posted"


def test_api_trial_mode_sends_configured_fields(cfg, conn, sample_video):
    cfg.data["cadence"]["trial_mode"] = "api"
    cfg.data["cadence"]["trial_api_fields"] = {"instagram_trial_reel": "true"}
    cid = auto_clip(cfg, conn, sample_video)
    trial = posts_by_step(conn, cid)["trial"][0]
    seen = []

    class Pub(FakePublisher):
        def schedule(self, clip_path, caption, platforms, publish_at, title=None, extra=None):
            seen.append(extra)
            return super().schedule(clip_path, caption, platforms, publish_at, title)

    publish_tick(conn, cfg, Pub(), now=db.parse_iso(trial["publish_at"]) - timedelta(hours=1))
    assert {"instagram_trial_reel": "true"} in seen


def test_skip_mode_has_no_trials(cfg, conn, sample_video):
    cfg.data["cadence"]["trial_mode"] = "skip"
    cid = auto_clip(cfg, conn, sample_video)
    assert set(posts_by_step(conn, cid)) == {"feed"}


def test_disabled_cadence_posts_once_per_platform(cfg, conn, sample_video):
    cfg.data["cadence"]["enabled"] = False
    cid = auto_clip(cfg, conn, sample_video)
    rows = conn.execute("SELECT platform, trial FROM posts WHERE clip_id=?", (cid,)).fetchall()
    assert sorted(r["platform"] for r in rows) == ["instagram", "tiktok", "youtube"]


def test_reposts_stop_after_two(cfg, conn, tmp_path):
    sid = db.insert_stream(conn, tmp_path / "s.mp4", "h-max")
    old = db.iso(db.utcnow() - timedelta(days=200))
    root = db.insert_clip(conn, sid, {"start": 0, "end": 30, "score": 95}, "posted")
    for v in (2, 3):
        db.insert_clip(conn, sid, {"start": 0, "end": 30, "score": 95}, "posted", parent_clip_id=root, version=v)
    for cid in (1, 2, 3):
        pid = db.insert_post(conn, cid, "instagram", db.utcnow() - timedelta(days=200))
        db.update_post(conn, pid, status="posted", posted_at=old)
    assert find_candidates(conn, cfg) == []
    cfg.data["reedit"]["max_reposts"] = 3
    assert [c["root_id"] for c in find_candidates(conn, cfg)] == [root]


def test_automatic_trial_falls_back_to_manual_if_rejected(cfg, conn, sample_video):
    assert cfg.get("cadence.trial_mode") == "api"
    assert cfg.get("cadence.trial_api_fields") == {"share_mode": "TRIAL_REELS_SHARE_TO_FOLLOWERS_IF_LIKED"}
    cid = auto_clip(cfg, conn, sample_video)
    trial = posts_by_step(conn, cid)["trial"][0]

    class Rejects(FakePublisher):
        def schedule(self, clip_path, caption, platforms, publish_at, title=None, extra=None):
            if extra:
                from kir_clipper.publishers import PublishError
                raise PublishError("Upload-Post rejected the request: trial reels not available", retryable=False)
            return super().schedule(clip_path, caption, platforms, publish_at, title)

    publish_tick(conn, cfg, Rejects(), now=db.parse_iso(trial["publish_at"]) - timedelta(hours=1))
    row = conn.execute("SELECT status, last_error FROM posts WHERE id=?", (trial["id"],)).fetchone()
    assert row["status"] == "manual" and "trial reels not available" in row["last_error"]
    assert "needs posting by hand" in (cfg.data_dir / "notifications.log").read_text()
