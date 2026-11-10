"""Fault injection.

Bus faults are frame filters (see sdvrig.can); power faults act on a node. Together
they reproduce what a bring-up or flashing test has to survive:

  ErrorFrames   interference that corrupts frames; controllers retransmit, so
                traffic arrives late but intact
  Disturbance   a burst long enough to drive senders bus-off: every frame on the
                bus in the window is lost
  Partition     one node cut off the bus (a connector, a harness, a dead transceiver)
  power_cut     supply gone, volatile state lost, the node reboots when it returns
  brownout      supply below the programming threshold
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from sdvrig.can import CanBus, CanFrame


@dataclass
class ErrorFrames:
    rng: random.Random
    probability: float
    corrupted: int = 0

    def __call__(self, bus: CanBus, frame: CanFrame, sender: str):
        if self.rng.random() < self.probability:
            self.corrupted += 1
            return "retry"
        return None


@dataclass
class Disturbance:
    start_us: int
    duration_us: int
    lost: int = 0

    def __call__(self, bus: CanBus, frame: CanFrame, sender: str):
        if self.start_us <= bus.sim.now < self.start_us + self.duration_us:
            self.lost += 1
            return "drop"
        return None


@dataclass
class Partition:
    node_name: str
    active: bool = True
    lost: int = 0

    def __call__(self, bus: CanBus, frame: CanFrame, sender: str):
        if self.active and (sender == self.node_name or sender == f"CGW>{self.node_name}"):
            self.lost += 1
            return "drop"
        return None


def power_cut(vehicle, node_name: str, off_ms: int) -> None:
    node = vehicle.node(node_name)
    node.power_off()
    vehicle.sim.later(off_ms * 1000, node.power_on)


def brownout(vehicle, node_name: str, mv: int, duration_ms: int | None = None) -> None:
    ecu = vehicle.node(node_name).ecu
    normal = vehicle.rig["power"]["supply_mv"]
    ecu.set_supply(mv)
    if duration_ms is not None:
        vehicle.sim.later(duration_ms * 1000, ecu.set_supply, normal)
