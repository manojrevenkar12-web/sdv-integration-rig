"""ISO 15765-2 transport: segmentation, flow control and timeouts, for classic CAN and CAN FD.

Frame types, by the high nibble of the first byte (the protocol control information):
  0x0 single frame      0x1 first frame      0x2 consecutive frame      0x3 flow control

CAN FD adds escape sequences: a single frame longer than 7 bytes is `00 len`, and
a first frame longer than 4095 bytes is `10 00` followed by a 32-bit length.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Callable

from sdvrig.can import CanFrame, CanPort, fd_length_for
from sdvrig.sim import Sim, Timer

SF, FF, CF, FC = 0x0, 0x1, 0x2, 0x3
FC_CTS, FC_WAIT, FC_OVFLW = 0, 1, 2


class IsoTpError(enum.Enum):
    TIMEOUT_BS = "N_Bs timeout: no flow control from the receiver"
    TIMEOUT_CR = "N_Cr timeout: consecutive frame missing"
    WRONG_SN = "consecutive frame out of sequence"
    OVERFLOW = "receiver reported buffer overflow"
    WFT_OVERRUN = "receiver sent more WAIT frames than allowed"
    UNEXPECTED_PDU = "new message started before the last one finished"
    TX_FAILED = "frame could not be sent (node offline)"


def encode_st_min(us: int) -> int:
    if us <= 0:
        return 0
    if us < 1000:
        return 0xF0 + max(1, min(9, us // 100))
    return min(0x7F, us // 1000)


def decode_st_min(raw: int) -> int:
    """STmin in microseconds. Reserved values mean the maximum, 127 ms."""
    if raw <= 0x7F:
        return raw * 1000
    if 0xF1 <= raw <= 0xF9:
        return (raw - 0xF0) * 100
    return 127_000


@dataclass(frozen=True)
class IsoTpParams:
    fd: bool
    block_size: int = 0
    st_min_us: int = 0
    n_bs_us: int = 1_000_000
    n_cr_us: int = 1_000_000
    wft_max: int = 4
    padding: int = 0xCC
    max_message_bytes: int = 65535

    @property
    def tx_dl(self) -> int:
        return 64 if self.fd else 8

    @property
    def sf_max(self) -> int:
        return 62 if self.fd else 7


def pad(data: bytes, params: IsoTpParams) -> bytes:
    size = fd_length_for(len(data)) if params.fd else 8
    if params.fd and len(data) <= 8:
        size = 8
    return data + bytes([params.padding]) * (size - len(data))


def segment(payload: bytes, params: IsoTpParams) -> tuple[bytes, list[bytes]]:
    """Split a message into its first frame (or single frame) and consecutive frames.

    Returned frames are unpadded; `pad` is applied when sending.
    """
    n = len(payload)
    if n == 0:
        raise ValueError("ISO-TP messages carry at least one byte")
    if n <= 7:
        return bytes([n]) + payload, []
    if params.fd and n <= params.sf_max:
        return bytes([0x00, n]) + payload, []
    dl = params.tx_dl
    if n <= 0xFFF:
        head = bytes([0x10 | (n >> 8), n & 0xFF])
    else:
        head = bytes([0x10, 0x00]) + n.to_bytes(4, "big")
    first = head + payload[: dl - len(head)]
    rest = payload[dl - len(head):]
    cfs = []
    sn = 1
    for i in range(0, len(rest), dl - 1):
        cfs.append(bytes([0x20 | sn]) + rest[i: i + dl - 1])
        sn = (sn + 1) & 0x0F
    return first, cfs


class IsoTpLink:
    """One ISO-TP connection: frames out on `tx_id`, frames in on `rx_id`."""

    def __init__(
        self,
        sim: Sim,
        port: CanPort,
        tx_id: int,
        rx_id: int,
        params: IsoTpParams,
        on_message: Callable[[bytes], None],
        on_error: Callable[[IsoTpError], None] | None = None,
        name: str = "",
    ):
        self.sim = sim
        self.port = port
        self.tx_id = tx_id
        self.rx_id = rx_id
        self.params = params
        self.on_message = on_message
        self.on_error = on_error or (lambda err: None)
        self.name = name or f"{tx_id:#x}/{rx_id:#x}"
        self.errors: list[IsoTpError] = []
        # transmit state
        self._tx_cfs: list[bytes] = []
        self._tx_index = 0
        self._tx_bs_left = 0
        self._tx_st_min = 0
        self._tx_wft = 0
        self._tx_timer: Timer | None = None
        self._tx_waiting_fc = False
        self._tx_done: Callable[[bool], None] | None = None
        # receive state
        self._rx_buf = bytearray()
        self._rx_len = 0
        self._rx_sn = 0
        self._rx_bs_count = 0
        self._rx_timer: Timer | None = None
        self.messages_sent = 0
        self.messages_received = 0

    # ------------------------------------------------------------------ transmit
    @property
    def busy(self) -> bool:
        return self._tx_done is not None

    def send(self, payload: bytes, done: Callable[[bool], None] | None = None) -> None:
        if self.busy:
            raise RuntimeError(f"{self.name}: a message is already being sent")
        first, cfs = segment(payload, self.params)
        self._tx_done = done or (lambda ok: None)
        self._tx_cfs = cfs
        self._tx_index = 0
        self._tx_wft = 0
        if not cfs:
            self._send_frame(first, self._finish_tx)
            return
        self._tx_waiting_fc = True
        self._send_frame(first, self._after_first)

    def _send_frame(self, data: bytes, done: Callable[[bool], None]) -> None:
        frame = CanFrame(self.tx_id, pad(data, self.params), fd=self.params.fd, brs=self.params.fd)
        self.port.send(frame, done)

    def _after_first(self, ok: bool) -> None:
        if not ok:
            self._fail_tx(IsoTpError.TX_FAILED)
            return
        self._arm_tx(self.params.n_bs_us, IsoTpError.TIMEOUT_BS)

    def _arm_tx(self, delay: int, err: IsoTpError) -> None:
        if self._tx_timer:
            self._tx_timer.cancel()
        self._tx_timer = self.sim.later(delay, self._fail_tx, err)

    def _fail_tx(self, err: IsoTpError) -> None:
        if not self.busy:
            return
        self.errors.append(err)
        self._reset_tx()
        done = self._tx_done
        self._tx_done = None
        self.on_error(err)
        if done:
            done(False)

    def _reset_tx(self) -> None:
        if self._tx_timer:
            self._tx_timer.cancel()
            self._tx_timer = None
        self._tx_waiting_fc = False
        self._tx_cfs = []

    def _finish_tx(self, ok: bool) -> None:
        if not self.busy:
            return
        if not ok:
            self._fail_tx(IsoTpError.TX_FAILED)
            return
        self._reset_tx()
        done = self._tx_done
        self._tx_done = None
        self.messages_sent += 1
        if done:
            done(True)

    def _on_flow_control(self, data: bytes) -> None:
        if not self._tx_waiting_fc or len(data) < 3:
            return
        status = data[0] & 0x0F
        if status == FC_WAIT:
            self._tx_wft += 1
            if self._tx_wft > self.params.wft_max:
                self._fail_tx(IsoTpError.WFT_OVERRUN)
            else:
                self._arm_tx(self.params.n_bs_us, IsoTpError.TIMEOUT_BS)
            return
        if status == FC_OVFLW:
            self._fail_tx(IsoTpError.OVERFLOW)
            return
        if status != FC_CTS:
            return
        if self._tx_timer:
            self._tx_timer.cancel()
            self._tx_timer = None
        self._tx_waiting_fc = False
        self._tx_bs_left = data[1]
        self._tx_st_min = decode_st_min(data[2])
        self._send_next_cf()

    def _send_next_cf(self) -> None:
        if not self.busy:
            return
        data = self._tx_cfs[self._tx_index]
        self._tx_index += 1
        last = self._tx_index == len(self._tx_cfs)
        self._send_frame(data, self._finish_tx if last else self._after_cf)

    def _after_cf(self, ok: bool) -> None:
        if not self.busy:
            return
        if not ok:
            self._fail_tx(IsoTpError.TX_FAILED)
            return
        if self._tx_bs_left:
            self._tx_bs_left -= 1
            if self._tx_bs_left == 0:
                self._tx_waiting_fc = True
                self._arm_tx(self.params.n_bs_us, IsoTpError.TIMEOUT_BS)
                return
        if self._tx_st_min:
            self.sim.later(self._tx_st_min, self._send_next_cf)
        else:
            self._send_next_cf()

    # ------------------------------------------------------------------- receive
    def on_frame(self, frame: CanFrame) -> None:
        data = frame.data
        if not data:
            return
        kind = data[0] >> 4
        if kind == FC:
            self._on_flow_control(data)
        elif kind == SF:
            self._on_single(data)
        elif kind == FF:
            self._on_first(data)
        elif kind == CF:
            self._on_consecutive(data)

    @property
    def receiving(self) -> bool:
        return self._rx_len > 0

    def _abort_rx(self, err: IsoTpError) -> None:
        if self._rx_timer:
            self._rx_timer.cancel()
            self._rx_timer = None
        self._rx_len = 0
        self._rx_buf = bytearray()
        self.errors.append(err)
        self.on_error(err)

    def _on_single(self, data: bytes) -> None:
        if self.receiving:
            self._abort_rx(IsoTpError.UNEXPECTED_PDU)
        n = data[0] & 0x0F
        body = data[1:]
        if n == 0 and self.params.fd and len(data) > 8:
            n = data[1]
            body = data[2:]
        if n == 0 or n > len(body):
            return
        self.messages_received += 1
        self.on_message(bytes(body[:n]))

    def _on_first(self, data: bytes) -> None:
        if self.receiving:
            self._abort_rx(IsoTpError.UNEXPECTED_PDU)
        n = ((data[0] & 0x0F) << 8) | data[1]
        body = data[2:]
        if n == 0:
            n = int.from_bytes(data[2:6], "big")
            body = data[6:]
        if n > self.params.max_message_bytes:
            self._send_fc(FC_OVFLW)
            return
        self._rx_len = n
        self._rx_buf = bytearray(body)
        self._rx_sn = 1
        self._rx_bs_count = 0
        self._send_fc(FC_CTS)
        self._arm_rx()

    def _arm_rx(self) -> None:
        if self._rx_timer:
            self._rx_timer.cancel()
        self._rx_timer = self.sim.later(self.params.n_cr_us, self._abort_rx, IsoTpError.TIMEOUT_CR)

    def _send_fc(self, status: int) -> None:
        data = bytes([0x30 | status, self.params.block_size, encode_st_min(self.params.st_min_us)])
        self._send_frame(data, lambda ok: None)

    def _on_consecutive(self, data: bytes) -> None:
        if not self.receiving:
            return
        sn = data[0] & 0x0F
        if sn != self._rx_sn:
            self._abort_rx(IsoTpError.WRONG_SN)
            return
        self._rx_sn = (sn + 1) & 0x0F
        need = self._rx_len - len(self._rx_buf)
        self._rx_buf.extend(data[1: 1 + need])
        if len(self._rx_buf) >= self._rx_len:
            if self._rx_timer:
                self._rx_timer.cancel()
                self._rx_timer = None
            payload = bytes(self._rx_buf)
            self._rx_len = 0
            self._rx_buf = bytearray()
            self.messages_received += 1
            self.on_message(payload)
            return
        if self.params.block_size:
            self._rx_bs_count += 1
            if self._rx_bs_count == self.params.block_size:
                self._rx_bs_count = 0
                self._send_fc(FC_CTS)
        self._arm_rx()

    def reset(self) -> None:
        """Drop all state, as a power cycle would."""
        if self._rx_timer:
            self._rx_timer.cancel()
            self._rx_timer = None
        self._rx_len = 0
        self._rx_buf = bytearray()
        self._reset_tx()
        self._tx_done = None
