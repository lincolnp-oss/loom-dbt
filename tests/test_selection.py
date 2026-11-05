"""The selector engine: grammar layers, dbt
set operators and graph forms, ranking and teaching errors,
shape banding in search mode against strict selection (the
guardrail), and dbt-style named selectors."""

from __future__ import annotations

from pathlib import Path

import pytest

from loom.errors import LoomError
from loom.selection import (
    SelectableRecord,
    diagnose_empty_branch,
    evaluate_selectors,
    load_named_selectors,
    parse_branch,
)


def _records() -> list[SelectableRecord]:
    """A small Data Vault shaped record set with a source, two
    macros (one from a package) and a test hanging off it."""
    return [
        SelectableRecord(
            name="stg_customers",
            status="generated",
            generator="staging",
            pattern="staging_pattern",
            paradigm="staging",
            source_inputs=("raw.customers",),
            inputs=(),
            columns=("customer_id", "email", "name"),
            upstream=(),
            downstream=("hub_customer",),
            path="models/staging/stg_customers.sql",
        ),
        SelectableRecord(
            name="stg_orders",
            status="generated",
            generator="staging",
            pattern="staging_pattern",
            paradigm="staging",
            source_inputs=("raw.orders",),
            columns=("order_id", "customer_id", "amount"),
            downstream=("hub_order",),
            path="models/staging/stg_orders.sql",
        ),
        SelectableRecord(
            name="hub_customer",
            status="generated",
            generator="hub",
            pattern="hub_pattern",
            paradigm="dv",
            owner="analytics",
            tags=("finance", "nightly"),
            keys=("customer_id",),
            inputs=("stg_customers",),
            columns=("customer_hk", "customer_id", "email"),
            description="The customer hub.",
            column_descriptions=(("email", "Contact email"),),
            documented=True,
            columns_documented=1,
            columns_total=3,
            has_owner=True,
            has_tests=True,
            upstream=("stg_customers",),
            downstream=("sat_customer_details", "dim_customer"),
            path="models/dv/hub_customer.sql",
        ),
        SelectableRecord(
            name="hub_order",
            status="generated",
            generator="hub",
            pattern="hub_pattern",
            paradigm="dv",
            tags=("nightly",),
            keys=("order_id",),
            inputs=("stg_orders",),
            columns=("order_hk", "order_id"),
            upstream=("stg_orders",),
            downstream=(),
            path="models/dv/hub_order.sql",
        ),
        SelectableRecord(
            name="sat_customer_details",
            status="generated",
            generator="satellite",
            pattern="satellite_pattern",
            paradigm="dv",
            keys=("customer_id",),
            inputs=("hub_customer",),
            columns=("customer_hk", "email", "address", "phone"),
            columns_total=4,
            upstream=("hub_customer", "stg_customers"),
            downstream=("dim_customer",),
            path="models/dv/sat_customer_details.sql",
        ),
        SelectableRecord(
            name="dim_customer",
            status="unmanaged",
            keys=("customer_id",),
            columns=("customer_id", "email", "address"),
            columns_total=3,
            upstream=(
                "sat_customer_details",
                "hub_customer",
                "stg_customers",
            ),
            path="models/marts/dim_customer.sql",
        ),
        SelectableRecord(
            name="customer_mart",
            status="registered",
            classification="mart",
            owner="bi",
            has_owner=True,
            keys=("customer_id",),
            inputs=("dim_customer",),
            columns=("revenue",),
            upstream=("dim_customer",),
            path="models/marts/customer_mart.sql",
        ),
        SelectableRecord(
            name="raw.customers",
            kind="source",
            downstream=("stg_customers",),
            path="models/_generated/staging/sources.yml",
        ),
        SelectableRecord(
            name="money",
            kind="macro",
            package="dbt_utils",
            provenance="package",
            arguments=("amount", "currency"),
            used_by=("stg_orders",),
        ),
        SelectableRecord(
            name="local_macro",
            kind="macro",
            used_by=("hub_customer",),
        ),
        SelectableRecord(
            name="unique_hub_customer",
            kind="test",
            upstream=("hub_customer",),
        ),
    ]


