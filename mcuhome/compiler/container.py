# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""What this host knows about the build image, before anything runs.

ADR 0007 makes the builder image *the* build environment: the host needs
git and docker, everything else — Zephyr SDK, west, gn, zap, ccache —
lives in an image versioned in lockstep with the Zephyr pin
(``containers/build-container/``). This module is the small half of that
which is decided **outside** a build: what the image is called
(:data:`IMAGE` and the revision history behind it), which container
program to drive, whether that program and its daemon are there at all
(:func:`preflight`, three refusals with three different fixes), and where
this user's compiler cache lives (:func:`ccache_directory`).

Driving a build is elsewhere and deliberately not here.
:mod:`mcuhome.compiler.localbackend` is the backend of the build-container
contract — it starts the container, mounts what a session needs at the
paths of :mod:`mcuhome.model.containerpaths`, and speaks the invocation
ABI. :mod:`mcuhome.compiler.workspace` keeps the ``local-dev`` path for
people who already have a west workspace with a toolchain in it, which is
MCUHome's own contributors and nobody else.

This module used to carry a third way of compiling — a ``docker run``
around ``west build``, from before the contract existed. It went when its
last caller did: two code paths that start containers differently is one
too many, and the surviving one is the one the contract describes.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from mcuhome.model.errors import BuildError
from mcuhome.model.userpaths import expand, home

__all__ = [
    "CCACHE_DIR_VAR",
    "CONTAINER_HOME",
    "DOCKER_VAR",
    "IMAGE",
    "IMAGE_REPOSITORY",
    "IMAGE_REVISION",
    "IMAGE_TAG",
    "IMAGE_VAR",
    "ZEPHYR_RELEASE",
    "ccache_directory",
    "docker_program",
    "image_reference",
    "missing_image_refusal",
    "preflight",
]

# --------------------------------------------------------------------------
# What the image is called
# --------------------------------------------------------------------------

#: Zephyr release the image is built for. **Lockstep rule (ADR 0007/0008):
#: this is the ``revision:`` of the ``zephyr`` project in ``west.yml``
#: without the leading ``v``.** Bumping one without the other is a bug.
ZEPHYR_RELEASE = "4.4.0"

#: Rebuilds of the image for the same Zephyr release: a new tool version,
#: a new Python dependency, a fix in the Dockerfile. Starts at 1 and is
#: bumped by whoever changes ``containers/build-container/``, in the same commit.
#:
#: r2 adds ``cryptography`` — MCUboot's imgtool imports it at module load,
#: so r1 could not build the MCUboot image at all: sysbuild runs
#: ``imgtool getpub`` to generate ``autogen-pubkey.c`` on every build,
#: signing or not. Every Matter image built before 2026-08-09 therefore
#: came from the host method, where the host interpreter supplied it.
#:
#: r3 adds a real west workspace at ``/mcuhome/workspace`` (ADR 0020
#: decision E5): Zephyr, its modules, MCUboot and the Matter SDK at the
#: revisions ``west.yml`` pins, with ``patches/`` applied, plus a record
#: of what that is at ``/mcuhome/workspace.json``. The manifest
#: repository's directory is there and empty — ADR 0018 makes the SDK a
#: hash-pinned package fetched per build, so it is mounted, not baked.
#: The point is that ``git describe`` in the workspace decides
#: ``BUILD_VERSION`` and therefore the firmware bytes, so the workspace's
#: git state is a build input; baked, it is a property of the image
#: digest. Nothing reads it yet — this module still mounts the host's
#: workspace — which is why r3 changes no build output.
#:
#: From r3 on, ``west.yml`` and ``patches/`` are image inputs too: a
#: change to either needs a revision bump just as ``containers/build-container/``
#: does.
#:
#: r4 installs the program of the build-container contract at
#: ``/mcuhome/run`` (§2.2) — a thin launcher over :mod:`mcuhome.compiler.abi`,
#: which is where the invocation ABI and the actions of §7 live. That
#: module is **not** image content: it arrives with the SDK mount, so
#: adding an action to it does not change this image and does not bump
#: this number.
#:
#: r5 adds the ``org.mcuhome.*`` labels of §2.1 and nothing else. The
#: conformance claim became true elsewhere — all three actions of §7
#: implemented, and §4's D1 erratum sanctioning the declared SDK mount
#: point — but a label is image metadata, and image metadata is an image
#: change (the tag is the content identity, so a label under an old tag
#: would make two different images answer to one name).
#:
#: r6 = the launcher follows the package split, nothing else. ADR 0020's
#: migration moved the invocation ABI to :mod:`mcuhome.compiler.abi`, and
#: ``/mcuhome/run`` is the one file that names it by its import path —
#: image content, unlike the module it launches, which still arrives with
#: the SDK mount.
#:
#: r7 = ``/mcuhome/describe.json`` and the label grammar, which are the
#: same subject seen twice: everything a backend may learn about this
#: image *before* it starts a container. The file is §2.2's optional
#: static self-description, and the Dockerfile generates it by running
#: the program's own ``describe`` at image build time, so it cannot say
#: anything the program would not. It matters because §6.1 splits this
#: program in two — the launcher is image content, the body arrives with
#: the SDK mount — and a backend that has not chosen a mount point yet
#: therefore has no way to ask. The labels are the older half of the same
#: promise and were unusable in all three spellings until here: a name
#: ADR 0020's rename walked into, a Zephyr value carrying the ``v``
#: §2.1.1 forbids, and a toolchain value with a ``/`` outside the
#: permitted character class. A constraint is evaluated against those
#: values, and "a container that does not carry a named label does not
#: qualify" — so this image satisfied no SDK release's constraint at all.
#:
#: r9 = the compiler cache, in the two roles ccache itself has, at two
#: paths the image configures and that a build therefore never has to be
#: told about (``/ccache/cache-local`` writable, ``/ccache/cache-shared``
#: read-only). ``CCACHE_DIR`` leaves the environment in the same move,
#: because an environment variable overrides the file that is supposed to
#: decide this. The measurement behind it: a real build made 1318
#: cacheable compiles and took 2 of them from a cache — the only cache
#: there was lived in the session's ``work`` directory, and every build
#: wipes that before it starts.
IMAGE_REVISION = 9

