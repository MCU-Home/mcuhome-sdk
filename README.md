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
- The build environment (`containers/build-container/`): the Zephyr SDK, the
  toolchains and a baked west workspace, published for amd64 and arm64 as
  `ghcr.io/mcu-home/build-container`.
- Zephyr snippets and devicetree bindings (`snippets/`, `dts/`) for Matter,
  debug output over RTT and boot mode.
- Sample applications (`samples/`): a composed Matter node and a network-core
  radio image, both driven by twister.

## Using it

A device build does not clone this repository. The workbench resolves the SDK
constraint of a device to one released `mcuhome-sdk-<version>.tar.zst` archive,
mounts it into the build environment and invokes `bin/generate` through the
invocation ABI declared in `mcuhome-sdk.json`, so from a project directory the
whole of it is one command:

```sh
mcuhome device build <device>
```

The repository is also a plain Zephyr module and the manifest repository of its
own west workspace, which is how an application consumes the C runtime directly.

## How it fits into MCUHome

- [mcuhome-workbench](https://github.com/mcu-home/mcuhome-workbench) — resolves
  the SDK pin, builds the context, drives the build environment through this
  SDK's invocation ABI, and signs the resulting image afterwards.
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
| `mcuhome/` | `mcuhome-model` and `mcuhome-compiler`: device model, registry, build context, code generation, west orchestration, invocation-ABI adapter |
| `packaging/` | Distribution metadata for the two Python distributions |
| `components/`, `lib/`, `drivers/` | The C runtime: Matter and sensor components, portable libraries, out-of-tree drivers |
| `include/`, `dts/` | Public headers and devicetree bindings |
| `app/`, `snippets/` | The generic application main every device shares, and the Zephyr snippets a device class pulls in |
| `containers/` | The build environment: Dockerfile, contract launcher, workspace record |
| `samples/`, `tests/` | Sample firmware, the Python suite and the twister suites |
| `patches/` | Patches applied to the pinned upstream trees |

## Working on this repository

The Python side needs Python 3.13 and the two distributions installed editable.
The firmware side needs a west workspace — `west init -m
https://github.com/mcu-home/mcuhome-sdk && west update` — and the Zephyr SDK,
most simply by running twister inside the build-environment image.

```sh
pip install -e ./packaging/model -e ./packaging/compiler 'pytest>=8.0' zstandard
ruff check . && ruff format --check . && pytest -q tests/python
west twister -T mcuhome-sdk/tests/twister --integration
```

C sources are formatted with clang-format. Continuous integration runs those
checks on every change, and builds the build environment and the reference
Matter device on both amd64 and arm64.

## Security

This repository derives Matter commissioning credentials and states the
arguments a firmware image has to be signed with, but it never signs. A build
produces an **unsigned** image; the signature is applied afterwards on the
machine that holds the private key, so no build environment ever sees one. To
report a vulnerability, follow the organization's security policy at
[SECURITY.md](https://github.com/mcu-home/.github/blob/main/SECURITY.md).

## Documentation

- [docs/spec/](docs/spec/) — the normative build-environment specifications
- [docs/design/](docs/design/) — pipeline, component model and YAML schema
- [docs/adr/](docs/adr/) — the decisions behind them
- [github.com/mcu-home](https://github.com/mcu-home) — the MCUHome project

## Contributing and support

Report a problem or propose a change through this repository's
[issue tracker](https://github.com/mcu-home/mcuhome-sdk/issues). The rules for
contributing to any MCUHome repository are at
[CONTRIBUTING.md](https://github.com/mcu-home/.github/blob/main/CONTRIBUTING.md).

## License

Apache License 2.0, see [LICENSE](LICENSE).
