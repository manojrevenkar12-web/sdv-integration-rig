import pytest

from sdvrig.capture import LINKTYPE_CAN_SOCKETCAN, socketcan_frame
from sdvrig.capture.crosscheck import RUST_TOOL, crosscheck
from sdvrig.flash.campaign import flasher_for
from sdvrig.flash.image import build_image
from sdvrig.vehicle import build_vehicle

pytest.importorskip("scapy")


def test_socketcan_record_layout():
    rec = socketcan_frame(0x7A1, b"\x02\x10\x03", fd=False, brs=False)
    assert len(rec) == 16 and rec[:4] == b"\x00\x00\x07\xa1" and rec[4] == 3 and rec[5] == 0
    fd = socketcan_frame(0x7A1, bytes(12), fd=True, brs=True)
    assert len(fd) == 72 and fd[5] == 0x05
    assert LINKTYPE_CAN_SOCKETCAN == 227


@pytest.mark.parametrize("ecu", ["ZCU_FRONT", "BMS"])
def test_independent_decoders_agree_with_the_tester(ecu, tmp_path):
    v = build_vehicle()
    flasher_for(v, "robust", ecu).flash(build_image(ecu, "4.0.0", v.node(ecu).ecu.spec.image_bytes))
    report = crosscheck(v, ecu, tmp_path, ecu)
    by = {c["decoder"]: c for c in report["checks"]}
    assert by["doip/scapy"]["agrees"], by["doip/scapy"]
    assert by["isotp/scapy"]["agrees"], by["isotp/scapy"]
    if ecu == "ZCU_FRONT":     # CAN FD: scapy 2.6.1 needs the frame-level check for escaped single frames
        assert by["isotp/scapy"]["fd_escape_frames_checked_at_frame_level"] > 0
    if RUST_TOOL.exists():
        assert by["isotp/rust"]["agrees"] and by["isotp/rust"]["decoder_errors"] == 0, by["isotp/rust"]
