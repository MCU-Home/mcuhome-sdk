# mcuhome-sdk

mcuhome-sdk is the firmware SDK of MCUHome: the C runtime, its components, and
the west manifest that pins the trees they build against. It is the repository a
device build compiles from, and it defines the build environment that compiles it.

## What this repository holds

- `west.yml`, the manifest pinning Zephyr, its HALs, OpenThread, mbedTLS,
  MCUboot and upstream CHIP to the exact revisions this SDK builds against.
- The C runtime (`components/`, `lib/`, `drivers/`, `include/`): Matter bring-up
  and OTA, a Zephyr-sensor-to-Matter-attribute adapter, watchdog-backed health
  monitoring, and the headers a generated application compiles against.
- `mcuhome-model` and `mcuhome-compiler` (`mcuhome/`): the device-model
  vocabulary every MCUHome tool speaks, and the code generation and west
  orchestration that turn a build context into firmware.
- The build environment (`packaging/build-environment/`,
  `containers/build-environment/`): the Zephyr SDK, the toolchains and a
  pinned west workspace, distributed as two hash-pinned packages and
  assembled into `ghcr.io/mcu-home/build-environment` for amd64 and arm64.
- Zephyr snippets and devicetree bindings (`snippets/`, `dts/`) for Matter,
  debug output over RTT and boot mode.
- Sample applications (`samples/`): a composed Matter node and a network-core
  radio image, both driven by twister.

## Using it

A device build does not clone this repository. The workbench resolves the SDK
constraint of a device to one released `mcuhome-sdk-<version>.tar.zst` archive,
delivers it to the build environment, which reaches code generation through
the entry point `mcuhome-sdk.json` declares — so from a project directory the
whole of it is one command:

```sh
mcuhome device build <device>
```

The repository is also a plain Zephyr module and the manifest repository of its
own west workspace, which is how an application consumes the C runtime directly.

## How it fits into MCUHome

