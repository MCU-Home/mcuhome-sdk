# MCUHome Build Environment Specification

**Spec generation 3.** Draft — not yet released.

A **build environment** turns a MCUHome build context into firmware. This
document is everything you need to build your own one, and everything your
build environment may rely on in return.

Anything not written here is deliberately yours: where your source trees
live, what your packages contain, which compiler and build system you use,
and how you do the work. This specification describes a boundary, not a
build.

Two things it deliberately leaves to the orchestrator — which actions
exist, and what is in a build context — are documented separately,
because they belong to whoever drives your environment rather than to
the boundary itself. For MCUHome that is
[build actions](build-actions.md) and the
[build context format](build-context-format.md).

## 1. Terms

| Term | Meaning |
|---|---|
| **build environment** | A set of packages that satisfies this specification. |
| **package** | One distributable archive with a name, a version, and a content hash. |
| **package set** | The packages one environment is made of. It is the environment's identity: the same set, however it was delivered, is the same environment. |
| **entry point** | The executable the orchestrator runs, at a fixed path in your environment. |
| **builder** | The process started from your entry point, and everything it spawns. |
| **orchestrator** | The MCUHome software that runs your build environment. You never talk to it directly; you exchange two files with it. |
| **session** | A sequence of steps that together produce one set of artifacts. |
| **step** | One execution of the entry point. Steps of a session run one after another, never at the same time. Also called an *invocation*. |
| **build context** | The resolved input of a build: what to build, for which board, with which settings. |
| **generator** | The tool that produced the build context. MCUHome's own is `mcuhome-workbench`. |

There are two **profiles**. In the **container profile** your environment
is delivered as a container image and runs as a container. In the
**subprocess profile** its packages are unpacked into a read-only store on
the host and the entry point is run as an ordinary process. Every rule in
this document applies to both unless a paragraph says otherwise.

## 2. The package set

> **A build environment is defined by its packages, not by an image.**

An environment is a named, versioned, hash-identified set of packages.
Which packages, and how many, is yours to decide; what this specification
fixes is that the set exists, that the environment declares it (§5), and
that the set — not the wrapping it arrives in — is what identifies the
environment.

MCUHome's own environment is two packages:

| Package | Architecture | Content |
|---|---|---|
| `mcuhome-build-workspace` | neutral | The materialized source world: the framework and its dependencies at pinned revisions, the base patches already applied, binary blobs already fetched, generated code that is a release constant already generated, plus a record of what was resolved. |
| `mcuhome-build-tools_<os>-<arch>` | one per platform | The host tools: compiler toolchain, build system, workspace tool, and the Python wheel set the build's virtual environment is created from. |

The split is by cadence, not by taste: the source world moves with every
release, the tools move rarely and are large.

A third package, `mcuhome-sdk`, is **not** part of the environment. Each
build context chooses its own SDK version; the orchestrator delivers it at
`mcuhome/sdk` (§4). An environment builds whatever SDK it is handed, and
does not have an opinion about which one that is.

**Two profiles, one set.** In the container profile the orchestrator
selects a container image that declares exactly the package set the build
context asks for; the image is a delivery of the set, and two images built
from the same set are the same environment. In the subprocess profile the
orchestrator provisions the same packages into a read-only store on the
host and runs the entry point from there. Neither profile is privileged:
an environment that behaves differently in one of them is broken, not
clever.

Which of your packages carries the entry point is your business too. The
specification fixes only the path it is found at, and that the profile in
use puts it there — as image content in the container profile, from the
store in the subprocess profile.

## 3. The environment at the start of a step

> **At the start of every step, the environment's source trees are
> pristine: exactly what its packages define, with nothing an earlier step
> did left in them. Only the directories under `mcuhome/` listed in §4 are
> managed by the orchestrator.**

How that is achieved is not your concern and differs by profile: the
container profile starts a fresh container per step, the subprocess
profile keeps the store read-only and hands every step fresh directories
under `mcuhome/`. Steps of a session run strictly one after another, never
at the same time.

Three consequences follow, and they are the whole mental model:

