"""Saves every live world as one mission recording: each drone's raw.<sid> bytes as they arrived, plus the sim truth
published for it. A drone's bytes go into a new segment whenever they come from a new connection (a new MPEG-TS
stream, whose timestamps start over). The recording is finalized when ingest ends the world (worlds.<wid> with
ended: no frame for EXPIRE_S, or Stop mission), so a drone that goes quiet and comes back stays in it."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from blade_common.bus import decode, kv_read

from .recording import finalize, finalize_mission

log = logging.getLogger("sessions.recorder")


@dataclass
class Segment:
    """One connection of one drone, in the single-stream format (recording.py)."""
    dir: Path
    src: str  # the ingest source key its bytes came from: another one is another MPEG-TS stream
    ts: object
    index: object
    truth: object
    bytes: int = 0
    truth_n: int = 0

    def close(self) -> None:
        for f in (self.ts, self.index, self.truth):
            f.close()

    def flush(self) -> None:
        for f in (self.ts, self.index, self.truth):
            f.flush()


@dataclass
class Mission:
    """One live world being recorded."""
    id: str
    wid: str
    dir: Path
    mission: str
    map: str
    started: float
    last: float = field(default_factory=time.time)  # wall time of its last chunk
    current: dict[str, Segment] = field(default_factory=dict)  # sid -> the segment its bytes go to now
    names: dict[str, str] = field(default_factory=dict)  # sid -> drone name
    bytes: int = 0


def _names(*names: str | None) -> str:
    """"arma-live1", "uav 1" -> "arma-live1_uav_1": a folder name from the names people typed"""
    return "_".join(re.sub(r"[^\w.-]+", "_", n) for n in names if n)


class Recorder:
    def __init__(self, root: Path, record_all: bool, idle_s: float = 660.0):
        self.root = root
        self.record_all = record_all
        self.idle_s = idle_s  # a world not heard of this long is finalized anyway (ingest restarted without ending it)
        self.missions: dict[str, Mission] = {}  # wid -> its recording
        self.wanted: dict[str, bool] = {}  # per-sid override of record_all
        self.kv = None
        self.js = None
        self.closing = False

    def recover(self) -> list[str]:
        """Finalize recordings a crash left open; remove the ones too small to keep."""
        done = []
        for d in sorted(self.root.iterdir() if self.root.exists() else []):
            m = d / "manifest.json"
            if not d.is_dir() or d.is_symlink() or not m.exists():
                continue
            try:
                meta = json.loads(m.read_text())
            except ValueError:
                continue
            if not meta.get("recording"):
                continue
            done.append(d.name)
            if not (d / "stream.ts").exists():  # a mission recording
                finalize_mission(d, {"recovered": True})
                log.info("%s: unfinished mission recording: finalized", d.name)
                continue
            size = (d / "stream.ts").stat().st_size  # a single-stream recording from before mission recordings
            if size < 1 << 20:
                log.info("%s: unfinished, %d bytes: removed", d.name, size)
                shutil.rmtree(d, ignore_errors=True)
                continue
            try:
                finalize(d, {"recording": False, "recovered": True})
                log.info("%s: unfinished, %.1f MB: finalized", d.name, size / 1e6)
            except Exception as e:
                m.write_text(json.dumps({**meta, "recording": False, "broken": str(e), "bytes": size}, indent=2))
                log.warning("%s: unfinished and unreadable (%s): marked broken", d.name, e)
        return done

    async def close(self) -> None:
        """Stop for good: bytes ingest flushes at shutdown must not open new recordings."""
        self.closing = True
        for wid in list(self.missions):
            await self.finish(wid)

    def should_record(self, sid: str) -> bool:
        return self.wanted.get(sid, self.record_all)

    def _open_mission(self, wid: str, mission: str, map_name: str, t: float) -> Mission:
        stamp = datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d_%H%M%S")
        rid = _names(stamp, mission, map_name)
        if (self.root / rid).exists():
            rid = _names(rid, wid)
        d = self.root / rid
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"v": 1, "id": rid, "wid": wid, "mission": mission,
                                                     "map": map_name, "source": "live", "started": t,
                                                     "recording": True}, indent=2))
        m = self.missions[wid] = Mission(rid, wid, d, mission, map_name, t)
        log.info("recording world %s (mission %s, map %s) -> %s", wid, mission, map_name, d)
        return m

    async def _open_segment(self, m: Mission, sid: str, name: str, src: str, t: float) -> Segment:
        drone_dir = m.dir / _names(name)
        n = len(list(drone_dir.glob("[0-9][0-9][0-9]"))) if drone_dir.exists() else 0
        d = drone_dir / f"{n:03d}"
        d.mkdir(parents=True)
        (d / "manifest.json").write_text(json.dumps({"v": 1, "id": d.name, "drone": name, "sid": name,
                                                     "source": "live", "started": t, "recording": True}))
        seg = Segment(d, src, open(d / "stream.ts", "ab"), open(d / "index.jsonl", "a"), open(d / "truth.jsonl", "a"))
        # spawn records came long before this stream: copy them from the registry
        if self.kv is None and self.js is not None:
            with contextlib.suppress(Exception):
                self.kv = await self.js.key_value("registry")
        spawns = await kv_read(self.kv, f"{sid}.") if self.kv is not None else {}
        for rec in spawns.values():
            self._truth(seg, "spawn", t, rec)
        m.current[sid], m.names[sid] = seg, name
        log.info("recording drone %s (%s) of %s -> %s (%d registry records)", name, sid, m.id, d.relative_to(m.dir),
                 len(spawns))
        return seg

    def _truth(self, seg: Segment, kind: str, t: float, rec: dict) -> None:
        seg.truth.write(json.dumps({"kind": kind, "t": t, "rec": rec}, separators=(",", ":")) + "\n")
        seg.truth_n += 1

    async def on_raw(self, msg) -> None:
        sid = msg.subject.split(".", 1)[1]
        h = msg.headers or {}
        wid = h.get("wid")
        if self.closing or not wid or not self.should_record(sid):
            return
        t = float(h.get("t", time.time()))
        m = self.missions.get(wid) or self._open_mission(wid, h.get("mission", ""), h.get("map", ""), t)
        seg = m.current.get(sid)
        src = h.get("src", "")
        if seg is None or seg.src != src:  # its first bytes, or a new connection: a new MPEG-TS stream
            if seg is not None:
                seg.close()
            seg = await self._open_segment(m, sid, h.get("drone") or sid, src, t)
        seg.index.write(json.dumps({"o": seg.bytes, "n": len(msg.data), "t": t}) + "\n")
        seg.ts.write(msg.data)
        seg.bytes += len(msg.data)
        m.bytes += len(msg.data)
        m.last = time.time()

    async def on_truth(self, msg) -> None:
        env, _ = decode(msg)
        m = self.missions.get(env.wid or "")
        seg = m.current.get(env.sid or "") if m else None
        if seg is None:
            return
        kind = {"truth": "gt", "registry": "spawn", "events": "evt"}[msg.subject.split(".", 1)[0]]
        self._truth(seg, kind, env.ts.get("truth_recv", time.time()), env.data)

    async def on_world_info(self, msg) -> None:
        env, _ = decode(msg)
        if env.data.get("ended"):
            await self.finish(env.data["wid"])

    async def stop(self, sid: str) -> None:
        """Stop recording one drone (recording turned off for it); its mission goes on."""
        for m in self.missions.values():
            seg = m.current.pop(sid, None)
            if seg is not None:
                seg.close()

    async def finish(self, wid: str) -> dict | None:
        m = self.missions.pop(wid, None)
        if m is None:
            return None
        for seg in m.current.values():
            seg.close()
        log.info("recording %s closed: %.1f MB", m.id, m.bytes / 1e6)
        try:
            return await asyncio.to_thread(finalize_mission, m.dir, {"wid": wid})
        except Exception as e:
            log.exception("%s: could not finalize", m.dir)
            mp = m.dir / "manifest.json"
            meta = json.loads(mp.read_text()) if mp.exists() else {"id": m.id}
            mp.write_text(json.dumps({**meta, "recording": False, "broken": str(e), "bytes": m.bytes}, indent=2))
            return None

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(1)
            for wid, m in list(self.missions.items()):
                if time.time() - m.last > self.idle_s:
                    log.warning("world %s: no bytes for %.0f s and no end from ingest: finalized", wid, self.idle_s)
                    await self.finish(wid)
                else:
                    for seg in m.current.values():
                        seg.flush()

    def status(self) -> list[dict]:
        return [{"id": m.id, "wid": m.wid, "mission": m.mission, "map": m.map, "drones": sorted(set(m.names.values())),
                 "bytes": m.bytes, "started": m.started, "last": m.last} for m in self.missions.values()]
