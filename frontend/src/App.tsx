// Session picker, the live connection and the top bar shared by both views.
import { type FormEvent, useEffect, useState } from "react";

import { Command } from "./Command";
import type { Alert, Recording, Session } from "./contracts";
import { recordingName } from "./format";
import { connect, getJSON, startReplay, type Target } from "./gateway";
import { Operator } from "./Operator";
import { ReplayBar } from "./Replay";
import { addPreview, apply, dispose, emptySituation, type Filter, latencyS, transmitting } from "./situation";

const CLOCK_MS = 1000;

export type View = "operator" | "command";
export type MainPanel = "video" | "map";

export function App() {
  const [target, setTarget] = useState<Target | null>(null);
  if (!target) return <Pick onPick={setTarget} />;
  return <Live key={JSON.stringify(target)} target={target} onPick={setTarget} onLeave={() => setTarget(null)} />;
}

function Pick({ onPick }: { onPick: (t: Target) => void }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [wid, setWid] = useState("");

  useEffect(() => {
    Promise.all([getJSON<Session[]>("/sessions"), getJSON<Recording[]>("/api/recordings")])
      .then(([s, r]) => {
        setSessions(s);
        setRecordings(r);
      })
      .catch((e: Error) => setError(`Gateway unreachable (${e.message}). Is tools/dev.py running?`));
  }, []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (wid.trim()) onPick({ wid: wid.trim() });
  };
  const replay = (rec: Recording) => {
    setError(null);
    startReplay(rec).then((id) => onPick({ session: id }))
      .catch((e: Error) => setError(`Cannot start the replay (${e.message}).`));
  };

  return (
    <main className="pick">
      <h1>Blade</h1>
      {error && <p className="error">{error}</p>}
      <h2>Sessions</h2>
      {sessions.length === 0 && !error && <p>No sessions on the gateway yet.</p>}
      <ul>
        {sessions.map((s) => (
          <li key={s.id}>
            <button onClick={() => onPick({ session: s.id })}>{s.name}</button> {s.state},{" "}
            {s.streams.map((st) => `${st.sid} replays ${st.recording} in world ${st.wid}`).join(", ")}
          </li>
        ))}
      </ul>
      <h2>Recordings</h2>
      {recordings.length === 0 && !error && <p>No recordings on the server.</p>}
      <ul>
        {recordings.map((r) => (
          <li key={r.id}>
            <button onClick={() => replay(r)}>Replay</button> {recordingName(r)}, drone {r.sid}
            {r.has_truth === false && ", without true positions"}
          </li>
        ))}
      </ul>
      <h2>Or a mission</h2>
      <form onSubmit={submit}>
        <label>mission <input value={wid} placeholder="uplink --mission" onChange={(e) => setWid(e.target.value)} />
        </label>
        <button type="submit">Connect</button>
      </form>
    </main>
  );
}

function Live({ target, onPick, onLeave }: { target: Target; onPick: (t: Target) => void; onLeave: () => void }) {
  const [sit] = useState(emptySituation);
  const [, setTick] = useState(0);
  const [closed, setClosed] = useState<string | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>("operator");
  const [main, setMain] = useState<MainPanel>("video");
  const [picked, setPicked] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>({ hidden: new Set(), stale: true });

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
        if (m.type === "snapshot") {
          getJSON<Alert[]>(`/api/decision/${m.wid}/alerts`)
            .then((alerts) => {
              apply(sit, { type: "alerts", wid: m.wid, data: { alerts } });
              redraw();
            })
            .catch((e: Error) => setError(`Cannot read the active alerts of world ${m.wid} (${e.message}).`));
        }
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
    getJSON<Session[]>("/sessions").then(setSessions)
      .catch((e: Error) => setError(`Cannot list the sessions to switch between Live and Replay (${e.message}).`));
  }, []);

  const now = Date.now() / 1000;
  const sids = [...sit.drones.keys()]; // every drone seen so far, in the order they joined
  const sid = picked && sids.includes(picked) ? picked : (sids[0] ?? null);
  const drone = sid ? sit.drones.get(sid) : undefined;
  const replay = "session" in target; // sessions are replays; live is a mission
  const toReplay = () => {
    const s = sessions.filter((x) => x.state === "running").at(-1);
    if (s) onPick({ session: s.id });
    else onLeave();
  };
  const fps = sid ? sit.diag.get("ingest")?.streams[sid]?.fps_in : undefined;
  const latency = drone ? latencyS(drone) : null;

  return (
    <div className="live">
      <header className="topbar">
        <strong className="brand">Blade</strong>
        <div className="switch" role="group" aria-label="View">
          <button aria-pressed={view === "operator"} onClick={() => setView("operator")}>Operator</button>
          <button aria-pressed={view === "command"} onClick={() => setView("command")}>Command</button>
        </div>
        <label className="muted">
          {replay ? "Session" : "Mission"}{" "}
          <button onClick={onLeave}>{"wid" in target ? target.wid : (sit.session?.name ?? "…")}</button>
        </label>
        <div className="switch" role="group" aria-label="Mode">
          <button aria-pressed={!replay} onClick={() => replay && onLeave()}>
            <span className="dot live-dot" />Live
          </button>
          <button aria-pressed={replay} onClick={() => replay || toReplay()}>
            Replay
          </button>
        </div>
        <span className="muted">Main panel</span>
        <div className="switch" role="group" aria-label="Main panel">
          <button aria-pressed={main === "map"} onClick={() => setMain("map")}>Map</button>
          <button aria-pressed={main === "video"} onClick={() => setMain("video")}>Video</button>
        </div>
        <div className="topbar-right">
          {sid && (
            <span className="chip">
              <span className={drone && transmitting(drone, now) ? "dot ok" : "dot warn"} />
              {sids.length > 1 ? (
                <label>
                  Drone{" "}
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
              latency <span className="mono value">{(latency * 1000).toFixed(0)} ms</span>
            </span>
          )}
          <span className="mono">{new Date().toLocaleTimeString("en-GB")}</span>
        </div>
      </header>
      {closed && <p className="error banner">WebSocket closed: {closed}</p>}
      {error && <p className="error banner">{error}</p>}
      {replay && sit.session && (
        <ReplayBar session={sit.session} onSwitch={(id) => onPick({ session: id })} />
      )}
      {view === "operator"
        ? <Operator sit={sit} sid={sid} main={main} onMain={setMain} onDrone={setPicked} filter={filter}
          onFilter={setFilter} />
        : <Command sit={sit} sids={sids} sid={sid} main={main} onMain={setMain} onDrone={setPicked}
          onOperator={() => setView("operator")} />}
    </div>
  );
}
