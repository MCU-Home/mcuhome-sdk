# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""One distribution per subpackage, out of one source tree.

The tree under ``mcuhome/`` is a PEP 420 namespace with one subpackage
per distribution, and the project files that ship them live under
``packaging/``. Two properties hold that arrangement together, and
neither is visible from the code:

* **every subpackage is shipped by exactly one distribution.** Project
  files claiming file subsets of one directory is a relationship
  pip cannot express, so the claims have to partition — and a
  subpackage that no project file names would simply never be
  installed by anyone.
* **they carry the same version, from the same place.** One version,
  one tag, one release, and therefore no "which package works with
  which SDK" to answer. A second
  literal of the version number anywhere is the beginning of that
  question.

Read out of the ``pyproject.toml`` files with the standard library's own
TOML parser rather than out of built artifacts: a test that builds three
wheels needs a network for the build backend, and this suite is the fast
half of the strategy (builder-pipeline.md §9). What a built wheel
actually contains is proved once, at migration time, against the wheels.

What is *not* here is anything that is a claim about
``mcuhome-workbench`` alone — its ``remote`` extra lives in
``test_packaging_workbench.py``, on the other side of the repository split.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from conftest import NAMESPACE, NAMESPACE_DIR, PACKAGES, REPO_ROOT

import mcuhome.model

PACKAGING_DIR = REPO_ROOT / "packaging"

#: The definition file of the three release lines, and the member that is
#: the version of everything this repository publishes as a distribution.
ENVIRONMENT_FILE = PACKAGING_DIR / "build-environment" / "environment.json"

#: ``<import package> -> <distribution>``. Both halves are asserted
#: against reality below; the mapping itself is what a reader needs.
#: ``mcuhome-workbench`` is the third of the three published
#: distributions and is not here — the repository split ships it out of
#: the tools repository, whose own ``test_packaging`` makes the claims
#: about it.
DISTRIBUTIONS = {
    "mcuhome.compiler": "mcuhome-compiler",
    "mcuhome.model": "mcuhome-model",
}

#: Where the one version lives (``mcuhome/model/__init__.py``), spelled
#: the way ``[tool.setuptools.dynamic]`` has to spell it.
VERSION_ATTR = "mcuhome.model.__version__"


def _project_files() -> dict[str, dict]:
    """Every distribution's parsed ``pyproject.toml``, by directory name."""
    found = {
        path.parent.name: tomllib.loads(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGING_DIR.glob("*/pyproject.toml"))
    }
    assert found, f"no project files under {PACKAGING_DIR}"
    return found


def test_every_subpackage_is_shipped_by_exactly_one_distribution() -> None:
    shipped: dict[str, str] = {}
    for directory, project in _project_files().items():
        packages = project["tool"]["setuptools"]["packages"]
        assert packages == [f"{NAMESPACE}.{directory}"], (
            f"packaging/{directory} ships {packages}; a distribution owns its "
            "own subpackage and nothing else, or two of them claim one file"
        )
        for name in packages:
            assert name not in shipped, f"{name} is shipped twice ({shipped[name]}, {directory})"
            shipped[name] = directory
    assert set(shipped) == set(PACKAGES), (
        f"the tree holds {sorted(PACKAGES)} but packaging/ ships {sorted(shipped)} — "
        "a subpackage nothing ships is a subpackage nobody can install"
    )


def test_the_package_directory_maps_onto_the_shared_tree() -> None:
    """The project files are elsewhere; the code they ship is not copied.

    ``mcuhome/`` has to sit at the repository root because that root is
    also the SDK package: the build environment's entry point puts it on
    ``PYTHONPATH`` (``packaging/build-environment/build-environment-entry``)
    and ``bin/generate`` inserts it into ``sys.path``. So the project
    directories reach up into it instead of holding sources of their own.
    """
    for directory, project in _project_files().items():
        package_dir = project["tool"]["setuptools"]["package-dir"]
        assert package_dir == {"": "../.."}, f"packaging/{directory} maps {package_dir}"
        # `build/`, `__pycache__/` and `*.egg-info/` are what a build
        # backend leaves behind; they are gitignored and mean nothing here.
        # `packaging/model` is imported as a backend of its own, so it
        # grows the second of those.
        sources = [
            path.name
            for path in (PACKAGING_DIR / directory).iterdir()
            if path.is_dir()
            and path.name not in ("build", "__pycache__")
            and not path.name.endswith(".egg-info")
        ]
        assert not sources, (
            f"packaging/{directory} has grown {sources} — the one source tree "
            "is mcuhome/, and a second copy of a module is not one"
        )


@pytest.mark.parametrize(("package", "distribution"), sorted(DISTRIBUTIONS.items()))
def test_the_distribution_name_is_the_one_the_adr_names(package: str, distribution: str) -> None:
    project = _project_files()[package.rpartition(".")[2]]
    assert project["project"]["name"] == distribution


