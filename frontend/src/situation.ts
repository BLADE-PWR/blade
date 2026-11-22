// The live picture of one gateway connection: the latest of every message type, updated in place as messages arrive.
// A connection may show several worlds (a mission flown on two maps): each keeps its own objects, events, true
// positions, alerts and clock, since their ids and coordinates only mean something within that world.
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
  WorldInfo,
  WorldObject,
  WorldObjectsData,
} from "./contracts";
import type { Frame, Message } from "./gateway";

const PREVIEWS_KEPT = 30; // per drone, ~2 s at the gateway's 15 fps
const ROUTE_STEP_M = 5;
const ROUTE_POINTS = 500;
const EVENTS_KEPT = 50;
const DETS_FRESH_S = 1; // s
const MATCH_FRAMES = 2;
const SILENT_S = 3; // s
const SEEK_MATCH_S = 2; // a frame this close to a seek's target ends the wait
const SEEK_GIVE_UP_S = 15;
const SEEK_STALE_S = 1; // s after a jump during which messages from before it (still on their way) are dropped

export type Preview = { frame: number | null; url: string; blob: Blob; w: number; h: number; src: [number, number];
  media: number | null }; // a replay frame's time in its recording, s

export type Drone = {
  sid: string;
  name: string | null; // from its frames (tel.drone)
  wid: string | null; // its world, from worlds.<wid>, detections and geo
  // a replay jump: its target in recording time (if known), when (wall s), and whether a frame is expected there
  seekTo: { media: number | null; at: number; wait: boolean } | null;
  previews: Preview[];
  lastFrameAt: number;
  dets: (DetectionsData & { frame: number | null; at: number }) | null;
  geo: GeoData | null;
  route: Vec2[];
  stamps: Stamps | null;
};

export type WorldView = {
  wid: string;
  map: string | null;
  clock: number | null; // the world's time, s
  objects: Map<string, WorldObject>; // oid -> object
  events: Map<string, WorldEvent>;
  truth: TruthMatch[];
  alerts: Map<string, Alert>; // aid -> active alert
  info: WorldInfo | null;
};

export type ShownAlert = Alert & { wid: string; key: string }; // key: unique over all worlds, unlike aid

export type Situation = {
  session: Session | null;
  drones: Map<string, Drone>;
  worlds: Map<string, WorldView>;
  diag: Map<string, DiagData>; // service -> streams of all its replicas
  replicas: Map<string, DiagData & { at: number }>; // "service instance" -> that replica's last report
};

export type Filter = { hidden: Set<string>; stale: boolean };

export const visible = (o: WorldObject, f: Filter) => !f.hidden.has(o.grp) && (f.stale || o.state === "active");

export const boxVisible = (d: Det, f: Filter) => !f.hidden.has(d.grp);

export function emptySituation(): Situation {
  return { session: null, drones: new Map(), worlds: new Map(), diag: new Map(), replicas: new Map() };
}

const emptyWorld = (wid: string): WorldView => ({ wid, map: null, clock: null, objects: new Map(), events: new Map(),
  truth: [], alerts: new Map(), info: null });

function world(s: Situation, wid: string): WorldView {
  let w = s.worlds.get(wid);
  if (!w) {
    w = emptyWorld(wid);
    s.worlds.set(wid, w);
  }
  return w;
}

function drone(s: Situation, sid: string): Drone {
  let d = s.drones.get(sid);
  if (!d) {
    d = { sid, name: null, wid: null, seekTo: null, previews: [], lastFrameAt: 0, dets: null, geo: null, route: [], stamps: null };
    s.drones.set(sid, d);
  }
  return d;
}

function dropWorld(s: Situation, wid: string): void {
  s.worlds.delete(wid);
  for (const [sid, d] of s.drones) {
    if (d.wid !== wid) continue;
    for (const p of d.previews) URL.revokeObjectURL(p.url);
    s.drones.delete(sid);
  }
}

