# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""One declared artifact: ``root``, ``path``, ``role``, ``sha256``.

One entry, and every execution site needs it: a local build reads it out
of the build environment's result document, a remote build reads it off
the session protocol's verdict
(:mod:`mcuhome.workbench.sessionclient`), and whatever signs or flashes
the image afterwards reads it from whichever of them ran. That is what
makes it a vocabulary word, and why the class lives here rather than in
either of them: a dispatcher over the build targets can only read *one*
answer if the answer carries one type, and ``mcuhome.workbench`` may not
import ``mcuhome.compiler`` to borrow it.

An entry missing any of the four fields is not resolvable, and a consumer
skips it exactly as it skips an unknown ``root``.
:func:`artifacts_from_wire` does that skipping, so anything that reaches
this class has all four.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = ["Artifact", "artifacts_from_wire"]


@dataclass(frozen=True)
class Artifact:
    """One entry of ``artifacts[]``, as the program declared it.

    ``root`` names the directory the path is relative to, ``path`` is
    that relative path, ``role`` is what the artifact is for, and
    ``sha256`` is the hash the producer measured — the one a consumer
    re-computes rather than trusts.
    """

    root: str
    path: str
    role: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"root": self.root, "path": self.path, "role": self.role, "sha256": self.sha256}


def artifacts_from_wire(entries: Iterable[Any]) -> tuple[Artifact, ...]:
    """Parse declared artifacts, skipping every entry §5.4 makes unusable.

    The wire form of an artifact is a mapping of the four fields, and an
    entry that is missing one — or carries something that is not a string
    in it — is skipped rather than repaired: a consumer that guessed a
    missing ``root`` would attribute bytes to a directory nobody named.
    """
    found: list[Artifact] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        values = [entry.get(name) for name in ("root", "path", "role", "sha256")]
        if not all(isinstance(value, str) and value for value in values):
            continue
        root, path, role, sha256 = values
        found.append(Artifact(root=root, path=path, role=role, sha256=sha256))
    return tuple(found)
