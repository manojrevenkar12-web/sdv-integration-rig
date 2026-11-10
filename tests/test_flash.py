import ast
import json
from pathlib import Path

import pytest

from sdvrig import uds
from sdvrig.flash.campaign import flasher_for
from sdvrig.flash.image import build_image
from sdvrig.scenarios import CHECKS, FAMILIES, generate, run_scenario
from sdvrig.uds import dtc_to_text
from sdvrig.vehicle import ROOT, build_vehicle

ECUS = ("ZCU_FRONT", "ZCU_REAR", "BMS", "THERMAL")


def flash(kind, ecu, **image):
    v = build_vehicle()
    img = build_image(ecu, "9.0.0", v.node(ecu).ecu.spec.image_bytes, **image)
    return v, flasher_for(v, kind, ecu).flash(img)


@pytest.mark.parametrize("ecu", ECUS)
def test_robust_flash_updates_and_leaves_the_vehicle_clean(ecu):
    v, r = flash("robust", ecu)
    v.settle(2000)
    assert r.outcome == "updated" and r.running_version == "9.0.0" and r.boot_state == uds.BOOT_CONFIRMED
    assert all(not n.ecu.dtcs for n in v.nodes.values())
    assert {n.ecu.session for n in v.nodes.values()} == {uds.DEFAULT_SESSION}


def test_baseline_flash_leaves_a_comm_loss_dtc_on_the_neighbour():
    v, r = flash("baseline", "BMS")
    v.settle(2000)
    assert r.outcome == "updated"
    assert [dtc_to_text(c) for c in v.node("THERMAL").ecu.dtcs] == ["U0100-00"]


def test_robust_rolls_back_a_build_that_fails_its_self_test():
    v, r = flash("robust", "ZCU_REAR", healthy=False)
    ecu = v.node("ZCU_REAR").ecu
    assert r.outcome == "rolled_back" and ecu.running.version == "3.1.0" and ecu.trial_slot is None


def test_baseline_reports_failure_but_leaves_the_bad_build_running():
    v, r = flash("baseline", "ZCU_REAR", healthy=False)
    ecu = v.node("ZCU_REAR").ecu
    assert r.outcome == "failed" and ecu.running.version == "9.0.0" and ecu.trial_slot is not None


def test_scenario_generation_is_deterministic_and_covers_every_family():
    a, b = generate(5, 2, "x"), generate(5, 2, "x")
    assert a == b and {s["family"] for s in a} == set(FAMILIES)


@pytest.mark.parametrize("family", ["power_cut_transfer", "harness_fault", "brownout_dip", "power_cut_after_reset",
                                    "combined", "flaky_supply", "wrong_key", "brownout_persistent"])
def test_robust_passes_every_check_on_a_family_sample(family):
    s = next(s for s in generate(99, 1, "t") if s["family"] == family)
    rec = run_scenario(s, "robust")
    assert rec.passed, rec.details
    assert set(rec.checks) == set(CHECKS)


def test_power_cut_mid_transfer_restarts_programming():
    s = next(s for s in generate(7, 1, "t") if s["family"] == "power_cut_transfer")
    rec = run_scenario(s, "robust")
    assert rec.passed and rec.result["attempts"] == 2


def test_flashers_reach_the_vehicle_only_through_the_tester():
    """The flasher may not touch nodes, ECUs or buses: only the tester and addresses."""
    tree = ast.parse((ROOT / "sdvrig" / "flash" / "campaign.py").read_text())
    forbidden = {"nodes", "node", "ecu", "buses", "slots", "dtcs", "power_off", "power_on", "set_supply"}
    used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not used & forbidden, used & forbidden


def test_held_out_scenarios_match_their_lock():
    import hashlib
    lock = json.loads((ROOT / "config" / "heldout.lock").read_text())
    for name, digest in lock.items():
        assert hashlib.sha256((ROOT / "scenarios" / name).read_bytes()).hexdigest() == digest


@pytest.mark.slow
def test_full_dev_set_robust():
    scen = json.loads((ROOT / "scenarios" / "flash_dev.json").read_text())
    failed = [s["id"] for s in scen if not run_scenario(s, "robust").passed]
    assert failed == []
