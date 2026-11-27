import json
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from blade_common.geo import to_latlon
from blade_world import main as wm
from blade_world.export import geojson, kml, points

SNAP = {"wid": "w-1", "t": 1795724192.0, "map": "altis",
        "objects": [{"oid": "o-1", "grp": "soldier", "pose": "standing", "xy": [1000.0, 2000.0], "z": 52.3,
                     "sigma_m": 2.04, "n_obs": 9, "seen_by": ["uav1"], "first": 1795724173.0,
                     "last": 1795724190.0, "state": "active"},
                    {"oid": "o-2", "grp": "tank", "xy": [1500.0, 2500.0], "vel": [3.0, 0.0], "n_obs": 20,
                     "pred": [{"dt_s": 30, "xy": [1590.0, 2500.0], "sigma_m": 5.0}], "seen_by": ["uav1", "uav2"],
                     "first": 1795724180.0, "last": 1795724191.0, "state": "stale"}],
        "events": [{"eid": "e-1", "kind": "explosion", "xy": [1200.0, 2100.0], "t": 1795724185.0, "ttl_s": 5,
                    "sid": "uav2"}]}


def test_geojson_has_one_point_per_object_and_event_longitude_first():
    doc = geojson(points("altis", SNAP["objects"], SNAP["events"]))
    assert doc["type"] == "FeatureCollection" and len(doc["features"]) == 3
    soldier, tank, blast = doc["features"]
    lat, lon = to_latlon("altis", 1000.0, 2000.0)
    assert soldier["geometry"] == {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]}  # RFC 7946: lon, lat
    assert soldier["properties"] == {"name": "soldier o-1", "type": "object", "id": "o-1", "class": "soldier",
                                    "pose": "standing", "state": "active", "altitude_m": 52.3, "sigma_m": 2.0,
                                    "first_seen": "2026-11-26T20:16:13Z", "last_seen": "2026-11-26T20:16:30Z",
                                    "seen_by": "uav1"}
    assert tank["properties"]["speed_mps"] == 3.0 and tank["properties"]["heading_deg"] == 90.0  # moving east
    assert blast["properties"]["type"] == "event" and blast["properties"]["time"] == "2026-11-26T20:16:25Z"


def test_kml_has_a_placemark_per_point_with_its_data():
    root = ET.fromstring(kml(points("altis", SNAP["objects"], SNAP["events"]), "situation"))
    ns = {"k": "http://www.opengis.net/kml/2.2"}
    marks = root.findall("k:Document/k:Placemark", ns)
    assert [m.findtext("k:name", namespaces=ns) for m in marks] == ["soldier o-1", "tank o-2", "explosion e-1"]
    lat, lon = to_latlon("altis", 1000.0, 2000.0)
    assert marks[0].findtext("k:Point/k:coordinates", namespaces=ns) == f"{round(lon, 7)},{round(lat, 7)}"
    data = {d.get("name"): d.findtext("k:value", namespaces=ns) for d in marks[0].findall("k:ExtendedData/k:Data", ns)}
    assert data["state"] == "active" and data["class"] == "soldier"


def test_export_endpoint_serves_both_formats_as_files(monkeypatch):
    snap = dict(SNAP)

    async def snapshot(wid):
        return snap

    monkeypatch.setattr(wm, "worlds", {"w-1": object()})
    monkeypatch.setattr(wm, "snapshot", snapshot)
    api = TestClient(wm.app)  # no lifespan: no NATS
    r = api.get("/worlds/w-1/export")
    assert r.headers["content-type"] == "application/geo+json"
    assert r.headers["content-disposition"].startswith('attachment; filename="situation_altis_')
    assert r.headers["content-disposition"].endswith('.geojson"') and len(json.loads(r.content)["features"]) == 3
    r = api.get("/worlds/w-1/export?format=kml")
    assert r.headers["content-type"] == "application/vnd.google-earth.kml+xml" and r.content.startswith(b"<?xml")
    assert api.get("/worlds/w-1/export?format=csv").status_code == 422
    assert api.get("/worlds/w-2/export").status_code == 404
    snap["map"] = "tanoa"
    assert api.get("/worlds/w-1/export").status_code == 404  # no anchor for that map