function addEvents(w: WorldView, events: WorldEvent[]): void {
  for (const e of events) w.events.set(e.eid, e);
  while (w.events.size > EVENTS_KEPT) w.events.delete(w.events.keys().next().value!);
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
    case "world": {
      const info = m.data as WorldInfo;
      if (info.ended) { // ended by ingest (no frame for 10 min, or Stop mission): its drones and objects go
        dropWorld(s, info.wid);
        break;
      }
      const w = world(s, info.wid);
      w.info = info;
      w.map = info.map;
      for (const [sid, name] of Object.entries(info.drones)) { // also drones that send nothing any more
        const d = drone(s, sid);
        d.name ??= name;
        d.wid = info.wid;
      }
      break;
    }
    case "snapshot": {
      const snap = m.data as Snapshot;
      const w = world(s, m.wid ?? "");
      if (snap.map) w.map = snap.map;
      for (const o of snap.objects) w.objects.set(o.oid, o);
      addEvents(w, snap.events);
      if (snap.t) w.clock = snap.t;
      break;
    }
    case "detections":
      if (m.sid && !fromBeforeJump(s.drones.get(m.sid), m.ts?.media)) {
        const d = drone(s, m.sid);
        d.dets = { ...(m.data as DetectionsData), frame: m.frame ?? null, at: Date.now() / 1000 };
        d.wid = m.wid ?? d.wid;
      }
      break;
    case "geo":
      if (m.sid && !fromBeforeJump(s.drones.get(m.sid), m.ts?.media)) {
        const d = drone(s, m.sid);
        d.geo = m.data as GeoData;
        d.wid = m.wid ?? null;
        addRoute(d, [d.geo.camera[0], d.geo.camera[1]]);
      }
      break;
    case "world.objects": {
      const data = m.data as WorldObjectsData;
      const w = world(s, m.wid ?? "");
      w.map = data.map;
      for (const o of data.objects) w.objects.set(o.oid, o);
      for (const oid of [...data.expired, ...(data.forgotten ?? [])]) w.objects.delete(oid);
      if (data.t) w.clock = data.t;
      if (m.sid && m.ts) drone(s, m.sid).stamps = m.ts as Stamps;
      break;
    }
    case "world.events":
      addEvents(world(s, m.wid ?? ""), (m.data as { events: WorldEvent[] }).events);
      break;
    case "alerts": {
      const w = world(s, m.wid ?? "");
      for (const a of (m.data as { alerts: Alert[] }).alerts) {
        if (a.state === "active") w.alerts.set(a.aid, a);
        else w.alerts.delete(a.aid);
      }
      break;
    }
    case "eval":
      world(s, m.wid ?? "").truth = (m.data as EvalData).truth;
      break;
    case "diag": {
      const diag = m.data as DiagData;
      const now = Date.now() / 1000;
      s.replicas.set(`${diag.service} ${diag.instance ?? ""}`, { ...diag, at: now });
      const streams = {};
      for (const [key, r] of s.replicas) {
        if (now - r.at >= SILENT_S) s.replicas.delete(key);
        else if (r.service === diag.service) Object.assign(streams, r.streams);
      }
      s.diag.set(diag.service, { ...diag, streams });
      break;
    }
  }
}

export function addPreview(s: Situation, f: Frame): void {
  const d = drone(s, f.header.sid);
  d.name = f.header.drone ?? d.name;
  const src = f.header.src;
  const media = typeof f.header.ts.media === "number" ? f.header.ts.media : null;
  if (fromBeforeJump(d, media)) return;
  d.previews.push({ frame: f.header.frame, url: URL.createObjectURL(f.jpeg), blob: f.jpeg, w: f.header.w,
    h: f.header.h, src: src ? [src.w, src.h] : [f.header.w, f.header.h], media });
  d.seekTo = null; // not from before a jump: the jump is over
  d.lastFrameAt = Date.now() / 1000;
  while (d.previews.length > PREVIEWS_KEPT) URL.revokeObjectURL(d.previews.shift()!.url);
}

/** A message from before a replay jump, still on its way: its picture, boxes or position are of another moment. */
function fromBeforeJump(d: Drone | undefined, media: unknown): boolean {
  const j = d?.seekTo;
  if (!j || j.media === null || typeof media !== "number" || Date.now() / 1000 - j.at > SEEK_STALE_S) return false;
  return Math.abs(media - j.media) >= SEEK_MATCH_S;
}

/** A replay jumps by deltaS. Every drone's picture, boxes and position are of another moment, so they go; the drones
 * playing there wait for a frame from there (playing: their sids), the others (not joined yet, or gone by then) show
 * nothing until they send. */
