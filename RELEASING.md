# Releasing

This repository cuts **three release lines out of one history**, and they
are versioned independently:

| Line | Tag | What it publishes |
|---|---|---|
| SDK | `v<version>` | `mcuhome-sdk-<version>.tar.zst` — the source a build compiles from |
| build workspace | `workspace-v<version>` | `mcuhome-build-workspace-<version>.tar.zst` — the build environment's pinned source world |
| build tools | `tools-v<version>` | `mcuhome-build-tools_linux-amd64` and `_linux-arm64` — the build environment's host tools |

No artifact takes another one's version. A tag releases exactly one line,
and nothing else travels with it.

All three numbers live in one file,
[`packaging/build-environment/environment.json`](packaging/build-environment/environment.json),
together with what each line requires of the one below it:

```json
{
  "sdk":       { "version": "0.1.10.dev3", "requires": { "mcuhome-build-workspace": "~=0.1.0" } },
  "workspace": { "version": "0.1.0",       "requires": { "mcuhome-build-tools": "~=0.1.0" } },
  "tools":     { "version": "0.1.0" }
}
```

A **chain, not a matrix**: an SDK accepts a range of workspace packages, a
workspace package accepts a range of tools packages, and the tools end the
chain. Whoever builds resolves each stage to the newest published version
satisfying the constraint above it and pins that one exactly.

`mcuhome/model/VERSION` is generated from `sdk.version` by the package
build and is never committed. There is no other place a version is
written down.

## Before you tag

**Every push already answers the release questions.** The `Build` workflow
(`.github/workflows/ci-build.yml`) builds all three packages out of the
commit and reports per line whether it could be released from here —
`Assess (SDK release readiness)` and its two siblings. Read those before
cutting anything: a red one is a release that would fail its gate.

**And you can rehearse the tag itself**, against any ref, without
publishing anything:

```sh
gh workflow run release.yml --ref main -f rehearse=workspace-v0.1.0
```

That runs the whole release act except the publishing: the tag check, the
gate, the package builds and every firmware build the gate demands. It
creates no release, pushes no image, and moves nothing.

## 1. Bump the line

Edit the version of the line you are releasing in
`packaging/build-environment/environment.json`, and — if this release
changes what it accepts — its `requires`. Commit that on `main`.

The bump is not bookkeeping: it is the statement of what the change *is*.
A published version is immutable, so the first change to a line's inputs
after a release **has** to move that line's version, and CI refuses the
push otherwise:

> `workspace 0.1.0 is already published as workspace-v0.1.0, and this
> commit's inputs are not the ones it was published with.`

with the two input hashes and the list of what moved between them. The fix
is always the same sentence: bump the version, patch, minor or major,
whichever this change is.

**Which of the three is a change to?** `scripts/release_lines.py` answers
it for a commit without building anything:

```sh
scripts/release_lines.py inputs-listing workspace --revision HEAD
scripts/release_lines.py inputs-sha256 tools --architecture linux-amd64
```

Those input lists are what a line *is*: the manifest, the patches and the
pre-generation for the workspace; the tool downloads and the requirement
set for the tools; the archived tree for the SDK — plus, in each case, the
script that decides the bytes. A cosmetic change to one of those scripts
moves the hash, which is the safe direction: nothing can tell a comment
from a behaviour by reading a file.

## 2. Tag it, and push the tag

```sh
git tag -a workspace-v0.1.0 -m "mcuhome-build-workspace 0.1.0"
git push && git push origin workspace-v0.1.0
```

The tag has to name the version the commit declares for that line; the
release workflow's first step refuses otherwise, before anything is built.
The archives are named after the version in the **commit**, never after the
tag.

## 3. What the tag does

The `Release` workflow branches on the tag and walks five stages.

**`gate-release`** — which line, which version, and what has to pass. The
tag is held against `environment.json`, and then the catalogue is built
under the rule a tag lives by: **only published versions count**, and
published means a GitHub release of this repository. The package registry
is never asked; it is fed by hand afterwards, so a check that asked it
would be answering about yesterday.

What the catalogue demands, per line:

