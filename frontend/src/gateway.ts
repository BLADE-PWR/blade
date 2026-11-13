// The gateway WebSocket (services/gateway/README.md): JSON text messages, plus binary video previews
// b"ZPIF" + uint32 big-endian header length + header JSON + JPEG.

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
  frame: number | null;
  w: number;
  h: number;
  src: { w: number; h: number } | null;
  ts: Record<string, unknown>;
};

export type Frame = { header: FrameHeader; jpeg: Blob };

export type Target = { session: string } | { sids: string[]; wids: string[] };

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
    : new URLSearchParams({ sids: target.sids.join(","), wids: target.wids.join(",") });
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

export async function patchJSON(path: string, body: object): Promise<void> {
  const r = await fetch(path, { method: "PATCH", headers: { "content-type": "application/json" },
    body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`PATCH ${path}: ${r.status}`);
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
    ws.onclose = null; // closing on purpose is not an error to show
    ws.close();
  };
}
