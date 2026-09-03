#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Assemble the build-environment container image from its two packages.

The container profile of the build environment specification
(``docs/spec/build-environment-specification.md`` §2) delivers MCUHome's
environment as an image. This script is the assembly: it takes the two
archives ``scripts/build_env_package.py`` produced, hands them to
``containers/build-environment/Dockerfile``, and puts §5.2's labels on the
result.

**Why the labels cannot live in the Dockerfile.** §5.2 requires an image to
repeat *every* member of its packages' declaration as an OCI label with the
identical value, and the declaration is a file inside the package — a
``LABEL`` instruction cannot read one. So the declaration is read here and
passed on the command line, member for member, whatever members it has:
the ones §5 defines today and any ``x-`` member a future environment adds,
without this script having to learn about it.

**And why the package members are resolved rather than copied.** The
declaration a package carries states the *abstract* set (§5.1): the carrier
cannot state its own hash, and the tools entry names the family because the
sibling platforms' bytes differ on purpose. An image is a delivery of exact
bytes, so §5.1 requires the other form from it — every package member with
its hash, and the tools family replaced by the one concrete
``<family>_<os>-<arch>`` package the image really contains. This script is
the party that can do that: it is holding both archives. It hashes them,
holds their own manifests against the declaration — "an image assembled
from packages it does not declare is simply lying about what it is" (§5.2)
— and labels the image with the completed set. A mismatch is a refusal, not
a warning.

**The tag is derived, not typed.** MCUHome's container-image scheme is
``ghcr.io/mcu-home/build-environment:<workspace package version>-r<n>``, so
the version half is read out of the package the image delivers and only the
assembly revision is a command-line value. ``--tag`` overrides the whole
reference for a local experiment; nothing else may. Building does not
publish — pushing the image is a separate, deliberate act.

Usage::

    scripts/build_env_image.py \\
        --workspace out/mcuhome-build-workspace-<version>.tar.zst \\
        --tools out/mcuhome-build-tools_linux-amd64-<version>.tar.zst

Exit status: 0 on success, 2 on a usage error; a failing child command
propagates as a ``CalledProcessError``.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

try:
    import zstandard
except ModuleNotFoundError:  # a system python, not the repo's venv
    sys.exit(
        "build_env_image.py needs the zstandard module.\n"
        "Run it from this repository's own venv, which carries it via\n"
        "mcuhome-compiler:\n"
        "    python3 -m venv .venv && . .venv/bin/activate\n"
        "    pip install -e ./packaging/model -e ./packaging/compiler"
    )

#: This repository, seen from ``scripts/``.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The image definition this script drives.
DOCKERFILE = REPO_ROOT / "containers" / "build-environment" / "Dockerfile"

#: The declaration (specification §5), inside the package that carries it
#: and, byte for byte the same document, beside the archive. Both copies are
#: read and compared rather than one being preferred: §5 requires them to
#: say the same thing, so a difference is a refusal. See :func:`declaration`.
DECLARATION_FILE = "build-environment.json"

#: Each package's own statement of what it is, at the top of its archive.
WORKSPACE_MANIFEST = "build-workspace.json"
TOOLS_MANIFEST = "build-tools.json"

#: Specification §5.2's label prefix. Every member of the declaration
#: becomes ``<prefix><member>``, with the identical value.
LABEL_PREFIX = "org.mcuhome.build-environment."

#: The image this repository publishes, and the tag shape it carries:
#: ``<workspace package version>-r<n>``, where *n* counts assemblies from
#: one and the same package set. The version comes from the package rather
#: than from a command line, so a tag cannot name a version the image does
#: not contain — the same rule the SDK release archive follows.
#:
#: **A tag is a location, never the identity.** An image is matched by its
#: ``packages.<name>`` labels (§5.2); the tag is how a registry finds it.
IMAGE_REPOSITORY = "ghcr.io/mcu-home/build-environment"

#: The assembly revision of a fresh assembly. Raised by hand when the same
#: packages are assembled again — a new base image, a changed Dockerfile —
#: because nothing here can know that the previous one was published.
DEFAULT_REVISION = 1

#: §5.1's package members: ``packages.<full package name>``.
PACKAGE_MEMBER_PREFIX = "packages."

#: §5.1's package-name grammar — lowercase alphanumerics and ``-``, with an
#: optional architecture suffix introduced by the one ``_`` a name may hold.
PACKAGE_NAME = re.compile(r"\A[a-z0-9][a-z0-9-]*(?:_[a-z0-9][a-z0-9-]*)?\Z")