- [mcuhome-workbench](https://github.com/mcu-home/mcuhome-workbench) — resolves
  the SDK pin, builds the context, drives the build environment one step at a
  time, and signs the resulting image afterwards.
- [mcuhome-cli](https://github.com/mcu-home/mcuhome-cli) and
  [mcuhome-ui](https://github.com/mcu-home/mcuhome-ui) — reach this repository
  only through the workbench.
- [mcuhome-buildserver](https://github.com/mcu-home/mcuhome-buildserver) —
  speaks the build-context vocabulary of `mcuhome-model` and runs the build
  environment for a build driven from another machine.
- [mcuhome-packagetool](https://github.com/mcu-home/mcuhome-packagetool) — publishes
  a released SDK archive as a signed, hash-pinned package source.

## Layout

| Path | Purpose |
|---|---|
| `mcuhome/` | `mcuhome-model` and `mcuhome-compiler`: device model, registry, build context, code generation, west orchestration, and the builder program a build environment runs |
| `packaging/` | Distribution metadata for the two Python distributions |
| `components/`, `lib/`, `drivers/` | The C runtime: Matter and sensor components, portable libraries, out-of-tree drivers |
| `include/`, `dts/` | Public headers and devicetree bindings |
| `app/`, `snippets/` | The generic application main every device shares, and the Zephyr snippets a device class pulls in |
| `containers/` | The build environment as a container image, and the pinned toolchain its packages are produced in |
| `samples/`, `tests/` | Sample firmware, the Python suite and the twister suites |
| `patches/` | Patches applied to the pinned upstream trees |

## Releasing

This repository cuts **three release lines out of one history**, versioned
independently and declared together in
[`packaging/build-environment/environment.json`](packaging/build-environment/environment.json):
`v<version>` releases the SDK package, `workspace-v<version>` the build
workspace package and `tools-v<version>` the build tools packages. Each
line states a PEP 440 constraint on the one below it, so a build resolves a
chain — SDK → build workspace → build tools — and pins every stage by name,
version and hash.

A tag runs the gate before it publishes anything: the tag has to name the
version the commit declares for that line, and the reference Matter device
has to build against the published versions around it. Then the release
carries exactly those archives, a build workspace release assembles the
container image from them, and the registry publish is the operator's own
step afterwards. The whole procedure, including what to bump where and what
each gate demands, is [`RELEASING.md`](RELEASING.md).

## Development — how to work on this repository

This repository has its own virtual environment in `.venv/`; nothing is
installed into the system Python or into another repository's environment.
`bin/` holds the user-facing entry points, `scripts/` the development
tooling: `scripts/test` and `scripts/lint` dispatch the checks — `all` runs
every one, `list` names them, `<name>` runs one — and each check is its own
wrapper in `scripts/test.d/` or `scripts/lint.d/`. The wrappers select
`.venv` themselves (never activate one by hand) and are exactly what CI
runs, one job per check.

Needs Python ≥3.13 for `packaging/model` and `packaging/compiler`. C sources
follow `.clang-format`, checked with a pinned clang-format binary.

**Where the checkout lies.** This repository is the manifest repository of
a west workspace, and it has to lie *inside* that workspace: `west init -l`
resolves a symlinked manifest repository and anchors the workspace at the
checkout's **physical** parent, so the workspace directory is the
checkout's parent, and a convenience symlink points into the workspace,
never the other way round. From scratch:

```sh
mkdir mcuhome-workspace && cd mcuhome-workspace
git clone https://github.com/mcu-home/mcuhome-sdk.git mcuhome-sdk
west init -l mcuhome-sdk
west update
```

`.west/`, `zephyr/`, `modules/` and `bootloader/` end up beside the
checkout. If you are used to reaching the repository at some other path,
put a link there and nothing else changes — `ln -s
mcuhome-workspace/mcuhome-sdk mcuhome-sdk` in the parent directory; git,
editable installs and the wrappers all work through it, and west still
anchors on the physical location.

That workspace is also what a device build compiles against while you are
changing the SDK: point `build.dev_workspace` at the workspace directory
(not at this checkout) and build without a container —
`mcuhome config set build.mode subprocess` — and the build uses these
trees and the tools on your `PATH` instead of a provisioned environment,
without writing anything into them. The
[workbench's README](https://github.com/mcu-home/mcuhome-workbench#building-against-a-west-workspace-of-your-own)
documents what such a build does and refuses.

`scripts/test twister` needs a build environment and a west workspace. The
workspace is the one this checkout lies in, so a checkout laid out as above
needs nothing set; a different workspace is passed as the wrapper's first
argument — one that does not start with `-`, otherwise it is left for
`west twister` instead — or in `MCUHOME_SDK_WEST_WORKSPACE`. Extra
arguments, a leading option included, pass through to `west twister`. The
build environment comes in
either of the two ways it is delivered. On a developer machine: a container
runtime and a build-environment image already pulled — the wrapper never
fetches one. In CI: the workspace and tools packages the commit under test
produces, unpacked, with `MCUHOME_SDK_BUILD_TOOLS` naming the tools tree;
then nothing is containerized and the suites compile on the host with that
package's toolchain. `.github/workflows/ci-build.yml`'s `test-twister` job
shows the second form end to end. Twister's output — build trees, logs, the
report — goes to a temporary directory that is removed when the run ends;
`MCUHOME_SDK_TWISTER_KEEP=1` keeps it and prints its path.

```sh
python3 -m venv .venv && .venv/bin/pip install \
  -e ./packaging/model -e ./packaging/compiler --group dev
```

```sh
scripts/test all
scripts/lint all
```

The rules that hold across every MCUHome repository — coding standards,
commits, licensing — are in the organization's
[contributing guide](https://github.com/mcu-home/.github/blob/main/CONTRIBUTING.md).

## Security

This repository derives Matter commissioning credentials and states the
arguments a firmware image has to be signed with, but it never signs. A build
produces an **unsigned** image; the signature is applied afterwards on the
machine that holds the private key, so no build environment ever sees one. To
report a vulnerability, follow the organization's security policy at
[SECURITY.md](https://github.com/mcu-home/.github/blob/main/SECURITY.md).

## Documentation

- [docs/spec/](docs/spec/) — the normative build-environment specifications
- [docs/design/](docs/design/) — pipeline, component model, YAML schema, the
  build-environment design and the Matter/Zephyr integration notes
- [github.com/mcu-home](https://github.com/mcu-home) — the MCUHome project

## Contributing and support

Report a problem or propose a change through this repository's
[issue tracker](https://github.com/mcu-home/mcuhome-sdk/issues). The rules for
contributing to any MCUHome repository are at
[CONTRIBUTING.md](https://github.com/mcu-home/.github/blob/main/CONTRIBUTING.md).

## License

Apache License 2.0, see [LICENSE](LICENSE).
