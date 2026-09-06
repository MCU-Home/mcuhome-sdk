# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""What a build environment says about itself, and what an SDK asks for.

Two documents, one vocabulary, so they live in one module.

``build-environment.json`` is the **self-description** every build
environment carries, defined by
``docs/spec/build-environment-specification.md`` §5: one flat JSON object
whose members are strings — the specification generation the environment
implements, the Zephyr version it builds against, which build contexts it
accepts, and one member per package of its set. A container image repeats
every member as an OCI label under ``org.mcuhome.build-environment.``
(§5.2), so the same parse serves an unpacked store entry and an image.

``build-environment.lock.json`` is what an **SDK release** states about
the environment it was built and tested with. It is the same document
minus everything an SDK cannot know: an SDK is not an environment, so it
declares no generation, no Zephyr version and no context constraint — it
names packages and their versions, and nothing else. That is exactly
§5.1's *abstract* package set, the form a package's own metadata uses
when hashes are not knowable yet, and the reason it fits here without a
single field name of its own:

* the architecture-neutral workspace package is stated at the SDK's own
  version, because it is built from the SDK's tag;
* the per-platform tools package is named by its **family**, at version
  level only, because its bytes differ per platform on purpose;
* neither carries a hash, because at SDK-archive time nobody has built
  those archives yet. The hashes come from the package host's signed
  index, where a version resolves to bytes.

Whoever reads the lock therefore learns *which versions*, and resolves
the rest the way every other pin is resolved.

**No PEP 440 here.** This package has no dependencies by construction, so
a version is checked as a spelling and never parsed; comparing one
against a constraint is the job of whoever holds ``packaging``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcuhome.model.buildimage import LABEL_PREFIX as _LABEL_NAMESPACE
from mcuhome.model.errors import BuildError

__all__ = [
    "ARCH_SEPARATOR",
    "DECLARATION_FILE",
    "DEFAULT_BUILD_TOOLS",
    "DEFAULT_BUILD_WORKSPACE",
    "GENERATOR_CONSTRAINT_MEMBER",
    "GENERATOR_CONSTRAINT_MODE_MEMBER",
    "LABEL_PREFIX",
    "LOCK_FILE",
    "MODE_CHAIN",
    "MODE_STRICT",
    "PACKAGE_MEMBER_PREFIX",
    "SPEC_GENERATION",
    "SPEC_GENERATION_MEMBER",
    "TOOLS_FAMILY",
    "TOOLS_SOURCE",
    "WORKSPACE_PACKAGE",
    "WORKSPACE_SOURCE",
    "ZEPHYR_VERSION_MEMBER",
    "Declaration",
    "EnvironmentLock",
    "PackageMember",
    "declaration_from_labels",
    "family_of",
    "member_name",
    "parse_declaration",
    "parse_lock",
    "parse_member",
]

#: The environment's self-description, at the top of the package that
#: carries it and, byte for byte the same document, beside the archive.
DECLARATION_FILE = "build-environment.json"

#: What an SDK release states about the build environment it was built
#: and tested with. One file per release, next to the SDK package and
#: inside it.
LOCK_FILE = "build-environment.lock.json"

#: Specification §5's members. Written out rather than derived, because a
#: reader in another language reads these strings and not this module.
SPEC_GENERATION_MEMBER = "spec-generation"
ZEPHYR_VERSION_MEMBER = "zephyr.version"
GENERATOR_CONSTRAINT_MEMBER = "build-context.generator-constraint"
GENERATOR_CONSTRAINT_MODE_MEMBER = "build-context.generator-constraint-mode"

#: §5.1's member prefix: one member per package, never a list packed into
#: one value, so a package can be named and looked up on its own.
PACKAGE_MEMBER_PREFIX = "packages."

#: §5.2's label prefix — an image repeats every member under it, with the
#: identical value. Derived from the namespace
#: :mod:`mcuhome.model.buildimage` already states rather than spelled a
#: second time, so the two can never drift apart.
LABEL_PREFIX = f"{_LABEL_NAMESPACE}."

#: How the generator chain is read (§9.1). ``strict`` trusts only the
#: leftmost entry; ``chain`` walks the chain and accepts at the first
#: entry that matches.
MODE_STRICT = "strict"
MODE_CHAIN = "chain"

#: The specification generation this project implements. An orchestrator
#: does not start an environment whose generation it does not implement
#: (§12), and this is the number it compares against.
SPEC_GENERATION = "3"

