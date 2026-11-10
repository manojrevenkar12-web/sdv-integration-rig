import pytest

from sdvrig import uds
from sdvrig.flash.image import build_image
from sdvrig.uds import NegativeResponse, dtc_from_text, dtc_to_text
from sdvrig.uds.security import key_for
from sdvrig.vehicle.tester import parse_dtc_records

BMS = 0x1B01


def nrc(fn, *args):
    with pytest.raises(NegativeResponse) as exc:
        fn(*args)
    return exc.value.nrc


@pytest.mark.parametrize("text,code", [("U0100-00", 0xC10000), ("P0A7F-1C", 0x0A7F1C), ("B1A23-16", 0x9A2316),
                                       ("C0035-00", 0x403500), ("U3000-49", 0xF00049)])
def test_dtc_text_round_trip(text, code):
    assert dtc_from_text(text) == code and dtc_to_text(code) == text


def test_identification_dids(vehicle):
    t = vehicle.tester
    assert t.read_did(BMS, uds.DID_VIN) == b"1RVSDV0TEST000001"
    assert t.read_did(BMS, uds.DID_SOFTWARE_VERSION) == b"1.8.2"
    assert nrc(t.read_did, BMS, 0x1234) == uds.NRC_REQUEST_OUT_OF_RANGE


def test_session_rules(vehicle):
    t = vehicle.tester
    # programming only from extended (or programming)
    assert nrc(t.change_session, BMS, uds.PROGRAMMING_SESSION) == uds.NRC_SUBFUNCTION_NOT_SUPPORTED_IN_SESSION
    resp = t.change_session(BMS, uds.EXTENDED_SESSION)
    assert resp[:2] == b"\x50\x03" and int.from_bytes(resp[2:4], "big") == 50      # P2server in ms
    assert t.change_session(BMS, uds.PROGRAMMING_SESSION)[:2] == b"\x50\x02"
    # services outside their sessions
    t.change_session(BMS, uds.EXTENDED_SESSION)
    assert nrc(t.request, BMS, bytes([uds.TRANSFER_DATA, 1, 0])) == uds.NRC_SERVICE_NOT_SUPPORTED_IN_SESSION
    assert nrc(t.request, BMS, bytes([0xBA])) == uds.NRC_SERVICE_NOT_SUPPORTED


def test_s3_timeout_returns_to_default(vehicle):
    t = vehicle.tester
    t.change_session(BMS, uds.EXTENDED_SESSION)
    vehicle.settle(4000)
    assert vehicle.node("BMS").ecu.session == uds.EXTENDED_SESSION
    vehicle.settle(1500)
    assert vehicle.node("BMS").ecu.session == uds.DEFAULT_SESSION


def test_tester_present_keeps_session(vehicle):
    t = vehicle.tester
    t.change_session(BMS, uds.EXTENDED_SESSION)
    for _ in range(4):
        vehicle.settle(2000)
        t.tester_present(BMS)
    assert vehicle.node("BMS").ecu.session == uds.EXTENDED_SESSION


def test_suppress_positive_response(vehicle):
    t = vehicle.tester
    before = len([e for e in t.log if e[1] == "rsp"])
    t.request(BMS, bytes([uds.DIAGNOSTIC_SESSION_CONTROL, 0x83]), expect=False)
    vehicle.settle(100)
    assert vehicle.node("BMS").ecu.session == uds.EXTENDED_SESSION
    assert len([e for e in t.log if e[1] == "rsp"]) == before


def test_functional_requests_reach_every_ecu_and_stay_silent_on_nrc(vehicle):
    t = vehicle.tester
    t.functional(vehicle.functional_address, bytes([uds.DIAGNOSTIC_SESSION_CONTROL, 0x83]))
    assert {n.ecu.session for n in vehicle.nodes.values()} == {uds.EXTENDED_SESSION}
    before = len(t.log)
    t.functional(vehicle.functional_address, bytes([0xBA, 0x00]))   # unsupported: silent when functional
    vehicle.settle(100)
    assert not [e for e in t.log[before:] if e[1] == "rsp"]


def test_security_access_seed_key_attempts_and_delay(vehicle):
    t, ecu = vehicle.tester, vehicle.node("BMS").ecu
    t.change_session(BMS, uds.EXTENDED_SESSION)
    wrong = b"\x00" * 16
    for expected in (uds.NRC_INVALID_KEY, uds.NRC_INVALID_KEY, uds.NRC_EXCEEDED_ATTEMPTS):
        seed = t.request(BMS, bytes([uds.SECURITY_ACCESS, 0x11]))[2:]
        assert len(seed) == 8 and any(seed)
        assert nrc(t.request, BMS, bytes([uds.SECURITY_ACCESS, 0x12]) + wrong) == expected
    assert nrc(t.request, BMS, bytes([uds.SECURITY_ACCESS, 0x11])) == uds.NRC_DELAY_NOT_EXPIRED
    vehicle.settle(10_100)
    t.change_session(BMS, uds.EXTENDED_SESSION)
    t.unlock(BMS)
    assert ecu.security_unlocked
    # an unlocked ECU answers requestSeed with a zero seed
    assert t.request(BMS, bytes([uds.SECURITY_ACCESS, 0x11]))[2:] == bytes(8)
    # sendKey without requestSeed is a sequence error
    t.change_session(BMS, uds.EXTENDED_SESSION)
    assert nrc(t.request, BMS, bytes([uds.SECURITY_ACCESS, 0x12]) + key_for(b"x", b"y")) == uds.NRC_REQUEST_SEQUENCE_ERROR


