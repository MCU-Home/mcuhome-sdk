#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Assert that a finished build left the flashable files behind, well-formed.

The gate behind the Matter build job in ``.github/workflows/ci.yml``: a
build that exits 0 but leaves no flashable image behind is a worse result
than a build that fails, because nothing downstream notices. This script
turns "the compiler was happy" into "the things a device needs exist and
are non-empty, and the build's own description of itself is well-formed".

Usage::

    check_build_artifacts.py <build-dir>

where ``<build-dir>`` is what ``mcuhome build --build-dir`` was given.

**One build shape.** ``mcuhome device build`` compiles in a build
container and delivers ``build-report.json`` (the §7.2.1 report) beside
the unsigned artifacts, which the host then signs. The artifact set is
flat: ``firmware.{hex,bin}`` (unsigned), ``bootloader.hex`` when the build
produced one, ``firmware.signed.{hex,bin}`` (host-signed) and one
``<device>-<version>.ota`` wrapped from the signed image.

**Why the artifacts are checked by presence, not by hash.** The §7.2.1
report carries no per-artifact hash list, so there is no recorded value to
re-hash against. There is also no need for one here: the build container
already re-hashed every artifact on egress against the report it emits
(build-container-contract §5.3), so a second hash oracle in CI would only
re-check what the container already guaranteed. CI's job here is therefore
narrow and exactly right — "the flashable files exist and are non-empty,
and the report is a well-formed §7.2.1 document" — which is what catches a
build that silently produced nothing, or a report a signer would refuse.

What is required (each present and non-empty):

* ``build-report.json`` — and it must parse, carry ``report`` == 1, a
  ``signing`` block whose ``signature_type`` is ``ecdsa-p256`` and whose
  ``arguments`` is an object holding all four imgtool keys (``version``,
  ``header-size``, ``align``, ``slot-size``). Without it a detached signer
  has nothing to read, and §7.2 requires exactly one report.
* ``firmware.hex`` and ``firmware.bin`` — the unsigned firmware the
  container declared with role ``firmware`` (§7.2).
* ``firmware.signed.hex`` and ``firmware.signed.bin`` — what the host
  signer produced; an unsigned application is one MCUboot refuses to
  chain-load.
* exactly one ``*.ota`` — the Matter update image wrapped from the signed
  binary.
* ``bootloader.hex`` is checked non-empty *when present*, but a missing
  bootloader is not a failure by itself: §7.2 requires at least one
  firmware artifact and exactly one report, and makes the bootloader
  optional (a board without an MCUboot member, a build that does not
  deliver one).

The image and file names below are written out rather than imported from
the code under test: this is an independent oracle for a regression gate,
and one that imported its expectations from the code that produced the
artifacts would follow that code silently wherever it went. Standard
library only, for the same reason.

Exit status: 0 when the artifact set is complete, 1 with findings (one
per line), 2 on usage errors.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Delivered beside the unsigned firmware: the §7.2.1 report a host signer
#: consumes, and what says a build directory is a finished one.
BUILD_REPORT_FILE = "build-report.json"

#: The one report format version this gate understands (§7.2.1).
REPORT_VERSION = 1

#: The one signature algorithm MCUHome images carry
#: (``mcuhome.model.registry.SIGNATURE_TYPE``).
SIGNATURE_TYPE = "ecdsa-p256"

#: imgtool's own option names, the keys a §7.2.1 ``signing.arguments``
#: object must carry so a detached signer can turn it back into a command
#: (``mcuhome.model.signing.SigningParameters.to_dict``).
SIGNING_ARGUMENT_KEYS = ("version", "header-size", "align", "slot-size")

#: The flashable files the container path must leave behind: the unsigned
#: firmware in both encodings, and the host-signed forms of each.
REPORT_REQUIRED_FILES = (
    "firmware.hex",
    "firmware.bin",
    "firmware.signed.hex",
    "firmware.signed.bin",
)

#: The bootloader image, when the build delivered one. Optional: §7.2
#: requires firmware + report, not a bootloader.
BOOTLOADER_FILE = "bootloader.hex"

