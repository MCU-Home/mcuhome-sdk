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

What the default *is* changed with the client-side pin: it is a bare
repository (:data:`DEFAULT_ENVIRONMENT`), not one image. The publisher
recommends a revision through the moving tags below, a client picks the
Zephyr release out of what its SDK declares, and the labels decide
whether the two agree. :data:`IMAGE` — one exact image — survives as
what *this* repository builds and tags, which is a different question
from what a build runs.
"""

from __future__ import annotations

__all__ = [
    "CCACHE_DIR_VAR",
    "CONTAINER_HOME",
    "CONTRACT_LABEL",
    "DEFAULT_ENVIRONMENT",
    "DOCKERFILE_DIR",
    "DOCKER_VAR",
    "IMAGE",
    "IMAGE_REPOSITORY",
    "IMAGE_REVISION",
    "IMAGE_TAG",
    "IMAGE_VAR",
    "LABEL_PREFIX",
    "LATEST_SUFFIX",
    "REQUIRED_LABELS",
    "TOOLCHAIN_LABEL",
    "ZEPHYR_LABEL",
    "ZEPHYR_RELEASE",
    "aggregate_tags",
    "latest_tag",
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
#:
#: r10 = the labels are renamed to the ``org.mcuhome.build-environment.*``
#: scheme below, and the Zephyr one states its subject (``.zephyr.version``).
#: An image is now chosen by a *client*, which reads those labels out of a
#: registry before it has pulled anything, so what they are called is part
#: of how an environment is found rather than a detail of how one is
#: checked afterwards. Images built before this carry the old names, do not
#: qualify, and are not meant to — the names are the break.
IMAGE_REVISION = 10

#: GitHub Container Registry under the MCUHome organization. Public
#: since 2026-08-15 — ``docker pull`` works anonymously.
IMAGE_REPOSITORY = "ghcr.io/mcu-home/build-container"

#: ``zephyr-<release>-r<revision>`` — the immutable name of exactly these
#: bytes, and never ``latest``: a build environment that changes under a
#: stable name is not one.
IMAGE_TAG = f"zephyr-{ZEPHYR_RELEASE}-r{IMAGE_REVISION}"

#: The image this version of the builder compiles in.
IMAGE = f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"

#: The suffix of the *moving* tags beside the immutable ones. Publishing
#: them is the publisher's half of how an environment is found: a client
#: knows which Zephyr it needs and asks for that, and the publisher says
#: which revision of it he currently recommends. Nobody else can — the
#: publisher is the party that knows his container was fixed yesterday.
LATEST_SUFFIX = "-latest"


def latest_tag(release: str) -> str:
    """The moving tag for one exact Zephyr *release* — ``zephyr-4.4.0-latest``.

    Required of every build environment. It is the tag a client lands on
    when it asks for one particular release, and the only tag whose
    existence a resolution may assume, because a publisher who has a
    release at all can always say which of its revisions he recommends.
    """
    return f"zephyr-{release}{LATEST_SUFFIX}"


def aggregate_tags(release: str) -> tuple[str, ...]:
    """The moving tags for the *prefixes* of *release*, longest first.

    ``4.4.0`` yields ``zephyr-4.4-latest`` and ``zephyr-4-latest``: the
    names for "the newest 4.4.x you recommend" and "the newest 4.x". They
    are **recommended, not required**. A client that asks for a range
    rather than one release tries them first and gets its answer in two
    requests; a publisher who does not keep them costs that client a tag
    listing and nothing else.
    """
    parts = release.split(".")
    return tuple(
        f"zephyr-{'.'.join(parts[:count])}{LATEST_SUFFIX}" for count in range(len(parts) - 1, 0, -1)
    )


#: What a device is built in when nothing says otherwise: this project's
#: build environment, named as a bare repository. **No tag on purpose** —
#: a reference that named one would freeze every device created while it
#: was the default onto that revision, and choosing the revision is the
#: publisher's job (:func:`latest_tag`). What the *client* chooses is the
#: Zephyr release, out of what its SDK declares.
DEFAULT_ENVIRONMENT = IMAGE_REPOSITORY

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


# --------------------------------------------------------------------------
# What the image says about itself
# --------------------------------------------------------------------------

#: The namespace of every label a build environment declares itself with.
#: It names the *thing* — a build environment — rather than the project,
#: because a third party publishes one of these too, under this scheme,
#: and reads it the same way.
LABEL_PREFIX = "org.mcuhome.build-environment"

#: The build-container contract version the image implements. A whole
#: number as a string; the one label whose value is also stated by the
#: program inside the image, so the two can be cross-checked.
CONTRACT_LABEL = f"{LABEL_PREFIX}.contract"

#: The **exact Zephyr release** the environment carries — ``4.4.0``, never
#: a line and never west's leading ``v``. It is the label a constraint is
#: evaluated against, which is why it says ``version``: what a client
#: states is a range, and what an image states is one point in it.
ZEPHYR_LABEL = f"{LABEL_PREFIX}.zephyr.version"

#: Which toolchain the environment builds with (``zephyr-sdk-1.0.1``).
#: Carried, not constrained: nothing selects on it today, and a build
#: report that cannot say which compiler produced the bytes is worth less
#: than the label costs.
TOOLCHAIN_LABEL = f"{LABEL_PREFIX}.toolchain"

#: The three together — what an image must carry to be one of these at
#: all. **Absence is never read as compatible**: an environment that does
#: not say what it builds against has not made the declaration a
#: constraint is written against, so a missing label disqualifies rather
#: than defaulting.
REQUIRED_LABELS = (CONTRACT_LABEL, ZEPHYR_LABEL, TOOLCHAIN_LABEL)
