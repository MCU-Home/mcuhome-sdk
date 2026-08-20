# MCUHome Build Actions

**For spec generation 1.** Draft — not yet released.

The [build environment specification](build-environment-specification.md)
gives every step an `action` and says which actions exist is not its
business: "the orchestrator documents them". This is that document.

Read it together with the
[build context format](build-context-format.md), which describes what an
action gets handed.

## 1. What an action is

A name in a request document, and a job an environment either does or
declines. Spelled `[a-z][a-z0-9-]*` — lowercase, no underscores.

An environment implements the actions it can and answers `unsupported`
to every other one, which is the specification's own rule: it means *no
environment of my kind can do this*, and it lets the orchestrator look
for a different environment instead of reporting a broken build. An
environment that implements nothing is useless but not wrong.

Arguments travel in the request's `parameters` object. A parameter is
optional unless this document says otherwise, and an environment ignores
parameters it does not know — the same rule that governs every other
field of the two documents, and what lets a parameter be added without
moving the spec generation.

## 2. `build`

Compile the firmware for the device the build context describes.

**Reads:** everything. `mcuhome/build-context` for the device model, the
verification key and the patches, `mcuhome/sdk` for the code generator
and the framework sources, and whatever source trees the environment
carries.

**Parameters:** none today. `parameters` is `{}`.

**Does:**

1. Apply the context's patches to the trees they name, per §9 of the
   specification.
2. Generate the Zephyr application for the device model. MCUHome's own
   environment does not do this itself — the code generator ships in
   `mcuhome/sdk`, so the generated application belongs to the SDK the
   context pinned rather than to the environment's own vintage.
3. Compile the application and the bootloader, with the context's
   `keys/signing.pub` compiled in as the bootloader's verification key.
4. Copy the results into `mcuhome/out` and write the build report.

**Never does:** sign. A build environment does not have the private key
and must never be handed one; what it produces is an unsigned image, and
signing happens afterwards on the machine that holds the key. This is
not a convention that can be relaxed by an environment that would find
it convenient — the whole build path is built around the key not
travelling.

### 2.1 The artifacts

| Name in `out/` | What it is |
|---|---|
| `firmware.bin` | The unsigned application image, raw binary. |
| `firmware.hex` | The same image as Intel HEX. |
| `bootloader.hex` | MCUboot, if the build produced one. |
| `build-report.json` | The report below. Always. |

The names are fixed here because the result document lists artifacts by
name and nothing else: whoever signs has to find the image, and it finds
it by knowing what it is called. `bootloader.hex` is the one optional
entry — a device built without a bootloader has none, which is a correct
result and not a missing file.

An environment may write more files into `out/` and list them; the
orchestrator uses the ones it knows. It may not write a file under one
of the names above that is not what the table says it is.

### 2.2 The build report

`build-report.json`, one JSON object, written by every successful build:

```json
{
  "report": 1,
  "signing": {
    "signature_type": "ecdsa-p256",
    "arguments": {
      "version": "1.4.0+0",
      "header-size": 512,
      "align": 4,
      "slot-size": 933888
    }
  },
  "memory": [
    {"image": "mcuhome", "region": "FLASH", "used": 748960, "total": 933888, "percent": 80.2}
  ]
}
```

`report` is the report format's own version, and a consumer that does
not implement the version it finds **must not sign from the document**.

`signing` exists for exactly one reader: the party that signs, which is
the only party holding the private key. `arguments` are the `imgtool
sign` arguments the image was built for, spelled as imgtool spells them.
Getting one of them wrong produces an image the bootloader silently
refuses, so they are reported by the side that knows them rather than
guessed by the side that signs. `version` is what MCUboot compares
monotonically when it decides whether an update is newer — a build that
cannot state it fails instead of reporting a default.

`memory` is one entry per image and region, and it is optional: a build
that relinked nothing reports none, which is correct rather than
incomplete. It exists to be shown to a person, not to be acted on.

## 3. What is not an action

**Describing the environment.** Its Zephyr version, the spec generation
it implements and the contexts it accepts are OCI labels, read before
anything is started. An action could only answer the same questions
later and at the cost of a container.

**Verifying the context.** The orchestrator creates the context, hashes
it, and delivers it; the environment is forbidden to modify it. There is
nothing an environment could confirm that the orchestrator does not
already know from its own bytes.

**Signing.** See above.

## 4. How the vocabulary grows

A new action is additive: environments that do not implement it answer
`unsupported`, orchestrators that do not send it are unaffected, and the
spec generation does not move. That is the point of the refusal being
typed.

An action is worth adding when a step needs a different environment, or
when it must be able to fail on its own — those are the two things
`out/` and the step boundary buy. Work that always runs in the same
environment and always fails the whole build with it is a phase of an
existing action, not an action.
