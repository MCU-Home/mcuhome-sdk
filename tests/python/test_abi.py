# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build step, the SDK entry point, and the build behind both.

:mod:`mcuhome.compiler.abi` is what a MCUHome build environment runs when
a step starts. The invocation is
``docs/spec/build-environment-specification.md``'s: the entry point is run
with **no arguments**, the request document is at a fixed path below
``MCUHOME_BUILDER_BASE_DIR``, the answer is
``mcuhome/out/result-<invocation_id>.json``, and the exit code is zero
exactly when that document says ``success``. The actions are
``docs/spec/build-actions.md``'s, and this environment implements
``build``.

The same module carries the **SDK entry point's** side of a second, much
smaller ABI: code generation is a child process with two operands, a
request document and a result document, and
:func:`mcuhome.compiler.abi.run_invocation` is the outer sequence both
ends of that call share (:mod:`mcuhome.compiler.sdkentry` is the other
end). That is what the middle section of this file is about.

This suite is mostly about *refusals*: a program that builds firmware and
answers a malformed request with a traceback is not usable by whoever
drives it; one that answers with a typed refusal and nothing half-written
on disk is. The properties that carry most of the file:

* **Exactly one thing produces no result document** — a request with no
  ``invocation_id`` a result could be named after. Everything else is a
  document, because a refusal nobody can read is not a refusal.
* **The result document is the last write action, and it is atomic.** It
  is read whenever it exists, whatever the exit code, so a half-written
  one is worse than none.
* **Nothing is invented.** A step declares the artifacts it actually
  wrote.

Every test arranges what the other side arranges: the filesystem tree of
§4 plus the environment's own packages.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import EXAMPLES_DIR, resolve_file, write_context_manifest

from mcuhome.compiler import abi
from mcuhome.model import jobs
from mcuhome.model.context import (
    ContextFile,
    ContextManifest,
    EnvironmentPin,
    PackagePin,
    SdkPin,
    context_id,
)
from mcuhome.model.errors import BuildError

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The reference device, whose resolved model every build here carries
#: (see the ``device_model_json`` fixture).
EXAMPLE = EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml"

#: Where the SDK entry point is started from, as its own launcher states
#: it. Only ever ``argv[0]`` here, and never read.
GENERATOR_PATH = "mcuhome/sdk/bin/generate"


class Caller:
    """What the caller of the generator ABI does around one invocation.

    It owns a per-invocation directory, writes the request document into
    it and names the result file inside it. Nothing here is a fixed path:
    that ABI defines no mount points, and this suite would not notice if
    it did.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.request = directory / "request.json"
        self.result = directory / "result.json"

    def preamble(self, **fields: Any) -> dict[str, Any]:
        """A request document carrying nothing but the preamble, plus *fields*."""
        return {"request": 1, "result": str(self.result), **fields}

    def document(self) -> dict[str, Any]:
        """The result document, parsed."""
        return json.loads(self.result.read_text(encoding="utf-8"))


@pytest.fixture
def caller(tmp_path: Path) -> Caller:
    return Caller(tmp_path / "s-42" / "inv-7")


# --------------------------------------------------------------------------
# A locked build context
# --------------------------------------------------------------------------

#: The resolved pins the contexts below carry. Synthetic on purpose:
#: nothing exercised by these tests looks a pin up or cross-checks it
#: against real bytes, so nothing here has to name a build environment or
#: an SDK package that exists.
ENVIRONMENT_PIN = EnvironmentPin(
    workspace=PackagePin(name="mcuhome-build-workspace", version="0.1.0", sha256="ab" * 32),
    tools=PackagePin(name="mcuhome-build-tools", version="0.1.0", sha256="ba" * 32),
)
SDK = SdkPin(
    constraint="^0.1.0",
    version="0.1.0",
    url="https://example.invalid/mcuhome-sdk-0.1.0.tar.zst",
    sha256="cd" * 32,
)

#: What a context carries here by default: the canonical device model and
#: one patch, at the two locations the format fixes. There is no
#: ``keys/signing.pub`` here, deliberately: a caller that needs one
#: supplies its own files.
CONTEXT_FILES = {
    "model/device-model.json": '{"model": 1}\n',
    "patches/zephyr/0001-fix.patch": "--- a\n+++ b\n",
}


def locked_context(root: Path, files: dict[str, str] | None = None) -> ContextManifest:
    """A context directory as a locked context is left (see
    ``docs/spec/build-context-format.md`` §5).

    Written file by file rather than through the workbench's
    ``create_context``, because half of these tests need a context no
    builder would ever produce: one file short, one file too many, a
    manifest that lies about its own ID. What is
    *not* rebuilt here is the ID rule — it comes from
    :func:`~mcuhome.model.context.context_id`, the same function the program
    under test reaches through, because the hashing rule is frozen
    (``docs/spec/build-context-format.md`` §6) and a second implementation
    of it in a test file would be the first place the two could drift
    apart.
    """
    contents = CONTEXT_FILES if files is None else files
    entries = []
    for name, text in sorted(contents.items()):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        entries.append(ContextFile(path=name, sha256=digest))
    manifest = ContextManifest(
        sdk=SDK,
        build_environment=ENVIRONMENT_PIN,
        board="nrf7002dk/nrf5340/cpuapp",
        files=tuple(entries),
        id=context_id(
            sdk_sha256=SDK.sha256,
            environment=ENVIRONMENT_PIN,
            board="nrf7002dk/nrf5340/cpuapp",
            files=entries,
        ),
    )
    write_context_manifest(manifest, out_dir=root)
    return manifest


@pytest.fixture
def context(tmp_path: Path) -> Path:
    """A clean locked context, at a path nothing fixes."""
    root = tmp_path / "s-42" / "ctx"
    root.mkdir(parents=True)
    locked_context(root)
    return root


# --------------------------------------------------------------------------
# build (``docs/spec/build-actions.md`` §2)
# --------------------------------------------------------------------------

#: A sysbuild log carrying one memory report per image, each preceded by
#: the build-step banner that is the only thing in a sysbuild log saying
#: whose output follows (``mcuhome/compiler/workspace.py:838``). The report itself
#: names no image, which is exactly why the banner has to be there.
BUILD_LOG = """\
[1/2] Performing build step for 'mcuboot'
Memory region         Used Size  Region Size  %age Used
           FLASH:       49152 B        64 KB     75.00%
[2/2] Performing build step for 'app'
Memory region         Used Size  Region Size  %age Used
           FLASH:      859672 B         1 MB     81.99%
"""

#: The same build, relinked the other way round. Which image sysbuild
#: builds first is ninja's scheduling decision, so both logs are ordinary
#: output of one context — and the report has to come out the same.
BUILD_LOG_APP_FIRST = """\
[1/2] Performing build step for 'app'
Memory region         Used Size  Region Size  %age Used
           FLASH:      859672 B         1 MB     81.99%
[2/2] Performing build step for 'mcuboot'
Memory region         Used Size  Region Size  %age Used
           FLASH:       49152 B        64 KB     75.00%