- **Nothing you write survives a step** — except what you put in
  `mcuhome/out` and in the writable cache tiers.
- **You never have to clean up after yourself.** There is nothing left
  over from the last step, nothing to undo, and nothing that could be
  applied twice.
- **What you may modify depends on the profile.** Trees that are
  disposable may be changed freely; a read-only store may not be touched
  at all. §10 says how patches are handled under both, and the habit that
  follows from it is simply: work in `work`.

## 4. The filesystem tree

The orchestrator sets the environment variable **`MCUHOME_BUILDER_BASE_DIR`**
to an absolute path. It is the only environment variable this
specification defines, and the root of everything below:

```
$MCUHOME_BUILDER_BASE_DIR/
  mcuhome/
    bin/
      build-environment-entry        your executable — the entry point
    invocation-request.json          written by the orchestrator, read by you
    work/                            your scratch space for this step
    sdk/                             the MCUHome SDK
    build-context/                   the resolved build input
    out/                             the artifacts of this session
    cache/
      local/
      session/
      project/
      shared/
```

`MCUHOME_BUILDER_BASE_DIR` is often `/`, but never assume it. Resolve
every path against it at the start of **every** step, and never keep an
absolute path from one step to the next.

| Path | Written by | Present at step start | You may write |
|---|---|---|---|
| `mcuhome/bin/build-environment-entry` | you, in a package | your environment's content | — |
| `mcuhome/invocation-request.json` | orchestrator | this step's request | no |
| `mcuhome/work` | you | **empty** | yes |
| `mcuhome/sdk` | orchestrator | the SDK | assume no |
| `mcuhome/build-context` | orchestrator | the build context | **never** |
| `mcuhome/out` | you | what earlier steps left | yes |
| `mcuhome/cache/*` | mixed — see §8 | see §8 | see §8 |

Everything outside `mcuhome/` is your environment as its packages define
it. In the subprocess profile that is a read-only store, possibly shared
with other builds running right now, so treat it as read-only in both
profiles. `work`, `out` and `cache/local` are the places you are
guaranteed to be able to write.

## 5. Self-description

Your environment declares itself in the metadata of its packages. The
declaration is one JSON object; every member is a string:

| Member | Required | Value |
|---|---|---|
| `spec-generation` | yes | The generation of this specification your environment implements. Currently `3`. |
| `zephyr.version` | yes | The Zephyr version your environment builds against, as SemVer 2.0.0 — for example `4.4.0` or `4.5.0-rc.1`. |
| `build-context.generator-constraint` | yes | Which build contexts you accept. See §9. |
| `build-context.generator-constraint-mode` | no | `strict` (default) or `chain`. See §9. |
| `packages.<package name>` | one per package, at least one | One package of the set: its version and its content hash where the declaring side knows them, or — on a family — the range the set is resolved within. See §5.1. |

```json
{
  "spec-generation": "3",
  "zephyr.version": "4.4.0",
  "build-context.generator-constraint": "mcuhome-workbench:~=1.0.5",
  "packages.mcuhome-build-tools": "1.2.0",
  "packages.mcuhome-build-workspace": "2.4.0"
}
```

There is deliberately no list. One member per package means a package can
be named, added or looked up on its own — in a JSON object, in an image's
labels, in a query — without anybody having to parse a string that packs
several packages into one value.

One package of the set carries the declaration and it speaks for the whole
set. For MCUHome's own environment that is `mcuhome-build-workspace`, the
architecture-neutral one: a set that spans architectures needs a carrier
that does not.

Member names not defined here are reserved, exactly as §5.2 reserves
their label mirrors; names prefixed `x-` are free.

### 5.1 The package members

A member `packages.<package name>` names exactly one package or one
family, and its value states which version, and — where the declaring side
knows them — which bytes:

```
<version>
<version>@sha256:<64 lowercase hex digits>
<constraint>
```

`<version>` is a PEP 440 version; `<constraint>` is a PEP 440 specifier
set such as `~=1.2.0`. A value beginning with a comparison operator is a
constraint and never a version, which is unambiguous because a version
begins with an alphanumeric.

