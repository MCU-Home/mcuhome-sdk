# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The builder program: one build step, and the generator it reaches.

This module is what a MCUHome build environment runs when a step starts.
``docs/spec/build-environment-specification.md`` fixes the invocation: the
entry point is run with **no arguments** (§6), everything the step is
about is in ``mcuhome/invocation-request.json`` below the base directory
``MCUHOME_BUILDER_BASE_DIR`` names (§4), the answer is one result document
at ``mcuhome/out/result-<invocation_id>.json`` (§6.2), and the exit code
is zero exactly when that document says ``success`` (§6.3). Which actions
exist is deliberately not the specification's business;
``docs/spec/build-actions.md`` documents MCUHome's, and ``build`` is the
one an environment of this kind implements. :func:`step` is that
invocation.

**The build itself** is :class:`_Build`, which applies the context's
patches, reaches code generation through the SDK entry point, compiles
with ``west build --sysbuild`` and delivers the artifacts. What it knows
before it starts is the environment's own: the trees are the packages'
(:func:`environment_workspace`), ``work`` is empty at the start of every
step (§3), so every step is a clean build, and the context arrives
measured — ``docs/spec/build-actions.md`` §3: "there is nothing an
environment could confirm that the orchestrator does not already know
from its own bytes". A step therefore checks that the files it needs are
there and builds.

``firmware.hex``, ``firmware.bin``, ``bootloader.hex`` (when the build
produced one) and ``build-report.json`` go into ``out`` under exactly
those names, and the result document names them relative to it
(``docs/spec/build-actions.md`` §2.1 and §2.2).

**The second, much smaller ABI in this file** is the one the generator is
reached over: two operands, a request document, a result document, and
:func:`run_invocation` as the outer sequence — argv, parsing, the atomic
result, the catch-all that turns a crash into a legible failure.
:mod:`mcuhome.compiler.sdkentry` is the other end of that call and imports
the same function rather than transcribing it, which is what keeps the
two sides agreeing.

Decisions this module took that no document made for it:

*The generated application tree and the CMake tree live in ``work``.*
They are the two directories a build produces, and a tree in a per-step
scratch directory would be thrown away twice over.

*The code generator is a child, and it is handed an empty ``out``.* The
generator ships in ``mcuhome/sdk``, which the orchestrator delivers per
build context, so the generated application belongs to the SDK the
context pinned rather than to the environment's vintage
(``mcuhome-sdk.json`` declares the entry point and its runtime). What it
produced is then absorbed into ``work/tree`` content-aware: a file whose
bytes are already there is left alone, mtime and all, because CMake
watches the tree and a rewritten unchanged ``CMakeLists.txt`` re-runs the
whole Matter sub-build.

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
from typing import Any, NamedTuple

from mcuhome.compiler import report, workspace
from mcuhome.compiler.generate import APP_DIR
from mcuhome.model import jobs, registry
from mcuhome.model.context import MODEL_FILE, PATCHES_DIR
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file
from mcuhome.model.modelfile import read_model

# Nothing here imports mcuhome.compiler.contextread, and that is worth
# stating: the SDK entry point imports this module for
# :func:`run_invocation`, and its runtime is what the SDK package
# declares — a bare interpreter, no third-party packages. contextread
# carries the YAML emitter and pulls ruamel at import, which only a build
# environment provides. tests/python/test_container_closure.py pins the
# entry point's closure to stdlib plus mcuhome.

__all__ = [
    "BASE_DIR_VAR",
    "BOOTLOADER_ARTIFACT",
    "CACHE_TIERS",
    "CarriedWorkspace",
    "EXIT_FAILURE",
    "EXIT_SUCCESS",
    "EXIT_UNUSABLE",
    "FIRMWARE_ARTIFACTS",
    "LAYERS",
    "REPORT_ARTIFACT",
    "REPORT_VERSION",
    "REQUEST_VERSIONS",
    "RESULT_PREFIX",
    "RESULT_SUFFIX",
    "RESULT_VERSION",
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
    "environment_workspace",
    "patchset",
    "run_invocation",
    "sdk_entry_point",
    "step",
]

