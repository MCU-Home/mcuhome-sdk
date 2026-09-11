# packaging/build-environment/

MCUHome's own **build environment**: the two packages that satisfy the
[build environment specification](../../docs/spec/build-environment-specification.md)
(generation 3), and the entry point they are entered through. How and why
they are cut this way is the
[build environment design](../../docs/design/build-environment.md); this
file is the layout — what is in each archive, at which path, and who reads
it.

Both packages are built by [`scripts/build_env_package.py`](../../scripts/build_env_package.py),
deterministically: two builds of one revision produce byte-identical
archives. It runs the steps that cannot be done on an arbitrary host
inside the **packager image**
([`containers/build-environment-packager/`](../../containers/build-environment-packager/README.md)),
which carries the same `west` the environment does, the `zap` the pinned
CHIP revision names, and the interpreter the wheel set has to fit. The
container image that *delivers* the packages — the specification's
container profile — is assembled from the archives by
[`scripts/build_env_image.py`](../../scripts/build_env_image.py); see
[`containers/build-environment/README.md`](../../containers/build-environment/README.md).

| Entry | Role |
|---|---|
| `environment.json` | The definition file of this repository's three release lines — the SDK, the workspace package and the tools package. See below |
| `build-environment-entry` | The entry point of specification §4/§6. Ships in the tools package; the profile in use puts it at `$MCUHOME_BUILDER_BASE_DIR/mcuhome/bin/build-environment-entry` |
| `requirements.txt` | The environment's Python dependency set, pinned transitively. Not installed from here: a wheel is built for every line and the wheel set ships in the tools package |
| `workspace-record.py` | Writes `workspace.json`, the record of what west resolved, into the workspace package. Run once per package build, inside the packager image |

## `environment.json` — the three release lines

The SDK, the workspace package and the tools package are versioned
**independently**, and this file is where all three are declared: the
version each line's next release carries, and the PEP 440 constraint a
line puts on the one below it.

```json
{
  "sdk":       { "version": "…", "requires": { "mcuhome-build-workspace": "~=0.1.0" } },
  "workspace": { "version": "…", "requires": { "mcuhome-build-tools": "~=0.1.0" } },
  "tools":     { "version": "…" }
}
```

A chain and not a matrix: each stage accepts a *range* of the next, a
build resolves that to the newest published version satisfying it and
pins it exactly, and the tools end the chain. Independent versions are
what stop a toolchain that did not move from being republished for every
SDK release. The recommended constraint shape is `~=X.Y.Z` — patch-level
float.

Nothing else writes a version down. `mcuhome.model.__version__` is
derived from `sdk.version`: a checkout reads this file, and a built
distribution reads the generated `mcuhome/model/VERSION` the build wrote
beside the module — which is why that file is in `.gitignore` and never
committed. The tags follow the lines: `v<version>` releases the SDK,
`workspace-v<version>` the workspace package, `tools-v<version>` the
tools packages, and a tag must equal the version this file declares for
its line.

## The meta file

Every package carries `meta.json` at the top of its archive and
`<archive file name>.meta.json` beside it — the same bytes, written once,
so the copy a reader happens to take cannot decide what the package is.
Inside, because an unpacked store entry has to be able to say what it is
with nothing else present; beside, because whoever resolves a release
chain reads what a package requires *before* fetching a gigabyte of it.

```json
{
  "schema": 1,
  "package": { "name": "mcuhome-build-workspace", "version": "…", "architecture": null },
  "requires": { "mcuhome-build-tools": "~=0.1.0" },
  "inputs_sha256": "…",
  "contents": { "projects": { … }, "patches": [ … ], "environment": { … } }
}
```

`package.architecture` is `null` for an architecture-neutral package and
`<os>-<arch>` for a per-platform one, whose `package.name` is then the
*family*: the concrete name is the two joined, and stating it twice would
let them disagree. `requires` is **absent** for the tools package, which
ends the chain — "any version" and "this one forgot to say" look
identical otherwise.

