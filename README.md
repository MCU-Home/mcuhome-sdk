# MCUHome

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Status: phase 1 complete](https://img.shields.io/badge/status-phase_1_complete-yellow.svg)](#project-status)

**MCUHome turns YAML device descriptions into Zephyr-based smart home
firmware — built on standard protocols instead of a custom API.**

MCUHome is an open-source firmware framework in the spirit of
[ESPHome](https://esphome.io/), rebuilt from the ground up on a different
stack:

| | MCUHome | ESPHome |
|---|---|---|
| RTOS / build system | [Zephyr RTOS](https://zephyrproject.org/) + west | Arduino / ESP-IDF via PlatformIO |
| Network protocols | CoAP and [Matter](https://csa-iot.org/all-solutions/matter/) | custom native API (protobuf) |
| Transports | WiFi **and Thread**, incl. Sleepy End Devices (SED) | WiFi, Ethernet, BT proxy |
| Hardware scope | Everything Zephyr supports (nRF, ESP32, STM32, …) | Espressif-centric, plus RP2040 et al. |

You describe a device in YAML; the MCUHome builder composes Zephyr
configuration, generates the glue code and produces a flashable image. Thanks
to Matter, devices work with Home Assistant and every other Matter
controller out of the box — no custom integration required.

## Project status

**Phase 1 complete.** The firmware runtime — tables-contract framework,
channel layer, netcore entropy service, and a BMP180 two-endpoint sample
— is implemented and hardware-verified: commissioned into a production
Home Assistant instance over Thread (design record: see
[docs/adr/](docs/adr/)). The Python YAML builder (phase 2) is under
construction, and now goes end to end: `mcuhome device validate <device>`
checks a configuration and prints what it resolves to, and `mcuhome
device build <device>` generates the Zephyr application for it and compiles it
into a flashable image, reporting where the image is and what it costs
in flash and RAM. Compiling happens in the versioned builder image
([ADR 0007](docs/adr/0007-containerized-toolchain.md)), so the host needs
nothing but git and docker.
The companion web interface lives in
[mcu-home/dashboard](https://github.com/mcu-home/dashboard).

## Repository layout

This repository has a dual role: it is the **west manifest repository** of
the MCUHome workspace (T2 topology) *and* a reusable **Zephyr module**.

| Path | Purpose |
|---|---|
| `west.yml` | West manifest pinning Zephyr and modules |
| `zephyr/module.yml` | Zephyr module definition (boards, DTS, snippets roots) |
| `mcuhome/` | Python source tree: a PEP 420 namespace with one subpackage per distribution published from this repo (ADR 0020) — `model/` (shared vocabulary), `compiler/` (codegen, west orchestration). The namespace's third subpackage, `workbench/` (config pipeline, build methods, signing — `mcuhome.workbench.api` is its supported surface), lives in its own repository, [mcu-home/mcuhome](https://github.com/mcu-home/mcuhome) (the `mcuhome` command itself is [mcu-home/cli](https://github.com/mcu-home/cli)) |
| `packaging/` | The project file of each distribution built from this repo: `mcuhome-model`, `mcuhome-compiler` — one version, one tag, one release (`mcuhome-workbench` is published from [mcu-home/mcuhome](https://github.com/mcu-home/mcuhome)) |
| `components/` | MCUHome components (Python schema + C sources side by side) |
| `app/` | The generic application main every generated device shares |
| `boards/`, `drivers/`, `dts/bindings/` | Out-of-tree Zephyr hardware support |
| `snippets/` | Connectivity/device-class variants (wifi, thread-sed, …) |
| `include/mcuhome/`, `lib/` | Public runtime API and portable libraries |
| `samples/`, `tests/` | Twister-driven samples and test suites |
| `tests_py/` | pytest suite of this repo's two Python packages |
| `containers/build-container/` | The build-container image — the contract's reference implementation (ADR 0007) |
| `scripts/` | Development tooling and future custom west extension commands |
| `docs/adr/` | Architecture decision records |

## Getting started (developers)

MCUHome uses a [west workspace](https://docs.zephyrproject.org/latest/develop/west/workspaces.html).
The workspace top directory must **not** be a git repository:

```sh
mkdir mcuhome-workspace && cd mcuhome-workspace
git clone https://github.com/mcu-home/mcuhome-sdk
west init -l mcuhome-sdk
west update
```

Requirements on your machine: **git, docker and Python ≥ 3.11** — no
Zephyr SDK, no cross-compilers, no vendor tools. The toolchain lives in
the MCUHome builder image
([ADR 0007](docs/adr/0007-containerized-toolchain.md)), which is
versioned in lockstep with the pinned Zephyr release:

```sh
docker pull ghcr.io/mcu-home/build-container:zephyr-4.4.0-r11
```

Then build a device from its YAML description. The `mcuhome` command is
a thin shell in its own repository
([mcu-home/cli](https://github.com/mcu-home/cli)), and the build methods
and signing live in the workbench, in its own repository too
([mcu-home/mcuhome](https://github.com/mcu-home/mcuhome)); until the
packages are published they are installed from checkouts next to this
one:

```sh
pip install -e mcuhome-sdk/packaging/model \
            -e mcuhome-sdk/packaging/compiler   # the two SDK-side Python packages
git clone https://github.com/mcu-home/mcuhome
pip install -e mcuhome                    # the workbench (build methods, signing)
git clone https://github.com/mcu-home/cli
pip install -e cli                # the `mcuhome` command
mcuhome device build mcuhome-sdk/docs/design/examples/00-bmp180-two-endpoints.yaml \
  --build-dir build/bmp180-node
```

That writes the generated Zephyr application to `build/bmp180-node/app`,
compiles it in the container as your own user, and prints both images —
MCUboot and the application signed for it — with their flash/RAM
footprints and the flash layout they were built against. The first build
also draws your own ECDSA P-256 signing key into
`~/.config/mcuhome/signing.key` and says so: every device you flash
verifies its firmware against it, so it is worth keeping (ADR 0015).
`--generate-only` stops after the generating half, which needs nothing
but Python. Details, including how to build the image yourself, are in
[containers/build-container/README.md](containers/build-container/README.md).

Build parallelism is auto-detected from CPU count and available RAM (a
Matter compile unit peaks around 1-1.5 GiB, so the formula budgets 2 GiB
per job and never exceeds the core count) — `MCUHOME_JOBS=N` overrides
the auto-detection, `--jobs N` overrides both, and it applies inside the
builder container too, resolved on the host before `docker run`.

The default builder is configured through the project or user configuration (ADR 0023);
the fully manual form is `--build-mode local|remote` with mode-specific flags.

### Starting a device from nothing

```sh
mcuhome device new bedroom-climate --board nrf7002dk/nrf5340/cpuapp
mcuhome device matter-pairing --new bedroom-climate      # this device's commissioning codes
mcuhome device validate bedroom-climate
```

`mcuhome device new` writes `devices/bedroom-climate/main.yaml` — a complete
configuration with a commented, working hardware example to uncomment.
It never draws commissioning credentials itself: those are drawn once,
by their own command, so that every build of a device is byte-identical
(`docs/design/yaml-schema.md` §4.1).

### Signing where the key is, building where the CPU is

Every image is signed with your own key
([ADR 0015](docs/adr/draft/0015-update-and-partition-architecture.md) decision 8).
Normally that happens during the build. When the machine that compiles
is not the machine that owns the key — a build server, or the future
dashboard's build App — the two are separated:

```sh
mcuhome public-key > signing.pub          # the half that may travel
mcuhome device build <device> --no-sign --public-key signing.pub
mcuhome device sign-firmware build/<device>                # where the private key is
```

The unsigned build compiles the bootloader with the public key in it and
leaves the application unsigned, and writes `build-report.json` stating
the exact `imgtool` parameters (`--version`, `--header-size`,
`--slot-size`, `--align`). `mcuhome device sign-firmware` reads them back and runs the
same tool with the same arguments — the result is the same image, and
`--no-sign` deliberately leaves no file behind that looks flashable and
is not.

### Machine-readable output

```sh
mcuhome device validate <device> -o json    # the resolved model, or the errors
mcuhome device build    <device> -o json    # the build manifest (log on stderr)

# A machine that only compiles takes the resolved model and nothing else:
# no configuration tree, no secrets (dashboard ADR 0007 decision 1). The
# generated tree is byte-identical to the one the direct route produces.
mcuhome device build --model device-model.json --build-dir build/<device>
mcuhome schema                      # JSON Schema for main.yaml
mcuhome schema registry             # boards, drivers, clusters, device types
```

### Using the builder from Python

`mcuhome.workbench.api` is the supported programmatic surface for
driving a build from Python — it lives in the workbench, which is
published from its own repository: see
[mcu-home/mcuhome](https://github.com/mcu-home/mcuhome). The two
packages this repo publishes, `mcuhome.model` and `mcuhome.compiler`,
are building blocks the workbench consumes; using them directly is an
implementation detail and may change between releases.

To see the framework run without the builder in the picture, build the
reference sample by hand:

```sh
west build -p -b nrf7002dk/nrf5340/cpuapp -S matter -S debug-rtt mcuhome-sdk/samples/matter-node
```

The `matter` and `debug-rtt` snippets are mandatory, not optional. See
[samples/matter-node/README.md](samples/matter-node/README.md) for
hardware prerequisites and wiring. Note that `mcuhome-sdk/app` is *not* a
buildable application — it holds the generic main the builder compiles
into every generated device ([app/README.md](app/README.md)).

## Relationship to ESPHome

MCUHome is inspired by ESPHome's YAML-first user experience but shares no
code with it. ESPHome's C++ runtime is GPLv3; MCUHome is Apache-2.0 and must
stay clean of GPL code — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Questions and ideas go to
[GitHub Discussions](https://github.com/mcu-home/mcuhome/discussions);
bug reports to the [issue tracker](https://github.com/mcu-home/mcuhome/issues).

## License

Apache License 2.0 — see [LICENSE](LICENSE). This repository follows the
[REUSE](https://reuse.software/) specification; every file carries SPDX
license information.
