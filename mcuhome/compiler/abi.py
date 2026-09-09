# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The builder program: two invocations, one build.

This module is what a MCUHome build environment runs when a step starts,
and it answers two calling conventions.

**The v3 invocation is the primary one**, and the one new work is written
against. ``docs/spec/build-environment-specification.md`` fixes it: the
entry point is run with **no arguments** (§6), everything the step is
about is in ``mcuhome/invocation-request.json`` below the base directory
``MCUHOME_BUILDER_BASE_DIR`` names (§4), the answer is one result document
at ``mcuhome/out/result-<invocation_id>.json`` (§6.2), and the exit code
is zero exactly when that document says ``success`` (§6.3). Which actions
exist is deliberately not the specification's business;
``docs/spec/build-actions.md`` documents MCUHome's, and ``build`` is the
one an environment of this kind implements. :func:`step` is that
invocation, and the section "The v3 invocation" below is all of it.

**The legacy invocation belongs to the baked build container** and is kept
working, unchanged, until the switchover retires that image. It is the
two-operand form ``/mcuhome/run <action> <absolute path of the request
document>``, with a request and a result document of its own and three
actions — ``describe``, ``verify`` and ``build``. :func:`main` is that
invocation. The design document it implements, the build container
contract, has been retired and is no longer in this repository: the ``§``
references in the legacy half of this module and of its test suite name
sections of *that* document and resolve nowhere else. They are left as
they are, because that half is deleted whole at switchover and nothing
outside it reads them.

**Both invocations end in the same builder** — :class:`_Build`, which
applies the context's patches, reaches code generation through the SDK
entry point, compiles with ``west build --sysbuild`` and delivers the
artifacts. What differs is what each invocation knows before the build
starts:

*Where the trees are.* The legacy invocation is told, per invocation, in
its request document, and checks what it is told against the image's own
workspace record. The v3 invocation is told nothing: the trees are the
environment's own, and it reads its packages' record for them
(:func:`environment_workspace`). Either way the answer is one path per
layer, and a build against any other path is refused rather than
attempted — west resolves project paths from ``.west/config`` plus the
manifest, and nothing here can move them at invocation time. What would
replace that is a west re-registration step: a manifest rewrite, or
``west config`` per project. That is a design decision, not a
translation.

*What a step may keep.* The legacy contract gives a session a persistent
``work`` directory, which is what makes its ``incremental`` mode mean
anything, and the program writes a session marker there so it can tell
its own state from another session's. The v3 specification is the
opposite and simpler: ``work`` is empty at the start of every step (§3),
nothing survives except ``out`` and the cache tiers, so every v3 step is a
clean build and no marker is worth writing.

*Who measures the context.* The legacy contract makes the program compute
the effective context ID and report it, which is why ``verify`` exists at
all. Under the v3 specification the orchestrator creates the context,
hashes it and delivers it, and ``docs/spec/build-actions.md`` §3 strikes
the question outright: "there is nothing an environment could confirm
that the orchestrator does not already know from its own bytes". A v3
step therefore checks that the files it needs are there and builds.

*Where the artifacts and the report go.* Both put ``firmware.hex``,
``firmware.bin``, ``bootloader.hex`` (when the build produced one) and
``build-report.json`` into ``out``, under exactly those names, and the
build report is the same document — ``docs/spec/build-actions.md`` §2.1
and §2.2 fix both, and the legacy contract fixed the same ones. The two
invocations differ only in how they *declare* them: the legacy result
document carries an object per artifact with its role and its hash, the
v3 result document carries the file names relative to ``out``.

Decisions this module took that no document made for it, and that both
invocations inherit:

*The generated application tree and the CMake tree live in ``work``.*
They are the two directories a build produces that are worth keeping for
the next step of a session, and a tree in a per-step scratch directory
could never make an incremental build mean anything.

*The code generator is a child, and it is handed an empty ``out``.* The
generator ships in ``mcuhome/sdk``, which the orchestrator delivers per
build context, so the generated application belongs to the SDK the
context pinned rather than to the environment's vintage. It is invoked
over the legacy two-operand ABI (``mcuhome-sdk.json`` declares the entry
point and its runtime), and what it produced is then absorbed into
``work/tree`` content-aware: a file whose bytes are already there is left
alone, mtime and all, because CMake watches the tree and a rewritten
unchanged ``CMakeLists.txt`` re-runs the whole Matter sub-build.

*Sysbuild's combined hex never reaches ``out``.* On a build that never
signs it is the *unsigned* application under a name that looks flashable,
which is a hazard rather than an artifact. Nothing lands in ``out`` that
this program did not put there deliberately.

*A build never signs.* It is handed ``keys/signing.pub`` from the context
as the bootloader's verification key and produces an unsigned image; the
private half never enters a build environment. There is no fallback to
MCUboot's own default key, because that default is MCUboot's demo key and
its private half is published — a context without the file fails the step
instead.

*``HOME`` and the west configuration are copied into ``work``.* A build
runs as a user with no home directory of its own, and tools that cache in
``$HOME`` fail obscurely without a writable one; west writes ``zephyr.base``
back into ``.west/config`` on its first build, and the environment's
workspace is input this program does not write into — ever, in any
profile. So both live in ``work``, and the workspace is not written to
incidentally either.

*``PATH`` is never composed here.* It arrives with the environment the
caller stated, because it is the one variable naming things this program
did not put anywhere: the toolchain, west, ``git``, the runtime the SDK
declared. The tools it names are checked before anything is compiled, so
an environment that cannot start ``west`` is a legible refusal rather than
a child that fails to exec ten minutes in.

This module reads no process state: ``argv`` and the environment arrive as
arguments, and every path it touches comes out of a request document or
out of the environment it was handed. The environment its **children** get
is built here and passed to them, which is a different thing from reading
one. That is what keeps this module off the exemption list of
``tests/python/test_userpaths.py``, and the one place allowed to read the
process is the ``__main__`` guard at the bottom, which is the process
boundary itself.

What follows is the legacy half, and only what it decided for itself: the
document that would justify the rest of it is gone, and the code below is
the last thing in this repository that implements it.

*Exactly one thing produces no result document*: a request that cannot be
read at all — the wrong argv arity, a relative request path, a document
that is not one JSON object, one carrying a duplicate key or a ``null``,
or one naming no absolute ``result`` path. That is exit 66 with nothing
written. Everything after it is a result document, including an
unimplemented request format version, because ``result`` is in the
preamble that is read first. "Not writable" is found out by writing: the
result document is the last write action of an invocation, so the whole
document is built in memory and a failing atomic write is the same exit
66, with neither a result nor a temporary file left behind.

*The order of the checks is the invocation's bootstrap chain*: argv arity,
parse, preamble, action, ``required``, the remaining fields.

*``program.actions`` lists what is implemented*, never a constant copied
out of a document: a backend must not invoke what is absent from the
list, so a list that ran ahead of the code would be the one lie a backend
acts on.

*What a request must carry is per action, not per document.* ``describe``
needs the preamble; ``verify`` needs ``context`` and ``session`` and is
refused nothing else, because it reads the context and does nothing with
``out``, ``work``, ``tmp``, ``trees`` or ``limits``; ``build`` needs all
seven mandatory fields, because it acts on all seven. The same is true of
the pointers a request may demand in ``required``: promising to honour a
value nothing reads is the cheapest way to accept a job and quietly
deliver something else.

*A ``params.mode`` this program does not implement is
``unsupported.required``*, not ``unsupported.request``: the field is
present and well-formed, and it is its value that is not implemented.
Executing ``reproducible`` as ``clean`` stays forbidden.

*A session marker in ``work`` is what makes ``incremental`` decidable.*
It is optional in the legacy contract, and without one there is no way to
tell "no prior state of this session" from "somebody else's state". It is
read on every invocation before anything in ``work`` is touched, and a
marker this program cannot parse is treated as foreign — a marker it
wrote is one it can read, and state it cannot claim is state it must not
use, delete or overwrite.

*A context whose files disagree with its own integrity list does not stop
a ``build``.* That disagreement is what ``verify`` exists to report; a
build reports the ID it measured and lets the backend, which has its own,
decide.

*The ``reason`` values and the keys inside ``error.details`` are the
legacy contract's registry*, and the values this program puts under them
are its own: the missing path under ``missing``, the offending pointers
under ``required``, the layer name under ``layer``, the offending context
paths under ``paths``, both session IDs under ``session`` and ``found``. A
failing ``verify`` reports a ``context`` exactly when it measured one:
what an invocation did not measure it does not report, because the
backend compares both against its own values.

*``build.image.started``, cancellation and ``limits.deadline_seconds`` are
not implemented*, all three optional: events are written when the request
names a file for them, ``/cancel`` is not polled, and enforcing a deadline
is the backend's job.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from mcuhome.compiler import report, workspace
from mcuhome.compiler.generate import APP_DIR
from mcuhome.model import __version__, jobs, registry
from mcuhome.model.context import MANIFEST_FILE, MODEL_FILE, PATCHES_DIR
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file
from mcuhome.model.invocation import (
    ACTIONS,
    CONTRACT_VERSION,
    REQUEST_VERSIONS,
    RESULT_VERSION,
    RESULT_VERSIONS,
)
from mcuhome.model.modelfile import read_model

# mcuhome.compiler.contextread is imported inside _open_context, not
# here: the SDK entry point (§6.1) imports this module for
# run_invocation, and its runtime is what the SDK package declares — a
# bare interpreter, no third-party packages. contextdir carries the YAML
# emitter and pulls ruamel at import, which only the program's own
# environment provides. tests/python/test_container_closure.py pins the
# entry point's closure to stdlib plus mcuhome.
if TYPE_CHECKING:
    from mcuhome.compiler.contextread import ContextVerification

__all__ = [
    "BASE_DIR_VAR",
    "BOOTLOADER_ARTIFACT",
    "CACHE_TIERS",
    "CONTRACT_VERSION",
    "EXIT_FAILURE",
    "EXIT_SUCCESS",
    "EXIT_UNUSABLE",
    "FIRMWARE_ARTIFACTS",
    "IMPLEMENTED_ACTIONS",
    "LAYERS",
    "MODES",
    "PROGRAM_ID",
    "REPORT_ARTIFACT",
    "REPORT_VERSION",
    "REQUEST_VERSIONS",
    "RESULT_PREFIX",
    "RESULT_SUFFIX",
    "RESULT_VERSION",
    "RESULT_VERSIONS",
    "SDK_METADATA_FILE",
    "SDK_METADATA_VERSIONS",
    "SIGNING_KEY_FILE",
    "SPEC_GENERATION",
    "STEP_ACTIONS",
    "STEP_CACHE",
    "STEP_CONTEXT",
    "STEP_DIR",
    "STEP_OUT",
    "STEP_PATCHED",
    "STEP_REQUEST",
    "STEP_SDK",
    "STEP_TMP",
    "STEP_VIEW",
    "STEP_WORK",
    "WORKSPACE_PACKAGE_MANIFEST",
    "WORKSPACE_PACKAGE_VAR",
    "WORKSPACE_RECORD",
    "CarriedWorkspace",
    "environment_workspace",
    "honoured_required",
    "main",
    "run_invocation",
    "patchset",
    "program",
    "sdk_entry_point",
    "step",
    "trees",
]

# --------------------------------------------------------------------------
# What this program is (§7.1.1)
# --------------------------------------------------------------------------
#
# The numbers and the action names are not this program's to state: they
# are contract v1's, and the party driving this program has to know them
# without importing it (:mod:`mcuhome.model.invocation`). What is stated
# here is what *this* implementation is — its identity, and that it
# implements all of them.

#: A stable identifier of the *implementation*, reverse-DNS, opaque to a
#: backend (§7.1.1). Not of an image, a tag, a version or a vendor.
PROGRAM_ID = "org.mcuhome.build-container"

#: Every action this program implements, and the whole of ``describe``'s
#: ``program.actions``. This program implements all of contract v1.
IMPLEMENTED_ACTIONS = ACTIONS

#: The layer registry of contract v1 §1.1, in the order ``describe``
#: reports them. Third-party layers carry an ``x-`` prefix and reach the
#: block only by way of the image's own record.
LAYERS = ("zephyr", "sdk", "chip", "mcuboot")

#: What the image says about the west workspace it carries
#: (``containers/builder/workspace-record.py``). Absent everywhere else,
#: which the module docstring covers. ``/mcuhome/`` is the namespace §2.2
#: reserves for this project inside an image, so this is not a promise
#: about somebody else's filesystem.
WORKSPACE_RECORD = Path("/mcuhome/workspace.json")

# --------------------------------------------------------------------------
# What a `build` is made of (§7.2)
# --------------------------------------------------------------------------

#: The two ``params.mode`` values §7.2 defines, and the default §5.2
#: fixes: "an absent ``params``, a ``params`` object without a ``mode``
#: key, and ``params: {}`` are the same thing and all three mean
#: ``mode: "clean"``".
MODES = ("clean", "incremental")
DEFAULT_MODE = MODES[0]

#: The bootloader's verification key inside the context. Mandatory for
#: ``build`` and for ``build`` alone (§7.2), with no fallback: MCUboot's
#: own default is a demo key whose private half is published.
SIGNING_KEY_FILE = "keys/signing.pub"

#: Where the SDK package declares its code-generation entry point, at the
#: root of ``trees.sdk`` (§6.1, normative). Contract v1 fixes the file
#: name and three field names — ``sdk``, ``generate.program``,
#: ``generate.runtime`` — and no values.
SDK_METADATA_FILE = "mcuhome-sdk.json"

#: ``sdk`` metadata format versions this program implements. A version
#: outside it is ``error.build.failed`` and never ``unsupported``: "the
#: program implements everything this contract asks of it, and no other
#: container would fare better with this SDK package" (§6.1).
SDK_METADATA_VERSIONS = (1,)

#: The action the SDK entry point is invoked with (§6.1). Never an action
#: of *this* program.
GENERATE_ACTION = "generate"

#: What the program keeps inside ``work`` — the session's persistent area
#: (§4). Every name is this program's own; the contract fixes none of
#: them, and nothing outside this module may depend on them.
WORK_MARKER = "session.json"
WORK_PATCH_RECORDS = "patches"
WORK_TREE = "tree"
WORK_BUILD = "build"
WORK_CCACHE = "ccache"
WORK_HOME = "home"
#: The writable copy of the workspace's ``.west/config`` (see
#: :meth:`_Invocation._environment`).
WORK_WEST_CONFIG = "west-config"
#: ``XDG_CACHE_HOME`` for the build's children (see
#: :meth:`_Invocation._environment`).
WORK_XDG_CACHE = "cache"

#: ``<sysbuild artifact> -> <name in out>`` for the unsigned application
#: image, whose role is ``firmware``. §7.2: "MCUHome's own container
#: writes ``firmware.hex`` and ``firmware.bin``".
FIRMWARE_ARTIFACTS = (("zephyr.hex", "firmware.hex"), ("zephyr.bin", "firmware.bin"))

#: The same for MCUboot, whose role is ``bootloader``. Not required by
#: §7.2 and declared anyway — see the module docstring.
BOOTLOADER_ARTIFACT = ("zephyr.hex", "bootloader.hex")

#: The mandatory ``report`` artifact (§7.2, §7.2.1), and the format
#: version this module writes. "A consumer that does not implement the
#: version it finds MUST NOT sign from the document."
REPORT_ARTIFACT = "build-report.json"
REPORT_VERSION = 1

#: The prefix of §5.4's ``layers[<name>].patchset`` encoding, stated by
#: the contract as a literal and therefore never composed here.
_PATCHSET_PREFIX = "mcuhome-patchset-1\n"

# --------------------------------------------------------------------------
# The frozen exit codes (§5.3)
# --------------------------------------------------------------------------

#: The invocation ran and the work succeeded; result document present.
EXIT_SUCCESS = 0

#: The invocation ran and the work did not succeed; result document
#: present, with a ``status`` that is not ``success``.
EXIT_FAILURE = 1

