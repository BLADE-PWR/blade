// The gateway WebSocket (services/gateway/README.md): JSON text messages, plus binary video previews
// b"ZPIF" + uint32 big-endian header length + header JSON + JPEG. Also the few REST calls the UI makes.
import type { Recording, Session } from "./contracts";

export type Message = {
  type: string;
  sid?: string | null;
  wid?: string | null;
  frame?: number | null;
  ts?: Record<string, unknown>;
  data: unknown;
};

export type FrameHeader = {
  type: "frame";
  sid: string;
  drone?: string | null; // its name
  frame: number | null;
  w: number;
  h: number;
  src: { w: number; h: number } | null;
  ts: Record<string, unknown>;
};

export type Frame = { header: FrameHeader; jpeg: Blob };

// a mission: every live world of it (one per map) and every drone in them, also ones starting or joining later
export type Target = { session: string } | { mission: string };

export function parseFrame(buf: ArrayBuffer): Frame {
  const magic = new TextDecoder().decode(new Uint8Array(buf, 0, 4));
  if (magic !== "ZPIF") throw new Error(`not a ZPIF frame: ${magic}`);
  const n = new DataView(buf).getUint32(4);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 8, n))) as FrameHeader;
  return { header, jpeg: new Blob([new Uint8Array(buf, 8 + n)], { type: "image/jpeg" }) };
}

function wsUrl(target: Target): string {
  const q = "session" in target
    ? new URLSearchParams({ session: target.session })
    : new URLSearchParams({ mission: target.mission });
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws?${q}`;
}

export type Handlers = {
  onMessage: (m: Message) => void;
  onFrame: (f: Frame) => void;
  onClose: (reason: string) => void;
};

export async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`GET ${path}: ${r.status}`);
  return (await r.json()) as T;
}

export async function send<T>(method: "POST" | "PUT" | "PATCH", path: string, body: object = {}): Promise<T> {
  const r = await fetch(path, { method, headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${method} ${path}: ${r.status} ${await r.text()}`);
  return (await r.json()) as T;
}

/** -> the session replaying rec: the running one that already does, else a new one. */
export async function startReplay(rec: Recording): Promise<string> {
  const running = (await getJSON<Session[]>("/sessions")).find((s) => s.state === "running"
    && s.replays.length === 1 && s.replays[0].recording === rec.id);
  if (running) return running.id;
  // new ids each time, so it never mixes with the live mission or another replay; the UI shows the recorded names
  const id = () => Math.random().toString(16).slice(2, 8);
  const sids = Object.fromEntries(rec.drones.map((d) => [d.name, `rp-${id()}`]));
  const s = await send<Session>("POST", "/sessions", { name: `Replay ${rec.scene ?? rec.id}`,
    replays: [{ recording: rec.id, wid: `rw-${id()}`, sids, mission: rec.mission, loop: true }] });
  await send("POST", `/sessions/${s.id}/start`);
  return s.id;
}

/** Opens the WebSocket; returns a function that closes it. */
export function connect(target: Target, h: Handlers): () => void {
  const ws = new WebSocket(wsUrl(target));
  ws.binaryType = "arraybuffer";
  ws.onmessage = (e: MessageEvent<string | ArrayBuffer>) => {
    if (typeof e.data === "string") h.onMessage(JSON.parse(e.data) as Message);
    else h.onFrame(parseFrame(e.data));
  };
  ws.onclose = (e) => h.onClose(e.reason || `closed (${e.code})`);
  return () => {
    ws.onclose = null;
    ws.close();
  };
}
