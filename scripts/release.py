#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The release act: what a tag is about, what it has to pass, what it publishes.

This repository cuts **three release lines out of one history** — the SDK
package, the build workspace package and the build tools packages — and
each of them carries a version of its own, declared in
``packaging/build-environment/environment.json``. A tag says which line it
releases by its prefix:

===================== =======================================
``v<version>``        the SDK package
``workspace-v…``      the build workspace package
``tools-v…``          the build tools packages, one per platform
===================== =======================================

Three questions follow from that, and this module answers them for
``.github/workflows/release.yml``:

``check-tag``
    Which line is this, and does the version it names match what the
    tagged commit declares for that line? The archives are named after the
    version in the **commit**, never after the tag, so a tag on an unbumped
    commit would publish bytes under a number that means something else —
    and a published version is immutable and eternal. Checked before
    anything is built.

``gate``
    What has to be built and to pass before anything is published. The
    catalogue is the one ``scripts/release_readiness.py`` writes per commit,
    under the rule a tag lives by: **only published versions count**, where
    published means a GitHub release of this repository and never a package
    registry. The tagged line's own package is built in this run at its
    real version; the stages around it are published releases; and where
    the line below has nothing published that satisfies what this one
    requires, the release is blocked rather than tested against something
    nobody could resolve to.

``assets``
    Exactly the files the release publishes, out of the directory they were
    built into — so what is uploaded is the set that was tested, and
    nothing else travels with it.

``image-packages``
    The chain around one build workspace package: the newest published
    build tools its own meta file accepts — which is what an image of that
    package delivers — and the newest published SDK that accepts the
    workspace, which is what a verification afterwards compiles.

**What this module is not.** It cuts nothing, commits nothing and tags
nothing: a tag is a deliberate act, made by hand, and the whole procedure
around it is ``RELEASING.md``. Everything here answers questions about a
tag that already exists.

Usage::

    release.py check-tag <tag> [--revision <rev>]
    release.py gate <tag> --output gate.json [--releases F] [--metas D]
    release.py assets <stage> --version <version> --directory <dir>
    release.py image-packages (--workspace-meta F | --workspace-version V)

Exit status: 0 when the answer is yes, 1 when it is a refusal — a tag that
names the wrong version, a blocked release, a missing asset — and 2 on a
usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_lines  # noqa: E402 - repo-relative import, needs the path above
import release_readiness  # noqa: E402 - same

__all__ = [
    "PLATFORMS",
    "RELEASE_JOBS",
    "artifact_name",
    "check_run",
    "check_tag",
    "expected_jobs",
    "digest_of",
    "gate_packages",
    "image_packages",
    "line_of",
    "release_assets",
    "verify_plan",
    "verify_sources",
]

#: This repository, seen from ``scripts/``.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The platforms the tools line publishes a package for. Everything that
#: builds firmware is built on both of them, so every release needs both.
PLATFORMS = ("linux-amd64", "linux-arm64")

#: The runner label a platform's package is built on. It is workflow
#: knowledge and it is stated here because a *computed* package matrix has
#: to carry it: a GitHub matrix cannot map one of its own values onto
#: another. ``None`` is the architecture-neutral stages, which run anywhere.
RUNNER_FOR_PLATFORM = {
    None: "ubuntu-latest",
    "linux-amd64": "ubuntu-latest",
    "linux-arm64": "ubuntu-24.04-arm",
}


def _gh(*arguments: str) -> str:
    """One ``gh`` call, its stdout, and no shell."""
    completed = subprocess.run(["gh", *arguments], check=True, stdout=subprocess.PIPE, text=True)
    return completed.stdout


def artifact_name(stage: str, platform: str | None) -> str:
    """The artifact one package build uploads, per stage and platform.

    One name in one place: the firmware, publish and image jobs download it
    back by exactly this name, and ``ci-build.yml`` writes the same one for
    its per-commit packages.
    """
    return f"packages-{stage}" + (f"-{platform}" if platform else "")


# --------------------------------------------------------------------------
# check-tag: which line a tag releases
# --------------------------------------------------------------------------


