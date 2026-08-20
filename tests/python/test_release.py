# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``scripts/release.py``: what it changes, and everything it refuses.

A release script is judged by its refusals. Everything it does is
reversible until the push — but the mistakes it is there to prevent are
not: a published version is immutable and eternal, so a wrong number or a
tag that disagrees with the commit becomes permanent the moment the
package host records it.

The one that is easy to miss and expensive to hit is the last: the SDK
archive is named after ``__version__`` *as the tagged commit carries it*,
never after the tag, so tagging an unbumped commit produces a package
whose name contradicts the release it hangs on. ``--check-tag`` is that
check with no dependencies, and CI runs it before building anything.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release.py"


@pytest.fixture(scope="module")
def release():
    """``release.py`` as a module — ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("release", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before executing: the script's dataclass has a
    # default_factory field and `from __future__ import annotations`, and
    # resolving that pair sends dataclasses back through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def git(root: Path, *arguments: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


CHANGELOG = """# Changelog

## [Unreleased]

### Added

- A thing worth releasing.

## [0.0.9] - 2026-01-01

- The one before.
"""


def make_repo(tmp_path: Path, *, version: str = "0.1.0", two_files: bool = False) -> Path:
    """A minimal repository shaped like ours, with an origin to compare against."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text(f'"""A package."""\n\n__version__ = "{version}"\n')
    files = ["pkg/__init__.py"] + (["pyproject.toml"] if two_files else [])
    project = "[project]\nname = 'x'\n"
    if two_files:
        project += f'version = "{version}"\n'
    project += (
        "\n[tool.mcuhome-release]\n"
        f"version_files = {files!r}\n"
        'changelog = "CHANGELOG.md"\n'
        "gates = []\n"
        'next_steps = ["push {tag}"]\n'
    )
    (root / "pyproject.toml").write_text(project)
    (root / "CHANGELOG.md").write_text(CHANGELOG)

    git(root.parent, "init", "--quiet", "--initial-branch=main", str(root))
    git(root, "config", "user.email", "test@example.org")
    git(root, "config", "user.name", "Test")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "initial")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], check=True)
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "--quiet", "origin", "main")
    return root


def test_a_release_commits_and_tags_but_never_pushes(release, tmp_path, capsys):
    root = make_repo(tmp_path)
    assert release.main(["0.2.0", "--repo", str(root)]) == 0

    assert release.declared_version(root / "pkg" / "__init__.py") == "0.2.0"
    assert f"## [0.2.0] - {date.today().isoformat()}" in (root / "CHANGELOG.md").read_text()
    assert git(root, "tag", "--list") == "v0.2.0"
    assert "chore(release): 0.2.0" in git(root, "log", "-1", "--pretty=%s")
    assert "Signed-off-by:" in git(root, "log", "-1", "--pretty=%b")
    # The push is where a release stops being reversible, so it stays a
    # decision somebody makes.
    assert git(root, "rev-parse", "origin/main") != git(root, "rev-parse", "HEAD")
    assert "push v0.2.0" in capsys.readouterr().out


def test_the_unreleased_section_keeps_its_place(release, tmp_path):
    root = make_repo(tmp_path)
    release.main(["0.2.0", "--repo", str(root)])
    text = (root / "CHANGELOG.md").read_text()
    assert text.index("## [Unreleased]") < text.index("## [0.2.0]") < text.index("## [0.0.9]")
    assert "A thing worth releasing." in text.split("## [0.2.0]")[1]
    # Emptied, not removed: the next change has somewhere to go.
    assert text.split("## [Unreleased]")[1].split("## [")[0].strip() == ""


def test_a_dry_run_leaves_nothing_behind(release, tmp_path):
    root = make_repo(tmp_path)
    before = (root / "pkg" / "__init__.py").read_text()
    assert release.main(["0.2.0", "--repo", str(root), "--dry-run"]) == 0
    assert (root / "pkg" / "__init__.py").read_text() == before
    assert git(root, "tag", "--list") == ""
    assert git(root, "status", "--porcelain") == ""


def test_every_version_file_is_bumped_together(release, tmp_path):
    root = make_repo(tmp_path, two_files=True)
    release.main(["0.2.0", "--repo", str(root)])
    assert release.declared_version(root / "pyproject.toml") == "0.2.0"
    assert release.declared_version(root / "pkg" / "__init__.py") == "0.2.0"


def test_version_files_that_disagree_are_refused(release, tmp_path):
    root = make_repo(tmp_path, two_files=True)
    (root / "pyproject.toml").write_text(
        (root / "pyproject.toml").read_text().replace('version = "0.1.0"', 'version = "0.0.5"', 1)
    )
    git(root, "commit", "--quiet", "-am", "drift")
    git(root, "push", "--quiet", "origin", "main")
    with pytest.raises(SystemExit, match="they must agree first"):
        release.main(["0.2.0", "--repo", str(root)])


@pytest.mark.parametrize(
    ("version", "reason"),
    [("0.0.1", "comes before"), ("nope", "not a PEP 440")],
)
def test_a_version_never_moves_backwards(release, tmp_path, version, reason):
    root = make_repo(tmp_path)
    with pytest.raises(SystemExit, match=reason):
        release.main([version, "--repo", str(root)])


def test_the_declared_version_may_be_released_as_it_stands(release, tmp_path):
    """The first release of all: the number exists in the tree, nowhere else.

    Refusing it would make a repository unable to release the version it
    already declares. What must never happen twice is *publishing* one —
    the tag check here and the duplicate refusal at the package host.
    """
    root = make_repo(tmp_path, version="0.1.0.dev0")
    assert release.main(["0.1.0.dev0", "--repo", str(root)]) == 0
    assert git(root, "tag", "--list") == "v0.1.0.dev0"
    assert "## [0.1.0.dev0] - " in (root / "CHANGELOG.md").read_text()

    git(root, "push", "--quiet", "origin", "main")
    with pytest.raises(SystemExit, match="never replaced"):
        release.main(["0.1.0.dev0", "--repo", str(root)])


def test_a_dirty_tree_is_refused(release, tmp_path):
    root = make_repo(tmp_path)
    (root / "stray.txt").write_text("uncommitted\n")
    with pytest.raises(SystemExit, match="uncommitted changes"):
        release.main(["0.2.0", "--repo", str(root)])


def test_an_existing_tag_is_refused(release, tmp_path):
    root = make_repo(tmp_path)
    git(root, "tag", "v0.2.0")
    with pytest.raises(SystemExit, match="never replaced"):
        release.main(["0.2.0", "--repo", str(root)])


def test_a_branch_ahead_of_origin_is_refused(release, tmp_path):
    root = make_repo(tmp_path)
    (root / "later.txt").write_text("unpushed\n")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "unpushed")
    with pytest.raises(SystemExit, match="differ"):
        release.main(["0.2.0", "--repo", str(root)])


def test_an_empty_changelog_section_is_refused(release, tmp_path):
    root = make_repo(tmp_path)
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\n## [0.0.9] - 2026-01\n")
    git(root, "commit", "--quiet", "-am", "empty changelog")
    git(root, "push", "--quiet", "origin", "main")
    with pytest.raises(SystemExit, match="Unreleased section is empty"):
        release.main(["0.2.0", "--repo", str(root)])


def test_a_failing_gate_stops_the_release(release, tmp_path):
    root = make_repo(tmp_path)
    (root / "pyproject.toml").write_text(
        (root / "pyproject.toml").read_text().replace("gates = []", 'gates = ["false"]')
    )
    git(root, "commit", "--quiet", "-am", "add a failing gate")
    git(root, "push", "--quiet", "origin", "main")
    with pytest.raises(SystemExit, match="A gate failed"):
        release.main(["0.2.0", "--repo", str(root)])


def test_check_tag_is_the_guard_ci_runs(release, tmp_path, capsys):
    root = make_repo(tmp_path)
    assert release.main(["--check-tag", "v0.1.0", "--repo", str(root)]) == 0
    assert release.main(["--check-tag", "v0.2.0", "--repo", str(root)]) == 1
    # The reason has to name both numbers: the whole failure mode is that
    # they silently differ.
    assert "0.1.0" in capsys.readouterr().err


def test_check_tag_needs_no_third_party_import(release):
    """CI runs it in a job that installs only the compressor."""
    source = SCRIPT.read_text()
    top_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not any("packaging" in line for line in top_level), (
        "packaging must stay a function-level import so --check-tag runs on a bare python"
    )


def test_this_repository_declares_a_release_block(release):
    """The config is what makes the script repository-agnostic — ours must exist."""
    config = release.load_config(REPO_ROOT)
    assert config.version_files == [REPO_ROOT / "mcuhome" / "model" / "__init__.py"]
    assert config.tag_prefix == "v"
    assert config.gates, "a release without gates is not a release"
