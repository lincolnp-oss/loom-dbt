from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.inventory import validate_project
from metadata_helpers import write_pattern_instance


def test_invalid_expanded_inputs_keep_aggregate_relationship_diagnostics(
    tmp_path: Path,
) -> None:
    fields = {"description": "Relationship validation", "owner": "analytics"}
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            **fields,
            "source_ref": {"name": "raw", "table": "customer"},
            "columns": [7, {"source": "id", "alias": "id", "macro": "missing_macro"}],
        },
        pattern="staging",
    )
    write_pattern_instance(
        tmp_path,
        "hub_customer",
        "hub",
        {
            **fields,
            "input_models": ["stg_customer"],
            "business_key": "customer_key",
        },
        pattern="hub",
    )
    write_pattern_instance(
        tmp_path,
        "link_customer_missing",
        "link",
        {
            **fields,
            "input_models": ["stg_customer"],
            "endpoints": [
                {"hub": "hub_customer", "key": "id"},
                {"hub": "hub_missing", "key": "missing_id"},
            ],
        },
        pattern="link",
    )
    write_pattern_instance(
        tmp_path,
        "dim_customer",
        "scd2_dimension",
        {
            **fields,
            "input_models": ["stg_customer"],
            "business_key": "id",
            "tracked_columns": ["name", 7],
        },
        pattern="history",
    )

    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)

    # The aggregate diagnostics all survive,
    # grouped by file then entry under a count headline - the
    # aggregation this test exists to pin is unchanged; only the
    # presentation is grouped.
    assert str(error.value).splitlines() == [
        "Validation failed: 9 problems in 4 files",
        "  metadata/instances/history.yml:",
        "    instances[dim_customer]:",
        "      - tracked_columns.1: 7 is not of type 'string'",
        "      - input_models staging model 'stg_customer' does not "
        "generate required SCD2 column alias(es): name",
        "  metadata/instances/staging.yml:",
        "    instances[stg_customer]:",
        "      - columns.0: 7 is not of type 'object'",
        "      - columns[1].macro: 'missing_macro' is not defined "
        "by a project-authored macro under macro-paths (macros)",
        "      - source_ref raw.customer was not found in dbt native "
        "source YAML under model-paths",
        "  metadata/instances/hub.yml:",
        "    instances[hub_customer]:",
        "      - input_models staging model 'stg_customer' does not "
        "generate required hub column alias: customer_key",
        "  metadata/instances/link.yml:",
        "    instances[link_customer_missing]:",
        "      - input_models staging model 'stg_customer' does not "
        "generate required link column alias(es): missing_id",
        "      - endpoints.0.key 'id' must match 'hub_customer' business_key "
        "'customer_key'",
        "      - endpoints.1.hub 'hub_missing' is not an inventoried hub",
        "Fix the file(s) above and re-run `loom validate`.",
    ]


def test_threenf_only_project_still_validates(tmp_path: Path) -> None:
    """A project with only normalized_table entries must not skip
    relationship or required-field validation."""
    write_pattern_instance(
        tmp_path,
        "n_customer",
        "normalized_table",
        {
            "description": "Customer entity",
            "owner": "analytics",
            "input_models": ["does_not_exist"],
        },
        pattern="normalized_table",
    )

    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)
    message = str(error.value)
    assert "input_models 'does_not_exist' not found in model inventory" in message
    assert "entity_columns must be a non-empty list" in message
    assert "attribute_columns must be a non-empty list" in message


def test_threenf_only_project_valid_when_coherent(tmp_path: Path) -> None:
    registrations = tmp_path / "local" / "registrations"
    registrations.mkdir(parents=True)
    (registrations / "bronze_customers.yml").write_text(
        "name: bronze_customers\n"
        "classification: bronze\n"
        "description: Landed customer extract.\n"
        "owner: analytics\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        tmp_path,
        "n_customer",
        "normalized_table",
        {
            "description": "Customer entity",
            "owner": "analytics",
            "input_models": ["bronze_customers"],
            "entity_columns": ["customer_id"],
            "attribute_columns": ["country"],
        },
        pattern="normalized_table",
    )

    entries = validate_project(tmp_path)
    assert {entry.data["name"] for entry in entries} == {
        "bronze_customers",
        "n_customer",
    }