"""

#: What the built application's ``.config`` says. imgtool's ``--version``
#: is the one of the four signing arguments the builder does not itself
#: decide, and ``CONFIG_ROM_START_OFFSET`` is the header offset the image
#: was really linked with — ``mcuhome/compiler/report.py`` cross-checks it against
#: the board's layout, so it has to be the registry's 512 here.
BUILD_KCONFIG = 'CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="1.4.0+0"\nCONFIG_ROM_START_OFFSET=0x200\n'

#: The board every fixture below builds for, and the four ``imgtool sign``
#: arguments ``mcuhome/model/registry.py`` states for it.
BOARD = "nrf7002dk/nrf5340/cpuapp"
SIGNING_ARGUMENTS = {"version": "1.4.0+0", "header-size": 512, "align": 4, "slot-size": 933888}

#: What an SDK package declares at the root of ``trees.sdk``. This
#: program fixes the file name and these three field names, and no
#: values — ``runtime`` is an opaque string and is never interpreted.
SDK_METADATA = {"sdk": 1, "generate": {"program": "bin/generate", "runtime": "py"}}

#: The ``layers[<name>].patchset`` encoding (see
#: :func:`mcuhome.compiler.abi.patchset`) over the two patches below,
#: computed once from the stated rule rather than by running the
#: implementation.
PATCHSET_OF_TWO = "sha256:e3078d6fac12ec834a2359fff0ebcfbcfecb38524e6d2f765c1a75d8f73e6a98"


@pytest.fixture(scope="session")
def device_model_json() -> str:
    """The canonical model a locked context carries, as JSON.

    A real model rather than a hand-written one: the format puts
    ``model/device-model.json`` in the context and
    ``mcuhome.model.modelfile.read_model`` refuses anything that is not
    one, so a fixture that faked it would test the fake. It comes from
    the reference device's golden wire document — the same document a
    remote build sends — because resolving a configuration into one is
    the workbench's half of the pipeline and lives in the tools
    repository.
    """
    return resolve_file(EXAMPLE).to_json()


class BuildStubs:
    """The two child processes of a build, stubbed, and the failures they can have.

    A real Matter compile is a quarter of an hour and this suite promises
    one second. The generate half is a real implementation of the
    generator ABI seen from the other side — it reads the request
    document the builder wrote, puts an application tree where that
    document says, and answers with a result document — because anything
    less would let the builder's own checking of that answer go untested.
    """

    def __init__(self, monkeypatch) -> None:
        #: Every child process the program started, in order.
        self.children: list[dict[str, Any]] = []
        #: Every :class:`~mcuhome.compiler.workspace.BuildPlan` it would have run.
        self.plans: list[Any] = []
        self.child_code = 0
        self.build_code = 0
        #: What a stubbed ``git apply`` leaves in the tree it was pointed
        #: at, so a test can see *which* tree was patched rather than only
        #: which path was named. Off by default: a patch that changes
        #: nothing is what every other test here wants.
        self.patch_marker: str | None = None
        #: The failure modes a build can have, drivable one at a time: a
        #: generate child that dies without a result document, one that
        #: answers a non-``success``, a compiler that cannot even start,
        #: and a sysbuild that leaves no tree to report the signing
        #: parameters from.
        self.generate_writes_result = True
        self.generate_status = "success"
        self.build_raises: str | None = None
        self.omit_app_hex = False
        self.omit_config = False
        #: The log the stubbed compile answers with. Settable, because
        #: which image a sysbuild relinks first is ninja's decision and
        #: the report must not depend on it.
        self.build_log = BUILD_LOG
        monkeypatch.setattr(abi, "_run_child", self._run_child)
        monkeypatch.setattr(abi.workspace, "run_build", self._run_build)

    def _run_child(self, command, *, env, directory):
        """Stands in for ``git apply`` and for the SDK's ``generate``."""
        record = {"command": list(command), "env": dict(env), "directory": Path(directory)}
        if len(command) == 3 and command[1] == abi.GENERATE_ACTION:
            document = json.loads(Path(command[2]).read_text(encoding="utf-8"))
            handed = Path(document["out"])
            # What the child sees the moment it starts — asserted empty by
            # the invocation test, because the ABI promises it emptiness.
            record["out_entries"] = sorted(p.name for p in handed.iterdir())
            # The request document itself, kept because it is written into
            # the step's `tmp` and is gone by the time a test could read it.
            record["request_document"] = document
        self.children.append(record)
        if self.patch_marker is not None and command[0] == "git":
            # `git -C <tree> apply …` — command[2] is the tree, and the
            # marker lands in it exactly as a real patch's changes would.
            (Path(command[2]) / self.patch_marker).write_text("patched\n", encoding="utf-8")
        if len(command) == 3 and command[1] == abi.GENERATE_ACTION:
            request = json.loads(Path(command[2]).read_text(encoding="utf-8"))
            tree = Path(request["out"]) / "app"
            tree.mkdir(parents=True, exist_ok=True)
            (tree / "CMakeLists.txt").write_text("# generated\n", encoding="utf-8")
            if self.generate_writes_result:
                Path(request["result"]).write_text(
                    json.dumps(
                        {
                            "result": 1,
                            "status": self.generate_status,
                            "action": abi.GENERATE_ACTION,
                            "session": request["session"],
                            "reason": None if self.generate_status == "success" else "x-test.made",
                            "error": None,
                        }
                    ),
                    encoding="utf-8",
                )
        return self.child_code, "" if self.child_code == 0 else "the child refused"

    def _run_build(self, plan, *, stream=None):
        """Stands in for stage 5, leaving behind exactly what sysbuild does."""
        self.plans.append(plan)
        if self.build_raises is not None:
            raise BuildError(self.build_raises)
        if self.build_code != 0:
            return self.build_code, self.build_log
        for image in ("app", "mcuboot"):
            output = plan.build_dir / image / "zephyr"
            output.mkdir(parents=True, exist_ok=True)
            if not (image == "app" and self.omit_app_hex):
                (output / "zephyr.hex").write_text(f":00000001FF {image}\n", encoding="utf-8")
            (output / "zephyr.bin").write_bytes(image.encode("utf-8"))
        if not self.omit_config:
            (plan.build_dir / "app" / "zephyr" / ".config").write_text(
                BUILD_KCONFIG, encoding="utf-8"
            )
        return 0, self.build_log


# --------------------------------------------------------------------------
# The SDK package's own entry point
# --------------------------------------------------------------------------

SDK_ROOT = Path(__file__).resolve().parents[2]


def test_the_sdk_package_declares_its_entry_point() -> None:
    """Held against the real repository rather than a fixture.

    At the root of the tree the orchestrator hands over as ``trees.sdk``
    there is a file named ``mcuhome-sdk.json`` — and for MCUHome's own
    SDK that tree is this repository. Every ``build`` test in this file
    manufactures the metadata in its fixture, so without this test the
    real package could ship without the file (it did — the review that
    forced this test found exactly that) and nothing would go red.

    Parsed through :func:`abi.sdk_entry_point` — the same rules the
    program applies to a foreign SDK — so the fixture's schema and the
    real file cannot drift apart without one of them failing.
    """
    entry, runtime = abi.sdk_entry_point(SDK_ROOT)
    assert entry == SDK_ROOT / "bin" / "generate"
    assert entry.is_file()
    assert os.access(entry, os.X_OK), "the entry point is invoked, not imported"
    assert runtime == "python3"


def test_the_entry_point_generates_the_real_tree(tmp_path, device_model_json: str) -> None:
    """The real stage 4, reached the way a build environment reaches it.

    In-process through :func:`mcuhome.compiler.sdkentry.main` — the launcher adds
    nothing but ``sys.path`` — with a real context directory and a real
    resolved model. The tree it writes is compared against
    :func:`mcuhome.compiler.generate.generate` for the same model, which is the
    byte-identity the module docstring promises: a remote build's
    application tree is not a second code path.
    """
    from mcuhome.compiler import sdkentry
    from mcuhome.compiler.generate import generate
    from mcuhome.model.modelfile import read_model

    context = tmp_path / "ctx"
    (context / "model").mkdir(parents=True)
    (context / "model" / "device-model.json").write_text(device_model_json, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    result = tmp_path / "result.json"
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "request": 1,
                "result": str(result),
                "session": "s-42",
                "context": str(context),
                "out": str(out),
            }
        ),
        encoding="utf-8",
    )

    assert sdkentry.main(["generate", "generate", str(request)]) == abi.EXIT_SUCCESS
    answer = json.loads(result.read_text(encoding="utf-8"))
    assert answer["status"] == "success"
    assert answer["action"] == "generate"
    assert answer["session"] == "s-42"

    model = read_model(context / "model" / "device-model.json")
    expected = generate(model, config_name=model.device.source)
    for relative, content in expected.items():
        assert (out / relative).read_text(encoding="utf-8") == content, relative


def test_the_entry_point_refuses_what_it_cannot_generate(tmp_path) -> None:
    """A context without a model is a ``failure`` the caller can read.

    This entry point maps any non-``success`` onto ``error.build.failed``;
    answering in that vocabulary means the message a user finally sees
    carries the actual cause instead of a generic wrapper around a lost
    detail.
    """
    from mcuhome.compiler import sdkentry

    context = tmp_path / "ctx"
    context.mkdir()
    result = tmp_path / "result.json"
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "request": 1,
                "result": str(result),
                "context": str(context),
                "out": str(tmp_path / "out"),
            }
        ),
        encoding="utf-8",
    )
    assert sdkentry.main(["generate", "generate", str(request)]) == abi.EXIT_FAILURE
    answer = json.loads(result.read_text(encoding="utf-8"))
    assert answer["status"] == "failure"
    assert answer["reason"] == "error.build.failed"
    assert "device-model.json" in answer["error"]["message"]


def test_the_launcher_runs_the_real_entry_point(tmp_path, device_model_json: str) -> None:
    """``bin/generate`` end to end, as a child process, shebang included.

    One subprocess in the whole suite, deliberately: the in-process tests
    above prove the action, and this proves the one thing they cannot —
    that the file this format names is startable by a caller that knows
    nothing but the path and the ABI. A caller written in Go gets exactly
    this.
    """
    import subprocess
    import sys

    context = tmp_path / "ctx"
    (context / "model").mkdir(parents=True)
    (context / "model" / "device-model.json").write_text(device_model_json, encoding="utf-8")
    out = tmp_path / "out"
    result = tmp_path / "result.json"
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"request": 1, "result": str(result), "context": str(context), "out": str(out)}),
        encoding="utf-8",
    )
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [str(SDK_ROOT / "bin" / "generate"), "generate", str(request)],
        capture_output=True,
        text=True,
        env={"PATH": str(Path(sys.executable).parent)},
    )
    assert completed.returncode == abi.EXIT_SUCCESS, completed.stderr
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "success"
    assert (out / "app" / "CMakeLists.txt").is_file()


