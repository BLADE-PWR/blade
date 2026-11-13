"""World objects from per-stream tracks: a constant-velocity Kalman filter per object on the ground plane,
updated with R = sigma^2 from georef. Soldiers match soldiers; all vehicle classes match each other.

A new track (sid, tid) is matched, nearest pair first, to:
  1. stitch:     an object this stream lost at most GAP_S ago, inside the Kalman gate;
  2. merge:      an object another stream of this world sees now, inside the Kalman gate;
  3. reacquire:  an object unseen for over GAP_S, within REACQ_M (growing by VMAX/s if it moved);
  4. otherwise a new object.
An object this stream still watches is never a candidate. Vehicle objects closer than DUP_M merge.
An object is tentative until CONFIRM_N observations (not published; dropped if unseen for GAP_S).
The reported sigma is floored at FLOOR x the measurement sigma: one camera's errors are correlated.
A moving object gets straight-line predictions after HORIZONS_S: the filter's covariance without ACCEL (tuned
for 30 fps, it would give kilometres at 60 s) plus MANEUVER x the distance travelled."""
from __future__ import annotations

import itertools
import math
from collections import Counter, deque

import numpy as np

from blade_common.contracts import Prediction, WorldObject

GAP_S = 2.0  # stitching window
LIVE_S = 1.0  # "watching now" for cross-stream merges
CHI2 = 9.21  # 99 % gate in 2-D
MAX_GATE_M = {"soldier": 12.0, "tank": 25.0, "military_vehicle": 25.0}
ACCEL = {"soldier": 1.0, "tank": 3.0, "military_vehicle": 4.0}  # process noise, m/s^2
VEL0 = {"soldier": 2.0, "tank": 8.0, "military_vehicle": 10.0}  # initial speed sigma, m/s
FLOOR = 0.5
POSE = {"soldier_standing": "standing", "soldier_kneeling": "kneeling", "soldier_prone": "prone"}
EVENT_MERGE_M = 15.0

VEHICLES = {"tank", "military_vehicle"}
WATCH_S = {"soldier": 0.25}  # "still watching": vehicles only within the same frame
REACQ_M = {"soldier": 3.0, "vehicle": 8.0}  # re-acquisition radius for an object standing still
VMAX = {"soldier": 2.5, "vehicle": 12.0}  # m/s the radius grows for a moving one, up to REACQ_CAP_M
REACQ_CAP_M = {"soldier": 15.0, "vehicle": 50.0}
STILL_V = 1.0  # m/s: slower counts as standing still
DUP_M = 2.5  # vehicle centres this close are one vehicle
DUP_FRAMES = 3  # ... after this many frames if both are seen
CONFIRM_N = 5  # observations before an object is published
HORIZONS_S = (30.0, 60.0)
MANEUVER = 0.1  # 1-sigma spread of a prediction, share of the distance travelled
PRED_VMAX = {"soldier": 7.0, "vehicle": 30.0}  # m/s: faster is the filter catching up with a jump (seek, ID switch)


def assoc(grp: str) -> str:
    return "vehicle" if grp in VEHICLES else grp


def sd(P: np.ndarray, i: int) -> float:
    """-> the larger 1-sigma of the pair i, i+1 of a covariance: position (0) or velocity (2)"""
    return math.sqrt(max(P[i, i], P[i + 1, i + 1]))


