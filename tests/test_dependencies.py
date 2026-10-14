"""Every module-level third-party import must be a core dependency in pyproject.toml.

Optional packages (faster_whisper, cv2, rembg, ...) are imported inside functions, so they are skipped.
"""
import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST_NAME = {"PIL": "pillow", "yaml": "pyyaml", "dotenv": "python-dotenv"}


def _top_level_imports(path: Path) -> set[str]:
    names = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_module_level_imports_are_declared():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = {re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0].lower() for d in project["dependencies"]}
    missing = {}
    for py in (ROOT / "kir_clipper").rglob("*.py"):
        for mod in _top_level_imports(py):
            if mod in sys.stdlib_module_names or mod == "kir_clipper":
                continue
            if DIST_NAME.get(mod, mod).lower() not in declared:
                missing.setdefault(mod, []).append(py.relative_to(ROOT).as_posix())
    assert not missing, f"imported at module level but not in pyproject dependencies: {missing}"