- **SDK tag** — the firmware at the lowest and the highest published build
  workspace its `requires` admits (each with the newest published tools
  that workspace accepts). That range is what an SDK release *promises*, so
  both ends of it are tried. Nothing published satisfies it → the release
  is **blocked**, before anything is built:

  > mcuhome-sdk 0.1.10.dev3 requires mcuhome-build-workspace '~=0.1.0', and
  > nothing published satisfies it.
  > mcuhome-sdk 0.1.10.dev3 cannot be released: a package no chain can be
  > resolved through is a dead end.
  > Release the mcuhome-build-workspace line first — tag
  > workspace-v&lt;version&gt; with a version that constraint admits — and cut
  > this one afterwards.
- **workspace tag** — the firmware with the oldest and the newest published
  SDK whose `requires` already accepts this version (a patch of the line),
  or — where none does, which is what a line start looks like — with the
  SDK of this commit, and the verdict says that no released SDK uses it
  yet. The build tools it requires have to be published — an image and a
  build both resolve through them — and the same refusal fires one stage
  down when they are not.
- **tools tag** — the same one stage down, against published build
  workspaces.

A stage that has nothing published to stand in for it is built from this
commit under a `+gate.<sha>` local version — the one shape a package host
refuses to publish, so a stand-in can never be mistaken for a release.

**`build-packages`** — the tagged line's package(s), built from the tagged
commit inside the pinned packager, at the real version with no suffix.
Those are the bytes everything below uses and the bytes that get published;
there is no second build of the same inputs anywhere in the run.

**`build-firmware`** — the reference Matter device, once per combination
the gate named and on both architectures, built the way a user builds one:
`mcuhome device build`, subprocess profile, the packages as local sources.
A red leg publishes nothing.

**`publish-release`** — the GitHub release, carrying exactly three files
per package: the archive, its `.sha256` and its `.meta.json`. The asset
list is named rather than globbed, because a stand-in package for another
line is in the same run's artifacts. `index.json` stays behind: it belongs
to a *source*, and a source is signed by the package host. The release
notes carry each package's `requires`, its `inputs_sha256` and the sha256
of every attached byte.

**`build-environment-image`** and **`verify-release`** — see below.

### The meta file

Every package carries `meta.json` inside the archive and, byte for byte the
same document, `<archive>.meta.json` beside it:

```json
{
  "schema": 1,
  "package": { "name": "mcuhome-build-workspace", "version": "0.1.0", "architecture": null },
  "requires": { "mcuhome-build-tools": "~=0.1.0" },
  "inputs_sha256": "…",
  "contents": { "…": "the resolved project revisions, patches, tool versions" }
}
```

It is the document the whole chain runs on: the package host records it
beside the archive and lists it in the signed index, a workbench fetches
exactly one per stage to learn what that stage requires before downloading
gigabytes, and this repository's own CI reads the same file off the release
page. A package without it is not a resolution candidate for anybody, which
is why `publish-release` refuses to upload a package that is missing one.

## 4. Publish it — the registry operator's step

The registry does not watch this repository. Publishing is a deliberate act
on the registry host, done by its operator, and nothing in this repository
or its CI can trigger or observe it. This repository's part ends when the
tag's release carries the archives.

From there the operator's pipeline discovers the new release — each source
matches its own tag pattern (`v*`, `workspace-v*`, `tools-v*`) — downloads
the assets, checks each archive against its `.sha256`, records it and its
meta file in a signed index and publishes a new registry snapshot. A
package whose checksum or meta sidecar is missing is refused there. The
`build-tools` source additionally records the meta package
`mcuhome-build-tools`, which points at both platforms — and it waits until
both are present, which is why one tools release carries both.

