"""georef: detections.<sid> -> geo.<sid>. Stateless; rays through each box anchor onto the DEM (project.py) of the
map the drone is on (tel["map"]). Every map package in MAPS_DIR is loaded at startup, keyed by folder name."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from blade_common import ports
from blade_common.bus import close, connect, decode, forward, now, publish
from blade_common.contracts import GeoData
from blade_common.service import Diag, env_str, run_app
from blade_geometry import MapPackage

from .project import project_frame

log = logging.getLogger("georef")
MAPS_DIR = Path(env_str("MAPS_DIR", "maps"))

state: dict = {"nc": None}
packages: dict[str, MapPackage] = {}
missing: set[str] = set()  # maps a drone reported that have no package, logged once
diag = Diag("georef")


async def on_detections(msg) -> None:
    t_in = now()
    env, _ = decode(msg)
    data = env.data
    name = data["tel"]["map"]
    pkg = packages.get(name)
    if pkg is None:  # still published (no points), so the world sees the drone
        if name not in missing:
            missing.add(name)
            log.warning("%s is on map %s, which has no package in %s: its detections are not placed",
                        env.sid, name, MAPS_DIR)
        geo = {"camera": data["tel"]["camera"]["pos_asl"], "footprint": [], "pts": []}
    else:
        geo = await asyncio.to_thread(project_frame, pkg.terrain, data["tel"], data["dets"])
    out = forward(env, GeoData.model_validate({**geo, "map": name}).model_dump(exclude_none=True), "georef", t_in)
    await publish(state["nc"], f"geo.{env.sid}", out)
    diag.got(env.sid)
    diag.sent(env.sid, latency_s=now() - env.ts.get("capture", now()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    for meta in sorted(MAPS_DIR.glob("*/meta.json")):
        name = meta.parent.name
        packages[name] = await asyncio.to_thread(MapPackage.load, meta.parent)
        t = packages[name].terrain
        log.info("map %s: %d x %d nodes, %.1f m cells, heights %.1f .. %.1f m", name, t.nx, t.ny, t.d, t.hmin, t.hmax)
    state["nc"] = await connect("georef")
    await state["nc"].subscribe("detections.>", cb=on_detections)
    task = asyncio.create_task(diag.run(state["nc"]))
    yield
    task.cancel()
    await close(state["nc"])


app = FastAPI(title="blade georef", lifespan=lifespan)


class ProjectReq(BaseModel):
    tel: dict
    dets: list[dict] = []
    pixels: list[tuple[float, float]] = []


@app.post("/project")
async def project(req: ProjectReq):
    pkg = packages.get(req.tel["map"])
    if pkg is None:
        raise HTTPException(404, f"no map package {req.tel['map']}")
    dets = list(req.dets) + [{"cls": "point", "grp": "point", "conf": 1.0, "box": [u, v, u, v]}
                             for u, v in req.pixels]
    return project_frame(pkg.terrain, req.tel, dets)


@app.get("/health")
async def health():
    return {"ok": bool(packages), "maps": {name: pkg.terrain.extent for name, pkg in packages.items()}}


def main() -> None:
    run_app("blade_georef.main:app", ports.GEOREF)
