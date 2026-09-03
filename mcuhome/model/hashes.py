# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The one implementation of the file hash both formats and the ABI share.

It lives in the vocabulary package because both sides of the contract
need it: a workbench declares a hash when it writes a context or a build
manifest, and a build server recomputes the very same hash from the
bytes it actually received rather than trusting the declared value — a
declared hash is advisory, and both parties are meant to arrive at the
same value from the same bytes when they compute it themselves, which is
what makes this cross-implementation-critical rather than merely shared:
"the hash of a file" may have exactly one definition here.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

__all__ = ["sha256_file"]

#: Read in blocks rather than whole: a context can carry large patches
#: and a build directory holds linked images, and neither has to fit in
#: memory to be hashed.
_HASH_BLOCK = 1 << 20


def sha256_file(path: Path) -> str:
    """The SHA-256 of a file **as it is on disk**, 64 lowercase hex digits.

    It takes a path rather than bytes on purpose: every declared hash in
    this project's formats is required to be read back from disk after
    ``fsync``, never taken from a buffer, and a function that cannot be
    handed a buffer is the one shape in which that rule cannot be broken
    by accident. The spelling is bare, with no ``sha256:`` prefix,
    because the key that carries a hash already names the algorithm.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_HASH_BLOCK):
            digest.update(block)
    return digest.hexdigest()
