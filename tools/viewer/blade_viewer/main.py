"""A small operator window that talks only to the gateway: a stream's video with its boxes, and the map
with world objects, their motion (arrow: 10 s ahead; cone: 2-sigma of the 30 / 60 s prediction), the camera
footprint, true positions and alerts.

Keys: Tab next stream · + / - zoom · f follow · w a s d pan · t truth layer · m motion layer · q quit
      replays: space pause · . one frame · [ ] slower / faster · j / l -/+10 s"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import math
import struct
import threading
import time
from collections import OrderedDict, defaultdict, deque

import cv2
import httpx
import numpy as np
import websockets

COLORS = {"soldier": (255, 120, 40), "tank": (60, 220, 160), "military_vehicle": (0, 200, 255),
          "explosion": (0, 0, 255), "tank_fire": (255, 0, 255)}  # BGR
VIDEO_W, VIDEO_H, MAP_W = 960, 540, 600
WHITE = (255, 255, 255)
ARROW_S = 10.0  # s of travel the motion arrow shows
CONE_K = 2.0  # cone radius in sigmas


def text(img, s: str, xy, scale: float, color=WHITE, thick: int = 1) -> None:
    cv2.putText(img, s, (int(xy[0]), int(xy[1])), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.frames: dict[str, deque] = defaultdict(lambda: deque(maxlen=45))  # (frame, jpeg, ts)
        self.dets: dict[str, OrderedDict] = defaultdict(OrderedDict)  # frame -> (dets, t)
        self.geo: dict[str, dict] = {}
        self.objects: dict[str, dict[str, dict]] = defaultdict(dict)  # wid -> oid -> obj
        self.events: deque = deque(maxlen=50)
        self.diag: dict[str, dict] = {}
        self.alerts: dict[str, dict] = {}  # aid -> active alert
        self.eval: dict[str, dict] = {}  # wid -> latest eval
        self.replays: dict[str, dict] = {}  # sid -> replay info
        self.status = "connecting"
        self.map_id: str | None = None

    def on_text(self, m: dict) -> None:
        typ, data = m.get("type"), m.get("data") or {}
        with self.lock:
            if typ == "session":
                self.map_id = self.map_id or data.get("map")
            elif typ == "detections":
                q = self.dets[m["sid"]]
                q[m["frame"]] = (data, time.time())
                while len(q) > 90:
                    q.popitem(last=False)
            elif typ == "geo":
                self.geo[m["sid"]] = data
            elif typ in ("world.objects", "snapshot"):
                objs = self.objects[m["wid"]]
                for o in data.get("objects", []):
                    objs[o["oid"]] = o
                for oid in data.get("expired", []):
                    objs.pop(oid, None)
            elif typ == "world.events":
                for e in data.get("events", []):
                    self.events.append((e, time.time()))
            elif typ == "diag":
                self.diag[data.get("service", "?")] = data
            elif typ == "alerts":
                for a in data.get("alerts", []):
                    if a.get("state") == "cleared":
                        self.alerts.pop(a["aid"], None)
                    else:
                        self.alerts[a["aid"]] = a
            elif typ == "eval":
                self.eval[m["wid"]] = data

    def on_frame(self, raw: bytes) -> None:
        (n,) = struct.unpack(">I", raw[4:8])
        head = json.loads(raw[8:8 + n])
        with self.lock:
            self.frames[head["sid"]].append((head["frame"], raw[8 + n:], head.get("ts", {})))


async def receive(url: str, st: State, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            async with websockets.connect(url, max_size=None) as ws:
                st.status = "connected"
                async for msg in ws:
                    if isinstance(msg, bytes) and msg[:4] == b"ZPIF":
                        st.on_frame(msg)
                    elif isinstance(msg, str):
                        st.on_text(json.loads(msg))
                    if stop.is_set():
                        return
        except Exception as e:
            st.status = f"reconnecting ({type(e).__name__})"
            await asyncio.sleep(1.0)


def poll_replays(gateway: str, st: State, stop: threading.Event) -> None:
    while not stop.is_set():
        with contextlib.suppress(httpx.HTTPError):
            r = httpx.get(f"{gateway}/api/replays", timeout=3)
            if r.status_code == 200:
                with st.lock:
                    st.replays = {x["sid"]: x for x in r.json()}
        stop.wait(1.0)


def control(gateway: str, sid: str, body: dict) -> None:
    """Replay control, off the UI thread."""
    def go():
        with contextlib.suppress(httpx.HTTPError):
            httpx.patch(f"{gateway}/api/replays/{sid}", json=body, timeout=3)
    threading.Thread(target=go, daemon=True).start()


class MapBackground:
    """Fetches terrain crops around the view in the background; one cached crop at a time."""

    def __init__(self, gateway: str):
        self.gateway = gateway
        self.img = None
        self.extent = None
        self.mpp = None
        self._busy = False

    def want(self, map_id: str | None, cx: float, cy: float, mpp: float, w: int, h: int) -> None:
        if not map_id or self._busy:
            return
        half_w, half_h = w * mpp / 2, h * mpp / 2
        e = self.extent
        if e and self.mpp == mpp and e[0] <= cx - half_w and e[2] >= cx + half_w and e[1] <= cy - half_h \
                and e[3] >= cy + half_h:
            return
        self._busy = True
        ext = (cx - 2 * half_w, cy - 2 * half_h, cx + 2 * half_w, cy + 2 * half_h)
        threading.Thread(target=self._fetch, args=(map_id, ext, mpp), daemon=True).start()

    def _fetch(self, map_id, ext, mpp) -> None:
        try:
            r = httpx.get(f"{self.gateway}/api/maps/{map_id}/crop", timeout=10,
                          params={"x0": ext[0], "y0": ext[1], "x1": ext[2], "y1": ext[3], "mpp": mpp})
            if r.status_code == 200:
                img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
                self.img, self.extent, self.mpp = img, ext, (ext[2] - ext[0]) / img.shape[1]
        except httpx.HTTPError:
            pass
        finally:
            self._busy = False

    def draw(self, canvas: np.ndarray, to_px) -> None:
        if self.img is None:
            return
        x0, y0, x1, y1 = self.extent
        (u0, v0), (u1, v1) = to_px(x0, y1), to_px(x1, y0)
        u0, v0, u1, v1 = int(round(u0)), int(round(v0)), int(round(u1)), int(round(v1))
        if u1 - u0 < 2 or v1 - v0 < 2:
            return
        img = cv2.resize(self.img, (u1 - u0, v1 - v0), interpolation=cv2.INTER_LINEAR) \
            if (u1 - u0, v1 - v0) != (self.img.shape[1], self.img.shape[0]) else self.img
        H, W = canvas.shape[:2]
        cu0, cv0, cu1, cv1 = max(u0, 0), max(v0, 0), min(u1, W), min(v1, H)
        if cu1 > cu0 and cv1 > cv0:
            canvas[cv0:cv1, cu0:cu1] = img[cv0 - v0:cv1 - v0, cu0 - u0:cu1 - u0]


def ahead(o: dict, now: float) -> list[dict]:
    """-> the predictions still in the future: pred is anchored at `last` and not republished while stale"""
    return [p for p in o.get("pred") or [] if o["last"] + p["dt_s"] > now]


def cone(o: dict, preds: list[dict]) -> np.ndarray:
    """-> hull of the CONE_K-sigma circles now and at each prediction, map metres"""
    a = np.linspace(0, 2 * np.pi, 24, endpoint=False)
    ring = np.stack([np.cos(a), np.sin(a)], 1)
    circles = [(o["xy"], o.get("sigma_m") or 0.0)] + [(p["xy"], p["sigma_m"]) for p in preds]
    pts = np.concatenate([np.asarray(xy) + CONE_K * r * ring for xy, r in circles]).astype(np.float32)
    return cv2.convexHull(pts)[:, 0]


def draw_video(st: State, sid: str | None) -> np.ndarray:
    canvas = np.full((VIDEO_H, VIDEO_W, 3), 25, np.uint8)
    if sid is None:
        text(canvas, f"no streams yet ({st.status})", (20, 40), 0.8, (200, 200, 200), 2)
        return canvas
    with st.lock:
        frames = list(st.frames[sid])
        dets = dict(st.dets[sid])
    if not frames:
        return canvas
    # the newest frame with detections, or the newest frame if detections stopped coming
    pick, boxes = frames[-1], None
    newest_det = max(dets.values(), key=lambda d: d[1], default=None)
    if newest_det and time.time() - newest_det[1] < 1.0:
        for f in reversed(frames):
            if f[0] in dets:
                pick, boxes = f, dets[f[0]][0]
                break
    frame_no, jpeg, ts = pick
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    img = cv2.resize(img, (VIDEO_W, VIDEO_H)) if img.shape[1] != VIDEO_W else img
    if boxes:
        sw, sh = boxes["img"]
        kx, ky = VIDEO_W / sw, VIDEO_H / sh
        with st.lock:  # (sid, tid) -> world object id
            oid_of = {(s, t): oid for w in st.objects.values() for oid, o in w.items() for s, t in o.get("tracks", [])}
        for d in boxes["dets"]:
            x1, y1, x2, y2 = d["box"]
            c = COLORS.get(d["grp"], (200, 200, 200))
            p1, p2 = (int(x1 * kx), int(y1 * ky)), (int(x2 * kx), int(y2 * ky))
            cv2.rectangle(img, p1, p2, c, 1 if d.get("tid") is None else 2)
            tid = d.get("tid")
            name = oid_of.get((sid, tid)) or (f"#{tid}" if tid is not None else "")
            label = f"{d['cls']} {d['conf']:.2f} {name}".rstrip()
            text(img, label, (p1[0], max(p1[1] - 4, 10)), 0.4, c)
    lat = ""
    if "capture" in ts and "ingest" in ts:
        lat = f"  capture->ingest {(ts['ingest'][1] - ts['capture']) * 1000:.0f} ms"
    model = boxes.get("model", "") if boxes else "no detections"
    with st.lock:
        rp = st.replays.get(sid)
    if rp:
        lat = (f"  replay {rp['position_s']:.1f}/{rp['duration_s']:.0f} s x{rp['speed']:g}"
               f"{' PAUSED' if rp.get('paused') else ''}{' all frames' if rp.get('delivery') == 'all' else ''}")
    text(img, f"{sid}  frame {frame_no}  {model}{lat}", (10, 22), 0.55, WHITE, 2)
    return img


def draw_map(st: State, bg: MapBackground, sid: str | None, view: dict) -> np.ndarray:
    canvas = np.full((VIDEO_H, MAP_W, 3), 35, np.uint8)
    with st.lock:
        geo = st.geo.get(sid) if sid else None
        objs = [o for w in st.objects.values() for o in w.values()]
        events = [(e, t) for e, t in st.events if time.time() - t < e.get("ttl_s", 5)]
        map_id = st.map_id
        evals = dict(st.eval)
        alerts = sorted(st.alerts.values(), key=lambda a: -a["t"])
    if view["follow"]:
        if geo and geo.get("centre"):
            view["cx"], view["cy"] = geo["centre"][0], geo["centre"][1]
        elif objs and view["cx"] is None:
            view["cx"] = float(np.mean([o["xy"][0] for o in objs]))
            view["cy"] = float(np.mean([o["xy"][1] for o in objs]))
    if view["cx"] is None:
        text(canvas, "map: waiting for geo", (20, 40), 0.7, (200, 200, 200), 2)
        return canvas
    cx, cy, mpp = view["cx"], view["cy"], view["mpp"]

    def to_px(x, y):
        return (x - cx) / mpp + MAP_W / 2, VIDEO_H / 2 - (y - cy) / mpp

    bg.want(map_id, cx, cy, mpp, MAP_W, VIDEO_H)
    bg.draw(canvas, to_px)
    if geo:
        fp = [p for p in geo.get("footprint", []) if p]
        if len(fp) >= 3:
            pts = np.array([to_px(*p) for p in fp], np.int32)
            cv2.polylines(canvas, [pts], True, WHITE, 1, cv2.LINE_AA)
        cam = geo.get("camera")
        if cam:
            u, v = to_px(cam[0], cam[1])
            if 0 <= u < MAP_W and 0 <= v < VIDEO_H:
                cv2.drawMarker(canvas, (int(u), int(v)), (255, 255, 0), cv2.MARKER_TRIANGLE_UP, 14, 2)
    preds = {o["oid"]: p for o in objs if (p := ahead(o, time.time()))} if view["motion"] else {}
    if preds:
        overlay = canvas.copy()
        for o in (o for o in objs if o["oid"] in preds):
            hull = np.array([to_px(*p) for p in cone(o, preds[o["oid"]])], np.int32)
            cv2.fillPoly(overlay, [hull], COLORS.get(o["grp"], (200, 200, 200)), cv2.LINE_AA)
        cv2.addWeighted(overlay, 0.2, canvas, 0.8, 0, canvas)
    for o in sorted(objs, key=lambda o: o.get("state") == "active"):  # remembered (stale) ones underneath
        u, v = to_px(*o["xy"])
        if not (0 <= u < MAP_W and 0 <= v < VIDEO_H):
            continue
        active = o.get("state") == "active"
        c = COLORS.get(o["grp"], (200, 200, 200))
        c = c if active else tuple(int(k * 0.55) for k in c)
        cv2.circle(canvas, (int(u), int(v)), 5, c, -1 if active else 1, cv2.LINE_AA)
        text(canvas, o["oid"][-4:], (u + 6, v - 4), 0.35, c)
        if o["oid"] in preds:
            vx, vy = o["vel"]
            tip = to_px(o["xy"][0] + vx * ARROW_S, o["xy"][1] + vy * ARROW_S)
            cv2.arrowedLine(canvas, (int(u), int(v)), (int(tip[0]), int(tip[1])), c, 2, cv2.LINE_AA, tipLength=0.25)
            text(canvas, f"{math.hypot(vx, vy) * 3.6:.0f} km/h", (u + 6, v + 10), 0.35, c)
            for p in preds[o["oid"]]:
                pu, pv = to_px(*p["xy"])
                cv2.drawMarker(canvas, (int(pu), int(pv)), c, cv2.MARKER_DIAMOND, 7, 1)
                text(canvas, f"{p['dt_s']:.0f}s", (pu + 5, pv - 3), 0.33, c)
    for e, _ in events:
        u, v = to_px(*e["xy"])
        cv2.drawMarker(canvas, (int(u), int(v)), COLORS.get(e["kind"], (0, 0, 255)), cv2.MARKER_TILTED_CROSS, 14, 2)
    by_oid = {o["oid"]: o for o in objs}
    y_text = 40
    if view["truth"]:
        for wid, ev in evals.items():
            for t in ev.get("truth", []):
                u, v = to_px(*t["xy"])
                cv2.drawMarker(canvas, (int(u), int(v)), WHITE, cv2.MARKER_CROSS, 9, 1)
                o = by_oid.get(t.get("oid"))
                if o:
                    cv2.line(canvas, (int(u), int(v)), tuple(int(c) for c in to_px(*o["xy"])), WHITE, 1)
            for grp, g in sorted(ev.get("groups", {}).items()):
                c50 = f"CEP50 {g['cep50_m']:.1f} m  CEP90 {g['cep90_m']:.1f} m" if g.get("cep50_m") is not None else ""
                txt = f"{wid} {grp}: {g.get('detected', 0)}/{g.get('in_view', 0)} found  {c50}"
                text(canvas, txt, (10, y_text), 0.42)
                y_text += 16
    sev = {"warning": (60, 60, 255), "notice": (0, 200, 255), "info": (220, 220, 220)}
    for a in alerts:
        if a.get("xy"):
            u, v = to_px(*a["xy"])
            cv2.circle(canvas, (int(u), int(v)), 18, sev.get(a["severity"], (200, 200, 200)), 1, cv2.LINE_AA)
    for k, a in enumerate(alerts[:5]):
        text(canvas, f"! {a['title']}", (10, VIDEO_H - 40 - 16 * k), 0.45, sev.get(a["severity"], (200, 200, 200)))
    n_active = sum(o.get("state") == "active" for o in objs)
    follow = "follow" if view["follow"] else ""
    text(canvas, f"{n_active} active / {len(objs)} objects   {mpp:.2f} m/px  {follow}", (10, 22), 0.5)
    L = int(100 / mpp)  # 100 m scale bar
    cv2.line(canvas, (10, VIDEO_H - 15), (10 + L, VIDEO_H - 15), WHITE, 2)
    text(canvas, "100 m", (14 + L, VIDEO_H - 11), 0.4)
    return canvas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", default="http://127.0.0.1:8000")
    ap.add_argument("--session")
    ap.add_argument("--sids", default="")
    ap.add_argument("--wids", default="")
    ap.add_argument("--map", help="map id for the background (default: the session's map)")
    ap.add_argument("--mpp", type=float, default=1.0, help="map metres per pixel")
    ap.add_argument("--save", help="no window: render for --seconds, write this PNG and exit")
    ap.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args()

    sids, wids = args.sids, args.wids
    if not args.session and not sids:
        r = httpx.get(f"{args.gateway}/streams", timeout=5).json()
        sids = ",".join(s["sid"] for s in r if s.get("sid"))
        wids = wids or ",".join(sorted({s["wid"] for s in r if s.get("wid")}))
        if not sids:
            print("no streams at ingest yet; give --sids/--wids or start a stream first")
            return 1
    q = f"session={args.session}" if args.session else f"sids={sids}&wids={wids}"
    url = args.gateway.replace("http", "ws", 1) + "/ws?" + q
    st = State()
    st.map_id = args.map
    stop = threading.Event()
    threading.Thread(target=lambda: asyncio.run(receive(url, st, stop)), daemon=True).start()
    bg = MapBackground(args.gateway)
    view = {"cx": None, "cy": None, "mpp": args.mpp, "follow": True, "truth": True, "motion": True}
    threading.Thread(target=poll_replays, args=(args.gateway, st, stop), daemon=True).start()
    order = [s for s in sids.split(",") if s]
    current = 0
    t_start = time.time()
    if not args.save:
        cv2.namedWindow("blade", cv2.WINDOW_AUTOSIZE)
    while True:
        with st.lock:
            for s in st.frames:
                if s not in order:
                    order.append(s)
        sid = order[current % len(order)] if order else None
        img = np.hstack([draw_video(st, sid), draw_map(st, bg, sid, view)])
        if args.save:
            time.sleep(0.033)
            if time.time() - t_start >= args.seconds:
                cv2.imwrite(args.save, img)
                print(f"wrote {args.save} ({st.status})")
                break
            continue
        cv2.imshow("blade", img)
        k = cv2.waitKey(33) & 0xFF
        if k in (ord("q"), 27):
            break
        if k == 9 and order:
            current += 1
        elif k in (ord("+"), ord("=")):
            view["mpp"] = max(view["mpp"] / 1.5, 0.05)
        elif k == ord("-"):
            view["mpp"] = min(view["mpp"] * 1.5, 50.0)
        elif k == ord("f"):
            view["follow"] = not view["follow"]
        elif k == ord("t"):
            view["truth"] = not view["truth"]
        elif k == ord("m"):
            view["motion"] = not view["motion"]
        elif sid and chr(k) in " .[]jl":
            with st.lock:
                rp = st.replays.get(sid)
            if rp:
                control(args.gateway, sid, {
                    " ": {"paused": not rp.get("paused")}, ".": {"step": 1},
                    "[": {"speed": rp["speed"] / 2}, "]": {"speed": rp["speed"] * 2},
                    "j": {"seek_s": rp["position_s"] - 10}, "l": {"seek_s": rp["position_s"] + 10}}[chr(k)])
        elif k in (ord("w"), ord("a"), ord("s"), ord("d")) and view["cx"] is not None:
            view["follow"] = False
            step = 100 * view["mpp"]
            view["cx"] += {ord("a"): -step, ord("d"): step}.get(k, 0)
            view["cy"] += {ord("s"): -step, ord("w"): step}.get(k, 0)
    stop.set()
    if not args.save:
        cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
