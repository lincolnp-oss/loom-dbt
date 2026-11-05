"""The manifest macro index and reference verification (Tier 1)."""
from __future__ import annotations

from loom.manifest import manifest_macro_index, verify_macro_reference


def _document() -> dict:
    return {
        "metadata": {"project_name": "shop"},
        "macros": {
            "macro.shop.cents_to_dollars": {
                "name": "cents_to_dollars",
                "package_name": "shop",
                "arguments": [{"name": "column_name", "type": None}],
                "macro_sql": "{% macro cents_to_dollars(column_name) %}x{% endmacro %}",
            },
            "macro.dbt.date_trunc": {
                "name": "date_trunc",
                "package_name": "dbt",
                "arguments": [
                    {"name": "datepart", "type": None},
                    {"name": "date", "type": None},
                ],
                "macro_sql": "{% macro date_trunc(datepart, date) %}x{% endmacro %}",
            },
            "macro.pkg.flex": {
                "name": "flex",
                "package_name": "pkg",
                "arguments": [
                    {"name": "column_name", "type": None},
                    {"name": "fmt", "type": None},
                    {"name": "strict", "type": None},
                ],
                "macro_sql": (
                    "{% macro flex(column_name, fmt='iso', "
                    "strict=flag(true, (1, 2))) %}x{% endmacro %}"
                ),
            },
        },
    }


def test_index_keys_by_package_and_name_with_signature_order() -> None:
    index = manifest_macro_index(_document())
    assert set(index) == {
        ("shop", "cents_to_dollars"),
        ("dbt", "date_trunc"),
        ("pkg", "flex"),
    }
    assert [a["name"] for a in index[("dbt", "date_trunc")]] == [
        "datepart",
        "date",
    ]


def test_defaults_come_from_the_signature_not_the_arguments_list() -> None:
    index = manifest_macro_index(_document())
    flex = {a["name"]: a["has_default"] for a in index[("pkg", "flex")]}
    # fmt and strict carry defaults - strict's default contains
    # parens and commas and must not truncate the signature.
    assert flex == {"column_name": False, "fmt": True, "strict": True}


def test_verify_existence_and_arity() -> None:
    index = manifest_macro_index(_document())
    assert (
        verify_macro_reference(index, "dbt.date_trunc", 2, project_name="shop")
        is None
    )
    problem = verify_macro_reference(
        index, "dbt.date_trunc", 1, project_name="shop"
    )
    assert problem is not None and "takes 2 argument(s)" in problem
    problem = verify_macro_reference(index, "dbt.missing", 1, project_name="shop")
    assert problem is not None and "is not defined in the manifest" in problem


def test_verify_arity_spans_defaults() -> None:
    index = manifest_macro_index(_document())
    # flex: 1 required of 3 declared - 1, 2 and 3 supplied all fit.
    for supplied in (1, 2, 3):
        assert (
            verify_macro_reference(index, "pkg.flex", supplied, project_name="shop")
            is None
        )
    problem = verify_macro_reference(index, "pkg.flex", 4, project_name="shop")
    assert problem is not None and "between 1 and 3 arguments" in problem
    problem = verify_macro_reference(index, "pkg.flex", 0, project_name="shop")
    assert problem is not None


def test_bare_reference_resolves_against_the_project_package() -> None:
    index = manifest_macro_index(_document())
    assert (
        verify_macro_reference(
            index, "cents_to_dollars", 1, project_name="shop"
        )
        is None
    )
    problem = verify_macro_reference(
        index, "cents_to_dollars", 1, project_name="other"
    )
    assert problem is not None and "is not defined" in problem


# --- M7: undocumented macros derive parameters from the signature ---


def _undocumented(sql: str, name: str = "m") -> dict:
    return {
        "metadata": {"project_name": "shop"},
        "macros": {
            f"macro.shop.{name}": {
                "name": name,
                "package_name": "shop",
                "arguments": [],
                "macro_sql": sql,
            }
        },
    }


def test_undocumented_macro_parameters_come_from_the_signature() -> None:
    index = manifest_macro_index(
        _undocumented(
            "{% macro add_tax(col, rate) %}{{ col }}{% endmacro %}",
            name="add_tax",
        )
    )
    assert index[("shop", "add_tax")] == [
        {"name": "col", "has_default": False},
        {"name": "rate", "has_default": False},
    ]


def test_undocumented_macro_defaults_come_from_the_signature() -> None:
    index = manifest_macro_index(
        _undocumented(
            "{% macro m(a, b=(1, 2), sep=', ', quoted=')', "
            "computed=range(1, 3)) %}x{% endmacro %}"
        )
    )
    # Hostile defaults: parens, a comma inside quotes, a quoted
    # closing paren, a nested call - none may truncate or split
    # the parameter list.
    assert index[("shop", "m")] == [
        {"name": "a", "has_default": False},
        {"name": "b", "has_default": True},
        {"name": "sep", "has_default": True},
        {"name": "quoted", "has_default": True},
        {"name": "computed", "has_default": True},
    ]


def test_undocumented_macro_multiline_and_whitespace_control() -> None:
    index = manifest_macro_index(
        _undocumented(
            "{%- macro m(\n    first,\n    second='x'\n) -%}y{% endmacro %}"
        )
    )
    assert index[("shop", "m")] == [
        {"name": "first", "has_default": False},
        {"name": "second", "has_default": True},
    ]


def test_undocumented_zero_argument_macro_stays_empty() -> None:
    index = manifest_macro_index(
        _undocumented("{% macro m() %}x{% endmacro %}")
    )
    assert index[("shop", "m")] == []


def test_unparseable_signature_falls_back_to_empty() -> None:
    # A parameter that is not a bare identifier: never fabricate.
    index = manifest_macro_index(
        _undocumented("{% macro m(a b) %}x{% endmacro %}")
    )
    assert index[("shop", "m")] == []
    # A signature that never closes: same fall-back.
    index = manifest_macro_index(
        _undocumented("{% macro m(a, b %}x{% endmacro %}")
    )
    assert index[("shop", "m")] == []


def test_declared_arguments_win_over_the_signature() -> None:
    # When dbt DID record arguments, they are the index's list;
    # the signature still supplies only the defaults.
    document = _undocumented(
        "{% macro m(from_sql) %}x{% endmacro %}", name="m"
    )
    document["macros"]["macro.shop.m"]["arguments"] = [
        {"name": "documented", "type": None}
    ]
    index = manifest_macro_index(document)
    assert index[("shop", "m")] == [
        {"name": "documented", "has_default": False}
    ]


def test_verify_arity_uses_derived_parameters() -> None:
    index = manifest_macro_index(
        _undocumented(
            "{% macro m(col, fmt='iso') %}x{% endmacro %}", name="m"
        )
    )
    assert (
        verify_macro_reference(index, "m", 1, project_name="shop") is None
    )
    assert (
        verify_macro_reference(index, "m", 2, project_name="shop") is None
    )
    assert (
        verify_macro_reference(index, "m", 0, project_name="shop")
        is not None
    )
    assert (
        verify_macro_reference(index, "m", 3, project_name="shop")
        is not None
    )
