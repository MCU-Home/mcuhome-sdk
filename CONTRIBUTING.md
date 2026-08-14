# Contributing to MCUHome

Thank you for considering a contribution! MCUHome is in its design phase, so
the most valuable contributions right now are discussion and review of the
[architecture decision records](docs/adr/) and participation in
[GitHub Discussions](https://github.com/mcu-home/mcuhome/discussions).

## Development environment

MCUHome is a Zephyr west workspace application (T2 topology). The workspace
top directory must **not** be a git repository:

```sh
mkdir mcuhome-workspace && cd mcuhome-workspace
git clone https://github.com/mcu-home/mcuhome-sdk
west init -l mcuhome-sdk
west update
```

Install the [Zephyr SDK](https://docs.zephyrproject.org/latest/develop/getting_started/index.html)
matching the Zephyr release pinned in [west.yml](west.yml).

For the Python builder package — this repo's two SDK-side packages; the
workbench (build methods, signing) is developed in its own repository,
[mcu-home/mcuhome](https://github.com/mcu-home/mcuhome):

```sh
cd mcuhome-sdk
python3 -m venv .venv && . .venv/bin/activate
pip install -e ./packaging/model -e ./packaging/compiler
pre-commit install --install-hooks
pre-commit install --hook-type commit-msg
```

## Building and testing

```sh
west twister -T mcuhome-sdk/tests --integration  # C test suites (native_sim)
pytest                                           # builder test suite (tests_py/)
```

For a full firmware build, build a device from its YAML description — or
a sample, if you want the framework without the builder in the picture:

```sh
mcuhome device build mcuhome-sdk/docs/design/examples/00-bmp180-two-endpoints.yaml \
  --build-dir build/bmp180-node
west build -p -b nrf7002dk/nrf5340/cpuapp -S matter -S debug-rtt \
  mcuhome-sdk/samples/matter-node
```

`mcuhome-sdk/app` is not a buildable application: it holds the generic
application main the builder compiles into every generated device, and
building it directly is refused with a message saying so.

## Coding standards

- **C:** Zephyr coding style, enforced by the repo's `.clang-format`
  (tabs, Linux-kernel-derived style). Prefer static allocation; no heap
  allocation after initialization; keep Sleepy End Device power budgets in
  mind (no busy-waiting, no unnecessary wakeups).
- **Python:** `ruff` (lint + format), settings in `pyproject.toml`.
- **Licensing:** every new file needs SPDX headers (a
  `SPDX-FileCopyrightText` line and an `Apache-2.0` license identifier —
  copy them from any existing file).
  **Never copy code from GPL-licensed projects — this explicitly includes
  ESPHome's C++ runtime.**

## Commit and PR rules

- **Conventional Commits:** `feat: …`, `fix: …`, `docs: …`, `chore: …` etc.
  Commit types drive automated releases (SemVer).
- **DCO sign-off:** every commit must be signed off (`git commit -s`),
  certifying the [Developer Certificate of Origin](https://developercertificate.org/).
  We use DCO instead of a CLA.
- Keep PRs focused; one logical change per PR.
- Non-trivial design decisions about this repo need an ADR draft in
  [docs/adr/draft/](docs/adr/draft/) — propose it in the PR. Drafts are
  living documents; the final ADR is written from the real result once
  the component is done ([docs/adr/README.md](docs/adr/README.md)).
  Project-wide decisions (spanning this repo and the tools repo) live in
  [mcu-home/mcuhome](https://github.com/mcu-home/mcuhome) instead.

## Reporting issues

Use the [issue forms](https://github.com/mcu-home/mcuhome/issues/new/choose).
Security vulnerabilities go through [SECURITY.md](SECURITY.md), never public
issues.

## Code of Conduct

This project follows the [Contributor Covenant 3.0](CODE_OF_CONDUCT.md).