def test_a_crash_inside_an_action_still_writes_a_result_document(caller: Caller) -> None:
    """Exit 1 promises a result document — a traceback is neither.

    Driven through :func:`abi.run_invocation` with an invoke that raises,
    which is exactly what an unhandled ``OSError`` in the middle of code
    generation is. The alternative this pins against: the exception
    escapes, Python exits 1 with a traceback on stderr, and the caller
    reads a promised-but-absent result document for a condition the
    program could have answered.
    """
    caller.request.write_text(json.dumps(caller.preamble(session="s-42")), encoding="utf-8")

    def explode(action: str, document: dict[str, Any]) -> dict[str, Any]:
        raise OSError("disk went away mid-action")

    code = abi.run_invocation([GENERATOR_PATH, "generate", str(caller.request)], explode)
    assert code == abi.EXIT_FAILURE
    document = caller.document()
    assert document["status"] == "failure"
    assert document["action"] == "generate"
    assert document["session"] == "s-42"
    # error.internal, not error.build.failed: the program crashed, which
    # is a fact about the program and not about the work it was doing.
    assert document["reason"] == "error.internal"
    assert "disk went away mid-action" in document["error"]["message"]


def test_the_echo_is_what_the_request_carried_and_no_more(caller: Caller) -> None:
    """A crash answers on the channel that was open, with what it was given.

    The action is echoed verbatim — whatever it was, implemented or not —
    and a session only when the request carried one, because a caller
    matches the answer to its own invocation by those two fields.
    """
    caller.request.write_text(json.dumps(caller.preamble()), encoding="utf-8")

    def explode(action: str, document: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("generation blew up")

    code = abi.run_invocation([GENERATOR_PATH, "nonsense", str(caller.request)], explode)
    assert code == abi.EXIT_FAILURE
    document = caller.document()
    assert document["action"] == "nonsense"
    assert "session" not in document
    assert document["reason"] == "error.internal"


# --------------------------------------------------------------------------
# The v3 invocation (docs/spec/build-environment-specification.md)
# --------------------------------------------------------------------------
#
# The primary invocation, and a different subject from everything above:
# no arguments, a fixed request path below a base directory that is never
# assumed, a result document named after the invocation, and one action.

#: What the record inside a workspace package says, and what nothing on
#: the machine that unpacks it can possibly find: the paths of the
#: machine that *built* it. Every step resolves them against where the
#: package actually is, which is why this is deliberately not a tmp path.
PACKED_TOPDIR = "/build/workspace"


class StepSetup(BuildStubs):
    """What an orchestrator and the environment's own packages arrange around a step.

    §4's tree below a base directory that is emphatically not ``/``, plus
    the two things the specification says nothing about because they are
    the environment's own: the unpacked workspace package (its manifest,
    its west workspace, its record, its pre-generated Matter code) and the
    variable that says where it is.
    """

    def __init__(self, tmp_path: Path, model_json: str, monkeypatch) -> None:
        super().__init__(monkeypatch)
        # -- what the orchestrator arranges (§4) ---------------------------
        self.base = tmp_path / "base"
        root = self.base / abi.STEP_DIR
        self.request_path = root / abi.STEP_REQUEST
        self.out = root / abi.STEP_OUT
        self.work = root / abi.STEP_WORK
        self.sdk = root / abi.STEP_SDK
        self.context = root / abi.STEP_CONTEXT
        self.cache = root / abi.STEP_CACHE
        for path in (self.out, self.work, self.context, self.cache):
            path.mkdir(parents=True)
        self.files = {
            "model/device-model.json": model_json,
            "keys/signing.pub": "-----BEGIN PUBLIC KEY-----\nnot-a-real-key\n",
        }
        self.manifest = locked_context(self.context, self.files)
        # The SDK the context pinned, delivered where §4 puts it — not
        # inside the environment's workspace, which is the whole reason a
        # step has to join the two.
        (self.sdk / "bin").mkdir(parents=True)
        (self.sdk / "bin" / "generate").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.sdk / abi.SDK_METADATA_FILE).write_text(json.dumps(SDK_METADATA), encoding="utf-8")

        # -- what the environment's packages provide -----------------------
        self.package = tmp_path / "env"
        self.topdir = self.package / "workspace"
        self.manifest_dir = self.topdir / "mcuhome-sdk"
        self.layers = {
            "zephyr": self.topdir / "zephyr",
            "chip": self.topdir / "modules" / "lib" / "connectedhomeip",
            "mcuboot": self.topdir / "bootloader" / "mcuboot",
        }
        for path in (*self.layers.values(), self.manifest_dir):
            path.mkdir(parents=True)
        for name, path in self.layers.items():
            # One file per tree, so that a view of the workspace can be
            # told apart from a copy of it: a mirrored tree's entries are
            # links to these, a copied tree's are bytes of their own.
            (path / "VERSION").write_text(f"{name}\n", encoding="utf-8")
        (self.topdir / ".west").mkdir()
        (self.topdir / ".west" / "config").write_text(
            "[manifest]\npath = mcuhome-sdk\nfile = west.yml\n", encoding="utf-8"
        )
        self.pregen = self.package / "matter-pregen" / "modules" / "lib" / "connectedhomeip"
        self.pregen.mkdir(parents=True)
        self.write_package_manifest(
            {
                "package": "mcuhome-build-workspace",
                "version": "0.1.0",
                "workspace": "workspace",
                "workspace-record": "workspace.json",
                "manifest-directory": "workspace/mcuhome-sdk",
                "matter-pregen-chip-root": "matter-pregen/modules/lib/connectedhomeip",
            }
        )
        self.write_record(self.packed_record())

        #: The environment the step was started in. No ``zap`` on it, on
        #: purpose: the tools package does not carry one, and the
        #: pre-generated data model is what makes that correct.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for tool in ("west", "gn"):
            executable = bindir / tool
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
        self.env = {
            abi.BASE_DIR_VAR: str(self.base),
            abi.WORKSPACE_PACKAGE_VAR: str(self.package),
            "PATH": str(bindir),
        }

    # -- arranging ---------------------------------------------------------

    def packed_record(self) -> dict[str, Any]:
        """The record as the package build wrote it: the builder's paths."""
        return {
            "workspace": 1,
            "topdir": PACKED_TOPDIR,
            "manifest": {"path": "mcuhome-sdk", "file": "west.yml"},
            "layers": {
                "zephyr": {"path": f"{PACKED_TOPDIR}/zephyr", "revision": "v4.4.0"},
                "chip": {"path": f"{PACKED_TOPDIR}/modules/lib/connectedhomeip"},
                "mcuboot": {"path": f"{PACKED_TOPDIR}/bootloader/mcuboot"},
                "sdk": {"path": f"{PACKED_TOPDIR}/mcuhome-sdk", "mounted": True},
            },
        }

    def write_record(self, document: Any) -> None:
        (self.package / "workspace.json").write_text(json.dumps(document), encoding="utf-8")

    def write_package_manifest(self, document: Any) -> None:
        (self.package / abi.WORKSPACE_PACKAGE_MANIFEST).write_text(
            json.dumps(document), encoding="utf-8"
        )

    def freeze(self) -> None:
        """Make the workspace package read-only, as the store's freeze does.

        The subprocess profile unpacks the environment's packages into a
        per-user store and marks the entry read-only; from then on it is
        immutable and shared by any number of concurrent builds. This is
        that state, produced the same way — the permission bits — so that
        "may not be written" is found out here exactly as it is found out
        there.
        """
        for directory, subdirectories, files in os.walk(self.package, topdown=False):
            base = Path(directory)
            for name in (*files, *subdirectories):
                entry = base / name
                if not entry.is_symlink():
                    entry.chmod(entry.stat().st_mode & ~0o222)
            base.chmod(base.stat().st_mode & ~0o222)

    def thaw(self) -> None:
        """Undo :meth:`freeze`, so the temporary directory can be removed."""
        for directory, subdirectories, files in os.walk(self.package):
            base = Path(directory)
            base.chmod(base.stat().st_mode | 0o200)
            for name in (*files, *subdirectories):
                entry = base / name
                if not entry.is_symlink():
                    entry.chmod(entry.stat().st_mode | 0o200)

    def workspace_state(self) -> dict[str, Any]:
        """Every byte and every mode of the workspace package, for comparison."""
        state: dict[str, Any] = {}
        for path in sorted(self.package.rglob("*")):
            relative = str(path.relative_to(self.package))
            if path.is_symlink():
                state[relative] = ("link", os.readlink(path))
            elif path.is_dir():
                state[relative] = ("dir", path.stat().st_mode)
            else:
                state[relative] = ("file", path.stat().st_mode, path.read_bytes())
        return state

    def view(self) -> Path:
        """Where the view of a workspace that may not be written is."""
        return self.work / abi.STEP_VIEW

    def patch(self, layer: str, name: str, body: str) -> None:
        """Put a patch into the context and re-lock it over the new file set."""
        path = self.context / "patches" / layer / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.files[f"patches/{layer}/{name}"] = body
        self.manifest = locked_context(self.context, self.files)

    def request(self, **fields: Any) -> dict[str, Any]:
        """The request document of §6.1, with *fields* replacing its own."""
        document = {
            "spec_generation": abi.SPEC_GENERATION,
            "session_id": "9f2c1a",
            "invocation_id": "9f2c1a-3",
            "action": "build",
            "parameters": {},
        }
        document.update(fields)
        return document

    def run(self, *, text: str | None = None, **fields: Any) -> int:
        """Write the request document and take one step. Returns the exit code."""
        if text is None:
            text = json.dumps(self.request(**fields))
        self.request_path.write_text(text, encoding="utf-8")
        return abi.step(self.env)

    def result(self, invocation_id: str = "9f2c1a-3") -> dict[str, Any]:
        """The result document §6.2 named."""
        path = self.out / f"{abi.RESULT_PREFIX}{invocation_id}{abi.RESULT_SUFFIX}"
        return json.loads(path.read_text(encoding="utf-8"))

    def out_entries(self) -> list[str]:
        return sorted(entry.name for entry in self.out.iterdir())

    def build_env(self) -> dict[str, str]:
        """The environment the compile would have run in."""
        return self.plans[0].env


