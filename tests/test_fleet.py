import pytest

from sdvrig.fleet.evaluate import generate, run_fleet_scenario, score
from sdvrig.fleet.monitor import StratifiedMonitor, _Stats
from sdvrig.fleet.triage import triage
from sdvrig.fleet.world import FleetWorld, load_fleet_config

CFG = load_fleet_config()


def scenario(defect=None, ecu="ZCU_FRONT", forced=None, seed=3):
    return {"id": "t", "kind": "t", "seed": seed, "forced_snaps": forced or [],
            "releases": [{"ecu": ecu, "version": "3.2.0", "start_day": 10, "defect": defect}]}


def test_cmh_statistic_is_zero_without_a_difference():
    s = _Stats()
    for _ in range(10):
        s.add(5, 10, 100, 200)       # same rate on both sides
    assert abs(s.z) < 1e-9 and s.rate_ratio == pytest.approx(1.0)


def test_cmh_rate_ratio_recovers_a_known_effect():
    s = _Stats()
    s.add(30, 10, 100, 100)
    s.add(6, 20, 20, 200)
    assert s.rate_ratio == pytest.approx(3.0, rel=0.05) and s.z > 3.5


def test_early_waves_lean_to_the_nordic_region():
    w = FleetWorld(scenario(), CFG, vehicles=4000)
    first = w.rollout_order[: int(0.02 * w.n)]
    share = sum(w.vehicles[i].region == "nordic" for i in first) / len(first)
    assert share > 0.35          # against 10% of the fleet


def test_uploads_hold_raw_uds_readouts():
    class Grab:
        records = []
        halts = []

        def observe(self, batch):
            self.records += [r for r in batch if r.dtc_readouts]

        def decide(self, day):
            return []

    g = Grab()
    FleetWorld({**scenario(), "days": 3}, CFG, vehicles=500).run(g)
    ecu, raw = g.records[0].dtc_readouts[0]
    assert raw[:2] == b"\x59\x02" and len(raw) == 7


def test_stratified_halts_a_cold_hardware_defect_and_localises_it():
    defect = {"when": "cold", "below_c": -10, "hw": "HW03", "dtc": "B1A23-16", "rate": 0.06}
    res = run_fleet_scenario(scenario(defect), CFG, vehicles=4000, methods=("stratified",))
    rel = res["methods"]["stratified"]["releases"][0]
    assert rel["halt"] and rel["halt"]["dtc"] == "B1A23-16"
    assert rel["share_installed"] <= 0.5
    loc = rel["halt"]["triage_localization"]
    assert loc["hardware"] == ["HW03"]


def test_healthy_release_in_a_nordic_cold_snap_is_not_halted_by_the_stratified_monitor():
    forced = [{"region": "nordic", "start": 10, "length": 10, "drop": 16},
              {"region": "na_north", "start": 10, "length": 10, "drop": 12}]
    res = run_fleet_scenario(scenario(None, ecu="BMS", forced=forced, seed=8), CFG, vehicles=4000,
                             methods=("stratified", "pooled"))
    assert res["methods"]["stratified"]["releases"][0]["halt"] is None


def test_scoring_counts_catches_and_false_halts():
    res = run_fleet_scenario(scenario({"when": "always", "hw": None, "dtc": "B1A23-16", "rate": 0.02}),
                             CFG, vehicles=3000, methods=("none", "stratified"))
    s = score([res])
    assert s["none"]["caught"] == 0 and s["stratified"]["caught"] == 1
    assert s["stratified"]["median_bad_exposure_avoided"] > 0.5


def test_generation_is_deterministic():
    assert generate(4, 1, "x") == generate(4, 1, "x")


def test_triage_reproduces_a_cold_defect_on_the_bench():
    halt = {"ecu": "ZCU_FRONT", "version": "3.2.0", "dtc": "B1A23-16", "z": 5.0, "rate_ratio": 8.0,
            "exposed_events": 9, "expected_events": 1.0, "day": 12,
            "triage_localization": {"hardware": ["HW03"], "ambient_at_or_below_c": -10}}
    release = {"ecu": "ZCU_FRONT", "version": "3.2.0",
               "defect": {"when": "cold", "below_c": -10, "hw": "HW03", "dtc": "B1A23-16", "rate": 0.05}}
    ticket = triage(halt, release, CFG["owners"])
    assert ticket["reproduced"] is True
    roles = {r["role"]: r for r in ticket["runs"]}
    assert roles["room"]["dtcs"] == [] and "B1A23-16" in roles["field"]["dtcs"]
    assert roles["control-hw"]["dtcs"] == [] and roles["control-previous"]["version"] == "3.1.0"


def test_triage_asks_for_a_soak_test_when_nothing_localises():
    halt = {"ecu": "BMS", "version": "1.9.0", "dtc": "P1A40-00", "z": 5.0, "rate_ratio": 2.5,
            "exposed_events": 30, "expected_events": 12.0, "day": 15,
            "triage_localization": {"hardware": "all", "ambient_at_or_below_c": None}}
    ticket = triage(halt, {"ecu": "BMS", "version": "1.9.0", "defect": None}, CFG["owners"])
    assert ticket["reproduced"] is None and "soak" in ticket["next_step"]
