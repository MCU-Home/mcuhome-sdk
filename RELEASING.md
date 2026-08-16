# Releasing

A release of this repository is **one number for three artifacts**: the
`mcuhome-model` and `mcuhome-compiler` distributions and the
`mcuhome-sdk-<version>.tar.zst` package a build compiles from. They share
one version source, `mcuhome/model/__init__.py`.

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

## 2. Push, and the package builds itself

```sh
git push && git push origin v0.1.0
```

The tag starts the `Release` workflow: it refuses immediately if the tag
does not name the version the commit declares, then builds the archive
from that **commit** with a pinned compressor, and attaches the archive
and its `.sha256` to the GitHub release.

## 3. Publish it — one click

The package host does not watch this repository; publishing is a
deliberate act, because what it records is permanent.

> **github.com/mcu-home/packages.mcuhome.org → Actions →
> "Publish a package" → Run workflow**
> `source` = `sdk`, `tag` = `v0.1.0`

It fetches the asset, checks it against the sidecar, records it in a
signed index and verifies the result before committing. Within a minute
the package is live at `https://packages.mcuhome.org/sdk/`.

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