@pytest.fixture
def stepped(tmp_path: Path, device_model_json: str, monkeypatch):
    setup = StepSetup(tmp_path, device_model_json, monkeypatch)
    yield setup
    # A frozen workspace and the copies taken out of it are read-only, and
    # pytest has to be able to remove its temporary directory afterwards.
    setup.thaw()
    for directory, subdirectories, _files in os.walk(tmp_path):
        for name in subdirectories:
            entry = Path(directory) / name
            if not entry.is_symlink():
                entry.chmod(entry.stat().st_mode | 0o700)


# -- the invocation (§6) ---------------------------------------------------


def test_a_step_takes_no_arguments_and_answers_in_out(stepped: StepSetup) -> None:
    """§6: run with no arguments, request at a fixed path, result in ``out``.

    The whole invocation in one test: nothing is passed, everything is
    found, and the answer is at the one name §6.2 fixes.
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    assert "result-9f2c1a-3.json" in stepped.out_entries()
    assert stepped.result()["status"] == "success"


def test_the_base_directory_is_never_assumed(stepped: StepSetup) -> None:
    """§4: "often ``/``, but never assume it".

    Every path of the step is resolved against the variable — nothing in
    this test lives at an absolute path this program could have guessed —
    and an environment without it gets no step at all.
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    assert (stepped.base / abi.STEP_DIR / abi.STEP_OUT / "firmware.bin").is_file()

    without = dict(stepped.env)
    del without[abi.BASE_DIR_VAR]
    assert abi.step(without) == abi.EXIT_UNUSABLE
    relative = {**stepped.env, abi.BASE_DIR_VAR: "base"}
    assert abi.step(relative) == abi.EXIT_UNUSABLE


def test_a_request_document_that_cannot_be_read_writes_nothing(stepped: StepSetup) -> None:
    """The one outcome that is not a result document.

    A step that cannot read the request has no ``invocation_id`` either,
    so there is no name to write an answer under. §6.3 covers it from the
    other side: "a step that produced no readable result document failed,
    whatever it exited with".
    """
    assert abi.step(stepped.env) == abi.EXIT_UNUSABLE
    assert stepped.out_entries() == []

    assert stepped.run(text="[1, 2, 3]") == abi.EXIT_UNUSABLE
    assert stepped.out_entries() == []

    assert stepped.run(text="{not json") == abi.EXIT_UNUSABLE
    assert stepped.out_entries() == []


@pytest.mark.parametrize("value", [None, 7, "", "../escape", ".", "with space", "a/b"])
def test_an_invocation_id_no_file_can_be_named_after_is_refused(
    stepped: StepSetup, value: Any
) -> None:
    """§6.1 promises the id is "safe to use directly in a filename".

    A value that is not gets no result document at all, rather than one at
    a path this program composed out of somebody else's ``..``.
    """
    document = stepped.request()
    if value is None:
        del document["invocation_id"]
    else:
        document["invocation_id"] = value
    assert stepped.run(text=json.dumps(document)) == abi.EXIT_UNUSABLE
    assert stepped.out_entries() == []


def test_unknown_fields_are_ignored(stepped: StepSetup) -> None:
    """ "Ignore fields you do not know" (§6.1) — what makes a change additive."""
    assert stepped.run(x_vendor="anything", unknown={"deep": [1, 2]}) == abi.EXIT_SUCCESS
    assert stepped.result()["status"] == "success"


# -- the two refusals (§6.2, §12) ------------------------------------------


@pytest.mark.parametrize("generation", [2, 4, "3", None, True])
def test_a_generation_this_environment_does_not_speak_is_unsupported(
    stepped: StepSetup, generation: Any
) -> None:
    """§12: "your entry point answers ``unsupported`` to a request
    generation it does not implement"."""
    document = stepped.request()
    if generation is None:
        del document["spec_generation"]
    else:
        document["spec_generation"] = generation
    assert stepped.run(text=json.dumps(document)) == abi.EXIT_FAILURE
    result = stepped.result()
    assert result["status"] == "unsupported"
    assert result["spec_generation"] == abi.SPEC_GENERATION
    assert result["artifacts"] == []
    assert stepped.plans == []


@pytest.mark.parametrize("action", ["describe", "verify", "sign", "x-vendor-thing", "", 4])
def test_an_action_this_environment_does_not_implement_is_unsupported(
    stepped: StepSetup, action: Any
) -> None:
    """``docs/spec/build-actions.md`` §1: an environment "answers
    ``unsupported`` to every other one".

    ``describe`` and ``verify`` are in the list on purpose: neither is an
    action (``docs/spec/build-actions.md`` §3) — the environment describes
    itself in its package metadata, and verifying the context is the
    orchestrator's own business.
    """
    assert stepped.run(action=action) == abi.EXIT_FAILURE
    result = stepped.result()
    assert result["status"] == "unsupported"
    assert result["message"]
    assert stepped.plans == []


# -- the result document (§6.2) --------------------------------------------


def test_the_result_document_carries_the_five_fields_and_no_others(stepped: StepSetup) -> None:
    """§6.2's table, read as a whole."""
    assert stepped.run() == abi.EXIT_SUCCESS
    result = stepped.result()
    assert list(result) == [
        "spec_generation",
        "invocation_id",
        "status",
        "message",
        "artifacts",
    ]
    assert result["spec_generation"] == 3
    assert result["invocation_id"] == "9f2c1a-3"
    assert result["status"] == "success"
    assert result["message"] == ""


def test_the_artifacts_are_the_names_build_actions_fixes(stepped: StepSetup) -> None:
    """``docs/spec/build-actions.md`` §2.1, and §6.2's "paths relative to ``out/``".

    The names are fixed because "the result document lists artifacts by
    name and nothing else: whoever signs has to find the image, and it
    finds it by knowing what it is called".
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.result()["artifacts"] == [
        "firmware.hex",
        "firmware.bin",
        "bootloader.hex",
        "build-report.json",
    ]
    for name in stepped.result()["artifacts"]:
        assert (stepped.out / name).is_file()
        assert "/" not in name


def test_the_result_document_is_not_one_of_the_artifacts(stepped: StepSetup) -> None:
    """§7 reserves ``result-*.json`` at the top of ``out`` for the answer itself."""
    assert stepped.run() == abi.EXIT_SUCCESS
    assert not any(name.startswith("result-") for name in stepped.result()["artifacts"])
    assert "result-9f2c1a-3.json" in stepped.out_entries()


def test_the_build_report_is_the_document_build_actions_describes(stepped: StepSetup) -> None:
    """``docs/spec/build-actions.md`` §2.2: the report a signer reads.

    The report version, the ``signing`` block with the four ``imgtool``
    arguments, and the memory report the sysbuild log carried.
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    report = json.loads((stepped.out / "build-report.json").read_text(encoding="utf-8"))
    assert report["report"] == abi.REPORT_VERSION
    assert report["signing"]["arguments"] == SIGNING_ARGUMENTS
    assert {entry["image"] for entry in report["memory"]} == {"app", "mcuboot"}


def test_a_steps_exit_code_and_its_status_say_the_same_thing(stepped: StepSetup) -> None:
    """§6.3: exit ``0`` when a result document says ``success``, non-zero otherwise."""
    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.result()["status"] == "success"

    (stepped.context / "keys" / "signing.pub").unlink()
    assert stepped.run(invocation_id="9f2c1a-4") == abi.EXIT_FAILURE
    assert stepped.result("9f2c1a-4")["status"] == "failure"


def test_a_failure_says_what_went_wrong_and_declares_nothing(stepped: StepSetup) -> None:
    """§6.2: ``message`` is "free text for a human", ``artifacts`` is what
    this step wrote — and a step that failed wrote none."""
    (stepped.context / "keys" / "signing.pub").unlink()
    assert stepped.run() == abi.EXIT_FAILURE
    result = stepped.result()
    assert result["status"] == "failure"
    assert "signing.pub" in result["message"]
    assert result["artifacts"] == []


