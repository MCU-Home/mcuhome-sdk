#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The `sources:` block a CI firmware build states, and the check that it held.

CI builds the reference device out of the packages the run itself
produced: one directory per stage, each with the `index.json` that makes
it a package source, all three handed to `mcuhome device build` as
`--sdk-sources`. That says where packages may come *from*; it does not
say which ones the build wants. A device that names none is built with
the workbench's own default SDK constraint, and a local directory holding
a version outside that constraint is passed over in favour of the package
registry — the build then compiles a published chain while the packages
under test sit unused in the workspace. That is silent, it survives every
gate, and the firmware it produces says nothing about the release it was
supposed to prove.

So the device states the packages. `write` reads the three indexes and
writes a `sources:` block naming exactly what is in them — each package
by name, version and the hash of its archive — and `check` holds the
build context the build actually wrote against that block afterwards.
Two halves of one statement: the first makes the build ask for these
packages, the second makes a build that resolved something else a red
run rather than a surprise at the next step. For the build workspace and
build tools that comparison is a restatement — a fully pinned reference
is copied by the resolver, not resolved, so the manifest can only report
back the pin, and the bytes are already enforced by the hash check at
fetch time. The SDK stage is the independent one, below.

``write``
    The block, out of ``<source>/index.json`` for each source directory
    given, appended to a device file that carries no ``sources:`` block —
    a device that already has one is refused rather than extended,
    because its entries and these would be two answers to one question.
    Every stage must resolve to exactly one version, which is the
    invariant the source directories are built to anyway.

``check``
    The resolved pins of a finished build — the build context's
    ``manifest.yaml`` — against the ones the device names. The SDK hash
    is checked here and nowhere else: ``sources.sdk`` selects by
    constraint only, so the hash it carries is a statement no resolution
    holds itself to, and the one place it can be held is the context that
    came out.

Usage::

    device_pins.py write --device DEVICE --platform <os>-<arch> SOURCE...
    device_pins.py check --device DEVICE --build-dir DIR [--context DIR]

``--platform`` names the tools package: that one is published per
platform, so the pin names one platform's package outright
(``mcuhome-build-tools_linux-amd64``) rather than the family. A family pin
would be resolved through the index's platform map, which a directory
holding one platform's archive does not carry — and the exact bytes are
what this pin exists to name.

Exit status: 0 when the block was written or the pins held, 1 on a
finding (a stage that is not in the indexes exactly once, a device that
already pins something, a build that resolved something else), 2 on a
usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

from mcuhome.compiler.contextread import read_context_manifest
from mcuhome.model.buildenvironment import (
    ARCH_SEPARATOR,
    TOOLS_FAMILY,
    TOOLS_SOURCE,
    WORKSPACE_PACKAGE,
    WORKSPACE_SOURCE,
)
from mcuhome.model.context import MANIFEST_FILE, EnvironmentPin
from mcuhome.model.sdkindex import INDEX_FILE, SDK_PACKAGE_NAME

#: The device file's own key for each stage, in the order the block is
#: written — the reading order of the chain, SDK first.
SDK_KEY = "sdk"
WORKSPACE_KEY = "build_workspace"
TOOLS_KEY = "build_tools"

#: The source shelf each package is published under. A reference states it
#: in front of the package name, and CI states it too: a bare name is read
#: against the stage's default, and a block that spells both out reads the
#: same way to a person as it does to the resolver.
SDK_SOURCE = "sdk"

#: The top-level device key this tool writes, and the one it refuses to
#: overwrite.
SOURCES_BLOCK = "sources"

#: Where the workbench keeps the build context of a local build, under the
#: build directory. Not vocabulary either side publishes, so ``check``
#: takes ``--context`` as well and searches the build directory when this
#: path is not there.
LOCAL_WORK_DIR = ".mcuhome-local"
CONTEXT_DIR = "context"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")

#: ``<os>-<arch>``, the architecture suffix a tools package carries after
#: the first underscore of its name.
_PLATFORM = re.compile(r"[a-z0-9]+-[a-z0-9]+\Z")

#: The one reference form written here: source, package, exact version,
#: archive hash. Read back by ``check`` rather than re-derived from the
#: indexes, so what is checked is what the device says.
_REFERENCE = re.compile(
    r"(?P<source>[a-z0-9][a-z0-9-]*)/(?P<name>[a-z0-9][a-z0-9-]*(?:_[a-z0-9][a-z0-9-]*)?)"
    r":(?P<version>[0-9][0-9A-Za-z.+!_-]*)@sha256:(?P<sha256>[0-9a-f]{64})\Z"
)


