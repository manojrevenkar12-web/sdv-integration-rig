"""Wiring that puts an ECU on a CAN bus.

An `EcuNode` owns the ECU's physical side: its ISO-TP link (requests in on the
request id, responses out on the response id), functionally addressed requests on
the shared functional id, a periodic status frame other nodes watch for liveness,
and power. A reset or a power cut takes the node off the bus for its boot time.

A node that watches a peer raises U0100 (lost communication) when the peer's status
frame stays away longer than the comm-loss timeout. That is how a flash on one ECU
leaves fault codes on its neighbours unless the tester first turns DTC setting off
on the whole vehicle.
"""

from __future__ import annotations

from sdvrig.can import CanBus, CanFrame
from sdvrig.isotp import IsoTpLink, IsoTpParams
from sdvrig.sim import Sim, Timer
from sdvrig.uds import PROGRAMMING_SESSION, dtc_from_text
from sdvrig.uds.server import UdsTimings
from sdvrig.vehicle.ecu import Ecu, EcuBehaviour, EcuSpec

COMM_LOSS_DTC = dtc_from_text("U0100-00")


class EcuNode:
    def __init__(
        self,
        sim: Sim,
        bus: CanBus,
        spec: EcuSpec,
        *,
        request_id: int,
        response_id: int,
        functional_id: int,
        status_id: int,
        timings: UdsTimings,
        behaviour: EcuBehaviour,
        isotp_params: IsoTpParams,
        status_period_us: int,
        comm_loss_us: int,
        monitor_start_us: int = 0,
    ):
        self.sim = sim
        self.bus = bus
        self.ecu = Ecu(sim, spec, timings, behaviour)
        self.name = spec.name
        self.request_id = request_id
        self.response_id = response_id
        self.functional_id = functional_id
        self.status_id = status_id
        self.status_period_us = status_period_us
        self.comm_loss_us = comm_loss_us
        self.monitor_start_us = monitor_start_us
        self.powered = True
        self.port = bus.attach(spec.name, self)
        self.link = IsoTpLink(sim, self.port, response_id, request_id, isotp_params,
                              on_message=self._on_request, name=f"{spec.name}-diag")
        self.ecu.send_response = self._send_response
        self.ecu.reboot_hook = self.reboot
        self.ecu.app_start()
        self.ecu.comm_restart_hook = self._restart_monitors
        self._outbox: list[bytes] = []
        self._watch_timers: dict[int, Timer] = {}
        self._boot_timer: Timer | None = None
        self.boots = 0
        self.sim.later(0, self._emit_status)

    @property
    def online(self) -> bool:
        return self.bus.is_online(self.name)

    # ------------------------------------------------------------------ status
    def _emit_status(self) -> None:
        # The application sends status frames; in the programming session the
        # bootloader runs instead and the bus goes quiet for this node.
        if (self.online and self.ecu.app_running and self.ecu.normal_tx_enabled
                and self.ecu.session != PROGRAMMING_SESSION):
            counter = (self.sim.now // self.status_period_us) & 0xFF
            payload = bytes([self.ecu.active_slot, self.ecu.boot_state, counter])
            data = payload.ljust(8, b"\x00")
            self.port.send(CanFrame(self.status_id, data, fd=self.bus.fd))
        self.sim.later(self.status_period_us, self._emit_status)

    def watch(self, peer_status_id: int) -> None:
        self._arm_watch(peer_status_id)

    def _arm_watch(self, peer_status_id: int, extra_us: int = 0) -> None:
        old = self._watch_timers.get(peer_status_id)
        if old:
            old.cancel()
        self._watch_timers[peer_status_id] = self.sim.later(self.comm_loss_us + extra_us,
                                                            self._peer_lost, peer_status_id)

    def _peer_lost(self, peer_status_id: int) -> None:
        if self.online and self.ecu.app_running:
            self.ecu.set_dtc(COMM_LOSS_DTC)
        self._arm_watch(peer_status_id)

    # ------------------------------------------------------------------- frames
    def on_frame(self, frame: CanFrame) -> None:
        if frame.arb_id in self._watch_timers:
            self._arm_watch(frame.arb_id)
        elif frame.arb_id == self.request_id:
            self.link.on_frame(frame)
        elif frame.arb_id == self.functional_id:
            payload = _single_frame_payload(frame.data)
            if payload:
                self.ecu.on_request(payload, functional=True)

    def _on_request(self, payload: bytes) -> None:
        self.ecu.on_request(payload, functional=False)

    def _send_response(self, payload: bytes) -> None:
        if not self.online or not payload:
            return
        if self.link.busy:
            self._outbox.append(payload)
            return
        self.link.send(payload, self._after_send)

    def _after_send(self, ok: bool) -> None:
        if self._outbox and self.online:
            self.link.send(self._outbox.pop(0), self._after_send)

    # ------------------------------------------------------------- power/reset
    def reboot(self) -> None:
        """A reset: off the bus, the bootloader decides what to run, back after boot time."""
        self.bus.set_online(self.name, False)
        self.link.reset()
        self._outbox.clear()
        self.ecu.power_cycle()
        self.boots += 1
        if self._boot_timer:
            self._boot_timer.cancel()
        self._boot_timer = self.sim.later(self.ecu.spec.boot_ms * 1000, self._boot_complete)

    def _boot_complete(self) -> None:
        self._boot_timer = None
        if not self.powered:
            return
        self.bus.set_online(self.name, True)
        self.ecu.app_start()
        self._restart_monitors()

    def _restart_monitors(self) -> None:
        """Monitors start with a first-timeout grace period, after boot and whenever
        normal communication is switched back on."""
        for peer in list(self._watch_timers):
            self._arm_watch(peer, self.monitor_start_us)

    def power_off(self) -> None:
        """A power cut. Volatile state is lost the moment the supply goes."""
        self.powered = False
        self.bus.set_online(self.name, False)
        self.link.reset()
        self._outbox.clear()
        self.ecu.reset_volatile()
        self.ecu.download = type(self.ecu.download)()
        self.ecu.app_running = False
        if self._boot_timer:
            self._boot_timer.cancel()
            self._boot_timer = None

    def power_on(self) -> None:
        self.powered = True
        self.reboot()


def _single_frame_payload(data: bytes) -> bytes:
    """Functional requests are single frames only (ISO 15765-2, 9.6.2)."""
    if not data or data[0] >> 4 != 0:
        return b""
    n = data[0] & 0x0F
    if n:
        return bytes(data[1: 1 + n])
    if len(data) > 8 and data[1]:
        return bytes(data[2: 2 + data[1]])
    return b""
