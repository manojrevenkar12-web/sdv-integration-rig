"""Check the rig's captures with decoders that share no code with it.

For one flash, every diagnostic message the tester sent or received must come out
of the captures byte for byte, in order:

  doip/scapy    the DoIP capture, TCP-reassembled and decoded by scapy's DoIP layer
  isotp/scapy   the CAN capture, frames decoded by scapy's CAN/CANFD layers and
                reassembled by scapy's ISOTPMessageBuilder
  isotp/rust    the CAN capture, reassembled by tools/isotp-trace (Rust, no deps)

scapy 2.6.1's ISOTPMessageBuilder does not implement the CAN FD single-frame
escape (ISO 15765-2:2016: SF_DL > 7 sits in byte 1). It returns those messages
empty. They are counted separately and checked at the frame level instead: the
CAN FD frame scapy decoded must start 00 <len> and carry the message.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from sdvrig.capture import write_captures
from sdvrig.vehicle import ROOT

RUST_TOOL = ROOT / "tools" / "isotp-trace" / "target" / "release" / "isotp-trace"


def expected_messages(vehicle, ecu: str) -> list[bytes]:
    """What the tester exchanged with this ECU, in the order it crossed the gateway."""
    addr = vehicle.addr(ecu)
    return [bytes(payload) for _, _, a, payload in vehicle.tester.log if a == addr]


def _compare(name: str, expected: list[bytes], got: list[bytes]) -> dict:
    equal = sum(1 for e, g in zip(expected, got) if e == g)
    first_diff = next((i for i, (e, g) in enumerate(zip(expected, got)) if e != g), None)
    return {"decoder": name, "expected": len(expected), "decoded": len(got), "equal": equal,
            "agrees": equal == len(expected) == len(got),
            "first_difference": first_diff}


def check_doip_scapy(pcap: Path, expected: list[bytes], ecu_addr: int) -> dict:
    from scapy.all import sniff
    from scapy.contrib.automotive.doip import DoIP
    from scapy.sessions import TCPSession

    got = []
    for pkt in sniff(offline=str(pcap), session=TCPSession):
        if not pkt.haslayer(DoIP):
            continue
        d = pkt[DoIP]
        if d.payload_type != 0x8001:
            continue
        if ecu_addr not in (d.source_address, d.target_address):
            continue
        got.append(bytes(d.payload))
    return _compare("doip/scapy", expected, got)


def check_isotp_scapy(pcap: Path, expected: list[bytes], ids: list[int]) -> dict:
    from scapy.all import rdpcap
    from scapy.contrib.isotp.isotp_utils import ISOTPMessageBuilder

    frames = [p for p in rdpcap(str(pcap)) if p.identifier in ids]
    builder = ISOTPMessageBuilder(use_ext_address=False, rx_id=ids)
    builder.feed(frames)
    got = []
    while builder.count:
        got.append(bytes(builder.pop().data))
    # Frame-level check for the FD single-frame escape scapy does not reassemble.
    escaped = [bytes(p.data) for p in frames if len(p.data) > 8 and p.data[0] == 0x00]
    escaped_payloads = [f[2: 2 + f[1]] for f in escaped]
    fixed, unexplained = [], 0
    pool = list(escaped_payloads)
    for e, g in zip(expected, got):
        if e == g:
            fixed.append(g)
        elif g == b"" and 7 < len(e) <= 62 and e in pool:
            pool.remove(e)
            fixed.append(e)
        else:
            fixed.append(g)
            unexplained += 1
    out = _compare("isotp/scapy", expected, fixed)
    out["fd_escape_frames_checked_at_frame_level"] = len(escaped_payloads) - len(pool)
    out["raw_equal_before_frame_check"] = sum(1 for e, g in zip(expected, got) if e == g)
    return out


def check_isotp_rust(pcap: Path, expected: list[bytes], ids: list[int]) -> dict:
    tool = RUST_TOOL if RUST_TOOL.exists() else shutil.which("isotp-trace")
    if not tool:
        return {"decoder": "isotp/rust", "skipped": "tools/isotp-trace is not built (cargo build --release)"}
    proc = subprocess.run([str(tool), str(pcap), *[f"{i:X}" for i in ids]], capture_output=True, text=True)
    got, errors = [], 0
    for line in proc.stdout.splitlines():
        rec = json.loads(line)
        if "error" in rec:
            errors += 1
        else:
            got.append(bytes.fromhex(rec["data"]))
    out = _compare("isotp/rust", expected, got)
    out["decoder_errors"] = errors
    return out


def crosscheck(vehicle, ecu: str, out_dir: Path, stem: str) -> dict:
    files = write_captures(vehicle, out_dir, stem)
    node = vehicle.node(ecu)
    ids = [node.request_id, node.response_id]
    expected = expected_messages(vehicle, ecu)
    return {
        "ecu": ecu,
        "bus": node.bus.name,
        "fd": node.bus.fd,
        "messages": len(expected),
        "can_frames": len(vehicle.buses[node.bus.name].trace),
        "files": {k: str(Path(v).relative_to(ROOT)) if Path(v).is_relative_to(ROOT) else v for k, v in files.items()},
        "checks": [
            check_doip_scapy(Path(files["doip"]), expected, vehicle.addr(ecu)),
            check_isotp_scapy(Path(files[node.bus.name]), expected, ids),
            check_isotp_rust(Path(files[node.bus.name]), expected, ids),
        ],
    }
