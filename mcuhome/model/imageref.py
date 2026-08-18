# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""How MCUHome names an external input: the Docker reference form.

A build needs two things nobody keeps in the project folder — the SDK
package and the build environment — and both are named the same way,
because a person naming them already knows this spelling::

    [registry/]path[:tag][@sha256:...]

Registry, tag and digest are each optional, and each optional part means
something different. **Absent registry** means "the official one for this
kind of thing", which is a different host for an SDK than for a build
environment, so it is a parameter of parsing rather than a constant here.
**Absent tag** means "whatever the publisher currently recommends" — the
moving name a resolution follows. **Absent digest** means the reference
has not been pinned yet; a resolution's whole job is to add one.

The type is deliberately dumb: it parses, it renders, and it holds the
four parts apart. It resolves nothing, fetches nothing, and knows about
neither SDKs nor containers — the two resolvers that do live where they
can reach a network, and they take one of these and answer with one of
these carrying a digest. That is also why it is in this package rather
than in a workbench: the client writes a pinned reference into a build
context and a build server reads it back out, so both ends need to agree
on the spelling without sharing any of the code that produced it.

**A pinned reference keeps its tag.** ``repo:zephyr-4.4.0-r9@sha256:…``
binds to the digest — that is what docker fetches and verifies — while
the tag stays as documentation for whoever reads the pin a year later.
Dropping it would lose the only human-readable part of the record, and
keeping it costs nothing because nothing ever resolves it again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from mcuhome.model.errors import BuildError

__all__ = [
    "DOCKER_HUB",
    "Reference",
    "parse_reference",
]

#: The registry docker itself assumes for a reference that names none.
#: It is the right default for a *build environment*, which is an
#: ordinary container image and may come from any registry at all. An
#: SDK's default is MCUHome's own package host, and the caller states it.
DOCKER_HUB = "docker.io"

#: A registry host: a DNS-ish name or address, optionally with a port.
#: Deliberately permissive about the host — an operator's internal name
#: can be almost anything — and strict about the shape, because the shape
#: is what distinguishes a registry from the first component of a path.
_REGISTRY = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]+)?\Z")

#: One slash-separated component of a repository path, as docker's own
#: grammar defines it: runs of lowercase alphanumerics, joined by a
#: single period, a single underscore, a double underscore, or any run of
#: hyphens. A component may be a single character (``a/b/c`` is a path).
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"

#: A repository path. Lowercase, because a registry rejects anything else
#: and refusing it here turns a 400 from a stranger's server into a
#: sentence naming the actual problem.
_PATH = re.compile(rf"{_COMPONENT}(?:/{_COMPONENT})*\Z")

#: A tag: docker's grammar, 128 characters, no leading period or hyphen.
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}\Z")

#: A digest. Only SHA-256, which is the only algorithm anything in this
#: project computes or compares — a reference naming another one would be
#: accepted here and refused by every consumer, which is a worse place to
#: find out.
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class Reference:
    """One external input, named: registry, path, and what pins it.

    :attr:`registry` is always filled — parsing resolves an absent one to
    the default for its kind — so that two spellings of one location are
    one value here. :attr:`tag` and :attr:`digest` are independently
    optional: a bare repository has neither, a resolution answers with
    both, and a hand-pinned reference may carry only a digest.
    """

    registry: str
    path: str
    tag: str | None = None
    digest: str | None = None

    @property
    def repository(self) -> str:
        """``registry/path`` — the reference with everything moving removed."""
        return f"{self.registry}/{self.path}"

    @property
    def pinned(self) -> bool:
        """Does this reference name exact bytes?"""
        return self.digest is not None

    def with_digest(self, digest: str, *, tag: str | None = None) -> Reference:
        """This reference, pinned — the answer shape of every resolver.

        *tag* records which moving name the digest was found under, so a
        resolution that followed ``zephyr-4.4-latest`` records both the
        digest it bound to and the name it came from. An existing tag is
        kept when none is given.
        """
        if not _DIGEST.fullmatch(digest):
            raise BuildError(
                f'"{digest}" is not a sha256 digest.',
                hint="a digest is sha256: followed by 64 lowercase hex digits",
            )
        return replace(self, digest=digest, tag=tag if tag is not None else self.tag)

    def __str__(self) -> str:
        """The full explicit form — what docker is handed, what a pin records."""
        text = self.repository
        if self.tag:
            text = f"{text}:{self.tag}"
        if self.digest:
            text = f"{text}@{self.digest}"
        return text

    def runnable(self) -> str:
        """How to run these bytes: by digest where there is one.

        Docker accepts ``repo:tag@sha256:…`` and verifies the digest, but
        a reference that carries *only* a tag is a moving name, and
        running one after resolving it would throw away the resolution.
        A pinned reference is therefore run as ``repo@sha256:…`` — the
        tag is documentation in the record, not an input to the run.
        """
        return f"{self.repository}@{self.digest}" if self.digest else str(self)


def parse_reference(text: str, *, default_registry: str, what: str = "reference") -> Reference:
    """Parse *text* as ``[registry/]path[:tag][@digest]``.

    *default_registry* is the host an absent one means — MCUHome's
    package host for an SDK, Docker Hub for a build environment — and
    *what* names the thing in a refusal.

    The registry is recognized the way docker recognizes it, and there is
    no better rule available: a first path component containing a dot or
    a colon, or spelled ``localhost``, is a host, and anything else is
    part of the path. That is why ``sdk/mcuhome-sdk`` is a repository on
    the default host while ``packages.mcuhome.org/sdk/mcuhome-sdk`` names
    its own.

    Every part is checked, and a refusal names the part that is wrong
    rather than the whole string: these values are typed by people into
    configuration files, and "not a valid reference" sends them to reread
    a line that is nine tenths correct.
    """
    if not isinstance(text, str) or not text.strip():
        raise BuildError(
            f"The {what} is empty.",
            hint="name it as registry/path:tag@sha256:… — registry, tag and digest optional",
        )
    rest = text.strip()

    digest: str | None = None
    rest, at_sign, tail = rest.partition("@")
    if at_sign:
        if not _DIGEST.fullmatch(tail):
            raise BuildError(
                f'The {what} "{text}" names a digest that is not one: "{tail}".',
                hint="a digest is sha256: followed by 64 lowercase hex digits",
            )
        digest = tail

    # The tag is separated last-colon-first, and only when what follows
    # holds no slash: `registry:5000/path` has a colon that belongs to
    # the registry's port, not to a tag.
    tag: str | None = None
    head, colon, maybe_tag = rest.rpartition(":")
    if colon and "/" not in maybe_tag:
        if not _TAG.fullmatch(maybe_tag):
            raise BuildError(
                f'The {what} "{text}" names a tag that is not one: "{maybe_tag}".',
                hint=(
                    "a tag is up to 128 characters of letters, digits, period, "
                    "underscore and hyphen, and does not start with a period or hyphen"
                ),
            )
        tag = maybe_tag
        rest = head

    registry = default_registry
    first, slash, remainder = rest.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        if not _REGISTRY.fullmatch(first):
            raise BuildError(
                f'The {what} "{text}" names a registry that is not one: "{first}".',
                hint="a registry is a host name, optionally with a :port",
            )
        registry = first
        rest = remainder

    if not _PATH.fullmatch(rest):
        raise BuildError(
            f'The {what} "{text}" names no repository path.',
            hint=(
                "a path is lowercase letters, digits and the separators "
                "period, underscore and hyphen, in slash-separated components — "
                f'"{default_registry}" is assumed when none is named'
            ),
        )
    return Reference(registry=registry, path=rest, tag=tag, digest=digest)