#: The Matter update image, wrapped from the signed binary and named
#: ``<device>-<version>.ota`` — matched by suffix, of which there is one.
OTA_GLOB = "*.ota"


def _nonempty(path: Path, *, what: str) -> list[str]:
    """A file that must exist and hold bytes, or the finding that says so."""
    if not path.is_file():
        return [f"{what} is missing"]
    if path.stat().st_size == 0:
        return [f"{what} is empty"]
    return []


def _check_report_document(report_path: Path) -> list[str]:
    """The §7.2.1 ``build-report.json`` itself: parses, version, signing block."""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"{BUILD_REPORT_FILE} is not readable JSON: {error}"]
    if not isinstance(report, dict):
        return [f"{BUILD_REPORT_FILE} does not describe a build"]

    findings: list[str] = []
    version = report.get("report")
    if version != REPORT_VERSION:
        findings.append(
            f"{BUILD_REPORT_FILE} is report format version {version!r}, not {REPORT_VERSION}"
        )

    signing = report.get("signing")
    if not isinstance(signing, dict):
        findings.append(f"{BUILD_REPORT_FILE} carries no signing block")
        return findings

    signature_type = signing.get("signature_type")
    if signature_type != SIGNATURE_TYPE:
        findings.append(
            f"{BUILD_REPORT_FILE} signs with signature_type {signature_type!r}, "
            f"not {SIGNATURE_TYPE!r}"
        )

    arguments = signing.get("arguments")
    if not isinstance(arguments, dict):
        findings.append(f"{BUILD_REPORT_FILE} signing.arguments is not an object")
    else:
        missing = [key for key in SIGNING_ARGUMENT_KEYS if key not in arguments]
        if missing:
            findings.append(
                f"{BUILD_REPORT_FILE} signing.arguments is missing {', '.join(missing)}"
            )
    return findings


def check_report(out_dir: Path) -> list[str]:
    """Every finding about a container-path build directory."""
    findings = _check_report_document(out_dir / BUILD_REPORT_FILE)

    for name in REPORT_REQUIRED_FILES:
        findings += _nonempty(out_dir / name, what=name)

    bootloader = out_dir / BOOTLOADER_FILE
    if bootloader.is_file() and bootloader.stat().st_size == 0:
        findings.append(f"{BOOTLOADER_FILE} is present but empty")

    otas = sorted(out_dir.glob(OTA_GLOB))
    if not otas:
        findings.append("no .ota image: the build wrapped none from the signed firmware")
    elif len(otas) > 1:
        names = ", ".join(path.name for path in otas)
        findings.append(f"expected exactly one .ota image, found {len(otas)}: {names}")
    else:
        findings += _nonempty(otas[0], what=otas[0].name)
    return findings


def describe_report(out_dir: Path) -> str:
    """One line naming what was checked in a container-path build directory."""
    otas = sorted(out_dir.glob(OTA_GLOB))
    ota = otas[0].name if otas else "no .ota"
    present = [
        name for name in (BOOTLOADER_FILE, *REPORT_REQUIRED_FILES) if (out_dir / name).is_file()
    ]
    return f"{ota}: {', '.join(present)}"


# --- dispatch -----------------------------------------------------------


def check(out_dir: Path) -> list[str]:
    """Every finding about *out_dir*."""
    if not (out_dir / BUILD_REPORT_FILE).is_file():
        return [f"{BUILD_REPORT_FILE} is not in {out_dir}: this is not a finished build directory"]
    return check_report(out_dir)


def describe(out_dir: Path) -> str:
    """One line naming what was checked, for the log."""
    return describe_report(out_dir)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <build-dir>", file=sys.stderr)
        return 2
    out_dir = Path(argv[1]).resolve()

    findings = check(out_dir)
    if not findings:
        print(f"Artifact set complete in {out_dir}")
        print(f"  {describe(out_dir)}")
        return 0

    print(f"Incomplete build output in {out_dir}:\n")
    for finding in findings:
        print(f"  {finding}")
    print(
        "\nA build that exits 0 without a complete artifact set is a failure "
        "nothing downstream would notice: mcuhome sign, the OTA wrapper and "
        "every flashing path expect the files a finished build names to be there."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
