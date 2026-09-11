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
    in, and what each one would prove. A combination is assembled from
    *exactly one* version per stage, so nothing has to be pinned in a
    device file: the build resolves the chain the way a user's build does,
    and there is only one candidate for it to resolve to.

``summarize``
    One stage's release-readiness table, out of the plan and the outcomes
    of those builds.

``sources``
    A directory of release assets — archive, ``.sha256``, ``.meta.json`` —
    turned into a package source directory by writing the ``index.json``
    a release page does not carry. It is what lets CI feed published
    packages to the workbench as local sources.

Usage::

    release_readiness.py [--releases FILE] [--metas DIR] check-versions
    release_readiness.py [--releases FILE] [--metas DIR] plan --output plan.json
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
    "build_plan",
    "check_versions",
    "constraint_on",
    "directory_metas",
    "published_of",
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
    try:
        before = release_lines.inputs_listing(
            stage, tag, repository=repository, architecture=architecture
        )
        after = release_lines.inputs_listing(
            stage, revision, repository=repository, architecture=architecture
        )
    except (subprocess.CalledProcessError, SystemExit):
        return [
            f"  (the commit {tag} names is not in this checkout, so the two input listings "
            "cannot be compared here — fetch the tag to see which input moved)"
        ]
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
    """One (SDK, workspace, tools) triple, under the identity it is built with."""

    identifier: str
    sdk: Stage
    workspace: Stage
    tools: Stage

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
    """Everything the three assessment jobs need, decided once."""

    declared: dict[str, str]
    combinations: list[Combination] = field(default_factory=list)
    catalogue: dict[str, list[Row]] = field(default_factory=dict)
    verdicts: dict[str, str] = field(default_factory=dict)

    def document(self) -> dict:
        return {
            "declared": self.declared,
            "combinations": [one.document() for one in self.combinations],
            "catalogue": {
                stage: [row.document() for row in rows] for stage, rows in self.catalogue.items()
            },
            "verdicts": self.verdicts,
        }


class _Combinations:
    """The combinations asked for, deduplicated, in the order they were asked.

    Three stages ask for overlapping triples — today all three ask for the
    same one — and a triple is built once. The identity is the triple, so
    an identifier never names two different sets of packages.
    """

    def __init__(self) -> None:
        self.found: list[Combination] = []
        self._by_triple: dict[tuple, Combination] = {}

    def add(self, sdk: Stage, workspace: Stage, tools: Stage) -> str:
        candidate = Combination("", sdk, workspace, tools)
        triple = candidate.triple()
        if triple in self._by_triple:
            return self._by_triple[triple].identifier
        everything_here = all(one.source == "checkout" for one in (sdk, workspace, tools))
        candidate.identifier = (
            "checkout" if everything_here else f"combination-{len(self.found) + 1}"
        )
        self._by_triple[triple] = candidate
        self.found.append(candidate)
        return candidate.identifier


def _ends(entries: list[Published]) -> list[tuple[Published, str]]:
    """The lowest and the highest of *entries*, each with what it is."""
    if not entries:
        return []
    if len(entries) == 1:
        return [(entries[0], "the only published")]
    return [(entries[0], "the lowest published"), (entries[-1], "the highest published")]


