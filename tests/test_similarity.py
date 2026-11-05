"""Shape similarity:
fingerprint derivation, the documented weighted overlap
(keys 5, inputs 4, columns 3, generator family 1, with the key
gate), banded ranking by fingerprint score, and --similar-to
search by example.

The fixture set is purpose-built with known shape
relationships: same grain
different attributes, same attributes different grain, same
shape different generator family, partial column overlap, and
a partial-shape reference model for --similar-to. Expected
rankings and shares / differs text are asserted exactly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from loom.cli import main
from loom.errors import LoomError
from loom.inventory_corpus import CorpusColumn, ModelRecord
from loom.selection import SelectableRecord, evaluate_selectors
from loom.similarity import (
    compare,
    fingerprint_from_record,
    fingerprint_from_shape,
    rank_similar,
    similar_to,
)
from metadata_helpers import write_model_manifest, write_pattern_instance

# --- The purpose-built fixture set ------------------------------
#
# Reference shape: a Data Vault satellite over the customer
# hub - grain customer_id, fed by hub_customer, carrying
# customer_id, email, address, phone. Total weight 5+4+3+1=13.

_REFERENCE = SelectableRecord(
    name="sat_customer_details",
    status="generated",
    generator="satellite",
    paradigm="dv",
    keys=("customer_id",),
    inputs=("hub_customer",),
    columns=("customer_id", "email", "address", "phone"),
)

# Same shape, different generator family: everything shared
# except the family. Score (5+4+3+0)/13 = 12/13.
_SAME_SHAPE_OTHER_FAMILY = SelectableRecord(
    name="dim_customer_full",
    status="generated",
    generator="dimension",
    paradigm="kimball",
    keys=("customer_id",),
    inputs=("hub_customer",),
    columns=("customer_id", "email", "address", "phone"),
)

# Same grain, different attributes: keys, inputs and family
# shared, two of four columns. Score (5+4+1.5+1)/13 = 11.5/13.
_SAME_GRAIN_OTHER_ATTRIBUTES = SelectableRecord(
    name="sat_customer_contact",
    status="generated",
    generator="satellite",
    paradigm="dv",
    keys=("customer_id",),
    inputs=("hub_customer",),
    columns=("customer_id", "email", "city"),
)

# Partial column overlap, different input: keys and family
# shared, one of four columns. Score (5+0+0.75+1)/13 = 6.75/13.
_PARTIAL_OVERLAP = SelectableRecord(
    name="sat_customer_region",
    status="generated",
    generator="satellite",
    paradigm="dv",
    keys=("customer_id",),
    inputs=("stg_customers",),
    columns=("customer_id", "region"),
)

# Same attributes, different grain: inputs, family and three
# of four columns shared, but the grain is order_id. Raw score
# (0+4+2.25+1)/13 = 7.25/13 - higher than the partial-overlap
# model above, yet the key gate ranks it below every candidate
# that shares the grain.
_OTHER_GRAIN = SelectableRecord(
    name="sat_order_details",
    status="generated",
    generator="satellite",
    paradigm="dv",
    keys=("order_id",),
    inputs=("hub_customer",),
    columns=("order_id", "email", "address", "phone"),
)

# Nothing shared: not a similarity at all.
_UNRELATED = SelectableRecord(
    name="stg_warehouses",
    status="generated",
    generator="staging",
    paradigm="staging",
    keys=("warehouse_id",),
    source_inputs=("raw.warehouses",),
    columns=("warehouse_id", "name"),
)

_CANDIDATES = [
    _SAME_SHAPE_OTHER_FAMILY,
    _SAME_GRAIN_OTHER_ATTRIBUTES,
    _PARTIAL_OVERLAP,
    _OTHER_GRAIN,
    _UNRELATED,
]


def _ranked() -> tuple[list[str], dict[str, object]]:
    target = fingerprint_from_record(_REFERENCE)
    ranked = rank_similar(
        target,
        [(record.name, fingerprint_from_record(record)) for record in _CANDIDATES],
        include_extras=True,
    )
    return [item.name for item in ranked], {item.name: item for item in ranked}  # type: ignore[dict-item]


def test_fixture_ranking_is_exact() -> None:
    order, ranked = _ranked()
    # The unrelated model shares nothing and is excluded. The
    # different-grain model out-scores the partial-overlap
    # model on raw overlap but the key gate demotes it below
    # every candidate sharing the grain.
    assert order == [
        "dim_customer_full",
        "sat_customer_contact",
        "sat_customer_region",
        "sat_order_details",
    ]
    assert ranked["dim_customer_full"].score == pytest.approx(12 / 13)  # type: ignore[attr-defined]
    assert ranked["sat_customer_contact"].score == pytest.approx(11.5 / 13)  # type: ignore[attr-defined]
    assert ranked["sat_customer_region"].score == pytest.approx(6.75 / 13)  # type: ignore[attr-defined]
    assert ranked["sat_order_details"].score == pytest.approx(7.25 / 13)  # type: ignore[attr-defined]
    assert ranked["sat_order_details"].gate == 2  # type: ignore[attr-defined]
    assert ranked["sat_customer_region"].gate == 0  # type: ignore[attr-defined]


def test_shares_and_differs_text_is_exact() -> None:
    _order, ranked = _ranked()
    contact = ranked["sat_customer_contact"]
    assert contact.shares == (  # type: ignore[attr-defined]
        "keys: customer_id (1 of 1)",
        "inputs: hub_customer (1 of 1)",
        "columns: customer_id, email (2 of 4)",
        "generator: satellite (family dv)",
    )
    assert contact.differs == (  # type: ignore[attr-defined]
        "columns: missing address, phone",
        "columns: extra city",
    )
    other_family = ranked["dim_customer_full"]
    assert other_family.shares == (  # type: ignore[attr-defined]
        "keys: customer_id (1 of 1)",
        "inputs: hub_customer (1 of 1)",
        "columns: customer_id, email, address, phone (4 of 4)",
    )
    assert other_family.differs == ("generator family: kimball, not dv",)  # type: ignore[attr-defined]
    other_grain = ranked["sat_order_details"]
    assert other_grain.differs == (  # type: ignore[attr-defined]
        "keys: missing customer_id",
        "keys: extra order_id",
        "columns: missing customer_id",
        "columns: extra order_id",
    )
    partial = ranked["sat_customer_region"]
    assert partial.differs == (  # type: ignore[attr-defined]
        "inputs: missing hub_customer",
        "inputs: extra stg_customers",
        "columns: missing email, address, phone",
        "columns: extra region",
    )


def test_fingerprint_from_corpus_model_record() -> None:
    model = ModelRecord(
        name="sat_customer_details",
        status="generated",
        generator="satellite",
        paradigm="dv",
        keys=("customer_id",),
        inputs=("hub_customer",),
        source_inputs=("crm.customers",),
        columns=(CorpusColumn("customer_id"), CorpusColumn("email")),
        columns_partial=True,
    )
    fingerprint = fingerprint_from_record(model)
    assert fingerprint.family == "dv"
    assert fingerprint.inputs == ("hub_customer", "crm.customers")
    assert fingerprint.columns == ("customer_id", "email")
    assert fingerprint.partial_components == ("columns (partial list)",)


def test_source_input_matches_by_source_name() -> None:
    # The input identity normalisation the input: qualifier
    # uses: a query naming the source matches a model fed by
    # one of its tables.
    target = fingerprint_from_shape(inputs=["crm"])
    candidate = fingerprint_from_record(
        SelectableRecord(
            name="stg_customers",
            source_inputs=("crm.customers",),
            columns=("customer_id",),
        )
    )
    result = compare(target, candidate)
    assert result.shares == ("inputs: crm (1 of 1)",)
    assert result.score == 1.0


def test_generator_value_implies_its_family() -> None:
    target = fingerprint_from_shape(generator="hub")
    assert target.family == "dv"
    candidate = fingerprint_from_record(_SAME_GRAIN_OTHER_ATTRIBUTES)
    result = compare(target, candidate)
    # A satellite is not the hub generator, but it is the same
    # family - and the family is the scored component.
    assert result.shares == ("generator family: dv",)
    assert result.score == 1.0


# --- --similar-to over records, incl. a partial reference -------


def test_similar_to_ranks_and_excludes_the_reference() -> None:
    records = [_REFERENCE, *_CANDIDATES]
    reference, target, ranked, omitted = similar_to(
        records, "SAT_CUSTOMER_DETAILS"
    )
    assert reference.name == "sat_customer_details"
    assert target.keys == ("customer_id",)
    assert [item.name for item in ranked] == [
        "dim_customer_full",
        "sat_customer_contact",
        "sat_customer_region",
        "sat_order_details",
    ]
    # stg_warehouses shares nothing with the reference and
    # is counted as omitted, not silently dropped.
    assert omitted == 1


def test_similar_to_partial_reference_scores_only_derivable_components() -> None:
    # A partial-shape reference: an unmanaged model with no
    # derivable keys and no generator family, a partial column
    # list, one input. Scoring covers inputs and columns only
    # (weights 4 and 3); the underivable components are named,
    # never scored as zero.
    partial = SelectableRecord(
        name="legacy_customer_export",
        status="unmanaged",
        inputs=("hub_customer",),
        columns=("email", "address"),
        columns_partial=True,
    )
    full_shape = SelectableRecord(
        name="sat_customer_details",
        status="generated",
        generator="satellite",
        paradigm="dv",
        keys=("customer_id",),
        inputs=("hub_customer",),
        columns=("customer_id", "email", "address", "phone"),
    )
    input_only = SelectableRecord(
        name="hub_customer",
        status="generated",
        generator="hub",
        paradigm="dv",
        keys=("customer_id",),
        inputs=("stg_customers",),
        columns=("customer_hk", "customer_id"),
    )
    reference, target, ranked, omitted = similar_to(
        [partial, full_shape, input_only], "legacy_customer_export"
    )
    assert reference.name == "legacy_customer_export"
    assert target.partial_components == (
        "keys",
        "columns (partial list)",
        "generator family",
    )
    assert [item.name for item in ranked] == ["sat_customer_details"]
    assert ranked[0].score == 1.0
    # hub_customer shares neither the input nor a scored
    # column, so it is not a similarity here - and it is
    # counted as omitted, not silently dropped.
    assert all(item.name != "hub_customer" for item in ranked)
    assert omitted == 1


def test_similar_to_errors_teach() -> None:
    with pytest.raises(LoomError, match="no record named"):
        similar_to([_REFERENCE], "hub_customer")
    macro = SelectableRecord(name="cents_to_dollars", kind="macro")
    with pytest.raises(LoomError, match="not a model"):
        similar_to([_REFERENCE, macro], "cents_to_dollars")
    shapeless = SelectableRecord(name="empty_model", status="unmanaged")
    with pytest.raises(LoomError, match="no shape can be derived"):
        similar_to([shapeless], "empty_model")


# --- Banding ranks by fingerprint, not by criterion count -------


def _banding_records() -> list[SelectableRecord]:
    return [
        SelectableRecord(
            name="full_match",
            status="generated",
            generator="satellite",
            paradigm="dv",
            keys=("customer_id",),
            columns=("email", "address"),
        ),
        SelectableRecord(
            name="key_only",
            status="generated",
            generator="satellite",
            paradigm="dv",
            keys=("customer_id",),
            columns=("customer_id", "phone"),
        ),
        SelectableRecord(
            name="two_columns_no_key",
            status="generated",
            generator="dimension",
            paradigm="kimball",
            keys=("order_id",),
            columns=("email", "address"),
        ),
        SelectableRecord(
            name="one_column_no_key",
            status="generated",
            generator="dimension",
            paradigm="kimball",
            keys=("order_id",),
            columns=("email", "city"),
        ),
    ]


def test_banding_close_matches_rank_by_fingerprint_score() -> None:
    result = evaluate_selectors(
        _banding_records(),
        ["key:customer_id,column:email,column:address"],
        mode="search",
    )
    assert result.banded
    assert result.names == ["full_match"]
    # Per-criterion counting would rank
    # two_columns_no_key first (two satisfied criteria). The
    # fingerprint ranking puts the model sharing the grain
    # first despite satisfying one criterion, and the key gate
    # holds both no-key models below it.
    assert [match.name for match in result.close] == [
        "key_only",
        "two_columns_no_key",
        "one_column_no_key",
    ]
    key_only = result.close[0]
    assert key_only.satisfied == ["key:customer_id"]
    assert key_only.shares == ["keys: customer_id (1 of 1)"]
    assert key_only.differs == ["columns: missing email, address"]
    assert key_only.score == pytest.approx(5 / 8)
    assert result.close[1].score == pytest.approx(3 / 8)
    assert result.close[1].gate == 2


def test_selection_stays_strict_on_the_banding_fixtures() -> None:
    # The guardrail over these fixtures: the identical
    # string under selection takes only the full match.
    result = evaluate_selectors(
        _banding_records(),
        ["key:customer_id,column:email,column:address"],
        mode="select",
    )
    assert result.names == ["full_match"]
    assert result.close == []
    assert not result.banded


# --- CLI: --similar-to ------------------------------------------

_FIELDS = {"description": "Customer domain", "owner": "analytics"}


def _write_cli_project(project: Path) -> None:
    models = project / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "sources.yml").write_text(
        "version: 2\n"
        "sources:\n"
        "  - name: raw\n"
        "    tables:\n"
        "      - name: customers\n",
        encoding="utf-8",
    )
    write_pattern_instance(
        project,
        "stg_customers",
        "staging",
        {
            **_FIELDS,
            "source_ref": {"name": "raw", "table": "customers"},
            "columns": [
                {"source": "id", "alias": "customer_id"},
                {"source": "email", "alias": "email"},
                {"source": "addr", "alias": "address"},
            ],
        },
        pattern="staging",
    )
    write_pattern_instance(
        project,
        "hub_customer",
        "hub",
        {
            **_FIELDS,
            "input_models": ["stg_customers"],
            "business_key": "customer_id",
        },
        pattern="hubs",
    )
    write_pattern_instance(
        project,
        "sat_customer_details",
        "satellite",
        {
            **_FIELDS,
            "parent": {"model": "hub_customer", "key": "customer_id"},
            "input_models": ["stg_customers"],
            "attribute_columns": ["email", "address"],
            "record_source": "crm",
        },
        pattern="satellites",
    )
    write_model_manifest(
        project, ["stg_customers", "hub_customer", "sat_customer_details"]
    )


def test_search_similar_to_json_and_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_cli_project(tmp_path)
    assert (
        main(
            [
                "search",
                "--similar-to",
                "hub_customer",
                "--project-dir",
                str(tmp_path),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["similar_to"]["name"] == "hub_customer"
    assert payload["similar_to"]["fingerprint"]["keys"] == ["customer_id"]
    names = [item["name"] for item in payload["results"]]
    assert "hub_customer" not in names
    assert "sat_customer_details" in names
    satellite = next(
        item for item in payload["results"] if item["name"] == "sat_customer_details"
    )
    assert satellite["band"] == "similar"
    assert satellite["similarity"]["score"] > 0
    assert any(
        share.startswith("keys: customer_id")
        for share in satellite["similarity"]["shares"]
    )

    assert (
        main(
            [
                "search",
                "--similar-to",
                "hub_customer",
                "--project-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Similar to hub_customer" in out
    assert "Reference shape:" in out
    assert "shares:" in out
    assert "shape overlap" in out


def test_search_similar_to_combinations_and_unknown_model_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_cli_project(tmp_path)
    assert (
        main(
            [
                "search",
                "key:customer_id",
                "--similar-to",
                "hub_customer",
                "--project-dir",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert "cannot be combined" in capsys.readouterr().err
    assert (
        main(
            [
                "search",
                "--similar-to",
                "hub_nobody",
                "--project-dir",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert "no record named" in capsys.readouterr().err
    assert (
        main(
            [
                "search",
                "--similar-to",
                "hub_customer",
                "--project-dir",
                str(tmp_path),
                "--select",
                "key:customer_id",
            ]
        )
        == 1
    )
    assert "cannot be combined" in capsys.readouterr().err
