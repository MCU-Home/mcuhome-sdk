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

import hashlib
import importlib.util
import json
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


def make_repo(
    tmp_path: Path, *, version: str = "0.1.0", two_files: bool = False, changelog: bool = True
) -> Path:
    """A minimal repository shaped like ours, with an origin to compare against.

    ``changelog=False`` shapes a repository the way ours looks before
    1.0.0: no ``changelog`` key in ``[tool.mcuhome-release]`` and no
    ``CHANGELOG.md`` on disk at all.
    """
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
        + ('changelog = "CHANGELOG.md"\n' if changelog else "")
        + "gates = []\n"
        'next_steps = ["push {tag}"]\n'
    )
    (root / "pyproject.toml").write_text(project)
    if changelog:
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


def test_a_missing_changelog_is_refused(release, tmp_path):
    """Once a repository names a changelog, the file has to be there.

    What must not happen is a release that writes the version into every
    source, commits and tags, and only then discovers it has nowhere to
    record what changed. So the refusal comes before anything is written.
    """
    root = make_repo(tmp_path)
    (root / "CHANGELOG.md").unlink()
    git(root, "commit", "--quiet", "-am", "no changelog")
    git(root, "push", "--quiet", "origin", "main")
    with pytest.raises(SystemExit, match="is missing"):
        release.main(["0.2.0", "--repo", str(root)])


def test_no_changelog_key_releases_without_a_changelog_step(release, tmp_path):
    """A repository that keeps no changelog at all — several here do not,
    on purpose, while the format still changes weekly — omits the
    ``changelog`` key entirely, and the release proceeds with no
    changelog file read, written or required.
    """
    root = make_repo(tmp_path, changelog=False)
    assert not (root / "CHANGELOG.md").exists()

    assert release.main(["0.2.0", "--repo", str(root)]) == 0

    assert release.declared_version(root / "pkg" / "__init__.py") == "0.2.0"
    assert not (root / "CHANGELOG.md").exists()
    assert git(root, "tag", "--list") == "v0.2.0"
    committed = git(root, "show", "--stat", "--pretty=format:", "HEAD")
    assert "CHANGELOG.md" not in committed


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


# --------------------------------------------------------------------------
# scripts/release_readiness.py: what a commit can say about releasing
# --------------------------------------------------------------------------
#
# The module answers about the *published* world, and published means a
# GitHub release of this repository. Every test below hands it that world
# as a list and its meta documents as files, which is what the two
# injection points (`--releases`, `--metas`) exist for: no test here opens
# a socket, and the answers are therefore the same on a bench as on a
# runner.

READINESS_SCRIPT = REPO_ROOT / "scripts" / "release_readiness.py"


@pytest.fixture(scope="module")
def readiness():
    """``release_readiness.py`` as a module — ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("release_readiness", READINESS_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def declared(readiness):
    """The three versions this checkout's HEAD declares."""
    import release_lines

    document = release_lines.environment(REPO_ROOT, "HEAD")
    return {stage: release_lines.version_of(document, stage) for stage in release_lines.STAGES}


def meta_document(*, name, version, architecture=None, requires=None, inputs="0" * 64):
    document = {
        "schema": 1,
        "package": {"name": name, "version": version, "architecture": architecture},
        "inputs_sha256": inputs,
        "contents": {},
    }
    if requires is not None:
        document["requires"] = requires
    return document


def write_meta(root: Path, tag: str, file_name: str, document: dict) -> Path:
    directory = root / tag
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / file_name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def release_entry(tag: str, *assets: str, draft: bool = False) -> dict:
    return {"tag_name": tag, "draft": draft, "assets": list(assets)}


def test_published_versions_are_read_off_the_tags(readiness):
    """One prefix per line, and a draft is not published."""
    releases = [
        release_entry("v0.1.10.dev2", "mcuhome-sdk-0.1.10.dev2.tar.zst"),
        release_entry("v0.1.9"),
        release_entry("workspace-v0.2.0", "x.tar.zst.meta.json"),
        release_entry("workspace-v0.1.0", "x.tar.zst.meta.json"),
        release_entry("tools-v0.1.0", draft=True),
        release_entry("something-else"),
        release_entry("vnot-a-version"),
    ]
    assert [one.version for one in readiness.published_of("sdk", releases)] == [
        "0.1.9",
        "0.1.10.dev2",
    ]
    assert [one.version for one in readiness.published_of("workspace", releases)] == [
        "0.1.0",
        "0.2.0",
    ]
    assert readiness.published_of("tools", releases) == []


