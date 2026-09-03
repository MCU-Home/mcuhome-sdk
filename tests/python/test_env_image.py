# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The thin build-environment image: what it may contain, and what it claims.

``containers/build-environment/Dockerfile`` and
``scripts/build_env_image.py`` deliver MCUHome's environment as a container
image — the container profile of
``docs/spec/build-environment-specification.md``. Building one for real
costs gigabytes and a container runtime; what this file tests are the
properties that do not:

* **It is an assembly.** §2 makes an image "a delivery of the set", so
  nothing may enter it that did not come out of a package: no source tree
  fetched at image build time, no wheel resolved against an index.
* **It says what it is.** §5.2's labels are read from the packages rather
  than typed, and an image assembled from packages it does not declare "is
  simply lying about what it is" — so the assembly refuses that case before
  it builds. What it labels is §5.1's *concrete* set: every package member
  hashed, and the tools family replaced by the one platform's package the
  image actually contains.
* **The pins agree.** The base image decides which interpreter the packaged
  wheel set fits, so the thin image, the baked image and the package build
  have to name the same one.

**No docker here.** Everything asserted below is a property of this
repository's own files.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import sys
import tarfile
from pathlib import Path

import pytest
import zstandard

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "build_env_image.py"
DOCKERFILE = REPO_ROOT / "containers" / "build-environment" / "Dockerfile"
BAKED_DOCKERFILE = REPO_ROOT / "containers" / "build-container" / "Dockerfile"
PACKAGE_SCRIPT = REPO_ROOT / "scripts" / "build_env_package.py"


def _load(path: Path, name: str):
    """A script in ``scripts/`` imported as a module.

    Loaded by path rather than imported: ``scripts/`` is tooling, not a
    package, and adding it to the import path for the whole suite would put
    every script in it one name collision away from a test.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def assembler():
    return _load(SCRIPT, "build_env_image")


@pytest.fixture(scope="module")
def packager():
    return _load(PACKAGE_SCRIPT, "build_env_package")


@pytest.fixture(scope="module")
def dockerfile() -> str:
    """The instructions, without the comments.

    The comments in that file explain at length what it does *not* do —
    "no ``west update``", "no index" — and a test that searched the whole
    text would be reading the explanation instead of the image.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def _arg(text: str, name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", text, re.M)
    assert match, f"no ARG {name} is declared"
    return match.group(1)


# --------------------------------------------------------------------------
# The base, which decides the interpreter the packaged wheels fit
# --------------------------------------------------------------------------


def test_the_base_image_is_the_one_the_wheel_set_was_built_for(dockerfile, packager):
    """One base, named in three places, and the wheels only fit that one."""
    assert _arg(dockerfile, "DEBIAN_IMAGE") == packager.BASE_IMAGE
    assert _arg(BAKED_DOCKERFILE.read_text(encoding="utf-8"), "DEBIAN_IMAGE") == packager.BASE_IMAGE


# --------------------------------------------------------------------------
# An assembly, not a build
# --------------------------------------------------------------------------


def test_nothing_in_the_image_fetches_a_source_tree(dockerfile):
    """§2: the image delivers the package set, it does not reproduce it."""
    for forbidden in ("west update", "git clone", "curl ", "wget "):
        assert forbidden not in dockerfile, f"the image runs {forbidden!r} at build time"


def test_the_wheel_set_is_installed_offline(dockerfile):
    """A wheel resolved against an index is not one the package contains."""
    install = re.search(r"pip install.*?\n(?:\s+.*\n)*", dockerfile)
    assert install, "the image never creates the build virtual environment"
    assert "--no-index" in install.group(0)
    assert "--find-links" in install.group(0)


def test_the_entry_point_is_reachable_where_the_specification_fixes_it(dockerfile):
    """§4: ``mcuhome/bin/build-environment-entry``, and it is a link.

    Relative, so that the layout survives being relocated — copied out of
    the image, or unpacked at another prefix — which is the case the entry
    point's fallback to its own location exists for.
    """
    match = re.search(r"ln -s (\S+) \\?\n?\s*(/mcuhome/bin/build-environment-entry)", dockerfile)
    assert match, "no link is placed at /mcuhome/bin/build-environment-entry"
    assert not match.group(1).startswith("/"), "the link target is absolute"
    assert match.group(1).endswith("/bin/build-environment-entry")


def test_the_image_states_where_it_put_its_own_packages(dockerfile, assembler):
    """The one thing the entry point cannot work out for itself.

    It takes the tools root from its own location when it has to, but it can
    only *check* the workspace root — and the builder needs it to find the
    source world and the pre-generated Matter code.
    """
    for variable in ("MCUHOME_BUILD_ENV_TOOLS", "MCUHOME_BUILD_ENV_WORKSPACE"):
        match = re.search(rf"\b{variable}=(\S+)", dockerfile)
        assert match, f"the image does not set {variable}"
        root = match.group(1)
        assert f"COPY --from=packages /environment {root.rsplit('/', 1)[0]}" in dockerfile


# --------------------------------------------------------------------------
# §5.1 and §5.2: the declaration, mirrored rather than retyped
# --------------------------------------------------------------------------

DECLARATION = {
    "spec-generation": "3",
    "zephyr.version": "4.4.0",
    "build-context.generator-constraint": "mcuhome-workbench:",
    "packages.mcuhome-build-tools": "1.2.0",
    "packages.mcuhome-build-workspace": "2.4.0",
    "x-experiment": "on",
}


def test_every_member_of_the_declaration_becomes_a_label(assembler):
    """§5.2: "repeats **every** member … with the identical value".

    Including one nobody has defined yet — an environment may add ``x-``
    members freely, and an assembly that mirrored only the members it knows
    would silently drop them.
    """
    produced = dict(entry.split("=", 1) for entry in assembler.labels(DECLARATION))
    assert produced == {
        f"org.mcuhome.build-environment.{member}": value for member, value in DECLARATION.items()
    }


def test_the_labels_are_sorted_by_member_name(assembler):
    """§5.1: two assemblies of one set produce one command line."""
    names = [entry.split("=", 1)[0] for entry in assembler.labels(DECLARATION)]
    assert names == sorted(names, key=lambda name: name.encode("utf-8"))


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("packages.mcuhome-build-workspace", "2.4.0"),
        ("packages.mcuhome-build-workspace", "2.4.0@sha256:" + "7c" * 32),
        ("packages.mcuhome-build-tools_linux-amd64", "1.2.0@sha256:" + "b9" * 32),
    ],
)
def test_a_package_member_parses_with_and_without_a_hash(assembler, member, value):
    parsed = assembler.package_members({**DECLARATION, member: value})
    name = member[len("packages.") :]
    assert parsed[name][0] == value.split("@", 1)[0]
    assert parsed[name][1] == (value.split("@sha256:")[1] if "@" in value else None)