def _write_vault_core(project: Path) -> None:
    """Staging + two hubs + a link: the vault core Kimball reads from."""
    write_pattern_instance(
        project,
        "stg_order_items",
        "staging",
        {
            "description": "Order item staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "schema": "main", "table": "order_items"},
            "columns": [
                {"source": "order_id", "alias": "order_id"},
                {"source": "customer_id", "alias": "customer_id"},
                {"source": "quantity", "alias": "quantity"},
            ],
        },
        pattern="staging",
    )
    for name, key in (("hub_order", "order_id"), ("hub_customer", "customer_id")):
        write_pattern_instance(
            project,
            name,
            "hub",
            {
                "description": f"{name} hub",
                "owner": "analytics",
                "input_models": ["stg_order_items"],
                "business_key": key,
            },
            pattern="hub",
        )
    write_pattern_instance(
        project,
        "link_order_customer",
        "link",
        {
            "description": "Order-customer link",
            "owner": "analytics",
            "input_models": ["stg_order_items"],
            "endpoints": [
                {"hub": "hub_order", "key": "order_id"},
                {"hub": "hub_customer", "key": "customer_id"},
            ],
        },
        pattern="link",
    )


def test_kimball_fact_rejects_underivable_input(tmp_path: Path) -> None:
    """A gold model exists in the inventory but its outputs are not
    derivable, so a fact cannot validate referenced columns against
    it and validation fails explicitly."""
    _write_vault_core(tmp_path)
    write_pattern_instance(
        tmp_path,
        "gold_totals",
        "gold",
        {
            "description": "Gold totals",
            "owner": "analytics",
            "input_models": ["stg_order_items"],
            "measures": [
                {"column": "quantity", "expression": "SUM(quantity)", "alias": "total"}
            ],
            "group_by": ["customer_id"],
        },
        pattern="gold",
    )
    write_pattern_instance(
        tmp_path,
        "fact_orders",
        "fact",
        {
            "description": "Order facts",
            "owner": "analytics",
            "input_models": ["gold_totals"],
            "dimension_keys": ["customer_id"],
            "measure_columns": ["total"],
        },
        pattern="fact",
    )
    with pytest.raises(LoomError) as error:
        validate_project(tmp_path)
    assert "whose output columns Loom can determine" in str(error.value)


def test_unquoted_on_key_in_handwritten_yaml_is_restored(tmp_path: Path) -> None:
    """YAML 1.1 parses an unquoted `on:` mapping key as boolean True.

    Hand-written metadata uses `on:` naturally (the documented joins
    shape); Loom restores the intended key at load instead of
    reporting the join as missing its keys.
    """
    _write_vault_core(tmp_path)
    instances_dir = tmp_path / "metadata" / "instances"
    instances_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "metadata" / "patterns" / "obt.yml").write_text(
        "name: obt\ngenerator: obt\n",
        encoding="utf-8",
    )
    (instances_dir / "obt.yml").write_text(
        "pattern: obt\n"
        "instances:\n"
        "  - name: obt_lines\n"
        "    params:\n"
        "      description: Wide lines\n"
        "      owner: analytics\n"
        "      input_models: [stg_order_items]\n"
        "      columns: [order_item_id]\n"
        "      joins:\n"
        "        - model: hub_customer\n"
        "          on:\n"
        "            - customer_id\n"
        "          columns: [customer_hk]\n",
        encoding="utf-8",
    )
    entries = validate_project(tmp_path)
    obt = next(entry for entry in entries if entry.data["name"] == "obt_lines")
    assert obt.data["joins"][0]["on"] == ["customer_id"]


# ------------------------------------------------------------------
# Loop consolidation: the fact/dimension mirror
# pairs below were per-case pytest nodes asserting the same shape
# with different row data. Each loop asserts every row's content
# (the same strings the individual tests asserted) and names the
# failing row; only node granularity is lost. Former test names are
# recorded in each loop's docstring.
# ------------------------------------------------------------------


