// The start page, the live connection and the top bar shared by both views.
import { type FormEvent, useEffect, useState } from "react";

import { Command } from "./Command";
import type { Alert, LiveMission, Recording, Replay, Session } from "./contracts";
import { recordingName } from "./format";
import { connect, getJSON, send, startReplay, type Target } from "./gateway";
import { Operator } from "./Operator";
import { RecordingSelect, ReplayBar } from "./Replay";
import {
  addPreview, apply, dispose, droneName, emptySituation, type Filter, latencyS, seek, transmitting,
} from "./situation";

const CLOCK_MS = 1000;
const MISSIONS_POLL_MS = 3000;

export type View = "operator" | "command";
export type MainPanel = "video" | "map";

export function App() {
  const [target, setTarget] = useState<Target | null>(null);
  // kept across missions, recordings and Live / Replay, which each start a new Live
  const [view, setView] = useState<View>("operator");
  const [main, setMain] = useState<MainPanel>("video");
  if (!target) return <Pick onPick={setTarget} />;
  return <Live key={JSON.stringify(target)} target={target} onPick={setTarget} onLeave={() => setTarget(null)}
    view={view} onView={setView} main={main} onMain={setMain} />;
}

/** The live missions, refreshed while shown; current: the one watched (also when it is not live, or none yet). */
function MissionSelect({ current, onPick }: { current: string | null; onPick: (mission: string) => void }) {
  const [missions, setMissions] = useState<LiveMission[]>([]);

  useEffect(() => {
    const live = () => getJSON<LiveMission[]>("/api/missions").then(setMissions).catch(() => setMissions([]));
    live();
    const timer = setInterval(live, MISSIONS_POLL_MS);
    return () => clearInterval(timer);
  }, []);

  const names = missions.map((m) => m.mission);
  return (
    <label className="muted">
      Mission{" "}
      <select value={current ?? ""} onChange={(e) => onPick(e.target.value)}>
        {current === null && <option value="" disabled>{names.length ? "choose a mission" : "no mission is live"}</option>}
        {current !== null && !names.includes(current) && <option value={current}>{current} (not live)</option>}
        {missions.map((m) => (
          <option key={m.mission} value={m.mission}>
            {m.mission} ({m.worlds.map((w) => w.map).join(", ")})
          </option>
        ))}
      </select>
    </label>
  );
}

