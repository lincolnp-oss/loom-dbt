from __future__ import annotations

import shutil
from pathlib import Path

from loom.cli import main
from loom.inventory import validate_project


ROOT = Path(__file__).parents[1]


def test_first_project_inventory_is_valid_and_coherent() -> None:
    entries = validate_project(ROOT / "examples" / "first-project")

    assert {(entry.data["name"], entry.data.get("generator")) for entry in entries} == {
        ("stg_customers", "staging"),
        ("stg_orders", "staging"),
        ("hub_customer", "hub"),
        ("customer_order_counts", None),
    }
    staging = [
        entry.data for entry in entries if entry.data.get("generator") == "staging"
    ]
    assert len(staging) == 2
    assert all(
        data["source_ref"]["name"] == "raw" and "inline_source" not in data
        for data in staging
    )


def _readme_block_after(readme: str, marker: str) -> str:
    """The contents of the first fenced block following the line holding marker."""
    lines = readme.splitlines()
    start = next(i for i, line in enumerate(lines) if marker in line)
    fence = next(i for i in range(start, len(lines)) if lines[i].startswith("```"))
    end = next(i for i in range(fence + 1, len(lines)) if lines[i].startswith("```"))
    return "\n".join(lines[fence + 1 : end])


def test_readme_example_is_the_captured_output(tmp_path: Path) -> None:
    # The README's 30-second example is a capture, and this test keeps it
    # one: the declaration block must equal the first project's hub
    # instance set as authored, and the SQL block must equal, byte for
    # byte, what generation writes for it today - including the final
    # newline (QOL A-8: generated files end with one, so the captured
    # block plus its line terminator IS the file). A fenced block joins
    # its lines with "\n" and cannot show the last line's terminator,
    # hence the explicit + "\n" rather than a stripped comparison.
    # If generation changes, re-capture the block; never edit it
    # by hand to match.
    project = tmp_path / "first-project"
    shutil.copytree(ROOT / "examples" / "first-project", project)
    assert main(["generate", "--project-dir", str(project), "--write"]) == 0

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert _readme_block_after(readme, "This declaration:") + "\n" == (
        project / "metadata" / "instances" / "hubs.yml"
    ).read_text(encoding="utf-8")
    assert _readme_block_after(readme, "generates this model:") + "\n" == (
        project / "models" / "hub_customer.sql"
    ).read_text(encoding="utf-8")
