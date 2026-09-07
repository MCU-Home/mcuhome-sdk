# MCUHome Build Context Format

**Format version 4.** Draft — not yet released.

A **build context** is the resolved input of one build: what to build,
for which board, with which SDK, in which environment, patched how. It is
a plain directory, and it is what a build environment finds at
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

The chain format is the specification's (§9). More keys may join this
file later; a reader ignores what it does not know.

## 4. `context.yaml` — the request

```yaml
context: 4
created: 2026-08-10T09:00:00Z
mcuhome:
  constraint: ~=2.3.6
  version: 2.4.0
  package:
    url: https://mirror-1.packages.mcuhome.org/sdk/mcuhome-sdk-2.4.0.tar.zst
    sha256: 9d1c…
build_environment:
  workspace:
    name: mcuhome-build-workspace
    version: 2.4.0
    sha256: 7c31…
    url: https://mirror-1.packages.mcuhome.org/build-workspace/mcuhome-build-workspace-2.4.0.tar.zst
  tools:
    name: mcuhome-build-tools
    version: 1.2.0
    sha256: b90a…
    url: https://mirror-1.packages.mcuhome.org/build-tools/
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
| `mcuhome.package.sha256` | The bytes themselves — this is what identifies the SDK. Empty in the developer form below, where the SDK is a checkout nobody packed. |
| `build_environment.workspace` | The architecture-neutral package carrying the environment's source world: `name`, `version`, `sha256`, and `url` as a hint. |
| `build_environment.tools` | The tools package: `name`, `version`, `sha256`, and `url` as a hint. The same fields as the workspace entry, and they mean the same thing. |
| `build_environment` as a whole | Either the two entries above, or the single word `developer` — see "The developer form" below. The two forms are exclusive and each fixes what `mcuhome.package.sha256` may be. |
| `target.board` | The Zephyr board. |

Both environment entries are a plain `(name, version, sha256)` triple. The
tools entry names **either** a meta package — the family, whose hash is
derived from the per-platform packages it points at — **or** one concrete
per-platform package. Which of the two it is, is not written in the
context: the package host's index entry for that name says it, and it says
it in a form neither side can misread. A meta package's hash is the
SHA-256 of the UTF-8 encoding of the RFC 8785 canonical JSON of the
`meta` object with every referenced package expanded to `{"name",
"sha256"}`; the meta version equals its members' version by definition.
**This rule is frozen**, the same way §6 freezes the ID rule: once
stated, it is computed this one way for good.

- A **meta** entry resolves: the orchestrator looks up the host's own
  platform in it and fetches the concrete package the index names there.
  This is the normal case, and it is what makes one context build the same
  firmware on an amd64 host and on an arm64 host.
- A **concrete** entry does not resolve: it is that one platform's bytes,
  and a host of another platform refuses legibly rather than substituting
  something. That is the case for an explicitly architecture-targeted
  build, and it exists because "test exactly these bytes" is a real
  request.

Either way the context states a hash, and either way the hash is checked
against what was fetched — a meta hash against the index entry the
platform was resolved through, a concrete hash against the archive.

The build environment is referenced **by its packages**, which is what
the specification says an environment is: a package set, not an image.
Whoever runs this context finds an environment whose own `packages.`
members state these packages — a container image that declares them, or a
store provisioned from them. Both are the same environment. A meta pin is
resolved to its platform's concrete package first, because that is what an
environment actually contains.

The environment is **pinned, not requested**. The party that created the
context already chose the packages, hash and all; nobody downstream picks
anything. That is what lets one context mean one firmware: a context that
named a requirement could be answered by two different package sets, and
two different package sets are two different builds.

The one thing a meta pin leaves open is the platform, and it leaves it
open without giving anything up: the meta hash is derived from the hashes
of every platform's package, so pinning the family still pins the exact
bytes each platform will get. A host does not choose a package — it looks
up the one entry that was already decided for it.

### The developer form

`build_environment` has a second form, and it is the single word
`developer`:

```yaml
context: 4
created: 2026-09-07T09:00:00Z
mcuhome:
  constraint: ""
  version: ""
  package:
    url: ""
    sha256: ""
build_environment: developer
target:
  board: nrf7002dk/nrf5340/cpuapp