`inputs_sha256` identifies what the package was built from: a SHA-256
over a canonical listing of the repository paths that stage is built
from, each with the git object the packaged commit gives it, plus the
packager image it is produced in and, for the tools, the architecture.
`scripts/release_lines.py` computes it, needs no build, no container and
no network, and answers for any commit — which is what lets a push check
whether a stage changed without a release version being bumped for it.

`contents` is what the package resolved to: for the workspace every west
project with the revision the manifest pinned and the commit it resolved
to, the patch files applied, and the §5 declaration it carries; for the
tools the toolchain, CMake, Ninja, gn, the interpreter and west, with the
architecture.

Beside the archives, `index.json` records both files per `(name,
version)`: the archive under `file`/`sha256`/`size`, and the meta sidecar
under **`meta_file`** with the same three fields — the name a package
registry's index uses for it, so a reader finds it identically in an
operator's directory and on a host. It is not called `meta`: in that index
`meta` already means "this entry is a meta package", the family that maps
platforms onto concrete packages.

The meta file and the §5 **declaration** below are two documents and stay
two: the declaration is what the *specification* asks a build environment
for and what an image mirrors into its labels, the meta file is what
MCUHome's own release chain asks a *package* for.

## `mcuhome-build-workspace`

Architecture-neutral, one per change to its own inputs. It is the
package that carries the environment's **self-description** (specification
§5) — a package set that spans architectures needs a carrier that does not.

```
meta.json                      what this package is, requires and contains
build-environment.json         the §5 declaration, for the whole set
build-workspace.json           what this package is and where its parts are
workspace.json                 the record of what west resolved
workspace/                     the west workspace top directory
  .west/config                 manifest.path and zephyr.base, pre-populated
  zephyr/  modules/  bootloader/
  mcuhome-sdk/                 present and EMPTY — the SDK mount point
matter-pregen/                 the pre-generated Matter data-model code
```

The declaration is written **twice, from one document**: at the top of the
archive as above, and beside the archive in the output directory as
`<archive file name>.build-environment.json`. Inside, because an unpacked
store entry has to be able to say what it is with nothing else present;
beside, because the orchestrator reads the declaration *before* it starts
anything — and before it starts anything it has an archive and not a tree.
The sidecar is named for the file rather than for the package, like the
`.sha256` beside it: a source directory holds more than one version at a
time.

It states the **abstract** package set specification §5.1 asks a package's
own metadata for — one `packages.<name>` member per package, carrying a
version and no hash:

```json
"packages.mcuhome-build-tools": "1.2.0",
"packages.mcuhome-build-workspace": "2.4.0"
```

Neither hash can be stated here. The carrier's own would have to cover the
bytes that contain it, and the tools family's siblings are separate
archives that may not exist yet when this one is packed — which is also why
the tools member names the family and not one platform's package. Whoever
delivers exact bytes completes it: the image assembly hashes both archives
and labels the image with the concrete set, family entry replaced by the
`mcuhome-build-tools_<os>-<arch>` package it contains.

`workspace/` is a real west workspace at the revisions `west.yml` pins,
fetched narrow and shallow, with `patches/` applied as working-tree changes,
the tag refs fetched back, and the Espressif HAL blobs fetched. `.git` is
kept on purpose: west resolves the Zephyr `import:` out of
`refs/heads/manifest-rev`, and Zephyr's version stamping shells out to
`git describe` — stripping it would break both and change the firmware.

`zephyr.base` is pre-populated so that no lazy configuration write ever
happens: west's Zephyr extension writes it on first use, and a frozen store
has nowhere to write.

`mcuhome-sdk/` is empty because the SDK is **not** part of the environment
(specification §2): each build context chooses its own SDK version, and the
orchestrator delivers it at `mcuhome/sdk`.

### The pre-generated Matter code

Generating the Matter data model needs `zap`, which is distributed only as
an Electron application and needs a desktop shared-library set no lean host
baseline should carry. MCUHome's input is a single static root-node
configuration that changes only with a release, so its output is a release
constant and is generated here instead. `zap` is deliberately absent from
the tools package.

