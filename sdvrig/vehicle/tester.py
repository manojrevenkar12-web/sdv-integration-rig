"""The external diagnostic tester, connected to the gateway over DoIP.

It drives the clock forward (`sim.run_until`) until the response it waits for
arrives or its P2client timer expires, so scenario code reads as plain calls:

    tester.change_session(ZCU_FRONT, PROGRAMMING_SESSION)
    tester.routine_start(ZCU_FRONT, RID_ERASE_MEMORY)

A response-pending NRC (0x78) extends the deadline to P2*client, as ISO 14229-2
requires. The tester only sees what crosses the DoIP link: it has no handle on any
ECU, which is what keeps the flashers honest.
"""

from __future__ import annotations

from dataclasses import dataclass

from sdvrig import doip, uds
from sdvrig.doip import DoIPLink, DoIPMessage, decode, routing_activation_req
from sdvrig.sim import Sim
from sdvrig.uds import NegativeResponse
from sdvrig.uds.security import key_for


class TesterTimeout(Exception):
    pass


class DoIPNack(Exception):
    def __init__(self, code: int):
        super().__init__(f"gateway refused the diagnostic message (NACK {code:#04x})")
        self.code = code


@dataclass
class _Wait:
    kind: str
    sid: int = 0
    source: int = 0
    prefix: bytes = b""
    done: bool = False
    response: bytes | None = None
    error: Exception | None = None


