"""Operator sessions: which streams, which model, which map and world, record or not."""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from blade_common.bus import kv_read

log = logging.getLogger("gateway.sessions")


class StreamSpec(BaseModel):
    sid: str
    source: str = Field("live", description="live | replay")
    wid: str
    record: bool = True
    recording: str | None = None
    speed: float = 1.0
    loop: bool = False
    offset_s: float = 0.0
    pacing: str = "realtime"
    delivery: str = "latest"


class SessionSpec(BaseModel):
    name: str = "session"
    model: str | None = None
    streams: list[StreamSpec] = []


class Session(SessionSpec):
    id: str
    state: str = "created"
    created: float
    started: float | None = None
    errors: list[str] = []

    @property
    def wids(self) -> list[str]:
        return sorted({s.wid for s in self.streams})

    @property
    def sids(self) -> list[str]:
        return [s.sid for s in self.streams]


class Orchestrator:
    def __init__(self, urls: dict[str, str], store: Path | None = None):
        self.urls = urls
        self.nc = None
        self.sessions: dict[str, Session] = {}
        self.store = store
        if store and store.exists():
            for d in json.loads(store.read_text()):
                s = Session.model_validate(d)
                s.state = "stopped" if s.state == "running" else s.state
                self.sessions[s.id] = s

    def save(self) -> None:
        if self.store:
            self.store.parent.mkdir(parents=True, exist_ok=True)
            self.store.write_text(json.dumps([s.model_dump() for s in self.sessions.values()], indent=1))

    def create(self, spec: SessionSpec) -> Session:
        s = Session(**spec.model_dump(), id=uuid.uuid4().hex[:8], created=time.time())
        self.sessions[s.id] = s
        self.save()
        return s

    async def _call(self, client: httpx.AsyncClient, s: Session, method: str, svc: str, path: str,
                    ok404: bool = False, **kw):
        try:
            r = await client.request(method, f"{self.urls[svc]}{path}", **kw)
            if r.status_code >= 400 and not (ok404 and r.status_code == 404):
                s.errors.append(f"{svc} {method} {path}: {r.status_code} {r.text[:200]}")
            return r
        except httpx.HTTPError as e:
            s.errors.append(f"{svc} {method} {path}: {e}")
            return None

    async def detectors(self) -> tuple[dict[str, dict], dict[str, dict]]:
        """Live detector replicas and the current stream assignments."""
        js = self.nc.jetstream()

        async def read(bucket: str) -> dict[str, dict]:
            try:
                return await kv_read(await js.key_value(bucket))
            except Exception:
                return {}

        live, assign = await read("detectors"), await read("assign")
        t = time.time()
        return {k: v for k, v in live.items() if t - v.get("t", 0) < 6}, assign

    async def assign(self, s: Session) -> None:
        """Each stream to the replica with the fewest assigned streams; the model goes with it."""
        live, assign = await self.detectors()
        if not live:
            s.errors.append("no detector replica has reported in; streams use the default model")
            return
        load = {k: 0 for k in live}
        for a in assign.values():
            if a.get("instance") in load:
                load[a["instance"]] += 1
        kv = await self.nc.jetstream().key_value("assign")
        for st in s.streams:
            prev = assign.get(st.sid, {}).get("instance")
            if prev in load:
                load[prev] -= 1
            inst = min(load, key=lambda k: (load[k], k))
            load[inst] += 1
            entry = {"instance": inst, "model": s.model or assign.get(st.sid, {}).get("model"), "session": s.id}
            await kv.put(st.sid, json.dumps({k: v for k, v in entry.items() if v}).encode())

    async def start(self, s: Session) -> Session:
        s.errors = []
        if self.nc is not None:
            try:
                await self.assign(s)
            except Exception as e:
                s.errors.append(f"detector assignment: {e}")
        async with httpx.AsyncClient(timeout=10) as c:
            for st in s.streams:
                if st.source == "live":
                    await self._call(c, s, "PUT", "sessions", f"/recording/{st.sid}", json={"on": st.record})
                elif st.source == "replay":
                    if not st.recording:
                        s.errors.append(f"{st.sid}: replay without a recording")
                        continue
                    await self._call(c, s, "DELETE", "sessions", f"/replays/{st.sid}", ok404=True)
                    await self._call(c, s, "POST", "sessions", "/replays", json={
                        "recording": st.recording, "sid": st.sid, "wid": st.wid, "speed": st.speed,
                        "loop": st.loop, "offset_s": st.offset_s, "pacing": st.pacing,
                        "delivery": st.delivery})
        s.state, s.started = "running", time.time()
        self.save()
        return s

    async def stop(self, s: Session) -> Session:
        async with httpx.AsyncClient(timeout=10) as c:
            for st in s.streams:
                if st.source == "replay":
                    await self._call(c, s, "DELETE", "sessions", f"/replays/{st.sid}", ok404=True)
        s.state = "stopped"
        self.save()
        return s
