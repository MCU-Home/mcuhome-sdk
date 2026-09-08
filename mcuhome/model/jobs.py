# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""How many compile jobs a build can sustain, and out of what.

Vocabulary rather than machinery, which is why it sits here: *both* ends
of a build need it and neither owns it. The orchestrator states what a
step should keep itself within — the build environment specification's
``limits`` in the request document, a CPU figure and a memory figure —
and the builder sizes its parallelism from that. Where nothing was
stated, the machine answers for itself.

**The limits are a recommendation and the parallelism is derived from
them.** The orchestrator enforces its own hard limits from outside
(a container's ``--cpus``/``--memory``), so a build that planned with
what the machine appears to have rather than with what it was given is
the build that gets killed. Nothing here enforces anything: it turns two
numbers into a job count.

Nothing here reads the process environment
(:mod:`mcuhome.model.userpaths` says why), and nothing takes a job count
out of one: limits travel in the request document and nowhere else. It
reads ``/proc/meminfo``, which is a fact about the machine rather than
about a caller.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "BYTES_PER_JOB",
    "BuildLimits",
    "ResolvedJobs",
    "auto_jobs",
    "available_ram_bytes",
    "detect_jobs",
    "resolve_jobs",
]

_GIB = 1024**3

#: What one compile job is budgeted. Measured CHIP C++ compiles peak
#: around 1-1.5 GiB; the final link spikes higher, but only one link runs
#: at a time, so it does not change the per-job budget. Stated here
#: because :func:`auto_jobs` and a stated memory limit have to divide by
#: the same number — a build sized from a limit and a build sized from
#: the machine differ in where the figure came from and in nothing else.
BYTES_PER_JOB = 2 * _GIB

#: :func:`available_ram_bytes` rather than a hardcoded path only inside
#: it, so the test suite can point it at a fixture file instead of
#: monkeypatching the standard library.
_MEMINFO_PATH = Path("/proc/meminfo")


def available_ram_bytes(path: Path | None = None) -> int:
    """Best-effort available RAM right now, without a psutil dependency.

    Reads ``MemAvailable`` from ``/proc/meminfo`` — Linux (kernel >= 3.14)
    already discounts reclaimable page cache from it, which is closer to
    "usable before swapping starts" than ``MemFree``. Where that key is
    missing — an old kernel, or *path* pointing nowhere, which is every
    non-Linux platform — this falls back to half of ``MemTotal``, a rough
    "assume something else already claimed half of it" heuristic that
    needs no new dependency. Where neither key is readable at all, this
    assumes nothing is available, which drives :func:`auto_jobs` to its
    floor of 2 rather than guessing high on a machine it cannot see.
    """
    meminfo = _MEMINFO_PATH if path is None else path
    try:
        text = meminfo.read_text("utf-8")
    except OSError:
        text = ""
    values: dict[str, int] = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        if name not in ("MemAvailable", "MemTotal"):
            continue
        fields = rest.strip().split()
        if fields and fields[0].isdigit():
            values[name] = int(fields[0])  # kB, per proc(5)
    if "MemAvailable" in values:
        return values["MemAvailable"] * 1024
    if "MemTotal" in values:
        return (values["MemTotal"] * 1024) // 2
    return 0


