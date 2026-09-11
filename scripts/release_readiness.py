#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""What a commit can already say about releasing the three lines.

``scripts/release_lines.py`` says what the three lines *are* — their
versions, their constraints on each other, and the hash that identifies a
stage's inputs. This module holds those answers against what is
**published**, and published means one thing only: a GitHub release of
this repository. No package registry is consulted here and none may be —
a registry is fed by hand, hours or days after a release, so a check that
asked it would be answering about yesterday.

**What this trusts.** The release inventory, the meta documents and the
archives all come off GitHub over TLS, vouched for by the forge and by
nothing else: a GitHub release carries no signed index, which is what a
package registry adds on top when the operator publishes there later. That
is deliberate — the CI is testing what this repository itself produced and
published, not acting as a client of the registry — and it is the reason
``sources`` writes an index out of the files rather than fetching a signed
one.

It does four jobs, one per question a push has to answer:

``check-versions``
    The bump discipline, as a blocker. If the version a stage declares is
    already published, this commit's inputs must be the ones that were
    published — otherwise the first change after a release would silently
    redefine what that version means. The fix is always the same sentence:
    bump the stage's version and thereby say whether the change is a
    patch, a minor or a major.

``plan``
    Which (SDK, workspace, tools) combinations this commit has to be tried
    in, and what each one would prove. Per stage in **both directions**:
    against the published versions of the stage it requires (the range its
    own release would promise) and against the published versions of the
    stage that requires it (what a new version of this line would reach
    without a release above it). A combination is assembled from *exactly
    one* version per stage, so nothing has to be pinned in a device file:
    the build resolves the chain the way a user's build does, and there is
    only one candidate for it to resolve to. ``--published-only`` is the
    tag-time rule, where the checkout of a neighbouring stage no longer
    counts.

``summarize``
    One stage's release-readiness table, out of the plan and the outcomes
    of those builds — one section per direction, each with its own verdict.

``sources``
    A directory of release assets — archive, ``.sha256``, ``.meta.json`` —
    turned into a package source directory by writing the ``index.json``
    a release page does not carry. It is what lets CI feed published
    packages to the workbench as local sources.

Usage::

    release_readiness.py [--releases FILE] [--metas DIR] check-versions
    release_readiness.py [--releases FILE] [--metas DIR] plan --output plan.json
                                                              [--published-only]
    release_readiness.py summarize <stage> --plan plan.json [--results DIR]
    release_readiness.py sources <dir>

``--releases`` reads the release inventory from a file instead of asking
GitHub and ``--metas`` reads the published meta documents from
``<dir>/<tag>/`` instead of downloading them; together they make every
answer here reproducible without a network, which is what the tests use.

Exit status: 0 when the question is answered without a finding, 1 when the
finding is a blocker (``check-versions``) or a combination that was
claimed to work and did not (``summarize``), 2 on a usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_lines  # noqa: E402 - repo-relative import, needs the path above

__all__ = [
    "FAMILY",
    "TAG_PREFIX",
    "Combination",
    "Plan",
    "Published",
    "Stage",
    "accepts",
    "build_gate",
    "build_plan",
    "check_versions",
    "constraint_on",
    "directory_metas",
    "published_of",
    "resolvable",
    "summarize",
    "write_index",
]

#: This repository, seen from ``scripts/``.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The tag that publishes each line. One prefix per line, because three
#: lines share one repository and a bare ``v`` cannot mean three things.
TAG_PREFIX = {"sdk": "v", "workspace": "workspace-v", "tools": "tools-v"}

#: The package family each line publishes. The tools line publishes one
#: package per platform under ``<family>_<platform>``; a constraint names
#: the family.
FAMILY = {
    "sdk": "mcuhome-sdk",
    "workspace": "mcuhome-build-workspace",
    "tools": "mcuhome-build-tools",
}

#: The stage below each one — the one it states a constraint on. The
#: tools end the chain and constrain nothing, which is why they are not a
#: key here.
NEXT_STAGE = {"sdk": "workspace", "workspace": "tools"}

#: The stage above each one — whose constraint decides whether a new
#: version of this line reaches anybody without a release above it.
STAGE_ABOVE = {"workspace": "sdk", "tools": "workspace"}

#: The sidecar a published version has to carry to be usable at all. A
#: package that does not say what it requires cannot be resolved through,
#: which is the workbench's rule and therefore this module's.
META_SUFFIX = release_lines.META_SUFFIX

# --------------------------------------------------------------------------
# The release inventory
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Published:
    """One published version of one line, as its release page shows it."""

    stage: str
    version: str
    tag: str
    #: The asset names of the release. A version whose release carries no
    #: ``<archive>.meta.json`` is not a candidate for anything.
    assets: tuple[str, ...] = ()
    #: The meta document, once it has been read. ``None`` while it has not
    #: been, which is the state every version starts in: the inventory is
    #: one call and the documents are one download each.
    meta: dict | None = None

    @property
    def has_meta(self) -> bool:
        return any(name.endswith(META_SUFFIX) for name in self.assets)

    @property
    def parsed(self) -> Version:
        return Version(self.version)

    def requires(self) -> dict[str, str]:
        """What this version requires of the stage below it."""
        requires = (self.meta or {}).get("requires")
        return requires if isinstance(requires, dict) else {}


#: How the meta documents of one published version are obtained. A
#: function rather than a flag, because there are exactly two answers —
#: download them, or read them out of a directory somebody already
#: downloaded them into — and neither belongs inside the planning.
MetaSource = Callable[[Published], list[dict]]


def _gh(*arguments: str) -> str:
    """One ``gh`` call, its stdout, and no shell."""
    completed = subprocess.run(["gh", *arguments], check=True, stdout=subprocess.PIPE, text=True)
    return completed.stdout


def repository_slug(given: str | None) -> str:
    """``<owner>/<name>`` of the repository whose releases decide."""
    if given:
        return given
    return _gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()


