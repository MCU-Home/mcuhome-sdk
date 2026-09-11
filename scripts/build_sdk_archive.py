#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Build the ``mcuhome-sdk-<version>.tar.zst`` package — the same bytes every time.

This is a third artifact of the one release: the SDK is additionally
published as its own CI-built, hash-pinned source package (the
``mcuhome-sdk-<version>`` archive a build fetches) — same repository,
same version, different artifact. Repo, Python packages and SDK package
are names for one release. This script is the "CI-built" half; the
session-protocol side names the other half, and it is deliberately
small — a directory holding that archive is the whole first
implementation.

**Why the bytes have to be reproducible, and not merely correct.** The
pin travels in the build context as ``mcuhome.package.sha256``, and that
pin is hashed into the context ID. So two parties building the
same tag must reach the same digest, or the second one's package can
never satisfy a pin the first one resolved — and an archive that differs
by a timestamp yields a different context ID for a build in which
nothing build-relevant changed. Everything below that looks fussy is
that requirement: PAX format, entries sorted by name, one mtime for the
whole archive, ``uid``/``gid`` 0 with empty ``uname``/``gname``, modes
narrowed to two values, and one fixed zstd level. The residual variable
is the zstd *library*: identical parameters give identical output for
one libzstd, and a compressor version bump can move the bytes without
moving the content. That is why CI pins the ``zstandard`` version it
installs, and why a release is cut once rather than rebuilt on demand.