def test_all_three_read_the_one_version_from_the_one_place() -> None:
    for name, project in _project_files().items():
        assert "version" in project["project"]["dynamic"], (
            f"packaging/{name} pins a version literal; the three "
            "distributions must share one version"
        )
        source = project["tool"]["setuptools"]["dynamic"]["version"]
        assert source == {"attr": VERSION_ATTR}, f"packaging/{name} reads {source}"


def test_a_checkout_answers_with_the_version_the_definition_file_declares() -> None:
    """No literal anywhere: ``sdk.version`` is the answer, read where it is.

    This is the path every developer, every test run and every editable
    install takes — there is no generated ``VERSION`` in a checkout, on
    purpose, because one left behind would answer for the tree long after
    the definition file moved on.
    """
    declared = json.loads(ENVIRONMENT_FILE.read_text(encoding="utf-8"))["sdk"]["version"]
    assert mcuhome.model.__version__ == declared
    assert not (NAMESPACE_DIR / "model" / "VERSION").exists(), (
        "a checkout carries no generated VERSION; this one would shadow the "
        "definition file for everything that imports mcuhome.model"
    )


def test_a_shipped_copy_answers_with_the_generated_version_file(tmp_path: Path) -> None:
    """The other path: a wheel and an SDK archive carry ``VERSION`` and no more.

    Neither ships ``packaging/``, so the derivation has nothing to read —
    which is why the build writes the answer beside the module and the
    module prefers it.
    """
    package = tmp_path / "site" / "mcuhome" / "model"
    package.mkdir(parents=True)
    (package / "VERSION").write_text("9.9.9.dev7\n", encoding="utf-8")
    assert mcuhome.model._declared_version(package) == "9.9.9.dev7"


def test_a_copy_with_neither_says_so_rather_than_guessing(tmp_path: Path) -> None:
    """A version nobody can derive is a refusal that names both places."""
    package = tmp_path / "mcuhome" / "model"
    package.mkdir(parents=True)
    with pytest.raises(RuntimeError) as refusal:
        mcuhome.model._declared_version(package)
    assert "VERSION" in str(refusal.value)
    assert "environment.json" in str(refusal.value)