def _select(texts, exclude=(), **kwargs):
    return evaluate_selectors(
        _records(), texts, exclude, mode="select", **kwargs
    )


def _search(texts, exclude=(), **kwargs):
    return evaluate_selectors(
        _records(), texts, exclude, mode="search", **kwargs
    )


# --- Set operators -----------------------------------


def test_comma_intersects_within_one_selector() -> None:
    result = _select(["hub_customer,generator:hub"])
    assert result.names == ["hub_customer"]
    # The plan's example shape: both criteria must hold.
    result = _select(["tag:finance,generator:hub"])
    assert result.names == ["hub_customer"]


def test_separate_arguments_union() -> None:
    result = _select(["hub_customer", "generator:satellite"])
    assert result.names == ["hub_customer", "sat_customer_details"]


def test_comma_is_no_longer_union() -> None:
    # Generate's retired ad hoc reading treated the comma as a
    # union; under the dbt reading this selector intersects to
    # nothing (no hub is named stg_customers) and so errors.
    with pytest.raises(LoomError, match="matched no"):
        _select(["generator:hub,stg_customers"])


def test_exclude_subtracts_and_unmatched_exclude_is_quiet() -> None:
    result = _select(["generator:hub"], ["hub_order"])
    assert result.names == ["hub_customer"]
    assert result.excluded == ["hub_order"]
    result = _select(["generator:hub"], ["no_such_model"])
    assert result.names == ["hub_customer", "hub_order"]


def test_exclude_only_selection() -> None:
    result = _select([], ["is:generated"])
    assert "hub_customer" not in result.names
    assert "dim_customer" in result.names
    assert "dbt_utils.money" in result.names


# --- Bare terms and qualifier exactness (7.3 item 6) -----------


def test_bare_name_exact_glob_package_and_fqn_in_selection() -> None:
    assert _select(["hub_customer"]).names == ["hub_customer"]
    assert _select(["HUB_CUSTOMER"]).names == ["hub_customer"]
    assert _select(["hub_*"]).names == ["hub_customer", "hub_order"]
    # A bare package name selects the package's records (dbt).
    assert _select(["dbt_utils"]).names == ["dbt_utils.money"]
    # The fqn shape: path segments ending in the model name.
    assert _select(["dv.hub_customer"]).names == ["hub_customer"]
    assert _select(["staging/stg_orders"]).names == ["stg_orders"]
    with pytest.raises(LoomError, match="matched no"):
        _select(["hub"])  # no exact name, no glob: nothing


def test_qualifier_values_match_exactly_unless_globbed() -> None:
    with pytest.raises(LoomError, match="matched no"):
        _select(["generator:hu"])
    assert _select(["generator:hu*"]).names == ["hub_customer", "hub_order"]
    assert _select(["owner:analytics"]).names == ["hub_customer"]
    with pytest.raises(LoomError, match="matched no"):
        _select(["owner:analytic"])


def test_compatibility_methods_tag_path_package() -> None:
    assert _select(["tag:finance"]).names == ["hub_customer"]
    assert _select(["tag:nightly"]).names == ["hub_customer", "hub_order"]
    assert _select(["path:models/dv"]).names == [
        "hub_customer",
        "hub_order",
        "sat_customer_details",
    ]
    assert _select(["package:dbt_utils"]).names == ["dbt_utils.money"]