# --------------------------------------------------------------------------
# The two documents exchanged with the SDK entry point
# --------------------------------------------------------------------------
#
# Code generation is a child process with an ABI of its own: two operands,
# a request document, a result document (:mod:`mcuhome.compiler.sdkentry`
# is the other end, :func:`run_invocation` the shared outer sequence).
# These are that ABI's two version numbers, and they live here because
# this module is the only party that writes either.

#: Request format versions the generator can be handed. The first is what
#: :meth:`_Build._generate` writes; the entry point states the same tuple
#: from its own side and refuses anything outside it.
REQUEST_VERSIONS = (1,)

#: The result format version :func:`_result_document` writes.
RESULT_VERSION = 1

#: The layers a build environment's workspace is made of, in the order
#: they are reported. Third-party layers carry an ``x-`` prefix and reach
#: a build only by way of the environment's own record.
LAYERS = ("zephyr", "sdk", "chip", "mcuboot")

# --------------------------------------------------------------------------
# What a `build` is made of
# --------------------------------------------------------------------------

#: A step is a clean build and there is no other kind: ``work`` is empty
#: at the start of every step (specification §3), so there is no prior
#: state an incremental build could stand on. The value is still a
#: parameter of :meth:`_Build.execute` because the builder decides two
#: things by it — whether it discards its own state and how it configures
#: west's pristine mode — and naming it beats a bare string in both.
DEFAULT_MODE = "clean"

#: The bootloader's verification key inside the context. Mandatory for
#: ``build`` and for ``build`` alone, with no fallback: MCUboot's own
#: default is a demo key whose private half is published.
SIGNING_KEY_FILE = "keys/signing.pub"

#: Where the SDK package declares its code-generation entry point, at the
#: root of ``trees.sdk``. This module fixes the file name and three field
#: names — ``sdk``, ``generate.program``, ``generate.runtime`` — and no
#: values.
SDK_METADATA_FILE = "mcuhome-sdk.json"

#: ``sdk`` metadata format versions this program implements. A version
#: outside it is ``error.build.failed`` and never ``unsupported``: this
#: program implements everything the format asks of it, and no other
#: build environment would fare better with this SDK package.
SDK_METADATA_VERSIONS = (1,)

#: The action the SDK entry point is invoked with. Never an action of
#: *this* program.
GENERATE_ACTION = "generate"

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
#: image, whose role is ``firmware``. This module writes ``firmware.hex``
#: and ``firmware.bin`` under those names.
FIRMWARE_ARTIFACTS = (("zephyr.hex", "firmware.hex"), ("zephyr.bin", "firmware.bin"))

#: The same for MCUboot, whose role is ``bootloader``. Declared when the
#: build produces one — see the module docstring.
BOOTLOADER_ARTIFACT = ("zephyr.hex", "bootloader.hex")

#: The mandatory ``report`` artifact (``docs/spec/build-actions.md``
#: §2.2), and the format version this module writes. "A consumer that
#: does not implement the version it finds must not sign from the
#: document."
REPORT_ARTIFACT = "build-report.json"
REPORT_VERSION = 1

#: The prefix of the ``layers[<name>].patchset`` encoding (see
#: :func:`patchset`), fixed as a literal and therefore never composed
#: here.
_PATCHSET_PREFIX = "mcuhome-patchset-1\n"

# --------------------------------------------------------------------------
# The exit codes
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

_REASON_INCOMPLETE = "error.context.incomplete"
_REASON_LAYER = "error.layer.unknown"
_REASON_PATCH = "error.patch.incomplete"
_REASON_BUILD = "error.build.failed"
_REASON_INTERNAL = "error.internal"

# --------------------------------------------------------------------------
# The request document
# --------------------------------------------------------------------------


