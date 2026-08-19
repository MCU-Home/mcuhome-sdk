# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Pipeline stage 5: turning the generated application into an image.

Stage 4 (:mod:`mcuhome.compiler.generate`) writes a standalone Zephyr
application. This module is everything it takes to compile one with
``west``: the environment two Matter build tools need, the command,
running it, and reading what came out of it — the artifact set and the
memory report.

**One caller, and it is inside a build container** (ADR 0007).
:mod:`mcuhome.compiler.abi` composes a :class:`BuildPlan` from the
invocation request and runs it in the frozen west workspace the image
carries. Nothing here goes looking for a workspace or decides where one
is: that used to be the host-side ``local-dev`` path, which is gone —
a development change reaches a build as a *patch* in the build context,
so the environment it is compiled against is the declared one rather
than whatever a developer's checkout happens to be.

**Two images, since ADR 0015.** Every MCUHome device boots through
MCUboot, and vanilla Zephyr builds a bootloader only under sysbuild, so
what this module drives is ``west build --sysbuild``: one build directory
with one sub-directory per image, an application, and a bootloader that
verifies it against the user's own key (:mod:`mcuhome.workbench.signing`).

**Nothing here knows the device model.** The inputs are a board name, a
snippet list and two directories; whatever produced them is somebody
else's problem. That keeps the interesting parts — command assembly,
prerequisite checking, memory-report parsing — pure functions that the
test suite exercises without ever running west.