def test_loom_layer_qualifiers() -> None:
    assert _select(["pattern:hub_pattern"]).names == [
        "hub_customer",
        "hub_order",
    ]
    assert _select(["paradigm:dv"]).names == [
        "hub_customer",
        "hub_order",
        "sat_customer_details",
    ]
    assert _select(["classification:mart"]).names == ["customer_mart"]
    assert _select(["column:email"]).names == [
        "stg_customers",
        "hub_customer",
        "sat_customer_details",
        "dim_customer",
    ]
    assert _select(["key:customer_id"]).names == [
        "hub_customer",
        "sat_customer_details",
        "dim_customer",
        "customer_mart",
    ]
    # input: matches declared model inputs and, for a staging
    # model, its declared source (by full identity or by the
    # source's name alone).
    assert _select(["input:stg_customers"]).names == ["hub_customer"]
    # By source name alone, every model the source feeds; by
    # full source identity, just the one table's model.
    assert _select(["input:raw"]).names == ["stg_customers", "stg_orders"]
    assert _select(["input:raw.customers"]).names == ["stg_customers"]
    # doc: is a substring over model and column documentation.
    assert _select(["doc:contact"]).names == ["hub_customer"]
    assert _select(["doc:customer hub"]).names == ["hub_customer"]


def test_is_and_has_qualifiers() -> None:
    assert _select(["is:unmanaged"]).names == ["dim_customer"]
    assert _select(["is:package"]).names == ["dbt_utils.money"]
    assert _select(["is:project,kind:macro"]).names == ["local_macro"]
    assert _select(["has:owner"]).names == ["hub_customer", "customer_mart"]
    assert _select(["has:no-owner,is:generated"]).names == [
        "stg_customers",
        "stg_orders",
        "hub_order",
        "sat_customer_details",
    ]
    assert _select(["has:tests"]).names == ["hub_customer"]
    # M5: the owner/tests hygiene forms are scoped to model
    # records. Tier-2 records carry has_owner/has_tests as
    # false by construction, so unscoped the negative forms
    # matched every macro, source and test in the corpus
    # (on jaffle, has:no-tests headlined 708 - 671 macros,
    # 27 tests, 6 sources - against 4 models). The test
    # record unique_hub_customer and the macros money and
    # local_macro match neither negative form now; the
    # positive forms are unchanged.
    assert _select(["has:no-owner"]).names == [
        "stg_customers",
        "stg_orders",
        "hub_order",
        "sat_customer_details",
        "dim_customer",
    ]
    assert _select(["has:no-tests"]).names == [
        "stg_customers",
        "stg_orders",
        "hub_order",
        "sat_customer_details",
        "dim_customer",
        "customer_mart",
    ]
    assert _select(["has:no-description"]).names == [
        "stg_customers",
        "stg_orders",
        "hub_order",
        "sat_customer_details",
        "dim_customer",
        "customer_mart",
        "raw.customers",
        "dbt_utils.money",
        "local_macro",
        "unique_hub_customer",
    ]
    # hub_customer documents one of three columns; the satellite
    # and the dim document none of theirs.
    assert _select(["has:undocumented-columns"]).names == [
        "hub_customer",
        "sat_customer_details",
        "dim_customer",
    ]


def test_kind_qualifier() -> None:
    assert _select(["kind:macro"]).names == ["dbt_utils.money", "local_macro"]
    assert _select(["kind:source"]).names == ["raw.customers"]
    assert _select(["kind:test"]).names == ["unique_hub_customer"]


# --- Graph forms (7.4) ------------------------------------------


def test_graph_operator_forms() -> None:
    # Upstream runs through the staging model to its source:
    # sources are lineage parents, as in dbt.
    assert set(_select(["+hub_customer"]).names) == {
        "hub_customer",
        "stg_customers",
        "raw.customers",
    }
    # Downstream runs the whole closure: the satellite, the
    # dim, the mart beyond it, and the hub's own test.
    assert set(_select(["hub_customer+"]).names) == {
        "hub_customer",
        "sat_customer_details",
        "dim_customer",
        "customer_mart",
        "unique_hub_customer",
    }
    assert set(_select(["+hub_customer+"]).names) == {
        "hub_customer",
        "stg_customers",
        "raw.customers",
        "sat_customer_details",
        "dim_customer",
        "customer_mart",
        "unique_hub_customer",
    }
    # A macro is never a model's upstream: the hub's closure
    # does not reach the macro the hub calls.
    assert "local_macro" not in _select(["+hub_customer"]).names