**A constraint may only name a family, and never carries a hash.** It
states a *range* the set is resolved within rather than one member of it,
so there are no bytes for it to pin. Which package inside that range an
environment is delivered with is decided by whoever assembles the
environment, at the moment it is assembled — see the two rules below.

**The package name.** Lowercase alphanumerics and `-`, optionally followed
by an architecture suffix introduced by `_`:

```
<name>    ::= <part> [ "_" <part> ]
<part>    ::= [a-z0-9] [a-z0-9-]*
```

`_` appears in a package name for that one purpose, so the first `_`
unambiguously splits the family name from the platform it was built for:
`mcuhome-build-tools_linux-amd64` is the `linux-amd64` build of the
`mcuhome-build-tools` family. A reader that wants the family takes what
is in front of the first `_`; one that wants the exact package takes the
whole name. Where the members are written out one after another — as a
label list, as a rendering of the declaration — they are sorted by member
name in ascending byte order, so that two writers of one set produce one
text.

**A declaration in package metadata states the abstract set.** It is
written when the package is built, and two things are then unknowable:

- The carrier cannot state its own hash. The declaration is inside the
  archive it would be describing, so the hash would have to cover bytes
  that contain it. Its own entry carries the version alone.
- An architecture-specific package is named by its **family**
  (`mcuhome-build-tools`). Its bytes differ per platform on purpose, and
  the sibling platforms' archives may not exist yet when the carrier is
  built.
- A family member may state a **constraint** instead of a version, and
  then it states which versions of that family the set may be resolved
  within. This is what lets the two lines move on their own cadences: a
  package released inside the declared range reaches an environment
  without the carrier being republished, and a carrier that named one
  version would have frozen a choice it has no way to revisit.

**A delivery states the concrete resolved set, hashes and all.** An image
(§5.2), or any other assembly of exact bytes, knows precisely what it
unpacked: it **must** carry a hash on every member, it **must not** carry
a constraint — it resolved every range it was given and names the result —
it completes the carrier's own entry with the hash of the archive it took,
and it replaces the family entry with the one concrete package it actually
contains —

```
packages.mcuhome-build-workspace   = 2.4.0@sha256:7c31…
packages.mcuhome-build-tools_linux-amd64 = 1.2.0@sha256:b90a…
```

— which is also why an image is per platform while a package set is not.

Whoever matches a set matches what is stated: hashes where they are given,
a version inside the range where a range is given, name and version
otherwise. So the abstract declaration matches every delivery of that set,
and a concrete one matches only its own bytes.

### 5.2 Container images mirror the declaration

An image delivering the environment repeats every member of the
declaration as an OCI label `org.mcuhome.build-environment.<member>`, with
the identical value — the package members included, one label per package:

```dockerfile
LABEL org.mcuhome.build-environment.spec-generation="3" \
      org.mcuhome.build-environment.zephyr.version="4.4.0" \
      org.mcuhome.build-environment.build-context.generator-constraint="mcuhome-workbench:~=1.0.5" \
      org.mcuhome.build-environment.packages.mcuhome-build-workspace="2.4.0@sha256:7c31…" \
      org.mcuhome.build-environment.packages.mcuhome-build-tools_linux-amd64="1.2.0@sha256:b90a…"
```

An image is a delivery, so §5.1's second rule applies to it in full: every
`packages.` label carries a hash, and the tools family is named as the one
concrete package the image contains rather than as a family. The image
builder is the party that can do this — it is holding the archives.

The `packages.` labels are what make an image findable. A build context
references packages, never an image; the orchestrator looks for the image
whose package labels are the set it wants and starts it. An image whose
package labels say something else is a different environment, whatever is
otherwise inside it — and an image assembled from packages it does not
declare is simply lying about what it is.

**Every other name under `org.mcuhome.build-environment.` is reserved.**
Do not invent one — a future generation of this specification may define
it and mean something else. Names prefixed `x-` are free for testing,
development and your own experiments; MCUHome uses them the same way, to
try a feature out before a later generation adopts it properly.

