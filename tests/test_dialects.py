from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from loom.dialects import (
    DIALECTS,
    ADAPTER_ALIASES,
    ADAPTER_TO_DIALECT,
    Dialect,
    canonical_adapter_name,
    detect_dialect,
    detect_profile_adapter,
    get_dialect,
)
from loom.errors import LoomError


class TestDialectRegistry:
    """Test that all 8 dialects are defined with correct metadata."""

    EXPECTED_DIALECTS = {
        "duckdb",
        "snowflake",
        "bigquery",
        "postgres",
        "redshift",
        "databricks",
        "azuresynapse",
        "clickhouse",
    }

    def test_all_eight_dialects_defined(self) -> None:
        assert set(DIALECTS.keys()) == self.EXPECTED_DIALECTS

    def test_all_dialects_are_frozen_dataclasses(self) -> None:
        for name, dialect in DIALECTS.items():
            assert isinstance(dialect, Dialect)
            # Verify frozen (immutable) by attempting to modify a field
            with pytest.raises(Exception):
                dialect.name = "modified"

    def test_dialect_spelling_tables(self) -> None:
        # The per-dialect record pins (hash, deleted-flag,
        # cursor, timestamp, link-guard, snapshot tables) collapsed
        # into one loop. Every expected value below is copied
        # verbatim from the tests it replaces - only the pytest node
        # granularity is lost, no assertion is dropped.
        hash_expected = {
            "duckdb": ("sha256", "LOWER(sha256({key}))", "VARCHAR"),
            "snowflake": ("SHA2", "LOWER(SHA2({key}))", None),
            "bigquery": (
                "SHA256",
                "LOWER(TO_HEX(SHA256(CAST({key} AS BYTES))))",
                "STRING",
            ),
            "postgres": (
                "sha256",
                "encode(sha256(convert_to({key}, 'UTF8')), 'hex')",
                None,
            ),
            "redshift": ("SHA2", "LOWER(SHA2({key}, 256))", None),
            "databricks": ("sha2", "lower(sha2({key}, 256))", "STRING"),
            "azuresynapse": (
                "HASHBYTES",
                "LOWER(CONVERT(VARCHAR(MAX), HASHBYTES('SHA2_256', {key}), 2))",
                None,
            ),
            "clickhouse": ("SHA256", "lower(hex(SHA256({key})))", "String"),
        }
        for name, (function, template, string_type) in hash_expected.items():
            dialect = DIALECTS[name]
            assert dialect.hash_function == function, name
            assert dialect.hash_template == template, name
            if string_type is not None:
                assert dialect.string_type == string_type, name
        deleted_flag_expected = {
            "duckdb": "CAST({col} AS BOOLEAN)",
            "snowflake": "CAST({col} AS BOOLEAN)",
            "bigquery": "CAST({col} AS BOOL)",
            "postgres": "CAST({col} AS BOOLEAN)",
            "redshift": "CAST({col} AS BOOLEAN)",
            "databricks": "CAST({col} AS BOOLEAN)",
            "azuresynapse": "CAST({col} AS BIT)",
            "clickhouse": "CAST({col} AS Bool)",
        }
        for name, template in deleted_flag_expected.items():
            assert DIALECTS[name].deleted_flag_template == template, name
            assert "{col}" in template
        cursor_expected = {
            "duckdb": "{expr} - INTERVAL '{n}' {unit}",
            "snowflake": "DATEADD({unit}, -{n}, {expr})",
            "bigquery": "{expr} - INTERVAL {n} {unit}",
            "postgres": "{expr} - INTERVAL '{n}' {unit}",
            "redshift": "{expr} - INTERVAL '{n}' {unit}",
            "databricks": "{expr} - INTERVAL {n} {unit}",
            "azuresynapse": "DATEADD({unit}, -{n}, {expr})",
            "clickhouse": "date_add({expr}, INTERVAL -{n} {unit})",
        }
        for name, template in cursor_expected.items():
            assert DIALECTS[name].cursor_dateadd_template == template, name
            for placeholder in ("{expr}", "{n}", "{unit}"):
                assert placeholder in template, (name, placeholder)
        assert DIALECTS["duckdb"].current_timestamp == "localtimestamp"
        assert DIALECTS["redshift"].current_timestamp == "GETDATE()"
        assert DIALECTS["clickhouse"].current_timestamp == "now()"
        # Gate idioms by engine family: a message-bearing
        # raise where the engine has one, integer division by zero
        # elsewhere; ClickHouse needs intDiv because its / is float
        # division.
        assert "error('" in DIALECTS["duckdb"].link_guard_template
        assert "ERROR('" in DIALECTS["bigquery"].link_guard_template
        assert "raise_error('" in DIALECTS["databricks"].link_guard_template
        assert DIALECTS["clickhouse"].link_guard_template.startswith("intDiv(1,")
        for name in ("postgres", "redshift", "snowflake", "azuresynapse"):
            assert DIALECTS[name].link_guard_template.startswith(
                "1 / (CASE WHEN"
            ), name
        for dialect in DIALECTS.values():
            assert "{violations}" in dialect.link_guard_template, dialect.name
        for name, dialect in DIALECTS.items():
            if name == "clickhouse":
                # dbt-clickhouse rejects the hard_deletes snapshot
                # property outright and its snapshots carry no
                # deleted-flag column (verified against a live
                # server).
                assert dialect.snapshot_hard_deletes is None, name
                assert dialect.snapshot_deleted_column is None, name
                continue
            assert dialect.snapshot_deleted_column == "dbt_is_deleted", name
            assert dialect.snapshot_hard_deletes is True, name

    def test_dialect_incremental_capabilities(self) -> None:
        # The incremental capability pins collapsed
        # into one loop; expected sets copied verbatim.
        # Adapter-verified dbt strategy capability sets
        # (dbt's incremental-strategy adapter table). merge is 7/8 -
        # ClickHouse has none; BigQuery has neither append nor
        # delete+insert; insert_overwrite needs partition support the
        # table grants to four adapters; microbatch is absent only
        # on ClickHouse.
        expected = {
            "duckdb": {"append", "merge", "delete+insert", "microbatch"},
            "snowflake": {
                "append", "merge", "delete+insert", "insert_overwrite",
                "microbatch",
            },
            "bigquery": {"merge", "insert_overwrite", "microbatch"},
            "postgres": {"append", "merge", "delete+insert", "microbatch"},
            "redshift": {"append", "merge", "delete+insert", "microbatch"},
            "databricks": {
                "append", "merge", "delete+insert", "insert_overwrite",
                "microbatch",
            },
            "azuresynapse": {"append", "merge", "delete+insert", "microbatch"},
            "clickhouse": {"append", "delete+insert", "insert_overwrite"},
        }
        for name, strategies in expected.items():
            assert DIALECTS[name].incremental_strategies == frozenset(
                strategies
            ), name
        assert "merge" not in DIALECTS["clickhouse"].incremental_strategies
        assert "append" not in DIALECTS["bigquery"].incremental_strategies
        assert "delete+insert" not in DIALECTS["bigquery"].incremental_strategies
        # The dead/wrong single-strategy fields are gone
        # entirely - no silent dual-carry alongside the capability set.
        for dialect in DIALECTS.values():
            assert not hasattr(dialect, "incremental_strategy"), dialect.name
            assert not hasattr(dialect, "supports_merge"), dialect.name