def test_graph_depth_forms() -> None:
    assert _select(["1+sat_customer_details"]).names == [
        "hub_customer",
        "sat_customer_details",
    ]
    assert _select(["stg_customers+1"]).names == ["stg_customers", "hub_customer"]
    assert _select(["stg_customers+2"]).names == [
        "stg_customers",
        "hub_customer",
        "sat_customer_details",
        "unique_hub_customer",
    ]
    assert _select(["hub_customer+0"]).names == ["hub_customer"]
    # The unmanaged dim declares no inputs; its direct edge is
    # recovered from its upstream closure by reduction, so two
    # steps up reach the hub but not the staging model.
    assert _select(["2+dim_customer"]).names == [
        "hub_customer",
        "sat_customer_details",
        "dim_customer",
    ]
    assert "stg_customers" in _select(["3+dim_customer"]).names


def test_word_form_graph_qualifiers_and_depth_companion() -> None:
    assert set(_select(["upstream:dim_customer"]).names) == {
        "dim_customer",
        "sat_customer_details",
        "hub_customer",
        "stg_customers",
        "raw.customers",
    }
    assert set(_select(["downstream:stg_customers"]).names) == {
        "stg_customers",
        "hub_customer",
        "sat_customer_details",
        "dim_customer",
        "customer_mart",
        "unique_hub_customer",
    }
    assert _select(["downstream:stg_customers,depth:1"]).names == [
        "stg_customers",
        "hub_customer",
    ]
    with pytest.raises(LoomError, match="depth: only applies"):
        parse_branch("generator:hub,depth:2")
    with pytest.raises(LoomError, match="depth:"):
        parse_branch("upstream:hub_customer,depth:two")


def test_at_form_includes_descendants_parents() -> None:
    # @hub_customer: the hub, its full downstream closure, and
    # the descendants' parents - but not the hub's own parents
    # (the staging model and its source stay out).
    assert set(_select(["@hub_customer"]).names) == {
        "hub_customer",
        "sat_customer_details",
        "dim_customer",
        "customer_mart",
        "unique_hub_customer",
    }


def test_macro_impact_is_its_used_by_downstream() -> None:
    # What breaks if the macro changes: the models that call
    # it, and everything downstream of those models.
    assert set(_select(["downstream:local_macro"]).names) == {
        "local_macro",
        "hub_customer",
        "sat_customer_details",
        "dim_customer",
        "customer_mart",
        "unique_hub_customer",
    }
    assert set(_select(["downstream:money"]).names) == {
        "dbt_utils.money",
        "stg_orders",
        "hub_order",
    }


def test_graph_operators_wrap_qualifiers() -> None:
    # Everything downstream of any hub, hubs included.
    assert set(_select(["generator:hub+"]).names) == {
        "hub_customer",
        "hub_order",
        "sat_customer_details",
        "dim_customer",
        "customer_mart",
        "unique_hub_customer",
    }


# --- Errors (7.5 item 4) ----------------------------------------


def test_unmatched_selector_error_names_the_branch() -> None:
    with pytest.raises(LoomError) as error:
        _select(["hub_customer", "generator:mart"])
    assert "--select 'generator:mart' matched no models" in str(error.value)


def test_pool_restricted_branch_must_match_inside_the_pool() -> None:
    pool = {"hub_customer"}
    result = _select(["generator:hub"], pool=pool)
    assert result.names == ["hub_customer"]
    with pytest.raises(LoomError, match="matched no"):
        _select(["generator:satellite"], pool=pool)


def test_unknown_qualifier_is_a_teaching_error() -> None:
    with pytest.raises(LoomError) as error:
        parse_branch("foo:bar")
    message = str(error.value)
    assert "unknown qualifier 'foo:'" in message
    assert "generator:" in message and "key:" in message


