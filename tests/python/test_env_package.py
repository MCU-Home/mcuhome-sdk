# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build-environment packages: the same bytes twice, and pins that agree.

``scripts/build_env_package.py`` produces the two packages the build
environment specification's package-set model asks for
(``docs/spec/build-environment-specification.md`` §2). Building one of them
for real costs gigabytes and a network; what this file tests are the
properties that do not:

* **Determinism.** A build context pins packages by hash, so a
  byte-different but content-identical archive can never satisfy a pin
  somebody else resolved. The proof is a second pack of the same tree
  compared byte for byte, over a tree that carries everything a package may
  carry — an executable, an empty directory, a symbolic link.
* **The declaration.** §5 fixes which members exist and §5.1 fixes the
  value of a package member and what a package's own metadata may state:
  the *abstract* set, without its own hash and with the tools family in
  place of one platform's package. A declaration that is merely almost right
  is not findable: the orchestrator matches images by these members.
* **The pins.** The tools package restates the Zephyr SDK, toolchain and gn
  pins the baked image already carries, and a restated pin is a drift risk —
  so it is checked against the Dockerfile, the way
  ``test_builder_workspace.py`` checks the Dockerfile against ``west.yml``.

**No docker and no downloads here.** Everything asserted below is a property
of this repository's own files.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_env_package.py"
DOCKERFILE = REPO_ROOT / "containers" / "build-container" / "Dockerfile"


@pytest.fixture(scope="module")
def builder():
    """``scripts/build_env_package.py`` imported as a module.

    Loaded by path rather than imported: ``scripts/`` is tooling, not a
    package, and adding it to the import path for the whole suite would put
    every script in it one name collision away from a test.
    """
    spec = importlib.util.spec_from_file_location("build_env_package", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    """A tree carrying every member kind a package may hold."""
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "empty").mkdir()
    (root / "a.txt").write_text("hello\n", encoding="utf-8")
    script = root / "sub" / "run"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o700)
    (root / "sub" / "link").symlink_to("../a.txt")
    return root


def _members(archive: Path) -> list[tarfile.TarInfo]:
    with open(archive, "rb") as handle:
        payload = zstandard.ZstdDecompressor().stream_reader(handle).read()
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        return tar.getmembers()


def test_two_packs_of_one_tree_are_byte_identical(builder, sample_tree, tmp_path):
    """The property the whole script exists for."""
    first = builder.write_archive(sample_tree, tmp_path / "one.tar.zst", mtime=1700000000)
    second = builder.write_archive(sample_tree, tmp_path / "two.tar.zst", mtime=1700000000)
    assert first == second
    assert (tmp_path / "one.tar.zst").read_bytes() == (tmp_path / "two.tar.zst").read_bytes()


def test_archive_carries_every_member_kind_normalized(builder, sample_tree, tmp_path):
    """Directories, symbolic links and the exec bit survive; nothing else does.

    The exec bit is the one mode fact a consumer reads — a toolchain full of
    compilers and the entry point are all spawned as children — and
    ownership, timestamps and the rest of the mode are noise that would
    change the digest for nothing.
    """
    builder.write_archive(sample_tree, tmp_path / "out.tar.zst", mtime=1700000000)
    members = {member.name: member for member in _members(tmp_path / "out.tar.zst")}

    assert set(members) == {"a.txt", "empty", "sub", "sub/link", "sub/run"}
    assert members["empty"].isdir()
    assert members["sub/link"].issym()
    assert members["sub/link"].linkname == "../a.txt"
    assert members["sub/run"].mode == 0o755
    assert members["a.txt"].mode == 0o644
    for member in members.values():
        assert member.mtime == 1700000000
        assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")