def build_plan(
    *,
    revision: str,
    releases: list[dict],
    metas: MetaSource,
    repository: Path = REPO_ROOT,
) -> Plan:
    """The whole catalogue: three stages, their candidates, their combinations."""
    environment = release_lines.environment(repository, revision)
    declared = {
        stage: release_lines.version_of(environment, stage) for stage in release_lines.STAGES
    }
    here = {stage: Stage("checkout", version) for stage, version in declared.items()}
    inventory = {stage: published_of(stage, releases) for stage in release_lines.STAGES}
    candidates = {
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
    combinations = _Combinations()
    plan = Plan(declared=declared)

    def tools_for(requires: dict[str, str]) -> tuple[Stage, str]:
        """The tools package a workspace's own constraint resolves to."""
        constraint = constraint_on(requires, FAMILY["tools"])
        satisfying = [one for one in candidates["tools"] if accepts(constraint, one.version)]
        if satisfying:
            newest = satisfying[-1]
            return Stage("published", newest.version, newest.tag), ""
        if accepts(constraint, declared["tools"]):
            return here["tools"], "no published build tools satisfy it — this commit's are used"
        return here["tools"], (
            f"neither a published build tools package nor this commit's {declared['tools']} "
            f"satisfies {constraint!r} — built with this commit's anyway"
        )

    plan.catalogue["sdk"], plan.verdicts["sdk"] = _downward(
        environment=environment,
        declared=declared,
        here=here,
        inventory=inventory,
        candidates=candidates,
        combinations=combinations,
        tools_for=tools_for,
    )
    for stage in ("workspace", "tools"):
        plan.catalogue[stage], plan.verdicts[stage] = _upward(
            stage=stage,
            environment=environment,
            declared=declared,
            here=here,
            candidates=candidates,
            combinations=combinations,
            tools_for=tools_for,
        )
    plan.combinations = combinations.found
    return plan


def _downward(
    *,
    environment: dict,
    declared: dict[str, str],
    here: dict[str, Stage],
    inventory: dict[str, list[Published]],
    candidates: dict[str, list[Published]],
    combinations: _Combinations,
    tools_for,
) -> tuple[list[Row], str]:
    """The SDK line against the workspaces its own constraint admits.

    An SDK release promises that every workspace inside its range works,
    so the two ends of that range are what have to be tried. Where the
    range has no published member the SDK cannot be released at all — not
    because something is broken, but because the thing it requires does not
    exist yet.
    """
    constraint = constraint_on(release_lines.requires_of(environment, "sdk"), FAMILY["workspace"])
    satisfying = [one for one in candidates["workspace"] if accepts(constraint, one.version)]
    rows: list[Row] = []
    for entry, role in _ends(satisfying):
        tools, note = tools_for(entry.requires())
        rows.append(
            Row(
                role=f"{role} build workspace",
                subject=f"{FAMILY['workspace']} {entry.version}",
                combination=combinations.add(
                    here["sdk"], Stage("published", entry.version, entry.tag), tools
                ),
                required=True,
                note=note,
            )
        )
    unpublished = declared["workspace"] not in {one.version for one in inventory["workspace"]}
    if accepts(constraint, declared["workspace"]) and unpublished:
        tools, note = tools_for(release_lines.requires_of(environment, "workspace"))
        rows.append(
            Row(
                role="the build workspace of this commit",
                subject=f"{FAMILY['workspace']} {declared['workspace']} (this commit)",
                combination=combinations.add(here["sdk"], here["workspace"], tools),
                required=True,
                note=note,
            )
        )
    if satisfying:
        verdict = (
            f"{len(satisfying)} published build workspace(s) satisfy {constraint!r}; the "
            "ends of that range are what an SDK release promises."
        )
    else:
        verdict = (
            f"SDK release blocked until a build workspace satisfying {constraint!r} is published."
        )
    return rows, verdict


def _upward(
    *,
    stage: str,
    environment: dict,
    declared: dict[str, str],
    here: dict[str, Stage],
    candidates: dict[str, list[Published]],
    combinations: _Combinations,
    tools_for,
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
        rows.append(
            Row(
                role=f"{role} {FAMILY[above]}",
                subject=f"{FAMILY[above]} {entry.version}",
                combination=_combination_for(
                    stage=stage,
                    above_entry=entry,
                    environment=environment,
                    here=here,
                    candidates=candidates,
                    combinations=combinations,
                    tools_for=tools_for,
                ),
                required=True,
            )
        )
    rows.append(
        Row(
            role=f"the {FAMILY[above]} of this commit",
            subject=f"{FAMILY[above]} {declared[above]} (this commit)",
            combination=_combination_for(
                stage=stage,
                above_entry=None,
                environment=environment,
                here=here,
                candidates=candidates,
                combinations=combinations,
                tools_for=tools_for,
            ),
            required=True,
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


def _combination_for(
    *,
    stage: str,
    above_entry: Published | None,
    environment: dict,
    here: dict[str, Stage],
    candidates: dict[str, list[Published]],
    combinations: _Combinations,
    tools_for,
) -> str:
    """The triple that tries *stage* of this commit under one version above it."""
    if stage == "workspace":
        sdk = (
            here["sdk"]
            if above_entry is None
            else Stage("published", above_entry.version, above_entry.tag)
        )
        tools, _ = tools_for(release_lines.requires_of(environment, "workspace"))
        return combinations.add(sdk, here["workspace"], tools)
    # The tools line: the workspace above decides, and the SDK above that
    # one follows from the workspace — the newest published SDK that
    # accepts it, and this commit's where none does.
    if above_entry is None:
        return combinations.add(here["sdk"], here["workspace"], here["tools"])
    accepting = [
        one
        for one in candidates["sdk"]
        if accepts(constraint_on(one.requires(), FAMILY["workspace"]), above_entry.version)
    ]
    sdk = Stage("published", accepting[-1].version, accepting[-1].tag) if accepting else here["sdk"]
    return combinations.add(
        sdk, Stage("published", above_entry.version, above_entry.tag), here["tools"]
    )


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


def summarize(*, stage: str, plan: dict, results: Path | None, out=None, errors=None) -> int:
    """One stage's release-readiness table, and whether it is a failure.

    A row fails the job when it claims validity and did not build: a
    combination assembled from published versions and this commit's own
    definition is one somebody could put together today, so a broken one
    is a broken promise rather than a warning.
    """
    out = sys.stdout if out is None else out
    errors = sys.stderr if errors is None else errors
    outcomes = read_results(results)
    labels = {one["id"]: one["label"] for one in plan.get("combinations", [])}
    rows = plan.get("catalogue", {}).get(stage, [])
    lines = [
        f"## Release readiness: {FAMILY[stage]} {plan['declared'][stage]}",
        "",
        plan.get("verdicts", {}).get(stage, ""),
        "",
    ]
    broken: list[str] = []
    if not rows:
        lines.append(
            "Nothing to try: no published version satisfies this line's constraint, and "
            "this commit's own next stage does not either."
        )
    else:
        lines.append("| tried against | combination | result | note |")
        lines.append("|---|---|---|---|")
    for row in rows:
        identifier = row.get("combination")
        built = [
            (architecture, document)
            for (one, architecture), document in sorted(outcomes.items())
            if one == identifier
        ]
        if not built:
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
            f"| {row.get('subject')} | {labels.get(identifier, identifier)} | {result} | "
            f"{row.get('note') or ''} |"
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
        if checksum.is_file():
            stated = checksum.read_text(encoding="utf-8").split()[0]
            if stated != digest:
                raise SystemExit(
                    f"{archive.name} hashes to {digest} and {checksum.name} says {stated}"
                )
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
