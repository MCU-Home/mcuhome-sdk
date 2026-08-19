# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The imgtool parameters a finished build has to be signed with.

Three of the four ``imgtool sign`` arguments come from the registry —
ADR 0015 decision 2 makes the partition table per-board data, and the
generator is what wrote it into the overlay — and the fourth from the
application image's own Kconfig, because imgtool's ``--version`` is the
one parameter the generator does not itself decide.

That is a *contract* rather than a report: the host signs afterwards
(ADR 0015 decision 8) and it must sign against the offsets the image was
actually linked with, so the header offset is cross-checked here and a
mismatch is a refusal instead of firmware that builds and does not boot.

:mod:`mcuhome.compiler.abi` puts what comes out of here into the §7.2.1
build report, which is the one description of a finished build there is.
"""

from __future__ import annotations

from pathlib import Path

from mcuhome.compiler import workspace
from mcuhome.model import registry
from mcuhome.model.errors import BuildError
from mcuhome.model.signing import SigningParameters

__all__ = [
    "HEADER_SIZE_SYMBOL",
    "KCONFIG_FILE",
    "SIGN_VERSION_DEFAULT",
    "SIGN_VERSION_SYMBOL",
    "kconfig_path",
    "read_kconfig",
    "signing_parameters",
]

#: Kconfig symbol carrying imgtool's ``--version`` on the application
#: image, and the value Zephyr defaults it to. Read from the built image
#: rather than assumed, because it is the one of the four parameters the
#: builder does not itself decide.
SIGN_VERSION_SYMBOL = "CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION"
SIGN_VERSION_DEFAULT = "0.0.0+0"

#: Kconfig symbol carrying the MCUboot header offset the application was
#: linked with — imgtool's ``--header-size``. Cross-checked against the
#: registry rather than trusted blindly: a disagreement between what the
#: builder thinks the layout is and what the image was actually linked
#: for is exactly the kind of silent mismatch that produces firmware
#: which builds, flashes and then does not boot.
HEADER_SIZE_SYMBOL = "CONFIG_ROM_START_OFFSET"

#: The built application's Kconfig, next to its artifacts. Sysbuild's
#: output layout itself is :data:`mcuhome.compiler.workspace.IMAGE_OUTPUT_DIR`, so
#: this module states the one file it reads there and nothing else.
KCONFIG_FILE = ".config"


def kconfig_path(build_dir: Path, app_image: str) -> Path:
    """Where the built application left the ``.config`` this module reads."""
    return workspace.image_output(build_dir, app_image) / KCONFIG_FILE


def read_kconfig(path: Path) -> dict[str, str]:
    """A Zephyr ``.config`` as ``symbol -> value``, quotes stripped.

    Deliberately not a Kconfig parser: the file is already the *result*
    of one, one assignment per line, and the builder reads two symbols
    out of it.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        if not line.startswith("CONFIG_") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip('"')
    return values


def _as_int(text: str, fallback: int) -> int:
    try:
        return int(text, 0)
    except (TypeError, ValueError):
        return fallback


def signing_parameters(
    scheme: registry.UpdateSchemeDef, *, kconfig: dict[str, str] | None = None
) -> SigningParameters:
    """The arguments the inline build signs with, from board data.

    Three of the four are the registry's: the header offset and the write
    alignment are properties of the part, and the slot size is the
    partition table ADR 0015 decision 2 makes per-board data — the same
    table the builder rendered into the overlay this image was linked
    against, which is why it can state them without asking the build.

    *kconfig* is the built application's ``.config`` when there is one.
    It carries imgtool's ``--version``, which the builder does not decide,
    and it is where the header offset is cross-checked: a linker that
    reserved a different offset than the layout assumes produces firmware
    that builds and does not boot, and that is worth a refusal rather than
    a manifest stating something untrue.
    """
    values = kconfig or {}
    header_size = _as_int(values.get(HEADER_SIZE_SYMBOL, ""), scheme.header_size)
    if header_size != scheme.header_size:
        raise BuildError(
            f"The application was linked with a {header_size}-byte MCUboot header "
            f"offset, but this board's layout says {scheme.header_size}.",
            hint=(
                f"{HEADER_SIZE_SYMBOL} and the board's header_size in "
                "mcuhome/model/registry.py have to agree — an image signed against the "
                "wrong offset boots nowhere. This is a builder bug worth reporting."
            ),
        )
    return SigningParameters(
        header_size=scheme.header_size,
        align=scheme.write_block_size,
        slot_size=scheme.imgtool_slot.size,
        version=values.get(SIGN_VERSION_SYMBOL) or SIGN_VERSION_DEFAULT,
    )
