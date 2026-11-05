import importlib
import re
import tomllib
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def test_distribution_metadata_declares_backend_cli_and_supported_python() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)

    assert config["build-system"]["build-backend"] == "uv_build"
    # The distribution is loom-dbt 0.1.0 (Alpha); the import
    # package and the CLI stay loom.
    assert config["project"]["name"] == "loom-dbt"
    assert config["project"]["version"] == "0.1.0"
    assert config["tool"]["uv"]["build-backend"]["module-name"] == "loom"
    assert config["project"]["scripts"]["loom"] == "loom.cli:main"
    assert config["project"]["requires-python"] == ">=3.11,<3.15"
    runtime_dependencies = config["project"]["dependencies"]
    assert runtime_dependencies == ["PyYAML>=6.0,<7", "wcwidth>=0.2,<1"]
    dev = config["dependency-groups"]["dev"]
    integration = config["dependency-groups"]["integration"]
    assert any(dependency.startswith("pytest-cov>=7.1.0") for dependency in dev)
    assert not any(
        dependency.lower().startswith(("dbt", "jsonschema", "pytest-cov"))
        for dependency in runtime_dependencies
    )
    assert any(dependency.startswith("duckdb>=1.2,<2") for dependency in dev)
    assert any(dependency.startswith("jinja2>=3.1,<4") for dependency in dev)
    assert any(dependency.startswith("pytest>=8.0") for dependency in dev)
    assert any(
        dependency.startswith("dbt-duckdb>=1.9,<2")
        for dependency in integration
        if isinstance(dependency, str)
    )
    assert {"include-group": "dev"} in integration
    assert not any(
        dependency.startswith("duckdb")
        for dependency in integration
        if isinstance(dependency, str)
    )
    assert not any(
        dependency.lower().startswith("duckdb")
        for dependency in config["project"]["dependencies"]
    )


def test_distribution_only_packages_loom() -> None:
    assert (ROOT / "src" / "loom" / "__init__.py").is_file()
    assert not (ROOT / "loom").exists()


def test_generated_output_names_are_not_tool_branded() -> None:
    # Negative pin: generated names use the `__` system
    # namespace (`__loaded_at`, `__rn`, ...) and the `_generated/`
    # layout; the tool-branded development names must not creep
    # back into the product sources or the pinned generated
    # outputs. (This test names the old tokens to forbid them; it
    # is the one deliberate occurrence in the tree's test sources.)
    retired = (
        "loom_loaded_at",
        "loom_rn",
        "loom_source_ordinal",
        "loom_window",
        "loom_link_guard",
        "loom_guard",
        "loom_invalid_keys",
        "loom_unmatched_keys",
        "_loom_staging",
        "_loom_tests",
        "loom_sources.yml",
    )
    offenders = []
    for base in (ROOT / "src" / "loom", ROOT / "tests" / "goldens", ROOT / "tests" / "fixtures"):
        for path in base.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                text = path.read_text(encoding="utf-8", errors="ignore")
                for token in retired:
                    if token in text:
                        offenders.append(f"{path.relative_to(ROOT)}: {token}")
    assert not offenders, offenders


def test_internal_register_references_do_not_return() -> None:
    # Negative pin: the internal findings register and its
    # item numbers are programme-internal; they were scrubbed
    # from the release tree and must not creep back into shipped
    # text. (This test names the tokens to forbid them; it is
    # the one deliberate occurrence, and it excludes itself.)
    pattern = re.compile(r"FI-\d+")
    roots = (
        ROOT / "src",
        ROOT / "tests",
        ROOT / "docs",
        ROOT / "examples",
        ROOT / ".github",
    )
    files = [
        path
        for base in roots
        for path in base.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    files += [
        ROOT / name
        for name in ("README.md", "CONTRIBUTING.md", "CHANGELOG.md", "SUPPORT.md")
    ]
    own = Path(__file__).resolve()
    offenders = []
    for path in files:
        if path.resolve() == own:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text) or "future-improvements" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, offenders


def test_distribution_declares_mit_license_and_release_artefacts() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)

    assert config["project"]["license"] == "MIT"
    assert config["project"]["urls"]["Repository"] == (
        "https://github.com/lincolnp-oss/loom-dbt"
    )
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert license_text.startswith("MIT License")
    for artefact in ("SUPPORT.md", "CONTRIBUTING.md", "CHANGELOG.md"):
        assert (ROOT / artefact).is_file(), artefact
    for template in (
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
    ):
        assert (ROOT / template).is_file(), template
