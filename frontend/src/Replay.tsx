// The replay bar: the mission (recording) being replayed, position, pause, step, jumps and speed for every replay
// of the session (kept in step).
import { useEffect, useState } from "react";

import type { Recording, Replay, Session } from "./contracts";
import { recordingName } from "./format";
import { getJSON, send, startReplay } from "./gateway";

const POLL_MS = 1000;
const JUMP_S = 10;
const SPEEDS = [0.5, 1, 2, 4];

const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;

type Props = { session: Session; onSwitch: (sessionId: string) => void };

export function ReplayBar({ session, onSwitch }: Props) {
  const sids = session.streams.filter((st) => st.source === "replay").map((st) => st.sid).join(",");
  const [replays, setReplays] = useState<Replay[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<number | null>(null); // the slider position while it is being dragged
  const [recording, setRecording] = useState<Recording | null>(null);
  const [missions, setMissions] = useState<Recording[] | null>(null); // the open list of recordings to switch to

  useEffect(() => {
    let stopped = false;
    const mine = sids.split(",");
    const poll = () => getJSON<Replay[]>("/api/replays")
      .then((all) => {
        if (!stopped) setReplays(all.filter((r) => mine.includes(r.sid)));
      })
      .catch((e: Error) => setError(`Cannot read the replay state (${e.message}).`));
    poll();
    const timer = setInterval(poll, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [sids]);

  const recordingId = replays[0]?.recording;
  useEffect(() => {
    if (!recordingId) return;
    getJSON<Recording>(`/api/recordings/${recordingId}`).then(setRecording)
      .catch((e: Error) => setError(`Cannot read the recording ${recordingId} (${e.message}).`));
  }, [recordingId]);

  const first = replays[0];
  if (!first) {
    return <div className="replaybar muted">Replay {sids}: waiting for its state.{error && ` ${error}`}</div>;
  }

  const control = (body: (r: Replay) => object) => {
    setError(null);
    Promise.all(replays.map((r) => send<Replay>("PATCH", `/sessions/${session.id}/replays/${r.sid}`, body(r))))
      .then(setReplays) // the answer is the new state: the buttons change without waiting for the next poll
      .catch((e: Error) => setError(`Replay control failed (${e.message}).`));
  };
  const jump = (delta: number) => {
    control((r) => ({ seek_s: Math.max(0, r.position_s + delta) }));
  };
  const commit = () => {
    if (draft === null) return;
    jump(draft - first.position_s);
    setDraft(null);
  };
  const position = draft ?? first.position_s;
  const toggleMissions = () => {
    if (missions) {
      setMissions(null);
    } else {
      getJSON<Recording[]>("/api/recordings").then(setMissions)
        .catch((e: Error) => setError(`Cannot list the recordings (${e.message}).`));
    }
  };
  // a new session for the chosen recording; this one is stopped so only one replay keeps the detector busy
  const switchTo = (rec: Recording) => {
    setMissions(null);
    startReplay(rec)
      .then(async (id) => {
        await send("POST", `/sessions/${session.id}/stop`);
        onSwitch(id);
      })
      .catch((e: Error) => setError(`Cannot switch to ${rec.scene ?? rec.id} (${e.message}).`));
  };

  return (
    <div className="replaybar">
      <div className="missions">
        <span className="muted">Recording </span>
        <button aria-expanded={missions !== null} onClick={toggleMissions}>
          {recording ? recordingName(recording) : first.recording}
        </button>
        {missions && (
          <ul className="mission-list" aria-label="Recordings">
            {missions.map((r) => (
              <li key={r.id}>
                <button disabled={r.id === first.recording} onClick={() => switchTo(r)}>
                  {recordingName(r)}, drone {r.sid}{r.id === first.recording && " (playing)"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="replay-buttons">
        <button onClick={() => jump(-JUMP_S)}>Back {JUMP_S} s</button>
        <button className="primary" onClick={() => control(() => ({ paused: !first.paused }))}>
          {first.paused ? "Play" : "Pause"}
        </button>
        <button onClick={() => jump(JUMP_S)}>Forward {JUMP_S} s</button>
        {first.paused && <button onClick={() => control(() => ({ step: 1 }))}>Next frame</button>}
      </div>
      <span className="mono nowrap">{mmss(position)} of {mmss(first.duration_s)}</span>
      <input className="timeline" type="range" min="0" max={first.duration_s} step="0.1" value={position}
        aria-label="Position in the recording" onChange={(e) => setDraft(Number(e.target.value))} onPointerUp={commit}
        onKeyUp={commit} />
      <div className="switch" role="group" aria-label="Speed">
        {SPEEDS.map((v) => (
          <button key={v} aria-pressed={first.speed === v} onClick={() => control(() => ({ speed: v }))}>
            ×{v}
          </button>
        ))}
      </div>
      <label className="inline muted"><input type="checkbox" checked={first.loop} disabled />Loop</label>
      {error && <span className="error">{error}</span>}
    </div>
  );
}
