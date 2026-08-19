# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The ``imgtool sign`` arguments an image has to be signed with.

MCUboot signing is a post-build step over the linked binary (ADR 0015
decision 8): the build produces an **unsigned** image and states the four
arguments it was linked for, and the signature happens later, on the
machine where the private key lives. This is the vocabulary of that
statement, and it sits with the shared model rather than with either end
because both ends need it and neither owns it — the build container's
program writes it into the §7.2.1 build report, and the workbench turns
it back into a command.

Keys in the serialized form are imgtool's own option names, so the block
reads as the command it stands for and a consumer does not have to know
how Zephyr derives any of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcuhome.model.errors import BuildError

__all__ = ["SigningParameters"]


@dataclass(frozen=True)
class SigningParameters:
    """The four ``imgtool sign`` arguments an MCUHome image is signed with.

    ``header_size`` and ``slot_size`` are byte counts; ``version`` is
    imgtool's ``major.minor.revision+build`` string, which MCUboot
    compares monotonically.
    """

    header_size: int
    align: int
    slot_size: int
    version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "header-size": self.header_size,
            "align": self.align,
            "slot-size": self.slot_size,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SigningParameters:
        try:
            return cls(
                header_size=int(data["header-size"]),
                align=int(data["align"]),
                slot_size=int(data["slot-size"]),
                version=str(data["version"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise BuildError(
                f"The build report's signing parameters are incomplete: {error}.",
                hint=(
                    "the report is written by mcuhome device build; an edited or "
                    "truncated one cannot be signed from. Build again."
                ),
            ) from error