@dataclass(frozen=True)
class Pin:
    """One stage's pin: the package, the version, the archive's bytes."""

    source: str
    name: str
    version: str
    sha256: str

    def reference(self) -> str:
        """The pin as a device reference: a bare version and a hash.

        Bare and not ``==<version>``, which the resolver reads as the same
        constraint but not as the same *kind* of statement: for the two
        environment packages a version together with a hash decides the
        pin outright — answered without an index and without the release
        chain above it — while a constraint, exact or not, is resolved, and
        resolving is where a source that does not hold the wanted version
        falls through to the registry. ``sources.sdk`` is read for its
        constraint alone, so the hash there says which archive is meant
        without selecting it, and ``check`` is where that half of the
        statement is held against what came out.
        """
        return f"{self.source}/{self.name}:{self.version}@sha256:{self.sha256}"


def read_indexes(sources: list[Path]) -> dict[str, dict[str, dict]]:
    """Every package every source directory publishes, by name and version.

    One map over all of them rather than one per directory: which stage a
    directory happens to hold is CI's layout and not something a pin
    depends on. A ``(name, version)`` that two directories disagree about
    is refused — the pin would state bytes that are not the only ones on
    offer under that name.
    """
    packages: dict[str, dict[str, dict]] = {}
    for source in sources:
        path = Path(source) / INDEX_FILE
        if not path.is_file():
            raise SystemExit(f"{source} is no package source: it has no {INDEX_FILE}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as failure:
            raise SystemExit(f"{path} is not readable as JSON: {failure}") from failure
        found = document.get("packages") if isinstance(document, dict) else None
        if not isinstance(found, dict):
            raise SystemExit(f"{path} is not an index: no packages object")
        for name, versions in found.items():
            if not isinstance(versions, dict):
                raise SystemExit(f"{path} lists {name} without versions")
            for version, entry in versions.items():
                known = packages.setdefault(name, {}).get(version)
                if known is not None and known != entry:
                    raise SystemExit(
                        f"{name} {version} is published twice with different bytes — "
                        f"{path} disagrees with an earlier source directory"
                    )
                packages[name][version] = entry
    return packages


def pin_for(packages: dict[str, dict[str, dict]], *, name: str, source: str, stage: str) -> Pin:
    """The one version of *name* the sources hold, as a pin.

    Exactly one, and the refusal says which it found instead: a stage with
    two candidates is a build whose resolution nobody stated, which is the
    situation this tool exists to end.
    """
    versions = packages.get(name)
    if not versions:
        raise SystemExit(
            f"no source directory publishes {name}, so the {stage} of this build "
            f"cannot be pinned to the packages it was given"
        )
    if len(versions) != 1:
        raise SystemExit(
            f"the source directories publish {len(versions)} versions of {name} "
            f"({', '.join(sorted(versions))}) — a {stage} pin needs exactly one"
        )
    version, entry = next(iter(versions.items()))
    if not isinstance(entry, dict):
        raise SystemExit(f"the index entry for {name} {version} is not an object")
    if "meta" in entry:
        # A meta entry maps platforms onto concrete packages and its hash
        # is over that mapping, not over an archive. Pinning it would
        # state bytes nothing downloads.
        raise SystemExit(
            f"{name} {version} is a meta entry mapping platforms onto packages — "
            f"pin the platform's own package instead"
        )
    sha256 = entry.get("sha256")
    if not isinstance(sha256, str) or _SHA256_HEX.fullmatch(sha256) is None:
        raise SystemExit(
            f"the index entry for {name} {version} states no usable sha256 "
            f"({sha256!r}), so its bytes cannot be pinned"
        )
    return Pin(source=source, name=name, version=version, sha256=sha256)


def pins_for(packages: dict[str, dict[str, dict]], *, platform: str) -> dict[str, Pin]:
    """The three pins of a build environment chain, by device key.

    *platform* is checked before it is pasted onto the tools family: an
    empty one — an environment variable a workflow forgot to set — would
    otherwise become a package name no index holds, and the refusal would
    be about a missing package rather than about the missing value.
    """
    if _PLATFORM.fullmatch(platform) is None:
        raise SystemExit(
            f"{platform!r} is no platform: the tools package is named "
            f"{TOOLS_FAMILY}{ARCH_SEPARATOR}<os>-<arch>, as in linux-amd64"
        )
    tools = f"{TOOLS_FAMILY}{ARCH_SEPARATOR}{platform}"
    return {
        SDK_KEY: pin_for(packages, name=SDK_PACKAGE_NAME, source=SDK_SOURCE, stage="SDK"),
        WORKSPACE_KEY: pin_for(
            packages, name=WORKSPACE_PACKAGE, source=WORKSPACE_SOURCE, stage="build workspace"
        ),
        TOOLS_KEY: pin_for(packages, name=tools, source=TOOLS_SOURCE, stage="build tools"),
    }


def block_for(pins: dict[str, Pin]) -> str:
    """The ``sources:`` block, as the lines a device file carries."""
    lines = [f"{SOURCES_BLOCK}:"]
    lines += [f"  {key}: {pins[key].reference()}" for key in (SDK_KEY, WORKSPACE_KEY, TOOLS_KEY)]
    return "\n".join(lines) + "\n"


def device_document(device: Path) -> dict:
    """The device file, parsed — and refused if it is not a mapping."""
    if not device.is_file():
        raise SystemExit(f"{device} is not a file")
    yaml = YAML(typ="safe")
    try:
        document = yaml.load(device.read_text(encoding="utf-8"))
    except Exception as failure:  # ruamel raises several types for one problem
        raise SystemExit(f"{device} is not readable as YAML: {failure}") from failure
    if not isinstance(document, dict):
        raise SystemExit(f"{device} is not a device file: its top level is no mapping")
    return document


def write_block(device: Path, pins: dict[str, Pin]) -> str:
    """Append the block for *pins* to *device*, which must pin nothing yet.

    Appended as text rather than written through the parser: everything
    else in the file — comments above all — stays byte for byte what it
    was, and a device whose remaining content changed under CI would be a
    build of something other than the example.
    """
    document = device_document(device)
    if SOURCES_BLOCK in document:
        raise SystemExit(
            f"{device} already carries a {SOURCES_BLOCK}: block. It would be two answers "
            f"to one question beside the packages this build was given — pin that device "
            f"by hand or build one that pins nothing"
        )
    block = block_for(pins)
    existing = device.read_text(encoding="utf-8")
    separator = "" if existing.endswith("\n\n") else "\n" if existing.endswith("\n") else "\n\n"
    device.write_text(existing + separator + block, encoding="utf-8")
    return block


def parse_reference(value: str, *, key: str) -> Pin:
    """One ``sources:`` entry this tool wrote, read back.

    Only the fully pinned form is accepted. Anything else in that block
    was not written here, and a check that guessed what it means would
    pass on a device nobody pinned.
    """
    if not isinstance(value, str):
        raise SystemExit(f"{SOURCES_BLOCK}.{key} is not a reference: {value!r}")
    found = _REFERENCE.fullmatch(value.strip())
    if found is None:
        raise SystemExit(
            f"{SOURCES_BLOCK}.{key} is not a pin this tool wrote ({value!r}) — "
            f"expected <source>/<package>:<version>@sha256:<hash>"
        )
    return Pin(
        source=found["source"],
        name=found["name"],
        version=found["version"],
        sha256=found["sha256"],
    )


def device_pins(device: Path) -> dict[str, Pin]:
    """The three pins a device states, read back off the file."""
    document = device_document(device)
    block = document.get(SOURCES_BLOCK)
    if not isinstance(block, dict):
        raise SystemExit(f"{device} carries no {SOURCES_BLOCK}: block, so it pins nothing")
    pins = {}
    for key in (SDK_KEY, WORKSPACE_KEY, TOOLS_KEY):
        if key not in block:
            raise SystemExit(f"{device} states no {SOURCES_BLOCK}.{key}")
        pins[key] = parse_reference(block[key], key=key)
    return pins


def manifest_path(*, build_dir: Path | None, context: Path | None) -> Path:
    """The build context manifest of a finished local build.

    The workbench writes its context under the build directory and locks
    it there — this holds only without ``--context``, which points this
    function at a manifest of the caller's choosing and skips the search
    below entirely. Without it, the manifest is the resolution as it was
    actually delivered, and its location inside the build directory is
    the workbench's own business, which is why a missing one is searched
    for rather than declared absent — and why finding two is a refusal,
    not a choice.
    """
    if context is not None:
        return Path(context) / MANIFEST_FILE
    if build_dir is None:
        raise SystemExit("nothing to read: state --build-dir or --context")
    build_dir = Path(build_dir)
    expected = build_dir / LOCAL_WORK_DIR / CONTEXT_DIR / MANIFEST_FILE
    if expected.is_file():
        return expected
    found = sorted(path for path in build_dir.rglob(MANIFEST_FILE) if path.is_file())
    if len(found) == 1:
        return found[0]
    if not found:
        raise SystemExit(
            f"{expected} is not there and nothing under {build_dir} is a "
            f"{MANIFEST_FILE} — this build wrote no context to check"
        )
    raise SystemExit(
        f"{expected} is not there and {len(found)} files under {build_dir} are called "
        f"{MANIFEST_FILE} ({', '.join(str(path) for path in found)}) — name one with --context"
    )


def check_pins(pins: dict[str, Pin], manifest: Path, *, out=None) -> int:
    """Hold the resolution in *manifest* against the pins the device named.

    For the build workspace and build tools this is a restatement, not an
    independent check: a fully pinned reference decides those two stages
    outright, so the manifest can only report back the same name, version
    and hash the pin already stated, and their bytes were already
    enforced by the hash check at fetch time. The SDK stage is the one
    comparison that is not a restatement — ``sources.sdk`` is read for
    its constraint alone, so the version and bytes the manifest reports
    are what the constraint actually picked, held here against what the
    pin said they should be.

    Three packages, three facts each: the name the pin resolved to, the
    version, and the bytes. A mismatch is reported per stage and for every
    stage, because "it resolved something else" is worth seeing whole — one
    line of it would hide whether the rest held.
    """
    out = out if out is not None else sys.stdout
    document = read_context_manifest(manifest)
    environment = document.build_environment
    if not isinstance(environment, EnvironmentPin):
        raise SystemExit(
            f"{manifest} names no build-environment packages — this build compiled a "
            f"workspace of its own, and the packages the device pins were not used"
        )
    # The manifest's SDK section names no package — a context pins one SDK
    # and the format has one place for it — so the name held against the
    # pin is the SDK package's own.
    resolved = {
        SDK_KEY: (SDK_PACKAGE_NAME, document.sdk.version, document.sdk.sha256),
        WORKSPACE_KEY: (
            environment.workspace.name,
            environment.workspace.version,
            environment.workspace.sha256,
        ),
        TOOLS_KEY: (environment.tools.name, environment.tools.version, environment.tools.sha256),
    }
    findings = []
    for key in (SDK_KEY, WORKSPACE_KEY, TOOLS_KEY):
        pin = pins[key]
        name, version, sha256 = resolved[key]
        print(f"{key}: pinned {pin.name} {pin.version} {pin.sha256[:12]}", file=out)
        print(f"{' ' * len(key)}  built  {name} {version} {sha256[:12]}", file=out)
        if (name, version, sha256) != (pin.name, pin.version, pin.sha256):
            findings.append(
                f"{SOURCES_BLOCK}.{key} pins {pin.name} {pin.version} "
                f"({pin.sha256[:12]}) and the build resolved {name} {version} ({sha256[:12]})"
            )
    if findings:
        for finding in findings:
            print(f"::error::{finding}", file=sys.stderr)
        print(
            "The build did not use the packages the device names. Nothing this run "
            "produced says anything about them.",
            file=sys.stderr,
        )
        return 1
    print(f"the build resolved exactly what {manifest} was pinned to", file=out)
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="question", required=True)

    writer = sub.add_parser("write", help="pin a device to the packages the sources hold")
    writer.add_argument("--device", type=Path, required=True, help="the device file to pin")
    writer.add_argument(
        "--platform", required=True, help="<os>-<arch> of the build tools package to pin"
    )
    writer.add_argument(
        "sources", nargs="+", type=Path, metavar="SOURCE", help="package source directories"
    )

    checker = sub.add_parser("check", help="what a finished build resolved, against those pins")
    checker.add_argument("--device", type=Path, required=True, help="the pinned device file")
    checker.add_argument("--build-dir", type=Path, help="the build directory of that build")
    checker.add_argument("--context", type=Path, help="the build context directory, named outright")

    args = parser.parse_args(argv)
    if args.question == "write":
        pins = pins_for(read_indexes(args.sources), platform=args.platform)
        sys.stdout.write(write_block(args.device, pins))
        return 0
    return check_pins(
        device_pins(args.device),
        manifest_path(build_dir=args.build_dir, context=args.context),
    )


if __name__ == "__main__":  # pragma: no cover - the command line's entry point
    raise SystemExit(main(sys.argv[1:]))
