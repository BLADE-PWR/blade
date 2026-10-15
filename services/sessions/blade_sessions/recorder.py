"""Saves every live stream as it arrived: raw.<sid> bytes, plus the sim truth published for that sid."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from blade_common.bus import decode, kv_read

from .recording import finalize

log = logging.getLogger("sessions.recorder")


@dataclass
class Active:
    id: str
    sid: str
    dir: Path
    ts: object
    index: object
    truth: object
    started: float = field(default_factory=time.time)
    last: float = field(default_factory=time.time)
    bytes: int = 0
    truth_n: int = 0


class Recorder:
    def __init__(self, root: Path, record_all: bool, idle_s: float = 5.0, map_id: str | None = None):
        self.root = root
        self.record_all = record_all
        self.idle_s = idle_s
        self.map_id = map_id
        self.active: dict[str, Active] = {}
        self.wanted: dict[str, bool] = {}  # per-sid override of record_all
        self.kv = None
        self.js = None

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
            ts = d / "stream.ts"
            size = ts.stat().st_size if ts.exists() else 0
            if size < 1 << 20:
                log.info("%s: unfinished, %d bytes: removed", d.name, size)
                shutil.rmtree(d, ignore_errors=True)
                done.append(d.name)
                continue
            try:
                finalize(d, {"recording": False, "recovered": True})
                log.info("%s: unfinished, %.1f MB: finalized", d.name, size / 1e6)
            except Exception as e:
                m.write_text(json.dumps({**meta, "recording": False, "broken": str(e), "bytes": size}, indent=2))
                log.warning("%s: unfinished and unreadable (%s): marked broken", d.name, e)
            done.append(d.name)
        return done

    async def close(self) -> None:
        for sid in list(self.active):
            await self.stop(sid)

    def should_record(self, sid: str) -> bool:
        return self.wanted.get(sid, self.record_all)

    async def _open(self, sid: str, t: float) -> Active:
        stamp = datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d_%H%M%S")
        rid = f"{stamp}_{sid}"
        d = self.root / rid
        d.mkdir(parents=True, exist_ok=True)
        a = Active(rid, sid, d, open(d / "stream.ts", "ab"), open(d / "index.jsonl", "a"),
                   open(d / "truth.jsonl", "a"))
        (d / "manifest.json").write_text(json.dumps({"v": 1, "id": rid, "sid": sid, "source": "live",
                                                     "map": self.map_id, "started": t, "recording": True}, indent=2))
        # spawn records came long before this stream: copy them from the registry
        if self.kv is None and self.js is not None:
            with contextlib.suppress(Exception):
                self.kv = await self.js.key_value("registry")
        spawns = await kv_read(self.kv, f"{sid}.") if self.kv is not None else {}
        for rec in spawns.values():
            self._truth(a, "spawn", t, rec)
        self.active[sid] = a
        log.info("recording %s -> %s (%d registry records)", sid, d, len(spawns))
        return a

    def _truth(self, a: Active, kind: str, t: float, rec: dict) -> None:
        a.truth.write(json.dumps({"kind": kind, "t": t, "rec": rec}, separators=(",", ":")) + "\n")
        a.truth_n += 1

    async def on_raw(self, msg) -> None:
        sid = msg.subject.split(".", 1)[1]
        if not self.should_record(sid):
            return
        t = float(msg.headers.get("t", time.time())) if msg.headers else time.time()
        a = self.active.get(sid) or await self._open(sid, t)
        a.index.write(json.dumps({"o": a.bytes, "n": len(msg.data), "t": t}) + "\n")
        a.ts.write(msg.data)
        a.bytes += len(msg.data)
        a.last = time.time()

    async def on_truth(self, msg) -> None:
        sid = msg.subject.split(".", 1)[1]
        a = self.active.get(sid)
        if a is None:
            return
        env, _ = decode(msg)
        kind = {"truth": "gt", "registry": "spawn", "events": "evt"}[msg.subject.split(".", 1)[0]]
        self._truth(a, kind, env.ts.get("truth_recv", time.time()), env.data)

    async def stop(self, sid: str) -> dict | None:
        a = self.active.pop(sid, None)
        if a is None:
            return None
        for f in (a.ts, a.index, a.truth):
            f.close()
        log.info("recording %s closed: %.1f MB, %d truth records", a.id, a.bytes / 1e6, a.truth_n)
        try:
            return await asyncio.to_thread(finalize, a.dir, {"source": "live", "sid": a.sid, "map": self.map_id,
                                                             "started": a.started, "ended": a.last,
                                                             "recording": False})
        except Exception as e:
            if a.bytes < 1 << 20:  # a scrap, e.g. the tail of a stream that just ended
                log.info("%s: %d bytes and no playable video, removed", a.id, a.bytes)
                shutil.rmtree(a.dir, ignore_errors=True)
                return None
            log.warning("%s: could not finalize (%s), kept and marked broken", a.dir, e)
            m = a.dir / "manifest.json"
            meta = json.loads(m.read_text()) if m.exists() else {"id": a.id}
            m.write_text(json.dumps({**meta, "recording": False, "broken": str(e), "bytes": a.bytes}, indent=2))
            return None

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(1)
            for sid, a in list(self.active.items()):
                if time.time() - a.last > self.idle_s:
                    await self.stop(sid)
                else:
                    for f in (a.ts, a.index, a.truth):
                        f.flush()

    def status(self) -> list[dict]:
        return [{"id": a.id, "sid": a.sid, "bytes": a.bytes, "truth": a.truth_n, "started": a.started,
                 "last": a.last} for a in self.active.values()]
