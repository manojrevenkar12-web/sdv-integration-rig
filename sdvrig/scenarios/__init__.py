"""Flashing scenarios: generate them, run them, and check the vehicle afterwards.

A scenario names a target ECU, an image, the faults to inject and the outcome a
correct flasher must report. The harness is the only code that knows the faults
and the expected outcome. It injects faults through the flasher's progress hook
(step name and block index) and then checks the vehicle's own state, not what
the flasher says about it:

  outcome          the flasher reported the expected outcome
  no_brick         every ECU runs a valid application and is on the bus
  truthful_report  "updated" means the target runs the new image, committed;
                   anything else means it runs the previous image, committed
  no_stray_dtcs    no ECU holds a fault code the scenario did not cause on purpose
  vehicle_restored every ECU is back in the default session with communication
                   and DTC setting on

Scenarios come from seeded generators in families (clean, interference, bus-off
bursts, power cuts at each stage, brown-outs, harness faults, bad builds), so the
dev and held-out sets are draws from the same families with different seeds.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from sdvrig import uds
from sdvrig.flash.campaign import FlashResult, flasher_for
from sdvrig.flash.image import build_image
from sdvrig.uds import dtc_from_text, dtc_to_text
from sdvrig.vehicle import ROOT, Vehicle, build_vehicle
from sdvrig.vehicle.faults import Disturbance, ErrorFrames, Partition, brownout, power_cut

SCENARIO_DIR = ROOT / "scenarios"
CHECKS = ("outcome", "no_brick", "truthful_report", "no_stray_dtcs", "vehicle_restored")
ECU_BUS = {"ZCU_FRONT": "zone-front", "ZCU_REAR": "zone-rear", "BMS": "powertrain", "THERMAL": "powertrain"}


# --------------------------------------------------------------------- running
@dataclass
class RunRecord:
    scenario: str
    family: str
    flasher: str
    ecu: str
    expect: str
    result: dict
    checks: dict[str, bool]
    details: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    def to_dict(self) -> dict:
        return {"scenario": self.scenario, "family": self.family, "flasher": self.flasher,
                "ecu": self.ecu, "expect": self.expect, "passed": self.passed,
                "checks": self.checks, "details": self.details, "result": self.result}


class _Injector:
    """Fires each fault once, the first time the flasher reaches its trigger point."""

    def __init__(self, vehicle: Vehicle, scenario: dict):
        self.v = vehicle
        self.s = scenario
        self.pending = [f for f in scenario["faults"] if "at" in f]
        self.rearm: list[dict] = []
        self.fired: list[str] = []
        self.quiet_after_us = 0   # when the last fault that has an end is over
        self.rng = random.Random(scenario["seed"])
        for fault in scenario["faults"]:
            if "at" not in fault:
                self._apply(fault)

    def progress(self, step: str, n: int) -> None:
        for fault in list(self.pending):
            at = fault["at"]
            if at["step"] == step and n >= at.get("block", 0):
                self.pending.remove(fault)
                self._apply(fault)
                if fault.get("repeat"):
                    self.rearm.append(fault)
        if step in ("programming-session", "identify"):
            self.pending += self.rearm   # repeating faults fire again in the next attempt
            self.rearm = []

    def _apply(self, fault: dict) -> None:
        v, kind = self.v, fault["kind"]
        self.fired.append(f"{kind}@{v.sim.now / 1000:.1f}ms")
        span_ms = fault.get("off_ms", fault.get("duration_ms", 0))
        self.quiet_after_us = max(self.quiet_after_us, v.sim.now + span_ms * 1000)
        ecu = self.s["ecu"]
        bus = v.buses[fault.get("bus", ECU_BUS[ecu])]
        if kind == "error_frames":
            bus.filters.append(ErrorFrames(self.rng, fault["probability"]))
        elif kind == "disturbance":
            bus.filters.append(Disturbance(v.sim.now, fault["duration_ms"] * 1000))
        elif kind == "power_cut":
            power_cut(v, ecu, fault["off_ms"])
        elif kind == "brownout":
            brownout(v, ecu, fault["mv"], fault.get("duration_ms"))
        elif kind == "partition":
            part = Partition(ecu)
            bus.filters.append(part)

            def heal(p=part) -> None:
                p.active = False
            v.sim.later(fault["duration_ms"] * 1000, heal)
        else:
            raise ValueError(f"unknown fault kind {kind}")


def run_scenario(scenario: dict, flasher: str) -> RunRecord:
    return run_scenario_with_vehicle(scenario, flasher)[0]


def run_scenario_with_vehicle(scenario: dict, flasher: str) -> tuple[RunRecord, Vehicle]:
    v = build_vehicle()
    ecu = scenario["ecu"]
    node = v.node(ecu)
    before = {name: set(n.ecu.dtcs) for name, n in v.nodes.items()}
    previous = node.ecu.running.version
    img = scenario["image"]
    image = build_image(ecu, img["version"], node.ecu.spec.image_bytes, healthy=img.get("healthy", True),
                        cold_defect=img.get("cold_defect"))
    injector = _Injector(v, scenario)
    if "tester_secret" in scenario:
        v.tester.secrets[v.addr(ecu)] = bytes.fromhex(scenario["tester_secret"])
    result: FlashResult = flasher_for(v, flasher, ecu, progress=injector.progress).flash(image)
    # Let every power restore, partition heal and voltage recovery happen, then settle.
    settle_us = 3_000_000
    v.sim.run_until(lambda: False, max(v.sim.now, injector.quiet_after_us) + settle_us)
    checks, details = check_vehicle(v, scenario, result, previous, before)
    details["faults_fired"] = ", ".join(injector.fired) or "none"
    return RunRecord(scenario["id"], scenario["family"], flasher, ecu, scenario["expect"],
                     result.to_dict(), checks, details), v


def check_vehicle(v: Vehicle, scenario: dict, result: FlashResult, previous: str,
                  dtcs_before: dict[str, set[int]]) -> tuple[dict[str, bool], dict[str, str]]:
    ecu_name = scenario["ecu"]
    target = v.node(ecu_name).ecu
    details: dict[str, str] = {}
    checks: dict[str, bool] = {}

    checks["outcome"] = result.outcome == scenario["expect"]
    if not checks["outcome"]:
        details["outcome"] = f"reported {result.outcome}, expected {scenario['expect']}: {result.reason}"

    bricked = [n for n, node in v.nodes.items()
               if not node.ecu.running.valid or not node.online or not node.ecu.app_running
               or node.ecu.boot_state == uds.BOOT_NO_APPLICATION]
    checks["no_brick"] = not bricked
    if bricked:
        details["no_brick"] = "not running: " + ", ".join(bricked)

    committed = target.trial_slot is None
    if result.ok:
        truthful = target.running.version == scenario["image"]["version"] and committed
    else:
        truthful = target.running.version == previous and committed
    checks["truthful_report"] = truthful
    if not truthful:
        details["truthful_report"] = (f"reported {result.outcome}; vehicle runs {target.running.version} "
                                      f"({'committed' if committed else 'uncommitted trial'}), "
                                      f"previous was {previous}")

    allowed: dict[str, set[int]] = {name: set() for name in v.nodes}
    for entry in scenario.get("allowed_dtcs", []):
        where, _, code = entry.rpartition(":")
        for name in (v.nodes if where == "*" else [where or ecu_name]):
            allowed[name].add(dtc_from_text(code))
    stray = []
    for name, node in v.nodes.items():
        new = set(node.ecu.dtcs) - dtcs_before[name] - allowed[name]
        stray += [f"{name}:{dtc_to_text(code)}" for code in sorted(new)]
    checks["no_stray_dtcs"] = not stray
    if stray:
        details["no_stray_dtcs"] = ", ".join(stray)

    left = [n for n, node in v.nodes.items()
            if node.ecu.session != uds.DEFAULT_SESSION or not node.ecu.normal_tx_enabled
            or not node.ecu.dtc_setting_on]
    checks["vehicle_restored"] = not left
    if left:
        details["vehicle_restored"] = "not restored: " + ", ".join(left)
    return checks, details


# ------------------------------------------------------------------ generation
FAMILIES = ("clean", "interference", "bus_off_burst", "power_cut_transfer", "power_cut_erase",
            "power_cut_exit", "power_cut_after_reset", "brownout_dip", "brownout_persistent",
            "harness_fault", "harness_fault_long", "bad_build", "combined", "flaky_supply", "wrong_key")


def _scenario(rng: random.Random, sid: str, family: str) -> dict:
    ecu = rng.choice(sorted(ECU_BUS))
    blocks = {"ZCU_FRONT": 41, "ZCU_REAR": 33, "BMS": 48, "THERMAL": 32}[ecu]
    version = f"{rng.randint(2, 9)}.{rng.randint(0, 9)}.{rng.randint(0, 20)}"
    s = {"id": sid, "family": family, "seed": rng.randrange(1 << 30), "ecu": ecu,
         "image": {"version": version, "healthy": True}, "faults": [], "expect": "updated"}
    block = rng.randint(1, blocks - 2)
    if family == "interference":
        s["faults"].append({"kind": "error_frames", "probability": round(rng.uniform(0.005, 0.08), 3)})
    elif family == "bus_off_burst":
        for _ in range(rng.randint(1, 3)):
            s["faults"].append({"kind": "disturbance", "duration_ms": rng.randint(5, 250),
                                "at": {"step": "transfer", "block": rng.randint(1, blocks - 2)}})
    elif family == "power_cut_transfer":
        s["faults"].append({"kind": "power_cut", "off_ms": rng.randint(200, 4000),
                            "at": {"step": "transfer", "block": block}})
    elif family == "power_cut_erase":
        s["faults"].append({"kind": "power_cut", "off_ms": rng.randint(200, 4000), "at": {"step": "erase"}})
    elif family == "power_cut_exit":
        s["faults"].append({"kind": "power_cut", "off_ms": rng.randint(200, 4000),
                            "at": {"step": rng.choice(["transfer-exit", "check-memory", "activate"])}})
    elif family == "power_cut_after_reset":
        s["faults"].append({"kind": "power_cut", "off_ms": rng.randint(200, 3000),
                            "at": {"step": rng.choice(["self-test", "commit"])}})
    elif family == "brownout_dip":
        step = rng.choice(["preconditions", "transfer"])
        at = {"step": step, "block": block} if step == "transfer" else {"step": step}
        s["faults"].append({"kind": "brownout", "mv": rng.randint(11000, 12400),
                            "duration_ms": rng.randint(300, 6000), "at": at})
    elif family == "brownout_persistent":
        s["faults"].append({"kind": "brownout", "mv": rng.randint(10500, 12400)})
        s["expect"] = "refused"
    elif family == "harness_fault":
        s["faults"].append({"kind": "partition", "duration_ms": rng.randint(100, 9000),
                            "at": {"step": "transfer", "block": block}})
    elif family == "harness_fault_long":
        s["faults"].append({"kind": "partition", "duration_ms": rng.randint(40000, 60000),
                            "at": {"step": "transfer", "block": block}})
        s["expect"] = "failed"
        s["allowed_dtcs"] = ["*:U0100-00"]   # a real, unrepaired disconnection is a real fault
    elif family == "bad_build":
        s["image"]["healthy"] = False
        s["expect"] = "rolled_back"
        s["allowed_dtcs"] = ["U3000-49"]
    elif family == "flaky_supply":
        # The supply drops out at the same point in every attempt: the flasher must give up safely.
        s["faults"].append({"kind": "power_cut", "off_ms": rng.randint(200, 2000), "repeat": True,
                            "at": {"step": "transfer", "block": block}})
        s["expect"] = "failed"
    elif family == "wrong_key":
        # The tester was given the wrong security secret for this ECU.
        s["tester_secret"] = "%032x" % rng.getrandbits(128)
        s["expect"] = "failed"
    elif family == "combined":
        s["faults"].append({"kind": "error_frames", "probability": round(rng.uniform(0.005, 0.04), 3)})
        s["faults"].append({"kind": "power_cut", "off_ms": rng.randint(200, 3000),
                            "at": {"step": "transfer", "block": block}})
        s["faults"].append({"kind": "disturbance", "duration_ms": rng.randint(5, 150),
                            "at": {"step": "transfer", "block": rng.randint(1, blocks - 2)}})
    return s


def generate(seed: int, per_family: int, prefix: str) -> list[dict]:
    rng = random.Random(seed)
    out = []
    for family in FAMILIES:
        for i in range(per_family):
            out.append(_scenario(rng, f"{prefix}-{family}-{i + 1:02d}", family))
    return out


def write_set(path: Path, scenarios: list[dict]) -> str:
    text = json.dumps(scenarios, indent=1) + "\n"
    path.write_text(text)
    return hashlib.sha256(text.encode()).hexdigest()


def load_set(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