**The source is a commit, never the working tree.** ``git archive`` is
what reads it, so an uncommitted edit, an untracked file and a stale
``__pycache__`` cannot reach a package — the class of accident Block 0
was created to end ("three build inputs that existed on this machine and
in no repository"). The version is read out of the *archived* revision
for the same reason: ``mcuhome/model/__init__.py`` is "the version of
the whole release, and the only place it is written down", and reading
the working tree's copy would let the name on the file disagree with the
content inside it.

**What goes in is an explicit allowlist, and nothing else.** Each entry
below has a consumer that fails without it:

===============================  ==============================================
``mcuhome-sdk.json``             §6.1's entry-point declaration at the root of
                                 ``trees.sdk``; without it the container never
                                 reaches code generation
``bin/generate``                 the entry point itself — spawned as a child by
                                 absolute path, so its **exec bit** is archive
                                 content and not repository cosmetics
``mcuhome/model``,               the program body: a build environment's
``mcuhome/compiler``             entry point hands over to
                                 ``mcuhome/compiler/abi.py`` and refuses the
                                 step without it, and that module's import
                                 closure since the repository split is the
                                 model and the compiler themselves — the
                                 workbench neither travels in the SDK package
                                 nor exists in a build environment
``west.yml``                     west re-reads it on every CMake configure, out
                                 of the manifest repository's directory, which
                                 is exactly where the SDK is mounted
``zephyr/module.yml``            what makes the tree a *named* Zephyr module,
                                 and therefore what makes
                                 ``${ZEPHYR_MCUHOME_MODULE_DIR}`` exist
``CMakeLists.txt``, ``Kconfig``  the module's build entry points
``components/``, ``drivers/``,   what those two and ``module.yml`` point at:
``lib/``, ``include/``,          four ``add_subdirectory()`` calls, an include
``boards/``, ``dts/``,           root, and the three roots ``module.yml`` sets
``snippets/``                    to ``.``
``app/``                         the generic application main every generated
                                 CMakeLists compiles by module-relative path
``compat/``                      the include root the CHIP patch adds to CHIP's
                                 compile flags — the patch is image content,
                                 the headers are not
``scripts/pyshim/``              first on the children's ``PYTHONPATH`` for
                                 every build (CHIP v1.5.1.0 ships without the
                                 ``python_path`` helper its codegen imports)
``samples/``                     the netcore image a user must flash is built
                                 from ``samples/netcore-radio``, and the
                                 registry tells the user so
``LICENSE``, ``LICENSES/``,      this is a redistributed source distribution of
``REUSE.toml``                   Apache-2.0 material carrying per-file SPDX
                                 headers
===============================  ==============================================

Everything else is out, and the list is an allowlist rather than a
denylist precisely so that anything *new* in the repository stays out
until somebody names it here. The four that are out on purpose, because
each looks includable: ``patches/`` (the program applies patches from
the context, never from ``trees.sdk``, and the environment's own patch
set is applied when its workspace package is built), ``packaging/``
(nothing pip-installs this tree — the build environment's entry point
puts the SDK root on ``PYTHONPATH``), ``tests/python/`` and ``tests/``
(nothing in a build environment reads them), and ``containers/`` (an
image is a separate artifact, and the SDK is delivered *into* it).

**The tree is rooted at the SDK, with no wrapper directory.** The build
server unpacks into a directory it chose and hands that same directory
over as ``trees.sdk``; a ``mcuhome-sdk-<version>/`` wrapper would put
``mcuhome-sdk.json`` one level below the declared root and fail every
build. Member names are plain relative paths for the same reason — no
``./`` prefix, which the server's extractor refuses outright.

Alongside the archive go two files with no reader in the container path
and one reader each outside it: a ``.sha256`` sidecar in ``sha256sum``'s
own format, so an operator can check a mirrored file with the tool
already on the machine, and ``index.json``, the static index the
session protocol asks for. Neither carries a URL or a host name: the source
list is the operator's configuration, "``package.url`` is a hint" that
is never fetched, and the workspace rule against naming a domain before
the service behind it exists applies here too.

Usage::

    build_sdk_archive.py --output-dir <dir> [--revision <rev>] [--repo <dir>]

Exit status: 0 on success, 2 on a usage error, and a non-zero ``git``
exit propagates as a ``CalledProcessError`` — a package built from a
revision git could not read is not a failure worth recovering from.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

try:
    import zstandard
except ModuleNotFoundError:  # a system python, not the repo's venv
    sys.exit(
        "build_sdk_archive.py needs the zstandard module.\n"
        "Run it from this repository's own venv, which carries it via\n"
        "mcuhome-compiler:\n"
        "    python3 -m venv .venv && . .venv/bin/activate\n"
        "    pip install -e ./packaging/model -e ./packaging/compiler"
    )

#: This repository, seen from ``scripts/``. The default rather than a
#: constant, so a test can build a package from a checkout somewhere else.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The one place the release version is written down, read out of the
#: revision being archived rather than imported.
VERSION_FILE = "mcuhome/model/__init__.py"

#: The static index, next to the archives it indexes.
INDEX_FILE = "index.json"

#: The distribution name inside the index. Not the archive's file name:
#: the file name carries the version, the index key does not.
PACKAGE_NAME = "mcuhome-sdk"

#: The environment lock: which build-environment packages this release
#: was built and tested with. Written into the archive *and* beside it —
#: inside, because whoever resolves the pins has already verified those
#: bytes against the index, and reading the answer out of the package
#: itself is the one route that cannot be substituted; beside, so a
#: mirror and a release page can serve it without unpacking anything.
#:
#: Its shape is the abstract package set of the build environment
#: specification §5.1: one ``packages.<name>`` member per package, value
#: ``<version>``, no hashes. An SDK release cannot state hashes — the
#: workspace package is built *from* this tag and the tools package's
#: bytes differ per platform — so the versions are the statement and the
#: signed index turns them into bytes.
LOCK_FILE = "build-environment.lock.json"
PACKAGE_MEMBER_PREFIX = "packages."
WORKSPACE_PACKAGE = "mcuhome-build-workspace"
TOOLS_FAMILY = "mcuhome-build-tools"

#: Where the tools version is written down: the packaging script's own
#: constant, read out of the archived commit rather than imported, for
#: the reason :data:`VERSION_FILE` is.
TOOLS_VERSION_FILE = "scripts/build_env_package.py"

#: Fixed, and part of what makes two builds of one tag agree. 19 is the
#: highest level with a bounded memory appetite; the archive is written
#: once per release and read on every build, so the asymmetry is the
#: right way round.
ZSTD_LEVEL = 19

#: Files taken by name. A tree below would sweep in whatever lands next
#: to them — ``bin/`` is one file today, ``zephyr/`` holds only
#: ``module.yml``, and both are directories somebody could add to.
SDK_FILES = frozenset(
    {
        "CMakeLists.txt",
        "Kconfig",
        "LICENSE",
        "REUSE.toml",
        "bin/generate",
        "mcuhome-sdk.json",
        "west.yml",
        "zephyr/module.yml",
    }
)

#: Trees taken whole. Each one is named in the module docstring's table
#: together with the consumer that fails without it.
SDK_TREES = (
    "LICENSES",
    "app",
    "boards",
    "compat",
    "components",
    "drivers",
    "dts",
    "include",
    "lib",
    "mcuhome/compiler",
    "mcuhome/model",
    "samples",
    "scripts/pyshim",
    "snippets",
)


@dataclass(frozen=True)
class SdkArchive:
    """One built package: the file, and everything the index says about it."""

    path: Path
    version: str
    #: The commit the tree came from, for a build log and a bug report.
    commit: str
    sha256: str
    size: int


def package_filename(version: str) -> str:
    """What the package is called — restated from the only implemented consumer.

    ``mcuhome.buildserver.sdkstore.package_filename`` builds the same
    string to *find* a candidate in a source directory, and the two are
    kept apart on purpose: this repository must not import the build
    server to name its own artifact, and a package named anything else is
    a package that server cannot find.
    """
    return f"mcuhome-sdk-{version}.tar.zst"


#: Members this script writes itself rather than taking from the commit.
#: They are allowlisted like every other member, so the check that the
#: archive holds nothing outside the allowlist covers them too.
GENERATED_FILES = frozenset({LOCK_FILE})


def included(path: str) -> bool:
    """Whether *path* is allowlisted: a named file, a named tree, or generated."""
    return (
        path in SDK_FILES
        or path in GENERATED_FILES
        or any(path.startswith(f"{tree}/") for tree in SDK_TREES)
    )


def _git(repository: Path, *arguments: str) -> bytes:
    """One git command against *repository*, its stdout, and no shell."""
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
    )
    return completed.stdout


