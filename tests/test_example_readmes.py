"""Pin the example READMEs' taught command lines against the CLI parser.

The two example READMEs are the product's most-followed instruction
sequences, and their prose sits outside the conformance nets that pin
docs/commands.md, the architecture document and the skill suite: a
renamed command survived in both walkthroughs as a taught invocation
that argparse rejects. This test closes that hole at the class level:
every `loom ...` command line in a fenced block in either example
README must name a command the parser ships, and every long flag on
the line must exist on that command.

Bound: expected-output blocks are not pinned here. Pinning them means
running dbt builds inside the unit suite; the generated files the
walkthroughs produce are pinned by the example projects' own tests,
the root README's captured example is pinned by
tests/test_first_project.py, and walkthrough outputs are re-verified
literally when the text changes.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from loom.cli import _parser


ROOT = Path(__file__).parents[1]
EXAMPLE_READMES = [
    ROOT / "examples" / "first-project" / "README.md",
    ROOT / "examples" / "quickstart" / "README.md",
]


def _command_options() -> dict[str, set[str]]:
    """Every leaf command and its long options, from the parser."""
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
        commands[prefix] = {
            option
            for action in parser._actions
            for option in action.option_strings
            if option.startswith("--") and option != "--help"
        }

    walk(_parser(), "")
    return commands


def _taught_invocations(readme: Path) -> list[tuple[int, list[str]]]:
    """(line number, tokens) for each fenced line beginning `loom `."""
    invocations: list[tuple[int, list[str]]] = []
    in_fence = False
    for number, line in enumerate(readme.read_text(encoding="utf-8").splitlines(), 1):
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        stripped = line.strip()
        if not in_fence or not stripped.startswith("loom "):
            continue
        tokens = []
        for token in stripped.split():
            if token.startswith("#"):
                break
            tokens.append(token.strip("`"))
        invocations.append((number, tokens))
    return invocations


def test_example_readme_commands_exist_in_the_parser() -> None:
    commands = _command_options()
    for readme in EXAMPLE_READMES:
        invocations = _taught_invocations(readme)
        assert invocations, f"{readme.name}: no taught command lines found"
        for number, tokens in invocations:
            where = f"{readme.relative_to(ROOT)}:{number}"
            assert len(tokens) >= 2, f"{where}: bare 'loom' invocation"
            head = tokens[1]
            if head.startswith("--"):
                assert head == "--version", f"{where}: teaches {head!r} on the root parser"
                continue
            assert head in commands, (
                f"{where}: teaches command {head!r}, which the CLI does not ship"
            )
            for token in tokens[2:]:
                if token.startswith("--"):
                    flag = token.split("=", 1)[0]
                    assert flag in commands[head], (
                        f"{where}: teaches {flag!r} on 'loom {head}', "
                        f"which the CLI does not ship there"
                    )


def test_no_retired_command_spelling_survives_in_the_examples() -> None:
    # The rename record lives in the changelog; the teaching texts
    # must never carry a retired spelling again, in prose or in code.
    retired = re.compile(r"check-coverage|capture-source-structure|compare-source-structure")
    for readme in EXAMPLE_READMES:
        assert not retired.search(readme.read_text(encoding="utf-8")), (
            f"{readme.relative_to(ROOT)}: teaches a retired command spelling"
        )
