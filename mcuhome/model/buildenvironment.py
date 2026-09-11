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

``meta.json`` is what a **package** says about itself — at the top of the
archive and, byte for byte the same document, beside it as
``<archive>.meta.json``. Every MCUHome package carries one: the SDK, the
build workspace, the build tools. It answers three questions the
declaration deliberately does not:

* **What is this?** ``package``: the name it is published under, its
  version, and the platform its bytes are for (``null`` where they are
  for all of them).
* **What does it need below it?** ``requires``: a map from package name
  to a PEP 440 specifier. The SDK requires a range of build workspaces,
  a build workspace requires a range of build tools, and the tools
  require nothing — a chain, one link per stage, and the member is
  **absent** where a package ends the chain.
* **What went into it, and what came out?** ``inputs_sha256`` identifies
  the repository inputs it was built from; ``contents`` states what those
  resolved to — the workspace's project revisions and patches, the
  tools' tool versions.

A constraint and not a version, because the three are released on lines of
their own: whoever resolves a chain takes the newest published version
satisfying each constraint and pins that one exactly, by name, version and
hash. The hashes are never here — a package cannot state its own, and the
one below it may not be built yet — they come from the package host's
signed index, where a version resolves to bytes.

**No PEP 440 here.** This package has no dependencies by construction, so
a version and a specifier are checked as spellings and never parsed;
comparing one against the other is the job of whoever holds ``packaging``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mcuhome.model.errors import BuildError

__all__ = [
    "ARCH_SEPARATOR",
    "DECLARATION_FILE",
    "DEFAULT_BUILD_TOOLS",
    "DEFAULT_BUILD_WORKSPACE",
    "ENVIRONMENT_IMAGE_REPOSITORY",
    "GENERATOR_CONSTRAINT_MEMBER",
    "GENERATOR_CONSTRAINT_MODE_MEMBER",
    "LABEL_NAMESPACE",
    "LABEL_PREFIX",
    "META_FILE",
    "META_SCHEMA",
    "META_SUFFIX",
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
    "PackageMember",
    "PackageMeta",
    "declaration_from_labels",
    "family_of",
    "member_name",
    "parse_declaration",
    "parse_member",
    "parse_meta",
]

#: The environment's self-description, at the top of the package that
#: carries it and, byte for byte the same document, beside the archive.
DECLARATION_FILE = "build-environment.json"

#: What a package says about itself: at the top of the archive, and the
#: same bytes beside it as ``<archive file name>.meta.json``. Named for
#: the file rather than for the package, like the ``.sha256`` sidecar, so
#: a directory holding two versions keeps two of them.
META_FILE = "meta.json"
META_SUFFIX = f".{META_FILE}"

#: The schema this module implements. A meta file that states another
#: number is refused rather than read: a reader that guessed at a shape it
#: does not know would resolve a chain from a document it misunderstood.
META_SCHEMA = 1

#: Specification §5's members. Written out rather than derived, because a
#: reader in another language reads these strings and not this module.
SPEC_GENERATION_MEMBER = "spec-generation"
ZEPHYR_VERSION_MEMBER = "zephyr.version"
GENERATOR_CONSTRAINT_MEMBER = "build-context.generator-constraint"
GENERATOR_CONSTRAINT_MODE_MEMBER = "build-context.generator-constraint-mode"

#: §5.1's member prefix: one member per package, never a list packed into
#: one value, so a package can be named and looked up on its own.
PACKAGE_MEMBER_PREFIX = "packages."

#: The namespace every label a build environment declares itself with sits
#: in. It names the *thing* — a build environment — rather than the project,
#: because a third party publishes one of these too, under this scheme, and
#: an orchestrator reads it the same way.
LABEL_NAMESPACE = "org.mcuhome.build-environment"

#: §5.2's label prefix — an image repeats every member under it, with the
#: identical value. Derived from the namespace above rather than spelled a
#: second time, so the two can never drift apart.
LABEL_PREFIX = f"{LABEL_NAMESPACE}."

