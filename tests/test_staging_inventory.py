from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from loom.dialects import DIALECTS
from loom.errors import LoomError
from loom.inventory import check_inventory_coverage, search_inventory, validate_project
from loom.inventory_relationships import staging_macro_warnings
from loom.renderers import get_renderer
from loom.renderers import StagingRenderer
from metadata_helpers import write_model_manifest, write_pattern_instance


def _staging_instance(
    project: Path,
    *,
    name: str = "stg_customers",
    columns: list[dict[str, str]] | None = None,
    source_ref: dict[str, str] | None = None,
    inline_source: dict[str, str] | None = None,
) -> Path:
    params: dict[str, object] = {
        "description": "Clean customer source",
        "owner": "analytics",
        "columns": columns
        if columns is not None
        else [
            {"source": "raw_id", "alias": "customer_id"},
            {"source": "raw_email", "alias": "email"},
        ],
    }
    if source_ref is not None:
        params["source_ref"] = source_ref
    if inline_source is not None:
        params["inline_source"] = inline_source
    return write_pattern_instance(
        project, name, "staging", params, pattern="raw_staging"
    )


def _native_source(
    project: Path,
    relative_path: str = "models/sources.yml",
    *,
    source_name: str = "ecom",
    table_name: str = "raw_customers",
    columns: list[str] | None = None,
) -> Path:
    path = project / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    column_yaml = ""
    if columns is not None:
        column_yaml = "        columns:\n" + "".join(
            f"          - name: {column}\n" for column in columns
        )
    path.write_text(
        "version: 2\n"
        "sources:\n"
        f"  - name: {source_name}\n"
        "    tables:\n"
        f"      - name: {table_name}\n"
        f"{column_yaml}",
        encoding="utf-8",
    )
    return path


def test_source_ref_uses_native_sources_under_configured_model_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "dbt_project.yml").write_text(
        "name: sample\nmodel-paths: [dbt_models]\n", encoding="utf-8"
    )
    _native_source(
        tmp_path,
        "dbt_models/staging/sources.yaml",
        columns=["raw_id", "raw_email"],
    )
    _staging_instance(
        tmp_path,
        source_ref={"name": "ecom", "table": "raw_customers"},
    )

    entries = validate_project(tmp_path)

    assert entries[0].data["name"] == "stg_customers"


def test_source_ref_does_not_require_native_column_declarations(
    tmp_path: Path,
) -> None:
    _native_source(tmp_path)
    _staging_instance(
        tmp_path,
        source_ref={"name": "ecom", "table": "raw_customers"},
    )

    assert validate_project(tmp_path)[0].data["name"] == "stg_customers"


def test_coverage_validates_staging_fields_with_staging_schema(tmp_path: Path) -> None:
    _native_source(tmp_path, columns=["raw_id", "raw_email"])
    _staging_instance(
        tmp_path,
        source_ref={"name": "ecom", "table": "raw_customers"},
    )
    manifest = write_model_manifest(tmp_path, ["stg_customers"])
    # A generated staging model lives under models/_generated/staging/
    # (the staging spec's last output root); place the fixture file
    # where generation would put it so the misplaced-output
    # check is not incidental to this schema-validation test.
    staged = tmp_path / "models" / "_generated/staging" / "stg_customers.sql"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_text("select 1\n", encoding="utf-8")
    (tmp_path / "models" / "stg_customers.sql").unlink()
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["nodes"]["model.sample.stg_customers"]["original_file_path"] = (
        "models/_generated/staging/stg_customers.sql"
    )
    # The staged file was written after the helper generated the
    # manifest; refresh the timestamp so freshness checks pass.
    document["metadata"]["generated_at"] = datetime.now(timezone.utc).isoformat()
    manifest.write_text(json.dumps(document), encoding="utf-8")

    coverage = check_inventory_coverage(tmp_path, manifest)

    assert coverage.ok


@pytest.mark.parametrize(
    ("native_files", "declared_columns", "message"),
    [
        ([], ["raw_id", "raw_email"], "was not found"),
        (
            ["models/first.yml", "models/second.yml"],
            ["raw_id", "raw_email"],
            "is ambiguous",
        ),
        (["models/sources.yml"], ["raw_id"], "mapped raw column"),
    ],
)
def test_source_ref_must_resolve_unambiguously_with_declared_columns(
    tmp_path: Path,
    native_files: list[str],
    declared_columns: list[str],
    message: str,
) -> None:
    for native_file in native_files:
        _native_source(
            tmp_path,
            native_file,
            columns=(
                declared_columns if native_files == ["models/sources.yml"] else None
            ),
        )
    _staging_instance(
        tmp_path,
        source_ref={"name": "ecom", "table": "raw_customers"},
    )

    with pytest.raises(LoomError, match=message):
        validate_project(tmp_path)