class _Unusable(Exception):
    """No result can be addressed: exit 66, nothing written.

    Not an error type from :mod:`mcuhome.model.errors`, on purpose. Those render
    themselves for a person reading a terminal; this one is never rendered
    anywhere, because the whole point of exit 66 is that there is no
    channel to say anything on.
    """


def _is_absolute_path(value: Any) -> bool:
    """A path value, as this ABI requires one: a string, and absolute."""
    return isinstance(value, str) and Path(value).is_absolute()


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """One JSON object, refusing duplicate keys, which are invalid here."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise _Unusable("the request document has a duplicate key")
    return dict(pairs)


def _carries_null(value: Any) -> bool:
    """Whether *value* holds a ``null`` anywhere inside it.

    ``null`` never means "absent" in this document; it is invalid at any
    depth and in any field, including one this program would otherwise
    ignore. The rule governs the document, not the fields one program
    happens to read.
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

    The only program-caused error that cannot produce a result document,
    and precisely the case in which the program does not know where a
    result would go. A byte-order mark is refused by the JSON parser
    itself, which is what this format's "UTF-8 without BOM" rule asks for.
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

    From here on every error is a result document — which is true exactly
    because this function refused everything that would have made that
    impossible.
    """
    value = document.get("result")
    if not _is_absolute_path(value):
        raise _Unusable("the request document names no absolute result path")
    return Path(value)


# --------------------------------------------------------------------------
# The result document
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
    """A result document in the field order this format prints it in.

    *echo* is what the invocation was given and nothing else: ``action``
    always, because it is ``argv[1]``, and ``session`` iff the request
    document carried it. A program must not invent a value for a field it
    was never given.

    Every optional field below is passed exactly when the invocation
    *measured* the thing it reports, and omitted otherwise: fabricating
    one would be worse than omitting it, since whoever reads the document
    compares it against values of its own.
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


