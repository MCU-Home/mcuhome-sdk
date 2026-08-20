# MCUHome Build Environment Specification

**Spec generation 1.** Draft — not yet released.

A **build environment** turns a MCUHome build context into firmware. This
document is everything you need to build your own one, and everything your
build environment may rely on in return.

Anything not written here is deliberately yours: where your source trees
live, what your image contains, which compiler and build system you use,
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
| **build environment** | An OCI image that satisfies this specification. |
| **entry point** | The executable the orchestrator runs, at a fixed path in your image. |
| **builder** | The process started from your entry point, and everything it spawns. |
| **orchestrator** | The MCUHome software that runs your build environment. You never talk to it directly; you exchange two files with it. |
| **session** | A sequence of steps that together produce one set of artifacts. |
| **step** | One execution of the entry point. Steps of a session run one after another, never at the same time. Also called an *invocation*. |
| **build context** | The resolved input of a build: what to build, for which board, with which settings. |
| **generator** | The tool that produced the build context. MCUHome's own is `mcuhome-workbench`. |

There are two **profiles**. In the **container profile** your image runs as
a container. In the **subprocess profile** your image is unpacked into a
directory and the entry point is run as an ordinary process. Every rule in
this document applies to both unless a paragraph says otherwise.

## 2. The environment at the start of a step

> **At the start of every step, your build environment is exactly what your
> image defines. Only the directories under `mcuhome/` listed in §3 are
> managed by the orchestrator.**

How that is achieved is not specified and is not your concern: in the
container profile the orchestrator starts a fresh container from your
image, in the subprocess profile it unpacks your image afresh. Either way
you get your image, unmodified, every time.

Two consequences follow, and they are the whole mental model:

- **Nothing you write survives a step** — except what you put in
  `mcuhome/out` and in the writable cache tiers.
- **You never have to clean up after yourself.** Modify your own trees
  freely; the next step will not see it.

## 3. The filesystem tree

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
| `mcuhome/bin/build-environment-entry` | you, in the image | your image content | — |
| `mcuhome/invocation-request.json` | orchestrator | this step's request | no |
| `mcuhome/work` | you | **empty** | yes |
| `mcuhome/sdk` | orchestrator | the SDK | assume no |
| `mcuhome/build-context` | orchestrator | the build context | **never** |
| `mcuhome/out` | you | what earlier steps left | yes |
| `mcuhome/cache/*` | mixed — see §7 | see §7 | see §7 |

Everything outside `mcuhome/` is your image, as you built it.

## 4. Labels

Your image declares itself with OCI labels under
`org.mcuhome.build-environment.`:

| Label | Required | Value |
|---|---|---|
| `spec-generation` | yes | The generation of this specification your environment implements. Currently `1`. |
| `zephyr.version` | yes | The Zephyr version your environment builds against, as SemVer 2.0.0 — for example `4.4.0` or `4.5.0-rc.1`. |
| `build-context.generator-constraint` | yes | Which build contexts you accept. See §8. |
| `build-context.generator-constraint-mode` | no | `strict` (default) or `chain`. See §8. |

```dockerfile
LABEL org.mcuhome.build-environment.spec-generation="1" \
      org.mcuhome.build-environment.zephyr.version="4.4.0" \
      org.mcuhome.build-environment.build-context.generator-constraint="mcuhome-workbench:~=1.0.5"
```

**Every other name under `org.mcuhome.build-environment.` is reserved.**
Do not invent one — a future generation of this specification may define
it and mean something else. Names prefixed `x-` are free for testing,
development and your own experiments; MCUHome uses them the same way, to
try a feature out before a later generation adopts it properly.

Labels **outside** that prefix are yours entirely. If you want to publish
feature flags of your own, do it under a name you control.

The orchestrator reads these labels from the image configuration before it
starts anything — in both profiles, since unpacking an image means reading
its configuration.

## 5. The invocation

The orchestrator runs your entry point **once per step**:

```
$MCUHOME_BUILDER_BASE_DIR/mcuhome/bin/build-environment-entry
```

with **no arguments**. Do not rely on the working directory. Everything
the step is about is in the request document.

The entry point must be executable by the user the orchestrator runs it as.

