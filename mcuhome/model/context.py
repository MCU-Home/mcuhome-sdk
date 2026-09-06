# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context — the self-contained input artifact of a remote build.

A build context is a plain directory: ``build-context.json``,
``manifest.yaml``, the canonical device model under ``model/``,
optionally patches under ``patches/<layer>/``. It contains everything a build needs except the
build environment (the packages carrying the toolchain and the source
world) and the SDK (fetched as a hash-pinned package). It travels as an
archive, but the archive is transport: the directory is the artifact.

**The client pins the build environment, and the pin is part of the
identity.** A context names the environment **by its packages** — the
architecture-neutral workspace package and the tools package, each as a
``(name, version, sha256)`` triple — and nobody downstream chooses
anything: whoever runs the context finds an environment whose own
package declaration states exactly those packages, or refuses. That is
what makes a context a complete statement of a build — the same context
yields the same firmware on any machine that can obtain the packages,
which a context naming only a *requirement* could not promise, because
two backends could answer one requirement with two different package
sets.

A container image that declares those packages and a store provisioned
from them are the same environment; the image is a delivery of the set
and never the identity. That is why the pin is a package set rather than
an image reference, which is what format version 3 carried.

**The context ID is normative — fixed with ``context`` format version 4,
and it can never change afterwards.** Everything that ever names a
context — integrity verification, artifact attribution ("built from
*this*"), archival references — depends on computing the same ID from
the same inputs forever. The rule is stated in
``docs/spec/build-context-format.md`` §6 and implemented here; both
parties to a build compute it independently from the bytes they hold.
The ID is the SHA-256 over the canonical JSON (RFC 8785) of exactly this
document::

    {"build_environment": {"tools":     {"name": ..., "sha256": ..., "version": ...},
                           "workspace": {"name": ..., "sha256": ..., "version": ...}},
     "files": [{"path": ..., "sha256": ...}, ...],
     "sdk": {"sha256": ...},
     "target": {"board": ...}}

— the manifest's build-relevant fields under the format's fixed names
(``sdk.sha256`` carries the manifest's ``mcuhome.package.sha256``, and
both environment entries contribute the full triple).
``files`` is sorted by ``path`` in
ascending byte order of its UTF-8 encoding — which UTF-8 makes equal
to code-point order, so a plain string sort implements it — and every
listed file contributes its own content hash; the sort only makes the
encoding deterministic.

Both environment entries are hashed the same way, and the ``name`` is
inside the ID because a tools pin's name decides what its hash *means*:
the family name says "resolve this per platform", a name carrying an
architecture suffix says "these exact bytes, on this platform only". Two
different builds would otherwise share one identity.

Explicitly excluded, so they can never influence the ID: ``created``
(informational), ``mcuhome.constraint`` (the intent, not the
resolution), ``mcuhome.version`` and every ``url`` — names for and
locations of bytes the hashes already pin.

The Zephyr *line* is not in the document at all. A pinned environment
answers it: the workspace package states which Zephyr it carries, the
resolution checked that statement against the model's constraint before
pinning, and the model itself is an ordinary hashed entry of ``files``.
A separate copy would be a third place for the same fact to be wrong in.

``manifest.yaml`` itself and the backend-written ``.mcuhome/`` runtime
directory are never integrity entries, so they cannot influence the ID
either. ``build-context.json`` is one, deliberately: it names the tool
that wrote the context, a build environment declares which tools it
accepts, and a file that decides who may run a build has to be covered
by the identity that build is claimed under. Neither the YAML file bytes
nor the transport archive bytes are ever hashed — neither serialization
is deterministic. New
build-relevant fields enter the hash only together with a bump of the
``context`` format version.

Patches carry no manifest section of their own. A patch is a file: its
target layer is its subfolder, its application order is its ``NNNN-``
filename prefix, and its integrity entry sits in ``files`` like every
other file's. There is deliberately no declared patch list that could
disagree with the files actually present, so a build server re-derives
its patch policy from the paths alone.

**This module is the format and the rule; it never touches a
filesystem.** Creating a context directory, hashing what is in one and
:func:`~mcuhome.workbench.contextdir.verify_context` — the server-side integrity
primitive that recomputes every file hash and the ID from the bytes
actually present — are :mod:`mcuhome.workbench.contextdir`. The cut is
deliberate: a build server recomputes a context ID from bytes it
received off a socket and must carry no build logic to do it, so
:func:`context_id` takes values and not a directory, and this module
imports nothing but the standard library and
:mod:`mcuhome.model.errors`. PEP 440 is not enforced here for the same
reason — parsing a version needs ``packaging``, and this package has no
dependencies by construction.

:data:`CONTEXT_ID_VECTORS` is the conformance suite that keeps a second
implementation honest — the frozen rule stated as inputs and outputs
rather than as code.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mcuhome.model.errors import BuildError

__all__ = [
    "BACKEND_DIR",
    "BUILD_CONTEXT_FILE",
    "CONTEXT_FILE",
    "KEYS_DIR",
    "SIGNING_KEY_FILE",
    "CONTEXT_ID_VECTORS",
    "CONTEXT_VERSION",
    "MANIFEST_FILE",
    "MODEL_FILE",
    "PATCHES_DIR",
    "ContextFile",
    "ContextManifest",
    "ContextRequest",
    "EnvironmentPin",
    "GeneratorEntry",
    "PackagePin",
    "SdkPin",
    "canonical_json",
    "context_id",
    "format_generator_chain",
    "parse_generator_chain",
    "validate_environment",
    "validate_manifest",
    "vector_id",
]

#: Format version of the context manifest. The normative hashing rule is
#: locked to it: a field can join the hashed document only together with
#: a bump here, and version 4's rule never changes.
#:
#: Earlier versions are gone rather than supported alongside this one,
#: for the reason version 1 was dropped when 2 arrived: nothing is
#: published, no context written to an older format exists outside a
#: test, and a reader that accepted several would have to carry a hashing
#: rule per format forever to serve exactly zero documents. Version 1
#: pinned a container image and hashed it; version 2 stated a Zephyr line
#: and let the backend choose; version 3 pinned an image again and hashed
#: its digest; this one pins the environment's **packages**, which is
#: what an environment actually is — an image and a provisioned store are
#: two deliveries of one package set, and only the set identifies the
#: build.
CONTEXT_VERSION = 4

#: The one file a builder must parse first, at the top of the context.
MANIFEST_FILE = "manifest.yaml"

#: The request document with the pins, next to the manifest. It is
#: excluded from the integrity list **as a statement about the hash, not
#: about layout**: its never-hashed fields (constraint, url, created)
#: would otherwise leak into an identity that is computed from resolved
#: values alone.
CONTEXT_FILE = "context.yaml"

#: The generator declaration, at the top of every context. The build
#: environment specification requires it by this name and reads exactly
#: one key out of it — see :func:`parse_generator_chain`. Everything else
#: in a context belongs to the generator's own format; this one file is
#: the boundary, which is why the name is fixed here and not in the
#: workbench that writes it.
BUILD_CONTEXT_FILE = "build-context.json"

#: Where the MCUboot verification key lives inside a context. Required
#: for the ``build`` action: the bootloader compiles it in as the
#: verification key.
KEYS_DIR = "keys"
SIGNING_KEY_FILE = "keys/signing.pub"

#: Where the canonical device model lives inside the context — the
#: existing wire format (:mod:`mcuhome.model.model`), unchanged.
MODEL_FILE = "model/device-model.json"

#: Where patches live: ``patches/<layer>/NNNN-name.patch``.
PATCHES_DIR = "patches"

#: The backend-written runtime directory that could once appear inside a
#: mounted context (``.mcuhome/command.json``), from an earlier design of
#: this project's own build-container contract. Plumbing, not content:
#: never an integrity entry, never identity.
#:
#: **That design has moved on and this constant has not yet.** Under the
#: v3 specification set (``docs/spec/build-environment-specification.md``
#: §4, §6.1) the per-invocation request document lives outside the build
#: context entirely, at ``mcuhome/invocation-request.json``, sibling to
#: the context directory rather than inside it — so there is no
#: ``.mcuhome/`` in a context at all, exactly as this constant already
#: assumes. Removing the directory from the exclusion rules is migration
#: work; keeping it excluded in the meantime is harmless, because a
#: context that never contains one cannot be affected by the exclusion.
BACKEND_DIR = ".mcuhome"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PRODUCT = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
#: A package name: lowercase alphanumerics and ``-``, optionally one
#: architecture suffix after the first ``_``.
_PACKAGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*(?:_[a-z0-9][a-z0-9-]*)?\Z")
#: The characters a PEP 440 version is spelled with. Not a parse — this
#: package has no ``packaging`` — but enough that a hashed identity input
#: cannot be an empty string or a block of YAML.
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+!_-]*\Z")