#: The request was unusable; no result could be addressed, nothing written.
EXIT_UNUSABLE = 66

_STATUS_SUCCESS = "success"
_STATUS_FAILURE = "failure"
_STATUS_UNSUPPORTED = "unsupported"

_REASON_REQUEST = "unsupported.request"
_REASON_REQUIRED = "unsupported.required"
_REASON_ACTION = "unsupported.action"
_REASON_CONTEXT = "unsupported.context"
_REASON_INCOMPLETE = "error.context.incomplete"
_REASON_MISMATCH = "error.context.mismatch"
_REASON_UNREADABLE = "error.context.unreadable"
_REASON_LAYER = "error.layer.unknown"
_REASON_PATCH = "error.patch.incomplete"
_REASON_WORK = "error.work.foreign"
_REASON_BUILD = "error.build.failed"
_REASON_INTERNAL = "error.internal"

# --------------------------------------------------------------------------
# The request document (§5.2)
# --------------------------------------------------------------------------


class _Unusable(Exception):
    """No result can be addressed: exit 66, nothing written (§5.3).

    Not an error type from :mod:`mcuhome.model.errors`, on purpose. Those render
    themselves for a person reading a terminal; this one is never rendered
    anywhere, because the whole point of exit 66 is that there is no
    channel to say anything on.
    """


#: Top-level fields whose value is a path (§5.2). ``result`` is not among
#: them: it is checked in the preamble, where a relative one is exit 66
#: rather than a refusal nobody could read (see the module docstring).
_PATH_FIELDS = ("out", "work", "tmp", "context", "events", "cancel")

#: Fields carrying one ``{path, …}`` object, and fields carrying a map of
#: them. ``trees`` is the map (§4.1); ``ccache`` is the single object (§10).
_PATH_OBJECTS = ("ccache",)
_PATH_OBJECT_MAPS = ("trees",)

#: "Absent" as distinct from "present and null" — although a request
#: document can never carry the latter, since a ``null`` anywhere in it is
#: exit 66 before any pointer is resolved.
_MISSING = object()


def _is_request_version(value: Any) -> bool:
    """A request format version this program parses.

    ``bool`` is excluded explicitly: it is an ``int`` in Python, and
    ``"request": true`` would otherwise be read as version 1.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value in REQUEST_VERSIONS


def _is_absolute_path(value: Any) -> bool:
    """A path value as §5.2 defines one: a string, and absolute."""
    return isinstance(value, str) and Path(value).is_absolute()


def _is_present(value: Any) -> bool:
    """Anything at all, as opposed to :data:`_MISSING`."""
    return value is not _MISSING


def _is_jobs(value: Any) -> bool:
    """``limits.jobs`` as §5.2 defines it: authoritative, so a real count.

    ``bool`` is excluded for the reason :func:`_is_request_version` gives.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_tree_entry(value: Any) -> bool:
    """One ``trees`` entry: an object with an absolute ``path`` (§4.1).

    ``writable`` is not checked here, and deliberately not: it is
    "asserted by the backend, never probed by the program", so a value
    this program cannot verify is not one it can promise to honour by
    inspecting it. What it promises is to *read* the flag rather than to
    test it, which is what §6.2 asks for.
    """
    return isinstance(value, dict) and _is_absolute_path(value.get("path"))


def _is_mode(value: Any) -> bool:
    """A ``params.mode`` value §7.2 defines. Absent is ``clean`` (§5.2)."""
    return value is _MISSING or value in MODES


#: The JSON Pointers this program honours **per action**, each with the
#: values it can honour there (§5.2 rule 2: "knowing the path is not
#: enough").
#:
#: Per action, because the same pointer is a promise for one action and a
#: lie for another: a ``verify`` "reads the context and nothing else"
#: (§7.3) and does nothing whatever with ``/out``, ``/work``, ``/tmp``,
#: ``/trees/sdk`` or ``/limits/jobs``, although §5.2 makes all five
#: mandatory in the request it arrives in — while a ``build`` writes into
#: three of them and takes its parallelism from the fourth. Promising to
#: honour a value nothing reads is the cheapest form of the lie §5.2
#: names: "accept the job and quietly deliver something else".
#:
#: The preamble is honoured by every action, and each entry for its own
#: reason: ``/request`` because a version outside :data:`REQUEST_VERSIONS`
#: is refused rather than parsed hopefully; ``/result`` because the result
#: document is written to exactly that path; ``/session`` because it is
#: echoed, which is the whole of what §5.2 permits anyone to do with it,
#: and honourable only as the opaque *string* token the contract defines.
#:
#: Two pointers a conforming ``build`` request may carry are deliberately
#: **absent**: ``/cancel``, because cancellation is a SHOULD this program
#: does not implement (§8), and ``/limits/deadline_seconds``, because it
#: is advisory and enforcement is the backend's (§5.2). A backend that
#: demands either is told which pointer failed.
_PREAMBLE_HONOURED: dict[str, Callable[[Any], bool]] = {
    "/request": _is_request_version,
    "/result": _is_absolute_path,
    "/session": lambda value: isinstance(value, str),
}

#: The part of the table that is a property of this *program*. What it
#: honours for ``/trees/<layer>`` is a property of the *image* instead,
#: and :func:`honoured_required` is where the two are put together.
_HONOURED_REQUIRED: dict[str, dict[str, Callable[[Any], bool]]] = {
    "describe": dict(_PREAMBLE_HONOURED),
    "verify": {**_PREAMBLE_HONOURED, "/context": _is_absolute_path},
    "build": {
        **_PREAMBLE_HONOURED,
        "/context": _is_absolute_path,
        "/out": _is_absolute_path,
        "/work": _is_absolute_path,
        "/tmp": _is_absolute_path,
        "/events": _is_absolute_path,
        "/ccache": _is_tree_entry,
        "/limits/jobs": _is_jobs,
        "/params/mode": _is_mode,
    },
}


def _tree_at(expected: Path | None) -> Callable[[Any], bool]:
    """Honours a ``trees`` entry naming *expected*, and no other path.

    ``None`` honours nothing: a layer this program has no tree for is a
    layer no value of ``/trees/<layer>`` can be honoured for.
    """

    def honours(value: Any) -> bool:
        if expected is None or not _is_tree_entry(value):
            return False
        return Path(value["path"]) == expected

    return honours


def honoured_required(
    action: str, record: Path = WORKSPACE_RECORD
) -> dict[str, Callable[[Any], bool]]:
    """The pointers *action* honours, with the values it can honour there.

    §5.2 rule 2, per action, because the same pointer is a promise for one
    action and a lie for another: a ``verify`` "reads the context and
    nothing else" (§7.3) and does nothing whatever with ``/out``,
    ``/work``, ``/tmp``, ``/trees/sdk`` or ``/limits/jobs``, although §5.2
    makes all five mandatory in the request it arrives in — while a
    ``build`` writes into three of them and takes its parallelism from the
    fourth. Promising to honour a value nothing reads is the cheapest form
    of the lie §5.2 names: "accept the job and quietly deliver something
    else".

    **``/trees/<layer>`` is honoured for exactly one value per layer**,
    which is why the table needs the image's record and cannot be a
    constant. "Knowing the path is not enough: it must be able to honour
    the value it finds there" — and :meth:`_Build._workspace` can honour
    exactly the path the record already has for that layer, because west
    resolves project paths from ``.west/config`` plus the manifest and
    nothing here moves them at invocation time. A backend that names any
    other path in ``required`` is therefore told ``unsupported.required``,
    which is what §5.2 rule 2 mandates, rather than being served
    ``error.build.failed`` from the middle of the build — the same fact,
    reported in the channel the backend asked in.

    A layer the record does not name is honoured for nothing at all, and
    that includes every layer when there is no record: a program with no
    workspace of its own cannot promise to build against any tree.
    """
    table = dict(_HONOURED_REQUIRED.get(action, {}))
    if action == "build":
        paths = _record_tree_paths(record)
        for layer in dict.fromkeys([*LAYERS, *sorted(paths)]):
            table[f"/trees/{layer}"] = _tree_at(paths.get(layer))
    return table


#: What each action needs to find in the request document, as a JSON
#: Pointer and the values that are usable there. Rule 3 of §5.2: "A field
#: the program needs for this action and does not find … ⇒ ``status:
#: "unsupported"``, ``reason: "unsupported.request"``".
#:
#: ``describe`` "needs only the preamble" (§5.2) and so is absent here.
#: ``verify`` needs two of the seven fields §5.2 makes mandatory for a
#: working action, and the module docstring says why the other five are
#: not demanded back. ``build`` needs all seven, because it acts on all
#: seven. ``/session`` is checked for presence and not for type: §5.4's
#: echo rule says a program echoes what it was given, verbatim, and §5.2
#: forbids composing a path from it, so its type never has to be believed.
#: A backend that wants the token's type honoured names it in
#: ``required``, and :data:`HONOURED_REQUIRED` answers that.
#:
#: ``/params/mode`` is here rather than only in the honoured table
#: ``/params/mode`` is not here: a missing mode is usable (it means
#: ``clean``), so the only way it could fail this presence check is a
#: value like ``reproducible`` — and §5.2 ("`required` and value
#: granularity") already fixes what that is: "A program that knows
#: ``/params/mode`` but not the value ``reproducible`` MUST refuse with
#: ``unsupported.required`` rather than accept the job." That is a
#: present field with an unimplemented value, not a missing one, so
#: :func:`_unsupported_mode` handles it apart from rule 3's missing
#: fields.
_NEEDED_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "verify": {"/context": _is_absolute_path, "/session": _is_present},
    "build": {
        "/context": _is_absolute_path,
        "/session": _is_present,
        "/out": _is_absolute_path,
        "/work": _is_absolute_path,
        "/tmp": _is_absolute_path,
        "/trees/sdk/path": _is_absolute_path,
        "/limits/jobs": _is_jobs,
    },
}


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """One JSON object, refusing the duplicate keys §5.2 calls invalid."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Unusable("the request document has a duplicate key")
    return dict(pairs)


def _carries_null(value: Any) -> bool:
    """Whether *value* holds a ``null`` anywhere inside it.

    "``null`` never means 'absent'; it is invalid" (§5.2) — at any depth
    and in any field, including one this program would otherwise ignore.
    The rule governs the document, not the fields one program happens to
    read.
    """
    if value is None:
        return True
    if isinstance(value, dict):
        return any(_carries_null(item) for item in value.values())
    if isinstance(value, list):
        return any(_carries_null(item) for item in value)
    return False


def _parse_request(path: str) -> dict[str, Any]:
    """The request document at *path*, or :class:`_Unusable`.

    The only program-caused error that cannot produce a result document
    (§5.1 step 4), and precisely the case in which the program does not
    know where a result would go. A byte-order mark is refused by the JSON
    parser itself, which is what §5.2's "UTF-8 without BOM" asks for.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as failure:
        raise _Unusable(f"the request document cannot be read: {failure}") from failure
    try:
        document = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except ValueError as failure:
        raise _Unusable(f"the request document is not JSON: {failure}") from failure
    if not isinstance(document, dict):
        raise _Unusable("the request document is not a JSON object")
    if _carries_null(document):
        raise _Unusable("the request document carries a null")
    return document


def _result_path(document: dict[str, Any]) -> Path:
    """Where the result document goes, from the immortal preamble.

    "From here on **every** error is a result document" (§5.1 step 5) —
    which is true exactly because this function refused everything that
    would have made that impossible.
    """
    value = document.get("result")
    if not _is_absolute_path(value):
        raise _Unusable("the request document names no absolute result path")
    return Path(value)


def _resolve_pointer(document: dict[str, Any], pointer: str) -> Any:
    """RFC 6901 evaluation of *pointer* against *document*.

    Returns :data:`_MISSING` for anything that does not resolve, an
    invalid pointer syntax included: §5.2 rule 2 asks whether the program
    "can honour the value it finds there", and finding nothing is one way
    of not being able to.
    """
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return _MISSING
    current: Any = document
    for escaped in pointer.split("/")[1:]:
        token = escaped.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (token != "0" and token.startswith("0")):
                return _MISSING
            index = int(token)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _unhonourable(action: str, document: dict[str, Any], record: Path) -> list[str] | None:
    """The entries of ``required`` *action* cannot honour (§5.2 rule 2).

    ``None`` means ``required`` is not a list of strings at all, which is
    a document this program cannot read as specified — rule 3, not rule 2,
    because there is no pointer to name in ``error.details.required``.
    An absent ``required`` is ``[]`` (§5.2), so it honours vacuously.
    """
    required = document.get("required", [])
    if not isinstance(required, list) or not all(isinstance(entry, str) for entry in required):
        return None
    honoured = honoured_required(action, record)
    offending: list[str] = []
    for pointer in required:
        honours = honoured.get(pointer)
        if honours is None:
            offending.append(pointer)
            continue
        value = _resolve_pointer(document, pointer)
        if value is _MISSING or not honours(value):
            offending.append(pointer)
    return offending


def _not_found(action: str, document: dict[str, Any]) -> list[str]:
    """The fields *action* needs and this document does not supply (rule 3).

    Named as pointers rather than as field names so the refusal reads in
    the same vocabulary ``required`` does, and so a nested field can be
    named the day one is needed. The list is :data:`_NEEDED_FIELDS`, which
    is deliberately *not* §5.2's list of fields mandatory for a working
    action: rule 3 refuses over what the program needs, and §4.1 forbids
    requiring what it does not.
    """
    needed = _NEEDED_FIELDS.get(action, {})
    return [
        pointer
        for pointer, usable in needed.items()
        if not usable(_resolve_pointer(document, pointer))
    ]


def _unsupported_mode(action: str, document: dict[str, Any]) -> Any:
    """A present ``/params/mode`` this program does not implement, or None.

    §7.2 enumerates ``clean`` and ``incremental``. A third value is one
    the program cannot honour, which §5.2 already scopes to
    ``unsupported.required`` ("A program that knows ``/params/mode`` but
    not the value ``reproducible`` MUST refuse with
    ``unsupported.required``") — distinct from a *missing* mandatory
    field (``unsupported.request``), because the field is present and
    well-formed; it is its value the program does not implement. An
    absent mode is ``clean`` (§5.2) and usable, so it is never one of
    these.
    """
    if action != "build":
        return None
    value = _resolve_pointer(document, "/params/mode")
    if value is _MISSING or value in MODES:
        return None
    return value


def _relative_paths(document: dict[str, Any]) -> list[str]:
    """Every known path field of *document* whose value is not absolute.

    "Every path value is absolute" (§5.2) is stated of the document rather
    than of one action's fields, so every field the contract defines as a
    path is checked — including the ones ``describe`` has no use for. An
    unknown field is not checked, because rule 1 says to ignore it, and a
    known field holding something that is not a string is not checked
    either: that is not a path value at all, and no action implemented
    here needs one.
    """
    found: list[str] = []

    def check(name: str, value: Any) -> None:
        if isinstance(value, str) and not _is_absolute_path(value):
            found.append(name)

    for name in _PATH_FIELDS:
        check(name, document.get(name))
    for name in _PATH_OBJECTS:
        entry = document.get(name)
        if isinstance(entry, dict):
            check(f"{name}.path", entry.get("path"))
    for name in _PATH_OBJECT_MAPS:
        entries = document.get(name)
        if isinstance(entries, dict):
            for key, entry in entries.items():
                if isinstance(entry, dict):
                    check(f"{name}.{key}.path", entry.get("path"))
    return sorted(found)


# --------------------------------------------------------------------------
# The result document (§5.4)
# --------------------------------------------------------------------------


