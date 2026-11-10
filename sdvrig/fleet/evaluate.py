"""Run fleet scenarios in closed loop and score each monitor against the truth.

For every scenario the world is run once per monitor, with the same seed: the
monitor sees each day's uploads and its halts stop the remaining rollout waves.
Only this module (and the world) read the truth; scoring happens after the run.
"""

from __future__ import annotations

import random
import statistics
import time

from sdvrig.fleet.monitor import MONITORS, NoMonitor
from sdvrig.fleet.world import FleetWorld, load_fleet_config

KINDS = ("cold_hw", "always", "healthy_cold_snap", "mixed")
DEFECT_TARGETS = {
    "ZCU_FRONT": {"dtc": "B1A23-16", "hw": ["HW03", "HW04"]},
    "BMS": {"dtc": "P1A40-00", "hw": ["HW07", "HW08"]},
    "THERMAL": {"dtc": "P1C02-00", "hw": [None]},
    "ZCU_REAR": {"dtc": "B1B10-11", "hw": [None]},
}
NEW_VERSION = {"ZCU_FRONT": "3.2.0", "ZCU_REAR": "3.2.0", "BMS": "1.9.0", "THERMAL": "2.1.0"}


def _defect(rng: random.Random, ecu: str, when: str) -> dict:
    t = DEFECT_TARGETS[ecu]
    if when == "cold":
        return {"when": "cold", "below_c": -10, "hw": rng.choice(t["hw"]), "dtc": t["dtc"],
                "rate": round(rng.uniform(0.02, 0.08), 3)}
    return {"when": "always", "hw": None, "dtc": t["dtc"], "rate": round(rng.uniform(0.004, 0.012), 4)}


def generate(seed: int, per_kind: int, prefix: str) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for kind in KINDS:
        for i in range(per_kind):
            ecus = rng.sample(sorted(DEFECT_TARGETS), 3)
            releases = [{"ecu": e, "version": NEW_VERSION[e], "start_day": rng.randint(8, 20), "defect": None}
                        for e in ecus]
            forced = []
            if kind == "cold_hw":
                cold_ecu = rng.choice([e for e in ecus if e in ("ZCU_FRONT", "BMS")] or ecus[:1])
                for r in releases:
                    if r["ecu"] == cold_ecu:
                        r["defect"] = _defect(rng, cold_ecu, "cold")
            elif kind == "always":
                releases[0]["defect"] = _defect(rng, releases[0]["ecu"], "always")
            elif kind == "healthy_cold_snap":
                for r in releases:
                    forced.append({"region": "nordic", "start": r["start_day"], "length": rng.randint(6, 10),
                                   "drop": rng.randint(10, 16)})
            elif kind == "mixed":
                releases[0]["defect"] = _defect(rng, releases[0]["ecu"], "always")
                releases[1]["defect"] = _defect(rng, releases[1]["ecu"], "cold")
            out.append({"id": f"{prefix}-{kind}-{i + 1:02d}", "kind": kind, "seed": rng.randrange(1 << 30),
                        "releases": releases, "forced_snaps": forced})
    return out


def run_fleet_scenario(scenario: dict, cfg: dict | None = None, vehicles: int | None = None,
                       methods=("none", "stratified", "pooled", "before_after")) -> dict:
    cfg = cfg or load_fleet_config()
    out = {"id": scenario["id"], "kind": scenario["kind"], "methods": {}}
    for method in methods:
        world = FleetWorld(scenario, cfg, vehicles)
        monitor = NoMonitor() if method == "none" else MONITORS[method](cfg)
        t0 = time.perf_counter()
        log = world.run(monitor)
        elapsed = time.perf_counter() - t0
        releases = []
        for rel in world.truth():
            key = (rel["ecu"], rel["version"])
            halt = next((h for h in monitor.halts if (h.ecu, h.version) == key), None)
            halt_d = dict(halt.__dict__) if halt else None
            if halt is not None:
                halt_d["triage_localization"] = monitor.triage(halt)
            releases.append({
                **rel,
                "defective": rel["defect"] is not None,
                "halt": halt_d,
                "share_installed": round(rel["installed"] / world.n, 4),
                "vehicle_days": log.exposure.get(key, 0),
            })
        out["methods"][method] = {"releases": releases, "seconds": round(elapsed, 2), "vehicles": world.n,
                                  "records": world.n * world.days}
    return out


def _localization_correct(rel: dict) -> bool | None:
    halt, defect = rel["halt"], rel["defect"]
    if not halt or not defect or not halt.get("triage_localization"):
        return None
    loc = halt["triage_localization"]
    want_hw = "all" if defect.get("hw") is None else [defect["hw"]]
    hw_ok = loc["hardware"] == want_hw
    if defect["when"] == "cold":
        t = loc["ambient_at_or_below_c"]
        temp_ok = t is not None and abs(t - defect["below_c"]) <= 5
    else:
        temp_ok = loc["ambient_at_or_below_c"] is None
    return hw_ok and temp_ok


def score(results: list[dict]) -> dict:
    summary = {}
    methods = list(results[0]["methods"]) if results else []
    for method in methods:
        bad, good = [], []
        for res in results:
            ref = {(r["ecu"], r["version"]): r for r in res["methods"]["none"]["releases"]} if "none" in res["methods"] else {}
            for rel in res["methods"][method]["releases"]:
                rel = dict(rel)
                base = ref.get((rel["ecu"], rel["version"]))
                rel["vehicle_days_without_monitor"] = base["vehicle_days"] if base else None
                (bad if rel["defective"] else good).append(rel)
        caught = [r for r in bad if r["halt"]]
        right_dtc = [r for r in caught if r["halt"]["dtc"] == r["defect"]["dtc"]]
        days = [r["halt"]["day"] - r["start_day"] for r in caught]
        shares = [r["share_installed"] for r in bad]
        exposure_cut = [1 - r["vehicle_days"] / r["vehicle_days_without_monitor"] for r in bad
                        if r["vehicle_days_without_monitor"]]
        loc = [_localization_correct(r) for r in caught]
        loc = [x for x in loc if x is not None]
        summary[method] = {
            "defective_releases": len(bad),
            "caught": len(caught),
            "caught_right_dtc": len(right_dtc),
            "healthy_releases": len(good),
            "false_halts": sum(1 for r in good if r["halt"]),
            "median_days_to_halt": statistics.median(days) if days else None,
            "median_fleet_share_on_bad_release": statistics.median(shares) if shares else None,
            "median_bad_exposure_avoided": round(statistics.median(exposure_cut), 3) if exposure_cut else None,
            "localization_correct": sum(loc) if loc else None,
            "localization_checked": len(loc) if loc else None,
        }
    return summary
