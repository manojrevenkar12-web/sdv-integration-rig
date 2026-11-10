"""Firmware image builder.

An image is deterministic for a given ECU, version and size. Its first 96 bytes are a
text header the simulated application reads after boot:

    VER:3.2.0          version reported in DID 0xF189
    ECU:ZCU_FRONT      the ECU the build is for
    HEALTHY:1          0 makes the post-boot self-test fail
    COLD:B1A23-16@HW03 a defect that only shows below the cold threshold on the
                       named hardware revision (or "-" for none)

The COLD field is how a release passes every bench test at room temperature and
still misbehaves in a Nordic winter: the bench cannot see it unless the rig is run
cold on the right hardware, which is what the fleet triage step does.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

HEADER_BYTES = 96


def build_image(ecu_name: str, version: str, size_bytes: int, *, healthy: bool = True,
                cold_defect: str | None = None) -> bytes:
    header = (f"VER:{version}\nECU:{ecu_name}\nHEALTHY:{int(healthy)}\n"
              f"COLD:{cold_defect or '-'}\n").encode()
    if len(header) > HEADER_BYTES:
        raise ValueError(f"image header does not fit in {HEADER_BYTES} bytes")
    if size_bytes < HEADER_BYTES:
        raise ValueError("image is smaller than its header")
    header = header.ljust(HEADER_BYTES, b"\x00")
    seed = f"{ecu_name}:{version}:{healthy}:{cold_defect}".encode()
    block = hashlib.sha256(seed).digest()
    body_len = size_bytes - HEADER_BYTES
    body = (block * (body_len // len(block) + 1))[:body_len]
    return header + body


@dataclass(frozen=True)
class ImageInfo:
    version: str
    ecu: str
    healthy: bool
    cold_dtc: str | None
    cold_hardware: str | None


def parse_header(image: bytes) -> ImageInfo:
    fields: dict[str, str] = {}
    for line in image[:HEADER_BYTES].split(b"\n"):
        if b":" in line:
            key, _, value = line.partition(b":")
            fields[key.decode("ascii", "replace")] = value.rstrip(b"\x00").decode("ascii", "replace")
    cold = fields.get("COLD", "-")
    cold_dtc = cold_hw = None
    if cold and cold != "-":
        cold_dtc, _, cold_hw = cold.partition("@")
        cold_hw = cold_hw or None
    return ImageInfo(
        version=fields.get("VER", "unknown"),
        ecu=fields.get("ECU", ""),
        healthy=fields.get("HEALTHY", "1") == "1",
        cold_dtc=cold_dtc,
        cold_hardware=cold_hw,
    )


def digest(image: bytes) -> str:
    return hashlib.sha256(image).hexdigest()