def _result_document(
    echo: dict[str, Any],
    status: str,
    *,
    reason: str | None = None,
    error: dict[str, Any] | None = None,
    context: str | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    layers: dict[str, Any] | None = None,
    program: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A result document in the field order §5.4 prints it in.

    *echo* is what the invocation was given and nothing else: ``action``
    always, because it is ``argv[1]``, and ``session`` iff the request
    document carried it. "A program MUST NOT invent a value for a field it
    was never given" (§5.4).

    Every optional field below is passed exactly when the invocation
    *measured* the thing it reports, which is what §5.4's "MUST, on
    success" rows are about: "An invocation that failed before it got that
    far reports what it measured and nothing more … Fabricating either
    would be worse than omitting it, since the backend compares both
    against its own values." So *context* appears once the effective ID
    has been computed, and *artifacts* and *layers* only on a successful
    ``build`` — for ``describe`` and ``verify`` they are the table's "MUST
    NOT" rows, and a ``verify`` that declared diagnostic output (which it
    MAY) would still declare no ``layers``, because "it reports work that
    was actually done, and ``verify`` does not do that work".
    """
    result: dict[str, Any] = {"result": RESULT_VERSION, "status": status}
    result.update(echo)
    result["reason"] = reason
    result["error"] = error
    if context is not None:
        result["context"] = context
    if artifacts is not None:
        result["artifacts"] = artifacts
    if layers is not None:
        result["layers"] = layers
    if program is not None:
        result["program"] = program
    return result


def _refusal(
    echo: dict[str, Any],
    reason: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    program: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """An ``unsupported`` result: exit 1, with a ``reason`` to match on.

    ``error.retryable`` is false for every refusal this program makes, and
    it is "the program's promise about its own failure, and about nothing
    else" (§5.4.1): re-running an identical request against an identical
    program produces the identical refusal. ``error.details`` is ``{}``
    wherever the contract fixes no contents for it — "Contract v1 fixes
    its contents only where a ``reason`` says so".
    """
    return _result_document(
        echo,
        _STATUS_UNSUPPORTED,
        reason=reason,
        error={"retryable": False, "message": message, "details": details or {}},
        program=program,
    )


def _failure(
    echo: dict[str, Any],
    reason: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    context: str | None = None,
) -> dict[str, Any]:
    """A ``failure`` result: the work ran and did not succeed (§5.3).

    The other half of the "not ``success``" space, and the one §5.4's
    registry reserves its ``error.*`` reasons for. ``retryable`` is false
    for every failure this program produces, for the same reason it is
    false for every refusal: it is "the program's promise about its own
    failure, and about nothing else" (§5.4.1), and a context that
    disagrees with its own integrity list disagrees with it just as much
    on a second reading. Nothing here is a transient condition the program
    could wait out — the remedy is a different context, which is a
    different invocation.
    """
    return _result_document(
        echo,
        _STATUS_FAILURE,
        reason=reason,
        error={"retryable": False, "message": message, "details": details or {}},
        context=context,
    )


def _write_atomically(path: Path, document: dict[str, Any]) -> None:
    """Write *document* to *path* the way §5.4 prescribes.

    "temporary file in the *same* directory, ``fsync``, ``rename``" —
    same directory so the rename cannot cross a filesystem, ``fsync`` so
    the bytes are on disk before the name exists, rename because that is
    the one operation a reader cannot observe half of. A failure anywhere
    leaves neither a result document nor a temporary file behind, which is
    what makes "exit 66, nothing written" true of the write as well as of
    the parse.

    The file keeps :func:`tempfile.mkstemp`'s own mode. The contract says
    nothing about the result document's permissions, and the backend
    either runs the program as itself or outranks it.
    """
    payload = json.dumps(document, indent=2) + "\n"
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# describe (§7.1)
# --------------------------------------------------------------------------


def _record_document(record: Path) -> dict[str, Any]:
    """The image's workspace record, or an empty document.

    Unreadable and malformed are the same answer as absent, on purpose: a
    ``describe`` that cannot answer is a failed conformance test (§7.1),
    while a ``describe`` reporting ``"path": null`` asks the backend to
    supply the trees — which is the safe direction and the one every
    backend can satisfy. A ``build`` reads the same document and cannot
    be so relaxed about it (:func:`_workspace`), because a program with no
    workspace of its own has no build environment to assemble.
    """
    try:
        content = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return content if isinstance(content, dict) else {}


def _record_layers(record: Path) -> dict[str, Any]:
    """The ``layers`` block of the image's workspace record, or nothing."""
    layers = _record_document(record).get("layers")
    return layers if isinstance(layers, dict) else {}


def _tree_paths(document: dict[str, Any]) -> dict[str, Path]:
    """``<layer> -> <where this environment builds it>``, from a record.

    The one answer to "which path can this program honour for this
    layer", read by :func:`honoured_required` before a legacy build starts
    and by :meth:`_Build._workspace` while any build runs. A layer whose
    entry names no string path is absent from the result rather than
    present with a guess.
    """
    layers = document.get("layers")
    return {
        name: Path(entry["path"])
        for name, entry in (layers if isinstance(layers, dict) else {}).items()
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }


def _record_tree_paths(record: Path) -> dict[str, Path]:
    """:func:`_tree_paths` of the record at *record*."""
    return _tree_paths(_record_document(record))


def trees(record: Path = WORKSPACE_RECORD) -> dict[str, dict[str, Any]]:
    """Where this program keeps each layer it carries (§7.1.1 ``trees``).

    Every layer of the contract's registry is reported, plus anything else
    the record names — an image that carries an ``x-`` layer says so. A
    ``path`` of ``null`` is the contract's own way of saying "this tree is
    not in my image; put it wherever you like and name it in ``trees``",
    and it is reported only where that is true: no record at all, or a
    record entry naming no path.

    **A layer the record marks ``mounted`` is reported at the path the
    record names, not as ``null``.** The record means "not baked, mounted
    per session" by that flag, and reporting ``null`` for it read as "put
    it wherever you like" — which :meth:`_Build._workspace` then refuses,
    because west resolves project paths from ``.west/config`` plus the
    manifest and this program has no way to move them at invocation time.
    A backend that arranged itself by such a ``describe`` could never have
    built. ``describe`` is "**authoritative** about what the program can
    do" (§7.1), so it says the path the SDK has to be mounted at.

    §4 sanctions this since the D1 erratum: "A ``trees`` entry is the
    one thing a program may have a fixed path for", because a tree is a
    property of the *image* rather than of the session — "a declared
    path is then a requirement the backend MUST satisfy for that image,
    and not a convention". Declaring it here, in ``describe``, is the
    mechanism the erratum names: the backend learns the requirement
    before it starts a session, not from a refusal in the middle of one.

    ``version`` is the revision the record carries, and is omitted rather
    than guessed where there is none — §7.1.1 makes it optional for
    exactly that case, and a mounted tree is exactly that case.
    """
    layers = _record_layers(record)
    block: dict[str, dict[str, Any]] = {}
    for name in dict.fromkeys([*LAYERS, *sorted(layers)]):
        found = layers.get(name)
        entry: dict[str, Any] = found if isinstance(found, dict) else {}
        path = entry.get("path")
        tree: dict[str, Any] = {"path": path if isinstance(path, str) else None}
        revision = entry.get("revision")
        if isinstance(revision, str):
            tree["version"] = revision
        block[name] = tree
    return block


def program(record: Path = WORKSPACE_RECORD) -> dict[str, Any]:
    """The self-description of §7.1.1, every field of it.

    ``version`` is the package's own and is opaque to a backend: "A
    backend MAY log it … it MUST NOT parse it and MUST NOT make a
    compatibility decision from it." Compatibility is decided by
    ``contract``, ``request``, ``result`` and ``actions``, which are
    declarations rather than inferences — and all four are constants here.
    """
    return {
        "id": PROGRAM_ID,
        "version": __version__,
        "contract": CONTRACT_VERSION,
        "request": list(REQUEST_VERSIONS),
        "result": list(RESULT_VERSIONS),
        "actions": list(IMPLEMENTED_ACTIONS),
        "trees": trees(record),
    }


def _describe(echo: dict[str, Any], record: Path) -> dict[str, Any]:
    """``describe``: read two fields, write four, plus ``program`` (§7.1).

    It "never touches the context, writes nothing but the result document,
    and fills the ``program`` block", so there is no context ID to report
    and nothing measured to declare.
    """
    return _result_document(echo, _STATUS_SUCCESS, program=program(record))


# --------------------------------------------------------------------------
# verify (§7.3)
# --------------------------------------------------------------------------


def _reportable(value: Any) -> Any:
    """*value* as something :func:`json.dumps` can write.

    The declared ``context`` format version reaches ``error.details``
    straight out of a YAML document, where a scalar can parse as a date,
    a mapping or anything else. The result document is the last write
    action of the invocation (§5.4), so a value the JSON encoder chokes on
    there would cost the whole invocation its answer — nothing written and
    exit 66, for a context this program diagnosed perfectly well. Carrying
    the value as text is the smaller loss, and the field exists so a
    backend can see what it sent.
    """
    return value if isinstance(value, bool | int | str) else str(value)


class _Refused(Exception):
    """A typed answer an action already has; carries its result document.

    An action of this contract is a chain of checks that each end the
    invocation with a *document* rather than with a value, and ``build``
    (§7.2) is fourteen of them deep. Raising the finished document keeps
    the chain readable as the sequence §7.2 states it in, instead of as
    fourteen nested ``if`` statements — and every raise site is a
    ``reason`` from §5.4's registry, never a Python error class leaking
    out. It is caught in exactly one place per action.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        super().__init__(document.get("reason"))
        self.document = document


class _BuildFailed(Exception):
    """A build that did not succeed, before it is a result document.

    The builder (:class:`_Build`) is shared by two invocations whose result
    documents have nothing in common, so it raises the *facts* and lets the
    invocation that asked render them: the legacy shell into a ``failure``
    with its ``reason`` and ``error.details``, a v3 step into a ``failure``
    with its ``message``. Everything a build refuses is one of these, and
    every raise site reads the same either way.
    """

    def __init__(self, reason: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details


def _open_context(echo: dict[str, Any], root: Path) -> ContextVerification:
    """The materialized context at *root*, measured, or a typed refusal.

    Shared by ``verify`` and ``build``, which is the point: both compute
    the effective context ID, and §3.3 is only worth anything while each
    side of the contract has *one* implementation of it. Every hash and
    the ID come from :func:`~mcuhome.compiler.contextread.verify_context`.

    The refusals are §3.1's and §3.2's, and neither is action-specific:
    a context with no ``manifest.yaml`` "is missing a file the action
    needs" (§5.4), and a ``context`` format version this program does not
    implement is ``unsupported.context`` — "nothing about this context is
    broken" (§3.2). The manifest that cannot be read at all lands on
    ``error.context.mismatch``, which is a gap in contract v1 the module
    docstring records rather than papers over.

    What the caller does with :attr:`~ContextVerification.ok` differs, and
    that is why it is not decided here: ``verify`` exists to report a
    disagreement (§7.3), while for a ``build`` §5.4 makes ``result.context``
    a value "for comparison only" and never makes a mismatch a build
    failure.
    """
    # Lazy on purpose — see the note at the module's import block: the
    # SDK entry point imports this module under a bare runtime, and only
    # the actions that measure a context may pull the YAML machinery.
    from mcuhome.compiler.contextread import ContextFormatVersionError, verify_context

    if not (root / MANIFEST_FILE).is_file():
        # "is missing a file the action needs … the missing path in
        # error.details" (§5.4). §3.1 makes this *the* file: "manifest.yaml
        # is the program's entry point; a program MUST NOT require any
        # out-of-band knowledge beyond it and this contract." A context
        # directory that is not there at all lands here too, which is
        # right — from the program's side the two are the same absence.
        raise _Refused(
            _failure(
                echo,
                _REASON_INCOMPLETE,
                f"the context at {root} carries no {MANIFEST_FILE}",
                details={"missing": [MANIFEST_FILE]},
            )
        )
    try:
        return verify_context(root)
    except ContextFormatVersionError as unimplemented:
        if unimplemented.found is None:
            raise _Refused(
                _failure(
                    echo,
                    _REASON_UNREADABLE,
                    f"the context at {root} states no {MANIFEST_FILE} format version",
                )
            ) from unimplemented
        raise _Refused(
            _refusal(
                echo,
                _REASON_CONTEXT,
                unimplemented.message,
                details={"context": _reportable(unimplemented.found)},
            )
        ) from unimplemented
    except (BuildError, OSError) as unreadable:
        # "found ``manifest.yaml`` and cannot read it as one: broken YAML,
        # a missing section, or a hash in a spelling §3.3.1 refuses" — the
        # reason §5.4's registry provides for exactly this case, distinct
        # from a mismatch so a backend can tell a corrupt manifest from a
        # tampered context file without parsing untrusted message text.
        detail = unreadable.message if isinstance(unreadable, BuildError) else str(unreadable)
        raise _Refused(
            _failure(
                echo,
                _REASON_UNREADABLE,
                f"the context at {root} cannot be read as one: {detail}",
            )
        ) from unreadable


def _verify(echo: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """``verify``: the materialized context against its own integrity list.

    §7.3: "Asserts that the materialized context is the context the
    manifest describes. It checks the **effective** context — the file set
    as materialized, against the integrity list in ``manifest.yaml`` … —
    and reports the resulting ``context`` ID in its result. A file that is
    missing, a file whose bytes hash to something else, and a file present
    but absent from the list are one outcome and one typed answer:
    ``status: "failure"``, ``reason: "error.context.mismatch"``, the
    offending paths in ``error.details``."

    Every hash and the ID itself come from
    :func:`~mcuhome.compiler.contextread.verify_context`, never from this module —
    see the module docstring for why a second implementation of §3.3 here
    would be the defect that rule exists against. Its
    :attr:`~mcuhome.compiler.contextread.ContextVerification.ok` covers one case
    §7.3 does not enumerate and §3.3 demands anyway: a manifest whose
    declared ``id`` is not the ID its own contents yield. "Implementations
    … MUST NOT trust a declared ``id`` value" — and a declared value
    nobody checks is one nothing in the system would ever catch, since
    every other party recomputes and would agree with itself.

    **This invocation writes nothing but its result document.** §9.2 point
    10 forbids a ``verify`` to "Apply a patch, write into a ``trees``
    entry, or write into ``work``"; this one needs none of those paths at
    all. No event is written either: ``events`` is optional in both
    directions (§8) and "a program that offers fewer names than the table
    is conforming". ``cancel`` is not polled, which §8 leaves as a SHOULD
    "so that a fifty-line third-party program stays possible" — this
    action is one pass over one directory, and the backend's SIGTERM
    remains the hard path.
    """
    root = Path(document["context"])
    try:
        verification = _open_context(echo, root)
    except _Refused as refused:
        return refused.document

    if not verification.ok:
        return _failure(
            echo,
            _REASON_MISMATCH,
            "; ".join(verification.problems()),
            details={"paths": [mismatch.path for mismatch in verification.mismatches]},
            # Measured, so reported — the module docstring quotes the line.
            context=verification.actual_id,
        )
    return _result_document(echo, _STATUS_SUCCESS, context=verification.actual_id)


# --------------------------------------------------------------------------
# build (§7.2)
# --------------------------------------------------------------------------


class _Events:
    """The optional NDJSON event stream of §8, or nothing at all.

    "Only if the request document carries ``events``. The program appends
    NDJSON to that file — one JSON object per line, UTF-8, flushed after
    every line, append-only, never truncated. Every object carries
    ``"event": "<name>"`` and a monotonic ``"seq"`` starting at 1."

    **Nothing here can fail an invocation.** "A program MUST NOT block on
    writing an event and MUST NOT die if the write fails. Where the two
    obligations collide — a full pipe, a stalled disk — **not blocking
    wins**." So every write is guarded, nothing is retried, and the file
    is flushed rather than ``fsync``ed: a reader tailing it wants the
    bytes now, and an event nobody read is not worth a build.
    """

    def __init__(self, path: Any) -> None:
        self._path = Path(path) if isinstance(path, str) else None
        self._seq = 0

    def emit(self, name: str, **fields: Any) -> None:
        if self._path is None:
            return
        self._seq += 1
        try:
            line = json.dumps({"event": name, "seq": self._seq, **fields})
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except (OSError, TypeError, ValueError):
            return


def _write_file(path: Path, data: bytes) -> None:
    """Write *data* and make it real before anybody hashes it.

    The ``fsync`` is the point: §5.4 requires every declared hash to be
    read back from disk, and reading back a file whose bytes are still in
    the page cache would satisfy the letter and none of the reason — the
    backend re-hashes the same file from *its* side of the mount (§9.3).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _write_json(path: Path, document: dict[str, Any]) -> None:
    """One of this program's own state files in ``work``, durably."""
    _write_file(path, (json.dumps(document, indent=2) + "\n").encode("utf-8"))


