#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Cut a release: version, changelog, gates, commit, tag — and stop there.

Nobody remembers a release procedure, and the parts of one that are
remembered wrongly are the expensive parts. This script is the whole
local half of it, and it is **deliberately repository-agnostic**: what
differs between MCUHome's repositories is a small block in
``pyproject.toml``, not this file, so the same script serves the SDK
today and `mcuhome`, `cli` and `build-server` when they publish.

```toml
[tool.mcuhome-release]
version_files = ["mcuhome/model/__init__.py"]   # every place the number is written
changelog     = "CHANGELOG.md"
tag_prefix    = "v"
branch        = "main"
gates         = ["{python} -m ruff check .", "{python} -m pytest -q tests/python"]
next_steps    = ["git push && git push origin {tag}", "…"]
```

**It stops before pushing, on purpose.** Everything up to the tag is
local and reversible (`git reset`, `git tag -d`); the push is the moment
a release becomes other people's problem, so it stays a decision someone
makes rather than a side effect of running a script.

Two guards matter more than the convenience:

- **The tag must name the version the commit declares.** The SDK archive
  is named after ``__version__`` *as the tagged commit carries it*, not
  after the tag, so a tag on an unbumped commit yields a package whose
  name disagrees with the release it hangs on. ``--check-tag`` is that
  same check with no dependencies, so CI runs it before building
  anything.
- **A version only ever moves forward.** Published versions are
  immutable and eternal; a re-used or lowered number is refused here
  rather than discovered at the package host.

Usage::

    release.py <version> [--dry-run]
    release.py --check-tag <tag>      # what CI runs; stdlib only
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The two spellings a version assignment takes in these repositories:
#: ``__version__`` in the package, ``version`` in a static project table.
ASSIGNMENT = re.compile(r"^(__version__|version)(\s*=\s*)([\"'])(?P<value>[^\"']+)\3", re.MULTILINE)

UNRELEASED = "## [Unreleased]"


class Refused(SystemExit):
    """A refusal with the reason on the way out — never a traceback."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(f"{message}\n{hint}" if hint else message)


@dataclass(frozen=True)
class Config:
    """The per-repository half of a release."""

    version_files: list[Path]
    changelog: Path
    tag_prefix: str = "v"
    branch: str = "main"
    gates: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)


def load_config(root: Path) -> Config:
    project = root / "pyproject.toml"
    if not project.is_file():
        raise Refused(f"{project} is missing — this is not a release-able repository")
    table = tomllib.loads(project.read_text(encoding="utf-8")).get("tool", {})
    block = table.get("mcuhome-release")
    if not isinstance(block, dict):
        raise Refused(
            "pyproject.toml declares no [tool.mcuhome-release] block.",
            "That block is what makes this script repository-agnostic; see its docstring.",
        )
    files = [root / name for name in block.get("version_files", [])]
    if not files:
        raise Refused("[tool.mcuhome-release] names no version_files")
    return Config(
        version_files=files,
        changelog=root / block.get("changelog", "CHANGELOG.md"),
        tag_prefix=block.get("tag_prefix", "v"),
        branch=block.get("branch", "main"),
        gates=list(block.get("gates", [])),
        next_steps=list(block.get("next_steps", [])),
    )


def declared_version(path: Path) -> str:
    """The version *path* writes down, or a refusal naming the file."""
    if not path.is_file():
        raise Refused(f"{path} is missing, but [tool.mcuhome-release] lists it")
    found = ASSIGNMENT.findall(path.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise Refused(
            f"{path} holds {len(found)} version assignments, expected exactly one",
            "A version has one place per file — otherwise a bump can half-apply.",
        )
    return found[0][3]


def set_version(path: Path, old: str, new: str) -> None:
    """Rewrite the one version assignment in *path*, or refuse."""
    text = path.read_text(encoding="utf-8")
    rewritten, count = ASSIGNMENT.subn(
        lambda match: match.group(0).replace(f"{match.group('value')}", new), text, count=1
    )
    if count != 1 or declared_version(path) != old:
        raise Refused(f"{path}: could not rewrite the version assignment")
    path.write_text(rewritten, encoding="utf-8")


def update_changelog(path: Path, version: str, when: date) -> None:
    """Move everything under ``## [Unreleased]`` into a dated section for *version*."""
    if not path.is_file():
        raise Refused(f"{path} is missing")
    text = path.read_text(encoding="utf-8")
    start = text.find(UNRELEASED)
    if start < 0:
        raise Refused(f"{path} has no '{UNRELEASED}' section")
    body_from = start + len(UNRELEASED)
    following = text.find("\n## ", body_from)
    body_to = len(text) if following < 0 else following + 1
    body = text[body_from:body_to]
    if not body.strip():
        raise Refused(
            f"{path}: the Unreleased section is empty",
            "A release with nothing to say about it is a defect, not a release.",
        )
    entries = body.lstrip("\n")
    section = f"{UNRELEASED}\n\n## [{version}] - {when.isoformat()}\n\n{entries}"
    path.write_text(text[:start] + section + text[body_to:], encoding="utf-8")


