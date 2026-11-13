// The live picture of one gateway connection: the latest of every message type, updated in place as messages arrive.
import type {
  Alert,
  Det,
  DetectionsData,
  DiagData,
  EvalData,
  GeoData,
  Session,
  Snapshot,
  Stamps,
  TruthMatch,
  Vec2,
  WorldEvent,
  WorldObject,
  WorldObjectsData,
} from "./contracts";
import type { Frame, Message } from "./gateway";

const PREVIEWS_KEPT = 30; // per drone, ~2 s at the gateway's 15 fps
const ROUTE_STEP_M = 5;
const ROUTE_POINTS = 500;
const EVENTS_KEPT = 50;
const DETS_FRESH_S = 1; // boxes this much older than the newest picture are not drawn (a paused replay keeps them)
const MATCH_FRAMES = 2; // a preview this close to the boxes' frame counts as the same picture
const SILENT_S = 3; // a drone with no frame for this long is shown as not transmitting

export type Preview = { frame: number | null; url: string; blob: Blob; w: number; h: number; src: [number, number] };

export type Drone = {
  sid: string;
  previews: Preview[];
  lastFrameAt: number;
  dets: (DetectionsData & { frame: number | null; at: number }) | null;
  geo: GeoData | null;
  route: Vec2[];
  stamps: Stamps | null;
};

export type Situation = {
  session: Session | null;
  drones: Map<string, Drone>;
  objects: Map<string, WorldObject>;
  events: Map<string, WorldEvent>;
  alerts: Map<string, Alert>; // "wid aid" -> active alert
  truth: TruthMatch[];
  diag: Map<string, DiagData>;
  clocks: Map<string, number>; // wid -> world clock, s
};

/** What the operator chose to see: hidden groups and event kinds, remembered objects, the box threshold */
export type Filter = { hidden: Set<string>; stale: boolean; minConf: number };

export const visible = (o: WorldObject, f: Filter) => !f.hidden.has(o.grp) && (f.stale || o.state === "active");

export const boxVisible = (d: Det, f: Filter) => d.conf >= f.minConf && !f.hidden.has(d.grp);

export function emptySituation(): Situation {
  return { session: null, drones: new Map(), objects: new Map(), events: new Map(), alerts: new Map(), truth: [],
    diag: new Map(), clocks: new Map() };
}

function drone(s: Situation, sid: string): Drone {
  let d = s.drones.get(sid);
  if (!d) {
    d = { sid, previews: [], lastFrameAt: 0, dets: null, geo: null, route: [], stamps: null };
    s.drones.set(sid, d);
  }
  return d;
}

function upsert(s: Situation, objects: WorldObject[]): void {
  for (const o of objects) s.objects.set(o.oid, o);
}

function addEvents(s: Situation, events: WorldEvent[]): void {
  for (const e of events) s.events.set(e.eid, e);
  while (s.events.size > EVENTS_KEPT) s.events.delete(s.events.keys().next().value!);
}

function addRoute(d: Drone, xy: Vec2): void {
  const last = d.route.at(-1);
  if (last && Math.hypot(xy[0] - last[0], xy[1] - last[1]) < ROUTE_STEP_M) return;
  d.route.push(xy);
  if (d.route.length > ROUTE_POINTS) d.route.shift();
}

export function apply(s: Situation, m: Message): void {
  switch (m.type) {
    case "session":
      s.session = m.data as Session;
      break;
    case "snapshot": {
      const snap = m.data as Snapshot;
      upsert(s, snap.objects);
      addEvents(s, snap.events);
      if (m.wid && snap.t) s.clocks.set(m.wid, snap.t);
      break;
    }
    case "detections":
      if (m.sid) {
        drone(s, m.sid).dets = { ...(m.data as DetectionsData), frame: m.frame ?? null, at: Date.now() / 1000 };
      }
      break;
    case "geo":
      if (m.sid) {
        const d = drone(s, m.sid);
        d.geo = m.data as GeoData;
        addRoute(d, [d.geo.camera[0], d.geo.camera[1]]);
      }
      break;
    case "world.objects": {
      const w = m.data as WorldObjectsData;
      upsert(s, w.objects);
      for (const oid of [...w.expired, ...(w.forgotten ?? [])]) s.objects.delete(oid);
      if (m.wid && w.t) s.clocks.set(m.wid, w.t);
      if (m.sid && m.ts) drone(s, m.sid).stamps = m.ts as Stamps;
      break;
    }
    case "world.events":
      addEvents(s, (m.data as { events: WorldEvent[] }).events);
      break;
    case "alerts":
      for (const a of (m.data as { alerts: Alert[] }).alerts) { // only the alerts raised or cleared just now
        if (a.state === "active") s.alerts.set(`${m.wid} ${a.aid}`, a);
        else s.alerts.delete(`${m.wid} ${a.aid}`);
      }
      break;
    case "eval":
      s.truth = (m.data as EvalData).truth;
      break;
    case "diag": {
      const diag = m.data as DiagData;
      s.diag.set(diag.service, diag);
      break;
    }
  }
}

export function addPreview(s: Situation, f: Frame): void {
  const d = drone(s, f.header.sid);
  const src = f.header.src;
  d.previews.push({ frame: f.header.frame, url: URL.createObjectURL(f.jpeg), blob: f.jpeg, w: f.header.w,
    h: f.header.h, src: src ? [src.w, src.h] : [f.header.w, f.header.h] });
  d.lastFrameAt = Date.now() / 1000;
  while (d.previews.length > PREVIEWS_KEPT) URL.revokeObjectURL(d.previews.shift()!.url);
}

/** -> the time of the session's worlds (stands still while a replay is paused); the browser's until one is sent */
export function worldNow(s: Situation): number {
  return s.clocks.size ? Math.max(...s.clocks.values()) : Date.now() / 1000;
}

export function dispose(s: Situation): void {
  for (const d of s.drones.values()) for (const p of d.previews) URL.revokeObjectURL(p.url);
  s.drones.clear();
}

/** -> the picture to show and the boxes that belong to it (none when the boxes are stale or of another frame) */
export function shownFrame(d: Drone): { preview: Preview | null; dets: Drone["dets"] } {
  const newest = d.previews.at(-1) ?? null;
  const dets = d.dets;
  if (!dets || dets.frame === null || d.lastFrameAt - dets.at > DETS_FRESH_S) return { preview: newest, dets: null };
  let best: Preview | null = null;
  for (const p of d.previews) {
    if (p.frame === null || Math.abs(p.frame - dets.frame) > MATCH_FRAMES) continue;
    if (!best || Math.abs(p.frame - dets.frame) < Math.abs(best.frame! - dets.frame)) best = p;
  }
  return best ? { preview: best, dets } : { preview: newest, dets: null };
}

export function transmitting(d: Drone, now: number): boolean {
  return now - d.lastFrameAt < SILENT_S;
}

export function allAlerts(s: Situation): Alert[] {
  return [...s.alerts.values()];
}

/** -> seconds from capture to the world model, from the stamps of the drone's latest world update */
export function latencyS(d: Drone): number | null {
  const capture = d.stamps?.capture;
  const world = d.stamps?.world;
  return typeof capture === "number" && Array.isArray(world) ? world[1] - capture : null;
}

/** -> [stage, seconds] spent in each stage of the drone's latest world update */
export function stageTimes(d: Drone): [string, number][] {
  return Object.entries(d.stamps ?? {}).flatMap(([stage, t]) => (Array.isArray(t) ? [[stage, t[1] - t[0]]] : []));
}
