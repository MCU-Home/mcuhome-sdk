# MCUHome Builder Pipeline — Design

> **Status: approved by the product owner (2026-08-03).**
> Builds on the approved YAML schema design ([yaml-schema.md](yaml-schema.md))
> and on two product decisions: the toolchain is not installed on the
> user's machine, and the integration targets Matter only.
>
> **What this document covers:** the principles behind MCUHome's code
> generation, the configuration tree a device is described in, the
> pipeline stages from YAML to a complete Zephyr application, the Matter
> data-model wiring, what happens to the build's artifacts on the host,
> the CLI surface and the testing strategy.
>
> **What it does not cover:** the build environment. What a build
> environment must provide, how it is invoked, which actions exist and
> what a build context contains are fixed by the specification set —
> [build-environment-specification.md](../spec/build-environment-specification.md),
> [build-actions.md](../spec/build-actions.md) and
> [build-context-format.md](../spec/build-context-format.md). How
> MCUHome builds, distributes and runs its *own* environment is
> [build-environment.md](build-environment.md). The former §5 (build
> execution) and §6 (build service boundary) described that area and
> were removed; the sections after them keep their numbers, so
> references from other documents and from the test suite stay valid.

## 1. Principles

1. **Thin code generation.** The builder generates *data*, not logic:
   static configuration tables, a devicetree overlay and Kconfig
   fragments. All behavior lives in the generic MCUHome runtime
   (C, in `components/` and `lib/`) which interprets those tables.
   ESPHome generates C++ program logic per config — we deliberately
   don't: generated logic is hard to test, diff and debug.
   *Size note:* this does **not** bloat binaries — component selection
   still happens at compile time (Kconfig enables only what the YAML
   uses; the linker strips the rest). The interpreter engine costs a few
   KB; the flash budget is dominated by the Matter/Thread stacks
   (~600–800 KB), which is also what defines the minimum viable MCU.
   The memory figures every build reports
   ([build-actions.md](../spec/build-actions.md)) track this permanently.
2. **One canonical intermediate model.** Validation and resolution
   produce a normalized "device model" (JSON): the single internal
   representation between YAML and generators, and what actually travels
   to a build — it is the `model/device-model.json` of the build context
   ([build-context-format.md](../spec/build-context-format.md)), and it
   is schema-versioned. No generator reads raw YAML.
3. **Every intermediate artifact is inspectable.** The build directory
   contains the resolved model and all generated files as plain text —
   debuggable with standard tools, no hidden state.
4. **Reproducible by construction.** A pinned dependency world and a
   versioned build environment: same config tree + same MCUHome version
   = same image, on any machine — including someone else's build server.
   Nothing a build consumes is resolved "latest" at build time.
5. **Fail early, fail precisely.** The validation layers from the schema
   design run before anything is generated; every error carries
   file/line/key and a fix hint.

## 2. Configuration tree

A device is always a **folder**, never a bare file; reusable fragments
live in a parallel folder. Layout (Home Assistant add-on example —
standalone use has the same tree under any root):

```
/config/mcuhome/
├── devices/
│   ├── bedroom-climate/
│   │   └── main.yaml            # entry point of this device
│   └── office-co2-guard/
│       ├── main.yaml
│       └── notes.md             # device-local files are fine
├── shared/
│   ├── thread-sed-defaults.yaml # reusable fragments (consumed by the
│   └── i2c-standard-pins.yaml   #  packages mechanism, schema rev. 2)
├── components/                  # tree-wide custom components (see the
│                                #  component-model design, §8)
└── secrets.yaml                 # one secrets store for the whole tree
```

- `devices/<name>/main.yaml` is the canonical entry point; the folder
  name is authoritative for tooling (`mcuhome build bedroom-climate`).
  Device folders later also host device-local custom components and
  extra fragments — the folder-per-device rule makes that growth free.
- `shared/` is reserved for reusable fragments now and becomes active
  together with the packages/include mechanism (schema revision 2);
  creating the folder and its semantics from day one avoids a migration.