#: The repository MCUHome publishes its environment images under. An
#: image's identity is never its tag: it is matched by the ``packages.``
#: labels it declares, and a repository only says where copies of it are.
#: This is the default an orchestrator searches when nothing says otherwise
#: — a fact both sides of the boundary need, which is why it is stated in
#: the same module as the rest of the environment's vocabulary.
ENVIRONMENT_IMAGE_REPOSITORY = "ghcr.io/mcu-home/build-environment"

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
class PackageMeta:
    """One package's ``meta.json``, as data.

    *architecture* is ``None`` where the package is architecture-neutral,
    and *requires* is empty where the package ends the chain — the tools
    require nothing below them, and their meta file states no ``requires``
    member at all.
    """

    name: str
    version: str
    architecture: str | None
    requires: Mapping[str, str]
    inputs_sha256: str
    contents: Mapping[str, Any]
    schema: int = META_SCHEMA

    @property
    def package(self) -> str:
        """The concrete package name: the family plus its platform, if any."""
        if self.architecture is None:
            return self.name
        return f"{self.name}{ARCH_SEPARATOR}{self.architecture}"

    def constraint_on(self, package: str) -> str:
        """What this package requires of *package*, or a refusal.

        The refusal is the interesting half: a package that names no
        constraint on the stage below it cannot have one guessed for it —
        "any version" and "this one forgot to say" look identical from
        here and mean entirely different things.
        """
        constraint = self.requires.get(package)
        if constraint is None:
            named = ", ".join(sorted(self.requires)) or "none"
            raise BuildError(
                f'{self.package} {self.version} states no requirement on "{package}".',
                hint=(
                    f"its {META_FILE} requires: {named}. Name the package in the "
                    "device's sources, or use a release that states which versions "
                    "it was built and tested with."
                ),
            )
        return constraint


def parse_meta(document: Any, *, what: str = f"A package's {META_FILE}") -> PackageMeta:
    """A ``meta.json``, checked and typed, or a refusal in plain language."""
    if not isinstance(document, dict):
        raise BuildError(
            f"{what} is not a JSON object.",
            hint="a package's meta file is one object describing that package",
        )
    schema = document.get("schema")
    if schema != META_SCHEMA:
        raise BuildError(
            f"{what} states schema {schema!r}, and this MCUHome reads {META_SCHEMA}.",
            hint="update MCUHome, or use a package this version can read",
        )
    package = document.get("package")
    if not isinstance(package, dict):
        raise BuildError(
            f"{what} does not say which package it describes.",
            hint="the package member states the name, the version and the architecture",
        )
    name = package.get("name")
    version = package.get("version")
    architecture = package.get("architecture")
    if _PACKAGE_NAME.fullmatch(name or "") is None:
        raise BuildError(
            f'{what} names the package "{name}", which is not a package name.',
            hint="lowercase letters, digits and -, with the platform stated separately",
        )
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise BuildError(
            f'{what} states the version "{version}", which is not a version.',
            hint="a version is PEP 440, as in 0.1.0 or 0.1.10.dev3",
        )
    if architecture is not None and (
        not isinstance(architecture, str) or _PACKAGE_NAME.fullmatch(architecture) is None
    ):
        raise BuildError(
            f'{what} states the architecture "{architecture}".',
            hint="an architecture is <os>-<arch>, as in linux-amd64, or null for none",
        )
    requires = document.get("requires", {})
    if not isinstance(requires, dict) or not all(
        isinstance(key, str) and isinstance(value, str) and value for key, value in requires.items()
    ):
        raise BuildError(
            f"{what} states requires as something other than constraints.",
            hint="requires maps a package name to a PEP 440 specifier, as in ~=0.1.0",
        )
    inputs = document.get("inputs_sha256")
    if not isinstance(inputs, str) or _SHA256_HEX.fullmatch(inputs) is None:
        raise BuildError(
            f'{what} states the input hash "{inputs}".',
            hint="the input hash is 64 lowercase hex digits",
        )
    contents = document.get("contents", {})
    if not isinstance(contents, dict):
        raise BuildError(
            f"{what} states contents as something other than an object.",
            hint="contents says what the package resolved to, as an object",
        )
    return PackageMeta(
        name=name,
        version=version,
        architecture=architecture,
        requires=dict(requires),
        inputs_sha256=inputs,
        contents=contents,
        schema=META_SCHEMA,
    )
