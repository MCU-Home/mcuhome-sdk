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

The build environment's container image is assembled from those two
packages once they are published — step 4.

Both environment packages are produced **inside the build-environment
packager** (`containers/build-environment-packager/`), the pinned
toolchain that carries the environment's own `west`, the `zap` the pinned
CHIP revision names and the interpreter the wheel set is built with. It is
published by the `Build` workflow on `main` and pinned by digest in
`scripts/packager_image.py`; the package jobs pull it and refuse when it
is absent. A release therefore needs that pin to be filled in — see
"When the packager changes" below.

Four steps.

## 1. Cut it locally

```sh
. .venv/bin/activate
python scripts/release.py 0.1.0          # --dry-run first, if you like
```

That checks you are on `main`, clean, and level with `origin`; that the
version moves forward; and that the gates pass. Then it bumps every
version file, commits with sign-off and creates the annotated tag.

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
(`<archive>.build-environment.json`) and the SDK archive's environment
lock (`<archive>.build-environment.lock.json`) go up alongside them. The
workspace package builds by default; a manual dispatch can turn it off with the
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

### The environment lock

The SDK archive carries a file `build-environment.lock.json` at its top
level, and the same bytes are written beside it as
`<archive>.build-environment.lock.json`. It states which build
environment this release was built and tested with:

```json
{
  "packages.mcuhome-build-tools": "0.1.10.dev1",
  "packages.mcuhome-build-workspace": "0.1.10.dev1"
}
```

`scripts/build_sdk_archive.py` generates it; nothing in the repository is
committed for it. The workspace version is the SDK's **own** version,
because the workspace package is built from this tag; the tools version
is read out of `scripts/build_env_package.py`'s `TOOLS_VERSION` at the
packaged commit, because the tools move on their own cadence. Neither
member carries a hash — at this point nobody has built those archives —
so a client resolves the versions to bytes through the package host's
signed index.

**What this means for a release that changes the tools.** Bump
`TOOLS_VERSION` in `scripts/build_env_package.py` *before* cutting the
release, or the lock will name the old tools package and every device
built with this SDK will get it. The two versions are independent on
purpose, and the lock is the only place they are tied together.

A workbench reads the lock out of the archive it already verified against
the pin, which is why the copy inside the package is the authoritative
one; the sidecar exists so a mirror and a release page can serve the same
statement without unpacking anything. The image dispatch of step 4 reads
the same copy, for the same reason — which is why this file decides both
what a device is built with and what an image is assembled from.

## 3. Publish it — the registry operator's step

The registry does not watch this repository; publishing is a deliberate
act on the registry host, done by its operator, not by anything in this
repository or its CI. This repository's part ends when the tag's release
carries the archives (step 2); from there:

- The operator triggers the server-side publish pipeline. It discovers
  the new GitHub release, downloads the asset(s) each source declares,
  checks them against their `.sha256` sidecars, records them in a signed
  index and atomically publishes a new registry snapshot.
  `build-workspace`'s declaration sidecar travels with its archive into
  the source, next to it. `build-tools` needs both architecture packages
  of the tag published together — once every member is present, the
  pipeline additionally records the meta package `mcuhome-build-tools`,
  which points at both.
