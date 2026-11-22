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

export type Cluster = {
  cid: string;
  objects: string[];
  composition: Record<string, number>;
  centre: Vec2;
  hull: Vec2[];
  area_m2: number;
  vel?: Vec2;
};

export type ClustersData = { clusters: Cluster[]; t?: number };

export type TruthMatch = { truth_id: number; grp: string; xy: Vec2; oid?: string; err_m?: number };

export type EvalData = { truth: TruthMatch[] };

export type StreamDiag = { fps_in: number; fps_out: number; dropped: number; latency_ms?: number | null };

export type DiagData = { service: string; instance?: string | null; streams: Record<string, StreamDiag> };

export type SessionStream = { sid: string; wid: string; recording: string; drone?: string | null;
  mission?: string | null }; // a replay

export type Session = { id: string; name: string; state: string; streams: SessionStream[] };

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
  sid: string; // the drone
  mission?: string | null; // its uplink --mission; none for stills, or while still recording
  scene: string | null;
  started: number;
  duration_s?: number; // both missing while the recording is still being written
  has_truth?: boolean;
};
