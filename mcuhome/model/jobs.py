# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""How many compile jobs this machine can sustain, and why that number.

Vocabulary rather than machinery, which is why it sits here: *both* ends
of a build need it and neither owns it. A command line resolves the
number once, on the host, before anything starts — the host is where the
RAM budget is knowable — and hands it down; the program inside a build
container takes the resolved figure as given and only falls back to
resolving it itself when nobody stated one.

It used to live in the compiler, beside the west invocation that consumes
it, and that was one edge too many: a host that drives a build container
needs the number and must not need a toolchain to learn it.

Nothing here reads the process environment (:mod:`mcuhome.model.userpaths`
says why). It reads ``/proc/meminfo``, which is a fact about the machine
rather than about a caller.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "JOBS_VAR",
    "ResolvedJobs",
    "auto_jobs",
    "available_ram_bytes",
    "detect_jobs",
    "resolve_jobs",
]

#: Environment variable that overrides job-count auto-detection outright
#: (see :func:`resolve_jobs`) — the escape hatch for a machine
#: :func:`auto_jobs` still guesses wrong for, e.g. a container with a
#: cgroup memory limit ``/proc/meminfo`` does not reflect. ``--jobs`` on
#: the command line beats it; it beats auto-detection.
JOBS_VAR = "MCUHOME_JOBS"

_GIB = 1024**3

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

    ``min(cpu_count, max(2, available_ram_gb // 2))``. Measured CHIP C++
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
    ram_gb = available_ram_bytes // _GIB
    return min(cpu_count, max(2, ram_gb // 2))


def detect_jobs() -> int:
    """:func:`auto_jobs`, fed this machine's live CPU count and free RAM."""
    return auto_jobs(os.cpu_count() or 1, available_ram_bytes())


@dataclass(frozen=True)
class ResolvedJobs:
    """A job count together with why it was chosen — for the build summary."""

    value: int
    #: ``"flag"`` (``--jobs``), ``"env"`` (:data:`JOBS_VAR`), or ``"auto"``
    #: (:func:`detect_jobs`).
    source: str


def resolve_jobs(*, env: dict[str, str], cli_jobs: int | None = None) -> ResolvedJobs:
    """The parallelism this build uses, and why — the single resolution point.

    Precedence, most specific wins: ``--jobs`` on the command line, then
    :data:`JOBS_VAR` in the environment, then :func:`detect_jobs`. This
    runs once, on the host, before a container build even starts docker —
    the container would see the host's CPU count either way, but its RAM
    budget is the host's (or the WSL VM's), not a figure guessed at from
    inside a container that may itself be memory-limited by a cgroup.
    Everything downstream then takes the resulting number as given rather
    than resolving it again.

    A :data:`JOBS_VAR` that is not a positive whole number is treated as
    unset rather than refused: a typo in a shell rc file should not be
    able to break every build until someone finds it, and auto-detection
    is always a reasonable answer.

    *env* is stated, never read from the process: one process serves
    several sessions, and "the environment" of a server is the operator's
    rather than any requesting user's. The command line passes its own.
    """
    if cli_jobs is not None:
        return ResolvedJobs(cli_jobs, "flag")
    raw = env.get(JOBS_VAR)
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed >= 1:
            return ResolvedJobs(parsed, "env")
    return ResolvedJobs(detect_jobs(), "auto")
