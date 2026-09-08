# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""How many compile jobs a machine sustains (``mcuhome/model/jobs.py``).

The number is resolved once, on the host, and handed down. These tests
pin the two halves of that: the hardware heuristic (cores against
available RAM) and the precedence ladder above it.
"""

from __future__ import annotations

import os

import pytest

from mcuhome.model import jobs

_GIB = 1024**3


@pytest.mark.parametrize(
    ("cpu_count", "available_gib", "expected"),
    [
        # This development machine: 4 cores, 15 GiB.
        (4, 15, 4),
        # A 24-thread/24-GiB WSL machine.
        (24, 24, 12),
        # Plenty of RAM, few cores: the CPU count is the ceiling.
        (2, 64, 2),
        # Plenty of cores, little RAM: the RAM budget is the ceiling.
        (16, 6, 3),
        # A single core: never ask for more than one job, however much RAM
        # the max(2, ...) floor would otherwise suggest.
        (1, 15, 1),
        # A RAM-starved multi-core machine still gets the floor of 2, not 0
        # or 1: max(2, ...) always wins over a floor-dividing-to-zero RAM
        # budget.
        (8, 1, 2),
    ],
)
def test_auto_jobs_boundary_cases(cpu_count: int, available_gib: int, expected: int) -> None:
    assert jobs.auto_jobs(cpu_count, available_gib * _GIB) == expected


def test_available_ram_reads_memavailable(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:       16384000 kB\nMemAvailable:    8192000 kB\nSwapTotal:             0 kB\n",
        "utf-8",
    )
    assert jobs.available_ram_bytes(meminfo) == 8192000 * 1024


def test_available_ram_falls_back_to_half_of_memtotal_without_memavailable(tmp_path) -> None:
    """An old kernel's /proc/meminfo has MemTotal but not MemAvailable."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16384000 kB\n", "utf-8")
    assert jobs.available_ram_bytes(meminfo) == (16384000 * 1024) // 2


def test_available_ram_is_zero_when_meminfo_has_neither_key(tmp_path) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("VmallocTotal:   34359738367 kB\n", "utf-8")
    assert jobs.available_ram_bytes(meminfo) == 0


def test_available_ram_is_zero_without_proc_meminfo_at_all(tmp_path) -> None:
    """Non-Linux, or any other reason the file just is not there."""
    assert jobs.available_ram_bytes(tmp_path / "does-not-exist") == 0


def test_detect_jobs_wires_cpu_count_and_available_ram_together(monkeypatch) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    monkeypatch.setattr(jobs, "available_ram_bytes", lambda: 12 * _GIB)
    assert jobs.detect_jobs() == 6


def test_detect_jobs_survives_an_unknown_cpu_count(monkeypatch) -> None:
    """`os.cpu_count()` returns None where the count is indeterminable."""
    monkeypatch.setattr(os, "cpu_count", lambda: None)
    monkeypatch.setattr(jobs, "available_ram_bytes", lambda: 64 * _GIB)
    assert jobs.detect_jobs() == 1


def test_a_stated_job_count_beats_everything() -> None:
    """A caller that resolved the number itself is answered with it."""
    resolved = jobs.resolve_jobs(cli_jobs=3, limits=jobs.BuildLimits(cpus=16))
    assert (resolved.value, resolved.source) == (3, "flag")


def test_the_recommended_limits_beat_auto_detection(monkeypatch) -> None:
    """What the orchestrator said the step should fit in is what the step
    plans with — the machine's own answer is about a machine this build
    was not given all of."""
    monkeypatch.setattr(jobs, "detect_jobs", lambda: pytest.fail("auto-detection ran"))
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    monkeypatch.setattr(jobs, "available_ram_bytes", lambda: 64 * _GIB)
    resolved = jobs.resolve_jobs(limits=jobs.BuildLimits(cpus=6, memory_bytes=64 * _GIB))
    assert (resolved.value, resolved.source) == (6, "limits")


def test_limits_that_state_nothing_are_the_same_as_none(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "detect_jobs", lambda: 5)
    assert jobs.resolve_jobs(limits=jobs.BuildLimits()).source == "auto"
    assert jobs.resolve_jobs().value == 5


def test_the_memory_limit_is_divided_by_the_same_budget_auto_detection_uses() -> None:
    """A build sized from a limit and a build sized from the machine
    differ in where the figure came from and in nothing else."""
    limits = jobs.BuildLimits(memory_bytes=8 * _GIB)
    assert limits.jobs(cpu_count=16, available=64 * _GIB) == 4
    assert jobs.auto_jobs(16, 8 * _GIB) == 4


def test_a_fractional_cpu_limit_rounds_down() -> None:
    """``--cpus 1.5`` is one and a half cores' worth of time; half a core
    is not half a compile, so the ceiling is one — and the floor of two
    that every other path has still applies."""
    assert jobs.BuildLimits(cpus=1.5).jobs(cpu_count=16, available=64 * _GIB) == 1
    assert jobs.BuildLimits(cpus=3.9).jobs(cpu_count=16, available=64 * _GIB) == 3


def test_the_machine_answers_for_whichever_figure_was_not_stated() -> None:
    """An orchestrator that can bound the memory but not the CPU states
    the one it means, and the other is the machine's.

    A limit only ever narrows: a step told it may use more than the
    machine has is still a step on that machine.
    """
    # Memory limited far above what the machine has: the machine decides.
    assert jobs.BuildLimits(memory_bytes=64 * _GIB).jobs(cpu_count=8, available=6 * _GIB) == 3
    # CPU limited above the machine's count: the machine decides again.
    assert jobs.BuildLimits(cpus=99).jobs(cpu_count=8, available=6 * _GIB) == 3
    # And the limit decides wherever it is the narrower of the two.
    assert jobs.BuildLimits(memory_bytes=4 * _GIB).jobs(cpu_count=8, available=64 * _GIB) == 2


def test_a_limits_member_that_is_not_a_positive_number_is_read_as_absent() -> None:
    """The soft copy of a limit is not worth failing a step over: what
    actually holds is enforced from outside, and refusing here would turn
    a cosmetic bug into a failed build."""
    assert jobs.BuildLimits.from_document(None) == jobs.BuildLimits()
    assert jobs.BuildLimits.from_document({"cpus": "four"}) == jobs.BuildLimits()
    assert jobs.BuildLimits.from_document({"cpus": 0, "memory_bytes": -1}) == jobs.BuildLimits()
    assert jobs.BuildLimits.from_document({"cpus": True}) == jobs.BuildLimits()
    assert jobs.BuildLimits.from_document({"cpus": 2.5}) == jobs.BuildLimits(cpus=2.5)


def test_the_document_form_states_only_what_was_limited() -> None:
    assert jobs.BuildLimits().to_dict() == {}
    assert jobs.BuildLimits(cpus=2).to_dict() == {"cpus": 2}
    assert jobs.BuildLimits(cpus=2, memory_bytes=17).to_dict() == {
        "cpus": 2,
        "memory_bytes": 17,
    }
