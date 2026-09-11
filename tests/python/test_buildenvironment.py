# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build environment's self-description and a package's own meta file.

``mcuhome.model.buildenvironment`` parses two documents that share one
vocabulary: ``build-environment.json`` (an environment's own §5
declaration, read straight or through an image's labels) and ``meta.json``
(what a package says about itself — what it is, what it requires of the
stage below it, and what went into it). Both are read before a build
starts and both decide whether it starts at all, so every refusal is
checked for the thing a builder or an operator actually needs from it:
which member is missing, which package name is not a package name, which
hash is not a hash.
"""

from __future__ import annotations

import pytest

from mcuhome.model.buildenvironment import (
    GENERATOR_CONSTRAINT_MEMBER,
    GENERATOR_CONSTRAINT_MODE_MEMBER,
    MODE_CHAIN,
    MODE_STRICT,
    SPEC_GENERATION_MEMBER,
    ZEPHYR_VERSION_MEMBER,
    Declaration,
    PackageMember,
    declaration_from_labels,
    family_of,
    member_name,
    parse_declaration,
    parse_member,
    parse_meta,
)
from mcuhome.model.errors import BuildError

HASH_A = "a" * 64
HASH_B = "b" * 64


# --------------------------------------------------------------------------
# parse_member / PackageMember
# --------------------------------------------------------------------------


def test_parse_member_accepts_a_bare_version() -> None:
    member = parse_member("mcuhome-build-tools", "0.1.10.dev1", what="test")
    assert member == PackageMember(name="mcuhome-build-tools", version="0.1.10.dev1")


def test_parse_member_accepts_a_version_with_a_hash() -> None:
    member = parse_member("mcuhome-build-tools", f"0.1.10@sha256:{HASH_A}", what="test")
    assert member == PackageMember(name="mcuhome-build-tools", version="0.1.10", sha256=HASH_A)


def test_parse_member_refuses_an_uppercase_hash() -> None:
    """§5.1 fixes lowercase hex; an uppercase digest is a different spelling, not the same bytes."""
    with pytest.raises(BuildError) as caught:
        parse_member("mcuhome-build-tools", f"0.1.10@sha256:{HASH_A.upper()}", what="test")
    assert "mcuhome-build-tools" in caught.value.message


def test_parse_member_refuses_a_short_hash() -> None:
    with pytest.raises(BuildError):
        parse_member("mcuhome-build-tools", f"0.1.10@sha256:{HASH_A[:63]}", what="test")


def test_parse_member_refuses_a_missing_sha256_prefix() -> None:
    """The ``@`` marks a hash, so what follows it must say ``sha256:``, not just look like one."""
    with pytest.raises(BuildError):
        parse_member("mcuhome-build-tools", f"0.1.10@{HASH_A}", what="test")


def test_parse_member_refuses_a_non_string_value() -> None:
    with pytest.raises(BuildError):
        parse_member("mcuhome-build-tools", 1, what="test")  # type: ignore[arg-type]


def test_parse_member_refuses_an_empty_value() -> None:
    with pytest.raises(BuildError):
        parse_member("mcuhome-build-tools", "", what="test")


def test_parse_member_refuses_an_uppercase_package_name() -> None:
    with pytest.raises(BuildError) as caught:
        parse_member("MCUHome-Build-Tools", "0.1.10", what="test")
    assert "MCUHome-Build-Tools" in caught.value.message


def test_parse_member_refuses_an_otherwise_illegal_package_name() -> None:
    """A space is not "lowercase letters, digits and -", whichever position it is in."""
    with pytest.raises(BuildError):
        parse_member("mcuhome build tools", "0.1.10", what="test")


def test_parse_member_refuses_a_name_with_two_underscores() -> None:
    """§5.1 admits exactly one architecture suffix; a second ``_`` is not that grammar."""
    with pytest.raises(BuildError):
        parse_member("mcuhome-build-tools_linux_amd64", "0.1.10", what="test")


def test_package_member_value_round_trips_the_bare_form() -> None:
    member = PackageMember(name="mcuhome-build-tools", version="0.1.10")
    assert member.value() == "0.1.10"
    assert parse_member("mcuhome-build-tools", member.value(), what="test") == member


def test_package_member_value_round_trips_the_hashed_form() -> None:
    member = PackageMember(name="mcuhome-build-tools", version="0.1.10", sha256=HASH_A)
    assert member.value() == f"0.1.10@sha256:{HASH_A}"
    assert parse_member("mcuhome-build-tools", member.value(), what="test") == member


def test_package_member_concrete_is_true_only_with_an_architecture_suffix() -> None:
    family = PackageMember(name="mcuhome-build-tools", version="0.1.10")
    concrete = PackageMember(name="mcuhome-build-tools_linux-amd64", version="0.1.10")
    assert family.concrete is False
    assert concrete.concrete is True


def test_family_of_and_member_name() -> None:
    assert family_of("mcuhome-build-tools_linux-amd64") == "mcuhome-build-tools"
    assert family_of("mcuhome-build-tools") == "mcuhome-build-tools"
    assert member_name("mcuhome-build-tools") == "packages.mcuhome-build-tools"


# --------------------------------------------------------------------------
# parse_declaration
# --------------------------------------------------------------------------


def _declaration_document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        SPEC_GENERATION_MEMBER: "3",
        ZEPHYR_VERSION_MEMBER: "3.7.0",
        GENERATOR_CONSTRAINT_MEMBER: "mcuhome-context/3",
        "packages.mcuhome-build-workspace": "0.1.10",
        "packages.mcuhome-build-tools_linux-amd64": f"0.1.10@sha256:{HASH_A}",
    }
    document.update(overrides)
    return document


def test_parse_declaration_reads_a_complete_document() -> None:
    declaration = parse_declaration(_declaration_document())
    assert declaration.spec_generation == "3"
    assert declaration.zephyr_version == "3.7.0"
    assert declaration.generator_constraint == "mcuhome-context/3"
    assert declaration.generator_constraint_mode == MODE_STRICT
    assert set(declaration.packages) == {
        "mcuhome-build-workspace",
        "mcuhome-build-tools_linux-amd64",
    }


@pytest.mark.parametrize(
    "member",
    [SPEC_GENERATION_MEMBER, ZEPHYR_VERSION_MEMBER, GENERATOR_CONSTRAINT_MEMBER],
)
def test_parse_declaration_refuses_each_missing_required_member(member: str) -> None:
    """The refusal has to name the missing member, or a reader cannot fix its own document."""
    document = _declaration_document()
    del document[member]
    with pytest.raises(BuildError) as caught:
        parse_declaration(document)
    assert member in caught.value.message


def test_parse_declaration_default_generator_constraint_mode_is_strict() -> None:
    document = _declaration_document()
    assert GENERATOR_CONSTRAINT_MODE_MEMBER not in document
    assert parse_declaration(document).generator_constraint_mode == MODE_STRICT


def test_parse_declaration_accepts_the_chain_mode() -> None:
    document = _declaration_document(**{GENERATOR_CONSTRAINT_MODE_MEMBER: MODE_CHAIN})
    assert parse_declaration(document).generator_constraint_mode == MODE_CHAIN


def test_parse_declaration_refuses_an_unknown_generator_constraint_mode() -> None:
    document = _declaration_document(**{GENERATOR_CONSTRAINT_MODE_MEMBER: "loose"})
    with pytest.raises(BuildError) as caught:
        parse_declaration(document)
    assert "loose" in caught.value.message


def test_parse_declaration_ignores_unknown_members() -> None:
    """An added member must stay an additive change: a reader that does not know it parses on."""
    plain = _declaration_document()
    with_extra = _declaration_document(**{"an-unknown-member": "whatever"})
    assert parse_declaration(with_extra) == parse_declaration(plain)


def test_parse_declaration_refuses_a_document_with_no_packages_member() -> None:
    document = _declaration_document()
    for key in [key for key in document if key.startswith("packages.")]:
        del document[key]
    with pytest.raises(BuildError):
        parse_declaration(document)


def test_parse_declaration_refuses_a_non_object() -> None:
    with pytest.raises(BuildError):
        parse_declaration(["not", "an", "object"])


# --------------------------------------------------------------------------
# declaration_from_labels
# --------------------------------------------------------------------------


def test_declaration_from_labels_parses_the_same_document_as_labels() -> None:
    document = _declaration_document()
    labels = {f"org.mcuhome.build-environment.{key}": value for key, value in document.items()}
    labels["org.opencontainers.image.title"] = "not ours"
    assert declaration_from_labels(labels) == parse_declaration(document)


def test_declaration_from_labels_ignores_labels_outside_the_prefix() -> None:
    document = _declaration_document()
    labels = {f"org.mcuhome.build-environment.{key}": value for key, value in document.items()}
    labels["com.example.unrelated"] = "packages.should-not-appear"
    declaration = declaration_from_labels(labels)
    assert "should-not-appear" not in declaration.packages


# --------------------------------------------------------------------------
# parse_meta / PackageMeta
# --------------------------------------------------------------------------


def _meta(**changed: object) -> dict:
    """A valid meta file, with anything the caller wants broken."""
    document = {
        "schema": 1,
        "package": {
            "name": "mcuhome-build-workspace",
            "version": "0.1.0",
            "architecture": None,
        },
        "requires": {"mcuhome-build-tools": "~=0.1.0"},
        "inputs_sha256": HASH_A,
        "contents": {"patches": []},
    }
    document.update(changed)
    return document


def test_parse_meta_reads_what_a_package_is_and_what_it_requires() -> None:
    meta = parse_meta(_meta())
    assert meta.name == "mcuhome-build-workspace"
    assert meta.version == "0.1.0"
    assert meta.architecture is None
    assert meta.package == "mcuhome-build-workspace"
    assert meta.constraint_on("mcuhome-build-tools") == "~=0.1.0"
    assert meta.inputs_sha256 == HASH_A
    assert meta.contents == {"patches": []}


def test_a_per_platform_package_names_its_family_and_its_platform_apart() -> None:
    """The concrete name is the two of them joined, so it is derived, not stated."""
    meta = parse_meta(
        _meta(
            package={
                "name": "mcuhome-build-tools",
                "version": "0.1.0",
                "architecture": "linux-amd64",
            }
        )
    )
    assert meta.package == "mcuhome-build-tools_linux-amd64"
    assert family_of(meta.package) == meta.name


def test_a_package_at_the_end_of_the_chain_requires_nothing() -> None:
    """Absent, not empty — and asking it for a constraint is a typed refusal."""
    document = _meta()
    del document["requires"]
    meta = parse_meta(document)
    assert meta.requires == {}
    with pytest.raises(BuildError) as caught:
        meta.constraint_on("mcuhome-build-tools")
    assert "mcuhome-build-tools" in caught.value.message
    assert "none" in caught.value.hint


def test_a_constraint_the_package_does_not_state_hints_what_it_does() -> None:
    meta = parse_meta(_meta())
    with pytest.raises(BuildError) as caught:
        meta.constraint_on("mcuhome-build-tools_linux-amd64")
    assert "mcuhome-build-tools" in caught.value.hint


def test_a_schema_this_version_does_not_read_is_refused_rather_than_guessed() -> None:
    """A reader that guessed at an unknown shape would resolve a chain wrongly."""
    with pytest.raises(BuildError) as caught:
        parse_meta(_meta(schema=2))
    assert "2" in caught.value.message


@pytest.mark.parametrize(
    ("broken", "expected"),
    [
        ({"package": "mcuhome-build-workspace"}, "which package"),
        (
            {"package": {"name": "Not A Name", "version": "0.1.0", "architecture": None}},
            "Not A Name",
        ),
        ({"package": {"name": "mcuhome-sdk", "version": "", "architecture": None}}, "version"),
        (
            {"package": {"name": "mcuhome-sdk", "version": "0.1.0", "architecture": "Linux AMD64"}},
            "architecture",
        ),
        ({"requires": {"mcuhome-build-tools": 1}}, "requires"),
        ({"requires": {"mcuhome-build-tools": ""}}, "requires"),
        ({"inputs_sha256": "not-a-hash"}, "input hash"),
        ({"inputs_sha256": HASH_A.upper()}, "input hash"),
        ({"contents": []}, "contents"),
    ],
)
def test_a_meta_file_that_cannot_be_read_says_which_part(broken, expected) -> None:
    with pytest.raises(BuildError) as caught:
        parse_meta(_meta(**broken))
    assert expected in caught.value.message


def test_a_meta_file_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(BuildError):
        parse_meta(["mcuhome-sdk"])


# --------------------------------------------------------------------------
# Declaration.described
# --------------------------------------------------------------------------


def test_described_sorts_by_member_name_ascending() -> None:
    """§5.1 requires ascending byte order; a package set is not read in insertion order."""
    declaration = Declaration(
        spec_generation="3",
        zephyr_version="3.7.0",
        generator_constraint="mcuhome-context/3",
        packages={
            "z-package": PackageMember(name="z-package", version="1.0"),
            "a-package": PackageMember(name="a-package", version="2.0"),
            "m-package": PackageMember(name="m-package", version="3.0"),
        },
    )
    assert declaration.described() == "a-package 2.0, m-package 3.0, z-package 1.0"
