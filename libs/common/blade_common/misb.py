"""MISB ST 0601.8 UAS Datalink Local Set: the standard drone telemetry KLV item, built from tel record."""
from __future__ import annotations

import logging
import math
import struct

from .klv import ber_length

log = logging.getLogger("misb")

UAS_LS_KEY = bytes.fromhex("060e2b34020b01010e01030101000000")
VERSION = 8  # ST 0601.8, the revision this follows (Tag 65)

# (latitude, longitude) of each map's south-west corner, deg. Approximate: the real islands the maps are modelled on
# (Altis: Lemnos, Stratis: Agios Efstratios). Any fixed value keeps the positions of one map consistent.
ANCHORS = {"altis": (39.78, 25.07), "stratis": (39.49, 24.93)}

_A = 6378137.0  # WGS84 semi-major axis, m
_E2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)  # WGS84 first eccentricity squared
_warned: set[str] = set()


def to_latlon(map_name: str, x: float, y: float) -> tuple[float, float]:
    """Map metres east / north of the south-west corner -> (lat, lon) deg, on the plane tangent at the anchor."""
    lat0, lon0 = ANCHORS[map_name]
    s = math.sin(math.radians(lat0))
    w = 1 - _E2 * s * s
    meridian = _A * (1 - _E2) / w ** 1.5  # radius of curvature north-south
    normal = _A / math.sqrt(w)  # east-west, before the cos(lat) of the parallel
    return lat0 + math.degrees(y / meridian), lon0 + math.degrees(x / (normal * math.cos(math.radians(lat0))))


def camera_angles(cam: dict) -> tuple[float, float, float]:
    """(azimuth, elevation, roll) deg of the camera in ST 0601 terms: azimuth clockwise from north, elevation
    negative down, roll clockwise looking from behind the camera with the top of the image at zero."""
    f, u = _unit(cam["z_axis"]), _unit(cam["y_axis"])  # forward (optical axis), up (top of the image)
    hx, hy = f[0], f[1]
    if math.hypot(hx, hy) < 1e-6:  # straight down (or up): the top of the image gives the heading
        hx, hy = (u[0], u[1]) if f[2] < 0 else (-u[0], -u[1])
    az = math.atan2(hx, hy)
    el = math.asin(max(-1.0, min(1.0, f[2])))
    right0 = (math.cos(az), -math.sin(az), 0.0)  # the camera's right with no roll: level
    up0 = _cross(right0, f)
    roll = math.atan2(_dot(u, right0), _dot(u, up0))
    return math.degrees(az) % 360, math.degrees(el), math.degrees(roll) % 360


def unsigned(value: float, lo: float, hi: float, nbytes: int) -> bytes:
    """ST 0601 "map 0..(2^n-1) to lo..hi", clamped to the range."""
    top = (1 << (8 * nbytes)) - 1
    return round((min(max(value, lo), hi) - lo) / (hi - lo) * top).to_bytes(nbytes, "big")


def signed(value: float, hi: float, nbytes: int) -> bytes:
    """ST 0601 "map -(2^(n-1)-1)..(2^(n-1)-1) to +/-hi", clamped to the range."""
    top = (1 << (8 * nbytes - 1)) - 1
    return round(min(max(value, -hi), hi) / hi * top).to_bytes(nbytes, "big", signed=True)


def checksum(data: bytes) -> int:
    """Tag 1: low 16 bits of the sum of 16-bit big-endian words, from the key up to the checksum's length byte."""
    return sum(b << (8 * ((i + 1) % 2)) for i, b in enumerate(data)) & 0xFFFF


def item(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + ber_length(len(value)) + value  # every tag used here is < 128: one BER-OID byte


def encode_0601(tel: dict) -> bytes | None:
    """The UAS Datalink LS of one tel record (with the uplink's t_capture, drone, mission), as one KLV item.
    None for a map without an anchor."""
    mp = tel.get("map")
    if mp not in ANCHORS:
        if mp not in _warned:
            _warned.add(mp)
            log.warning("no MISB anchor for map %r: frames carry only the blade telemetry", mp)
        return None
    cam = tel["camera"]
    x, y, z = cam["pos_asl"]
    lat, lon = to_latlon(mp, x, y)
    az, el, roll = camera_angles(cam)
    items = [item(2, struct.pack(">Q", round(tel["t_capture"] * 1e6)))]  # Tag 2 first (ST 0601.8-09)
    for tag, key in ((3, "mission"), (10, "drone")):
        if text := tel.get(key):
            items.append(item(tag, str(text).encode("ascii", "replace")[:127]))
    items += [item(5, unsigned(0.0, 0, 360, 2)), item(6, signed(0.0, 20, 2)), item(7, signed(0.0, 50, 2)),
              item(13, signed(lat, 90, 4)), item(14, signed(lon, 180, 4)), item(15, unsigned(z, -900, 19000, 2)),
              item(16, unsigned(cam["hfov_deg"], 0, 180, 2)), item(17, unsigned(cam["vfov_deg"], 0, 180, 2)),
              item(18, unsigned(az, 0, 360, 4)), item(19, signed(el, 180, 4)), item(20, unsigned(roll, 0, 360, 4))]
    rng = tel.get("range_m")
    if rng and rng > 0:  # the laser point: along the optical axis, so pos + range * forward
        f = _unit(cam["z_axis"])
        hx, hy, hz = (p + rng * d for p, d in zip(cam["pos_asl"], f))
        hlat, hlon = to_latlon(mp, hx, hy)
        items += [item(21, unsigned(rng, 0, 5_000_000, 4)), item(23, signed(hlat, 90, 4)),
                  item(24, signed(hlon, 180, 4)), item(25, unsigned(hz, -900, 19000, 2))]
    items.append(item(65, bytes([VERSION])))
    body = b"".join(items)
    head = UAS_LS_KEY + ber_length(len(body) + 4) + body + b"\x01\x02"  # Tag 1 last, its value the sum so far
    return head + checksum(head).to_bytes(2, "big")


def _unit(v) -> tuple[float, float, float]:
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return v[0] / n, v[1] / n, v[2] / n


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b) -> tuple[float, float, float]:
    return a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]
