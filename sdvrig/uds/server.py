"""UDS server mechanics shared by every ECU in the rig: sessions, the S3 timer,
response-pending (0x78) for long operations, suppressed positive responses and the
rules for functionally addressed requests.

Subclasses register services with `self.service(sid, sessions, handler)`. A handler
returns the positive response bytes, raises `NegativeResponse`, or returns
`Pending(duration_us, finish)` when the work takes longer than P2server.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sdvrig import uds
from sdvrig.sim import Sim, Timer
from sdvrig.uds import NegativeResponse

ALL_SESSIONS = frozenset({uds.DEFAULT_SESSION, uds.PROGRAMMING_SESSION, uds.EXTENDED_SESSION})
NON_DEFAULT = frozenset({uds.PROGRAMMING_SESSION, uds.EXTENDED_SESSION})

# Services whose first parameter byte is a sub-function carrying the
# suppressPosRspMsgIndicationBit (0x80).
SUBFUNCTION_SERVICES = {uds.DIAGNOSTIC_SESSION_CONTROL, uds.ECU_RESET, uds.TESTER_PRESENT,
                        uds.CONTROL_DTC_SETTING, uds.COMMUNICATION_CONTROL, uds.ROUTINE_CONTROL,
                        uds.READ_DTC_INFORMATION, uds.SECURITY_ACCESS}


@dataclass
class Pending:
    duration_us: int
    finish: Callable[[], bytes]


@dataclass(frozen=True)
class UdsTimings:
    p2_server_us: int
    p2_star_server_us: int
    s3_server_us: int
    processing_us: int
    pending_interval_us: int
    reset_delay_us: int

    @classmethod
    def from_config(cls, cfg: dict) -> "UdsTimings":
        return cls(
            p2_server_us=cfg["p2_server_ms"] * 1000,
            p2_star_server_us=cfg["p2_star_server_ms"] * 1000,
            s3_server_us=cfg["s3_server_ms"] * 1000,
            processing_us=cfg["processing_us"],
            pending_interval_us=cfg["pending_interval_ms"] * 1000,
            reset_delay_us=cfg["reset_delay_ms"] * 1000,
        )


Handler = Callable[[bytes], "bytes | Pending | None"]


class UdsServer:
    def __init__(self, sim: Sim, name: str, timings: UdsTimings):
        self.sim = sim
        self.name = name
        self.t = timings
        self.send_response: Callable[[bytes], None] = lambda data: None
        self._services: dict[int, tuple[frozenset[int], Handler]] = {}
        self.session = uds.DEFAULT_SESSION
        self._s3: Timer | None = None
        self._busy = False
        self._work: list[Timer] = []
        self.requests_handled = 0
        self.pending_sent = 0
        self.service(uds.DIAGNOSTIC_SESSION_CONTROL, ALL_SESSIONS, self._session_control)
        self.service(uds.TESTER_PRESENT, ALL_SESSIONS, self._tester_present)

    # ---------------------------------------------------------------- framework
    def service(self, sid: int, sessions: frozenset[int], handler: Handler) -> None:
        self._services[sid] = (sessions, handler)

    def allowed_sessions(self, target: int) -> set[int]:
        """Sessions a tester may switch to from the current one."""
        if target == uds.PROGRAMMING_SESSION:
            return {uds.EXTENDED_SESSION, uds.PROGRAMMING_SESSION}
        return set(ALL_SESSIONS)

    def on_request(self, request: bytes, functional: bool = False) -> None:
        if not request:
            return
        if self._busy:
            if not functional:
                self._respond(bytes([uds.NEGATIVE_RESPONSE, request[0], uds.NRC_BUSY_REPEAT]), functional)
            return
        self._busy = True
        self._schedule(self.t.processing_us, self._dispatch, request, functional)

    def _schedule(self, delay: int, fn, *args) -> None:
        self._work.append(self.sim.later(delay, fn, *args))

    def _dispatch(self, request: bytes, functional: bool) -> None:
        self.requests_handled += 1
        self._touch_s3()
        sid = request[0]
        suppress = sid in SUBFUNCTION_SERVICES and len(request) > 1 and bool(request[1] & 0x80)
        try:
            entry = self._services.get(sid)
            if entry is None:
                raise NegativeResponse(sid, uds.NRC_SERVICE_NOT_SUPPORTED)
            sessions, handler = entry
            if self.session not in sessions:
                raise NegativeResponse(sid, uds.NRC_SERVICE_NOT_SUPPORTED_IN_SESSION)
            result = handler(request)
        except NegativeResponse as nrc:
            self._busy = False
            self._respond(bytes([uds.NEGATIVE_RESPONSE, sid, nrc.nrc]), functional)
            return
        if isinstance(result, Pending):
            self._run_pending(sid, result, functional, suppress)
            return
        self._busy = False
        if result is not None and not suppress:
            self._respond(result, functional)

    def _run_pending(self, sid: int, job: Pending, functional: bool, suppress: bool) -> None:
        if job.duration_us <= self.t.p2_server_us:
            self._schedule(job.duration_us, self._finish_pending, sid, job, functional, suppress)
            return
        self._send_pending(sid, functional)
        remaining = job.duration_us
        elapsed = 0
        while remaining - elapsed > self.t.pending_interval_us:
            elapsed += self.t.pending_interval_us
            self._schedule(elapsed, self._send_pending, sid, functional)
        self._schedule(job.duration_us, self._finish_pending, sid, job, functional, suppress)

    def _send_pending(self, sid: int, functional: bool) -> None:
        self.pending_sent += 1
        self._respond(bytes([uds.NEGATIVE_RESPONSE, sid, uds.NRC_RESPONSE_PENDING]), functional)

    def _finish_pending(self, sid: int, job: Pending, functional: bool, suppress: bool) -> None:
        self._busy = False
        try:
            response = job.finish()
        except NegativeResponse as nrc:
            self._respond(bytes([uds.NEGATIVE_RESPONSE, sid, nrc.nrc]), functional)
            return
        if response is not None and not suppress:
            self._respond(response, functional)

    def _respond(self, response: bytes, functional: bool) -> None:
        if functional and response[0] == uds.NEGATIVE_RESPONSE and response[2] in uds.FUNCTIONAL_SILENT_NRCS:
            return
        self.send_response(response)

    def _touch_s3(self) -> None:
        if self._s3:
            self._s3.cancel()
            self._s3 = None
        if self.session != uds.DEFAULT_SESSION:
            self._s3 = self.sim.later(self.t.s3_server_us, self._s3_expired)

    def _s3_expired(self) -> None:
        self._s3 = None
        self.enter_session(uds.DEFAULT_SESSION)

    def enter_session(self, session: int) -> None:
        self.session = session
        self.on_session_change(session)
        self._touch_s3()

    def on_session_change(self, session: int) -> None:
        """Hook for subclasses (e.g. re-enable DTC setting on return to default)."""

    def reset_volatile(self) -> None:
        """Forget everything a power cycle forgets."""
        for timer in self._work:
            timer.cancel()
        self._work.clear()
        if self._s3:
            self._s3.cancel()
            self._s3 = None
        self._busy = False
        self.session = uds.DEFAULT_SESSION

    # ----------------------------------------------------------------- services
    def _session_control(self, req: bytes) -> bytes:
        if len(req) != 2:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        target = req[1] & 0x7F
        if target not in ALL_SESSIONS:
            raise NegativeResponse(req[0], uds.NRC_SUBFUNCTION_NOT_SUPPORTED)
        if self.session not in self.allowed_sessions(target):
            raise NegativeResponse(req[0], uds.NRC_SUBFUNCTION_NOT_SUPPORTED_IN_SESSION)
        self.enter_session(target)
        p2 = self.t.p2_server_us // 1000
        p2_star = self.t.p2_star_server_us // 10_000
        return bytes([0x50, target]) + p2.to_bytes(2, "big") + p2_star.to_bytes(2, "big")

    def _tester_present(self, req: bytes) -> bytes:
        if len(req) != 2:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        if req[1] & 0x7F != 0x00:
            raise NegativeResponse(req[0], uds.NRC_SUBFUNCTION_NOT_SUPPORTED)
        return bytes([0x7E, 0x00])