- `components/` holds custom components shared across the tree;
  device-local ones live in `devices/<name>/components/`. Resolution
  order and the future git-referenced mechanism are defined in the
  component-model design (§8).
- `secrets.yaml` lives at tree root (`!secret` resolves against it).
- Build output does **not** pollute the config tree: it goes to a
  separate work dir (`build/<device>/`, location configurable) — the
  config tree stays clean, diffable and git-friendly for users.

## 3. Pipeline stages

```
devices/<name>/main.yaml
  │  1 load        YAML parse, !secret resolution
  │  2 validate    schema shape → cross-refs → board capabilities → Matter conformance
  │  3 resolve     defaults, device-type completion, endpoint 0 synthesis
  ▼                → device-model.json  (canonical model)
  │  4 generate    from the model only:
  │                  ├─ app/boards/<board>.overlay   (board wiring + flash layout + hardware:)
  │                  ├─ app/prj.conf fragments       (from network:/power:/components)
  │                  ├─ mcuhome_config.c/.h          (endpoint/cluster/automation tables)
  │                  ├─ app/CMakeLists.txt           (generated app skeleton)
  │                  ├─ app/sysbuild.conf            (bootloader, mode, signature type)
  │                  └─ app/sysbuild/mcuboot.{conf,overlay}   (the bootloader image)
  │  5 build       compiled in a build environment (sysbuild)
  ▼
artifacts: the unsigned application image, the bootloader, and the build
report — names and content per build-actions.md (§7)
```

The flash layout and the bootloader configuration are **per-board
registry data**, not generator logic: stage 4 renders
`BoardDef.update_scheme` into the two devicetree overlays and the two
Kconfig fragments above, and nothing in the builder branches on a board
name. Supporting a new board is therefore a registry entry, never a code
change in the generator.

- Stages are separately invocable (`mcuhome validate`, `mcuhome build`);
  stage 4's output is a complete, standalone Zephyr application that
  consumes the MCUHome Zephyr module — a developer can `west build` it
  manually without the builder.
- Automations compile to a compact static table (triggers, conditions,
  actions as data) interpreted by a small runtime engine — no generated
  C control flow.

## 4. Matter data model wiring (verified by prototype)

The Matter SDK's conventional path generates cluster code from ZAP files
at compile time. For a YAML-driven framework that is hostile: ZAP is a
heavy toolchain and static per-config codegen contradicts §1. The
integration prototype (2026-08-04, see
[matter-zephyr-integration.md](matter-zephyr-integration.md))
**verified dynamic endpoint registration at runtime on hardware**
(nRF5340, upstream CHIP v1.5.1.0): endpoints register from tables via
`emberAfSetDynamicEndpoint`, with a static ZAP-generated data model only
for the fixed root endpoint — generated once per MCUHome release, not
per device config. `CHIP_DEVICE_CONFIG_DYNAMIC_ENDPOINT_COUNT` sizing is
resolved: it derives automatically from
`CONFIG_MCUHOME_MATTER_MAX_DYNAMIC_ENDPOINTS`
(`include/mcuhome/matter/chip_project_config.h`), so the builder's
remaining job there is at most a Kconfig passthrough — one Kconfig symbol
instead of a number that has to be kept in sync by hand.

Because that data model is a release constant, it is generated when
MCUHome's build environment is packaged, not while a device is built:
neither ZAP nor its provisioning is part of a build
([build-environment.md](build-environment.md)). The vanilla-Zephyr patch
set ([../../patches/](../../patches/)) is applied at the same point.

## 7. Artifacts

What a build produces, under which names, and what the build report
contains is fixed by the action vocabulary
([build-actions.md](../spec/build-actions.md)): the unsigned application
image, the bootloader where one was built, and `build-report.json`. This
section is about what happens to them afterwards, on the host — the part
no build environment ever does, because it needs the private key.