# --------------------------------------------------------------------------
# The generator declaration
# --------------------------------------------------------------------------
#
# The one thing about a build context that is not the generator's own
# business: which tool produced it. A build environment declares the
# generators it accepts as a version constraint, and the orchestrator
# checks that constraint before it starts anything — so both sides need
# one spelling of "who wrote this", and it is fixed here rather than in
# either of them.
#
# The chain reads right to left in time: the rightmost entry created the
# context, everything left of it modified the context afterwards, and the
# leftmost entry is whoever touched it last. Listing the entries to its
# right is a *claim* by the tool on the left that it kept the context
# compatible with them — which is why the default check believes only the
# leftmost one.
#
# PEP 440 is deliberately not enforced here. Parsing a version specifier
# needs `packaging`, this package has no dependencies by construction,
# and the party that needs the parse is the one comparing a version
# against a constraint — not the one reading a name out of a document.


@dataclass(frozen=True)
class GeneratorEntry:
    """One ``<product>:<version>`` link of a generator chain."""

    #: Lowercase, ``[a-z0-9][a-z0-9._-]*`` — a distribution name in
    #: practice, and checked as a spelling rather than looked up.
    product: str
    #: A PEP 440 version, unvalidated here (see the section note above).
    #: Never contains ``;`` or ``:``, which is what keeps the chain
    #: parseable without escaping.
    version: str

    def __str__(self) -> str:
        return f"{self.product}:{self.version}"