def git(*arguments: str, root: Path = REPO_ROOT, check: bool = True) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and done.returncode:
        raise Refused(f"git {' '.join(arguments)} failed:\n{done.stderr.strip()}")
    return done.stdout.strip()


def check_working_state(config: Config, tag: str, root: Path) -> None:
    """Everything that must be true before a release is even attempted."""
    if git("status", "--porcelain", root=root):
        raise Refused(
            "The working tree has uncommitted changes.",
            "A release describes a commit; commit or stash first.",
        )
    branch = git("rev-parse", "--abbrev-ref", "HEAD", root=root)
    if branch != config.branch:
        raise Refused(f"On branch {branch}, but releases are cut from {config.branch}.")
    if git("tag", "--list", tag, root=root):
        raise Refused(
            f"The tag {tag} already exists.",
            "A version is published once and never replaced — use the next number.",
        )
    git("fetch", "--quiet", "origin", root=root)
    remote = git("rev-parse", f"origin/{config.branch}", root=root, check=False)
    if remote and git("rev-parse", "HEAD", root=root) != remote:
        raise Refused(
            f"HEAD and origin/{config.branch} differ.",
            "Pull or push first — a release must be reproducible from what others can see.",
        )


def check_version(new: str, old: str) -> None:
    from packaging.version import InvalidVersion, Version

    try:
        candidate = Version(new)
    except InvalidVersion as broken:
        raise Refused(f"{new!r} is not a PEP 440 version") from broken
    if candidate < Version(old):
        raise Refused(
            f"{new} comes before the declared {old}.",
            "Versions only move forward: what is published is permanent.",
        )
    # Equal is allowed on purpose: a version the tree declares but that
    # was never released — the first release of all, or a number bumped
    # in an earlier commit — is a legitimate thing to cut. What must
    # never happen twice is *publishing* one, and that is what the tag
    # check below and the package host's duplicate refusal are for.


def run_gates(config: Config, root: Path) -> None:
    for gate in config.gates:
        command = shlex.split(gate.format(python=sys.executable))
        print(f"→ {' '.join(command)}")
        if subprocess.run(command, cwd=root, check=False).returncode:
            raise Refused(
                "A gate failed — releasing anyway is how a broken release happens.",
                "Fix it, commit, and run this again.",
            )


def check_tag(tag: str, root: Path = REPO_ROOT) -> int:
    """Whether *tag* names the version the tree declares. Stdlib only, for CI.

    The archive is named after the version the *commit* carries, so a tag
    that says something else produces a package whose name contradicts
    the release it is attached to. Cheap to check, silent and expensive
    to miss.
    """
    config = load_config(root)
    version = declared_version(config.version_files[0])
    expected = f"{config.tag_prefix}{version}"
    if tag != expected:
        print(
            f"REFUSED  the tag is {tag}, but {config.version_files[0].name} declares "
            f"{version} — expected {expected}.\n"
            "         Bump the version first, then tag: the archive is named after the "
            "version in the commit, never after the tag.",
            file=sys.stderr,
        )
        return 1
    print(f"OK  {tag} matches the declared version {version}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("version", nargs="?", help="the version to release, e.g. 0.1.0")
    parser.add_argument("--check-tag", help="only check that a tag matches the declared version")
    parser.add_argument("--dry-run", action="store_true", help="show the changes, then undo them")
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)

    root = arguments.repo.resolve()
    if arguments.check_tag:
        return check_tag(arguments.check_tag, root)
    if not arguments.version:
        parser.error("a version is required (or --check-tag)")

    config = load_config(root)
    current = declared_version(config.version_files[0])
    check_version(arguments.version, current)
    tag = f"{config.tag_prefix}{arguments.version}"
    check_working_state(config, tag, root)
    run_gates(config, root)

    touched = [*config.version_files, config.changelog]
    for path in config.version_files:
        if declared_version(path) != current:
            raise Refused(
                f"{path} declares {declared_version(path)}, "
                f"{config.version_files[0]} declares {current} — they must agree first"
            )
        set_version(path, current, arguments.version)
    update_changelog(config.changelog, arguments.version, date.today())

    if arguments.dry_run:
        print(git("diff", root=root))
        git("checkout", "--", *[str(path.relative_to(root)) for path in touched], root=root)
        print("\n(dry run — nothing was committed, the files are back as they were)")
        return 0

    git("add", *[str(path.relative_to(root)) for path in touched], root=root)
    git("commit", "-s", "-m", f"chore(release): {arguments.version}", root=root)
    git("tag", "-a", tag, "-m", f"{root.name} {arguments.version}", root=root)

    print(f"\nReleased {arguments.version} locally: commit + tag {tag}.")
    print("Nothing has been pushed. Next:\n")
    for step in config.next_steps:
        print(f"  {step.format(tag=tag, version=arguments.version)}")
    print("\nTo undo: git tag -d " + tag + " && git reset --hard HEAD~1")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
