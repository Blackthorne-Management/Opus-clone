"""Folder watcher: detects new recordings and hands them on only once OBS has finished writing."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config

log = logging.getLogger("kir.watcher")


@dataclass
class _Seen:
    size: int
    mtime: float
    since: float = field(default_factory=time.monotonic)


class StabilityTracker:
    """Decides when a file is finished: allowed extension, size+mtime unchanged for `stable_seconds`,
    and openable for writing (OBS keeps a handle open while recording on Windows)."""

    def __init__(self, extensions: list[str], stable_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.extensions = {e.lower() for e in extensions}
        self.stable_seconds = stable_seconds
        self.clock = clock
        self._seen: dict[Path, _Seen] = {}
        self._done: dict[Path, tuple[int, float]] = {}  # path -> (size, mtime) already handled

    def accepts(self, path: Path) -> bool:
        name = path.name
        if name.startswith((".", "~")) or path.suffix.lower() not in self.extensions:
            return False
        return path.is_file()

    def ready(self, path: Path) -> bool:
        try:
            st = path.stat()
        except FileNotFoundError:
            self._seen.pop(path, None)
            return False
        now = self.clock()
        prev = self._seen.get(path)
        if prev is None or prev.size != st.st_size or prev.mtime != st.st_mtime:
            self._seen[path] = _Seen(st.st_size, st.st_mtime, now)
            return False
        if st.st_size == 0 or now - prev.since < self.stable_seconds:
            return False
        return _not_locked(path)

    def check(self, candidates) -> list[Path]:
        ready = []
        for p in sorted(set(candidates)):
            if not self.accepts(p) or self._done.get(p) == _signature(p):
                continue
            if self.ready(p):
                ready.append(p)
        return ready

    def mark_done(self, path: Path) -> None:
        """Remember a handled file so a copy left behind (e.g. a failed move) is not retried in a loop.
        A new recording that reuses the same name has a different size/mtime and is picked up."""
        self._seen.pop(path, None)
        sig = _signature(path)
        if sig:
            self._done[path] = sig


def _signature(path: Path) -> tuple[int, float] | None:
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime)
    except OSError:
        return None


def _not_locked(path: Path) -> bool:
    try:
        with open(path, "ab"):
            return True
    except OSError:
        return False


class _Handler(FileSystemEventHandler):
    def __init__(self, pending: set[Path], lock: threading.Lock):
        self.pending, self.lock = pending, lock

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        target = getattr(event, "dest_path", None) or event.src_path
        with self.lock:
            self.pending.add(Path(str(target)))


class InboxWatcher:
    """Watches the inbox with watchdog events plus a periodic rescan (covers missed events and files that
    were already there at startup). Finished files are passed to `on_ready` one at a time."""

    def __init__(self, cfg: Config, on_ready: Callable[[Path], None]):
        self.inbox = cfg.path("inbox")
        w = cfg.get("watcher") or {}
        self.poll = float(w.get("poll_seconds", 10))
        self.tracker = StabilityTracker(w.get("extensions", [".mp4", ".mkv", ".mov", ".flv"]),
                                        float(w.get("stable_seconds", 60)))
        self.on_ready = on_ready
        self._pending: set[Path] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def scan(self) -> list[Path]:
        with self._lock:
            candidates = set(self._pending) | {p for p in self.inbox.iterdir() if p.is_file()}
            self._pending.clear()
        ready = self.tracker.check(candidates)
        # Keep watching anything not ready yet.
        with self._lock:
            self._pending |= {p for p in candidates if p not in ready and self.tracker.accepts(p)}
        return ready

    def run(self) -> None:
        observer = Observer()
        observer.schedule(_Handler(self._pending, self._lock), str(self.inbox), recursive=False)
        observer.start()
        log.info("Watching %s (files must be unchanged for %ss)", self.inbox, self.tracker.stable_seconds)
        try:
            while not self._stop.is_set():
                for path in self.scan():
                    log.info("Recording finished: %s", path.name)
                    try:
                        self.on_ready(path)
                    except Exception:
                        log.exception("Processing %s crashed", path)
                    self.tracker.mark_done(path)
                self._stop.wait(self.poll)
        finally:
            observer.stop()
            observer.join(timeout=5)

    def stop(self) -> None:
        self._stop.set()
