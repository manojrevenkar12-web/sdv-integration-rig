import pytest

from sdvrig.can import CanBus
from sdvrig.isotp import IsoTpError, IsoTpLink, IsoTpParams, decode_st_min, encode_st_min, segment
from sdvrig.sim import Sim
from sdvrig.vehicle.faults import Disturbance


def pair(fd=False, block_size=0, st_min_us=0, max_bytes=65535):
    sim = Sim()
    bus = CanBus(sim, "b", "fd" if fd else "classic", 500_000, 2_000_000)
    got_a, got_b, err_a, err_b = [], [], [], []
    pa = IsoTpParams(fd=fd, block_size=block_size, st_min_us=st_min_us, max_message_bytes=max_bytes)
    ends = {}

    class End:
        def __init__(self, key):
            self.key = key

        def on_frame(self, frame):
            link = ends[self.key]
            if frame.arb_id == link.rx_id:
                link.on_frame(frame)

    port_a = bus.attach("a", End("a"))
    port_b = bus.attach("b", End("b"))
    ends["a"] = IsoTpLink(sim, port_a, 0x7E0, 0x7E8, pa, got_a.append, err_a.append)
    ends["b"] = IsoTpLink(sim, port_b, 0x7E8, 0x7E0, pa, got_b.append, err_b.append)
    return sim, bus, ends["a"], ends["b"], got_b, err_a, err_b


def test_single_frame_classic_and_fd_escape():
    p = IsoTpParams(fd=False)
    assert segment(b"\x22\xF1\x90", p) == (b"\x03\x22\xF1\x90", [])
    first, cfs = segment(bytes(20), IsoTpParams(fd=True))
    assert first[:2] == b"\x00\x14" and cfs == []          # SF_DL in byte 1 (ISO 15765-2:2016)


def test_first_frame_length_escape_above_4095():
    first, cfs = segment(bytes(5000), IsoTpParams(fd=True))
    assert first[:6] == b"\x10\x00" + (5000).to_bytes(4, "big")
    assert sum(len(c) - 1 for c in cfs) + len(first) - 6 == 5000


def test_sequence_numbers_wrap():
    _, cfs = segment(bytes(200), IsoTpParams(fd=False))
    sns = [c[0] & 0x0F for c in cfs]
    assert sns[:3] == [1, 2, 3] and sns[15] == 0 and sns[16] == 1


@pytest.mark.parametrize("raw,us", [(0x00, 0), (0x0A, 10_000), (0x7F, 127_000), (0xF1, 100), (0xF9, 900), (0x80, 127_000)])
def test_st_min_decode(raw, us):
    assert decode_st_min(raw) == us


def test_st_min_encode_round_trip():
    for us in (0, 100, 500, 1000, 25_000):
        assert decode_st_min(encode_st_min(us)) == us


@pytest.mark.parametrize("fd,size,bs", [(False, 1000, 8), (False, 4095, 0), (True, 4002, 16), (True, 9000, 4)])
def test_round_trip_with_flow_control(fd, size, bs):
    sim, bus, a, b, got_b, err_a, err_b = pair(fd=fd, block_size=bs)
    payload = bytes((i * 7) & 0xFF for i in range(size))
    done = []
    a.send(payload, done.append)
    sim.run_for(5_000_000)
    assert got_b == [payload] and done == [True] and not err_a and not err_b


def test_st_min_is_respected():
    sim, bus, a, b, got_b, *_ = pair(block_size=0, st_min_us=5000)
    a.send(bytes(70))      # FF + 9 CFs, 5 ms apart
    sim.run_for(1_000_000)
    times = [r.time_us for r in bus.trace if r.sender == "a"]
    gaps = [t2 - t1 for t1, t2 in zip(times[1:], times[2:])]
    assert got_b and min(gaps) >= 5000


def test_missing_flow_control_times_out_n_bs():
    sim, bus, a, b, got_b, err_a, err_b = pair()
    bus.set_online("b", False)
    done = []
    a.send(bytes(100), done.append)
    sim.run_for(2_000_000)
    assert done == [False] and err_a == [IsoTpError.TIMEOUT_BS]


def test_lost_consecutive_frame_aborts_reception():
    sim, bus, a, b, got_b, err_a, err_b = pair()
    bus.filters.append(Disturbance(1_000, 300))      # lose a frame or two mid-message
    a.send(bytes(300))
    sim.run_for(3_000_000)
    assert got_b == [] and err_b and err_b[0] in (IsoTpError.WRONG_SN, IsoTpError.TIMEOUT_CR)


def test_receiver_refuses_oversized_message():
    sim, bus, a, b, got_b, err_a, err_b = pair(max_bytes=500)
    a.send(bytes(800))
    sim.run_for(1_000_000)
    assert got_b == [] and err_a == [IsoTpError.OVERFLOW]


def test_one_message_at_a_time():
    sim, bus, a, *_ = pair()
    a.send(bytes(100))
    with pytest.raises(RuntimeError):
        a.send(b"\x01")
