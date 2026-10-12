import re
import subprocess
import wave
from pathlib import Path

import pytest

from kir_clipper import db
from kir_clipper.config import load_config
from kir_clipper.highlights import CANDIDATE_SCHEMA

ROOT = Path(__file__).resolve().parents[1]


class FakeTranscriber:
    """Emits a word every 0.4s for the whole chunk; every 10th word ends a sentence."""

    def __init__(self):
        self.calls = 0

    def words(self, wav: Path) -> list[dict]:
        self.calls += 1
        with wave.open(str(wav)) as wf:
            dur = wf.getnframes() / wf.getframerate()
        out, t, n = [], 0.1, 0
        while t + 0.3 < dur:
            n += 1
            out.append({"w": f"word{n}" + ("." if n % 10 == 0 else ""), "s": round(t, 2), "e": round(t + 0.3, 2), "p": 1.0})
            t += 0.4
        return out


class FakeLLM:
    """Deterministic stand-in for Claude. Proposes 8-sentence clips (~32s) and keeps them in refinement."""

    def __init__(self, scores=(95, 80, 70, 50)):
        self.scores = list(scores)
        self.calls = []

    def __call__(self, system, user, schema, max_tokens=16000):
        self.calls.append(schema)
        props = schema.get("properties", {})
        if "cuts" in props:  # long-form plan for one transcript window
            idx = [int(m) for m in re.findall(r"^\[(\d+) ", user, re.M)]
            a = idx[0]
            return {"cuts": [{"start_idx": a + 2, "end_idx": a + 3, "category": "chat_reading", "confidence": 90,
                              "reason": "reading names"},
                             {"start_idx": a + 5, "end_idx": a + 5, "category": "distracted", "confidence": 40,
                              "reason": "unsure"}],
                    "chapters": [{"idx": a, "title": f"Topic {a}"}, {"idx": idx[len(idx) // 2], "title": "Second topic"}],
                    "lower_thirds": [{"idx": a + 1, "title": "Discipline", "subtitle": "Why motivation fades"}],
                    "popups": [{"idx": a + 4, "text": "Motivation is a feeling, discipline is a decision today"}],
                    "zooms": [{"idx": a + 6}]}
        if "concepts" in props:  # thumbnail concepts
            return {"concepts": [
                {"text": "STOP WAITING", "emphasis": "WAITING", "layout": "split", "expression": "", "accent": "arrow", "why": "a"},
                {"text": "BROKE TO $10K", "emphasis": "$10K", "layout": "spotlight", "expression": "", "accent": "money", "why": "b"},
                {"text": "YOU VS YOU", "emphasis": "VS", "layout": "versus", "expression": "", "accent": "check_x", "why": "c"},
            ]}
        if set(props) == {"title", "description", "tags"}:
            return {"title": "Full stream: discipline", "description": "We talk discipline.", "tags": ["#kir", "mindset"]}
        if schema is CANDIDATE_SCHEMA:
            idx = [int(m) for m in re.findall(r"^\[(\d+) ", user, re.M)]
            clips = []
            for k, start in enumerate(range(idx[0], idx[-1] - 8, 12)):
                clips.append({"start_idx": start, "end_idx": start + 7, "score": self.scores[k % len(self.scores)],
                              "hook": f"hook {start}", "reason": "test"})
            return {"clips": clips}
        m = re.search(r"Candidate: sentences (\d+) to (\d+), provisional score (\d+)", user)
        a, b, score = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else (0, 0, 75)
        n = len(self.calls)
        copy = {"title": f"title {n}", "caption": f"caption {n}"}
        return {"start_idx": a, "end_idx": b, "score": score, "hook": f"new hook {n}", "hook_overlay": f"overlay {n}",
                "reason": "test", "title": f"title {n}", "caption": f"caption {n}", "hashtags": ["#kir", "#mindset"],
                "tiktok": copy, "instagram": copy, "youtube": copy, "facebook": copy, "x": copy}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    overrides = {
        "paths": {k: str(tmp_path / k) for k in ("inbox", "processed", "failed", "data")},
        "render": {"width": 270, "height": 480, "fps": 15, "encoder": "libx264", "crf": 30},
        "watcher": {"stable_seconds": 0, "poll_seconds": 0.1},
        "review": {"dry_run": False},
        "publishing": {"provider": "dry_run"},
        "transcription": {"chunk_minutes": 1, "overlap_seconds": 5},
        "llm": {"window_minutes": 1.5, "window_overlap_minutes": 0.25},
        "brand": {"logo": str(tmp_path / "nologo.png")},
    }
    return load_config(ROOT / "config.yaml", overrides)


@pytest.fixture
def conn(cfg):
    return db.connect(cfg.db_path)


def make_video(path: Path, seconds: int = 150, color: str = "testsrc2") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{color}=s=640x360:r=15:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=330:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    return make_video(tmp_path_factory.mktemp("media") / "stream.mp4")