def test_source_ref_without_table_fails_validation_naming_the_key(
    tmp_path: Path,
) -> None:
    # Pi-review P2 acceptance: the session's third iteration -
    # source_ref carrying only a name - must fail loom validate
    # naming the missing key, not generate source('ecom',
    # 'unknown') and fail at dbt compile.
    _staging_instance(tmp_path, source_ref={"name": "ecom"})
    with pytest.raises(LoomError, match=r"source_ref\.table is a required property"):
        validate_project(tmp_path)


def test_source_ref_without_name_fails_validation_naming_the_key(
    tmp_path: Path,
) -> None:
    _staging_instance(tmp_path, source_ref={"table": "raw_customers"})
    with pytest.raises(LoomError, match=r"source_ref\.name is a required property"):
        validate_project(tmp_path)


def test_inline_source_without_table_fails_validation_naming_the_key(
    tmp_path: Path,
) -> None:
    _staging_instance(tmp_path, inline_source={"name": "ecom"})
    with pytest.raises(
        LoomError, match=r"inline_source\.table is a required property"
    ):
        validate_project(tmp_path)


def test_source_ref_with_empty_table_fails_validation(tmp_path: Path) -> None:
    _staging_instance(tmp_path, source_ref={"name": "ecom", "table": "  "})
    with pytest.raises(
        LoomError, match=r"source_ref\.table must be a non-empty string"
    ):
        validate_project(tmp_path)


def test_staging_renderer_refuses_a_table_less_source_mapping() -> None:
    # The renderer's 'unknown' fallbacks are gone (P2): an
    # unvalidated path fails in code, so no generated SQL can
    # carry 'unknown' as a table name.
    renderer = StagingRenderer(DIALECTS["duckdb"])
    columns = [{"source": "raw_id", "alias": "customer_id"}]
    for params in (
        {"source_ref": {"name": "ecom"}},
        {"source_ref": {"table": "raw_customers"}},
        {"inline_source": {"name": "ecom"}},
        {"inline_source": {"table": "raw_customers"}},
    ):
        with pytest.raises(ValueError, match="requires .* 'name' and 'table'"):
            renderer.render(
                {"name": "stg_customers", "columns": columns, **params},
                DIALECTS["duckdb"],
                None,
            )


def test_inline_source_rejects_any_authored_source_name_collision(
    tmp_path: Path,
) -> None:
    _native_source(tmp_path, table_name="raw_orders")
    _staging_instance(
        tmp_path,
        inline_source={
            "name": "ecom",
            "table": "raw_customers",
            "schema": "raw",
            "database": "warehouse",
        },
    )

    with pytest.raises(LoomError, match="collides with authored dbt source"):
        validate_project(tmp_path)


def test_inline_source_ignores_loom_generated_source_yaml(tmp_path: Path) -> None:
    generated = tmp_path / "models" / "_generated/staging" / "stg_existing.yml"
    generated.parent.mkdir(parents=True)
    generated.write_text(
        "# GENERATED FILE. Source: "
        "metadata/instances/raw_staging.yml#instances[stg_existing]\n"
        "version: 2\n"
        "sources:\n"
        "  - name: ecom\n"
        "    tables:\n"
        "      - name: raw_customers\n",
        encoding="utf-8",
    )
    _staging_instance(
        tmp_path,
        inline_source={
            "name": "ecom",
            "table": "raw_customers",
            "schema": "raw",
        },
    )

    assert validate_project(tmp_path)[0].data["name"] == "stg_customers"


@pytest.mark.parametrize(
    ("columns", "message"),
    [
        (
            [
                {"source": "raw_id", "alias": "customer_id"},
                {"source": "RAW_ID", "alias": "email"},
            ],
            "duplicate source identifier",
        ),
        (
            [
                {"source": "raw_id", "alias": "customer_id"},
                {"source": "raw_email", "alias": "CUSTOMER_ID"},
            ],
            "duplicate alias identifier",
        ),
    ],
)
def test_staging_mappings_reject_case_insensitive_duplicates(
    tmp_path: Path, columns: list[dict[str, str]], message: str
) -> None:
    _staging_instance(
        tmp_path,
        columns=columns,
        inline_source={"name": "ecom", "table": "raw_customers", "schema": "raw"},
    )

    with pytest.raises(LoomError, match=message):
        validate_project(tmp_path)


