# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""What this repository publishes as its build image (``model/buildimage.py``).

Three facts, and all three are about *this repository* rather than about
any host: the image tag follows the Zephyr pin in ``west.yml``, the
reference is never a moving name, and the Dockerfile is where the
refusal that mentions it says it is. They are checked here because this
is the repository that builds and tags the image — its CI reads the same
constants — while the host-side half that resolves and drives it is the
workbench's, and is tested there (``test_buildenv.py``).

Nothing here runs docker, and nothing here needs to: none of these is a
question about a machine.
"""

from __future__ import annotations

import re
from pathlib import Path

from mcuhome.model import buildimage

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_image_tag_is_the_zephyr_pin_plus_a_revision() -> None:
    """ADR 0007: images are versioned in lockstep with the Zephyr pin.

    Read out of west.yml rather than restated, because a lockstep rule
    nobody checks is a rule that holds until the first bump. Bumping
    Zephyr without rebuilding the image fails here, which is the point.
    """
    manifest = (REPO_ROOT / "west.yml").read_text(encoding="utf-8")
    pinned = re.search(r"name: zephyr\s+remote: \S+\s+revision: v(\S+)", manifest)
    assert pinned is not None, "west.yml no longer states the Zephyr revision as expected"
    assert pinned.group(1) == buildimage.ZEPHYR_RELEASE
    assert f"zephyr-{pinned.group(1)}-r{buildimage.IMAGE_REVISION}" == buildimage.IMAGE_TAG


def test_the_image_is_never_latest() -> None:
    """A build environment that changes under a stable name is not one."""
    assert f"{buildimage.IMAGE_REPOSITORY}:{buildimage.IMAGE_TAG}" == buildimage.IMAGE
    assert not buildimage.IMAGE.endswith(":latest")


def test_the_dockerfile_is_where_the_error_message_says_it_is() -> None:
    assert (REPO_ROOT / buildimage.DOCKERFILE_DIR / "Dockerfile").is_file()
