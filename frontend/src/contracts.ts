// Message payloads the UI reads, copied by hand from libs/common/blade_common/contracts.py (only the fields used).

export type Vec2 = [number, number];
export type Vec3 = [number, number, number];

/** Per-stage [t_in, t_out] epoch seconds, plus "capture" */
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
  forgotten?: string[]; // removed without losing contact (merged, or the replay went back)
  t?: number; // world clock, s: stands still while a replay is paused
};

export type WorldEvent = { eid: string; kind: string; xy: Vec2; t: number; ttl_s: number; sid: string };

export type Snapshot = { objects: WorldObject[]; events: WorldEvent[]; t?: number };

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

export type TruthMatch = { truth_id: number; grp: string; xy: Vec2; oid: string | null; err_m: number | null };

export type EvalData = { truth: TruthMatch[] };

export type StreamDiag = { fps_in: number; fps_out: number; dropped: number; latency_ms?: number | null };

export type DiagData = { service: string; streams: Record<string, StreamDiag> };

export type SessionStream = { sid: string; wid: string; source: "live" | "replay"; recording?: string | null };

export type Session = { id: string; name: string; map: string; state: string; streams: SessionStream[] };

export type Replay = {
  sid: string;
  recording: string;
  position_s: number;
  duration_s: number;
  speed: number;
  paused: boolean;
  loop: boolean;
};

export type Recording = {
  id: string;
  sid: string;
  map: string;
  scene: string | null;
  started: number;
  duration_s: number;
  has_truth: boolean;
};