def test_a_crash_inside_a_step_is_still_a_result_document(stepped: StepSetup, monkeypatch) -> None:
    """A step that produced no readable result document failed anyway (§6.3).

    So an unexpected error is answered on the channel that was going to be
    written regardless, rather than as a traceback the orchestrator has to
    guess from.
    """

    def explode(self, mode):
        raise RuntimeError("the builder blew up")

    monkeypatch.setattr(abi._Build, "execute", explode)
    assert stepped.run() == abi.EXIT_FAILURE
    result = stepped.result()
    assert result["status"] == "failure"
    assert "the builder blew up" in result["message"]


# -- the environment's own half --------------------------------------------


def test_the_record_is_read_where_the_package_actually_is(stepped: StepSetup) -> None:
    """A package is unpacked somewhere else than it was built.

    The record names the paths of the machine that built the workspace;
    the step resolves them against where the package is now. Nothing here
    exists at :data:`PACKED_TOPDIR`, so a step that used the record
    verbatim could not build at all. What it is resolved against is where
    the package is now; the view under ``work`` is then one further move of
    the same record, and neither of them is the packed path.
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    plan = stepped.plans[0]
    assert plan.topdir == stepped.view()
    assert PACKED_TOPDIR not in str(plan.env.get("ZEPHYR_BASE"))
    assert plan.env["ZEPHYR_BASE"] == str(stepped.view() / "zephyr")
    assert (stepped.view() / "zephyr" / "VERSION").read_text(encoding="utf-8") == "zephyr\n"


def test_a_layer_outside_the_recorded_workspace_is_a_typed_failure(stepped: StepSetup) -> None:
    """The one path that cannot be moved with the workspace it is not in."""
    record = stepped.packed_record()
    record["layers"]["chip"]["path"] = "/elsewhere/connectedhomeip"
    stepped.write_record(record)
    assert stepped.run() == abi.EXIT_FAILURE
    assert "/elsewhere/connectedhomeip" in stepped.result()["message"]


def test_an_environment_that_cannot_say_where_its_workspace_is(stepped: StepSetup) -> None:
    """Both halves of it: the variable, and the package's own manifest."""
    without = {key: value for key, value in stepped.env.items() if key != abi.WORKSPACE_PACKAGE_VAR}
    stepped.request_path.write_text(json.dumps(stepped.request()), encoding="utf-8")
    assert abi.step(without) == abi.EXIT_FAILURE
    assert abi.WORKSPACE_PACKAGE_VAR in stepped.result()["message"]

    stepped.write_package_manifest({"package": "mcuhome-build-workspace"})
    assert stepped.run(invocation_id="9f2c1a-4") == abi.EXIT_FAILURE
    assert "build-workspace.json" in stepped.result("9f2c1a-4")["message"]


def test_the_delivered_sdk_is_placed_where_west_looks_for_it(stepped: StepSetup) -> None:
    """§4 delivers the SDK at ``mcuhome/sdk``; west wants it in the workspace.

    The workspace package carries the manifest repository's directory
    empty for exactly this, and the step joins the two — inside the view,
    which is the only workspace a step builds in and the only place this
    program writes.
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    linked = stepped.view() / "mcuhome-sdk"
    assert linked.is_symlink()
    assert linked.resolve() == stepped.sdk.resolve()
    assert (linked / abi.SDK_METADATA_FILE).is_file()
    assert stepped.manifest_dir.is_dir() and not stepped.manifest_dir.is_symlink()


def test_a_step_sizes_its_parallelism_from_the_recommended_limits(
    stepped: StepSetup, monkeypatch
) -> None:
    """§6.1's ``limits``: what the orchestrator says the step should fit
    in is what the step plans with.

    The auto-detection behind the fallback is made to explode, so a step
    that sized itself from the machine — which, in a container, is the
    host and not what this build was given — fails this test rather than
    passing it quietly.
    """
    monkeypatch.setattr(jobs, "detect_jobs", lambda: pytest.fail("the machine was asked"))
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    monkeypatch.setattr(jobs, "available_ram_bytes", lambda: 64 * 1024**3)
    assert stepped.run(limits={"cpus": 3, "memory_bytes": 64 * 1024**3}) == abi.EXIT_SUCCESS
    plan = stepped.plans[0]
    assert "-o=-j3" in plan.command
    assert plan.env[abi.workspace.CHIP_JOBS_VAR] == "3"
    assert plan.env[abi.workspace.CMAKE_JOBS_VAR] == "3"


def test_a_step_without_limits_sizes_itself(stepped: StepSetup, monkeypatch) -> None:
    """An orchestrator that states no limits has said nothing about the
    machine, so the machine answers."""
    monkeypatch.setattr(jobs, "detect_jobs", lambda: 7)
    assert stepped.run() == abi.EXIT_SUCCESS
    assert "-o=-j7" in stepped.plans[0].command


def test_a_limits_object_that_states_neither_figure_is_the_same_as_none(
    stepped: StepSetup, monkeypatch
) -> None:
    monkeypatch.setattr(jobs, "detect_jobs", lambda: 7)
    assert stepped.run(limits={}) == abi.EXIT_SUCCESS
    assert "-o=-j7" in stepped.plans[0].command


def test_a_step_never_takes_a_job_count_out_of_the_environment(
    stepped: StepSetup, monkeypatch
) -> None:
    """The one environment variable this boundary defines is the base
    directory (§4). A job count in the environment would be a second
    channel for something the request document carries."""
    monkeypatch.setattr(jobs, "detect_jobs", lambda: 5)
    stepped.env["MCUHOME_JOBS"] = "11"
    assert stepped.run() == abi.EXIT_SUCCESS
    assert "-o=-j5" in stepped.plans[0].command


def test_a_record_without_an_sdk_layer_fails_the_step(stepped: StepSetup) -> None:
    """The view is built around the manifest repository's place, and a
    record that names none has no workspace this program can build in.

    The failure is the record's, said once and before anything is
    assembled: a workspace package whose record forgot a layer is broken,
    and a step that assembled a view of it anyway would fail later and
    about something else.
    """
    record = stepped.packed_record()
    del record["layers"]["sdk"]
    stepped.write_record(record)

    assert stepped.run() == abi.EXIT_FAILURE
    # The record's own refusal, verbatim rather than "sdk appears
    # somewhere in the message": without it the step dies later on a
    # KeyError whose message contains the word too.
    assert stepped.result()["message"] == (
        f"the west workspace at {stepped.topdir} has no sdk layer"
    )
    assert stepped.plans == []
    assert not stepped.view().exists()


def test_a_step_without_a_delivered_sdk_says_so(stepped: StepSetup) -> None:
    """ "Assume nothing exists. Check what your step needs **before** you
    start long work" (§7)."""
    (stepped.sdk / abi.SDK_METADATA_FILE).unlink()
    assert stepped.run() == abi.EXIT_FAILURE
    assert str(stepped.sdk) in stepped.result()["message"]
    assert stepped.plans == []


def test_the_delivered_sdk_wins_over_one_sitting_in_the_workspace(
    stepped: StepSetup,
) -> None:
    """§4's ``mcuhome/sdk`` is the SDK, whatever else is at the manifest path.

    A tree at the manifest repository's place is not a delivery: it is
    whatever the environment's own workspace happens to carry — a leftover,
    a profile's mount, a checkout somebody left there — and the context
    pinned the one the orchestrator delivered. The view links what was
    delivered, so that is what is generated from and compiled; the
    workspace's own directory is left exactly as it was found. A workspace
    where the two are the **same** directory is the developer's, and that
    resolves to the same rule with nothing to choose between.
    """
    (stepped.manifest_dir / "bin").mkdir()
    (stepped.manifest_dir / "bin" / "generate").write_text("#!/bin/sh\n", encoding="utf-8")
    (stepped.manifest_dir / abi.SDK_METADATA_FILE).write_text(
        json.dumps(SDK_METADATA), encoding="utf-8"
    )
    before = stepped.workspace_state()

    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.workspace_state() == before

    linked = stepped.view() / "mcuhome-sdk"
    assert linked.resolve() == stepped.sdk.resolve()
    generated = [child for child in stepped.children if child["command"][1] == abi.GENERATE_ACTION]
    assert (
        Path(generated[0]["command"][0]).resolve() == (stepped.sdk / "bin" / "generate").resolve()
    )


