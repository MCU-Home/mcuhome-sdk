# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The shared vocabulary: what MCUHome's parties must agree about.

The device model and its JSON form, the registry (boards, drivers,
clusters, partitions, update schemes), the context and build-manifest
formats including the frozen ID rule, the OTA version arithmetic, the
commissioning credentials and the curve under them, the
error types, and the file hash a build context and a build environment
share.
**No build machinery** — nothing here compiles, generates, or drives a
west workspace, and nothing here reads the process it runs in.

It is the package every execution site needs and none of them may
re-derive. Most sharply the context ID: a build server recomputes it
from bytes it received while carrying no build logic at all, and the
entire purpose of that value is that independent parties arrive at the
same one.

===================================  =================================================
:mod:`mcuhome.model.model`           the canonical device model
:mod:`mcuhome.model.registry`        the static tables everything validates against
:mod:`mcuhome.model.context`         the remote-build context format
:mod:`mcuhome.model.artifacts`       one declared artifact, as every build reports it
:mod:`mcuhome.model.ota`             the device version, and what derives from it
:mod:`mcuhome.model.jobs`            how many compile jobs this machine sustains
:mod:`mcuhome.model.signing`         the imgtool arguments an image is signed with
:mod:`mcuhome.model.pairing`         commissioning credentials, as one atomic group
:mod:`mcuhome.model.p256`            the curve arithmetic pairing and signing share
:mod:`mcuhome.model.export`          the registry, as data
:mod:`mcuhome.model.toolchain`       Zephyr line and blob resolution
:mod:`mcuhome.model.hashes`          the one file hash both sides of a build compute
:mod:`mcuhome.model.userpaths`       per-user directories, from a given environment
:mod:`mcuhome.model.errors`          the error type and its plain-language rendering
===================================  =================================================
"""

from pathlib import Path as _Path

#: The generated file this package is shipped with, beside this module.
#: It holds the bare version string and nothing else. It is **written by
#: the build, never committed** (``.gitignore``): ``scripts/
#: build_sdk_archive.py`` writes it into the SDK archive and
#: ``packaging/model``'s build backend writes it into the wheel, both
#: from the definition file below — so an artifact states the version it
#: was cut from even where that definition is not shipped with it.
_VERSION_FILE = "VERSION"

#: The definition file of the three release lines, relative to the
#: repository root: ``sdk.version`` is this distribution's. Restated here
#: as a path rather than imported, because this module is the root of the
#: import chain and runs before anything else in the package exists.
_ENVIRONMENT_FILE = ("packaging", "build-environment", "environment.json")


def _declared_version(package_dir: _Path) -> str:
    """The version of this release, for a copy of this package at *package_dir*.

    Two sources, in this order, and never a literal: an installed
    distribution carries the generated ``VERSION`` beside this module, and
    a checkout carries none — there the answer is ``sdk.version`` of
    ``packaging/build-environment/environment.json``, which is where the
    three release lines of this repository are declared and the only place
    this number is written down.

    The order is what makes both true at once. A wheel or an SDK archive
    has no definition file to read, and a working tree must not be able to
    answer with a stale generated file from an earlier build, which is why
    the generated one is never committed.
    """
    generated = package_dir / _VERSION_FILE
    if generated.is_file():
        return generated.read_text(encoding="utf-8").strip()
    definition = package_dir.parents[1].joinpath(*_ENVIRONMENT_FILE)
    if definition.is_file():
        import json  # noqa: PLC0415 - only a checkout gets this far

        return str(json.loads(definition.read_text(encoding="utf-8"))["sdk"]["version"])
    raise RuntimeError(
        f"mcuhome.model cannot say which version it is: neither {generated} "
        f"nor {definition} exists."
    )


#: The version of the whole release, and the only place it is *read* from.
#: The release gives ``mcuhome-model``, ``mcuhome-workbench`` and
#: ``mcuhome-compiler`` one shared version, one tag and one release; this
#: package is the root of the dependency chain, so it is where that number
#: is answered. The ``pyproject.toml`` files under ``packaging/`` read it
#: from here (``dynamic = ["version"]``), and it is derived rather than
#: written down so that no second copy exists to disagree with the
#: definition file.
__version__ = _declared_version(_Path(__file__).resolve().parent)
