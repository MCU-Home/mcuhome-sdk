# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The pins a CI firmware build states, and the check that they held.

``scripts/device_pins.py`` exists because of a failure mode with no
symptom: CI hands the packages a run produced to the build as local
sources, the device names none of them, the workbench's own default SDK
constraint decides instead — and a local directory holding a version
outside that constraint is passed over for the package registry. Every
gate stays green while the firmware is compiled from a published chain
that has nothing to do with the release under test.

What is tested here is therefore not "the block is written" but the two
properties that make such a run impossible:

* **The block names what the source directories hold, exactly** — one
  version per stage, the archive's own hash beside it, and the fully
  pinned spelling (a bare version, not ``==<version>``) that the
  resolver answers without consulting an index at all. A stage the
  directories do not hold exactly once is refused rather than guessed
  at, because guessing is what produced the silent build.
* **A build that resolved something else is a finding** — the check
  reads the pins back off the device and the resolution off the build
  context the build actually wrote, and reports every stage rather than
  the first mismatch.

The manifests here are written through
:mod:`mcuhome.model.context` and read back through the program's own
``read_context_manifest``, so the fixture cannot agree with the code
under test about a format that neither of them is free to choose.

The example device is asserted to carry no ``sources:`` block. The day
it does, the pins CI writes would be a second answer beside its own, and
this file is where that is noticed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import EXAMPLES_DIR, REPO_ROOT, write_context_manifest
from ruamel.yaml import YAML

from mcuhome.compiler.contextread import read_context_manifest
from mcuhome.model.context import (
    ContextManifest,
    DeveloperEnvironment,
    EnvironmentPin,
    PackagePin,
    SdkPin,
    context_id,
)

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import device_pins  # noqa: E402 - repo tooling, needs the path above

#: The device the firmware jobs build — the one file this tooling is
#: pointed at in CI.
EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"

#: A platform, spelled the way the workflows spell theirs.
PLATFORM = "linux-amd64"

SDK_VERSION = "0.2.0"
SDK_SHA = "a1" * 32
WORKSPACE_SHA = "b2" * 32
TOOLS_SHA = "c3" * 32

BOARD = "nrf7002dk/nrf5340/cpuapp"


