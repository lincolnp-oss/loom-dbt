"""The shared YAML loader prefers libyaml's C loader.

Every command loads the project's metadata through
``_UniqueKeySafeLoader``; with the pure-Python SafeLoader that parse
dominated wall time at scale (~55% of `validate` at 5k models in the
measurements). The C loader must be behaviour-identical for
Loom's inputs - same values, same duplicate-key rejection - with the
pure-Python loader as the fallback when libyaml is unavailable.
"""
from __future__ import annotations

import pytest
import yaml

from loom.project_files import _UniqueKeyLoaderBase, _UniqueKeySafeLoader


def _load(text: str):
    return yaml.load(text, Loader=_UniqueKeySafeLoader)


def test_loader_base_prefers_c_loader_when_available() -> None:
    expected = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    assert _UniqueKeyLoaderBase is expected
    assert issubclass(_UniqueKeySafeLoader, expected)


def test_loader_parses_representative_metadata() -> None:
    document = _load(
        "name: stg_orders\n"
        "count: 3\n"
        "ratio: 1.5\n"
        "flag: true\n"
        "nothing: null\n"
        "joins:\n"
        "  - model: dim_customer\n"
        "    on: [customer_id]\n"
        "nested:\n"
        "  on: yes\n"
    )
    assert document["name"] == "stg_orders"
    assert document["count"] == 3
    assert document["ratio"] == 1.5
    assert document["flag"] is True
    assert document["nothing"] is None
    # YAML 1.1 plain-scalar resolution matches SafeLoader exactly: the
    # bare key `on` arrives as boolean True (inventory restores such
    # keys downstream), in flow and block mappings alike.
    assert document["joins"] == [{"model": "dim_customer", True: ["customer_id"]}]
    assert document["nested"] == {True: True}


def test_loader_rejects_duplicate_keys() -> None:
    with pytest.raises(yaml.YAMLError, match="duplicate key 'name'"):
        _load("name: first\nname: second\n")


def test_loader_rejects_unsafe_tags() -> None:
    with pytest.raises(yaml.YAMLError):
        _load("value: !!python/object/apply:os.system ['echo hi']\n")


def test_loader_honours_merge_keys() -> None:
    # The regression: the duplicate-key constructor
    # originally replaced SafeConstructor.construct_mapping
    # without its flatten_mapping step, so every merge key was
    # rejected ("could not determine a constructor for the tag
    # 'tag:yaml.org,2002:merge'"). Merged values must take
    # effect, not merely parse.
    document = _load(
        "defaults: &defaults\n"
        "  description: templated\n"
        "  owner: analytics\n"
        "model:\n"
        "  <<: *defaults\n"
        "  name: stg_orders\n"
    )
    assert document["model"] == {
        "description": "templated",
        "owner": "analytics",
        "name": "stg_orders",
    }


def test_loader_merge_explicit_key_overrides_merged() -> None:
    document = _load(
        "defaults: &defaults\n"
        "  description: templated\n"
        "  owner: analytics\n"
        "model:\n"
        "  <<: *defaults\n"
        "  description: specific\n"
    )
    assert document["model"]["description"] == "specific"
    assert document["model"]["owner"] == "analytics"


def test_loader_merge_sequence_first_source_wins() -> None:
    document = _load(
        "a: &a\n"
        "  key: from_a\n"
        "  only_a: 1\n"
        "b: &b\n"
        "  key: from_b\n"
        "  only_b: 2\n"
        "model:\n"
        "  <<: [*a, *b]\n"
    )
    assert document["model"] == {
        "key": "from_a",
        "only_a": 1,
        "only_b": 2,
    }


def test_loader_merge_still_rejects_duplicate_own_keys() -> None:
    # Duplicate detection scopes to the mapping's own keys: an
    # own key repeated in a mapping that also merges is still
    # an error (the override case above is one own key).
    with pytest.raises(yaml.YAMLError, match="duplicate key 'name'"):
        _load(
            "defaults: &defaults\n"
            "  owner: analytics\n"
            "model:\n"
            "  <<: *defaults\n"
            "  name: first\n"
            "  name: second\n"
        )


def test_loader_merge_template_duplicates_still_rejected() -> None:
    # An anchored template is constructed in its own right at
    # its definition site, so duplicates inside it are caught
    # there even though the template is also merged elsewhere.
    with pytest.raises(yaml.YAMLError, match="duplicate key 'owner'"):
        _load(
            "defaults: &defaults\n"
            "  owner: first\n"
            "  owner: second\n"
            "model:\n"
            "  <<: *defaults\n"
        )
