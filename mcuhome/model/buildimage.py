# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build image this project publishes, as a statement both sides read.

Two parties need the same handful of facts about the build container and
they sit on opposite sides of the split. **This repository builds it** —
its CI tags and pushes exactly the reference named here, its tests check
that reference against the Zephyr pin in ``west.yml``, and whoever edits
``containers/build-container/`` bumps the revision in the same commit.
**A workbench runs it** — it resolves which image a build compiles in,
and the default is this one.

So the facts live here, in the vocabulary package both sides already
depend on, rather than in either side's own code. What is deliberately
*not* here is anything that acts: finding the container program, checking
whether a daemon is running, working out where a user's compiler cache
lives. Those are things a host does, they belong to the orchestrator
(``mcuhome.workbench.buildenv``), and none of them is a fact about the
image.

The default is an interim. A build states which environment it wants,
and pinning that in a device configuration is what replaces a constant
compiled into a tool.
"""

from __future__ import annotations

__all__ = [
    "CCACHE_DIR_VAR",
    "CONTAINER_HOME",
    "DOCKERFILE_DIR",
    "DOCKER_VAR",
    "IMAGE",
    "IMAGE_REPOSITORY",
    "IMAGE_REVISION",
    "IMAGE_TAG",
    "IMAGE_VAR",
    "ZEPHYR_RELEASE",
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
