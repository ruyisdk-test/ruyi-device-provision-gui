from __future__ import annotations

import pathlib
import tomllib


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_ruyi_dependency_uses_registry_source() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    dependencies = pyproject["project"]["dependencies"]
    assert "ruyi>=0.51.0a20260616,<0.53" in dependencies
    assert "ruyi" not in pyproject.get("tool", {}).get("uv", {}).get("sources", {})

    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text())
    ruyi = next(package for package in lock["package"] if package["name"] == "ruyi")
    assert ruyi["source"] == {"registry": "https://pypi.org/simple"}


def test_lock_file_has_no_machine_local_ruyi_path() -> None:
    lock_text = (PROJECT_ROOT / "uv.lock").read_text()
    assert "../ruyi" not in lock_text
    assert "/home/" not in lock_text