class Obj:
    def __init__(self, oid: str, grp: str, xy, r: float, t: float):
        self.oid, self.grp, self.key = oid, grp, assoc(grp)
        self.reset(xy, r, t)
        self.r_last = r
        self.z = None
        self.first = self.last = t
        self.n = 0
        self.seen_by: dict[str, float] = {}  # sid -> last time
        self.tracks: dict[tuple[str, int], float] = {}  # (sid, tid) -> last time
        self.poses: deque[str] = deque(maxlen=15)
        self.grps: deque[str] = deque(maxlen=30)  # recent detector groups; the most common wins
        self.history: deque = deque(maxlen=600)
        self.state = "active"
        self.confirmed = False

    def predicted(self, t: float, accel: float | None = None) -> tuple[np.ndarray, np.ndarray]:
        dt = max(t - self.t, 0.0)
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        q = (ACCEL.get(self.grp, 2.0) if accel is None else accel) ** 2
        G = np.array([[dt * dt / 2, 0], [0, dt * dt / 2], [dt, 0], [0, dt]])
        return F @ self.x, F @ self.P @ F.T + q * (G @ G.T)

    def gate(self, xy, r: float, t: float) -> float | None:
        """Squared Mahalanobis distance of a measurement, or None outside the gate."""
        x, P = self.predicted(t)
        d = np.asarray(xy) - x[:2]
        if math.hypot(*d) > MAX_GATE_M.get(self.grp, 20.0):
            return None
        S = P[:2, :2] + np.eye(2) * r * r
        m = float(d @ np.linalg.solve(S, d))
        return m if m <= CHI2 else None

    @property
    def speed(self) -> float:
        return math.hypot(self.x[2], self.x[3])

    @property
    def vel_sigma(self) -> float:
        return sd(self.P, 2)

    def forecast(self) -> list[Prediction] | None:
        if not max(STILL_V, 2 * self.vel_sigma) <= self.speed <= PRED_VMAX.get(self.key, 30.0):
            return None
        out = []
        for dt in HORIZONS_S:
            x, P = self.predicted(self.t + dt, accel=0.0)
            s = math.hypot(max(sd(P, 0), FLOOR * self.r_last), MANEUVER * self.speed * dt)
            out.append(Prediction(dt_s=dt, xy=(round(float(x[0]), 2), round(float(x[1]), 2)), sigma_m=round(s, 2)))
        return out

    def reacq_radius(self, t: float) -> float:
        base = REACQ_M.get(self.key, 5.0)
        if self.speed < STILL_V:
            return base
        return min(base + VMAX.get(self.key, 5.0) * (t - self.last), REACQ_CAP_M.get(self.key, 20.0))

    def reacq(self, xy, t: float) -> float | None:
        """A score on the gate's scale (0 .. CHI2), or None."""
        d, rad = math.dist(xy, self.x[:2]), self.reacq_radius(t)
        return CHI2 * (d / rad) ** 2 if d <= rad else None

    def reset(self, xy, r: float, t: float) -> None:
        """Start the filter again at this measurement."""
        v0 = VEL0.get(self.grp, 5.0) ** 2
        self.x = np.array([xy[0], xy[1], 0.0, 0.0])
        self.P = np.diag([r * r, r * r, v0, v0])
        self.t = t

    def update(self, xy, z, r: float, t: float) -> None:
        x, P = self.predicted(t)
        if math.dist(xy, x[:2]) > 3 * MAX_GATE_M.get(self.grp, 20.0):  # an ID switch: restart
            self.reset(xy, r, t)
            x, P = self.x, self.P
        H = np.zeros((2, 4))
        H[0, 0] = H[1, 1] = 1
        S = H @ P @ H.T + np.eye(2) * r * r
        K = P @ H.T @ np.linalg.inv(S)
        self.x = x + K @ (np.asarray(xy) - x[:2])
        self.P = (np.eye(4) - K @ H) @ P
        self.t, self.r_last, self.z = t, r, z
        self.last = t
        self.n += 1
        self.state = "active"
        self.history.append((round(t, 3), round(float(self.x[0]), 2), round(float(self.x[1]), 2)))

    def saw_as(self, grp: str) -> None:
        self.grps.append(grp)
        self.grp = Counter(self.grps).most_common(1)[0][0]

    def absorb(self, other: Obj) -> None:
        """Take over another object that turned out to be this one."""
        for k, ts in other.tracks.items():
            self.tracks[k] = max(ts, self.tracks.get(k, ts))
        for s, ts in other.seen_by.items():
            self.seen_by[s] = max(ts, self.seen_by.get(s, ts))
        if other.last > self.last:
            self.x, self.P, self.t, self.r_last, self.z = other.x, other.P, other.t, other.r_last, other.z
            self.last, self.state = other.last, other.state
        self.n += other.n
        self.confirmed = self.confirmed or other.confirmed
        self.first = min(self.first, other.first)
        self.poses.extend(other.poses)
        for g in other.grps:
            self.saw_as(g)
        self.history = deque(sorted([*self.history, *other.history]), maxlen=self.history.maxlen)

    @property
    def sigma(self) -> float:
        return max(sd(self.P, 0), FLOOR * self.r_last)

    def model(self) -> WorldObject:
        pose = Counter(self.poses).most_common(1)[0][0] if self.poses else None
        xy = (round(float(self.x[0]), 2), round(float(self.x[1]), 2))
        return WorldObject(oid=self.oid, grp=self.grp, pose=pose, xy=xy,
                           z=None if self.z is None else round(self.z, 2),
                           vel=(round(float(self.x[2]), 2), round(float(self.x[3]), 2)),
                           vel_sigma=round(self.vel_sigma, 2), pred=self.forecast(),
                           sigma_m=round(self.sigma, 2), n_obs=self.n, seen_by=sorted(self.seen_by),
                           tracks=sorted(self.tracks), first=self.first, last=self.last, state=self.state)