### 5.1 The request document

`mcuhome/invocation-request.json`, one JSON object, UTF-8:

```json
{
  "spec_generation": 1,
  "session_id": "9f2c1a",
  "invocation_id": "9f2c1a-3",
  "action": "build",
  "parameters": {}
}
```

| Field | Meaning |
|---|---|
| `spec_generation` | The generation the orchestrator is speaking. If it is not one you implement, refuse with `unsupported`. |
| `session_id` | Identifies the session. Opaque — never build a path from it. |
| `invocation_id` | Identifies this step. Safe to use directly in a filename. |
| `action` | What to do. Which actions exist is not part of this specification; the orchestrator documents them — MCUHome's are in [build actions](build-actions.md). |
| `parameters` | Arguments for the action, or `{}`. |

Ignore fields you do not know.

### 5.2 The result document

Every step writes `mcuhome/out/result-<invocation_id>.json`, one JSON
object, UTF-8, as its last action:

```json
{
  "spec_generation": 1,
  "invocation_id": "9f2c1a-3",
  "status": "success",
  "message": "",
  "artifacts": ["mcuhome-firmware.bin", "mcuhome-firmware.hex"]
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

### 5.3 Exit code

Exit `0` when you wrote a result document with `status: "success"`, and
non-zero otherwise.

The orchestrator reads the result document whenever it exists, whatever
the exit code. A step that produced no readable result document failed,
whatever it exited with.

## 6. `work` and `out`

**`work` is empty at the start of every step.** It is your scratch space:
unpack, generate, configure, compile there. Point `TMPDIR` at a directory
inside it if the tools you drive need one.

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
- Name artifacts for what they are: `mcuhome-firmware.bin`, not
  `image.bin`. You do not know what else is in there.
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

## 7. Caches

Four tiers. They exist so that a build can be fast; a build must be
**correct without any of them**.

| Tier | Belongs to | Writable | Survives |
|---|---|---|---|
| `cache/local` | you | **always** | nothing — it is per step |
| `cache/session` | orchestrator | maybe | the session |
| `cache/project` | orchestrator | maybe | sessions of the same project |
| `cache/shared` | orchestrator | assume not | anything, and nothing |

At the start of a step, `local` holds either nothing or whatever your own
image put there — the orchestrator may or may not mount something over it.
Either way it is yours, it is writable, and it is gone afterwards.

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
  `<namespace>` is something you own — a domain, or your image's name.

Use the most local writable tier as your primary cache and the rest as
read-only secondaries. Which tiers an orchestrator actually provides is
its operator's decision.

## 8. The build context

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

### 8.1 Declaring which contexts you accept

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
generator chain in the build context. This is why the label is required:
an image that declares no constraint accepts nothing and can never pass.

When the check fails you are not started at all and never see the context.
You may still refuse a context yourself, for a reason a version constraint
cannot express — answer `unsupported`.

## 9. Patches

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

Apply patches to your trees in place. Your environment is fresh at the
start of every step (§2), so there is nothing to undo and nothing to
apply twice.

## 10. What you must not assume

- **No network.** Never require it. Everything a build needs is in your
  image, in `mcuhome/sdk`, or in the build context. An orchestrator may
  cut off the network, and often will.
- **Limits are enforced.** Whatever CPU, memory, disk and time budget the
  orchestrator has set, it may enforce hard. Be prepared to be killed;
  behave accordingly.
- **Nothing survives a step** except `out` and the writable cache tiers.
- **You are not alone.** Another build of the same project may be running
  against the same `project` cache right now.

## 11. Spec generations

The generation is a single number. It goes up by one whenever this
specification changes in a way that an environment built for the previous
generation would get wrong. Anything that only adds — a new label under
the reserved prefix, a new optional field in either document, a new
action — does not raise it.

Your image declares the generation it implements
(`org.mcuhome.build-environment.spec-generation`); the orchestrator states
the generation it speaks in every request document. If they cannot agree,
the side that notices refuses: the orchestrator does not start an
environment whose generation it does not implement, and your entry point
answers `unsupported` to a request generation it does not implement.

In both documents, **ignore fields you do not know**. That is what makes
an additive change additive.