def line_of(tag: str) -> tuple[str, str]:
    """``(stage, version)`` for *tag*, or a refusal naming the three shapes.

    Three lines share one repository, so the prefix is the only thing that
    says which of them a tag is about. The longest matching prefix wins —
    ``workspace-v`` and ``tools-v`` do not start with ``v``, so there is
    nothing ambiguous today, and the rule stays right if a future prefix
    ever is.
    """
    from packaging.version import InvalidVersion, Version

    prefixes = sorted(
        release_readiness.TAG_PREFIX.items(), key=lambda item: len(item[1]), reverse=True
    )
    for stage, prefix in prefixes:
        if not tag.startswith(prefix):
            continue
        version = tag[len(prefix) :]
        try:
            Version(version)
        except InvalidVersion:
            break
        return stage, version
    raise SystemExit(
        f"{tag!r} names no release line of this repository.\n"
        "A tag releases exactly one of them, and says which by its prefix:\n"
        "  v<version>            the SDK package\n"
        "  workspace-v<version>  the build workspace package\n"
        "  tools-v<version>      the build tools packages\n"
        "The version part is a PEP 440 version."
    )


def check_tag(*, tag: str, revision: str = "HEAD", repository: Path = REPO_ROOT, out=None) -> int:
    """Hold *tag* against the version its line declares at *revision*.

    Prints ``key=value`` lines, which is what a workflow appends to its job
    outputs; a disagreement is a refusal with the fix in it.
    """
    out = sys.stdout if out is None else out
    stage, version = line_of(tag)
    declared = release_lines.version_of(release_lines.environment(repository, revision), stage)
    if version != declared:
        raise SystemExit(
            f"{tag} would release the {stage} line as {version}, and "
            f"{release_lines.ENVIRONMENT_FILE} at this commit declares {stage}.version "
            f"{declared}.\n"
            "Bump the version first and tag the commit that carries it: the packages are "
            "named after the version in the commit, never after the tag."
        )
    print(f"stage={stage}", file=out)
    print(f"version={version}", file=out)
    print(f"tag={tag}", file=out)
    print(f"family={release_readiness.FAMILY[stage]}", file=out)
    return 0


# --------------------------------------------------------------------------
# assets: exactly what a release of one line publishes
# --------------------------------------------------------------------------


def release_assets(directory: Path, *, stage: str, version: str) -> list[Path]:
    """The files a release of *stage* uploads, checked against *directory*.

    Exactly three per package — the archive, its ``.sha256`` and its
    ``.meta.json`` — because that is what the package host's publish
    pipeline reads: it refuses a package whose checksum sidecar is absent
    and, for these sources, one whose meta file is. Nothing else may
    travel. An ``index.json`` belongs to a *source* and a source is signed
    over there, and an archive in this directory that the tag is not about
    means the run built something it is not releasing.

    The tools line publishes one package per platform and both have to be
    on the one release: the family's meta entry is recorded only once every
    member exists at its version.
    """
    names = (
        [
            f"{release_readiness.FAMILY[stage]}_{platform}-{version}.tar.zst"
            for platform in PLATFORMS
        ]
        if stage == "tools"
        else [f"{release_readiness.FAMILY[stage]}-{version}.tar.zst"]
    )
    wanted: list[Path] = []
    for name in names:
        for asset in (name, f"{name}.sha256", f"{name}{release_lines.META_SUFFIX}"):
            path = directory / asset
            if not path.is_file():
                raise SystemExit(
                    f"{directory}/{asset} is missing, and a release of the {stage} line "
                    "publishes it.\nA package travels as three files: the archive, its "
                    "checksum and its meta file."
                )
            wanted.append(path)
    strays = sorted(path.name for path in directory.glob("*.tar.zst") if path.name not in names)
    if strays:
        raise SystemExit(
            f"{directory} holds archives this release does not publish: "
            f"{', '.join(strays)}.\nA tag releases one line, and "
            f"{release_readiness.TAG_PREFIX[stage]}{version} is about "
            f"{', '.join(names)}."
        )
    return wanted


# --------------------------------------------------------------------------
# verify-plan: the published chain around a release that already exists
# --------------------------------------------------------------------------