#: §5.1's package-member value: a PEP 440 version, optionally followed by
#: the content hash of that package's archive. The version is matched to
#: PEP 440's character set rather than to "anything up to the ``@``", so
#: that a value from an older shape of this declaration — a whole package
#: list packed into one string — is refused here instead of becoming a
#: label that reads like a version and is not one.
PACKAGE_VALUE = re.compile(
    r"\A(?P<version>[0-9][0-9a-zA-Z.!+_-]*)(?:@sha256:(?P<sha256>[0-9a-f]{64}))?\Z"
)


def _archive_members(archive: Path, wanted: set[str]) -> dict[str, bytes]:
    """The named top-level members of a ``.tar.zst``, read once, streamed.

    Streamed and stopped as soon as everything asked for has been seen: the
    workspace package is well over half a gigabyte compressed and the two
    documents this script reads are its first entries, so decompressing the
    rest would be several minutes spent on nothing.
    """
    found: dict[str, bytes] = {}
    decompressor = zstandard.ZstdDecompressor()
    with (
        open(archive, "rb") as handle,
        decompressor.stream_reader(handle) as stream,
        tarfile.open(fileobj=io.BufferedReader(stream), mode="r|") as tar,
    ):
        for member in tar:
            if member.name in wanted:
                extracted = tar.extractfile(member)
                if extracted is not None:
                    found[member.name] = extracted.read()
            if found.keys() >= wanted:
                break
    return found


def _json_member(archive: Path, name: str) -> dict:
    """One JSON document out of *archive*, or a refusal naming both."""
    members = _archive_members(archive, {name})
    if name not in members:
        raise SystemExit(f"{archive} carries no {name} — this is not a MCUHome build package")
    try:
        document = json.loads(members[name].decode("utf-8"))
    except ValueError as unreadable:
        raise SystemExit(f"{name} in {archive} is not readable JSON: {unreadable}") from unreadable
    if not isinstance(document, dict):
        raise SystemExit(f"{name} in {archive} is not a JSON object")
    return document


