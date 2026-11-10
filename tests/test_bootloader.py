from sdvrig import uds
from sdvrig.flash.image import build_image, parse_header
from sdvrig.vehicle.node import COMM_LOSS_DTC


def program(vehicle, name="BMS", version="7.0.0", healthy=True, cold=None):
    import hashlib
    t, addr = vehicle.tester, vehicle.addr(name)
    ecu = vehicle.node(name).ecu
    image = build_image(name, version, ecu.spec.image_bytes, healthy=healthy, cold_defect=cold)
    t.change_session(addr, uds.EXTENDED_SESSION)
    t.change_session(addr, uds.PROGRAMMING_SESSION)
    t.unlock(addr)
    t.routine_start(addr, uds.RID_ERASE_MEMORY)
    block = t.request_download(addr, len(image)) - 2
    for i, off in enumerate(range(0, len(image), block)):
        t.transfer_data(addr, (i + 1) & 0xFF, image[off: off + block])
    t.transfer_exit(addr)
    assert t.routine_start(addr, uds.RID_CHECK_MEMORY, hashlib.sha256(image).digest())[-1] == 0
    t.routine_start(addr, uds.RID_ACTIVATE_INACTIVE_SLOT)
    t.ecu_reset(addr)
    vehicle.settle(400)
    return ecu, addr


def test_image_header_round_trip():
    info = parse_header(build_image("ZCU_FRONT", "3.2.0", 4096, healthy=False, cold_defect="B1A23-16@HW03"))
    assert (info.version, info.ecu, info.healthy, info.cold_dtc, info.cold_hardware) == \
        ("3.2.0", "ZCU_FRONT", False, "B1A23-16", "HW03")


def test_trial_boot_then_commit(vehicle):
    ecu, addr = program(vehicle)
    t = vehicle.tester
    assert ecu.boot_state == uds.BOOT_TRIAL and t.read_did(addr, uds.DID_SOFTWARE_VERSION) == b"7.0.0"
    t.change_session(addr, uds.EXTENDED_SESSION)
    assert t.routine_start(addr, uds.RID_SELF_TEST_RESULT)[-1] == 0
    t.routine_start(addr, uds.RID_COMMIT_SLOT)
    t.ecu_reset(addr)
    vehicle.settle(400)
    assert ecu.boot_state == uds.BOOT_CONFIRMED and ecu.running.version == "7.0.0"


def test_uncommitted_trial_reverts_on_next_reset(vehicle):
    ecu, addr = program(vehicle)
    vehicle.tester.ecu_reset(addr)
    vehicle.settle(400)
    assert ecu.boot_state == uds.BOOT_REVERTED and ecu.running.version == "1.8.2" and ecu.reverts == 1


def test_failed_self_test_cannot_be_committed(vehicle):
    ecu, addr = program(vehicle, healthy=False)
    t = vehicle.tester
    t.change_session(addr, uds.EXTENDED_SESSION)
    assert t.routine_start(addr, uds.RID_SELF_TEST_RESULT)[-1] == 1
    import pytest
    from sdvrig.uds import NegativeResponse
    with pytest.raises(NegativeResponse):
        t.routine_start(addr, uds.RID_COMMIT_SLOT)


def test_power_cut_mid_transfer_keeps_running_image(vehicle):
    t, addr = vehicle.tester, vehicle.addr("BMS")
    node = vehicle.node("BMS")
    t.change_session(addr, uds.EXTENDED_SESSION)
    t.change_session(addr, uds.PROGRAMMING_SESSION)
    t.unlock(addr)
    t.routine_start(addr, uds.RID_ERASE_MEMORY)
    t.request_download(addr, node.ecu.spec.image_bytes)
    t.transfer_data(addr, 1, bytes(1024))
    node.power_off()
    vehicle.settle(500)
    node.power_on()
    vehicle.settle(500)
    assert node.ecu.running.valid and node.ecu.running.version == "1.8.2"
    assert not node.ecu.slots[node.ecu.inactive_slot].valid
    assert t.read_did(addr, uds.DID_SOFTWARE_VERSION) == b"1.8.2"


def test_erase_refused_while_trial_uncommitted(vehicle):
    ecu, addr = program(vehicle)
    t = vehicle.tester
    t.change_session(addr, uds.EXTENDED_SESSION)
    t.change_session(addr, uds.PROGRAMMING_SESSION)
    t.unlock(addr)
    import pytest
    from sdvrig.uds import NegativeResponse
    with pytest.raises(NegativeResponse) as exc:
        t.routine_start(addr, uds.RID_ERASE_MEMORY)
    assert exc.value.nrc == uds.NRC_CONDITIONS_NOT_CORRECT


def test_silent_peer_raises_comm_loss_unless_dtc_setting_is_off(vehicle):
    thermal = vehicle.node("THERMAL")
    vehicle.node("BMS").power_off()
    vehicle.settle(1500)
    assert COMM_LOSS_DTC in thermal.ecu.dtcs
    thermal.ecu.dtcs.clear()
    thermal.ecu.dtc_setting_on = False
    vehicle.settle(1500)
    assert COMM_LOSS_DTC not in thermal.ecu.dtcs


def test_cold_defect_needs_cold_and_matching_hardware(vehicle):
    ecu, addr = program(vehicle, name="ZCU_FRONT", cold="B1A23-16@HW03")
    from sdvrig.uds import dtc_from_text
    code = dtc_from_text("B1A23-16")
    assert code not in ecu.dtcs
    ecu.set_ambient(-5)
    assert code not in ecu.dtcs
    ecu.set_ambient(-12)
    assert code in ecu.dtcs
