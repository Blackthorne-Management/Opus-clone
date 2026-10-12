"""Settings the owner can change from the dashboard. Saved to config.local.yaml, applied immediately."""

from __future__ import annotations

import re

import yaml

from .config import Config, _deep_merge, secret

# key -> (type, validator/choices, help shown in the dashboard)
FIELDS: dict[str, tuple] = {
    "review.dry_run": (bool, None, "Dry run: score and render everything but post nothing"),
    "review.auto_post_threshold": (int, (50, 99), "Clips scoring this or higher post automatically"),
    "review.auto_reject_floor": (int, (0, 95), "Clips scoring below this are dropped"),
    "highlights.max_clips_per_stream": (int, (1, 40), "Most clips kept from one stream"),
    "cadence.enabled": (bool, None, "Post everything five times (trial, feed, mirrored trial, +90, +180)"),
    "cadence.trial_mode": (str, ("manual", "api", "skip"), "Trial reels: post by hand, through Upload-Post, or skip"),
    "reedit.style": (str, ("reedit", "same"), "+90/+180 reposts: new edit each time, or the same file"),
    "reedit.max_reposts": (int, (0, 5), "How many +90-day reposts per clip"),
    "publishing.slots": (list, None, "Posting times each day (HH:MM, your time zone)"),
    "publishing.max_posts_per_day.instagram": (int, (0, 20), "Instagram feed posts per day"),
    "publishing.max_posts_per_day.instagram_trial": (int, (0, 20), "Instagram trial reels per day"),
    "publishing.max_posts_per_day.tiktok": (int, (0, 20), "TikTok posts per day"),
    "publishing.max_posts_per_day.youtube": (int, (0, 20), "YouTube posts per day"),
    "longform.enabled": (bool, None, "Make a long-form YouTube version of every stream"),
    "dashboard.lan_access": (bool, None, "Open the dashboard from your phone on the same Wi-Fi (restart needed)"),
    "dashboard.pin": (str, "pin", "PIN for phone access, 4-8 digits (restart needed)"),
}


def current(cfg: Config) -> dict:
    return {k: {"value": cfg.get(k), "type": t.__name__, "choices": list(c) if isinstance(c, tuple) and t is str else None,
                "range": list(c) if isinstance(c, tuple) and t is int else None, "help": h}
            for k, (t, c, h) in FIELDS.items()}


def _coerce(key: str, value):
    typ, rule, _ = FIELDS[key]
    if typ is bool:
        if isinstance(value, str):
            value = value.lower() in ("1", "true", "yes", "on")
        return bool(value)
    if typ is int:
        value = int(value)
        lo, hi = rule
        if not lo <= value <= hi:
            raise ValueError(f"{FIELDS[key][2]}: must be between {lo} and {hi}")
        return value
    if typ is list:
        items = value if isinstance(value, list) else re.split(r"[,\s]+", str(value))
        items = [i.strip() for i in items if i.strip()]
        for i in items:
            if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", i):
                raise ValueError(f"'{i}' is not a time like 11:30 or 19:30")
        if not items:
            raise ValueError("Add at least one posting time")
        return sorted(items, key=lambda x: tuple(int(n) for n in x.split(":")))
    value = str(value).strip()
    if rule == "pin":
        if value and not re.fullmatch(r"\d{4,8}", value):
            raise ValueError("The PIN must be 4 to 8 digits")
        return value
    if value not in rule:
        raise ValueError(f"{FIELDS[key][2]}: choose one of {', '.join(rule)}")
    return value


def _nest(dotted: str, value) -> dict:
    out: dict = {}
    node = out
    parts = dotted.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value
    return out


def update(cfg: Config, changes: dict) -> dict:
    """Validate, apply to the running config, and persist to config.local.yaml. Returns what changed."""
    clean = {}
    for key, value in changes.items():
        if key not in FIELDS:
            raise ValueError(f"'{key}' can't be changed from the dashboard")
        clean[key] = _coerce(key, value)
    merged = {**{k: cfg.get(k) for k in FIELDS}, **clean}
    if merged["review.auto_reject_floor"] >= merged["review.auto_post_threshold"]:
        raise ValueError("The drop-below score must be lower than the auto-post score")
    if clean.get("review.dry_run") is False and cfg.get("publishing.provider") == "upload_post":
        if not secret("UPLOAD_POST_API_KEY"):
            raise ValueError("Can't go live yet: add UPLOAD_POST_API_KEY to the .env file first")
        if not cfg.get("publishing.upload_post.user"):
            raise ValueError("Can't go live yet: set publishing.upload_post.user in config.yaml")
    if clean.get("dashboard.lan_access") and not str(merged.get("dashboard.pin") or ""):
        raise ValueError("Set a PIN before turning on phone access")

    patch: dict = {}
    for key, value in clean.items():
        patch = _deep_merge(patch, _nest(key, value))
    local = cfg.root / "config.local.yaml"
    existing = yaml.safe_load(local.read_text(encoding="utf-8")) if local.exists() else {}
    local.write_text("# Changed from the dashboard's Settings tab. Same keys as config.yaml; these win.\n"
                     + yaml.safe_dump(_deep_merge(existing or {}, patch), sort_keys=False), encoding="utf-8")
    cfg.data = _deep_merge(cfg.data, patch)
    return clean
