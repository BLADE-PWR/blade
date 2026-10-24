"""georef: detections.<sid> -> geo.<sid>. Stateless; rays through each box anchor onto the map's DEM (project.py)."""
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
MAP = env_str("MAP", "altis")

state: dict = {"pkg": None, "nc": None}
diag = Diag("georef")


async def on_detections(msg) -> None:
    t_in = now()
    env, _ = decode(msg)
    data = env.data
    geo = await asyncio.to_thread(project_frame, state["pkg"].terrain, data["tel"], data["dets"])
    out = forward(env, GeoData.model_validate(geo).model_dump(exclude_none=True), "georef", t_in)
    await publish(state["nc"], f"geo.{env.sid}", out)
    diag.got(env.sid)
    diag.sent(env.sid, latency_s=now() - env.ts.get("capture", now()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["pkg"] = await asyncio.to_thread(MapPackage.load, MAPS_DIR / MAP)
    t = state["pkg"].terrain
    log.info("map %s: %d x %d nodes, %.1f m cells, heights %.1f .. %.1f m", MAP, t.nx, t.ny, t.d, t.hmin, t.hmax)
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
    if state["pkg"] is None:
        raise HTTPException(503, "map not loaded")
    dets = list(req.dets) + [{"cls": "point", "grp": "point", "conf": 1.0, "box": [u, v, u, v]}
                             for u, v in req.pixels]
    return project_frame(state["pkg"].terrain, req.tel, dets)


@app.get("/health")
async def health():
    pkg = state["pkg"]
    return {"ok": pkg is not None, "map": MAP, "extent": pkg.terrain.extent if pkg else None}


def main() -> None:
    run_app("blade_georef.main:app", ports.GEOREF)
