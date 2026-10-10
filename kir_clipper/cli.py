"""Command line entry point: `kir <command>`. Run `kir --help` for the list."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import db, media
from .config import Config, load_config, secret
from .logging_setup import setup_logging

log = logging.getLogger("kir")


def _ctx(args) -> tuple[Config, object]:
    overrides = {}
    if getattr(args, "dry_run", False):
        overrides = {"review": {"dry_run": True}}
    cfg = load_config(args.config, overrides)
    setup_logging(cfg)
    return cfg, db.connect(cfg.db_path)


# --------------------------------------------------------------------------- commands

def cmd_doctor(args) -> int:
    cfg, _ = _ctx(args)
    ok = True

    def check(label, passed, hint=""):
        nonlocal ok
        ok &= bool(passed)
        print(f"  [{'OK' if passed else '!!'}] {label}" + ("" if passed else f"  -> {hint}"))

    print("KIR clipper setup check")
    check("ffmpeg on PATH", shutil.which("ffmpeg"), "install: winget install Gyan.FFmpeg")
    check("ffprobe on PATH", shutil.which("ffprobe"), "comes with ffmpeg")
    if shutil.which("ffmpeg"):
        print(f"  [..] video encoder: {media.pick_encoder(cfg.get('render.encoder', 'auto'))}")
    check("ANTHROPIC_API_KEY set", secret("ANTHROPIC_API_KEY"), "add it to .env")
    if cfg.get("publishing.provider") == "upload_post":
        check("UPLOAD_POST_API_KEY set", secret("UPLOAD_POST_API_KEY") or cfg.dry_run,
              "add it to .env (not needed while dry_run is on)")
    backend = cfg.get("transcription.backend")
    if backend == "openai":
        check("OPENAI_API_KEY set", secret("OPENAI_API_KEY"), "add it to .env")
    else:
        try:
            import faster_whisper  # noqa: F401
            check("faster-whisper installed", True)
        except ImportError:
            check("faster-whisper installed", False, 'pip install -e ".[whisper]"')
    try:
        import cv2  # noqa: F401
        check("face tracking (opencv) installed", True)
    except ImportError:
        print('  [--] opencv not installed: clips use center crop. pip install -e ".[faces]" for face tracking')
    logo = cfg.get("brand.logo")
    if logo and not cfg.resolve(logo).exists():
        print(f"  [--] logo not found at {cfg.resolve(logo)}: clips render without a watermark")
    for key in ("inbox", "processed", "failed", "data"):
        print(f"  [..] {key}: {cfg.path(key)}")
    print(f"  [..] mode: {'DRY RUN (nothing posts)' if cfg.dry_run else 'LIVE'}")
    return 0 if ok else 1


def cmd_process(args) -> int:
    from .pipeline import process_file
    cfg, conn = _ctx(args)
    sid = process_file(cfg, conn, Path(args.file))
    print(f"stream id: {sid}")
    return 0


def cmd_reprocess(args) -> int:
    from .pipeline import reprocess
    cfg, conn = _ctx(args)
    reprocess(cfg, conn, args.stream_id)
    return 0


def cmd_watch(args) -> int:
    from .pipeline import process_file
    from .watcher import InboxWatcher
    cfg, conn = _ctx(args)
    InboxWatcher(cfg, lambda p: process_file(cfg, conn, p)).run()
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    from .web.app import create_app
    cfg, _ = _ctx(args)
    uvicorn.run(create_app(cfg), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def cmd_publish(args) -> int:
    from .publishers import get_publisher
    from .scheduler import publish_tick
    cfg, conn = _ctx(args)
    print(publish_tick(conn, cfg, get_publisher(cfg)))
    return 0


def cmd_reedit(args) -> int:
    from .reedit import run_reedit
    overrides = {"reedit": {"interval_days": args.interval_days}} if args.interval_days is not None else {}
    cfg = load_config(args.config, overrides)
    setup_logging(cfg)
    created = run_reedit(cfg, db.connect(cfg.db_path))
    print(f"re-edits created: {created}")
    return 0


def cmd_summary(args) -> int:
    from .notify import daily_summary, notify
    cfg, conn = _ctx(args)
    text = daily_summary(conn, cfg)
    print(text)
    if args.send:
        notify(cfg, "Daily summary", text)
    return 0


def cmd_perf_import(args) -> int:
    from .performance import import_csv
    _, conn = _ctx(args)
    print(f"imported {import_csv(conn, Path(args.csv))} rows")
    return 0


def cmd_perf_report(args) -> int:
    from .performance import score_report
    _, conn = _ctx(args)
    print(score_report(conn))
    return 0


def cmd_longform(args) -> int:
    from .longform import build, set_segments
    cfg, conn = _ctx(args)
    if args.action == "list":
        for r in conn.execute("SELECT * FROM longform ORDER BY stream_id, idx"):
            print(f"#{r['id']} stream {r['stream_id']} part {r['idx']} {r['start']:.0f}-{r['end']:.0f}s "
                  f"{r['status']} {r['error'] or ''}")
    elif args.action == "build":
        print(f"clip id: {build(cfg, conn, args.id)}")
    elif args.action == "whole":
        stream = conn.execute("SELECT duration FROM streams WHERE id=?", (args.id,)).fetchone()
        ids = set_segments(conn, args.id, [{"start": 0, "end": stream["duration"]}], stream["duration"])
        print(f"queued long-form job(s): {ids}")
    return 0


def cmd_run(args) -> int:
    """Everything in one long-running process: watcher, publisher loop, daily jobs, dashboard."""
    import uvicorn

    from .notify import daily_summary, notify
    from .pipeline import process_file
    from .publishers import get_publisher
    from .reedit import run_reedit
    from .scheduler import publish_tick
    from .watcher import InboxWatcher
    from .web.app import create_app

    cfg, conn = _ctx(args)
    stop = threading.Event()
    log.info("KIR clipper starting (%s)", "DRY RUN" if cfg.dry_run else "LIVE")

    work_conn = db.connect(cfg.db_path)
    watcher = InboxWatcher(cfg, lambda p: process_file(cfg, work_conn, p))
    threading.Thread(target=watcher.run, name="watcher", daemon=True).start()

    if not args.no_dashboard:
        server = uvicorn.Server(uvicorn.Config(create_app(cfg), host="127.0.0.1", port=args.port, log_level="warning"))
        threading.Thread(target=server.run, name="dashboard", daemon=True).start()
        log.info("Dashboard: http://127.0.0.1:%d", args.port)

    def publisher_loop():
        pconn = db.connect(cfg.db_path)
        publisher = None
        interval = max(5.0, float(cfg.get("publishing.status_poll_seconds", 30)))
        while not stop.is_set():
            try:
                if not cfg.dry_run and publisher is None:
                    publisher = get_publisher(cfg)
                stats = publish_tick(pconn, cfg, publisher)
                if any(v for k, v in stats.items() if k != "held"):
                    log.info("Publish tick: %s", stats)
            except Exception as exc:
                log.exception("Publish loop error")
                if "not set" in str(exc):
                    notify(cfg, "Posting is blocked", str(exc), level="alert")
                    stop.wait(3600)
            stop.wait(interval)

    threading.Thread(target=publisher_loop, name="publisher", daemon=True).start()

    def longform_loop():
        from .longform import worker_tick
        lconn = db.connect(cfg.db_path)
        while not stop.is_set():
            try:
                if worker_tick(cfg, lconn) is None:
                    stop.wait(20)
            except Exception:
                log.exception("Long-form worker error")
                stop.wait(60)

    threading.Thread(target=longform_loop, name="longform", daemon=True).start()

    tz = ZoneInfo(cfg.get("publishing.upload_post.timezone") or "UTC")
    daily_hour = int(cfg.get("daily.hour", 9))
    try:
        while True:
            local = datetime.now(tz)
            today = local.date().isoformat()
            done = conn.execute("SELECT 1 FROM events WHERE kind='daily_jobs' AND ref=?", (today,)).fetchone()
            if local.hour >= daily_hour and not done:
                db.log_event(conn, "daily_jobs", today)
                try:
                    run_reedit(cfg, conn)
                except Exception:
                    log.exception("Re-edit job failed")
                notify(cfg, "Daily summary", daily_summary(conn, cfg))
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Stopping")
        stop.set()
        watcher.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kir", description="KIR Auto-Clipper")
    p.add_argument("--config", default=None, help="path to config.yaml (default: ./config.yaml or KIR_CONFIG)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="run everything: watcher, posting, daily jobs, dashboard")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-dashboard", action="store_true")
    s.add_argument("--dry-run", action="store_true", help="force dry-run mode")
    s.set_defaults(fn=cmd_run)

    sub.add_parser("doctor", help="check the setup").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("process", help="process one recording now")
    s.add_argument("file")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_process)
    s = sub.add_parser("reprocess", help="re-run a failed stream")
    s.add_argument("stream_id", type=int)
    s.set_defaults(fn=cmd_reprocess)
    sub.add_parser("watch", help="watch the inbox only").set_defaults(fn=cmd_watch)
    s = sub.add_parser("serve", help="dashboard only")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_serve)
    sub.add_parser("publish", help="run one posting tick").set_defaults(fn=cmd_publish)
    s = sub.add_parser("reedit", help="run the re-edit job now")
    s.add_argument("--interval-days", type=float, default=None, help="override interval (e.g. 0 or 1 to test)")
    s.set_defaults(fn=cmd_reedit)
    s = sub.add_parser("summary", help="print the daily summary")
    s.add_argument("--send", action="store_true", help="also send it to the notification webhook")
    s.set_defaults(fn=cmd_summary)
    s = sub.add_parser("perf-import", help="import performance numbers from CSV")
    s.add_argument("csv")
    s.set_defaults(fn=cmd_perf_import)
    sub.add_parser("perf-report", help="score vs performance report").set_defaults(fn=cmd_perf_report)
    s = sub.add_parser("longform", help="long-form YouTube edits: list | build <longform id> | whole <stream id>")
    s.add_argument("action", choices=["list", "build", "whole"])
    s.add_argument("id", type=int, nargs="?")
    s.set_defaults(fn=cmd_longform)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