def fetch_releases(slug: str) -> list[dict]:
    """Every release of *slug*, as ``{"tag_name", "draft", "assets"}``.

    Streamed one object per line rather than one array per page:
    ``gh api --paginate`` concatenates the pages, and a reader that
    expected one document would parse the first page and drop the rest.
    Anonymous reads are enough for a public repository, which this is.
    """
    text = _gh(
        "api",
        f"repos/{slug}/releases",
        "--paginate",
        "--jq",
        ".[] | {tag_name, draft, assets: [.assets[].name]}",
    )
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def download_metas(slug: str, work: Path) -> MetaSource:
    """A meta source that downloads each release's sidecars into *work*."""

    def source(entry: Published) -> list[dict]:
        into = work / entry.tag
        into.mkdir(parents=True, exist_ok=True)
        _gh(
            "release",
            "download",
            entry.tag,
            "--repo",
            slug,
            "--pattern",
            f"*{META_SUFFIX}",
            "--dir",
            str(into),
            "--clobber",
        )
        return _read_metas(into)

    return source


def directory_metas(root: Path) -> MetaSource:
    """A meta source that reads ``<root>/<tag>/*.meta.json`` off the disk."""

    def source(entry: Published) -> list[dict]:
        return _read_metas(root / entry.tag)

    return source


def _read_metas(directory: Path) -> list[dict]:
    """Every meta document in *directory*, in file-name order."""
    if not directory.is_dir():
        return []
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob(f"*{META_SUFFIX}"))
    ]


def published_of(stage: str, releases: list[dict]) -> list[Published]:
    """Every published version of *stage*, oldest first.

    A draft is not published — nobody can fetch its assets — and a tag
    whose version part is not a PEP 440 version belongs to something that
    happens to start with the same letters.
    """
    prefix = TAG_PREFIX[stage]
    other_prefixes = tuple(
        value for key, value in TAG_PREFIX.items() if key != stage and value.startswith(prefix)
    )
    found: list[Published] = []
    for release in releases:
        tag = release.get("tag_name")
        if not isinstance(tag, str) or not tag.startswith(prefix) or release.get("draft"):
            continue
        if any(tag.startswith(other) for other in other_prefixes):
            continue
        version = tag[len(prefix) :]
        try:
            Version(version)
        except InvalidVersion:
            continue
        assets = release.get("assets") or []
        found.append(
            Published(
                stage=stage,
                version=version,
                tag=tag,
                assets=tuple(name for name in assets if isinstance(name, str)),
            )
        )
    return sorted(found, key=lambda entry: entry.parsed)


def constraint_on(requires: dict[str, str], family: str) -> str | None:
    """The specifier *requires* states for *family*, host prefix and all.

    A key may be ``<host>/<family>``: a package may require something from
    another host, and the host is not part of the name.
    """
    for key, value in requires.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key == family or key.endswith(f"/{family}"):
            return value
    return None


def accepts(constraint: str | None, version: str) -> bool:
    """Does *constraint* admit *version*? An absent constraint admits nothing."""
    if not constraint:
        return False
    try:
        return SpecifierSet(constraint).contains(version)
    except (InvalidSpecifier, InvalidVersion):
        return False


# --------------------------------------------------------------------------
# check-versions: the bump discipline, as a blocker
# --------------------------------------------------------------------------


def check_versions(
    *,
    revision: str,
    releases: list[dict],
    metas: MetaSource,
    repository: Path = REPO_ROOT,
    out=None,
    errors=None,
) -> int:
    """Hold every declared version against the release that already carries it.

    The only question here is whether a version number still means what it
    meant when it was published. Nothing else about a release is looked at:
    a version that is not published is free, and this commit may change its
    inputs as often as it likes.
    """
    out = sys.stdout if out is None else out
    errors = sys.stderr if errors is None else errors
    environment = release_lines.environment(repository, revision)
    lines = ["## Version bump discipline", ""]
    failures: list[str] = []
    for stage in release_lines.STAGES:
        version = release_lines.version_of(environment, stage)
        tag = f"{TAG_PREFIX[stage]}{version}"
        release = next((one for one in releases if one.get("tag_name") == tag), None)
        if release is None or release.get("draft"):
            lines.append(
                f"- **{stage} {version}** — not published yet: there is no {tag}, so this "
                "commit is free to change its inputs."
            )
            continue
        entry = Published(
            stage=stage,
            version=version,
            tag=tag,
            assets=tuple(name for name in (release.get("assets") or []) if isinstance(name, str)),
        )
        if not entry.has_meta:
            failures.append(
                f"{stage} {version} is already published as {tag}, and that release carries "
                f"no {META_SUFFIX} asset — so this commit's inputs cannot be held against "
                f"it at all.\n"
                f"Fix: bump {stage}.version in {release_lines.ENVIRONMENT_FILE} — patch, "
                "minor or major, whichever this change is."
            )
            lines.append(
                f"- **{stage} {version}** — FAILED: {tag} is published and states no inputs."
            )
            continue
        documents = metas(entry)
        if not documents:
            failures.append(
                f"{stage} {version} is published as {tag} and its meta files could not be "
                f"read, so this commit's inputs cannot be held against them."
            )
            lines.append(f"- **{stage} {version}** — FAILED: {tag}'s meta files are unreadable.")
            continue
        for document in documents:
            architecture = (document.get("package") or {}).get("architecture")
            was = document.get("inputs_sha256")
            here = release_lines.inputs_sha256(
                stage, revision, repository=repository, architecture=architecture
            )
            named = f"{stage} {version}" + (f" ({architecture})" if architecture else "")
            if was == here:
                lines.append(f"- **{named}** — published, and this commit's inputs are its own.")
                continue
            failures.append(
                _drift_message(
                    stage=stage,
                    version=version,
                    tag=tag,
                    architecture=architecture,
                    was=was,
                    here=here,
                    revision=revision,
                    repository=repository,
                )
            )
            lines.append(
                f"- **{named}** — FAILED: published as `{was}`, this commit computes `{here}`."
            )
    lines.append("")
    lines.append(
        "The push is refused: a published version may not be given other inputs."
        if failures
        else (
            "Every declared version is either unpublished or still carries the inputs it "
            "was published with."
        )
    )
    print("\n".join(lines), file=out)
    for message in failures:
        print(f"\n{message}", file=errors)
    return 1 if failures else 0


