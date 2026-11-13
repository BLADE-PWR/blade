// Session picker, the live connection and the top bar shared by both views.
import { type FormEvent, useEffect, useState } from "react";

import type { Session } from "./contracts";
import { connect, getJSON, type Target } from "./gateway";
import { Operator } from "./Operator";
import { addPreview, apply, dispose, emptySituation, latencyS, transmitting } from "./situation";

const CLOCK_MS = 1000;

export type View = "operator" | "command";
export type MainPanel = "video" | "map";

const list = (s: string) => s.split(",").map((x) => x.trim()).filter(Boolean);

export function App() {
  const [target, setTarget] = useState<Target | null>(null);
  if (!target) return <Pick onPick={setTarget} />;
  return <Live key={JSON.stringify(target)} target={target} onPick={setTarget} onLeave={() => setTarget(null)} />;
}

function Pick({ onPick }: { onPick: (t: Target) => void }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sids, setSids] = useState("rp-1");
  const [wids, setWids] = useState("rp-1");

  useEffect(() => {
    getJSON<Session[]>("/sessions")
      .then(setSessions)
      .catch((e: Error) => setError(`Brak połączenia z gatewayem (${e.message}). Czy działa tools/dev.py?`));
  }, []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    onPick({ sids: list(sids), wids: list(wids) });
  };

  return (
    <main className="pick">
      <h1>Blade</h1>
      {error && <p className="error">{error}</p>}
      <h2>Sesje</h2>
      {sessions.length === 0 && !error && <p>Gateway nie ma jeszcze żadnej sesji.</p>}
      <ul>
        {sessions.map((s) => (
          <li key={s.id}>
            <button onClick={() => onPick({ session: s.id })}>{s.name}</button> {s.state},{" "}
            {s.streams.map((st) => `${st.sid} (${st.source}) w świecie ${st.wid}`).join(", ")}
          </li>
        ))}
      </ul>
      <h2>Albo strumienie i światy</h2>
      <form onSubmit={submit}>
        <label>sids <input value={sids} onChange={(e) => setSids(e.target.value)} /></label>
        <label>wids <input value={wids} onChange={(e) => setWids(e.target.value)} /></label>
        <button type="submit">Połącz</button>
      </form>
    </main>
  );
}

function Live({ target, onPick, onLeave }: { target: Target; onPick: (t: Target) => void; onLeave: () => void }) {
  const [sit] = useState(emptySituation);
  const [, setTick] = useState(0);
  const [closed, setClosed] = useState<string | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [view, setView] = useState<View>("operator");
  const [main, setMain] = useState<MainPanel>("video");
  const [picked, setPicked] = useState<string | null>(null);

  useEffect(() => {
    let frame = 0;
    const redraw = () => {
      if (!frame) frame = requestAnimationFrame(() => {
        frame = 0;
        setTick((t) => t + 1);
      });
    };
    const stop = connect(target, {
      onMessage: (m) => {
        apply(sit, m);
        redraw();
      },
      onFrame: (f) => {
        addPreview(sit, f);
        redraw();
      },
      onClose: setClosed,
    });
    const clock = setInterval(redraw, CLOCK_MS);
    return () => {
      stop();
      clearInterval(clock);
      cancelAnimationFrame(frame);
      dispose(sit);
    };
  }, [sit, target]);

  useEffect(() => {
    getJSON<Session[]>("/sessions").then(setSessions).catch(() => setSessions([]));
  }, []);

  const now = Date.now() / 1000;
  const sids = sit.session?.streams.map((st) => st.sid) ?? ("sids" in target ? target.sids : []);
  const sid = picked && sids.includes(picked) ? picked : (sids[0] ?? null);
  const drone = sid ? sit.drones.get(sid) : undefined;
  const replay = sit.session?.streams.some((st) => st.source === "replay") ?? false;
  const latest = (source: "live" | "replay") => sessions.filter((s) => s.streams.some((st) => st.source === source))
    .at(-1);
  const switchMode = (source: "live" | "replay") => {
    const s = latest(source);
    if (s) onPick({ session: s.id });
  };
  const fps = sid ? sit.diag.get("ingest")?.streams[sid]?.fps_in : undefined;
  const latency = drone ? latencyS(drone) : null;

  return (
    <div className="live">
      <header className="topbar">
        <strong className="brand">Blade</strong>
        <div className="switch" role="group" aria-label="Widok">
          <button aria-pressed={view === "operator"} onClick={() => setView("operator")}>Operator</button>
          <button aria-pressed={view === "command"} onClick={() => setView("command")}>Dowodzenie</button>
        </div>
        <label className="muted">Sesja <button onClick={onLeave}>{sit.session?.name ?? "bez sesji"}</button></label>
        <div className="switch" role="group" aria-label="Tryb">
          <button aria-pressed={!replay} disabled={replay && !latest("live")} onClick={() => switchMode("live")}>
            <span className="dot live-dot" />Na żywo
          </button>
          <button aria-pressed={replay} disabled={!replay && !latest("replay")} onClick={() => switchMode("replay")}>
            Odtwarzanie
          </button>
        </div>
        <span className="muted">Główny panel</span>
        <div className="switch" role="group" aria-label="Główny panel">
          <button aria-pressed={main === "map"} onClick={() => setMain("map")}>Mapa</button>
          <button aria-pressed={main === "video"} onClick={() => setMain("video")}>Wideo</button>
        </div>
        <div className="topbar-right">
          {sid && (
            <span className="chip">
              <span className={drone && transmitting(drone, now) ? "dot ok" : "dot warn"} />
              {sids.length > 1 ? (
                <label>
                  Dron{" "}
                  <select value={sid} onChange={(e) => setPicked(e.target.value)}>
                    {sids.map((s) => <option key={s}>{s}</option>)}
                  </select>
                </label>
              ) : (
                <span className="mono">{sid}</span>
              )}
              {fps !== undefined && <span className="muted">{fps.toFixed(0)} fps</span>}
            </span>
          )}
          {latency !== null && (
            <span className="muted">
              opóźnienie <span className="mono value">{(latency * 1000).toFixed(0)} ms</span>
            </span>
          )}
          <span className="mono">{new Date().toLocaleTimeString("pl-PL")}</span>
        </div>
      </header>
      {closed && <p className="error banner">WebSocket zamknięty: {closed}</p>}
      {view === "operator"
        ? <Operator sit={sit} sid={sid} main={main} onMain={setMain} />
        : <p className="todo">Widok dowodzenia</p>}
    </div>
  );
}
