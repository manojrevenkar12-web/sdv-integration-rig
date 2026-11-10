import socket
import struct

import pytest

from sdvrig import doip, uds
from sdvrig.doip import DoIPError, DoIPMessage, decode, diag_message, routing_activation_req
from sdvrig.vehicle.tester import DoIPNack


class Peer:
    """A raw DoIP client on the in-process link, for checking the gateway's answers."""

    def __init__(self, vehicle):
        self.v = vehicle
        self.rx = b""
        vehicle.link.on_a_receive(self._rx)

    def _rx(self, data):
        self.rx += data

    def send(self, message: DoIPMessage | bytes):
        self.v.link.a_send(message if isinstance(message, bytes) else message.encode())
        self.v.settle(50)
        out = []
        while True:
            m, self.rx = decode(self.rx)
            if m is None:
                return out
            out.append(m)


def test_header_encode_decode():
    m = diag_message(0x0E80, 0x1A01, b"\x22\xF1\x90")
    raw = m.encode()
    assert raw[:8] == bytes([0x03, 0xFC, 0x80, 0x01, 0, 0, 0, 7])
    assert decode(raw + b"\x02") == (m, b"\x02")
    assert decode(raw[:5]) == (None, raw[:5])
    with pytest.raises(DoIPError):
        decode(b"\x03\x03" + raw[2:])


def test_vehicle_identification(vehicle):
    (resp,) = Peer(vehicle).send(doip.vehicle_id_request())
    assert resp.payload_type == doip.PT_VEHICLE_ID_RESP
    assert resp.payload[:17] == b"1RVSDV0TEST000001"
    assert struct.unpack(">H", resp.payload[17:19])[0] == 0x1010


def test_routing_activation_checks_tester_address(vehicle):
    peer = Peer(vehicle)
    (resp,) = peer.send(routing_activation_req(0x0001))
    assert resp.payload[4] == doip.RA_UNKNOWN_SA
    (resp,) = peer.send(routing_activation_req(0x0E80))
    assert resp.payload[4] == doip.RA_SUCCESS and resp.payload[2:4] == b"\x10\x10"


def test_diagnostic_message_before_activation_is_nacked(vehicle):
    (nack,) = Peer(vehicle).send(diag_message(0x0E80, 0x1A01, b"\x22\xF1\x90"))
    assert nack.payload_type == doip.PT_DIAG_NACK and nack.payload[4] == doip.DIAG_NACK_INVALID_SA


def test_unknown_target_is_nacked(vehicle):
    t = vehicle.tester
    with pytest.raises(DoIPNack) as exc:
        t.request(0x2222, b"\x22\xF1\x90")
    assert exc.value.code == doip.DIAG_NACK_UNKNOWN_TA


def test_ack_then_response(vehicle):
    peer = Peer(vehicle)
    peer.send(routing_activation_req(0x0E80))
    ack, resp = peer.send(diag_message(0x0E80, 0x1B01, b"\x22\xF1\x89"))
    assert ack.payload_type == doip.PT_DIAG_ACK and ack.payload[4] == 0x00
    assert resp.payload_type == doip.PT_DIAG_MESSAGE and resp.payload[4:] == b"\x62\xF1\x891.8.2"


def test_bad_pattern_and_unknown_payload_type(vehicle):
    peer = Peer(vehicle)
    (nack,) = peer.send(DoIPMessage(0x4242, b""))
    assert nack.payload_type == doip.PT_GENERIC_NACK and nack.payload == bytes([doip.NACK_UNKNOWN_PAYLOAD_TYPE])
    (nack,) = peer.send(b"\x03\x03\x00\x05\x00\x00\x00\x00")
    assert nack.payload == bytes([doip.NACK_INCORRECT_PATTERN])


def test_answers_in_the_testers_protocol_version(vehicle):
    peer = Peer(vehicle)
    req = routing_activation_req(0x0E80)
    (resp,) = peer.send(DoIPMessage(req.payload_type, req.payload, protocol_version=0x02))
    assert resp.protocol_version == 0x02


def test_response_pending_reaches_the_tester(vehicle):
    t = vehicle.tester
    t.change_session(0x1A01, uds.EXTENDED_SESSION)
    t.change_session(0x1A01, uds.PROGRAMMING_SESSION)
    t.unlock(0x1A01)
    t.routine_start(0x1A01, uds.RID_ERASE_MEMORY)   # 160 KiB: about 0.3 s, so 0x78 first
    pending = [p for _, k, a, p in t.log if k == "rsp" and p[:1] == b"\x7f" and p[2] == 0x78]
    assert pending


def test_real_socket_with_scapy_client(vehicle):
    scapy = pytest.importorskip("scapy.contrib.automotive.doip")
    from scapy.contrib.automotive.uds import UDS, UDS_RDBI
    from sdvrig.doip.server import DoIPServer

    with DoIPServer(vehicle, port=0) as server:
        for addr, version in ((0x1A01, b"3.1.0"), (0x1B02, b"2.0.4")):
            sock = scapy.UDS_DoIPSocket(ip="127.0.0.1", port=server.port, source_address=0x0E80,
                                        target_address=addr)
            try:
                resp = sock.sr1(UDS() / UDS_RDBI(identifiers=[0xF189]), timeout=2, verbose=False)
                assert bytes(resp) == b"\x62\xF1\x89" + version
                resp = sock.sr1(UDS() / UDS_RDBI(identifiers=[0x1234]), timeout=2, verbose=False)
                assert bytes(resp) == b"\x7F\x22\x31"
            finally:
                sock.close()
        # vehicle identification over UDP, decoded by scapy
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.settimeout(2)
        udp.sendto(doip.vehicle_id_request().encode(), ("127.0.0.1", server.port))
        data, _ = udp.recvfrom(1024)
        udp.close()
        pkt = scapy.DoIP(data)
        assert pkt.payload_type == 0x0004 and pkt.vin == b"1RVSDV0TEST000001"
