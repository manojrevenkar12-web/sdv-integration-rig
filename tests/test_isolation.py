"""The invariants that keep the numbers honest, checked on the source."""

import ast
from pathlib import Path

from sdvrig.vehicle import ROOT

PKG = ROOT / "sdvrig"


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_monitor_never_imports_the_world_or_the_evaluation():
    mods = imports(PKG / "fleet" / "monitor.py")
    assert not {m for m in mods if m.startswith(("sdvrig.fleet.world", "sdvrig.fleet.evaluate", "sdvrig.scenarios"))}


def test_flashers_never_import_the_scenario_harness_or_faults():
    mods = imports(PKG / "flash" / "campaign.py")
    assert not {m for m in mods if m.startswith(("sdvrig.scenarios", "sdvrig.vehicle.faults", "sdvrig.fleet"))}


def test_vehicle_day_carries_no_truth():
    from dataclasses import fields
    from sdvrig.fleet.world import VehicleDay
    assert {f.name for f in fields(VehicleDay)} == {"vehicle", "day", "region", "ambient_min_c", "hardware",
                                                    "software", "dtc_readouts"}


def test_settings_live_in_config():
    """Timing and threshold literals belong in config/*.json, not in the protocol code."""
    for rel in ("flash/campaign.py", "fleet/monitor.py"):
        tree = ast.parse((PKG / rel).read_text())
        numbers = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                   and isinstance(n.value, (int, float)) and not isinstance(n.value, bool)}
        assert numbers <= {0, 1, 2, 3, 4, 6, 8, 99, 999.0, 0.0, 1.0, 2.0, 1000, 1_000_000, 0xFF, 0x80, 0x82, 0x83, 0x40}, (rel, numbers)
