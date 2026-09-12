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

This is the only image a MCUHome build runs in. The repository builds one
other, [`containers/build-environment-packager/`](../build-environment-packager/README.md),
and no device is ever compiled in that one: it is the toolchain
`scripts/build_env_package.py` *produces* the two packages with, because
laying the workspace out needs the exact `west` it is later read by and
pre-generating the Matter data model needs a `zap` no lean build
environment carries.

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

The published image is built by CI, never pushed from a workstation. A
locally assembled image pins the hashes of locally built archives, and those
are not the bytes a release published; an image whose package labels no
index names is one no orchestrator can match. A local build is for testing,
and it stays local.

**A build workspace release assembles its own `-r1`.** The `Release`
workflow does it in the same run as the release, right after the archives
are attached: one leg per architecture, each taking the workspace package
this run just built and the build tools release that package's own
`meta.json` accepts, and a last job composing the OCI index over the two
and pushing it as `<version>-r<n>` — the reference a client resolves.
`amd64` and `arm64` both, because a Home Assistant box is an arm64 machine.

**Every other image is a revision dispatch:**

```sh
gh workflow run release.yml -f workspace_version=0.1.0 -f revision=2
```

Three reasons to raise `-r<n>`: a refreshed Debian base, a change to the
`Dockerfile` here, or a build tools release the workspace package accepts
and that should reach users without a new workspace release. It is assembled
from the release assets of `workspace-v<version>` and the tools release
resolved the same way, and it is verified the same way.

**Nothing here asks the package registry.** The registry is fed by hand by
its operator after a release, so an image assembled from it would either
wait days or pin bytes that are not served yet; what the image is assembled
from is the GitHub release of this repository, which is where the archive
and its checksum already are.

**The tag names the workspace package, not the SDK release.** The three are
release lines of their own — `v<version>` releases the SDK,
`workspace-v<version>` the build workspace, `tools-v<version>` the build
tools — and an image delivers one workspace package plus the tools that
package accepts, so its tag is `<workspace package version>-r<n>`. Which
tools version that is comes from the workspace package's own `meta.json`:
it states a PEP 440 constraint, and the run takes the newest published
version satisfying it. It is the same document, and the same answer, a
workbench provisioning that workspace package gets.

**An existing index tag is a refusal.** `<version>-r<n>` is the content
identity and `-r<n>` is the counter that exists for a second assembly of one
package set, so a run that finds that tag taken stops rather than
overwriting it. A **per-architecture** tag that already exists is left as it
is and the run carries on: those bytes are published, an assembly is not
bit-reproducible, and the index below is composed from them. A dispatch
whose per-architecture images went up but whose index did not is therefore
repeated with the same revision.

**And then it is verified.** `verify-release` builds the reference Matter
device in this image, addressed by the digest that was just pushed, on both
architectures, with the release's packages as local sources. The workbench
holds the image's `packages.` labels against the package set the context
resolved and refuses to build in an image that does not declare exactly it
— which is what makes that a verification of the image and not just another
build.

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
    ghcr.io/mcu-home/build-environment:<version>-r<n> \
    /mcuhome/bin/build-environment-entry
```

`MCUHOME_BUILDER_BASE_DIR` is `/` in this profile, the entry point is named by
the path §6 fixes and takes no arguments — the image declares it as its `CMD`
as well, but an orchestrator runs it by path, because that is what the
specification fixes — and the answer is `out/result-<invocation_id>.json` (§6). The
SDK is **not** environment content: each build context pins its own, and
the orchestrator delivers it at `mcuhome/sdk`.