def test_members_are_sorted_by_byte_order(builder, tmp_path):
    """Member order is a property of the names, not of the filesystem."""
    root = tmp_path / "tree"
    root.mkdir()
    for name in ("b", "A", "a-1", "a", "Z"):
        (root / name).write_text(name, encoding="utf-8")
    builder.write_archive(root, tmp_path / "out.tar.zst", mtime=1)
    names = [member.name for member in _members(tmp_path / "out.tar.zst")]
    assert names == sorted(names, key=lambda name: name.encode("utf-8"))


def test_package_file_names_follow_the_scheme(builder):
    """``<name>-<version>.tar.zst``, and the tools package names its platform."""
    assert (
        builder.package_filename(builder.WORKSPACE_PACKAGE, "0.1.0")
        == "mcuhome-build-workspace-0.1.0.tar.zst"
    )
    assert (
        builder.package_filename(builder.tools_package_name("linux", "amd64"), "0.1.0")
        == "mcuhome-build-tools_linux-amd64-0.1.0.tar.zst"
    )


def test_the_architecture_suffix_is_separated_by_an_underscore(builder):
    """§5.1: the one ``_`` in a name splits the family from the platform.

    Everything else is lowercase alphanumerics and ``-``, which is what
    makes the split possible without knowing the family's name first.
    """
    name = builder.tools_package_name("linux", "arm64")
    assert name == "mcuhome-build-tools_linux-arm64"
    assert name.split("_", 1)[0] == builder.TOOLS_FAMILY
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*(?:_[a-z0-9][a-z0-9-]*)?", name)


def test_declaration_has_exactly_the_required_members(builder):
    """§5: the three fixed members plus one per package, every value a string."""
    document = builder.declaration(
        zephyr="4.4.0", workspace_version="0.1.0.dev1", tools_version="0.1.0"
    )
    assert set(document) == {
        "spec-generation",
        "zephyr.version",
        "build-context.generator-constraint",
        "packages.mcuhome-build-tools",
        "packages.mcuhome-build-workspace",
    }
    assert all(isinstance(value, str) for value in document.values())
    assert document["spec-generation"] == "3"


def test_the_package_set_is_one_member_per_package(builder):
    """§5.1: ``packages.<name>``, and no list packed into one value."""
    members = builder.package_members("2.4.0", "1.2.0")
    assert members == {
        "packages.mcuhome-build-tools": "1.2.0",
        "packages.mcuhome-build-workspace": "2.4.0",
    }
    for member, value in members.items():
        name = member[len(builder.PACKAGE_MEMBER_PREFIX) :]
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*(?:_[a-z0-9][a-z0-9-]*)?", name)
        assert ";" not in value


def test_the_declaration_a_package_carries_is_the_abstract_set(builder):
    """§5.1's first rule, and the two things a package build cannot know.

    The carrier cannot state its own hash — the declaration is inside the
    archive it would describe — and the tools entry names the *family*,
    because the sibling platforms' archives may not exist yet and their
    bytes differ on purpose. Both are the image's job to complete.
    """
    members = builder.package_members("2.4.0", "1.2.0")
    assert not any("@sha256:" in value for value in members.values())
    assert f"packages.{builder.TOOLS_FAMILY}" in members
    assert not any("_" in member for member in members)


def test_declaration_is_written_on_both_sides_of_the_archive(builder, sample_tree, tmp_path):
    """§5: inside the package *and* beside it, and the same document twice.

    Inside, because an unpacked store entry has to be able to say what it is
    with nothing else present. Beside, because "the orchestrator reads the
    declaration before it starts anything" — and before it starts anything
    it has an archive, not a tree. The one that is easy to forget is the
    second: a package without it works perfectly until a provisioner has to
    decide whether to fetch it.
    """
    output = tmp_path / "out"
    document = builder.declaration(
        zephyr="4.4.0", workspace_version="0.1.0.dev1", tools_version="0.1.0"
    )
    package = builder.publish_workspace(
        sample_tree, output, version="0.1.0.dev1", mtime=1700000000, document=document
    )

    sidecar = builder.declaration_sidecar(output, package.path.name)
    assert sidecar.exists(), f"no declaration beside {package.path.name}"
    assert sidecar.name == f"{package.path.name}.build-environment.json"

    inside = {member.name: member for member in _members(package.path)}
    assert builder.DECLARATION_FILE in inside

    with open(package.path, "rb") as handle:
        payload = zstandard.ZstdDecompressor().stream_reader(handle).read()
    with tarfile.open(fileobj=io.BytesIO(payload)) as tar:
        extracted = tar.extractfile(builder.DECLARATION_FILE)
        assert extracted is not None
        assert extracted.read() == sidecar.read_bytes(), "the two declarations disagree"

    assert json.loads(sidecar.read_text(encoding="utf-8")) == document


