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

- **`mcuhome-build-workspace`** (arch-neutral, one per SDK release or
  `west.yml` change): the materialized west workspace — Zephyr, modules,
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

## 3. Release chain

```
SDK git tag
  → SDK release archive (existing release workflow)
  → build-workspace package build (CI; deterministic, records resolved
    commits; runs the Matter pre-generation)
  → build-tools package build (CI; only when the toolchain generation
    changes)
  → packagetool publication (manually dispatched — one deliberate
    publish act; downstream steps may then chain automatically)
  → container image assembled from the published packages
```

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
`MCUHOME_BUILDER_BASE_DIR` is `/` here. The public build server runs
step containers without network as operator policy; a private operator
may relax that. Which cache tiers exist, and what backs them, is the
operator's decision (specification §8).

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

## 6. Host baseline

The subprocess profile runs the packaged tools on the host, so the
tools package defines a minimum host baseline. With zap out of the
runtime (section 2), the floors are set by the Zephyr SDK toolchain and
the Zephyr build system:

| Dimension | Floor | Set by |
|---|---|---|
| glibc | ≥ 2.28 | Zephyr SDK prebuilt toolchain (gn needs only 2.18) |
| Python | ≥ 3.12 | Zephyr v4.4 build scripts |
| CMake | ≥ 3.20 | Zephyr v4.4 (4.5+ raises this to 3.28) |
| dtc | ≥ 1.4.6 | Zephyr build system |
| Architecture | x86_64, aarch64 (Linux) | all prebuilt tool sources |
| libc family | glibc only, no musl | Zephyr SDK prebuilt toolchain |

The container base image (Debian trixie: glibc 2.41, Python 3.13,
CMake 3.31, dtc 1.7) satisfies every floor — the container is simply a
host that always qualifies.

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
   invocation (no arguments, fixed request path). The old monolithic
   image continues in parallel until switchover.
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
   section 9).

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
  views of read-only trees; practical verification runs with the
  provisioner implementation.
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

- Symlink-view verification with the provisioner (section 8, item 3).
- Which Python ABI the bundled wheel set targets. It is built by the
  container base's interpreter today, so a host whose Python is a different
  minor version cannot install the compiled wheels in it — which the
  container profile never notices and the subprocess profile will.
