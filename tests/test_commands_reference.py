"""The command reference cannot drift from the CLI.

Every argparse subcommand must have a section in
docs/commands.md, and every long option of every command must be
documented in that command's section - the same anti-drift pattern
as the generator parameter lines checked in test_contracts.py.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from loom.cli import _parser

ROOT = Path(__file__).parents[1]


def _command_options() -> dict[str, set[str]]:
    """Every leaf command, keyed by its full command path.

    The walk recurses through nested subparsers (the shape a
    short-lived `loom migration …` group once took; the guard
    stays general): a group parser itself is
    not a leaf and needs no section, but every leaf under it does -
    with all its options.
    """
    commands: dict[str, set[str]] = {}

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        subparsers = next(
            (
                action
                for action in parser._actions
                if isinstance(action, argparse._SubParsersAction)
            ),
            None,
        )
        if subparsers is not None:
            for name, subparser in subparsers.choices.items():
                walk(subparser, f"{prefix} {name}".strip())
            return
        options = set()
        for action in parser._actions:
            for option in action.option_strings:
                if option.startswith("--") and option != "--help":
                    options.add(option)
        commands[prefix] = options

    walk(_parser(), "")
    return commands


def _sections() -> dict[str, str]:
    text = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    sections: dict[str, str] = {}
    parts = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    for part in parts:
        heading = part.split("\n", 1)[0].strip()
        match = re.fullmatch(r"`loom ([a-z-]+(?: [a-z-]+)?)`", heading)
        if match:
            sections[match.group(1)] = part
    return sections


def test_reference_covers_every_command_and_option() -> None:
    commands = _command_options()
    sections = _sections()
    assert set(sections) == set(commands), (
        f"reference sections {sorted(sections)} != CLI commands "
        f"{sorted(commands)}"
    )
    for name, options in commands.items():
        for option in options:
            assert f"`{option}`" in sections[name], (
                f"docs/commands.md: command {name!r} does not document "
                f"option {option!r}"
            )