function Pick({ onPick }: { onPick: (t: Target) => void }) {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [missions, setMissions] = useState<LiveMission[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [mission, setMission] = useState("");

  useEffect(() => {
    Promise.all([getJSON<Session[]>("/sessions"), getJSON<Recording[]>("/api/recordings")])
      .then(([s, r]) => {
        setSessions(s);
        setRecordings(r);
      })
      .catch((e: Error) => setError(`Gateway unreachable (${e.message}). Is tools/dev.py running?`));
    const live = () => getJSON<LiveMission[]>("/api/missions").then(setMissions).catch(() => setMissions([]));
    live();
    const timer = setInterval(live, MISSIONS_POLL_MS); // missions start and end while the picker is open
    return () => clearInterval(timer);
  }, []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (mission.trim()) onPick({ mission: mission.trim() });
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
      <h2>Live missions</h2>
      {missions.length === 0 && !error && <p>No drone is flying now.</p>}
      <ul>
        {missions.map((m) => (
          <li key={m.mission}>
            <button onClick={() => onPick({ mission: m.mission })}>Watch</button> {m.mission}:{" "}
            {m.worlds.map((w) => `${w.map} (${Object.values(w.drones).join(", ")})`).join("; ")}
          </li>
        ))}
      </ul>
      <form onSubmit={submit}>
        <label>or a mission by name <input value={mission} placeholder="uplink --mission"
          onChange={(e) => setMission(e.target.value)} />
        </label>
        <button type="submit">Connect</button>{" "}
        <span className="muted">(also before its first drone flies)</span>
      </form>
      <h2>Sessions</h2>
      {sessions.length === 0 && !error && <p>No sessions on the gateway yet.</p>}
      <ul>
        {sessions.map((s) => (
          <li key={s.id}>
            <button onClick={() => onPick({ session: s.id })}>{s.name}</button> {s.state},{" "}
            {s.replays.map((r) => `${r.recording}: ${Object.keys(r.sids).join(", ")}`).join("; ")}
          </li>
        ))}
      </ul>
      <h2>Recordings</h2>
      {recordings.length === 0 && !error && <p>No recordings on the server.</p>}
      <ul>
        {recordings.map((r) => (
          <li key={r.id}>
            <button onClick={() => replay(r)} disabled={r.recording}
              title={r.recording ? "Still recording: it can be replayed once it has ended" : undefined}>
              Replay
            </button>{" "}
            {recordingName(r)}
            {r.has_truth === false && ", without true positions"}
          </li>
        ))}
      </ul>
    </main>
  );
}

type LiveProps = {
  target: Target;
  onPick: (t: Target) => void;
  onLeave: () => void;
  view: View;
  onView: (v: View) => void;
  main: MainPanel;
  onMain: (p: MainPanel) => void;
};

function Live({ target, onPick, onLeave, view, onView: setView, main, onMain: setMain }: LiveProps) {
  const [sit] = useState(emptySituation);
  const [, setTick] = useState(0);
  const [closed, setClosed] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>({ hidden: new Set(), stale: true });
  const [replays, setReplays] = useState<Replay[]>([]); // the session's replays, as the replay bar last read them

  useEffect(() => {
    let frame = 0;
    const redraw = () => {
      if (!frame) frame = requestAnimationFrame(() => {
        frame = 0;
        setTick((t) => t + 1);
      });
    };
    const stop = "choose" in target ? () => {} : connect(target, { // nothing chosen yet: nothing to watch
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

  const now = Date.now() / 1000;
  // every drone seen so far, in the order they joined; in a replay not the ones that join only later on its timeline
  const all = [...sit.drones.keys()];
  const notYet = (s: string) => !sit.drones.get(s)?.previews.length && replays.some((r) =>
    r.drones.some((d) => d.sid === s && d.start_s > r.position_s + 0.5));
  const sids = all.filter((s) => !notYet(s));
  const sid = picked && sids.includes(picked) ? picked : (sids[0] ?? null);
  const drone = sid ? sit.drones.get(sid) : undefined;
  const replay = "session" in target || ("choose" in target && target.choose === "replay"); // live: a mission
  const startRecording = (rec: Recording) => {
    setError(null);
    startReplay(rec).then((id) => onPick({ session: id }))
      .catch((e: Error) => setError(`Cannot start the replay (${e.message}).`));
  };
  const stopMission = (mission: string) => {
    if (!window.confirm(`Stop mission ${mission}? Its map, objects and alerts are cleared and its recordings saved. `
      + "Drones still flying start it afresh.")) return;
    send("POST", `/api/missions/${encodeURIComponent(mission)}/stop`)
      .catch((e: Error) => setError(`Cannot stop mission ${mission} (${e.message}).`));
  };
  const fps = sid ? sit.diag.get("ingest")?.streams[sid]?.fps_in : undefined;
  const latency = drone ? latencyS(drone) : null;

  return (
    <div className="live">
      <header className="topbar">
        <button className="brand" onClick={onLeave} title="Back to the start page">Blade</button>
        <div className="switch" role="group" aria-label="View">
          <button aria-pressed={view === "operator"} onClick={() => setView("operator")}>Operator</button>
          <button aria-pressed={view === "command"} onClick={() => setView("command")}>Command</button>
        </div>
        <div className="switch" role="group" aria-label="Main panel">
          <button aria-pressed={main === "map"} onClick={() => setMain("map")}>Map</button>
          <button aria-pressed={main === "video"} onClick={() => setMain("video")}>Video</button>
        </div>
        <div className="switch" role="group" aria-label="Mode">
          <button aria-pressed={!replay} onClick={() => replay && onPick({ choose: "live" })}>
            <span className="dot live-dot" />Live
          </button>
          <button aria-pressed={replay} onClick={() => replay || onPick({ choose: "replay" })}>
            Replay
          </button>
        </div>
        {!replay && (
          <MissionSelect current={"mission" in target ? target.mission : null}
            onPick={(mission) => onPick({ mission })} />
        )}
        {view === "command" && "mission" in target && (
          <button className="danger" onClick={() => stopMission(target.mission)}>Stop mission</button>
        )}
        <div className="topbar-right">
          {sid && (
            <span className="chip">
              <span className={drone && transmitting(drone, now) ? "dot ok" : "dot warn"} />
              {sids.length > 1 ? (
                <label>
                  Drone{" "}
                  <select value={sid} onChange={(e) => setPicked(e.target.value)}>
                    {sids.map((s) => <option key={s} value={s}>{droneName(sit, s)}</option>)}
                  </select>
                </label>
              ) : (
                <span className="mono">{droneName(sit, sid)}</span>
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
        <ReplayBar session={sit.session} onSwitch={(id) => onPick({ session: id })}
          onSeek={(delta, playing) => seek(sit, all, delta, playing)} onState={setReplays} />
      )}
      {"choose" in target && (
        <div className="replaybar">
          {target.choose === "replay"
            ? <RecordingSelect onPick={startRecording} />
            : <span className="muted">Choose a live mission in the top bar.</span>}
        </div>
      )}
      {view === "operator"
        ? <Operator sit={sit} sid={sid} main={main} onMain={setMain} onDrone={setPicked} filter={filter}
          onFilter={setFilter} />
        : <Command sit={sit} sids={sids} sid={sid} main={main} onDrone={setPicked}
          onOperator={() => setView("operator")} />}
    </div>
  );
}