```

It says that this build ran against a **west workspace somebody
maintains themselves**: its sources are a checkout, its SDK is that
workspace's own manifest repository, and its tools are whatever was on
that person's `PATH`. None of it was published, so none of it has a
name, a version or a hash anybody could state — and the alternative to
saying so is inventing one.

The **SDK pin travels with it**: `mcuhome.package.sha256` is the empty
string, because the SDK this context builds is the same checkout. The
two are one form. A document that states the word and a real SDK hash,
or package entries and an empty one, is refused rather than read — a
context may not half-claim to be pinned. `mcuhome.constraint`,
`mcuhome.version` and `mcuhome.package.url` are informational as always
and may be empty or say whatever the writer knows.

Two consequences, and they are properties of the form rather than
policies anyone applies to it:

- **Such a context is not reproducible.** Its ID covers the files, the
  board and the word — never the bytes it was compiled against — so two
  developer contexts over the same files share an identity while the
  firmware they produce need not be the same. What it identifies is its
  *inputs*; it can never attribute an artifact the way the package form
  does.
- **Such a context is not remote-buildable.** Whoever runs a context
  finds an environment by the packages it names, and this one names
  none. A build server refuses it, and the party that writes one refuses
  to send it.

### Where the two pins come from

This is about the party that *writes* a context, not about the one that
reads it: a reader finds three resolved values per entry and needs to
know nothing about how they were arrived at. It is written down because
"pinned, not requested" only means something if the pinning has a rule.

The **versions** come from the SDK release. Every release carries a file
`build-environment.lock.json` — inside the SDK package and, byte for
byte the same document, beside it — stating the build environment it was
built and tested with. Its shape is the abstract package set of the
[build environment specification](build-environment-specification.md)
§5.1: one `packages.<name>` member per package, value `<version>`, no
hashes.

```json
{
  "packages.mcuhome-build-tools": "0.1.10.dev1",
  "packages.mcuhome-build-workspace": "0.1.10.dev1"
}
```

It carries no hashes because at that moment nobody could: the workspace
package is built *from* the SDK's own tag, and the tools package's bytes
differ per platform. So the **hashes** come from the package host's
index, where a version resolves to bytes — a concrete package's archive
hash, or a meta entry's hash over the members it points at. Whoever
writes a context resolves the lock's versions against an index it trusts
and writes the triples out.

A device may override either entry, and then that entry's version is the
device's and only the hash is looked up. An override that states a hash
as well decides the whole entry and nothing is looked up at all, which is
what lets a context be created with no index in reach.

None of this is in the context. A context states what was decided, and
two contexts with the same six values are the same environment however
either party got there.

## 5. `manifest.yaml` — the lock

The request restated, plus the two things that do not exist until the
file set is final:

```yaml
context: 4
mcuhome:
  constraint: ~=2.3.6
  version: 2.4.0
  package:
    url: https://mirror-1.packages.mcuhome.org/sdk/mcuhome-sdk-2.4.0.tar.zst
    sha256: 9d1c…
build_environment:
  workspace:
    name: mcuhome-build-workspace
    version: 2.4.0
    sha256: 7c31…
  tools:
    name: mcuhome-build-tools
    version: 1.2.0
    sha256: b90a…
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

A lock restates whichever form the request used, unchanged. For the
developer form (§4) that is the word and the empty SDK hash:

```yaml
context: 4
mcuhome:
  constraint: ""
  version: ""
  package:
    url: ""
    sha256: ""
build_environment: developer
target:
  board: nrf7002dk/nrf5340/cpuapp
files:
  - path: build-context.json
    sha256: 4175…
  - path: keys/signing.pub
    sha256: 8ab0…
  - path: model/device-model.json
    sha256: 22cd…
id: sha256:c8e7…
```

The `id` differs from the package-form example above it although the
files and the board are the same, and that is §6 working: the form is
hashed, so a context that names its environment and one that cannot are
never the same context.

## 6. The context ID

`id` names the context by its content. It is the SHA-256, written
`sha256:<64 lowercase hex digits>`, of the UTF-8 encoding of this
document in RFC 8785 canonical JSON:

```json
{"build_environment":{"tools":{"name":"…","sha256":"…","version":"…"},
                      "workspace":{"name":"…","sha256":"…","version":"…"}},
 "files":[{"path":"…","sha256":"…"}],
 "sdk":{"sha256":"…"},
 "target":{"board":"…"}}
```

