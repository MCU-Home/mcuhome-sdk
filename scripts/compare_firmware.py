#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Two builds of one device, compared byte for byte — and told apart.

CI builds the reference device on both architectures the build
environment is published for (``ubuntu-latest`` and ``ubuntu-24.04-arm``)
and the two images are **not** identical. That is a fact and not yet an
answer: "they differ in one build id" and "they differ everywhere" are
entirely different findings behind the same failed hash comparison, and
only looking at the bytes tells them apart.

So this prints *where* and *what*: how many bytes differ, in how many
contiguous runs, at which offsets, and what is actually there — as text
where the region is text, which is how a compiler stamp, a path or a
timestamp announces itself.

**Intel HEX is decoded rather than diffed as text.** A ``.hex`` file is
an address-and-checksum encoding of a program image; comparing the text
would report the record layout as a difference and would hide a real one
inside a re-flowed line. What is compared is the image the records
describe.

**Unsigned artifacts only** — the caller passes those. A signature is not
reproducible by construction (ECDSA draws a fresh nonce per signature),
so a signed image differs between two runs of the *same* architecture and
would drown the question this script exists to answer.

Usage::

    compare_firmware.py <left-dir> <right-dir> [--label-left A] [--label-right B]

It is a **report**: it exits 0 whether or not it found differences. What
a difference means is a judgement about the toolchain, and one this
script is in no position to make.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: How many differing runs are shown in full. A build that differs in
#: three places is worth reading; one that differs in three thousand has
#: already answered the question, and the rest is scroll.
MAX_RUNS_SHOWN = 12

#: Bytes of context printed on each side of a differing run, so that a
#: stamp is readable together with what surrounds it.
CONTEXT = 16

#: A run of differing bytes is reported as one when the gap between two
#: differences is smaller than this. Without it, "GCC 14.2" against
#: "GCC 14.3" reads as two runs because the space between them matches.
JOIN_GAP = 8


def decode_intel_hex(text: str) -> tuple[int, bytearray]:
    """The program image an Intel HEX file describes: ``(base, bytes)``.

    Only the record types this format really uses here: data (00), end of
    file (01), extended segment address (02) and extended linear address
    (04). A start-address record (03/05) says where execution begins and
    contributes no image bytes. An unknown record type is skipped rather
    than guessed at.

    Gaps between segments are filled with 0xFF — the erased state of NOR
    flash, and what the gap physically is on the device.

    **The image starts at its own lowest address, not at zero.** A
    bootloader linked at 0x08000000 would otherwise decode to 128 MB of
    padding in front of a few kilobytes of program, which is a memory
    accident rather than a comparison. The base is answered alongside so
    a caller can see it — two images at different base addresses are
    different in a way no byte comparison would name.
    """
    records: list[tuple[int, bytes]] = []
    base = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        raw = bytes.fromhex(line[1:])
        count, kind = raw[0], raw[3]
        offset = int.from_bytes(raw[1:3], "big")
        payload = raw[4 : 4 + count]
        if kind == 0x00:
            records.append((base + offset, payload))
        elif kind == 0x02:
            base = int.from_bytes(payload, "big") * 16
        elif kind == 0x04:
            base = int.from_bytes(payload, "big") << 16
        elif kind == 0x01:
            break
    if not records:
        return 0, bytearray()
    lowest = min(address for address, _payload in records)
    image = bytearray()
    for address, payload in records:
        at = address - lowest
        if at > len(image):
            image.extend(b"\xff" * (at - len(image)))
        image[at : at + len(payload)] = payload
    return lowest, image


def load(path: Path) -> tuple[int, bytearray]:
    """One artifact as ``(base address, bytes a device would see)``."""
    if path.suffix == ".hex":
        return decode_intel_hex(path.read_text(encoding="ascii", errors="replace"))
    return 0, bytearray(path.read_bytes())


def runs_of_difference(left: bytes, right: bytes) -> list[tuple[int, int]]:
    """``(offset, length)`` per contiguous region where the two differ.

    Compared over the shorter length; a length difference is reported by
    the caller, because "one image is longer" is a different statement
    from "these bytes differ".
    """
    found: list[tuple[int, int]] = []
    start: int | None = None
    last = 0
    for index in range(min(len(left), len(right))):
        if left[index] != right[index]:
            if start is None:
                start = index
            elif index - last > JOIN_GAP:
                found.append((start, last - start + 1))
                start = index
            last = index
    if start is not None:
        found.append((start, last - start + 1))
    return found


def _printable(data: bytes) -> str:
    return "".join(chr(byte) if 0x20 <= byte < 0x7F else "." for byte in data)


def describe_run(left: bytes, right: bytes, offset: int, length: int) -> list[str]:
    """One differing run, on both sides, with its neighbourhood as text."""
    start = max(0, offset - CONTEXT)
    end = min(len(left), len(right), offset + length + CONTEXT)
    lines = [f"  at 0x{offset:06x}, {length} byte(s):"]
    for label, data in (("left ", left), ("right", right)):
        window = bytes(data[start:end])
        lines.append(f"    {label} hex   {window[:48].hex(' ')}")
        lines.append(f"    {label} text  {_printable(window)}")
    return lines


def compare(left: Path, right: Path, *, labels: tuple[str, str]) -> list[str]:
    """The report for one artifact, as lines."""
    (one_base, one), (other_base, other) = load(left), load(right)
    report = [f"{left.name}:"]
    if one_base != other_base:
        report.append(
            f"  base addresses differ: {labels[0]} 0x{one_base:08x}, {labels[1]} 0x{other_base:08x}"
        )
    if len(one) != len(other):
        report.append(f"  sizes differ: {labels[0]} {len(one)} B, {labels[1]} {len(other)} B")
    if one == other:
        report.append(f"  identical ({len(one)} bytes)")
        return report

    found = runs_of_difference(one, other)
    changed = sum(length for _offset, length in found)
    span = min(len(one), len(other))
    report.append(
        f"  {changed} of {span} bytes differ ({changed / span:.4%}), in {len(found)} region(s)"
    )
    for offset, length in found[:MAX_RUNS_SHOWN]:
        report += describe_run(one, other, offset, length)
    if len(found) > MAX_RUNS_SHOWN:
        report.append(f"  … and {len(found) - MAX_RUNS_SHOWN} more region(s)")
    return report


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--label-left", default="left")
    parser.add_argument("--label-right", default="right")
    args = parser.parse_args(argv[1:])

    names = sorted(
        path.name
        for path in args.left.iterdir()
        if path.is_file() and (args.right / path.name).is_file()
    )
    if not names:
        print(f"nothing to compare: no file is in both {args.left} and {args.right}")
        return 0

    labels = (args.label_left, args.label_right)
    print(f"comparing {labels[0]} against {labels[1]}\n")
    identical = 0
    for name in names:
        lines = compare(args.left / name, args.right / name, labels=labels)
        identical += any(line.strip().startswith("identical") for line in lines)
        print("\n".join(lines))
        print()
    print(f"{identical} of {len(names)} artifact(s) identical")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
