# MCUHome's build environment

How MCUHome builds, distributes, and runs its own build environment. The
boundary itself — what any build environment must provide, the step
model, the request and result documents, caches, patches — is fixed by
the [build environment specification](../spec/build-environment-specification.md)
(spec generation 3) together with the
[build actions](../spec/build-actions.md) and the
[build context format](../spec/build-context-format.md). This document is
the other side: how MCUHome's own environment satisfies that
specification, and how MCUHome's orchestrator provides what the
specification promises an environment.

Status: design accepted; the feasibility findings in section 9 are folded
in.

## 1. The three layers

1. **Source** — the `mcuhome-sdk` repository: `west.yml` (the pinned
   dependency world), the base patches, the code generator, the C
   runtime. The single authority on what the environment packages
   contain.
2. **Packaging** — turns the source definition into the environment's
   package set and, derived from it, the container image.
3. **Execution** — the orchestrator (in `mcuhome-workbench`; the build
   server reuses it) runs steps against the environment in one of the
   specification's two profiles.

Build targets (local machine vs. remote build server) are orthogonal:
a remote build travels to the build server, which runs the same local
execution path. Only the profile differentiates how a step is entered.

## 2. The packages

Per the specification's package-set model, MCUHome's environment is two
packages, distributed like the SDK package through packagetool
(hash-pinned, signed index, mirrorable — packages.mcuhome.org):

- **`mcuhome-build-workspace`** (arch-neutral, one per change to its own
  inputs — `west.yml`, the patch set, the Matter data model): the
  materialized west workspace — Zephyr, modules,
  MCUboot, Matter SDK incl. submodules — with base patches applied,
  binary blobs fetched, west configuration pre-populated
  (`zephyr.base`, so no lazy config write ever happens on read-only
  trees), the pre-generated Matter data-model code, and the workspace
  record of resolved commits and patch hashes. Carries the environment's
  self-description (specification §5).
- **`mcuhome-build-tools_<os>-<arch>`** (one per platform, per toolchain
  generation): Zephyr SDK toolchain, cmake, ninja, west, gn, the Python
  wheel set for the build venv, and the entry point. cmake and ninja must
  satisfy the host baseline (section 6), and no upstream release binary is
  taken on trust — recent upstream ninja binaries require glibc 2.38 and
  would silently raise the floor. The pinned ones are measured instead,
  together with everything else in the package: the package build reads the
  versioned glibc symbol references out of every host binary it packs and
  refuses to produce a package whose highest one is above the baseline.
  Building from source is the fallback when no release binary passes; for
  the versions pinned today none does worse than the toolchain itself.

**How the platform is resolved.** The per-platform tools packages are
published individually and, in addition, as a *meta* package under the
family name `mcuhome-build-tools`: its index entry maps each `<os>-<arch>`
to the concrete package name and carries a hash derived from the hashes of
the packages it points at. A build context pins the meta package, and
whoever executes it resolves its own platform through that entry — so one
context stays portable and still pins exact bytes, because the meta hash
covers every platform's package. Pinning a concrete per-platform package
directly remains possible for an architecture-targeted build; a host of
another platform then refuses legibly instead of substituting something.