- How that pipeline is invoked, and everything else about the registry
  host itself, is documented on the operator side in
  [mcuhome-packagetool](https://github.com/mcu-home/mcuhome-packagetool)'s
  `deploy/` material — out of scope here.

Once published, the packages are not served from `packages.mcuhome.org`
directly — that host only carries the signed head documents (the trust
anchor, `mirrors.json`, `keys.json`) and a page that explains and browses
the registry. The bytes live on a mirror, discovered per source through
its `mirrors.json`; the official one is
`https://mirror-1.packages.mcuhome.org/<source>/`. To check a release
landed:

```sh
curl -fsSL https://mirror-1.packages.mcuhome.org/sdk/index.json
```

or verify a source in full against the trust anchor with
`mcuhome-packagetool`'s `verify.py`, as its README describes.

## 4. The build environment image — after the packages are published

`ghcr.io/mcu-home/build-environment` is the container profile of the build
environment, and the one artifact that is not built from the commit: it is
*assembled* from the packages the registry already serves. So it comes
after step 3, on its own dispatch, and never on a tag push — at tag time
its packages are not published yet, and nothing in CI can observe the
operator's publish.

```sh
gh workflow run release.yml -f tag=v0.1.0 -f image=true -f revision=1
```

`image` makes the run an image run and nothing else: no package job builds
beside it. Per architecture, on a runner of that architecture, the run asks
`packages.mcuhome.org` for each consumed source's signed mirror list,
fetches the served documents from every mirror named there, and verifies
them with `mcuhome-packagetool`'s own `verify.py` against the registry's
trust anchor — three sources: `sdk`, `build-workspace` and `build-tools`.
It then downloads `mcuhome-build-workspace` and that architecture's
`mcuhome-build-tools` from the verified mirror, checks each archive's size
and sha256 against the index bytes the verifier accepted, assembles the
image with `scripts/build_env_image.py` — which reads the packages' own
declaration and labels the image with it — and pushes
`<version>-r<n>-<arch>`. A last job composes the OCI index over the two and
pushes it as `<version>-r<n>`, the reference a client resolves.

**Which versions it assembles comes from the environment lock, not from
the tag.** The dispatched tag names an SDK release; the tools package is on
its own counter and is regularly older than the SDK asking for it, so a run
that assumed the tag's version for both would ask the index for a tools
package that was never built. Instead the run downloads
`mcuhome-sdk-<version>.tar.zst` from the verified mirror, checks it against
the index like any other package, reads the `build-environment.lock.json`
inside it, and resolves exactly the two versions that document states — the
sidecar beside the archive and the release asset are deliberately not used,
because the registry is what was verified here. The workspace version has
to be the SDK's own, since that package is built from this tag, and the run
refuses when it is not. So a tools release is not needed for every image:
**bump `TOOLS_VERSION` before cutting the release, and the lock carries the
rest** (step 2).

**Prerequisite: the organisation variable `MCUHOME_REGISTRY_ANCHOR`.** It
carries the content of `mcuhome-packagetool`'s
`deploy/mcuhome/anchor.json` — public material, the registry's root keys
and their threshold. It is a *variable* and not a fetched document on
purpose: an anchor downloaded at verification time verifies nothing. The
job refuses when it is unset or empty, because a publish that quietly
skipped verification would be worse than one that failed.

`revision` is the `-r<n>` assembly counter and starts at 1. Raise it when
the same packages are assembled again — a new base image, a changed
`Dockerfile`. A per-architecture tag that already exists in the registry is
skipped with a notice rather than overwritten, so repeating a dispatch
after one architecture failed publishes only the missing half.

Publish the image from CI, not from a workstation. A locally assembled
image pins the hashes of locally built archives, and those are not the
bytes the index names — the same rule the packages follow.

## When the packager changes

`containers/build-environment-packager/` is not part of a release and has
no version of its own beyond its tag. It is published from `main` by the
`Build` workflow (`build-packager`, `publish-packager-index`), and the
release jobs consume it by digest.

After a change to that directory:

1. bump `-r<n>` — or the SDK-version part, if the change is what makes a
   new SDK version need a new packager — in `scripts/packager_image.py`,
   and clear `DIGEST` in the same commit;
2. push to `main` and let the `Build` workflow publish the new tag; it
   prints the index digest as a run notice;
3. write that digest into `scripts/packager_image.py` and push again.

Between steps 1 and 3 no environment package can be built: the package
jobs refuse rather than run against an unpinned toolchain. That is the
intended order — the packages a release publishes have to name the exact
bytes they were produced in.

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