def test_config_matching_is_refused_with_the_reason() -> None:
    with pytest.raises(LoomError) as error:
        parse_branch("config.materialized:table")
    assert "config." in str(error.value)
    assert "no dbt config fields" in str(error.value)


def test_malformed_selectors_are_teaching_errors() -> None:
    with pytest.raises(LoomError, match="empty criterion"):
        parse_branch("hub_customer,,generator:hub")
    with pytest.raises(LoomError, match="only a graph operator"):
        parse_branch("+")
    with pytest.raises(LoomError, match="takes one of"):
        parse_branch("kind:widget")
    with pytest.raises(LoomError, match="needs a value"):
        parse_branch("generator:")


# --- Search: bare-term ranking and banding (7.5 items 1-2) ------


def test_search_bare_terms_are_ranked_substring_matches() -> None:
    result = _search(["customer"])
    # Name matches (tier 2 here) rank ahead of field matches
    # (stg_orders carries a customer_id column, tier 3), and
    # Loom-known status breaks ties: the unmanaged dim ranks
    # after the generated models at equal strength.
    names = result.names
    assert names.index("hub_customer") < names.index("dim_customer")
    assert names.index("dim_customer") < names.index("stg_orders")
    assert _search(["hub_customer"]).names[0] == "hub_customer"
    # Exact-name outranks prefix, prefix outranks substring.
    result = _search(["hub"])
    assert result.ranks["hub_customer"] == 1
    assert result.ranks["hub_order"] == 1
    assert result.names.index("hub_customer") < result.names.index("hub_order")


def test_search_bare_term_description_tiers_are_ordered() -> None:
    # Plan 7.5 item 1's ordering: a match in the model's own
    # description text (tier 4) ranks before a match only in
    # a column's description text (tier 5) - the two
    # documentation tiers the implementation had merged.
    records = [
        SelectableRecord(
            name="alpha_model",
            status="generated",
            description="Revenue recognised nightly.",
        ),
        SelectableRecord(
            name="beta_model",
            status="generated",
            column_descriptions=(
                ("rev", "Revenue recognised nightly."),
            ),
        ),
    ]
    result = evaluate_selectors(records, ["revenue"], [], mode="search")
    assert result.names == ["alpha_model", "beta_model"]
    assert result.ranks == {"alpha_model": 4, "beta_model": 5}


def test_search_qualifiers_stay_exact() -> None:
    # Forgiving matching is for bare terms only: a qualifier
    # value is exact in search too.
    assert _search(["generator:hu"]).names == []
    assert _search(["generator:hub"]).names == ["hub_customer", "hub_order"]


def test_banding_full_and_close_matches() -> None:
    result = _search(["key:customer_id,column:address"])
    assert result.banded
    assert result.names == ["sat_customer_details", "dim_customer"]
    close = {match.name: match for match in result.close}
    assert set(close) == {"hub_customer", "customer_mart"}
    assert close["hub_customer"].satisfied == ["key:customer_id"]
    assert close["hub_customer"].missing == ["column:address"]


def test_banding_keeps_non_shape_criteria_strict() -> None:
    result = _search(["owner:analytics,key:customer_id,column:address"])
    assert result.banded
    assert result.names == []
    assert [match.name for match in result.close] == ["hub_customer"]


def test_single_shape_qualifier_does_not_band() -> None:
    result = _search(["key:customer_id"])
    assert not result.banded
    assert result.close == []
    assert "hub_customer" in result.names


def test_guardrail_selection_never_bands() -> None:
    # The identical string: search bands (above), selection
    # takes only the full matches.
    result = _select(["key:customer_id,column:address"])
    assert result.names == ["sat_customer_details", "dim_customer"]
    assert not result.banded
    assert result.close == []


def test_zero_result_diagnosis_names_the_culprit() -> None:
    diagnosis = diagnose_empty_branch(
        _records(), "generator:hub,owner:nobody"
    )
    assert diagnosis.culprit == "owner:nobody"
    assert "analytics" in diagnosis.facets["owner"]
    assert "hub" in diagnosis.facets["generator"]