How that pipeline is invoked is documented on the operator side, in
[mcuhome-packagetool](https://github.com/mcu-home/mcuhome-packagetool)'s
`deploy/` material.

Once published, the packages are not served from `packages.mcuhome.org`
directly — that host carries the signed head documents and a page that
browses the registry. The bytes live on a mirror, discovered per source
through its `mirrors.json`. To check a release landed:

```sh
curl -fsSL https://mirror-1.packages.mcuhome.org/build-workspace/index.json
```

or verify a source in full against the trust anchor with
`mcuhome-packagetool`'s `verify.py`, as its README describes.

## 5. The build-environment image

`ghcr.io/mcu-home/build-environment` is the container profile of the build
environment: an **assembly** of one build workspace package and the newest
published build tools that package's own `meta.json` accepts. Its tag is
`<workspace package version>-r<n>`, where `-r<n>` counts assemblies of the
same package set from 1.

A **workspace release builds `-r1` itself**, in the same run, right after
the release is published — from the archive it just built and the tools
release its meta accepts, one manifest per architecture and an OCI index
over the two. It needs no published registry copy and asks none: the
assembly is a delivery of package bytes, and those bytes are on the release
page this run just wrote.

Every other image is a **revision dispatch**:

```sh
gh workflow run release.yml -f workspace_version=0.1.0 -f revision=2
```

That assembles `0.1.0-r2` from the published `workspace-v0.1.0` release and
whatever tools it accepts *today*. Three reasons to do it: a refreshed
Debian base, a change to `containers/build-environment/Dockerfile`, or a
build tools release the workspace package accepts and that should reach
users without a new workspace release.

**An existing tag is a refusal.** The tag is the content identity, and
`-r<n>` is the counter that exists for a second assembly; a run that found
its tag taken says so and stops. A half-published set (one architecture up,
one failed) is repeated under the next revision, never patched in place.

## 6. `verify-release`

The gate built the reference device in the *subprocess* profile. This job
builds it again in the **container** profile, against the image, on both
architectures, with the release's packages as local sources. The workbench
holds the image's `org.mcuhome.build-environment.packages.*` labels against
the package set the context resolved and refuses to build in an image that
does not declare exactly it — which is what makes this a verification of
the image rather than another build.

Which image, per line:

- **workspace tag, revision dispatch** — the image this run just pushed,
  addressed by its digest.
- **SDK tag** — the published image of the build workspace this release
  resolves to, highest revision first. Where no image exists for that
  workspace yet, the job says so and verifies nothing rather than failing a
  release for another line's state.
- **tools tag** — nothing to verify against, and the job says so: an image
  delivers a workspace package and the tools it accepts, and no published
  image can declare tools that did not exist when it was assembled. A
  revision dispatch takes the new tools into an image, and verifies there.

## When the packager changes

`containers/build-environment-packager/` is the pinned toolchain both
environment packages are produced in — west at the environment's own
version, the `zap` the pinned CHIP revision names, and the interpreter that
decides the wheel set's ABI. It is not part of a release and has no version
of its own beyond its tag. It is published from `main` by the `Build`
workflow (`build-packager`, `publish-packager-index`), and every package
build pulls it **by digest**.

After a change to that directory:

1. bump `-r<n>` — or the SDK-version part, if the change is what makes a
   new SDK version need a new packager — in `scripts/packager_image.py`,
   and clear `DIGEST` in the same commit;
2. push to `main` and let the `Build` workflow publish the new tag; it
   prints the index digest as a run notice;
3. write that digest into `scripts/packager_image.py` and push again.

Between steps 1 and 3 no environment package can be built: the package jobs
refuse rather than run against an unpinned toolchain. That is the intended
order — the packages a release publishes have to name the exact bytes they
were produced in. The packager's digest is an input of the workspace and
tools lines, so bumping it moves their input hashes and therefore demands a
version bump of both.

## The two rules that have no undo

- **A tag is never moved and never reused.** The package is named after the
  version in the commit and its bytes are pinned by hash; a second set of
  bytes under one number is exactly what the whole scheme exists to
  prevent. A botched release gets the next number.
- **A published version is never removed.** Not the file, not the index
  entry. Plan the number accordingly — and note that pre-releases are free:
  `0.2.0.dev1` is invisible to a stable pin like `~=0.2`, so exercising the
  pipeline costs nothing.

## What the version means elsewhere

The `mcuhome-model` and `mcuhome-compiler` distributions take `sdk.version`
and move together, by design (they version in lockstep). The sibling
repositories — the workbench, the command line, the build server — carry
their own numbers and their own release, and cross-repository edges are
`~=X.Y.0` from v1.0 on.

`imgtool` is pinned to the MCUboot line in `west.yml` and the two are
bumped as a pair; a release that moves one and not the other is a defect.
