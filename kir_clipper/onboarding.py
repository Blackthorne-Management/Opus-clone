""""Make it yours": the first-run setup that turns this copy into someone else's clipper.

Everything personal is saved outside the files that come from GitHub, so updating the app never
overwrites it:
  config.local.yaml   name, call to action, time zone, Upload-Post profile, recordings folder
  my-criteria.md      what makes a good clip for you (sent to Claude for every stream)
  brand/logo.png      your logo (optional)
  brand/my-voice.md, brand/my-audience.md   written by the Script studio
"""

from __future__ import annotations

import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .config import Config, _deep_merge

CRITERIA_FILE = "my-criteria.md"
TEMPLATE = "virality_criteria.template.md"


def current(cfg: Config) -> dict:
    criteria_path = cfg.resolve(cfg.get("highlights.criteria_file", "virality_criteria.md"))
    logo = cfg.resolve(cfg.get("brand.logo") or "brand/logo.png")
    return {
        "done": bool(cfg.get("setup.done")),
        "name": cfg.get("brand.name") or "",
        "cta_text": cfg.get("brand.cta_text") or "",
        "timezone": cfg.get("publishing.upload_post.timezone") or "",
        "upload_post_user": cfg.get("publishing.upload_post.user") or "",
        "inbox": str(cfg.resolve(cfg.data["paths"]["inbox"])),
        "criteria": criteria_path.read_text(encoding="utf-8") if criteria_path.exists() else "",
        "criteria_file": str(criteria_path),
        "has_logo": logo.exists(),
    }


def starter_criteria(cfg: Config, name: str, topics: str, wants: str) -> str:
    template = cfg.resolve(TEMPLATE)
    text = template.read_text(encoding="utf-8") if template.exists() else "# What makes a good clip for {name}\n"
    return (text.replace("{name}", name.strip() or "the creator")
                .replace("{topics}", topics.strip() or "(your topics)")
                .replace("{wants}", wants.strip() or "(what your viewers come for)"))


def save(cfg: Config, form: dict, logo_bytes: bytes | None = None) -> dict:
    """Validate and save. Returns {"restart": bool} (a new recordings folder needs a restart)."""
    name = str(form.get("name") or "").strip()
    if not name:
        raise ValueError("Enter your creator name or handle")
    tz = str(form.get("timezone") or "").strip()
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"'{tz}' isn't a time zone. Use one like America/New_York, Europe/London or America/Los_Angeles")
    inbox = str(form.get("inbox") or "").strip()
    if not inbox:
        raise ValueError("Enter the folder OBS records into")
    user = str(form.get("upload_post_user") or "").strip()
    if user and not re.fullmatch(r"[\w.\-]{1,64}", user):
        raise ValueError("The Upload-Post profile name can only use letters, numbers, dots, dashes and underscores")

    criteria = str(form.get("criteria") or "").strip()
    if not criteria:
        criteria = starter_criteria(cfg, name, str(form.get("topics") or ""), str(form.get("wants") or ""))
    (cfg.root / CRITERIA_FILE).write_text(criteria.replace("\r\n", "\n").rstrip() + "\n", encoding="utf-8")

    restart = str(cfg.resolve(inbox)) != str(cfg.resolve(cfg.data["paths"]["inbox"]))
    patch = {"setup": {"done": True},
             "brand": {"name": name, "cta_text": str(form.get("cta_text") or "").strip()},
             "publishing": {"upload_post": {"timezone": tz, "user": user}},
             "paths": {"inbox": inbox},
             "highlights": {"criteria_file": CRITERIA_FILE}}
    if logo_bytes:
        logo = cfg.root / "brand" / "logo.png"
        logo.parent.mkdir(parents=True, exist_ok=True)
        logo.write_bytes(logo_bytes)
        patch["brand"]["logo"] = "brand/logo.png"

    local = cfg.root / "config.local.yaml"
    existing = yaml.safe_load(local.read_text(encoding="utf-8")) if local.exists() else {}
    local.write_text("# Your own settings (Settings tab and Make it yours). Same keys as config.yaml; these win.\n"
                     + yaml.safe_dump(_deep_merge(existing or {}, patch), sort_keys=False), encoding="utf-8")
    cfg.data = _deep_merge(cfg.data, patch)
    return {"restart": restart}


def is_image(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def logo_path(cfg: Config) -> Path:
    return cfg.resolve(cfg.get("brand.logo") or "brand/logo.png")
