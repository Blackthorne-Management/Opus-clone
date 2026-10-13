"""Configuration loading: config.yaml, optional config.local.yaml overrides, and .env secrets."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "UPLOAD_POST_API_KEY", "OPENAI_API_KEY", "ALERT_WEBHOOK_URL",
                   "GEMINI_API_KEY", "GOOGLE_API_KEY")


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """Thin wrapper around the merged config dict.

    `root` is the folder holding config.yaml; relative paths in the config (brand assets,
    criteria file) resolve against it.
    """

    def __init__(self, data: dict, root: Path):
        self.data = data
        self.root = root

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def resolve(self, value: str | os.PathLike) -> Path:
        p = Path(os.path.expandvars(str(value))).expanduser()
        return p if p.is_absolute() else (self.root / p)

    def path(self, key: str) -> Path:
        """Resolve a key under `paths:` and make sure the folder exists."""
        p = self.resolve(self.data["paths"][key])
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_dir(self) -> Path:
        return self.path("data")

    def subdir(self, name: str) -> Path:
        p = self.data_dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_dir / "kir_clipper.db"

    @property
    def dry_run(self) -> bool:
        return bool(self.get("review.dry_run", True))


def load_config(path: str | os.PathLike | None = None, overrides: dict | None = None) -> Config:
    cfg_path = Path(path or os.environ.get("KIR_CONFIG", "config.yaml")).expanduser().resolve()
    root = cfg_path.parent
    load_dotenv(root / ".env")
    with open(cfg_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    local = root / "config.local.yaml"
    if local.exists():
        with open(local, encoding="utf-8") as fh:
            data = _deep_merge(data, yaml.safe_load(fh) or {})
    if overrides:
        data = _deep_merge(data, overrides)
    return Config(data, root)


def secret(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None