export function seek(s: Situation, sids: string[], deltaS: number, playing: Set<string>): void {
  const now = Date.now() / 1000;
  for (const sid of sids) {
    const d = s.drones.get(sid);
    if (!d) continue;
    const media = d.previews.at(-1)?.media;
    for (const p of d.previews) URL.revokeObjectURL(p.url);
    d.previews = [];
    d.dets = d.geo = null;
    d.route = [];
    d.seekTo = { media: media == null ? null : media + deltaS, at: now, wait: playing.has(sid) && media != null };
  }
}

export const seeking = (d: Drone | undefined, now: number) =>
  !!d?.seekTo?.wait && now - d.seekTo.at < SEEK_GIVE_UP_S;

/** The drone's world (empty until it is known). */
export function worldOf(s: Situation, d: Drone | undefined): WorldView {
  return (d?.wid ? s.worlds.get(d.wid) : undefined) ?? emptyWorld(d?.wid ?? "");
}

/** The world to show: the focused drone's, else the first one that has a map. */
export function shownWorld(s: Situation, focus: string | null): WorldView {
  const own = focus ? s.drones.get(focus)?.wid : null;
  const w = own ? s.worlds.get(own) : undefined;
  return w ?? [...s.worlds.values()].find((x) => x.map) ?? emptyWorld("");
}

export function shownMap(s: Situation, focus: string | null): string | undefined {
  return shownWorld(s, focus).map ?? undefined;
}

/** A drone of this world, to focus when something of that world is picked (an alert). */
export function droneIn(s: Situation, wid: string): string | undefined {
  return [...s.drones.values()].find((d) => d.wid === wid)?.sid;
}

function baseName(s: Situation, sid: string): string {
  return s.drones.get(sid)?.name ?? sid;
}

/** What to call a drone: its name (from its frames and its world; a replay's are the recorded ones), else its stream
 * id; with its map when a drone of another world (another map) has the same name. */
export function droneName(s: Situation, sid: string): string {
  const name = baseName(s, sid);
  const d = s.drones.get(sid);
  const twin = [...s.drones.values()].some((o) => o.sid !== sid && o.wid !== d?.wid && baseName(s, o.sid) === name);
  const map = twin ? worldOf(s, d).map : null;
  return map ? `${name} (${map})` : name;
}

/** The time to age things by: the world's clock, else the furthest of all worlds', else the wall clock. */
export function worldNow(s: Situation, w?: WorldView): number {
  if (w?.clock) return w.clock;
  const clocks = [...s.worlds.values()].flatMap((x) => (x.clock ? [x.clock] : []));
  return clocks.length ? Math.max(...clocks) : Date.now() / 1000;
}

export function dispose(s: Situation): void {
  for (const d of s.drones.values()) for (const p of d.previews) URL.revokeObjectURL(p.url);
  s.drones.clear();
}

export function shownFrame(d: Drone): { preview: Preview | null; dets: Drone["dets"] } {
  const newest = d.previews.at(-1) ?? null;
  const dets = d.dets;
  if (!dets || dets.frame === null || d.lastFrameAt - dets.at > DETS_FRESH_S) return { preview: newest, dets: null };
  const framed = d.previews.filter((p) => p.frame !== null) as (Preview & { frame: number })[];
  let best: (typeof framed)[number] | null = null;
  let gap = MATCH_FRAMES;
  for (const [i, p] of framed.entries()) {
    if (i > 0) gap = Math.max(gap, p.frame - framed[i - 1].frame);
    if (!best || Math.abs(p.frame - dets.frame) < Math.abs(best.frame - dets.frame)) best = p;
  }
  return best && Math.abs(best.frame - dets.frame) <= gap ? { preview: best, dets } : { preview: newest, dets: null };
}

export function transmitting(d: Drone, now: number): boolean {
  return now - d.lastFrameAt < SILENT_S;
}

/** Every active alert of every world: an operator sees all of them, whichever map is shown. */
export function allAlerts(s: Situation): ShownAlert[] {
  return [...s.worlds.values()].flatMap((w) => [...w.alerts.values()].map((a) => ({ ...a, wid: w.wid,
    key: `${w.wid} ${a.aid}` })));
}

export function latencyS(d: Drone): number | null {
  const capture = d.stamps?.capture;
  const world = d.stamps?.world;
  return typeof capture === "number" && Array.isArray(world) ? world[1] - capture : null;
}

export function stageTimes(d: Drone): [string, number][] {
  return Object.entries(d.stamps ?? {}).flatMap(([stage, t]) => (Array.isArray(t) ? [[stage, t[1] - t[0]]] : []));
}
