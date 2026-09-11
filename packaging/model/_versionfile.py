# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""setuptools, plus the one file a built ``mcuhome-model`` has to carry.

``mcuhome.model.__version__`` is derived, not written down: a checkout
answers from ``packaging/build-environment/environment.json``
(``sdk.version``), the definition file of this repository's three release
lines. An installed distribution has no such file — ``packaging/`` does
not ship in any wheel — so the build has to put the answer inside the
wheel instead, as ``mcuhome/model/VERSION``, which is what
:func:`mcuhome.model._declared_version` reads first.

Writing that file is the whole of this module, and it is a build backend
rather than a ``setup.py`` step for one reason: **an editable install must
not get it.** A ``VERSION`` left in the working tree would answer for the
tree forever, and the next bump of ``sdk.version`` would be invisible to
everything that imports the package — the exact drift the derivation
exists to end. PEP 517 separates the two cases by name, so the wrapper
below wraps ``build_wheel`` and ``build_sdist`` and hands
``build_editable`` through untouched.

For the same reason the file is removed again when the build is done: it
is generated, it is in ``.gitignore``, and the working tree it was written
into is a checkout that must keep answering from the definition file.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from setuptools import build_meta
from setuptools.build_meta import (
    build_editable,
    get_requires_for_build_editable,
    get_requires_for_build_sdist,
    get_requires_for_build_wheel,
    prepare_metadata_for_build_editable,
    prepare_metadata_for_build_wheel,
)

__all__ = [
    "build_editable",
    "build_sdist",
    "build_wheel",
    "get_requires_for_build_editable",
    "get_requires_for_build_sdist",
    "get_requires_for_build_wheel",
    "prepare_metadata_for_build_editable",
    "prepare_metadata_for_build_wheel",
]

#: This project directory's repository root — ``packaging/model/`` is two
#: levels down, and the source tree the distribution ships lives there
#: (``[tool.setuptools.package-dir]`` maps the empty prefix to it).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The definition file of the three release lines, and the member that is
#: this distribution's version.
ENVIRONMENT_FILE = REPO_ROOT / "packaging" / "build-environment" / "environment.json"

#: What the wheel carries instead, beside the module that reads it.
VERSION_FILE = REPO_ROOT / "mcuhome" / "model" / "VERSION"


def declared_version() -> str:
    """``sdk.version`` of the definition file, or a refusal naming it."""
    try:
        document = json.loads(ENVIRONMENT_FILE.read_text(encoding="utf-8"))
    except OSError as missing:
        raise SystemExit(
            f"{ENVIRONMENT_FILE} is missing — it declares the version this "
            "distribution is built at."
        ) from missing
    version = document.get("sdk", {}).get("version")
    if not isinstance(version, str) or not version:
        raise SystemExit(f"{ENVIRONMENT_FILE} declares no string sdk.version")
    return version


@contextmanager
def generated_version() -> Iterator[Path]:
    """Write ``mcuhome/model/VERSION`` for the duration of one build.

    A file that is already there is **not** adopted silently. It is
    generated, gitignored and removed again by every build that writes it,
    so the only ways one survives are a killed build and something else
    having put it there — and in both cases a number that disagrees with
    the definition file would answer for this working tree from then on:
    every wheel built from it, and every import of ``mcuhome.model``. That
    is exactly the drift the derivation exists to end, so it is a refusal
    that says how to clear it.

    A file that agrees is left as it is and removed by nobody: it is the
    right answer, and a build backend that deleted another party's file
    would be a surprise nobody could debug.
    """
    declared = declared_version()
    if VERSION_FILE.exists():
        found = VERSION_FILE.read_text(encoding="utf-8").strip()
        if found != declared:
            raise SystemExit(
                f"{VERSION_FILE} says {found!r} and {ENVIRONMENT_FILE} declares "
                f"{declared!r}.\nThat file is generated and never committed; a stale one "
                "answers for this\nworking tree until it is removed. Delete it and build "
                "again."
            )
        yield VERSION_FILE
        return
    VERSION_FILE.write_text(f"{declared}\n", encoding="utf-8")
    try:
        yield VERSION_FILE
    finally:
        VERSION_FILE.unlink(missing_ok=True)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):  # noqa: ANN001, ANN201 - the PEP 517 signature
    """setuptools' ``build_wheel``, with ``VERSION`` present while it runs."""
    with generated_version():
        return build_meta.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):  # noqa: ANN001, ANN201 - the PEP 517 signature
    """setuptools' ``build_sdist``, with ``VERSION`` present while it runs."""
    with generated_version():
        return build_meta.build_sdist(sdist_directory, config_settings)