def test_no_zap_is_needed_when_the_package_carries_the_data_model(stepped: StepSetup) -> None:
    """zap is deliberately absent from the tools package.

    Its output is a release constant, generated when the workspace package
    was built, and CHIP's own switch points the build at it. The
    environment in this suite has no ``zap`` on ``PATH`` at all, which is
    what makes this a test rather than a claim.
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.build_env()[abi.workspace.PREGEN_DIR_VAR] == str(stepped.pregen)
    assert abi.workspace.missing_tools(stepped.build_env()) == []


def test_a_package_without_a_pre_generated_model_still_needs_zap(stepped: StepSetup) -> None:
    """The other side of the same rule, so it is not a coincidence."""
    stepped.write_package_manifest({"workspace": "workspace", "workspace-record": "workspace.json"})
    assert stepped.run() == abi.EXIT_FAILURE
    assert "zap" in stepped.result()["message"]


# -- work, out and the caches (§7, §8) -------------------------------------


def test_the_step_works_in_work_and_delivers_into_out(stepped: StepSetup) -> None:
    """§7: build in ``work``, copy the finished file into ``out``.

    ``TMPDIR`` points inside ``work`` as §7 advises, and nothing but the
    artifacts and the result document reaches ``out``.
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    assert Path(stepped.build_env()["TMPDIR"]) == stepped.work / abi.STEP_TMP
    assert stepped.plans[0].build_dir.is_relative_to(stepped.work)
    assert stepped.out_entries() == [
        "bootloader.hex",
        "build-report.json",
        "firmware.bin",
        "firmware.hex",
        "result-9f2c1a-3.json",
    ]


