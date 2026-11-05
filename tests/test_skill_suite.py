"""The bundled agent skills cannot drift from the CLI.

The suite ships twice: the canonical copies inside the package
(``src/loom/.agents/skills/``) and the mirror at the repository
root (``skills/``). Three checks, in the same both-directions
spirit as the command-reference check in
test_commands_reference.py:

- the two copies of each skill are byte-identical;
- every bundled reference artefact a skill carries under
  ``references/`` is byte-identical to the document or example
  file it copies - a bundle is a copy, never a second authored
  text, so drift from the governing source fails;
- every skill's YAML frontmatter parses, naming the skill it
  describes (a harness reads the frontmatter before anything
  else; a description scalar a YAML parser rejects makes the
  skill unloadable);
- everything the suite teaches as current - commands, the flags
  on taught invocations, and selector qualifiers - exists in the
  shipped parser and selector engine (taught-but-not-shipped
  fails), with ``config.`` taught only as a refusal, matching the
  engine;
- every shipped command is accounted for somewhere in the suite,
  named in an invocation or in prose (shipped-but-unaccounted
  fails).

Extraction is targeted, not a prose parser: taught invocations
are backticked spans and fenced lines beginning ``loom ``, taught
qualifiers are the ``word:`` tokens of selector examples and of
the catalogue passages, and the retired spellings the suite names
only to un-teach them (the old search flags, ``--write-drafts``,
``--dry-run``) never appear inside a taught invocation, so they
are never extracted.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

from loom.cli import _parser
from loom.selection import _QUALIFIER_KEYS

ROOT = Path(__file__).parents[1]
CANONICAL = ROOT / "src" / "loom" / ".agents" / "skills"
MIRROR = ROOT / "skills"
SKILLS = (
    "loom-core",
    "loom-bootstrap",
    "loom-adoption",
    "loom-authoring",
    "loom-ask",
    "loom-maintenance",
)

# Bundled reference artefacts: each skill carries byte-identical
# copies of the documents it defers to under ``references/``, so
# the governing text is on disk beside the skill that paraphrases
# it (an offline agent has the skills plus validation errors and
# nothing deeper otherwise). The map is the repo-relative source
# document and the skills bundling a copy of it under its own
# name; the pin below holds every copy to its source.
BUNDLED_REFERENCES = {
    "docs/metadata-reference.md": ("loom-authoring", "loom-adoption"),
    "docs/adoption-playbook.md": ("loom-adoption",),
    "docs/commands.md": ("loom-core",),
    "docs/terminology.md": ("loom-core",),
}

# The first-project example teaches by example; the skills that
# walk a practitioner through it bundle its metadata-side files
# verbatim under ``references/first-project/``, at their example
# paths (whole files, never rewritten into skill prose - the
# example's README is a human walkthrough and is not bundled).
FIRST_PROJECT = ROOT / "examples" / "first-project"
FIRST_PROJECT_FILES = (
    "metadata/instances/staging.yml",
    "metadata/instances/hubs.yml",
    "metadata/patterns/staging.yml",
    "metadata/patterns/current_hub.yml",
    "models/sources.yml",
    "local/registrations/customer_order_counts.yml",
)
FIRST_PROJECT_SKILLS = ("loom-bootstrap", "loom-adoption")

# Qualifier keys the engine accepts outside _QUALIFIER_KEYS: the
# word-form graph qualifiers, their depth companion, and named
# selectors.
_EXTRA_QUALIFIER_KEYS = {"upstream", "downstream", "depth", "selector"}
SHIPPED_QUALIFIERS = set(_QUALIFIER_KEYS) | _EXTRA_QUALIFIER_KEYS


def _suite_texts() -> dict[str, str]:
    return {
        name: (CANONICAL / name / "SKILL.md").read_text(encoding="utf-8")
        for name in SKILLS
    }


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


def _segments(text: str) -> list[tuple[bool, str]]:
    """The text split into fenced blocks and the prose between
    them, as (is_fence, segment) pairs in order. A fence opens
    and closes on a line whose first non-space characters are
    three backticks; the delimiter lines belong to the fenced
    segment."""
    segments: list[tuple[bool, str]] = []
    in_fence = False
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            if in_fence:
                current.append(line)
                segments.append((True, "".join(current)))
                current = []
                in_fence = False
            else:
                if current:
                    segments.append((False, "".join(current)))
                current = [line]
                in_fence = True
            continue
        current.append(line)
    if current:
        segments.append((in_fence, "".join(current)))
    return segments


def _prose_spans(text: str) -> list[str]:
    """Backticked spans in the prose segments, with internal
    whitespace normalised to single spaces.

    Spans are matched per prose segment, never over the whole
    document: inside a fence the backticks are fence delimiters,
    not inline-code markers, and a whole-document match mispairs
    them (a closing fence's backticks pair with the next span's
    opener), swallowing real spans. Within prose, backticks pair
    inline, so a span may wrap across a line break - a wrapped
    invocation is still one taught invocation."""
    spans = []
    for is_fence, segment in _segments(text):
        if is_fence:
            continue
        spans += [
            " ".join(span.split())
            for span in re.findall(r"`([^`]+)`", segment)
        ]
    return spans


def _invocations(text: str) -> list[list[str]]:
    """Taught command lines: backticked spans in prose (a span
    may wrap across a line break) and fenced lines that begin
    with ``loom ``, tokenised on whitespace with bracket and
    comment decoration removed."""
    candidates = []
    for is_fence, segment in _segments(text):
        if is_fence:
            candidates += [
                line.strip()
                for line in segment.splitlines()
                if line.strip().startswith("loom ")
            ]
        else:
            candidates += [
                " ".join(span.split())
                for span in re.findall(r"`([^`]+)`", segment)
            ]
    invocations = []
    for candidate in candidates:
        if not candidate.startswith("loom "):
            continue
        tokens = []
        for token in candidate.split():
            token = token.strip("[],")
            if token.startswith("#"):
                break
            tokens.append(token)
        invocations.append(tokens)
    return invocations


def _taught_qualifiers(texts: dict[str, str]) -> set[str]:
    """Selector qualifier keys the suite teaches: ``word:value``
    tokens in selector examples suite-wide, plus the ``word:``
    tokens of the catalogue passages (the Selection section of
    loom-core, where the catalogue is stated in full)."""
    taught: set[str] = set()
    for text in texts.values():
        for span in _prose_spans(text):
            for key in re.findall(r"(?<![a-zA-Z_-])([a-z][a-z-]*):(?=\S)", span):
                taught.add(key)
    core = texts["loom-core"]
    selection = core.split("## Selection", 1)[1].split("\n## ", 1)[0]
    selection = "".join(
        segment for is_fence, segment in _segments(selection) if not is_fence
    )
    for line in selection.splitlines():
        for found in re.finditer(r"`([^`\n]+)`", line):
            match = re.fullmatch(r"([a-z][a-z-]*):", found.group(1))
            if not match:
                continue
            # A catalogue line also names the one form it
            # un-teaches ("there is no `dependency:` qualifier");
            # a span introduced by "no" is a negation, not a
            # taught qualifier.
            if line[: found.start()].rstrip().endswith("no"):
                continue
            taught.add(match.group(1))
    return taught


def test_canonical_and_mirror_copies_are_byte_identical() -> None:
    for name in SKILLS:
        canonical = CANONICAL / name / "SKILL.md"
        mirror = MIRROR / name / "SKILL.md"
        assert canonical.is_file(), f"canonical skill missing: {name}"
        assert mirror.is_file(), f"mirror skill missing: {name}"
        assert canonical.read_bytes() == mirror.read_bytes(), (
            f"skill {name}: canonical and mirror copies differ"
        )


def test_canonical_and_mirror_skill_trees_are_byte_identical() -> None:
    # The SKILL.md check above predates bundled artefacts; the
    # whole tree (references/ included) ships twice, so the whole
    # tree is pinned: same relative files, same bytes.
    for name in SKILLS:
        canonical_files = sorted(
            path.relative_to(CANONICAL / name)
            for path in (CANONICAL / name).rglob("*")
            if path.is_file()
        )
        mirror_files = sorted(
            path.relative_to(MIRROR / name)
            for path in (MIRROR / name).rglob("*")
            if path.is_file()
        )
        assert canonical_files == mirror_files, (
            f"skill {name}: canonical and mirror trees carry "
            f"different files"
        )
        for relative in canonical_files:
            assert (CANONICAL / name / relative).read_bytes() == (
                MIRROR / name / relative
            ).read_bytes(), (
                f"skill {name}: {relative} differs between the "
                f"canonical and mirror trees"
            )


def _bundled_copies() -> list[tuple[Path, Path]]:
    """(source, bundled copy) pairs for every bundled artefact,
    in both skill trees."""
    pairs: list[tuple[Path, Path]] = []
    for relative, skills in BUNDLED_REFERENCES.items():
        source = ROOT / relative
        for skill in skills:
            for tree in (CANONICAL, MIRROR):
                pairs.append(
                    (source, tree / skill / "references" / Path(relative).name)
                )
    for relative in FIRST_PROJECT_FILES:
        source = FIRST_PROJECT / relative
        for skill in FIRST_PROJECT_SKILLS:
            for tree in (CANONICAL, MIRROR):
                pairs.append(
                    (
                        source,
                        tree
                        / skill
                        / "references"
                        / "first-project"
                        / relative,
                    )
                )
    return pairs


def test_bundled_references_are_byte_identical_to_their_sources() -> None:
    for source, copy in _bundled_copies():
        assert source.is_file(), f"bundled source missing: {source}"
        assert copy.is_file(), f"bundled copy missing: {copy}"
        assert copy.read_bytes() == source.read_bytes(), (
            f"bundled copy {copy} has drifted from its source "
            f"{source}: re-copy the source, never edit the bundle"
        )


def test_every_skill_frontmatter_parses_and_names_its_skill() -> None:
    for name, text in _suite_texts().items():
        match = re.match(r"^---\n(.*?)\n---", text, re.S)
        assert match, f"{name}: no YAML frontmatter block"
        frontmatter = yaml.safe_load(match.group(1))
        assert isinstance(frontmatter, dict), (
            f"{name}: frontmatter is not a mapping"
        )
        assert frontmatter.get("name") == name, (
            f"{name}: frontmatter names {frontmatter.get('name')!r}"
        )
        assert frontmatter.get("description"), (
            f"{name}: frontmatter carries no description"
        )


def test_taught_commands_and_flags_exist_in_the_parser() -> None:
    commands = _command_options()
    for name, text in _suite_texts().items():
        for tokens in _invocations(text):
            assert len(tokens) >= 2, f"{name}: bare 'loom' invocation"
            head = tokens[1]
            if head.startswith("--"):
                # A global flag taught on the root parser itself
                # (loom --version).
                assert head == "--version", f"{name}: taught {head!r}"
                continue
            assert head in commands, (
                f"{name}: teaches command {head!r}, which the CLI "
                f"does not ship"
            )
            for token in tokens[2:]:
                if token.startswith("--"):
                    flag = token.split("=", 1)[0]
                    assert flag in commands[head], (
                        f"{name}: teaches {flag!r} on 'loom {head}', "
                        f"which the CLI does not ship there"
                    )


def test_every_shipped_command_is_accounted_for_in_the_suite() -> None:
    commands = _command_options()
    accounted: set[str] = set()
    for text in _suite_texts().values():
        for tokens in _invocations(text):
            if len(tokens) >= 2 and not tokens[1].startswith("--"):
                accounted.add(tokens[1])
        for span in _prose_spans(text):
            if span in commands:
                accounted.add(span)
    missing = sorted(set(commands) - accounted)
    assert not missing, (
        f"shipped commands the skill suite never accounts for: {missing}"
    )


def test_taught_qualifiers_match_the_engine_registry() -> None:
    taught = _taught_qualifiers(_suite_texts())
    unshipped = sorted(taught - SHIPPED_QUALIFIERS)
    assert not unshipped, (
        f"qualifiers the suite teaches but the engine does not "
        f"accept: {unshipped}"
    )
    untaught = sorted(SHIPPED_QUALIFIERS - taught)
    assert not untaught, (
        f"qualifiers the engine accepts but the suite never "
        f"teaches: {untaught}"
    )


def test_config_matching_is_taught_only_as_a_refusal() -> None:
    # The engine recognises config. only to refuse it (records
    # carry no dbt config fields). The suite must teach that
    # refusal and must never present config. as a working form:
    # no taught invocation or selector example may use it.
    core = _suite_texts()["loom-core"]
    assert "refused" in core and "`config.`" in core, (
        "loom-core no longer teaches the config. refusal"
    )
    for name, text in _suite_texts().items():
        for span in re.findall(r"`([^`\n]+)`", text):
            assert not re.search(r"config\.[a-z]", span), (
                f"{name}: presents config. as a usable form in {span!r}"
            )