def verify_plan(*, stage: str, version: str, releases: list[dict], metas) -> dict[str, str]:
    """The three releases a published release is verified with, and the image.

    The gate answers this for a tag it is *about to* publish, out of the
    commit. This answers it for a release that already exists, out of the
    published world alone — which is what lets a failed verification be
    repeated without re-tagging anything, and what a release cut before this
    check existed needs.

    The chain is resolved the way a user resolves it: newest published
    satisfying each constraint, downwards from the released stage, and the
    newest published version above it that accepts it. A link that does not
    exist is a refusal naming it, because a verification against half a
    chain would prove nothing.
    """
    published = {one: release_readiness.published_of(one, releases) for one in release_lines.STAGES}
    resolved = release_readiness.resolvable(published, metas)
    tags = {}

    def newest_below(owner: release_readiness.Published, lower: str) -> release_readiness.Published:
        family = release_readiness.FAMILY[lower]
        constraint = release_readiness.constraint_on(owner.requires(), family)
        satisfying = [
            one for one in resolved[lower] if release_readiness.accepts(constraint, one.version)
        ]
        if not satisfying:
            raise SystemExit(
                f"{owner.tag} resolves to no {family}: it requires {constraint!r} and no "
                "published version satisfies it.\n"
                "There is no chain to verify this release in — release that line first."
            )
        return satisfying[-1]

    def the(stage_name: str) -> release_readiness.Published:
        found = next((one for one in resolved[stage_name] if one.version == version), None)
        if found is None:
            raise SystemExit(
                f"{release_readiness.TAG_PREFIX[stage_name]}{version} is not a published "
                f"{release_readiness.FAMILY[stage_name]} release carrying a meta file, so "
                "there is nothing to verify.\n"
                "A release says what it requires in its <archive>.meta.json; one without "
                "it cannot be resolved through by anybody."
            )
        return found

    if stage == "tools":
        raise SystemExit(
            "A build tools release is not verified on its own: an image delivers a build "
            "workspace package and the tools that package accepts, so the tools reach an "
            "image only when one is assembled.\n"
            "Dispatch the image revision (workspace_version + revision) — that run "
            "assembles them and verifies the result."
        )

    if stage == "sdk":
        sdk = the("sdk")
        workspace = newest_below(sdk, "workspace")
        tools = newest_below(workspace, "tools")
        tags = {"sdk_tag": sdk.tag, "workspace_tag": workspace.tag, "tools_tag": tools.tag}
        image_version = workspace.version
    else:
        workspace = the("workspace")
        tools = newest_below(workspace, "tools")
        sdk = next(
            (
                one
                for one in reversed(resolved["sdk"])
                if release_readiness.accepts(
                    release_readiness.constraint_on(
                        one.requires(), release_readiness.FAMILY["workspace"]
                    ),
                    workspace.version,
                )
            ),
            None,
        )
        if sdk is None:
            raise SystemExit(
                f"No published {release_readiness.FAMILY['sdk']} accepts "
                f"{release_readiness.FAMILY['workspace']} {workspace.version}, and the "
                "reference device is compiled from an SDK package.\n"
                "Verify it with the SDK release that requires it, once there is one."
            )
        tags = {"sdk_tag": sdk.tag, "workspace_tag": workspace.tag, "tools_tag": tools.tag}
        image_version = workspace.version

    return {**tags, "image_workspace_version": image_version}


# --------------------------------------------------------------------------
# The package matrix a gate run has to build
# --------------------------------------------------------------------------


#: The stage whose version may never carry a local suffix, even when it is
#: only standing in for a line nothing has published yet.
#:
#: The build workspace package carries the build environment's own
#: declaration, and that document names the **tools** version the package is
#: delivered with. A local segment on the workspace would therefore be a
#: statement about the tools that is not true — the tools of a release run
#: are the released or the published ones, and they carry no suffix — and
#: the unpacked environment refuses the pair on the spot. It is also the
#: version a container image is tagged after, and `+` is not a character a
#: container tag may hold. Standing in or released, this one is built under
#: the version the definition file declares; what distinguishes it from a
#: release is that a release uploads it and a gate does not.
UNSUFFIXED = "workspace"


