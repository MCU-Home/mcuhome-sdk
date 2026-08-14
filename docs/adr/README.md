# Architecture Decision Records

Non-trivial design decisions are recorded as numbered ADRs in a
lightweight [MADR](https://adr.github.io/madr/) style: **Context /
Decision / Consequences**, plus a status.

This index covers the ADRs that live **in this repository**
(`mcu-home/mcuhome-sdk`) — the ones about the west manifest/Zephyr
module, the C runtime, and the `mcuhome.model`/`mcuhome.compiler`
packages it publishes. Project-wide ADRs and the tools-repo/workbench
ADRs (0017-0024 among them, including the split itself, ADR 0024) live
in the flagship repository,
[mcu-home/mcuhome](https://github.com/mcu-home/mcuhome/tree/main/docs/adr).
Numbers are **one project-wide sequence shared by both repositories** —
gaps in the table below are not missing files, they are ADRs that live
on the other side.

## Lifecycle: draft first, final when real (ADR 0021)

An ADR starts as a **living document**: while the component it decides
about is being built, the decision may change, and then the draft's
*text* changes — no amendment or erratum sections, ever; git history is
the changelog. Drafts may be split, merged, or deleted. `draft`
describes the document's maturity, not missing approval: the decisions
in a draft are product-owner-approved when they are recorded.

When the component is implemented and verified, the ADR is finalized:
rewritten from the real result — the code is the authority — and moved
to the top-level `docs/adr/` of whichever repository owns it, with a
`Finalized:` date. Final ADRs are **immutable**: after finalization only
the status line may change (`superseded by NNNN`). Changing a finalized
decision means writing a new draft that supersedes the old final.

Numbers come from one sequence, assigned at draft creation, and follow
the document for life, across repositories. A final that consolidates
several drafts names the numbers it absorbs; absorbed numbers are
retired, never reused.

Statuses: `draft` (in `draft/`), `accepted`, `deferred`,
`superseded by NNNN`.

The full lifecycle rule is ADR 0021, which lives in the flagship repo:
[mcu-home/mcuhome, docs/adr/0021-draft-first-adr-lifecycle.md](https://github.com/mcu-home/mcuhome/blob/main/docs/adr/0021-draft-first-adr-lifecycle.md).
Project-wide decisions are recorded there; dashboard-specific decisions
live in [mcu-home/dashboard](https://github.com/mcu-home/dashboard).

## Final ADRs (this repo)

| ADR | Title | Status |
|---|---|---|
| [0004](0004-zephyr-west-t2-manifest-and-module.md) | Zephyr west T2 manifest repo that is also a Zephyr module | accepted |
| [0006](0006-matter-sdk-source.md) | Matter SDK source: upstream CHIP vs. Nordic fork | accepted |
| [0007](0007-containerized-toolchain.md) | Containerized toolchain, minimal host requirements | accepted |
| [0008](0008-zephyr-version-strategy.md) | Zephyr version strategy: track latest stable, not LTS | accepted |
| [0009](0009-matter-explicit-yaml-schema.md) | Matter-explicit YAML schema, aligned with devicetree conventions | accepted |
| [0010](0010-matter-only-coap-deferred.md) | Matter-only integration; CoAP deferred to a maintenance channel | accepted |
| [0011](0011-commissioning-and-radio-coexistence.md) | Commissioning strategy and BLE/Thread radio coexistence | accepted |
| [0014](0014-generated-tables-contract.md) | Generated-tables contract and native composed-node topology | accepted |

## Draft ADRs (this repo)

| ADR | Title |
|---|---|
| [0012](draft/0012-device-attestation-strategy.md) | Device attestation (DAC) strategy for user-built devices |
| [0013](draft/0013-binary-blob-policy.md) | Binary blob policy, build profiles, and per-device Zephyr pinning |
| [0015](draft/0015-update-and-partition-architecture.md) | Update and partition architecture per board class |
| [0016](draft/0016-device-onboarding-and-flash-transport.md) | Device onboarding, the MCUHome standard state, and flash transport |

## Elsewhere in the sequence

Numbers 0001-0003, 0005 and 0017-0024 are project-wide or tools/workbench
ADRs (repo topology, packaging, SemVer policy, the remote-build
architecture, the SDK/tools split itself) and live in
[mcu-home/mcuhome](https://github.com/mcu-home/mcuhome/tree/main/docs/adr)
— both its finalized top level and its `draft/`.
