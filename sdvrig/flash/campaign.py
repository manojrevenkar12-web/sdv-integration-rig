"""Flashing one ECU over DoIP, two ways.

`RobustFlasher` is what a production flashing tool has to do:

  1. read the running version and boot state; finish an interrupted trial boot first
  2. vehicle-wide, functionally addressed and silent: extended session, DTC setting
     off, normal communication off; a functional TesterPresent keeps every ECU in
     that session for the whole flash
  3. target: programming session, SecurityAccess, check programming preconditions
     (supply voltage), waiting for the voltage to recover before giving up
  4. erase the inactive slot, RequestDownload, TransferData with per-block retries
     (same block counter, so the ECU recognises a repeat), RequestTransferExit
  5. check-memory routine with the image's SHA-256
  6. activate the new slot, ECUReset, wait for the ECU to boot
  7. read the self-test result: commit if it passed, reset to roll back if not
  8. vehicle-wide default session, which restores DTC setting and communication

If the ECU disappears mid-programming (power cut, harness fault), it waits for the
ECU to come back and starts the programming again from step 3, up to a limit.

`BaselineFlasher` is the happy-path script that passes on a clean bench: the same
UDS sequence for the target alone, no vehicle-wide preparation, no retries, no
resume, no precondition check, and it commits without reading the self-test.

Neither flasher can touch an ECU except through the tester; the scenario harness,
not the flasher, is what knows the faults and checks the vehicle afterwards.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from sdvrig import uds
from sdvrig.flash.image import parse_header
from sdvrig.uds import NegativeResponse
from sdvrig.vehicle import CONFIG_DIR
from sdvrig.vehicle.tester import DoIPNack, Tester, TesterTimeout

Progress = Callable[[str, int], None]
LOST_STATE_NRCS = {uds.NRC_REQUEST_SEQUENCE_ERROR, uds.NRC_SERVICE_NOT_SUPPORTED_IN_SESSION,
                   uds.NRC_SECURITY_ACCESS_DENIED, uds.NRC_CONDITIONS_NOT_CORRECT,
                   uds.NRC_WRONG_BLOCK_SEQUENCE_COUNTER, uds.NRC_GENERAL_PROGRAMMING_FAILURE,
                   uds.NRC_TRANSFER_SUSPENDED}


def load_policy(config_dir: Path = CONFIG_DIR) -> dict:
    return json.loads((config_dir / "flash.json").read_text())


@dataclass
class FlashResult:
    ecu: str
    flasher: str
    target_version: str
    previous_version: str = ""
    outcome: str = "failed"          # updated | rolled_back | refused | failed
    failed_step: str | None = None
    reason: str | None = None
    running_version: str = ""
    boot_state: int | None = None
    attempts: int = 0
    blocks_sent: int = 0
    block_retries: int = 0
    duration_ms: float = 0.0
    steps: list[str] = field(default_factory=list)
    step_times_ms: list[float] = field(default_factory=list)   # when each step started
    events: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "updated"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


class Refused(Exception):
    """The flash must not start (or continue) under these conditions."""


class Interrupted(Exception):
    """The ECU lost its programming state; programming can be restarted."""


class Failed(Exception):
    pass


class _Flasher:
    name = "flasher"

    def __init__(self, tester: Tester, ecu_name: str, address: int, functional_address: int,
                 policy: dict | None = None, progress: Progress | None = None):
        self.t = tester
        self.ecu_name = ecu_name
        self.addr = address
        self.functional = functional_address
        self.p = policy or load_policy()
        self.progress = progress or (lambda step, n: None)
        self.result: FlashResult | None = None

    @property
    def now_ms(self) -> float:
        return self.t.sim.now / 1000

    def _step(self, step: str, n: int = 0) -> None:
        assert self.result is not None
        if not self.result.steps or self.result.steps[-1] != step:
            self.result.steps.append(step)
            self.result.step_times_ms.append(round(self.now_ms, 1))
        self.progress(step, n)

    def _event(self, text: str) -> None:
        assert self.result is not None
        self.result.events.append(f"{self.now_ms:.1f} ms  {text}")

    def _wait_ms(self, ms: int) -> None:
        self.t.sim.run_for(ms * 1000)

    def _wait_for_ecu(self, timeout_ms: int) -> bool:
        """Poll the software version until the ECU answers again."""
        deadline = self.t.sim.now + timeout_ms * 1000
        while self.t.sim.now < deadline:
            try:
                self.t.read_did(self.addr, uds.DID_SOFTWARE_VERSION)
                return True
            except (TesterTimeout, NegativeResponse, DoIPNack):
                self._wait_ms(self.p["boot_poll_ms"])
        return False

    def _finish_result(self, start_us: int) -> None:
        r = self.result
        assert r is not None
        r.duration_ms = round((self.t.sim.now - start_us) / 1000, 1)
        try:
            r.running_version = self.t.read_did(self.addr, uds.DID_SOFTWARE_VERSION).decode()
            r.boot_state = self.t.read_did(self.addr, uds.DID_BOOT_STATE)[0]
        except (TesterTimeout, NegativeResponse, DoIPNack):
            r.running_version = r.running_version or "unreachable"

    # The UDS reprogramming sequence for the target; both flashers share it.
    def _program(self, image: bytes, digest: bytes, robust: bool) -> None:
        r = self.result
        assert r is not None
        self._step("programming-session")
        self.t.change_session(self.addr, uds.EXTENDED_SESSION)
        self.t.change_session(self.addr, uds.PROGRAMMING_SESSION)
        self._step("security-access")
        self.t.unlock(self.addr)
        if robust:
            self._step("preconditions")
            self._check_preconditions()
        self._step("erase")
        self.t.routine_start(self.addr, uds.RID_ERASE_MEMORY)
        self._step("request-download")
        max_block = self.t.request_download(self.addr, len(image))
        chunk = max_block - 2
        self._step("transfer")
        bsc = 1
        for index, offset in enumerate(range(0, len(image), chunk)):
            self.progress("transfer", index)
            block = image[offset: offset + chunk]
            if robust:
                self._send_block(bsc, block)
            else:
                self.t.transfer_data(self.addr, bsc, block)
            r.blocks_sent += 1
            bsc = (bsc + 1) & 0xFF
        self._step("transfer-exit")
        self.t.transfer_exit(self.addr)
        self._step("check-memory")
        check = self.t.routine_start(self.addr, uds.RID_CHECK_MEMORY, digest)
        if check[-1] != 0x00:
            raise Interrupted("check-memory: the written image does not match its SHA-256")

    def _check_preconditions(self) -> None:
        deadline = self.t.sim.now + self.p["voltage_wait_ms"] * 1000
        while True:
            pre = self.t.routine_start(self.addr, uds.RID_CHECK_PRECONDITIONS)
            if pre[-1] == 0x00:
                return
            if self.t.sim.now >= deadline:
                raise Refused("supply voltage stayed below the programming threshold")
            self._event("supply voltage low; waiting before programming")
            self._wait_ms(self.p["voltage_poll_ms"])

    def _probe_session(self, within_ms: int) -> int | None:
        """Poll the active-session DID for up to `within_ms`; None if the ECU stays silent."""
        deadline = self.t.sim.now + within_ms * 1000
        while self.t.sim.now < deadline:
            try:
                return self.t.read_did(self.addr, uds.DID_ACTIVE_SESSION)[0]
            except (TesterTimeout, DoIPNack):
                self._wait_ms(self.p["boot_poll_ms"])
            except NegativeResponse:
                return None
        return None

    def _send_block(self, bsc: int, block: bytes) -> None:
        r = self.result
        assert r is not None
        tries = 0
        while True:
            try:
                self.t.transfer_data(self.addr, bsc, block)
                return
            except TesterTimeout:
                tries += 1
                r.block_retries += 1
                if tries > self.p["request_retries"]:
                    raise Interrupted(f"transfer: block {bsc} unanswered after {tries} tries")
                # A timeout is ambiguous: a lost frame or a lost ECU. Ask which session it is in.
                session = self._probe_session(self.p["retry_backoff_ms"])
                if session is not None and session != uds.PROGRAMMING_SESSION:
                    raise Interrupted(f"transfer: ECU restarted (now in session {session:#04x})")
                self._event(f"block {bsc}: no answer, repeating")
            except NegativeResponse as nrc:
                if nrc.nrc == uds.NRC_BUSY_REPEAT:
                    self._wait_ms(self.p["busy_retry_ms"])
                    continue
                if nrc.nrc == uds.NRC_VOLTAGE_TOO_LOW:
                    self._event(f"block {bsc}: voltage too low, waiting")
                    self._check_preconditions()
                    continue
                raise Interrupted(f"transfer: block {bsc} refused, {nrc}")


class RobustFlasher(_Flasher):
    name = "robust"

    def flash(self, image: bytes) -> FlashResult:
        info = parse_header(image)
        digest = hashlib.sha256(image).digest()
        self.result = r = FlashResult(self.ecu_name, self.name, info.version)
        start = self.t.sim.now
        prepared = False
        try:
            self._step("identify")
            if not self._wait_for_ecu(self.p["boot_timeout_ms"]):
                raise Refused("ECU does not answer")
            if self._boot_state() == uds.BOOT_TRIAL:
                self._event("found an uncommitted trial boot; resolving it first")
                self._resolve_trial(expected_version=None)
            r.previous_version = self.t.read_did(self.addr, uds.DID_SOFTWARE_VERSION).decode()
            self._step("prepare-vehicle")
            self._prepare_vehicle()
            prepared = True
            for attempt in range(1, self.p["max_attempts"] + 1):
                r.attempts = attempt
                try:
                    if attempt > 1 and self._already_done(info.version):
                        break
                    self._program(image, digest, robust=True)
                    self._activate_and_boot()
                    self._resolve_trial(expected_version=info.version)
                    break
                except (Interrupted, TesterTimeout, DoIPNack) as why:
                    self._recover(attempt, why)
                except NegativeResponse as nrc:
                    if nrc.nrc not in LOST_STATE_NRCS:
                        raise
                    self._recover(attempt, nrc)
        except Refused as why:
            r.outcome, r.reason = "refused", str(why)
            r.failed_step = r.steps[-1] if r.steps else None
        except (Failed, NegativeResponse, TesterTimeout, DoIPNack, Interrupted) as why:
            r.outcome, r.reason = "failed", str(why)
            r.failed_step = r.steps[-1] if r.steps else None
        finally:
            if prepared:
                self._restore_vehicle()
            self._finish_result(start)
        return r

    def _boot_state(self) -> int:
        return self.t.read_did(self.addr, uds.DID_BOOT_STATE)[0]

    def _recover(self, attempt: int, why: Exception) -> None:
        self._event(f"attempt {attempt} interrupted: {why}")
        if attempt == self.p["max_attempts"]:
            raise Failed(f"gave up after {attempt} attempts: {why}")
        self._step("reconnect")
        if not self._wait_for_ecu(self.p["reconnect_timeout_ms"]):
            raise Failed("ECU did not come back after the interruption")
        self._prepare_vehicle()

    def _already_done(self, version: str) -> bool:
        """After an interruption late in the sequence the new image may already be in place."""
        running = self.t.read_did(self.addr, uds.DID_SOFTWARE_VERSION).decode()
        state = self._boot_state()
        if running == version and state == uds.BOOT_TRIAL:
            self._resolve_trial(expected_version=version)
            return True
        if running == version and state == uds.BOOT_CONFIRMED:
            self.result.outcome = "updated"
            return True
        return False

    def _prepare_vehicle(self) -> None:
        f = self.functional
        self.t.functional(f, bytes([uds.DIAGNOSTIC_SESSION_CONTROL, 0x80 | uds.EXTENDED_SESSION]))
        self.t.functional(f, bytes([uds.CONTROL_DTC_SETTING, 0x82]))
        self.t.functional(f, bytes([uds.COMMUNICATION_CONTROL, 0x83, 0x01]))
        self._start_keepalive()

    def _start_keepalive(self) -> None:
        self._stop_keepalive()
        period = self.p["keepalive_ms"] * 1000
        sim = self.t.sim

        def beat() -> None:
            self.t.request(self.functional, bytes([uds.TESTER_PRESENT, 0x80]), expect=False)
            self._keepalive = sim.later(period, beat)

        self._keepalive = sim.later(period, beat)

    def _stop_keepalive(self) -> None:
        timer = getattr(self, "_keepalive", None)
        if timer:
            timer.cancel()
            self._keepalive = None

    def _restore_vehicle(self) -> None:
        self._step("restore-vehicle")
        self._stop_keepalive()
        self.t.functional(self.functional, bytes([uds.DIAGNOSTIC_SESSION_CONTROL, 0x80 | uds.DEFAULT_SESSION]))

    def _activate_and_boot(self) -> None:
        self._step("activate")
        self.t.routine_start(self.addr, uds.RID_ACTIVATE_INACTIVE_SLOT)
        self._step("reset")
        try:
            self.t.ecu_reset(self.addr)
        except TesterTimeout:
            self._event("no answer to ECUReset; the ECU may have reset already")
        self._wait_ms(self.p["boot_poll_ms"])
        if not self._wait_for_ecu(self.p["boot_timeout_ms"]):
            raise Interrupted("ECU did not come back after the reset")
        self._prepare_vehicle()   # the target rebooted into the default session

    def _resolve_trial(self, expected_version: str | None) -> None:
        """The new image runs on trial: commit it if its self-test passed, else roll back."""
        r = self.result
        assert r is not None
        self._step("self-test")
        running = self.t.read_did(self.addr, uds.DID_SOFTWARE_VERSION).decode()
        self.t.change_session(self.addr, uds.EXTENDED_SESSION)
        passed = self.t.routine_start(self.addr, uds.RID_SELF_TEST_RESULT)[-1] == 0x00
        right_version = expected_version is None or running == expected_version
        if passed and right_version:
            self._step("commit")
            self.t.routine_start(self.addr, uds.RID_COMMIT_SLOT)
            if expected_version is not None:
                r.outcome = "updated"
            return
        self._step("roll-back")
        self._event(f"self-test {'passed' if passed else 'failed'} on {running}; rolling back")
        self.t.ecu_reset(self.addr)
        self._wait_ms(self.p["boot_poll_ms"])
        self._wait_for_ecu(self.p["boot_timeout_ms"])
        self._prepare_vehicle()
        if expected_version is not None:
            r.outcome = "rolled_back"
            r.reason = "new image failed its self-test; previous image restored"


class BaselineFlasher(_Flasher):
    name = "baseline"

    def flash(self, image: bytes) -> FlashResult:
        info = parse_header(image)
        digest = hashlib.sha256(image).digest()
        self.result = r = FlashResult(self.ecu_name, self.name, info.version, attempts=1)
        start = self.t.sim.now
        try:
            self._step("identify")
            r.previous_version = self.t.read_did(self.addr, uds.DID_SOFTWARE_VERSION).decode()
            self._program(image, digest, robust=False)
            self._step("activate")
            self.t.routine_start(self.addr, uds.RID_ACTIVATE_INACTIVE_SLOT)
            self._step("reset")
            self.t.ecu_reset(self.addr)
            self._wait_ms(self.p["boot_poll_ms"])
            if not self._wait_for_ecu(self.p["boot_timeout_ms"]):
                raise Failed("ECU did not come back after the reset")
            self._step("commit")
            self.t.change_session(self.addr, uds.EXTENDED_SESSION)
            self.t.routine_start(self.addr, uds.RID_COMMIT_SLOT)
            self.t.change_session(self.addr, uds.DEFAULT_SESSION)
            r.outcome = "updated"
        except (Failed, NegativeResponse, TesterTimeout, DoIPNack, Interrupted) as why:
            r.outcome, r.reason = "failed", str(why)
            r.failed_step = r.steps[-1] if r.steps else None
        finally:
            self._finish_result(start)
        return r


FLASHERS = {"robust": RobustFlasher, "baseline": BaselineFlasher}


def flasher_for(vehicle, kind: str, ecu_name: str, progress: Progress | None = None,
                policy: dict | None = None) -> _Flasher:
    cls = FLASHERS[kind]
    return cls(vehicle.tester, ecu_name, vehicle.addr(ecu_name), vehicle.functional_address,
               policy=policy, progress=progress)