def patchset(layer_dir: Path) -> str:
    """``layers[<name>].patchset`` for one ``patches/<layer>/`` directory.

    §5.4 defines the value exactly, "otherwise a cross-implementation
    audit is worthless"::

        SHA-256( "mcuhome-patchset-1\\n"
                 + for each file under patches/<layer>/, ascending byte order:
                     <64 hex chars of the file's SHA-256> + " " + <filename> + "\\n" )

    "The value carries its own algorithm, so it is rendered ``sha256:`` +
    64 lowercase hex digits, and each ``<64 hex chars>`` inside the input
    is lowercase (§3.3.1)." The sort is over the filename's **bytes**, not
    over its code points — the two agree for the ``NNNN-name.patch``
    grammar :mod:`mcuhome.compiler.contextread` enforces, and stating the byte order
    is what makes a second implementation agree for a name outside it.
    """
    files = sorted(
        (entry for entry in layer_dir.iterdir() if entry.is_file()),
        key=lambda entry: entry.name.encode("utf-8"),
    )
    text = _PATCHSET_PREFIX + "".join(f"{sha256_file(entry)} {entry.name}\n" for entry in files)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _absorb_tree(source: Path, target: Path) -> int:
    """Merge the generate child's *source* tree into *target*, content-aware.

    The other half of handing the child an empty ``out`` (§4): what it
    produced still has to end up in the session's persistent tree, and it
    must land there the way :func:`mcuhome.compiler.generate.write_tree` would
    have written it — a file whose bytes are already in *target* is left
    alone, mtime and all, because CMake watches the tree and a rewritten
    unchanged ``CMakeLists.txt`` re-runs the Matter sub-build. That is
    the whole difference between §7.2's ``incremental`` meaning
    something and meaning "clean, slowly".

    Nothing is deleted from *target*: the generator never deletes either
    (its contract is a mapping of files to write), and a stale file from
    an earlier model is exactly as stale after a direct
    ``write_tree(work/tree)`` would have run. Returns how many files the
    child produced, for the ``generate.written`` event.
    """
    produced = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        produced += 1
        destination = target / path.relative_to(source)
        content = path.read_bytes()
        if destination.is_file() and destination.read_bytes() == content:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    return produced