@pytest.mark.parametrize(
    ("member", "value"),
    [
        ("packages.MCUHome", "1.0"),
        ("packages.a_b_c", "1.0"),
        ("packages.a", "1.0@sha256:beef"),
        ("packages.a", "mcuhome-build-tools:1.2.0;mcuhome-build-workspace:2.4.0"),
    ],
)
def test_a_package_member_that_is_not_the_format_is_refused(assembler, member, value):
    with pytest.raises(SystemExit):
        assembler.package_members({member: value})


def test_a_declaration_without_a_package_member_is_refused(assembler):
    """§5 makes at least one ``packages.<name>`` member required."""
    with pytest.raises(SystemExit):
        assembler.package_members({"spec-generation": "3"})


def _archive(path: Path, documents: dict[str, dict]) -> None:
    """A ``.tar.zst`` carrying nothing but the named JSON documents."""
    with (
        open(path, "wb") as handle,
        zstandard.ZstdCompressor().stream_writer(handle) as compressed,
        tarfile.open(fileobj=compressed, mode="w|") as tar,
    ):
        for name, document in documents.items():
            payload = json.dumps(document).encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))


def _tools_archive(path: Path, *, package: str, version: str, arch: str) -> None:
    _archive(
        path,
        {
            "build-tools.json": {
                "package": package,
                "version": version,
                "os": "linux",
                "arch": arch,
            }
        },
    )


