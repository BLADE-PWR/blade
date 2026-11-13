"""Message contract v1: one envelope for every bus message, one payload model per subject
(frames.<sid>, detections.<sid>, geo.<sid>, world.<wid>.objects, alerts.<wid>, ...).
`python -m blade_common.contracts` writes contracts/*.schema.json."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = 1

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]
Box = tuple[float, float, float, float]  # x1, y1, x2, y2 in tel.image pixels


class _Open(BaseModel):
    """Records that come from the game: keep fields we do not model."""
    model_config = ConfigDict(extra="allow")


class Envelope(BaseModel):
    v: int = CONTRACT_VERSION
    sid: str | None = None
    frame: int | None = None
    wid: str | None = None
    delivery: str | None = Field(None, description="'all': a replay that waits for every frame (ack.<sid>)")
    ts: dict[str, float | list[float]] = Field(
        default_factory=dict,
        description="capture: epoch s; every stage appends [t_in, t_out] under its own name, on its own clock; "
                    "media (replays only): the frame's tel.t_capture in the recording, epoch s; the world model "
                    "runs a replayed stream on this clock")
    data: dict[str, Any] = Field(default_factory=dict)


class Camera(_Open):
    pos_asl: Vec3
    x_axis: Vec3 = Field(description="right, unit vector")
    y_axis: Vec3 = Field(description="up, unit vector")
    z_axis: Vec3 = Field(description="forward (optical axis), unit vector")
    hfov_deg: float
    vfov_deg: float


class ImageSize(_Open):
    w: int
    h: int


class Tel(_Open):
    """Arma's tel record, as the mission emits it, plus what the uplink adds (sid, t_capture)."""
    frame: int = Field(description="full diag_frameNo")
    t_game: float | None = None
    t_tick: float | None = None
    sid: str | None = None
    t_capture: float | None = Field(None, description="epoch s when the uplink grabbed the frame")
    camera: Camera
    range_m: float | None = None
    image: ImageSize
    scene: str | None = None
    seed: int | None = None
    region: int | None = None


class SpawnRecord(_Open):
    id: int
    cls: str
    classname: str | None = None
    frame: int | None = None
    pos_asl: Vec3 | None = None
    dir_deg: float | None = None
    bbox_min: Vec3 | None = None
    bbox_max: Vec3 | None = None


class EventRecord(_Open):
    frame: int | None = None
    t_game: float | None = None
    kind: str
    data: Any = None


class JpegInfo(BaseModel):
    w: int
    h: int


class FrameData(BaseModel):
    tel: Tel
    jpeg: JpegInfo


class TruthData(BaseModel):
    frame: int
    t_game: float | None = None
    objs: list[list[Any]] = Field(description="[id, pos_asl, dir_deg, velocity, stance] per object")


class Det(BaseModel):
    tid: int | None = Field(None, description="track id, per stream; None for an untracked box")
    cls: str
    grp: str
    conf: float
    box: Box


class DetectionsData(BaseModel):
    model: str = Field(description="name@version")
    img: tuple[int, int] = Field(description="pixel space of the boxes = tel.image")
    dets: list[Det]
    tel: Tel = Field(description="passed through so georef needs no second subscription")


class GeoPoint(BaseModel):
    tid: int | None = None
    cls: str
    grp: str
    conf: float
    xyz: Vec3 | None = Field(None, description="ground point in Arma map metres (ASL); None if the ray missed")
    sigma_m: float | None = None


class GeoData(BaseModel):
    camera: Vec3
    footprint: list[Vec2 | None] = Field(description="ground under the 4 image corners: TL, TR, BR, BL")
    centre: Vec3 | None = None
    range_check_m: float | None = Field(None, description="|axis hit distance - laser range|")
    gsd_m: float | None = Field(None, description="ground metres per pixel at the image centre")
    pts: list[GeoPoint]


class Prediction(BaseModel):
    dt_s: float = Field(description="horizon after `last`, s")
    xy: Vec2
    sigma_m: float = Field(description="1-sigma radius of the predicted position, m")


