# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Zephyr line and binary-blob resolution — the seam for it.

The eventual binary-blob policy describes real machinery: a
per-board blob availability matrix extracted from the builder container
images, a resolver that trades a Zephyr release line against blob
compatibility, and a drift check that flags overrides which have become
redundant.

**None of that exists yet, and this module does not pretend otherwise.**
What it provides is the seam:

* the vocabulary of ``device.blob_usage`` / ``device.zephyr_version`` /
  ``device.blobs`` is accepted and validated (that part is stable —
  yaml-schema.md §3);
* :func:`available_blobs` is the single hook the future availability
  matrix plugs into. Today it returns nothing for every board, because
  MCUHome integrates no blob yet (the MPSL/SDC and nrf_cc3xx feasibility
  work is still open);
* :data:`SUPPORTED_ZEPHYR_LINES` is the single place the "at most two
  concurrent lines" rule will grow into. Today there is exactly one.

A user who forces something this cannot deliver gets the plain-language
refusal the blob policy asks for, not a silent downgrade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from mcuhome.model.errors import ErrorCollector, Location

__all__ = [
    "SUPPORTED_ZEPHYR_LINES",
    "ZEPHYR_CONSTRAINT",
    "ResolvedToolchain",
    "available_blobs",
    "line_of",
    "normalize_release",
    "resolve_toolchain",
    "satisfies_line",
]

#: Zephyr release lines this MCUHome release can build (a line, never a
#: frozen point release — patch releases with security backports are
#: always taken). CHIP v1.5.1.0 is pinned against 4.4.
SUPPORTED_ZEPHYR_LINES: tuple[str, ...] = ("4.4",)

#: The newest supported line; what ``zephyr_version: latest`` resolves to.
LATEST_ZEPHYR_LINE = SUPPORTED_ZEPHYR_LINES[-1]

#: **What this SDK release requires of a build environment**, as a PEP 440
#: constraint rather than a version. It is the SDK author's statement and
#: nobody else's: he knows that CHIP v1.5.1.0 needs 4.4, and that a fix he
#: depends on landed in a particular patch release, so he writes the range
#: and lets whoever publishes environments decide which revision of it to
#: recommend.
#:
#: A constraint rather than a version because the two failure modes are
#: not symmetric. A pinned version means a security patch release cannot
#: be taken without a new SDK; a range means it is taken automatically,
#: which is what the release-line policy already decided for the
#: *line*. And an SDK
#: that genuinely needs one exact release says ``==4.4.2`` here, in the
#: same grammar, with no special case anywhere downstream.
#:
#: It is evaluated where ``packaging`` is available — this package has no
#: dependencies by construction — so what lives here is the
#: string and what reads it is
#: :mod:`mcuhome.workbench.resolve_env`.
ZEPHYR_CONSTRAINT = "~=4.4.0"


@dataclass(frozen=True)
class ResolvedToolchain:
    """What the builder will actually build against."""

    #: Resolved Zephyr release line, e.g. ``"4.4"``.
    zephyr_line: str
    #: Resolved value of ``device.blob_usage``.
    blob_usage: str
    #: Resolved per-blob decisions. Empty until blobs are integrated.
    blobs: dict[str, str] = field(default_factory=dict)
    #: The PEP 440 constraint a build environment has to satisfy — this
    #: SDK's :data:`ZEPHYR_CONSTRAINT`, narrowed to the line the device
    #: asked for when it named one. The two are the same statement seen
    #: from two sides, and both belong in the model: the constraint is
    #: what selects an environment, the line is what a human reads.
    zephyr_constraint: str = ZEPHYR_CONSTRAINT


#: A Zephyr release as an image may spell it: dotted decimal components,
#: optionally followed by an upstream suffix after a hyphen. The
#: ``org.mcuhome.build-environment.zephyr.version`` label's own value
#: range is SemVer 2.0.0 (``docs/spec/build-environment-specification.md``
#: §5, §5.2) — a stricter grammar than this pattern accepts: SemVer fixes
#: the numeric part at exactly three components and splits what follows a
#: hyphen into a dash-prefixed pre-release part and a separate
#: plus-prefixed build-metadata part, while this pattern accepts any
#: number of dot-separated numeric components and folds everything after
#: one hyphen into a single opaque suffix. That is deliberate: this
#: pattern only has to tell which line a release is *in*, not enforce the
#: label's full grammar — what does not parse as a version cannot be *in*
#: a line, which is the only question :func:`satisfies_line` asks.
_RELEASE = re.compile(r"(?P<numbers>[0-9]+(\.[0-9]+)*)(?P<suffix>-[A-Za-z0-9._+-]+)?\Z")


def satisfies_line(version: str, *, line: str) -> bool:
    """Does a build container carrying Zephyr *version* serve *line*?

    The one implementation of the release-line match every backend must
    perform, in ``mcuhome-model`` because **both** backends perform it —
    the local build method against the image on this host, the build
    server against
    the images in its inventory — and two spellings of "this container
    serves 4.4" is how one of them starts accepting a container the other
    refuses.

    A line is a prefix of a release, component by component: ``4.4``
    is satisfied by ``4.4``, ``4.4.0`` and ``4.4.12``, and by nothing
    else — not by ``4.5.0``, and not by ``4.40.0``, whose leading
    component is a different number that merely starts with the same
    digits. That is the rule a line exists for: "a line, never
    a frozen point release — patch releases with security backports are
    always taken".

    A release carrying an upstream suffix (``4.5.0-rc1``) satisfies **no**
    line, including its own. As PEP 440 itself states of such a value, it
    is not ordered at all: it satisfies only ``=``, and it never satisfies
    a range — and a line is a range.
    A pre-release is a container an operator chose to build; asking for a
    line is asking for the released ones.

    A *version* that is not a Zephyr release at all — a label somebody
    filled in by hand, an empty string, a value with the leading ``v``
    west uses — satisfies nothing. Absence is never read as compatible,
    and neither is nonsense.
    """
    found = _RELEASE.fullmatch(version.strip()) if isinstance(version, str) else None
    asked = _RELEASE.fullmatch(line.strip()) if isinstance(line, str) else None
    if found is None or asked is None or found.group("suffix") or asked.group("suffix"):
        return False
    wanted = [int(part) for part in asked.group("numbers").split(".")]
    have = [int(part) for part in found.group("numbers").split(".")]
    if len(wanted) > len(have):
        return False
    return all(a == b for a, b in zip(wanted, have, strict=False))