@pytest.fixture
def package_set(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "mcuhome-build-workspace-2.4.0.tar.zst"
    tools = tmp_path / "mcuhome-build-tools_linux-amd64-1.2.0.tar.zst"
    _archive(
        workspace,
        {
            "build-environment.json": DECLARATION,
            "build-workspace.json": {"package": "mcuhome-build-workspace", "version": "2.4.0"},
        },
    )
    _tools_archive(tools, package="mcuhome-build-tools_linux-amd64", version="1.2.0", arch="amd64")
    return workspace, tools


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_declaration_is_read_out_of_the_package(assembler, package_set):
    workspace, _tools = package_set
    assert assembler.declaration(workspace) == DECLARATION


def test_a_sidecar_that_disagrees_with_the_archive_is_refused(assembler, package_set):
    """§5: "Both must say the same thing."

    A provisioner reads the sidecar before it unpacks anything and a store
    entry reads the copy inside, so two documents that differ mean the
    environment is whatever the reader happened to open. That is a refusal,
    not a preference — and the message names both places.
    """
    workspace, _tools = package_set
    sidecar = workspace.with_name(f"{workspace.name}.build-environment.json")
    sidecar.write_text(json.dumps({**DECLARATION, "x-experiment": "off"}), encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        assembler.declaration(workspace)
    assert str(sidecar) in str(refused.value)
    assert workspace.name in str(refused.value)


def test_a_sidecar_that_agrees_byte_for_byte_is_accepted(assembler, package_set):
    """The normal case: one document, written twice, identical."""
    workspace, _tools = package_set
    sidecar = workspace.with_name(f"{workspace.name}.build-environment.json")
    sidecar.write_bytes(
        assembler._archive_members(workspace, {"build-environment.json"})["build-environment.json"]
    )
    assert assembler.declaration(workspace) == DECLARATION


def test_a_sidecar_alone_is_a_complete_answer(assembler, tmp_path):
    """An archive that carries no declaration is still described by its sidecar."""
    workspace = tmp_path / "mcuhome-build-workspace-2.4.0.tar.zst"
    _archive(workspace, {"build-workspace.json": {"package": "w", "version": "2.4.0"}})
    sidecar = workspace.with_name(f"{workspace.name}.build-environment.json")
    sidecar.write_text(json.dumps(DECLARATION), encoding="utf-8")
    assert assembler.declaration(workspace) == DECLARATION


def test_neither_copy_present_is_refused(assembler, tmp_path):
    workspace = tmp_path / "mcuhome-build-workspace-2.4.0.tar.zst"
    _archive(workspace, {"build-workspace.json": {"package": "w", "version": "2.4.0"}})
    with pytest.raises(SystemExit):
        assembler.declaration(workspace)


def test_the_image_declares_the_concrete_resolved_set(assembler, package_set):
    """§5.1's second rule: hashes everywhere, and the platform's own package.

    The package's declaration is abstract — no hash on the carrier, the
    tools family in place of a platform. The image is holding both archives,
    so it is the party that completes both, and the members it ends up with
    are the ones an orchestrator matches an image by.
    """
    workspace, tools = package_set
    resolved = assembler.resolve(declared=DECLARATION, workspace=workspace, tools=tools)

    assert resolved["packages.mcuhome-build-workspace"] == f"2.4.0@sha256:{_sha256(workspace)}"
    assert resolved["packages.mcuhome-build-tools_linux-amd64"] == (
        f"1.2.0@sha256:{_sha256(tools)}"
    )
    assert "packages.mcuhome-build-tools" not in resolved, "the family entry is not carried over"
    # Everything that is not a package member survives untouched, ``x-``
    # members included.
    for member in ("spec-generation", "zephyr.version", "x-experiment"):
        assert resolved[member] == DECLARATION[member]


def test_a_declaration_that_already_names_the_platform_is_accepted(assembler, package_set):
    """A concrete member is matched before the family it belongs to."""
    workspace, tools = package_set
    declared = {
        **{k: v for k, v in DECLARATION.items() if k != "packages.mcuhome-build-tools"},
        "packages.mcuhome-build-tools_linux-amd64": "1.2.0",
    }
    resolved = assembler.resolve(declared=declared, workspace=workspace, tools=tools)
    assert resolved["packages.mcuhome-build-tools_linux-amd64"].startswith("1.2.0@sha256:")


def test_an_archive_the_declaration_does_not_name_is_refused(assembler, package_set, tmp_path):
    """§5.2: an image assembled from packages it does not declare is lying."""
    workspace, _tools = package_set
    other = tmp_path / "mcuhome-other-tools_linux-arm64-9.9.9.tar.zst"
    _tools_archive(other, package="mcuhome-other-tools_linux-arm64", version="9.9.9", arch="arm64")
    with pytest.raises(SystemExit):
        assembler.resolve(declared=DECLARATION, workspace=workspace, tools=other)


def test_the_right_family_at_the_wrong_version_is_refused(assembler, package_set, tmp_path):
    """A version that disagrees is as much "not this set" as a missing name."""
    workspace, _tools = package_set
    other = tmp_path / "mcuhome-build-tools_linux-arm64-9.9.9.tar.zst"
    _tools_archive(other, package="mcuhome-build-tools_linux-arm64", version="9.9.9", arch="arm64")
    with pytest.raises(SystemExit):
        assembler.resolve(declared=DECLARATION, workspace=workspace, tools=other)


def test_an_archive_whose_manifest_is_not_a_package_name_is_refused(
    assembler, package_set, tmp_path
):
    """The manifest decides a label's name, so it is held to the grammar too."""
    workspace, _tools = package_set
    other = tmp_path / "tools.tar.zst"
    _tools_archive(other, package="Build_Tools_x86", version="1.2.0", arch="amd64")
    with pytest.raises(SystemExit, match="not a package name"):
        assembler.resolve(declared=DECLARATION, workspace=workspace, tools=other)


def test_a_package_the_image_is_not_assembled_from_is_refused(assembler, package_set):
    """The other direction: a set the image cannot actually deliver."""
    workspace, tools = package_set
    declared = {**DECLARATION, "packages.mcuhome-build-extras": "1.0.0"}
    with pytest.raises(SystemExit):
        assembler.resolve(declared=declared, workspace=workspace, tools=tools)


def test_a_hash_the_declaration_states_is_checked_and_not_overwritten(assembler, package_set):
    """A package that pinned bytes and got other ones is the case pinning is for."""
    workspace, tools = package_set
    declared = {**DECLARATION, "packages.mcuhome-build-workspace": "2.4.0@sha256:" + "00" * 32}
    with pytest.raises(SystemExit):
        assembler.resolve(declared=declared, workspace=workspace, tools=tools)


def test_a_hash_the_declaration_states_correctly_is_kept(assembler, package_set):
    """The other half of the same rule: a matching pin passes and survives.

    "Checked, never overwritten" needs the passing case as much as the
    failing one — a resolve that silently replaced the stated hash with the
    measured one would look identical here and would never refuse anything.
    """
    workspace, tools = package_set
    measured = _sha256(workspace)
    declared = {**DECLARATION, "packages.mcuhome-build-workspace": f"2.4.0@sha256:{measured}"}
    resolved = assembler.resolve(declared=declared, workspace=workspace, tools=tools)
    assert resolved["packages.mcuhome-build-workspace"] == f"2.4.0@sha256:{measured}"


# --------------------------------------------------------------------------
# The tag: derived from the package, never typed
# --------------------------------------------------------------------------


def test_the_tag_follows_the_naming_scheme(assembler, package_set):
    """``<repository>:<workspace package version>-r<n>``."""
    workspace, _tools = package_set
    assert assembler.image_reference(workspace, 1) == "ghcr.io/mcu-home/build-environment:2.4.0-r1"
    assert assembler.image_reference(workspace, 7).endswith(":2.4.0-r7")


def test_the_tag_names_the_version_the_package_states(assembler, tmp_path):
    """The manifest decides, not the file name — a file can be renamed."""
    workspace = tmp_path / "renamed.tar.zst"
    _archive(
        workspace,
        {
            "build-environment.json": DECLARATION,
            "build-workspace.json": {"package": "mcuhome-build-workspace", "version": "9.9.9"},
        },
    )
    assert assembler.image_reference(workspace, 1).endswith(":9.9.9-r1")


def test_a_package_without_a_version_gets_no_tag(assembler, tmp_path):
    workspace = tmp_path / "mcuhome-build-workspace-2.4.0.tar.zst"
    _archive(workspace, {"build-workspace.json": {"package": "mcuhome-build-workspace"}})
    with pytest.raises(SystemExit):
        assembler.image_reference(workspace, 1)
