"""A synthetic fleet: vehicles, weather, staged over-the-air rollouts and fault codes.

This module is the truth. It knows which release carries which defect; nothing in
sdvrig.fleet.monitor may import it. What crosses to the monitor is only what a
vehicle would upload: one record per vehicle-day with its region, the lowest
ambient temperature it saw, the hardware and software of each ECU, and each ECU's
DTC readout as the raw `59 02` UDS response bytes.

Why the naive comparisons fail here, on purpose, because fleets are like this:
  - early rollout waves go to an employee/early-adopter pool that leans to the
    Nordic region, so exposed vehicles are colder than the rest of the fleet;
  - cold snaps hit whole regions for days, and several background DTCs (battery
    and thermal ones) genuinely rise in the cold whatever software is installed;
  - some defects only show on one hardware revision below a temperature.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from sdvrig.uds import CONFIRMED_DTC, TEST_FAILED, TEST_FAILED_THIS_CYCLE, dtc_from_text
from sdvrig.vehicle import CONFIG_DIR

ECUS = ("ZCU_FRONT", "ZCU_REAR", "BMS", "THERMAL")
DAILY_STATUS = TEST_FAILED | TEST_FAILED_THIS_CYCLE | CONFIRMED_DTC


def load_fleet_config(config_dir: Path = CONFIG_DIR) -> dict:
    return json.loads((config_dir / "fleet.json").read_text())


@dataclass(frozen=True)
class VehicleDay:
    """Exactly what one vehicle uploads for one day. No truth in here."""

    vehicle: int
    day: int
    region: str
    ambient_min_c: int
    hardware: tuple[str, ...]          # per ECU, in ECUS order
    software: tuple[str, ...]          # per ECU, in ECUS order
    dtc_readouts: tuple[tuple[str, bytes], ...]   # (ecu, raw 59 02 response) for ECUs with a failure today


@dataclass
class Release:
    ecu: str
    version: str
    start_day: int
    defect: dict | None = None
    halted_day: int | None = None
    waves_done: int = 0
    installed: int = 0


@dataclass
class Vehicle:
    vid: int
    region: str
    hardware: list[str]
    software: list[str]
    early: float          # position in the rollout order; lower installs earlier


@dataclass
class WorldLog:
    exposure: dict[tuple[str, str], int] = field(default_factory=dict)   # (ecu, version) -> vehicle-days
    installs: dict[tuple[str, str], int] = field(default_factory=dict)
    halts: list[dict] = field(default_factory=list)


class FleetWorld:
    def __init__(self, scenario: dict, cfg: dict | None = None, vehicles: int | None = None):
        self.cfg = cfg or load_fleet_config()
        self.s = scenario
        w = self.cfg["world"]
        self.days = scenario.get("days", w["days"])
        self.n = vehicles or scenario.get("vehicles", w["vehicles"])
        self.rng = random.Random(scenario["seed"])
        self.releases = [Release(r["ecu"], r["version"], r["start_day"], r.get("defect")) for r in scenario["releases"]]
        self._build_weather()
        self._build_vehicles()
        self._rates_cache: dict = {}

    # -------------------------------------------------------------- the world
    def _build_weather(self) -> None:
        w = self.cfg["world"]
        rng = self.rng
        self.temp: dict[str, list[float]] = {}
        self.snaps: dict[str, list[tuple[int, int, float]]] = {}
        for region, r in w["regions"].items():
            series = [r["mean_c"] + r["swing_c"] * rng.uniform(-1, 1) * 0.5 for _ in range(self.days)]
            for d in range(1, self.days):   # weather persists from day to day
                series[d] = 0.6 * series[d - 1] + 0.4 * series[d]
            snaps = []
            for _ in range(rng.randint(*w["cold_snaps_per_region"])):
                start = rng.randrange(self.days)
                length = rng.randint(*w["cold_snap_days"])
                drop = rng.uniform(*w["cold_snap_drop_c"])
                snaps.append((start, length, round(drop, 1)))
                for d in range(start, min(self.days, start + length)):
                    series[d] -= drop
            for forced in self.s.get("forced_snaps", []):
                if forced["region"] == region:
                    snaps.append((forced["start"], forced["length"], forced["drop"]))
                    for d in range(forced["start"], min(self.days, forced["start"] + forced["length"])):
                        series[d] -= forced["drop"]
            self.temp[region] = series
            self.snaps[region] = snaps

    def _build_vehicles(self) -> None:
        w, ro = self.cfg["world"], self.cfg["rollout"]
        rng = self.rng
        regions = list(w["regions"])
        shares = [w["regions"][r]["share"] for r in regions]
        early_w = ro["early_pool_region_weights"]
        self.vehicles: list[Vehicle] = []
        for vid in range(self.n):
            region = rng.choices(regions, shares)[0]
            hw = []
            for ecu in ECUS:
                revs = w["hardware"][ecu]
                hw.append(rng.choices(list(revs), list(revs.values()))[0])
            # Early-adopter bias: the rollout order leans to regions with a high pool weight.
            bias = early_w[region] / w["regions"][region]["share"]
            early = rng.random() ** bias
            self.vehicles.append(Vehicle(vid, region, hw, [w["software"][e] for e in ECUS], early))
        self.rollout_order = sorted(range(self.n), key=lambda i: self.vehicles[i].early)

    def _rates(self, ecu_i: int, version: str, hw: str, temp: int) -> tuple[float, list[tuple[float, int]]]:
        key = (ecu_i, version, hw, temp)
        hit = self._rates_cache.get(key)
        if hit:
            return hit
        ecu = ECUS[ecu_i]
        items = []
        for d in self.cfg["world"]["background_dtcs"][ecu]:
            rate = d["rate"] * (1 + d["cold_slope"] * max(0, -temp) / 10)
            items.append([rate, dtc_from_text(d["dtc"])])
        for rel in self.releases:
            if rel.ecu != ecu or rel.version != version or not rel.defect:
                continue
            df = rel.defect
            applies = df["when"] == "always" or (df["when"] == "cold" and temp <= df["below_c"])
            if applies and (df.get("hw") is None or hw == df["hw"]):
                code = dtc_from_text(df["dtc"])
                for it in items:
                    if it[1] == code:
                        it[0] += df["rate"]
                        break
                else:
                    items.append([df["rate"], code])
        total = sum(r for r, _ in items)
        cum, acc = [], 0.0
        for rate, code in items:
            acc += rate
            cum.append((acc, code))
        self._rates_cache[key] = (total, cum)
        return total, cum

    # -------------------------------------------------------------- rollout
    def _apply_waves(self, day: int, log: WorldLog) -> None:
        ro = self.cfg["rollout"]
        for rel in self.releases:
            if rel.halted_day is not None:
                continue
            while rel.waves_done < len(ro["wave_fractions"]) and day >= rel.start_day + ro["wave_offsets_days"][rel.waves_done]:
                target = int(round(ro["wave_fractions"][rel.waves_done] * self.n))
                ecu_i = ECUS.index(rel.ecu)
                for vid in self.rollout_order[rel.installed:target]:
                    self.vehicles[vid].software[ecu_i] = rel.version
                log.installs[(rel.ecu, rel.version)] = target
                rel.installed = max(rel.installed, target)
                rel.waves_done += 1

    # ----------------------------------------------------------------- run
    def run(self, monitor) -> WorldLog:
        """Run every day: install waves, upload telemetry, let the monitor decide halts."""
        log = WorldLog()
        noise = self.cfg["world"]["vehicle_noise_c"]
        rng = self.rng
        by_version = {(r.ecu, r.version): r for r in self.releases}
        for day in range(self.days):
            self._apply_waves(day, log)
            batch: list[VehicleDay] = []
            for v in self.vehicles:
                temp = int(round(self.temp[v.region][day] + rng.gauss(0, noise)))
                readouts = []
                for i in range(len(ECUS)):
                    u = rng.random()
                    total, cum = self._rates(i, v.software[i], v.hardware[i], temp)
                    if u < total:
                        code = next(c for edge, c in cum if u < edge)
                        raw = bytes([0x59, 0x02, 0x2F]) + code.to_bytes(3, "big") + bytes([DAILY_STATUS])
                        readouts.append((ECUS[i], raw))
                    key = (ECUS[i], v.software[i])
                    log.exposure[key] = log.exposure.get(key, 0) + 1
                batch.append(VehicleDay(v.vid, day, v.region, temp, tuple(v.hardware), tuple(v.software),
                                        tuple(readouts)))
            monitor.observe(batch)
            for halt in monitor.decide(day):
                rel = by_version.get((halt.ecu, halt.version))
                if rel and rel.halted_day is None:
                    rel.halted_day = day
                    log.halts.append({"ecu": halt.ecu, "version": halt.version, "day": day})
        return log

    def truth(self) -> list[dict]:
        return [{"ecu": r.ecu, "version": r.version, "start_day": r.start_day, "defect": r.defect,
                 "halted_day": r.halted_day, "installed": r.installed} for r in self.releases]
