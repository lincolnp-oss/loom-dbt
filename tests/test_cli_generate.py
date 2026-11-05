from __future__ import annotations

from pathlib import Path

import pytest

from loom.cli import main
from loom import __version__
from loom.dialects import Dialect, get_dialect
from loom.inventory import InventoryEntry


def test_generate_returns_nonzero_when_a_model_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        InventoryEntry(
            "metadata/instances/broken.yml",
            {"name": "broken_model", "generator": "bronze"},
        ),
        InventoryEntry(
            "metadata/instances/working.yml",
            {"name": "working_model", "generator": "silver"},
        ),
    ]
    monkeypatch.setattr("loom.cli.validate_project", lambda _: entries)

    class Renderer:
        dialect = get_dialect("duckdb")

        def __init__(self, generator: str) -> None:
            self.generator = generator

        def render(
            self, params: dict[str, object], dialect: Dialect, output_path: Path | None
        ) -> str:
            if self.generator == "bronze":
                raise ValueError("renderer rejected input")
            return "select 1"

        def render_tests(
            self, params: dict[str, object], dialect: Dialect, output_path: Path | None
        ) -> None:
            return None

    monkeypatch.setattr(
        "loom.cli.get_renderer",
        lambda generator, dialect: Renderer(generator),
    )

    result = main(["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
    output = capsys.readouterr()

    assert result == 1
    assert (
        "failed to generate broken_model (bronze): renderer rejected input"
        in output.err
    )
    assert "Generation incomplete" in output.err
    assert "Wrote: 1 models" not in output.out
    assert (tmp_path / "models" / "working_model.sql").read_text() == "select 1"


def test_generate_reports_invalid_profile_without_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "dbt_project.yml").write_text(
        "profile: quickstart\n",
        encoding="utf-8",
    )
    (tmp_path / "profiles.yml").write_text(
        "quickstart:\n  target: missing\n  outputs: {}\n",
        encoding="utf-8",
    )

    assert main(["generate", "--write", "--project-dir", str(tmp_path)]) == 1
    assert "loom: target 'missing' is not defined in profile 'quickstart'" in (
        capsys.readouterr().err
    )


def test_generate_returns_nonzero_for_invalid_generated_test_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        InventoryEntry(
            "metadata/instances/working.yml",
            {"name": "working_model", "generator": "silver"},
        )
    ]
    monkeypatch.setattr("loom.cli.validate_project", lambda _: entries)

    class Renderer:
        dialect = get_dialect("duckdb")

        def render(
            self, params: dict[str, object], dialect: Dialect, output_path: Path | None
        ) -> str:
            return "select 1"

        def render_tests(
            self, params: dict[str, object], dialect: Dialect, output_path: Path | None
        ) -> str:
            return "[invalid"

    monkeypatch.setattr("loom.cli.get_renderer", lambda generator, dialect: Renderer())

    result = main(["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
    output = capsys.readouterr()

    assert result == 1
    assert (
        "failed to generate working_model (silver): renderer produced invalid "
        "data-test YAML"
    ) in output.err
    assert "Generation incomplete" in output.err
    assert not (tmp_path / "models" / "_generated/tests" / "data_working_model.yml").exists()


def test_generate_preview_classifies_files_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch(monkeypatch, _entries(("working_model", "bronze")))

    assert (
        main(
            [
                "generate",
                "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--verbose",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    # The default mode is a preview: it states the mode, counts
    # the planned files by their state against disk, names each
    # file under --verbose, and ends with the apply hint.
    assert "dry run - nothing written" in output
    assert "Would write: 1 file(s)" in output
    assert "Would change: 0 file(s)" in output
    assert "Unchanged: 0 file(s)" in output
    assert "would write: models/working_model.sql" in output
    assert "Re-run with --write to apply: loom generate --write" in output
    assert not (tmp_path / "models").exists()

    # After a write run the same preview reports the file as
    # unchanged; a hand edit (header kept) makes it would-change.
    assert (
        main(
            [
                "generate",
                "--write",
                "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(["generate", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
        == 0
    )
    output = capsys.readouterr().out
    assert "Unchanged: 1 file(s)" in output
    target = tmp_path / "models" / "working_model.sql"
    target.write_text("-- Generated by Loom\nselect 2\n", encoding="utf-8")
    assert (
        main(["generate", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
        == 0
    )
    output = capsys.readouterr().out
    assert "Would change: 1 file(s)" in output


def test_generate_write_summary_matches_preview_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # QOL FR symmetry: the write summary used to restate the
    # in-scope totals ("Wrote: 1 models") even when the preview
    # had classified the only file as would-change. The write
    # headline now reports the same classification the preview
    # forecast: written / changed / unchanged file counts.
    _patch(monkeypatch, _entries(("working_model", "bronze")))
    assert (
        main(["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
        == 0
    )
    first = capsys.readouterr().out
    assert "Wrote: 1 file(s) · Changed: 0 file(s) · Unchanged: 0 file(s)" in first

    target = tmp_path / "models" / "working_model.sql"
    rendered = target.read_text(encoding="utf-8")
    target.write_text("-- Generated by Loom\nselect 2\n", encoding="utf-8")
    assert (
        main(["generate", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
        == 0
    )
    preview = capsys.readouterr().out
    assert "Would write: 0 file(s)" in preview
    assert "Would change: 1 file(s)" in preview

    assert (
        main(["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
        == 0
    )
    written = capsys.readouterr().out
    assert "Wrote: 0 file(s) · Changed: 1 file(s) · Unchanged: 0 file(s)" in written
    assert target.read_text(encoding="utf-8") == rendered

    # JSON carries the same counts under file_changes.
    assert (
        main(
            [
                "generate",
                "--write",
                "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--format",
                "json",
            ]
        )
        == 0
    )
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["file_changes"] == {
        "written": 0,
        "changed": 0,
        "unchanged": 1,
    }


def _write_scd2_project(project: Path) -> None:
    """A minimal project whose only create-only outputs are an
    SCD2 dimension's snapshot + current view (fixture)."""
    (project / "models").mkdir(parents=True)
    (project / "metadata" / "patterns").mkdir(parents=True)
    (project / "metadata" / "instances").mkdir()
    (project / "dbt_project.yml").write_text(
        "name: scd2_headline_test\n"
        "version: '1.0.0'\n"
        "config-version: 2\n"
        "profile: scd2_headline_test\n",
        encoding="utf-8",
    )
    (project / "profiles.yml").write_text(
        "scd2_headline_test:\n"
        "  target: local\n"
        "  outputs:\n"
        "    local:\n"
        "      type: duckdb\n"
        "      path: test.duckdb\n"
        "      schema: main\n",
        encoding="utf-8",
    )
    (project / "models" / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    schema: main\n"
        "    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    patterns = {
        "staging": (
            "generator: staging\n"
            "required_params: [description, source_ref, columns]\n"
        ),
        "scd2": (
            "generator: scd2_dimension\n"
            "required_params: [description, input_models, business_key, "
            "tracked_columns]\n"
        ),
    }
    for name, body in patterns.items():
        (project / "metadata" / "patterns" / f"{name}.yml").write_text(
            f"name: {name}\n"
            "defaults: {owner: test}\n"
            f"{body}",
            encoding="utf-8",
        )
    instances = {
        "staging": (
            "pattern: staging\n"
            "instances:\n"
            "  - name: stg_customers\n"
            "    params:\n"
            "      description: Customer staging.\n"
            "      source_ref: {name: raw, table: customers}\n"
            "      columns:\n"
            "        - {source: id, alias: customer_id}\n"
            "        - {source: name, alias: customer_name}\n"
        ),
        "scd2": (
            "pattern: scd2\n"
            "instances:\n"
            "  - name: dim_customers_scd2\n"
            "    params:\n"
            "      description: Customer history.\n"
            "      input_models: [stg_customers]\n"
            "      business_key: customer_id\n"
            "      tracked_columns: [customer_name]\n"
        ),
    }
    for name, body in instances.items():
        (project / "metadata" / "instances" / f"{name}.yml").write_text(
            f"{body}",
            encoding="utf-8",
        )


def test_generate_headline_counts_create_only_files_already_current(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Create-only SCD2 files already on disk drop
    # out of the written count, so the old headline fell when models
    # were added elsewhere (23 -> 22 on a re-run). The
    # headline now states the already-current create-only count.
    # The headline is the counts line under the shared
    # grammar ("Generate: <project> (dialect <d>)" / "Wrote:...");
    # since the QOL FR fix the Wrote line carries the file-change
    # counts (the preview's classification) and the create-only
    # split rides in the scope line's models segment.
    _write_scd2_project(tmp_path)

    assert (
        main(["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
        == 0
    )
    first = capsys.readouterr().out
    assert "Changed: 0 file(s)" in first
    assert "Scope: 3 models in scope" in first
    assert "(dialect duckdb)" in first
    assert "already current" not in first

    assert (
        main(["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
        == 0
    )
    second = capsys.readouterr().out
    assert (
        "Scope: 1 models in scope (2 create-only file(s) already current)"
        in second
    )
    # The re-run changed nothing: the Wrote line says so, in the
    # same counts the preview forecast.
    assert "Wrote: 0 file(s) · Changed: 0 file(s)" in second



class _Renderer:
    dialect = get_dialect("duckdb")

    def render(
        self, params: dict[str, object], dialect: Dialect, output_path: Path | None
    ) -> str:
        return "-- Generated by Loom\nselect 1\n"

    def render_tests(
        self, params: dict[str, object], dialect: Dialect, output_path: Path | None
    ) -> None:
        return None

def _entries(*specs: tuple[str, str]) -> list[InventoryEntry]:
    return [
        InventoryEntry(
            f"metadata/instances/{name}.yml", {"name": name, "generator": gen}
        )
        for name, gen in specs
    ]

def _patch(monkeypatch: pytest.MonkeyPatch, entries: list[InventoryEntry]) -> None:
    monkeypatch.setattr("loom.cli.validate_project", lambda _: entries)
    monkeypatch.setattr(
        "loom.cli.get_renderer", lambda generator, dialect: _Renderer()
    )

def test_generate_refuses_to_overwrite_practitioner_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _entries(("mine", "silver"), ("other", "silver"))
    _patch(monkeypatch, entries)
    models = tmp_path / "models"
    models.mkdir()
    handwritten = models / "mine.sql"
    handwritten.write_text("select 'hand written'\n", encoding="utf-8")

    result = main(
        ["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "duckdb"]
    )

    assert result == 1
    err = capsys.readouterr().err
    assert "refusing to overwrite" in err
    assert "nothing was written" in err
    assert handwritten.read_text(encoding="utf-8") == "select 'hand written'\n"
    # The preflight aborts before writing anything at all.
    assert not (models / "other.sql").exists()

def test_generate_force_overwrites_practitioner_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, _entries(("mine", "silver")))
    models = tmp_path / "models"
    models.mkdir()
    target = models / "mine.sql"
    target.write_text("select 'hand written'\n", encoding="utf-8")

    assert (
        main(
            [
                "generate",
                "--write", "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--force",
            ]
        )
        == 0
    )
    assert target.read_text(encoding="utf-8") == "-- Generated by Loom\nselect 1\n"

def test_generate_regenerates_loom_owned_and_identical_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch, _entries(("owned", "silver"), ("same", "silver")))
    models = tmp_path / "models"
    models.mkdir()
    owned = models / "owned.sql"
    owned.write_text("-- Generated by Loom\nselect 'old'\n", encoding="utf-8")
    identical = models / "same.sql"
    identical.write_text("-- Generated by Loom\nselect 1\n", encoding="utf-8")

    assert (
        main(["generate", "--write", "--project-dir", str(tmp_path), "--dialect", "duckdb"])
        == 0
    )
    assert owned.read_text(encoding="utf-8") == "-- Generated by Loom\nselect 1\n"
    assert identical.read_text(encoding="utf-8") == "-- Generated by Loom\nselect 1\n"

def test_generate_select_by_name_and_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _entries(("alpha", "silver"), ("beta", "bronze"), ("gamma", "silver"))
    _patch(monkeypatch, entries)

    assert (
        main(
            [
                "generate",
                "--write", "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--select",
                "alpha",
            ]
        )
        == 0
    )
    models = tmp_path / "models"
    assert (models / "alpha.sql").exists()
    assert not (models / "beta.sql").exists()
    assert not (models / "gamma.sql").exists()

    # Repeated --select flags union (the dbt reading): the
    # bronze models plus gamma.
    assert (
        main(
            [
                "generate",
                "--write", "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--select",
                "generator:bronze",
                "--select",
                "gamma",
            ]
        )
        == 0
    )
    assert (models / "beta.sql").exists()
    assert (models / "gamma.sql").exists()


def test_generate_select_comma_intersects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The comma flip (plan 7.3 item 5): within one --select,
    # comma-separated criteria intersect, where generate's old
    # ad hoc parsing unioned them. `generator:silver,gamma`
    # selects only gamma; `generator:bronze,gamma` selects
    # nothing and is an unmatched-selector error.
    entries = _entries(("alpha", "silver"), ("beta", "bronze"), ("gamma", "silver"))
    _patch(monkeypatch, entries)

    assert (
        main(
            [
                "generate",
                "--write", "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--select",
                "generator:silver,gamma",
            ]
        )
        == 0
    )
    models = tmp_path / "models"
    assert (models / "gamma.sql").exists()
    assert not (models / "alpha.sql").exists()
    assert not (models / "beta.sql").exists()

    assert (
        main(
            [
                "generate",
                "--write", "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--select",
                "generator:bronze,gamma",
            ]
        )
        == 1
    )

def test_generate_exclude_subtracts_from_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = _entries(("alpha", "silver"), ("beta", "bronze"), ("gamma", "silver"))
    _patch(monkeypatch, entries)

    assert (
        main(
            [
                "generate",
                "--write", "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--exclude",
                "generator:silver",
            ]
        )
        == 0
    )
    models = tmp_path / "models"
    assert (models / "beta.sql").exists()
    assert not (models / "alpha.sql").exists()
    assert not (models / "gamma.sql").exists()


def test_generate_select_unknown_token_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch(monkeypatch, _entries(("alpha", "silver")))

    assert (
        main(
            [
                "generate",
                "--write", "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--select",
                "nope",
            ]
        )
        == 1
    )
    assert "matched no generated models" in capsys.readouterr().err
    assert not (tmp_path / "models").exists()

def test_generate_preview_respects_select(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch(monkeypatch, _entries(("alpha", "silver"), ("beta", "bronze")))

    assert (
        main(
            [
                "generate",
                "--project-dir",
                str(tmp_path),
                "--dialect",
                "duckdb",
                "--select",
                "beta",
                "--verbose",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "would write: models/beta.sql" in out
    assert "alpha" not in out
    assert "Would write: 1 file(s)" in out

def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"loom {__version__}"


def test_version_comes_from_the_loom_dbt_distribution() -> None:
    # Loom.__version__ reads the loom-dbt distribution's
    # metadata (the distribution was renamed from loom; the old
    # lookup silently fell back to "unknown").
    from importlib.metadata import PackageNotFoundError, version

    try:
        expected = version("loom-dbt")
    except PackageNotFoundError:
        pytest.skip("loom-dbt distribution is not installed")
    assert __version__ == expected
    assert __version__ != "unknown"
