# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The baked container image this repository still builds, by name.

A single subject: what ``containers/build-container/`` is called once it
is built and pushed. The repository's CI tags and pushes exactly the
reference named here, and whoever edits that directory bumps the revision
in the same commit.

**What it is not any more.** No MCUHome build runs in this image. Devices
are built in the environment ``docs/design/build-environment.md``
describes — a package set, delivered as
``ghcr.io/mcu-home/build-environment`` — and the vocabulary both sides of
that boundary share lives in :mod:`mcuhome.model.buildenvironment`. What
this image is still needed for is the one thing no other image can do
yet: ``scripts/build_env_package.py`` lays the environment's west
workspace out and pre-generates the Matter data model inside it, because
the west that lays a workspace out has to be the west that later reads
it, and the pre-generation needs a ``zap`` no lean build environment
carries.

What is deliberately *not* here is anything that acts: finding the
container program, checking whether a daemon is running, working out
where a user's compiler cache lives. Those are things a host does, they
belong to whoever runs a container, and none of them is a fact about the
image.
"""

from __future__ import annotations

__all__ = [
    "DOCKERFILE_DIR",
    "IMAGE",
    "IMAGE_REPOSITORY",
    "IMAGE_REVISION",
    "IMAGE_TAG",
    "LATEST_SUFFIX",
    "ZEPHYR_RELEASE",
    "aggregate_tags",
    "latest_tag",
]


# --------------------------------------------------------------------------
# What the image is called
# --------------------------------------------------------------------------

#: Zephyr release the image is built for. **Lockstep rule: this is the
#: ``revision:`` of the ``zephyr`` project in ``west.yml`` without the
#: leading ``v``** — bumping one without the other is a bug, and
#: ``tests/python`` asserts it. It is also the number
#: ``scripts/build_env_package.py`` writes into the environment's own
#: declaration, read out of this file at the packaged revision.
ZEPHYR_RELEASE = "4.4.0"

#: Rebuilds of the image for the same Zephyr release: a new tool version, a
#: new Python dependency, a fix in the Dockerfile. Starts at 1 and is bumped
#: by whoever changes ``containers/build-container/``, in the same commit.
#:
#: ``west.yml`` and ``patches/`` are image inputs too, because the image
#: bakes a west workspace with the patch set applied: a change to either
#: needs a revision bump just as a change to the Dockerfile does.
IMAGE_REVISION = 11

#: GitHub Container Registry under the MCUHome organization. Public —
#: ``docker pull`` works anonymously.
IMAGE_REPOSITORY = "ghcr.io/mcu-home/build-container"

#: ``zephyr-<release>-r<revision>`` — the immutable name of exactly these
#: bytes, and never ``latest``: an image that changes under a stable name
#: cannot be the reason two package builds agree.
IMAGE_TAG = f"zephyr-{ZEPHYR_RELEASE}-r{IMAGE_REVISION}"

#: The image this revision of the repository builds its environment
#: packages in.
IMAGE = f"{IMAGE_REPOSITORY}:{IMAGE_TAG}"

#: The suffix of the *moving* tags beside the immutable ones: the publisher
#: saying which revision of one Zephyr release he currently recommends.
LATEST_SUFFIX = "-latest"


def latest_tag(release: str) -> str:
    """The moving tag for one exact Zephyr *release* — ``zephyr-4.4.0-latest``.

    The tag somebody lands on who asks for one particular release without
    knowing which revision of it is current.
    """
    return f"zephyr-{release}{LATEST_SUFFIX}"


def aggregate_tags(release: str) -> tuple[str, ...]:
    """The moving tags for the *prefixes* of *release*, longest first.

    ``4.4.0`` yields ``zephyr-4.4-latest`` and ``zephyr-4-latest``: the
    names for "the newest 4.4.x you recommend" and "the newest 4.x".
    """
    parts = release.split(".")
    return tuple(
        f"zephyr-{'.'.join(parts[:count])}{LATEST_SUFFIX}" for count in range(len(parts) - 1, 0, -1)
    )


#: Where the Dockerfile lives, relative to the repository root — quoted in
#: messages that have to stay true.
DOCKERFILE_DIR = "containers/build-container"