For the **developer form** (§4) the same four members are hashed and two
of them are what that form says: `build_environment` is the JSON string
`"developer"` in place of the object, and `sdk.sha256` is the empty
string. Nothing else changes — the member is not omitted, the `sdk`
object keeps its shape, and the whole document is canonicalised exactly
as above:

```json
{"build_environment":"developer",
 "files":[{"path":"…","sha256":"…"}],
 "sdk":{"sha256":""},
 "target":{"board":"…"}}
```

A conformance vector for it, complete, so a second implementation can
check itself against a number rather than against a description — board
`nrf52840dk/nrf52840`, one file `model/device-model.json` with
`sha256` = `c` × 64:

```json
{"build_environment":"developer",
 "files":[{"path":"model/device-model.json","sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}],
 "sdk":{"sha256":""},
 "target":{"board":"nrf52840dk/nrf52840"}}
```

```
id = sha256:0eb27f7b019ef3fb730628278557c120f9b73e7335e80adbe006cb24e0a7b4a5
```

The same inputs in the package form are the "one file" vector of
MCUHome's own table (`mcuhome.model.context.CONTEXT_ID_VECTORS`), and the
two IDs differ, which is the point: a context that names its environment
and one that cannot are never the same context.

`files` is sorted by `path` in ascending byte order of its UTF-8
encoding, which for these names is a plain string sort.

Four things are hashed and nothing else: the SDK's content hash, the
environment — both packages as the full `(name, version, sha256)` triple
§4 gives them, or the word — the board, and every file with its own
hash.

The two environment entries are hashed **the same way**, and that is the
point rather than a tidiness. A tools pin's `name` decides what the hash
means — the family name says "resolve this per platform", a suffixed name
says "these exact bytes, on this platform only" — so the name has to be
inside the ID or two different builds would share one. Once the name is in
for one entry there is no reason to leave it out of the other, and one
shape for both is one rule to implement twice rather than two.

Deliberately outside it are `created`, `mcuhome.constraint`,
`mcuhome.version` and every `url` — a timestamp, an intent, and hints
about where bytes were found rather than which bytes they are.

**The rule is frozen with format version 4.** Everything that ever names a
context depends on computing the same ID from the same inputs forever, so
a field can join the hashed document only together with a new format
version. Version 4 is still an unreleased draft and this rule has been
changed inside it twice: when the environment reference became a package
set and then a pair of triples, and on 2026-09-07 when the developer form
was added — the second change left every package-form ID exactly as it
was, because it only says what the two members are for a context that has
no packages to name. Nothing that shipped ever computed any older shape.
From the moment version 4 is released, this is what it means. Both parties to a
build compute the ID independently, from the bytes they actually hold; a
declared `id` is advisory, like every declared value.

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
lives; how a patch is applied — and where the patched tree ends up, which
depends on the environment's profile — is the specification's §10.

## 10. Versioning

The `context` key in both YAML documents is this format's version. A
reader that does not implement the version it finds refuses — the
specification calls that `unsupported`, and it is a legible answer an
orchestrator can act on by choosing a different environment.

The version rises whenever a reader written for the previous one would
get something wrong. Adding a file or an optional field does not raise
it: readers ignore what they do not know, which is what makes such a
change additive.

Version 4 is where the environment reference became a package set. That
moved `build_environment` from one string to a structure and changed what
the context ID hashes, and a reader of version 3 would get both wrong —
which is precisely when the number goes up.

Version 4 is a **draft** and has not been released. It has already changed
within itself twice. First the tools entry lost its `platforms` map and
became the same `(name, version, sha256)` triple as the workspace entry,
and the context ID's hashed document changed with it. Then, on
2026-09-07, `build_environment` gained its second form — the word
`developer`, with the empty SDK hash beside it (§4) — because a build
against a workspace somebody maintains themselves has no package set and
no packed SDK, and the format had no way to say so without a hash
somebody made up. That second change added a form rather than reshaping
one: every ID a package-pinned context had before it, it has after it.
The number did not move for either, because a draft has no readers to get
anything wrong — but it is worth saying plainly rather than leaving a
reader of an intermediate copy to work it out. Once version 4 is
released, §6's rule is fixed for good and the next change is version 5.
