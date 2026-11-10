"""Write what crossed the wires to pcap files Wireshark (and scapy) can open.

  CAN and CAN FD frames: one pcap per bus, LINKTYPE_CAN_SOCKETCAN (227). Each
  record is the SocketCAN frame with the identifier in network byte order, the
  data length, the FD flags (FDF and BRS for CAN FD) and the data padded to 8 or 64.

  DoIP: LINKTYPE_ETHERNET (1), with Ethernet/IPv4/TCP headers and checksums around
  each chunk the tester and the gateway exchanged, after a TCP handshake, on port
  13400. Wireshark's DoIP dissector, and scapy's, decode the payload.

Timestamps are simulation time from a fixed epoch, so a capture is byte-for-byte
reproducible for a given scenario.
"""

from __future__ import annotations

import struct
from pathlib import Path

from sdvrig.can import CanBus

LINKTYPE_ETHERNET = 1
LINKTYPE_CAN_SOCKETCAN = 227
CANFD_BRS = 0x01
CANFD_FDF = 0x04
EPOCH = 1_767_225_600  # 2026-01-01T00:00:00Z

TESTER = {"mac": bytes.fromhex("020000000e80"), "ip": bytes([192, 168, 10, 2]), "port": 50320}
GATEWAY = {"mac": bytes.fromhex("02005e001010"), "ip": bytes([192, 168, 10, 1]), "port": 13400}


def _global_header(linktype: int) -> bytes:
    return struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, linktype)


def _record(time_us: int, data: bytes) -> bytes:
    sec, usec = divmod(time_us, 1_000_000)
    return struct.pack("<IIII", EPOCH + sec, usec, len(data), len(data)) + data


def socketcan_frame(arb_id: int, data: bytes, fd: bool, brs: bool) -> bytes:
    flags = (CANFD_FDF | (CANFD_BRS if brs else 0)) if fd else 0
    body = data.ljust(64 if fd else 8, b"\x00")
    return struct.pack(">IBBH", arb_id, len(data), flags, 0) + body


def write_can_pcap(path: Path, bus: CanBus) -> int:
    with open(path, "wb") as fh:
        fh.write(_global_header(LINKTYPE_CAN_SOCKETCAN))
        for rec in bus.trace:
            f = rec.frame
            fh.write(_record(rec.time_us, socketcan_frame(f.arb_id, f.data, f.fd, f.brs)))
    return len(bus.trace)


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f">{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def _tcp_packet(src: dict, dst: dict, seq: int, ack: int, flags: int, payload: bytes, ip_id: int) -> bytes:
    tcp_len = 20 + len(payload)
    tcp = struct.pack(">HHIIBBHHH", src["port"], dst["port"], seq, ack, 5 << 4, flags, 64240, 0, 0)
    pseudo = src["ip"] + dst["ip"] + struct.pack(">BBH", 0, 6, tcp_len)
    csum = _checksum(pseudo + tcp + payload)
    tcp = tcp[:16] + struct.pack(">H", csum) + tcp[18:]
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + tcp_len, ip_id, 0x4000, 64, 6, 0, src["ip"], dst["ip"])
    ip = ip[:10] + struct.pack(">H", _checksum(ip)) + ip[12:]
    eth = dst["mac"] + src["mac"] + b"\x08\x00"
    return eth + ip + tcp + payload


SYN, ACK, PSH = 0x02, 0x10, 0x08
MSS = 1460


def write_doip_pcap(path: Path, chunks: list[tuple[int, str, bytes]]) -> int:
    """`chunks` are (time_us, "a>b" or "b>a", bytes) as the DoIP link carried them."""
    seq = {"a": 1000, "b": 5000}
    ip_id = 1
    records = []
    t0 = chunks[0][0] if chunks else 0
    hs = max(0, t0 - 300)
    records.append((hs, _tcp_packet(TESTER, GATEWAY, seq["a"] - 1, 0, SYN, b"", ip_id)))
    records.append((hs + 100, _tcp_packet(GATEWAY, TESTER, seq["b"] - 1, seq["a"], SYN | ACK, b"", ip_id + 1)))
    records.append((hs + 200, _tcp_packet(TESTER, GATEWAY, seq["a"], seq["b"], ACK, b"", ip_id + 2)))
    ip_id += 3
    for time_us, direction, data in chunks:
        sender = direction[0]
        receiver = "b" if sender == "a" else "a"
        src, dst = (TESTER, GATEWAY) if sender == "a" else (GATEWAY, TESTER)
        for i in range(0, len(data), MSS):
            piece = data[i: i + MSS]
            records.append((time_us, _tcp_packet(src, dst, seq[sender], seq[receiver], PSH | ACK, piece, ip_id)))
            seq[sender] = (seq[sender] + len(piece)) & 0xFFFFFFFF
            ip_id = (ip_id + 1) & 0xFFFF
    with open(path, "wb") as fh:
        fh.write(_global_header(LINKTYPE_ETHERNET))
        for time_us, packet in records:
            fh.write(_record(time_us, packet))
    return len(records)


def write_captures(vehicle, out_dir: Path, stem: str) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, bus in vehicle.buses.items():
        path = out_dir / f"{stem}.{name}.pcap"
        write_can_pcap(path, bus)
        written[name] = str(path)
    path = out_dir / f"{stem}.doip.pcap"
    write_doip_pcap(path, vehicle.link.capture)
    written["doip"] = str(path)
    return written
