"""The situation picture as standard map files: GeoJSON (RFC 7946) and KML (OGC KML 2.2). One point per object and
event of the world, at its latitude / longitude (blade_common.geo)."""
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

from blade_common.geo import to_latlon


def points(map_name: str, objects: list[dict], events: list[dict]) -> list[dict]:
    """One {name, lat, lon, props} per object and event of a world snapshot."""
    out = []
    for o in objects:
        props = {"type": "object", "id": o["oid"], "class": o["grp"], "pose": o.get("pose"), "state": o["state"],
                 "altitude_m": _round(o.get("z")), "sigma_m": _round(o.get("sigma_m")),
                 "first_seen": _iso(o["first"]), "last_seen": _iso(o["last"]), "seen_by": ", ".join(o["seen_by"])}
        if o.get("pred") and o.get("vel"):  # moving: the world predicts only those (world README)
            vx, vy = o["vel"]
            props |= {"speed_mps": round(math.hypot(vx, vy), 2),
                      "heading_deg": round(math.degrees(math.atan2(vx, vy)) % 360, 1)}
        out.append(_point(map_name, f"{o['grp']} {o['oid']}", o["xy"], props))
    for e in events:
        props = {"type": "event", "id": e["eid"], "class": e["kind"], "altitude_m": _round(e.get("z")),
                 "time": _iso(e["t"]), "seen_by": e["sid"]}
        out.append(_point(map_name, f"{e['kind']} {e['eid']}", e["xy"], props))
    return out


def geojson(pts: list[dict]) -> dict:
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},  # lon first
         "properties": {"name": p["name"], **p["props"]}} for p in pts]}


def kml(pts: list[dict], title: str) -> bytes:
    root = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    doc = ET.SubElement(root, "Document")
    ET.SubElement(doc, "name").text = title
    for p in pts:
        mark = ET.SubElement(doc, "Placemark")
        ET.SubElement(mark, "name").text = p["name"]
        data = ET.SubElement(mark, "ExtendedData")
        for key, value in p["props"].items():
            ET.SubElement(ET.SubElement(data, "Data", name=key), "value").text = str(value)
        ET.SubElement(ET.SubElement(mark, "Point"), "coordinates").text = f"{p['lon']},{p['lat']}"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _point(map_name: str, name: str, xy, props: dict) -> dict:
    lat, lon = to_latlon(map_name, *xy)
    return {"name": name, "lat": round(lat, 7), "lon": round(lon, 7),
            "props": {k: v for k, v in props.items() if v not in (None, "")}}


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 1)
