// Message payloads the UI reads, copied by hand from libs/common/blade_common/contracts.py (only the fields used).

export type Vec2 = [number, number];
export type Vec3 = [number, number, number];

export type Stamps = Record<string, number | [number, number]>;

export type Det = { tid: number | null; cls: string; grp: string; conf: number; box: [number, number, number, number] };

export type DetectionsData = { model: string; img: [number, number]; dets: Det[] };

export type GeoPoint = { tid: number | null; grp: string; conf: number; xyz: Vec3 | null; sigma_m: number | null };

export type GeoData = {
  camera: Vec3;
  footprint: (Vec2 | null)[];
  centre: Vec3 | null;
  gsd_m: number | null;
  pts: GeoPoint[];
  map: string; // the map the drone is on
};

export type Prediction = { dt_s: number; xy: Vec2; sigma_m: number };

export type WorldObject = {
  oid: string;
  grp: string;
  xy: Vec2;
  z: number | null;
  vel: Vec2 | null;
  pred: Prediction[] | null;
  sigma_m: number | null;
  n_obs: number;
  seen_by: string[];
  tracks: [string, number][];
  first: number;
  last: number;
  state: "active" | "stale";
};

export type WorldObjectsData = {
  objects: WorldObject[];
  expired: string[];
  forgotten?: string[];
  t?: number; // world clock, s
  map: string; // the world's map
};

export type WorldEvent = { eid: string; kind: string; xy: Vec2; t: number; ttl_s: number; sid: string };

// worlds.<wid>, from ingest: a world's mission, map and the names of its drones
export type WorldInfo = { wid: string; mission: string; map: string; kind: "live" | "replay"; started: number;
  ended?: number | null; drones: Record<string, string> }; // sid -> name

export type Snapshot = { objects: WorldObject[]; events: WorldEvent[]; t?: number; map?: string | null };

export type Alert = {
  aid: string;
  rule: string;
  severity: "info" | "notice" | "warning";
  title: string;
  explanation: string;
  objects: string[];
  xy: Vec2 | null;
  t: number;
  state: "active" | "cleared";
};

export type TruthMatch = { truth_id: number; grp: string; xy: Vec2; oid?: string; err_m?: number };

export type EvalData = { truth: TruthMatch[] };

export type StreamDiag = { fps_in: number; fps_out: number; dropped: number; latency_ms?: number | null };

export type DiagData = { service: string; instance?: string | null; streams: Record<string, StreamDiag> };

// one mission recording played into its own world, every drone of it under a stream id of its own
export type SessionReplay = { recording: string; wid: string; sids: Record<string, string> }; // drone name -> sid

export type LiveMission = { mission: string; worlds: WorldInfo[] }; // from ingest: one world per map

export type Session = { id: string; name: string; state: string; replays: SessionReplay[] };

export type RecordedDrone = { name: string; start_s: number }; // start_s: where it starts on the mission's timeline

export type Replay = {
  wid: string;
  recording: string;
  drones: (RecordedDrone & { sid: string; segments: [number, number][] })[]; // segments: [from, to] on the timeline
  generation: number; // goes up with every jump (a seek, from any tab, or a loop)
  position_s: number;
  duration_s: number;
  speed: number;
  paused: boolean;
  loop: boolean;
};

export type Recording = {
  id: string;
  mission?: string | null; // its uplink --mission; none for stills, or while still recording
  map?: string | null;
  scene: string | null;
  started: number;
  duration_s?: number; // both missing while the recording is still being written
  recording?: boolean; // still being written: listed, but cannot be replayed yet
  has_truth?: boolean;
  drones: RecordedDrone[]; // every drone of the mission, in the order they joined
};