def format_generator_chain(entries: Iterable[GeneratorEntry]) -> str:
    """*entries* as the one string a ``generator`` value is, most recent first.

    Prepending is how a tool that modifies a context records itself::

        chain = format_generator_chain(
            (GeneratorEntry("my-tool", "1.2.0"), *parse_generator_chain(found))
        )
    """
    listed = tuple(entries)
    if not listed:
        raise BuildError(
            "A build context cannot declare an empty generator chain.",
            hint="the chain names at least the tool that created the context",
        )
    for entry in listed:
        _check_generator_entry(entry.product, entry.version)
    return ";".join(str(entry) for entry in listed)


def parse_generator_chain(value: object) -> tuple[GeneratorEntry, ...]:
    """The ``generator`` value of ``build-context.json``, split into its links.

    Strict, because the result decides which build environments may run
    this context: an entry that cannot be read is refused rather than
    skipped, since skipping the leftmost one would silently hand the
    decision to a tool that did not write the context last.
    """
    if not isinstance(value, str) or not value:
        raise BuildError(
            f"The build context declares no generator: {value!r}.",
            hint=(
                "build-context.json carries a generator — one or more "
                "<product>:<version> entries separated by semicolons, the tool "
                "that wrote the context last on the left"
            ),
        )
    entries = []
    for part in value.split(";"):
        product, separator, version = part.partition(":")
        if not separator:
            raise BuildError(
                f'"{part}" is not a generator entry.',
                hint="an entry is <product>:<version>, like mcuhome-workbench:1.2.0",
            )
        _check_generator_entry(product, version)
        entries.append(GeneratorEntry(product=product, version=version))
    return tuple(entries)


def _check_generator_entry(product: object, version: object) -> None:
    if not isinstance(product, str) or _PRODUCT.fullmatch(product) is None:
        raise BuildError(
            f"{product!r} is not a generator product name.",
            hint=(
                "lowercase letters, digits, ., - and _, starting with a letter or "
                "digit — the distribution name of the tool, like mcuhome-workbench"
            ),
        )
    if not isinstance(version, str) or not version or ":" in version:
        raise BuildError(
            f"{version!r} is not a generator version.",
            hint="a PEP 440 version, like 1.2.0 or 0.1.0.dev0",
        )


# --------------------------------------------------------------------------
# The manifest, as data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextFile:
    """One integrity entry: a context-relative path and its content hash."""

    #: Relative to the context directory, forward slashes on every
    #: platform. Part of the hashed identity, so its spelling is checked
    #: strictly (:func:`context_id` refuses anything else).
    path: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True)
class SdkPin:
    """The resolved mcuhome pin — the manifest's ``mcuhome:`` section.

    ``constraint`` is the original intent from the device configuration
    (a PEP 440 specifier such as ``~=2.3.6``); ``version`` and the
    package are what it resolved to at context creation. Only ``sha256``
    is part of the context's identity: the constraint is intent, and the
    version and URL are names for the bytes the hash already pins.
    """

    constraint: str
    version: str
    #: Where the resolved SDK package can be fetched. A hint only — a
    #: server resolves (version, sha256) against its own source list.
    url: str
    sha256: str


@dataclass(frozen=True)
class PackagePin:
    """One package of the build environment: name, version, content hash.

    The triple is the pin; ``url`` is a hint beside it and is never
    hashed, exactly as :attr:`SdkPin.url` is not.

    :attr:`name` is load-bearing and not decoration. A package published
    per architecture has a **family** name (``mcuhome-build-tools``) and
    one concrete name per platform (``mcuhome-build-tools_linux-amd64``),
    separated by the first underscore. Which of the two a pin carries
    decides what its hash means: a family's hash is derived from the
    hashes of every platform's package, so pinning the family still pins
    the exact bytes each platform gets, while a concrete name pins one
    platform's bytes and nothing else. The index of the host the package
    comes from says which kind an entry is; the context does not, because
    a context that stated it could disagree with the index.
    """

    name: str
    version: str
    sha256: str
    #: Where those bytes were found. A hint; may be empty.
    url: str = ""

    def to_dict(self, *, url: bool = True) -> dict[str, Any]:
        """The entry as a document writes it — with the hint, or without.

        The request carries the hint, the lock does not: the lock states
        what is in the context, and where the bytes once came from is not
        part of that. Both restate the same triple.
        """
        entry: dict[str, Any] = {
            "name": self.name,
            "version": self.version,
            "sha256": self.sha256,
        }
        if url:
            entry["url"] = self.url
        return entry

    @staticmethod
    def from_dict(data: Any, *, what: str) -> PackagePin:
        if not isinstance(data, dict):
            raise BuildError(
                f"The build environment's {what} entry is not a block of fields.",
                hint="each entry states name, version and sha256, and optionally url",
            )
        return PackagePin(
            # Not coerced: name, version and hash are identity, checked
            # for their one legal spelling where they are used rather
            # than reshaped on read.
            name=data["name"],
            version=data["version"],
            sha256=data["sha256"],
            url=str(data.get("url", "")),
        )

    def identity(self) -> dict[str, Any]:
        """The three hashed members, for :func:`context_id`."""
        return {"name": self.name, "sha256": self.sha256, "version": self.version}