def gate_packages(combinations: list[dict]) -> list[dict]:
    """Which packages this run builds, read off the combinations it will try.

    Every stage a combination takes from this commit is built here: the
    tagged line at the version the tag names — those are the bytes that get
    published — and any stand-in for a line that has nothing published,
    under a local version no package host will accept (except
    :data:`UNSUFFIXED`). The tools stage is always built for both platforms,
    because the firmware is built on both and each host needs its own.
    """
    wanted: dict[str, str] = {}
    for combination in combinations:
        for stage in release_lines.STAGES:
            source = (combination.get(stage) or {}).get("source")
            if source in ("release", "checkout") and wanted.get(stage) != "release":
                wanted[stage] = source
    matrix: list[dict] = []
    for stage in release_lines.STAGES:
        if stage not in wanted:
            continue
        released = wanted[stage] == "release"
        for platform in PLATFORMS if stage == "tools" else (None,):
            matrix.append(
                {
                    "stage": stage,
                    "platform": platform or "",
                    "artifact": artifact_name(stage, platform),
                    "runner": RUNNER_FOR_PLATFORM[platform],
                    "release": released,
                    "suffix": not released and stage != UNSUFFIXED,
                }
            )
    return matrix


def digest_of(path: Path) -> str:
    """The sha256 of one file, as the sixty-four hex digits an index records."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Where the verification takes each stage's bytes from
# --------------------------------------------------------------------------


def verify_sources(combination: dict, *, released_tag: str, rehearsal: bool) -> dict[str, dict]:
    """Per stage: this run's artefact, or a GitHub release, and which one.

    One rule per source, and the reason each is what it is:

    ``published``
        A release of this repository, named by the combination. Downloaded
        and turned into a package source directory, exactly as the gate's
        firmware builds do.
    ``release``
        The line being released. Its release exists by the time anything is
        verified — ``publish-release`` runs first — so the bytes come from
        **the release**, which makes the verification a statement about what
        was published rather than about what was uploaded. In a rehearsal
        nothing is published, so there the run's own artefact is the only
        copy there is.
    ``checkout``
        A stand-in for a line nothing has published. It carries a local
        version no package host will ever accept, so there is no release to
        take it from and the artefact is the only answer.

    Returned rather than computed in the workflow because a matrix, three
    sources and two modes is exactly the kind of condition that is wrong in
    one of its six cases and nobody notices until that case is a release.
    """
    answer: dict[str, dict] = {}
    for stage in release_lines.STAGES:
        entry = combination.get(stage) or {}
        source = entry.get("source")
        if source == "published":
            answer[stage] = {"from": "release", "tag": entry.get("tag", "")}
        elif source == "release" and not rehearsal:
            answer[stage] = {"from": "release", "tag": released_tag}
        else:
            answer[stage] = {"from": "artifact", "tag": ""}
    return answer


# --------------------------------------------------------------------------
# image-packages: what an image of one workspace release delivers
# --------------------------------------------------------------------------


def image_packages(
    *,
    workspace_meta: dict,
    releases: list[dict],
    metas,
) -> dict[str, str]:
    """The chain around one build workspace package, for the image jobs.

    An image is an assembly of a workspace package and the build tools that
    package accepts — never of "the tag's version" for both, because the two
    lines do not share a cadence. Which tools that is comes out of the
    workspace package's own meta file: a PEP 440 constraint, resolved to the
    **newest published** version satisfying it, which is the same answer a
    workbench provisioning that package gets.

    The SDK above it is resolved the same way and is not part of the image:
    it is what the verification afterwards compiles, and a workspace release
    that no published SDK accepts simply has none yet.
    """
    package = workspace_meta.get("package") or {}
    workspace_version = package.get("version")
    if not isinstance(workspace_version, str):
        raise SystemExit("the workspace meta file names no version")
    requires = workspace_meta.get("requires")
    requires = requires if isinstance(requires, dict) else {}
    published = {
        stage: release_readiness.published_of(stage, releases) for stage in ("sdk", "tools")
    }
    resolved = release_readiness.resolvable(published, metas)

    constraint = release_readiness.constraint_on(requires, release_readiness.FAMILY["tools"])
    satisfying = [
        one for one in resolved["tools"] if release_readiness.accepts(constraint, one.version)
    ]
    if not satisfying:
        raise SystemExit(
            f"{release_readiness.FAMILY['workspace']} {workspace_version} requires "
            f"{release_readiness.FAMILY['tools']} {constraint!r}, and no published version "
            "satisfies it.\nAn image delivers the tools a workspace package accepts, so "
            "release that line first."
        )
    tools = satisfying[-1]

    accepting = [
        one
        for one in resolved["sdk"]
        if release_readiness.accepts(
            release_readiness.constraint_on(one.requires(), release_readiness.FAMILY["workspace"]),
            workspace_version,
        )
    ]
    sdk = accepting[-1] if accepting else None
    return {
        "workspace_version": workspace_version,
        "tools_tag": tools.tag,
        "tools_version": tools.version,
        "sdk_tag": sdk.tag if sdk else "",
        "sdk_version": sdk.version if sdk else "",
    }


# --------------------------------------------------------------------------
# check-run: the release did what this kind of run is for
# --------------------------------------------------------------------------
#
# A workflow reports green when nothing failed, and "skipped" is not
# failing. That is the right default for a graph whose branches are
# deliberate — a tools tag builds no image, an SDK tag publishes none — and
# it is exactly wrong for the one question a release run has to answer: did
# the jobs this run exists for actually run?
#
# The graph has four shapes, one per kind of run, and each states which jobs
# it needs. A job that was supposed to run and was skipped is a failure
# here, which is what makes a half-finished dispatch red instead of green.

#: Every job the release workflow can run, in the order it runs them.
RELEASE_JOBS = (
    "gate-release",
    "build-packages",
    "build-firmware",
    "publish-release",
    "build-environment-image",
    "publish-environment-image-index",
    "verify-release",
)


def expected_jobs(*, mode: str, stage: str, verify_mode: str) -> dict[str, str]:
    """Which jobs this run needs, and which it is right to have skipped.

    ``mode`` is what the run is — a tag, a rehearsal, an image revision, a
    verification of something already released — and the two other
    arguments are what the gate resolved. The answer is a verdict per job:
    ``success`` for one this run exists for, ``skipped`` for one that has
    nothing to do in it.
    """
    if mode not in ("tag", "rehearse", "image", "verify"):
        raise SystemExit(f"{mode!r} is not a kind of release run")
    builds = mode in ("tag", "rehearse")
    # An image is assembled by a build workspace release and by a revision
    # dispatch, and by nothing else: it delivers a workspace package.
    image = mode == "image" or (mode == "tag" and stage == "workspace")
    verified = verify_mode not in ("", "skip")
    wanted = {
        "gate-release": True,
        "build-packages": builds,
        "build-firmware": builds,
        "publish-release": mode == "tag",
        "build-environment-image": image,
        "publish-environment-image-index": image,
        "verify-release": verified,
    }
    return {job: ("success" if needed else "skipped") for job, needed in wanted.items()}


def check_run(*, mode: str, stage: str, verify_mode: str, results: dict[str, str], out=None) -> int:
    """Hold what the run did against what this kind of run is for."""
    out = sys.stdout if out is None else out
    expected = expected_jobs(mode=mode, stage=stage, verify_mode=verify_mode)
    lines = [
        f"## What this {mode} run had to do",
        "",
        "| job | expected | result |",
        "|---|---|---|",
    ]
    wrong: list[str] = []
    for job in RELEASE_JOBS:
        was = results.get(job, "missing")
        # A job that was right to skip may also have been cancelled with
        # the run; only a job that had work to do is held to its result.
        if expected[job] == "success" and was != "success":
            wrong.append(f"{job} was {was} and this run needs it")
        lines.append(f"| `{job}` | {expected[job]} | {was} |")
    lines.append("")
    lines.append(
        "**Failed:** " + "; ".join(wrong)
        if wrong
        else "Every job this run exists for ran and succeeded."
    )
    print("\n".join(lines), file=out)
    for one in wrong:
        print(f"::error::{one}", file=sys.stderr)
    return 1 if wrong else 0


# --------------------------------------------------------------------------
# The command line
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT, help="the checkout to read the revision from"
    )
    parser.add_argument("--revision", default="HEAD", help="the commit to read (default: HEAD)")
    parser.add_argument("--releases", type=Path, help="read the release inventory from this file")
    parser.add_argument(
        "--metas", type=Path, help="read published meta documents from <dir>/<tag>/ instead"
    )
    parser.add_argument(
        "--repository-slug", help="<owner>/<name> of the repository whose releases decide"
    )
    sub = parser.add_subparsers(dest="question", required=True)

    tag_check = sub.add_parser("check-tag", help="which line a tag releases, and its version")
    tag_check.add_argument("tag")

    gate = sub.add_parser("gate", help="what this tag has to pass before anything is published")
    gate.add_argument("tag")
    gate.add_argument("--output", type=Path, required=True, help="where the gate plan is written")
    gate.add_argument(
        "--rehearsal",
        action="store_true",
        help="this run publishes nothing, so the released line exists only as this run's "
        "own artefact and no image delivers it",
    )

    verify = sub.add_parser("verify-plan", help="the published chain around a released tag")
    verify.add_argument("tag")

    ran = sub.add_parser("check-run", help="the release did what this kind of run is for")
    ran.add_argument("--mode", required=True)
    ran.add_argument("--stage", default="")
    ran.add_argument("--verify-mode", default="")
    ran.add_argument("--results", type=Path, required=True, help="job name to result, as JSON")

    assets = sub.add_parser("assets", help="exactly the files this release publishes")
    assets.add_argument("stage", choices=release_lines.STAGES)
    assets.add_argument("--version", required=True)
    assets.add_argument("--directory", type=Path, required=True)

    image = sub.add_parser("image-packages", help="the chain around one build workspace package")
    source = image.add_mutually_exclusive_group(required=True)
    source.add_argument("--workspace-meta", type=Path, help="the package's meta file, on disk")
    source.add_argument(
        "--workspace-version", help="a published version, whose meta file is downloaded"
    )

    arguments = parser.parse_args(argv)

    if arguments.question == "check-tag":
        return check_tag(tag=arguments.tag, revision=arguments.revision, repository=arguments.repo)

    if arguments.question == "assets":
        for path in release_assets(
            arguments.directory, stage=arguments.stage, version=arguments.version
        ):
            print(path)
        return 0

    if arguments.question == "image-packages":
        return _image_packages(arguments)

    if arguments.question == "verify-plan":
        return _verify_plan(arguments)

    if arguments.question == "check-run":
        return check_run(
            mode=arguments.mode,
            stage=arguments.stage,
            verify_mode=arguments.verify_mode,
            results=json.loads(arguments.results.read_text(encoding="utf-8")),
        )

    return _gate(arguments)


def _verify_inputs(document: dict, *, released_tag: str, rehearsal: bool) -> None:
    """Fill the gate's verify block with where each stage's bytes come from.

    A rehearsal publishes nothing, so two things change: the released line
    exists only as this run's own artefact, and no image can deliver it —
    a published image declares the *published* workspace package's hash,
    and a rehearsal's is a different archive. Verifying an SDK rehearsal
    against the published image is still exactly right, though: an image
    declares the workspace and the tools, never the SDK.
    """
    verify = document.get("verify")
    if not verify:
        return
    chosen = next(
        (one for one in document.get("combinations", []) if one["id"] == verify.get("combination")),
        None,
    )
    if chosen is None:
        verify["mode"] = "skip"
        verify["reason"] = "this release has no combination to verify with"
        return
    if rehearsal and verify["mode"] == "pushed-image":
        verify["mode"] = "skip"
        verify["reason"] = (
            "a rehearsal pushes no image, and no published image can deliver a build "
            "workspace package that was never published — its labels carry the hash of "
            "the archive a release attached. Cut the tag to see this verified."
        )
    verify["sources"] = verify_sources(chosen, released_tag=released_tag, rehearsal=rehearsal)


def _published_world(arguments, work: Path):
    """The release inventory and the meta source every question here reads.

    Both edges are injectable — ``--releases`` and ``--metas`` — so every
    answer is reproducible without a network, which is what the tests use
    and what lets a release be rehearsed on a bench.
    """
    if arguments.releases is not None:
        releases = json.loads(arguments.releases.read_text(encoding="utf-8"))
        if not isinstance(releases, list):
            raise SystemExit(f"{arguments.releases} is not a list of releases")
    else:
        releases = release_readiness.fetch_releases(
            release_readiness.repository_slug(arguments.repository_slug)
        )
    if arguments.metas is not None:
        metas = release_readiness.directory_metas(arguments.metas)
    else:
        metas = release_readiness.download_metas(
            release_readiness.repository_slug(arguments.repository_slug), work
        )
    return releases, metas


def _image_packages(arguments) -> int:
    """``image-packages``: the tools an image delivers and the SDK above it."""
    work = Path(tempfile.mkdtemp(prefix="mcuhome-release-image-"))
    try:
        releases, metas = _published_world(arguments, work)
        if arguments.workspace_meta is not None:
            meta = json.loads(arguments.workspace_meta.read_text(encoding="utf-8"))
        else:
            meta = _published_workspace_meta(arguments, work)
        answer = image_packages(workspace_meta=meta, releases=releases, metas=metas)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for key, value in answer.items():
        print(f"{key}={value}")
    return 0


def _published_workspace_meta(arguments, work: Path) -> dict:
    """The meta file of a published workspace release, off its release page."""
    tag = f"{release_readiness.TAG_PREFIX['workspace']}{arguments.workspace_version}"
    into = work / tag
    into.mkdir(parents=True, exist_ok=True)
    _gh(
        "release",
        "download",
        tag,
        "--repo",
        release_readiness.repository_slug(arguments.repository_slug),
        "--pattern",
        f"*{release_lines.META_SUFFIX}",
        "--dir",
        str(into),
        "--clobber",
    )
    found = sorted(into.glob(f"*{release_lines.META_SUFFIX}"))
    if not found:
        raise SystemExit(
            f"{tag} carries no {release_lines.META_SUFFIX} asset, so that release does not "
            "say what it requires and no image can be assembled from it."
        )
    return json.loads(found[0].read_text(encoding="utf-8"))


def _verify_plan(arguments) -> int:
    """``verify-plan``: which releases verify an already published one."""
    stage, version = line_of(arguments.tag)
    work = Path(tempfile.mkdtemp(prefix="mcuhome-release-verify-"))
    try:
        releases, metas = _published_world(arguments, work)
        answer = verify_plan(stage=stage, version=version, releases=releases, metas=metas)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"stage={stage}")
    print(f"version={version}")
    for key, value in answer.items():
        print(f"{key}={value}")
    return 0


def _gate(arguments) -> int:
    """``gate``: the catalogue, the package matrix, and what it publishes."""
    stage, version = line_of(arguments.tag)
    declared = release_lines.version_of(
        release_lines.environment(arguments.repo, arguments.revision), stage
    )
    if version != declared:
        raise SystemExit(
            f"{arguments.tag} would release {version} and this commit declares {declared} — "
            "run check-tag first."
        )
    work = Path(tempfile.mkdtemp(prefix="mcuhome-release-gate-"))
    try:
        releases, metas = _published_world(arguments, work)
        document = release_readiness.build_gate(
            stage=stage,
            revision=arguments.revision,
            releases=releases,
            metas=metas,
            repository=arguments.repo,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    document["release"] = {
        "stage": stage,
        "version": version,
        "tag": arguments.tag,
        "family": release_readiness.FAMILY[stage],
    }
    document["packages"] = gate_packages(document["combinations"])
    _verify_inputs(document, released_tag=arguments.tag, rehearsal=arguments.rehearsal)
    arguments.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(document, indent=2, sort_keys=True))
    blocked = document.get("blocked")
    if blocked:
        # A blocked release is the one answer somebody has to read, so it
        # goes where a reader looks rather than only into the raw log: an
        # annotation at the top of the run, and the step summary. The
        # workflow keeps the gate document either way.
        print(f"\n{blocked}", file=sys.stderr)
        print(f"::error::{blocked.splitlines()[0]}", file=sys.stderr)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as out:
                heading = f"## {arguments.tag} cannot be released"
                body = "\n".join(f"> {line}" for line in blocked.splitlines())
                print(f"{heading}\n\n{body}\n", file=out)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - the command line's entry point
    raise SystemExit(main(sys.argv[1:]))