#: GitHub Container Registry under the MCUHome organization. Public
#: since 2026-08-15 — ``docker pull`` works anonymously.
IMAGE_REPOSITORY = "ghcr.io/mcu-home/build-container"

#: ``zephyr-<line>-r<revision>``, and never ``latest``: a build
#: environment that changes under a stable name is not one. CI also
#: publishes the moving ``zephyr-<line>`` alias for people who want the
#: newest revision of a Zephyr line, but the builder never asks for it.
IMAGE_TAG = f"zephyr-{ZEPHYR_RELEASE}-r{IMAGE_REVISION}"

#: The image this version of the builder compiles in.
IMAGE = f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"

#: Overrides :data:`IMAGE`. For a locally built image, a mirror, or a
#: bisect across image revisions. ``--container-image`` beats it, it beats the
#: default.
IMAGE_VAR = "MCUHOME_BUILDER_IMAGE"

#: Overrides where the ccache lives on the host. Useful for putting it on
#: a faster disk, or for sharing one cache between checkouts.
CCACHE_DIR_VAR = "MCUHOME_CCACHE_DIR"

#: Overrides the container program. ``podman`` is command-line compatible
#: for everything used here; it is not tested, hence a variable and not a
#: documented feature.
DOCKER_VAR = "MCUHOME_DOCKER"

#: A writable ``HOME`` for a UID that has no entry in the container's
#: ``/etc/passwd`` — which is the normal case, because the UID comes from
#: the host. Without it, tools that cache in ``$HOME`` fail obscurely.
CONTAINER_HOME = "/tmp/mcuhome-home"

#: Where the Dockerfile lives, relative to the repository root — quoted
#: in the "you have no image" message, so it has to stay true.
DOCKERFILE_DIR = "containers/build-container"


def image_reference(env: dict[str, str], *, override: str | None = None) -> str:
    """Which image to build in: ``--container-image``, then the variable, then the pin."""
    if override:
        return override
    return env.get(IMAGE_VAR) or IMAGE


def docker_program(env: dict[str, str]) -> str:
    """The container program to drive."""
    return env.get(DOCKER_VAR) or "docker"