@dataclass(frozen=True)
class EnvironmentPin:
    """Which build environment a context is compiled in — its packages.

    Two entries, because that is MCUHome's own package set: the
    architecture-neutral :attr:`workspace` carrying the source world, and
    the :attr:`tools` package carrying the toolchain. Both are the same
    kind of value and both are hashed the same way.

    The pin is **not** an image. An image that declares these packages
    and a store provisioned from them are the same environment, and the
    party that runs the context finds one of them; a reference to one
    particular delivery would make two identical builds look different.
    """

    workspace: PackagePin
    tools: PackagePin

    def to_dict(self, *, url: bool = True) -> dict[str, Any]:
        return {
            "workspace": self.workspace.to_dict(url=url),
            "tools": self.tools.to_dict(url=url),
        }

    @staticmethod
    def from_dict(data: Any) -> EnvironmentPin:
        if not isinstance(data, dict):
            raise BuildError(
                "The context names no build environment.",
                hint=(
                    "build_environment states a workspace and a tools entry, each "
                    "with name, version and sha256"
                ),
            )
        return EnvironmentPin(
            workspace=PackagePin.from_dict(data["workspace"], what="workspace"),
            tools=PackagePin.from_dict(data["tools"], what="tools"),
        )

    def without_urls(self) -> EnvironmentPin:
        """This pin with both location hints dropped.

        What the **lock** records. The manifest's document carries no
        ``url`` — where bytes were found is the request's hint and not
        part of what is in the context — so the object it is built from
        does not either, and a manifest read back off disk equals the one
        that was written.
        """
        from dataclasses import replace

        return EnvironmentPin(
            workspace=replace(self.workspace, url=""),
            tools=replace(self.tools, url=""),
        )

    def described(self) -> str:
        """The set as one line, for a log and for a build's own record."""
        return (
            f"{self.workspace.name} {self.workspace.version}, "
            f"{self.tools.name} {self.tools.version}"
        )


