# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The release act, and what a commit can already say about releasing.

``scripts/release.py`` answers about a *tag*: which of the three lines it
releases, whether the version it names is the one the commit declares, and
exactly which files that release publishes. ``scripts/release_readiness.py``
answers about the *published world* — which versions exist, which satisfy
what, and which combinations therefore have to be built.

Both are judged by their refusals. Everything around a release is
reversible until the tag is pushed; what the refusals prevent is not, because
a published version is immutable and eternal. The one that is easy to miss
and expensive to hit: the archives are named after the version the tagged
**commit** declares, never after the tag, so tagging an unbumped commit
would publish bytes under a number that already means something else.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def load(name: str):
    """One of ``scripts/`` as a module — that directory is not a package."""
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered before executing: a dataclass with a default_factory field
    # under `from __future__ import annotations` sends dataclasses back
    # through sys.modules while the module is still being executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def release():
    return load("release")


def git(root: Path, *arguments: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


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
    # And it says WHICH input moved — out of the tagged commit's own
    # listing where this checkout has that tag, and by saying it cannot
    # compare where it does not. Either answer is right and which one comes
    # out is a property of the clone, not of the check: a full clone has
    # the tag, a shallow CI checkout may not.
    assert "What changed since" in captured.err or "cannot be compared here" in captured.err


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
    platforms = ("linux-amd64", "linux-arm64")
    for platform in platforms:
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
    for platform in platforms:
        assert platform in printed


def test_with_nothing_published_the_plan_is_one_combination(readiness, declared):
    """Every line can only be tried against this commit's own neighbour."""
    plan = readiness.build_plan(
        revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
    ).document()
    assert [one["id"] for one in plan["combinations"]] == ["checkout"]
    combination = plan["combinations"][0]
    assert combination["required"] is True
    for stage in ("sdk", "workspace", "tools"):
        assert combination[stage] == {"source": "checkout", "version": declared[stage]}
    # Both directions where both exist, one where only one does.
    assert set(plan["catalogue"]["sdk"]) == {"down"}
    assert set(plan["catalogue"]["workspace"]) == {"down", "up"}
    assert set(plan["catalogue"]["tools"]) == {"up"}
    for stage, directions in plan["catalogue"].items():
        for direction, rows in directions.items():
            assert rows, f"{stage}/{direction} has nothing to try"
            assert all(row["combination"] == "checkout" for row in rows)
    assert "blocked until a mcuhome-build-workspace" in plan["verdicts"]["sdk"]["down"]
    assert "blocked until a mcuhome-build-tools" in plan["verdicts"]["workspace"]["down"]
    assert "nothing for" in plan["verdicts"]["workspace"]["up"]
    assert "nothing for" in plan["verdicts"]["tools"]["up"]


def test_the_workspace_is_held_against_the_tools_it_requires(readiness, declared):
    """The blocker this file exists for: the workspace has a "down" half too.

    It is what says which of the three lines has to be released first —
    with nothing published, the tools are the only line whose release is
    not blocked on something below it.
    """
    plan = readiness.build_plan(
        revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
    ).document()
    down = plan["catalogue"]["workspace"]["down"]
    assert [row["subject"] for row in down] == [
        f"mcuhome-build-tools {declared['tools']} (this commit)"
    ]
    assert plan["verdicts"]["workspace"]["down"] == (
        "mcuhome-build-workspace release blocked until a mcuhome-build-tools satisfying "
        "'~=0.1.0' is published."
    )
    # The tools line has no stage below it and therefore no such verdict.
    assert "down" not in plan["catalogue"]["tools"]


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
    rows = plan["catalogue"]["sdk"]["down"]
    subjects = [row["subject"] for row in rows]
    assert "mcuhome-build-workspace 0.1.0" in subjects
    assert "mcuhome-build-workspace 0.1.7" in subjects
    assert "0.1.4" not in " ".join(subjects), "only the ends of the range are promised"
    # The declared workspace version is published here, so it is not tried
    # a second time as "this commit's".
    assert len({row["combination"] for row in rows}) == 2
    assert "satisfy" in plan["verdicts"]["sdk"]["down"]


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
    assert plan["verdicts"]["workspace"]["up"].startswith("Patch release possible")
    published = [
        one
        for one in plan["combinations"]
        if one["sdk"] == {"source": "published", "version": "0.1.9", "tag": tag}
    ]
    assert published, "the published SDK has to be tried against this workspace"
    assert published[0]["workspace"]["source"] == "checkout"
    assert published[0]["required"] is True


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
    assert plan["verdicts"]["workspace"]["up"].startswith("Minor needed above")


def full_inventory(root: Path, declared: dict) -> list[dict]:
    """Four workspaces, two tools packages and two SDKs, all with meta files.

    The shape the catalogue is actually about: ranges with an inside and
    two ends, one stage that only some of the versions above accept, and a
    published version of every line — so each of the six halves has
    something to say.
    """
    releases = []
    for version, requires in (
        ("0.1.0", "~=0.1.0"),
        ("0.1.3", "~=0.1.0"),
        ("0.1.9", "~=0.1.0"),
        ("0.2.0", "~=0.2.0"),
    ):
        tag = f"workspace-v{version}"
        write_meta(
            root,
            tag,
            "w.tar.zst.meta.json",
            meta_document(
                name="mcuhome-build-workspace",
                version=version,
                requires={"mcuhome-build-tools": requires},
            ),
        )
        releases.append(release_entry(tag, "w.tar.zst", "w.tar.zst.meta.json"))
    for version in ("0.1.0", "0.1.5"):
        tag = f"tools-v{version}"
        for platform in ("linux-amd64", "linux-arm64"):
            write_meta(
                root,
                tag,
                f"t_{platform}.tar.zst.meta.json",
                meta_document(name="mcuhome-build-tools", version=version, architecture=platform),
            )
        releases.append(release_entry(tag, "t.tar.zst", "t.tar.zst.meta.json"))
    for version, requires in (("0.1.9", "~=0.1.0"), ("0.2.0", "~=0.2.0")):
        tag = f"v{version}"
        write_meta(
            root,
            tag,
            "sdk.tar.zst.meta.json",
            meta_document(
                name="mcuhome-sdk",
                version=version,
                requires={"mcuhome-build-workspace": requires},
            ),
        )
        releases.append(release_entry(tag, "sdk.tar.zst", "sdk.tar.zst.meta.json"))
    return releases


def test_the_whole_catalogue_over_a_populated_registry(readiness, declared, tmp_path):
    """Six halves, each with a published version at both ends of its range."""
    releases = full_inventory(tmp_path, declared)
    plan = readiness.build_plan(
        revision="HEAD",
        releases=releases,
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    ).document()
    catalogue = plan["catalogue"]
    # The SDK declares ~=0.1.0, so 0.2.0 is outside it and 0.1.0/0.1.9 are
    # the ends; the declared workspace 0.1.0 is published, so no extra row.
    assert [row["subject"] for row in catalogue["sdk"]["down"]] == [
        "mcuhome-build-workspace 0.1.0",
        "mcuhome-build-workspace 0.1.9",
    ]
    # The workspace declares ~=0.1.0 of the tools: both published ones are
    # inside it, and the declared 0.1.0 is published, so again two rows.
    assert [row["subject"] for row in catalogue["workspace"]["down"]] == [
        "mcuhome-build-tools 0.1.0",
        "mcuhome-build-tools 0.1.5",
    ]
    assert (
        "2 published mcuhome-build-tools release(s) satisfy"
        in (plan["verdicts"]["workspace"]["down"])
    )
    # Looking up: only the SDK at ~=0.1.0 accepts workspace 0.1.0.
    assert [row["subject"] for row in catalogue["workspace"]["up"]] == [
        "mcuhome-sdk 0.1.9",
        f"mcuhome-sdk {declared['sdk']} (this commit)",
    ]
    # And only the three workspaces at ~=0.1.0 accept tools 0.1.0.
    assert [row["subject"] for row in catalogue["tools"]["up"]] == [
        "mcuhome-build-workspace 0.1.0",
        "mcuhome-build-workspace 0.1.9",
        f"mcuhome-build-workspace {declared['workspace']} (this commit)",
    ]
    # Every triple the catalogue names holds together, so every row counts.
    assert all(one["required"] for one in plan["combinations"]), [
        one for one in plan["combinations"] if not one["required"]
    ]


def test_the_tag_time_rule_drops_this_commit_s_neighbours(readiness, declared, tmp_path):
    """`--published-only`: at a tag only what is published may decide."""
    releases = full_inventory(tmp_path, declared)
    plan = readiness.build_plan(
        revision="HEAD",
        releases=releases,
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
        published_only=True,
    ).document()
    subjects = [
        row["subject"]
        for directions in plan["catalogue"].values()
        for rows in directions.values()
        for row in rows
    ]
    assert subjects, "the catalogue is not empty just because the checkout is out"
    assert not any("this commit" in subject for subject in subjects)
    # Nothing is assembled out of a checkout neighbour either — the stage
    # under assessment is the only local one.
    for combination in plan["combinations"]:
        sources = [combination[stage]["source"] for stage in ("sdk", "workspace", "tools")]
        assert sources.count("checkout") <= 1, combination


def test_at_tag_time_an_unsatisfiable_stage_is_a_row_without_a_build(readiness, declared, tmp_path):
    """Nothing published to build it with is a verdict, not a crash."""
    plan = readiness.build_plan(
        revision="HEAD",
        releases=[],
        metas=lambda entry: [],
        repository=REPO_ROOT,
        published_only=True,
    ).document()
    assert plan["combinations"] == []
    for directions in plan["catalogue"].values():
        for rows in directions.values():
            assert all(row["combination"] is None for row in rows)
            assert all(row["required"] is False for row in rows)


def test_a_chain_that_cannot_resolve_is_reported_and_not_required(readiness, declared, tmp_path):
    """A combination nothing claims must not fail a job or a firmware leg.

    The published SDK here takes only 0.2.x workspaces, so pairing it with
    this commit's is a triple whose chain does not hold — worth building to
    see what a refusal looks like, never worth a red mark.
    """
    tag = "v0.3.0"
    write_meta(
        tmp_path,
        tag,
        "sdk.tar.zst.meta.json",
        meta_document(
            name="mcuhome-sdk",
            version="0.3.0",
            requires={"mcuhome-build-workspace": "~=0.2.0"},
        ),
    )
    # The workspace's "up" half adds the published SDK only when it accepts
    # the declared version, so the unresolvable pairing is reached through
    # the tools line, whose SDK follows from the workspace above it.
    plan = readiness.build_plan(
        revision="HEAD",
        releases=[release_entry(tag, "sdk.tar.zst", "sdk.tar.zst.meta.json")],
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    ).document()
    assert all(one["required"] for one in plan["combinations"])

    # Now make it real: a published workspace this commit's SDK does not
    # accept, tried from the tools line.
    write_meta(
        tmp_path,
        "workspace-v0.9.0",
        "w.tar.zst.meta.json",
        meta_document(
            name="mcuhome-build-workspace",
            version="0.9.0",
            requires={"mcuhome-build-tools": "~=0.1.0"},
        ),
    )
    plan = readiness.build_plan(
        revision="HEAD",
        releases=[
            release_entry(tag, "sdk.tar.zst", "sdk.tar.zst.meta.json"),
            release_entry("workspace-v0.9.0", "w.tar.zst", "w.tar.zst.meta.json"),
        ],
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    ).document()
    unresolvable = [one for one in plan["combinations"] if not one["required"]]
    assert unresolvable, "the tools line pairs 0.9.0 with an SDK that refuses it"
    assert "requires" in unresolvable[0]["reason"]
    assert "not required to work" in unresolvable[0]["reason"]
    rows = [
        row
        for row in plan["catalogue"]["tools"]["up"]
        if row["combination"] == unresolvable[0]["id"]
    ]
    assert rows and rows[0]["required"] is False
    assert "requires" in rows[0]["note"]


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


# --------------------------------------------------------------------------
# scripts/release.py: the release act
# --------------------------------------------------------------------------
#
# What a tag is about, what it has to pass, and what it publishes. The
# questions here are asked before anything is built, which is the only time
# their answers are still cheap.


def test_each_prefix_names_its_line(release):
    assert release.line_of("v0.1.10.dev3") == ("sdk", "0.1.10.dev3")
    assert release.line_of("workspace-v0.1.0") == ("workspace", "0.1.0")
    assert release.line_of("tools-v0.2.1") == ("tools", "0.2.1")


@pytest.mark.parametrize(
    "tag",
    [
        "something-else",  # not a release of this repository at all
        "vnot-a-version",  # the prefix is right and the rest is not a version
        "v",  # a prefix and nothing else
        "0.1.0",  # a version without a line
        "workspace-0.1.0",  # the line without the v
    ],
)
def test_a_tag_that_names_no_line_is_refused(release, tag):
    with pytest.raises(SystemExit, match="names no release line"):
        release.line_of(tag)


def test_check_tag_accepts_the_version_the_commit_declares(release, declared, capsys):
    for stage, prefix in (("sdk", "v"), ("workspace", "workspace-v"), ("tools", "tools-v")):
        tag = f"{prefix}{declared[stage]}"
        assert release.check_tag(tag=tag, revision="HEAD", repository=REPO_ROOT) == 0
        printed = capsys.readouterr().out
        assert f"stage={stage}" in printed
        assert f"version={declared[stage]}" in printed
        assert f"tag={tag}" in printed


def test_check_tag_refuses_a_version_the_commit_does_not_declare(release, declared):
    """The expensive mistake: a tag on an unbumped commit."""
    with pytest.raises(SystemExit) as refused:
        release.check_tag(tag="workspace-v9.9.9", revision="HEAD", repository=REPO_ROOT)
    # Both numbers, because the whole failure mode is that they differ
    # silently.
    assert "9.9.9" in str(refused.value)
    assert declared["workspace"] in str(refused.value)


def test_check_tag_tells_the_lines_apart(release, declared):
    """`v<workspace version>` is an SDK tag, and almost certainly a mistake."""
    with pytest.raises(SystemExit, match="sdk.version"):
        release.check_tag(tag=f"v{declared['workspace']}", revision="HEAD", repository=REPO_ROOT)


def write_package(directory: Path, name: str, *, meta: bool = True, checksum: bool = True) -> None:
    """An archive and the sidecars a release publishes beside it."""
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / name
    archive.write_bytes(b"not really an archive")
    if checksum:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (directory / f"{name}.sha256").write_text(f"{digest}  {name}\n")
    if meta:
        (directory / f"{name}.meta.json").write_text(json.dumps({"schema": 1}))


def test_a_release_publishes_the_archive_and_its_two_sidecars(release, tmp_path):
    write_package(tmp_path, "mcuhome-build-workspace-0.1.0.tar.zst")
    # index.json belongs to a source and a source is signed at the package
    # host: it is written by the package build and must not travel.
    (tmp_path / "index.json").write_text("{}")
    assets = release.release_assets(tmp_path, stage="workspace", version="0.1.0")
    assert [path.name for path in assets] == [
        "mcuhome-build-workspace-0.1.0.tar.zst",
        "mcuhome-build-workspace-0.1.0.tar.zst.sha256",
        "mcuhome-build-workspace-0.1.0.tar.zst.meta.json",
    ]


def test_the_tools_line_publishes_both_platforms_on_one_release(release, tmp_path):
    """The family's meta entry is recorded only once every member exists."""
    for platform in ("linux-amd64", "linux-arm64"):
        write_package(tmp_path, f"mcuhome-build-tools_{platform}-0.1.0.tar.zst")
    assets = release.release_assets(tmp_path, stage="tools", version="0.1.0")
    assert len(assets) == 6
    assert sum(path.name.endswith(".meta.json") for path in assets) == 2


def test_a_tools_release_missing_an_architecture_is_refused(release, tmp_path):
    write_package(tmp_path, "mcuhome-build-tools_linux-amd64-0.1.0.tar.zst")
    with pytest.raises(SystemExit, match="linux-arm64"):
        release.release_assets(tmp_path, stage="tools", version="0.1.0")


@pytest.mark.parametrize("absent", ["meta", "checksum"])
def test_a_package_without_its_sidecar_is_refused(release, tmp_path, absent):
    """The package host reads both and refuses a package that has neither."""
    write_package(
        tmp_path,
        "mcuhome-sdk-0.1.0.tar.zst",
        meta=absent != "meta",
        checksum=absent != "checksum",
    )
    with pytest.raises(SystemExit, match="is missing"):
        release.release_assets(tmp_path, stage="sdk", version="0.1.0")


def test_an_archive_this_tag_is_not_about_is_refused(release, tmp_path):
    """One tag releases one line — a second archive means something is wrong."""
    write_package(tmp_path, "mcuhome-build-workspace-0.1.0.tar.zst")
    write_package(tmp_path, "mcuhome-build-workspace-0.1.0+gate.abc123.tar.zst")
    with pytest.raises(SystemExit, match=r"does not publish"):
        release.release_assets(tmp_path, stage="workspace", version="0.1.0")


def combination(sdk: str, workspace: str, tools: str) -> dict:
    return {
        "id": "combination-1",
        "sdk": {"source": sdk, "version": "1"},
        "workspace": {"source": workspace, "version": "1"},
        "tools": {"source": tools, "version": "1"},
    }


def test_the_package_matrix_builds_what_this_commit_contributes(release):
    """A published stage is downloaded, not built; a stand-in is built."""
    matrix = release.gate_packages([combination("published", "release", "published")])
    assert [entry["stage"] for entry in matrix] == ["workspace"]
    assert matrix[0]["release"] is True
    assert matrix[0]["artifact"] == "packages-workspace"


def test_the_tools_stage_is_built_for_both_platforms(release):
    matrix = release.gate_packages([combination("checkout", "published", "release")])
    tools = [entry for entry in matrix if entry["stage"] == "tools"]
    assert [entry["platform"] for entry in tools] == ["linux-amd64", "linux-arm64"]
    assert {entry["runner"] for entry in tools} == {"ubuntu-latest", "ubuntu-24.04-arm"}
    stand_in = [entry for entry in matrix if entry["stage"] == "sdk"]
    assert stand_in and stand_in[0]["release"] is False


def test_the_released_stage_wins_over_a_stand_in(release):
    """Two combinations, one of which uses the tagged line as a stand-in."""
    matrix = release.gate_packages(
        [
            combination("published", "release", "published"),
            combination("checkout", "release", "published"),
        ]
    )
    workspace = [entry for entry in matrix if entry["stage"] == "workspace"]
    assert len(workspace) == 1 and workspace[0]["release"] is True


def summary_text(readiness, *, stage, plan, results) -> tuple[int, str]:
    """The summary as text and its exit status, without pytest's capture."""
    out, errors = io.StringIO(), io.StringIO()
    status = readiness.summarize(stage=stage, plan=plan, results=results, out=out, errors=errors)
    return status, out.getvalue()


def test_a_combination_nobody_claims_does_not_fail_the_job(readiness, tmp_path):
    """The other half of the rule: a broken chain is reported, never red."""
    plan = readiness.build_plan(
        revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
    ).document()
    # Made unclaimed, as an unresolvable chain would be.
    for combination in plan["combinations"]:
        combination["required"] = False
    for directions in plan["catalogue"].values():
        for rows in directions.values():
            for row in rows:
                row["required"] = False
    (tmp_path / "result-checkout-amd64.json").write_text(
        json.dumps({"combination": "checkout", "architecture": "amd64", "outcome": "failure"})
    )
    status, printed = summary_text(readiness, stage="sdk", plan=plan, results=tmp_path)
    assert status == 0
    assert "amd64: failure" in printed
    assert "**Failed:**" not in printed


def test_the_summary_shows_both_directions_where_both_exist(readiness):
    """The workspace is the one line with a stage above it and one below."""
    plan = readiness.build_plan(
        revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
    ).document()
    status, printed = summary_text(readiness, stage="workspace", plan=plan, results=None)
    assert status == 0
    assert "### Against what it requires" in printed
    assert "### Against what requires it" in printed
    assert "blocked until a mcuhome-build-tools" in printed


def test_the_sdk_summary_has_only_the_downward_half(readiness):
    """Nothing requires the SDK, so there is no second section to write."""
    plan = readiness.build_plan(
        revision="HEAD", releases=[], metas=lambda entry: [], repository=REPO_ROOT
    ).document()
    _, printed = summary_text(readiness, stage="sdk", plan=plan, results=None)
    assert "### Against what it requires" in printed
    assert "### Against what requires it" not in printed


def test_an_archive_without_a_checksum_is_refused(readiness, tmp_path):
    """A release always publishes one; without it nothing vouches for the bytes."""
    archive = package_files(tmp_path, name="mcuhome-build-workspace", version="0.1.0")
    (tmp_path / f"{archive.name}.sha256").unlink()
    with pytest.raises(SystemExit) as refusal:
        readiness.write_index(tmp_path)
    assert "vouched for by nothing" in str(refusal.value)


# --------------------------------------------------------------------------
# The release gate: the catalogue under the rule a tag lives by
# --------------------------------------------------------------------------
#
# Only published versions count, and "published" is a GitHub release. The
# three cases that decide a release are: this line requires something
# nobody has published (blocked), nothing published sits on top of it (a
# line start, and a verdict), and both ends of a published range exist
# (what a release promises).


def workspace_release(version="0.1.0", tools="~=0.1.0"):
    return (
        release_entry(
            f"workspace-v{version}",
            f"mcuhome-build-workspace-{version}.tar.zst",
            f"mcuhome-build-workspace-{version}.tar.zst.meta.json",
        ),
        meta_document(
            name="mcuhome-build-workspace",
            version=version,
            requires={"mcuhome-build-tools": tools} if tools else None,
        ),
    )


def tools_release(version="0.1.0"):
    return (
        release_entry(
            f"tools-v{version}",
            f"mcuhome-build-tools_linux-amd64-{version}.tar.zst",
            f"mcuhome-build-tools_linux-amd64-{version}.tar.zst.meta.json",
        ),
        meta_document(name="mcuhome-build-tools", version=version, architecture="linux-amd64"),
    )


def sdk_release(version="0.1.9", workspace="~=0.1.0"):
    return (
        release_entry(
            f"v{version}",
            f"mcuhome-sdk-{version}.tar.zst",
            f"mcuhome-sdk-{version}.tar.zst.meta.json",
        ),
        meta_document(
            name="mcuhome-sdk",
            version=version,
            requires={"mcuhome-build-workspace": workspace},
        ),
    )


def world(tmp_path, *made):
    """A published world: the release inventory and its meta documents."""
    releases = []
    for entry, document in made:
        releases.append(entry)
        write_meta(tmp_path, entry["tag_name"], f"{entry['tag_name']}.tar.zst.meta.json", document)
    return releases


def gate(readiness, stage, releases, tmp_path):
    return readiness.build_gate(
        stage=stage,
        revision="HEAD",
        releases=releases,
        metas=readiness.directory_metas(tmp_path),
        repository=REPO_ROOT,
    )


@pytest.mark.parametrize(
    ("stage", "names"),
    [("sdk", "mcuhome-build-workspace"), ("workspace", "mcuhome-build-tools")],
)
def test_a_line_whose_requirement_is_unpublished_is_blocked(readiness, tmp_path, stage, names):
    """Publishing a package no chain can be resolved through is a dead end."""
    answer = gate(readiness, stage, [], tmp_path)
    assert names in answer["blocked"]
    # The fix is the order the first releases have to go in.
    assert "Release the" in answer["blocked"]
    assert answer["combinations"] == []


def test_the_tools_line_has_nothing_below_it_and_is_never_blocked(readiness, tmp_path):
    answer = gate(readiness, "tools", [], tmp_path)
    assert "blocked" not in answer


def test_a_line_start_is_tried_under_this_commit(readiness, tmp_path, declared):
    """Nothing published accepts it, so only this commit can say anything."""
    answer = gate(readiness, "tools", [], tmp_path)
    (combination,) = answer["combinations"]
    assert combination["tools"] == {"source": "release", "version": declared["tools"]}
    assert combination["workspace"]["source"] == "checkout"
    assert combination["sdk"]["source"] == "checkout"
    assert "nothing for" in answer["verdicts"]["up"]


def test_a_workspace_release_takes_the_tools_it_requires(readiness, tmp_path, declared):
    """Published below, this commit's above — the chain a line start has."""
    releases = world(tmp_path, tools_release())
    answer = gate(readiness, "workspace", releases, tmp_path)
    (combination,) = answer["combinations"]
    assert combination["workspace"] == {"source": "release", "version": declared["workspace"]}
    assert combination["tools"] == {
        "source": "published",
        "version": "0.1.0",
        "tag": "tools-v0.1.0",
    }
    assert combination["sdk"]["source"] == "checkout"
    assert answer["verify"]["mode"] == "pushed-image"


def test_an_sdk_release_is_tried_against_the_published_range(readiness, tmp_path, declared):
    releases = world(tmp_path, tools_release(), workspace_release())
    answer = gate(readiness, "sdk", releases, tmp_path)
    (combination,) = answer["combinations"]
    assert combination["sdk"] == {"source": "release", "version": declared["sdk"]}
    assert combination["workspace"]["source"] == "published"
    assert combination["tools"]["source"] == "published"
    # The image an SDK release is verified in is the one of the workspace it
    # resolves to, and it may not exist yet.
    assert answer["verify"] == {
        "mode": "published-image",
        "combination": combination["id"],
        "workspace_version": "0.1.0",
    }


def test_a_published_workspace_with_unpublished_tools_blocks_an_sdk_release(readiness, tmp_path):
    """The chain has to hold end to end, not only at the first link."""
    releases = world(tmp_path, workspace_release())
    answer = gate(readiness, "sdk", releases, tmp_path)
    assert "resolves to" in answer["blocked"]
    assert "mcuhome-build-tools" in answer["blocked"]


def test_the_gate_carries_only_the_line_being_released(readiness, tmp_path):
    releases = world(tmp_path, tools_release(), workspace_release())
    answer = gate(readiness, "sdk", releases, tmp_path)
    # The SDK has no stage above it, so one direction and one only.
    assert set(answer["catalogue"]) == {"down"}
    assert set(answer["verdicts"]) == {"down"}


def test_both_ends_of_a_published_range_are_tried(readiness, tmp_path):
    releases = world(
        tmp_path, tools_release(), workspace_release("0.1.0"), workspace_release("0.1.9")
    )
    answer = gate(readiness, "sdk", releases, tmp_path)
    tried = sorted(one["workspace"]["version"] for one in answer["combinations"])
    assert tried == ["0.1.0", "0.1.9"]
    # A release promises the whole range, so the image worth verifying is
    # the one a user actually resolves to: the newest.
    assert answer["verify"]["workspace_version"] == "0.1.9"


def test_a_tools_release_is_verified_by_an_image_revision_and_says_so(readiness, tmp_path):
    answer = gate(readiness, "tools", [], tmp_path)
    assert answer["verify"]["mode"] == "skip"
    assert "revision dispatch" in answer["verify"]["reason"]


# --------------------------------------------------------------------------
# image-packages: what an image of one workspace release delivers
# --------------------------------------------------------------------------


def test_an_image_delivers_the_newest_tools_the_workspace_accepts(release, readiness, tmp_path):
    releases = world(tmp_path, tools_release("0.1.0"), tools_release("0.1.4"), sdk_release())
    answer = release.image_packages(
        workspace_meta=meta_document(
            name="mcuhome-build-workspace",
            version="0.1.0",
            requires={"mcuhome-build-tools": "~=0.1.0"},
        ),
        releases=releases,
        metas=readiness.directory_metas(tmp_path),
    )
    assert answer["tools_tag"] == "tools-v0.1.4"
    assert answer["workspace_version"] == "0.1.0"
    # The SDK is not part of the image; it is what the verification
    # afterwards compiles.
    assert answer["sdk_tag"] == "v0.1.9"


def test_an_image_whose_tools_are_unpublished_is_refused(release, readiness, tmp_path):
    releases = world(tmp_path, tools_release("0.9.0"))
    with pytest.raises(SystemExit, match="no published version satisfies it"):
        release.image_packages(
            workspace_meta=meta_document(
                name="mcuhome-build-workspace",
                version="0.1.0",
                requires={"mcuhome-build-tools": "~=0.1.0"},
            ),
            releases=releases,
            metas=readiness.directory_metas(tmp_path),
        )


def test_a_workspace_no_sdk_accepts_has_no_sdk_to_verify_with(release, readiness, tmp_path):
    releases = world(tmp_path, tools_release("0.1.0"), sdk_release(workspace="~=0.9.0"))
    answer = release.image_packages(
        workspace_meta=meta_document(
            name="mcuhome-build-workspace",
            version="0.1.0",
            requires={"mcuhome-build-tools": "~=0.1.0"},
        ),
        releases=releases,
        metas=readiness.directory_metas(tmp_path),
    )
    assert answer["sdk_tag"] == ""
    assert answer["tools_tag"] == "tools-v0.1.0"


def test_the_build_workspace_never_carries_a_local_suffix(release):
    """Its declaration names the tools version, and a tag cannot hold a `+`."""
    matrix = release.gate_packages([combination("checkout", "checkout", "release")])
    by_stage = {entry["stage"]: entry for entry in matrix}
    assert by_stage["sdk"]["suffix"] is True
    # Standing in for a line nothing has published, and still unsuffixed:
    # the tools it is delivered with are the released ones, which are not.
    assert by_stage["workspace"]["release"] is False
    assert by_stage["workspace"]["suffix"] is False
    assert by_stage["tools"]["suffix"] is False


# --------------------------------------------------------------------------
# Where a verification takes each stage's bytes from
# --------------------------------------------------------------------------
#
# The case that broke a real release: an SDK tag verifies with its own
# released package plus two published ones, and that combination of sources
# is the one no rehearsal had ever produced.


def test_an_sdk_release_verifies_with_its_release_and_two_published_ones(release):
    sources = release.verify_sources(
        {
            "sdk": {"source": "release", "version": "0.1.10.dev3"},
            "workspace": {"source": "published", "version": "0.1.0", "tag": "workspace-v0.1.0"},
            "tools": {"source": "published", "version": "0.1.0", "tag": "tools-v0.1.0"},
        },
        released_tag="v0.1.10.dev3",
        rehearsal=False,
    )
    assert sources == {
        # Its own release, not the artefact it was uploaded from: what is
        # verified is what was published.
        "sdk": {"from": "release", "tag": "v0.1.10.dev3"},
        "workspace": {"from": "release", "tag": "workspace-v0.1.0"},
        "tools": {"from": "release", "tag": "tools-v0.1.0"},
    }


def test_a_rehearsal_has_only_its_own_artefact_for_the_released_line(release):
    sources = release.verify_sources(
        {
            "sdk": {"source": "release", "version": "0.1.10.dev3"},
            "workspace": {"source": "published", "version": "0.1.0", "tag": "workspace-v0.1.0"},
            "tools": {"source": "published", "version": "0.1.0", "tag": "tools-v0.1.0"},
        },
        released_tag="v0.1.10.dev3",
        rehearsal=True,
    )
    assert sources["sdk"] == {"from": "artifact", "tag": ""}
    assert sources["workspace"]["from"] == "release"


def test_a_stand_in_is_never_taken_from_a_release(release):
    """It carries a local version no package host will ever accept."""
    sources = release.verify_sources(
        {
            "sdk": {"source": "checkout", "version": "0.1.10.dev3"},
            "workspace": {"source": "release", "version": "0.1.0"},
            "tools": {"source": "published", "version": "0.1.0", "tag": "tools-v0.1.0"},
        },
        released_tag="workspace-v0.1.0",
        rehearsal=False,
    )
    assert sources["sdk"] == {"from": "artifact", "tag": ""}
    assert sources["workspace"] == {"from": "release", "tag": "workspace-v0.1.0"}


def test_every_stage_of_a_verification_has_a_source(release):
    """Three directories, one per stage — the job refuses an empty one."""
    for rehearsal in (False, True):
        sources = release.verify_sources(
            {
                "sdk": {"source": "release", "version": "1"},
                "workspace": {"source": "published", "version": "1", "tag": "workspace-v1"},
                "tools": {"source": "published", "version": "1", "tag": "tools-v1"},
            },
            released_tag="v1",
            rehearsal=rehearsal,
        )
        assert set(sources) == {"sdk", "workspace", "tools"}
        assert all(one["from"] in ("release", "artifact") for one in sources.values())
        assert all(one["tag"] or one["from"] == "artifact" for one in sources.values())


# --------------------------------------------------------------------------
# verify-plan: the published chain around a release that already exists
# --------------------------------------------------------------------------


def plan_for(release, readiness, stage, version, releases, tmp_path):
    return release.verify_plan(
        stage=stage,
        version=version,
        releases=releases,
        metas=readiness.directory_metas(tmp_path),
    )


def test_a_published_sdk_release_resolves_its_whole_chain(release, readiness, tmp_path):
    releases = world(
        tmp_path, tools_release("0.1.0"), workspace_release("0.1.0"), sdk_release("0.1.10.dev3")
    )
    answer = plan_for(release, readiness, "sdk", "0.1.10.dev3", releases, tmp_path)
    assert answer == {
        "sdk_tag": "v0.1.10.dev3",
        "workspace_tag": "workspace-v0.1.0",
        "tools_tag": "tools-v0.1.0",
        "image_workspace_version": "0.1.0",
    }


def test_a_published_workspace_release_resolves_up_and_down(release, readiness, tmp_path):
    releases = world(
        tmp_path, tools_release("0.1.0"), workspace_release("0.1.0"), sdk_release("0.1.10.dev3")
    )
    answer = plan_for(release, readiness, "workspace", "0.1.0", releases, tmp_path)
    assert answer["tools_tag"] == "tools-v0.1.0"
    assert answer["sdk_tag"] == "v0.1.10.dev3"
    assert answer["image_workspace_version"] == "0.1.0"


def test_verifying_a_tag_that_is_not_published_is_refused(release, readiness, tmp_path):
    releases = world(tmp_path, tools_release("0.1.0"))
    with pytest.raises(SystemExit, match="not a published"):
        plan_for(release, readiness, "sdk", "9.9.9", releases, tmp_path)


def test_a_chain_with_a_missing_link_is_refused(release, readiness, tmp_path):
    """Half a chain proves nothing, so it is a refusal and not a warning."""
    releases = world(tmp_path, sdk_release("0.1.10.dev3"))
    with pytest.raises(SystemExit, match="resolves to no mcuhome-build-workspace"):
        plan_for(release, readiness, "sdk", "0.1.10.dev3", releases, tmp_path)


def test_a_workspace_no_published_sdk_accepts_cannot_be_verified(release, readiness, tmp_path):
    releases = world(
        tmp_path,
        tools_release("0.1.0"),
        workspace_release("0.1.0"),
        sdk_release(workspace="~=9.9.0"),
    )
    with pytest.raises(SystemExit, match="compiled from an SDK package"):
        plan_for(release, readiness, "workspace", "0.1.0", releases, tmp_path)


def test_a_tools_release_is_verified_by_an_image_revision(release, readiness, tmp_path):
    releases = world(tmp_path, tools_release("0.1.0"))
    with pytest.raises(SystemExit, match="revision"):
        plan_for(release, readiness, "tools", "0.1.0", releases, tmp_path)
