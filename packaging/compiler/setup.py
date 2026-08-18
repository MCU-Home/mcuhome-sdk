# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""This distribution's dependencies, which cannot be written down.

One dynamic field, one reason: ADR 0020 decision 8 makes the pin on the
sibling distribution exact, and an exact pin spelled out here would be a
second place the release version lives.

``mcuhome-model`` and nothing above it (ADR 0024): the compiler ships
inside the SDK package and runs in the build container, where the
workbench neither exists nor belongs — its own context reading lives in
:mod:`mcuhome.compiler.contextread`.
"""

import sys
from pathlib import Path

from setuptools import setup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from mcuhome.model import __version__  # noqa: E402 - needs the path above

setup(
    install_requires=[
        # The model and nothing above it (ADR 0024): the compiler ships
        # inside the SDK package and runs in the build container, where
        # the workbench neither exists nor belongs. The workbench pulls
        # THIS package through its `local` extra, never the other way.
        f"mcuhome-model=={__version__}",
        # mcuhome.compiler.contextread parses manifest.yaml — the same
        # parser the workbench uses, declared here in its own right.
        "ruamel.yaml>=0.18",
        # PEP 440 version handling and a zstd binding were here for the
        # orchestrator, which is the workbench's now: what is left of this
        # package runs *inside* a build container, where the SDK package
        # is mounted rather than fetched and unpacked. scripts/
        # build_sdk_archive.py still needs zstandard and installs it in
        # its own right.
    ]
)
