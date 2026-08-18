# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and helpers for the builder tests.

Everything here needs nothing but :mod:`mcuhome.model` and
:mod:`mcuhome.compiler` — the two packages ADR 0024 moves into this
repository. Nothing imports :mod:`mcuhome.workbench`, which lives in the
tools repository and is not installed next to these tests; where a test
used to reach for it, this file carries the small piece of it the test
actually needed (the example model below, the context writer at the end).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from ruamel.yaml import YAML

from mcuhome.model.context import (
    BACKEND_DIR,
    CONTEXT_FILE,
    MANIFEST_FILE,
    ContextFile,
    ContextManifest,
    ContextRequest,
    EnvironmentPin,
    context_id,
)
from mcuhome.model.hashes import sha256_file
from mcuhome.model.model import DeviceModel

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
EXAMPLES_DIR = REPO_ROOT / "docs" / "design" / "examples"
DATA_DIR = TESTS_DIR / "data"
GOLDEN_DIR = DATA_DIR / "golden"

#: The import package the distributions of ADR 0020 share, and the
#: directory it is assembled from in this checkout. It is a PEP 420
#: namespace package, which is why the directory is named here at all:
#: the import system cannot enumerate one. ``find_spec("mcuhome")``
#: answers with ``origin is None`` and a search-location list that holds
#: path-hook tokens rather than directories under an editable install, so
#: "what is under the namespace" is a question about the tree.
NAMESPACE = "mcuhome"
NAMESPACE_DIR = REPO_ROOT / NAMESPACE

#: The packages the whole-package invariant searches must cover — the
#: distributions of ADR 0020 decision 1 this repository ships, by import
#: name. ``mcuhome.workbench`` is the third and lives in the tools
#: repository since ADR 0024, where the same list names it alone.
#: :func:`package_modules` checks this list against what is actually in
#: :data:`NAMESPACE_DIR`, so a third subpackage cannot arrive unsearched.
PACKAGES = ("mcuhome.compiler", "mcuhome.model")


def package_modules() -> list[Path]:
    """Every ``.py`` file of every package the invariants have to cover.

    Derived from the importable packages rather than from one module's
    directory. A directory glob reads "every module there is" only while
    there is one package; after the ADR 0020 split it would keep passing
    while quietly examining fewer files, which is worse than not
    searching at all. Callers assert that a module they know must be
    examined came back, so the day this list falls behind is the day a
    test fails.

    Three things are checked here rather than left to those callers,
    because none of them has a module a caller could name:

    * ``mcuhome`` is still a namespace package. An ``__init__.py`` there
      would have to belong to one of the distributions that all deliver
      into that directory, and PEP 420 forbids it for exactly that reason.
    * No module sits directly under the namespace directory. Such a file
      is in no distribution, ships with none of them, and is invisible to
      every search below.
    * :data:`PACKAGES` lists every subpackage there is, and each one is
      imported *from this checkout*. The second half matters as much as
      the first: against a non-editable install the searches would read
      copies in ``site-packages`` while the tests exercise the tree.
    """
    spec = importlib.util.find_spec(NAMESPACE)
    assert spec is not None, f"{NAMESPACE} is not importable"
    assert spec.origin is None, (
        f"{NAMESPACE} has become a regular package (origin={spec.origin}). "
        "PEP 420 forbids an __init__.py there — several distributions deliver "
        "into that directory and only one of them could own the file."
    )

    loose = sorted(path.name for path in NAMESPACE_DIR.glob("*.py"))
    assert not loose, (
        f"{loose} sit directly under {NAMESPACE_DIR} — no distribution "
        "ships them and no invariant searches them"
    )
    subpackages = {
        path.name for path in NAMESPACE_DIR.iterdir() if (path / "__init__.py").is_file()
    }
    expected = {name.rpartition(".")[2] for name in PACKAGES}
    assert subpackages == expected, (
        f"the namespace holds {sorted(subpackages)} but the searches cover "
        f"{sorted(expected)} — extend conftest.PACKAGES"
    )

    found: list[Path] = []
    for name in PACKAGES:
        spec = importlib.util.find_spec(name)
        assert spec is not None and spec.origin is not None, f"{name} is not importable"
        directory = Path(spec.origin).parent
        assert directory == NAMESPACE_DIR / name.rpartition(".")[2], (
            f"{name} imports from {directory}, not from this checkout — the "
            "invariants would search files the tests do not run"
        )
        found.extend(directory.glob("*.py"))
    assert found, "the invariant searches would examine nothing"
    return sorted(found)