class World:
    def __init__(self, wid: str, stale_s: float = 3.0, expire_s: float = 600.0, event_ttl_s: float = 5.0,
                 event_classes: set[str] | None = None, confirm_n: int | None = None):
        self.wid = wid
        self.confirm_n = CONFIRM_N if confirm_n is None else confirm_n
        self.stale_s, self.expire_s, self.event_ttl_s = stale_s, expire_s, event_ttl_s
        self.event_classes = event_classes or {"explosion", "tank_fire"}
        self.objects: dict[str, Obj] = {}
        self.by_track: dict[tuple[str, int], str] = {}
        self.events: dict[str, dict] = {}
        self.counts: deque = deque()  # (t, Counter of active objects)
        self.merged: list[str] = []  # absorbed oids, not yet published
        self.close: Counter = Counter()  # (oid, oid) -> frames both seen within DUP_M
        self._ids = itertools.count(1)
        self._eids = itertools.count(1)
        self.stats = Counter()

    def _score(self, o: Obj, sid: str, xy, r: float, t: float, present: set) -> tuple[float, str] | None:
        """(score, kind) for matching a new track of this stream to object o, or None."""
        if self._watched_by(o, sid, t, present):
            return None
        mine = o.seen_by.get(sid)
        others = [s for s, ts in o.seen_by.items() if s != sid and t - ts <= LIVE_S]
        if mine is not None and t - mine <= GAP_S:
            kind, m = "stitch", o.gate(xy, r, t)
        elif others:
            kind, m = "merge", o.gate(xy, r, t)
        elif t - o.last > GAP_S:
            kind, m = "reacquire", o.reacq(xy, t)
        else:
            return None
        return None if m is None else (m, kind)

    def _watched_by(self, o: Obj, sid: str, t: float, present: set) -> bool:
        """This stream still has a live track on the object, so a new tid is another object."""
        window = WATCH_S.get(o.key, 0.0)
        return any(s == sid and ((s, tid) in present or t - ts < window) for (s, tid), ts in o.tracks.items())

    def observe(self, sid: str, pts: list[dict], t: float) -> tuple[list[Obj], list[dict]]:
        events = []
        tracked = []
        for p in pts:
            if not p.get("xyz"):
                continue
            if p["cls"] in self.event_classes:
                e = self._event(sid, p, t)
                if e:
                    events.append(e)
            elif p.get("tid") is not None:
                tracked.append(p)
        present = {(sid, int(p["tid"])) for p in tracked}
        known = [p for p in tracked if self.by_track.get((sid, int(p["tid"]))) in self.objects]
        fresh = [p for p in tracked if p not in known]

        taken: set[str] = {self.by_track[(sid, int(p["tid"]))] for p in known}
        pairs = []
        for i, p in enumerate(fresh):
            key, xy, r = assoc(p["grp"]), (p["xyz"][0], p["xyz"][1]), self._r(p)
            for oid, o in self.objects.items():
                if o.key == key and oid not in taken:
                    hit = self._score(o, sid, xy, r, t, present)
                    if hit:
                        pairs.append((hit[0], i, oid, hit[1]))
        assigned: dict[int, str] = {}
        for _, i, oid, kind in sorted(pairs):  # nearest pair first
            if i not in assigned and oid not in taken:
                assigned[i] = oid
                taken.add(oid)
                self.stats[kind] += 1
        for i, p in enumerate(fresh):
            oid = assigned.get(i)
            if oid is None:
                oid = f"w-{next(self._ids):04d}"
                self.objects[oid] = Obj(oid, p["grp"], (p["xyz"][0], p["xyz"][1]), self._r(p), t)
                self.stats["new"] += 1
            self.by_track[(sid, int(p["tid"]))] = oid

        changed: dict[str, Obj] = {}
        for p in tracked:
            key = (sid, int(p["tid"]))
            o = self.objects[self.by_track[key]]
            o.update((p["xyz"][0], p["xyz"][1]), p["xyz"][2], self._r(p), t)
            o.seen_by[sid] = t
            o.tracks[key] = t
            o.saw_as(p["grp"])
            if p["cls"] in POSE:
                o.poses.append(POSE[p["cls"]])
            changed[o.oid] = o
        self._dedupe(changed)
        for o in changed.values():
            o.confirmed = o.confirmed or o.n >= self.confirm_n
        return [o for oid, o in changed.items() if oid in self.objects and o.confirmed], events

    def published(self) -> list[Obj]:
        """Objects past confirmation, the ones the map shows."""
        return [o for o in self.objects.values() if o.confirmed]

    @staticmethod
    def _r(p: dict) -> float:
        return max(float(p.get("sigma_m") or 3.0), 0.3)

    def _dedupe(self, changed: dict[str, Obj]) -> None:
        """Merge vehicle objects closer than DUP_M into the older one."""
        for o in list(changed.values()):
            if o.key != "vehicle" or o.oid not in self.objects:
                continue
            for p in list(self.objects.values()):
                if p is o or p.key != "vehicle" or p.oid not in self.objects:
                    continue
                pair = tuple(sorted((o.oid, p.oid)))
                if math.dist(o.x[:2], p.x[:2]) > DUP_M:
                    self.close.pop(pair, None)
                    continue
                if p.oid in changed:  # both seen now: wait DUP_FRAMES frames
                    self.close[pair] += 1
                    if self.close[pair] < DUP_FRAMES:
                        continue
                keep, drop = (o, p) if (o.first, o.oid) <= (p.first, p.oid) else (p, o)
                keep.absorb(drop)
                for k in drop.tracks:
                    self.by_track[k] = keep.oid
                del self.objects[drop.oid]
                changed.pop(drop.oid, None)
                changed[keep.oid] = keep
                self.close.pop(pair, None)
                if drop.confirmed:  # a tentative one was never published
                    self.merged.append(drop.oid)
                self.stats["dedupe"] += 1
                o = keep

    def take_merged(self) -> list[str]:
        """oids absorbed since the last call (published as forgotten)."""
        out, self.merged = self.merged, []
        return out

    def forget(self, sid: str) -> list[str]:
        """Drop the objects only this stream saw and its tracks from the others; -> the published oids dropped"""
        dropped = []
        for oid, o in list(self.objects.items()):
            if set(o.seen_by) <= {sid}:
                del self.objects[oid]
                if o.confirmed:
                    dropped.append(oid)
            else:
                o.seen_by.pop(sid, None)
                o.tracks = {k: v for k, v in o.tracks.items() if k[0] != sid}
        self.by_track = {k: v for k, v in self.by_track.items() if k[0] != sid and v in self.objects}
        self.close = Counter({k: v for k, v in self.close.items() if k[0] in self.objects and k[1] in self.objects})
        self.events = {eid: e for eid, e in self.events.items() if e["sid"] != sid}
        return dropped

    def _event(self, sid: str, p: dict, t: float) -> dict | None:
        xy = (p["xyz"][0], p["xyz"][1])
        for e in self.events.values():
            if e["kind"] == p["cls"] and t - e["t"] < e["ttl_s"] and math.dist(e["xy"], xy) < EVENT_MERGE_M:
                e["seen"] = t
                return None
        e = {"eid": f"e-{next(self._eids):05d}", "kind": p["cls"], "xy": [round(xy[0], 2), round(xy[1], 2)],
             "z": p["xyz"][2], "t": t, "ttl_s": self.event_ttl_s, "sid": sid, "seen": t}
        self.events[e["eid"]] = e
        return {k: v for k, v in e.items() if k != "seen"}

    def sweep(self, t: float) -> tuple[list[Obj], list[str]]:
        stale, expired, gone = [], [], False
        for oid, o in list(self.objects.items()):
            if not o.confirmed:
                if t - o.last > GAP_S:  # a false positive: drop it, nobody saw it
                    del self.objects[oid]
                    self.stats["dropped"] += 1
                    gone = True
            elif t - o.last > self.expire_s:
                expired.append(oid)
                del self.objects[oid]
            elif t - o.last > self.stale_s and o.state == "active":
                o.state = "stale"
                if o.speed < STILL_V:
                    o.x[2:] = 0.0
                stale.append(o)
        if expired or gone:
            self.by_track = {k: v for k, v in self.by_track.items() if v in self.objects}
            self.close = Counter({k: v for k, v in self.close.items() if k[0] in self.objects and k[1] in self.objects})
        for eid in [e for e, ev in self.events.items() if t - ev["seen"] > ev["ttl_s"]]:
            del self.events[eid]
        return stale, expired

    def smoothed_counts(self, t: float, window_s: float = 0.5) -> dict[str, float]:
        """Active objects per group, median over the last window."""
        self.counts.append((t, Counter(o.grp for o in self.published() if o.state == "active")))
        while self.counts and t - self.counts[0][0] > window_s:
            self.counts.popleft()
        groups = {g for _, c in self.counts for g in c}
        return {g: float(np.median([c.get(g, 0) for _, c in self.counts])) for g in sorted(groups)}