def test_zero_result_bare_term_suggests_close_names() -> None:
    diagnosis = diagnose_empty_branch(_records(), "hub_customr")
    assert diagnosis.culprit == "hub_customr"
    assert diagnosis.suggestions == ["hub_customer"]


def test_zero_result_diagnosis_lists_doc_has_and_path_facets() -> None:
    # Plan 7.5 item 4 promises the facet values that exist
    # for the qualifiers used; doc:, has: and path: were the
    # families the diagnosis did not cover.
    diagnosis = diagnose_empty_branch(
        _records(), "doc:customer,owner:nobody"
    )
    assert diagnosis.culprit == "owner:nobody"
    # doc: searches description text, so the values that
    # exist are the description texts themselves - the
    # model's own and its columns'.
    assert "The customer hub." in diagnosis.facets["doc"]
    assert "Contact email" in diagnosis.facets["doc"]

    diagnosis = diagnose_empty_branch(
        _records(), "has:owner,owner:nobody"
    )
    assert diagnosis.culprit == "owner:nobody"
    # has: has no field to read: the values that exist are
    # the named forms at least one record satisfies - here,
    # all seven.
    assert diagnosis.facets["has"] == [
        "description",
        "no-description",
        "no-owner",
        "no-tests",
        "owner",
        "tests",
        "undocumented-columns",
    ]

    diagnosis = diagnose_empty_branch(
        _records(), "path:models/dv,owner:nobody"
    )
    assert diagnosis.culprit == "owner:nobody"
    assert "models/dv/hub_customer.sql" in diagnosis.facets["path"]
    assert (
        "models/staging/stg_customers.sql" in diagnosis.facets["path"]
    )


# --- Named selectors ---------------------------------------


def _write_selectors(project: Path) -> None:
    (project / "selectors.yml").write_text(
        "selectors:\n"
        "  - name: nightly_hubs\n"
        "    definition:\n"
        "      intersection:\n"
        "        - method: tag\n"
        "          value: finance\n"
        "        - generator:hub\n"
        "  - name: dv_and_macros\n"
        "    definition:\n"
        "      union:\n"
        "        - paradigm:dv\n"
        "        - method: resource_type\n"
        "          value: macro\n"
        "      exclude:\n"
        "        - hub_order\n",
        encoding="utf-8",
    )


def test_named_selectors_translate_and_evaluate(tmp_path: Path) -> None:
    _write_selectors(tmp_path)
    named = load_named_selectors(tmp_path)
    assert set(named) == {"nightly_hubs", "dv_and_macros"}
    result = _select(["selector:nightly_hubs"], named=named)
    assert result.names == ["hub_customer"]
    result = _select(["selector:dv_and_macros"], named=named)
    assert result.names == [
        "hub_customer",
        "sat_customer_details",
        "dbt_utils.money",
        "local_macro",
    ]


def test_named_selector_graph_flags(tmp_path: Path) -> None:
    (tmp_path / "selectors.yml").write_text(
        "selectors:\n"
        "  - name: hub_impact\n"
        "    definition:\n"
        "      method: tag\n"
        "      value: finance\n"
        "      children: true\n",
        encoding="utf-8",
    )
    named = load_named_selectors(tmp_path)
    result = _select(["selector:hub_impact"], named=named)
    assert "sat_customer_details" in result.names
    assert "hub_customer" in result.names


def test_unknown_named_selector_lists_the_defined_names(
    tmp_path: Path,
) -> None:
    _write_selectors(tmp_path)
    named = load_named_selectors(tmp_path)
    with pytest.raises(LoomError) as error:
        _select(["selector:nope"], named=named)
    assert "nightly_hubs" in str(error.value)


def test_no_selectors_file_means_no_named_selectors(tmp_path: Path) -> None:
    assert load_named_selectors(tmp_path) == {}
