# containers/build-environment/

MCUHome's build environment as a **container image** — the container
profile of the
[build environment specification](../../docs/spec/build-environment-specification.md)
(generation 3). The image is an *assembly* of the two packages
[`packaging/build-environment/`](../../packaging/build-environment/README.md)
describes, and it adds nothing to them that a build compiles with.

The image is `ghcr.io/mcu-home/build-environment`, tagged
`<workspace package version>-r<n>` — the version of the workspace package it
delivers, plus an assembly revision counted from 1 for rebuilds from the
same packages. The version half is read out of the package, never typed, so
a tag cannot name a version the image does not contain.

> **A tag is a location, not an identity.** An orchestrator finds this image
> by its `org.mcuhome.build-environment.packages.<name>` labels (§5.2), and
> two tags on one package set are the same environment. Never resolve an
> environment by tag.

This is **not** the image `containers/build-container/` builds. That one
bakes its own workspace and its own toolchain and implements the legacy
invocation; it is untouched, still published, and both run side by side
until the switchover.

## Building one

```
scripts/build_env_image.py \
    --workspace <dir>/mcuhome-build-workspace-<version>.tar.zst \
    --tools <dir>/mcuhome-build-tools_linux-amd64-<version>.tar.zst
```

The script derives the tag; `--revision <n>` sets the assembly counter and
`--tag` overrides the whole reference for a local experiment. Building does
not publish — pushing is a separate, deliberate act, and one this repository
does in CI (below) rather than from a workstation.

Through the script, not with a bare `docker build`. Specification §5.2 asks
an image to repeat every member of its packages' declaration as an OCI
label, and a `LABEL` instruction cannot read a file — so the script reads
the declaration out of the workspace package, passes one `--label` per
member, and reads the built image back to check that the two agree. An image
built straight from the `Dockerfile` carries none of those labels, and "the
`packages.` labels are what make an image findable": no orchestrator would
ever select it.

**The declaration is read from both copies and they must agree.** §5 has the
orchestrator read it "from the package metadata when it provisions packages,
from the image configuration when it runs an image. Both must say the same
thing" — and the package itself writes it twice, beside the archive for a
reader that has not unpacked anything and inside it for an unpacked store
entry. The script compares the two byte for byte and refuses on a
difference; either one alone is a complete answer.

**The package labels are the concrete set, not a copy of the package's.**
The declaration a package carries is abstract (§5.1): the carrier cannot
state its own hash, and the tools entry names the family because the other
platforms' bytes differ on purpose. An image is a delivery of exact bytes,
so the script hashes both archives and labels the image with

```
org.mcuhome.build-environment.packages.mcuhome-build-workspace=<version>@sha256:…
org.mcuhome.build-environment.packages.mcuhome-build-tools_linux-amd64=<version>@sha256:…
```

— the carrier's entry completed, and the family replaced by the one
platform's package the image really contains. It refuses before building if
the archives are not the set the declaration names, or if a hash the
declaration already stated does not match the bytes it was handed.

## Publishing one

The published image is built by CI, from the **published** packages — never
pushed from a workstation. A locally assembled image pins the hashes of
locally built archives, and those are not the bytes the registry serves; an
image that labels package hashes no index names is one no orchestrator can
match. A local build is for testing, and it stays local.

```sh
gh workflow run release.yml -f tag=v0.1.0 -f image=true -f revision=1
```

That is the `Release` workflow's image dispatch. It runs on nothing else —
no tag push publishes an image, because at tag time the packages it is
assembled from are not published yet, and CI cannot observe the registry
operator's publish. `image` also makes the run an image run alone: no
package job builds beside it.

Per architecture, on a runner of that architecture: the three consumed
sources (`sdk`, `build-workspace`, `build-tools`) are discovered from
`packages.mcuhome.org`, their served documents are verified with
`mcuhome-packagetool`'s reference verifier against the registry's trust
anchor, the packages are downloaded from the verified mirror, each archive
is checked by size and sha256 against the index bytes the verifier
accepted, the image is assembled by the script above and pushed as
`<version>-r<n>-<arch>`. A last job composes the OCI index over the two and
pushes it as `<version>-r<n>`, which is the reference a client resolves —
`amd64` and `arm64` both, because a Home Assistant box is an arm64 machine.

