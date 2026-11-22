"""Replay sessions: which recordings play, into which worlds, with which model. Live drones need no session:
they join the world of their uplink's --mission. Sessions live in memory only: when its last viewer leaves, a
session's replays are paused (the next viewer, maybe someone else, presses play); one nobody has watched for idle_s
is deleted with its replays (reap), so a restart starts with none."""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections import Counter

import httpx
from pydantic import BaseModel

from blade_common.bus import kv_read

log = logging.getLogger("gateway.sessions")


class ReplaySpec(BaseModel):
    """One mission recording, played into its own world: every drone of it under a stream id of its own."""
    recording: str
    wid: str  # new for every replay, so it never mixes with the live mission or another replay
    sids: dict[str, str]  # drone name in the recording -> stream id; new for every replay too
    speed: float = 1.0
    loop: bool = False
    offset_s: float = 0.0


class SessionSpec(BaseModel):
    name: str = "session"
    model: str | None = None
    replays: list[ReplaySpec] = []


class Session(SessionSpec):
    id: str
    state: str = "created"
    created: float
    started: float | None = None
    errors: list[str] = []

    @property
    def wids(self) -> list[str]:
        return sorted({r.wid for r in self.replays})

    @property
    def sids(self) -> list[str]:
        return [sid for r in self.replays for sid in r.sids.values()]


class Orchestrator:
    def __init__(self, urls: dict[str, str]):
        self.urls = urls
        self.nc = None
        self.sessions: dict[str, Session] = {}
        self.viewers: Counter[str] = Counter()  # session id -> open WebSockets
        self.idle_since: dict[str, float] = {}  # session id -> when its last viewer left (or it was created)

    def create(self, spec: SessionSpec) -> Session:
        s = Session(**spec.model_dump(), id=uuid.uuid4().hex[:8], created=time.time())
        self.sessions[s.id] = s
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
        for sid in s.sids:
            prev = assign.get(sid, {}).get("instance")
            if prev in load:
                load[prev] -= 1
            inst = min(load, key=lambda k: (load[k], k))
            load[inst] += 1
            entry = {"instance": inst, "model": s.model or assign.get(sid, {}).get("model"), "session": s.id}
            await kv.put(sid, json.dumps({k: v for k, v in entry.items() if v}).encode())

    async def start(self, s: Session) -> Session:
        s.errors = []
        if self.nc is not None:
            try:
                await self.assign(s)
            except Exception as e:
                s.errors.append(f"detector assignment: {e}")
        async with httpx.AsyncClient(timeout=10) as c:
            for r in s.replays:
                await self._call(c, s, "DELETE", "sessions", f"/replays/{r.wid}", ok404=True)
                await self._call(c, s, "POST", "sessions", "/replays", json={
                    "recording": r.recording, "wid": r.wid, "sids": r.sids, "speed": r.speed, "loop": r.loop,
                    "offset_s": r.offset_s})
        s.state, s.started = "running", time.time()
        return s

    async def stop(self, s: Session) -> Session:
        async with httpx.AsyncClient(timeout=10) as c:
            for wid in s.wids:
                await self._call(c, s, "DELETE", "sessions", f"/replays/{wid}", ok404=True)
        s.state = "stopped"
        return s

    async def delete(self, s: Session) -> None:
        await self.stop(s)
        del self.sessions[s.id]
        self.viewers.pop(s.id, None)
        self.idle_since.pop(s.id, None)

    async def unwatched(self, s: Session) -> None:
        """A viewer left: if it was the last one, pause the replays; the next viewer resumes them by hand."""
        self.viewers[s.id] -= 1
        if self.viewers[s.id] or s.id not in self.sessions:
            return
        async with httpx.AsyncClient(timeout=10) as c:
            for wid in s.wids:
                await self._call(c, s, "PATCH", "sessions", f"/replays/{wid}", json={"paused": True})

    async def reap(self, now: float, idle_s: float) -> None:
        """Delete the sessions nobody has watched for idle_s."""
        for s in list(self.sessions.values()):
            if self.viewers[s.id]:
                self.idle_since.pop(s.id, None)
            elif now - self.idle_since.setdefault(s.id, now) >= idle_s:
                log.info("session %s (%s): nobody watching for %.0f s, deleted", s.id, s.name, idle_s)
                await self.delete(s)
