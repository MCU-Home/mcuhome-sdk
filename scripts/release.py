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
    "artifact_name",
    "check_tag",
    "digest_of",
    "gate_packages",
    "image_packages",
    "line_of",
    "release_assets",
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
# The package matrix a gate run has to build
# --------------------------------------------------------------------------


def gate_packages(combinations: list[dict]) -> list[dict]:
    """Which packages this run builds, read off the combinations it will try.

    Every stage a combination takes from this commit is built here: the
    tagged line at the version the tag names and with **no** local suffix —
    those are the bytes that get published — and any stand-in for a line
    that has nothing published, under a local version no package host will
    accept. The tools stage is always built for both platforms, because the
    firmware is built on both and each host needs its own.
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
        for platform in PLATFORMS if stage == "tools" else (None,):
            matrix.append(
                {
                    "stage": stage,
                    "platform": platform or "",
                    "artifact": artifact_name(stage, platform),
                    "runner": RUNNER_FOR_PLATFORM[platform],
                    "release": wanted[stage] == "release",
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

    return _gate(arguments)


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
    arguments.output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(document, indent=2, sort_keys=True))
    blocked = document.get("blocked")
    if blocked:
        print(f"\n{blocked}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - the command line's entry point
    raise SystemExit(main(sys.argv[1:]))