def test_a_version_without_a_meta_file_is_no_candidate(readiness):
    """The workbench's rule, and therefore this module's."""
    with_meta, without = readiness.published_of(
        "workspace",
        [
            release_entry("workspace-v0.1.0", "a.tar.zst", "a.tar.zst.meta.json"),
            release_entry("workspace-v0.2.0", "b.tar.zst"),
        ],
    )
    assert with_meta.has_meta
    assert not without.has_meta


def test_nothing_published_means_every_version_is_free(readiness, capsys):
    """Today's answer, and it has to be said rather than left silent."""
    assert (
        readiness.check_versions(
            revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "not published yet" in printed
    for stage in ("sdk", "workspace", "tools"):
        assert stage in printed


def test_a_published_version_keeps_its_inputs(readiness, declared, tmp_path, capsys):
    """The hash the release states is this commit's — nothing to bump."""
    import release_lines

    tag = f"workspace-v{declared['workspace']}"
    write_meta(
        tmp_path,
        tag,
        "workspace.tar.zst.meta.json",
        meta_document(
            name="mcuhome-build-workspace",
            version=declared["workspace"],
            inputs=release_lines.inputs_sha256("workspace", "HEAD", repository=REPO_ROOT),
        ),
    )
    status = readiness.check_versions(
        revision="HEAD",
        releases=[release_entry(tag, "workspace.tar.zst", "workspace.tar.zst.meta.json")],
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    )
    assert status == 0
    assert "this commit's inputs are its own" in capsys.readouterr().out


def test_changed_inputs_under_a_published_version_are_a_blocker(
    readiness, declared, tmp_path, capsys
):
    """The whole point of the check: the refusal names both hashes and the fix."""
    tag = f"workspace-v{declared['workspace']}"
    write_meta(
        tmp_path,
        tag,
        "workspace.tar.zst.meta.json",
        meta_document(
            name="mcuhome-build-workspace", version=declared["workspace"], inputs="a" * 64
        ),
    )
    status = readiness.check_versions(
        revision="HEAD",
        releases=[release_entry(tag, "workspace.tar.zst", "workspace.tar.zst.meta.json")],
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    )
    assert status == 1
    captured = capsys.readouterr()
    assert "a" * 64 in captured.err
    assert "bump workspace.version" in captured.err
    assert "packaging/build-environment/environment.json" in captured.err
    # The tag is not in this checkout, so the listing diff says so instead
    # of inventing one.
    assert "cannot be compared here" in captured.err


def test_a_published_version_without_a_meta_file_is_a_blocker(readiness, declared, capsys):
    """A version that cannot be compared is a version that cannot be reused."""
    tag = f"v{declared['sdk']}"
    status = readiness.check_versions(
        revision="HEAD",
        releases=[release_entry(tag, "mcuhome-sdk.tar.zst")],
        metas=lambda entry: [],
        repository=REPO_ROOT,
    )
    assert status == 1
    assert "no .meta.json asset" in capsys.readouterr().err


def test_the_tools_line_is_checked_per_architecture(readiness, declared, tmp_path, capsys):
    """Its inputs carry the architecture, so one hash per platform is compared."""
    import release_lines

    tag = f"tools-v{declared['tools']}"
    for platform in readiness.ARCHITECTURES:
        write_meta(
            tmp_path,
            tag,
            f"mcuhome-build-tools_{platform}.tar.zst.meta.json",
            meta_document(
                name="mcuhome-build-tools",
                version=declared["tools"],
                architecture=platform,
                inputs=release_lines.inputs_sha256(
                    "tools", "HEAD", repository=REPO_ROOT, architecture=platform
                ),
            ),
        )
    status = readiness.check_versions(
        revision="HEAD",
        releases=[release_entry(tag, "a.tar.zst", "a.tar.zst.meta.json")],
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    )
    assert status == 0
    printed = capsys.readouterr().out
    for platform in readiness.ARCHITECTURES:
        assert platform in printed


def test_with_nothing_published_the_plan_is_one_combination(readiness, declared):
    """Every line can only be tried against this commit's own next stage."""
    plan = readiness.build_plan(
        revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
    ).document()
    assert [one["id"] for one in plan["combinations"]] == ["checkout"]
    combination = plan["combinations"][0]
    for stage in ("sdk", "workspace", "tools"):
        assert combination[stage] == {"source": "checkout", "version": declared[stage]}
        assert plan["catalogue"][stage], f"{stage} has nothing to try"
        assert all(row["combination"] == "checkout" for row in plan["catalogue"][stage])
    assert "blocked until a build workspace" in plan["verdicts"]["sdk"]
    assert "nothing for" in plan["verdicts"]["workspace"]
    assert "nothing for" in plan["verdicts"]["tools"]


def test_a_published_workspace_is_what_an_sdk_release_promises(readiness, declared, tmp_path):
    """The ends of the declared range, and the tools each of them requires."""
    for version in ("0.1.0", "0.1.4", "0.1.7"):
        write_meta(
            tmp_path,
            f"workspace-v{version}",
            "w.tar.zst.meta.json",
            meta_document(
                name="mcuhome-build-workspace",
                version=version,
                requires={"mcuhome-build-tools": "~=0.1.0"},
            ),
        )
    releases = [
        release_entry(f"workspace-v{version}", "w.tar.zst", "w.tar.zst.meta.json")
        for version in ("0.1.0", "0.1.4", "0.1.7")
    ]
    plan = readiness.build_plan(
        revision="HEAD",
        releases=releases,
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    ).document()
    rows = plan["catalogue"]["sdk"]
    subjects = [row["subject"] for row in rows]
    assert "mcuhome-build-workspace 0.1.0" in subjects
    assert "mcuhome-build-workspace 0.1.7" in subjects
    assert "0.1.4" not in " ".join(subjects), "only the ends of the range are promised"
    # The declared workspace version is published here, so it is not tried
    # a second time as "this commit's".
    tried = {row["combination"] for row in rows}
    assert len(tried) == 2
    assert "satisfy" in plan["verdicts"]["sdk"]


def test_a_published_sdk_that_accepts_the_workspace_means_a_patch(readiness, declared, tmp_path):
    """The workspace line looking up: what would reach a user without a re-cut."""
    tag = "v0.1.9"
    write_meta(
        tmp_path,
        tag,
        "sdk.tar.zst.meta.json",
        meta_document(
            name="mcuhome-sdk",
            version="0.1.9",
            requires={"mcuhome-build-workspace": "~=0.1.0"},
        ),
    )
    plan = readiness.build_plan(
        revision="HEAD",
        releases=[release_entry(tag, "sdk.tar.zst", "sdk.tar.zst.meta.json")],
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    ).document()
    assert plan["verdicts"]["workspace"].startswith("Patch release possible")
    labels = {one["id"]: one for one in plan["combinations"]}
    published = [
        one
        for one in labels.values()
        if one["sdk"] == {"source": "published", "version": "0.1.9", "tag": tag}
    ]
    assert published, "the published SDK has to be tried against this workspace"
    assert published[0]["workspace"]["source"] == "checkout"


def test_a_published_sdk_that_refuses_the_workspace_means_a_minor(readiness, tmp_path):
    """No published constraint includes it, but this commit's does."""
    tag = "v0.1.9"
    write_meta(
        tmp_path,
        tag,
        "sdk.tar.zst.meta.json",
        meta_document(
            name="mcuhome-sdk",
            version="0.1.9",
            requires={"mcuhome-build-workspace": "~=9.9.0"},
        ),
    )
    plan = readiness.build_plan(
        revision="HEAD",
        releases=[release_entry(tag, "sdk.tar.zst", "sdk.tar.zst.meta.json")],
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    ).document()
    assert plan["verdicts"]["workspace"].startswith("Minor needed above")


def test_a_host_prefixed_requirement_is_still_a_requirement(readiness):
    """`requires` keys may name the host; the host is not part of the name."""
    assert (
        readiness.constraint_on(
            {"packages.example.test/mcuhome-build-tools": "~=0.1.0"}, "mcuhome-build-tools"
        )
        == "~=0.1.0"
    )
    assert readiness.constraint_on({"other": "~=1.0"}, "mcuhome-build-tools") is None
    assert not readiness.accepts(None, "0.1.0")
    assert not readiness.accepts("", "0.1.0")
    assert readiness.accepts("~=0.1.0", "0.1.0+ci.abc1234"), (
        "a per-commit build has to satisfy the constraint its release would"
    )


def test_the_summary_says_what_was_not_evaluated(readiness, tmp_path, capsys):
    """A gate that did not open is not a failure, and must not read as one."""
    plan = readiness.build_plan(
        revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
    ).document()
    assert readiness.summarize(stage="sdk", plan=plan, results=None) == 0
    assert "not evaluated in this run" in capsys.readouterr().out


def test_the_summary_reports_a_green_combination(readiness, tmp_path, capsys):
    plan = readiness.build_plan(
        revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
    ).document()
    (tmp_path / "result-checkout-amd64.json").write_text(
        json.dumps({"combination": "checkout", "architecture": "amd64", "outcome": "success"})
    )
    assert readiness.summarize(stage="workspace", plan=plan, results=tmp_path) == 0
    assert "amd64: success" in capsys.readouterr().out


def test_a_claimed_combination_that_failed_fails_the_job(readiness, tmp_path, capsys):
    plan = readiness.build_plan(
        revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
    ).document()
    (tmp_path / "result-checkout-arm64.json").write_text(
        json.dumps({"combination": "checkout", "architecture": "arm64", "outcome": "failure"})
    )
    assert readiness.summarize(stage="tools", plan=plan, results=tmp_path) == 1
    captured = capsys.readouterr()
    assert "did not build" in captured.err
    assert "**Failed:**" in captured.out


def package_files(directory: Path, *, name: str, version: str, architecture=None) -> Path:
    """One archive with the two sidecars a release publishes beside it."""
    full = f"{name}_{architecture}" if architecture else name
    archive = directory / f"{full}-{version}.tar.zst"
    archive.write_bytes(b"not really an archive, and nothing here unpacks it")
    (directory / f"{archive.name}.meta.json").write_text(
        json.dumps(meta_document(name=name, version=version, architecture=architecture))
    )
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (directory / f"{archive.name}.sha256").write_text(f"{digest}  {archive.name}\n")
    return archive


def test_release_assets_become_a_package_source(readiness, tmp_path):
    """The index a release page does not carry, written out of the files."""
    package_files(tmp_path, name="mcuhome-build-workspace", version="0.1.0")
    package_files(tmp_path, name="mcuhome-build-tools", version="0.1.0", architecture="linux-amd64")
    index = readiness.write_index(tmp_path)
    assert set(index["packages"]) == {
        "mcuhome-build-workspace",
        "mcuhome-build-tools_linux-amd64",
    }
    entry = index["packages"]["mcuhome-build-tools_linux-amd64"]["0.1.0"]
    assert entry["file"] == "mcuhome-build-tools_linux-amd64-0.1.0.tar.zst"
    assert entry["meta_file"]["file"] == f"{entry['file']}.meta.json"
    assert entry["size"] == (tmp_path / entry["file"]).stat().st_size
    assert json.loads((tmp_path / "index.json").read_text()) == index


def test_a_meta_file_that_describes_another_package_is_refused(readiness, tmp_path):
    archive = package_files(tmp_path, name="mcuhome-build-workspace", version="0.1.0")
    (tmp_path / f"{archive.name}.meta.json").write_text(
        json.dumps(meta_document(name="mcuhome-build-workspace", version="0.2.0"))
    )
    with pytest.raises(SystemExit) as refusal:
        readiness.write_index(tmp_path)
    assert "0.2.0" in str(refusal.value)


def test_a_checksum_that_disagrees_is_refused(readiness, tmp_path):
    archive = package_files(tmp_path, name="mcuhome-build-workspace", version="0.1.0")
    (tmp_path / f"{archive.name}.sha256").write_text(f"{'b' * 64}  {archive.name}\n")
    with pytest.raises(SystemExit) as refusal:
        readiness.write_index(tmp_path)
    assert "b" * 64 in str(refusal.value)
