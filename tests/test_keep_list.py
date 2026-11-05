"""Keep-list enforcement: every entry in keep_list.txt
must still exist - a named test as an attribute of its module, a
bare path as a file containing at least one test. A consolidation
PR that deletes or renames a listed test fails here, by design.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

TESTS_DIR = Path(__file__).parent


def _entries() -> list[str]:
    lines = (TESTS_DIR / "keep_list.txt").read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.startswith("#")
    ]


def test_keep_list_entries_exist() -> None:
    failures: list[str] = []
    for entry in _entries():
        if "::" not in entry:
            path = TESTS_DIR.parent / entry
            if not path.exists() or "def test_" not in path.read_text(
                encoding="utf-8"
            ):
                failures.append(f"{entry}: layer file missing or testless")
            continue
        rel, name = entry.split("::", 1)
        path = TESTS_DIR.parent / rel
        if not path.exists():
            failures.append(f"{entry}: file missing")
            continue
        spec = importlib.util.spec_from_file_location(path.stem, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - reported as a failure
            failures.append(f"{entry}: module import failed: {exc}")
            continue
        if not hasattr(module, name) and f"def {name}(" not in path.read_text(
            encoding="utf-8"
        ):
            failures.append(f"{entry}: test not found")
    assert not failures, "\n".join(failures)
