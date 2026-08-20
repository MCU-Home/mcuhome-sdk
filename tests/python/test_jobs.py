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


def test_a_command_line_flag_beats_everything() -> None:
    """The environment is passed in, so it has to be passed in here too.

    This test used to `monkeypatch.setenv` and hand `resolve_jobs` an
    empty environment, which stopped meaning anything once `env` became
    a required argument: it proved "flag beats auto-detection", which the
    next test over already covers, under a name promising more.
    """
    resolved = jobs.resolve_jobs(env={jobs.JOBS_VAR: "6"}, cli_jobs=3)
    assert (resolved.value, resolved.source) == (3, "flag")


def test_the_environment_variable_beats_auto_detection(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "detect_jobs", lambda: pytest.fail("auto-detection ran"))
    resolved = jobs.resolve_jobs(env={jobs.JOBS_VAR: "6"})
    assert (resolved.value, resolved.source) == (6, "env")


def test_neither_flag_nor_environment_falls_back_to_auto_detection(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "detect_jobs", lambda: 5)
    resolved = jobs.resolve_jobs(env={})
    assert (resolved.value, resolved.source) == (5, "auto")


def test_a_nonsense_environment_value_is_treated_as_unset(monkeypatch) -> None:
    """A typo in a shell rc file falls back to auto rather than breaking every build."""
    monkeypatch.setattr(jobs, "detect_jobs", lambda: 5)
    resolved = jobs.resolve_jobs(env={jobs.JOBS_VAR: "not-a-number"})
    assert (resolved.value, resolved.source) == (5, "auto")


def test_a_zero_environment_value_is_also_treated_as_unset(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "detect_jobs", lambda: 5)
    resolved = jobs.resolve_jobs(env={jobs.JOBS_VAR: "0"})
    assert (resolved.value, resolved.source) == (5, "auto")