def index(directory: Path, packages: dict[str, dict[str, str]]) -> Path:
    """A package source directory holding *packages*: ``name -> version -> sha``.

    Only the two fields a pin is made of are filled in; the archive
    itself is never opened by the tool under test, which is the point of
    reading an index rather than a directory listing.
    """
    directory.mkdir(parents=True, exist_ok=True)
    document: dict = {"packages": {}}
    for name, versions in packages.items():
        for version, sha256 in versions.items():
            document["packages"].setdefault(name, {})[version] = {
                "file": f"{name}-{version}.tar.zst",
                "sha256": sha256,
                "size": 4096,
                "meta_file": {
                    "file": f"{name}-{version}.tar.zst.meta.json",
                    "sha256": "d4" * 32,
                    "size": 128,
                },
            }
    (directory / "index.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return directory


@pytest.fixture
def sources(tmp_path: Path) -> list[Path]:
    """The three source directories a firmware job assembles, one version each."""
    return [
        index(tmp_path / "sources" / "sdk", {"mcuhome-sdk": {SDK_VERSION: SDK_SHA}}),
        index(
            tmp_path / "sources" / "workspace",
            {"mcuhome-build-workspace": {"0.2.0": WORKSPACE_SHA}},
        ),
        index(
            tmp_path / "sources" / "tools",
            {f"mcuhome-build-tools_{PLATFORM}": {"0.2.0": TOOLS_SHA}},
        ),
    ]


@pytest.fixture
def device(tmp_path: Path) -> Path:
    """The example device, copied the way the workflows copy it."""
    target = tmp_path / "devices" / EXAMPLE.name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def written(device: Path) -> dict[str, str]:
    """The ``sources:`` block of *device*, parsed."""
    document = YAML(typ="safe").load(device.read_text(encoding="utf-8"))
    return document["sources"]


def pinned(device: Path, sources: list[Path], platform: str = PLATFORM) -> int:
    """``write`` over *sources*, as the workflow step calls it."""
    return device_pins.main(
        ["write", "--device", str(device), "--platform", platform, *(str(one) for one in sources)]
    )


# --------------------------------------------------------------------------
# write — the block
# --------------------------------------------------------------------------


def test_the_block_names_the_three_packages_the_sources_hold(device, sources, capsys):
    """One entry per stage, each the package and the bytes the index states.

    The tools entry names **one platform's** package and not the family:
    a family pin is resolved through the index's platform map, which a
    directory holding one platform's archive does not carry, and the
    exact bytes are what the pin exists to name.
    """
    assert pinned(device, sources) == 0
    assert written(device) == {
        "sdk": f"sdk/mcuhome-sdk:{SDK_VERSION}@sha256:{SDK_SHA}",
        "build_workspace": f"build-workspace/mcuhome-build-workspace:0.2.0@sha256:{WORKSPACE_SHA}",
        "build_tools": f"build-tools/mcuhome-build-tools_{PLATFORM}:0.2.0@sha256:{TOOLS_SHA}",
    }
    # Printed as well as written: the block is what the run built with,
    # and a log that states it is the difference between reading a
    # firmware job and re-running it.
    assert capsys.readouterr().out == device_pins.block_for(
        device_pins.pins_for(device_pins.read_indexes(sources), platform=PLATFORM)
    )


def test_a_pin_states_a_bare_version_and_a_hash(device, sources):
    """The spelling is the fully pinned form, and that is not cosmetic.

    ``<version>@sha256:<hash>`` decides an environment package's pin
    outright — no index is read for it and no release chain is walked.
    ``==<version>@sha256:<hash>`` is a *constraint* with a hash beside
    it, which is resolved like any other, and resolution is exactly the
    step that falls through to the package registry when a local
    directory does not hold the wanted version.
    """
    assert pinned(device, sources) == 0
    for key, reference in written(device).items():
        version, _, digest = reference.partition("@")
        assert not version.rpartition(":")[2].startswith("="), key
        assert digest.startswith("sha256:"), key
        assert len(digest) == len("sha256:") + 64, key


def test_the_device_keeps_everything_else_verbatim(device, sources):
    """Only the block is added — the rest is byte for byte the example.

    The firmware jobs build the example device, and a tool that
    reformatted it on the way in would have them build something else.
    """
    before = EXAMPLE.read_text(encoding="utf-8")
    assert pinned(device, sources) == 0
    after = device.read_text(encoding="utf-8")
    assert after.startswith(before)
    assert after[len(before) :].strip().splitlines()[0] == "sources:"


@pytest.mark.parametrize("tail", ["", "\n", "\n\n"])
def test_the_block_is_a_block_however_the_file_ended(device, sources, tail):
    """A key at the top level, whatever the last byte of the device was.

    The block is appended as text, so how the file ended decides whether
    it lands on a line of its own — and a ``sources:`` glued to the last
    value of the device would be a parse error at build time rather than
    a pin.
    """
    device.write_text(device.read_text(encoding="utf-8").rstrip("\n") + tail, encoding="utf-8")
    assert pinned(device, sources) == 0
    assert set(written(device)) == {"sdk", "build_workspace", "build_tools"}


def test_the_example_device_carries_no_sources_block():
    """The example pins nothing, which is what makes CI's pins the only ones.

    An example that starts pinning packages of its own is a legitimate
    change to make — and the firmware jobs would then be writing a second
    answer beside it. This assertion is where that is noticed, rather
    than in a release run whose device file has two ``sources:`` keys.
    """
    document = YAML(typ="safe").load(EXAMPLE.read_text(encoding="utf-8"))
    assert "sources" not in document


def test_a_device_that_already_pins_something_is_refused(device, sources):
    """Two answers to one question are refused, never merged."""
    device.write_text(
        device.read_text(encoding="utf-8") + "\nsources:\n  sdk: sdk/mcuhome-sdk:0.1.9\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as refusal:
        pinned(device, sources)
    assert "already carries a sources: block" in str(refusal.value)


def test_a_stage_the_sources_do_not_hold_is_refused(device, tmp_path):
    """A missing stage is a refusal and names the package it wanted."""
    only_sdk = [index(tmp_path / "sdk", {"mcuhome-sdk": {SDK_VERSION: SDK_SHA}})]
    with pytest.raises(SystemExit) as refusal:
        pinned(device, only_sdk)
    assert "mcuhome-build-workspace" in str(refusal.value)


def test_two_versions_of_one_stage_are_refused(device, sources, tmp_path):
    """The invariant the source directories are built to, asserted.

    One directory per stage and one version in each — a second candidate
    means the build would resolve something nobody stated, which is the
    situation these pins exist to end.
    """
    sources[0] = index(
        tmp_path / "two", {"mcuhome-sdk": {SDK_VERSION: SDK_SHA, "0.2.1": "e5" * 32}}
    )
    with pytest.raises(SystemExit) as refusal:
        pinned(device, sources)
    assert "exactly one" in str(refusal.value)


def test_a_tools_family_entry_is_not_the_platform_package(device, sources, tmp_path):
    """A family entry does not answer for the platform's package.

    Its hash is over the platform map rather than over an archive, so a
    pin built from it would state bytes nothing downloads. The refusal
    says which package to put in the directory instead.
    """
    sources[2] = index(tmp_path / "family", {"mcuhome-build-tools": {"0.2.0": TOOLS_SHA}})
    with pytest.raises(SystemExit) as refusal:
        pinned(device, sources)
    assert f"mcuhome-build-tools_{PLATFORM}" in str(refusal.value)


def test_a_meta_entry_is_refused_outright(device, sources, tmp_path):
    """Even under the platform's own name: a meta entry pins no archive."""
    directory = index(tmp_path / "meta", {f"mcuhome-build-tools_{PLATFORM}": {"0.2.0": TOOLS_SHA}})
    document = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    document["packages"][f"mcuhome-build-tools_{PLATFORM}"]["0.2.0"]["meta"] = {
        PLATFORM: {"package": f"mcuhome-build-tools_{PLATFORM}", "version": "0.2.0"}
    }
    (directory / "index.json").write_text(json.dumps(document), encoding="utf-8")
    sources[2] = directory
    with pytest.raises(SystemExit) as refusal:
        pinned(device, sources)
    assert "meta entry" in str(refusal.value)


def test_a_platform_the_workflow_did_not_state_is_refused(device, sources):
    """An unset environment variable is said so, not pasted onto a name.

    Without the check the refusal would be about a package called
    ``mcuhome-build-tools_`` that no index holds, which sends whoever
    reads it looking in the wrong place.
    """
    with pytest.raises(SystemExit) as refusal:
        pinned(device, sources, platform="")
    assert "no platform" in str(refusal.value)


def test_a_directory_without_an_index_is_no_package_source(device, sources, tmp_path):
    """A download that produced no index is said so, not resolved around."""
    empty = tmp_path / "empty"
    empty.mkdir()
    sources[1] = empty
    with pytest.raises(SystemExit) as refusal:
        pinned(device, sources)
    assert "index.json" in str(refusal.value)


def test_two_sources_disagreeing_about_one_package_are_refused(device, sources, tmp_path):
    """The same name and version with different bytes pins nothing.

    Which of the two the build would fetch depends on the order the
    directories are searched in, and a pin whose meaning depends on that
    is not a pin.
    """
    sources.append(index(tmp_path / "other", {"mcuhome-sdk": {SDK_VERSION: "f6" * 32}}))
    with pytest.raises(SystemExit) as refusal:
        pinned(device, sources)
    assert "different bytes" in str(refusal.value)


# --------------------------------------------------------------------------
# check — what the build resolved
# --------------------------------------------------------------------------


def manifest(
    root: Path,
    *,
    sdk_version: str = SDK_VERSION,
    sdk_sha: str = SDK_SHA,
    workspace: tuple[str, str, str] = ("mcuhome-build-workspace", "0.2.0", WORKSPACE_SHA),
    tools: tuple[str, str, str] = (f"mcuhome-build-tools_{PLATFORM}", "0.2.0", TOOLS_SHA),
) -> ContextManifest:
    """A locked context at *root*, stating what a build resolved.

    Written through the format's own classes and ID rule rather than as
    YAML text: the tool under test reads a manifest with the program's
    own reader, and a fixture that spelled the document by hand could
    agree with a format neither side is free to choose.
    """
    root.mkdir(parents=True, exist_ok=True)
    environment = EnvironmentPin(
        workspace=PackagePin(*workspace),
        tools=PackagePin(*tools),
    )
    document = ContextManifest(
        sdk=SdkPin(constraint=f"=={sdk_version}", version=sdk_version, url="", sha256=sdk_sha),
        build_environment=environment,
        board=BOARD,
        files=(),
        id=context_id(sdk_sha256=sdk_sha, environment=environment, board=BOARD, files=()),
    )
    write_context_manifest(document, out_dir=root)
    return document


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    """A finished build's directory, with its context where one leaves it."""
    return tmp_path / "build" / "bmp180-node"


def checked(device: Path, build_dir: Path) -> int:
    return device_pins.main(["check", "--device", str(device), "--build-dir", str(build_dir)])


def test_the_check_holds_when_the_build_resolved_the_pins(device, sources, build_dir, capsys):
    """The green case: three stages, each resolved to what the device names."""
    assert pinned(device, sources) == 0
    manifest(build_dir / ".mcuhome-local" / "context")
    assert checked(device, build_dir) == 0
    output = capsys.readouterr().out
    assert "resolved exactly what" in output


def test_a_build_that_resolved_another_chain_is_a_finding(device, sources, build_dir, capsys):
    """The failure this tool exists for, in the shape it actually had.

    A release run built the registry's 0.1 chain while the 0.2 packages
    it was gating sat in the workspace unused. Every stage is reported:
    one line of it would leave open whether the rest held.
    """
    assert pinned(device, sources) == 0
    manifest(
        build_dir / ".mcuhome-local" / "context",
        sdk_version="0.1.10.dev3",
        sdk_sha="11" * 32,
        workspace=("mcuhome-build-workspace", "0.1.0", "22" * 32),
        tools=(f"mcuhome-build-tools_{PLATFORM}", "0.1.0", "33" * 32),
    )
    assert checked(device, build_dir) == 1
    captured = capsys.readouterr()
    assert "0.1.10.dev3" in captured.err
    assert captured.err.count("::error::") == 3


def test_one_stage_resolving_elsewhere_is_enough(device, sources, build_dir, capsys):
    """A chain that held in two places and not in the third is still red."""
    assert pinned(device, sources) == 0
    manifest(
        build_dir / ".mcuhome-local" / "context",
        tools=(f"mcuhome-build-tools_{PLATFORM}", "0.1.0", "33" * 32),
    )
    assert checked(device, build_dir) == 1
    assert capsys.readouterr().err.count("::error::") == 1


def test_the_same_version_from_other_bytes_is_a_finding(device, sources, build_dir):
    """Version equality is not enough — the hash is half the pin.

    Two archives of one version are two build environments, and a
    verification that accepted either would verify neither.
    """
    assert pinned(device, sources) == 0
    manifest(build_dir / ".mcuhome-local" / "context", sdk_sha="99" * 32)
    assert checked(device, build_dir) == 1


def test_a_context_elsewhere_under_the_build_directory_is_found(device, sources, build_dir):
    """Where the workbench keeps its work directory is the workbench's business.

    The expected path is tried first and the build directory searched
    when it is not there, so a renamed work directory fails no release.
    """
    assert pinned(device, sources) == 0
    manifest(build_dir / ".mcuhome-elsewhere" / "context")
    assert checked(device, build_dir) == 0


def test_two_contexts_under_one_build_directory_are_refused(device, sources, build_dir):
    """A choice between two resolutions is refused rather than made."""
    assert pinned(device, sources) == 0
    manifest(build_dir / "one" / "context")
    manifest(build_dir / "two" / "context")
    with pytest.raises(SystemExit) as refusal:
        checked(device, build_dir)
    assert "--context" in str(refusal.value)


def test_a_build_that_wrote_no_context_is_refused(device, sources, build_dir):
    """Nothing to check is a finding too, never a pass."""
    assert pinned(device, sources) == 0
    build_dir.mkdir(parents=True)
    with pytest.raises(SystemExit) as refusal:
        checked(device, build_dir)
    assert "no context to check" in str(refusal.value)


def test_a_developer_build_is_refused(device, sources, build_dir):
    """A context that names no packages never used the pins."""
    assert pinned(device, sources) == 0
    root = build_dir / ".mcuhome-local" / "context"
    root.mkdir(parents=True)
    environment = DeveloperEnvironment()
    write_context_manifest(
        ContextManifest(
            sdk=SdkPin(constraint="", version="", url="", sha256=""),
            build_environment=environment,
            board=BOARD,
            files=(),
            id=context_id(sdk_sha256="", environment=environment, board=BOARD, files=()),
        ),
        out_dir=root,
    )
    with pytest.raises(SystemExit) as refusal:
        checked(device, build_dir)
    assert "compiled a workspace of its own" in str(refusal.value)


def test_a_device_pinning_nothing_cannot_be_checked(device, build_dir):
    """The check is about pins, and a device without them has none."""
    manifest(build_dir / ".mcuhome-local" / "context")
    with pytest.raises(SystemExit) as refusal:
        checked(device, build_dir)
    assert "pins nothing" in str(refusal.value)


def test_a_pin_nothing_here_wrote_is_refused(device, build_dir):
    """A hand-written constraint is not checked as though it were a pin.

    Holding ``~=0.2`` against a resolved version is resolution, not
    comparison, and a tool that guessed what a loose reference meant
    would report a chain it never verified.
    """
    device.write_text(
        device.read_text(encoding="utf-8") + "\nsources:\n"
        "  sdk: sdk/mcuhome-sdk:~=0.2\n"
        "  build_workspace: build-workspace/mcuhome-build-workspace:0.2.0\n"
        f"  build_tools: build-tools/mcuhome-build-tools_{PLATFORM}:0.2.0\n",
        encoding="utf-8",
    )
    manifest(build_dir / ".mcuhome-local" / "context")
    with pytest.raises(SystemExit) as refusal:
        checked(device, build_dir)
    assert "not a pin this tool wrote" in str(refusal.value)


def test_the_manifest_is_read_with_the_programs_own_reader(device, sources, build_dir):
    """The fixture is a manifest the build environment's own reader accepts.

    Not an assertion about ``device_pins`` at all: it is what makes every
    green case above mean something. A fixture the program would refuse
    would prove that the check passes on documents no build ever writes.
    """
    assert pinned(device, sources) == 0
    root = build_dir / ".mcuhome-local" / "context"
    written_manifest = manifest(root)
    read_back = read_context_manifest(root / "manifest.yaml")
    assert read_back.id == written_manifest.id
    assert read_back.build_environment == written_manifest.build_environment