CHIP's build glue indexes its pre-generation directory by the path of the
data-model file **relative to `CHIP_ROOT`**
(`build/chip/chip_codegen.cmake`). MCUHome's data model lives in the SDK,
outside CHIP, so that relative path climbs out of the CHIP tree and back
down into the manifest repository's directory. `matter-pregen/` is therefore
laid out as a **shadow of the workspace**:

```
matter-pregen/
  modules/lib/connectedhomeip/          empty, and there on purpose
  mcuhome-sdk/components/matter/zap/mcuhome-root/
    codegen/cpp-app/...                 from the .matter IDL
    zap/app-templates/zap-generated/... from the .zap
```

and a build is handed the *shadow's CHIP root* as `CHIP_CODEGEN_PREGEN_DIR`:

```
CHIP_CODEGEN_PREGEN_DIR=<package root>/matter-pregen/modules/lib/connectedhomeip
```

Then "relative to `CHIP_ROOT`" means the same thing in the shadow as in the
workspace, and nothing has to agree about how deep anything is. The value is
stated in `build-workspace.json` as `matter-pregen-chip-root`, so a consumer
reads it rather than reconstructing it.

The shadow's CHIP root is an **empty directory the package carries on
purpose**. The include path the compiler is handed keeps the `..` in it, and
the kernel resolves `..` by walking rather than by rewriting the string:
every component in front of it has to exist, or the whole path is `ENOENT`
and the first pre-generated header is simply not found.

## `mcuhome-build-tools_<os>-<arch>`

One per platform, per toolchain generation.

```
meta.json                      what this package is and contains
build-tools.json               what this package is, and every pin in it
bin/build-environment-entry    the entry point
cmake/bin/cmake                CMake, from the upstream release tarball
ninja/ninja                    Ninja, from the upstream release zip
gn/gn                          gn, from the CIPD package
zephyr-sdk-<version>/          the minimal Zephyr SDK plus one toolchain
wheels/*.whl                   the complete Python set of a build
venv/                          created by provisioning, not by the package
```

**Why wheels and not a ready virtual environment.** Virtual environments are
not relocatable — absolute interpreter paths, absolute shebangs — so
provisioning creates one at its final location, offline, from `wheels/`.
Everything is built to a wheel beforehand because some transitive
dependencies ship as sdists, and building those during provisioning would
be fragile on a host with no network and no compiler.

**Why the tools come from their projects' own releases.** The package sets
the minimum host baseline for the subprocess profile — glibc ≥ 2.28, from
the Zephyr SDK toolchain. A binary copied out of the Debian trixie base
image would be linked against trixie's glibc and raise that floor by
thirteen releases for two tools that have nothing to do with it. The package
build measures instead: it reads the versioned glibc symbol references out
of every *host* binary it packs and refuses to produce a package whose
highest one is above the baseline. For the versions pinned today the highest
is the Zephyr SDK's own linker, and nothing else comes close to it.

## The entry point

Specification §4 fixes the path and §6 fixes the invocation: no arguments,
the request document at `mcuhome/invocation-request.json`, the result
document at `mcuhome/out/result-<invocation_id>.json`.

`build-environment-entry` sets the environment up — the build virtual
environment first on `PATH`, then CMake, Ninja and gn, plus
`ZEPHYR_SDK_INSTALL_DIR` and `ZEPHYR_TOOLCHAIN_VARIANT` — and hands over to
`mcuhome.compiler.abi`, which arrives with the SDK and is therefore not
environment content. It finds the two packages through two environment
variables, each falling back to what it can work out on its own:

| Variable | Meaning |
|---|---|
| `MCUHOME_BUILD_ENV_TOOLS` | The unpacked `mcuhome-build-tools_<os>-<arch>` package. Defaults to the parent of the directory the entry point was run from |
| `MCUHOME_BUILD_ENV_WORKSPACE` | The unpacked `mcuhome-build-workspace` package |

Both are checked against the package's own `build-tools.json` /
`build-workspace.json`, so a wrong value fails immediately and says what was
expected, rather than half-way through a build.