def _write_staging_hub(project: Path) -> None:
    """Staging + hub where the staging carries a record_source column
    the hub does not declare."""
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Customer staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "schema": "main", "table": "customers"},
            "columns": [
                {"source": "customer_id", "alias": "customer_id"},
                {"source": "source_system", "alias": "record_source"},
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "hub_customer",
        "hub",
        {
            "description": "Customer hub",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        pattern="hub",
    )


def _write_provenance_hub(project: Path) -> None:
    """Staging + persistent hub declaring record_source."""
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            "description": "Customer staging",
            "owner": "analytics",
            "inline_source": {"name": "raw", "schema": "main", "table": "customers"},
            "columns": [
                {"source": "customer_id", "alias": "customer_id"},
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "hub_customer",
        "hub",
        {
            "description": "Customer hub",
            "owner": "analytics",
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
            "retention": "persistent",
            "record_source": "crm",
        },
        pattern="hub",
    )


def test_kimball_consumers_missing_staging_reference(tmp_path: Path) -> None:
    """Formerly test_kimball_fact_missing_staging_reference and
    test_kimball_dimension_missing_staging_reference."""
    rows = [
        (
            "fact",
            "fct_orders",
            {
                "description": "Order facts",
                "owner": "analytics",
                "input_models": ["stg_nonexistent"],
                "measure_columns": ["quantity"],
                "dimension_keys": ["customer_id"],
            },
        ),
        (
            "dimension",
            "dim_customers",
            {
                "description": "Customer dimension",
                "owner": "analytics",
                "input_models": ["stg_nonexistent"],
                "natural_key": "customer_id",
                "surrogate_key": "customer_sk",
                "attribute_columns": ["customer_name"],
            },
        ),
    ]
    failures: list[str] = []
    for generator, name, params in rows:
        write_pattern_instance(tmp_path, name, generator, params, pattern=generator)
        try:
            validate_project(tmp_path)
            failures.append(f"{generator}: validation did not raise")
        except LoomError as error:
            message = str(error)
            if "stg_nonexistent" not in message:
                failures.append(f"{generator}: missing name not named")
            if "not found in model inventory" not in message:
                failures.append(f"{generator}: missing inventory message")
        # Reset the project between rows: each row is an independent
        # single-consumer project.
        for child in list((tmp_path / "metadata").rglob("*.yml")):
            child.unlink()
    assert not failures, failures


def test_kimball_consumers_missing_staging_columns(tmp_path: Path) -> None:
    """Formerly test_kimball_fact_missing_staging_columns and
    test_kimball_dimension_missing_staging_columns."""
    rows = [
        (
            "stg_orders",
            "orders",
            [{"source": "order_id", "alias": "order_id"}],
            "fact",
            "fct_orders",
            {
                "description": "Order facts",
                "owner": "analytics",
                "input_models": ["stg_orders"],
                "measure_columns": ["quantity"],
                "dimension_keys": ["customer_id"],
            },
            ["customer_id", "quantity"],
        ),
        (
            "stg_customers",
            "customers",
            [{"source": "customer_id", "alias": "customer_id"}],
            "dimension",
            "dim_customers",
            {
                "description": "Customer dimension",
                "owner": "analytics",
                "input_models": ["stg_customers"],
                "natural_key": "customer_id",
                "surrogate_key": "customer_sk",
                "attribute_columns": ["customer_name", "email"],
            },
            ["customer_name", "email"],
        ),
    ]
    failures: list[str] = []
    for staging, table, columns, generator, name, params, expected in rows:
        write_pattern_instance(
            tmp_path,
            staging,
            "staging",
            {
                "description": "Staging",
                "owner": "analytics",
                "inline_source": {"name": "raw", "schema": "main", "table": table},
                "columns": columns,
            },
            pattern="staging",
        )
        write_pattern_instance(tmp_path, name, generator, params, pattern=generator)
        try:
            validate_project(tmp_path)
            failures.append(f"{generator}: validation did not raise")
        except LoomError as error:
            message = str(error)
            if "does not generate required column" not in message:
                failures.append(f"{generator}: missing column message")
            for column in expected:
                if column not in message:
                    failures.append(f"{generator}: {column} not named")
        for child in list((tmp_path / "metadata").rglob("*.yml")):
            child.unlink()
    assert not failures, failures


def test_kimball_consumers_require_staging_reference(tmp_path: Path) -> None:
    """Formerly test_kimball_fact_requires_staging_reference and
    test_kimball_dimension_requires_staging_reference."""
    rows = [
        (
            "stg_orders",
            "orders",
            [
                {"source": "order_id", "alias": "order_id"},
                {"source": "customer_id", "alias": "customer_id"},
                {"source": "product_id", "alias": "product_id"},
                {"source": "quantity", "alias": "quantity"},
                {"source": "unit_price", "alias": "unit_price"},
            ],
            "fact",
            "fct_orders",
            {
                "description": "Order facts",
                "owner": "analytics",
                "input_models": ["stg_orders"],
                "measure_columns": ["quantity", "unit_price"],
                "dimension_keys": ["customer_id", "product_id"],
            },
        ),
        (
            "stg_customers",
            "customers",
            [
                {"source": "customer_id", "alias": "customer_id"},
                {"source": "customer_name", "alias": "customer_name"},
                {"source": "email", "alias": "email"},
            ],
            "dimension",
            "dim_customers",
            {
                "description": "Customer dimension",
                "owner": "analytics",
                "input_models": ["stg_customers"],
                "natural_key": "customer_id",
                "surrogate_key": "customer_sk",
                "attribute_columns": ["customer_name", "email"],
            },
        ),
    ]
    failures: list[str] = []
    for staging, table, columns, generator, name, params in rows:
        write_pattern_instance(
            tmp_path,
            staging,
            "staging",
            {
                "description": "Staging",
                "owner": "analytics",
                "inline_source": {"name": "raw", "schema": "main", "table": table},
                "columns": columns,
            },
            pattern="staging",
        )
        write_pattern_instance(tmp_path, name, generator, params, pattern=generator)
        entries = validate_project(tmp_path)
        if len(entries) != 2:
            failures.append(f"{generator}: expected 2 entries, got {len(entries)}")
        for child in list((tmp_path / "metadata").rglob("*.yml")):
            child.unlink()
    assert not failures, failures


def test_kimball_consumers_reject_underivable_columns(tmp_path: Path) -> None:
    """Formerly test_kimball_fact_from_link_rejects___loaded_at,
    test_kimball_dimension_from_hub_rejects___loaded_at,
    test_kimball_dimension_from_hub_rejects_record_source_from_staging
    and test_kimball_fact_from_link_rejects_unknown_column. Rows carry
    their own setup: the first three share the vault core, the
    record_source row its staging+hub setup."""
    rows = [
        (
            _write_vault_core,
            "fact",
            "fact_orders",
            {
                "description": "Order facts",
                "owner": "analytics",
                "input_models": ["link_order_customer"],
                "dimension_keys": ["order_hk"],
                "measure_columns": ["__loaded_at"],
            },
            "__loaded_at",
        ),
        (
            _write_vault_core,
            "dimension",
            "dim_customers",
            {
                "description": "Customer dimension",
                "owner": "analytics",
                "input_models": ["hub_customer"],
                "natural_key": "customer_id",
                "surrogate_key": "customer_sk",
                "attribute_columns": ["__loaded_at"],
            },
            "__loaded_at",
        ),
        (
            _write_staging_hub,
            "dimension",
            "dim_customers",
            {
                "description": "Customer dimension",
                "owner": "analytics",
                "input_models": ["hub_customer"],
                "natural_key": "customer_id",
                "surrogate_key": "customer_sk",
                "attribute_columns": ["__record_source"],
            },
            "__record_source",
        ),
        (
            _write_vault_core,
            "fact",
            "fact_orders",
            {
                "description": "Order facts",
                "owner": "analytics",
                "input_models": ["link_order_customer"],
                "dimension_keys": ["order_hk"],
                "measure_columns": ["quantity"],
            },
            "quantity",
        ),
    ]
    failures: list[str] = []
    for setup, generator, name, params, column in rows:
        setup(tmp_path)
        write_pattern_instance(tmp_path, name, generator, params, pattern=generator)
        try:
            validate_project(tmp_path)
            failures.append(f"{generator}/{column}: validation did not raise")
        except LoomError as error:
            expected = f"does not generate required column(s): {column}"
            if expected not in str(error):
                failures.append(f"{generator}/{column}: {expected!r} not in error")
        for child in list((tmp_path / "metadata").rglob("*.yml")):
            child.unlink()
    assert not failures, failures


def test_kimball_consumers_read_from_vault_core(tmp_path: Path) -> None:
    """Formerly test_kimball_fact_reads_from_link,
    test_kimball_dimension_reads_from_hub and
    test_kimball_dimension_reads_hub_provenance_columns."""
    rows = [
        (
            _write_vault_core,
            "fact",
            "fact_orders",
            {
                "description": "Order facts from the vault link",
                "owner": "analytics",
                "input_models": ["link_order_customer"],
                "dimension_keys": ["order_hk", "customer_hk"],
                "measure_columns": [],
            },
            5,
        ),
        (
            _write_vault_core,
            "dimension",
            "dim_customers",
            {
                "description": "Customer dimension from the vault hub",
                "owner": "analytics",
                "input_models": ["hub_customer"],
                "natural_key": "customer_id",
                "surrogate_key": "customer_sk",
                "attribute_columns": ["customer_hk"],
            },
            5,
        ),
        (
            _write_provenance_hub,
            "dimension",
            "dim_customers",
            {
                "description": "Customer dimension",
                "owner": "analytics",
                "input_models": ["hub_customer"],
                "natural_key": "customer_id",
                "surrogate_key": "customer_sk",
                "attribute_columns": ["__record_source", "__loaded_at"],
            },
            3,
        ),
    ]
    failures: list[str] = []
    for setup, generator, name, params, expected_count in rows:
        setup(tmp_path)
        write_pattern_instance(tmp_path, name, generator, params, pattern=generator)
        entries = validate_project(tmp_path)
        if len(entries) != expected_count:
            failures.append(
                f"{generator}: expected {expected_count} entries, "
                f"got {len(entries)}"
            )
        for child in list((tmp_path / "metadata").rglob("*.yml")):
            child.unlink()
    assert not failures, failures


def _staging_project(tmp_path: Path, macro: str) -> Path:
    fields = {"description": "Macro validation", "owner": "analytics"}
    write_pattern_instance(
        tmp_path,
        "stg_customer",
        "staging",
        {
            **fields,
            "source_ref": {"name": "raw", "table": "customer"},
            "columns": [
                {"source": "id", "alias": "id"},
                {"source": "created_at", "alias": "created_date", "macro": macro},
            ],
        },
        pattern="staging",
    )
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: customer\n",
        encoding="utf-8",
    )
    return tmp_path