**Signing is detached.** A build never signs and never sees a private
key; it compiles the public half in, which is all MCUboot needs to
verify at boot. `mcuhome sign <build dir>` then runs `imgtool` with the
parameters the build reported, on the machine that holds the key. Three
of those parameters — header size, slot size and alignment — come from
the board's registry entry, the same partition table stage 4 rendered
into the overlay; the fourth, the image version, is read out of the
built application's Kconfig. They are reported by the side that knows
them rather than guessed by the side that signs: get one of them wrong
and the result is an image the bootloader silently refuses.

Signing afterwards costs nothing in equivalence: it does not touch the
image, it appends a signature. ECDSA draws a fresh random nonce per
signature, so two signings of identical bytes differ in the signature
TLV (occasionally in its length) and in nothing else — mcuhome-workbench
asserts exactly that in `tests/python/test_imgtool.py`: header, payload,
protected TLVs and the SHA-256 over all of them equal, signature
different, both verifying.

**The Matter OTA file is written after signing, on the same machine**
(`mcuhome/model/ota.py` here; the writing end lives in
mcuhome-workbench). It wraps the *signed* application, so it can only be
produced where the signature is, and it is written only for a device
that could receive one — the board's update scheme has a staging slot
and the device has a Matter stack. MCUHome writes the format itself
instead of calling CHIP's `ota_image_tool.py`, which is what lets the
key holder produce an OTA image without a device configuration and
without the Matter SDK; the pytest suite compares the two byte for byte
wherever the SDK is present. The Matter `SoftwareVersion` is derived
from the firmware version, so the number a controller compares is never
maintained by hand.

Flashing UX (`mcuhome flash`, browser-based flashing from the dashboard)
is its own later design; the artifacts above are designed so both work.

## 8. CLI surface (v0.1)

The command vocabulary, its flags and the `--json`/exit-code contract
are the CLI's own decisions and are documented in the mcuhome-cli
repository. The enumeration this section used to carry had drifted from
what was built — it listed `--keep-going`, which never existed — and is
not repeated here: one place per decision beats two that disagree.

What stays pipeline-relevant: `<device>` is a folder name resolved
against the config tree root (`devices/<name>/main.yaml`; an explicit
path works too; tree root `--config-root`, else auto-discovered cwd
upwards — as built today; the target model is a project directory with
`mcuhome.yaml` and `--project-dir`). `mcuhome device
matter-pairing --new` is the exception to "the builder never writes into
the configuration tree" (§2), and it exists because of §1.4: a device
needs credentials nobody else has, and a build has to be reproducible, so
the randomness happens once — into the device's `secrets/devices/<name>.
yaml` with `!secret` references in `main.yaml` — and every build after
that is deterministic input in, deterministic bytes out (yaml-schema.md
§4.1).
`new` is the other end of the same rule: it deliberately draws no
credentials, so re-running it after a mistake cannot silently
invalidate every controller that already knows the device. Everything
else (`flash`, `logs`, `migrate`, `update`) arrives with its own
design. In process, the supported programmatic surface is
`mcuhome.workbench.api`.

## 9. Testing strategy

- **Golden-file tests** for stages 1–4: example YAMLs → expected
  device-model.json / overlay / fragments / tables, byte-exact
  (pytest, runs without Zephyr — fast).
- **Compile tests**: golden outputs build against `native_sim` and one
  real board per release via twister — this is where repo CI starts
  (per the scaffold decision: CI lands together with the first tests).
- Validation error messages are tested (bad configs → expected
  error + location), because they are UX.

## 10. Open points

| Topic | Status |
|---|---|
| Build-server client API (authentication, secrets transport) | Own design doc |
| Flashing UX (CLI + browser) | Own design doc |
| device-model.json schema versioning | **Closed.** `MODEL_VERSION` is 1 and is a published contract: a consumer pins what it sends and what it can read (`versions.py`), and a build reads the model out of the build context, so nothing negotiates a version range at run time |
| `mcuhome migrate` (ESPHome import) | Later milestone |
