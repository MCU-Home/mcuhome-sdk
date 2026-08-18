# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The invocation ABI's frozen numbers and action names (contract v1).

Six facts that both ends of the build-container contract have to agree
on, and that neither end owns: which actions exist, which request format
versions can be handed over, which result format version comes back, and
which contract they all belong to. The program answers ``describe`` with
them; the orchestrator writes them into a request document and checks
them in a result document.

They live in the vocabulary package for the same reason the context
format does: an orchestrator that had to import the program to learn the
name of an action would be importing a build environment in order to
drive one, and the two are on opposite sides of a boundary the contract
draws precisely so they can be replaced independently. A third-party
program implements these numbers; it does not import them.

**Frozen** means what §11 says it means: contract v1 does not gain an
action, and a version tuple grows only by adding a version a party can
still refuse. Nothing here is a default that a caller may override.
"""

from __future__ import annotations

__all__ = [
    "ACTIONS",
    "CONTRACT_VERSION",
    "REQUEST_VERSIONS",
    "RESULT_VERSION",
    "RESULT_VERSIONS",
]

#: The contract these numbers belong to. This is contract v1.
CONTRACT_VERSION = 1

#: Request format versions a contract-v1 program can be handed
#: (§7.1.1 ``request``).
REQUEST_VERSIONS = (1,)

#: Result format versions a contract-v1 program may write
#: (§7.1.1 ``result``).
RESULT_VERSIONS = (1,)

#: The one written, out of :data:`RESULT_VERSIONS`.
RESULT_VERSION = RESULT_VERSIONS[0]

#: Every action contract v1 defines, in the order a program announces
#: them. ``generate`` is deliberately absent: the program assembles its
#: own build environment and nobody outside it asks for that step (§6.1).
ACTIONS = ("describe", "verify", "build")