**Why the environment has to be set at all.** Two build-time tools of the
Matter SDK are not part of a Zephyr installation: ``gn`` (CHIP builds its
own libraries with it) and ``zap`` (it generates the root-node data model
from the framework's ``.zap``). And CHIP v1.5.1.0's release tarball is
missing the ``python_path`` helper its codegen scripts import, which
``scripts/pyshim/`` stands in for — that one can be fixed here, by putting
the shim on ``PYTHONPATH``; the other two can only be checked for and
explained. The build environment provides all three.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from mcuhome.compiler.generate import BOOTLOADER_IMAGE, DETACHED_SIGNING_VAR
from mcuhome.model.errors import BuildError

__all__ = [
    "BIN_ARTIFACT",
    "BOOTLOADER_IMAGE",
    "CHIP_JOBS_VAR",
    "CMAKE_JOBS_VAR",
    "HEX_ARTIFACT",
    "IMAGE_OUTPUT_DIR",
    "PYSHIM_SUBDIR",
    "SIGNING_KEY_OPTION",
    "TOOLS",
    "BuildPlan",
    "ImageArtifacts",
    "MemoryRegion",
    "ToolNeed",
    "artifacts",
    "build_environment",
    "build_images",
    "image_output",
    "missing_tools",
    "parse_image_memory_report",
    "pristine_mode",
    "require_tools",
    "run_build",
    "west_build_command",
]

#: Stand-in for CHIP's missing ``python_path`` helper (see the module
#: docstring and ``scripts/pyshim/README.md``), **relative to the MCUHome
#: module directory**. A pure value: which module directory it applies to
#: is an argument every time (:func:`plan_build`).
PYSHIM_SUBDIR = Path("scripts") / "pyshim"


#: Sysbuild's output layout, stated once. Every image of a sysbuild build
#: gets a directory named after the image, and Zephyr leaves that image's
#: artifacts in a ``zephyr/`` directory inside it —
#: ``<build dir>/<image>/zephyr/zephyr.hex`` and so on. Three modules read
#: that layout (this one, :mod:`mcuhome.compiler.report`, :mod:`mcuhome.compiler.abi`), and
#: a fourth spelling of it is how one of them would keep reading a layout
#: Zephyr had moved.
IMAGE_OUTPUT_DIR = "zephyr"

#: The two raw forms of a linked image, under the names Zephyr gives
#: them. Named rather than spelled out because the contract's ``build``
#: delivers them under names of its own (:mod:`mcuhome.compiler.abi`) and the
#: mapping is only readable while one side of it is a constant.
HEX_ARTIFACT = "zephyr.hex"
BIN_ARTIFACT = "zephyr.bin"


def image_output(build_dir: Path, image: str) -> Path:
    """Where sysbuild leaves *image*'s artifacts inside *build_dir*."""
    return build_dir / image / IMAGE_OUTPUT_DIR


#: Environment variable the vendored CHIP GN sub-build reads to cap its own
#: inner ``ninja`` invocation (patch hunk in
#: ``patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch``, applied to
#: ``config/common/cmake/chip_gn.cmake``). Upstream always runs a bare
#: ``ninja`` there, so without this the outer ``-o=-j{jobs}`` above is
#: invisible to it and the Matter sub-build regenerates ninja's default of
#: nproc+2 — the exact OOM risk the resolved job count (:func:`resolve_jobs`)
#: exists to avoid, just one process tree down.
CHIP_JOBS_VAR = "MCUHOME_CHIP_JOBS"

#: CMake's own cap on ``cmake --build`` parallelism. Under sysbuild the
#: ``-o=-j{jobs}`` below only reaches the *outer* ninja: each image is an
#: ExternalProject whose build step is a fresh ``cmake --build .``, and
#: nothing of the outer invocation is inherited by it. Without this the
#: inner ninja falls back to its own default of nproc+2, which is the
#: exact OOM the resolved job count exists to prevent. Images do not build
#: concurrently (sysbuild puts their build steps in ninja's ``console``
#: pool), so one cap serves the whole run.
CMAKE_JOBS_VAR = "CMAKE_BUILD_PARALLEL_LEVEL"

#: Sysbuild Kconfig symbol naming the MCUboot signing key. Passed on the
#: command line and never written into the generated tree: it is the path
#: of a per-user secret (ADR 0015 decision 8, :mod:`mcuhome.workbench.signing`).
SIGNING_KEY_OPTION = "SB_CONFIG_BOOT_SIGNATURE_KEY_FILE"

#: Written by sysbuild and by nothing else. Its presence is how a build
#: directory says which kind of build made it (see :func:`pristine_mode`).
_DOMAINS_FILE = "domains.yaml"


@dataclass(frozen=True)
class ToolNeed:
    """One build-time tool the host has to provide, and how to say so."""

    #: What to call it in a message.
    name: str
    #: Executables that satisfy it; any one is enough.
    commands: tuple[str, ...]
    #: Environment variables that satisfy it instead of a PATH entry.
    env_vars: tuple[str, ...]
    #: Why the build needs it, in one clause.
    why: str
    #: Where it comes from, for the fix line.
    source: str

    def satisfied_by(self, env: dict[str, str]) -> bool:
        # The default is "", never None: which(path=None) answers from
        # the *process* environment, and this check exists to judge the
        # **stated** one — the env a build's children will actually run
        # in. A caller that states no PATH has no tools, and hears so as
        # a typed refusal instead of a child that fails to exec.
        path = env.get("PATH", "")
        if any(shutil.which(command, path=path) for command in self.commands):
            return True
        return any(env.get(name) for name in self.env_vars)


#: Checked before west is invoked. West itself is first: without it none of
#: the others matter, and its absence is the one that a plain "command not
#: found" would explain worst.
TOOLS: tuple[ToolNeed, ...] = (
    ToolNeed(
        name="west",
        commands=("west",),
        env_vars=(),
        why="it is the Zephyr build front end",
        source="pip install west",
    ),
    ToolNeed(
        name="gn",
        commands=("gn",),
        env_vars=(),
        why="the Matter SDK builds its own libraries with GN",
        source="https://gn.googlesource.com/gn (a single binary; put it on PATH)",
    ),
    ToolNeed(
        name="zap",
        commands=("zap", "zap-cli"),
        env_vars=("ZAP_INSTALL_PATH",),
        why="it generates the Matter root-node data model from the framework .zap",
        source=(
            "https://github.com/project-chip/zap/releases (put the install "
            "directory on PATH, or point ZAP_INSTALL_PATH at it)"
        ),
    ),
)


# --------------------------------------------------------------------------
# Environment and prerequisites
# --------------------------------------------------------------------------


def build_environment(
    env: dict[str, str],
    *,
    jobs: int,
    pyshim_dir: Path,
    zephyr_base: Path | None = None,
    tmpdir: Path | None = None,
    home: Path | None = None,
) -> dict[str, str]:
    """*env* plus what the Matter build needs, without mutating the input.

    *env* is stated rather than read from the process, for the reason
    :func:`mcuhome.model.jobs.resolve_jobs` gives.

    **This is the one definition of a Matter build environment**, and both
    callers reach it: the workbench's orchestrator for the ``docker run``,
    and :class:`mcuhome.compiler.abi` for the build-container contract's
    ``build`` action. Each adds what only it knows — a ccache location,
    the contract's ``TMPDIR`` — and neither restates what is here. A
    second copy is how one of them silently lost ``HOME``.

    ``ZEPHYR_BASE`` is filled in only when it is not already set: west
    would follow a value someone set on purpose too, and a builder that
    silently disagrees with the tool it drives is worse than one that
    leaves the choice alone. Whether the directory is really there is the
    *caller's* question, because only the caller knows what it is looking
    at — a build environment states the workspace it baked.

    ``TMPDIR`` and ``HOME`` are set unconditionally when given, and both
    override an inherited value on purpose. The contract's ``tmp`` is per
    invocation and emptied by the backend, which is exactly what a child
    process should be scribbling in (build-container-contract.md §4); and
    an inherited ``HOME`` inside a container belongs to whoever built the
    image, not to the UID the build runs as — see
    :data:`mcuhome.model.buildimage.CONTAINER_HOME` for what that costs when it
    is missing.
    """
    prepared = dict(env)
    existing = prepared.get("PYTHONPATH", "")
    entries = [str(pyshim_dir), *[entry for entry in existing.split(os.pathsep) if entry]]
    # Deduplicate while keeping order: re-running the builder inside its own
    # environment must not grow PYTHONPATH one copy at a time.
    seen: dict[str, None] = {}
    for entry in entries:
        seen.setdefault(entry, None)
    prepared["PYTHONPATH"] = os.pathsep.join(seen)
    # Same job count as the outer `-o=-j{jobs}` (west_build_command): the
    # vendored CHIP GN sub-build otherwise ignores it entirely, see
    # CHIP_JOBS_VAR — and so does each sysbuild image's own inner ninja,
    # see CMAKE_JOBS_VAR.
    prepared[CHIP_JOBS_VAR] = str(jobs)
    prepared[CMAKE_JOBS_VAR] = str(jobs)
    if zephyr_base is not None and not prepared.get("ZEPHYR_BASE"):
        prepared["ZEPHYR_BASE"] = str(zephyr_base)
    if tmpdir is not None:
        prepared["TMPDIR"] = str(tmpdir)
    if home is not None:
        prepared["HOME"] = str(home)
    return prepared


def missing_tools(env: dict[str, str]) -> list[ToolNeed]:
    """The entries of :data:`TOOLS` *env* does not provide, in order."""
    return [tool for tool in TOOLS if not tool.satisfied_by(env)]


def _refuse_missing(tools: list[ToolNeed]) -> BuildError:
    names = ", ".join(tool.name for tool in tools)
    noun = "tool" if len(tools) == 1 else "tools"
    lines = [f"the {noun} the build needs, and where each comes from:"]
    for tool in tools:
        lines.append(f"    {tool.name} — {tool.why}")
        lines.append(f"      from: {tool.source}")
    lines.append(
        "A build environment that MCUHome compiles in carries all of them; one "
        "that does not cannot build Matter firmware, whatever else it can do."
    )
    pronoun = "it" if len(tools) == 1 else "them"
    return BuildError(
        f"This build environment has no {names} on its PATH, and MCUHome "
        f"cannot compile without {pronoun}.",
        hint="\n".join(lines),
    )


def require_tools(env: dict[str, str]) -> None:
    """Refuse, naming every missing tool at once, or return."""
    missing = missing_tools(env)
    if missing:
        raise _refuse_missing(missing)


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def pristine_mode(build_dir: Path) -> str:
    """``"auto"``, or ``"always"`` for a build directory of the wrong kind.

    ``auto`` is what a normal rebuild wants: west re-runs CMake from
    scratch when the board or the application directory changed and keeps
    the object files otherwise, which is the difference between a
    ten-minute and a ten-second edit cycle on the Matter tree.

    It cannot cover one case, and that case is a migration every existing
    build directory hits exactly once: a directory built before ADR 0015
    holds a single-image CMake tree whose source directory is the
    application, and sysbuild's is Zephyr's own ``share/sysbuild``. CMake
    refuses that with a message about the source directory not matching,
    which is true and unhelpful. Sysbuild writes :data:`_DOMAINS_FILE` and
    a single-image build does not, so the two are told apart by looking.
    """
    if not (build_dir / "CMakeCache.txt").is_file():
        return "auto"
    return "auto" if (build_dir / _DOMAINS_FILE).is_file() else "always"


def west_build_command(
    *,
    app_dir: Path,
    build_dir: Path,
    board: str,
    snippets: tuple[str, ...] = (),
    bootloader_snippets: tuple[str, ...] = (),
    signing_key: Path | None = None,
    detached_signing: bool = False,
    jobs: int,
    pristine: str = "auto",
) -> list[str]:
    """The ``west build --sysbuild`` invocation for one generated application.

    **Snippets are named per image, never once.** Sysbuild hands a bare
    ``SNIPPET`` down to every image it builds, and an application's
    snippets do not merely fail to apply in a bootloader — ``-S matter``
    puts CHIP's heap sizing and a symbol MCUboot has never heard of into
    MCUboot's Kconfig, and an assignment to an undefined symbol stops the
    build. So the application's list goes to ``<app>_SNIPPET`` (sysbuild
    names the main image after its directory) and the bootloader's to
    ``mcuboot_SNIPPET``, and the global name is left unset.

    **The signing key is an argument, not a file in the tree.** Leaving
    it out is not an error to sysbuild: MCUboot's default is its own demo
    key, whose private half is published. :mod:`mcuhome.workbench.signing` is what
    makes sure there is a real one to pass.

    **Detached signing passes the same argument with a different file.**
    With *detached_signing* the key handed to sysbuild is the *public*
    half — enough for the bootloader, which compiles the public key in,
    and useless for signing, which is the point (ADR 0015 decision 8).
    The generated tree's ``sysbuild.cmake`` reads the variable set here
    and clears the application's copy of the setting, so no signing step
    runs at all (:func:`~mcuhome.compiler.generate.render_detached_signing_cmake`).
    """
    command = [
        "west",
        "build",
        "--board",
        board,
        "--build-dir",
        str(build_dir),
        "--pristine",
        pristine,
        "--sysbuild",
    ]
    # `-o=` and not `-o `: the value starts with a dash, and argparse reads
    # a separate token starting with a dash as the next option.
    command.append(f"-o=-j{jobs}")
    command.append(str(app_dir))

    options: list[str] = []
    if snippets:
        options.append(f"-D{app_dir.name}_SNIPPET={';'.join(snippets)}")
    if bootloader_snippets:
        options.append(f"-D{BOOTLOADER_IMAGE}_SNIPPET={';'.join(bootloader_snippets)}")
    if signing_key is not None:
        # The quotes are part of the value, not shell syntax: CMake writes
        # this straight into a Kconfig fragment, where an unquoted string
        # assignment is a "malformed string literal ... Assignment
        # ignored" warning. Zephyr turns Kconfig warnings into errors, so
        # today that stops the build rather than silently leaving the
        # symbol at MCUboot's default, which is the demo key. Do not rely
        # on that: quote it.
        options.append(f'-D{SIGNING_KEY_OPTION}="{signing_key}"')
    # Always stated, never only when true. It lands in the sysbuild
    # CMake cache, so a build directory that was once built with
    # --no-sign would keep answering "yes" to every later build in it —
    # and that build would produce an unsigned image while its manifest
    # said it had signed one. Passing the answer every time is what makes
    # the flag describe *this* build instead of the last one.
    options.append(f"-D{DETACHED_SIGNING_VAR}={'y' if detached_signing else 'n'}")
    if options:
        command.append("--")
        command += options
    return command


@dataclass(frozen=True)
class BuildPlan:
    """Everything stage 5 needs, decided before anything is executed.

    *command* is the ``west build`` invocation, and *image* names the
    build environment it runs in when the plan was composed for one —
    ``None`` means the toolchain of the machine this runs on, which is
    what the program inside a build container sees.
    """

    topdir: Path
    app_dir: Path
    build_dir: Path
    command: list[str]
    env: dict[str, str]
    image: str | None = None


# --------------------------------------------------------------------------
# Running it
# --------------------------------------------------------------------------


def run_build(plan: BuildPlan, *, stream: TextIO | None = None) -> tuple[int, str]:
    """Run *plan*, echoing the build log live, and return (code, log).

    The log is echoed rather than swallowed on purpose: a Matter build
    takes minutes, and a builder that prints nothing while it runs is
    indistinguishable from one that hung. It is captured as well, because
    the memory report at the end of it is part of the summary.
    """
    stream = sys.stdout if stream is None else stream
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            plan.command,
            cwd=str(plan.topdir),
            env=plan.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as error:
        raise BuildError(
            f"MCUHome could not start the build: {error.strerror}.",
            hint=f"the command was: {' '.join(plan.command)}",
        ) from error

    captured: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        captured.append(line)
        stream.write(line)
        stream.flush()
    return process.wait(), "".join(captured)


# --------------------------------------------------------------------------
# What came out
# --------------------------------------------------------------------------

#: Image formats Zephyr writes that MCUHome has a use for today, in
#: reporting order: the ELF first because that is what a debugger wants,
#: then the raw forms, then the signed ones. The signed forms only exist
#: for images MCUboot chain-loads — the bootloader itself is not signed,
#: it *is* the trust anchor.
_ARTIFACT_NAMES = (
    "zephyr.elf",
    HEX_ARTIFACT,
    BIN_ARTIFACT,
    "zephyr.signed.hex",
    "zephyr.signed.bin",
    "zephyr.signed.confirmed.hex",
    "zephyr.signed.confirmed.bin",
    "zephyr.uf2",
)

#: The file each image is reported by size on. Zephyr's own end-of-build
#: ``Memory region / FLASH / Used Size`` is byte-identical to the size of
#: this file, which makes it the one number available for every image of a
#: multi-image build regardless of what survived in the log.
_FLASH_ARTIFACT = BIN_ARTIFACT

#: Human-readable role per sysbuild image name. Anything else is reported
#: under its own name without a role, which is what a future third image
#: (a firmware loader, a network-core image) should do until it is named
#: here on purpose.
_IMAGE_ROLES = {BOOTLOADER_IMAGE: "bootloader"}


def artifacts(build_dir: Path) -> list[Path]:
    """The images one image directory contains, in reporting order."""
    output = build_dir / IMAGE_OUTPUT_DIR
    return [output / name for name in _ARTIFACT_NAMES if (output / name).is_file()]


@dataclass(frozen=True)
class ImageArtifacts:
    """Everything one image of a sysbuild build produced."""

    #: Sysbuild's name for the image, which is also its sub-directory.
    name: str
    #: ``"bootloader"``, ``"application"``, or the image name again.
    role: str
    files: list[Path]
    #: Size of ``zephyr.bin``, i.e. what the linker put in flash, or None
    #: for an image that produced no raw binary.
    flash_bytes: int | None

    def describe(self) -> str:
        size = "" if self.flash_bytes is None else f"  {self.flash_bytes / 1024:.1f} KiB"
        return f"{self.name} ({self.role}){size}"


def build_images(build_dir: Path, *, app_image: str) -> list[ImageArtifacts]:
    """One entry per sysbuild image that produced something, bootloader first.

    Order is boot order, which is also install order and the order the
    two things a user does with them happen in.
    """
    found: list[ImageArtifacts] = []
    for name in (BOOTLOADER_IMAGE, app_image):
        files = artifacts(build_dir / name)
        if not files:
            continue
        binary = image_output(build_dir, name) / _FLASH_ARTIFACT
        found.append(
            ImageArtifacts(
                name=name,
                role=_IMAGE_ROLES.get(name, "application" if name == app_image else name),
                files=files,
                flash_bytes=binary.stat().st_size if binary.is_file() else None,
            )
        )
    return found


@dataclass(frozen=True)
class MemoryRegion:
    """One line of Zephyr's ``Memory region`` table."""

    name: str
    used: int
    total: int
    percent: float

    def describe(self) -> str:
        return (
            f"{self.name} {self.used / 1024:.1f} KiB of "
            f"{self.total / 1024:.1f} KiB ({self.percent:.1f}%)"
        )


_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}

