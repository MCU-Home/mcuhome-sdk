# Releasing

A release of this repository is **one number for three artifacts**: the
`mcuhome-model` and `mcuhome-compiler` distributions and the
`mcuhome-sdk-<version>.tar.zst` package a build compiles from. They share
one version source, `mcuhome/model/__init__.py`.

A tag also builds `mcuhome-build-workspace-<version>.tar.zst` — the
build environment's source world, named after the same version. The
build environment's host tools
(`mcuhome-build-tools_linux-amd64`/`_linux-arm64`) are on a separate
counter, `TOOLS_VERSION` in `scripts/build_env_package.py`, and do not
build on every release — see step 2.

Three steps. Only the first is typed.

## 1. Cut it locally

```sh
. .venv/bin/activate
python scripts/release.py 0.1.0          # --dry-run first, if you like
```

That checks you are on `main`, clean, and level with `origin`; that the
version moves forward; and that the gates pass. Then it bumps every
version file, moves `CHANGELOG.md`'s `[Unreleased]` section into a dated
one, commits with sign-off and creates the annotated tag.

**It stops there.** Nothing is pushed, and it prints how to undo:

```sh
git tag -d v0.1.0 && git reset --hard HEAD~1
```

The bump only changes `mcuhome/model/__init__.py` on disk; an editable
install of `mcuhome-model`/`mcuhome-compiler` made before the bump still
reports its old version, because editable-install metadata is frozen at
install time. Refresh both after cutting a release, or `scripts/test
pytest` fails its installed-version checks:

```sh
pip install --no-deps -e ./packaging/model -e ./packaging/compiler
```

## 2. Push, and the packages build themselves

```sh
git push && git push origin v0.1.0
```

The tag starts the `Release` workflow: it refuses immediately if the tag
does not name the version the commit declares, then builds the SDK
archive and the `mcuhome-build-workspace` package from that **commit**
with a pinned compressor, and attaches each archive and its `.sha256` to
the GitHub release — the workspace package's declaration sidecar
(`<archive>.build-environment.json`) goes up alongside it. The workspace
package builds by default; a manual dispatch can turn it off with the
`workspace` input (`workflow_dispatch`, default `true`) when only the SDK
archive is wanted.

The `mcuhome-build-tools_linux-amd64`/`_linux-arm64` packages do **not**
build on a tag push. They build only on an explicit `workflow_dispatch`
with the `tools` input set — one run, both architectures, each on its own
runner — because a release that did not touch the toolchain generation
would otherwise republish gigabytes of identical binaries. Each
architecture is built twice on its runner and the two digests are
compared before anything uploads, so a tools release is never a single
unverified build.

GitHub rejects a release asset at or over 2 GiB, and does so illegibly (a
bare HTTP 500 or an empty asset); every archive this workflow uploads is
checked against that limit first and fails with a sentence instead. The
workspace package — a multi-gigabyte source-world snapshot — is the one
that can actually reach it; the SDK and tools archives sit far below.

## 3. Publish it — one click per source

The package host does not watch this repository; publishing is a
deliberate act, because what it records is permanent. `sdk`,
`build-workspace` and `build-tools` are three separate sources, each
published by its own dispatch of the same workflow:

> **github.com/mcu-home/mcuhome-packagetool → Actions →
> "Publish a package" → Run workflow**
> `source` = `sdk` | `build-workspace` | `build-tools`, `tag` = `v0.1.0`

It fetches the release asset(s) the source declares, checks them against
their `.sha256` sidecars, records them in a signed index and verifies the
result before committing. `build-workspace`'s declaration sidecar travels
with its archive into the source, next to it. `build-tools` fetches both
architecture packages of the tag in one dispatch — publishing one without
the other is refused — and, once every member is present, additionally
records the meta package `mcuhome-build-tools`, which points at both,
automatically. Within a minute the package is live at
`https://packages.mcuhome.org/<source>/`.

## The two rules that have no undo

- **A tag is never moved and never reused.** The package is named after
  the version in the commit and its bytes are pinned by hash; a second
  set of bytes under one number is exactly what the whole scheme exists
  to prevent. A botched release gets the next number.
- **A published version is never removed.** Not the file, not the index
  entry. Plan the number accordingly — and note that pre-releases are
  free: `0.2.0.dev1` is invisible to a stable pin like `~=0.2`, so
  exercising the pipeline costs nothing.

## What the version means elsewhere

Bumping `mcuhome/model/__init__.py` moves `mcuhome-model` and
`mcuhome-compiler` together, by design (they version in lockstep). The
sibling repositories — `mcuhome` (the workbench), `cli`, `build-server` —
carry their own numbers and their own release, and cross-repository
edges are `~=X.Y.0` from v1.0 on.

`imgtool` is pinned to the MCUboot line in `west.yml` and the two are
bumped as a pair; a release that moves one and not the other is a defect.
