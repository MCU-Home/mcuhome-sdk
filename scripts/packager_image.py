#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The one place this repository names its build-environment packager image.

``containers/build-environment-packager/`` produces the pinned toolchain
that ``scripts/build_env_package.py`` builds MCUHome's two
build-environment packages in — west at the environment's own version,
the ``zap`` the pinned CHIP revision names, and the base image's Python,
which decides the wheel set's ABI. This module states what that image is
called, and it is imported rather than restated: the packaging script
runs it, ``.github/workflows/ci-build.yml`` builds and publishes it, and
``.github/workflows/release.yml`` pulls it before a package job starts.

**The identity is the digest, never the tag.** A tag is a location — it
can be moved, and an image that changes under a stable name cannot be the
reason two package builds agree. So :data:`DIGEST` is what
:func:`reference` hands out, and :data:`TAG` exists for the one party that
has to address an image that does not exist yet: the job that publishes
it.

The tag is ``<sdk version>-r<n>``. The version part is the first SDK
version that needs this packager — read it as "for SDKs from this version
on", not as "built from this commit"; there is no alias tag per release.
``-r<n>`` counts rebuilds with the same tool set: a base refresh, a
security fix, anything that changes the bytes without changing what the
image is. Both parts move by hand, in the commit that changes the image.
"""

from __future__ import annotations

__all__ = [
    "DIGEST",
    "DOCKERFILE_DIR",
    "REPOSITORY",
    "TAG",
    "publish_reference",
    "reference",
]

#: GitHub Container Registry under the MCUHome organization. Public —
#: ``docker pull`` works anonymously.
REPOSITORY = "ghcr.io/mcu-home/build-environment-packager"

#: The tag the publishing job pushes, and the only place a version number
#: for this image is written down.
TAG = "0.1.10.dev3-r1"

#: The bytes :data:`TAG` resolved to when it was published — an index
#: digest, covering both architectures.
#:
#: **Empty until the first publish.** The image cannot be pinned by a
#: digest before it has one, so everything that *runs* the packager
#: refuses legibly while this is empty (:func:`reference`), and the jobs
#: that build and publish it use :func:`publish_reference` instead. Filling
#: it in is the step that follows the first successful publish; the
#: refusal below says how.
DIGEST = ""

#: Where the Dockerfile lives, relative to the repository root — quoted in
#: messages that have to stay true.
DOCKERFILE_DIR = "containers/build-environment-packager"


def publish_reference() -> str:
    """``<repository>:<tag>`` — where the publishing job puts the image.

    The one reference that is usable before the image exists, and the only
    one the build and publish jobs need: they address a location, they do
    not consume a pinned environment.
    """
    return f"{REPOSITORY}:{TAG}"


def reference() -> str:
    """``<repository>@<digest>`` — the packager to run a package build in.

    Refuses while :data:`DIGEST` is empty, because an unpinned toolchain
    would make the packages it produces unattributable to any bytes.
    """
    if not DIGEST:
        raise SystemExit(
            f"{REPOSITORY} is not pinned yet.\n"
            f"The image has to be published once before a package build can name its\n"
            f"bytes. Publish {publish_reference()} with the Build workflow "
            f"(push to main),\n"
            f"take the index digest it prints, and write it into\n"
            f"scripts/packager_image.py:\n"
            f'\n    DIGEST = "sha256:<the index digest>"\n\n'
            f"To run against an image you built yourself in the meantime, pass\n"
            f"--packager-image <reference> to scripts/build_env_package.py."
        )
    return f"{REPOSITORY}@{DIGEST}"
