"""A replay plays a mission recording back into one world: one track per drone, each pushed into ingest
(POST /push/{sid}?wid=) with its truth sent to the truth port, all on one clock (clock.py). A drone that joined the
mission later starts later on the timeline, as it did live. From ingest onwards the drones look like live ones.
Control: pause, speed, seek, step N frames, loop."""
from __future__ import annotations

import asyncio
import bisect
import contextlib
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .clock import ReplayClock
from .recording import frame_ends, load_index, load_truth

log = logging.getLogger("sessions.replay")


@dataclass
class TrackSpec:
    name: str  # the drone's name in the recording
    dir: Path  # its stream.ts, index.jsonl, truth.jsonl
    start_s: float  # where it starts on the mission's timeline


class Track:
    """One drone: its recorded chunks, sent when the clock reaches their time on the mission's timeline."""

    def __init__(self, spec: TrackSpec, sid: str, send_truth: bool):
        self.name, self.dir, self.start_s, self.sid = spec.name, spec.dir, spec.start_s, sid
        self.index = load_index(self.dir)
        if not self.index:
            raise ValueError(f"{self.dir}: empty index")
        t0 = self.index[0]["t"]
        self.times = [e["t"] - t0 for e in self.index]  # s from the track's own start
        self.offsets = [e["o"] for e in self.index]
        self.size = self.index[-1]["o"] + self.index[-1]["n"]
        self.truth = sorted((dict(r, t=r["t"] - t0) for r in load_truth(self.dir)), key=lambda r: r["t"]) \
            if send_truth else []
        self.truth_times = [r["t"] for r in self.truth]
        self.end_s = self.start_s + self.times[-1]
        self.frames: list[int] | None = None  # frame_ends, loaded on the first step
        self.steps = 0  # the clock's steps this track has sent
        self.error: str | None = None

    def _chunk_at(self, byte: int) -> int:
        return max(bisect.bisect_right(self.offsets, byte) - 1, 0)

    def _time_at(self, byte: int) -> float:
        return self.times[self._chunk_at(byte)]

    def _byte_at(self, t: float) -> int:
        """The first chunk at or after mission time t (the end if the track is over by then)."""
        rel = t - self.start_s
        if rel > self.times[-1]:
            return self.size
        return self.offsets[bisect.bisect_left(self.times, rel)]

    async def body(self, clock: ReplayClock, send_truth):
        f = open(self.dir / "stream.ts", "rb")

        def read(a: int, b: int) -> bytes:
            f.seek(a)
            return f.read(b - a)

        j = 0

        def truth_until(rel: float) -> None:
            nonlocal j
            while j < len(self.truth) and self.truth[j]["t"] <= rel:
                if self.truth[j]["kind"] != "spawn":
                    send_truth(self.sid, self.truth[j])
                j += 1

        try:
            while True:  # once per jump (the start, a seek, a loop)
                gen = clock.generation
                byte = self._byte_at(clock.position())
                for r in self.truth:  # every spawn first, before any gt mentions it
                    if r["kind"] == "spawn":
                        send_truth(self.sid, r)
                j = bisect.bisect_left(self.truth_times, self._time_at(byte))
                self.steps = clock.steps
                while byte < self.size:
                    if self.steps != clock.steps:  # paused, and frames asked for: send this track's next ones
                        n, self.steps = clock.steps - self.steps, clock.steps
                        if self.start_s > clock.position():  # the drone has not joined the mission yet
                            continue
                        if self.frames is None:
                            self.frames = await asyncio.to_thread(frame_ends, self.dir / "stream.ts")
                        k = bisect.bisect_right(self.frames, byte)  # frames already complete
                        if self.frames:
                            target = self.frames[min(k + n - 1, len(self.frames) - 1)]
                            if target > byte:
                                truth_until(self._time_at(target))
                                yield read(byte, target)
                                byte = target
                        clock.stepped_to(self.start_s + self._time_at(byte))
                        continue
                    i = self._chunk_at(byte)
                    why = await clock.wait_until(self.start_s + self.times[i], gen, self.steps)
                    if why == "jumped":
                        break
                    if why == "step":
                        continue
                    truth_until(self.times[i])
                    end = self.offsets[i] + self.index[i]["n"]
                    yield read(byte, end)  # from byte: a step may have sent the start of this chunk
                    byte = end
                else:
                    await clock.jumped(gen)  # this track is over: wait for a seek or a loop
        finally:
            f.close()


@dataclass
class Replay:
    recording: str
    wid: str
    tracks: list[Track]
    ingest_url: str
    truth_addr: tuple[str, int]
    speed: float = 1.0
    loop: bool = False
    offset_s: float = 0.0
    state: str = "playing"  # or stopped, failed; playing covers paused (info tells them apart)
    loops: int = 0
    error: str | None = None
    _tasks: list[asyncio.Task] = field(default_factory=list)

    def __post_init__(self):
        self.duration_s = max(t.end_s for t in self.tracks)
        self.clock = ReplayClock(self.duration_s, self.speed, self.loop, self.offset_s)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._conduct(), name=f"replay-{self.wid}")]
        self._tasks += [asyncio.create_task(self._push(t), name=f"replay-{self.wid}-{t.sid}") for t in self.tracks]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.state = "stopped"

    def patch(self, paused: bool | None = None, speed: float | None = None, seek_s: float | None = None,
              step: int | None = None, loop: bool | None = None) -> None:
        if loop is not None:
            self.clock.set_loop(loop)
        if speed is not None and speed > 0:
            self.clock.set_speed(speed)
        if seek_s is not None:
            self.clock.seek(seek_s)
        if step:
            self.clock.step(int(step))
        elif paused is not None:
            self.clock.set_paused(paused)

    def info(self) -> dict:
        c = self.clock
        state = self.state if self.state in ("stopped", "failed") else "paused" if c.paused else "playing"
        return {"wid": self.wid, "recording": self.recording, "state": state,
                "position_s": round(c.position(), 3), "duration_s": round(self.duration_s, 2), "speed": c.speed,
                "paused": c.paused, "loop": c.loop, "loops": self.loops, "error": self.error,
                "drones": [{"name": t.name, "sid": t.sid, "start_s": round(t.start_s, 3), "error": t.error}
                           for t in self.tracks]}

    def _send_truth(self, sid: str, r: dict) -> None:
        msg = {"sid": sid, "wid": self.wid, "kind": r["kind"], "t": time.time(), "rec": r["rec"]}
        try:
            self.sock.sendto(json.dumps(msg, separators=(",", ":")).encode(), self.truth_addr)
        except OSError as e:
            log.debug("truth send: %s", e)

    async def _conduct(self) -> None:
        """The end of the timeline: go round with loop, else wait there paused like a player."""
        c = self.clock
        while True:
            gen = c.generation
            if await c.wait_until(self.duration_s, gen) != "reached":
                continue
            if c.loop:
                self.loops += 1
                c.seek(0.0)
            else:
                c.end()
                await c.jumped(gen)

    async def _push(self, track: Track) -> None:
        url = f"{self.ingest_url}/push/{track.sid}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5)) as client:
                r = await client.post(url, params={"wid": self.wid}, content=track.body(self.clock, self._send_truth))
                r.raise_for_status()
                log.info("replay %s, drone %s done: %s", self.wid, track.name, r.json())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            track.error = self.error = f"{track.name}: {e}"
            self.state = "failed"
            log.exception("replay %s, drone %s failed", self.wid, track.name)
