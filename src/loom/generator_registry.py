from __future__ import annotations

from dataclasses import dataclass

from loom.errors import LoomError


@dataclass(frozen=True)
class GeneratorSpec:
    name: str
    output_roots: tuple[str, ...]
    data_test_provider: str | None = None
    paradigm: str | None = None


GENERATOR_PARADIGM: dict[str, str] = {
    "staging": "staging",
    "hub": "dv",
    "link": "dv",
    "scd2_dimension": "kimball",
    "fact": "kimball",
    "dimension": "kimball",
    "bronze": "medallion",
    "silver": "medallion",
    "gold": "medallion",
    "satellite": "dv",
    "pit": "dv",
    "reference_hub": "dv",
    "reference_table": "dv",
    "bridge": "dv",
    "obt": "obt",
    "normalized_table": "threenf",
    "view": "threenf",
    "mart": "hybrid",
}


GENERATOR_REGISTRATIONS = (
    GeneratorSpec("staging", ("models", "models/_generated/staging"), paradigm="staging"),
    GeneratorSpec(
        "hub",
        ("models",),
        data_test_provider="hub",
        paradigm="dv",
    ),
    GeneratorSpec("link", ("models",), data_test_provider="link", paradigm="dv"),
    GeneratorSpec(
        "satellite",
        ("models", "models"),
        data_test_provider="satellite",
        paradigm="dv",
    ),
    GeneratorSpec(
        "pit",
        ("models",),
        data_test_provider="pit",
        paradigm="dv",
    ),
    GeneratorSpec(
        "reference_hub",
        ("models",),
        data_test_provider="reference_hub",
        paradigm="dv",
    ),
    GeneratorSpec(
        "reference_table",
        ("models",),
        data_test_provider="reference_table",
        paradigm="dv",
    ),
    GeneratorSpec(
        "bridge",
        ("models",),
        data_test_provider="bridge",
        paradigm="dv",
    ),
    GeneratorSpec(
        "scd2_dimension",
        ("snapshots", "models"),
        data_test_provider="scd2_dimension",
        paradigm="kimball",
    ),
    GeneratorSpec("fact", ("models",), data_test_provider="fact", paradigm="kimball"),
    GeneratorSpec(
        "dimension",
        ("models",),
        data_test_provider="dimension",
        paradigm="kimball",
    ),
    GeneratorSpec("bronze", ("models",), paradigm="medallion"),
    GeneratorSpec(
        "silver",
        ("models",),
        data_test_provider="silver",
        paradigm="medallion",
    ),
    GeneratorSpec(
        "gold",
        ("models",),
        data_test_provider="gold",
        paradigm="medallion",
    ),
    GeneratorSpec("obt", ("models",), paradigm="obt"),
    GeneratorSpec(
        "normalized_table",
        ("models",),
        data_test_provider="normalized_table",
        paradigm="threenf",
    ),
    GeneratorSpec("view", ("models",), paradigm="threenf"),
    GeneratorSpec("mart", ("models",), data_test_provider="mart", paradigm="hybrid"),
)


def build_generator_registry(
    registrations: tuple[GeneratorSpec, ...] | list[GeneratorSpec],
) -> dict[str, GeneratorSpec]:
    """Build the private registry and reject ambiguous generator declarations."""
    registry: dict[str, GeneratorSpec] = {}
    for spec in registrations:
        if spec.name in registry:
            raise LoomError(f"duplicate generator registration {spec.name!r}")
        if not spec.name or not spec.output_roots:
            raise LoomError("generator registrations require name and output roots")
        registry[spec.name] = spec
    return registry


GENERATOR_REGISTRY = build_generator_registry(GENERATOR_REGISTRATIONS)