def _write_atomically(path: Path, document: dict[str, Any]) -> None:
    """Write *document* to *path* atomically.

    Temporary file in the *same* directory, ``fsync``, ``rename`` — same
    directory so the rename cannot cross a filesystem, ``fsync`` so the
    bytes are on disk before the name exists, rename because that is the
    one operation a reader cannot observe half of. A failure anywhere
    leaves neither a result document nor a temporary file behind, which is
    what makes "exit 66, nothing written" true of the write as well as of
    the parse.

    The file keeps :func:`tempfile.mkstemp`'s own mode. Nothing here
    states the result document's permissions; whoever invoked this
    program either runs it as itself or outranks it.
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
# Layer paths, from a workspace record
# --------------------------------------------------------------------------


def _tree_paths(document: dict[str, Any]) -> dict[str, Path]:
    """``<layer> -> <where this environment builds it>``, from a record.

    The one answer to "which path can this program honour for this
    layer", read by :meth:`_Build._workspace` while any build runs. A
    layer whose entry names no string path is absent from the result
    rather than present with a guess.
    """
    layers = document.get("layers")
    return {
        name: Path(entry["path"])
        for name, entry in (layers if isinstance(layers, dict) else {}).items()
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }


# --------------------------------------------------------------------------
# A build's own failures
# --------------------------------------------------------------------------


class _BuildFailed(Exception):
    """A build that did not succeed, before it is a result document.

    The builder (:class:`_Build`) raises the *facts* — a reason, a message
    and optional details — and lets the invocation that asked render them:
    a step renders a ``failure`` with the ``message``. Everything a build
    refuses is one of these, and every raise site reads the same.
    """

    def __init__(self, reason: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.details = details


# --------------------------------------------------------------------------
# The build
# --------------------------------------------------------------------------


class _Events:
    """The optional NDJSON event stream, or nothing at all.

    Only if the invocation names an ``events`` file: the program appends
    NDJSON to it — one JSON object per line, UTF-8, flushed after every
    line, append-only, never truncated. Every object carries
    ``"event": "<name>"`` and a monotonic ``"seq"`` starting at 1.

    **Nothing here can fail an invocation.** The program must not block on
    writing an event and must not die if the write fails. Where the two
    obligations collide — a full pipe, a stalled disk — **not blocking
    wins**. So every write is guarded, nothing is retried, and the file is
    flushed rather than ``fsync``ed: a reader tailing it wants the bytes
    now, and an event nobody read is not worth a build.
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

    The ``fsync`` is the point: every declared hash has to be read back
    from disk, and reading back a file whose bytes are still in the page
    cache would satisfy the letter and none of the reason — the
    orchestrator re-hashes the same file from its own side of the mount.
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

    The value is defined exactly, otherwise a cross-implementation audit
    is worthless::

        SHA-256( "mcuhome-patchset-1\\n"
                 + for each file under patches/<layer>/, ascending byte order:
                     <64 hex chars of the file's SHA-256> + " " + <filename> + "\\n" )

    The value carries its own algorithm, so it is rendered ``sha256:`` +
    64 lowercase hex digits, and each ``<64 hex chars>`` inside the input
    is lowercase. The sort is over the filename's **bytes**, not over its
    code points — the two agree for the ``NNNN-name.patch`` grammar
    :mod:`mcuhome.compiler.contextread` enforces, and stating the byte order
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

    The other half of handing the child an empty ``out``: what it produced
    still has to end up in the session's persistent tree, and it must land
    there the way :func:`mcuhome.compiler.generate.write_tree` would have
    written it — a file whose bytes are already in *target* is left alone,
    mtime and all, because CMake watches the tree and a rewritten
    unchanged ``CMakeLists.txt`` re-runs the Matter sub-build.

    Nothing is deleted from *target*: the generator never deletes either
    (it only maps files to write), and a stale file from an earlier model
    is exactly as stale after a direct ``write_tree(work/tree)`` would
    have run. Returns how many files the child produced, for the
    ``generate.written`` event.
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
    output because the two are one raw, opaque log stream, and the stream
    is echoed onward as well as captured: a consumer must not parse the
    log stream for machine decisions, and this program does not — it
    re-emits it so that the orchestrator collecting this program's output
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


def _is_sdk_version(value: Any) -> bool:
    """An ``mcuhome-sdk.json`` format version this program implements."""
    return isinstance(value, int) and not isinstance(value, bool) and value in SDK_METADATA_VERSIONS


def sdk_entry_point(sdk_path: Path) -> tuple[Path, str]:
    """The code-generation entry point declared at the root of ``trees.sdk``.

    ``mcuhome-sdk.json``: one JSON object, UTF-8 without BOM, RFC 8259,
    read with the same JSON parser every request document is, fixing
    three names and no values — ``sdk``, ``generate.program``,
    ``generate.runtime``. Returns the absolute path of the program and
    the runtime string, and raises
    :class:`~mcuhome.model.errors.BuildError` for everything that counts
    as "code generation cannot be reached".

    A missing file, a missing field and a ``sdk`` version the program
    does not implement are all one situation, and all three fail the
    invocation with ``reason: "error.build.failed"``. They are not
    ``unsupported``: the program implements everything the format asks of
    it, and no other build environment would fare better with this SDK
    package. :meth:`_Build._sdk_metadata` is where that becomes a result
    document; here it is an error a caller can also raise while checking
    an SDK package it is *shipping*, which is the second reader this
    function has.

    ``generate.runtime`` is read and not interpreted: it is an opaque
    string, and the honest consequence is that a conforming build
    environment must *provide* the runtime, not that it can check the
    name against anything. It is required to be there because this
    module fixes the field.
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
    """One build, for the step that asked for it.

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

    Steps 2 to 7 are :meth:`execute`. A step has no context ID to report,
    and it starts every time from an empty ``work``.

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
        #: field. Empty means "the environment's own".
        self.given_trees: dict[str, Any] = given_trees if isinstance(given_trees, dict) else {}
        #: A shared ``ccache`` object, or ``None``. A step states its cache
        #: in *extra_env* instead, from the tiers the specification gives
        #: it, so this is always ``None`` on that path.
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
        #: The effective context ID, once some caller measured one.
        #: Nothing sets it today.
        self.measured: str | None = None

    # -- refusals ----------------------------------------------------------

    def fail(
        self, reason: str, message: str, details: dict[str, Any] | None = None
    ) -> _BuildFailed:
        """The build did not succeed, said once and rendered by the caller.

        *reason* and *details* are carried through untouched, even though
        the step's result document has no field for either and reports
        only *message* — which is what its ``message`` field is for:
        "free text for a human".
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

        A context submitted to a ``build`` that does not carry it fails
        the invocation typed — ``status: "failure"``, ``reason:
        "error.context.incomplete"``, the missing path in
        ``error.details`` — and the program must not build anyway. There
        is no fallback to MCUboot's default key, because that default is
        MCUboot's demo key and **its private half is published**.

        A key that is present but unusable — wrong curve, a private key by
        mistake — is not typed at all (only the absence is), so nothing is
        checked here beyond existence: the file is handed to sysbuild, and
        MCUboot's own tooling is the thing that knows what a verification
        key is.
        """
        if not (self.context_root / SIGNING_KEY_FILE).is_file():
            raise self.fail(
                _REASON_INCOMPLETE,
                f"a build needs {SIGNING_KEY_FILE} as the bootloader's verification key, "
                f"and the context at {self.context_root} carries none",
                {"missing": [SIGNING_KEY_FILE]},
            )

    def _discard_state(self) -> None:
        """``clean`` — a fresh workspace, concretely.

        The generated application tree and the CMake tree are this
        program's own state in ``work`` (module docstring), so a fresh
        workspace is those two removed. The session marker and the
        per-layer patch records stay: patch application happens once per
        session and explicitly not once per invocation, and a ``clean``
        that re-applied patches would either fail on the second build of a
        session or need a layer reset — and there is no layer reset.
        """
        for name in (WORK_TREE, WORK_BUILD):
            shutil.rmtree(self.work_dir / name, ignore_errors=True)

    # -- 4. the workspace ---------------------------------------------------

    def _workspace(self) -> tuple[Path, dict[str, Path]]:
        """The west workspace this environment carries, checked against ``trees``.

        The workspace record (``packaging/build-environment/workspace-record.py``
        writes it, into the workspace package) says where each layer is,
        and a ``trees`` entry the caller named is accepted only where it
        names that same path — which is exactly what mounting a writable
        view *over* the tree produces. Anything else fails the build,
        naming the layer and both paths; the module docstring says why
        nothing here can move west's idea of where a project lives, and
        what would replace it.

        A program with no record has no workspace at all, and says so
        rather than compiling against nothing.
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

    # -- 5. the child environment --------------------------------------

    def _environment(self, topdir: Path, sdk_path: Path) -> dict[str, str]:
        """What every child of this invocation runs in.

        Assembled from what this program was *told* it runs in — never
        read out of the process (the module docstring) — by
        :func:`~mcuhome.compiler.workspace.build_environment`, which is the one
        definition of a Matter build environment in this package and is
        also what the orchestrator reaches for the other direction. It
        contributes the codegen shim on ``PYTHONPATH``, the two job caps
        that nothing inherits, ``ZEPHYR_BASE`` so the generated
        CMakeLists finds Zephyr and the Matter SDK next to it, a writable
        ``HOME``, and the ``TMPDIR`` this program points at the request's
        ``tmp``.

        **``HOME`` is in ``work``, and it is not decoration.** Whoever
        invokes this program runs it as the calling user where it can,
        which in a container is a UID that has no ``/etc/passwd`` entry —
        it comes from the host — and therefore no home directory either;
        tools that cache in ``$HOME`` fail obscurely without a writable
        one. ``work`` rather than ``tmp`` because those caches are worth
        keeping for the next invocation of the session, and it is a place
        this program may write.

        **What is *not* built here is ``PATH``.** It arrives with the
        environment the caller stated, because it is the one variable
        naming things this program did not put anywhere: the environment's
        toolchain, west, ``git``, the runtime ``generate.runtime`` names.
        A program that composed one would be describing a filesystem
        nothing here owns. :func:`~mcuhome.compiler.workspace.require_tools`
        is called on the finished environment before anything is compiled,
        so an environment that cannot start ``west`` is a typed refusal
        naming the tool rather than a child process that fails to exec ten
        minutes in.

        ``limits.jobs`` is taken as given. It is authoritative, so
        :func:`~mcuhome.model.jobs.resolve_jobs` and its auto-detection
        stay on the caller's side and are never called here — an optional
        field would be worthless: a foreign program would fall back to
        ``nproc``, which is exactly the case the field exists against.

        The cache follows the tiering below **when a shared cache is
        named**. A ``writable: true`` shared cache may be used as the
        primary cache; a ``writable: false`` one is treated as a
        read-only secondary cache, with its own primary cache in ``work``
        or ``tmp``, which is what the two variables below say to ccache.
        ``writable`` is read and never probed.

        When none is named — which is the case for a step, always —
        this says nothing about ccache at all, and that is deliberate
        rather than an omission. The build environment configures both of
        ccache's roles itself (this environment's ``/etc/ccache.conf``: a
        writable cache, and a read-only secondary), an environment
        variable set here would *override* that file rather than agree
        with it, and what actually lives at those two paths is decided by
        whoever mounts them or does not. Any cache nothing was named for
        is the program's own, and dies with the session.

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
        # baked workspace belongs to whoever built the image, and whoever
        # invokes this program runs it as the calling user — but the
        # deeper point is that the workspace is a frozen input this
        # program never writes, incidental caches included. So the local
        # config is copied into
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
                    # `|read-only` attribute is what makes the read-only rule
                    # above enforced rather than merely intended.
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

    # -- 6. patched layers ---------------------------------------------

    def _apply_patches(self, paths: dict[str, Path], env: dict[str, str]) -> dict[str, Any]:
        """Apply every patched layer's patches, once per session.

        The semantics are fixed and the tool is free: ``git apply``,
        ``patch -p1``, or a diff implementation the program brings itself
        are all conforming. This uses ``git apply`` with no ``--3way`` and
        no fallback, which is what ``patches/README.md``, CI and the image
        build already do — and it is what "a patch that does not apply is
        a failure of the invocation and not something to search around"
        asks for.

        The per-layer record in ``work`` is what "once per session" means:
        started is written before the first patch, complete only after
        the last patch of that layer applied cleanly, and a layer found
        started-but-not-complete is ``error.patch.incomplete`` — terminal,
        because restoring the pristine baseline is not possible from
        inside the merged view at all.

        Every patched layer appears in the returned ``layers`` block,
        including one an earlier invocation of this session already
        applied — the patch set of a locked context cannot change.
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
            # Both these conditions are typed the same way: if
            # `patches/<layer>/` names a layer for which there is no
            # `trees` entry, or which the program does not know, the
            # program must not proceed: `status: "failure"`, `reason:
            # "error.layer.unknown"`.
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
                # The third case, and it is not typed the same way as the
                # other two: the entry is there and does not assert that
                # the tree may be written. It is not "no entry" and not "a
                # layer this program does not know", so it is not
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

    # -- 7. code generation ----------------------------------------------

    def _sdk_metadata(self, sdk_path: Path) -> tuple[Path, str]:
        """:func:`sdk_entry_point`, as a refusal of *this* invocation.

        The reading of ``mcuhome-sdk.json`` is a module-level function so
        that the SDK package's own suite can hold its metadata against the
        rules the program applies to it, rather than against a second
        transcription of the format in a fixture.
        """
        try:
            return sdk_entry_point(sdk_path)
        except BuildError as unreachable:
            raise self.fail(_REASON_BUILD, unreachable.message) from unreachable

    def _generate(self, sdk_path: Path, env: dict[str, str]) -> Path:
        """Invoke the SDK entry point as a child, over this same ABI.

        ``<trees.sdk.path>/<generate.program> generate <absolute path of a
        request document>`` — the same invocation shape as this program's
        own: the program writes that request document into its own
        ``tmp`` and is the caller of that invocation.

        Two things about the invocation are fixed and both are here: the
        entry point reads the build context from ``context`` and writes
        the per-device Zephyr application tree into ``out``. Where that
        ``out`` is, is this program's choice as the caller — and it owes
        the child an ``out`` that is **empty**. So the child writes into a
        fresh per-invocation directory under ``tmp``, and what it produced
        is then absorbed into the session's ``work/tree`` content-aware —
        a file whose bytes are already there is left alone, mtime and
        all. Handing the child ``work/tree`` directly would be cheaper
        and wrong twice over: a foreign SDK entry point (the whole
        point is that it need not be MCUHome's) may rely on the emptiness
        of ``out``, and one that lists ``out`` before writing would see
        another invocation's files. The absorb is what keeps a warm
        ``work/tree`` meaningful — CMake watches the tree's mtimes, so an
        unchanged file must stay untouched. Everything else in the
        document is between the SDK package and itself; the rest of the
        fields are sent because the entry point speaks this ABI and they
        are mandatory in it.

        A non-zero exit, a missing result document or a ``status`` other
        than ``success`` fails the invocation with ``reason:
        "error.build.failed"``.
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
        per-image snippet rule, and its ``detached_signing`` path is what
        this program requires: it uses ``keys/signing.pub`` from the
        context as the bootloader's verification key and never signs
        images. With that flag the key handed to sysbuild is the public
        half and the generated tree clears the application's signing
        step, so no signed file is produced at all.

        The device model comes out of the context (at
        ``model/device-model.json``) through
        :func:`~mcuhome.model.modelfile.read_model`, which is documented as
        exactly this receiving end. A board this builder has no update scheme for
        is ``error.build.failed``: a build report has to carry a
        ``signing`` block, and there is nothing to put in it.

        ``clean`` is ``--pristine always`` regardless of what the build
        directory looks like; any other mode lets
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
            # never started, the exact command line — and details is the
            # field that carries it. Dropping it once reduced "could not
            # start the build: No such file or directory" to a riddle with
            # no file name in it.
            details = {"hint": failure.hint} if failure.hint else None
            raise self.fail(_REASON_BUILD, failure.message, details) from failure
        if code != 0:
            raise self.fail(_REASON_BUILD, f"west build exited with {code}")
        return scheme, build_dir, log

    # -- 9. what leaves ------------------------------------------------

    def _deliver(self, source: Path, name: str, role: str) -> dict[str, Any]:
        """Copy one file into ``out`` and declare it.

        Four fields and nothing else: ``root`` is ``"out"``, the only
        value this program ever uses; ``path`` is relative to it with
        segments matching ``[A-Za-z0-9._-]+``; ``role`` identifies it by
        function; ``hashes`` is keyed by algorithm and read back from
        disk. No size — an artifact entry declares no size.

        Copied rather than declared where the linker left it, because
        everything in ``out`` is then something this program put there
        deliberately: sysbuild's combined hex, which on a never-signed
        build is the *unsigned* application under a flashable-looking
        name, simply never arrives.
        """
        destination = self.out_dir / name
        _write_file(destination, source.read_bytes())
        digest = sha256_file(destination)
        self.events.emit(
            "artifact.collected", role=role, path=name, size=destination.stat().st_size
        )
        return {"root": "out", "path": name, "role": role, "hashes": {"sha256": digest}}

    def _collect(self, scheme: Any, build_dir: Path, log: str) -> list[dict[str, Any]]:
        """The artifacts a successful build declares.

        A successful device build declares at least two artifacts: the
        unsigned image with role ``firmware``, and exactly one artifact
        with role ``report``. Both firmware files carry the ``firmware``
        role and the bootloader is declared as well — the module
        docstring says why for each.

        There is no ``ota`` artifact: the OTA wrapper's payload has to be
        the **signed** binary, and this program never signs.
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
        """The build report (``docs/spec/build-actions.md`` §2.2) — one
        JSON object, for one consumer.

        It exists for one consumer and one purpose: the client that signs
        detached, which is the only party holding the private key. So it
        carries the ``report`` version, the mandatory ``signing`` block and
        the optional ``memory`` list, and nothing else — this program
        never signs, the input is the ``firmware`` artifact, and where the
        signed output goes is the signer's business.

        The four arguments are :func:`~mcuhome.compiler.report.signing_parameters`
        unchanged — three of them board data the build already had to know
        and the fourth, imgtool's ``--version``, read out of the built
        application's own ``.config`` (``docs/spec/build-actions.md``
        §2.2). A build that left no ``.config`` behind has no version to
        state, and stating Zephyr's ``0.0.0+0`` default for it would
        produce a signed image MCUboot compares monotonically against the
        wrong number — so that is ``error.build.failed`` rather than a
        report.

        ``memory`` is omitted when the build relinked nothing: a build
        that relinked nothing reports none, which is correct rather than
        incomplete.
        """
        kconfig = build_dir / APP_DIR / "zephyr" / ".config"
        if not kconfig.is_file():
            raise self.fail(
                _REASON_BUILD,
                f"the build left no {kconfig.name} for the application image, so the "
                "signing parameters a build report has to carry cannot be stated",
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


# --------------------------------------------------------------------------
# The generator ABI's invocation
# --------------------------------------------------------------------------


def run_invocation(
    argv: list[str],
    invoke: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> int:
    """The generator ABI's outer sequence around *invoke*; the return value
    is the exit code.

    Shared with the SDK package's entry point
    (:mod:`mcuhome.compiler.sdkentry`), which reuses this ABI on purpose:
    a second calling convention would be a second frozen thing to design
    and to implement twice, and reusing this one means the entry point is
    reached with the parser, the two documents and the four exit values
    this program has anyway. Sharing the sequence is what keeps that a
    fact of the code rather than a claim about two transcriptions of it.

    **A crash inside *invoke* becomes a result document, not a
    traceback.** Exit 1 promises a result document, and every other exit
    is undefined — so an unexpected exception after the preamble was
    read is answered on the channel that was already open: ``status:
    "failure"``, the exception in ``error.message``, exit 1. The
    ``reason`` is ``error.internal``, reserved for exactly this — the
    program itself failed, inside any action — so a caller is never told
    a crash was a build-work failure. Only when even that document
    cannot be written does the invocation end with exit 66 — the same
    answer the ordinary write path gives, because a result nobody can
    address is that case whatever was computed.
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
    except Exception as died:  # noqa: BLE001 - the catch-all turns a crash into a result document
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
# reaches into it. What it shares with the generator ABI above is the
# builder (:class:`_Build`) and the atomic write, and nothing else.

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
    profile that mounts it straight into the workspace instead — the
    container profile does — is the second place, and the only other one
    there is.
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
    # The invocation takes no arguments (specification §6), which is how
    # packaging/build-environment/build-environment-entry runs this
    # module. Arguments are not a second calling convention to dispatch
    # on — there is only one — so they are refused rather than
    # interpreted: a caller that passes any is not driving this program.
    if len(sys.argv) != 1:
        raise SystemExit(
            f"{sys.argv[0]}: a build step takes no arguments; "
            f"everything it is about is in the request document"
        )
    raise SystemExit(step(dict(os.environ)))
