"""Build the simulated reference vehicle from config/vehicle.json and config/rig.json.

Topology: a tester on DoIP, a central gateway (CGW), two CAN FD zone buses with a
zonal controller each, and a classic CAN powertrain bus with a battery management
system and a thermal controller that watch each other's status frames.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sdvrig.can import CanBus
from sdvrig.doip import DoIPLink
from sdvrig.doip.gateway import DoIPGateway, GatewayIdentity
from sdvrig.isotp import IsoTpParams
from sdvrig.sim import Sim
from sdvrig.uds import dtc_from_text
from sdvrig.uds.server import UdsTimings
from sdvrig.vehicle.ecu import EcuBehaviour, EcuSpec
from sdvrig.vehicle.node import EcuNode
from sdvrig.vehicle.tester import Tester

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"


def hexint(value) -> int:
    return int(value, 16) if isinstance(value, str) else int(value)


@dataclass
class Vehicle:
    sim: Sim
    buses: dict[str, CanBus]
    gateway: DoIPGateway
    tester: Tester
    link: DoIPLink
    nodes: dict[str, EcuNode]
    address_of: dict[str, int]
    functional_address: int
    rig: dict
    vcfg: dict
    notes: list[str] = field(default_factory=list)

    def node(self, name: str) -> EcuNode:
        return self.nodes[name]

    def addr(self, name: str) -> int:
        return self.address_of[name]

    def settle(self, ms: int = 100) -> None:
        self.sim.run_for(ms * 1000)


def load_config(config_dir: Path = CONFIG_DIR) -> tuple[dict, dict]:
    vcfg = json.loads((config_dir / "vehicle.json").read_text())
    rig = json.loads((config_dir / "rig.json").read_text())
    return vcfg, rig


def _isotp(icfg: dict, fd: bool, block_size: int, st_min_us: int) -> IsoTpParams:
    return IsoTpParams(fd=fd, block_size=block_size, st_min_us=st_min_us,
                       n_bs_us=icfg["n_bs_ms"] * 1000, n_cr_us=icfg["n_cr_ms"] * 1000,
                       wft_max=icfg["wft_max"], padding=hexint(icfg["padding"]),
                       max_message_bytes=icfg["max_message_bytes"])


def build_vehicle(sim: Sim | None = None, config_dir: Path = CONFIG_DIR,
                  overrides: dict | None = None) -> Vehicle:
    """`overrides` replaces per-ECU settings by name, e.g. {"ZCU_FRONT": {"hardware": "SIM-ZCUF-HW04"}}."""
    sim = sim or Sim()
    vcfg, rig = load_config(config_dir)
    timings = UdsTimings.from_config(rig["uds"])
    icfg, bg, ecfg = rig["isotp"], rig["background"], rig["ecu"]
    behaviour = EcuBehaviour(
        min_programming_mv=rig["power"]["min_programming_mv"],
        security_attempts=ecfg["security_attempts"],
        security_delay_us=ecfg["security_delay_ms"] * 1000,
        cold_threshold_c=ecfg["cold_threshold_c"],
        self_test_dtc=dtc_from_text(ecfg["self_test_dtc"]),
    )
    buses = {name: CanBus(sim, name, b["kind"], b["nominal_bps"], b.get("data_bps"))
             for name, b in vcfg["buses"].items()}
    functional_can_id = hexint(vcfg["functional_can_id"])

    nodes: dict[str, EcuNode] = {}
    address_of: dict[str, int] = {}
    secrets: dict[int, bytes] = {}
    for raw in vcfg["ecus"]:
        e = {**raw, **(overrides or {}).get(raw["name"], {})}
        spec = EcuSpec(
            name=e["name"], image_kib=e["image_kib"], max_block_length=e["max_block_length"],
            erase_us_per_kib=e["erase_us_per_kib"], write_us_per_kib=e["write_us_per_kib"],
            verify_us_per_kib=e["verify_us_per_kib"], boot_ms=e["boot_ms"],
            part_number=e["part_number"], hardware=e["hardware"], serial=e["serial"],
            software=e["software"], security_secret=bytes.fromhex(e["security_secret"]),
        )
        bus = buses[e["bus"]]
        nodes[spec.name] = EcuNode(
            sim, bus, spec,
            request_id=hexint(e["request_id"]), response_id=hexint(e["response_id"]),
            functional_id=functional_can_id, status_id=hexint(e["status_id"]),
            timings=timings, behaviour=behaviour,
            isotp_params=_isotp(icfg, bus.fd, icfg["ecu_block_size"], icfg["ecu_st_min_us"]),
            status_period_us=bg["status_period_ms"] * 1000, comm_loss_us=bg["comm_loss_ms"] * 1000,
            monitor_start_us=bg["monitor_start_delay_ms"] * 1000,
        )
        nodes[spec.name].ecu.vin = vcfg["vin"]
        address_of[spec.name] = hexint(e["logical_address"])
        secrets[address_of[spec.name]] = spec.security_secret

    for e in vcfg["ecus"]:
        for peer in e.get("monitors", []):
            nodes[e["name"]].watch(nodes[peer].status_id)

    link = DoIPLink(sim, rig["doip"]["link_latency_us"])
    gw = vcfg["gateway"]
    identity = GatewayIdentity(vin=vcfg["vin"], logical_address=hexint(gw["logical_address"]),
                               eid=bytes.fromhex(gw["eid"].replace(":", "")),
                               gid=bytes.fromhex(gw["gid"].replace(":", "")))
    gateway = DoIPGateway(
        sim, link, identity,
        functional_address=hexint(vcfg["functional_address"]),
        functional_can_id=functional_can_id,
        isotp_params_by_bus={n: _isotp(icfg, b.fd, icfg["gateway_block_size"], icfg["gateway_st_min_us"])
                             for n, b in buses.items()},
    )
    for e in vcfg["ecus"]:
        gateway.add_ecu(nodes[e["name"]], hexint(e["logical_address"]))

    tcfg = rig["tester"]
    tester = Tester(sim, link, source_address=hexint(vcfg["tester"]["logical_address"]),
                    p2_client_us=tcfg["p2_client_ms"] * 1000,
                    p2_star_client_us=tcfg["p2_star_client_ms"] * 1000,
                    functional_gap_us=tcfg["functional_gap_ms"] * 1000, secrets=secrets)
    vehicle = Vehicle(sim, buses, gateway, tester, link, nodes, address_of,
                      hexint(vcfg["functional_address"]), rig, vcfg)
    vehicle.settle(50)
    return vehicle