def auto_jobs(cpu_count: int, available_ram_bytes: int) -> int:
    """Parallelism this hardware can sustain without swapping.

    ``min(cpu_count, max(2, available_ram_bytes // BYTES_PER_JOB))``. Measured CHIP C++
    compiles peak around 1-1.5 GiB per job; the final link spikes higher,
    but only one link runs at a time (ninja serializes it), so it does not
    change the per-job budget. Budgeting 2 GiB per job keeps a no-swap
    machine safe with headroom, and ``cpu_count`` remains the hard ceiling
    underneath that — more jobs than cores never builds faster. This
    development machine (4 cores / 15 GiB) resolves to 4; a 24-thread /
    24 GiB WSL machine resolves to 12. The floor of 2 matches the
    previous static default, so even a RAM-starved machine can still
    overlap one compile with the next.

    :param cpu_count: usually ``os.cpu_count()``.
    :param available_ram_bytes: usually :func:`available_ram_bytes`.
    """
    return min(cpu_count, max(2, available_ram_bytes // BYTES_PER_JOB))


def detect_jobs() -> int:
    """:func:`auto_jobs`, fed this machine's live CPU count and free RAM."""
    return auto_jobs(os.cpu_count() or 1, available_ram_bytes())


@dataclass(frozen=True)
class BuildLimits:
    """What a step is expected to fit in: the request document's ``limits``.

    Both members are optional and each is read on its own — an
    orchestrator that can bound the memory but not the CPU states the one
    it means. Absent throughout is a limits object that says nothing,
    which is the same as none at all.

    :attr:`cpus` is a number of cores and may be fractional, exactly as
    ``docker run --cpus`` is: ``1.5`` is one and a half cores' worth of
    time. A job count derived from it rounds **down** — half a core is
    not half a compile.
    """

    cpus: float | None = None
    memory_bytes: int | None = None

    @property
    def stated(self) -> bool:
        """Whether anything was limited at all."""
        return self.cpus is not None or self.memory_bytes is not None

    def to_dict(self) -> dict[str, float | int]:
        """The document form: only the members that were stated."""
        document: dict[str, float | int] = {}
        if self.cpus is not None:
            document["cpus"] = self.cpus
        if self.memory_bytes is not None:
            document["memory_bytes"] = self.memory_bytes
        return document

    @staticmethod
    def from_document(value: Any) -> BuildLimits:
        """Read ``limits`` out of a request document, tolerantly.

        A member that is not a positive number is read as absent rather
        than refused. This is the recommendation, not the enforcement: an
        orchestrator that writes nonsense into it has already set the
        hard limits that actually hold, and refusing the step over the
        soft copy would turn a cosmetic bug into a failed build.
        """
        if not isinstance(value, dict):
            return BuildLimits()
        return BuildLimits(
            cpus=_positive_number(value.get("cpus")),
            memory_bytes=_positive_int(value.get("memory_bytes")),
        )

    def jobs(self, *, cpu_count: int | None = None, available: int | None = None) -> int:
        """The parallelism these limits allow, on this machine.

        The same arithmetic :func:`auto_jobs` does, with what was stated
        standing in for what the machine has: the CPU figure rounded down
        is the ceiling, the memory figure divided by :data:`BYTES_PER_JOB`
        is the budget, and the machine answers for whichever of the two
        was not stated.
        """
        cores = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
        memory = available if available is not None else available_ram_bytes()
        if self.cpus is not None:
            cores = min(cores, int(self.cpus))
        if self.memory_bytes is not None:
            memory = min(memory, self.memory_bytes)
        return auto_jobs(max(1, cores), memory)


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class ResolvedJobs:
    """A job count together with why it was chosen — for the build summary."""

    value: int
    #: ``"flag"`` (a job count a caller stated outright), ``"limits"``
    #: (derived from the request document's ``limits``), or ``"auto"``
    #: (:func:`detect_jobs`).
    source: str


def resolve_jobs(*, cli_jobs: int | None = None, limits: BuildLimits | None = None) -> ResolvedJobs:
    """The parallelism this build uses, and why — the single resolution point.

    Precedence, most specific wins: a job count a caller stated outright,
    then the limits the orchestrator recommended, then this machine's own
    answer. Nothing is read from the environment: the limits travel in
    the request document, which is the one place the specification puts
    them.

    Limits that state neither figure are the same as none — an
    orchestrator that wrote an empty object said nothing about the
    machine, and the machine can still answer for itself.
    """
    if cli_jobs is not None:
        return ResolvedJobs(cli_jobs, "flag")
    if limits is not None and limits.stated:
        return ResolvedJobs(limits.jobs(), "limits")
    return ResolvedJobs(detect_jobs(), "auto")
