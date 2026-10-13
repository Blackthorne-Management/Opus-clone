from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kir_clipper import db
from kir_clipper import settings as settings_mod
from kir_clipper.optimize import suggest, weekly
from kir_clipper.performance import record
from kir_clipper.scheduler import next_slot

NY = ZoneInfo("America/New_York")


def history(conn, tmp_path, views_by_hour: dict, per_hour=5, platform="tiktok"):
    sid = db.insert_stream(conn, tmp_path / "s.mp4", f"hist-{platform}")
    day = datetime(2026, 9, 1, tzinfo=NY)
    n = 0
    for hour, views in views_by_hour.items():
        for k in range(per_hour):
            cid = db.insert_clip(conn, sid, {"start": 0, "end": 30, "score": 90}, "posted")
            when = (day + timedelta(days=n, hours=hour, minutes=30)).astimezone(timezone.utc)
            pid = db.insert_post(conn, cid, platform, when, step="feed")
            db.update_post(conn, pid, status="posted", posted_at=db.iso(when))
            record(conn, pid, source="upload_post", at=when + timedelta(days=3), views=views + k * 10)
            n += 1


def test_suggests_best_hours_and_keeps_exploring(cfg, conn, tmp_path):
    cfg.data["optimize"]["min_posts"] = 10
    history(conn, tmp_path, {8: 300, 12: 900, 17: 2000, 21: 5000})
    s = suggest(conn, cfg, "tiktok")
    assert s["enough"] and len(s["suggested"]) == 3
    assert "21:30" in s["suggested"] and "17:30" in s["suggested"]   # the two best hours
    assert s["explore_slot"] in s["suggested"]                          # one slot tests a new hour
    lift = {h["hour"]: h["lift"] for h in s["hours"]}
    assert lift[21] > 1 > lift[8]


def test_waits_for_enough_data(cfg, conn, tmp_path):
    history(conn, tmp_path, {12: 500, 19: 900}, per_hour=2)
    s = suggest(conn, cfg, "tiktok")
    assert not s["enough"] and s["suggested"] == s["current"]


def test_apply_sets_per_platform_times(cfg, conn, tmp_path):
    cfg.root = tmp_path
    cfg.data["optimize"].update(min_posts=10, auto_apply=True)
    history(conn, tmp_path, {8: 300, 12: 900, 17: 2000, 21: 5000})
    lines = weekly(conn, cfg)
    assert lines and "tiktok" in lines[0]
    assert cfg.get("publishing.slots_by_platform.tiktok") == suggest(conn, cfg, "tiktok")["suggested"]
    # TikTok now uses its own times; Instagram keeps the shared ones.
    now = datetime(2026, 10, 12, 4, 0, tzinfo=timezone.utc)
    tik = next_slot(conn, cfg, "tiktok", now).astimezone(NY).strftime("%H:%M")
    ig = next_slot(conn, cfg, "instagram", now).astimezone(NY).strftime("%H:%M")
    assert tik in cfg.get("publishing.slots_by_platform.tiktok") and ig in cfg.get("publishing.slots")


def test_per_platform_slot_setting_is_validated(cfg, tmp_path):
    cfg.root = tmp_path
    with pytest.raises(ValueError):
        settings_mod.update(cfg, {"publishing.slots_by_platform.tiktok": "9:99"})
    with pytest.raises(ValueError):
        settings_mod.update(cfg, {"publishing.slots_by_platform.myspace": "9:00"})
