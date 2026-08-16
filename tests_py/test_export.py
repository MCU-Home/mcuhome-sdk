# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The registry, as exported data (:mod:`mcuhome.model.export`).

The model half of the subject. ``registry.json`` is a contract with a
consumer that is not in this repository (dashboard ADR 0011): a board
picker populates itself from it. It is therefore golden-tested byte for
byte — a change to it is a change a human approves, not one that
happens.

The other exported document, the ``main.yaml`` JSON Schema, is built by
:mod:`mcuhome.workbench.configschema` and tested in
``test_export_workbench.py``.
"""

from __future__ import annotations

from conftest import GOLDEN_DIR

from mcuhome.model import __version__, export, registry
from mcuhome.model.model import MODEL_VERSION

REGISTRY_GOLDEN = GOLDEN_DIR / "registry.json"

#: Placeholder the goldens carry where the builder's own version would
#: be. A golden that pinned the version would have to be regenerated on
#: every release, which would train a reviewer to regenerate goldens.
VERSION_PLACEHOLDER = "0.1.0.dev0"


def _stable(text: str) -> str:
    # Not only the quoted form: a version stamped *inside* a description
    # would slip past that and make a release turn a golden red for a
    # reason unrelated to its content — which is what happened to the
    # workbench's schema golden when 0.1.0.dev1 went out.
    return text.replace(__version__, VERSION_PLACEHOLDER)


# --------------------------------------------------------------------------
# Golden
# --------------------------------------------------------------------------


def test_the_registry_export_is_what_it_was() -> None:
    assert _stable(export.to_json(export.registry_data())) == REGISTRY_GOLDEN.read_text("utf-8")


# --------------------------------------------------------------------------
# The registry says what the registry knows
# --------------------------------------------------------------------------


def test_every_board_is_exported_with_its_update_scheme() -> None:
    data = export.registry_data()
    assert [board["name"] for board in data["boards"]] == list(registry.BOARDS)
    for board in data["boards"]:
        scheme = board["update_scheme"]
        assert scheme is not None, board["name"]
        assert scheme["signature_type"] == registry.SIGNATURE_TYPE
        labels = [entry["fixed_label"] for entry in scheme["partitions"]]
        assert "mcuboot" in labels and "storage" in labels


def test_a_board_says_which_buses_a_peripheral_can_sit_on() -> None:
    """The fact a picker needs, as data rather than as an error hint.

    ``hardware.buses.<id>.controller`` takes a devicetree node label, and
    a consumer offering that choice cannot invent the list. Every bus
    named here is one a supported driver could actually be attached to,
    so the kinds are a subset of the kinds the drivers speak.
    """
    kinds = {driver.bus for driver in registry.DRIVERS.values() if driver.bus is not None}
    for board in export.registry_data()["boards"]:
        assert board["buses"], board["name"]
        for bus in board["buses"]:
            assert bus["kind"] in kinds, (board["name"], bus)
            assert bus["controller"] and bus["description"]


def test_every_board_carries_its_bootstrap_instructions() -> None:
    """ADR 0016: the bootstrap path is registry data, instructions included."""
    for board in export.registry_data()["boards"]:
        bootstrap = board["bootstrap"]
        assert bootstrap is not None, board["name"]
        assert bootstrap["state"] in ("standard", "coexistence")
        assert bootstrap["steps"], board["name"]


def test_drivers_are_keyed_by_compatible() -> None:
    """One name for one thing: the YAML, the model and the export agree."""
    data = export.registry_data()
    assert [driver["compatible"] for driver in data["drivers"]] == list(registry.DRIVERS)
    assert all("driver" not in driver for driver in data["drivers"])


def test_clusters_carry_the_conversion_a_consumer_needs() -> None:
    cluster = next(
        entry
        for entry in export.registry_data()["clusters"]
        if entry["name"] == "temperature_measurement"
    )
    assert cluster["id"] == 0x0402
    assert cluster["unit"] == "°C"
    assert cluster["raw_per_unit"] == [100, 1]
    assert any(attr["role"] == "measured_value" for attr in cluster["attributes"])


def test_device_types_name_their_mandatory_clusters() -> None:
    data = export.registry_data()
    known = {cluster["name"] for cluster in data["clusters"]}
    for device_type in data["device_types"]:
        assert set(device_type["mandatory_clusters"]) <= known


def test_the_planned_tables_come_with_their_reasons() -> None:
    """ "Not supported yet" and why — the same message the validator gives."""
    planned = export.registry_data()["planned_boards"]
    assert {entry["name"] for entry in planned} == set(registry.PLANNED_BOARDS)
    assert all(entry["reason"] for entry in planned)


def test_the_export_states_the_model_version() -> None:
    assert export.registry_data()["model_version"] == MODEL_VERSION
