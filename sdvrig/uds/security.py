"""Seed and key for SecurityAccess (0x27).

Real seed/key algorithms are each manufacturer's secret. The rig uses a published
one so anyone can check it: key = HMAC-SHA256(per-ECU secret, seed), truncated to
16 bytes. The secrets live in config/vehicle.json and are test values only.
"""

from __future__ import annotations

import hashlib
import hmac

SEED_BYTES = 8
KEY_BYTES = 16


def key_for(secret: bytes, seed: bytes) -> bytes:
    return hmac.new(secret, seed, hashlib.sha256).digest()[:KEY_BYTES]


def keys_match(secret: bytes, seed: bytes, key: bytes) -> bool:
    return hmac.compare_digest(key_for(secret, seed), key)
