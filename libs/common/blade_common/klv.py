"""Telemetry inside the MPEG-TS: one KLV packet per video frame (same PTS) holding the tel record as JSON."""
from __future__ import annotations

import json

TEL_KEY = bytes.fromhex("060e2b34010101010f0000005a504901")
PAD_KEY = bytes.fromhex("060e2b34010101010f0000005a504900")  # empty filler, see TsWriter.write


def ber_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def read_ber(buf: bytes, i: int) -> tuple[int, int]:
    """-> (length, index after the length field)."""
    first = buf[i]
    if first < 0x80:
        return first, i + 1
    k = first & 0x7F
    return int.from_bytes(buf[i + 1:i + 1 + k], "big"), i + 1 + k


def encode(key: bytes, value: bytes) -> bytes:
    return key + ber_length(len(value)) + value


def decode(buf: bytes) -> list[tuple[bytes, bytes]]:
    """All (key, value) pairs in a packet; stops at a truncated tail."""
    out, i = [], 0
    while i + 17 <= len(buf):
        key = bytes(buf[i:i + 16])
        n, j = read_ber(buf, i + 16)
        if j + n > len(buf):
            break
        out.append((key, bytes(buf[j:j + n])))
        i = j + n
    return out


def encode_tel(tel: dict) -> bytes:
    return encode(TEL_KEY, json.dumps(tel, separators=(",", ":")).encode())


def decode_tel(buf: bytes) -> dict | None:
    return next((json.loads(v) for k, v in decode(buf) if k == TEL_KEY), None)
