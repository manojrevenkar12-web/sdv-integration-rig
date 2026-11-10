"""ISO 14229-1 (UDS) identifiers shared by the server and the client."""

from __future__ import annotations

# Service identifiers
DIAGNOSTIC_SESSION_CONTROL = 0x10
ECU_RESET = 0x11
CLEAR_DTC = 0x14
READ_DTC_INFORMATION = 0x19
READ_DATA_BY_IDENTIFIER = 0x22
SECURITY_ACCESS = 0x27
COMMUNICATION_CONTROL = 0x28
WRITE_DATA_BY_IDENTIFIER = 0x2E
ROUTINE_CONTROL = 0x31
REQUEST_DOWNLOAD = 0x34
TRANSFER_DATA = 0x36
REQUEST_TRANSFER_EXIT = 0x37
TESTER_PRESENT = 0x3E
CONTROL_DTC_SETTING = 0x85
NEGATIVE_RESPONSE = 0x7F

SERVICE_NAMES = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2E: "WriteDataByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
}

# Sessions
DEFAULT_SESSION = 0x01
PROGRAMMING_SESSION = 0x02
EXTENDED_SESSION = 0x03

# Negative response codes
NRC_GENERAL_REJECT = 0x10
NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_SUBFUNCTION_NOT_SUPPORTED = 0x12
NRC_INCORRECT_LENGTH = 0x13
NRC_BUSY_REPEAT = 0x21
NRC_CONDITIONS_NOT_CORRECT = 0x22
NRC_REQUEST_SEQUENCE_ERROR = 0x24
NRC_REQUEST_OUT_OF_RANGE = 0x31
NRC_SECURITY_ACCESS_DENIED = 0x33
NRC_INVALID_KEY = 0x35
NRC_EXCEEDED_ATTEMPTS = 0x36
NRC_DELAY_NOT_EXPIRED = 0x37
NRC_UPLOAD_DOWNLOAD_NOT_ACCEPTED = 0x70
NRC_TRANSFER_SUSPENDED = 0x71
NRC_GENERAL_PROGRAMMING_FAILURE = 0x72
NRC_WRONG_BLOCK_SEQUENCE_COUNTER = 0x73
NRC_RESPONSE_PENDING = 0x78
NRC_SUBFUNCTION_NOT_SUPPORTED_IN_SESSION = 0x7E
NRC_SERVICE_NOT_SUPPORTED_IN_SESSION = 0x7F
NRC_VOLTAGE_TOO_LOW = 0x93

NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
    0x93: "voltageTooLow",
}

# NRCs a server never sends in answer to a functionally addressed request (ISO 14229-1, 7.5).
FUNCTIONAL_SILENT_NRCS = {0x11, 0x12, 0x31, 0x7E, 0x7F}

# Data identifiers (0xF1xx are the ISO 14229-1 Annex C identification DIDs)
DID_ACTIVE_SESSION = 0xF186
DID_SPARE_PART_NUMBER = 0xF187
DID_SOFTWARE_VERSION = 0xF189
DID_SERIAL_NUMBER = 0xF18C
DID_VIN = 0xF190
DID_HARDWARE_NUMBER = 0xF191
DID_FINGERPRINT = 0xF15A
DID_SUPPLY_VOLTAGE = 0x0101
DID_BOOT_STATE = 0xFD01
DID_AMBIENT_TEMPERATURE = 0xFD02

# Routine identifiers. 0xFF00 and 0xFF01 are defined by ISO 14229-1 Annex F;
# the 0x02xx routines are manufacturer specific and defined by this rig.
RID_ERASE_MEMORY = 0xFF00
RID_CHECK_DEPENDENCIES = 0xFF01
RID_CHECK_MEMORY = 0x0202
RID_CHECK_PRECONDITIONS = 0x0203
RID_ACTIVATE_INACTIVE_SLOT = 0x0210
RID_SELF_TEST_RESULT = 0x0211
RID_COMMIT_SLOT = 0x0212
RID_REVERT_SLOT = 0x0213

# Security access level used for programming (requestSeed 0x11 / sendKey 0x12).
SECURITY_LEVEL_PROGRAMMING = 0x11

# DTC status bits (ISO 14229-1 Annex D.2)
TEST_FAILED = 0x01
TEST_FAILED_THIS_CYCLE = 0x02
PENDING_DTC = 0x04
CONFIRMED_DTC = 0x08
TEST_FAILED_SINCE_CLEAR = 0x20
STATUS_AVAILABILITY_MASK = 0x2F

# Boot states reported in DID 0xFD01
BOOT_CONFIRMED = 0x00
BOOT_TRIAL = 0x01
BOOT_TRIAL_HEALTHY = 0x02
BOOT_REVERTED = 0x03
BOOT_NO_APPLICATION = 0x04

_DTC_LETTERS = "PCBU"


def dtc_to_text(code: int) -> str:
    """Three-byte DTC to its SAE J2012 display form, e.g. 0xC11100 -> 'U0111-00'."""
    high = code >> 8
    letter = _DTC_LETTERS[(high >> 14) & 0x3]
    digits = f"{(high >> 12) & 0x3}{(high >> 8) & 0xF:X}{(high >> 4) & 0xF:X}{high & 0xF:X}"
    return f"{letter}{digits}-{code & 0xFF:02X}"


def dtc_from_text(text: str) -> int:
    base, _, ftb = text.partition("-")
    letter = _DTC_LETTERS.index(base[0].upper())
    first = int(base[1], 16)
    if first > 3:
        raise ValueError(f"the first digit of a DTC is 0-3, not {base[1]}")
    high = (letter << 14) | (first << 12) | int(base[2:5], 16)
    return (high << 8) | int(ftb or "00", 16)


class NegativeResponse(Exception):
    """Raised by a service handler to answer 7F <sid> <nrc>; raised by the client on receipt."""

    def __init__(self, sid: int, nrc: int):
        super().__init__(f"{SERVICE_NAMES.get(sid, hex(sid))}: {NRC_NAMES.get(nrc, hex(nrc))} ({nrc:#04x})")
        self.sid = sid
        self.nrc = nrc