@dataclass(frozen=True)
class ContextManifest:
    """``manifest.yaml``, as an object."""

    sdk: SdkPin
    #: The build environment this context is compiled in, pinned by its
    #: packages. Repeated verbatim from the request: the locking party
    #: records what the client stated, it does not choose.
    build_environment: EnvironmentPin
    #: The target board — the manifest's ``target:`` section.
    board: str
    #: The integrity list: every file in the context except the manifest
    #: itself, patches included, sorted by path.
    files: tuple[ContextFile, ...]
    #: ``sha256:<hex>`` — the declared context ID. Advisory like every
    #: declared value; :func:`verify_context` recomputes it.
    id: str
    context_version: int = CONTEXT_VERSION

    def compute_id(self) -> str:
        """The ID this manifest's hashed fields yield, per the normative rule."""
        return context_id(
            sdk_sha256=self.sdk.sha256,
            environment=self.build_environment,
            board=self.board,
            files=self.files,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context_version,
            "mcuhome": {
                "constraint": self.sdk.constraint,
                "version": self.sdk.version,
                "package": {"url": self.sdk.url, "sha256": self.sdk.sha256},
            },
            # No ``url`` on either entry: the lock states what is in the
            # context, and where the bytes were found is the request's
            # hint rather than part of the record.
            "build_environment": self.build_environment.to_dict(url=False),
            "target": {"board": self.board},
            "files": [entry.to_dict() for entry in self.files],
            "id": self.id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ContextManifest:
        mcuhome = data["mcuhome"]
        package = mcuhome["package"]
        # ``created`` is deliberately not read: it dates the *request*
        # and lives in context.yaml alone — the one field that does not
        # travel. A manifest that carries one anyway is handled by the
        # unknown-field rule: ignored.
        return ContextManifest(
            context_version=int(data["context"]),
            sdk=SdkPin(
                constraint=str(mcuhome["constraint"]),
                version=str(mcuhome["version"]),
                url=str(package["url"]),
                # Hashes and paths are deliberately not coerced: they are
                # identity, and _validate_manifest type-checks them.
                sha256=package["sha256"],
            ),
            build_environment=EnvironmentPin.from_dict(data["build_environment"]),
            board=data["target"]["board"],
            files=tuple(
                ContextFile(path=item["path"], sha256=item["sha256"]) for item in data["files"]
            ),
            id=data["id"],
        )


@dataclass(frozen=True)
class ContextRequest:
    """``context.yaml``, as an object — the pinning *request*.

    The client-written half of a context: the ``lock-context`` freeze
    splits what a context states into a *request* and a *result*, and
    this is the request. It carries the ``context`` format version, the
    resolved SDK pin, the pinned build environment and the original
    intent the session is admitted on — and deliberately **nothing that
    depends on the final file set**: no ``files`` list and no ``id``.
    Those are the result the locking party computes and writes into
    :class:`ContextManifest`.

    It carries the **pinned build environment** — the client resolved it
    before writing this, out of what its device model states and what the
    resolved SDK's environment lock declares. A backend therefore reads a
    decision here, not a requirement to answer, which is what makes the
    manifest's copy of it a restatement rather than a second opinion.

    It repeats :class:`SdkPin` verbatim — the same pin
    :class:`ContextManifest` later restates, so intent and resolution
    stand side by side where a human reads back what was asked for — and
    adds ``created``. That timestamp is the one field that does not
    travel: it dates the request, is never hashed, and lives here alone.
    """

    sdk: SdkPin
    #: The build environment, pinned by its packages — what the client
    #: resolved its model's sources and its SDK's environment lock to.
    #: **Hashed**, through both triples: these are resolved values like
    #: the SDK's package hash, not an intent like the constraint beside
    #: them.
    build_environment: EnvironmentPin
    #: The target board — the request's ``target:`` section.
    board: str
    #: The instant the request was created, as an ISO 8601 UTC string
    #: (e.g. ``2026-08-10T09:00:00Z``). Informational and never hashed;
    #: carried as text so two requests differ only where their ``created``
    #: does, and so the value round-trips through YAML without a parser
    #: reinterpreting it as a native timestamp.
    created: str
    context_version: int = CONTEXT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context_version,
            "created": self.created,
            "mcuhome": {
                "constraint": self.sdk.constraint,
                "version": self.sdk.version,
                "package": {"url": self.sdk.url, "sha256": self.sdk.sha256},
            },
            "build_environment": self.build_environment.to_dict(),
            "target": {"board": self.board},
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ContextRequest:
        mcuhome = data["mcuhome"]
        package = mcuhome["package"]
        return ContextRequest(
            context_version=int(data["context"]),
            sdk=SdkPin(
                constraint=str(mcuhome["constraint"]),
                version=str(mcuhome["version"]),
                url=str(package["url"]),
                # Not coerced: the sha256 is identity, spelling-checked
                # where it is used, not silently normalized on read.
                sha256=package["sha256"],
            ),
            build_environment=EnvironmentPin.from_dict(data["build_environment"]),
            board=data["target"]["board"],
            created=str(data["created"]),
        )


# --------------------------------------------------------------------------
# The normative hash
# --------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """*value* as RFC 8785 canonical JSON — for this module's value domain.

    The hashed document contains only objects with fixed ASCII key
    names, arrays, and string values. For that domain Python's own
    serializer *is* the JSON Canonicalization Scheme: keys sorted
    (code-point order, which equals JCS's UTF-16 order for ASCII keys),
    minimal separators, strings escaped the ECMAScript way — the
    two-character escapes, lowercase ``\\u00xx`` for the remaining
    control characters, everything else emitted literally — and the
    UTF-8 encoding done by the caller. Deliberately this small instead
    of a full JCS implementation: JCS's only hard part is number
    serialization, and numbers never occur in the document.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_sha256(value: object, *, what: str) -> None:
    if not isinstance(value, str) or _SHA256_HEX.fullmatch(value) is None:
        raise BuildError(
            f"{what} is not a SHA-256 hash: {value!r}.",
            hint=(
                "the canonical spelling is exactly 64 lowercase hex digits — one "
                "spelling per hash, so two manifests can never name the same bytes "
                "differently"
            ),
        )


def _require_digest(value: object, *, what: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise BuildError(
            f"{what} is not a sha256 digest: {value!r}.",
            hint='the canonical form is "sha256:" followed by 64 lowercase hex digits',
        )


def _require_package_name(value: object, *, what: str) -> None:
    """A package name, in the one spelling the package format allows.

    Lowercase alphanumerics and ``-``, optionally followed by an
    architecture suffix introduced by ``_``. Checked strictly because the
    name is a hashed identity input: a family name and a concrete name
    mean different things, and a spelling that could be read two ways
    would make one identity cover two builds.
    """
    if not isinstance(value, str) or _PACKAGE_NAME.fullmatch(value) is None:
        raise BuildError(
            f"{what} is not a package name: {value!r}.",
            hint=(
                "lowercase letters, digits and -, optionally with an architecture "
                "suffix after a single _ — like mcuhome-build-tools_linux-amd64"
            ),
        )


def _require_version(value: object, *, what: str) -> None:
    """A version, checked as a spelling rather than parsed.

    The format says PEP 440 and this package has no ``packaging`` to say
    it with, so what is checked here is that the value is a non-empty
    string of the characters a PEP 440 version is made of. Whoever
    resolves a constraint against it does the real parse.
    """
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise BuildError(
            f"{what} is not a version: {value!r}.",
            hint="versions are PEP 440 — for example 0.1.10, 2.4.0 or 0.1.10.dev1",
        )


def _require_package(pin: PackagePin, *, what: str) -> None:
    """One environment entry, all three hashed members of it."""
    _require_package_name(pin.name, what=f"The build environment's {what} package")
    _require_version(pin.version, what=f"The build environment's {what} version")
    _require_sha256(pin.sha256, what=f"The build environment's {what} hash")


def validate_environment(environment: EnvironmentPin) -> None:
    """Both environment entries, spelled the one way the format allows.

    Public because the *request* document carries the same pin as the
    lock and has no :func:`validate_manifest` to be checked by: a reader
    of ``context.yaml`` has to be exactly as strict about the fields an
    identity is computed over as a reader of ``manifest.yaml``.
    """
    _require_package(environment.workspace, what="workspace")
    _require_package(environment.tools, what="tools")


def _require_board(value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise BuildError(
            "The context names no target board.",
            hint=(
                "the board is a hashed identity field — a context that does not say "
                "what it builds for has no identity"
            ),
        )


def _require_path(value: object) -> None:
    usable = (
        isinstance(value, str)
        and value
        and "\\" not in value
        and not value.startswith("/")
        and all(part not in ("", ".", "..") for part in value.split("/"))
    )
    if not usable:
        raise BuildError(
            f"{value!r} is not a usable context path.",
            hint=(
                "context paths are relative, use forward slashes and contain no "
                '"." or ".." segments — they are hashed identity and extraction '
                "targets at once"
            ),
        )


def _require_files(entries: Iterable[ContextFile]) -> None:
    seen: set[str] = set()
    for entry in entries:
        _require_path(entry.path)
        _require_sha256(entry.sha256, what=f'The hash of "{entry.path}"')
        if (
            entry.path in (MANIFEST_FILE, CONTEXT_FILE)
            or entry.path.split("/", 1)[0] == BACKEND_DIR
        ):
            raise BuildError(
                f'The integrity list must not name "{entry.path}".',
                hint=(
                    "manifest.yaml describes the list and .mcuhome/ is backend "
                    "plumbing — neither belongs in the context's integrity list"
                ),
            )
        if entry.path in seen:
            raise BuildError(
                f'The integrity list names "{entry.path}" twice.',
                hint="one entry per file — a duplicate would make the canonical encoding ambiguous",
            )
        seen.add(entry.path)


def context_id(
    *,
    sdk_sha256: str,
    environment: EnvironmentPin,
    board: str,
    files: Iterable[ContextFile],
) -> str:
    """The context ID — SHA-256 over the canonical form of the hashed fields.

    This function is the normative rule of the module docstring, locked
    with ``context`` format version 4. It takes exactly the four hashed
    inputs and nothing else, so an informational field *cannot* leak
    into the ID by construction — the same discipline every version of
    this format has had, over the list that version fixed. Inputs are
    checked strictly rather than normalized: an ID computed over a
    mistyped hash would be silently wrong forever, and normalizing (say,
    uppercase hex) would give the same bytes two names.

    *environment* contributes both of its entries as the full
    ``(name, version, sha256)`` triple, and the two are hashed the same
    way. The ``url`` hints beside them are not hashed: they say where
    bytes were found, not which bytes they are.
    """
    entries = tuple(files)
    _require_sha256(sdk_sha256, what="The SDK package hash")
    validate_environment(environment)
    _require_board(board)
    _require_files(entries)
    document = {
        "build_environment": {
            "tools": environment.tools.identity(),
            "workspace": environment.workspace.identity(),
        },
        "files": [entry.to_dict() for entry in sorted(entries, key=lambda entry: entry.path)],
        "sdk": {"sha256": sdk_sha256},
        "target": {"board": board},
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_manifest(manifest: ContextManifest) -> None:
    """Check a parsed manifest's shape and spelling, not its truth.

    Every hashed field, spelled the one way the format allows, plus the
    declared ID. There is no unhashed pair to check beside them: both
    environment entries *are* hashed fields, and the Zephyr line the
    environment used to be checked against is not in the document at all.

    Whether the declared values match bytes on a disk is
    :func:`~mcuhome.workbench.contextdir.verify_context`'s question and needs a
    disk to answer.
    """
    _require_sha256(manifest.sdk.sha256, what="The SDK package hash")
    validate_environment(manifest.build_environment)
    _require_board(manifest.board)
    _require_files(manifest.files)
    _require_digest(manifest.id, what="The context id")


# --------------------------------------------------------------------------
# Conformance vectors
# --------------------------------------------------------------------------

#: The frozen rule, stated as inputs and outputs.
#:
#: A build server recomputes the context ID from the bytes it received,
#: and both sides of a build compute the same value at lock-context.
#: Whoever writes the second implementation — a third-party build
#: environment, a server in another language, a future rewrite of this
#: one — needs something to be wrong against that is not this file's
#: source code. These are it: run :func:`context_id` (or your own) over
#: each ``inputs`` and you must get ``id``.
#:
#: **A released version's vectors never change.** A vector may be added;
#: an existing one may not be altered, because altering one would mean
#: the rule changed, and the rule changing means a new ``context``
#: format version (:data:`CONTEXT_VERSION`) with vectors of its own.
#: They cover what an independent implementation gets wrong: an empty
#: file list (the document still has the key), the ordinary single-file
#: case, a list whose given order is not its hashed order, a path
#: with non-ASCII characters — which :func:`canonical_json` emits
#: literally rather than as ``\\u`` escapes, and which therefore only
#: hashes the same if the other side agrees about that — and the two
#: concerns *together*, which is the third thing and the one no single
#: vector could state: the sort is over code points (equivalently, over
#: UTF-8 bytes) and **not** over UTF-16 code units. The two orders agree
#: for the whole BMP and disagree the moment a path outside it meets one
#: inside it, and UTF-16 order is the plausible mistake rather than an
#: exotic one — RFC 8785, which this module's hashed document *is*, uses
#: exactly that order for object keys, so an implementation that reaches
#: for its JCS library's comparator for the ``files`` array gets it
#: wrong. The "astral and BMP paths" vector is the one that catches it;
#: every other vector in this table a UTF-16 sort passes.
#:
#: All of them were regenerated for format version 4, which is the only
#: thing that may cause a vector's ID to change and is exactly what a
#: version bump is for: the environment member stopped being one digest
#: and became two ``(name, version, sha256)`` triples, so every ID over
#: the same files and pins is a different number. Their other **inputs**
#: were kept identical to version 3's on purpose — a diff of this table
#: then shows one member reshaped and the hashes moved, rather than a new
#: table nobody can compare against the old one. Two vectors isolate the
#: environment member itself, and each differs from "one file" in exactly
#: one thing: "same file, other environment" in one byte of the tools
#: hash, "same file, concrete tools package" in the tools *name* alone —
#: which is the pair that catches an implementation leaving the name out
#: of the hashed triple, since a family pin and a per-platform pin would
#: then share one identity.
#:
#: The board names below are hash inputs and nothing else: the rule never
#: looks a board up, so a vector stays valid after the registry drops the
#: board it happens to name. Renaming one here would change an ID.
CONTEXT_ID_VECTORS: tuple[dict[str, Any], ...] = (
    {
        "name": "no files",
        "inputs": {
            "sdk_sha256": "a" * 64,
            "environment": {
                "workspace": {
                    "name": "mcuhome-build-workspace",
                    "version": "2.4.0",
                    "sha256": "1a" * 32,
                },
                "tools": {
                    "name": "mcuhome-build-tools",
                    "version": "1.2.0",
                    "sha256": "1b" * 32,
                },
            },
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (),
        },
        "id": "sha256:0d2525a387b277f7b28eae811620bd719e14e805fa815bdc4fa175fdfc918d72",
    },
    {
        "name": "one file",
        "inputs": {
            "sdk_sha256": "b" * 64,
            "environment": {
                "workspace": {
                    "name": "mcuhome-build-workspace",
                    "version": "2.4.0",
                    "sha256": "2b" * 32,
                },
                "tools": {
                    "name": "mcuhome-build-tools",
                    "version": "1.2.0",
                    "sha256": "2c" * 32,
                },
            },
            "board": "nrf52840dk/nrf52840",
            "files": (("model/device-model.json", "c" * 64),),
        },
        "id": "sha256:fc79e338cda258ffbad86e0ad8391bda16c2b7edff70888a482b5d54707e742a",
    },
    {
        "name": "given out of order",
        "inputs": {
            "sdk_sha256": "d" * 64,
            "environment": {
                "workspace": {
                    "name": "mcuhome-build-workspace",
                    "version": "2.4.0",
                    "sha256": "3c" * 32,
                },
                "tools": {
                    "name": "mcuhome-build-tools",
                    "version": "1.2.0",
                    "sha256": "3d" * 32,
                },
            },
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("patches/zephyr/0002-b.patch", "2" * 64),
                ("model/device-model.json", "1" * 64),
                ("patches/zephyr/0001-a.patch", "3" * 64),
                ("keys/signing.pub", "4" * 64),
            ),
        },
        "id": "sha256:da42a5dbc52b203cb934666ec955274437880cfe29059aa1818bad0e26239ae0",
    },
    {
        # The vector tests/python/test_context.py has pinned since the format
        # was written, kept where a second implementation can reach it.
        "name": "model and one patch",
        "inputs": {
            "sdk_sha256": "cd" * 32,
            "environment": {
                "workspace": {
                    "name": "mcuhome-build-workspace",
                    "version": "2.4.0",
                    "sha256": "4d" * 32,
                },
                "tools": {
                    "name": "mcuhome-build-tools",
                    "version": "1.2.0",
                    "sha256": "4e" * 32,
                },
            },
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("model/device-model.json", "11" * 32),
                ("patches/zephyr/0001-fix.patch", "22" * 32),
            ),
        },
        "id": "sha256:40c5066b0891e91aedd299cfd21cbb81b12e028228f7b0007e7cf92f8522bca7",
    },
    {
        "name": "non-ascii path",
        "inputs": {
            "sdk_sha256": "e" * 64,
            "environment": {
                "workspace": {
                    "name": "mcuhome-build-workspace",
                    "version": "2.4.0",
                    "sha256": "5e" * 32,
                },
                "tools": {
                    "name": "mcuhome-build-tools",
                    "version": "1.2.0",
                    "sha256": "5f" * 32,
                },
            },
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (("model/d\u00e9vice-mod\u00e8l.json", "5" * 64),),
        },
        "id": "sha256:03c1700e71e3bf3f9fe0249e35734a83d4c4788dfbd5a9b7d34dde59f3d36c59",
    },
    {
        # The two paths straddle the BMP: U+FF21 FULLWIDTH LATIN CAPITAL
        # LETTER A is inside it, U+1F600 GRINNING FACE is not, and UTF-16
        # spells the second one as a surrogate pair from U+D800 — below
        # U+FF21. So code-point order puts the fullwidth A first and
        # UTF-16 code-unit order puts the emoji first, and an
        # implementation that sorted the array the way RFC 8785 sorts
        # object keys computes a different ID here and the same ID
        # everywhere else in this table. Given emoji-first, so that a
        # sort that never runs is caught too.
        #
        # Written as escapes rather than as the characters themselves —
        # the whole vector turns on *which* character each one is, and a
        # fullwidth A that looked like an ASCII A would be a trap in the
        # trap. The paths are "model/" + U+FF21 + ".json" and "model/" +
        # U+1F600 + ".json"; the bytes hashed are their UTF-8.
        "name": "astral and BMP paths",
        "inputs": {
            "sdk_sha256": "f" * 64,
            "environment": {
                "workspace": {
                    "name": "mcuhome-build-workspace",
                    "version": "2.4.0",
                    "sha256": "6f" * 32,
                },
                "tools": {
                    "name": "mcuhome-build-tools",
                    "version": "1.2.0",
                    "sha256": "60" * 32,
                },
            },
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("model/\U0001f600.json", "7" * 64),
                ("model/\uff21.json", "6" * 64),
            ),
        },
        "id": "sha256:7f19bf1022811afc9ea034232907ddc59f8cd4264d586d018708f1416be7754f",
    },
    {
        # "one file", with one byte of the tools hash changed and nothing
        # else. Two builds of the same sources in two different
        # environments are two builds, and this is the vector that says
        # so: an implementation that dropped the member — or hashed only
        # the workspace — agrees with the table everywhere except here.
        "name": "same file, other environment",
        "inputs": {
            "sdk_sha256": "b" * 64,
            "environment": {
                "workspace": {
                    "name": "mcuhome-build-workspace",
                    "version": "2.4.0",
                    "sha256": "2b" * 32,
                },
                "tools": {
                    "name": "mcuhome-build-tools",
                    "version": "1.2.0",
                    "sha256": "2c" * 31 + "2d",
                },
            },
            "board": "nrf52840dk/nrf52840",
            "files": (("model/device-model.json", "c" * 64),),
        },
        "id": "sha256:1fbd501259a7b6005e75c2c9ed5469964439ce157560b09fedfc81bb0f740fde",
    },
    {
        # "one file" again, with the tools entry named as one platform's
        # concrete package instead of as the family — same version, same
        # hash, same everything else. A family pin resolves per platform
        # and a concrete pin does not, so the two are different builds;
        # an implementation that left ``name`` out of the hashed triple
        # computes the same ID here as for "one file".
        "name": "same file, concrete tools package",
        "inputs": {
            "sdk_sha256": "b" * 64,
            "environment": {
                "workspace": {
                    "name": "mcuhome-build-workspace",
                    "version": "2.4.0",
                    "sha256": "2b" * 32,
                },
                "tools": {
                    "name": "mcuhome-build-tools_linux-amd64",
                    "version": "1.2.0",
                    "sha256": "2c" * 32,
                },
            },
            "board": "nrf52840dk/nrf52840",
            "files": (("model/device-model.json", "c" * 64),),
        },
        "id": "sha256:a4db29a7979b8665c2d52d471a1b7c8c581b63bfe8ca6af58bf4c7fc0573c12d",
    },
)


def vector_id(vector: dict[str, Any]) -> str:
    """Run one :data:`CONTEXT_ID_VECTORS` entry through :func:`context_id`.

    A vector states its files as ``(path, sha256)`` pairs and its
    environment as two plain objects rather than as :class:`ContextFile`
    and :class:`PackagePin` instances, so that the data stays copyable
    into a document another implementation can read.
    """
    inputs = vector["inputs"]
    environment = inputs["environment"]
    return context_id(
        sdk_sha256=inputs["sdk_sha256"],
        environment=EnvironmentPin(
            workspace=PackagePin(**environment["workspace"]),
            tools=PackagePin(**environment["tools"]),
        ),
        board=inputs["board"],
        files=[ContextFile(path=path, sha256=sha256) for path, sha256 in inputs["files"]],
    )
