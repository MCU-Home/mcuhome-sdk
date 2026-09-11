# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The record of the west workspace the build environment carries.

``packaging/build-environment/workspace-record.py`` writes it once per
workspace package, inside the packager image, after ``west update`` and
the patch set. What is tested here is the document it produces: the layer
names it uses, the resolved commit and patch digest it records, and the
two failure modes that would otherwise be silent — a layer west does not
know, and the SDK layer, which is deliberately absent from the package.

**Docker never runs here.** west is driven through a stub on ``PATH``, so
the suite stays the fast half of the strategy: no image, no clone, no
network.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORD_SCRIPT = REPO_ROOT / "packaging" / "build-environment" / "workspace-record.py"


def _record_module():
    """``workspace-record.py`` as a module — its name has a hyphen in it."""
    spec = importlib.util.spec_from_file_location("workspace_record", RECORD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# What the record says
# --------------------------------------------------------------------------


def _layer_name_grammar() -> re.Pattern[str]:
    """The layer-name pattern, read out of the spec itself (§9).

    Hardcoding the grammar here would defeat the point of this suite: a
    spec change to the pattern must fail this test rather than go
    unnoticed. Failing to find the pattern at all is the same kind of
    drift and gets its own clear message.
    """
    spec = REPO_ROOT / "docs" / "spec" / "build-context-format.md"
    text = spec.read_text(encoding="utf-8")
    found = re.search(r"Lowercase, `(\[a-z\]\[a-z0-9_-\]\*)`", text)
    assert found is not None, (
        f"could not find the §9 layer-name pattern in {spec} — "
        "has the grammar wording moved or changed?"
    )
    return re.compile(f"^{found.group(1)}$")


def test_the_recorded_layer_names_are_valid_layer_names() -> None:
    """Layer names must match the grammar the spec fixes (§9).

    There is deliberately no closed registry any more — a layer is
    "whatever the environment knows" — but the spelling is still
    constrained (docs/spec/build-context-format.md §9).
    """
    module = _record_module()
    layer_name = _layer_name_grammar()
    names = set(module.LAYER_PROJECTS) | {module.SDK_LAYER}
    assert names, "expected at least one recorded layer name"
    for name in names:
        assert layer_name.match(name), f"{name!r} is not a valid layer name"


def test_the_record_names_the_resolved_commit_and_the_patch_digest(tmp_path, monkeypatch) -> None:
    """A digest says "the same"; it never says "what".

    Two revisions in ``west.yml`` are movable tags, so a rebuild of the
    same Dockerfile can produce a different workspace. The 40-character
    commit per layer is what makes that checkable. Patches are identified
    by SHA-256 because ``patches/README.md`` prescribes regenerating one
    in place under its own name — the name is not an identity.
    """
    module = _record_module()
    topdir = tmp_path / "workspace"
    (topdir / ".west").mkdir(parents=True)
    config = topdir / ".west" / "config"
    config.write_text("[manifest]\npath = mcuhome\nfile = west.yml\n", "utf-8")
    patch = tmp_path / "zephyr-v4.4.0-nrf53-spinel-stack.patch"
    patch.write_bytes(b"--- a\n+++ b\n")

    monkeypatch.setattr(
        module,
        "_west_projects",
        lambda _topdir: {
            "zephyr": {"path": str(topdir / "zephyr"), "revision": "v4.4.0"},
            "connectedhomeip": {
                "path": str(topdir / "modules/lib/connectedhomeip"),
                "revision": "v1.5.1.0",
            },
            "mcuboot": {"path": str(topdir / "bootloader/mcuboot"), "revision": "a" * 40},
        },
    )
    monkeypatch.setattr(module, "_git", lambda _repository, *_arguments: "b" * 40)

    record = module.build_record(topdir, "narrow-depth-1", [("zephyr", patch)])
    # Serialisable, because a JSON document is what the image carries.
    assert json.loads(json.dumps(record)) == record

    assert record["topdir"] == str(topdir)
    assert record["manifest"] == {"path": "mcuhome", "file": "west.yml"}
    assert record["layers"]["zephyr"]["commit"] == "b" * 40
    assert record["layers"]["zephyr"]["revision"] == "v4.4.0"
    assert record["layers"]["zephyr"]["patches"] == [
        {
            "file": "zephyr-v4.4.0-nrf53-spinel-stack.patch",
            # sha256 of the two lines written above, not of the name.
            "sha256": "6e53bf2ad8f234c60294a05a013874a211fe3c03653f48df43ac4ec085ab9a24",
        }
    ]
    # A layer nobody patched says so, rather than saying nothing.
    assert record["layers"]["mcuboot"]["patches"] == []


def test_the_sdk_layer_is_recorded_as_absent(tmp_path, monkeypatch) -> None:
    """It is a hash-pinned package fetched per build.

    So it is a tree the image knows the location of and not the content
    of — which the contract already models as a ``trees`` entry without a
    version. Recording it as a normal layer would be a claim about bytes
    the image does not have.
    """
    module = _record_module()
    topdir = tmp_path / "workspace"
    (topdir / ".west").mkdir(parents=True)
    (topdir / ".west" / "config").write_text("[manifest]\npath = mcuhome\n", "utf-8")
    monkeypatch.setattr(
        module,
        "_west_projects",
        lambda _topdir: {
            project: {"path": str(topdir / project), "revision": "x"}
            for project in module.LAYER_PROJECTS.values()
        },
    )
    monkeypatch.setattr(module, "_git", lambda _repository, *_arguments: "c" * 40)

    layer = module.build_record(topdir, "narrow-depth-1", [])["layers"][module.SDK_LAYER]
    assert layer == {"path": str(topdir / "mcuhome"), "mounted": True}
    assert "revision" not in layer


def test_a_layer_west_does_not_know_is_a_failure_not_an_omission(tmp_path, monkeypatch) -> None:
    """A silently missing layer is an image that lies about itself."""
    module = _record_module()
    topdir = tmp_path / "workspace"
    (topdir / ".west").mkdir(parents=True)
    (topdir / ".west" / "config").write_text("[manifest]\npath = mcuhome\n", "utf-8")
    monkeypatch.setattr(module, "_west_projects", lambda _topdir: {"some-other-project": {}})

    with pytest.raises(SystemExit):
        module.build_record(topdir, "narrow-depth-1", [])
