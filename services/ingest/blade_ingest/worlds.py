"""Which world and stream id each drone gets. People pick the names (uplink --mission, --drone); ingest picks the
ids (wid, sid), so two missions that both have a drone called uav1, or a replay next to the live mission it was
recorded in, never mix.

A live world is one (mission, map) pair: drones on different maps can never share a picture. The first drone of a
pair starts the world, later ones join it. A drone keeps its id when its uplink reconnects. A name another connected
uplink already flies under gets a suffix: uav1-2, uav1-3, ...
A live world ends when none of its drones has sent a frame for EXPIRE_S (by then all its objects have expired), or
when the operator stops the mission; the next drone of that mission and map starts a new one, with new ids.
A replay brings its own ids (sessions picks them); it is only registered here, so its names are known too, and it
ends a few seconds after its last push closed (the session stopped)."""
from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


@dataclass
class World:
    wid: str
    mission: str
    map: str
    kind: str  # live | replay
    started: float = field(default_factory=time.time)
    last: float = field(default_factory=time.time)  # wall time of its last frame
    ended: float | None = None
    drones: dict[str, str] = field(default_factory=dict)  # sid -> name shown
    asked: dict[str, str] = field(default_factory=dict)  # sid -> name its uplink asked for (before any suffix)
    owner: dict[str, str] = field(default_factory=dict)  # sid -> source key of the uplink sending as it, if any
    previous: dict[str, str] = field(default_factory=dict)  # sid -> the sid the drone had before (settings go along)

    def info(self) -> dict:
        return {"wid": self.wid, "mission": self.mission, "map": self.map, "kind": self.kind,
                "started": self.started, "ended": self.ended, "drones": dict(self.drones),
                "previous": dict(self.previous)}


@dataclass
class Joined:
    world: World
    sid: str
    name: str  # the drone's name shown: the one asked for, or with a suffix
    changed: bool  # a new world or a new drone in it: worth announcing


class Worlds:
    def __init__(self, alive: Callable[[str], bool]):
        self.alive = alive  # source key -> that uplink is still connected
        self.lock = threading.Lock()  # demux threads of several sources assign at once
        self.live: dict[tuple[str, str], World] = {}  # (mission, map) -> its world
        self.by_wid: dict[str, World] = {}
        self.last_sid: dict[tuple[str, str], str] = {}  # (mission, drone name) -> its latest sid, in any world

    def join_live(self, mission: str, map_name: str, drone: str, key: str) -> Joined:
        with self.lock:
            w = self.live.get((mission, map_name))
            changed = w is None
            if w is None:
                w = World(new_id("w"), mission, map_name, "live")
                self.live[(mission, map_name)] = self.by_wid[w.wid] = w
            name, n = drone, 1
            while True:
                sid = next((s for s, x in w.drones.items() if x == name), None)
                if sid is None:  # a new drone
                    sid = new_id("d")
                    w.drones[sid], w.asked[sid] = name, drone
                    if (prev := self.last_sid.get((mission, name))) is not None:
                        w.previous[sid] = prev
                    self.last_sid[(mission, name)] = sid
                    changed = True
                    break
                owner = w.owner.get(sid)
                if owner in (None, key) or not self.alive(owner):  # free, its own, or its uplink came back
                    break
                n += 1  # another connected uplink flies under this name
                name = f"{drone}-{n}"
            w.owner[sid] = key
            w.last = time.time()
            return Joined(w, sid, name, changed)

    def leave(self, wid: str, sid: str) -> None:
        """The uplink sending as sid went to another world (another map): another uplink may take the name."""
        with self.lock:
            w = self.by_wid.get(wid)
            if w is not None:
                w.owner.pop(sid, None)

    def join_replay(self, wid: str, sid: str, mission: str, map_name: str, drone: str) -> Joined:
        with self.lock:
            w = self.by_wid.get(wid)
            changed = w is None
            if w is None:
                w = self.by_wid[wid] = World(wid, mission, map_name, "replay")
            if sid not in w.drones:
                w.drones[sid] = drone
                changed = True
            return Joined(w, sid, w.drones[sid], changed)

    def seen(self, wid: str, t: float) -> None:
        """A frame of the world arrived."""
        w = self.by_wid.get(wid)
        if w is not None:
            w.last = t

    def idle(self, now: float, live_s: float, pushed: set[str], replay_s: float) -> list[str]:
        """Worlds to end: live ones none of whose drones has sent a frame for live_s, replay ones with no push open
        (pushed: the replay worlds that have one) for replay_s."""
        with self.lock:
            for wid in pushed:
                if wid in self.by_wid:
                    self.by_wid[wid].last = now
            return [w.wid for w in self.by_wid.values() if now - w.last > (live_s if w.kind == "live" else replay_s)]

    def end(self, wid: str) -> World | None:
        """-> the world, ended and forgotten (None if it had ended already)."""
        with self.lock:
            w = self.by_wid.pop(wid, None)
            if w is None:
                return None
            if self.live.get((w.mission, w.map)) is w:
                del self.live[(w.mission, w.map)]
            w.ended = time.time()
            return w

    def missions(self) -> list[dict]:
        """The live missions: each with its worlds (one per map) and their drones, for picking one to watch."""
        with self.lock:
            out: dict[str, list[dict]] = {}
            for w in self.live.values():
                out.setdefault(w.mission, []).append(w.info())
        return [{"mission": m, "worlds": ws} for m, ws in sorted(out.items())]

    def of_mission(self, mission: str) -> list[dict]:
        """The live worlds of a mission (one per map), for a viewer that starts following it."""
        with self.lock:
            return [w.info() for w in self.live.values() if w.mission == mission]

    def truth_ids(self, mission: str, drone: str, host: str) -> tuple[str, str] | None:
        """-> (wid, sid) of the live drone an uplink's truth belongs to, or None before its first frame.
        Truth comes from its own socket, so it is found by names: of two uplinks under one name, the one on the
        truth's host; of one drone in two worlds (it changed maps), the one still connected."""
        with self.lock:
            hits = [(w.wid, sid, w.owner.get(sid, "")) for w in self.live.values() if w.mission == mission
                    for sid, asked in w.asked.items() if asked == drone]
        if not hits:
            return None
        wid, sid, _ = max(hits, key=lambda h: (bool(h[2]) and self.alive(h[2]), _host(h[2]) == host))
        return wid, sid


def _host(key: str) -> str:
    """"udp:10.0.0.5:51234" -> "10.0.0.5" """
    parts = key.split(":")
    return ":".join(parts[1:-1]) if len(parts) > 2 else ""
