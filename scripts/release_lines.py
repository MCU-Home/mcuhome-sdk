#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The three release lines this repository cuts, and what identifies them.

``packaging/build-environment/environment.json`` declares them —
``sdk``, ``workspace``, ``tools`` — each with the version its next release
carries and, where a line depends on the one below it, the PEP 440
constraint it accepts:

```json
{
  "sdk":       {"version": "…", "requires": {"mcuhome-build-workspace": "~=0.1.0"}},
  "workspace": {"version": "…", "requires": {"mcuhome-build-tools": "~=0.1.0"}},
  "tools":     {"version": "…"}
}
```

The constraints form a **chain, not a matrix**: the SDK accepts a range of
workspace packages, a workspace package accepts a range of tools packages,
and the tools constrain nothing. Whoever builds resolves each stage to the
newest published version satisfying the constraint above it and pins that
one exactly. No artifact takes another one's version, which is why there
are three versions and not one.

Two things are computed here rather than typed, and both are read **out of
a commit**, never out of the working tree — a package is built from a
commit, so everything that describes it has to come from the same place:

``inputs_sha256(stage, commit)``
    The identity of a stage's inputs. It is a hash over a canonical
    listing of the repository paths that stage is built from, each with
    the git object the commit gives it, plus the packager image the build
    runs in and, for the tools, the architecture. It needs no build, no
    container and no network, which is what lets a push check it and a
    release gate compare it against what was published.

``meta_document(...)``
    What every package carries beside it and inside it
    (``<archive>.meta.json`` / ``meta.json``): what the package is, what it
    requires of the next stage, the input hash above, and its resolved
    contents.

**Why a cosmetic change can move an input hash.** The packaging script
itself is one of the inputs — it decides how the workspace is laid out and
which tools are downloaded, and no reading of it can tell a comment from a
behaviour. A changed comment therefore reports "these inputs changed",
which is the safe direction: the alternative is a rule that can say "no
change" about a change.

Usage::

    release_lines.py version <stage> [--revision <rev>]
    release_lines.py requires <stage> [--revision <rev>]
    release_lines.py inputs-sha256 <stage> [--revision <rev>] [--architecture linux-amd64]
    release_lines.py inputs-listing <stage> [--revision <rev>] [--architecture linux-amd64]

Exit status: 0 on success, 2 on a usage error; a failing ``git`` call
propagates as a ``CalledProcessError``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

__all__ = [
    "ARCHITECTURE_KEY",
    "ENVIRONMENT_FILE",
    "INPUT_PATHS",
    "META_FILE",
    "META_SCHEMA",
    "META_SUFFIX",
    "PACKAGER_KEY",
    "STAGES",
    "environment",
    "inputs_listing",
    "inputs_sha256",
    "json_bytes",
    "meta_document",
    "requires_of",
    "suffixed",
    "version_of",
]

#: This repository, seen from ``scripts/``.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The definition file, as a path in the repository: it is read out of the
#: packaged commit, so it is named as a path and not as a file on disk.
ENVIRONMENT_FILE = "packaging/build-environment/environment.json"

#: The three lines, in the order the chain runs.
STAGES = ("sdk", "workspace", "tools")

#: Where the packager image is pinned, and where its base's interpreter
#: version is declared. Both are read out of the packaged commit.
PACKAGER_FILE = "scripts/packager_image.py"
PACKAGER_DOCKERFILE = "containers/build-environment-packager/Dockerfile"

#: The SDK archive's own script: it holds the allowlist that decides what
#: the archive contains (:func:`sdk_input_paths`), and it is an input of
#: that stage in its own right.
SDK_ARCHIVE_FILE = "scripts/build_sdk_archive.py"

#: This module, as a path in the repository. It writes a member of every
#: archive — ``meta.json`` — so it is an input of every stage.
RELEASE_LINES_FILE = "scripts/release_lines.py"

#: The schema of the meta file. One integer, and every consumer refuses a
#: number it does not implement rather than guessing at the shape.
META_SCHEMA = 1

#: The meta file, inside the archive at its top level and, byte for byte
#: the same document, beside it. Inside because an unpacked package has to
#: be able to say what it is with nothing else present; beside because
#: whoever resolves a chain reads what a package requires *before* it
#: fetches gigabytes — that is the whole point of the sidecar.
META_FILE = "meta.json"
META_SUFFIX = f".{META_FILE}"

#: The two entries of the input listing that are not repository paths. A
#: path can never collide with them: the lists below are closed, and
#: neither name is in any of them.
PACKAGER_KEY = "packager-image"
ARCHITECTURE_KEY = "architecture"

