from kir_clipper.watcher import InboxWatcher, StabilityTracker


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_half_written_file_waits_until_stable(tmp_path):
    clock = Clock()
    tracker = StabilityTracker([".mp4", ".mkv"], stable_seconds=60, clock=clock)
    f = tmp_path / "stream.mkv"
    f.write_bytes(b"x" * 100)
    assert tracker.check([f]) == []           # first sighting
    clock.t = 30
    with open(f, "ab") as fh:                 # OBS still writing
        fh.write(b"y" * 100)
    assert tracker.check([f]) == []
    clock.t = 80                              # only 50s since last growth
    assert tracker.check([f]) == []
    clock.t = 95                              # 65s unchanged
    assert tracker.check([f]) == [f]


def test_ignores_partial_and_unknown_extensions(tmp_path):
    tracker = StabilityTracker([".mp4"], stable_seconds=0, clock=lambda: 0)
    for name in ("a.mp4.part", "b.tmp", ".hidden.mp4", "~lock.mp4", "notes.txt"):
        (tmp_path / name).write_bytes(b"data")
    files = list(tmp_path.iterdir())
    tracker.check(files)
    assert tracker.check(files) == []


def test_empty_file_never_ready(tmp_path):
    tracker = StabilityTracker([".mp4"], stable_seconds=0, clock=lambda: 0)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"")
    tracker.check([f])
    assert tracker.check([f]) == []


def test_handled_file_not_retried_but_new_recording_with_same_name_is(tmp_path):
    tracker = StabilityTracker([".mp4"], stable_seconds=0, clock=lambda: 0)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"one")
    tracker.check([f])
    assert tracker.check([f]) == [f]
    tracker.mark_done(f)
    assert tracker.check([f]) == [] and tracker.check([f]) == []
    f.write_bytes(b"a different recording")
    tracker.check([f])
    assert tracker.check([f]) == [f]


def test_inbox_watcher_scan_finds_existing_files(cfg):
    inbox = cfg.path("inbox")
    (inbox / "s.mp4").write_bytes(b"video")
    w = InboxWatcher(cfg, on_ready=lambda p: None)
    assert w.scan() == []            # first sighting
    assert [p.name for p in w.scan()] == ["s.mp4"]
