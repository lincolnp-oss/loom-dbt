"""The carriage merge: declarations onto a model entry."""
from __future__ import annotations

from collections import Counter

import yaml

from generator_conformance import CANONICAL_INSTANCES
from loom.declared_generic_tests import (
    _split_rendered,
    apply_carried_tests,
    apply_carried_to_document,
    carried_generic_tests,
    render_test_entry,
    rendered_test_signature,
)
from loom.renderers import get_renderer


def test_render_test_entry_forms() -> None:
    assert render_test_entry({"test": "not_null"}) == "not_null"
    assert render_test_entry(
        {"test": "accepted_values", "kwargs": {"values": ["a", "b"]}}
    ) == {"accepted_values": {"values": ["a", "b"]}}
    assert render_test_entry(
        {"test": "unique", "config": {"severity": "warn"}}
    ) == {"unique": {"config": {"severity": "warn"}}}
    assert render_test_entry(
        {
            "test": "relationships",
            "kwargs": {"to": "ref('x')"},
            "config": {"severity": "error"},
        }
    ) == {
        "relationships": {
            "to": "ref('x')",
            "config": {"severity": "error"},
        }
    }


def test_apply_merges_model_and_column_tests() -> None:
    entry = {
        "name": "stg_customers",
        "description": "d",
        "columns": [
            {"name": "customer_id", "data_type": "BIGINT"},
            {"name": "customer_name"},
        ],
    }
    combined = apply_carried_tests(
        entry,
        [
            {"test": "not_null", "column_name": "customer_id"},
            {"test": "unique", "column_name": "CUSTOMER_ID"},
            {"test": "model_level_check"},
            {"test": "not_null", "column_name": "undeclared_col"},
        ],
    )
    assert combined["tests"] == ["model_level_check"]
    columns = {c["name"]: c for c in combined["columns"]}
    assert columns["customer_id"]["tests"] == ["not_null", "unique"]
    # A column the entry does not list gains a stub entry -
    # the carried test is never dropped.
    assert columns["undeclared_col"] == {
        "name": "undeclared_col",
        "tests": ["not_null"],
    }
    # The input entry is not mutated.
    assert "tests" not in entry


def test_apply_to_document_creates_a_missing_entry() -> None:
    document = {"version": 2, "models": [{"name": "other"}]}
    combined = apply_carried_to_document(
        document, "stg_customers", [{"test": "not_null", "column_name": "id"}]
    )
    names = [m["name"] for m in combined["models"]]
    assert names == ["other", "stg_customers"]
    assert combined["models"][1]["columns"] == [
        {"name": "id", "tests": ["not_null"]}
    ]


def test_carried_generic_tests_normalises() -> None:
    data = {
        "generic_tests": [
            {"test": "not_null", "column_name": "id", "kwargs": {}, "config": {}},
            {"test": "singular-like", "kwargs": {"a": 1}},
            "junk",
            {"column_name": "id"},
        ]
    }
    assert carried_generic_tests(data) == [
        {"test": "not_null", "column_name": "id"},
        {"test": "singular-like", "kwargs": {"a": 1}},
    ]
    assert carried_generic_tests({}) == []


# --- the generation-time dedup --------------------------------
#
# dbt names a generic test from its name, its attachment and
# its arguments; a carried declaration repeating a
# generator-emitted test on all three produced two entries
# with one name and `dbt parse` failed. Generation now emits
# such a test once.


def test_apply_dedupes_a_carried_duplicate_of_an_emitted_test() -> None:
    entry = {
        "name": "gold_sales",
        "columns": [
            {"name": "total_amount", "tests": ["not_null"]},
        ],
    }
    combined = apply_carried_tests(
        entry, [{"test": "not_null", "column_name": "total_amount"}]
    )
    assert combined["columns"][0]["tests"] == ["not_null"]


def test_apply_dedupes_model_level_tests() -> None:
    entry = {"name": "m", "tests": ["not_null"]}
    combined = apply_carried_tests(entry, [{"test": "not_null"}])
    assert combined["tests"] == ["not_null"]


def test_apply_merges_carried_config_onto_the_emitted_test() -> None:
    # The probe case: convert preserves a declared
    # severity of warn as carriage config. The config must
    # land on the single emitted entry, never be dropped
    # with the duplicate.
    entry = {
        "name": "gold_sales",
        "columns": [
            {"name": "total_amount", "tests": ["not_null"]},
        ],
    }
    combined = apply_carried_tests(
        entry,
        [
            {
                "test": "not_null",
                "column_name": "total_amount",
                "config": {"severity": "warn"},
            }
        ],
    )
    assert combined["columns"][0]["tests"] == [
        {"not_null": {"config": {"severity": "warn"}}}
    ]


