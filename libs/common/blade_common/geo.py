"""Map positions as WGS84 latitude / longitude. A map position is metres east / north of the map's south-west
corner. The game maps are not true copies of the real islands (other size, slightly turned), so each map is fitted
to its island by two reference points: places seen both in the game and on a real map, far apart."""
from __future__ import annotations

import math

# Per map: two reference points ((x, y) in the game, m; (latitude, longitude) of the same place, deg)
ANCHORS = {
    "altis": (((2310.08,9201.43), (39.829543, 25.044519)), ((28530.7,25722.3), (40.034230, 25.449009))),
    "stratis": (((2603.88,315.867), (39.462082, 24.986061)), ((6774.7,5333.1), (39.530887, 25.056792))),
}

_A = 6378137.0  # WGS84 semi-major axis, m
_E2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)  # WGS84 first eccentricity squared


def _radii(lat: float) -> tuple[float, float]:
    """Metres per radian north-south and east-west at a latitude."""
    s = math.sin(math.radians(lat))
    w = 1 - _E2 * s * s
    return _A * (1 - _E2) / w ** 1.5, _A / math.sqrt(w) * math.cos(math.radians(lat))


def to_latlon(map_name: str, x: float, y: float) -> tuple[float, float]:
    """Game metres east / north of the south-west corner -> (lat, lon) deg."""
    ((x1, y1), (lat1, lon1)), ((x2, y2), (lat2, lon2)) = ANCHORS[map_name]
    north, east = _radii(lat1)
    # points as complex numbers (east + i*north): one multiplication by `turn` scales and turns game metres
    # into real metres around the first point
    turn = complex(math.radians(lon2 - lon1) * east, math.radians(lat2 - lat1) * north) / complex(x2 - x1, y2 - y1)
    real = turn * complex(x - x1, y - y1)
    return lat1 + math.degrees(real.imag / north), lon1 + math.degrees(real.real / east)
