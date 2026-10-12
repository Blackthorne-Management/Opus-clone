from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from kir_clipper import db
from kir_clipper.cadence import timeline
from kir_clipper.importer import import_reel
from kir_clipper.reedit import find_candidates, run_reedit
from kir_clipper.web.app import create_app

from .conftest import FakeLLM, FakeTranscriber, make_video
from .test_cadence import frame

NOW = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)   # Monday 10:00 in New York


@pytest.fixture(scope="module")
def reel(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("reel") / "my_reel.mp4", seconds=20)


@pytest.fixture(scope="module")
def other_reel(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("reel2") / "older_reel.mp4", seconds=12, color="smptehdbars")


def actions(result):
    return {s["name"]: s["action"] for s in result["steps"]}


def test_posted_this_morning_follows_the_schedule(cfg, conn, reel):
    posted = NOW - timedelta(hours=2)
    res = import_reel(cfg, conn, reel, posted, title="Discipline", platforms=["instagram", "tiktok"],
                      done_steps=["feed"], now=NOW)
    a = actions(res)
    assert a["feed"] == "already posted"
    assert a["trial"] == "scheduled"                                    # day 1 is still today
    assert a["mirrored_trial"].startswith("skipped (on-screen text")     # text would read backwards
    assert a["repost_1"] == a["repost_2"] == "repost job"
    cid = res["clip_id"]
    feed = conn.execute("SELECT platform, status, posted_at FROM posts WHERE clip_id=? AND step='feed'", (cid,)).fetchall()
    assert {r["platform"] for r in feed} == {"instagram", "tiktok"} and all(r["status"] == "posted" for r in feed)

    tl = timeline(cfg, conn, cid)
    assert [s["label"] for s in tl] == ["Feed Reel", "Trial Reel", "Feed Reel +90 days", "Feed Reel +180 days"]
    assert db.parse_iso(tl[2]["at"]) - posted == timedelta(days=90)

    # 90 days later the repost job reposts the file as posted (no clean copy), automatically.
    later = posted + timedelta(days=91)
    assert [c["root_id"] for c in find_candidates(conn, cfg, later)] == [cid]
    made = run_reedit(cfg, conn, llm=FakeLLM(), now=later)
    new = db.get_clip(conn, made[0])
    assert new["parent_clip_id"] == cid and new["version"] == 2 and new["status"] == "approved"
    assert new["file_path"] == db.get_clip(conn, cid)["file_path"]
    assert {r["step"] for r in conn.execute("SELECT step FROM posts WHERE clip_id=?", (made[0],))} == {"repost_1"}


def test_dates_already_passed_are_skipped_or_caught_up(cfg, conn, reel, other_reel):
    old = NOW - timedelta(days=10)
    a = actions(import_reel(cfg, conn, reel, old, done_steps=["feed"], now=NOW))
    assert a["trial"] == a["mirrored_trial"] == "skipped (date passed)"

    res = import_reel(cfg, conn, other_reel, old, done_steps=["feed"], missed="now", has_text=False, now=NOW)
    a = actions(res)
    assert a["trial"] == a["mirrored_trial"] == "scheduled"
    mirrored = conn.execute("SELECT * FROM posts WHERE clip_id=? AND step='mirrored_trial'", (res["clip_id"],)).fetchone()
    assert db.parse_iso(mirrored["publish_at"]) > NOW
    original = db.get_clip(conn, res["clip_id"])["file_path"]
    f0, f1 = frame(original, t=2), frame(mirrored["file_path"], t=2)
    assert np.abs(f0[:, ::-1] - f1).mean() < np.abs(f0 - f1).mean() * 0.5    # whole picture flipped


def test_clean_copy_gets_a_readable_mirrored_version(cfg, conn, reel, other_reel):
    t = FakeTranscriber()
    res = import_reel(cfg, conn, reel, NOW - timedelta(hours=1), clean=other_reel, done_steps=["trial", "feed"],
                      transcriber=t, now=NOW)
    assert t.calls == 1
    a = actions(res)
    assert a["trial"] == "already posted" and a["mirrored_trial"] == "scheduled"
    post = conn.execute("SELECT file_path FROM posts WHERE step='mirrored_trial'").fetchone()
    assert post["file_path"].endswith("_mirror.mp4")
    clip = db.clip_dict(db.get_clip(conn, res["clip_id"]))
    assert clip["recipe"]["clean"] and clip["recipe"]["imported"]


def test_earlier_reposts_are_recorded(cfg, conn, reel):
    first = NOW - timedelta(days=100)
    res = import_reel(cfg, conn, reel, first, done_steps=["trial", "feed", "mirrored_trial", "repost_1"], now=NOW)
    lineage = conn.execute("SELECT version FROM clips WHERE id=? OR parent_clip_id=?",
                           (res["clip_id"], res["clip_id"])).fetchall()
    assert sorted(r["version"] for r in lineage) == [1, 2]
    assert find_candidates(conn, cfg, NOW) == []                       # repost 1 was ~9 days ago
    assert [c["root_id"] for c in find_candidates(conn, cfg, NOW + timedelta(days=85))] == [res["clip_id"]]


def test_rejects_duplicates_and_future_dates(cfg, conn, reel):
    with pytest.raises(ValueError, match="future"):
        import_reel(cfg, conn, reel, NOW + timedelta(days=2), now=NOW)
    import_reel(cfg, conn, reel, NOW - timedelta(days=1), now=NOW)
    with pytest.raises(ValueError, match="already been imported"):
        import_reel(cfg, conn, reel, NOW - timedelta(days=1), now=NOW)


def test_dashboard_upload(cfg, reel):
    client = TestClient(create_app(cfg))
    with open(reel, "rb") as fh:
        r = client.post("/api/import", files={"video": ("my_reel.mp4", fh, "video/mp4")},
                        data={"posted_at": (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M"),
                              "title": "Old reel", "platforms": "instagram,youtube", "done": "feed,trial",
                              "has_text": "true", "missed": "skip", "auto_repost": "false"})
    assert r.status_code == 200, r.text
    assert {s["name"] for s in r.json()["steps"]} >= {"trial", "feed", "mirrored_trial", "repost_1", "repost_2"}
    assert client.get("/api/summary").json()["cadence"][0]["label"] == "Trial Reel"
    assert not any((cfg.data_dir / "imports" / "incoming").iterdir())  # temporary upload cleaned up
    with open(reel, "rb") as fh:
        dup = client.post("/api/import", files={"video": ("my_reel.mp4", fh, "video/mp4")},
                          data={"posted_at": "2026-10-01T10:00"})
    assert dup.status_code == 400 and "already" in dup.json()["detail"]