#: What each stage is built from, as repository paths. A path may be a
#: file or a directory; a directory contributes its git tree object, which
#: covers everything under it including names and modes.
#:
#: ``workspace``
#:     The manifest, the patch set, the Matter data model the
#:     pre-generation runs on, the ``python_path`` shim those generators
#:     import, the program that writes the workspace record, and the
#:     packaging script that drives all of it.
#: ``tools``
#:     The packaging script — which is where the tool downloads and their
#:     hashes are pinned, so it is the list — the environment's Python
#:     requirement set, and the entry point that ships in the package.
#: ``sdk``
#:     The archiver itself, plus exactly what the archive contains — the
#:     latter taken from the archive's own allowlist rather than restated
#:     (:func:`sdk_input_paths`).
#:
#: Every stage lists the script that produces it, because that script
#: decides the bytes: the allowlist, the layout, the compression level.
#: This module is in every list for the same reason — it writes
#: ``meta.json``, which is a member of every archive.
INPUT_PATHS: dict[str, tuple[str, ...]] = {
    "sdk": (
        RELEASE_LINES_FILE,
        SDK_ARCHIVE_FILE,
    ),
    "workspace": (
        "components/matter/zap/mcuhome-root.matter",
        "components/matter/zap/mcuhome-root.zap",
        "packaging/build-environment/workspace-record.py",
        "patches",
        RELEASE_LINES_FILE,
        "scripts/build_env_package.py",
        "scripts/pyshim",
        "west.yml",
    ),
    "tools": (
        "packaging/build-environment/build-environment-entry",
        "packaging/build-environment/requirements.txt",
        RELEASE_LINES_FILE,
        "scripts/build_env_package.py",
    ),
}

#: Which stages are produced inside the packager image, and therefore
#: carry its digest in their inputs. The SDK archive is not: it is written
#: from a git tree by this repository's own interpreter, and a refreshed
#: packager cannot change a byte of it.
PACKAGED_IN_PACKAGER = ("workspace", "tools")

#: The stage whose bytes differ per platform on purpose.
ARCHITECTURE_STAGE = "tools"

#: PEP 440's local version segment: lowercase alphanumerics in
#: dot-separated parts. ``--version-suffix`` takes one of these and
#: nothing else (:func:`suffixed`).
LOCAL_VERSION = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*\Z")


# --------------------------------------------------------------------------
# git, and the definition file inside a commit
# --------------------------------------------------------------------------


def _git(repository: Path, *arguments: str) -> bytes:
    """One git command against *repository*, its stdout, and no shell."""
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
    )
    return completed.stdout


def blob(repository: Path, commit: str, path: str) -> str:
    """One file of *commit*, as text."""
    return _git(repository, "cat-file", "blob", f"{commit}:{path}").decode("utf-8")


def object_id(repository: Path, commit: str, path: str) -> str:
    """The git object *commit* gives *path* — a blob id or a tree id.

    A path the commit does not carry is a refusal that says so. It happens
    for exactly one reason worth naming: a commit from before that path
    was an input, which is a commit whose inputs this list cannot describe
    — and a comparison against it would be a comparison of two different
    questions.
    """
    try:
        return _git(repository, "rev-parse", "--verify", f"{commit}:{path}").decode().strip()
    except subprocess.CalledProcessError:
        raise SystemExit(
            f"{commit} carries no {path}, which is one of the inputs this release line "
            "is identified by.\nA commit from before that path existed cannot be "
            "described by today's input list."
        ) from None


def environment(repository: Path, commit: str) -> dict:
    """The definition file as *commit* carries it, checked as far as it is used."""
    try:
        document = json.loads(blob(repository, commit, ENVIRONMENT_FILE))
    except ValueError as unreadable:
        raise SystemExit(
            f"{ENVIRONMENT_FILE} at {commit} is not readable JSON: {unreadable}"
        ) from unreadable
    if not isinstance(document, dict):
        raise SystemExit(f"{ENVIRONMENT_FILE} at {commit} is not a JSON object")
    return document


def _line(document: dict, stage: str) -> dict:
    """One release line's block, or a refusal naming the file and the line."""
    block = document.get(stage)
    if not isinstance(block, dict):
        raise SystemExit(f"{ENVIRONMENT_FILE} declares no {stage} release line")
    return block


def version_of(document: dict, stage: str) -> str:
    """The version *document* declares for *stage*, or a refusal."""
    version = _line(document, stage).get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"{ENVIRONMENT_FILE} declares no string {stage}.version")
    return version