#: MCUHome's own package set, by name. The architecture-neutral carrier
#: and the per-platform family; a concrete tools package appends
#: ``_<os>-<arch>`` to the family name.
WORKSPACE_PACKAGE = "mcuhome-build-workspace"
TOOLS_FAMILY = "mcuhome-build-tools"

#: The sources the two packages are published under inside a registry.
#: Same strings the store uses as its kinds, because they are the same
#: names: a source, a bound and a finalization are all keyed by what the
#: package is.
WORKSPACE_SOURCE = "build-workspace"
TOOLS_SOURCE = "build-tools"

#: What a device that says nothing points its environment packages at:
#: MCUHome's own, from the official host, at **no version** — which means
#: "the version the resolved SDK release was built and tested with". A
#: version here would freeze every device created while it was current,
#: which is exactly what these references exist not to do.
DEFAULT_BUILD_WORKSPACE = f"{WORKSPACE_SOURCE}/{WORKSPACE_PACKAGE}"
DEFAULT_BUILD_TOOLS = f"{TOOLS_SOURCE}/{TOOLS_FAMILY}"

#: §5.1's package-name grammar: lowercase alphanumerics and ``-``,
#: optionally one architecture suffix after the first ``_``.
_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*(?:_[a-z0-9][a-z0-9-]*)?\Z")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!_-]*\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")

#: The one place a package name is split into family and platform.
ARCH_SEPARATOR = "_"


def member_name(package: str) -> str:
    """The declaration member a package is named under."""
    return f"{PACKAGE_MEMBER_PREFIX}{package}"


def family_of(package: str) -> str:
    """The family half of a package name — the whole name when it has none."""
    return package.split(ARCH_SEPARATOR, 1)[0]


@dataclass(frozen=True)
class PackageMember:
    """One ``packages.<name>`` member: which package, and which bytes.

    ``sha256`` is ``None`` where the declaring side could not know it —
    a package's own metadata cannot state its own hash, and a family
    entry stands for one archive per platform. A *delivery* states it;
    §5.1 requires that of an image and of every other assembly of exact
    bytes.
    """

    name: str
    version: str
    sha256: str | None = None

    @property
    def concrete(self) -> bool:
        """Whether the name is one platform's package rather than a family."""
        return ARCH_SEPARATOR in self.name

    def value(self) -> str:
        """The member value, in the one spelling §5.1 defines."""
        if self.sha256 is None:
            return self.version
        return f"{self.version}@sha256:{self.sha256}"


def parse_member(name: str, value: object, *, what: str) -> PackageMember:
    """One package member, checked as the spelling §5.1 fixes.

    *name* is the package name — the member name with the prefix already
    removed — and *value* is ``<version>`` or
    ``<version>@sha256:<64 lowercase hex digits>``.
    """
    if _PACKAGE_NAME.fullmatch(name) is None:
        raise BuildError(
            f'{what} names a package "{name}" that is not a package name.',
            hint=(
                "lowercase letters, digits and -, optionally with an architecture "
                "suffix after a single _ — like mcuhome-build-tools_linux-amd64"
            ),
        )
    if not isinstance(value, str) or not value:
        raise BuildError(
            f'{what} states no version for "{name}".',
            hint="a package member is <version> or <version>@sha256:<64 hex digits>",
        )
    version, separator, digest = value.partition("@")
    if _VERSION.fullmatch(version) is None:
        raise BuildError(
            f'{what} states "{value}" for "{name}", which is not a version.',
            hint="a package member is <version> or <version>@sha256:<64 hex digits>",
        )
    if not separator:
        return PackageMember(name=name, version=version)
    if not digest.startswith("sha256:") or _SHA256_HEX.fullmatch(digest[7:]) is None:
        raise BuildError(
            f'{what} states "{value}" for "{name}", whose hash is not a sha256 digest.',
            hint='the canonical form is "sha256:" followed by 64 lowercase hex digits',
        )
    return PackageMember(name=name, version=version, sha256=digest[7:])