def ccache_directory(env: dict[str, str]) -> Path:
    """Where the compiler cache lives on the host — the root of both roles.

    A host directory rather than a named docker volume, for reasons that
    decide it together: a fresh named volume is created root-owned and a
    container running as the calling user cannot write to it; the shared
    half is meant to be filled from outside, and there is no way into a
    named volume without starting a container; and the cache has to be
    listable, movable and deletable when docker is not running at all
    (the ``subprocess`` profile has no docker in the first place). On
    Linux the two are the same bind mount underneath, so nothing is
    traded away for it.

    One cache per user, not per project. Its keys are content addresses —
    the preprocessed source, the compiler's own bytes, the command line —
    so two projects share an entry exactly when the compilation is the
    same compilation, and a per-project split would cost the sharing
    while protecting nothing. Isolation between *parties* is a different
    question with a different answer, and it belongs to whoever serves
    more than one of them.
    """
    override = env.get(CCACHE_DIR_VAR)
    if override:
        return expand(override, env)
    if os.name == "nt":
        # LOCALAPPDATA, not APPDATA: the latter roams, and a five-gigabyte
        # compiler cache has no business being copied to a file server at
        # every logon. An environment that names neither falls through to
        # the POSIX form below, which is wrong on Windows but is a path
        # rather than a crash.
        local = env.get("LOCALAPPDATA")
        if local:
            return expand(local, env) / "mcuhome" / "ccache"
    cache_home = env.get("XDG_CACHE_HOME")
    base = expand(cache_home, env) if cache_home else home(env) / ".cache"
    return base / "mcuhome" / "ccache"


# --------------------------------------------------------------------------
# Is there a container runtime at all?
# --------------------------------------------------------------------------

#: Runs a command, discards its output, and answers with the exit status —
#: or ``None`` when the program does not exist. The one impure thing in
#: this module, injectable so the test suite never needs docker.
Runner = Callable[[Sequence[str], dict[str, str]], int | None]


def _run_quiet(command: Sequence[str], env: dict[str, str]) -> int | None:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(command),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return None
    return completed.returncode


def _refuse_no_docker(docker: str) -> BuildError:
    return BuildError(
        f"MCUHome compiles in a container and cannot find {docker} on your PATH.",
        hint=(
            "install Docker — https://docs.docker.com/engine/install/ — and run the "
            "same command again. That is the whole host setup: the Zephyr SDK, gn, "
            "zap and ccache live in the MCUHome builder image, not on your machine.\n"
            "Building without a container is a build mode: mcuhome device build --help."
        ),
    )


def _refuse_no_daemon(docker: str) -> BuildError:
    return BuildError(
        f"MCUHome found {docker}, but cannot talk to the Docker daemon.",
        hint=(
            "start it and run the same command again:\n"
            "    sudo systemctl start docker      # Linux, system service\n"
            "    open -a Docker                   # macOS, Docker Desktop\n"
            f"If {docker} only works under sudo, add yourself to the `docker` group "
            "and log in again. Building without a container is a build mode: "
            "mcuhome device build --help."
        ),
    )


def missing_image_refusal(docker: str, reference: str) -> BuildError:
    """The one missing-image refusal, wherever the absence is noticed.

    PO-worded (2026-08-15): what is missing, the pull command as the
    fix, and one pointer at the build command's help for choosing a
    different image or another build mode — nothing else. Both the
    docker preflight and the image-resolution seam raise exactly this,
    so the user reads one text however the build got there.
    """
    if reference == IMAGE:
        message = "The default build container is missing on this host."
    else:
        message = f"The build container {reference} is missing on this host."
    return BuildError(
        message,
        hint=(
            "pull the image, then rerun the build:\n"
            f"    {docker} pull {reference}\n"
            "mcuhome device build --help shows how to select a different "
            "container image or how to choose another build mode."
        ),
    )


def preflight(
    docker: str,
    reference: str,
    *,
    env: dict[str, str],
    runner: Runner | None = None,
) -> None:
    """Refuse before the build starts, naming the one thing that is wrong.

    Three failures with three different fixes — no docker, no daemon, no
    image — and a build that dies ten seconds in with somebody else's
    error text does not tell them apart.

    *runner* defaults to :func:`_run_quiet`, resolved here rather than in
    the signature: a default bound at definition time cannot be replaced
    by monkeypatching the module, and a test that thinks it stubbed
    docker out but did not is a test that starts a real build.
    """
    runner = _run_quiet if runner is None else runner
    status = runner([docker, "version", "--format", "{{.Server.Version}}"], env)
    if status is None:
        raise _refuse_no_docker(docker)
    if status != 0:
        raise _refuse_no_daemon(docker)
    if runner([docker, "image", "inspect", reference], env) != 0:
        raise missing_image_refusal(docker, reference)