def _resolves(repository: Path, revision: str) -> bool:
    """Is *revision* a commit this checkout has?

    Asked before an input listing is computed for it, because everything
    that reads a commit answers "this commit carries no <path>" when the
    commit itself is missing — and "the tag is not here" and "the tag
    predates this input" are two different things to be told.
    """
    return (
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{revision}^{{commit}}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _drift_message(
    *,
    stage: str,
    version: str,
    tag: str,
    architecture: str | None,
    was: str | None,
    here: str,
    revision: str,
    repository: Path,
) -> str:
    """The whole refusal: what moved, by how much, and what to do about it."""
    named = f"{stage} {version}" + (f" ({architecture})" if architecture else "")
    lines = [
        f"{named} is already published as {tag}, and this commit's inputs are not the ones "
        "it was published with.",
        f"  published inputs_sha256: {was}",
        f"  this commit computes:    {here}",
    ]
    lines.extend(
        _listing_difference(
            stage=stage,
            tag=tag,
            revision=revision,
            repository=repository,
            architecture=architecture,
        )
    )
    lines.append(
        f"Fix: bump {stage}.version in {release_lines.ENVIRONMENT_FILE} — patch, minor or "
        "major, whichever this change is. The first change after a release has to say which."
    )
    return "\n".join(lines)


def _listing_difference(
    *, stage: str, tag: str, revision: str, repository: Path, architecture: str | None
) -> list[str]:
    """Which input moved, read off the two commits' listings.

    The published hash travels without its listing, so the listing of the
    *tagged commit* is recomputed here. Where that commit is not in the
    checkout — a shallow clone, a tag that was never fetched — the two
    hashes are the whole answer and this says so rather than guessing.
    """
    if not _resolves(repository, tag):
        return [
            f"  (the commit {tag} names is not in this checkout, so the two input listings "
            f"cannot be compared here — git fetch origin tag {tag} to see which input moved)"
        ]
    try:
        before = release_lines.inputs_listing(
            stage, tag, repository=repository, architecture=architecture
        )
        after = release_lines.inputs_listing(
            stage, revision, repository=repository, architecture=architecture
        )
    except SystemExit as gap:
        return [
            f"  ({tag} does not carry every path this stage is identified by today, so the "
            f"two listings would answer different questions: {gap})"
        ]
    except subprocess.CalledProcessError as failure:
        return [f"  (the input listings could not be read: {failure})"]
    was = dict(line.split("\t", 1) for line in before.splitlines() if "\t" in line)
    now = dict(line.split("\t", 1) for line in after.splitlines() if "\t" in line)
    lines = [f"What changed since {tag}:"]
    for key in sorted(set(was) | set(now)):
        old, new = was.get(key), now.get(key)
        if old == new:
            continue
        if old is None:
            lines.append(f"  + {key}\t{new}")
        elif new is None:
            lines.append(f"  - {key}\t{old}")
        else:
            lines.append(f"  ~ {key}\t{old} -> {new}")
    return lines


# --------------------------------------------------------------------------
# plan: which combinations this commit has to be tried in
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    """One stage of a combination: which package it is, and where it is from."""

    source: str  # "checkout" or "published"
    version: str
    tag: str | None = None

    def document(self) -> dict:
        entry = {"source": self.source, "version": self.version}
        if self.tag:
            entry["tag"] = self.tag
        return entry

    def label(self) -> str:
        return self.version if self.source == "published" else f"{self.version} (this commit)"


@dataclass
class Combination:
    """One (SDK, workspace, tools) triple, under the identity it is built with.

    *consistent* is whether the triple is one somebody could actually
    assemble: every stage's own constraint admits the one below it. An
    inconsistent triple is still built — what a broken chain does is worth
    seeing — but it is never a failure, because nothing claims it works.
    """

    identifier: str
    sdk: Stage
    workspace: Stage
    tools: Stage
    consistent: bool = True
    reason: str = ""

    def triple(self) -> tuple:
        return tuple(
            (stage.source, stage.version) for stage in (self.sdk, self.workspace, self.tools)
        )

    def document(self) -> dict:
        return {
            "id": self.identifier,
            "label": (
                f"SDK {self.sdk.label()} + workspace {self.workspace.label()} + tools "
                f"{self.tools.label()}"
            ),
            "sdk": self.sdk.document(),
            "workspace": self.workspace.document(),
            "tools": self.tools.document(),
            # What the firmware job reads: a leg that builds an
            # inconsistent triple reports its outcome and does not fail.
            "required": self.consistent,
            "reason": self.reason,
        }


@dataclass
class Row:
    """One line of a stage's readiness table."""

    role: str
    subject: str
    combination: str | None = None
    required: bool = False
    note: str = ""

    def document(self) -> dict:
        return {
            "role": self.role,
            "subject": self.subject,
            "combination": self.combination,
            "required": self.required,
            "note": self.note,
        }


@dataclass
class Plan:
    """Everything the three assessment jobs need, decided once.

    ``catalogue`` holds both directions D10.9 asks for, per stage:
    ``down`` is the line against the versions of the stage it requires,
    ``up`` against the versions of the stage that requires it. The SDK has
    no stage above it and the tools none below, so each of them has one
    direction; the workspace has both.
    """

    declared: dict[str, str]
    combinations: list[Combination] = field(default_factory=list)
    catalogue: dict[str, dict[str, list[Row]]] = field(default_factory=dict)
    verdicts: dict[str, dict[str, str]] = field(default_factory=dict)

    def document(self) -> dict:
        return {
            "declared": self.declared,
            "combinations": [one.document() for one in self.combinations],
            "catalogue": {
                stage: {
                    direction: [row.document() for row in rows]
                    for direction, rows in directions.items()
                }
                for stage, directions in self.catalogue.items()
            },
            "verdicts": self.verdicts,
        }


class _Combinations:
    """The combinations asked for, deduplicated, in the order they were asked.

    Three stages and two directions ask for overlapping triples — today
    they all ask for the same one — and a triple is built once. The
    identity is the triple, so an identifier never names two different sets
    of packages.
    """

    def __init__(self, consistency) -> None:
        self.found: list[Combination] = []
        self._by_triple: dict[tuple, Combination] = {}
        self._consistency = consistency

    def add(self, sdk: Stage, workspace: Stage, tools: Stage) -> Combination:
        candidate = Combination("", sdk, workspace, tools)
        triple = candidate.triple()
        if triple in self._by_triple:
            return self._by_triple[triple]
        everything_here = all(one.source == "checkout" for one in (sdk, workspace, tools))
        candidate.identifier = (
            "checkout" if everything_here else f"combination-{len(self.found) + 1}"
        )
        candidate.consistent, candidate.reason = self._consistency(sdk, workspace, tools)
        self._by_triple[triple] = candidate
        self.found.append(candidate)
        return candidate


def resolvable(
    inventory: dict[str, list[Published]], metas: MetaSource
) -> dict[str, list[Published]]:
    """The published versions anything may resolve to, with their meta read.

    A version whose release carries no ``<archive>.meta.json`` says nothing
    about what it requires, so no chain can be resolved through it — the
    workbench refuses such a version as a candidate and this module answers
    the same way. Reading the documents is one download per version, which
    is why it happens here rather than in the inventory.
    """
    return {
        stage: [
            Published(
                stage=one.stage,
                version=one.version,
                tag=one.tag,
                assets=one.assets,
                meta=(metas(one) or [None])[0],
            )
            for one in entries
            if one.has_meta
        ]
        for stage, entries in inventory.items()
    }


def _ends(entries: list[Published]) -> list[tuple[Published, str]]:
    """The lowest and the highest of *entries*, each with what it is."""
    if not entries:
        return []
    if len(entries) == 1:
        return [(entries[0], "the only published")]
    return [(entries[0], "the lowest published"), (entries[-1], "the highest published")]


def _row_for(role: str, subject: str, combination: Combination | None, note: str = "") -> Row:
    """One table line, required exactly when the triple claims to work.

    A row with no combination is one nothing could be assembled for — at
    tag time, where only published versions count, that is the ordinary
    answer for a stage nothing published satisfies yet.
    """
    if combination is None:
        return Row(role=role, subject=subject, combination=None, required=False, note=note)
    parts = [part for part in (note, combination.reason) if part]
    return Row(
        role=role,
        subject=subject,
        combination=combination.identifier,
        required=combination.consistent,
        note="; ".join(parts),
    )


def build_plan(
    *,
    revision: str,
    releases: list[dict],
    metas: MetaSource,
    repository: Path = REPO_ROOT,
    published_only: bool = False,
) -> Plan:
    """The whole catalogue: three stages, both directions, their combinations.

    *published_only* is the tag-time rule. At a push the checkout of a
    neighbouring stage counts as released — it is about to be — and the
    catalogue tries it; at a tag only what is actually published may
    decide whether a release goes out, so those rows are left out and a
    stage nothing published satisfies is a row with no combination and a
    verdict that says why.
    """
    environment = release_lines.environment(repository, revision)
    declared = {
        stage: release_lines.version_of(environment, stage) for stage in release_lines.STAGES
    }
    here = {stage: Stage("checkout", version) for stage, version in declared.items()}
    inventory = {stage: published_of(stage, releases) for stage in release_lines.STAGES}
    candidates = resolvable(inventory, metas)
    published_by_version = {
        stage: {one.version: one for one in entries} for stage, entries in candidates.items()
    }

    def requires_of_stage(name: str, stage: Stage) -> str | None:
        """What the package this stage names requires of the stage below it."""
        family = FAMILY[NEXT_STAGE[name]]
        if stage.source == "checkout":
            return constraint_on(release_lines.requires_of(environment, name), family)
        entry = published_by_version[name].get(stage.version)
        return constraint_on(entry.requires(), family) if entry is not None else None

    def consistency(sdk: Stage, workspace: Stage, tools: Stage) -> tuple[bool, str]:
        """Does this triple hold together, and if not, where does it break?"""
        faults = []
        for name, upper, lower in (("sdk", sdk, workspace), ("workspace", workspace, tools)):
            constraint = requires_of_stage(name, upper)
            if not accepts(constraint, lower.version):
                faults.append(
                    f"{FAMILY[name]} {upper.label()} requires {constraint!r} of "
                    f"{FAMILY[NEXT_STAGE[name]]} and this combination holds {lower.version}"
                )
        if faults:
            return False, "; ".join(faults) + " — built to see what happens, not required to work"
        return True, ""

    combinations = _Combinations(consistency)
    plan = Plan(declared=declared)

    def newest_satisfying(stage: str, constraint: str | None) -> Published | None:
        """The newest published version of *stage* inside *constraint*."""
        satisfying = [one for one in candidates[stage] if accepts(constraint, one.version)]
        return satisfying[-1] if satisfying else None

    def tools_under(workspace: Stage) -> tuple[Stage | None, str]:
        """The tools package a workspace's own constraint resolves to."""
        constraint = requires_of_stage("workspace", workspace)
        newest = newest_satisfying("tools", constraint)
        if newest is not None:
            return Stage("published", newest.version, newest.tag), ""
        if published_only:
            return None, f"no published {FAMILY['tools']} satisfies {constraint!r}"
        return here[
            "tools"
        ], f"no published {FAMILY['tools']} satisfies {constraint!r} — this commit's are used"

    def sdk_over(workspace: Stage) -> tuple[Stage | None, str]:
        """The SDK a workspace is built under: the newest published that takes it."""
        newest = next(
            (
                one
                for one in reversed(candidates["sdk"])
                if accepts(constraint_on(one.requires(), FAMILY["workspace"]), workspace.version)
            ),
            None,
        )
        if newest is not None:
            return Stage("published", newest.version, newest.tag), ""
        if published_only:
            return (
                None,
                f"no published {FAMILY['sdk']} accepts {FAMILY['workspace']} {workspace.version}",
            )
        return here[
            "sdk"
        ], f"no published {FAMILY['sdk']} accepts {workspace.version} — this commit's is used"

    def down_triple(stage: str, below: Stage) -> tuple[Combination | None, str]:
        """The triple that tries *stage* of this commit over one below it."""
        if stage == "sdk":
            tools, note = tools_under(below)
            if tools is None:
                return None, note
            return combinations.add(here["sdk"], below, tools), note
        # stage == "workspace": the tools are what varies, and an SDK has
        # to sit on top for anything to build at all.
        sdk, note = sdk_over(here["workspace"])
        if sdk is None:
            return None, note
        return combinations.add(sdk, here["workspace"], below), note

    def up_triple(stage: str, above: Stage) -> tuple[Combination | None, str]:
        """The triple that tries *stage* of this commit under one above it."""
        if stage == "workspace":
            tools, note = tools_under(here["workspace"])
            if tools is None:
                return None, note
            return combinations.add(above, here["workspace"], tools), note
        # stage == "tools": the workspace above decides, and the SDK above
        # that one follows from the workspace.
        sdk, note = sdk_over(above)
        if sdk is None:
            return None, note
        return combinations.add(sdk, above, here["tools"]), note

    for stage in ("sdk", "workspace"):
        rows, verdict = _downward(
            stage=stage,
            environment=environment,
            declared=declared,
            inventory=inventory,
            candidates=candidates,
            triple=down_triple,
            published_only=published_only,
        )
        plan.catalogue.setdefault(stage, {})["down"] = rows
        plan.verdicts.setdefault(stage, {})["down"] = verdict
    for stage in ("workspace", "tools"):
        rows, verdict = _upward(
            stage=stage,
            environment=environment,
            declared=declared,
            here=here,
            candidates=candidates,
            triple=up_triple,
            published_only=published_only,
        )
        plan.catalogue.setdefault(stage, {})["up"] = rows
        plan.verdicts.setdefault(stage, {})["up"] = verdict
    plan.combinations = combinations.found
    return plan


def _downward(
    *,
    stage: str,
    environment: dict,
    declared: dict[str, str],
    inventory: dict[str, list[Published]],
    candidates: dict[str, list[Published]],
    triple,
    published_only: bool,
) -> tuple[list[Row], str]:
    """One line against the versions of the stage it requires.

    A release promises that every version inside the range it declares
    works, so the two ends of that range are what have to be tried. Where
    the range has no published member the line cannot be released at all —
    not because something is broken, but because the thing it requires does
    not exist yet. That is the verdict that decides the order a first
    release of all three lines has to go in.
    """
    below = NEXT_STAGE[stage]
    constraint = constraint_on(release_lines.requires_of(environment, stage), FAMILY[below])
    satisfying = [one for one in candidates[below] if accepts(constraint, one.version)]
    rows: list[Row] = []
    for entry, role in _ends(satisfying):
        combination, note = triple(stage, Stage("published", entry.version, entry.tag))
        rows.append(
            _row_for(
                f"{role} {FAMILY[below]}", f"{FAMILY[below]} {entry.version}", combination, note
            )
        )
    unpublished = declared[below] not in {one.version for one in inventory[below]}
    if not published_only and accepts(constraint, declared[below]) and unpublished:
        combination, note = triple(stage, Stage("checkout", declared[below]))
        rows.append(
            _row_for(
                f"the {FAMILY[below]} of this commit",
                f"{FAMILY[below]} {declared[below]} (this commit)",
                combination,
                note,
            )
        )
    if satisfying:
        verdict = (
            f"{len(satisfying)} published {FAMILY[below]} release(s) satisfy {constraint!r}; "
            f"the ends of that range are what a {FAMILY[stage]} release promises."
        )
    else:
        verdict = (
            f"{FAMILY[stage]} release blocked until a {FAMILY[below]} satisfying "
            f"{constraint!r} is published."
        )
    return rows, verdict


def _upward(
    *,
    stage: str,
    environment: dict,
    declared: dict[str, str],
    here: dict[str, Stage],
    candidates: dict[str, list[Published]],
    triple,
    published_only: bool,
) -> tuple[list[Row], str]:
    """One line held against the published versions of the stage above it.

    The declared version states the intent. A patch reaches every published
    version above whose constraint already includes it, so those have to
    pass; a minor or a major reaches none of them, and then the only thing
    that has to pass is the stage above as this commit has it — which then
    has to be released together with this one.
    """
    above = STAGE_ABOVE[stage]
    family = FAMILY[stage]
    version = declared[stage]
    accepting = [
        one for one in candidates[above] if accepts(constraint_on(one.requires(), family), version)
    ]
    rows: list[Row] = []
    for entry, role in _ends(accepting):
        combination, note = triple(stage, Stage("published", entry.version, entry.tag))
        rows.append(
            _row_for(
                f"{role} {FAMILY[above]}", f"{FAMILY[above]} {entry.version}", combination, note
            )
        )
    if not published_only:
        combination, note = triple(stage, Stage("checkout", declared[above]))
        rows.append(
            _row_for(
                f"the {FAMILY[above]} of this commit",
                f"{FAMILY[above]} {declared[above]} (this commit)",
                combination,
                note,
            )
        )
    own = constraint_on(release_lines.requires_of(environment, above), family)
    if accepting:
        verdict = (
            f"Patch release possible: {len(accepting)} published {FAMILY[above]} release(s) "
            f"already accept {version}."
        )
    elif not candidates[above]:
        verdict = (
            f"No published {FAMILY[above]} says what it requires, so there is nothing for "
            f"{version} to be compatible with yet — only the {FAMILY[above]} of this commit "
            "can say anything about it."
        )
    elif accepts(own, version):
        verdict = (
            f"Minor needed above: no published {FAMILY[above]} accepts {version}, and the "
            f"one in this commit does ({own!r}) — the two have to be released together."
        )
    else:
        verdict = (
            f"Incompatible with everything: no published {FAMILY[above]} accepts {version}, "
            f"and this commit's own constraint {own!r} does not either."
        )
    return rows, verdict


# --------------------------------------------------------------------------
# gate: what a tag has to pass before anything is published
# --------------------------------------------------------------------------
#
# The catalogue above, under the rule a tag lives by — only published
# versions count — with three things a tag needs and a push does not:
#
#   1. the tagged line's own package is the one being RELEASED. It is still
#      built from this commit, but at the version the tag names and with no
#      local suffix, and it is what gets uploaded. The combinations say so,
#      because the firmware jobs and the publish job read them.
#   2. a line whose own requirement nothing published satisfies is BLOCKED.
#      Publishing a package no chain can be resolved through is publishing
#      a dead end, and the verdict already says which line to release
#      first.
#   3. a line that nothing published sits ON TOP of is a line start, not a
#      failure. The first build workspace package exists before any SDK
#      asks for it, so it is tried under the SDK of this commit and the
#      verdict says that no release uses it yet.
#
# Only the tagged stage's half of the catalogue travels: the other two
# lines' rows are about a release nobody is cutting, and building firmware
# for them would be runner time spent on somebody else's question.


def _newest_accepting(candidates: list[Published], family: str, version: str) -> Published | None:
    """The newest published package whose own constraint admits *version*."""
    return next(
        (
            one
            for one in reversed(candidates)
            if accepts(constraint_on(one.requires(), family), version)
        ),
        None,
    )


def _below(
    *,
    stage: str,
    declared: dict[str, str],
    environment: dict,
    candidates: dict[str, list[Published]],
) -> tuple[dict[str, dict], str]:
    """Every stage under the tagged one, resolved the way a user resolves it.

    Down the chain, newest published satisfying each constraint — the
    answer a workbench gives. A missing link is what BLOCKS a release:
    publishing a package whose own requirement nothing published satisfies
    is publishing a dead end, and it is far better to learn that before the
    archives exist than from the first person who tries to build with it.
    """
    stages: dict[str, dict] = {}
    owner = stage
    requires = release_lines.requires_of(environment, stage)
    owner_version = declared[stage]
    while owner in NEXT_STAGE:
        lower = NEXT_STAGE[owner]
        constraint = constraint_on(requires, FAMILY[lower])
        satisfying = [one for one in candidates[lower] if accepts(constraint, one.version)]
        if not satisfying:
            where = (
                ""
                if owner == stage
                else f"{FAMILY[owner]} {owner_version} is what this release resolves to, and "
            )
            return stages, (
                f"{where}{FAMILY[owner]} {owner_version} requires {FAMILY[lower]} "
                f"{constraint!r}, and nothing published satisfies it.\n"
                f"{FAMILY[stage]} {declared[stage]} cannot be released: a package no chain can "
                f"be resolved through is a dead end.\n"
                f"Release the {FAMILY[lower]} line first — tag {TAG_PREFIX[lower]}<version> "
                "with a version that constraint admits — and cut this one afterwards."
            )
        newest = satisfying[-1]
        stages[lower] = {"source": "published", "version": newest.version, "tag": newest.tag}
        owner, requires, owner_version = lower, newest.requires(), newest.version
    return stages, ""


def _above(
    *, stage: str, declared: dict[str, str], candidates: dict[str, list[Published]]
) -> tuple[dict[str, dict], list[str]]:
    """Every stage over the tagged one: published where one takes it, else this commit.

    A line starts somewhere. The first build workspace package exists before
    any SDK asks for it, so "nothing published accepts this" is a verdict
    and not a failure — but it has to be said, because it means the release
    above has to follow.
    """
    stages: dict[str, dict] = {}
    notes: list[str] = []
    lower, lower_version = stage, declared[stage]
    while lower in STAGE_ABOVE:
        upper = STAGE_ABOVE[lower]
        newest = _newest_accepting(candidates[upper], FAMILY[lower], lower_version)
        if newest is None:
            stages[upper] = {"source": "checkout", "version": declared[upper]}
            notes.append(
                f"no published {FAMILY[upper]} accepts {FAMILY[lower]} {lower_version} — "
                "this commit's is used"
            )
            lower, lower_version = upper, declared[upper]
            continue
        stages[upper] = {"source": "published", "version": newest.version, "tag": newest.tag}
        lower, lower_version = upper, newest.version
    return stages, notes


def build_gate(
    *,
    stage: str,
    revision: str,
    releases: list[dict],
    metas: MetaSource,
    repository: Path = REPO_ROOT,
) -> dict:
    """What a tag of *stage* has to pass, as the document the workflow reads.

    The catalogue is the one every push writes, under the rule a tag lives
    by — only published versions count — trimmed to the line being released:
    the other two lines' rows are about a release nobody is cutting, and
    building firmware for them would be runner time spent on somebody else's
    question. Three things a tag needs and a push does not are added.

    **The tagged line's package is the one being RELEASED.** It is still
    built from this commit, but at the version the tag names and with no
    local suffix, and it is what gets uploaded — so the combinations say
    ``release`` rather than ``checkout`` for it, and the publish job names
    its assets from that.

    **A line whose own requirement nothing published satisfies is BLOCKED**,
    before anything is built (:func:`_below`).

    **A line nothing published sits on top of is a line start**, not a
    failure: where the catalogue's published-only rows leave nothing to
    build at all, one combination is assembled out of the chain around this
    release — published below, this commit's above — and the verdict says
    that no release uses it yet.
    """
    environment = release_lines.environment(repository, revision)
    declared = {one: release_lines.version_of(environment, one) for one in release_lines.STAGES}
    candidates = resolvable(
        {one: published_of(one, releases) for one in release_lines.STAGES}, metas
    )
    below, refusal = _below(
        stage=stage, declared=declared, environment=environment, candidates=candidates
    )
    if refusal:
        return {
            "declared": declared,
            "catalogue": {},
            "verdicts": {},
            "combinations": [],
            "blocked": refusal,
        }

    plan = build_plan(
        revision=revision,
        releases=releases,
        metas=metas,
        repository=repository,
        published_only=True,
    )
    document = plan.document()
    by_id = {one["id"]: one for one in document["combinations"]}
    catalogue: dict[str, list[dict]] = {}
    wanted: list[dict] = []
    for direction, rows in plan.catalogue.get(stage, {}).items():
        catalogue[direction] = [row.document() for row in rows]
        for row in rows:
            if row.combination and by_id[row.combination] not in wanted:
                wanted.append(by_id[row.combination])

    if not wanted:
        combination, row = _chain(
            stage=stage, declared=declared, below=below, candidates=candidates
        )
        wanted.append(combination)
        # Into the upward half, which is the one a line start is about: the
        # downward half already said which published version this release
        # resolves to, and repeating the row there would list a statement
        # about the stage above under the heading of the stage below.
        direction = "up" if "up" in catalogue else next(iter(catalogue), "up")
        catalogue.setdefault(direction, []).append(row)

    for combination in wanted:
        if combination[stage]["source"] == "checkout":
            combination[stage]["source"] = "release"

    return {
        "declared": declared,
        "catalogue": catalogue,
        "verdicts": plan.verdicts.get(stage, {}),
        "combinations": wanted,
        "verify": _verify(stage=stage, catalogue=catalogue, combinations=wanted),
    }


def _chain(
    *,
    stage: str,
    declared: dict[str, str],
    below: dict[str, dict],
    candidates: dict[str, list[Published]],
) -> tuple[dict, dict]:
    """The one combination a release is tried in when nothing published can.

    Built here rather than by the planner because the planner's tag-time
    rule is exactly that this triple does not count — and for the line being
    released it is the only thing that can say anything at all.
    """
    above, notes = _above(stage=stage, declared=declared, candidates=candidates)
    stages = {
        **{one: {"source": "checkout", "version": declared[one]} for one in release_lines.STAGES},
        **below,
        **above,
        stage: {"source": "release", "version": declared[stage]},
    }
    combination = {
        "id": "line-start",
        "label": " + ".join(
            f"{FAMILY[one]} {stages[one]['version']}"
            + ("" if stages[one]["source"] == "published" else " (this commit)")
            for one in release_lines.STAGES
        ),
        "required": True,
        "reason": "",
        **stages,
    }
    upper = STAGE_ABOVE.get(stage)
    row = {
        "role": f"the {FAMILY[upper]} of this commit" if upper else "the chain of this commit",
        "subject": (
            f"{FAMILY[upper]} {declared[upper]} (this commit)" if upper else combination["label"]
        ),
        "combination": "line-start",
        "required": True,
        "note": "; ".join(notes),
    }
    return combination, row


def _verify(*, stage: str, catalogue: dict[str, list[dict]], combinations: list[dict]) -> dict:
    """Which image the release is verified in, once it is published.

    The chain a user would resolve is the newest of everything, so the last
    row of the direction that decides this line is the one worth verifying:
    for an SDK release the highest published build workspace it admits, for
    a build workspace release the newest published SDK that takes it.
    """
    direction = "down" if stage == "sdk" else "up"
    last = next((row for row in reversed(catalogue.get(direction, [])) if row["combination"]), None)
    chosen = last["combination"] if last else (combinations[-1]["id"] if combinations else "")
    if stage == "workspace":
        return {"mode": "pushed-image", "combination": chosen}
    if stage == "sdk":
        found = next((one for one in combinations if one["id"] == chosen), None)
        return {
            "mode": "published-image",
            "combination": chosen,
            "workspace_version": found["workspace"]["version"] if found else "",
        }
    return {
        "mode": "skip",
        "combination": chosen,
        "reason": (
            "a build-environment image delivers a build workspace package and the build tools "
            "that package accepts, and no published image can declare tools that did not exist "
            "when it was assembled. Take them into an image with the revision dispatch "
            "(workspace_version + revision) — that run verifies the result."
        ),
    }


# --------------------------------------------------------------------------
# summarize: one stage's readiness table
# --------------------------------------------------------------------------


def read_results(directory: Path | None) -> dict[tuple[str, str], dict]:
    """Every build outcome under *directory*, by combination and platform.

    One file per build — ``result-<combination>-<architecture>.json``,
    written by the job that ran it — so a combination with no file was not
    built, which is a different answer from "built and failed" and has to
    stay one. Searched recursively because a downloaded artifact set
    arrives one directory per artifact.
    """
    found: dict[tuple[str, str], dict] = {}
    if directory is None or not directory.is_dir():
        return found
    for path in sorted(directory.rglob("result-*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        identifier = document.get("combination")
        architecture = document.get("architecture")
        if isinstance(identifier, str) and isinstance(architecture, str):
            found[(identifier, architecture)] = document
    return found


#: What each direction of the catalogue is about, as a heading. "Down" is
#: the stage a line requires, "up" the stage that requires it.
DIRECTION_HEADING = {
    "down": "Against what it requires",
    "up": "Against what requires it",
}


def summarize(*, stage: str, plan: dict, results: Path | None, out=None, errors=None) -> int:
    """One stage's release-readiness table, and whether it is a failure.

    Both directions D10.9 asks for, each with its own verdict: what this
    line requires (its own constraint against the published versions of the
    stage below) and what requires it (the published versions above whose
    constraint already takes this one). The SDK has only the first and the
    tools only the second.

    A row fails the job when it claims validity and did not build: a
    combination assembled from published versions and this commit's own
    definition is one somebody could put together today, so a broken one is
    a broken promise rather than a warning. A row whose chain does not hold
    together in the first place claims nothing and is reported.
    """
    out = sys.stdout if out is None else out
    errors = sys.stderr if errors is None else errors
    outcomes = read_results(results)
    labels = {one["id"]: one["label"] for one in plan.get("combinations", [])}
    directions = plan.get("catalogue", {}).get(stage, {})
    verdicts = plan.get("verdicts", {}).get(stage, {})
    lines = [f"## Release readiness: {FAMILY[stage]} {plan['declared'][stage]}", ""]
    broken: list[str] = []
    for direction in ("down", "up"):
        if direction not in directions:
            continue
        rows = directions[direction]
        lines.append(f"### {DIRECTION_HEADING[direction]}")
        lines.append("")
        lines.append(verdicts.get(direction, ""))
        lines.append("")
        if not rows:
            lines.append(
                "Nothing to try here: no version of that stage is available to try this "
                "one against."
            )
            lines.append("")
            continue
        lines.append("| tried against | combination | result | note |")
        lines.append("|---|---|---|---|")
        for row in rows:
            identifier = row.get("combination")
            built = [
                (architecture, document)
                for (one, architecture), document in sorted(outcomes.items())
                if one == identifier
            ]
            if identifier is None:
                result = "nothing to build it with"
            elif not built:
                result = "not evaluated in this run"
            else:
                result = ", ".join(
                    f"{architecture}: {document.get('outcome', 'unknown')}"
                    for architecture, document in built
                )
                if row.get("required") and any(
                    document.get("outcome") != "success" for _, document in built
                ):
                    broken.append(f"{row.get('subject')} — {labels.get(identifier, identifier)}")
            lines.append(
                f"| {row.get('subject')} | {labels.get(identifier, identifier) or '—'} | "
                f"{result} | {row.get('note') or ''} |"
            )
        lines.append("")
    if broken:
        lines.append(
            "**Failed:** a combination this line claims to be valid did not build — "
            + "; ".join(broken)
        )
    print("\n".join(lines), file=out)
    for one in broken:
        print(
            f"release-readiness: {stage}: a claimed-valid combination did not build: {one}",
            file=errors,
        )
    return 1 if broken else 0


# --------------------------------------------------------------------------
# sources: release assets turned into a package source directory
# --------------------------------------------------------------------------


def write_index(directory: Path) -> dict:
    """Write the ``index.json`` that makes *directory* a package source.

    A GitHub release carries the archive, its ``.sha256`` and its
    ``.meta.json`` and nothing else — an index belongs to a source, and a
    release page is not one. The workbench needs one to resolve a version
    at all, so it is written here out of what the files themselves say:
    the name and the version from the meta document, the bytes from the
    archive, and the checksum sidecar held against them on the way.
    """
    packages: dict[str, dict] = {}
    for meta_path in sorted(directory.glob(f"*{META_SUFFIX}")):
        archive = directory / meta_path.name[: -len(META_SUFFIX)]
        if not archive.is_file():
            raise SystemExit(f"{meta_path.name} has no archive beside it ({archive.name})")
        document = json.loads(meta_path.read_text(encoding="utf-8"))
        package = document.get("package") or {}
        family = package.get("name")
        version = package.get("version")
        architecture = package.get("architecture")
        if not isinstance(family, str) or not isinstance(version, str):
            raise SystemExit(f"{meta_path.name} names no package")
        name = f"{family}_{architecture}" if architecture else family
        expected = f"{name}-{version}.tar.zst"
        if archive.name != expected:
            raise SystemExit(
                f"{archive.name} describes itself as {name} {version}, and that package's "
                f"file is called {expected}"
            )
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        checksum = directory / f"{archive.name}.sha256"
        # Refused rather than skipped: every release publishes the checksum
        # beside the archive, so an absent one means the download was
        # incomplete or the assets are not what they look like — and an
        # index written over it would state a hash nothing corroborated.
        if not checksum.is_file():
            raise SystemExit(
                f"{archive.name} has no {checksum.name} beside it, so its bytes are "
                "vouched for by nothing"
            )
        stated = checksum.read_text(encoding="utf-8").split()[0]
        if stated != digest:
            raise SystemExit(f"{archive.name} hashes to {digest} and {checksum.name} says {stated}")
        packages.setdefault(name, {})[version] = {
            "file": archive.name,
            "sha256": digest,
            "size": archive.stat().st_size,
            "meta_file": {
                "file": meta_path.name,
                "sha256": hashlib.sha256(meta_path.read_bytes()).hexdigest(),
                "size": meta_path.stat().st_size,
            },
        }
    document = {"packages": packages}
    (directory / "index.json").write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return document


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--releases", type=Path, help="read the release inventory from this file")
    parser.add_argument(
        "--metas", type=Path, help="read published meta documents from <dir>/<tag>/ instead"
    )
    parser.add_argument(
        "--repository-slug", help="<owner>/<name> of the repository whose releases decide"
    )
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT, help="the checkout to read the revision from"
    )
    parser.add_argument("--revision", default="HEAD", help="the commit to read (default: HEAD)")
    sub = parser.add_subparsers(dest="question", required=True)

    sub.add_parser("check-versions", help="the bump discipline, as a blocker")

    planner = sub.add_parser("plan", help="which combinations this commit has to be tried in")
    planner.add_argument("--output", type=Path, required=True, help="where the plan is written")
    planner.add_argument(
        "--published-only",
        action="store_true",
        help="the tag-time rule: only published versions of the other stages count, so the "
        "rows that try this commit's own neighbours are left out",
    )

    summary = sub.add_parser("summarize", help="one stage's release-readiness table")
    summary.add_argument("stage", choices=release_lines.STAGES)
    summary.add_argument("--plan", type=Path, required=True, help="the plan to read")
    summary.add_argument("--results", type=Path, help="directory holding the build outcomes")

    index = sub.add_parser("sources", help="write index.json for a directory of release assets")
    index.add_argument("directory", type=Path)

    arguments = parser.parse_args(argv)

    if arguments.question == "sources":
        print(json.dumps(write_index(arguments.directory), indent=2, sort_keys=True))
        return 0

    if arguments.question == "summarize":
        return summarize(
            stage=arguments.stage,
            plan=json.loads(arguments.plan.read_text(encoding="utf-8")),
            results=arguments.results,
        )

    if arguments.releases is not None:
        releases = json.loads(arguments.releases.read_text(encoding="utf-8"))
        if not isinstance(releases, list):
            raise SystemExit(f"{arguments.releases} is not a list of releases")
    else:
        releases = fetch_releases(repository_slug(arguments.repository_slug))

    work = Path(tempfile.mkdtemp(prefix="mcuhome-release-readiness-"))
    try:
        if arguments.metas is not None:
            metas: MetaSource = directory_metas(arguments.metas)
        else:
            metas = download_metas(repository_slug(arguments.repository_slug), work)
        if arguments.question == "check-versions":
            return check_versions(
                revision=arguments.revision,
                releases=releases,
                metas=metas,
                repository=arguments.repo,
            )
        plan = build_plan(
            revision=arguments.revision,
            releases=releases,
            metas=metas,
            repository=arguments.repo,
            published_only=arguments.published_only,
        )
        arguments.output.write_text(
            json.dumps(plan.document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(plan.document(), indent=2, sort_keys=True))
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover - the command line's entry point
    raise SystemExit(main(sys.argv[1:]))
