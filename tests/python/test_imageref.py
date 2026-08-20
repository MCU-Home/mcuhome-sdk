# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""How MCUHome names an external input, and where that spelling bites.

The type is small; the tests are not, because a reference is typed by a
person into a configuration file and every ambiguity in docker's own
grammar is one somebody will land on. The three that matter here: a
registry is told from a path component by punctuation, a port colon is
not a tag colon, and an absent registry means something different for an
SDK than for a container.
"""

from __future__ import annotations

import pytest

from mcuhome.model.errors import BuildError
from mcuhome.model.imageref import DOCKER_HUB, Reference, parse_reference
from mcuhome.model.sdkindex import DEFAULT_SDK, SDK_REGISTRY

DIGEST = "sha256:" + "ab" * 32


def image(text: str) -> Reference:
    return parse_reference(text, default_registry=DOCKER_HUB)


def package(text: str) -> Reference:
    return parse_reference(text, default_registry=SDK_REGISTRY)


# --------------------------------------------------------------------------
# what an absent registry means
# --------------------------------------------------------------------------


def test_the_short_sdk_form_means_the_official_package_host() -> None:
    """``sdk/mcuhome-sdk`` is the default a device is created with."""
    found = package(DEFAULT_SDK)
    assert found.registry == SDK_REGISTRY
    assert found.path == "sdk/mcuhome-sdk"
    assert str(found) == f"{SDK_REGISTRY}/sdk/mcuhome-sdk"


def test_a_container_that_names_no_registry_means_docker_hub() -> None:
    """The default differs by kind, so it is a parameter and not a constant."""
    assert image("someone/thing").registry == DOCKER_HUB
    assert package("sdk/thing").registry == SDK_REGISTRY


def test_naming_the_registry_is_how_another_source_is_used() -> None:
    found = package("registry.meine-packages.com/mcuhome/sdk/custom-sdk:v0.0.1")
    assert found.registry == "registry.meine-packages.com"
    assert found.path == "mcuhome/sdk/custom-sdk"
    assert found.tag == "v0.0.1"


@pytest.mark.parametrize(
    ("text", "registry", "path"),
    [
        # A dot makes the first component a host …
        ("ghcr.io/mcu-home/build-container", "ghcr.io", "mcu-home/build-container"),
        # … so does a port, …
        ("registry:5000/thing", "registry:5000", "thing"),
        # … and so does the one name that has neither.
        ("localhost/thing", "localhost", "thing"),
        # Everything else is path, however many components.
        ("a/b/c", DOCKER_HUB, "a/b/c"),
    ],
)
def test_a_registry_is_told_from_a_path_by_punctuation(text: str, registry: str, path: str) -> None:
    """Docker's own rule, and there is no better one available."""
    found = image(text)
    assert (found.registry, found.path) == (registry, path)


def test_a_registry_port_is_not_a_tag() -> None:
    """The colon that splits a tag is the last one, and only without a slash."""
    found = image("registry.example:5000/mcu-home/build-container")
    assert found.tag is None
    assert found.repository == "registry.example:5000/mcu-home/build-container"

    tagged = image("registry.example:5000/mcu-home/build-container:v1")
    assert tagged.tag == "v1"
    assert tagged.registry == "registry.example:5000"


# --------------------------------------------------------------------------
# pinning
# --------------------------------------------------------------------------


def test_a_pinned_reference_keeps_its_tag_and_binds_to_the_digest() -> None:
    """The tag is documentation; the digest is what a build fetches.

    Dropping it would lose the only human-readable part of a record
    somebody reads back a year later, and keeping it costs nothing
    because nothing resolves it again.
    """
    found = image(f"ghcr.io/mcu-home/build-container:zephyr-4.4.0-r10@{DIGEST}")
    assert found.tag == "zephyr-4.4.0-r10"
    assert found.digest == DIGEST
    assert found.pinned
    assert str(found) == f"ghcr.io/mcu-home/build-container:zephyr-4.4.0-r10@{DIGEST}"
    # Run by digest: a reference carrying only a tag is a moving name, and
    # running one after resolving it would throw the resolution away.
    assert found.runnable() == f"ghcr.io/mcu-home/build-container@{DIGEST}"


def test_an_unpinned_reference_is_run_as_it_stands() -> None:
    found = image("ghcr.io/mcu-home/build-container:dev")
    assert not found.pinned
    assert found.runnable() == "ghcr.io/mcu-home/build-container:dev"


def test_pinning_records_which_moving_name_the_digest_came_from() -> None:
    found = image("ghcr.io/mcu-home/build-container").with_digest(DIGEST, tag="zephyr-4.4-latest")
    assert str(found) == f"ghcr.io/mcu-home/build-container:zephyr-4.4-latest@{DIGEST}"


def test_pinning_keeps_an_existing_tag_when_none_is_given() -> None:
    found = image("ghcr.io/mcu-home/build-container:dev").with_digest(DIGEST)
    assert found.tag == "dev"


def test_a_digest_that_is_not_one_is_refused_rather_than_recorded() -> None:
    with pytest.raises(BuildError):
        image("ghcr.io/x").with_digest("sha256:short")


# --------------------------------------------------------------------------
# refusals name the part that is wrong
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "wrong"),
    [
        ("ghcr.io/x@sha256:short", "digest"),
        ("ghcr.io/x@md5:" + "ab" * 16, "digest"),
        ("ghcr.io/x:-leading-hyphen", "tag"),
        ("ghcr.io/x:" + "t" * 129, "tag"),
        ("ghcr.io/MCU-Home/build-container", "repository path"),
        ("ghcr.io/", "repository path"),
        ("", "empty"),
        ("   ", "empty"),
    ],
)
def test_a_refusal_names_the_part_that_is_wrong(text: str, wrong: str) -> None:
    """These values are typed by people into files.

    "Not a valid reference" sends them to reread a line that is nine
    tenths correct.
    """
    with pytest.raises(BuildError) as refusal:
        image(text)
    assert wrong in str(refusal.value)


def test_an_uppercase_path_is_refused_here_rather_than_by_a_stranger() -> None:
    """A registry rejects it with a 400 that says nothing about the cause."""
    with pytest.raises(BuildError):
        image("ghcr.io/MCU-Home/build-container")