Labels **outside** that prefix are yours entirely. If you want to publish
feature flags of your own, do it under a name you control.

The orchestrator reads the declaration before it starts anything: from the
package metadata when it provisions packages, from the image configuration
when it runs an image. Both must say the same thing.

## 6. The invocation

The orchestrator runs your entry point **once per step**:

```
$MCUHOME_BUILDER_BASE_DIR/mcuhome/bin/build-environment-entry
```

with **no arguments**. Do not rely on the working directory. Everything
the step is about is in the request document.

The entry point must be executable by the user the orchestrator runs it as.

### 6.1 The request document

`mcuhome/invocation-request.json`, one JSON object, UTF-8:

```json
{
  "spec_generation": 3,
  "session_id": "9f2c1a",
  "invocation_id": "9f2c1a-3",
  "action": "build",
  "parameters": {},
  "limits": { "cpus": 4, "memory_bytes": 8589934592 }
}
```

| Field | Meaning |
|---|---|
| `spec_generation` | The generation the orchestrator is speaking. If it is not one you implement, refuse with `unsupported`. |
| `session_id` | Identifies the session. Opaque — never build a path from it. |
| `invocation_id` | Identifies this step. Safe to use directly in a filename. |
| `action` | What to do. Which actions exist is not part of this specification; the orchestrator documents them — MCUHome's are in [build actions](build-actions.md). |
| `parameters` | Arguments for the action, or `{}`. |
| `limits` | What this step should keep itself within, or absent. See below. |

Ignore fields you do not know.

#### `limits` — what the step is expected to fit in

An optional object, with two optional members:

| Member | Meaning |
|---|---|
| `cpus` | How much CPU time the step should use, as a number of cores. Fractional values are allowed, and mean what they mean everywhere else: `1.5` is one and a half cores' worth of time, not two cores half the time. |
| `memory_bytes` | How much memory the step should use, as a whole number of bytes. |

**It is a recommendation, and the orchestrator is not asking politely.**
Honour it: size your parallelism from it rather than from what the
machine appears to have, because what the machine appears to have is not
what you were given. You *may* exceed it — a link that needs more memory
than one job's share is a normal thing to do — but expect the
orchestrator to enforce its own hard limits from outside, and those it
does not negotiate (§11): it may kill a process of yours, or the whole
step, at any point. An environment that treats the recommendation as the
budget it plans with is the one that never finds out how that is
implemented.

**Absent means: decide for yourself.** An orchestrator that states no
limits has said nothing about the machine, so size the build the way you
would on a machine of your own.

`MCUHOME_BUILDER_BASE_DIR` remains the only environment variable this
specification defines (§4). Limits travel in this document and nowhere
else — an orchestrator that put them in the environment would be adding
to a boundary that is deliberately one variable wide.

### 6.2 The result document

Every step writes `mcuhome/out/result-<invocation_id>.json`, one JSON
object, UTF-8, as its last action:

```json
{
  "spec_generation": 3,
  "invocation_id": "9f2c1a-3",
  "status": "success",
  "message": "",
  "artifacts": ["firmware.bin", "firmware.hex", "build-report.json"]
}
```

| Field | Meaning |
|---|---|
| `spec_generation` | The generation you are speaking. |
| `invocation_id` | Copied from the request. |
| `status` | `success`, `failure`, or `unsupported`. |
| `message` | Free text for a human. Empty on success. |
| `artifacts` | The files **this step** wrote into `out/`, as paths relative to `out/`. |

`unsupported` means *no environment of my kind can do this* — an action
you do not implement, a spec generation you do not speak, a build context
you do not understand. It tells the orchestrator to look for a different
environment rather than report a broken build. Everything else that goes
wrong is `failure`.

### 6.3 Exit code

Exit `0` when you wrote a result document with `status: "success"`, and
non-zero otherwise.

The orchestrator reads the result document whenever it exists, whatever
the exit code. A step that produced no readable result document failed,
whatever it exited with.

## 7. `work` and `out`