**The tag names the SDK release, not the environment.** Which versions of
the two packages get assembled is read out of that release's
`build-environment.lock.json`, which the run takes from the verified SDK
archive — the tools package is on its own counter and a release that did
not change the toolchain names an older one, so assuming the tag's version
for both would ask the index for a tools package that was never built. The
workspace version the lock states must be the SDK's own (that package is
built from this tag) and the run refuses when it is not; the tools version
is then resolved through the family's `meta.arch` map to this
architecture's package. It is the same document, and the same answer, a
workbench provisioning that SDK gets.

**The anchor arrives out of band**, as the organisation variable
`MCUHOME_REGISTRY_ANCHOR` (the content of `mcuhome-packagetool`'s
`deploy/mcuhome/anchor.json`). Without it the job refuses: an image whose
packages were only checked against a document from the same host they came
from has been checked against nothing much, and skipping that quietly would
be worse than failing.

`revision` is the `-r<n>` counter and starts at 1. Raise it when the same
packages are assembled again, for a new base image or a changed
`Dockerfile`: a **per-architecture** tag that already exists in the registry
is skipped with a notice rather than reassembled, so the counter is the only
way to publish a second assembly of one package set. (The index over the two
is composed and pushed either way — it names whichever manifests those two
tags hold, which is why a re-dispatch must not change one half of a set.)

The release runbook, and where this step sits in it, is
[`RELEASING.md`](../../RELEASING.md).

## What is in it

```
/opt/mcuhome/build-environment/
  workspace/                   the mcuhome-build-workspace package, unpacked
    build-environment.json     the §5 declaration the labels mirror
    workspace/                 the west workspace — never written, see below
    matter-pregen/             the pre-generated Matter data model
  tools/                       the mcuhome-build-tools package, unpacked
    venv/                      created here, offline, from tools/wheels
/mcuhome/
  bin/build-environment-entry  a relative link into the tools package
  work/ out/ sdk/ build-context/ cache/{local,session,project,shared}
                               empty mount points for the orchestrator
```

Everything under `/opt` is the environment's own content, which §4 leaves
entirely to the environment; the two paths are stated to the builder in
`MCUHOME_BUILD_ENV_TOOLS` and `MCUHOME_BUILD_ENV_WORKSPACE`, because the
entry point can check those but not derive them.

`PATH`, `VIRTUAL_ENV`, `ZEPHYR_SDK_INSTALL_DIR` and
`ZEPHYR_TOOLCHAIN_VARIANT` are deliberately *not* in the image: the entry
point sets them, so that both profiles get the same environment out of the
same file.

**The west workspace is readable by every user**, because the
orchestrator chooses the UID a step runs as and the image cannot know it.
It is world-*writable* as well, and that is now a leftover rather than a
requirement: §10 would allow this profile's disposable trees to be patched
in place, but the builder does not take that permission — it assembles a
view of the workspace under `work` and patches copies inside it, in every
profile alike (SDK design section 5). Nothing in the image is written by a
step any more.

**No network is needed at run time, and none at build time beyond the base
distribution.** No `west update`, no source tree fetched, no index reached:
the wheel set installs with `--no-index` out of the tools package. A step
runs fine with `--network none`.

## Running a step by hand

The orchestrator does this; by hand it looks like:

```
docker run --rm --network none \
    --user "$(id -u):$(id -g)" \
    --env MCUHOME_BUILDER_BASE_DIR=/ \
    --volume "$PWD/request.json:/mcuhome/invocation-request.json:ro" \
    --volume "$PWD/sdk:/mcuhome/sdk:ro" \
    --volume "$PWD/context:/mcuhome/build-context:ro" \
    --volume "$PWD/out:/mcuhome/out" \
    ghcr.io/mcu-home/build-environment:<version>-r<n>
```

`MCUHOME_BUILDER_BASE_DIR` is `/` in this profile, the entry point takes no
arguments, and the answer is `out/result-<invocation_id>.json` (§6). The
SDK is **not** environment content: each build context pins its own, and
the orchestrator delivers it at `mcuhome/sdk`.
