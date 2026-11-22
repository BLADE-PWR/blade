"""Which world and stream id each drone gets. People pick the names (uplink --mission, --drone); ingest picks the
ids (wid, sid), so two missions that both have a drone called uav1, or a replay next to the live mission it was
recorded in, never mix.

A live world is one (mission, map) pair: drones on different maps can never share a picture. The first drone of a
pair starts the world, later ones join it. A drone keeps its id when its uplink reconnects. A name another connected
uplink already flies under gets a suffix: uav1-2, uav1-3, ...
A replay brings its own ids (sessions picks them); it is only registered here, so its names are known too."""
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
    drones: dict[str, str] = field(default_factory=dict)  # sid -> name shown
    asked: dict[str, str] = field(default_factory=dict)  # sid -> name its uplink asked for (before any suffix)
    owner: dict[str, str] = field(default_factory=dict)  # sid -> source key of the uplink sending as it, if any

    def info(self) -> dict:
        return {"wid": self.wid, "mission": self.mission, "map": self.map, "kind": self.kind,
                "started": self.started, "drones": dict(self.drones)}


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
                    changed = True
                    break
                owner = w.owner.get(sid)
                if owner in (None, key) or not self.alive(owner):  # free, its own, or its uplink came back
                    break
                n += 1  # another connected uplink flies under this name
                name = f"{drone}-{n}"
            w.owner[sid] = key
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
