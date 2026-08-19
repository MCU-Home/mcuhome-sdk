# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Performing a build: stages 4-5, and the invocation-ABI adapter.

Code generation, west orchestration, artifact collection and the build
report — plus the adapter that turns a build container's invocation into
a build of this same code. It is an adapter and only an adapter: it
implements no build step of its own, so what an invocation runs and what
the modules below do cannot drift apart.

Where it runs is what defines it. This package **ships inside the SDK
package** and executes in the build container out of the mounted SDK,
which is what makes "bring your own build container" mean own toolchain
and own Zephyr rather than own build logic. It is therefore not "the back
half of the pipeline" but a deliverable of the SDK — and a consumer that
only ever drives builds through a container never installs it at all.

=====================================  ======================================
:mod:`mcuhome.compiler.generate`       stage 4: the per-device build tree
:mod:`mcuhome.compiler.workspace`      stage 5: compile in a west workspace
:mod:`mcuhome.compiler.report`         stage 5: what to sign the result with
:mod:`mcuhome.compiler.abi`            the invocation ABI of the build container
:mod:`mcuhome.compiler.sdkentry`       the SDK package's ``generate`` entry point
=====================================  ======================================

**Driving a container is not here.** The orchestrator that starts one and
speaks the ABI to the program inside is ``mcuhome.workbench``'s: this
package *is* that program, and one distribution playing both roles could
be replaced by neither half.
"""
