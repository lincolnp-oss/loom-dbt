"""CLI stdout encoding safety (finding Q1).

On a Windows console (cp1252), printing the convert report
crashed with UnicodeEncodeError on the report vocabulary's `→`
- after `convert --write` had already written its drafts, so a
completed conversion exited 1. `main()` now reconfigures
non-UTF-8 stdout/stderr to UTF-8 (errors="replace") at entry.
These tests run the render-print cycle for the reports that
carry the arrow sites through a cp1252-encoded stream.
"""
from __future__ import annotations

import io

import pytest

from loom.cli import _ensure_utf8_streams, main
from loom.reporting import render_convert_text, render_register_text


def _report(models: list[dict]) -> dict:
    summary = {
        "project_models": len(models),
        "controlled": 0,
        "known_not_controlled": 0,
        "unknown_to_loom": len(models),
        "registered": len(models),
        "scaffolded": sum(1 for m in models if "scaffold" in m),
        "conversion_drafted": sum(1 for m in models if "fit" in m),
    }
    return {
        "project": "census",
        "manifest": "target/manifest.json",
        "manifest_provenance": "parse-produced",
        "warnings": [],
        "summary": summary,
        "models": models,
    }


def _convert_report() -> dict:
    return _report(
        [
            {
                "name": "dim_scaffold",
                "state": "unknown_to_loom",
                "draft_registration": {"classification": "mart"},
                "interface_flags": [],
                "scaffold": {
                    "layer": "mart",
                    "target_generator": "dimension",
                    "state": "untouched",
                    "inputs": [],
                },
            },
            {
                "name": "fct_promoted",
                "state": "unknown_to_loom",
                "draft_registration": {"classification": "mart"},
                "interface_flags": [],
                "readiness_transition": {
                    "from": {"state": "needs_authoring", "holes": 2},
                    "to": {"state": "ready_to_promote", "holes": 0},
                },
            },
        ]
    )


def _cp1252_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")
    return buffer, stream


def test_reports_print_through_cp1252_stream_after_entry_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    convert_text = render_convert_text(_convert_report())
    register_text = render_register_text(_convert_report())
    # Both arrow sites are in the convert report (the scaffold
    # summary table and the readiness transitions).
    assert "mart → dimension: 1" in convert_text
    assert "(2 hole(s)) → ready to promote (0 hole(s))" in convert_text

    out_buffer, out_stream = _cp1252_stream()
    err_buffer, err_stream = _cp1252_stream()
    monkeypatch.setattr("sys.stdout", out_stream)
    monkeypatch.setattr("sys.stderr", err_stream)

    _ensure_utf8_streams()
    assert out_stream.encoding == "utf-8"
    assert err_stream.encoding == "utf-8"

    print(convert_text)
    print(register_text)
    print("loom: error → marker", file=err_stream)
    out_stream.flush()
    err_stream.flush()

    out_text = out_buffer.getvalue().decode("utf-8")
    assert "mart → dimension: 1" in out_text
    assert "(2 hole(s)) → ready to promote (0 hole(s))" in out_text
    # The text stream translates \n to the platform line ending
    # (\r\n on Windows), so normalise before asserting the tail:
    # the marker matters, not the platform's newline spelling.
    err_text = err_buffer.getvalue().decode("utf-8").replace("\r\n", "\n")
    assert err_text.endswith("error → marker\n")


def test_unguarded_cp1252_stream_still_cannot_encode_the_reports() -> None:
    # The defect mechanism, pinned: without the entry guard the
    # same print raises, exactly as it did on the owner's PC.
    _buffer, stream = _cp1252_stream()
    with pytest.raises(UnicodeEncodeError):
        print(render_convert_text(_convert_report()), file=stream)
        stream.flush()


def test_guard_leaves_utf8_and_non_streams_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    utf8_buffer = io.BytesIO()
    utf8_stream = io.TextIOWrapper(utf8_buffer, encoding="utf-8")
    monkeypatch.setattr("sys.stdout", utf8_stream)
    monkeypatch.setattr("sys.stderr", io.StringIO())
    _ensure_utf8_streams()  # must not raise on either stream
    assert utf8_stream.encoding == "utf-8"
    assert utf8_stream.errors == "strict"  # untouched, not reconfigured


def test_main_entry_applies_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _buffer, stream = _cp1252_stream()
    monkeypatch.setattr("sys.stdout", stream)
    monkeypatch.setattr("sys.stderr", io.StringIO())
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert stream.encoding == "utf-8"