def test_staging_mappings_require_safe_identifiers(tmp_path: Path) -> None:
    _staging_instance(
        tmp_path,
        columns=[{"source": "raw_id; select 1", "alias": "customer_id"}],
        inline_source={"name": "ecom", "table": "raw_customers", "schema": "raw"},
    )

    with pytest.raises(LoomError, match=r"columns\[0\]\.source"):
        validate_project(tmp_path)


def test_staging_sentinel_source_is_an_unresolved_placeholder(
    tmp_path: Path,
) -> None:
    # A conversion draft's `__TODO__` placeholder must never
    # survive promotion: validate rejects it by name.
    _staging_instance(
        tmp_path,
        columns=[{"source": "__TODO__", "alias": "customer_id"}],
        inline_source={"name": "ecom", "table": "raw_customers", "schema": "raw"},
    )

    with pytest.raises(LoomError, match="unresolved placeholder '__TODO__'"):
        validate_project(tmp_path)


def test_staging_sentinel_alias_is_an_unresolved_placeholder(
    tmp_path: Path,
) -> None:
    _staging_instance(
        tmp_path,
        columns=[{"source": "raw_id", "alias": "__TODO__"}],
        inline_source={"name": "ecom", "table": "raw_customers", "schema": "raw"},
    )

    with pytest.raises(LoomError, match="unresolved placeholder '__TODO__'"):
        validate_project(tmp_path)


def test_staging_repeated_sentinels_are_placeholders_not_duplicates(
    tmp_path: Path,
) -> None:
    # Several drafted placeholders in one instance are reported as
    # several placeholders - the duplicate-`from` error must not
    # fire between sentinels.
    _staging_instance(
        tmp_path,
        columns=[
            {"source": "__TODO__", "alias": "customer_id"},
            {"source": "__TODO__", "alias": "customer_name"},
        ],
        inline_source={"name": "ecom", "table": "raw_customers", "schema": "raw"},
    )

    with pytest.raises(LoomError) as excinfo:
        validate_project(tmp_path)
    message = str(excinfo.value)
    assert message.count("unresolved placeholder '__TODO__'") == 2
    assert "duplicate source" not in message
    assert "duplicate alias" not in message


def test_scd2_fields_must_be_generated_by_referenced_staging_aliases(
    tmp_path: Path,
) -> None:
    _staging_instance(
        tmp_path,
        columns=[{"source": "raw_id", "alias": "customer_id"}],
        inline_source={"name": "ecom", "table": "raw_customers", "schema": "raw"},
    )
    write_pattern_instance(
        tmp_path,
        "dim_customers",
        "scd2_dimension",
        {
            "description": "Customer history",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "tracked_columns": ["email"],
        },
        pattern="history_dimension",
    )

    with pytest.raises(LoomError, match="required SCD2 column alias"):
        validate_project(tmp_path)


def test_staging_registration_remains_descriptive(tmp_path: Path) -> None:
    registration = tmp_path / "local" / "registrations" / "stg_customers.yml"
    registration.parent.mkdir(parents=True)
    registration.write_text(
        "name: stg_customers\n"
        "classification: staging\n"
        "description: Local source model\n"
        "owner: analytics\n"
        "upstream: raw_customers\n",
        encoding="utf-8",
    )

    assert validate_project(tmp_path)[0].data["name"] == "stg_customers"


@pytest.mark.parametrize(
    ("name", "source_fields", "message"),
    [
        (
            "stg-customers",
            {"source_ref": {"name": "ecom", "table": "raw_customers"}},
            "name",
        ),
        (
            "stg_customers",
            {
                "source_ref": {"name": "ecom", "table": "raw_customers"},
                "inline_source": {
                    "name": "ecom",
                    "table": "raw_customers",
                    "schema": "raw",
                },
            },
            "exactly one of source_ref or inline_source",
        ),
        (
            "stg_customers",
            {},
            "exactly one of source_ref or inline_source",
        ),
    ],
)
def test_staging_requires_valid_name_and_exactly_one_source_mode(
    tmp_path: Path,
    name: str,
    source_fields: dict[str, dict[str, str]],
    message: str,
) -> None:
    _staging_instance(tmp_path, name=name, **source_fields)

    with pytest.raises(LoomError, match=message):
        validate_project(tmp_path)