def test_apply_config_merge_keeps_emitted_config_keys() -> None:
    entry = {
        "name": "m",
        "columns": [
            {
                "name": "c",
                "tests": [
                    {"not_null": {"config": {"severity": "error", "warn_after": 1}}}
                ],
            }
        ],
    }
    combined = apply_carried_tests(
        entry,
        [
            {
                "test": "not_null",
                "column_name": "c",
                "config": {"severity": "warn"},
            }
        ],
    )
    # The declaration's severity wins; the emitted entry's
    # other config keys survive the merge.
    assert combined["columns"][0]["tests"] == [
        {"not_null": {"config": {"severity": "warn", "warn_after": 1}}}
    ]


def test_apply_keeps_a_declaration_with_differing_kwargs() -> None:
    # Same name and column, different arguments: a distinct
    # test to dbt (a different generated name), carried in
    # full beside the emitted one.
    entry = {
        "name": "link_order_customer",
        "columns": [
            {
                "name": "customer_hk",
                "tests": [
                    {
                        "relationships": {
                            "arguments": {
                                "to": "ref('hub_customer')",
                                "field": "customer_hk",
                            }
                        }
                    }
                ],
            }
        ],
    }
    combined = apply_carried_tests(
        entry,
        [
            {
                "test": "relationships",
                "column_name": "customer_hk",
                "kwargs": {"to": "ref('hub_order')", "field": "customer_hk"},
            }
        ],
    )
    assert len(combined["columns"][0]["tests"]) == 2


def test_apply_dedupes_across_the_arguments_wrapper() -> None:
    # The link generator emits `relationships` in dbt's
    # `arguments:`-wrapped spelling; a declaration in the
    # direct spelling names the same test and dedupes.
    entry = {
        "name": "link_order_customer",
        "columns": [
            {
                "name": "customer_hk",
                "tests": [
                    {
                        "relationships": {
                            "arguments": {
                                "to": "ref('hub_customer')",
                                "field": "customer_hk",
                            }
                        }
                    }
                ],
            }
        ],
    }
    combined = apply_carried_tests(
        entry,
        [
            {
                "test": "relationships",
                "column_name": "customer_hk",
                "kwargs": {
                    "to": "ref('hub_customer')",
                    "field": "customer_hk",
                },
            }
        ],
    )
    assert len(combined["columns"][0]["tests"]) == 1


def test_signature_ignores_malformed_rendered_entries() -> None:
    # Renderers only ever emit a bare string or a single-key
    # mapping; anything else has no identity to dedup on and
    # is carried untouched rather than crashing generation.
    assert rendered_test_signature({"a": 1, "b": 2}) is None
    assert rendered_test_signature(42) is None
    assert rendered_test_signature({"not_null": "oops"}) is None
    assert (
        rendered_test_signature({"UniQue": {"config": {"severity": "warn"}}})
        == ("unique", "{}")
    )


def _emitted_pairs(document: dict) -> list:
    """(column or None, rendered entry) pairs in a tests
    document, in document order."""
    pairs = []
    for model in document.get("models") or []:
        for rendered in model.get("tests") or []:
            pairs.append((None, rendered))
        for column in model.get("columns") or []:
            for rendered in column.get("tests") or []:
                pairs.append((column.get("name"), rendered))
    return pairs


def test_every_generators_emitted_set_dedupes() -> None:
    """The product-wide fence: for every generator that
    emits tests, carriage declaring its full emitted set
    generates each test exactly once."""
    exercised = 0
    for name, generator, params in CANONICAL_INSTANCES:
        data = {"name": name, "generator": generator, **params}
        renderer = get_renderer(generator, "duckdb")
        test_yaml = renderer.render_tests(data, renderer.dialect, None)
        if not test_yaml:
            continue
        exercised += 1
        document = yaml.safe_load(test_yaml)
        emitted = _emitted_pairs(document)
        assert emitted, f"{generator}: render_tests parsed to no tests"
        declarations = []
        for column_name, rendered in emitted:
            split = _split_rendered(rendered)
            assert split is not None, f"{generator}: unparseable {rendered!r}"
            test_name, kwargs, _config = split
            declaration = {"test": test_name}
            if column_name is not None:
                declaration["column_name"] = column_name
            if kwargs:
                declaration["kwargs"] = kwargs
            declarations.append(declaration)
        combined = apply_carried_to_document(document, name, declarations)
        model = next(m for m in combined["models"] if m["name"] == name)
        result = _emitted_pairs({"models": [model]})
        before = Counter(
            (
                (column or "").casefold(),
                rendered_test_signature(rendered),
            )
            for column, rendered in emitted
        )
        after = Counter(
            (
                (column or "").casefold(),
                rendered_test_signature(rendered),
            )
            for column, rendered in result
        )
        assert after == before, (
            f"{generator}: carriage of the emitted set changed the "
            f"test multiset: {after} != {before}"
        )
    # The fence must actually exercise the product surface:
    # every generator but a handful emits a tests document
    # for its canonical instance.
    assert exercised >= 12, f"only {exercised} generators exercised"