class TestGetDialect:
    """Test get_dialect function."""

    def test_get_known_dialect(self) -> None:
        d = get_dialect("duckdb")
        assert d.name == "duckdb"

    def test_get_unknown_dialect_raises(self) -> None:
        with pytest.raises(LoomError, match="unknown dialect 'unknown'"):
            get_dialect("unknown")

    def test_get_unknown_dialect_lists_supported(self) -> None:
        with pytest.raises(LoomError, match="supported:"):
            get_dialect("unknown")


class TestAdapterToDialect:
    """Test ADAPTER_TO_DIALECT mapping."""

    def test_all_adapters_mapped(self) -> None:
        assert set(ADAPTER_TO_DIALECT.keys()) == set(ADAPTER_TO_DIALECT.values())
        assert ADAPTER_TO_DIALECT["duckdb"] == "duckdb"
        assert ADAPTER_TO_DIALECT["snowflake"] == "snowflake"
        assert ADAPTER_TO_DIALECT["bigquery"] == "bigquery"


class TestDetectDialect:
    """Test dialect auto-detection from dbt profile."""

    def test_detect_dialect_no_profile_returns_duckdb(self, tmp_path) -> None:
        result = detect_dialect(tmp_path)
        assert result == "duckdb"

    def test_detect_dialect_reads_active_profile_target(self, tmp_path: Path) -> None:
        import yaml

        (tmp_path / "dbt_project.yml").write_text(
            yaml.safe_dump({"profile": "selected_profile"}),
            encoding="utf-8",
        )
        profile_path = tmp_path / "profiles.yml"
        profile_path.write_text(
            yaml.safe_dump(
                {
                    "other_profile": {
                        "target": "other",
                        "outputs": {"other": {"type": "snowflake"}},
                    },
                    "selected_profile": {
                        "target": "production",
                        "outputs": {
                            "development": {"type": "duckdb"},
                            "production": {"type": "databricks"},
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        assert detect_dialect(tmp_path) == "databricks"

    def test_detect_dialect_unknown_adapter_raises(self, tmp_path: Path) -> None:
        import yaml

        (tmp_path / "dbt_project.yml").write_text(
            yaml.safe_dump({"profile": "my_profile"}),
            encoding="utf-8",
        )
        profile_path = tmp_path / "profiles.yml"
        profile_path.write_text(
            yaml.safe_dump(
                {
                    "my_profile": {
                        "target": "dev",
                        "outputs": {"dev": {"type": "unknown_adapter"}},
                    }
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(
            LoomError, match="unsupported dbt adapter 'unknown_adapter'"
        ):
            detect_dialect(tmp_path)

    def test_detect_dialect_snowflake(self, tmp_path: Path) -> None:
        import yaml

        (tmp_path / "dbt_project.yml").write_text(
            yaml.safe_dump({"profile": "my_profile"}),
            encoding="utf-8",
        )
        profile_path = tmp_path / "profiles.yml"
        profile_path.write_text(
            yaml.safe_dump(
                {
                    "my_profile": {
                        "target": "dev",
                        "outputs": {"dev": {"type": "snowflake"}},
                    }
                }
            ),
            encoding="utf-8",
        )
        result = detect_dialect(tmp_path)
        assert result == "snowflake"

    def test_detect_dialect_bigquery(self, tmp_path: Path) -> None:
        import yaml

        (tmp_path / "dbt_project.yml").write_text(
            yaml.safe_dump({"profile": "my_profile"}),
            encoding="utf-8",
        )
        profile_path = tmp_path / "profiles.yml"
        profile_path.write_text(
            yaml.safe_dump(
                {
                    "my_profile": {
                        "target": "dev",
                        "outputs": {"dev": {"type": "bigquery"}},
                    }
                }
            ),
            encoding="utf-8",
        )
        result = detect_dialect(tmp_path)
        assert result == "bigquery"

    def test_detect_dialect_malformed_profile_raises(self, tmp_path: Path) -> None:
        (tmp_path / "dbt_project.yml").write_text(
            "profile: my_profile\n",
            encoding="utf-8",
        )
        (tmp_path / "profiles.yml").write_text("my_profile: [", encoding="utf-8")

        with pytest.raises(LoomError, match="invalid YAML in profiles.yml"):
            detect_dialect(tmp_path)

    def test_detect_dialect_requires_target_output(self, tmp_path: Path) -> None:
        import yaml

        (tmp_path / "dbt_project.yml").write_text(
            yaml.safe_dump({"profile": "my_profile"}),
            encoding="utf-8",
        )
        (tmp_path / "profiles.yml").write_text(
            yaml.safe_dump({"my_profile": {"outputs": {"dev": {"type": "duckdb"}}}}),
            encoding="utf-8",
        )

        with pytest.raises(LoomError, match="must define a target"):
            detect_dialect(tmp_path)


class TestDetectProfileAdapter:
    """The tolerant profile-adapter resolver used for
    manifest provenance. Same traversal as detect_dialect, but
    unresolvable profiles return None instead of raising."""

    def _write_profile(
        self, tmp_path: Path, profile_name: str, target_type: str
    ) -> None:
        import yaml

        (tmp_path / "dbt_project.yml").write_text(
            yaml.safe_dump({"profile": profile_name}),
            encoding="utf-8",
        )
        (tmp_path / "profiles.yml").write_text(
            yaml.safe_dump(
                {
                    profile_name: {
                        "target": "dev",
                        "outputs": {"dev": {"type": target_type}},
                    }
                }
            ),
            encoding="utf-8",
        )

    def test_returns_declared_adapter_type(self, tmp_path: Path) -> None:
        self._write_profile(tmp_path, "my_profile", "bigquery")
        assert detect_profile_adapter(tmp_path) == "bigquery"

    def test_no_profiles_file_returns_none(self, tmp_path: Path) -> None:
        assert detect_profile_adapter(tmp_path) is None

    def test_malformed_profiles_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "dbt_project.yml").write_text(
            "profile: my_profile\n", encoding="utf-8"
        )
        (tmp_path / "profiles.yml").write_text(
            "my_profile: [", encoding="utf-8"
        )
        assert detect_profile_adapter(tmp_path) is None

    def test_missing_target_returns_none(self, tmp_path: Path) -> None:
        import yaml

        (tmp_path / "dbt_project.yml").write_text(
            yaml.safe_dump({"profile": "my_profile"}),
            encoding="utf-8",
        )
        (tmp_path / "profiles.yml").write_text(
            yaml.safe_dump(
                {"my_profile": {"outputs": {"dev": {"type": "duckdb"}}}}
            ),
            encoding="utf-8",
        )
        assert detect_profile_adapter(tmp_path) is None


class TestAdapterAliases:
    """Alias adapter types canonicalise to the
    ADAPTER_TO_DIALECT key so provenance comparisons do not report
    false mismatches."""

    def test_synapse_alias(self) -> None:
        assert ADAPTER_ALIASES == {"synapse": "azuresynapse"}
        assert canonical_adapter_name("synapse") == "azuresynapse"
        assert canonical_adapter_name("azuresynapse") == "azuresynapse"

    def test_canonicalisation_normalises_case_and_space(self) -> None:
        assert canonical_adapter_name(" BigQuery ") == "bigquery"
        assert canonical_adapter_name("duckdb") == "duckdb"



class TestStep2DetectDialectSearchOrder:
        @staticmethod
        def _set_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
            # Path.home() reads HOME on POSIX and USERPROFILE on Windows;
            # set both so the fixture is platform-neutral.
            monkeypatch.setenv("HOME", str(home))
            monkeypatch.setenv("USERPROFILE", str(home))

        def _project(self, project: Path) -> None:
            project.mkdir(parents=True, exist_ok=True)
            (project / "dbt_project.yml").write_text(
                yaml.safe_dump({"profile": "p"}), encoding="utf-8"
            )

        def _profiles(self, path: Path, adapter: str) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                yaml.safe_dump(
                    {"p": {"target": "dev", "outputs": {"dev": {"type": adapter}}}}
                ),
                encoding="utf-8",
            )

        def test_profiles_from_dbt_profiles_dir_env(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            project = tmp_path / "proj"
            self._project(project)
            profiles_dir = tmp_path / "profiles"
            self._profiles(profiles_dir / "profiles.yml", "snowflake")
            monkeypatch.setenv("DBT_PROFILES_DIR", str(profiles_dir))
            self._set_home(monkeypatch, tmp_path / "home")
            assert detect_dialect(project) == "snowflake"

        def test_profiles_from_home_dbt_dir(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            project = tmp_path / "proj"
            self._project(project)
            home = tmp_path / "home"
            self._profiles(home / ".dbt" / "profiles.yml", "postgres")
            monkeypatch.delenv("DBT_PROFILES_DIR", raising=False)
            self._set_home(monkeypatch, home)
            assert detect_dialect(project) == "postgres"

        def test_project_profiles_take_precedence(
            self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
        ) -> None:
            project = tmp_path / "proj"
            self._project(project)
            self._profiles(project / "profiles.yml", "duckdb")
            profiles_dir = tmp_path / "profiles"
            self._profiles(profiles_dir / "profiles.yml", "snowflake")
            monkeypatch.setenv("DBT_PROFILES_DIR", str(profiles_dir))
            self._set_home(monkeypatch, tmp_path / "home")
            assert detect_dialect(project) == "duckdb"