@pytest.fixture(autouse=True)
def _no_real_signing_key(monkeypatch, tmp_path):
    """No test may touch the developer's own firmware signing key.

    ``mcuhome build`` generates one on first need under
    ``$XDG_CONFIG_HOME/mcuhome/`` (ADR 0015 decision 8), which on the
    machine running this suite is a real, long-lived private key. A test
    that reaches it would either read a secret it has no business
    reading or — worse — create one silently outside a temporary
    directory. Point the variables at the test's own tmp_path instead;
    tests that care about the resolution rules pass an explicit ``env``.

    ``HOME`` is redirected as well, and not for symmetry: without
    ``XDG_CONFIG_HOME`` the key sits under ``~/.config``, so the two
    variables are two names for the same directory and covering one of
    them covers half the paths that lead there.

    **What this fixture no longer has to catch.** The package itself
    stopped reading the process — ``tests_py/test_userpaths.py`` proves
    it for every module — so nothing here resolves a key out of the
    environment pytest happens to run in. What is left for this fixture
    is everything that hands the process environment *in*: the command
    line's ``env=os.environ``, and any test that does the same.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.delenv("MCUHOME_SIGNING_KEY", raising=False)


# The autouse fixture that made every docker call an assertion error left
# with the thing that made docker calls: driving a build container is the
# workbench's now, and no module in this repository starts one. The guard
# lives there, over the orchestrator it guards.


# --- the example model, without the workbench -------------------------
#
# Resolving a configuration is stages 1-3, which is mcuhome.workbench —
# and since ADR 0024 the workbench lives in the tools repository. This
# repository reads the same model from the pregenerated wire document
# instead (the device-model.json golden, exactly what `mcuhome build
# --model` consumes): the tools repo's test_model_golden pins that
# golden against the real resolver, so both repositories test the same
# model without sharing code.

#: The fixture configuration tree. Nothing here resolves it — that is
#: the workbench's — but its ``secrets.yaml`` is where the one device
#: with commissioning credentials of its own states them, and
#: ``test_pairing`` builds that device's model from those values.
FIXTURE_TREE = DATA_DIR / "tree"


def resolve_file(path: Path) -> DeviceModel:
    """The example at *path*, resolved — from its golden wire document."""
    golden = GOLDEN_DIR / f"{path.stem}.device-model.json"
    return DeviceModel.from_dict(json.loads(golden.read_text(encoding="utf-8")))


# --- workbench-free context writer ------------------------------------
#
# The tests' own third implementation of the §3.3 rule, like the build
# server's. Creating a context directory is workbench machinery and since
# ADR 0024 that machinery is in the tools repository; the context
# *format* is vocabulary and lives in `mcuhome.model.context`, where
# `mcuhome.compiler.contextread` — the code under test — reads it from.
# So the tests write one the way every other party computes one: hash the
# content files, state them in a ContextManifest, and take the ID from
# `context_id` rather than restating the rule. A test that carried its
# own ID arithmetic would be the first place the two sides of the
# contract could agree with each other by accident.


def context_files(root: Path) -> tuple[ContextFile, ...]:
    """Every regular file under *root* that is context content, hashed.

    Neither context document — ``manifest.yaml`` (the list itself) nor
    ``context.yaml`` (the request, whose never-hashed fields would leak
    into the identity through the back door) — is content, and neither is
    the backend-written ``.mcuhome/`` runtime directory
    (build-container-contract.md §3.2).
    """
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in (MANIFEST_FILE, CONTEXT_FILE) or relative.split("/", 1)[0] == BACKEND_DIR:
            continue
        entries.append(ContextFile(path=relative, sha256=sha256_file(path)))
    entries.sort(key=lambda entry: entry.path)
    return tuple(entries)


def _dump_document(document: dict, path: Path) -> Path:
    """*document* as the YAML a context carries, at *path*.

    Line wrapping off, as on the writing side under test: a ``sha256:``
    digest is 71 characters and ruamel's default width folds it onto a
    second line, which §3.3.1 has a stricter reader refuse rather than
    reassemble.
    """
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.width = 4096
    with path.open("w", encoding="utf-8") as handle:
        yaml.dump(document, handle)
    return path


def write_context_request(request: ContextRequest, *, out_dir: Path) -> Path:
    """Write ``context.yaml`` into *out_dir* and return its path."""
    return _dump_document(request.to_dict(), out_dir / CONTEXT_FILE)


def write_context_manifest(manifest: ContextManifest, *, out_dir: Path) -> Path:
    """Write ``manifest.yaml`` into *out_dir* and return its path."""
    return _dump_document(manifest.to_dict(), out_dir / MANIFEST_FILE)


def lock_context(
    out_dir: Path, *, request: ContextRequest, build_environment: EnvironmentPin
) -> ContextManifest:
    """Freeze a created context: hash its files and write ``manifest.yaml``.

    *request* is passed rather than read back out of ``context.yaml``,
    which is the one place this helper is shorter than the workbench's
    ``lock_context``: the caller wrote the request a line earlier and a
    reader of the request is not what these tests are proving. Everything
    that reaches the manifest is the same — the request's pins restated,
    the pinned *build_environment* from the request, and the ID over the
    files actually present.
    """
    files = context_files(out_dir)
    manifest = ContextManifest(
        sdk=request.sdk,
        build_environment=build_environment,
        board=request.board,
        files=files,
        id=context_id(
            sdk_sha256=request.sdk.sha256,
            environment_digest=build_environment.digest,
            board=request.board,
            files=files,
        ),
    )
    write_context_manifest(manifest, out_dir=out_dir)
    return manifest
