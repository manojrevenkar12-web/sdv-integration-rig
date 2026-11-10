"""The central gateway: a DoIP edge node on one side, ISO-TP on the vehicle buses on
the other.

It answers vehicle identification, grants routing activation to a tester whose
source address is in the external-tester range, acknowledges every diagnostic
message it routes (or NACKs it with the reason), and relays requests and responses
between the tester and each ECU's ISO-TP connection. Requests to the functional
address go out once per bus on the functional CAN id, as single frames.
"""

from __future__ import annotations

import dataclasses
import struct
from dataclasses import dataclass, field

from sdvrig import doip
from sdvrig.can import CanBus, CanFrame
from sdvrig.doip import DoIPLink, DoIPMessage, decode
from sdvrig.isotp import IsoTpLink, IsoTpParams, pad
from sdvrig.sim import Sim
from sdvrig.vehicle.node import EcuNode

TESTER_ADDRESSES = range(0x0E00, 0x1000)  # ISO 13400-2 Table 39: external test equipment


@dataclass
class Route:
    node: EcuNode
    link: IsoTpLink
    queue: list[bytes] = field(default_factory=list)


@dataclass(frozen=True)
class GatewayIdentity:
    vin: str
    logical_address: int
    eid: bytes
    gid: bytes


class DoIPGateway:
    def __init__(self, sim: Sim, link: DoIPLink, identity: GatewayIdentity, functional_address: int,
                 functional_can_id: int, isotp_params_by_bus: dict[str, IsoTpParams]):
        self.sim = sim
        self.link = link
        self.identity = identity
        self.logical_address = identity.logical_address
        self.functional_address = functional_address
        self.functional_can_id = functional_can_id
        self.isotp_params_by_bus = isotp_params_by_bus
        self.routes: dict[int, Route] = {}
        self._functional_ports: dict[str, tuple[CanBus, object]] = {}
        self.activated_source: int | None = None
        self._rx = b""
        self.diag_messages = 0
        self.nacks = 0
        self.peer_version = doip.PROTOCOL_VERSION
        link.on_b_receive(self._on_bytes)

    def add_ecu(self, node: EcuNode, logical_address: int) -> None:
        params = self.isotp_params_by_bus[node.bus.name]
        endpoint = _Endpoint(node.response_id)
        port = node.bus.attach(f"CGW>{node.name}", endpoint)
        link = IsoTpLink(self.sim, port, node.request_id, node.response_id, params,
                         on_message=lambda payload, a=logical_address: self._on_ecu_response(a, payload),
                         name=f"CGW-{node.name}")
        endpoint.link = link
        self.routes[logical_address] = Route(node, link)
        if node.bus.name not in self._functional_ports:
            fport = node.bus.attach(f"CGW>functional:{node.bus.name}", _Endpoint(-1))
            self._functional_ports[node.bus.name] = (node.bus, fport)

    # ----------------------------------------------------------------- DoIP in
    def _on_bytes(self, data: bytes) -> None:
        self._rx += data
        while True:
            try:
                message, self._rx = decode(self._rx)
            except doip.DoIPError:
                self._send(DoIPMessage(doip.PT_GENERIC_NACK, bytes([doip.NACK_INCORRECT_PATTERN])))
                self._rx = b""
                return
            if message is None:
                return
            if message.protocol_version not in doip.SUPPORTED_VERSIONS:
                self._send(DoIPMessage(doip.PT_GENERIC_NACK, bytes([doip.NACK_INCORRECT_PATTERN])))
                continue
            # Answer in the protocol version the tester speaks (2012 or 2019 edition).
            self.peer_version = message.protocol_version
            self.handle(message)

    def handle(self, message: DoIPMessage) -> None:
        ptype = message.payload_type
        if ptype == doip.PT_ROUTING_ACTIVATION_REQ:
            self._routing_activation(message.payload)
        elif ptype == doip.PT_DIAG_MESSAGE:
            self._diag_message(message.payload)
        elif ptype == doip.PT_ALIVE_CHECK_REQ:
            self._send(DoIPMessage(doip.PT_ALIVE_CHECK_RESP, struct.pack(">H", self.logical_address)))
        elif ptype == doip.PT_VEHICLE_ID_REQ:
            self._send(self.vehicle_announcement())
        else:
            self._send(DoIPMessage(doip.PT_GENERIC_NACK, bytes([doip.NACK_UNKNOWN_PAYLOAD_TYPE])))

    def vehicle_announcement(self) -> DoIPMessage:
        ident = self.identity
        payload = (ident.vin.encode("ascii") + struct.pack(">H", ident.logical_address)
                   + ident.eid + ident.gid + bytes([0x00]))
        return DoIPMessage(doip.PT_VEHICLE_ID_RESP, payload)

    def _routing_activation(self, payload: bytes) -> None:
        if len(payload) not in (7, 11):
            self._send(DoIPMessage(doip.PT_GENERIC_NACK, bytes([doip.NACK_INVALID_LENGTH])))
            return
        source = struct.unpack(">H", payload[:2])[0]
        code = doip.RA_SUCCESS if source in TESTER_ADDRESSES else doip.RA_UNKNOWN_SA
        if code == doip.RA_SUCCESS:
            self.activated_source = source
        resp = struct.pack(">HHBI", source, self.logical_address, code, 0)
        self._send(DoIPMessage(doip.PT_ROUTING_ACTIVATION_RESP, resp))

    def _diag_message(self, payload: bytes) -> None:
        if len(payload) < 5:
            self._send(DoIPMessage(doip.PT_GENERIC_NACK, bytes([doip.NACK_INVALID_LENGTH])))
            return
        source, target = struct.unpack(">HH", payload[:4])
        user_data = payload[4:]
        if self.activated_source != source:
            self._send_diag_nack(source, target, doip.DIAG_NACK_INVALID_SA)
            return
        if target == self.functional_address:
            if len(user_data) > 7:
                self._send_diag_nack(source, target, doip.DIAG_NACK_OUT_OF_MEMORY)
                return
            self._send_diag_ack(source, target, user_data)
            for bus, port in self._functional_ports.values():
                params = self.isotp_params_by_bus[bus.name]
                frame = CanFrame(self.functional_can_id, pad(bytes([len(user_data)]) + user_data, params),
                                 fd=params.fd, brs=params.fd)
                port.send(frame)
            return
        route = self.routes.get(target)
        if route is None:
            self._send_diag_nack(source, target, doip.DIAG_NACK_UNKNOWN_TA)
            return
        self._send_diag_ack(source, target, user_data)
        self.diag_messages += 1
        if route.link.busy:
            route.queue.append(user_data)
        else:
            route.link.send(user_data, lambda ok, r=route: self._drain(r))

    def _drain(self, route: Route) -> None:
        if route.queue and not route.link.busy:
            route.link.send(route.queue.pop(0), lambda ok, r=route: self._drain(r))

    def _send_diag_ack(self, source: int, target: int, user_data: bytes) -> None:
        self._send(DoIPMessage(doip.PT_DIAG_ACK, struct.pack(">HHB", target, source, 0x00) + user_data[:1]))

    def _send_diag_nack(self, source: int, target: int, code: int) -> None:
        self.nacks += 1
        self._send(DoIPMessage(doip.PT_DIAG_NACK, struct.pack(">HHB", target, source, code)))

    def _on_ecu_response(self, ecu_address: int, payload: bytes) -> None:
        if self.activated_source is None:
            return
        self._send(doip.diag_message(ecu_address, self.activated_source, payload))

    def _send(self, message: DoIPMessage) -> None:
        self.link.b_send(dataclasses.replace(message, protocol_version=self.peer_version).encode())


class _Endpoint:
    """A bus receiver that passes only its own response id to its ISO-TP link."""

    def __init__(self, rx_id: int) -> None:
        self.rx_id = rx_id
        self.link: IsoTpLink | None = None

    def on_frame(self, frame) -> None:
        if self.link is not None and frame.arb_id == self.rx_id:
            self.link.on_frame(frame)