def line_of(version: str) -> str | None:
    """Which line a build container carrying Zephyr *version* serves.

    The inverse of :func:`satisfies_line`, for the one job that needs it:
    **telling a client what this host can answer.** A backend that cannot
    serve a context reports what it *could* serve, and both the session
    protocol's ``version.builder-unsatisfiable`` amendment and the build
    server's own error table call those values "the lines available".
    They are read off ``org.mcuhome.build-environment.zephyr.version`` labels, and a label states a
    *release* (``docs/spec/build-environment-specification.md`` §5, §5.2)
    — so reporting the labels verbatim reports releases under the name of
    lines, and a client that echoed one back as its ``zephyr`` would pin
    a frozen point release (which the release-line policy forbids) or,
    for a pre-release, send a value no image can ever satisfy.

    So: ``4.4.12`` and ``4.4`` are both the ``4.4`` line, and
    ``4.5.0-rc1``, ``v4.5``, ``latest`` and ``""`` are ``None`` —
    exactly the values :func:`satisfies_line` matches against nothing.
    The invariant the two share, and the reason they live side by side:
    whenever this returns a line, ``satisfies_line(version, line=...)``
    over that line is true.
    """
    found = _RELEASE.fullmatch(version.strip()) if isinstance(version, str) else None
    if found is None or found.group("suffix"):
        return None
    return ".".join(found.group("numbers").split(".")[:2])


def normalize_release(version: str) -> str:
    """Strip west's leading ``v``, and no more of it than that.

    West states every pinned revision with a ``v`` no Zephyr release
    grammar carries: ``west list`` answers ``v4.4.0`` for what everywhere
    else — :func:`line_of`, :func:`satisfies_line`, the
    ``org.mcuhome.build-environment.zephyr.version`` label a build
    environment declares itself with — is spelled ``4.4.0``. An
    environment that does not carry a readable label does not qualify,
    and a label spelled ``v4.4.0`` satisfies no release's constraint at
    all, so the two spellings have to be reconciled in exactly one
    place.

    Exactly one leading ``v`` is dropped and nothing else about the value
    is touched: a second ``v`` is not west's doing and stays, and a value
    that never had one comes back unchanged. This is the one place that
    normalization happens, so that a build server reading a program's
    ``describe`` answer and this module's own release grammar agree on
    what a release looks like without a second implementation of the
    strip.
    """
    return version[1:] if version.startswith("v") else version


def available_blobs(board: str) -> dict[str, str]:
    """Blobs applicable to *board*, by name, with their source.

    The future blob-availability matrix hooks in here. It is empty today:
    MCUHome integrates no vendor blob yet, on any board.
    """
    del board
    return {}


def resolve_toolchain(
    *,
    board: str | None,
    blob_usage: str | None,
    zephyr_version: str | None,
    blobs: dict[str, str],
    blob_locs: dict[str, Location],
    version_loc: Location,
    errors: ErrorCollector,
) -> ResolvedToolchain:
    """Resolve the Zephyr line and blob set, or record why it cannot be."""
    resolved_usage = blob_usage or "auto"

    requested = zephyr_version or "auto"
    if requested in ("auto", "latest"):
        line = LATEST_ZEPHYR_LINE
    elif requested in SUPPORTED_ZEPHYR_LINES:
        line = requested
    else:
        errors.add(
            f"MCUHome cannot build against Zephyr {requested}.",
            location=version_loc,
            hint=(
                "this release builds against Zephyr "
                f"{', '.join(SUPPORTED_ZEPHYR_LINES)} only — remove the "
                "zephyr_version: line and let MCUHome choose:\n"
                "    zephyr_version: auto"
            ),
        )
        line = LATEST_ZEPHYR_LINE

    integrated = available_blobs(board or "")
    for name, decision in blobs.items():
        if decision != "enabled":
            # "disabled" and "auto" are honest no-ops while nothing is
            # integrated: nothing is being switched off, and "auto" is
            # defined to self-heal once a compatible blob appears.
            continue
        if name not in integrated:
            errors.add(
                f'MCUHome cannot enable the "{name}" binary blob: no vendor blob is '
                "integrated yet.",
                location=blob_locs.get(name, version_loc),
                hint=(
                    "blob support is still being evaluated — remove the "
                    f"blobs: entry for now:\n    # {name}: enabled"
                ),
            )

    # A device that named its line narrows the SDK's constraint rather
    # than replacing it: "the 4.4 line" and "what this SDK needs" are two
    # requirements, and a device may not talk its SDK out of the second.
    # Composed as text — evaluating it needs `packaging`, which this
    # package deliberately does not have.
    constraint = (
        ZEPHYR_CONSTRAINT
        if requested in ("auto", "latest")
        else (f"{ZEPHYR_CONSTRAINT},=={line}.*")
    )
    return ResolvedToolchain(
        zephyr_line=line,
        blob_usage=resolved_usage,
        blobs={},
        zephyr_constraint=constraint,
    )