**`work` is empty at the start of every step.** It is your scratch space:
unpack, generate, configure, compile there. Point `TMPDIR` at a directory
inside it if the tools you drive need one. It is also where a patched copy
of a read-only tree goes (§10).

**`out` is created empty when the session starts and survives every step
of it.** It holds the artifacts of the whole session — and it is the only
thing that reaches the next step. That makes it worth some discipline:

Rules:

- Write your result document to `out/result-<invocation_id>.json`. Names
  matching `result-*.json` at the top of `out` are reserved for it.
- Put only regular files and directories in `out`. No symlinks, hard
  links, device nodes, sockets or FIFOs — the orchestrator rejects them,
  because these files travel to other people's machines.

Advice, because a session's output is a shared space and you cannot see
the other steps:

- Build in `work` and copy the finished file into `out`. `out` is not
  scratch space.
- Name artifacts for what they are: `firmware.bin`, not `image.bin`. You
  do not know what else is in there. Where the orchestrator's action
  vocabulary fixes a name, that name wins.
- Assume nothing exists. Check what your step needs **before** you start
  long work, and fail immediately if something is missing — nobody
  benefits from a twenty-minute compile that ends at a signature file
  that was never there.
- Do not overwrite what you did not write.
- If you need an artifact an earlier step produced, copy it into `work`
  and work on the copy.

You may delete a file in `out` that your step has replaced — if your step
turns a plain firmware image into an update package, removing the
intermediate keeps the session's result honest. Be sure before you do.

## 8. Caches

Four tiers. They exist so that a build can be fast; a build must be
**correct without any of them**.

| Tier | Belongs to | Writable | Survives |
|---|---|---|---|
| `cache/local` | you | **always** | nothing — it is per step |
| `cache/session` | orchestrator | maybe | the session |
| `cache/project` | orchestrator | maybe | sessions of the same project |
| `cache/shared` | orchestrator | assume not | anything, and nothing |

At the start of a step, `local` holds either nothing or whatever your own
packages put there — the orchestrator may or may not mount something over
it. Either way it is yours, it is writable, and it is gone afterwards.

Assume every tier except `local` is read-only, may be missing entirely,
and may change between steps. `shared` in particular belongs to the
orchestrator: files can appear, vanish, or be completely different from
one step to the next. `project` may be in use by another build of the
same project at the same time.

The orchestrator guarantees that a stranger cannot reach your `session`
and `project` tiers. It guarantees nothing about `shared`, and nothing
about other build environments used within your own project.

**So: treat cached data as a hint, never as trusted input.** A wrong or
corrupt cache entry must not be able to change what you produce. Content
addressed caches such as ccache give you this for free.

Where things go:

- **ccache** goes in `<tier>/ccache`.
- Anything else of yours goes in `<tier>/private/<namespace>`, where
  `<namespace>` is something you own — a domain, or your environment's
  name.

Use the most local writable tier as your primary cache and the rest as
read-only secondaries. Which tiers an orchestrator actually provides is
its operator's decision.

## 9. The build context

The build context is at `mcuhome/build-context`. **Never modify anything
in it.** Its structure belongs to the tool that produced it, not to this
specification — MCUHome's own is the
[build context format](build-context-format.md) — but two things about
it are fixed here, because the orchestrator depends on them.

**First**, the context contains a file `build-context.json`, one JSON
object, carrying at least:

```json
{ "generator": "mcuhome-workbench:1.2.0" }
```

**Second**, the format of that value. It is a chain of
`<product>:<version>` entries separated by semicolons, **most recent
writer first**:

```
custom-tool:4.2.3;other-tool:1.0.1;mcuhome-workbench:0.1.0
```

The leftmost entry is the tool that touched the context last. Entries to
its right touched it before, ending with the tool that created it. By
listing the others, a tool *claims* to have kept the context compatible
with them.

`<product>` is lowercase, `[a-z0-9][a-z0-9._-]*`. `<version>` is a
PEP 440 version.

A context without `build-context.json`, or without a readable `generator`
in it, is not a valid build context; the orchestrator refuses it before
your environment is started.

### 9.1 Declaring which contexts you accept