def test_a_step_is_clean_and_writes_no_session_marker(stepped: StepSetup) -> None:
    """§3: ``work`` is empty at the start of every step.

    So there is no prior state of this session to find, no marker worth
    writing, and the build is the clean one — ``--pristine always``.
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    command = stepped.plans[0].command
    assert command[command.index("--pristine") + 1] == "always"
    assert not (stepped.work / "session.json").exists()


def test_the_local_cache_tier_is_the_primary_one(stepped: StepSetup) -> None:
    """§8: ``local`` "is yours, it is writable, and it is gone afterwards",
    and ccache goes in ``<tier>/ccache``."""
    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.build_env()["CCACHE_DIR"] == str(stepped.cache / "local" / "ccache")
    assert "CCACHE_REMOTE_STORAGE" not in stepped.build_env()


def test_a_tier_the_orchestrator_mounted_is_a_read_only_secondary(stepped: StepSetup) -> None:
    """§8: "Assume every tier except ``local`` is read-only" — so it is used
    as a secondary and never written."""
    shared = stepped.cache / "session" / "ccache"
    shared.mkdir(parents=True)
    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.build_env()["CCACHE_REMOTE_STORAGE"] == f"file:{shared}|read-only"
    assert stepped.build_env()["CCACHE_DIR"] == str(stepped.cache / "local" / "ccache")


# -- patches (§10) ---------------------------------------------------------


def test_a_patched_layer_is_applied_to_the_environments_own_tree(stepped: StepSetup) -> None:
    """§10: applying the context's patches is the environment's job,
    because it is the only one who knows where the trees are.

    The tree it applies them to is the copy inside the view, never the
    environment's own — that is the same in every profile.
    """
    stepped.patch("zephyr", "0001-fix.patch", "--- a\n+++ b\n")
    assert stepped.run() == abi.EXIT_SUCCESS
    copy = stepped.view() / stepped.layers["zephyr"].relative_to(stepped.topdir)
    applied = [child for child in stepped.children if child["command"][0] == "git"]
    assert len(applied) == 1
    assert applied[0]["command"][:4] == ["git", "-C", str(copy), "apply"]
    assert "-p1" in applied[0]["command"]


def test_a_patch_that_does_not_apply_fails_the_step(stepped: StepSetup) -> None:
    """§10: "A patch that does not apply fails the step. Do not retry at
    another strip level and do not apply it partially"."""
    stepped.patch("zephyr", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.child_code = 1
    assert stepped.run() == abi.EXIT_FAILURE
    assert "0001-fix.patch" in stepped.result()["message"]
    assert stepped.plans == []


def test_a_patched_sdk_layer_is_applied_to_a_copy_under_work(stepped: StepSetup) -> None:
    """§10: a tree that may not be written is copied under ``work`` first.

    The SDK is the orchestrator's input — delivered per build context,
    shared with whoever else holds those bytes — so this is the one tree a
    step copies before patching it. "Materialize a patched copy of it
    under ``work``, and build against a view of the environment in which
    that copy stands in for the original."
    """
    stepped.patch("sdk", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.patch_marker = "patched.txt"
    assert stepped.run() == abi.EXIT_SUCCESS

    copy = stepped.work / abi.STEP_PATCHED / "sdk"
    assert (copy / abi.SDK_METADATA_FILE).is_file()
    assert (copy / "patched.txt").is_file()
    applied = [child for child in stepped.children if child["command"][0] == "git"]
    assert len(applied) == 1
    assert Path(applied[0]["command"][2]).resolve() == copy.resolve()


def test_a_patched_sdk_layer_never_touches_what_was_delivered(stepped: StepSetup) -> None:
    """The point of the copy: ``mcuhome/sdk`` comes out of the step as it
    went in, byte for byte and file for file."""
    before = {
        path.relative_to(stepped.sdk): path.read_bytes()
        for path in sorted(stepped.sdk.rglob("*"))
        if path.is_file()
    }
    stepped.patch("sdk", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.patch_marker = "patched.txt"
    assert stepped.run() == abi.EXIT_SUCCESS
    after = {
        path.relative_to(stepped.sdk): path.read_bytes()
        for path in sorted(stepped.sdk.rglob("*"))
        if path.is_file()
    }
    assert after == before
    assert not (stepped.sdk / "patched.txt").exists()


def test_the_patched_copy_is_what_the_build_actually_uses(stepped: StepSetup) -> None:
    """The view §10 asks for: the copy stands in for the original.

    The link west follows points at the copy, so everything downstream —
    west, the code generator the SDK declares, every include path — reaches
    the patched tree without knowing there was a patch. A copy nothing
    built against would be a copy for nothing.
    """
    stepped.patch("sdk", "0001-fix.patch", "--- a\n+++ b\n")
    assert stepped.run() == abi.EXIT_SUCCESS

    copy = stepped.work / abi.STEP_PATCHED / "sdk"
    linked = stepped.view() / "mcuhome-sdk"
    assert linked.is_symlink()
    assert linked.resolve() == copy.resolve()
    assert stepped.manifest_dir.is_dir() and not stepped.manifest_dir.is_symlink()
    generated = [child for child in stepped.children if child["command"][1] == abi.GENERATE_ACTION]
    assert Path(generated[0]["command"][0]).resolve() == (copy / "bin" / "generate").resolve()


def test_an_unpatched_sdk_is_not_copied_at_all(stepped: StepSetup) -> None:
    """§10: "Trees no patch names stay where they are; copying is the price
    of a patch and is paid only for the trees a patch actually names"."""
    assert stepped.run() == abi.EXIT_SUCCESS
    assert not (stepped.work / abi.STEP_PATCHED).exists()
    assert (stepped.view() / "mcuhome-sdk").resolve() == stepped.sdk.resolve()


# -- the view every step builds against (§10) -------------------------------


def test_a_writable_workspace_is_viewed_too(stepped: StepSetup) -> None:
    """§10 permits patching a disposable tree in place; this program does not.

    Nothing here decides which profile is running, because nothing here
    behaves differently: a workspace this step could write into is built
    against a view exactly like one it could not, and comes out of the step
    byte for byte as it went in. The container profile's workspace pays the
    view's cost for that, and what it buys is one code path.
    """
    stepped.patch("zephyr", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.patch_marker = "patched.txt"
    before = stepped.workspace_state()
    assert stepped.run() == abi.EXIT_SUCCESS

    view = stepped.view()
    assert stepped.plans[0].topdir == view
    assert stepped.workspace_state() == before
    assert stepped.manifest_dir.is_dir() and not stepped.manifest_dir.is_symlink()
    copy = view / stepped.layers["zephyr"].relative_to(stepped.topdir)
    assert (copy / "patched.txt").is_file()


def test_a_frozen_workspace_is_built_against_a_view(stepped: StepSetup) -> None:
    """§10: "build against a view of the environment".

    The subprocess profile's store may not be touched at all, so the
    workspace the build runs in is the one this step assembled under
    ``work`` — and the whole build sees it: west's top directory, the
    Zephyr base every generated CMakeLists resolves against, every layer
    path.
    """
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS
    view = stepped.view()
    assert stepped.plans[0].topdir == view
    assert stepped.build_env()["ZEPHYR_BASE"] == str(view / "zephyr")
    assert (view / ".west" / "config").is_file()


def test_an_unpatched_build_on_a_frozen_workspace_copies_nothing(stepped: StepSetup) -> None:
    """§10: "copying is the price of a patch and is paid only for the trees
    a patch actually names".

    A read-only store still needs a view — the SDK has to be reachable
    where west looks for the manifest repository, and that place is in the
    store. But the view shares the store's bytes rather than duplicating
    them: a mirrored file is a hard link onto the store's own inode.
    """
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS
    assert not (stepped.work / abi.STEP_PATCHED).exists()
    view = stepped.view()
    for name, path in stepped.layers.items():
        entry = view / path.relative_to(stepped.topdir) / "VERSION"
        original = path / "VERSION"
        assert not entry.is_symlink(), name
        assert entry.stat().st_ino == original.stat().st_ino, name
        assert entry.stat().st_dev == original.stat().st_dev, name


def test_a_frozen_workspace_is_byte_identical_after_a_patched_build(
    stepped: StepSetup,
) -> None:
    """The point of the view: the store comes out of the step as it went in.

    §3's promise for this profile — "the subprocess profile keeps the store
    read-only" — measured over every file, every mode and every link of the
    package, with a patch on one of its trees.
    """
    stepped.patch("chip", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.patch_marker = "patched.txt"
    stepped.freeze()
    before = stepped.workspace_state()
    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.workspace_state() == before


def test_a_patched_tree_of_a_frozen_workspace_is_copied_into_the_view(
    stepped: StepSetup,
) -> None:
    """§10: "Materialize a patched copy of it under ``work``".

    The copy stands at the tree's own place inside the view, so the
    patches land in it and everything that resolves a path through the
    workspace reaches it.
    """
    stepped.patch("chip", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.patch_marker = "patched.txt"
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS

    copy = stepped.view() / stepped.layers["chip"].relative_to(stepped.topdir)
    assert copy.is_dir() and not copy.is_symlink()
    assert (copy / "patched.txt").is_file()
    applied = [child for child in stepped.children if child["command"][0] == "git"]
    assert [child["command"][2] for child in applied] == [str(copy)]


def test_only_the_patched_tree_of_a_frozen_workspace_is_copied(stepped: StepSetup) -> None:
    """§10: "Trees no patch names stay where they are"."""
    stepped.patch("chip", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS

    view = stepped.view()
    for name in ("zephyr", "mcuboot"):
        mirrored = view / stepped.layers[name].relative_to(stepped.topdir) / "VERSION"
        original = stepped.layers[name] / "VERSION"
        assert mirrored.stat().st_ino == original.stat().st_ino, name
    copied = view / stepped.layers["chip"].relative_to(stepped.topdir) / "VERSION"
    assert copied.stat().st_ino != (stepped.layers["chip"] / "VERSION").stat().st_ino


def test_a_mirrored_tree_of_a_frozen_workspace_resolves_inside_the_view(
    stepped: StepSetup,
) -> None:
    """West's containment check resolves both sides, so the view must be real.

    ``west.commands._ext_specs`` refuses a workspace whose project
    declares a ``west-commands`` file that "escapes project path" — and it
    decides that with ``west.util.escapes_directory``, which calls
    ``Path.resolve()`` on the file and on the project directory. A
    mirrored tree of symbolic links fails that check: the directory
    resolves to the view and the file behind the link resolves to the
    store. Zephyr and MCUboot both declare ``west-commands``, and
    ``west build`` is one of those extensions, so this is the difference
    between a build and a workspace west will not load at all.
    """
    for layer in stepped.layers.values():
        (layer / "scripts").mkdir()
        (layer / "scripts" / "west-commands.yml").write_text("west-commands:\n", encoding="utf-8")
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS

    view = stepped.view()
    for name, path in stepped.layers.items():
        project = view / path.relative_to(stepped.topdir)
        spec = project / "scripts" / "west-commands.yml"
        assert spec.is_file(), name
        # escapes_directory(spec, project) — verbatim, in west's own terms.
        assert spec.resolve().is_relative_to(project.resolve()), name


def test_a_mirrored_tree_is_copied_where_no_hard_link_can_be_made(
    stepped: StepSetup, monkeypatch
) -> None:
    """A store on another filesystem than ``work`` still yields a real view.

    ``os.link`` answers ``EXDEV`` across a filesystem boundary, and that
    is an ordinary way to run this: the store is a per-user cache in the
    user's home and ``work`` is wherever the session directory was put —
    another disk, a tmpfs, a container mount. The mirror then copies
    instead of linking, and the copy has to carry the two properties the
    link carried: the store's own mode, which a frozen store makes
    read-only, so a build cannot write through the view either way; and a
    path that resolves inside the view, which is what west's containment
    check needs and the whole reason the mirror is real directories.
    """

    def across_a_filesystem(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", across_a_filesystem)
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS

    view = stepped.view()
    for name, path in stepped.layers.items():
        original = path / "VERSION"
        project = view / path.relative_to(stepped.topdir)
        entry = project / "VERSION"
        assert not entry.is_symlink(), name
        assert entry.stat().st_ino != original.stat().st_ino, name
        assert entry.read_bytes() == original.read_bytes(), name
        mode = stat.S_IMODE(entry.stat().st_mode)
        assert mode == stat.S_IMODE(original.stat().st_mode), name
        assert not mode & 0o222, name
        assert entry.resolve().is_relative_to(project.resolve()), name


def test_a_patched_copy_of_a_frozen_tree_can_be_written(stepped: StepSetup) -> None:
    """A copy of a read-only tree is read-only until it is made writable.

    ``shutil.copytree`` copies the modes with the bytes, so the copy of a
    frozen store entry arrives as unwritable as the entry — and a patch
    could not be applied to it. Checked directly, because the stubbed
    ``git apply`` of this suite would not notice.
    """
    stepped.freeze()
    stepped.patch("chip", "0001-fix.patch", "--- a\n+++ b\n")
    assert stepped.run() == abi.EXIT_SUCCESS

    copy = stepped.view() / stepped.layers["chip"].relative_to(stepped.topdir)
    (copy / "written-by-the-patch").write_text("ok\n", encoding="utf-8")


def test_a_patch_that_does_not_apply_fails_a_step_on_a_frozen_workspace(
    stepped: StepSetup,
) -> None:
    """§10: "A patch that does not apply fails the step" — view or no view."""
    stepped.patch("chip", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.freeze()
    stepped.child_code = 1
    assert stepped.run() == abi.EXIT_FAILURE
    assert "0001-fix.patch" in stepped.result()["message"]
    assert stepped.plans == []


def test_the_sdk_is_linked_into_the_view_of_a_frozen_workspace(stepped: StepSetup) -> None:
    """West needs the manifest repository inside the workspace, and the
    workspace may not be written — so the link goes into the view.

    The store's own manifest directory stays the empty directory the
    package carries; nothing was placed in it and nothing was replaced.
    """
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS

    linked = stepped.view() / "mcuhome-sdk"
    assert linked.is_symlink()
    assert linked.resolve() == stepped.sdk.resolve()
    assert stepped.manifest_dir.is_dir() and not stepped.manifest_dir.is_symlink()
    assert list(stepped.manifest_dir.iterdir()) == []


def test_a_patched_sdk_on_a_frozen_workspace_is_the_link_target(stepped: StepSetup) -> None:
    """The two mechanisms compose: the SDK's copy is under ``work`` because
    the SDK is the orchestrator's input, and the view links to that copy
    instead of to what was delivered."""
    stepped.patch("sdk", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS

    copy = stepped.work / abi.STEP_PATCHED / "sdk"
    assert (stepped.view() / "mcuhome-sdk").resolve() == copy.resolve()


def test_every_tree_of_a_view_is_a_real_directory(stepped: StepSetup) -> None:
    """Why the view mirrors the trees instead of linking them.

    ``os.getcwd`` resolves symbolic links, so a tool started with its
    working directory inside a linked tree is in the store as far as the
    kernel is concerned — and west, walking up from there, finds the
    store's workspace rather than this view. Zephyr's build does exactly
    that: ``cmake/modules/zephyr_module.cmake`` runs
    ``scripts/zephyr_module.py`` with the working directory set to
    ``ZEPHYR_BASE``, and that script asks west which projects the
    workspace has. Measured against west 1.5.0: with the tree linked the
    answer names the store, with the tree a real directory it names the
    view.
    """
    stepped.patch("chip", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS

    view = stepped.view()
    for name, path in stepped.layers.items():
        entry = view / path.relative_to(stepped.topdir)
        assert entry.is_dir() and not entry.is_symlink(), name


def test_an_unpatched_tree_is_declared_unwritable_to_what_reads_it(
    stepped: StepSetup,
) -> None:
    """What a step says a tree may take is what this step actually copied.

    The declaration is not decoration: it is the gate a patch has to pass
    (a layer that carries patches and is not declared writable fails the
    step) and the ``sdk`` entry travels verbatim into the code generator's
    own request document, which is where it can be read back. An unpatched
    build copies nothing, so every tree in the view is a link or a mirror
    onto the environment's workspace and none of them may be written —
    including on a workspace that happens to be writable, which is what
    the removed write probe used to answer "yes" to.
    """
    assert stepped.run() == abi.EXIT_SUCCESS
    generated = [child for child in stepped.children if child["command"][1] == abi.GENERATE_ACTION]
    assert generated[0]["request_document"]["trees"]["sdk"] == {
        "path": str(stepped.view() / "mcuhome-sdk"),
        "writable": False,
    }


def test_a_patched_tree_is_declared_writable_because_it_is_a_copy(
    stepped: StepSetup,
) -> None:
    """The other half of the same statement, and the only ``True`` there is.

    The ``sdk`` layer patched means the step copied it under ``work``, and
    that copy is the one thing here anything may write into.
    """
    stepped.patch("sdk", "0001-fix.patch", "--- a\n+++ b\n")
    assert stepped.run() == abi.EXIT_SUCCESS
    generated = [child for child in stepped.children if child["command"][1] == abi.GENERATE_ACTION]
    assert generated[0]["request_document"]["trees"]["sdk"] == {
        "path": str(stepped.view() / "mcuhome-sdk"),
        "writable": True,
    }


def test_a_developer_workspace_is_viewed_and_never_written(stepped: StepSetup) -> None:
    """The workspace somebody is developing in, and the SDK is its own checkout.

    A build against a west workspace a person maintains delivers that
    workspace's **manifest checkout** as ``mcuhome/sdk`` (§4): the SDK
    under development and the manifest repository are the same directory.
    Nothing about that reaches this program — it is neither told nor asked
    — and nothing about it needs to: the view links the manifest
    repository's place at what was delivered, which here is the directory
    that is already there, and the workspace comes out of the step byte for
    byte, mode for mode and link for link as it went in.

    That is the whole of the old write probe's job. It called a writable
    workspace disposable and replaced the manifest repository's directory
    with a symbolic link into a build directory that is gone at the end of
    the step — into a tree somebody keeps.
    """
    shutil.rmtree(stepped.sdk)
    stepped.sdk.symlink_to(stepped.manifest_dir)
    (stepped.manifest_dir / "bin").mkdir()
    (stepped.manifest_dir / "bin" / "generate").write_text("#!/bin/sh\n", encoding="utf-8")
    (stepped.manifest_dir / abi.SDK_METADATA_FILE).write_text(
        json.dumps(SDK_METADATA), encoding="utf-8"
    )
    before = stepped.workspace_state()

    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.workspace_state() == before

    linked = stepped.view() / "mcuhome-sdk"
    assert linked.is_symlink()
    assert linked.resolve() == stepped.manifest_dir.resolve()
    assert stepped.plans[0].topdir == stepped.view()


def test_a_workspace_nobody_froze_is_not_written_into_either(stepped: StepSetup) -> None:
    """The probe is gone, and with it the question it answered.

    A workspace with no marker and every write permission — the container
    profile's baked tree, and a developer's own checkout — used to be
    "disposable": patched where it stood, with the SDK linked into it. Both
    of those are writes into a tree this program does not own, and §11 says
    to assume it may not be written. So neither happens, and the signals
    that used to decide are not consulted because there is nothing left to
    decide.
    """
    assert os.access(stepped.topdir, os.W_OK)
    assert not (stepped.package / ".mcuhome-provisioned").exists()
    stepped.patch("chip", "0001-fix.patch", "--- a\n+++ b\n")
    before = stepped.workspace_state()

    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.workspace_state() == before
    view = stepped.view()
    assert view.is_dir()
    copy = view / stepped.layers["chip"].relative_to(stepped.topdir)
    assert copy.is_dir() and not copy.is_symlink()
    applied = [child for child in stepped.children if child["command"][0] == "git"]
    assert [child["command"][2] for child in applied] == [str(copy)]


def test_a_workspace_without_a_manifest_directory_still_gets_its_sdk(
    stepped: StepSetup,
) -> None:
    """The link is made by name, not by what the workspace happens to hold.

    The manifest repository's directory is the one the workspace package
    carries empty for this moment. A package that carries it not at all
    would otherwise produce a view with no SDK in it, and a failure much
    later — in west or in CMake, about something else entirely.
    """
    stepped.manifest_dir.rmdir()
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS

    linked = stepped.view() / "mcuhome-sdk"
    assert linked.is_symlink()
    assert linked.resolve() == stepped.sdk.resolve()


def test_the_view_has_the_shape_of_the_workspace(stepped: StepSetup) -> None:
    """Nothing of the workspace is lost on the way into the view.

    A view is only a view if everything the workspace has is reachable
    through it under the same name — west's configuration, the trees, and
    whatever else a package put at the top level. What differs is how each
    entry is reached, not whether it is there.
    """
    (stepped.topdir / "extra-thing").write_text("in the package\n", encoding="utf-8")
    stepped.patch("chip", "0001-fix.patch", "--- a\n+++ b\n")
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS

    view = stepped.view()
    assert sorted(entry.name for entry in view.iterdir()) == sorted(
        entry.name for entry in stepped.topdir.iterdir()
    )
    assert (view / "extra-thing").read_text(encoding="utf-8") == "in the package\n"
    assert (view / ".west" / "config").read_text(encoding="utf-8") == (
        stepped.topdir / ".west" / "config"
    ).read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("west") is None, reason="west is not on PATH")
def test_west_resolves_the_workspace_through_the_view(stepped: StepSetup) -> None:
    """The view is a west workspace, checked with west itself.

    Replacing a project directory with a symbolic link is a pattern west
    documents in its own manifest code ("some existing users … use symlinks
    to existing project repositories outside the workspace as a cache"),
    and its containment check is lexical for that reason. This holds the
    view against the real tool rather than against that promise: the top
    directory, and the projects the manifest resolves to.
    """
    stepped.freeze()
    assert stepped.run() == abi.EXIT_SUCCESS
    view = stepped.view()
    (view / "mcuhome-sdk" / "west.yml").write_text(
        "manifest:\n  self:\n    path: mcuhome-sdk\n  projects: []\n", encoding="utf-8"
    )
    for start in (view, view / "zephyr"):
        topdir = subprocess.run(
            ["west", "topdir"],
            cwd=start,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert Path(topdir) == view, start


# -- the generator, and what it is handed ----------------------------------


def test_the_code_generator_comes_from_the_delivered_sdk(stepped: StepSetup) -> None:
    """``docs/spec/build-actions.md`` §2: "the code generator ships in
    ``mcuhome/sdk``, so the generated application belongs to the SDK the
    context pinned rather than to the environment's own vintage"."""
    assert stepped.run() == abi.EXIT_SUCCESS
    generated = [child for child in stepped.children if child["command"][1] == abi.GENERATE_ACTION]
    assert len(generated) == 1
    assert generated[0]["command"][0] == str(stepped.view() / "mcuhome-sdk" / "bin" / "generate")
    assert (
        Path(generated[0]["command"][0]).resolve() == (stepped.sdk / "bin" / "generate").resolve()
    )
    assert generated[0]["out_entries"] == []


def test_the_session_id_reaches_the_generator_and_nothing_else(stepped: StepSetup) -> None:
    """§6.1: ``session_id`` is opaque — "never build a path from it"."""
    assert stepped.run(session_id="../../not-a-path") == abi.EXIT_SUCCESS
    generated = [child for child in stepped.children if child["command"][1] == abi.GENERATE_ACTION]
    request = json.loads(Path(generated[0]["command"][2]).read_text(encoding="utf-8"))
    assert request["session"] == "../../not-a-path"
    assert stepped.out_entries() == [
        "bootloader.hex",
        "build-report.json",
        "firmware.bin",
        "firmware.hex",
        "result-9f2c1a-3.json",
    ]


def test_the_step_never_writes_into_the_build_context(stepped: StepSetup) -> None:
    """§9: "Never modify anything in it"."""
    before = {
        path: path.read_bytes() for path in sorted(stepped.context.rglob("*")) if path.is_file()
    }
    assert stepped.run() == abi.EXIT_SUCCESS
    after = {
        path: path.read_bytes() for path in sorted(stepped.context.rglob("*")) if path.is_file()
    }
    assert after == before


# -- the entry point the environment ships ---------------------------------


def test_the_entry_point_runs_this_module_with_no_arguments() -> None:
    """The other side of §6, and the reason the argv shape can be the
    discriminator: the environment's entry point passes nothing.

    ``packaging/build-environment/build-environment-entry`` is what a
    profile puts at ``mcuhome/bin/build-environment-entry``; everything it
    does is set the environment up and hand over to this module.
    """
    entry = (REPO_ROOT / "packaging" / "build-environment" / "build-environment-entry").read_text(
        encoding="utf-8"
    )
    assert "-m mcuhome.compiler.abi" in entry
    assert not re.search(r"-m mcuhome\.compiler\.abi\s+\S", entry)
    assert f"${{{abi.BASE_DIR_VAR}:-/}}" in entry


def test_a_step_does_not_verify_the_context_it_was_handed(stepped: StepSetup) -> None:
    """``docs/spec/build-actions.md`` §3: verifying the context is not an action.

    "The orchestrator creates the context, hashes it, and delivers it …
    There is nothing an environment could confirm that the orchestrator
    does not already know from its own bytes." So a step reads what it
    needs — the model, the key, the patches — and builds; the integrity
    list it is not responsible for is not even required to be there.
    """
    (stepped.context / "manifest.yaml").unlink()
    assert stepped.run() == abi.EXIT_SUCCESS
    assert stepped.result()["status"] == "success"
    assert "context" not in stepped.result()