def test_declaration_sidecar_is_named_for_the_file_not_the_package(builder, tmp_path):
    """Two versions in one source directory must not overwrite each other."""
    first = builder.declaration_sidecar(tmp_path, "mcuhome-build-workspace-0.1.0.tar.zst")
    second = builder.declaration_sidecar(tmp_path, "mcuhome-build-workspace-0.1.1.tar.zst")
    assert first != second


def test_generator_constraint_names_the_workbench(builder):
    """§9.1: an environment that declares no constraint accepts nothing."""
    product, separator, _specifier = builder.GENERATOR_CONSTRAINT.partition(":")
    assert product == "mcuhome-workbench"
    assert separator == ":"


def test_index_extends_what_is_already_there(builder, tmp_path):
    """A source directory holding two packages keeps describing both."""
    index = tmp_path / "index.json"
    builder.write_index(
        index, package="mcuhome-build-workspace", version="0.1.0", file="a", sha256="x", size=1
    )
    builder.write_index(
        index,
        package="mcuhome-build-tools_linux-amd64",
        version="0.1.0",
        file="b",
        sha256="y",
        size=2,
    )
    document = json.loads(index.read_text(encoding="utf-8"))
    assert set(document["packages"]) == {
        "mcuhome-build-workspace",
        "mcuhome-build-tools_linux-amd64",
    }


def test_index_refuses_a_file_that_is_not_an_index(builder, tmp_path):
    """Never an overwrite: it is the only record of what is in that directory."""
    index = tmp_path / "index.json"
    index.write_text('{"nope": 1}', encoding="utf-8")
    with pytest.raises(SystemExit):
        builder.write_index(
            index, package="mcuhome-build-workspace", version="0.1.0", file="a", sha256="x", size=1
        )


def _dockerfile_arg(name: str) -> str:
    """The default of one ``ARG`` in the builder image's Dockerfile."""
    match = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE.read_text(encoding="utf-8"), re.M)
    assert match, f"{DOCKERFILE} declares no ARG {name}"
    return match.group(1)


@pytest.mark.parametrize(
    ("constant", "argument"),
    [
        ("ZEPHYR_SDK_VERSION", "ZEPHYR_SDK_VERSION"),
        ("ZEPHYR_TOOLCHAIN", "ZEPHYR_TOOLCHAIN"),
        ("GN_REVISION", "GN_REVISION"),
    ],
)
def test_tool_pins_agree_with_the_builder_image(builder, constant, argument):
    """A restated pin is a drift risk, so it is checked rather than trusted."""
    assert getattr(builder, constant) == _dockerfile_arg(argument)


@pytest.mark.parametrize(
    ("arch", "tool", "argument"),
    [
        ("amd64", "zephyr-sdk", "ZEPHYR_SDK_SHA256_AMD64"),
        ("arm64", "zephyr-sdk", "ZEPHYR_SDK_SHA256_ARM64"),
        ("amd64", "zephyr-toolchain", "ZEPHYR_TOOLCHAIN_SHA256_AMD64"),
        ("arm64", "zephyr-toolchain", "ZEPHYR_TOOLCHAIN_SHA256_ARM64"),
        ("amd64", "gn", "GN_SHA256_AMD64"),
        ("arm64", "gn", "GN_SHA256_ARM64"),
    ],
)
def test_download_hashes_agree_with_the_builder_image(builder, arch, tool, argument):
    """The same artifact means the same bytes, whichever build fetches it."""
    assert builder.TOOL_DOWNLOADS[arch][tool]["sha256"] == _dockerfile_arg(argument)


