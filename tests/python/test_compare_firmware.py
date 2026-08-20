# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Telling "one stamp" apart from "everywhere" (``scripts/compare_firmware.py``).

The script is a **report** and never fails a build, which is exactly the
shape that can rot unnoticed: a comparison that silently finds nothing to
compare, or that calls two identical images different, would go on
printing forever and nobody would learn anything from it. So the cases
here are the ones its answer hangs on.

Loaded by path, the same way ``test_check_build_artifacts.py`` loads its
script: ``scripts/`` is not a package.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compare_firmware.py"


@pytest.fixture(scope="module")
def script():
    spec = importlib.util.spec_from_file_location("compare_firmware", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["compare_firmware"] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Intel HEX is an encoding, not the image
# --------------------------------------------------------------------------


def test_the_image_starts_at_its_own_lowest_address(script) -> None:
    """A bootloader linked high would otherwise decode to megabytes of padding.

    Found by running it: an extended linear address of 0x0800 gave a
    134 MB image for four bytes of program, because the fill started at
    zero. The base is answered separately instead — two images at
    different base addresses differ in a way no byte comparison names.
    """
    hexed = ":020000040800F2\n:0400000041424344B2\n:00000001FF\n"
    base, image = script.decode_intel_hex(hexed)
    assert base == 0x08000000
    assert bytes(image) == b"ABCD"


def test_a_gap_between_segments_is_erased_flash(script) -> None:
    """0xFF is what is physically there, and what a device would read."""
    hexed = ":0100000041BE\n:01001000 42".replace(" ", "") + "\n:00000001FF\n"
    _base, image = script.decode_intel_hex(hexed)
    assert image[0:1] == b"A"
    assert image[0x10:0x11] == b"B"
    assert set(image[1:0x10]) == {0xFF}


def test_a_hex_file_with_no_data_is_empty_rather_than_a_crash(script) -> None:
    assert script.decode_intel_hex(":00000001FF\n") == (0, bytearray())


# --------------------------------------------------------------------------
# One stamp, or everywhere
# --------------------------------------------------------------------------


def test_a_single_stamp_is_one_region(script) -> None:
    """The finding this script exists to make legible."""
    left = b"\x00" * 64 + b"zephyr-4.4.0 build 1" + b"\x00" * 64
    right = b"\x00" * 64 + b"zephyr-4.4.0 build 2" + b"\x00" * 64
    assert script.runs_of_difference(left, right) == [(83, 1)]


def test_two_stamps_far_apart_stay_two_regions(script) -> None:
    """Joining them would hide that there are two places, not one."""
    left = b"A" + b"\x00" * 100 + b"B"
    right = b"X" + b"\x00" * 100 + b"Y"
    assert len(script.runs_of_difference(left, right)) == 2


def test_bytes_that_differ_throughout_are_not_reported_as_thousands(script) -> None:
    """Adjacent differences are one region; that is what makes the count mean something."""
    assert script.runs_of_difference(b"\x00" * 4096, b"\xff" * 4096) == [(0, 4096)]


def test_identical_images_are_said_to_be_identical(script, tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    for side in ("a", "b"):
        (tmp_path / side / "firmware.bin").write_bytes(b"same")
    report = script.compare(
        tmp_path / "a" / "firmware.bin",
        tmp_path / "b" / "firmware.bin",
        labels=("amd64", "arm64"),
    )
    assert any("identical" in line for line in report)


def test_the_differing_region_is_shown_as_text(script, tmp_path) -> None:
    """A compiler stamp announces itself only if it is printed as characters."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "firmware.bin").write_bytes(b"built on host-alpha\x00\x00")
    (tmp_path / "b" / "firmware.bin").write_bytes(b"built on host-betaa\x00\x00")
    report = "\n".join(
        script.compare(
            tmp_path / "a" / "firmware.bin",
            tmp_path / "b" / "firmware.bin",
            labels=("amd64", "arm64"),
        )
    )
    assert "host-alpha" in report and "host-betaa" in report


# --------------------------------------------------------------------------
# The failure mode a report has: comparing nothing, quietly
# --------------------------------------------------------------------------


def test_two_directories_with_nothing_in_common_say_so(script, tmp_path, capsys) -> None:
    """The rot this file exists against: a green report that compared nothing."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "firmware.bin").write_bytes(b"x")
    assert script.main(["x", str(tmp_path / "a"), str(tmp_path / "b")]) == 0
    assert "nothing to compare" in capsys.readouterr().out


def test_a_difference_is_reported_and_never_fails(script, tmp_path, capsys) -> None:
    """What a difference means is a judgement about the toolchain, not this script's."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "firmware.bin").write_bytes(b"one")
    (tmp_path / "b" / "firmware.bin").write_bytes(b"two")
    assert script.main(["x", str(tmp_path / "a"), str(tmp_path / "b")]) == 0
    assert "bytes differ" in capsys.readouterr().out