def test_package_macro_reference_validates_and_warns(tmp_path: Path) -> None:
    # Tier 1: a dotted package reference is not an error at
    # validate time (validation is manifest-free); it warns as
    # unverified instead.
    from loom.inventory_relationships import staging_macro_warnings

    project = _staging_project(tmp_path, "dbt.date_trunc")
    entries = validate_project(project)
    warnings = staging_macro_warnings(entries)
    assert len(warnings) == 1
    assert "'dbt.date_trunc' is an unverified package macro" in warnings[0]


def test_bare_macro_still_requires_a_project_definition(
    tmp_path: Path,
) -> None:
    project = _staging_project(tmp_path, "missing_macro")
    with pytest.raises(LoomError) as error:
        validate_project(project)
    assert "'missing_macro' is not defined" in str(error.value)


@pytest.mark.parametrize(
    "macro",
    ["a.b.c", "pkg.9bad", "pkg.", ".name", "two words"],
)
def test_malformed_macro_references_are_rejected(
    tmp_path: Path, macro: str
) -> None:
    project = _staging_project(tmp_path, macro)
    with pytest.raises(LoomError) as error:
        validate_project(project)
    assert "is not a safe macro identifier" in str(error.value)


def test_reserved_name_segment_rejected_in_dotted_form(
    tmp_path: Path,
) -> None:
    project = _staging_project(tmp_path, "pkg.ref")
    with pytest.raises(LoomError) as error:
        validate_project(project)
    assert "is a reserved dbt/Jinja name" in str(error.value)


def test_validate_command_prints_unverified_package_macro_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from loom.cli import main

    project = _staging_project(tmp_path, "dbt_utils.star")
    assert main(["validate", "--project-dir", str(project)]) == 0
    captured = capsys.readouterr()
    assert "Validated" in captured.out
    assert "unverified package macro" in captured.err