@pytest.fixture(scope="module")
def version_backend():
    """``packaging/model/_versionfile.py`` imported as a module.

    Loaded by path: it is a PEP 517 in-tree backend, addressed by
    ``backend-path`` and by nothing that is importable from here.
    """
    spec = importlib.util.spec_from_file_location(
        "_versionfile", PACKAGING_DIR / "model" / "_versionfile.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_model_build_backend_writes_and_removes_the_version_file(version_backend) -> None:
    """The generated file exists for the build and never outlives it.

    A wheel build has to put ``VERSION`` into the source tree — that is
    where the distribution's code is read from — and a working tree that
    kept it afterwards would stop deriving its version. Both halves are
    the property, so both are asserted.
    """
    assert version_backend.declared_version() == mcuhome.model.__version__
    assert not version_backend.VERSION_FILE.exists()
    with version_backend.generated_version() as written:
        assert written.read_text(encoding="utf-8") == f"{mcuhome.model.__version__}\n"
    assert not version_backend.VERSION_FILE.exists()


def test_a_stale_generated_version_is_a_refusal_and_not_a_second_opinion(
    version_backend,
) -> None:
    """A killed build can leave one behind, and it would answer forever.

    Every wheel built from this tree and every import of
    ``mcuhome.model`` would take the stale number instead of the declared
    one — the drift the derivation exists to end — so a file that
    disagrees stops the build and says how to clear it. One that agrees is
    the right answer and is left exactly where it is.
    """
    stale = version_backend.VERSION_FILE
    assert not stale.exists()
    stale.write_text("0.0.0.dev0\n", encoding="utf-8")
    try:
        with pytest.raises(SystemExit) as refusal, version_backend.generated_version():
            pass
        assert "0.0.0.dev0" in str(refusal.value)
        assert version_backend.declared_version() in str(refusal.value)
        assert stale.exists(), "a file this backend did not write is not deleted by it"

        stale.write_text(f"{version_backend.declared_version()}\n", encoding="utf-8")
        with version_backend.generated_version() as kept:
            assert kept == stale
        assert stale.exists()
    finally:
        stale.unlink(missing_ok=True)


def test_an_editable_install_goes_through_the_untouched_backend(version_backend) -> None:
    """``build_editable`` is setuptools' own, so no ``VERSION`` is ever written.

    This is the whole reason the generation is a build backend and not a
    ``setup.py`` step: PEP 517 names the editable case separately, and it
    is the one case that must not get the file.
    """
    import setuptools.build_meta  # noqa: PLC0415 - only this test needs a backend

    project = tomllib.loads(
        (PACKAGING_DIR / "model" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert project["build-system"]["build-backend"] == "_versionfile"
    assert project["build-system"]["backend-path"] == ["."]
    assert project["tool"]["setuptools"]["package-data"] == {"mcuhome.model": ["VERSION"]}

    assert version_backend.build_editable is setuptools.build_meta.build_editable
    assert version_backend.build_wheel is not setuptools.build_meta.build_wheel


def test_the_installed_distributions_all_carry_that_version() -> None:
    """And it is the version the code answers with, not a stale install.

    The property a single shared version buys is that "which package
    works with which SDK" cannot be asked. It stops being true the
    moment two of the three
    are installed from different releases, which no amount of metadata
    prevents — so it is checked where it can be seen, in the environment
    the suite runs in.
    """
    for distribution in sorted(DISTRIBUTIONS.values()):
        assert metadata.version(distribution) == mcuhome.model.__version__


def test_no_module_sits_outside_the_three_subpackages() -> None:
    """Restated against the tree, because ``package_modules`` is lazy.

    The whole-package invariants check this on their way past, but only
    when one of them runs. It is the layout rule of the migration and
    deserves a name of its own: a module directly under the namespace
    ships in no wheel, and PEP 420 forbids the ``__init__.py`` that would
    make it ship in all three.
    """
    assert not list(NAMESPACE_DIR.glob("*.py"))
    assert {path.name for path in NAMESPACE_DIR.iterdir() if path.is_dir()} - {"__pycache__"} == {
        name.rpartition(".")[2] for name in PACKAGES
    }


def test_the_sdk_entry_point_still_names_a_program_that_exists() -> None:
    """``mcuhome-sdk.json`` is unchanged by the split, and must stay true."""
    import json

    sdk = json.loads((REPO_ROOT / "mcuhome-sdk.json").read_text(encoding="utf-8"))
    program = REPO_ROOT / sdk["generate"]["program"]
    assert program.is_file()
    source = program.read_text(encoding="utf-8")
    assert "from mcuhome.compiler.sdkentry import main" in source


def test_the_environment_entry_point_execs_the_module_that_holds_the_abi() -> None:
    """The entry point ships in the tools package and names an import path.

    It is the one file outside Python that spells the ABI module, and
    getting it wrong fails a real build in the environment rather than
    here — which is why it is asserted rather than assumed.
    """
    entry = (REPO_ROOT / "packaging" / "build-environment" / "build-environment-entry").read_text(
        encoding="utf-8"
    )
    assert "-m mcuhome.compiler.abi" in entry


def test_the_pyproject_at_the_root_ships_nothing() -> None:
    """The plain name ``mcuhome`` belongs to the command line (decision 2).

    A ``[project]`` table here would either claim that name or invent a
    fourth distribution nobody publishes. What the file keeps is the tool
    configuration, which is why it still exists at all.
    """
    root = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "project" not in root
    assert "build-system" not in root
    assert {"ruff", "pytest", "codespell"} <= set(root["tool"])


def test_only_the_model_distribution_is_dependency_free() -> None:
    """The build server consumes ``mcuhome-model`` and nothing else.

    The package it consumes carrying no third-party dependency at all
    is not decoration: it is what makes "orchestrates, never builds"
    survive contact with a dependency resolver.
    """
    requires = {
        name: metadata.requires(distribution) or [] for name, distribution in DISTRIBUTIONS.items()
    }
    assert requires["mcuhome.model"] == []
    assert f"mcuhome-model=={mcuhome.model.__version__}" in requires["mcuhome.compiler"]
    # And nothing here depends on the workbench, which is the packaging
    # half of the repository split: the compiler ships inside the SDK
    # package and runs in a build environment the workbench never enters.
    assert not [
        requirement
        for requirements in requires.values()
        for requirement in requirements
        if "mcuhome-workbench" in requirement
    ]


def test_the_import_edges_follow_the_dependency_arrows() -> None:
    """model imports model; the compiler never imports the workbench. As syntax.

    The fresh-venv proof of the migration demonstrated this once, at
    install level; this is the same fact as a permanent invariant, read
    from the syntax tree so it holds on every run rather than on the day
    somebody installs a distribution alone. The dependency arrows come
    from the package split, and the repository split leaves them for
    this repository: model depends on nothing, the compiler on the
    model — and an import
    against the arrow is a wheel that breaks only in the environment of
    whoever installed the smaller set, which is the quietest possible
    way to break. Here it is smaller than a virtual environment: an
    import of the workbench is a build step that cannot start, because
    the workbench is not in the build environment at all.
    """
    import ast

    allowed = {
        "model": {"model"},
        "compiler": {"model", "compiler"},
    }
    for package, may_use in allowed.items():
        for module in (NAMESPACE_DIR / package).glob("*.py"):
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
                found: str | None = None
                if isinstance(node, ast.ImportFrom) and node.module:
                    parts = node.module.split(".")
                    if parts[0] == "mcuhome" and len(parts) > 1:
                        found = parts[1]
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        parts = alias.name.split(".")
                        if parts[0] == "mcuhome" and len(parts) > 1:
                            found = parts[1]
                if found is not None:
                    assert found in may_use, (
                        f"{package}/{module.name} imports mcuhome.{found} — against "
                        f"the dependency arrow; {package} may use {sorted(may_use)}"
                    )