def test_base_image_agrees_with_the_builder_image(builder):
    """The wheel set's ABI is decided by this image's Python."""
    assert _dockerfile_arg("DEBIAN_IMAGE") == builder.BASE_IMAGE


def test_every_architecture_pins_every_tool(builder):
    """A half-pinned architecture is an architecture nobody can build for."""
    expected = {"zephyr-sdk", "zephyr-toolchain", "gn", "cmake", "ninja"}
    for arch, downloads in builder.TOOL_DOWNLOADS.items():
        assert set(downloads) == expected, arch
        for tool, pin in downloads.items():
            assert re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]), f"{arch}/{tool}"
            assert pin["url"].startswith("https://"), f"{arch}/{tool}"


def test_pregen_directory_is_the_shadow_workspace_chip_root(builder):
    """What a build is handed as ``CHIP_CODEGEN_PREGEN_DIR``.

    CHIP's CMake glue indexes the pre-generation directory by the path of
    the data-model file relative to ``CHIP_ROOT``, so the directory is laid
    out as a shadow of the workspace and the consumer is handed the shadow's
    CHIP root.
    """
    assert builder.pregen_chip_root() == f"{builder.PREGEN_DIR}/{builder.CHIP_PATH}"


def test_entry_point_is_an_executable_shell_script(builder):
    """§6: the orchestrator runs it, so it has to be runnable."""
    entry = REPO_ROOT / builder.ENTRY_SOURCE
    assert entry.exists()
    assert entry.stat().st_mode & 0o111, f"{entry} is not executable"
    subprocess.run(["sh", "-n", str(entry)], check=True)


def test_entry_point_takes_no_arguments_and_reads_the_fixed_paths(builder):
    """The invocation of §6, spelled out in the file that implements it."""
    text = (REPO_ROOT / builder.ENTRY_SOURCE).read_text(encoding="utf-8")
    assert "MCUHOME_BUILDER_BASE_DIR" in text
    assert "mcuhome/sdk" in text
    assert "mcuhome.compiler.abi" in text
    assert '"$@"' not in text, "the entry point is invoked with no arguments"


@pytest.mark.parametrize("absolute", [False, True])
def test_the_tools_root_fallback_follows_either_kind_of_link(builder, tmp_path, absolute):
    """§4 puts the entry point at a fixed path, and a profile may link it there.

    The fallback that derives ``MCUHOME_BUILD_ENV_TOOLS`` from the entry
    point's own location has to resolve that link, and a link target is
    absolute as often as it is relative — a profile that linked absolutely
    would otherwise get the directory the *link* sits in, which exists, looks
    plausible and is not the package.

    Run for real rather than read: the run gets far enough to fail on the
    virtual environment, and the path in that message is the answer the
    fallback arrived at.
    """
    package = tmp_path / "store" / "tools"
    (package / "bin").mkdir(parents=True)
    entry = package / "bin" / "build-environment-entry"
    shutil.copy(REPO_ROOT / builder.ENTRY_SOURCE, entry)
    entry.chmod(0o755)
    (package / "build-tools.json").write_text("{}", encoding="utf-8")

    link = tmp_path / "mcuhome" / "bin" / "build-environment-entry"
    link.parent.mkdir(parents=True)
    link.symlink_to(entry if absolute else os.path.relpath(entry, link.parent))

    completed = subprocess.run(
        [str(link)],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 70
    assert f"no build environment at {package}/venv" in completed.stderr, completed.stderr