def requires_of(document: dict, stage: str) -> dict[str, str]:
    """What *stage* requires of the stage below it — empty where it requires nothing.

    A constraint that constrains nothing is refused here rather than
    published. An empty string is a *valid* PEP 440 specifier set and it
    matches every version there is, so a package declaring one would
    quietly accept anything the next stage ever publishes — including the
    release that breaks it. "No requirement at all" is stated by leaving
    the member out, which is what the tools package does; an empty value
    is a mistake that looks like a statement.
    """
    requires = _line(document, stage).get("requires", {})
    if not isinstance(requires, dict) or not all(
        isinstance(name, str) and isinstance(value, str) for name, value in requires.items()
    ):
        raise SystemExit(
            f"{ENVIRONMENT_FILE} states {stage}.requires as something other than a "
            "map from package name to constraint"
        )
    empty = sorted(name for name, value in requires.items() if not value.strip())
    if empty:
        raise SystemExit(
            f"{ENVIRONMENT_FILE} states {stage}.requires for {', '.join(empty)} as an "
            "empty constraint.\nAn empty PEP 440 specifier matches every version there "
            "is, which is not a requirement.\nState one, such as ~=0.1.0, or leave the "
            "entry out where the stage requires nothing."
        )
    return {name: value.strip() for name, value in requires.items()}


def suffixed(version: str, suffix: str | None) -> str:
    """*version* with a PEP 440 local segment appended, or *version* itself.

    The one thing this may produce is a **local** version — ``<version>
    +ci.<sha>`` — because a local version is exactly the thing a package
    host refuses to publish. A per-commit build has to name itself
    something, and it must not be able to name itself something that could
    be mistaken for a release, which is why anything but a local segment is
    refused here rather than quietly built.
    """
    if suffix is None:
        return version
    value = suffix[1:] if suffix.startswith("+") else suffix
    if LOCAL_VERSION.fullmatch(value) is None:
        raise SystemExit(
            f"{suffix!r} is not a local version segment.\n"
            "A version suffix names a build, never a release: lowercase letters and\n"
            "digits in dot-separated parts, such as ci.4f1c2ab — it is appended as\n"
            f"{version}+<suffix>."
        )
    return f"{version}+{value}"


# --------------------------------------------------------------------------
# The packager, as the packaged commit pins it
# --------------------------------------------------------------------------


def _assigned(source: str, name: str, *, where: str) -> str:
    """The string a module-level ``<name> = "..."`` assigns, or a refusal."""
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        if name not in (target.id for target in node.targets if isinstance(target, ast.Name)):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str):
            return value
    raise SystemExit(f"{where} declares no string {name}")


def packager_digest(repository: Path, commit: str) -> str:
    """The packager image *commit* pins, as ``sha256:…``.

    The committed pin, deliberately, and never the image an invocation was
    given with ``--packager-image``: an input hash is a property of a
    commit, computed by parties that build nothing at all.
    """
    return _assigned(
        blob(repository, commit, PACKAGER_FILE), "DIGEST", where=f"{PACKAGER_FILE} at {commit}"
    )


def packager_python(repository: Path, commit: str) -> str:
    """The interpreter version the packager's base carries, at *commit*.

    Read out of the Dockerfile's ``ARG PYTHON_VERSION``, which is the value
    the image build itself asserts against the image it produced and
    publishes as a label — so the number here is checked where it can be
    checked, rather than being a second copy.
    """
    text = blob(repository, commit, PACKAGER_DOCKERFILE)
    found = re.search(r"^ARG PYTHON_VERSION=(\S+)$", text, re.M)
    if found is None:
        raise SystemExit(f"{PACKAGER_DOCKERFILE} at {commit} declares no ARG PYTHON_VERSION")
    return found.group(1)


# --------------------------------------------------------------------------
# The input hash
# --------------------------------------------------------------------------


def _collection(source: str, name: str, *, where: str) -> list[str]:
    """The strings a module-level ``<name> = <literal collection>`` holds.

    ``frozenset({...})`` is a call around a literal rather than a literal,
    so the call is unwrapped and its one argument evaluated — which is the
    shape the allowlist happens to take and the only shape accepted here.
    """
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        if name not in (target.id for target in node.targets if isinstance(target, ast.Name)):
            continue
        value = node.value
        if isinstance(value, ast.Call) and len(value.args) == 1:
            value = value.args[0]
        found = ast.literal_eval(value)
        if isinstance(found, (list, tuple, set, frozenset)) and all(
            isinstance(entry, str) for entry in found
        ):
            return sorted(found)
    raise SystemExit(f"{where} declares no collection of strings named {name}")


def sdk_input_paths(repository: Path, commit: str) -> tuple[str, ...]:
    """What the SDK archive is built from, from the archive's own allowlist.

    Read rather than restated: the question "did the SDK's inputs change"
    is exactly "did anything the archive contains change", and two lists
    would answer it two ways the day somebody adds a directory to one of
    them. The generated members are not here — they are written by the
    build out of what is, and a file the commit does not carry has no
    object to name.

    Read **out of the commit**, like everything else in this module and for
    the same reason: a release gate compares a published commit's input
    hash against the checkout's, so the allowlist that decided the
    published archive has to be the published one. Taking the working
    tree's would make an added directory look like a change to the old
    commit as well.
    """
    source = blob(repository, commit, SDK_ARCHIVE_FILE)
    where = f"{SDK_ARCHIVE_FILE} at {commit}"
    named = _collection(source, "SDK_FILES", where=where)
    trees = _collection(source, "SDK_TREES", where=where)
    return tuple(sorted(set(named) | set(trees)))