def test_session_change_relocks(vehicle):
    t, ecu = vehicle.tester, vehicle.node("BMS").ecu
    t.change_session(BMS, uds.EXTENDED_SESSION)
    t.unlock(BMS)
    t.change_session(BMS, uds.PROGRAMMING_SESSION)
    assert not ecu.security_unlocked
    assert nrc(t.routine_start, BMS, uds.RID_ERASE_MEMORY) == uds.NRC_SECURITY_ACCESS_DENIED


def test_long_erase_answers_response_pending(vehicle):
    t = vehicle.tester
    t.change_session(BMS, uds.EXTENDED_SESSION)
    t.change_session(BMS, uds.PROGRAMMING_SESSION)
    t.unlock(BMS)
    pending = t.pending_seen
    t.routine_start(BMS, uds.RID_ERASE_MEMORY)       # 48 KiB x 2.5 ms/KiB = 120 ms > P2server
    assert t.pending_seen > pending


def test_low_voltage_blocks_programming(vehicle):
    t = vehicle.tester
    vehicle.node("BMS").ecu.set_supply(11_800)
    t.change_session(BMS, uds.EXTENDED_SESSION)
    t.change_session(BMS, uds.PROGRAMMING_SESSION)
    t.unlock(BMS)
    assert t.routine_start(BMS, uds.RID_CHECK_PRECONDITIONS)[-1] == 0x01
    assert nrc(t.routine_start, BMS, uds.RID_ERASE_MEMORY) == uds.NRC_VOLTAGE_TOO_LOW


def _start_download(vehicle, size=None):
    t = vehicle.tester
    t.change_session(BMS, uds.EXTENDED_SESSION)
    t.change_session(BMS, uds.PROGRAMMING_SESSION)
    t.unlock(BMS)
    t.routine_start(BMS, uds.RID_ERASE_MEMORY)
    return t.request_download(BMS, size or vehicle.node("BMS").ecu.spec.image_bytes)


def test_download_rules(vehicle):
    t = vehicle.tester
    assert nrc(_start_download, vehicle, 1234) == uds.NRC_REQUEST_OUT_OF_RANGE
    max_block = t.request_download(BMS, vehicle.node("BMS").ecu.spec.image_bytes)
    assert max_block == 1026
    assert nrc(t.transfer_data, BMS, 2, b"x") == uds.NRC_WRONG_BLOCK_SEQUENCE_COUNTER
    assert t.transfer_data(BMS, 1, bytes(1024)) == b"\x76\x01"
    assert t.transfer_data(BMS, 1, bytes(1024)) == b"\x76\x01"     # a repeat is acknowledged, not rewritten
    assert vehicle.node("BMS").ecu.download.received == 1024
    assert nrc(t.transfer_data, BMS, 2, bytes(1025)) == uds.NRC_INCORRECT_LENGTH
    assert nrc(t.transfer_exit, BMS) == uds.NRC_GENERAL_PROGRAMMING_FAILURE   # incomplete image


def test_check_memory_compares_sha256(vehicle):
    import hashlib
    t = vehicle.tester
    ecu = vehicle.node("BMS").ecu
    image = build_image("BMS", "7.0.0", ecu.spec.image_bytes)
    _start_download(vehicle)
    for i, off in enumerate(range(0, len(image), 1024)):
        t.transfer_data(BMS, (i + 1) & 0xFF, image[off: off + 1024])
    t.transfer_exit(BMS)
    assert t.routine_start(BMS, uds.RID_CHECK_MEMORY, bytes(32))[-1] == 0x01      # wrong hash: slot invalidated
    assert not ecu.slots[ecu.inactive_slot].valid
    assert nrc(t.routine_start, BMS, uds.RID_ACTIVATE_INACTIVE_SLOT) == uds.NRC_CONDITIONS_NOT_CORRECT
    assert hashlib.sha256(image).digest() != bytes(32)


def test_read_and_clear_dtcs(vehicle):
    t, ecu = vehicle.tester, vehicle.node("THERMAL").ecu
    ecu.set_dtc(dtc_from_text("P1C02-00"))
    records = t.read_dtcs(0x1B02)
    assert [(dtc_to_text(c), s & uds.CONFIRMED_DTC) for c, s in records] == [("P1C02-00", uds.CONFIRMED_DTC)]
    count = t.request(0x1B02, bytes([uds.READ_DTC_INFORMATION, 0x01, 0x08]))
    assert int.from_bytes(count[4:6], "big") == 1
    t.clear_dtcs(0x1B02)
    assert t.read_dtcs(0x1B02) == []
    assert nrc(t.clear_dtcs, 0x1B02, 0x123456) == uds.NRC_REQUEST_OUT_OF_RANGE


def test_dtc_record_parser_rejects_bad_input():
    with pytest.raises(ValueError):
        parse_dtc_records(b"\x59\x01\x2f")
    with pytest.raises(ValueError):
        parse_dtc_records(b"\x59\x02\x2f\x01\x02")