**Why the venv ships as wheels, not as a ready venv.** Python virtual
environments are not relocatable (absolute interpreter paths, absolute
shebangs — verified: a moved venv fails with "bad interpreter" while a
venv created in place works at any depth). Provisioning creates the venv
at its final location, offline. The bundled set is built entirely to
wheels beforehand (`pip wheel`) — some transitive dependencies (e.g.
west's docopt) ship as sdists by default, and building those during
provisioning would be fragile.

**Why Matter code generation happens at package build time.** The zap
tool that turns the Matter data-model configuration (`.zap`) into
generated C++ and `.matter` IDL is distributed only as an Electron
application — even headless it needs a desktop shared-library set no
lean host baseline should carry. MCUHome's zap input is a single static
root-node configuration that changes only with a release (device
endpoints are registered dynamically at runtime), so its output is a
release constant. The workspace package build runs zap and the Matter
codegen once (CHIP's pre-generation mechanism, `codepregen.py`) and
ships the output in the package; builds consume it via
`CHIP_CODEGEN_PREGEN_DIR`, which makes both tools optional at build
time. zap is deliberately absent from the tools package.

**The SDK is not part of the environment** (specification §2): each
build context pins its own SDK package, and the orchestrator delivers it
at `mcuhome/sdk`.

### Three release lines, and what identifies them

The SDK, the workspace package and the tools package are **versioned
independently**. `packaging/build-environment/environment.json` declares
all three — the version each line's next release carries, and the PEP 440
constraint a line puts on the one below it:

```json
{
  "sdk":       { "version": "…", "requires": { "mcuhome-build-workspace": "~=0.1.0" } },
  "workspace": { "version": "…", "requires": { "mcuhome-build-tools": "~=0.1.0" } },
  "tools":     { "version": "…" }
}
```

A chain, not a matrix: each stage accepts a range of the next, resolution
takes the newest published version satisfying it and pins that one
exactly, and the tools end the chain. No artifact takes another one's
version — which is what stops a toolchain that did not move from being
republished for every SDK release, and stops an SDK that only changed a
generator from claiming a new workspace.

Every package states this in its own `meta.json` (inside the archive and,
byte for byte the same document, beside it): what it is, what it
requires, **the hash of its inputs**, and what it resolved to. The input
hash is computed over the repository paths that stage is built from, each
with the git object the commit gives it, plus the packager image the
package is produced in and, for the tools, the architecture — so "did
this stage change" is a question anybody can answer for a commit they
never built. A cosmetic change to the packaging script moves it, because
nothing can tell a comment from a behaviour by reading a file; that is the
safe direction.

## 3. Release chain

```
packager image published from main (containers/build-environment-packager/;
  west, zap, the base interpreter — pinned by digest, changes rarely)
git tag per line — v<version>, workspace-v<version>, tools-v<version>
  → the tagged line's package(s), built in the packager: deterministic,
    with the version environment.json declares for that line, the input
    hash, and meta.json on both sides of the archive
  → packagetool publication (manually dispatched — one deliberate
    publish act; downstream steps may then chain automatically)
  → container image assembled from a published workspace package and the
    newest published tools its constraint accepts
```

The packager is the bootstrap of this chain and deliberately outside it:
the packages cannot be produced on an arbitrary host — the workspace has
to be laid out by the west that later reads it, the Matter pre-generation
needs a `zap` no build environment carries, and the wheel set has to be
built by the interpreter the environment runs on. It carries nothing else,
compiles nothing, and is pinned by digest at one place in the repository
so that the packages a release publishes name the exact bytes they were
produced in.

The image is a thin assembly: a base providing the host baseline
(section 6), the two packages unpacked, provisioning finalization baked
in, and the specification's labels mirroring the packages' declaration —
including one `packages.<package name>` label per package, which is what
the orchestrator matches images by. An image states the *concrete* set
(specification §5.1): every one of those labels carries the archive's
hash, and the tools family is named as the one platform's package the
image actually contains.

## 4. The container profile

The build context references the package set (context format v4); the
orchestrator resolves the tools pin to this host's platform, looks for a
container image whose `packages.` labels declare exactly the resulting
set, checks it against the operator's repository allowlist,
and starts **one fresh container per step** — which is how the
specification's pristine-tree guarantee is met for free. Mounts provide
`mcuhome/` (request document, sdk, build-context, out, cache tiers);
`MCUHOME_BUILDER_BASE_DIR` is `/` here. The image's baked workspace is
disposable and the specification would allow patching it in place; the
builder does not take that permission and views it like any other
workspace (section 5), so this profile pays the view's cost too — and in
a container that cost is the mirrored layers' **bytes**, because `work`
is the container's writable layer and the workspace a layer below it
(measured, section 9). This profile accepts that cost: `work` stays in
the container's writable layer, the builder is unchanged, and the
launcher mounts exactly the tree specification §4 defines and nothing
else. Measured, the view of all three mirrored layers costs 12.0 s and
587 MB of writable layer per step (392 MB for the `zephyr` layer alone)
where the kernel copies a file's metadata up only, and about 1.3 GB
where it copies the bytes; the container is discarded at the end of the
step, so this is throughput cost and not growth. Every cheaper
arrangement measured either hands the step an environment it can write
and its successors inherit, or asks the orchestrator to know how the
image and the builder are built inside (section 9). The public build
server runs step containers without network as operator policy; a
private operator may relax that. Which cache tiers exist, and what backs
them, is the operator's decision (specification §8).

**What the side that builds the environment may rely on.** The
specification is the whole of it. An environment declares its package
set and its entry point; the orchestrator delivers the tree of
specification §4 and starts a step. Layer names, west paths, where an
image keeps its workspace, where the builder assembles its view are
internal to the environment and to the builder, and nothing on the
orchestrating side may act on them — MCUHome's own image and MCUHome's
own builder happen to be built the way this document describes, which
makes the shortcut available, not legitimate. The boundary is not
tidiness: it is what lets an environment with no west workspace and no
Zephyr in it at all — an ESP-IDF one, say — be offered later behind the
same orchestration. It binds both profiles equally, and the subprocess
profile's provisioner may assume no more about a store than the
container launcher may about an image.

## 5. The subprocess profile

For hosts where containers are unavailable (inside an unprivileged
container, for instance) and for builds the operator already trusts.
**This profile isolates nothing** — the builder runs as an ordinary
process; a build server accepting contexts from other parties must use
the container profile. The build context is untrusted input: it carries
patches, which are code.

The provisioner (workbench component) turns packages into a runnable
environment:

1. **Resolve** the context's package references; **fetch** through the
   tiered sources (local operator directories first, then
   packages.mcuhome.org) and verify hashes and signatures — the same
   verified-acquisition path the SDK package already uses, generalized.
2. **Unpack** into the environment store:
   `${XDG_CACHE_HOME:-~/.cache}/mcuhome/build-environments/<package-name>-<version>/`
   — always under the user's home, never a system path, overridable via
   `MCUHOME_BUILD_ENV_STORE`.
3. **Finalize** (once per store entry): create the build venv from the
   bundled wheels (offline), toolchain setup, `git safe.directory` for
   the workspace trees.
4. **Freeze**: mark the entry read-only. From then on it is immutable
   and shared by any number of concurrent builds.

Per step, the orchestrator lays out fresh `mcuhome/` directories (work,
out per session, cache tiers as configured), writes the request
document, sets `MCUHOME_BUILDER_BASE_DIR`, and executes the entry point
from the store. The read-only store plus fresh per-step directories is
this profile's implementation of the pristine-tree guarantee; when a
context carries patches, the environment materializes patched copies
under `work` (specification §10) — the copy cost is paid only for
patched trees, and overlay filesystems are deliberately not relied on
(they are frequently unavailable exactly where this profile is needed).

**One path in the builder, and it is the view.** Specification §10
permits either behaviour — a disposable tree may be patched in place, a
read-only store must not be touched — and MCUHome's builder takes only
the second: every step assembles the view under `work` (section 9) and
builds in it, whatever the environment's trees are. Nothing in the
builder asks which profile is running, because nothing in it behaves
differently. The alternative it replaces was a write probe on the
workspace: it answered for the filesystem rather than for the owner, so
a store readable by root and a west workspace somebody is working in
both came out "disposable" — and were patched in place and had the
manifest repository's directory replaced with a symbolic link into a
build directory that is gone at the end of the step. What the single
path costs is the view on every step of every profile; what it buys is
that the builder never writes into a tree it does not own, and one code
path to test instead of two that differ by a probe.

## 6. Host baseline

The subprocess profile runs the packaged tools on the host, so the
tools package defines a minimum host baseline. With zap out of the
runtime (section 2), the floors are set by the Zephyr SDK toolchain and
the Zephyr build system — except for Python, which is not a floor but an
exact requirement:

| Dimension | Requirement | Set by |
|---|---|---|
| glibc | ≥ 2.28 | Zephyr SDK prebuilt toolchain (gn needs only 2.18) |
| Python | exactly the minor the wheel set targets — the current Debian stable's, 3.13 today | tools package's wheel set (Zephyr v4.4's build scripts only need ≥ 3.12, a lower floor) |
| CMake | ≥ 3.20 | Zephyr v4.4 (4.5+ raises this to 3.28) |
| dtc | ≥ 1.4.6 | Zephyr build system |
| Architecture | x86_64, aarch64 (Linux) | all prebuilt tool sources |
| libc family | glibc only, no musl | Zephyr SDK prebuilt toolchain |

Compiled wheels install only into the minor they were built for, so the
subprocess profile needs that minor and no other; the provisioner checks
the host interpreter against the wheel set before it creates the build's
virtual environment and refuses legibly, naming the version it needs.
Nothing is downloaded or compiled to paper over a difference. The
container profile never sees the question, because the base image
already carries that Python.

The container base image (Debian trixie: glibc 2.41, Python 3.13,
CMake 3.31, dtc 1.7) satisfies every line of the table — the container
is simply a host that always qualifies.

## 7. Compiler cache

The orchestrator provides the specification's cache tiers; inside them,
MCUHome's environment uses `<tier>/ccache`. Zephyr embeds absolute paths
in every compile invocation, so hit rates follow path stability:

- **Container profile**: identical in-container paths every step — one
  shared cache works across machines and projects.
- **Coverage caveat**: Zephyr's CMake picks ccache up automatically, but
  the Matter SDK's inner GN build invokes the compiler directly — it
  must be routed through ccache explicitly (compiler-launcher wiring in
  the GN args), or the most expensive objects never hit the cache.
- **Subprocess profile**: the store path is stable per machine; varying
  per-step build directories are normalized with `CCACHE_BASEDIR` set to
  the parent of the build directories (it need not cover the store) plus
  `hash_dir = false` — without the latter, debug builds (`-g`) hash the
  working directory and always miss. Verified: with both set, a compile
  from a previously unseen build directory is a full direct-mode hit.
  Accepted trade-offs: the working directory is not part of the hash for
  debug info (harmless — build paths are prefix-mapped anyway), and
  cross-machine cache portability exists only in the container profile.

## 8. Migration outline

1. **Design** — the v3 specification set and this document. Complete.
2. **Packages** — package build scripts, CI jobs, packagetool sources,
   the thin image with the mirrored declaration labels; the Matter
   pre-generation and its validation against the pinned CHIP version (a
   pre-generation bug under Ninja is on record upstream,
   project-chip/connectedhomeip issue 39787; if it affects the pinned
   version, MCUHome carries a simple local patch — plain workaround,
   allowed to hardcode MCUHome's pre-generation usage — until upstream
   fixes it cleanly); the builder program aligned to the specification's
   invocation (no arguments, fixed request path). Done. The old
   monolithic image is gone: what it was still needed for — the pinned
   toolchain the packages are produced in — is the small, digest-pinned
   packager image (section 3), and the retired contract it implemented
   went with it.
3. **Provisioner and subprocess profile** — verified package
   acquisition generalized, the store with finalize/freeze, per-step
   layout, context format v4 in the workbench, image-from-packages
   resolution, the patched-copy mechanism (incl. practical verification
   of symlink-view workspaces).
4. **Switchover** — local and remote builds move to the package-built
   image, the old image is retired, and the development workspace moves
   out of the project root into a dedicated workspace directory (the
   manifest repository lives physically in that directory; the
   accustomed dev-root path stays as a symlink pointing into it —
   section 9). **Done for the builds themselves**: a local build in a
   container, a local build in a subprocess and a remote build through
   the build server all run the package-built image on the invocation
   this specification defines, and the vocabulary a user states them in
   is a target (`local` | `remote`) crossed with a mode (`container` |
   `subprocess`). This repository's own CI compiles the Zephyr suites
   and the reference Matter firmware in that same environment. What
   remains of this step is the workspace move and the documentation.
   The one thing that had to be settled before the workspace move is
   settled and measured (section 9): the copy-up is paid. A step's
   `work` stays in the container's writable layer, the view is mirrored
   there as it is everywhere else, and the layer it costs — 587 MB per
   step where the kernel copies metadata up only, about 1.3 GB where it
   copies the files — goes away with the container it belongs to. The
   arrangements that avoid it either put the environment on a filesystem
   the step can write, which is a worse thing to own than a large
   writable layer, or need the launcher to know internals the boundary
   does not hand it (section 4).

## 9. Feasibility findings

Recorded results of the design-phase experiments; sections above already
incorporate their consequences.

- **West workspace via symlink**: `west init -l` on a *symlinked*
  manifest repository resolves the symlink and anchors the workspace at
  the manifest repo's physical parent — the intended workspace folder
  stays empty. The reversed layout works fully: manifest repo physically
  inside the workspace directory, a convenience symlink pointing into it
  from elsewhere; workspace roots correctly, git operations and edits
  through the symlink are transparent. Read operations tolerate a
  read-only manifest repo. West's workspace containment check is
  deliberately lexical and replacing a *project* directory with a
  symlink is an upstream-documented pattern — the basis for symlink
  views of read-only trees.
- **View of the environment's workspace** (verified 2026-09-06 with
  west 1.5.0, the version the tools package ships; the builder assembles
  one for every step, read-only workspace or not — section 5). A view
  assembled
  under `work` — the workspace's top level mirrored, links to the store's
  trees, the delivered SDK linked in at the manifest repository's place —
  is a west workspace: `west topdir` answers with the view, `west list`
  resolves every project through it including the ones the Zephyr
  manifest `import:`s, `west manifest --path` names the view's manifest
  file, and `zephyr.base` stays the relative value the package ships and
  therefore resolves inside the view. The store's `.west/config` is never
  written, because the builder already copies it into `work` and points
  west at the copy with `WEST_CONFIG_LOCAL`.

  **One constraint the experiment produced, and it is not optional.**
  Every source tree in the view has to be a *real directory* — a mirror
  whose entries are links, or the patched copy itself — and not a link to
  the tree. `os.getcwd()` resolves symbolic links, so a tool started with
  its working directory inside a linked tree is in the store as far as
  the kernel is concerned; west then walks up from there and finds the
  store's workspace instead of the view. Zephyr's build does exactly
  that: `cmake/modules/west.cmake` and `cmake/modules/zephyr_module.cmake`
  both run with `WORKING_DIRECTORY ${ZEPHYR_BASE}`, and
  `scripts/zephyr_module.py` asks west for the workspace's projects from
  there. Measured with the tree linked, the answer named the store — a
  patched copy would silently not be built against; measured with the
  tree mirrored, it named the view.

  **A second constraint, found by the first real build (2026-09-07) and
  not by the experiment above: the mirror has to go all the way down, and
  its files have to be hard links.** West checks that a project's
  `west-commands` file stays inside that project
  (`west.commands._ext_specs` → `west.util.escapes_directory`) and it
  *resolves both sides* to do so. A mirror whose entries are symbolic
  links fails that check the moment a mirrored project declares
  `west-commands`: the project directory resolves to the view and the
  file behind the link resolves to the store, so west raises
  `west-commands file scripts/west-commands.yml escapes project path
  zephyr` and refuses the workspace before any command runs. Zephyr and
  MCUboot both declare `west-commands`, and `west build` is itself such
  an extension, so a shallow mirror does not build at all — measured
  first-hand against the real 0.1.10.dev1 workspace package. A mirrored
  layer is therefore reproduced as real directories with a **hard link
  per file** (`_mirror_tree`): the same bytes and the same inode, no copy,
  and every path in it resolves to itself. The measured cost for the real
  package is 90 005 files and 17 878 directories across the three
  mirrored layers in ≈ 3 s and ≈ 71 MB of directory entries. Where a hard
  link cannot be made — the store on another filesystem than the step's
  work directory, or a container's image layer below its writable one —
  that file is copied instead.

  **The rule applies to the layers, not to every project.** Mirrored as
  real trees are three of the four a build context can patch — zephyr,
  chip and mcuboot. The fourth, the manifest repository, is not mirrored
  at all: it is a symbolic link to the SDK the orchestrator delivered,
  which stands outside the workspace, so there is nothing of the store
  there to mirror. Every other west project of the workspace — the
  Zephyr modules the manifest `import:`s — is a plain link into the
  store, and `west topdir` started inside one of *those*
  does resolve to the store, as measured. That is sound rather than
  tolerated: a patch can only name a layer, so a link out of the view can
  only ever reach the same bytes the view would have shown, and mirroring
  a workspace of 124 000 members on every step would buy nothing. Such a
  project may declare `west-commands` too, and west's check passes for it
  because *both* sides resolve into the store together.

  **The findings were measured on a read-only store, and a container is
  not one.** The builder assembles this view for every step of every
  profile (section 5), so the container profile's baked workspace and a
  west workspace somebody is developing in are viewed the same way. What
  differs is what the mirror costs. It costs a copy wherever a hard link
  cannot be made — the workspace on another filesystem than `work` — and
  a container is such a place even though it looks like one filesystem:
  `work` is the writable layer and the baked workspace a layer below it,
  so overlayfs copies a file up before it links it. Measured 2026-09-08
  against `ghcr.io/mcu-home/build-environment:0.1.10.dev2-r1` (Docker
  29.7.2, containerd image store, overlayfs over ext4), the full view of
  the three mirrored layers takes 12.0 s and grows the container's
  writable layer by 587 MB, of which the `zephyr` layer alone is 392 MB —
  the figure the earlier measurement of that layer by itself had
  produced. The links are real: `link()` succeeds and the two paths share
  an inode afterwards. What the copy-up costs is the kernel's option
  rather than a constant: with overlayfs `metacopy` on, as it is on the
  machine measured, only the metadata is copied and a 2 027 256-byte file
  costs 45 056 bytes of writable layer; with `metacopy` off it costs its
  own size (2 043 904 bytes, measured on an overlay mounted for the
  purpose), which puts the same view at about 1.3 GB. That is what the
  container profile pays per step (section 4), into a layer thrown away
  with the container that step ran in.

  **A hard link needs a permission the mirror never had to ask a store
  for.** `fs.protected_hardlinks` is 1 on any current Linux, and it lets
  a process link only to a file it owns or may both read and write. The
  image's workspace is unpacked as root, the step runs as whatever UID
  the orchestrator chose, and the mirror succeeds only because that
  workspace is world-writable. Made `a+rX` — which is what a workspace
  nothing writes should be — every `link()` is refused, the builder falls
  back to copying, and the same view costs 21.5 s and 1.28 GB. Tightening
  the image's workspace permissions is therefore not a free hardening
  step: it waits on a view that does not link.

  **One filesystem is not enough; it has to be one mount.** Hard links do
  not cross a mount even inside a single filesystem: a store bind-mounted
  read-only beside a `work` bind of the same ext4 answers `EXDEV`, and so
  does a `work` on tmpfs (both measured). The two arrangements that do
  link were measured and rejected. A named volume populated from the
  image and holding the step's directories as well costs 56.4 s and
  1.9 GB once per environment, then 4.6 s and 74 MB per step; the
  workspace unpacked on the host with `work` beside it under one bind
  mount costs 55 s and 1.5 GB once, then 4.8 s and 74 MB per step, and
  leaves the image's own workspace unused. Both hand the step a writable
  environment that outlives it, which is the property this profile exists
  to deny. Moving `work` elsewhere in the image's rootfs is not a third
  arrangement: `/tmp/work` and `/mcuhome/work` are the same writable
  layer and cost the same to the megabyte.

  **The view would not have to be mirrored at all, and that is where the
  boundary bites.** What west and CMake need of a mirrored layer is a
  real directory whose paths resolve to themselves, and a *mount* of that
  layer at its place inside the view is one, exactly as a tree of hard
  links is — at no cost, because nothing is written to make it. Measured
  2026-09-08 with the three layers mounted read-only at their view paths
  straight out of the image (`docker run --mount
  type=image,…,image-subpath=…`, which the containerd image store makes
  possible): assembling the rest of the view — the workspace's top
  level, the links to the other projects, the delivered SDK — writes
  542 bytes and takes a millisecond, and the result is a west workspace
  by every check the mirror was built for. `west topdir` answers with
  the view, `west manifest --path` names the view's manifest, `west list`
  resolves every project including the ones the Zephyr manifest
  `import:`s, `west build --help` loads the
  extension command out of the mounted `zephyr` — west's `west-commands`
  containment check passing on a mounted tree — `scripts/zephyr_module.py`
  runs from inside it, and `git describe` answers with the pinned tag. It
  needs `safe.directory` for the view's path, because the mounted trees
  belong to whoever built the image; without it west stops at "failed
  manifest import in zephyr". What it needs beyond that is a launcher
  that knows which layers the workspace package has, where the image
  keeps them, and where the builder places them in the view — three
  things the boundary does not tell an orchestrator and section 4 does
  not let it assume. The arrangement is therefore possible only behind an
  explicit extension of that boundary, in which an environment declares
  the layers it can have mounted and the builder declares the view layout
  that receives them. That extension is deferred; until it exists, the
  container profile mirrors and pays.
- **Offline venv**: creating and populating a venv from a bundled wheel
  set works fully offline (`--no-index`, verified with an unreachable
  proxy) at arbitrary paths; `python3 -m venv` itself needs no network.
  A venv moved after creation fails ("bad interpreter") — hence
  provisioning creates it in place.
- **Host baseline**: floors in section 6; verified against the pinned
  Zephyr checkout (`PYTHON_MINIMUM_REQUIRED 3.12`,
  `cmake_minimum_required 3.20.0`). The pinned gn binary is dynamically
  linked (not static, as its download page suggests) but needs only
  GLIBC_2.18. CHIP's pre-generation switch (`CHIP_CODEGEN_PREGEN_DIR`)
  is present and branching correctly in the pinned CHIP version.
- **Matter pre-generation**: the pinned CHIP version already carries the
  fix for the pre-generation-under-Ninja report
  (project-chip/connectedhomeip issue 39787, fixed by pull request 39788 —
  the fix commit is an ancestor of the pinned tag, and the corrected
  `rebase_path(target_gen_dir, root_build_dir)` is present in
  `build/chip/chip_codegen.gni`). **No local patch is needed.**

  What did need solving is a different property of the same mechanism.
  CHIP's CMake glue indexes the pre-generation directory by the path of the
  data-model file *relative to `CHIP_ROOT`* (`build/chip/chip_codegen.cmake`),
  and MCUHome's data model lives in the SDK, outside CHIP — so that relative
  path climbs out of the CHIP tree and back down into the manifest
  repository's directory. The workspace package therefore lays the
  pre-generated output out as a shadow of the workspace and hands the build
  the shadow's CHIP root, which has to exist as an empty directory because
  the kernel resolves `..` by walking. `codepregen.py` is bypassed for two
  reasons of its own: it derives its output paths from the root it walked,
  which does not agree with what the consumer computes for an out-of-tree
  data model, and its ZAP step passes no `--zcl`, which an out-of-tree
  `.zap` needs. The two documented `generate.py`/`codegen.py` invocations
  are used directly instead.

  Verified end to end, 2026-09-02: the reference Matter sample builds from
  the packaged workspace with `CHIP_CODEGEN_PREGEN_DIR` set, **no network**
  and **no zap** (an empty directory mounted over the zap install,
  `ZAP_INSTALL_PATH` unset, zap absent from `PATH`) — all four pre-generated
  translation units are compiled out of the package and the firmware links.
- **ccache across build directories**: full direct-mode hits with
  `CCACHE_BASEDIR` over the build-dir parent plus `hash_dir = false`;
  neither alone suffices once `-g` and build-local include paths are in
  play. Measured with a host gcc; re-check with the pinned cross
  toolchain is part of the packages phase.

## 10. Open points

None. The last one — which Python ABI the bundled wheel set targets — is
answered in section 6: the set targets the minor it is built with, that
minor is the requirement, and the provisioner refuses a host that has
another one before it creates anything.