def archived_version(repository: Path, commit: str) -> str:
    """``__version__`` as *commit* carries it.

    Read out of the commit and parsed rather than imported, because the
    name on the file has to describe the bytes inside it: an editable
    install would answer with the working tree, which is precisely the
    tree this script refuses to package.
    """
    source = _git(repository, "cat-file", "blob", f"{commit}:{VERSION_FILE}").decode("utf-8")
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        named = (target.id for target in node.targets if isinstance(target, ast.Name))
        if "__version__" not in named:
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str):
            return value
    raise SystemExit(f"{VERSION_FILE} at {commit} declares no string __version__")


def _assigned(source: str, name: str, *, where: str) -> str:
    """The string a module-level ``<name> = "..."`` assigns, or a refusal."""
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign):
            continue
        if name not in (target.id for target in node.targets if isinstance(target, ast.Name)):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str):
            return value
    raise SystemExit(f"{where} declares no string {name}")


def tools_version(repository: Path, commit: str) -> str:
    """The tools package version *commit* builds its environment with.

    Read out of ``scripts/build_env_package.py`` at the packaged
    revision, which is the one place that number is written down. The
    tools move on their own cadence, so it is not the SDK's version and
    cannot be derived from it.
    """
    source = _git(repository, "cat-file", "blob", f"{commit}:{TOOLS_VERSION_FILE}").decode("utf-8")
    return _assigned(source, "TOOLS_VERSION", where=f"{TOOLS_VERSION_FILE} at {commit}")


