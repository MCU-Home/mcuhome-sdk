# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Where a session's directories are *inside* a build container.

Build-container contract §4 defines no mount points: every path a
program needs arrives in the request document, "as an absolute path
chosen by the backend", and MCUHome's conformance suite deliberately
moves every one of them. That rule is about the **program** — it may not
depend on a fixed path — and it stays exactly as it is. What §4 permits
in the same breath is a *backend* convention: "``/ctx``, ``/out`` and
``/ccache`` may be used as conventions by an image or a backend". This
module is that convention, written down once for the two backends that
implement the ``container`` profile: the ``local`` build method here and
the build server's session backend.

**Why the paths are the same for every build, rather than the host's
own.** Both backends used to mount every directory at its host path
(``--volume /home/…/work:/home/…/work``), which reads well in a log and
makes the compiler cache almost useless. Zephyr appends three
``-fmacro-prefix-map=<absolute path>=…`` options to every single compile
and, with ``-g``, ccache additionally hashes the working directory — so
the project directory ends up inside the cache key, and a cache warmed
by one project misses in every other one. Measured: two projects, same
sources, 0 of 2 hits with host paths, 2 of 2 with these.

``CCACHE_BASEDIR`` is the documented answer to that, and it is the wrong
one here: it only takes effect with ``hash_dir = false`` (measured, and
that setting exists to keep exactly these builds apart), and an object
served from the cache then carries the *first* project's directory in
its debug information — the build output would depend on whether the
cache happened to be warm. Identical paths need no normalizing, so the
cache stays honest and ``hash_dir`` stays on.

The ``subprocess`` profile keeps host paths and cannot use these: several
sessions share one filesystem namespace there, and they cannot all have
``/mcuhome/work``.

Nothing here is a promise to a program. A backend states these paths in
the request document like any others, and a program that reads them from
the document — as the contract requires — cannot tell them from any
other choice.
"""

from __future__ import annotations

from pathlib import PurePosixPath

__all__ = [
    "CCACHE_LOCAL",
    "CCACHE_ROOT",
    "CCACHE_SHARED",
    "CONTEXT",
    "INVOCATIONS",
    "ROOT",
    "SDK",
    "WORK",
    "invocation",
]

#: Everything a session owns lives under one prefix, next to the image's
#: own ``/mcuhome/run`` and ``/mcuhome/workspace``. The container is the
#: session's, so the prefix does not need to name it.
ROOT = PurePosixPath("/mcuhome")

#: The build context (§3), read-only.
CONTEXT = ROOT / "ctx"

#: The session's persistent working area (§4). The build tree, the
#: generated application and everything else that survives from one
#: invocation of a session to the next.
WORK = ROOT / "work"

#: Parent of the per-invocation directories. It is the parent that is
#: mounted, not each invocation: bind mounts are fixed when a container
#: is created, and an invocation directory does not exist yet then.
INVOCATIONS = ROOT / "inv"

#: Where the SDK package goes when ``describe`` declares **no** path for
#: the ``sdk`` tree (§4.1 leaves the choice to the backend then). A
#: program that needs the SDK somewhere particular says so and gets it
#: there; this is the answer for one that does not care.
SDK = ROOT / "sdk"

#: The compiler cache, in the two roles ccache itself distinguishes. The
#: image configures both statically (``/etc/ccache.conf``), so a program
#: never learns about them from anywhere and a backend steers by mounting
#: alone: mount nothing and the cache lives in the container and dies with
#: it; mount a host directory on :data:`CCACHE_LOCAL` and it survives;
#: mount one read-only on :data:`CCACHE_SHARED` and builds start warm.
CCACHE_ROOT = PurePosixPath("/ccache")
CCACHE_LOCAL = CCACHE_ROOT / "cache-local"
CCACHE_SHARED = CCACHE_ROOT / "cache-shared"


def invocation(identifier: str) -> PurePosixPath:
    """The directory of one invocation, by its id.

    One session runs one invocation at a time, but several over its life
    — the steps of one build — and each needs its own ``out``, ``tmp``,
    request, result and event file. Past invocations stay: their event
    files are what a reattaching client replays.
    """
    return INVOCATIONS / identifier
