"""MISB ST 0601.8 encoding, checked against the example values and bytes printed in the standard."""
import math
import struct

import pytest

from blade_common import geo, klv, misb
from blade_common.klv import read_ber


@pytest.mark.parametrize("value,args,hexbytes", [  # ST 0601.8 section 8, "Example Value" / "Example LS Packet"
    (159.9744, ("u", 0, 360, 2), "71C2"),  # Tag 5 Platform Heading
    (-0.4315251, ("s", 20, 2), "FD3D"),  # Tag 6 Platform Pitch
    (3.405814, ("s", 50, 2), "08B8"),  # Tag 7 Platform Roll
    (60.1768229669783, ("s", 90, 4), "5595B66D"),  # Tag 13 Sensor Latitude
    (128.426759042045, ("s", 180, 4), "5B5360C4"),  # Tag 14 Sensor Longitude
    (14190.72, ("u", -900, 19000, 2), "C221"),  # Tag 15 Sensor True Altitude
    (144.5713, ("u", 0, 180, 2), "CD9C"),  # Tag 16 HFOV
    (152.6436, ("u", 0, 180, 2), "D917"),  # Tag 17 VFOV
    (160.719211474396, ("u", 0, 360, 4), "724A0A20"),  # Tag 18 Sensor Relative Azimuth
    (-168.792324833941, ("s", 180, 4), "87F84B86"),  # Tag 19 Sensor Relative Elevation
    (176.865437690572, ("u", 0, 360, 4), "7DC55ECE"),  # Tag 20 Sensor Relative Roll
    (68590.9833, ("u", 0, 5_000_000, 4), "03830926"),  # Tag 21 Slant Range (printed rounded: 68590.98)
    (-10.5423886331461, ("s", 90, 4), "F101A229"),  # Tag 23 Frame Center Latitude
    (29.157890122923, ("s", 180, 4), "14BC082B"),  # Tag 24 Frame Center Longitude
    (3216.037, ("u", -900, 19000, 2), "34F3"),  # Tag 25 Frame Center Elevation
])
def test_values_encode_as_the_standard_examples(value, args, hexbytes):
    kind, *rest = args
    got = misb.unsigned(value, *rest) if kind == "u" else misb.signed(value, *rest)
    assert got == bytes.fromhex(hexbytes)


def test_timestamp_and_checksum_match_the_standard_examples():
    # Tag 2: Oct. 24, 2008 00:13:29.913 UTC -> 0x00 04 59 F4 A6 AA 4A A8
    assert struct.pack(">Q", 1224807209913000) == bytes.fromhex("000459F4A6AA4AA8")
    assert misb.checksum(bytes.fromhex("060E2B34020081BB")) == 0xB4FD  # 8.1.2 Sample Checksum Data


def tel(**over):
    t = {"frame": 7, "t_capture": 1764100000.123456, "drone": "uav1", "mission": "arma-live1", "map": "altis",
         "camera": {"pos_asl": [1000.0, 2000.0, 300.0], "x_axis": [1, 0, 0], "y_axis": [0, 0.6, 0.8],
                    "z_axis": [0, 0.8, -0.6], "hfov_deg": 20.0, "vfov_deg": 11.25},
         "range_m": 500.0, "image": {"w": 1920, "h": 1080}}
    return t | over


def parse(buf: bytes) -> list[tuple[int, bytes]]:
    """The items of one UAS Datalink LS, after checking the key, the length and the checksum."""
    assert buf[:16] == misb.UAS_LS_KEY
    n, i = read_ber(buf, 16)
    assert i + n == len(buf)
    assert misb.checksum(buf[:-2]) == int.from_bytes(buf[-2:], "big")
    items = []
    while i < len(buf):
        tag = buf[i]
        n, j = read_ber(buf, i + 1)
        items.append((tag, buf[j:j + n]))
        i = j + n
    return items


def test_a_packet_has_the_mandatory_items_in_place():
    items = parse(misb.encode_0601(tel()))
    tags = [t for t, _ in items]
    assert tags[0] == 2 and tags[-1] == 1 and dict(items)[65] == bytes([8])  # ST 0601.8-09, -11, -12
    assert len(set(tags)) == len(tags)
    assert set(tags) == {2, 3, 10, 5, 6, 7, 13, 14, 15, 16, 17, 18, 19, 20, 21, 23, 24, 25, 65, 1}
    v = dict(items)
    assert struct.unpack(">Q", v[2])[0] == 1764100000123456
    assert v[3] == b"arma-live1" and v[10] == b"uav1"
    hlat, _ = geo.to_latlon("altis", 1000.0, 2000.0 + 400.0)  # the laser point: 500 m along (0, 0.8, -0.6)
    assert v[23] == misb.signed(hlat, 90, 4) and v[25] == misb.unsigned(0.0, -900, 19000, 2)


def test_without_a_range_there_is_no_slant_range_nor_frame_center():
    tags = {t for t, _ in parse(misb.encode_0601(tel(range_m=None)))}
    assert not tags & {21, 23, 24, 25}


def test_a_map_without_an_anchor_gets_no_packet():
    assert misb.encode_0601(tel(map="tanoa")) is None


def cam(f, u):
    return {"z_axis": f, "y_axis": u}


@pytest.mark.parametrize("c,expected", [
    (cam([0, 1, 0], [0, 0, 1]), (0, 0, 0)),  # north, level
    (cam([math.cos(math.radians(30)), 0, -0.5], [0.5, 0, math.cos(math.radians(30))]), (90, -30, 0)),  # east, 30 down
    (cam([0, 0, -1], [1, 0, 0]), (90, -90, 0)),  # straight down, top of the image east
    (cam([0, 1, 0], [math.sin(math.radians(10)), 0, math.cos(math.radians(10))]), (0, 0, 10)),  # rolled clockwise
])
def test_camera_angles_follow_the_standard(c, expected):
    got = misb.camera_angles(c)
    assert got == pytest.approx(expected, abs=1e-6)


def test_the_server_still_reads_its_record_from_the_shared_packet():
    t = tel()
    assert klv.decode_tel(klv.encode_tel(t) + misb.encode_0601(t)) == t
