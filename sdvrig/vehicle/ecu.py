"""A simulated ECU: a UDS server with a bootloader, A/B application slots,
SecurityAccess, CommunicationControl, DTC storage and routines.

Reprogramming follows the usual UDS sequence:
  extended session -> DTC setting off + communication off (functional, whole vehicle)
  -> programming session -> SecurityAccess -> erase routine -> RequestDownload
  -> TransferData* -> RequestTransferExit -> check-memory routine
  -> activate-inactive-slot routine -> ECUReset -> trial boot -> self-test -> commit.

The bootloader keeps two application slots. A new image is written to the inactive
slot; activation asks the bootloader to boot it on trial at the next reset. If the
trial is not committed before the following reset, the bootloader boots the
previous slot again. The running slot is never erased or written, so whatever
happens to the power or the bus during a flash, the ECU comes back up on a valid
image.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Callable

from sdvrig import uds
from sdvrig.flash.image import build_image, parse_header
from sdvrig.sim import Sim, Timer
from sdvrig.uds import NegativeResponse, dtc_from_text
from sdvrig.uds.security import SEED_BYTES, keys_match
from sdvrig.uds.server import ALL_SESSIONS, NON_DEFAULT, Pending, UdsServer, UdsTimings

PROGRAMMING_ONLY = frozenset({uds.PROGRAMMING_SESSION})


@dataclass
class Slot:
    image: bytes = b""
    version: str = ""
    valid: bool = False

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.image).hexdigest()


@dataclass
class EcuSpec:
    name: str
    image_kib: int
    max_block_length: int
    erase_us_per_kib: int
    write_us_per_kib: int
    verify_us_per_kib: int
    boot_ms: int
    part_number: str
    hardware: str
    serial: str
    software: str
    security_secret: bytes

    @property
    def image_bytes(self) -> int:
        return self.image_kib * 1024


@dataclass(frozen=True)
class EcuBehaviour:
    """Rig-wide settings for the simulated application (config/rig.json, "ecu")."""

    min_programming_mv: int
    security_attempts: int
    security_delay_us: int
    cold_threshold_c: int
    self_test_dtc: int


@dataclass
class DownloadState:
    expected: int = 0
    received: int = 0
    block_index: int = 0
    buffer: bytearray = field(default_factory=bytearray)
    active: bool = False


class Ecu(UdsServer):
    def __init__(self, sim: Sim, spec: EcuSpec, timings: UdsTimings, behaviour: EcuBehaviour):
        super().__init__(sim, spec.name, timings)
        self.spec = spec
        self.b = behaviour
        self.rng = random.Random(f"seed:{spec.name}")
        # Non-volatile state: survives resets and power loss.
        factory = build_image(spec.name, spec.software, spec.image_bytes)
        self.slots = [Slot(factory, spec.software, True), Slot()]
        self.active_slot = 0
        self.previous_slot = 0
        self.boot_request: int | None = None
        self.trial_slot: int | None = None
        self.boot_state = uds.BOOT_CONFIRMED
        self.dtcs: dict[int, int] = {}
        self.fingerprint = b""
        self.vin = ""
        self.flash_count = 0
        self.reverts = 0
        # Environment, set by the scenario.
        self.supply_mv = 13800
        self.ambient_c = 20
        # Volatile state.
        self.download = DownloadState()
        self.erased_slot: int | None = None
        self.dtc_setting_on = True
        self.normal_tx_enabled = True
        self.security_unlocked = False
        self._seed: bytes | None = None
        self._failed_keys = 0
        self._lockout_until = 0
        self.self_test_passed: bool | None = None
        self.app_running = True
        # Set by the node: performs the physical reset after the positive response.
        self.reboot_hook: Callable[[], None] = lambda: None
        # Set by the node: restarts receive-deadline monitoring when normal
        # communication starts again (as AUTOSAR restarts it on I-PDU group start).
        self.comm_restart_hook: Callable[[], None] = lambda: None
        self._register()

    # ------------------------------------------------------------- registration
    def _register(self) -> None:
        self.service(uds.ECU_RESET, ALL_SESSIONS, self._ecu_reset)
        self.service(uds.READ_DATA_BY_IDENTIFIER, ALL_SESSIONS, self._read_did)
        self.service(uds.WRITE_DATA_BY_IDENTIFIER, NON_DEFAULT, self._write_did)
        self.service(uds.SECURITY_ACCESS, NON_DEFAULT, self._security_access)
        self.service(uds.READ_DTC_INFORMATION, ALL_SESSIONS, self._read_dtc)
        self.service(uds.CLEAR_DTC, ALL_SESSIONS, self._clear_dtc)
        self.service(uds.CONTROL_DTC_SETTING, NON_DEFAULT, self._control_dtc_setting)
        self.service(uds.COMMUNICATION_CONTROL, NON_DEFAULT, self._communication_control)
        self.service(uds.ROUTINE_CONTROL, NON_DEFAULT, self._routine_control)
        self.service(uds.REQUEST_DOWNLOAD, PROGRAMMING_ONLY, self._request_download)
        self.service(uds.TRANSFER_DATA, PROGRAMMING_ONLY, self._transfer_data)
        self.service(uds.REQUEST_TRANSFER_EXIT, PROGRAMMING_ONLY, self._transfer_exit)

    @property
    def running(self) -> Slot:
        return self.slots[self.active_slot]

    @property
    def inactive_slot(self) -> int:
        return 1 - self.active_slot

    def on_session_change(self, session: int) -> None:
        # Any session change relocks security (ISO 14229-1, 9.4).
        self.security_unlocked = False
        self._seed = None
        if session == uds.DEFAULT_SESSION:
            self.dtc_setting_on = True
            self.normal_tx_enabled = True
            self.download = DownloadState()
            self.comm_restart_hook()

    # ------------------------------------------------------------- boot / reset
    def _ecu_reset(self, req: bytes) -> bytes:
        if len(req) != 2:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        sub = req[1] & 0x7F
        if sub not in (0x01, 0x03):
            raise NegativeResponse(req[0], uds.NRC_SUBFUNCTION_NOT_SUPPORTED)
        # Answer first, then reset (ISO 14229-1, 10.3): the node performs the reset.
        self._schedule(self.t.reset_delay_us, self.reboot_hook)
        return bytes([0x51, sub])

    def power_cycle(self) -> None:
        """What the bootloader does at power-up. Only non-volatile state survives."""
        self.reset_volatile()
        self.download = DownloadState()
        self.erased_slot = None
        self.dtc_setting_on = True
        self.normal_tx_enabled = True
        self.security_unlocked = False
        self._seed = None
        self.self_test_passed = None
        self.app_running = False
        if self.boot_request is not None:
            slot = self.boot_request
            self.boot_request = None
            if self.slots[slot].valid:
                self.previous_slot = self.active_slot
                self.active_slot = slot
                self.trial_slot = slot
                self.boot_state = uds.BOOT_TRIAL
                return
        if self.trial_slot is not None:
            # The trial was never committed: boot the previous image again.
            self.active_slot = self.previous_slot
            self.trial_slot = None
            self.boot_state = uds.BOOT_REVERTED
            self.reverts += 1
            return
        if not self.running.valid:
            self.boot_state = uds.BOOT_NO_APPLICATION
        elif self.boot_state != uds.BOOT_REVERTED:
            self.boot_state = uds.BOOT_CONFIRMED

    def app_start(self) -> None:
        """The application is up: run its self-test and its environment checks."""
        if not self.running.valid:
            return
        self.app_running = True
        info = parse_header(self.running.image)
        self.self_test_passed = info.healthy
        if not info.healthy:
            self.set_dtc(self.b.self_test_dtc)
        self.check_environment()

    def check_environment(self) -> None:
        if not self.app_running:
            return
        info = parse_header(self.running.image)
        if info.cold_dtc and self.ambient_c <= self.b.cold_threshold_c:
            if info.cold_hardware is None or self.spec.hardware.endswith(info.cold_hardware):
                self.set_dtc(dtc_from_text(info.cold_dtc))

    def commit(self) -> None:
        if self.trial_slot is not None:
            self.trial_slot = None
            self.boot_state = uds.BOOT_CONFIRMED

    # ---------------------------------------------------------------- data by id
    def _read_did(self, req: bytes) -> bytes:
        if len(req) != 3:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        did = int.from_bytes(req[1:3], "big")
        value = self._did_value(did)
        if value is None:
            raise NegativeResponse(req[0], uds.NRC_REQUEST_OUT_OF_RANGE)
        return bytes([0x62]) + req[1:3] + value

    def _did_value(self, did: int) -> bytes | None:
        if did == uds.DID_ACTIVE_SESSION:
            return bytes([self.session])
        if did == uds.DID_SOFTWARE_VERSION:
            return self.running.version.encode() if self.running.valid else b"none"
        if did == uds.DID_VIN:
            return self.vin.encode() if self.vin else None
        if did == uds.DID_SPARE_PART_NUMBER:
            return self.spec.part_number.encode()
        if did == uds.DID_HARDWARE_NUMBER:
            return self.spec.hardware.encode()
        if did == uds.DID_SERIAL_NUMBER:
            return self.spec.serial.encode()
        if did == uds.DID_BOOT_STATE:
            return bytes([self.boot_state, self.active_slot])
        if did == uds.DID_SUPPLY_VOLTAGE:
            return self.supply_mv.to_bytes(2, "big")
        if did == uds.DID_AMBIENT_TEMPERATURE:
            return self.ambient_c.to_bytes(1, "big", signed=True)
        if did == uds.DID_FINGERPRINT:
            return self.fingerprint or b"\x00"
        return None

    def _write_did(self, req: bytes) -> bytes:
        if len(req) < 4:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        did = int.from_bytes(req[1:3], "big")
        if did != uds.DID_FINGERPRINT:
            raise NegativeResponse(req[0], uds.NRC_REQUEST_OUT_OF_RANGE)
        self.fingerprint = req[3:]
        return bytes([0x6E]) + req[1:3]

    # ---------------------------------------------------------- security access
    def _security_access(self, req: bytes) -> bytes:
        if len(req) < 2:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        sub = req[1] & 0x7F
        if sub == uds.SECURITY_LEVEL_PROGRAMMING:
            if len(req) != 2:
                raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
            if self.sim.now < self._lockout_until:
                raise NegativeResponse(req[0], uds.NRC_DELAY_NOT_EXPIRED)
            if self.security_unlocked:
                return bytes([0x67, sub]) + bytes(SEED_BYTES)  # already unlocked: zero seed
            self._seed = self.rng.getrandbits(SEED_BYTES * 8).to_bytes(SEED_BYTES, "big")
            return bytes([0x67, sub]) + self._seed
        if sub == uds.SECURITY_LEVEL_PROGRAMMING + 1:
            if self._seed is None:
                raise NegativeResponse(req[0], uds.NRC_REQUEST_SEQUENCE_ERROR)
            seed, self._seed = self._seed, None
            if not keys_match(self.spec.security_secret, seed, req[2:]):
                self._failed_keys += 1
                if self._failed_keys >= self.b.security_attempts:
                    self._failed_keys = 0
                    self._lockout_until = self.sim.now + self.b.security_delay_us
                    raise NegativeResponse(req[0], uds.NRC_EXCEEDED_ATTEMPTS)
                raise NegativeResponse(req[0], uds.NRC_INVALID_KEY)
            self._failed_keys = 0
            self.security_unlocked = True
            return bytes([0x67, sub])
        raise NegativeResponse(req[0], uds.NRC_SUBFUNCTION_NOT_SUPPORTED)

    def _require_unlocked(self, sid: int) -> None:
        if not self.security_unlocked:
            raise NegativeResponse(sid, uds.NRC_SECURITY_ACCESS_DENIED)

    # --------------------------------------------------------------------- DTCs
    def set_dtc(self, code: int, status: int = uds.CONFIRMED_DTC | uds.TEST_FAILED
                | uds.TEST_FAILED_SINCE_CLEAR) -> None:
        if self.dtc_setting_on:
            self.dtcs[code] = self.dtcs.get(code, 0) | status

    def _read_dtc(self, req: bytes) -> bytes:
        if len(req) < 2:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        sub = req[1] & 0x7F
        if sub not in (0x01, 0x02):
            raise NegativeResponse(req[0], uds.NRC_SUBFUNCTION_NOT_SUPPORTED)
        if len(req) != 3:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        mask = req[2]
        if sub == 0x01:  # reportNumberOfDTCByStatusMask
            count = sum(1 for s in self.dtcs.values() if s & mask)
            return bytes([0x59, 0x01, uds.STATUS_AVAILABILITY_MASK, 0x01]) + count.to_bytes(2, "big")
        out = bytearray([0x59, 0x02, uds.STATUS_AVAILABILITY_MASK])  # reportDTCByStatusMask
        for code, status in sorted(self.dtcs.items()):
            if status & mask:
                out += code.to_bytes(3, "big") + bytes([status & uds.STATUS_AVAILABILITY_MASK])
        return bytes(out)

    def _clear_dtc(self, req: bytes) -> bytes:
        if len(req) != 4:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        group = int.from_bytes(req[1:4], "big")
        if group == 0xFFFFFF:
            self.dtcs.clear()
        elif group in self.dtcs:
            del self.dtcs[group]
        else:
            raise NegativeResponse(req[0], uds.NRC_REQUEST_OUT_OF_RANGE)
        return bytes([0x54])

    def _control_dtc_setting(self, req: bytes) -> bytes:
        if len(req) < 2:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        sub = req[1] & 0x7F
        if sub not in (0x01, 0x02):
            raise NegativeResponse(req[0], uds.NRC_SUBFUNCTION_NOT_SUPPORTED)
        self.dtc_setting_on = sub == 0x01
        return bytes([0xC5, sub])

    def _communication_control(self, req: bytes) -> bytes:
        if len(req) != 3:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        sub = req[1] & 0x7F
        if sub not in (0x00, 0x01, 0x02, 0x03):
            raise NegativeResponse(req[0], uds.NRC_SUBFUNCTION_NOT_SUPPORTED)
        if req[2] & 0x03 == 0:
            raise NegativeResponse(req[0], uds.NRC_REQUEST_OUT_OF_RANGE)
        # Only transmission of normal messages is modelled: 0x00/0x02 enable it, 0x01/0x03 disable it.
        was_enabled = self.normal_tx_enabled
        self.normal_tx_enabled = sub in (0x00, 0x02)
        if self.normal_tx_enabled and not was_enabled:
            self.comm_restart_hook()
        return bytes([0x68, sub])

    # ----------------------------------------------------------------- routines
    def _routine_control(self, req: bytes) -> "bytes | Pending":
        if len(req) < 4:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        sub = req[1] & 0x7F
        rid = int.from_bytes(req[2:4], "big")
        if sub != 0x01:
            raise NegativeResponse(req[0], uds.NRC_SUBFUNCTION_NOT_SUPPORTED)
        ack = bytes([0x71, sub]) + req[2:4]
        if rid == uds.RID_CHECK_PRECONDITIONS:
            ok = self.supply_mv >= self.b.min_programming_mv
            return ack + bytes([0x00 if ok else 0x01])
        if rid == uds.RID_SELF_TEST_RESULT:
            if self.self_test_passed is None:
                raise NegativeResponse(req[0], uds.NRC_CONDITIONS_NOT_CORRECT)
            return ack + bytes([0x00 if self.self_test_passed else 0x01])
        if rid == uds.RID_COMMIT_SLOT:
            if self.trial_slot is None or not self.self_test_passed:
                raise NegativeResponse(req[0], uds.NRC_CONDITIONS_NOT_CORRECT)
            self.commit()
            return ack + bytes([0x00])
        if self.session != uds.PROGRAMMING_SESSION:
            raise NegativeResponse(req[0], uds.NRC_SERVICE_NOT_SUPPORTED_IN_SESSION)
        if rid == uds.RID_ERASE_MEMORY:
            return self._erase_routine(req, ack)
        if rid == uds.RID_CHECK_MEMORY:
            return self._check_memory(req, ack)
        if rid == uds.RID_ACTIVATE_INACTIVE_SLOT:
            self._require_unlocked(req[0])
            if not self.slots[self.inactive_slot].valid:
                raise NegativeResponse(req[0], uds.NRC_CONDITIONS_NOT_CORRECT)
            self.boot_request = self.inactive_slot
            return ack + bytes([0x00])
        raise NegativeResponse(req[0], uds.NRC_REQUEST_OUT_OF_RANGE)

    def _erase_routine(self, req: bytes, ack: bytes) -> Pending:
        self._require_unlocked(req[0])
        if self.supply_mv < self.b.min_programming_mv:
            raise NegativeResponse(req[0], uds.NRC_VOLTAGE_TOO_LOW)
        if self.trial_slot is not None:
            # Never erase the fallback image while a trial boot is uncommitted.
            raise NegativeResponse(req[0], uds.NRC_CONDITIONS_NOT_CORRECT)
        target = self.inactive_slot
        self.slots[target] = Slot()      # erasing starts now; a power cut leaves it empty
        self.boot_request = None

        def finish() -> bytes:
            self.erased_slot = target
            return ack + bytes([0x00])

        return Pending(self.spec.erase_us_per_kib * self.spec.image_kib, finish)

    def _check_memory(self, req: bytes, ack: bytes) -> Pending:
        if len(req) != 4 + 32:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        expected = req[4:]
        target = self.inactive_slot

        def finish() -> bytes:
            slot = self.slots[target]
            ok = slot.valid and hashlib.sha256(slot.image).digest() == expected
            if not ok:
                self.slots[target] = Slot()
            return ack + bytes([0x00 if ok else 0x01])

        return Pending(self.spec.verify_us_per_kib * self.spec.image_kib, finish)

    # -------------------------------------------------------- download/transfer
    def _request_download(self, req: bytes) -> bytes:
        self._require_unlocked(req[0])
        if self.erased_slot != self.inactive_slot:
            raise NegativeResponse(req[0], uds.NRC_REQUEST_SEQUENCE_ERROR)
        if self.supply_mv < self.b.min_programming_mv:
            raise NegativeResponse(req[0], uds.NRC_VOLTAGE_TOO_LOW)
        if len(req) < 4:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        size_len = (req[2] >> 4) & 0x0F
        addr_len = req[2] & 0x0F
        want = 3 + addr_len + size_len
        if len(req) != want or not size_len or not addr_len:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        size = int.from_bytes(req[3 + addr_len: want], "big")
        if size != self.spec.image_bytes:
            raise NegativeResponse(req[0], uds.NRC_REQUEST_OUT_OF_RANGE)
        self.download = DownloadState(expected=size, active=True)
        # lengthFormatIdentifier 0x20: a two-byte maxNumberOfBlockLength follows.
        return bytes([0x74, 0x20]) + self.spec.max_block_length.to_bytes(2, "big")

    def _transfer_data(self, req: bytes) -> bytes:
        if not self.download.active:
            raise NegativeResponse(req[0], uds.NRC_REQUEST_SEQUENCE_ERROR)
        if len(req) < 3:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        bsc = req[1]
        data = req[2:]
        if len(req) > self.spec.max_block_length:
            raise NegativeResponse(req[0], uds.NRC_INCORRECT_LENGTH)
        if self.supply_mv < self.b.min_programming_mv:
            raise NegativeResponse(req[0], uds.NRC_VOLTAGE_TOO_LOW)
        last = self.download.block_index & 0xFF
        if self.download.block_index > 0 and bsc == last:
            # The tester repeated a block whose response it lost: acknowledge, do not rewrite.
            return bytes([0x76, bsc])
        if bsc != (self.download.block_index + 1) & 0xFF:
            raise NegativeResponse(req[0], uds.NRC_WRONG_BLOCK_SEQUENCE_COUNTER)
        if self.download.received + len(data) > self.download.expected:
            raise NegativeResponse(req[0], uds.NRC_TRANSFER_SUSPENDED)
        self.download.buffer.extend(data)
        self.download.received += len(data)
        self.download.block_index += 1
        return bytes([0x76, bsc])

    def _transfer_exit(self, req: bytes) -> Pending:
        if not self.download.active:
            raise NegativeResponse(req[0], uds.NRC_REQUEST_SEQUENCE_ERROR)
        buffer = bytes(self.download.buffer)
        complete = self.download.received == self.download.expected
        target = self.inactive_slot
        self.download = DownloadState()

        def finish() -> bytes:
            if not complete:
                raise NegativeResponse(uds.REQUEST_TRANSFER_EXIT, uds.NRC_GENERAL_PROGRAMMING_FAILURE)
            self.slots[target] = Slot(buffer, parse_header(buffer).version, valid=True)
            self.erased_slot = None
            self.flash_count += 1
            return bytes([0x77])

        return Pending(self.spec.write_us_per_kib * self.spec.image_kib, finish)

    # ---------------------------------------------------------- environment
    def set_supply(self, mv: int) -> None:
        self.supply_mv = mv

    def set_ambient(self, celsius: int) -> None:
        self.ambient_c = celsius
        self.check_environment()


def lockout_remaining(ecu: Ecu) -> int:
    return max(0, ecu._lockout_until - ecu.sim.now)


__all__ = ["Ecu", "EcuSpec", "EcuBehaviour", "Slot", "Timer"]