def test_staging_mapping_and_source_search(tmp_path: Path) -> None:
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            "description": "Customer",
            "owner": "analytics",
            "inline_source": {"name": "ecom", "table": "raw_customer", "schema": "raw"},
            "columns": [{"source": "raw_id", "alias": "customer_id"}],
        },
        pattern="staging",
    )
    for arguments in (
        {"column": "CUSTOMER_ID"},
        {"column": "raw_id"},
        {"text": "raw_customer"},
        {"dependency": "ecom.raw_customer"},
    ):
        assert [
            entry.data["name"] for entry in search_inventory(tmp_path, **arguments)
        ] == ["stg_customer"]


def test_staging_source_ref_without_columns_selects_star() -> None:
    # Kept deliberately: the SELECT * fallback
    # for a staging instance with no declared columns is a renderer
    # branch no canonical instance exercises (all canonical staging
    # instances declare columns), so deleting the renderers-file
    # pin dropped it from coverage.
    renderer = get_renderer("staging", "duckdb")
    sql = renderer.render(
        {
            "name": "stg_customers",
            "source_ref": {"name": "raw", "table": "customers"},
        },
        DIALECTS["duckdb"],
        None,
    )
    assert "SELECT *" in sql
    assert "source('raw', 'customers')" in sql



class TestStep2StagingDegenerateGuards:
        def test_staging_empty_columns_rejected(self, tmp_path: Path) -> None:
            write_pattern_instance(
                tmp_path,
                "stg_customers",
                "staging",
                {
                    "description": "Staging",
                    "owner": "analytics",
                    "inline_source": {"name": "raw", "table": "customers"},
                    "columns": [],
                },
                pattern="staging",
            )
            message = _validation_error(tmp_path)
            assert "'columns' must be a non-empty list" in message

        def test_staging_explicit_empty_columns_raise(self) -> None:
            renderer = StagingRenderer(DIALECTS["duckdb"])
            with pytest.raises(ValueError, match="non-empty 'columns'"):
                renderer.render(
                    {
                        "name": "stg_customers",
                        "source_ref": {"name": "raw", "table": "customers"},
                        "columns": [],
                    },
                    renderer.dialect,
                    None,
                )


# Validation helpers.

def _register(project: Path, name: str, classification: str = "external") -> None:
    registrations = project / "local" / "registrations"
    registrations.mkdir(parents=True, exist_ok=True)
    (registrations / f"{name}.yml").write_text(
        f"name: {name}\n"
        f"classification: {classification}\n"
        "description: Registered input.\n"
        "owner: analytics\n",
        encoding="utf-8",
    )

def _validation_error(project: Path) -> str:
    with pytest.raises(LoomError) as error:
        validate_project(project)
    return str(error.value)


def test_staging_renderer_emits_package_macro_reference_verbatim() -> None:
    # Tier 1: generate stays manifest-free - a dotted package
    # reference renders exactly as declared, and a bare project
    # reference renders exactly as it did before the slot opened
    # (the byte-identical pin for existing instances).
    renderer = StagingRenderer(DIALECTS["duckdb"])
    dialect = renderer.dialect
    params = {
        "name": "stg_events",
        "source_ref": {"name": "raw", "table": "events"},
        "columns": [
            {"source": "id", "alias": "id"},
            {"source": "created_at", "alias": "created_date", "macro": "dbt.date_trunc"},
            {
                "source": "amount",
                "alias": "amount_usd",
                "macro": "cents_to_dollars",
                "macro_args": ["USD"],
            },
        ],
    }
    sql = renderer.render(params, dialect, None)
    assert "{{ dbt.date_trunc('created_at') }}" in sql
    assert "{{ cents_to_dollars('amount', 'USD') }}" in sql


