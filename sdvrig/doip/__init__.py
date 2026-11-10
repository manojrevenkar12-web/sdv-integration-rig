"""ISO 13400-2 (DoIP) message encoding and an in-process link.

Only the payload types the rig uses are implemented. The link stands in for a TCP
socket: bytes handed to `send` arrive at the peer after a fixed latency, in order.
Routing activation gates diagnostic messages, as a real DoIP edge node requires.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable

from sdvrig.sim import Sim

PROTOCOL_VERSION = 0x03  # ISO 13400-2:2019
SUPPORTED_VERSIONS = (0x02, 0x03)  # 2012 and 2019 editions

# Payload types
PT_GENERIC_NACK = 0x0000
PT_VEHICLE_ID_REQ = 0x0001
PT_VEHICLE_ID_RESP = 0x0004
PT_ROUTING_ACTIVATION_REQ = 0x0005
PT_ROUTING_ACTIVATION_RESP = 0x0006
PT_ALIVE_CHECK_REQ = 0x0007
PT_ALIVE_CHECK_RESP = 0x0008
PT_DIAG_MESSAGE = 0x8001
PT_DIAG_ACK = 0x8002
PT_DIAG_NACK = 0x8003

# Routing activation response codes
RA_SUCCESS = 0x10
RA_UNKNOWN_SA = 0x00
RA_NO_SOCKET = 0x01
RA_DENIED = 0x06

# Diagnostic NACK codes
DIAG_NACK_INVALID_SA = 0x02
DIAG_NACK_UNKNOWN_TA = 0x03
DIAG_NACK_OUT_OF_MEMORY = 0x05
DIAG_NACK_TARGET_UNREACHABLE = 0x06

# Generic NACK codes
NACK_INCORRECT_PATTERN = 0x00
NACK_UNKNOWN_PAYLOAD_TYPE = 0x01
NACK_MESSAGE_TOO_LARGE = 0x02
NACK_INVALID_LENGTH = 0x04


class DoIPError(Exception):
    pass


@dataclass(frozen=True)
class DoIPMessage:
    payload_type: int
    payload: bytes
    protocol_version: int = PROTOCOL_VERSION

    def encode(self) -> bytes:
        inverse = (~self.protocol_version) & 0xFF
        return struct.pack(">BBHI", self.protocol_version, inverse, self.payload_type,
                           len(self.payload)) + self.payload


def decode(buffer: bytes) -> tuple[DoIPMessage | None, bytes]:
    """Pull one message off the front of a byte stream. Returns (message, rest)."""
    if len(buffer) < 8:
        return None, buffer
    version, inverse, ptype, length = struct.unpack(">BBHI", buffer[:8])
    if (version ^ inverse) != 0xFF:
        raise DoIPError("protocol version and its inverse do not match")
    if len(buffer) < 8 + length:
        return None, buffer
    payload = buffer[8: 8 + length]
    return DoIPMessage(ptype, payload, version), buffer[8 + length:]


def diag_message(source: int, target: int, user_data: bytes) -> DoIPMessage:
    return DoIPMessage(PT_DIAG_MESSAGE, struct.pack(">HH", source, target) + user_data)


def vehicle_id_request() -> DoIPMessage:
    return DoIPMessage(PT_VEHICLE_ID_REQ, b"")


def routing_activation_req(source: int, activation_type: int = 0x00) -> DoIPMessage:
    return DoIPMessage(PT_ROUTING_ACTIVATION_REQ, struct.pack(">HBI", source, activation_type, 0))


class DoIPLink:
    """A bidirectional in-process stand-in for a TCP connection."""

    def __init__(self, sim: Sim, latency_us: int):
        self.sim = sim
        self.latency_us = latency_us
        self._a_rx: Callable[[bytes], None] = lambda b: None
        self._b_rx: Callable[[bytes], None] = lambda b: None
        self.open = True
        self.bytes_a_to_b = 0
        self.bytes_b_to_a = 0
        self.capture: list[tuple[int, str, bytes]] = []

    def on_a_receive(self, fn: Callable[[bytes], None]) -> None:
        self._a_rx = fn

    def on_b_receive(self, fn: Callable[[bytes], None]) -> None:
        self._b_rx = fn

    def a_send(self, data: bytes) -> None:
        if self.open:
            self.bytes_a_to_b += len(data)
            self.capture.append((self.sim.now, "a>b", data))
            self.sim.later(self.latency_us, self._b_rx, data)

    def b_send(self, data: bytes) -> None:
        if self.open:
            self.bytes_b_to_a += len(data)
            self.capture.append((self.sim.now, "b>a", data))
            self.sim.later(self.latency_us, self._a_rx, data)

    def close(self) -> None:
        self.open = False
