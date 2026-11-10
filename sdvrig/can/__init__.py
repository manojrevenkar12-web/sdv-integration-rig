"""Simulated CAN and CAN FD buses.

A bus serialises frames: one frame at a time, lowest identifier wins arbitration,
and a frame occupies the bus for as long as its bits take at the configured bit
rates. Faults are injected by filters that see every frame before delivery. A
filter can

  - corrupt the frame ("retry"): receivers signal an error frame and the sender's
    controller retransmits automatically, as CAN controllers do, so the frame
    arrives late but intact;
  - destroy it ("drop"): the sender is bus-off or the frame never reached the
    receivers, so it is lost. Lost frames are not written to the capture, because
    no receiver on a real bus would have accepted them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from sdvrig.sim import Sim

CLASSIC_MAX = 8
FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)


def fd_length_for(n: int) -> int:
    """Smallest CAN FD data length that holds `n` bytes."""
    for length in FD_LENGTHS:
        if length >= n:
            return length
    raise ValueError(f"CAN FD frames carry at most 64 bytes, not {n}")


@dataclass(frozen=True)
class CanFrame:
    arb_id: int
    data: bytes
    fd: bool = False
    brs: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.arb_id <= 0x7FF:
            raise ValueError(f"11-bit identifier expected, got {self.arb_id:#x}")
        limit = 64 if self.fd else CLASSIC_MAX
        if len(self.data) > limit:
            raise ValueError(f"{len(self.data)} bytes do not fit a {'FD' if self.fd else 'classic'} frame")
        if self.fd and len(self.data) not in FD_LENGTHS:
            raise ValueError(f"{len(self.data)} is not a valid CAN FD data length")


def frame_time_us(frame: CanFrame, nominal_bps: int, data_bps: int) -> int:
    """Approximate time on the wire, including worst-case-leaning bit stuffing.

    Classic base frame: 47 overhead bits + 8 per data byte, with stuff bits on the
    34 + 8n stuffable bits. CAN FD: arbitration and end-of-frame at the nominal
    rate, control/data/CRC at the data rate when bit-rate switching is on.
    """
    n = len(frame.data)
    if not frame.fd:
        bits = 47 + 8 * n + (34 + 8 * n) // 5
        return -(-bits * 1_000_000 // nominal_bps)
    crc_bits = 17 if n <= 16 else 21
    slow_bits = 30
    fast_bits = 8 + 8 * n + crc_bits + 4 + (8 * n) // 5
    rate = data_bps if frame.brs else nominal_bps
    return -(-slow_bits * 1_000_000 // nominal_bps) + -(-fast_bits * 1_000_000 // rate)


class Receiver(Protocol):
    def on_frame(self, frame: CanFrame) -> None: ...


# A filter sees (bus, frame, sender name) and returns None to deliver, "retry" to
# corrupt it (error frame, automatic retransmission), "drop" to lose it, or a
# replacement frame.
ERROR_FRAME_BITS = 20
FrameFilter = Callable[["CanBus", CanFrame, str], "None | str | CanFrame"]


@dataclass
class TraceRecord:
    time_us: int
    bus: str
    sender: str
    frame: CanFrame


@dataclass
class _Port:
    name: str
    receiver: Receiver
    queue: list[tuple[CanFrame, Callable[[bool], None] | None]] = field(default_factory=list)
    online: bool = True


class CanBus:
    def __init__(self, sim: Sim, name: str, kind: str, nominal_bps: int, data_bps: int | None = None):
        if kind not in ("classic", "fd"):
            raise ValueError(f"bus kind must be classic or fd, not {kind}")
        self.sim = sim
        self.name = name
        self.fd = kind == "fd"
        self.nominal_bps = nominal_bps
        self.data_bps = data_bps or nominal_bps
        self._ports: dict[str, _Port] = {}
        self._busy = False
        self.filters: list[FrameFilter] = []
        self.trace: list[TraceRecord] = []
        self.dropped: list[TraceRecord] = []
        self.busy_us = 0
        self.frames = 0
        self.error_frames = 0

    def attach(self, name: str, receiver: Receiver) -> "CanPort":
        if name in self._ports:
            raise ValueError(f"{name} is already on {self.name}")
        self._ports[name] = _Port(name, receiver)
        return CanPort(self, name)

    def set_online(self, name: str, online: bool) -> None:
        port = self._ports[name]
        port.online = online
        if not online:
            for _, done in port.queue:
                if done:
                    done(False)
            port.queue.clear()

    def is_online(self, name: str) -> bool:
        return self._ports[name].online

    def _enqueue(self, name: str, frame: CanFrame, done: Callable[[bool], None] | None) -> None:
        port = self._ports[name]
        if frame.fd and not self.fd:
            raise ValueError(f"{self.name} is a classic bus; it cannot carry FD frames")
        if not port.online:
            if done:
                self.sim.later(0, done, False)
            return
        port.queue.append((frame, done))
        if not self._busy:
            self._arbitrate()

    def _arbitrate(self) -> None:
        heads = [(p.queue[0][0].arb_id, p.name) for p in self._ports.values() if p.queue and p.online]
        if not heads:
            self._busy = False
            return
        _, winner = min(heads)
        frame, done = self._ports[winner].queue.pop(0)
        self._busy = True
        duration = frame_time_us(frame, self.nominal_bps, self.data_bps)
        self.busy_us += duration
        self.frames += 1
        self.sim.later(duration, self._complete, winner, frame, done)

    def _complete(self, sender: str, frame: CanFrame, done: Callable[[bool], None] | None) -> None:
        delivered: CanFrame | None = frame
        for flt in self.filters:
            verdict = flt(self, frame, sender)
            if verdict == "retry":
                self.error_frames += 1
                port = self._ports[sender]
                if port.online:
                    port.queue.insert(0, (frame, done))
                elif done:
                    done(False)
                error_us = -(-ERROR_FRAME_BITS * 1_000_000 // self.nominal_bps)
                self.busy_us += error_us
                self.sim.later(error_us, self._arbitrate)
                return
            if verdict == "drop":
                delivered = None
                break
            if isinstance(verdict, CanFrame):
                delivered = verdict
        if delivered is None:
            self.dropped.append(TraceRecord(self.sim.now, self.name, sender, frame))
        else:
            self.trace.append(TraceRecord(self.sim.now, self.name, sender, delivered))
            for port in list(self._ports.values()):
                if port.name != sender and port.online:
                    port.receiver.on_frame(delivered)
        if done:
            done(True)
        self._arbitrate()


class CanPort:
    """A node's connection to one bus."""

    def __init__(self, bus: CanBus, name: str):
        self.bus = bus
        self.name = name

    @property
    def fd(self) -> bool:
        return self.bus.fd

    def send(self, frame: CanFrame, done: Callable[[bool], None] | None = None) -> None:
        self.bus._enqueue(self.name, frame, done)