class Tester:
    def __init__(self, sim: Sim, link: DoIPLink, source_address: int, p2_client_us: int,
                 p2_star_client_us: int, functional_gap_us: int = 10_000,
                 secrets: dict[int, bytes] | None = None):
        self.sim = sim
        self.link = link
        self.source = source_address
        self.p2_client_us = p2_client_us
        self.p2_star_client_us = p2_star_client_us
        self.functional_gap_us = functional_gap_us
        self.secrets = secrets or {}
        self._rx = b""
        self._wait: _Wait | None = None
        self.activated = False
        self.requests_sent = 0
        self.pending_seen = 0
        self.timeouts = 0
        self.late_responses = 0
        self.log: list[tuple[int, str, int, bytes]] = []  # (time, "req"/"rsp", ecu, bytes)
        link.on_a_receive(self._on_bytes)

    # ------------------------------------------------------------------- DoIP
    def _on_bytes(self, data: bytes) -> None:
        self._rx += data
        while True:
            message, self._rx = decode(self._rx)
            if message is None:
                return
            self._handle(message)

    def _handle(self, message: DoIPMessage) -> None:
        wait = self._wait
        ptype = message.payload_type
        if ptype == doip.PT_ROUTING_ACTIVATION_RESP:
            self.activated = len(message.payload) >= 5 and message.payload[4] == doip.RA_SUCCESS
            if wait and wait.kind == "activation":
                wait.done = True
            return
        if ptype == doip.PT_DIAG_NACK:
            if wait and wait.kind == "uds" and not wait.done:
                wait.done = True
                wait.error = DoIPNack(message.payload[4] if len(message.payload) > 4 else 0)
            return
        if ptype != doip.PT_DIAG_MESSAGE or len(message.payload) < 5:
            return
        source = int.from_bytes(message.payload[:2], "big")
        user = message.payload[4:]
        self.log.append((self.sim.now, "rsp", source, user))
        if not wait or wait.kind != "uds" or wait.done or source != wait.source:
            self.late_responses += 1
            return
        is_for_us = user[0] == (wait.sid | 0x40) or (user[0] == uds.NEGATIVE_RESPONSE and user[1] == wait.sid)
        if not is_for_us:
            self.late_responses += 1
            return
        if user[0] != uds.NEGATIVE_RESPONSE and wait.prefix and not user.startswith(wait.prefix):
            self.late_responses += 1   # a positive answer to an earlier, repeated request
            return
        if user[0] == uds.NEGATIVE_RESPONSE and user[2] == uds.NRC_RESPONSE_PENDING:
            self.pending_seen += 1
            wait.response = None
            return
        wait.done = True
        wait.response = user

    def _pump(self, wait: _Wait, what: str) -> None:
        deadline = self.sim.now + self.p2_client_us
        seen = self.pending_seen
        while not wait.done:
            self.sim.run_until(lambda: wait.done or self.pending_seen > seen, deadline)
            if wait.done:
                return
            if self.pending_seen > seen:
                seen = self.pending_seen
                deadline = self.sim.now + self.p2_star_client_us
                continue
            self.timeouts += 1
            raise TesterTimeout(f"no response to {what} within P2client")

    # --------------------------------------------------------------- lifecycle
    def activate(self) -> None:
        wait = self._wait = _Wait("activation")
        self.link.a_send(routing_activation_req(self.source).encode())
        try:
            self._pump(wait, "routing activation")
        finally:
            self._wait = None
        if not self.activated:
            raise TesterTimeout("routing activation was not granted")

    # --------------------------------------------------------------- UDS calls
    def request(self, target: int, payload: bytes, expect: bool = True, prefix: bytes = b"") -> bytes:
        """Send a request to `target` and return the positive response bytes.

        `prefix`, when given, is the start a positive response must have (the echoed
        identifier or block counter); other positive answers are stale and ignored.
        """
        if not self.activated:
            self.activate()
        self.requests_sent += 1
        self.log.append((self.sim.now, "req", target, payload))
        wait = _Wait("uds", sid=payload[0], source=target, prefix=prefix)
        if expect:
            self._wait = wait   # a fire-and-forget request (keep-alive) leaves any wait alone
        self.link.a_send(doip.diag_message(self.source, target, payload).encode())
        if not expect:
            return b""
        try:
            name = uds.SERVICE_NAMES.get(payload[0], hex(payload[0]))
            self._pump(wait, f"{name} to {target:#06x}")
        finally:
            self._wait = None
        if wait.error:
            raise wait.error
        response = wait.response or b""
        if response[:1] == bytes([uds.NEGATIVE_RESPONSE]):
            raise NegativeResponse(response[1], response[2])
        return response

    def functional(self, functional_address: int, payload: bytes) -> None:
        """Broadcast a request with the suppress-positive-response bit set."""
        self.request(functional_address, payload, expect=False)
        self.sim.run_for(self.functional_gap_us)

    # Convenience wrappers --------------------------------------------------
    def change_session(self, target: int, session: int) -> bytes:
        return self.request(target, bytes([uds.DIAGNOSTIC_SESSION_CONTROL, session]))

    def tester_present(self, target: int) -> None:
        self.request(target, bytes([uds.TESTER_PRESENT, 0x80]), expect=False)

    def read_did(self, target: int, did: int) -> bytes:
        did_bytes = did.to_bytes(2, "big")
        resp = self.request(target, bytes([uds.READ_DATA_BY_IDENTIFIER]) + did_bytes, prefix=b"\x62" + did_bytes)
        return resp[3:]

    def write_did(self, target: int, did: int, value: bytes) -> bytes:
        return self.request(target, bytes([uds.WRITE_DATA_BY_IDENTIFIER]) + did.to_bytes(2, "big") + value)

    def unlock(self, target: int, secret: bytes | None = None) -> None:
        level = uds.SECURITY_LEVEL_PROGRAMMING
        seed = self.request(target, bytes([uds.SECURITY_ACCESS, level]))[2:]
        if not any(seed):
            return  # already unlocked
        key = key_for(secret if secret is not None else self.secrets[target], seed)
        self.request(target, bytes([uds.SECURITY_ACCESS, level + 1]) + key)

    def routine_start(self, target: int, rid: int, data: bytes = b"") -> bytes:
        rid_bytes = rid.to_bytes(2, "big")
        return self.request(target, bytes([uds.ROUTINE_CONTROL, 0x01]) + rid_bytes + data,
                            prefix=b"\x71\x01" + rid_bytes)

    def ecu_reset(self, target: int, sub: int = 0x01) -> bytes:
        return self.request(target, bytes([uds.ECU_RESET, sub]))

    def request_download(self, target: int, size: int, addr: int = 0x00000000) -> int:
        req = bytes([uds.REQUEST_DOWNLOAD, 0x00, 0x44]) + addr.to_bytes(4, "big") + size.to_bytes(4, "big")
        resp = self.request(target, req)
        length_bytes = (resp[1] >> 4) & 0x0F
        return int.from_bytes(resp[2: 2 + length_bytes], "big")

    def transfer_data(self, target: int, bsc: int, block: bytes) -> bytes:
        return self.request(target, bytes([uds.TRANSFER_DATA, bsc & 0xFF]) + block,
                            prefix=bytes([0x76, bsc & 0xFF]))

    def transfer_exit(self, target: int) -> bytes:
        return self.request(target, bytes([uds.REQUEST_TRANSFER_EXIT]))

    def read_dtcs(self, target: int, mask: int = uds.CONFIRMED_DTC) -> list[tuple[int, int]]:
        resp = self.request(target, bytes([uds.READ_DTC_INFORMATION, 0x02, mask]))
        return parse_dtc_records(resp)

    def clear_dtcs(self, target: int, group: int = 0xFFFFFF) -> bytes:
        return self.request(target, bytes([uds.CLEAR_DTC]) + group.to_bytes(3, "big"))


def parse_dtc_records(response: bytes) -> list[tuple[int, int]]:
    """Decode a 59 02 response into (dtc, status) pairs."""
    if response[:2] != b"\x59\x02":
        raise ValueError("not a reportDTCByStatusMask response")
    body = response[3:]
    if len(body) % 4:
        raise ValueError("DTC records are four bytes each")
    return [(int.from_bytes(body[i: i + 3], "big"), body[i + 3]) for i in range(0, len(body), 4)]