def _members(document: Any, *, what: str) -> dict[str, PackageMember]:
    """Every ``packages.<name>`` member of *document*, parsed."""
    if not isinstance(document, dict):
        raise BuildError(
            f"{what} is not a JSON object.",
            hint="the declaration is one flat object whose members are all strings",
        )
    found: dict[str, PackageMember] = {}
    for key, value in document.items():
        if not isinstance(key, str) or not key.startswith(PACKAGE_MEMBER_PREFIX):
            continue
        package = key[len(PACKAGE_MEMBER_PREFIX) :]
        found[package] = parse_member(package, value, what=what)
    if not found:
        raise BuildError(
            f"{what} names no packages.",
            hint=(
                "a build environment is a package set, so its declaration carries at "
                "least one packages.<name> member"
            ),
        )
    return found


@dataclass(frozen=True)
class Declaration:
    """A build environment's §5 self-description, as data.

    The three required members plus the package set. Unknown members are
    dropped rather than kept: this type is what a reader acts on, and
    ignoring what it does not know is what makes an added member an
    additive change.
    """

    spec_generation: str
    zephyr_version: str
    generator_constraint: str
    packages: Mapping[str, PackageMember]
    generator_constraint_mode: str = MODE_STRICT

    def described(self) -> str:
        """The package set as one line, sorted by member name as §5.1 asks."""
        return ", ".join(
            f"{name} {member.value()}" for name, member in sorted(self.packages.items())
        )


def parse_declaration(document: Any, *, what: str = "The environment declaration") -> Declaration:
    """A ``build-environment.json`` (or its label mirror), checked and typed.

    Refuses in plain language rather than raising a parser's error: the
    answer decides whether a build environment is started at all, and a
    declaration that cannot be read is a refusal before anything runs.
    """
    if not isinstance(document, dict):
        raise BuildError(
            f"{what} is not a JSON object.",
            hint="the declaration is one flat object whose members are all strings",
        )
    required = (SPEC_GENERATION_MEMBER, ZEPHYR_VERSION_MEMBER, GENERATOR_CONSTRAINT_MEMBER)
    missing = [member for member in required if not isinstance(document.get(member), str)]
    if missing:
        raise BuildError(
            f"{what} is missing {', '.join(missing)}.",
            hint=(
                "a build environment declares the specification generation it "
                "implements, the Zephyr version it builds against and which build "
                "contexts it accepts"
            ),
        )
    mode = document.get(GENERATOR_CONSTRAINT_MODE_MEMBER, MODE_STRICT)
    if mode not in (MODE_STRICT, MODE_CHAIN):
        raise BuildError(
            f'{what} states the generator-constraint mode "{mode}".',
            hint=f"the two modes are {MODE_STRICT} and {MODE_CHAIN}",
        )
    return Declaration(
        spec_generation=document[SPEC_GENERATION_MEMBER],
        zephyr_version=document[ZEPHYR_VERSION_MEMBER],
        generator_constraint=document[GENERATOR_CONSTRAINT_MEMBER],
        generator_constraint_mode=mode,
        packages=_members(document, what=what),
    )


def declaration_from_labels(
    labels: Mapping[str, str], *, what: str = "The image declaration"
) -> Declaration:
    """The §5.2 mirror: an image's labels read as the declaration they repeat.

    Every member appears as ``org.mcuhome.build-environment.<member>``
    with the identical value, so the mirror is a prefix filter and the
    same parse. Labels outside the prefix are the image's own business
    and are not looked at.
    """
    return parse_declaration(
        {
            key[len(LABEL_PREFIX) :]: value
            for key, value in labels.items()
            if key.startswith(LABEL_PREFIX)
        },
        what=what,
    )


@dataclass(frozen=True)
class EnvironmentLock:
    """An SDK release's ``build-environment.lock.json``, as data.

    Package members and nothing else — see the module docstring for why
    an SDK declares no generation, no Zephyr version and no hashes.
    """

    packages: Mapping[str, PackageMember]

    def version_of(self, package: str) -> str:
        """The version this release names for *package*, or a refusal."""
        member = self.packages.get(package)
        if member is None:
            named = ", ".join(sorted(self.packages)) or "none"
            raise BuildError(
                f'This SDK release names no version of "{package}".',
                hint=(
                    f"its {LOCK_FILE} states: {named}. The SDK and its build "
                    "environment are released together — a release that does not "
                    "name the package cannot be built with it."
                ),
            )
        return member.version


def parse_lock(document: Any, *, what: str = f"The SDK's {LOCK_FILE}") -> EnvironmentLock:
    """A ``build-environment.lock.json``, checked and typed."""
    return EnvironmentLock(packages=_members(document, what=what))