def test_staging_renderer_places_column_by_macro_column_param() -> None:
    # Tier 2: macro_column_param pins the 1-based position the
    # source column fills, for signatures that are not
    # column-first; macro_args carries the remaining literals in
    # signature order.
    renderer = StagingRenderer(DIALECTS["duckdb"])
    dialect = renderer.dialect
    params = {
        "name": "stg_events",
        "source_ref": {"name": "raw", "table": "events"},
        "columns": [
            {
                "source": "created_at",
                "alias": "created_date",
                "macro": "dbt.date_trunc",
                "macro_args": ["day"],
                "macro_column_param": 2,
            },
            {
                "source": "amount",
                "alias": "amount_band",
                "macro": "band_amount",
                "macro_args": ["low", "high"],
                "macro_column_param": 3,
            },
        ],
    }
    sql = renderer.render(params, dialect, None)
    assert "{{ dbt.date_trunc('day', 'created_at') }}" in sql
    assert "{{ band_amount('low', 'high', 'amount') }}" in sql


def test_staging_renderer_explicit_first_position_matches_slot_bytes() -> None:
    # An explicit macro_column_param of 1 is the slot's own
    # convention: the emitted call is byte-identical.
    renderer = StagingRenderer(DIALECTS["duckdb"])
    dialect = renderer.dialect
    base = {
        "name": "stg_events",
        "source_ref": {"name": "raw", "table": "events"},
        "columns": [
            {
                "source": "amount",
                "alias": "amount_usd",
                "macro": "cents_to_dollars",
                "macro_args": ["USD"],
            }
        ],
    }
    pinned = {
        **base,
        "columns": [{**base["columns"][0], "macro_column_param": 1}],
    }
    assert renderer.render(pinned, dialect, None) == renderer.render(
        base, dialect, None
    )


@pytest.mark.parametrize("bad", [0, -1, 3, "2", True, 1.5])
def test_staging_renderer_rejects_unplaceable_macro_column_param(bad) -> None:
    renderer = StagingRenderer(DIALECTS["duckdb"])
    dialect = renderer.dialect
    params = {
        "name": "stg_events",
        "source_ref": {"name": "raw", "table": "events"},
        "columns": [
            {
                "source": "amount",
                "alias": "amount_usd",
                "macro": "cents_to_dollars",
                "macro_args": ["USD"],
                "macro_column_param": bad,
            }
        ],
    }
    with pytest.raises(ValueError, match="macro_column_param"):
        renderer.render(params, dialect, None)


def test_macro_column_param_requires_macro(tmp_path: Path) -> None:
    _staging_instance(
        tmp_path,
        inline_source={"name": "raw", "schema": "raw", "table": "events"},
        columns=[
            {"source": "created_at", "alias": "created_date", "macro_column_param": 2}
        ],
    )
    assert "'macro' is a dependency of 'macro_column_param'" in _validation_error(
        tmp_path
    )


@pytest.mark.parametrize("bad", ["2", 0, -1, True])
def test_macro_column_param_must_be_a_positive_integer(
    tmp_path: Path, bad
) -> None:
    _staging_instance(
        tmp_path,
        inline_source={"name": "raw", "schema": "raw", "table": "events"},
        columns=[
            {
                "source": "created_at",
                "alias": "created_date",
                "macro": "dbt.date_trunc",
                "macro_args": ["day"],
                "macro_column_param": bad,
            }
        ],
    )
    assert "macro_column_param: must be a positive integer" in _validation_error(
        tmp_path
    )


def test_macro_column_param_cannot_exceed_the_declared_call(
    tmp_path: Path,
) -> None:
    _staging_instance(
        tmp_path,
        inline_source={"name": "raw", "schema": "raw", "table": "events"},
        columns=[
            {
                "source": "created_at",
                "alias": "created_date",
                "macro": "dbt.date_trunc",
                "macro_args": ["day"],
                "macro_column_param": 3,
            }
        ],
    )
    assert (
        "macro_column_param: position 3 exceeds the 2 argument(s)"
        in _validation_error(tmp_path)
    )


def test_macro_column_param_validates_within_the_declared_call(
    tmp_path: Path,
) -> None:
    _staging_instance(
        tmp_path,
        inline_source={"name": "raw", "schema": "raw", "table": "events"},
        columns=[
            {
                "source": "created_at",
                "alias": "created_date",
                "macro": "dbt.date_trunc",
                "macro_args": ["day"],
                "macro_column_param": 2,
            }
        ],
    )
    entries = validate_project(tmp_path)
    assert entries
    # The dotted reference itself stays warn-only at validate
    # (manifest-free by design); the position adds no error.
    assert any(
        "unverified package macro" in warning
        for warning in staging_macro_warnings(entries)
    )
