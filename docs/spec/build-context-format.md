# MCUHome Build Context Format

**Format version 3.** Draft — not yet released.

A **build context** is the resolved input of one build: what to build,
for which board, with which SDK, patched how. It is a plain directory,
and it is what a build environment finds at
`$MCUHOME_BUILDER_BASE_DIR/mcuhome/build-context` — see the
[build environment specification](build-environment-specification.md),
which is the document a build environment is written against. This one
describes what MCUHome's own generator, `mcuhome-workbench`, puts in it.

You need this document if you are writing a build environment that
actually builds MCUHome devices. You do **not** need it to satisfy the
specification: an environment may read one file of a context and refuse
everything else it does not recognise.

## 1. What the specification fixes, and what this document does

Exactly one thing about a build context belongs to the specification:
the file `build-context.json` and the `generator` in it. That is the
name a build environment declares a version constraint against, and the
orchestrator checks it before every step.

Everything else — every other file, every field, the whole layout — is
this format, and it changes on its own schedule.

## 2. The layout

```
build-context.json                the generator declaration
context.yaml                      the request: what this build was pinned to
manifest.yaml                     the lock: what is in the context, and its ID
model/device-model.json           the device to build
keys/signing.pub                  the bootloader's verification key
patches/<layer>/NNNN-name.patch   source patches, optional
```

Nothing else is ever in a context. A build environment that finds
something else has been handed a directory somebody assembled by hand.

The two YAML documents are written at different moments and by
different parties, which is the one thing worth knowing before reading
either: `context.yaml` is the **request**, written by whoever created
the context, and it states what the build was pinned to. `manifest.yaml`
is the **lock**, written afterwards by whoever froze the context, and it
states what is actually in it. A context reaches a build environment
locked; an unlocked one is still being assembled.

## 3. `build-context.json`

One JSON object, UTF-8:

```json
{
  "generator": "mcuhome-workbench:1.2.0"
}
```

The chain format is the specification's (§8). More keys may join this
file later; a reader ignores what it does not know.

## 4. `context.yaml` — the request

```yaml
context: 3
created: 2026-08-10T09:00:00Z
mcuhome:
  constraint: ~=2.3.6
  version: 2.4.0
  package:
    url: https://packages.mcuhome.org/sdk/mcuhome-sdk-2.4.0.tar.zst
    sha256: 9d1c…
build_environment: ghcr.io/mcu-home/build-container:zephyr-4.4.0-r11@sha256:4d2f…
target:
  board: nrf7002dk/nrf5340/cpuapp
```

| Field | Meaning |
|---|---|
| `context` | This format's version. |
| `created` | When the request was written, ISO 8601 UTC. Informational, and the only field two creations of the same inputs may differ in. |
| `mcuhome.constraint` | What the device configuration asked for — a PEP 440 specifier. The intent, not the answer. May be empty, which is PEP 440's "any version". |
| `mcuhome.version` | What the constraint resolved to. |
| `mcuhome.package.url` | Where those bytes were found. A hint; may be empty. |
| `mcuhome.package.sha256` | The bytes themselves — this is what identifies the SDK. |
| `build_environment` | The image this context is compiled in, as a container reference **carrying a digest**. |
| `target.board` | The Zephyr board. |

The build environment is **pinned, not requested**. The party that
created the context already chose the image, digest and all; nobody
downstream picks anything. That is what lets one context mean one
firmware: a context that named a requirement could be answered by two
different images, and two different images are two different builds.

## 5. `manifest.yaml` — the lock

The request restated, plus the two things that do not exist until the
file set is final:

```yaml
context: 3
mcuhome:
  constraint: ~=2.3.6
  version: 2.4.0
  package:
    url: https://packages.mcuhome.org/sdk/mcuhome-sdk-2.4.0.tar.zst
    sha256: 9d1c…
build_environment: ghcr.io/mcu-home/build-container:zephyr-4.4.0-r11@sha256:4d2f…
target:
  board: nrf7002dk/nrf5340/cpuapp
files:
  - path: build-context.json
    sha256: 4175…
  - path: keys/signing.pub
    sha256: 8ab0…
  - path: model/device-model.json
    sha256: 22cd…
id: sha256:f20d…
```

`files` lists **every file in the context except the two YAML documents
themselves**, sorted by path, each with the SHA-256 of its content.
Patches are ordinary entries; there is deliberately no patch list
anywhere, so nothing can disagree with the patches actually present.

`created` is not restated: it dates the request and lives there alone.

## 6. The context ID

`id` names the context by its content. It is the SHA-256, written
`sha256:<64 lowercase hex digits>`, of the UTF-8 encoding of this
document in RFC 8785 canonical JSON:

```json
{"build_environment":{"digest":"sha256:…"},
 "files":[{"path":"…","sha256":"…"}],
 "sdk":{"sha256":"…"},
 "target":{"board":"…"}}
```

`files` is sorted by `path` in ascending byte order of its UTF-8
encoding, which for these names is a plain string sort.

Four things are hashed and nothing else: the SDK's content hash, the
build environment's digest, the board, and every file with its own
hash. Deliberately outside it are `created`, `mcuhome.constraint`,
`mcuhome.version` and `mcuhome.package.url` — a timestamp, an intent,
and two names for bytes the hash already pins — and everything about the
build environment except its digest, for the same reason: a registry
and a tag are a location and a label for bytes that are already
identified.

**The rule is frozen.** Everything that ever names a context depends on
computing the same ID from the same inputs forever, so a field can join
the hashed document only together with a new format version, and this
version's rule never changes. Both parties to a build compute the ID
independently, from the bytes they actually hold; a declared `id` is
advisory, like every declared value.

## 7. `model/device-model.json`

The device, fully resolved: board, hardware, endpoints, channels,
transport, versions, commissioning identity. One JSON object, and the
only file a build environment actually has to understand to produce
firmware.

Its shape is the workbench's canonical device model, and MCUHome's own
build environment does not read it itself: it hands the file to the code
generator that ships in `mcuhome/sdk`, which turns it into a Zephyr
application. That is the supported way to consume it — the model grows
with every component MCUHome learns, and a second reader of it would
have to grow at the same rate.

## 8. `keys/signing.pub`

The **public** half of the user's MCUboot signing key, PEM, ECDSA P-256.
The build compiles it into the bootloader as the verification key.

The private half never enters a context and never reaches a build. What
a build produces is unsigned; signing happens afterwards, where the key
is. A context carrying a private key is refused when it is created.

## 9. `patches/<layer>/NNNN-name.patch`

Source patches for the trees a build environment carries.

- The **layer** is the subfolder — `zephyr`, `sdk`, `chip`, whatever the
  environment knows. Lowercase, `[a-z][a-z0-9_-]*`.
- The **order** is the filename: `NNNN-description.patch`, ascending
  within a layer.
- A layer folder holds patch files and nothing else. There is no deeper
  nesting, because nothing deeper would have a meaning.

A build environment decides which layers it accepts and where each one
lives; how a patch is applied is the specification's §9.

## 10. Versioning

The `context` key in both YAML documents is this format's version. A
reader that does not implement the version it finds refuses — the
specification calls that `unsupported`, and it is a legible answer an
orchestrator can act on by choosing a different environment.

The version rises whenever a reader written for the previous one would
get something wrong. Adding a file or an optional field does not raise
it: readers ignore what they do not know, which is what makes such a
change additive.

`build-context.json` joined this way and the version stayed at 3. It is
new content, no reader requires it yet, and the constraint check that
makes it load-bearing arrives with the specification's implementation —
at which point this format is reworked as a whole and gets the number
that comes with that.