`build-context.generator-constraint` is a list of
`<product>:<specifier>` entries separated by semicolons, where
`<specifier>` is a PEP 440 version specifier:

```
mcuhome-workbench:~=1.0.5;custom-tool:==0.5.3;custom-tool:~=0.6.2
```

An entry `<product>:<version>` **matches** if the constraint declares at
least one specifier for that product that the version satisfies. Several
specifiers for the same product are alternatives, as `custom-tool` is
above.

As PEP 440 requires, a specifier **excludes pre-releases unless it asks
for them**: `>=1.0` does not match `1.1rc1`.

`build-context.generator-constraint-mode` selects how the chain is read:

| Mode | Check |
|---|---|
| `strict` (default) | Only the leftmost entry must match. The claim a tool makes about the entries to its right is not trusted. |
| `chain` | Walk the chain from left to right and accept at the first entry that matches. |

An empty specifier accepts every version of that product, so
`mcuhome-workbench:` means "any build context the workbench produced".

The orchestrator runs this check before **every** step, against the
generator chain in the build context. This is why the constraint is a
required member of your declaration: an environment that declares none
accepts nothing and can never pass.

When the check fails you are not started at all and never see the context.
You may still refuse a context yourself, for a reason a version constraint
cannot express — answer `unsupported`.

## 10. Patches

A build context may carry patches for source trees in your environment.
Where those trees are is your business; applying the patches is your job,
because you are the only one who knows where they live.

So that two build environments produce the same tree from the same patch
files:

- A patch file is a unified diff.
- It is applied with `-p1` semantics relative to the root of the tree
  being patched: strip the first path component, resolve the rest against
  that root.
- Patches for one tree are applied in ascending order of their file names,
  each on top of the result of the last.
- A patch that does not apply fails the step. Do not retry at another
  strip level and do not apply it partially.

Which tool you use — `git apply`, `patch`, your own implementation — is
up to you.

**Where the patched tree lives depends on your profile.** The trees are
pristine at the start of every step (§3) and have to be pristine again for
the next one, so there is nothing to undo — but there is something not to
break:

- A tree that is **disposable** — the container profile's, which goes away
  with the container — may be patched in place.
- A tree in a **read-only store** — the subprocess profile's — must not be
  touched. Materialize a patched copy of it under `work`, and build
  against a view of the environment in which that copy stands in for the
  original. Trees no patch names stay where they are; copying is the price
  of a patch and is paid only for the trees a patch actually names.

Either way the packages' content is never durably modified, and the next
step starts from pristine trees again.

## 11. What you must not assume

- **No network.** Never require it. Everything a build needs is in your
  packages, in `mcuhome/sdk`, or in the build context. An orchestrator may
  cut off the network, and often will.
- **Not everything is writable.** Outside `work`, `out` and `cache/local`,
  assume read-only — your own trees included.
- **Limits are enforced.** Whatever CPU, memory, disk and time budget the
  orchestrator has set, it may enforce hard. Be prepared to be killed;
  behave accordingly. Where it tells you the budget it is holding you to,
  it does so in the request document's `limits` (§6.1) — that is the one
  place to read it, and reading it is how you avoid finding out the rest
  of this rule.
- **Nothing survives a step** except `out` and the writable cache tiers.
- **You are not alone.** Another build of the same project may be running
  against the same `project` cache right now — and in the subprocess
  profile, against the same store.

## 12. Spec generations

The generation is a single number. It goes up by one whenever this
specification changes in a way that an environment built for the previous
generation would get wrong. Anything that only adds — a new optional
member of the declaration, a new optional field in either document, a new
action — does not raise it.

Generation 3 is the current one. The two before it were drafts that were
never released and that no environment implements; the number keeps
counting rather than restarting, so that a generation number means one
thing forever.

Your environment declares the generation it implements (§5); the
orchestrator states the generation it speaks in every request document. If
they cannot agree, the side that notices refuses: the orchestrator does not
start an environment whose generation it does not implement, and your entry
point answers `unsupported` to a request generation it does not implement.

In both documents, **ignore fields you do not know**. That is what makes
an additive change additive.