def declaration(workspace: Path) -> dict[str, str]:
    """Specification §5's declaration for the whole package set.

    The declaration is written twice — beside the archive and inside it
    (``packaging/build-environment/README.md``) — for two different readers:
    a provisioner reads the sidecar *before* it unpacks anything, an
    unpacked store entry reads the copy inside. §5 is explicit about what
    that costs: "Both must say the same thing." So when both are present
    they are compared **byte for byte** and a difference is a refusal — an
    environment that describes itself in two ways describes itself in none,
    and the half a given reader happens to see would decide what the
    environment is.

    Either one alone is enough and is used as it stands: an archive
    published before the sidecar existed can still say what it is, and a
    sidecar is a complete answer for a reader that has not opened the
    archive.
    """
    sidecar = workspace.with_name(f"{workspace.name}.{DECLARATION_FILE}")
    beside = sidecar.read_bytes() if sidecar.is_file() else None
    inside = _archive_members(workspace, {DECLARATION_FILE}).get(DECLARATION_FILE)
    if beside is not None and inside is not None and beside != inside:
        raise SystemExit(
            f"{sidecar}\nand {DECLARATION_FILE} inside {workspace}\n"
            "are not the same document. A package declares itself once: the copy beside "
            "the archive and the copy inside it are written from one document and must "
            "stay byte-identical."
        )
    raw = beside if beside is not None else inside
    if raw is None:
        raise SystemExit(
            f"{workspace} carries no {DECLARATION_FILE}, and there is none beside it — "
            "this is not the package that carries the environment's declaration"
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except ValueError as unreadable:
        raise SystemExit(
            f"the declaration of {workspace.name} is not readable JSON: {unreadable}"
        ) from unreadable
    if not isinstance(document, dict):
        raise SystemExit(f"the declaration of {workspace.name} is not a JSON object")
    for member, value in document.items():
        if not isinstance(value, str):
            raise SystemExit(
                f"the declaration states {member} as {value!r}; "
                "every member of the declaration is a string"
            )
    return document


def package_members(declared: dict[str, str]) -> dict[str, tuple[str, str | None]]:
    """§5.1's package members as ``{package name: (version, sha256 or None)}``.

    One member per package, ``packages.<name>``, so reading the set is a
    prefix filter over the declaration rather than a parser — which is the
    whole reason the specification stopped packing it into one value.
    """
    parsed: dict[str, tuple[str, str | None]] = {}
    for member, value in declared.items():
        if not member.startswith(PACKAGE_MEMBER_PREFIX):
            continue
        name = member[len(PACKAGE_MEMBER_PREFIX) :]
        if PACKAGE_NAME.match(name) is None:
            raise SystemExit(
                f"the declaration states {member!r}, and {name!r} is not a package name — "
                "lowercase alphanumerics and '-', with an optional '_<os>-<arch>' suffix"
            )
        match = PACKAGE_VALUE.match(value)
        if match is None:
            raise SystemExit(
                f"the declaration states {member} as {value!r}, which is not "
                "<version>[@sha256:<64 hex digits>]"
            )
        parsed[name] = (match["version"], match["sha256"])
    if not parsed:
        raise SystemExit(
            "the declaration names no package — §5 requires one packages.<name> member "
            "per package of the set"
        )
    return parsed


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matched(
    archive: Path, wanted: dict[str, tuple[str, str | None]], *, names: list[str], version: object
) -> str:
    """The member of *wanted* one archive answers, or a refusal naming both.

    *names* are the member names that archive may legitimately appear under,
    most specific first — the concrete package name, then the family it
    belongs to. A version that disagrees is as much a mismatch as a name
    that is absent, and both are the same refusal: this is not the set.
    """
    for name in dict.fromkeys(names):
        if name in wanted:
            if wanted[name][0] != version:
                raise SystemExit(
                    f"{archive.name} says it is {name} {version}, and the declaration "
                    f"states {name} {wanted[name][0]}"
                )
            return name
    named = " or ".join(dict.fromkeys(names))
    raise SystemExit(
        f"{archive.name} says it is {named} {version}, and the declaration's package set is "
        f"{', '.join(sorted(wanted))}"
    )


def resolve(*, declared: dict[str, str], workspace: Path, tools: Path) -> dict[str, str]:
    """The concrete declaration this image must state (§5.1's second rule).

    Refuses unless the two archives are exactly the set the carrier's
    abstract declaration names, and returns that declaration with its
    package members completed: every one hashed by measuring the archive,
    the carrier's own entry included, and the tools family replaced by the
    concrete ``<family>_<os>-<arch>`` package this image contains.

    A hash the abstract declaration already stated is not overwritten but
    *checked* — a package that pinned bytes and got other ones is the case
    the pinning exists for.
    """
    wanted = package_members(declared)
    resolved: dict[str, str] = {}

    for archive, manifest_name in ((workspace, WORKSPACE_MANIFEST), (tools, TOOLS_MANIFEST)):
        manifest = _json_member(archive, manifest_name)
        name = str(manifest.get("package", ""))
        if PACKAGE_NAME.match(name) is None:
            raise SystemExit(
                f"{archive.name} says its package is {name!r}, which is not a package name — "
                "lowercase alphanumerics and '-', with an optional '_<os>-<arch>' suffix"
            )
        family = name.split("_", 1)[0]
        version = manifest.get("version")
        member = _matched(archive, wanted, names=[name, family], version=version)
        measured = _digest(archive)
        expected = wanted[member][1]
        if expected is not None and measured != expected:
            raise SystemExit(
                f"{archive.name} hashes to {measured}, and the declaration pins {expected}"
            )
        del wanted[member]
        resolved[f"{PACKAGE_MEMBER_PREFIX}{name}"] = f"{version}@sha256:{measured}"

    if wanted:
        raise SystemExit(
            "the declaration names packages this image is not assembled from: "
            + ", ".join(sorted(wanted))
        )

    return {
        **{
            member: value
            for member, value in declared.items()
            if not member.startswith(PACKAGE_MEMBER_PREFIX)
        },
        **resolved,
    }


def labels(declared: dict[str, str]) -> list[str]:
    """§5.2's labels: every member of the declaration, prefixed, unchanged.

    Sorted by member name in ascending byte order, which is what §5.1 asks
    of members written out one after another — so two assemblies of one set
    produce one command line and one diff.
    """
    return [
        f"{LABEL_PREFIX}{member}={value}"
        for member, value in sorted(declared.items(), key=lambda item: item[0].encode("utf-8"))
    ]


def _stage(workspace: Path, tools: Path, directory: Path) -> None:
    """The two archives, and nothing else, as the docker build context.

    Hard links where the filesystem allows them, because the workspace
    package is most of a gigabyte and copying it to hand it to a build is a
    minute spent on nothing. The names are kept: the Dockerfile matches each
    archive by the package-name prefix its file name starts with.
    """
    for archive in (workspace, tools):
        target = directory / archive.name
        try:
            os.link(archive, target)
        except OSError:
            shutil.copy2(archive, target)


def build(
    *, workspace: Path, tools: Path, tag: str, declared: dict[str, str], base_image: str | None
) -> None:
    """Run the image build with §5.2's labels on the command line."""
    command = ["docker", "build", "--file", str(DOCKERFILE), "--tag", tag]
    for label in labels(declared):
        command += ["--label", label]
    if base_image is not None:
        command += ["--build-arg", f"DEBIAN_IMAGE={base_image}"]
    staging_root = workspace.parent
    with tempfile.TemporaryDirectory(prefix="mcuhome-env-image-", dir=staging_root) as staging:
        _stage(workspace, tools, Path(staging))
        subprocess.run([*command, staging], check=True)


def verify_labels(tag: str, declared: dict[str, str]) -> None:
    """Read the built image back and hold its labels against the declaration.

    The orchestrator reads the declaration "from the package metadata when
    it provisions packages, from the image configuration when it runs an
    image. Both must say the same thing" (§5) — the same *set*, stated in
    the form each side is required to state it in: abstract in the package,
    concrete and fully hashed in the image (§5.1). What is checked here is
    therefore the resolved declaration, on the image that was just built
    rather than trusted because the command line looked right.
    """
    completed = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", tag],
        check=True,
        stdout=subprocess.PIPE,
    )
    present = json.loads(completed.stdout.decode("utf-8")) or {}
    wrong = [
        f"  {LABEL_PREFIX}{member}: image says {present.get(LABEL_PREFIX + member)!r}, "
        f"the package says {value!r}"
        for member, value in sorted(declared.items())
        if present.get(LABEL_PREFIX + member) != value
    ]
    if wrong:
        raise SystemExit("the image does not mirror its own declaration:\n" + "\n".join(wrong))


