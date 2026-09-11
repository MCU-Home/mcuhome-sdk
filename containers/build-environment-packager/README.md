# containers/build-environment-packager/

The **toolchain MCUHome's build-environment packages are produced in** —
not a build environment, and nothing a device is ever compiled in.

[`scripts/build_env_package.py`](../../scripts/build_env_package.py)
produces the two packages
[`packaging/build-environment/`](../../packaging/build-environment/README.md)
describes, and three of its steps cannot be done correctly on an arbitrary
host. This image is exactly what those three need and nothing else:

| Step | What it needs from here |
|---|---|
| laying the environment's west workspace out | the exact `west` the environment itself carries — one west lays a workspace out, the same west reads it later |
| pre-generating the Matter data model | the `zap` the pinned CHIP revision names, an Electron application with a desktop library set no lean build environment carries |
| building the wheel set | the interpreter the environment runs on, because the interpreter that builds a wheel decides which interpreter can install it |

So: `debian:trixie-slim` at a pinned digest, plus `python3`,
`python3-venv`, `git`, `ca-certificates`, `west`, the four Python packages
CHIP's generators import, and `zap` with its Electron libraries. No Zephyr
SDK, no CMake, no Ninja, no gn, no ccache, no baked workspace, no entry
point.

The image a device is built in is
[`containers/build-environment/`](../build-environment/README.md), which is
an assembly of the two packages produced here.

## What it is called, and what pins it

`ghcr.io/mcu-home/build-environment-packager`, tagged
`<sdk version>-r<n>`:

* the version part is the first SDK version that needs this packager —
  "for SDKs from this version on". There is no alias tag per release; an
  SDK whose packager did not change keeps naming the older tag.
* `-r<n>` counts rebuilds with the same tool set: a base refresh, a
  security fix, anything that changes the bytes without changing what the
  image is. It starts at 1 and is bumped by whoever changes this
  directory, in the same commit.

> **A tag is a location, not an identity.** What
> [`scripts/packager_image.py`](../../scripts/packager_image.py) hands to a
> package build is `<repository>@sha256:…` — an image that changed under a
> stable name could never be the reason two package builds agree. The tag
> exists for the job that publishes the image, which has to address bytes
> that do not exist yet.

`scripts/packager_image.py` is the one place in this repository that names
this image: the packaging script runs it, `ci-build.yml` builds and
publishes it, `release.yml` pulls it. Nothing restates the reference.

## What it declares about itself

Four labels, each a pin somebody downstream may need to compare, and each
verified against the built image by the last step of the `Dockerfile` — a
label that could drift from the bytes would be worse than no label:

| Label | Value |
|---|---|
| `org.mcuhome.build-environment-packager.base` | the digest-pinned `debian:trixie-slim` reference the image is built from |
| `org.mcuhome.build-environment-packager.python` | the interpreter version that decides the wheel set's ABI |
| `org.mcuhome.build-environment-packager.west` | the west version the environment's workspace is laid out by |
| `org.mcuhome.build-environment-packager.zap` | the zap release the Matter data model is generated with |

Plus the usual OCI `title`, `description`, `source` and `licenses`.

```sh
docker image inspect ghcr.io/mcu-home/build-environment-packager:<tag> \
    --format '{{json .Config.Labels}}'
```

## Building one

The context is **this directory** — everything the image needs is in it:

```sh
docker build -t ghcr.io/mcu-home/build-environment-packager:<tag> \
    containers/build-environment-packager
```

CI does it per architecture and composes an index over the two
(`.github/workflows/ci-build.yml`, `build-packager` and
`publish-packager-index`). Both architectures exist because the wheel set
is per architecture and has to be built natively on each.

To try a change before it is published, build it locally and point one
package build at it:

```sh
scripts/build_env_package.py workspace --output-dir dist \
    --packager-image <the local image>
```

A package built that way is not one anybody else can reproduce, which is
why the override is a flag and never a fallback.

## Changing it

1. Change the `Dockerfile` or `requirements.txt`.
2. Bump `-r<n>` in `scripts/packager_image.py` — or the version part, if
   the change is what makes a new SDK version need a new packager.
3. Clear `DIGEST` in the same file: the new tag has no digest until it is
   published, and a stale digest would silently keep the old image in use.
4. Push. CI publishes the new tag and prints the index digest.
5. Write that digest into `scripts/packager_image.py`.

The west pin is the one that must not drift on its own: it has to equal
the build environment's own west
(`packaging/build-environment/requirements.txt`), and
`tests/python/test_env_package.py` asserts every line of this directory's
`requirements.txt` against that file.
