// Walking skeleton: pick a gateway session (or streams and worlds), open the WebSocket, show the video
// previews and how many messages of each type arrive. The map and the panels come next.
import { type FormEvent, useEffect, useState } from "react";

import { connect, type Target } from "./gateway";

type Session = {
  id: string;
  name: string;
  state: string;
  streams: { sid: string; wid: string; source: string }[];
};

const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

export function App() {
  const [target, setTarget] = useState<Target | null>(null);
  if (target) return <Live target={target} onLeave={() => setTarget(null)} />;
  return <Pick onPick={setTarget} />;
}

function Pick({ onPick }: { onPick: (t: Target) => void }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sids, setSids] = useState("rp-1");
  const [wids, setWids] = useState("rp-1");

  useEffect(() => {
    fetch("/sessions")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`GET /sessions: ${r.status}`))))
      .then(setSessions)
      .catch((e: Error) => setError(`gateway unreachable (${e.message}); is tools/dev.py running?`));
  }, []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    onPick({ sids: list(sids), wids: list(wids) });
  };

  return (
    <main>
      <h1>blade</h1>
      {error && <p className="error">{error}</p>}
      <h2>Sessions</h2>
      {sessions.length === 0 && !error && <p>No sessions on the gateway yet.</p>}
      <ul>
        {sessions.map((s) => (
          <li key={s.id}>
            <button onClick={() => onPick({ session: s.id })}>{s.name}</button> {s.state} ·{" "}
            {s.streams.map((st) => `${st.sid} (${st.source}) → ${st.wid}`).join(", ")}
          </li>
        ))}
      </ul>
      <h2>Or streams and worlds</h2>
      <form onSubmit={submit}>
        <label>sids <input value={sids} onChange={(e) => setSids(e.target.value)} /></label>
        <label>wids <input value={wids} onChange={(e) => setWids(e.target.value)} /></label>
        <button type="submit">Connect</button>
      </form>
    </main>
  );
}

function Live({ target, onLeave }: { target: Target; onLeave: () => void }) {
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [frames, setFrames] = useState<Record<string, string>>({}); // sid -> object URL of the last preview
  const [closed, setClosed] = useState<string | null>(null);

  useEffect(() => {
    const urls: Record<string, string> = {};
    const count = (type: string) => setCounts((c) => ({ ...c, [type]: (c[type] ?? 0) + 1 }));
    const stop = connect(target, {
      onMessage: (m) => count(m.type),
      onFrame: ({ header, jpeg }) => {
        const old = urls[header.sid];
        urls[header.sid] = URL.createObjectURL(jpeg);
        setFrames((f) => ({ ...f, [header.sid]: urls[header.sid] }));
        if (old) URL.revokeObjectURL(old);
        count("frame");
      },
      onClose: setClosed,
    });
    return () => {
      stop();
      Object.values(urls).forEach((u) => URL.revokeObjectURL(u));
    };
  }, [target]);

  return (
    <main>
      <button onClick={onLeave}>← back</button>
      {closed && <p className="error">WebSocket {closed}</p>}
      <div className="videos">
        {Object.entries(frames).map(([sid, url]) => (
          <figure key={sid}>
            <img src={url} alt={`${sid} preview`} />
            <figcaption>{sid}</figcaption>
          </figure>
        ))}
      </div>
      <table>
        <thead>
          <tr><th>type</th><th>messages</th></tr>
        </thead>
        <tbody>
          {Object.entries(counts).sort().map(([type, n]) => (
            <tr key={type}><td>{type}</td><td>{n}</td></tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