def _run_child(command: list[str], *, env: dict[str, str], directory: Path) -> tuple[int, str]:
    """Run one short-lived child, and return its exit code and its output.

    The seam every test replaces. Standard error is merged into standard
    output because §8 makes the two "one raw, opaque log stream", and the
    stream is echoed onward as well as captured: a consumer "MUST NOT
    parse the log stream for machine decisions", and this program does not
    — it re-emits it so that the backend collecting the container's output
    sees what its children said, and keeps a copy only for the failure
    message.

    The west build does not come through here: it goes through
    :func:`~mcuhome.compiler.workspace.run_build`, which is the same shape with the
    live echoing a quarter of an hour of compiling needs.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=str(directory),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as failure:
        return 127, f"{command[0]}: {failure}"
    sys.stdout.write(completed.stdout)
    sys.stdout.flush()
    return completed.returncode, completed.stdout


def _requested_mode(document: dict[str, Any]) -> str:
    """``params.mode``, with §5.2's default applied.

    "An absent ``params``, a ``params`` object without a ``mode`` key, and
    ``params: {}`` are the same thing and all three mean ``mode:
    "clean"``". A value outside :data:`MODES` never reaches here — rule 3
    refused it (:data:`_NEEDED_FIELDS`) — so the fallback below is the
    default and not a guess.
    """
    params = document.get("params")
    if not isinstance(params, dict):
        return DEFAULT_MODE
    value = params.get("mode", DEFAULT_MODE)
    return value if value in MODES else DEFAULT_MODE


def _is_sdk_version(value: Any) -> bool:
    """An ``mcuhome-sdk.json`` format version this program implements."""
    return isinstance(value, int) and not isinstance(value, bool) and value in SDK_METADATA_VERSIONS


def sdk_entry_point(sdk_path: Path) -> tuple[Path, str]:
    """The code-generation entry point declared at the root of ``trees.sdk``.

    §6.1, normative: ``mcuhome-sdk.json``, "one JSON object, UTF-8 without
    BOM, RFC 8259, read with the JSON parser §5.1 already requires and
    nothing more", fixing three names and no values — ``sdk``,
    ``generate.program``, ``generate.runtime``. Returns the absolute path
    of the program and the runtime string, and raises
    :class:`~mcuhome.model.errors.BuildError` for everything §6.1 calls "code
    generation cannot be reached".

    "A missing file, a missing field and a ``sdk`` version the program
    does not implement are all one situation … and all three fail the
    invocation with ``reason: "error.build.failed"``. They are not
    ``unsupported``: the program implements everything this contract asks
    of it, and no other container would fare better with this SDK
    package." :meth:`_Build._sdk_metadata` is where that becomes a result
    document; here it is an error a caller can also raise while checking
    an SDK package it is *shipping*, which is the second reader this
    function has.

    ``generate.runtime`` is read and not interpreted: it is "an opaque
    string", and the honest consequence the contract states is that a
    conforming container must *provide* the runtime, not that it can check
    the name against anything. It is required to be there because the
    contract fixes the field.
    """
    path = sdk_path / SDK_METADATA_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as failure:
        raise BuildError(
            f"code generation cannot be reached: {path} — {failure}",
            hint=f"an SDK package declares its entry point in {SDK_METADATA_FILE}",
        ) from failure
    generate = data.get("generate") if isinstance(data, dict) else None
    version = data.get("sdk") if isinstance(data, dict) else None
    program = generate.get("program") if isinstance(generate, dict) else None
    runtime = generate.get("runtime") if isinstance(generate, dict) else None
    hint = (
        f"{SDK_METADATA_FILE} declares sdk, generate.program and generate.runtime, "
        "and nothing here interprets their values"
    )
    if not _is_sdk_version(version):
        raise BuildError(
            f"{path} states sdk metadata version {version!r}; this program "
            f"implements {list(SDK_METADATA_VERSIONS)}",
            hint=hint,
        )
    if not isinstance(program, str) or not isinstance(runtime, str):
        raise BuildError(
            f"{path} does not declare both generate.program and generate.runtime",
            hint=hint,
        )
    relative = Path(program)
    if relative.is_absolute() or ".." in relative.parts:
        raise BuildError(
            f"{path} declares generate.program as {program!r}, and it has to be "
            "a path relative to the root of the SDK",
            hint=hint,
        )
    return sdk_path / relative, runtime


class _Build:
    """One build, whichever invocation asked for it.

    The steps, each a method below and each ending either in the next one
    or in a :class:`_BuildFailed`:

    1. refuse a context without ``keys/signing.pub``;
    2. resolve the west workspace, against the ``trees`` the caller named
       where there are any;
    3. build the child environment;
    4. apply the patches of every patched layer, once per session;
    5. reach code generation through the SDK entry point;
    6. compile with ``west build --sysbuild``;
    7. deliver the artifacts into ``out`` and write the build report.

    Steps 2 to 7 are :meth:`execute` and are the same under both
    invocations. What is *not* here is everything only one of them has:
    the legacy contract's context measurement and its ``work`` claim are
    the legacy shell's (:func:`_build`), because a v3 step has no context
    ID to report and an empty ``work`` to start from.

    Nothing here writes into the context — it is a read-only input for the
    whole life of a session. The write scope is ``out``, ``work``, ``tmp``
    and the writable trees, and every path was handed in.
    """

    def __init__(
        self,
        *,
        context_root: Path,
        out_dir: Path,
        work_dir: Path,
        tmp_dir: Path,
        session: str,
        jobs: int,
        record: Path,
        record_document: dict[str, Any],
        given_trees: dict[str, Any] | None = None,
        ccache: Any = None,
        events: _Events | None = None,
        env: dict[str, str] | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self.context_root = context_root
        self.out_dir = out_dir
        self.work_dir = work_dir
        self.tmp_dir = tmp_dir
        self.session = session
        self.jobs = jobs
        #: Where the workspace record came from, for a message that has to
        #: name it, and what it said — already resolved for the profile in
        #: use, which is why the document is passed rather than re-read.
        self.record = record
        self.record_document = record_document
        #: The trees the caller named, where the invocation has such a
        #: field. Empty means "the environment's own", which is the v3
        #: answer and the one a legacy request without ``trees`` gets too.
        self.given_trees: dict[str, Any] = given_trees if isinstance(given_trees, dict) else {}
        #: The legacy contract's ``ccache`` object, or ``None``. The v3
        #: invocation states its cache in *extra_env* instead, from the
        #: tiers the specification gives it.
        self.ccache = ccache
        self.events = events if events is not None else _Events(None)
        #: The environment the program was *told* it runs in, never read
        #: out of the process — see the module docstring. Empty means the
        #: children get only what this program derives.
        self.base_env = dict(env or {})
        #: What the invocation knows and the build environment does not:
        #: applied last, so it wins over what is derived, and applied
        #: before the tools are checked, so a variable that makes a tool
        #: unnecessary counts (:data:`mcuhome.compiler.workspace.TOOLS`).
        self.extra_env = dict(extra_env or {})
        #: The effective context ID, once some caller measured one. The
        #: legacy shell sets it; a v3 step never does.
        self.measured: str | None = None

    # -- refusals ----------------------------------------------------------

    def fail(
        self, reason: str, message: str, details: dict[str, Any] | None = None
    ) -> _BuildFailed:
        """The build did not succeed, said once and rendered by the caller.

        *reason* and *details* are the legacy contract's vocabulary and are
        carried through untouched for it; the v3 result document has no
        field for either and reports *message*, which is what its
        ``message`` is for — "free text for a human".
        """
        return _BuildFailed(reason, message, details)

    # -- the chain ---------------------------------------------------------

    def execute(self, mode: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Everything from the workspace to the delivered artifacts.

        Returns the artifact declarations and the patched-layer block; both
        invocations render those into their own result document. Raises
        :class:`_BuildFailed` for everything that goes wrong on the way.
        """
        topdir, layer_paths = self._workspace()
        env = self._environment(topdir, layer_paths["sdk"])
        layers = self._apply_patches(layer_paths, env)

        if mode == DEFAULT_MODE:
            self._discard_state()
        tree = self._generate(layer_paths["sdk"], env)
        scheme, build_dir, log = self._compile(topdir, tree, env, mode)
        return self._collect(scheme, build_dir, log), layers

    # -- 1. the verification key -------------------------------------------

    def require_signing_key(self) -> None:
        """No ``keys/signing.pub``, no build — and no fallback.

        "A context submitted to a ``build`` that does not carry it fails
        the invocation typed — ``status: "failure"``, ``reason:
        "error.context.incomplete"``, the missing path in
        ``error.details`` — and the program MUST NOT build anyway. There
        is no fallback to MCUboot's default key, because that default is
        MCUboot's demo key and **its private half is published**."

        A key that is present but unusable — wrong curve, a private key by
        mistake — is not typed by contract v1 at all (§7.2 types only the
        absence), so nothing is checked here beyond existence: the file is
        handed to sysbuild, and MCUboot's own tooling is the thing that
        knows what a verification key is.
        """
        if not (self.context_root / SIGNING_KEY_FILE).is_file():
            raise self.fail(
                _REASON_INCOMPLETE,
                f"a build needs {SIGNING_KEY_FILE} as the bootloader's verification key, "
                f"and the context at {self.context_root} carries none",
                {"missing": [SIGNING_KEY_FILE]},
            )

    # -- work, and only under the legacy contract (§6.3) --------------------

    def claim_work(self) -> bool:
        """Read the session marker, then own it. Returns "warm".

        Called by the legacy shell alone: under the v3 specification
        ``work`` is empty at the start of every step, so there is no prior
        state to tell apart from a stranger's and no marker worth writing.

        §6.3: "A program that records a marker **MUST read it before using
        anything in ``work``**, on every invocation. A guard that is
        written and not read is worse than no guard, because it looks like
        one." So this is the first thing that touches ``work``.

        A marker naming another session "is terminal for the invocation …
        it MUST NOT use the state it found, MUST NOT delete or overwrite
        it, and MUST NOT fall back to a private working area of its own
        choosing. It writes nothing into ``work`` in this case, not even
        its own marker." A marker that cannot be parsed is treated the
        same way, for the reason the module docstring gives.

        The return value answers §7.2's ``incremental`` predicate, and
        only that: "no prior state of this session" is a marker this
        invocation had to write, and a marker it found is a previous
        invocation of the same session.
        """
        marker = self.work_dir / WORK_MARKER
        if marker.exists():
            try:
                found = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                found = None
            recorded = found.get("session") if isinstance(found, dict) else None
            if not isinstance(found, dict) or recorded != self.session:
                raise self.fail(
                    _REASON_WORK,
                    f"the work directory {self.work_dir} is marked for another session",
                    {"session": self.session, "found": recorded},
                )
            return True
        _write_json(marker, {"session": self.session})
        return False

    def _discard_state(self) -> None:
        """``clean`` — "fresh workspace" (§7.2), concretely.

        The generated application tree and the CMake tree are this
        program's own state in ``work`` (module docstring), so a fresh
        workspace is those two removed. The session marker and the
        per-layer patch records stay: §6.2 makes patch application "once
        per session" and explicitly not once per invocation, and a
        ``clean`` that re-applied patches would either fail on the second
        build of a session or need a layer reset — "There is no layer
        reset in this contract".
        """
        for name in (WORK_TREE, WORK_BUILD):
            shutil.rmtree(self.work_dir / name, ignore_errors=True)

    # -- 4. the workspace (§6.1) -------------------------------------------

    def _workspace(self) -> tuple[Path, dict[str, Path]]:
        """The west workspace this environment carries, checked against ``trees``.

        The workspace record (``containers/build-container/workspace-record.py``
        writes it, for the baked image and for the workspace package alike)
        says where each layer is, and a ``trees`` entry the caller named is
        accepted only where it names that same path — which is exactly what
        a backend mounting a writable view *over* the tree produces.
        Anything else fails the build, naming the layer and both paths;
        the module docstring says why nothing here can move west's idea of
        where a project lives, and what would replace it.

        A program with no record has no workspace at all, and says so
        rather than compiling against nothing. That is consistent with the
        legacy ``describe`` it would have given: every layer ``"path":
        null``, which asks the backend to supply trees this program then
        could not use — and with :func:`honoured_required`, which honours
        no ``/trees/<layer>`` value at all in that case.
        """
        document = self.record_document
        topdir = document.get("topdir")
        recorded = document.get("layers")
        if not isinstance(topdir, str) or not isinstance(recorded, dict):
            raise self.fail(
                _REASON_BUILD,
                f"this build environment carries no west workspace: {self.record} names none",
            )
        paths = _tree_paths(document)
        missing = [name for name in LAYERS if name not in paths]
        if missing:
            raise self.fail(
                _REASON_BUILD,
                f"the west workspace at {topdir} has no {', '.join(missing)} layer",
            )
        for name, entry in self.given_trees.items():
            if name not in paths or not isinstance(entry, dict):
                continue
            given = entry.get("path")
            if isinstance(given, str) and Path(given) != paths[name]:
                raise self.fail(
                    _REASON_BUILD,
                    f"this program builds the {name} layer at {paths[name]} and cannot "
                    f"move its west workspace to {given}; mount the view there instead",
                )
        return Path(topdir), paths

    # -- 5. the child environment (§6.1, §10) ------------------------------

    def _environment(self, topdir: Path, sdk_path: Path) -> dict[str, str]:
        """What every child of this invocation runs in.

        Assembled from what this program was *told* it runs in — never
        read out of the process (the module docstring) — by
        :func:`~mcuhome.compiler.workspace.build_environment`, which is the one
        definition of a Matter build environment in this package and is
        also what a backend reaches
        for the other direction. It contributes the codegen shim on
        ``PYTHONPATH``, the two job caps that nothing inherits,
        ``ZEPHYR_BASE`` so the generated CMakeLists finds Zephyr and the
        Matter SDK next to it, a writable ``HOME``, and the ``TMPDIR`` §4
        makes the program point at the request's ``tmp``.

        **``HOME`` is in ``work``, and it is not decoration.** The backend
        "runs the program as the calling user where it can" (§2.2), which
        in a container is a UID that has no ``/etc/passwd`` entry — it
        comes from the host — and therefore no home directory either;
        tools that cache in ``$HOME`` fail obscurely without a writable
        one.
        ``work`` rather than ``tmp`` because those caches are worth
        keeping for the next invocation of the session, and §9.2 point 1
        makes ``work`` a place this program may write.

        **What is *not* built here is ``PATH``.** It arrives with the
        environment the caller stated, because it is the one variable
        naming things this program did not put anywhere: the image's
        toolchain, west, ``git``, the runtime ``generate.runtime`` names.
        A program that composed one would be describing a filesystem the
        contract does not own (§4). :func:`~mcuhome.compiler.workspace.require_tools`
        is called on the finished environment before anything is compiled,
        so an environment that cannot start ``west`` is a typed refusal
        naming the tool rather than a child process that fails to exec ten
        minutes in.

        ``limits.jobs`` is taken as given. It is "**authoritative** and
        mandatory for working actions … An optional field would be
        worthless here: a foreign program would fall back to ``nproc``,
        which is exactly the case the field exists against" — so
        :func:`~mcuhome.model.jobs.resolve_jobs` and its auto-detection stay
        on the host side of the contract and are never called here.

        The cache follows §10 exactly **when the request names one**. A
        ``writable: true`` shared cache "MAY be used as the primary
        cache"; a ``writable: false`` one "MUST be treated as a read-only
        secondary cache, with its own primary cache in ``work`` or
        ``tmp``", which is what the two variables below say to ccache.
        ``writable`` is read and never probed (§4.1).

        When the request names **none** — which is the case for every
        backend MCUHome ships — this says nothing about ccache at all,
        and that is deliberate rather than an omission. The build
        environment configures both of ccache's roles itself (this
        image's ``/etc/ccache.conf``: a writable cache, and a read-only
        secondary), an environment variable set here would *override*
        that file rather than agree with it, and what actually lives at
        those two paths is the backend's to decide by mounting or not
        mounting. §10's "any cache is the program's own and dies with the
        session" describes exactly what happens with nothing mounted.

        ``CCACHE_BASEDIR`` is set by nobody, here or anywhere. It
        normalizes absolute paths below it into paths relative to the
        working directory, and every Zephyr compile carries ``-g``, which
        makes ccache hash the working directory regardless — so it
        changed the paths the compiler recorded and bought no hit for it.
        """
        home = self.work_dir / WORK_HOME
        home.mkdir(parents=True, exist_ok=True)
        # Zephyr's user cache (the toolchain capability database) goes
        # where XDG_CACHE_HOME points, then $HOME/.cache — but each
        # candidate only counts if it already EXISTS and is writable
        # (scripts/build/dir_is_writeable.py is a bare os.access), and a
        # fresh HOME has no .cache yet. The fallback is ZEPHYR_BASE/.cache
        # — inside the frozen workspace, where the first configure then
        # dies on the same wall as every other workspace write. So the
        # cache home is stated explicitly and created before anything
        # asks.
        cache_home = self.work_dir / WORK_XDG_CACHE
        cache_home.mkdir(parents=True, exist_ok=True)
        # West caches what it derives: the first `west build` writes
        # `zephyr.base` back into the workspace's own `.west/config`. The
        # baked workspace belongs to whoever built the image, and §2.2
        # runs this program as the calling user — but the deeper point is
        # that the workspace is a frozen input this program never writes,
        # incidental caches included. So the local config is copied into
        # `work` once per session and ``WEST_CONFIG_LOCAL`` points west at
        # the copy: topdir discovery still walks to `.west/`, only the
        # file west reads and writes moves, and any west write invented
        # later lands in `work` with it.
        west_config = self.work_dir / WORK_WEST_CONFIG
        if not west_config.exists():
            try:
                shutil.copyfile(topdir / ".west" / "config", west_config)
            except OSError as error:
                raise self.fail(
                    _REASON_BUILD,
                    f"the west workspace at {topdir} has no readable "
                    f".west/config: {error.strerror}",
                ) from error
        env = workspace.build_environment(
            self.base_env,
            jobs=self.jobs,
            pyshim_dir=sdk_path / workspace.PYSHIM_SUBDIR,
            zephyr_base=topdir / "zephyr",
            tmpdir=self.tmp_dir,
            home=home,
        )
        env["WEST_CONFIG_LOCAL"] = str(west_config)
        env["XDG_CACHE_HOME"] = str(cache_home)
        cache = self.ccache
        if isinstance(cache, dict):
            shared = cache.get("path")
            if cache.get("writable") is True:
                env["CCACHE_DIR"] = str(shared)
            else:
                env["CCACHE_DIR"] = str(self.work_dir / WORK_CCACHE)
                if isinstance(shared, str):
                    # ccache >= 4.8 spells the secondary store this way; the
                    # `|read-only` attribute is what makes the MUST above true
                    # rather than merely intended.
                    env["CCACHE_REMOTE_STORAGE"] = f"file:{shared}|read-only"
        # Last, so that what the invocation knows beats what was derived —
        # and before the tools are checked, because a pre-generated data
        # model is exactly the kind of thing that decides whether a tool is
        # needed at all.
        env.update(self.extra_env)
        try:
            workspace.require_tools(env)
        except BuildError as unusable:
            raise self.fail(
                _REASON_BUILD,
                f"this invocation cannot run a build in the environment it was given: "
                f"{unusable.message}",
            ) from unusable
        return env

    # -- 6. patched layers (§6.2) ------------------------------------------

    def _apply_patches(self, paths: dict[str, Path], env: dict[str, str]) -> dict[str, Any]:
        """Apply every patched layer's patches, once per session.

        §6.2 fixes the semantics and leaves the tool free: "``git apply``,
        ``patch -p1``, or a diff implementation the program brings itself
        are all conforming". This uses ``git apply`` with no ``--3way`` and
        no fallback, which is what ``patches/README.md``, CI and the image
        build already do — and it is what "a patch that does not apply is
        a failure of the invocation and not something to search around"
        asks for.

        The per-layer record in ``work`` is what "once per session" means:
        started is written before the first patch, complete only "after
        the last patch of that layer applied cleanly", and a layer found
        started-but-not-complete is ``error.patch.incomplete`` — terminal,
        because "restoring the pristine baseline is not possible from
        inside the merged view at all".

        Every patched layer appears in the returned ``layers`` block,
        including one an earlier invocation of this session already
        applied: §5.4 makes the block mandatory "for every patched layer"
        of a successful build, and the patch set of a locked context
        cannot change.
        """
        root = self.context_root / PATCHES_DIR
        names = (
            sorted(entry.name for entry in root.iterdir() if entry.is_dir())
            if root.is_dir()
            else []
        )
        records = self.work_dir / WORK_PATCH_RECORDS
        layers: dict[str, Any] = {}
        for name in names:
            # §6.2 types both of its conditions the same way: "If
            # `patches/<layer>/` names a layer for which there is no
            # `trees` entry, **or** which the program does not know, the
            # program MUST NOT proceed: `status: "failure"`, `reason:
            # "error.layer.unknown"`."
            if name not in LAYERS or name not in paths:
                raise self.fail(
                    _REASON_LAYER,
                    f"the context patches a layer this program has no tree for: {name}",
                    {"layer": name},
                )
            entry = self.given_trees.get(name)
            if not isinstance(entry, dict):
                raise self.fail(
                    _REASON_LAYER,
                    f"the context patches the {name} layer and this invocation names no "
                    f"tree for it",
                    {"layer": name},
                )
            if entry.get("writable") is not True:
                # The third case, and the legacy §6.2 does not type it: the
                # entry is there and does not assert that the tree may be
                # written. It is not "no entry" and not "a layer this
                # program does not know", so it is not
                # `error.layer.unknown`; it is the ordinary one.
                raise self.fail(
                    _REASON_BUILD,
                    f"the {name} layer carries patches and this invocation has no writable "
                    "view of it",
                    {"layer": name},
                )
            digest = patchset(root / name)
            layers[name] = {"patchset": digest}
            state = records / f"{name}.json"
            if state.exists():
                try:
                    recorded = json.loads(state.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    recorded = {}
                if isinstance(recorded, dict) and recorded.get("state") == "complete":
                    continue
                raise self.fail(
                    _REASON_PATCH,
                    f"the {name} layer was recorded as started and never completed; "
                    "this session cannot be recovered, start a new one",
                    {"layer": name},
                )
            _write_json(state, {"layer": name, "patchset": digest, "state": "started"})
            patches = sorted((root / name).iterdir(), key=lambda path: path.name.encode("utf-8"))
            for patch in patches:
                command = ["git", "-C", str(paths[name]), "apply", "--verbose", "-p1", str(patch)]
                code, log = _run_child(command, env=env, directory=paths[name])
                if code != 0:
                    raise self.fail(
                        _REASON_BUILD,
                        f"{patch.name} does not apply to the {name} layer: {log.strip()}",
                        {"layer": name},
                    )
            _write_json(state, {"layer": name, "patchset": digest, "state": "complete"})
            self.events.emit("patch.layer.applied", layer=name, count=len(patches))
        return layers

    # -- 7. code generation (§6.1) -----------------------------------------

    def _sdk_metadata(self, sdk_path: Path) -> tuple[Path, str]:
        """:func:`sdk_entry_point`, as a refusal of *this* invocation.

        The reading of ``mcuhome-sdk.json`` is a module-level function so
        that the SDK package's own suite can hold its metadata against the
        rules the program applies to it, rather than against a second
        transcription of §6.1 in a fixture.
        """
        try:
            return sdk_entry_point(sdk_path)
        except BuildError as unreachable:
            raise self.fail(_REASON_BUILD, unreachable.message) from unreachable

    def _generate(self, sdk_path: Path, env: dict[str, str]) -> Path:
        """Invoke the SDK entry point as a child, over this same ABI.

        §6.1: ``<trees.sdk.path>/<generate.program> generate <absolute path
        of a request document>`` — "That is §5.1 unchanged … The program
        writes that request document into its own ``tmp`` and is the
        *backend* of that invocation, in exactly the sense §1.1 defines."

        Two things about the invocation are fixed and both are here: "the
        entry point reads the build context from ``context`` and writes
        the per-device Zephyr application tree into ``out``". Where that
        ``out`` is, is this program's choice as backend — and as backend
        it owes the child what §4's table promises every invocation: an
        ``out`` that is **empty**. So the child writes into a fresh
        per-invocation directory under ``tmp``, and what it produced is
        then absorbed into the session's ``work/tree`` content-aware —
        a file whose bytes are already there is left alone, mtime and
        all. Handing the child ``work/tree`` directly would be cheaper
        and wrong twice over: a foreign SDK entry point (the whole
        point is that it need not be MCUHome's) may rely on the emptiness
        the contract states, and one that lists ``out`` before writing
        would see another invocation's files. The absorb is what keeps
        §7.2's ``incremental`` meaningful — CMake watches the tree's
        mtimes, so an unchanged file must stay untouched.
        "Everything else in the document is between the SDK package and
        itself"; the rest of §5.2's working-action fields are sent because
        the entry point speaks this ABI and they are mandatory in it.

        "A non-zero exit, a missing result document or a ``status`` other
        than ``success`` fails the invocation with ``reason:
        "error.build.failed"``."
        """
        entry, _runtime = self._sdk_metadata(sdk_path)
        tree = self.work_dir / WORK_TREE
        scratch = self.tmp_dir / GENERATE_ACTION
        scratch.mkdir(parents=True, exist_ok=True)
        tree.mkdir(parents=True, exist_ok=True)
        child_out = scratch / "out"
        child_out.mkdir(parents=True, exist_ok=True)
        child_work = self.work_dir / GENERATE_ACTION
        child_work.mkdir(parents=True, exist_ok=True)
        request_path = scratch / "request.json"
        result_path = scratch / "result.json"
        _write_json(
            request_path,
            {
                "request": REQUEST_VERSIONS[0],
                "result": str(result_path),
                "session": self.session,
                "out": str(child_out),
                "work": str(child_work),
                "tmp": str(scratch),
                "context": str(self.context_root),
                "trees": {"sdk": dict(self.given_trees["sdk"])},
                "limits": {"jobs": self.jobs},
            },
        )
        code, log = _run_child(
            [str(entry), GENERATE_ACTION, str(request_path)], env=env, directory=scratch
        )
        if code != 0:
            raise self.fail(
                _REASON_BUILD,
                f"code generation exited {code}: {log.strip()}",
            )
        try:
            answer = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as failure:
            raise self.fail(
                _REASON_BUILD,
                f"code generation wrote no readable result document: {failure}",
            ) from failure
        if not isinstance(answer, dict) or answer.get("status") != _STATUS_SUCCESS:
            said = answer.get("reason") if isinstance(answer, dict) else answer
            raise self.fail(_REASON_BUILD, f"code generation did not succeed: {said!r}")
        written = _absorb_tree(child_out, tree)
        self.events.emit("generate.written", files=written)
        return tree

    # -- 8. the compile (stage 5) ------------------------------------------

    def _compile(
        self, topdir: Path, tree: Path, env: dict[str, str], mode: str
    ) -> tuple[Any, Path, str]:
        """``west build --sysbuild``, assembled by stage 5 and run by it.

        Nothing about the command is decided here:
        :func:`~mcuhome.compiler.workspace.west_build_command` already knows the
        per-image snippet rule, and its ``detached_signing`` path is
        exactly what §7.2 requires — "It MUST use ``keys/signing.pub``
        from the context as the bootloader's verification key" and "The
        program MUST NOT sign images". With that flag the key handed to
        sysbuild is the public half and the generated tree clears the
        application's signing step, so no signed file is produced at all.

        The device model comes out of the context (§3.1 puts it at
        ``model/device-model.json``) through
        :func:`~mcuhome.model.modelfile.read_model`, which is documented as
        exactly this receiving end. A board this builder has no update scheme for
        is ``error.build.failed``: §7.2.1 makes the ``signing`` block
        mandatory, and there is nothing to put in it.

        ``clean`` is ``--pristine always`` regardless of what the build
        directory looks like; ``incremental`` lets
        :func:`~mcuhome.compiler.workspace.pristine_mode` answer, which is the
        function that already knows the one case ``auto`` cannot cover.
        """
        try:
            model = read_model(self.context_root / MODEL_FILE)
        except BuildError as failure:
            raise self.fail(_REASON_BUILD, failure.message) from failure
        board = registry.BOARDS.get(model.device.board)
        scheme = None if board is None else board.update_scheme
        if scheme is None:
            raise self.fail(
                _REASON_BUILD,
                f"this builder has no MCUboot layout for {model.device.board}, so it "
                "cannot state the signing parameters a build report has to carry",
            )
        app_dir = tree / APP_DIR
        build_dir = self.work_dir / WORK_BUILD
        plan = workspace.BuildPlan(
            topdir=topdir,
            app_dir=app_dir,
            build_dir=build_dir,
            command=workspace.west_build_command(
                app_dir=app_dir,
                build_dir=build_dir,
                board=model.device.board,
                snippets=tuple(model.build.snippets),
                bootloader_snippets=scheme.bootloader_snippets,
                signing_key=self.context_root / SIGNING_KEY_FILE,
                detached_signing=True,
                jobs=self.jobs,
                pristine="always" if mode == DEFAULT_MODE else workspace.pristine_mode(build_dir),
            ),
            env=env,
        )
        try:
            code, log = workspace.run_build(plan)
        except BuildError as failure:
            # The hint carries what the message cannot — for a build that
            # never started, the exact command line — and §5.4's details
            # is the field that exists for it. Dropping it once reduced
            # "could not start the build: No such file or directory" to a
            # riddle with no file name in it.
            details = {"hint": failure.hint} if failure.hint else None
            raise self.fail(_REASON_BUILD, failure.message, details) from failure
        if code != 0:
            raise self.fail(_REASON_BUILD, f"west build exited with {code}")
        return scheme, build_dir, log

    # -- 9. what leaves (§7.2, §7.2.1) -------------------------------------

    def _deliver(self, source: Path, name: str, role: str) -> dict[str, Any]:
        """Copy one file into ``out`` and declare it.

        The four mandatory fields of §5.4 and nothing else: ``root`` is
        ``"out"``, the only legal value in v1; ``path`` is relative to it
        with segments matching ``[A-Za-z0-9._-]+``; ``role`` identifies it
        by function; ``hashes`` is keyed by algorithm and read back from
        disk. No size — "an artifact entry declares no size".

        Copied rather than declared where the linker left it, because §7.2
        states what MCUHome's own container writes and because everything
        in ``out`` is then something this program put there deliberately:
        sysbuild's combined hex, which on a never-signed build is the
        *unsigned* application under a flashable-looking name, simply
        never arrives.
        """
        destination = self.out_dir / name
        _write_file(destination, source.read_bytes())
        digest = sha256_file(destination)
        self.events.emit(
            "artifact.collected", role=role, path=name, size=destination.stat().st_size
        )
        return {"root": "out", "path": name, "role": role, "hashes": {"sha256": digest}}

    def _collect(self, scheme: Any, build_dir: Path, log: str) -> list[dict[str, Any]]:
        """The artifacts a successful build declares (§7.2).

        "A successful device build MUST declare at least two artifacts:
        the unsigned image with role ``firmware`` … and **exactly one
        artifact with role ``report``**". Both firmware files carry the
        ``firmware`` role and the bootloader is declared as well — the
        module docstring says why for each.

        There is no ``ota`` role and no ``.ota`` file: "The OTA wrapper's
        payload has to be the **signed** binary and the same contract
        forbids the program to sign, so the requirement cancelled itself."
        """
        images = workspace.build_images(build_dir, app_image=APP_DIR)
        app_output = build_dir / APP_DIR / "zephyr"
        artifacts: list[dict[str, Any]] = []
        for produced, delivered in FIRMWARE_ARTIFACTS:
            source = app_output / produced
            if not source.is_file():
                raise self.fail(
                    _REASON_BUILD,
                    f"the build produced no {produced} for the application image",
                )
            artifacts.append(self._deliver(source, delivered, "firmware"))
        produced, delivered = BOOTLOADER_ARTIFACT
        bootloader = build_dir / workspace.BOOTLOADER_IMAGE / "zephyr" / produced
        if bootloader.is_file():
            artifacts.append(self._deliver(bootloader, delivered, "bootloader"))

        memory = workspace.parse_image_memory_report(log, images=[image.name for image in images])
        # By image name, and that is not cosmetic: the parser answers in
        # the order the log carried, which is the order sysbuild happened
        # to relink in. Two builds of one context can schedule those links
        # either way round, and a report that followed the log would then
        # differ between them although every number in it is the same —
        # which a consumer comparing two builds byte for byte reads as a
        # difference in the firmware. Within one image the linker's own
        # order is kept: it comes from one block of one log, and it is the
        # order a person reads a memory map in.
        regions = [
            {
                "image": image,
                "region": region.name,
                "used": region.used,
                "total": region.total,
                "percent": region.percent,
            }
            for image, found in sorted(memory.items())
            for region in found
        ]
        for region in regions:
            self.events.emit("build.memory.region", **region)
        _write_file(
            self.out_dir / REPORT_ARTIFACT,
            (json.dumps(self._report(scheme, build_dir, regions), indent=2) + "\n").encode("utf-8"),
        )
        artifacts.append(
            {
                "root": "out",
                "path": REPORT_ARTIFACT,
                "role": "report",
                "hashes": {"sha256": sha256_file(self.out_dir / REPORT_ARTIFACT)},
            }
        )
        self.events.emit(
            "artifact.collected",
            role="report",
            path=REPORT_ARTIFACT,
            size=(self.out_dir / REPORT_ARTIFACT).stat().st_size,
        )
        return artifacts

    def _report(
        self, scheme: Any, build_dir: Path, regions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The build report of §7.2.1 — one JSON object, for one consumer.

        "It exists for one consumer and one purpose: the client that signs
        detached, which is the only party holding the private key." So it
        carries the ``report`` version, the mandatory ``signing`` block and
        the optional ``memory`` list, and nothing else: the contract
        strikes ``signed``, ``signed_by_the_build``, ``inputs`` and
        ``outputs`` by name,
        because "a build container never signs, the input is the
        ``firmware`` artifact, and where the signed output goes is the
        signer's business".

        The four arguments are :func:`~mcuhome.compiler.report.signing_parameters`
        unchanged — three of them board data the build already had to know
        and the fourth, imgtool's ``--version``, read out of the built
        application's own ``.config``, which is the behaviour §7.2.1 cites.
        A build that left no ``.config`` behind has no version to state,
        and stating Zephyr's ``0.0.0+0`` default for it would produce a
        signed image MCUboot compares monotonically against the wrong
        number — so that is ``error.build.failed`` rather than a report.

        ``memory`` is omitted when the build relinked nothing: "A build
        that relinked nothing reports none, which is correct rather than
        incomplete."
        """
        kconfig = build_dir / APP_DIR / "zephyr" / ".config"
        if not kconfig.is_file():
            raise self.fail(
                _REASON_BUILD,
                f"the build left no {kconfig.name} for the application image, so the "
                "signing parameters §7.2.1 makes mandatory cannot be stated",
            )
        try:
            parameters = report.signing_parameters(scheme, kconfig=report.read_kconfig(kconfig))
        except BuildError as failure:
            raise self.fail(_REASON_BUILD, failure.message) from failure
        document: dict[str, Any] = {
            "report": REPORT_VERSION,
            "signing": {
                "signature_type": registry.SIGNATURE_TYPE,
                "arguments": parameters.to_dict(),
            },
        }
        if regions:
            document["memory"] = regions
        return document


def _build(
    echo: dict[str, Any], document: dict[str, Any], record: Path, env: dict[str, str] | None
) -> dict[str, Any]:
    """``build`` (§7.2) as the legacy contract asks for it.

    The shell around :class:`_Build`: it reads the invocation out of the
    request document, adds the two steps that are the legacy contract's
    alone — the effective context ID (§3.3) and the ``work`` claim (§6.3)
    — and renders whatever comes back into a result document of that
    contract's shape.
    """
    events = _Events(document.get("events"))
    given = document.get("trees")
    builder = _Build(
        context_root=Path(document["context"]),
        out_dir=Path(document["out"]),
        work_dir=Path(document["work"]),
        tmp_dir=Path(document["tmp"]),
        session=document["session"],
        jobs=int(document["limits"]["jobs"]),
        record=record,
        record_document=_record_document(record),
        given_trees=given if isinstance(given, dict) else {},
        ccache=document.get("ccache"),
        events=events,
        env=env,
    )
    events.emit("invocation.started", action="build")
    try:
        verification = _open_context(echo, builder.context_root)
        builder.measured = verification.actual_id
        events.emit("context.checked", context=builder.measured)
        builder.require_signing_key()
        # §7.2: "An `incremental` for which the program finds no prior
        # state of *this session* in `work` is executed as `clean`." The
        # marker is the predicate — see the module docstring for why this
        # program writes one at all.
        warm = builder.claim_work()
        mode = _requested_mode(document) if warm else DEFAULT_MODE
        artifacts, layers = builder.execute(mode)
        result = _result_document(
            echo,
            _STATUS_SUCCESS,
            context=builder.measured,
            artifacts=artifacts,
            layers=layers,
        )
    except _Refused as refused:
        result = refused.document
    except _BuildFailed as failed:
        result = _failure(
            echo,
            failed.reason,
            failed.message,
            details=failed.details,
            context=builder.measured,
        )
    events.emit("invocation.finished", status=result["status"])
    return result


# --------------------------------------------------------------------------
# The invocation (§5.1)
# --------------------------------------------------------------------------


def _invoke(
    action: str, document: dict[str, Any], record: Path, env: dict[str, str] | None
) -> dict[str, Any]:
    """One invocation, in the order of §5.1's bootstrap chain.

    §5.4's table makes ``program`` mandatory in a ``describe`` result and
    qualifies the row with nothing — "Everything not qualified is
    mandatory unconditionally" — so a ``describe`` that *refuses* carries
    the block too. Nothing is fabricated by doing so: the block is static
    self-description, and a refusal is exactly the moment a backend needs
    it, since ``program.request`` is what tells it which request format
    version to send instead. In a ``verify`` or ``build`` result the block
    is a MAY, and this program omits it there.
    """
    echo: dict[str, Any] = {"action": action}
    if "session" in document:
        # The echo rule (§5.4): whatever the request carried, verbatim.
        # ``session`` is an opaque token; nothing here composes a path from
        # it, so its type never has to be believed.
        echo["session"] = document["session"]
    block = program(record) if action == "describe" else None

    def refuse(reason: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        return _refusal(echo, reason, message, details=details, program=block)

    if not _is_request_version(document.get("request")):
        return refuse(
            _REASON_REQUEST,
            f"request format version {document.get('request')!r} is not implemented; "
            f"this program parses {list(REQUEST_VERSIONS)}",
        )

    if action not in IMPLEMENTED_ACTIONS:
        return refuse(
            _REASON_ACTION,
            f"action {action!r} is not implemented; this program implements "
            f"{list(IMPLEMENTED_ACTIONS)}",
        )

    offending = _unhonourable(action, document, record)
    if offending is None:
        return refuse(_REASON_REQUEST, "'required' is not an array of JSON Pointers")
    if offending:
        return refuse(
            _REASON_REQUIRED,
            f"this program does not honour {', '.join(offending)} with the value given",
            details={"required": offending},
        )

    relative = _relative_paths(document)
    if relative:
        return refuse(
            _REASON_REQUEST,
            f"every path value is absolute; these are not: {', '.join(relative)}",
        )

    unfound = _not_found(action, document)
    if unfound:
        return refuse(
            _REASON_REQUEST,
            f"{action!r} needs {', '.join(unfound)}, and this request document "
            f"supplies no usable value there",
        )

    bad_mode = _unsupported_mode(action, document)
    if bad_mode is not None:
        return refuse(
            _REASON_REQUIRED,
            f"this program implements the build modes {list(MODES)}, not {bad_mode!r}",
            details={"required": ["/params/mode"]},
        )

    if action == "verify":
        return _verify(echo, document)
    if action == "build":
        return _build(echo, document, record, env)
    return _describe(echo, record)


def main(
    argv: list[str],
    *,
    record: Path = WORKSPACE_RECORD,
    env: dict[str, str] | None = None,
) -> int:
    """One invocation of the program; the return value is its exit code.

    *argv* is the whole command line, program name included, exactly as a
    launcher hands ``sys.argv`` over: ``argv[1]`` is the action and
    ``argv[2]`` the absolute path of the request document. "Exactly two
    positional operands, both mandatory, **never a flag**. Any other arity
    is exit 66" (§5.1) — so there is no option parser here and there never
    will be one, because the argv is frozen and extensibility runs through
    the request document alone.

    *record* is where the image's workspace record is looked for, and is a
    parameter only so a test can point it somewhere. Nothing on the
    command line moves it: it is a property of the image, not of an
    invocation.

    *env* is the environment a ``build``'s child processes start from, and
    it is **stated by whoever started this program, never read out of the
    process**. That distinction is the invariant
    ``tests/python/test_userpaths.py`` enforces on every module here: one
    process serves several sessions, and a call-time ``os.environ`` is
    what makes two of them answer each other's questions. A caller that
    states nothing gets children that run in exactly what §6.1 makes the
    program's own responsibility and nothing else — which is correct and
    thin, and is why ``containers/build-container/run`` will have to hand its
    image's environment over before the first real compile happens inside
    the image (the other half of that, ``mcuhome-sdk.json``, is §6.1's and
    lives in the SDK package).

    **A relative ``argv[2]`` is exit 66**, alongside the wrong arity.
    §5.1 states the operand as "<absolute path of the request document>"
    and then forbids the only thing that could make a relative one
    meaningful: "the working directory is meaningless: a program MUST NOT
    rely on any ``cwd``". Reading it anyway is the one failure this
    module could have that nobody would see — the path resolves against
    whatever directory the backend happened to leave the process in, so
    it either finds nothing or, worse, finds a *different* request
    document and answers that one. Refusing costs a conforming backend
    nothing, because a conforming backend never sends one.
    """
    return run_invocation(argv, lambda action, document: _invoke(action, document, record, env))


def run_invocation(
    argv: list[str],
    invoke: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> int:
    """§5.1's outer sequence around *invoke*; the return value is the exit code.

    Shared between the program itself (:func:`main`) and the SDK
    package's entry point (:mod:`mcuhome.compiler.sdkentry`), because §6.1 reuses
    the invocation ABI on purpose: "A second calling convention would be
    a second frozen thing … this way the entry point is reached with the
    parser, the two documents and the four exit values every conforming
    program has anyway." Sharing the sequence is what keeps that a fact
    of the code rather than a claim about two transcriptions of it.

    **A crash inside *invoke* becomes a result document, not a
    traceback.** §5.3's exit 1 "promises a result document", and the
    table leaves every other exit as "the program died. Undefined
    forever" — so an unexpected exception after the preamble was read is
    answered on the channel that was already open: ``status:
    "failure"``, the exception in ``error.message``, exit 1. The
    ``reason`` is ``error.internal`` — the registry value §5.4.1's
    erratum added for exactly this ("the program itself failed, in any
    action"), so a backend is never told a ``describe`` or ``verify``
    crash was a build-work failure. Only when even that document cannot
    be written does the invocation end with exit 66 — the same answer
    the ordinary write path gives, because a result nobody can address
    is that case whatever was computed.
    """
    if len(argv) != 3:
        return EXIT_UNUSABLE
    action, request = argv[1], argv[2]
    if not _is_absolute_path(request):
        return EXIT_UNUSABLE

    try:
        document = _parse_request(request)
        destination = _result_path(document)
    except _Unusable:
        return EXIT_UNUSABLE

    echo: dict[str, Any] = {"action": action}
    if "session" in document:
        echo["session"] = document["session"]
    try:
        result = invoke(action, document)
    except Exception as died:  # noqa: BLE001 - the catch-all IS the contract duty
        result = _result_document(
            echo,
            _STATUS_FAILURE,
            reason=_REASON_INTERNAL,
            error={
                "retryable": False,
                "message": f"the program failed inside the action: {died}",
                "details": {},
            },
        )

    try:
        _write_atomically(destination, result)
    except OSError:
        return EXIT_UNUSABLE

    return EXIT_SUCCESS if result["status"] == _STATUS_SUCCESS else EXIT_FAILURE


# ==========================================================================
# The v3 invocation (docs/spec/build-environment-specification.md)
# ==========================================================================
#
# The primary invocation, and self-contained: nothing above this line
# reaches into it, so it moves out whole the day the legacy half is
# deleted. What it shares with that half is the builder (:class:`_Build`)
# and the atomic write, and nothing else.

#: The generation of the build environment specification this program
#: implements (§12). A request stating another one is answered
#: ``unsupported``, which is the specification's own instruction: "the
#: side that notices refuses".
SPEC_GENERATION = 3

#: The one environment variable the specification defines (§4). It is
#: "often ``/``, but never assume it", so every path of a step is resolved
#: against it at the start of that step, and none is kept for the next one.
BASE_DIR_VAR = "MCUHOME_BUILDER_BASE_DIR"

#: Where the unpacked ``mcuhome-build-workspace`` package is. Not the
#: specification's business — where an environment keeps its own content is
#: deliberately its own affair — but MCUHome's own environment has to find
#: it, and neither profile puts it at a path that could be a constant here
#: (``packaging/build-environment/README.md``).
WORKSPACE_PACKAGE_VAR = "MCUHOME_BUILD_ENV_WORKSPACE"

#: What that package says about itself: where its workspace is, where its
#: record is, and what a build hands CHIP as the pre-generated data model.
WORKSPACE_PACKAGE_MANIFEST = "build-workspace.json"

#: §4's tree, below the base directory: everything the orchestrator manages
#: is in here, and everything outside it is the environment's own content.
STEP_DIR = "mcuhome"
STEP_REQUEST = "invocation-request.json"
STEP_OUT = "out"
STEP_WORK = "work"
STEP_SDK = "sdk"
STEP_CONTEXT = "build-context"
STEP_CACHE = "cache"

#: The scratch directory this program points ``TMPDIR`` at — §7: "Point
#: ``TMPDIR`` at a directory inside it if the tools you drive need one".
#: Inside ``work``, which is empty at the start of every step.
STEP_TMP = "tmp"

#: Where a tree is copied to before its patches are applied — §10:
#: "Materialize a patched copy of it under ``work``". One subdirectory per
#: layer. The ``sdk`` layer lands here, because it is the orchestrator's
#: input and stands outside the workspace; a tree of the environment's own
#: workspace is copied to its place in the view below instead, so that it
#: keeps its position relative to the other trees.
STEP_PATCHED = "patched"

#: §10's "view of the environment in which that copy stands in for the
#: original": a directory under ``work`` that mirrors the workspace's top
#: level with links to the originals, holds the patched copies at their own
#: place inside it, and carries the delivered SDK where west looks for the
#: manifest repository. **Every step builds one**, whatever the environment's
#: trees are — this program never writes into them
#: (:func:`_workspace_view`).
STEP_VIEW = "view"

#: The four cache tiers (§8), most local first. ``local`` is the only one
#: that is always writable, and is therefore the primary cache; every other
#: one is read-only, may be missing entirely, and may be something else
#: next step.
CACHE_TIERS = ("local", "session", "project", "shared")

#: Where a cache tier keeps the compiler cache — §8: "**ccache** goes in
#: ``<tier>/ccache``".
CCACHE_SUBDIR = "ccache"

#: ``out/result-<invocation_id>.json`` (§6.2). Names matching
#: ``result-*.json`` at the top of ``out`` are reserved for it (§7).
RESULT_PREFIX = "result-"
RESULT_SUFFIX = ".json"

#: The actions this environment implements (``docs/spec/build-actions.md``).
#: Every other one is answered ``unsupported``, which means "no environment
#: of my kind can do this" and lets the orchestrator look for a different
#: environment rather than report a broken build.
STEP_ACTIONS = ("build",)

#: An ``invocation_id`` this program is willing to put into a file name.
#: §6.1 promises it is "safe to use directly in a filename"; a value that
#: is not gets no result document at all, rather than one at a path
#: composed out of somebody else's ``..``.
_SAFE_INVOCATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class _Step:
    """§4's tree for one step, resolved against the base directory."""

    def __init__(self, base: Path) -> None:
        root = base / STEP_DIR
        self.base = base
        self.request = root / STEP_REQUEST
        self.out = root / STEP_OUT
        self.work = root / STEP_WORK
        self.tmp = self.work / STEP_TMP
        self.sdk = root / STEP_SDK
        self.context = root / STEP_CONTEXT
        self.cache = root / STEP_CACHE

    def result(self, invocation_id: str) -> Path:
        """Where this step's result document goes (§6.2)."""
        return self.out / f"{RESULT_PREFIX}{invocation_id}{RESULT_SUFFIX}"

    def tier(self, name: str) -> Path:
        """One cache tier (§8), whether or not anything is mounted there."""
        return self.cache / name


class CarriedWorkspace(NamedTuple):
    """What the environment's own packages provide a build with.

    Read once per step, because a package is unpacked wherever the profile
    put it and §4 forbids keeping an absolute path from one step to the
    next.
    """

    #: The west workspace's top directory, where it actually is now.
    topdir: Path
    #: The workspace record, for a message that has to name it.
    record: Path
    #: What the record says, with its layer paths moved to *topdir*.
    record_document: dict[str, Any]
    #: What to hand CHIP as ``CHIP_CODEGEN_PREGEN_DIR``, or ``None`` when
    #: the package carries no pre-generated data model.
    pregen_chip_root: Path | None


def _rebased(document: dict[str, Any], topdir: Path, record: Path) -> dict[str, Any]:
    """*document*'s layer paths, moved to where the package actually is.

    A workspace record is written while the workspace is being built, so it
    names the paths of the machine that built it — and the package is then
    unpacked somewhere else entirely: a store entry under the user's cache,
    a directory in an image. The layers keep their place *inside* the
    workspace, so moving the top directory moves every one of them, and a
    record whose top directory is already the real one rebases to itself.

    A record that names no workspace at all comes back empty, which is what
    :meth:`_Build._workspace` refuses with the record's own path in the
    message. A layer *outside* the recorded workspace is the one case that
    cannot be moved with it, and it is a refusal rather than a guess.
    """
    recorded = document.get("topdir")
    layers = document.get("layers")
    if not isinstance(recorded, str) or not isinstance(layers, dict):
        return {}
    moved: dict[str, Any] = {}
    for name, entry in layers.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        try:
            inside = Path(entry["path"]).relative_to(recorded)
        except ValueError as outside:
            raise BuildError(
                f"{record} puts the {name} layer at {entry['path']}, outside the "
                f"workspace it records at {recorded}",
                hint="a packaged workspace carries its layers inside itself, or it "
                "cannot be unpacked anywhere but the machine that built it",
            ) from outside
        moved[name] = {**entry, "path": str(topdir / inside)}
    return {**document, "topdir": str(topdir), "layers": moved}


def environment_workspace(root: Path) -> CarriedWorkspace:
    """What the ``mcuhome-build-workspace`` package at *root* provides.

    The package states where its own parts are
    (``packaging/build-environment/README.md``), so this reads them rather
    than reconstructing them: a layout the package and this program both
    hardcode is a layout that breaks silently the day one of them changes.
    Raises :class:`~mcuhome.model.errors.BuildError` when the package
    cannot be read as one — which is a failed step, and never
    ``unsupported``: the environment implements everything asked of it, and
    no other environment would fare better with a broken package.
    """
    manifest_path = root / WORKSPACE_PACKAGE_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        raise BuildError(
            f"this build environment cannot read {manifest_path}: {unreadable}",
            hint=f"{WORKSPACE_PACKAGE_VAR} names the unpacked mcuhome-build-workspace package",
        ) from unreadable
    workspace_dir = manifest.get("workspace") if isinstance(manifest, dict) else None
    record_name = manifest.get("workspace-record") if isinstance(manifest, dict) else None
    if not isinstance(workspace_dir, str) or not isinstance(record_name, str):
        raise BuildError(
            f"{manifest_path} does not say where its workspace and its record are",
            hint="a mcuhome-build-workspace package states both in build-workspace.json",
        )
    topdir = root / workspace_dir
    record = root / record_name
    try:
        document = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as unreadable:
        raise BuildError(
            f"this build environment cannot read its workspace record {record}: {unreadable}",
        ) from unreadable
    pregen = manifest.get("matter-pregen-chip-root")
    return CarriedWorkspace(
        topdir=topdir,
        record=record,
        record_document=_rebased(document if isinstance(document, dict) else {}, topdir, record),
        pregen_chip_root=root / pregen if isinstance(pregen, str) else None,
    )


def _delivered_sdk(step: _Step, manifest_dir: Path) -> Path:
    """The SDK this step builds against, wherever the profile put it.

    §4 delivers it at ``mcuhome/sdk``, which is where this looks first. A
    profile that mounts it straight into the workspace instead — the baked
    image does — is the second place, and the only other one there is.
    """
    for candidate in (step.sdk, manifest_dir):
        if (candidate / SDK_METADATA_FILE).is_file():
            return candidate
    raise BuildError(
        f"there is no MCUHome SDK at {step.sdk}",
        hint="the orchestrator delivers the SDK there, and no application can be "
        "generated without it",
    )


def _patched_copy(source: Path, target: Path) -> Path:
    """A copy of *source* under ``work``, for the patches to be applied to.

    §10's rule for a tree that may not be written: "Materialize a patched
    copy of it under ``work``, and build against a view of the environment
    in which that copy stands in for the original. Trees no patch names
    stay where they are; copying is the price of a patch and is paid only
    for the trees a patch actually names."

    The delivered SDK is copied here: it is the orchestrator's input,
    shared with whoever else holds those bytes, and §4 says to assume it
    cannot be written. A tree of the environment's **own** workspace is
    copied to its place inside the view (:func:`_workspace_view`) instead,
    so that it keeps its position relative to every other tree.

    **The copy is made writable.** ``shutil.copytree`` copies the modes
    with the bytes, so a copy taken out of a frozen store is as unwritable
    as the store — and the whole point of the copy is that the patches go
    into it.
    """
    try:
        shutil.copytree(source, target, symlinks=True)
        _make_writable(target)
    except OSError as failure:
        raise BuildError(
            f"this build environment cannot copy {source} to {target} to patch it: "
            f"{failure.strerror}",
            hint="a patched tree is materialized under work, so that what the "
            "orchestrator delivered is never modified",
        ) from failure
    return target


def _make_writable(root: Path) -> None:
    """Give the owner write access to everything under *root*.

    Top-down, so a directory is writable before its own entries are
    reached, and symbolic links are left alone: their permission bits mean
    nothing on Linux and following them would change the mode of whatever
    they point at — which, for a copy of a store tree, is the store.
    """
    for directory, _subdirectories, files in os.walk(root):
        base = Path(directory)
        base.chmod(base.stat().st_mode | 0o200)
        for name in files:
            entry = base / name
            if not entry.is_symlink():
                entry.chmod(entry.stat().st_mode | 0o200)


def _patched_layers(context_root: Path) -> set[str]:
    """The layers this context carries patches for, by name.

    The same listing :meth:`_Build._apply_patches` does, read one step
    earlier: which trees are patched decides which of them have to be
    copied, and that has to be settled before anything is built against
    them. A name no layer answers to stays in the set — it is
    ``error.layer.unknown`` when the patches are applied, and inventing a
    second place that refuses it would put the two out of step.
    """
    root = context_root / PATCHES_DIR
    if not root.is_dir():
        return set()
    return {entry.name for entry in root.iterdir() if entry.is_dir()}


def _mirror_tree(source: Path, target: Path, *, base: Path, skipped: set[Path]) -> None:
    """*source* reproduced at *target* as a real tree over the same bytes.

    Directories are created, symbolic links are recreated with the target
    they had — a relative one then points inside the mirror, exactly as it
    pointed inside the original — and every file becomes a **hard link** to
    the original file. So the mirror costs directory entries and no file
    bytes, and every path in it resolves to itself rather than into the
    original tree, which is what west's containment checks need
    (:func:`_workspace_view`).

    A hard link shares the original's inode and therefore its permission
    bits, which a frozen store makes read-only: a build cannot write
    through the mirror any more than it could write through the symbolic
    links this replaced. On a tree that is *writable* the bits are the
    tree's own and the mirror carries them, so what protects such a
    workspace is not the filesystem but this program: the trees a patch
    names are copies, and no other step of a build writes into a source
    tree. That holds for every write this program makes — the tests hold
    the whole workspace against its own bytes before and after a step —
    and it is a statement about this program rather than about the tools
    it drives. Where a link cannot be made — the workspace on
    another filesystem than the step's work directory — the file is copied
    instead, so the view is correct on such a machine too and only pays
    for it.

    *skipped* are the paths, relative to the workspace's top directory,
    that the view fills in itself: a patched copy or the delivered SDK
    nested inside a mirrored tree. They are left out here rather than
    mirrored and then overwritten.
    """
    target.mkdir(parents=True, exist_ok=True)
    stack = [(source, target, base)]
    while stack:
        directory, into, relative = stack.pop()
        for entry in sorted(os.scandir(directory), key=lambda entry: entry.name):
            child = relative / entry.name
            if child in skipped:
                continue
            destination = into / entry.name
            if entry.is_symlink():
                os.symlink(os.readlink(entry.path), destination)
            elif entry.is_dir():
                destination.mkdir()
                stack.append((Path(entry.path), destination, child))
            else:
                try:
                    os.link(entry.path, destination)
                except OSError:
                    shutil.copy2(entry.path, destination, follow_symlinks=False)


def _workspace_view(
    topdir: Path,
    view: Path,
    *,
    linked: dict[Path, Path],
    mirrored: set[Path],
    materialized: set[Path],
) -> Path:
    """§10's view of the environment, and the one workspace a step builds in.

    "Materialize a patched copy of it under ``work``, and build against a
    view of the environment in which that copy stands in for the original.
    Trees no patch names stay where they are." The view is that
    environment: a directory under ``work`` that has the workspace's own
    shape, with symbolic links where the environment's trees are good
    enough and the step's own directories where they are not.

    **Every step builds one, and nothing decides otherwise.** §10 permits
    a *disposable* tree to be patched in place, and this program does not
    take that permission: it never writes into the environment's own
    trees, whether they are a frozen store entry, an image's workspace or
    a west workspace somebody is working in. What a profile hands over is
    then not a question this program has to answer, and there is one code
    path to test rather than two that differ in what a write probe
    happened to return.

    **What the view costs**, and it is paid on every step of every
    profile. The patched trees are copies and that is the patches' price.
    Everything else costs no file bytes but it is not free either: the
    layers this step keeps are mirrored as real directories with a hard
    link per file (:func:`_mirror_tree`), and on the real workspace
    package that is 90 005 hard links and 17 878 directories for the three
    mirrored layers — ≈ 3 s and ≈ 71 MB of directory entries, patches or
    none. Where a hard link cannot be made — the workspace on another
    filesystem than ``work`` — those files are copied and the view costs
    their bytes too, and a container is such a place even though it looks
    like one filesystem: ``work`` is the writable layer and a baked
    workspace is a layer below it, so overlayfs copies a file up before it
    links it. A profile that cares about that puts ``work`` where the
    environment's workspace actually lives.

    Three kinds of entry, and everything else in the workspace is a link
    to the original:

    *linked* are trees that stand somewhere else entirely — the delivered
    SDK, which west has to find inside the workspace and which no profile
    can put there. The link is made by name and unconditionally; where the
    delivered SDK *is* the workspace's own manifest checkout, it points at
    that same directory and nothing is written anywhere.

    *materialized* are trees already copied to their own place inside the
    view: the patched ones. They are skipped rather than linked, because
    they are already there.

    *mirrored* are the environment's own trees that this step keeps — each
    one reproduced inside the view as a real tree: real directories, and
    hard links to the original files (:func:`_mirror_tree`). **That the
    tree is real is not cosmetic, and neither is its depth.** Two tools
    insist on it, each in its own way.

    ``os.getcwd`` resolves symbolic links, so a tool started with its
    working directory inside a *linked* tree is, as far as the kernel is
    concerned, in the original — and west then walks up from there and
    finds the environment's own workspace rather than this view. Zephyr's
    build does exactly that: ``cmake/modules/zephyr_module.cmake`` runs
    ``scripts/zephyr_module.py`` with the working directory set to
    ``ZEPHYR_BASE``, and that script asks west for the workspace's
    projects. With the tree linked, the answer names the store's trees and
    a patched copy is silently not built against.

    And west **resolves both sides** when it checks that a project's
    ``west-commands`` file stays inside that project
    (``west.util.escapes_directory``, called from
    ``west.commands._ext_specs``). A directory of symbolic links is not
    enough for that check: the project directory resolves to the view and
    the file behind the link resolves to the store, so west refuses the
    workspace outright — with ``west-commands file … escapes project
    path …``, before any command runs. Zephyr and MCUboot both declare
    ``west-commands``, and ``west build`` is itself such an extension, so
    the whole build depends on the file resolving inside the view. Hard
    links give exactly that: the same bytes, at a path that resolves to
    itself.

    **Only the layers are mirrored**, and that is the whole rule rather
    than an omission: a west workspace has many more projects than
    :data:`LAYERS`, and every one of them is a plain link here. A tool that
    made one of *those* its working directory would resolve out of the view
    the same way — but a patch can only name a layer (``patches/<layer>/``
    and :meth:`_Build._apply_patches`), so a link out of the view can only
    ever reach the same bytes the view would have shown, and mirroring
    every project would cost a directory walk of the whole workspace on
    every step for nothing. A project *outside* the layers that declares
    ``west-commands`` is a plain link, and both sides of west's check
    resolve into the environment's workspace together, so it passes.
    """
    substitutes = {path.relative_to(topdir): target for path, target in linked.items()}
    shadowed = {path.relative_to(topdir) for path in mirrored}
    copied = {path.relative_to(topdir) for path in materialized}
    real = {Path()} | shadowed
    for relative in (*substitutes, *shadowed, *copied):
        real.update(relative.parents)
    real -= copied
    skipped = copied | set(substitutes)
    try:
        for relative in sorted(real - shadowed, key=lambda path: len(path.parts)):
            (view / relative).mkdir(parents=True, exist_ok=True)
            for entry in sorted((topdir / relative).iterdir()):
                child = relative / entry.name
                if child in real or child in skipped:
                    continue
                (view / child).symlink_to(entry)
        for relative in sorted(shadowed, key=lambda path: len(path.parts)):
            _mirror_tree(topdir / relative, view / relative, base=relative, skipped=skipped)
        # The substitutes by name rather than by what the workspace happens
        # to contain. The manifest repository's directory is the one the
        # workspace package carries *empty* for this moment, and a package
        # that carries it not at all would otherwise produce a view with no
        # SDK in it and a failure much later, in west or in CMake, about
        # something else. A delivered SDK that already *is* the workspace's
        # manifest checkout — a build against a workspace somebody is
        # developing in — links to itself here, which costs one link and
        # writes nothing anywhere.
        for relative, target in substitutes.items():
            (view / relative).parent.mkdir(parents=True, exist_ok=True)
            (view / relative).symlink_to(target)
    except OSError as failure:
        raise BuildError(
            f"this build environment cannot assemble a view of {topdir} at {view}: "
            f"{failure.strerror}",
            hint="every step builds against a view of the environment's workspace "
            "under work, and the view needs a writable work directory",
        ) from failure
    return view


def _viewed(
    step: _Step, carried: CarriedWorkspace, *, sdk_tree: Path, patched: set[str]
) -> CarriedWorkspace:
    """*carried*, as it looks from a view of it under ``work`` (§10).

    The environment's workspace is never written, so this step builds one
    it may write: the trees a patch names are copied to their own place
    inside the view, the SDK is linked in where west looks for the manifest
    repository, and everything else is a link to the original. The record
    is then rebased onto the view, which moves every layer path with it — a
    workspace record
    already survives being unpacked somewhere other than where it was
    written (:func:`_rebased`), and the view is one more such place.

    The pre-generated data model is deliberately **not** moved. It is read,
    never written, and CHIP finds its files by the path of the data model
    relative to the CHIP tree — a relative path the view preserves exactly,
    because the view has the workspace's own shape.
    """
    view = step.work / STEP_VIEW
    paths = _tree_paths(carried.record_document)
    own = {name: path for name, path in paths.items() if name != "sdk"}
    copies: set[Path] = set()
    for name in sorted(patched & own.keys()):
        source = own[name]
        _patched_copy(source, view / source.relative_to(carried.topdir))
        copies.add(source)
    _workspace_view(
        carried.topdir,
        view,
        linked={paths["sdk"]: sdk_tree},
        mirrored=set(own.values()) - copies,
        materialized=copies,
    )
    return carried._replace(
        topdir=view,
        record_document=_rebased(carried.record_document, view, carried.record),
    )


def _step_extra_env(step: _Step, carried: CarriedWorkspace) -> dict[str, str]:
    """What a step adds to the build environment: the caches and the pregen tree.

    **The pre-generated data model** is what makes zap unnecessary: the tool
    is deliberately not in the tools package, its output is a release
    constant, and CHIP's own switch points at the copy the workspace package
    carries.

    **The caches** are §8's tiers, used the way it prescribes: ``local`` is
    always writable and is the primary, the most local other tier that
    exists is a read-only secondary, and a build is correct with none of
    them — so a tier this step cannot even create is dropped rather than
    reported.
    """
    extra: dict[str, str] = {}
    if carried.pregen_chip_root is not None:
        extra[workspace.PREGEN_DIR_VAR] = str(carried.pregen_chip_root)
    primary = step.tier(CACHE_TIERS[0]) / CCACHE_SUBDIR
    try:
        primary.mkdir(parents=True, exist_ok=True)
    except OSError:
        return extra
    extra["CCACHE_DIR"] = str(primary)
    for name in CACHE_TIERS[1:]:
        secondary = step.tier(name) / CCACHE_SUBDIR
        if secondary.is_dir():
            # ccache >= 4.8 spells a secondary store this way; `|read-only`
            # is what keeps a tier the orchestrator owns untouched.
            extra["CCACHE_REMOTE_STORAGE"] = f"file:{secondary}|read-only"
            break
    return extra


def _step_build(
    step: _Step, document: dict[str, Any], invocation_id: str, env: dict[str, str]
) -> list[str]:
    """The ``build`` action (``docs/spec/build-actions.md`` §2) as one step.

    Everything it needs is where §4 puts it, plus the environment's own
    workspace, which it finds through :data:`WORKSPACE_PACKAGE_VAR`. The
    build itself is :class:`_Build`, and it runs in ``clean`` mode always:
    ``work`` is empty at the start of every step (§3), so there is never
    prior state of this session to keep.

    Returns the artifacts as §6.2 declares them — paths relative to ``out``.

    **Every layer is patchable, and no layer is ever patched in place**
    (§10). The ``sdk`` layer is the orchestrator's input, delivered per
    build context and shared with whoever else holds those bytes, so it
    gets a copy under ``work``. The environment's own trees get a view
    under ``work`` (:func:`_workspace_view`): every tree a patch names is
    copied into it, the trees no patch names are linked or mirrored into it
    from the workspace, the delivered SDK is linked in where west looks for
    the manifest repository, and the build runs against the view. An
    unpatched build copies nothing; it only links.

    **There is one path and it does not depend on what the workspace is.**
    §10 permits a disposable tree to be patched where it stands, and this
    program declines that permission: the environment's trees are the
    profile's — a frozen store entry, an image's baked workspace, a west
    workspace somebody is developing in — and this program treats all three
    alike by never writing into any of them. The write probe that used to
    tell them apart is gone with the branch it fed: it answered for the
    filesystem rather than for the owner, so a store readable by root and a
    developer's own workspace both came out "disposable" and were patched
    and linked into. What is paid for the single path is the view's cost on
    every step (:func:`_workspace_view`), the container profile's included.
    """
    root = env.get(WORKSPACE_PACKAGE_VAR)
    if not _is_absolute_path(root):
        raise BuildError(
            "this build environment does not know where its own workspace is",
            hint=f"{WORKSPACE_PACKAGE_VAR} names the unpacked "
            "mcuhome-build-workspace package, as an absolute path",
        )
    package_root = Path(str(root))
    carried = environment_workspace(package_root)
    paths = _tree_paths(carried.record_document)
    for directory in (step.work, step.tmp, step.out):
        directory.mkdir(parents=True, exist_ok=True)
    patched = _patched_layers(step.context)
    if "sdk" in paths:
        tree = _delivered_sdk(step, paths["sdk"])
        # The copy is made before the view is assembled, so that what the
        # view links at the manifest repository's place is already the tree
        # the patches will be applied to: nothing downstream ever sees the
        # delivered one.
        if "sdk" in patched:
            tree = _patched_copy(tree, step.work / STEP_PATCHED / "sdk")
        carried = _viewed(step, carried, sdk_tree=tree, patched=patched)
        paths = _tree_paths(carried.record_document)
    session = document.get("session_id")
    builder = _Build(
        context_root=step.context,
        out_dir=step.out,
        work_dir=step.work,
        tmp_dir=step.tmp,
        # Opaque, and never a path (§6.1). It reaches the code generator's
        # own invocation and nothing else; an id-less request is answered
        # with the one token this step is certain of.
        session=session if isinstance(session, str) else invocation_id,
        # The parallelism this step plans with: derived from the
        # `limits` the orchestrator recommended (§6.1) and from this
        # machine only where it recommended nothing. What is enforced is
        # enforced from outside and is not negotiated (§11) — a step that
        # planned with what the machine appears to have is the step that
        # gets killed.
        jobs=jobs.resolve_jobs(limits=jobs.BuildLimits.from_document(document.get("limits"))).value,
        record=carried.record,
        record_document=carried.record_document,
        # What may be written, said honestly: exactly the trees this step
        # copied a moment ago, which are exactly the ones a patch names.
        # Every other tree in the view is a link or a mirror onto the
        # environment's own workspace, which nothing here writes into. The
        # paths are the record's throughout, rebased onto the view, so a
        # patched copy stands in for its original everywhere without
        # anything downstream knowing.
        given_trees={
            name: {"path": str(path), "writable": name in patched} for name, path in paths.items()
        },
        env=env,
        extra_env=_step_extra_env(step, carried),
    )
    builder.require_signing_key()
    artifacts, _layers = builder.execute(DEFAULT_MODE)
    return [entry["path"] for entry in artifacts]


def _step_result(
    invocation_id: str, status: str, message: str, artifacts: list[str] | None = None
) -> dict[str, Any]:
    """§6.2's five fields, in the order it prints them.

    ``artifacts`` is "the files **this step** wrote into ``out/``", so it is
    empty on anything but a success: what a failed step left behind is not
    an artifact it is declaring, and the result document itself is not one
    either.
    """
    return {
        "spec_generation": SPEC_GENERATION,
        "invocation_id": invocation_id,
        "status": status,
        "message": message,
        "artifacts": list(artifacts or []),
    }


def _step_answer(
    step: _Step, document: dict[str, Any], invocation_id: str, env: dict[str, str]
) -> dict[str, Any]:
    """The result document for one request, whatever happens on the way.

    The two refusals first, both ``unsupported`` and both §6.2's meaning of
    it — *no environment of my kind can do this*: a generation this program
    does not speak (§12), and an action it does not implement. Everything
    else that goes wrong is a ``failure``, including a crash: a step that
    wrote no readable result document failed anyway (§6.3), so it is better
    to say so in the document that was going to be written regardless.
    """
    generation = document.get("spec_generation")
    if generation != SPEC_GENERATION:
        return _step_result(
            invocation_id,
            _STATUS_UNSUPPORTED,
            f"this build environment implements specification generation "
            f"{SPEC_GENERATION}, and this request states {generation!r}",
        )
    action = document.get("action")
    if action not in STEP_ACTIONS:
        return _step_result(
            invocation_id,
            _STATUS_UNSUPPORTED,
            f"this build environment implements {', '.join(STEP_ACTIONS)}, "
            f"and this request asks for {action!r}",
        )
    try:
        artifacts = _step_build(step, document, invocation_id, env)
    except _BuildFailed as failed:
        return _step_result(invocation_id, _STATUS_FAILURE, failed.message)
    except BuildError as failed:
        return _step_result(
            invocation_id,
            _STATUS_FAILURE,
            "\n".join(part for part in (failed.message, failed.hint) if part),
        )
    except Exception as died:  # noqa: BLE001 - a crash is a failure, not a lost answer
        return _step_result(
            invocation_id,
            _STATUS_FAILURE,
            f"this build environment failed inside the step: {died}",
        )
    return _step_result(invocation_id, _STATUS_SUCCESS, "", artifacts)


def _without_a_result(problem: str) -> int:
    """The one outcome that is not a result document, said where it can be.

    §6.3 distinguishes zero from non-zero and nothing else — "a step that
    produced no readable result document failed, whatever it exited with" —
    so the value is spent on legibility instead: :data:`EXIT_UNUSABLE` says
    "there was nowhere to put an answer", and the one line on standard error
    says why, because nobody will find a reason that was never written.
    """
    sys.stderr.write(f"MCUHome build environment: {problem}\n")
    return EXIT_UNUSABLE


def step(env: dict[str, str]) -> int:
    """One step of a session (§6). The return value is the exit code.

    *env* is the environment the step was started in, **stated by whoever
    started this program and never read out of the process** — the invariant
    ``tests/python/test_userpaths.py`` enforces on every module here. It
    carries the base directory the whole tree is resolved against, whatever
    else the orchestrator set, and the ``PATH`` the build's children need.

    The sequence is short because the specification is: resolve §4's tree,
    read the request document, name the result document after the
    invocation, answer, write. Only the first three can end without a result
    document, and each for the same reason — there is nowhere to write one
    or nothing to call it.

    Exit ``0`` exactly when a result document was written that says
    ``success`` (§6.3).
    """
    base = env.get(BASE_DIR_VAR)
    if not _is_absolute_path(base):
        return _without_a_result(
            f"{BASE_DIR_VAR} is not set to an absolute path. The orchestrator sets it "
            f"to the directory that {STEP_DIR}/ sits in."
        )
    layout = _Step(Path(str(base)))
    document = _step_request(layout.request)
    if document is None:
        return _without_a_result(f"there is no readable request document at {layout.request}.")
    invocation_id = _invocation_id(document)
    if invocation_id is None:
        return _without_a_result(
            f"the request document at {layout.request} states no invocation_id that a "
            f"result document could be named after."
        )
    result = _step_answer(layout, document, invocation_id, env)
    try:
        layout.out.mkdir(parents=True, exist_ok=True)
        _write_atomically(layout.result(invocation_id), result)
    except OSError as unwritable:
        return _without_a_result(
            f"the result document could not be written to {layout.out}: {unwritable}."
        )
    return EXIT_SUCCESS if result["status"] == _STATUS_SUCCESS else EXIT_FAILURE


def _step_request(path: Path) -> dict[str, Any] | None:
    """The request document at *path* (§6.1), or ``None`` if there is none.

    One JSON object, UTF-8, and nothing else is checked: "ignore fields you
    do not know" is the whole parsing rule, and a document that is not an
    object is not a request at all. A step that cannot read one has no
    ``invocation_id`` either, which is why this is the one thing that ends
    without a result document.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _invocation_id(document: dict[str, Any]) -> str | None:
    """The ``invocation_id`` a result document may be named after (§6.1)."""
    value = document.get("invocation_id")
    if isinstance(value, str) and _SAFE_INVOCATION_ID.match(value):
        return value
    return None


if __name__ == "__main__":  # pragma: no cover - the launcher's entry point
    import os
    import sys

    # THE process boundary, and the one place that may read process
    # state: when this module is the process, it is "whoever started
    # this program", and the environment it hands over is the one the
    # step was started in — PATH with the toolchain and west, and the
    # base directory everything is resolved against.
    # ``tests/python/test_userpaths.py`` exempts this guard by shape and
    # pins both handovers; library imports never execute it.
    #
    # **The argv is the discriminator, and it can only be read here.** The
    # v3 invocation passes no arguments at all
    # (packaging/build-environment/build-environment-entry runs this
    # module that way); the legacy one passes exactly two operands
    # (containers/build-container/run). Nothing else is either, and a
    # legacy launcher's wrong arity keeps the answer it always had.
    if len(sys.argv) == 1:
        raise SystemExit(step(dict(os.environ)))
    raise SystemExit(main(sys.argv, env=dict(os.environ)))
