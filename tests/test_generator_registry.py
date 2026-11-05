from __future__ import annotations

import pytest

from loom.errors import LoomError
from loom.generator_registry import (
    GENERATOR_PARADIGM,
    GENERATOR_REGISTRATIONS,
    GENERATOR_REGISTRY,
    GeneratorSpec,
    build_generator_registry,
)


def test_registered_generators_have_owned_outputs() -> None:
    assert set(GENERATOR_REGISTRY) == {
        registration.name for registration in GENERATOR_REGISTRATIONS
    }
    for spec in GENERATOR_REGISTRY.values():
        assert spec.output_roots


def test_generator_registration_seam_accepts_a_new_internal_generator() -> None:
    demo = GeneratorSpec(
        "demo",
        ("models",),
        data_test_provider=None,
    )
    registry = build_generator_registry([*GENERATOR_REGISTRATIONS, demo])
    assert registry["demo"] == demo


def test_duplicate_generator_registration_is_rejected() -> None:
    with pytest.raises(LoomError, match="duplicate generator registration 'hub'"):
        build_generator_registry([*GENERATOR_REGISTRATIONS, GENERATOR_REGISTRY["hub"]])


def test_generator_paradigm_lookup_exists() -> None:
    assert GENERATOR_PARADIGM["staging"] == "staging"
    assert GENERATOR_PARADIGM["hub"] == "dv"
    assert GENERATOR_PARADIGM["link"] == "dv"
    assert GENERATOR_PARADIGM["scd2_dimension"] == "kimball"
    assert GENERATOR_PARADIGM["fact"] == "kimball"
    assert GENERATOR_PARADIGM["dimension"] == "kimball"
    assert GENERATOR_PARADIGM["bronze"] == "medallion"
    assert GENERATOR_PARADIGM["silver"] == "medallion"
    assert GENERATOR_PARADIGM["gold"] == "medallion"
    assert GENERATOR_PARADIGM["obt"] == "obt"


def test_generator_spec_includes_paradigm() -> None:
    for spec in GENERATOR_REGISTRATIONS:
        assert spec.paradigm is not None
        assert spec.paradigm in GENERATOR_PARADIGM.values()


def test_generator_spec_accepts_paradigm() -> None:
    demo = GeneratorSpec(
        "demo",
        ("models",),
        paradigm="medallion",
    )
    assert demo.paradigm == "medallion"
