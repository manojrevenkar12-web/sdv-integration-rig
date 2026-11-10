import random

import pytest

from sdvrig.can import CanBus, CanFrame, fd_length_for, frame_time_us
from sdvrig.sim import Sim
from sdvrig.vehicle.faults import ErrorFrames


class Sink:
    def __init__(self):
        self.frames = []

    def on_frame(self, frame):
        self.frames.append(frame)


def test_fd_lengths_round_up_to_valid_dlc():
    assert [fd_length_for(n) for n in (0, 8, 9, 13, 33, 64)] == [0, 8, 12, 16, 48, 64]
    with pytest.raises(ValueError):
        fd_length_for(65)


def test_frame_validation():
    with pytest.raises(ValueError):
        CanFrame(0x800, b"")            # 11-bit identifiers only
    with pytest.raises(ValueError):
        CanFrame(0x100, bytes(9))       # classic frames carry 8 bytes
    with pytest.raises(ValueError):
        CanFrame(0x100, bytes(10), fd=True)   # 10 is not an FD length


def test_frame_time_classic_and_fd():
    # 8-byte classic frame at 500 kbit/s: 47 + 64 bits + stuffing, about 0.25 ms
    assert 220 <= frame_time_us(CanFrame(0x100, bytes(8)), 500_000, 500_000) <= 270
    fd = CanFrame(0x100, bytes(64), fd=True, brs=True)
    assert frame_time_us(fd, 500_000, 2_000_000) < frame_time_us(CanFrame(0x100, bytes(64), fd=True), 500_000, 500_000)


def test_lowest_identifier_wins_arbitration():
    sim = Sim()
    bus = CanBus(sim, "b", "classic", 500_000)
    a, b, sink = bus.attach("a", Sink()), bus.attach("b", Sink()), Sink()
    bus.attach("sink", sink)
    a.send(CanFrame(0x300, b"\x01"))      # takes the idle bus at once
    a.send(CanFrame(0x200, b"\x02"))
    b.send(CanFrame(0x100, b"\x03"))
    sim.run_for(10_000)
    assert [f.arb_id for f in sink.frames] == [0x300, 0x100, 0x200]


def test_error_frames_delay_but_deliver_intact():
    sim = Sim()
    bus = CanBus(sim, "b", "classic", 500_000)
    tx, sink = bus.attach("tx", Sink()), Sink()
    bus.attach("sink", sink)
    bus.filters.append(ErrorFrames(random.Random(1), 0.5))
    payloads = [bytes([i]) * 8 for i in range(50)]
    for p in payloads:
        tx.send(CanFrame(0x123, p))
    sim.run_for(1_000_000)
    assert [f.data for f in sink.frames] == payloads
    assert bus.error_frames > 10


def test_offline_node_neither_sends_nor_receives():
    sim = Sim()
    bus = CanBus(sim, "b", "classic", 500_000)
    tx, sink = bus.attach("tx", Sink()), Sink()
    bus.attach("sink", sink)
    results = []
    bus.set_online("tx", False)
    tx.send(CanFrame(0x1, b"x"), results.append)
    bus.set_online("tx", True)
    bus.set_online("sink", False)
    tx.send(CanFrame(0x2, b"y"))
    sim.run_for(10_000)
    assert results == [False] and sink.frames == []