def input_paths(stage: str, repository: Path, commit: str) -> tuple[str, ...]:
    """The repository paths *stage* is built from, as *commit* has them."""
    if stage == "sdk":
        return tuple(sorted(set(sdk_input_paths(repository, commit)) | set(INPUT_PATHS["sdk"])))
    if stage not in INPUT_PATHS:
        raise SystemExit(f"{stage!r} is not a release line: {', '.join(STAGES)}")
    return INPUT_PATHS[stage]


def inputs_listing(
    stage: str, commit: str, *, repository: Path = REPO_ROOT, architecture: str | None = None
) -> str:
    """The canonical listing :func:`inputs_sha256` hashes.

    One line per input, ``<key>\\t<value>\\n``, sorted by key in ascending
    byte order. A repository path's value is the git object *commit* gives
    it — a blob for a file, a tree for a directory, which covers every name,
    mode and byte under it. Two lines are not paths: the packager image the
    package is produced in, and the architecture of a per-platform package.

    Returned as text rather than only hashed, because a check that reports
    "the inputs changed" is worth very little if it cannot show which one.
    """
    entries = {
        path: object_id(repository, commit, path) for path in input_paths(stage, repository, commit)
    }
    if stage in PACKAGED_IN_PACKAGER:
        entries[PACKAGER_KEY] = packager_digest(repository, commit)
    if stage == ARCHITECTURE_STAGE:
        if not architecture:
            raise SystemExit(
                "the tools package is per platform, so its inputs need an architecture"
            )
        entries[ARCHITECTURE_KEY] = architecture
    return "".join(
        f"{key}\t{entries[key]}\n" for key in sorted(entries, key=lambda key: key.encode("utf-8"))
    )


def inputs_sha256(
    stage: str, commit: str, *, repository: Path = REPO_ROOT, architecture: str | None = None
) -> str:
    """The identity of *stage*'s inputs at *commit*: 64 lowercase hex digits."""
    listing = inputs_listing(stage, commit, repository=repository, architecture=architecture)
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# The meta file
# --------------------------------------------------------------------------


def json_bytes(document: dict) -> bytes:
    """One JSON document, in the one formatting this repository writes.

    Sorted keys, two-space indent, one trailing newline — the copy inside
    the archive and the copy beside it are written from this one value, so
    they cannot differ in a byte.
    """
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def meta_document(
    *,
    name: str,
    version: str,
    architecture: str | None,
    requires: dict[str, str] | None,
    inputs: str,
    contents: dict,
) -> dict:
    """What a package says about itself, in the one schema every consumer reads.

    *name* is the package name as it is published — the family for a
    per-platform package, whose one platform is named by *architecture*
    instead. *requires* is the constraint on the next stage and is **absent**
    for a package that constrains nothing, rather than present and empty: a
    reader asks "does this package require anything", and an empty map is a
    different answer from no map at all.
    """
    document: dict = {
        "schema": META_SCHEMA,
        "package": {"name": name, "version": version, "architecture": architecture},
        "inputs_sha256": inputs,
        "contents": contents,
    }
    if requires is not None:
        document["requires"] = requires
    return document


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "question",
        choices=("version", "requires", "inputs-sha256", "inputs-listing"),
        help="what to answer about the release line",
    )
    parser.add_argument("stage", choices=STAGES, help="which release line")
    parser.add_argument(
        "--revision", default="HEAD", help="the commit to read (default: HEAD) — never the tree"
    )
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT, help="the checkout to read the revision from"
    )
    parser.add_argument(
        "--architecture",
        help="the platform of a per-platform package, as <os>-<arch> (tools only)",
    )
    arguments = parser.parse_args(argv)

    repository = arguments.repo
    commit = (
        _git(repository, "rev-parse", "--verify", f"{arguments.revision}^{{commit}}")
        .decode()
        .strip()
    )
    if arguments.question == "version":
        print(version_of(environment(repository, commit), arguments.stage))
    elif arguments.question == "requires":
        print(
            json_bytes(requires_of(environment(repository, commit), arguments.stage)).decode(
                "utf-8"
            ),
            end="",
        )
    elif arguments.question == "inputs-sha256":
        print(
            inputs_sha256(
                arguments.stage,
                commit,
                repository=repository,
                architecture=arguments.architecture,
            )
        )
    else:
        print(
            inputs_listing(
                arguments.stage,
                commit,
                repository=repository,
                architecture=arguments.architecture,
            ),
            end="",
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - the command line's entry point
    raise SystemExit(main(sys.argv[1:]))
