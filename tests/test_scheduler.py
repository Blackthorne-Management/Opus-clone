from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kir_clipper import db
from kir_clipper.publishers import PostStatus, PublishError
from kir_clipper.scheduler import copy_for, next_slot, plan_posts, publish_tick

NOW = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)   # 10:00 in New York


class FakePublisher:
    name = "fake"

    def __init__(self, fail_times=0, retryable=True, status_state="posted"):
        self.fail_times, self.retryable, self.status_state = fail_times, retryable, status_state
        self.scheduled = []

    def schedule(self, clip_path, caption, platforms, publish_at, title=None):
        if self.fail_times:
            self.fail_times -= 1
            raise PublishError("boom", retryable=self.retryable)
        self.scheduled.append((platforms[0], publish_at, caption, title))
        return f"job{len(self.scheduled)}"

    def status(self, post_id):
        return PostStatus(state=self.status_state, platform_post_id=f"p-{post_id}")


def make_clip(conn, tmp_path, score=92, status="approved"):
    stream = db.insert_stream(conn, tmp_path / "s.mp4", f"h{score}{status}{tmp_path.name}{id(conn)}{datetime.now().timestamp()}")
    f = tmp_path / f"clip{score}.mp4"
    f.write_bytes(b"x")
    cid = db.insert_clip(conn, stream, {"start": 0, "end": 30, "score": score, "hook": "h", "title": "T",
                                        "caption": "Cap", "hashtags": ["#kir"],
                                        "platform_captions": {"youtube": {"title": "YT", "caption": "yt cap"}}}, status)
    db.update_clip(conn, cid, file_path=str(f))
    return cid


def test_slots_respect_daily_cap_and_gap(cfg, conn, tmp_path):
    cfg.data["publishing"]["max_posts_per_day"]["instagram"] = 2
    tz = ZoneInfo("America/New_York")
    times = []
    for _ in range(5):
        cid = make_clip(conn, tmp_path)
        plan_posts(conn, cfg, cid, now=NOW, platforms=["instagram"])
        times.append(db.parse_iso(conn.execute("SELECT publish_at FROM posts ORDER BY id DESC").fetchone()[0]))
    per_day = {}
    for t in times:
        per_day.setdefault(t.astimezone(tz).date(), []).append(t)
    assert all(len(v) <= 2 for v in per_day.values())
    times.sort()
    assert all(b - a >= timedelta(minutes=180) for a, b in zip(times, times[1:]))
    assert times[0] > NOW


def test_publish_submits_then_marks_posted(cfg, conn, tmp_path):
    cid = make_clip(conn, tmp_path)
    plan_posts(conn, cfg, cid, now=NOW, platforms=["tiktok", "youtube"])
    pub = FakePublisher()
    stats = publish_tick(conn, cfg, pub, now=NOW)
    assert stats["submitted"] == 2
    assert {p for p, *_ in pub.scheduled} == {"tiktok", "youtube"}
    yt = next(s for s in pub.scheduled if s[0] == "youtube")
    assert yt[3] == "YT" and "#shorts" in yt[2]
    assert db.get_clip(conn, cid)["status"] == "scheduled"
    later = NOW + timedelta(days=2)
    stats = publish_tick(conn, cfg, pub, now=later)
    assert stats["posted"] == 2
    assert db.get_clip(conn, cid)["status"] == "posted"


def test_failed_post_retries_with_backoff_then_alerts(cfg, conn, tmp_path):
    cid = make_clip(conn, tmp_path)
    plan_posts(conn, cfg, cid, now=NOW, platforms=["tiktok"])
    pub = FakePublisher(fail_times=99)
    now = NOW
    for attempt in range(1, 5):
        publish_tick(conn, cfg, pub, now=now)
        post = conn.execute("SELECT * FROM posts WHERE clip_id=?", (cid,)).fetchone()
        assert post["attempts"] == attempt
        if attempt < 4:
            assert post["status"] == "planned" and post["next_attempt_at"]
            publish_tick(conn, cfg, pub, now=now + timedelta(seconds=30))   # backoff not elapsed: no new attempt
            assert conn.execute("SELECT attempts FROM posts WHERE clip_id=?", (cid,)).fetchone()[0] == attempt
            now = db.parse_iso(post["next_attempt_at"])
    assert post["status"] == "failed"
    alerts = (cfg.data_dir / "notifications.log").read_text()
    assert "Post failed" in alerts and "boom" in alerts


def test_non_retryable_error_fails_immediately(cfg, conn, tmp_path):
    cid = make_clip(conn, tmp_path)
    plan_posts(conn, cfg, cid, now=NOW, platforms=["tiktok"])
    publish_tick(conn, cfg, FakePublisher(fail_times=1, retryable=False), now=NOW)
    assert conn.execute("SELECT status FROM posts WHERE clip_id=?", (cid,)).fetchone()[0] == "failed"


def test_tiktok_not_submitted_beyond_ten_days(cfg, conn, tmp_path):
    cid = make_clip(conn, tmp_path)
    db.insert_post(conn, cid, "tiktok", NOW + timedelta(days=20))
    db.insert_post(conn, cid, "youtube", NOW + timedelta(days=20))
    pub = FakePublisher()
    publish_tick(conn, cfg, pub, now=NOW)
    assert [p for p, *_ in pub.scheduled] == ["youtube"]


def test_missed_slot_is_replanned_not_burst(cfg, conn, tmp_path):
    cid = make_clip(conn, tmp_path)
    db.insert_post(conn, cid, "instagram", NOW - timedelta(days=1))
    stats = publish_tick(conn, cfg, FakePublisher(), now=NOW)
    assert stats["replanned"] == 1
    planned = conn.execute("SELECT publish_at FROM posts WHERE status='planned'").fetchone()[0]
    assert db.parse_iso(planned) > NOW


def test_dry_run_holds_everything(cfg, conn, tmp_path):
    cfg.data["review"]["dry_run"] = True
    cid = make_clip(conn, tmp_path)
    plan_posts(conn, cfg, cid, now=NOW, platforms=["tiktok"])
    pub = FakePublisher()
    stats = publish_tick(conn, cfg, pub, now=NOW + timedelta(days=1))
    assert pub.scheduled == [] and stats["held"] == 1


def test_rejected_clips_never_post(cfg, conn, tmp_path):
    cid = make_clip(conn, tmp_path, status="rejected")
    db.insert_post(conn, cid, "tiktok", NOW + timedelta(hours=1))
    pub = FakePublisher()
    publish_tick(conn, cfg, pub, now=NOW + timedelta(hours=2))
    assert pub.scheduled == []


def test_platform_copy_fallbacks():
    clip = {"title": "T", "caption": "Cap", "hashtags": ["kir", "#mindset"], "platform_captions": {}}
    assert copy_for(clip, "tiktok")[1].endswith("#kir #mindset")
    assert copy_for(clip, "youtube")[1].endswith("#shorts")
    assert len(copy_for({**clip, "caption": "x" * 400}, "x")[1]) <= 280


def test_next_slot_skips_past_slots_today(cfg, conn):
    slot = next_slot(conn, cfg, "tiktok", datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc))  # 16:00 NY
    assert slot.astimezone(ZoneInfo("America/New_York")).strftime("%H:%M") == "19:30"