class WorldObject(BaseModel):
    oid: str
    grp: str
    pose: str | None = None
    xy: Vec2
    z: float | None = None
    vel: Vec2 | None = Field(None, description="m/s, map east / north")
    vel_sigma: float | None = Field(None, description="1-sigma of the velocity, m/s")
    pred: list[Prediction] | None = Field(None, description="straight-line prediction; only for moving objects")
    sigma_m: float | None = None
    n_obs: int
    seen_by: list[str]
    tracks: list[tuple[str, int]] = Field(default_factory=list, description="(sid, tid) pairs behind this object")
    first: float = Field(description="world clock, s: the wall clock live, the recording's time in a replay")
    last: float = Field(description="world clock, s")
    state: str = Field(description="active | stale (not seen for a few seconds; remembered until expired)")


class WorldObjectsData(BaseModel):
    objects: list[WorldObject] = Field(default_factory=list, description="upserts")
    expired: list[str] = Field(default_factory=list, description="oids removed from the world")
    forgotten: list[str] = Field(default_factory=list,
                                 description="oids removed without losing contact: merged into another object, or "
                                             "dropped because their replay went back in time")
    counts: dict[str, float] | None = Field(None, description="active objects per group, median over ~0.5 s")
    t: float | None = Field(None, description="world clock when published, s: stands still while a replay is paused")


class WorldEvent(BaseModel):
    eid: str
    kind: str
    xy: Vec2
    z: float | None = None
    t: float
    ttl_s: float
    sid: str


class WorldEventsData(BaseModel):
    events: list[WorldEvent]


class Alert(BaseModel):
    """An attention cue for the operator. It never recommends an action or a target."""
    aid: str
    rule: str
    severity: str = Field(description="info | notice | warning")
    title: str
    explanation: str
    objects: list[str] = Field(default_factory=list)
    confidence: float | None = None
    xy: Vec2 | None = None
    t: float
    state: str = Field("active", description="active | cleared")


class AlertsData(BaseModel):
    alerts: list[Alert]


class TruthMatch(BaseModel):
    truth_id: int
    grp: str
    xy: Vec2
    oid: str | None = None
    err_m: float | None = None


class GroupEval(BaseModel):
    n: int
    cep50_m: float | None = None
    cep90_m: float | None = None
    detected: int = 0
    in_view: int = 0


class EvalData(BaseModel):
    window_s: float
    groups: dict[str, GroupEval]
    truth: list[TruthMatch] = Field(description="true positions of objects in view, with their world match")
    false_objects: list[str] = Field(default_factory=list, description="active world objects with no truth nearby")


class AckData(BaseModel):
    frame: int


class StreamDiag(BaseModel):
    fps_in: float = 0.0
    fps_out: float = 0.0
    clock_ms: float | None = Field(None, description="min(arrival - capture) over 1 s: network delay + clock offset")
    queue: int = 0
    dropped: int = 0
    latency_ms: float | None = None


class DiagData(BaseModel):
    service: str
    instance: str | None = None
    streams: dict[str, StreamDiag] = Field(default_factory=dict)


SUBJECTS: dict[str, type[BaseModel]] = {
    "envelope": Envelope,
    "tel": Tel,
    "frames": FrameData,
    "truth": TruthData,
    "registry": SpawnRecord,
    "events": EventRecord,
    "detections": DetectionsData,
    "geo": GeoData,
    "world.objects": WorldObjectsData,
    "world.events": WorldEventsData,
    "alerts": AlertsData,
    "eval": EvalData,
    "ack": AckData,
    "diag": DiagData,
}


def schemas() -> dict[str, dict]:
    return {name: model.model_json_schema() for name, model in SUBJECTS.items()}


def write_schemas(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    for name, schema in schemas().items():
        (out / f"{name}.schema.json").write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    return [out / f"{name}.schema.json" for name in SUBJECTS]


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[3] / "contracts"
    for p in write_schemas(target):
        print(p)