def environment_lock(*, version: str, tools: str) -> dict[str, str]:
    """The lock document: which environment packages this release wants.

    The workspace package is stated at the SDK's **own** version, because
    it is built from this tag; the tools package is named by its family
    at version level only, because its bytes differ per platform on
    purpose and the sibling platforms' archives may not exist yet.
    Neither carries a hash: nothing has built them at this point, and the
    signed index is where a version becomes bytes.
    """
    return {
        f"{PACKAGE_MEMBER_PREFIX}{TOOLS_FAMILY}": tools,
        f"{PACKAGE_MEMBER_PREFIX}{WORKSPACE_PACKAGE}": version,
    }


def lock_bytes(document: dict[str, str]) -> bytes:
    """*document* as the bytes both copies carry — the same bytes, twice.

    Sorted keys, two-space indent, one trailing newline: the archive
    member and the sidecar are written from one value and must not differ
    in a byte, or the copy a reader happened to take would decide what a
    release means.
    """
    return (json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def sdk_entries(archive: bytes) -> dict[str, tuple[bytes, bool]]:
    """The allowlisted regular files of a ``git archive`` tar: content, exec bit.

    Directory members of the input are dropped and the output's are
    derived from the surviving paths (:func:`_directories`), so the
    archive can hold no directory that nothing needs — and no empty one,
    which git cannot represent anyway.
    """
    entries: dict[str, tuple[bytes, bool]] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar:
            if not member.isfile() or not included(member.name):
                continue
            handle = tar.extractfile(member)
            if handle is None:  # pragma: no cover - isfile() was true a line ago
                raise SystemExit(f"{member.name} carries no data")
            entries[member.name] = (handle.read(), bool(member.mode & 0o111))
    return entries


def _directories(names: list[str]) -> set[str]:
    """Every ancestor directory of *names*, so the tar carries the tree."""
    found: set[str] = set()
    for name in names:
        parts = name.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            found.add("/".join(parts[:depth]))
    return found


def write_tar(entries: dict[str, tuple[bytes, bool]], *, mtime: int) -> bytes:
    """The entries as one deterministic tar.

    PAX rather than GNU because it is the POSIX-2001 format with a
    defined answer for everything an extractor might meet, and Python's
    writer emits an extended header only for a field that does not fit —
    which, with an integer *mtime*, ``uid``/``gid`` 0 and paths well
    under 100 bytes, is none of them here. Sorted by name so member order
    is a property of the tree rather than of the filesystem that produced
    it, and the sort puts a directory before everything under it.

    Modes are narrowed to 0755 and 0644: the exec bit is the one mode fact
    a consumer reads (§6.1 spawns ``generate.program`` as a child), and
    everything else a checkout's umask happens to carry is noise that
    would change the digest.
    """
    plan: list[tuple[str, tuple[bytes, bool] | None]] = [
        (name, None) for name in _directories(list(entries))
    ]
    plan += list(entries.items())

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, payload in sorted(plan, key=lambda item: item[0]):
            info = tarfile.TarInfo(name)
            info.mtime = mtime
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            if payload is None:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
                continue
            content, executable = payload
            info.type = tarfile.REGTYPE
            info.mode = 0o755 if executable else 0o644
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def compress(tar: bytes) -> bytes:
    """zstd, with every frame parameter stated rather than defaulted.

    ``threads=0`` because a multi-threaded compressor splits the input
    into jobs and produces different bytes for the same input; the
    checksum is off and the content size is on so that the frame header
    is decided by the parameters and not by which API happened to know
    the length.
    """
    compressor = zstandard.ZstdCompressor(
        level=ZSTD_LEVEL,
        write_checksum=False,
        write_content_size=True,
        threads=0,
    )
    return compressor.compress(tar)


def build_archive(*, repository: Path, revision: str, output_dir: Path) -> SdkArchive:
    """Build the package for *revision* into *output_dir*, sidecar and index included."""
    commit = _git(repository, "rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()
    # The commit's own committer date, so the timestamps in the archive
    # are a property of the revision. `git archive` would stamp the same
    # value; it is read out explicitly because this script writes its own
    # tar and nothing else would then decide it.
    mtime = int(_git(repository, "show", "-s", "--format=%ct", commit).decode().strip())
    version = archived_version(repository, commit)

    entries = sdk_entries(_git(repository, "archive", "--format=tar", commit))
    if "mcuhome-sdk.json" not in entries:
        raise SystemExit(f"{commit} carries no mcuhome-sdk.json — that tree is not an SDK")
    # Generated rather than committed: the workspace version *is* the SDK
    # version, and a file in the tree restating it would be a second
    # place for one number to be wrong in.
    lock = lock_bytes(environment_lock(version=version, tools=tools_version(repository, commit)))
    entries[LOCK_FILE] = (lock, False)
    payload = compress(write_tar(entries, mtime=mtime))
    digest = hashlib.sha256(payload).hexdigest()

    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / package_filename(version)
    archive.write_bytes(payload)
    # sha256sum's own format — bare hex, two spaces, the file name — so
    # `sha256sum -c` checks a mirrored copy with no tooling of ours.
    (output_dir / f"{archive.name}.sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    # The same bytes the archive carries, beside it — named for the file
    # rather than for the package, the way the .sha256 sidecar is, so a
    # directory holding two releases keeps two locks.
    (output_dir / f"{archive.name}.{LOCK_FILE}").write_bytes(lock)
    write_index(
        output_dir / INDEX_FILE,
        version=version,
        file=archive.name,
        sha256=digest,
        size=len(payload),
    )
    return SdkArchive(
        path=archive, version=version, commit=commit, sha256=digest, size=len(payload)
    )


def write_index(path: Path, *, version: str, file: str, sha256: str, size: int) -> None:
    """Record this package in the static index, keeping the versions already there.

    The index answers exactly one question — "which file, and which bytes,
    for ``(name, version)``" — because that is the only one a backend asks
    of it: it "resolves ``(name, version, sha256)`` against its configured
    source list", so the index supplies the mapping and the operator
    supplies the location. A URL here would be a second, weaker answer to
    a question the source list already answers, and a client that followed
    one would be the server-side request forgery the session protocol
    rules out.

    An existing index is read and extended rather than replaced: a source
    directory holding two releases is the whole first implementation of
    the index, and rewriting it for each would leave it describing one.
    An unreadable one is a refusal, never an overwrite — the file is the
    only record of the packages already in that directory.
    """
    document: dict[str, dict[str, dict[str, dict[str, object]]]] = {"packages": {}}
    if path.exists():
        try:
            found = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as failure:
            raise SystemExit(f"{path} is not readable as JSON: {failure}") from failure
        if not isinstance(found, dict) or not isinstance(found.get("packages"), dict):
            raise SystemExit(f"{path} is not an index: no packages object")
        document = found
    packages = document["packages"].setdefault(PACKAGE_NAME, {})
    packages[version] = {"file": file, "sha256": sha256, "size": size}
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="where the archive, its .sha256 sidecar and index.json are written",
    )
    parser.add_argument(
        "--revision",
        default="HEAD",
        help="the commit or tag to package (default: HEAD) — never the working tree",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=REPO_ROOT,
        help="the checkout to read the revision from (default: this repository)",
    )
    arguments = parser.parse_args(argv)

    package = build_archive(
        repository=arguments.repo,
        revision=arguments.revision,
        output_dir=arguments.output_dir,
    )
    print(f"{package.path.name}  {package.sha256}  {package.size} bytes  ({package.commit})")
    return 0


if __name__ == "__main__":  # pragma: no cover - the command line's entry point
    raise SystemExit(main(sys.argv[1:]))
