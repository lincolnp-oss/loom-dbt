from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.manifest import read_manifest, validate_model_freshness
from metadata_helpers import write_model_manifest


def _manifest(project: Path) -> Path:
    (project / "dbt_project.yml").write_text("name: example\n", encoding="utf-8")
    return write_model_manifest(project, ["model"], "example")


def test_manifest_identity_and_freshness(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    _, document = read_manifest(tmp_path, path, sections=("nodes",))
    validate_model_freshness(tmp_path, path, document)
    document["metadata"]["project_name"] = "wrong"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(LoomError, match="does not match"):
        read_manifest(tmp_path, path, sections=("nodes",))
    document["metadata"]["project_name"] = "example"
    document["metadata"]["generated_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(document), encoding="utf-8")
    # Reading never refuses over staleness (QOL M4): the
    # freshness verdict belongs to the guarded walk, so a
    # tolerant caller (search) can read and label the age.
    _, document = read_manifest(tmp_path, path, sections=("nodes",))
    with pytest.raises(LoomError, match="stale"):
        validate_model_freshness(tmp_path, path, document)


def test_project_file_freshness_is_a_guarded_walk_fact(tmp_path: Path) -> None:
    # M4 both directions: a dbt_project.yml edit after parsing
    # does not refuse the read, and the guarded walk refuses
    # naming the project file alongside any other stale file.
    path = _manifest(tmp_path)
    _, document = read_manifest(tmp_path, path, sections=("nodes",))
    validate_model_freshness(tmp_path, path, document)
    generated_at = datetime.fromisoformat(document["metadata"]["generated_at"])
    future = generated_at.timestamp() + 60
    project_file = tmp_path / "dbt_project.yml"
    os.utime(project_file, (future, future))
    _, document = read_manifest(tmp_path, path, sections=("nodes",))
    with pytest.raises(LoomError) as excinfo:
        validate_model_freshness(tmp_path, path, document)
    assert "dbt_project.yml (changed after parsing)" in str(excinfo.value)


@pytest.mark.parametrize("change", ["added", "removed", "modified"])
def test_manifest_rejects_changed_model_tree(tmp_path: Path, change: str) -> None:
    path = _manifest(tmp_path)
    _, document = read_manifest(tmp_path, path, sections=("nodes",))
    source = tmp_path / "models" / "model.sql"
    if change == "added":
        (source.parent / "new.sql").write_text("select 2\n", encoding="utf-8")
    elif change == "removed":
        source.unlink()
    else:
        source.write_text("select 2\n", encoding="utf-8")
        os.utime(source, (1, 1))
    with pytest.raises(LoomError, match="stale"):
        validate_model_freshness(tmp_path, path, document)


def test_disabled_models_are_not_false_positive_additions(tmp_path: Path) -> None:
    path = _manifest(tmp_path)
    source = tmp_path / "models" / "disabled.sql"
    source.write_text("select 3\n", encoding="utf-8")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["disabled"] = {
        "model.example.disabled": [
            {
                "resource_type": "model",
                "package_name": "example",
                "original_file_path": "models/disabled.sql",
            }
        ]
    }
    document["metadata"]["generated_at"] = datetime.now(timezone.utc).isoformat()
    validate_model_freshness(tmp_path, path, document)


def test_read_manifest_missing_file_names_file_and_remedy(
    tmp_path: Path,
) -> None:
    # The missing-manifest error once read
    # ".../target/manifest.json: cannot read dbt manifest: [Errno 2]
    # No such file or directory: '.../target'" - a raw OS string
    # naming the directory, with no remedy. The error must name the
    # manifest file itself, carry the remedy, and leak no Errno.
    missing = tmp_path / "target" / "manifest.json"
    with pytest.raises(LoomError) as excinfo:
        read_manifest(tmp_path, missing, sections=("nodes",))
    message = str(excinfo.value)
    assert str(missing) in message
    assert "dbt manifest not found" in message
    # The remedy steers to `dbt parse` - the assessment
    # consumes a parse-produced manifest (fit calibration
    # assumes it); `dbt build` stays fine for other purposes.
    assert "run `dbt parse` in the project first" in message
    assert "(or supply a manifest path)" in message
    assert "Errno" not in message


@pytest.mark.parametrize("change", ["added", "removed", "modified", "touched"])
def test_stale_manifest_errors_name_file_and_remedy(
    tmp_path: Path, change: str
) -> None:
    # The stale-manifest error named
    # the problem and the stale file but not the remedy, asymmetric
    # with the not-found error above. Every freshness raise
    # now carries the refresh command while keeping the file named.
    path = _manifest(tmp_path)
    _, document = read_manifest(tmp_path, path, sections=("nodes",))
    source = tmp_path / "models" / "model.sql"
    named = "model.sql"
    if change == "added":
        (source.parent / "new.sql").write_text("select 2\n", encoding="utf-8")
        named = "new.sql"
    elif change == "removed":
        source.unlink()
    elif change == "modified":
        source.write_text("select 2\n", encoding="utf-8")
        os.utime(source, (1, 1))
    else:  # touched: same content, newer mtime than the manifest
        generated_at = datetime.fromisoformat(document["metadata"]["generated_at"])
        future = generated_at.timestamp() + 60
        os.utime(source, (future, future))
    with pytest.raises(LoomError) as excinfo:
        validate_model_freshness(tmp_path, path, document)
    message = str(excinfo.value)
    assert "dbt manifest is stale" in message
    assert named in message
    assert "re-run `dbt parse` to refresh the manifest" in message



class TestStep2ManifestSchemaGating:
        def _manifest(self, project: Path, schema_version: str | None) -> Path:
            (project / "dbt_project.yml").write_text(
                "name: example\n", encoding="utf-8"
            )
            path = write_model_manifest(project, ["model"], "example")
            document = json.loads(path.read_text(encoding="utf-8"))
            if schema_version is None:
                document["metadata"].pop("dbt_schema_version", None)
            else:
                document["metadata"]["dbt_schema_version"] = schema_version
            path.write_text(json.dumps(document), encoding="utf-8")
            return path

        def test_supported_schema_v12_reads(self, tmp_path: Path) -> None:
            path = self._manifest(
                tmp_path, "https://schemas.getdbt.com/dbt/manifest/v12.json"
            )
            _, document = read_manifest(tmp_path, path, sections=("nodes",))
            assert document["metadata"]["project_name"] == "example"

        def test_unsupported_schema_v13_rejected(self, tmp_path: Path) -> None:
            path = self._manifest(
                tmp_path, "https://schemas.getdbt.com/dbt/manifest/v13.json"
            )
            with pytest.raises(LoomError, match="unsupported dbt manifest schema"):
                read_manifest(tmp_path, path, sections=("nodes",))

        def test_unrecognised_schema_value_rejected(self, tmp_path: Path) -> None:
            path = self._manifest(tmp_path, "not-a-schema-version")
            with pytest.raises(LoomError, match="not a recognised"):
                read_manifest(tmp_path, path, sections=("nodes",))

        def test_missing_schema_version_still_reads(self, tmp_path: Path) -> None:
            path = self._manifest(tmp_path, None)
            _, document = read_manifest(tmp_path, path, sections=("nodes",))
            assert document["metadata"]["project_name"] == "example"