def image_reference(workspace: Path, revision: int) -> str:
    """The scheme's tag for an image delivering *workspace*.

    ``<repository>:<workspace package version>-r<n>``. The version is taken
    from the package's own manifest — the archive's file name would do too,
    but a file can be renamed and a manifest is what the package says about
    itself.
    """
    manifest = _json_member(workspace, WORKSPACE_MANIFEST)
    version = manifest.get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(
            f"{workspace.name} states no version in {WORKSPACE_MANIFEST}, so the image "
            "tag the naming scheme asks for cannot be derived"
        )
    return f"{IMAGE_REPOSITORY}:{version}-r{revision}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--workspace", type=Path, required=True, help="the mcuhome-build-workspace archive"
    )
    parser.add_argument(
        "--tools", type=Path, required=True, help="the mcuhome-build-tools_<os>-<arch> archive"
    )
    parser.add_argument(
        "--revision",
        type=int,
        default=DEFAULT_REVISION,
        help="the assembly revision -r<n> of the tag; raise it when the same packages "
        f"are assembled again (default: {DEFAULT_REVISION})",
    )
    parser.add_argument(
        "--tag",
        help=f"override the whole image reference (default: {IMAGE_REPOSITORY}"
        ":<workspace package version>-r<revision>)",
    )
    parser.add_argument(
        "--base-image",
        help="override the Dockerfile's pinned Debian base (for testing a new base only)",
    )
    arguments = parser.parse_args(argv)

    for archive in (arguments.workspace, arguments.tools):
        if not archive.is_file():
            parser.error(f"{archive} is not a file")
    if arguments.revision < 1:
        parser.error("--revision counts assemblies from 1")

    tag = arguments.tag or image_reference(arguments.workspace, arguments.revision)
    declared = declaration(arguments.workspace)
    resolved = resolve(declared=declared, workspace=arguments.workspace, tools=arguments.tools)
    build(
        workspace=arguments.workspace,
        tools=arguments.tools,
        tag=tag,
        declared=resolved,
        base_image=arguments.base_image,
    )
    verify_labels(tag, resolved)

    print(f"{tag}")
    for label in labels(resolved):
        print(f"  {label}")
    print("\nBuilt, not published: pushing this image is a separate, deliberate act.")
    return 0


if __name__ == "__main__":  # pragma: no cover - the command line's entry point
    raise SystemExit(main(sys.argv[1:]))