#: ``           FLASH:      859672 B         1 MB      81.99%``
_REGION = re.compile(
    r"^\s*(?P<name>\w+):\s+"
    r"(?P<used>\d+)\s*(?P<used_unit>[KMG]?B)\s+"
    r"(?P<total>\d+)\s*(?P<total_unit>[KMG]?B)\s+"
    r"(?P<percent>[\d.]+)%"
)


#: ``[1/2] Performing build step for 'mcuboot'`` — what the outer ninja
#: prints when it hands over to one image's own build. It is the only
#: marker in a sysbuild log that says whose output follows; Zephyr's
#: memory report itself names no image.
_IMAGE_STEP = re.compile(r"Performing build step for '(?P<image>[^']+)'")


def parse_image_memory_report(log: str, *, images: Sequence[str]) -> dict[str, list[MemoryRegion]]:
    """The footprint table of each image, by sysbuild image name.

    A sysbuild build prints one report per image, and nothing in the
    report says which image it belongs to — so the reports are attributed
    by the build-step banner that precedes them. An incremental build
    that relinked only one image reports only that one, which is correct
    rather than incomplete: the other image did not change.

    *images* is required and is not a convenience: the same banner is
    printed for every nested ExternalProject, and the Matter build has
    one — ``chip-gn``, which runs inside the application's build and
    would otherwise be credited with the application's footprint.
    Banners for anything not named here are ignored, so what follows them
    still belongs to the image whose build they are part of.

    Output ordering follows the log, which is the order sysbuild happened
    to build in and deliberately not something the caller should rely on.
    """
    known = set(images)
    by_image: dict[str, list[MemoryRegion]] = {}
    current: str | None = None
    for line in log.splitlines():
        step = _IMAGE_STEP.search(line)
        if step is not None:
            if step["image"] in known:
                current = step["image"]
            continue
        match = _REGION.match(line)
        if match is None or current is None:
            continue
        by_image.setdefault(current, []).append(
            MemoryRegion(
                name=match["name"],
                used=int(match["used"]) * _UNITS[match["used_unit"]],
                total=int(match["total"]) * _UNITS[match["total_unit"]],
                percent=float(match["percent"]),
            )
        )
    return by_image
