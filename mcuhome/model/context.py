# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context — the self-contained input artifact of a remote build.

A build context is a plain directory: ``manifest.yaml``, the canonical
device model under ``model/``, optionally patches under
``patches/<layer>/``. It contains everything a build needs except the
build environment (a build container carrying a toolchain and Zephyr)
and the SDK (fetched as a hash-pinned package). It travels as an
archive, but the archive is transport: the directory is the artifact.

**The client pins the build environment, and the pin is part of the
identity.** A context names the exact image its firmware is compiled in,
as a Docker reference carrying a digest, and nobody downstream chooses
anything: a build server runs the bytes the reference names or refuses.
That is what makes a context a complete statement of a build — the same
context yields the same firmware on any machine that can fetch the
image, which a context naming only a *requirement* could not promise,
because two backends could answer one requirement with two different
containers.

It is the reverse of what version 2 did, and the reason is that the
client turned out to be able to do it. Selecting an environment needs a
registry's tag list and an image's labels, both of which are three
anonymous HTTP requests away — no pull, no container, no build server.
Once the client can resolve, having the backend resolve buys nothing and
costs the identity.

**The context ID is normative — fixed with ``context`` format version 3,
and it can never change afterwards.** Everything that ever names a
context — integrity verification, artifact attribution ("built from
*this*"), archival references — depends on computing the same ID from
the same inputs forever. The rule is stated in
``docs/design/build-container-contract.md`` §3.3 (ADR 0018); this
module implements it. The ID is the SHA-256 over the canonical JSON
(RFC 8785) of exactly this document::

    {"build_environment": {"digest": ...},
     "files": [{"path": ..., "sha256": ...}, ...],
     "sdk": {"sha256": ...},
     "target": {"board": ...}}

— the manifest's build-relevant fields under the contract's fixed
names (``sdk.sha256`` carries the manifest's ``mcuhome.package.sha256``,
``build_environment.digest`` the digest of ``build_environment``).
``files`` is sorted by ``path`` in
ascending byte order of its UTF-8 encoding — which UTF-8 makes equal
to code-point order, so a plain string sort implements it — and every
listed file contributes its own content hash; the sort only makes the
encoding deterministic.

Explicitly excluded, so they can never influence the ID: ``created``
(informational), ``mcuhome.constraint`` (the intent, not the
resolution), ``mcuhome.version`` and ``mcuhome.package.url`` (names for
the bytes ``package.sha256`` already pins), and everything about the
build environment **except its digest** — the registry it was fetched
from and the tag it was found under are a location and a label for
bytes the digest already identifies, exactly as ``package.url`` is for
the SDK.

The Zephyr *line* is not in the document at all, in either form. It was
version 2's requirement field, and a pinned environment answers it: the
image states which Zephyr it carries, the resolution checked that
statement against the model's constraint before pinning, and the model
itself is an ordinary hashed entry of ``files``. A separate copy would
be a third place for the same fact to be wrong in.

``manifest.yaml`` itself and the backend-written ``.mcuhome/`` runtime
directory are never integrity entries, so they cannot influence the ID
either. Neither the YAML file bytes nor the transport archive bytes are
ever hashed — neither serialization is deterministic. New
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
actually present — are :mod:`mcuhome.workbench.contextdir`. The cut is ADR 0020's:
the build server recomputes a context ID from bytes it received off a
socket and must carry no build logic to do it, so :func:`context_id`
takes values and not a directory, and this module imports nothing but
the standard library and :mod:`mcuhome.model.errors`.

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
from mcuhome.model.imageref import DOCKER_HUB, parse_reference

__all__ = [
    "BACKEND_DIR",
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
    "SdkPin",
    "canonical_json",
    "context_id",
    "environment_digest",
    "validate_manifest",
    "vector_id",
]

#: Format version of the context manifest. The normative hashing rule is
#: locked to it: a field can join the hashed document only together with
#: a bump here, and version 3's rule never changes.
#:
#: Versions 1 and 2 are gone rather than supported alongside this one,
#: for the reason version 1 was dropped when 2 arrived: nothing is
#: published, no context written to an older format exists outside a
#: test, and a reader that accepted several would have to carry a hashing
#: rule per format forever to serve exactly zero documents. Version 1
#: pinned a container and hashed it; version 2 stated a Zephyr line and
#: let the backend choose; this one pins again — and hashes the pin,
#: which is what version 1 got right and could not deliver, because the
#: client had no way to resolve a digest at the time.
CONTEXT_VERSION = 3

#: The one file a builder must parse first, at the top of the context.
MANIFEST_FILE = "manifest.yaml"

#: The request document with the pins, next to the manifest (§3.2). It
#: is excluded from the integrity list **as a statement about the hash,
#: not about layout**: its never-hashed fields (constraint, url,
#: created) would otherwise leak into an identity that §6 computes from
#: resolved values alone.
CONTEXT_FILE = "context.yaml"

#: Where the MCUboot verification key lives inside a context (ADR 0018's
#: 2026-08-09 amendment; required for ``build``, §7.2).
KEYS_DIR = "keys"
SIGNING_KEY_FILE = "keys/signing.pub"

#: Where the canonical device model lives inside the context — the
#: existing wire format (:mod:`mcuhome.model.model`), unchanged.
MODEL_FILE = "model/device-model.json"

#: Where patches live: ``patches/<layer>/NNNN-name.patch``.
PATCHES_DIR = "patches"

#: The backend-written runtime directory inside a mounted context
#: (``.mcuhome/command.json``), as contract v1 was first drafted.
#: Plumbing, not content: never an integrity entry, never identity.
#:
#: **The contract has moved on and this constant has not yet.** The
#: per-invocation request document now lives in a backend-owned
#: directory outside the context, and there is no ``.mcuhome/`` in a
#: context at all (build-container-contract.md §3.1, §5.2) — which is
#: what makes the context a genuinely read-only mount. Removing the
#: directory from the exclusion rules is migration work; keeping it
#: excluded in the meantime is harmless, because a context that never
#: contains one cannot be affected by the exclusion.
BACKEND_DIR = ".mcuhome"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


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
    (a PEP 440 specifier such as ``~=2.3.6`` — ADR 0018's PEP 440
    amendment); ``version`` and the package are what it resolved to at
    context creation. Only ``sha256`` is part of the context's identity:
    the constraint is intent, and the version and URL are names for the
    bytes the hash already pins.
    """

    constraint: str
    version: str
    #: Where the resolved SDK package can be fetched. A hint only — a
    #: server resolves (version, sha256) against its own source list.
    url: str
    sha256: str


@dataclass(frozen=True)
class EnvironmentPin:
    """Which build environment a context is compiled in — one reference.

    A Docker reference in its full explicit form, and it **must carry a
    digest**: ``ghcr.io/mcu-home/build-container:zephyr-4.4.0-r10@sha256:…``.
    That is what "pinned" means here — the tag is documentation for
    whoever reads the record later, the digest is what a build fetches
    and what this context's identity is computed over.

    One string rather than a block of parts, because every consumer is a
    container runtime and this is the spelling every container runtime
    takes. Splitting it into registry, path, tag and digest would create
    four fields that can disagree with each other and one join to get
    wrong in each reader; parsing it is
    :func:`mcuhome.model.imageref.parse_reference`, for the readers that
    care about a part.
    """

    reference: str

    @property
    def digest(self) -> str:
        """The hashed half — the value :func:`context_id` takes.

        Derived rather than stored so that the document has exactly one
        place the digest is written, which is the one a runtime reads.
        """
        return environment_digest(self.reference)


def environment_digest(reference: object) -> str:
    """The ``sha256:…`` a pinned build-environment reference ends in.

    Strict about **both halves**, because both are read by somebody. The
    digest is the identity input: it is checked for the one spelling the
    format allows rather than recovered from a near miss, since an ID
    computed over something else is silently wrong forever, and a
    reference without one is not a pin at all — it names a moving tag,
    and hashing a moving name would let two builds of different bytes
    claim one identity. The rest of the reference is what a container
    runtime is handed, so it is parsed here rather than trusted: a
    document that named no repository would be accepted by a hash rule
    that only ever looks past the ``@``, and refused later by whoever
    tried to run it.
    """
    if not isinstance(reference, str) or "@" not in reference:
        raise BuildError(
            "The build environment is not pinned to a digest.",
            hint=(
                "a context names the exact image it is compiled in, as "
                "repository:tag@sha256:… — a tag alone moves, and a build that "
                "cannot say which bytes produced it is not reproducible"
            ),
        )
    parsed = parse_reference(reference, default_registry=DOCKER_HUB, what="build environment")
    if parsed.digest is None or not _DIGEST.fullmatch(parsed.digest):
        raise BuildError(
            f'The build environment names a digest that is not one: "{parsed.digest}".',
            hint="a digest is sha256: followed by 64 lowercase hex digits",
        )
    return parsed.digest


@dataclass(frozen=True)
class ContextManifest:
    """``manifest.yaml``, as an object."""

    sdk: SdkPin
    #: The build environment this context is compiled in, pinned to a
    #: digest. Repeated verbatim from the request: the locking party
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
            environment_digest=self.build_environment.digest,
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
            "build_environment": self.build_environment.reference,
            "target": {"board": self.board},
            "files": [entry.to_dict() for entry in self.files],
            "id": self.id,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ContextManifest:
        mcuhome = data["mcuhome"]
        package = mcuhome["package"]
        # ``created`` is deliberately not read: it dates the *request* and
        # lives in context.yaml alone — "the one field that does not
        # travel" (ADR 0018). A manifest that carries one anyway is
        # handled by the unknown-field rule: ignored.
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
            # Not coerced: the reference carries the digest the identity
            # is computed over, and a value that is not a string is a
            # malformed document rather than one to str() into shape.
            build_environment=EnvironmentPin(reference=data["build_environment"]),
            board=data["target"]["board"],
            files=tuple(
                ContextFile(path=item["path"], sha256=item["sha256"]) for item in data["files"]
            ),
            id=data["id"],
        )


@dataclass(frozen=True)
class ContextRequest:
    """``context.yaml``, as an object — the pinning *request* (ADR 0018 amendment).

    The client-written half of the split ADR 0018 decision 6's single
    document became: the ``lock-context`` freeze splits the manifest into
    a *request* and a *result* (build-container-contract.md §3.2), and
    this is the request. It carries the ``context`` format version, the
    resolved SDK pin, the pinned build environment and the original
    intent the session is admitted on — and deliberately **nothing that
    depends on the final file set**: no ``files`` list and no ``id``.
    Those are the result the locking party computes and writes into
    :class:`ContextManifest`.

    It carries the **pinned build environment** — the client resolved it
    before writing this, out of the reference and the constraint its
    device model states. A backend therefore reads a decision here, not a
    requirement to answer, which is what makes the manifest's copy of it
    a restatement rather than a second opinion.

    It repeats :class:`SdkPin` verbatim — the same pin
    :class:`ContextManifest` later restates, so intent and resolution
    stand side by side where a human reads back what was asked for
    (decision 3) — and adds ``created``. That timestamp is "the one field
    that does not travel" (ADR 0018): it dates the request, is never
    hashed, and lives here alone.
    """

    sdk: SdkPin
    #: The build environment, pinned to a digest — what the client
    #: resolved its model's reference and Zephyr constraint to. **Hashed**,
    #: through its digest: it is a resolved value like the SDK's package
    #: hash, not an intent like the constraint beside it.
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
            "build_environment": self.build_environment.reference,
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
            # Not coerced, for the reason the hashes are not: it is an
            # identity input, checked for the one legal spelling where it
            # is used rather than reshaped on read.
            build_environment=EnvironmentPin(reference=data["build_environment"]),
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
                    "plumbing — the contract keeps both out of the context's "
                    "identity (build-container-contract.md §3.2)"
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
    environment_digest: str,
    board: str,
    files: Iterable[ContextFile],
) -> str:
    """The context ID — SHA-256 over the canonical form of the hashed fields.

    This function is the normative rule of the module docstring, locked
    with ``context`` format version 3. It takes exactly the four hashed
    inputs and nothing else, so an informational field *cannot* leak
    into the ID by construction — the same discipline every version of
    this format has had, over the list that version fixed. Inputs are
    checked strictly rather than normalized: an ID computed over a
    mistyped hash would be silently wrong forever, and normalizing (say,
    uppercase hex) would give the same bytes two names.

    *environment_digest* is the digest alone and not the reference it
    came from, so that the same image fetched from a mirror is the same
    build. :func:`environment_digest` is what takes one out of a pin.
    """
    entries = tuple(files)
    _require_sha256(sdk_sha256, what="The SDK package hash")
    _require_digest(environment_digest, what="The build environment digest")
    _require_board(board)
    _require_files(entries)
    document = {
        "build_environment": {"digest": environment_digest},
        "files": [entry.to_dict() for entry in sorted(entries, key=lambda entry: entry.path)],
        "sdk": {"sha256": sdk_sha256},
        "target": {"board": board},
    }
    digest = hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_manifest(manifest: ContextManifest) -> None:
    """Check a parsed manifest's shape and spelling, not its truth.

    Every hashed field, spelled the one way the format allows, plus the
    declared ID. There is no longer an unhashed pair to check beside
    them: the environment reference *is* a hashed field now, through its
    digest, and the Zephyr line it used to be checked against is not in
    the document at all.

    Whether the declared values match bytes on a disk is
    :func:`~mcuhome.workbench.contextdir.verify_context`'s question and needs a
    disk to answer.
    """
    _require_sha256(manifest.sdk.sha256, what="The SDK package hash")
    # Raises exactly where a malformed reference matters: the property is
    # what the ID is computed over, so checking it is checking the pin.
    environment_digest(manifest.build_environment.reference)
    _require_board(manifest.board)
    _require_files(manifest.files)
    _require_digest(manifest.id, what="The context id")


# --------------------------------------------------------------------------
# Conformance vectors
# --------------------------------------------------------------------------

#: The frozen rule, stated as inputs and outputs.
#:
#: ADR 0019 §8 obliges the build server to recompute the context ID from
#: the bytes it received, and ADR 0018 §6 says both sides of the contract
#: compute the same value. Whoever writes the second implementation — a
#: third-party build container, a server in another language, a future
#: rewrite of this one — needs something to be wrong against that is not
#: this file's source code. These are it: run :func:`context_id` (or your
#: own) over each ``inputs`` and you must get ``id``.
#:
#: **Version 2's vectors never change.** A vector may be added; an
#: existing one may not be altered, because altering one would mean the
#: rule changed, and the rule changing means a new ``context`` format
#: version (:data:`CONTEXT_VERSION`) with vectors of its own. They
#: cover what an independent implementation gets wrong: an empty
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
#: All six were regenerated for format version 3, which is the only
#: thing that may cause a vector's ID to change and is exactly what a
#: version bump is for: the hashed document gained a
#: ``build_environment`` member, so every ID over the same files and
#: pins is a different number. Their other **inputs** were kept
#: identical to version 2's on purpose — a diff of this table then shows
#: one member added and six hashes moved, rather than a new table nobody
#: can compare against the old one. The seventh is new, and adding one
#: is the only way a gap can be closed: an existing vector may not be
#: altered. It pins the member itself, by differing from "one file" in
#: nothing but the environment — the one thing no other pair in the
#: table isolates.
#:
#: The board names below are hash inputs and nothing else: the rule never
#: looks a board up, so a vector stays valid after the registry drops the
#: board it happens to name. Renaming one here would change an ID.
CONTEXT_ID_VECTORS: tuple[dict[str, Any], ...] = (
    {
        "name": "no files",
        "inputs": {
            "sdk_sha256": "a" * 64,
            "environment_digest": "sha256:" + "1a" * 32,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (),
        },
        "id": "sha256:008642bbad04acba634db35a94c0435ceeb19f49bbf518838418b27682d14073",
    },
    {
        "name": "one file",
        "inputs": {
            "sdk_sha256": "b" * 64,
            "environment_digest": "sha256:" + "2b" * 32,
            "board": "nrf52840dk/nrf52840",
            "files": (("model/device-model.json", "c" * 64),),
        },
        "id": "sha256:7363f5f0ca2f41118493880c97681a77fedb302ce599209098b3f37b49d88bc9",
    },
    {
        "name": "given out of order",
        "inputs": {
            "sdk_sha256": "d" * 64,
            "environment_digest": "sha256:" + "3c" * 32,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("patches/zephyr/0002-b.patch", "2" * 64),
                ("model/device-model.json", "1" * 64),
                ("patches/zephyr/0001-a.patch", "3" * 64),
                ("keys/signing.pub", "4" * 64),
            ),
        },
        "id": "sha256:19f02a62af58d3889d556389fd5dd79f2ad44e32a30940a40c08808a148f3300",
    },
    {
        # The vector tests_py/test_context.py has pinned since the format
        # was written, kept where a second implementation can reach it.
        "name": "model and one patch",
        "inputs": {
            "sdk_sha256": "cd" * 32,
            "environment_digest": "sha256:" + "4d" * 32,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("model/device-model.json", "11" * 32),
                ("patches/zephyr/0001-fix.patch", "22" * 32),
            ),
        },
        "id": "sha256:b033e1ddade6357860d87555d87c6575ec53901623b64b8452b16c954c9d3479",
    },
    {
        "name": "non-ascii path",
        "inputs": {
            "sdk_sha256": "e" * 64,
            "environment_digest": "sha256:" + "5e" * 32,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (("model/dévice-modèl.json", "5" * 64),),
        },
        "id": "sha256:de4db67451a7da65234ae445a2b7bf19e3a2f5b2ba372a09f7180928a13c71b5",
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
            "environment_digest": "sha256:" + "6f" * 32,
            "board": "nrf7002dk/nrf5340/cpuapp",
            "files": (
                ("model/\U0001f600.json", "7" * 64),
                ("model/\uff21.json", "6" * 64),
            ),
        },
        "id": "sha256:07be3c49d2753c414706be109666009e84afb677a23d7c5f03afe1cb7eaaa57a",
    },
    {
        # "one file", with one byte of the environment digest changed and
        # nothing else. Two builds of the same sources in two different
        # containers are two builds, and this is the vector that says so:
        # an implementation that dropped the member — or hashed the whole
        # reference instead of the digest — agrees with the table
        # everywhere except here.
        "name": "same file, other environment",
        "inputs": {
            "sdk_sha256": "b" * 64,
            "environment_digest": "sha256:" + "2b" * 31 + "2c",
            "board": "nrf52840dk/nrf52840",
            "files": (("model/device-model.json", "c" * 64),),
        },
        "id": "sha256:c2e2e6ed19c6040a3bd0d95cd95e0685d763935255ac2b173745e4fec5ed31ee",
    },
)


def vector_id(vector: dict[str, Any]) -> str:
    """Run one :data:`CONTEXT_ID_VECTORS` entry through :func:`context_id`.

    A vector states its files as ``(path, sha256)`` pairs rather than as
    :class:`ContextFile` objects, so that the data stays copyable into a
    document another implementation can read.
    """
    inputs = vector["inputs"]
    return context_id(
        sdk_sha256=inputs["sdk_sha256"],
        environment_digest=inputs["environment_digest"],
        board=inputs["board"],
        files=[ContextFile(path=path, sha256=sha256) for path, sha256 in inputs["files"]],
    )
