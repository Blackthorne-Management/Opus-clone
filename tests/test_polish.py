from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

from kir_clipper import db
from kir_clipper import settings as settings_mod
from kir_clipper.importer import import_reel
from kir_clipper.maintenance import backup_db, beat, service_status
from kir_clipper.web.app import create_app

from .conftest import FakeTranscriber, make_video

NOW = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def local_cfg(cfg, tmp_path):
    cfg.root = tmp_path          # config.local.yaml goes to a temp folder, not the repo
    return cfg


def test_settings_validate_and_persist(local_cfg, monkeypatch):
    cfg = local_cfg
    with pytest.raises(ValueError, match="lower than"):
        settings_mod.update(cfg, {"review.auto_reject_floor": 95})
    with pytest.raises(ValueError, match="not a time"):
        settings_mod.update(cfg, {"publishing.slots": "11:30, 25:00"})
    with pytest.raises(ValueError, match="UPLOAD_POST_API_KEY"):
        monkeypatch.delenv("UPLOAD_POST_API_KEY", raising=False)
        cfg.data["publishing"]["provider"] = "upload_post"
        settings_mod.update(cfg, {"review.dry_run": False})
    with pytest.raises(ValueError, match="PIN"):
        settings_mod.update(cfg, {"dashboard.lan_access": True})
    with pytest.raises(ValueError, match="can't be changed"):
        settings_mod.update(cfg, {"paths.inbox": "C:/"})

    changed = settings_mod.update(cfg, {"review.auto_post_threshold": "85", "publishing.slots": "19:30, 9:15",
                                        "cadence.trial_mode": "skip"})
    assert changed["publishing.slots"] == ["9:15", "19:30"]
    assert cfg.get("review.auto_post_threshold") == 85 and cfg.get("cadence.trial_mode") == "skip"
    saved = yaml.safe_load((cfg.root / "config.local.yaml").read_text())
    assert saved["review"]["auto_post_threshold"] == 85 and saved["publishing"]["slots"] == ["9:15", "19:30"]
    settings_mod.update(cfg, {"reedit.max_reposts": 1})              # later saves keep earlier ones
    saved = yaml.safe_load((cfg.root / "config.local.yaml").read_text())
    assert saved["review"]["auto_post_threshold"] == 85 and saved["reedit"]["max_reposts"] == 1


def test_go_live_from_settings(local_cfg, monkeypatch):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "up_test_key_123456")
    local_cfg.data["publishing"]["provider"] = "upload_post"
    client = TestClient(create_app(local_cfg))
    assert client.get("/api/settings").json()["fields"]["review.dry_run"]["value"] is False  # test config is live
    r = client.post("/api/settings", json={"review.dry_run": True})
    assert r.status_code == 200 and local_cfg.dry_run
    r = client.post("/api/settings", json={"review.dry_run": False})
    assert r.status_code == 200 and not local_cfg.dry_run
    bad = client.post("/api/settings", json={"review.auto_post_threshold": 200})
    assert bad.status_code == 400 and "between" in bad.json()["detail"]


def test_phone_access_needs_pin(local_cfg):
    local_cfg.data.setdefault("dashboard", {}).update(lan_access=True, pin="4821")
    client = TestClient(create_app(local_cfg))          # TestClient is not 127.0.0.1, like a phone
    assert client.get("/api/summary").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "Enter your PIN" in client.get("/login").text
    r = client.post("/login", data={"pin": "0000"}, follow_redirects=False)
    assert "Wrong" in r.headers["location"]
    r = client.post("/login", data={"pin": "4821"}, follow_redirects=False)
    assert r.status_code == 303 and "kir_session" in r.cookies
    assert client.get("/api/summary").status_code == 200


def test_pin_lockout(local_cfg):
    local_cfg.data.setdefault("dashboard", {}).update(lan_access=True, pin="4821")
    client = TestClient(create_app(local_cfg))
    for _ in range(5):
        client.post("/login", data={"pin": "1111"}, follow_redirects=False)
    r = client.post("/login", data={"pin": "4821"}, follow_redirects=False)
    assert "Too+many" in r.headers["location"]


def test_backups_rotate(cfg, conn):
    for i in range(4):
        path = backup_db(cfg, keep=2)
        path.rename(path.with_name(f"kir_clipper-2026010{i}-0000.db"))
    backup_db(cfg, keep=2)
    assert len(list((cfg.data_dir / "backups").glob("kir_clipper-*.db"))) == 2


def test_status_bar_data(cfg, conn):
    beat(conn, "watcher", inbox="C:/Streams/inbox", processing=None)
    st = service_status(conn)
    assert st["running"] and st["watcher"]["ok"]
    conn.execute("UPDATE heartbeats SET at=?", (db.iso(db.utcnow() - timedelta(hours=1)),))
    assert not service_status(conn)["running"]
    summary = TestClient(create_app(cfg)).get("/api/summary").json()
    assert "free_gb" in summary and "watcher" in summary["service"]


def test_clean_copy_trim_and_length_warning(cfg, conn, tmp_path):
    reel = make_video(tmp_path / "reel.mp4", seconds=10)
    raw = make_video(tmp_path / "raw.mp4", seconds=40, color="smptehdbars")
    res = import_reel(cfg, conn, reel, NOW - timedelta(hours=1), clean=raw, clean_start=12, clean_end=22,
                      transcriber=FakeTranscriber(), now=NOW)
    clip = db.get_clip(conn, res["clip_id"])
    assert (clip["start"], clip["end"]) == (12, 22) and res["warnings"] == []

    reel2 = make_video(tmp_path / "reel2.mp4", seconds=8, color="rgbtestsrc")
    raw2 = make_video(tmp_path / "raw2.mp4", seconds=30, color="testsrc")
    res2 = import_reel(cfg, conn, reel2, NOW - timedelta(hours=1), clean=raw2, transcriber=FakeTranscriber(), now=NOW)
    assert res2["warnings"] and "30s" in res2["warnings"][0]
    with pytest.raises(ValueError, match="at least 3 seconds"):
        import_reel(cfg, conn, make_video(tmp_path / "r3.mp4", seconds=6, color="yuvtestsrc"), NOW - timedelta(hours=1),
                    clean=make_video(tmp_path / "raw3.mp4", seconds=9, color="pal75bars"), clean_start=5, clean_end=6,
                    transcriber=FakeTranscriber(), now=NOW)
    # A rejected import leaves nothing behind, so fixing the times and retrying works.
    import_reel(cfg, conn, tmp_path / "r3.mp4", NOW - timedelta(hours=1), clean=tmp_path / "raw3.mp4",
                clean_start=1, clean_end=7, transcriber=FakeTranscriber(), now=NOW)


def test_failed_import_leaves_nothing_behind(cfg, conn, tmp_path):
    reel = make_video(tmp_path / "reel.mp4", seconds=8)
    raw = make_video(tmp_path / "raw.mp4", seconds=8, color="smptehdbars")

    class Broken:
        def words(self, wav):
            raise RuntimeError("whisper crashed")

    with pytest.raises(RuntimeError):
        import_reel(cfg, conn, reel, NOW - timedelta(hours=1), clean=raw, transcriber=Broken(), now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM streams").fetchone()[0] == 0
    res = import_reel(cfg, conn, reel, NOW - timedelta(hours=1), clean=raw, transcriber=FakeTranscriber(), now=NOW)
    assert res["clip_id"]
