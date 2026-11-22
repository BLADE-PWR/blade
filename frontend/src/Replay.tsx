// The replay bar: recording, position, pause, step, jumps and speed. One replay plays every drone of a mission
// recording on one clock; the bar drives every replay of the session together.
import { useEffect, useRef, useState } from "react";

import type { Recording, Replay, Session } from "./contracts";
import { recordingName } from "./format";
import { getJSON, send, startReplay } from "./gateway";

const POLL_MS = 1000;
const JUMP_S = 10;
const SPEEDS = [0.5, 1, 2, 4];
const JOIN_SLACK_S = 0.5; // a drone starting this soon after a jump's target counts as playing there

/** The drones of r with a segment at t (or starting just after it). */
const playingAt = (r: Replay, t: number) =>
  r.drones.filter((d) => d.segments.some(([a, b]) => t >= a - JOIN_SLACK_S && t <= b)).map((d) => d.sid);

const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;

type Props = {
  session: Session;
  onSwitch: (sessionId: string) => void;
  onSeek: (deltaS: number, playing: Set<string>) => void; // playing: the drones with a segment at the target
  onState: (replays: Replay[]) => void;
};

export function ReplayBar({ session, onSwitch, onSeek, onState }: Props) {
  /** Note how the replays are now; a jump this tab did not see coming (a loop, a seek from another tab, Play at the
   * end starting over) clears what the drones show, like a jump made here. */
  const update = (rs: Replay[], ownJump = false) => {
    for (const r of rs) {
      const last = seen.current.get(r.wid);
      if (!ownJump && last && last.generation !== r.generation) {
        seekRef.current(r.position_s - last.position, new Set(r.paused ? [] : playingAt(r, r.position_s)));
      }
      seen.current.set(r.wid, { generation: r.generation, position: r.position_s });
    }
  };
  const wids = session.replays.map((r) => r.wid).join(",");
  const [replays, setReplays] = useState<Replay[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<number | null>(null); // while the slider is dragged
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const seen = useRef(new Map<string, { generation: number; position: number }>()); // wid -> as last read
  const seekRef = useRef(onSeek);
  seekRef.current = onSeek; // the latest: it knows every drone shown now

  useEffect(() => {
    let stopped = false;
    const mine = wids.split(",");
    const poll = () => getJSON<Replay[]>("/api/replays")
      .then((all) => {
        if (stopped) return;
        const rs = all.filter((r) => mine.includes(r.wid));
        update(rs);
        setReplays(rs);
      })
      .catch((e: Error) => setError(`Cannot read the replay state (${e.message}).`));
    poll();
    const timer = setInterval(poll, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [wids]);

  useEffect(() => onState(replays), [replays, onState]);

  useEffect(() => {
    getJSON<Recording[]>("/api/recordings").then(setRecordings)
      .catch((e: Error) => setError(`Cannot list the recordings (${e.message}).`));
  }, []);

  const first = replays[0];
  if (!first) {
    return <div className="replaybar muted">Replay {wids}: waiting for its state.{error && ` ${error}`}</div>;
  }

  const control = (body: (r: Replay) => object, ownJump = false) => {
    setError(null);
    return Promise.all(replays.map((r) => send<Replay>("PATCH", `/sessions/${session.id}/replays/${r.wid}`,
      body(r))))
      .then((rs) => {
        update(rs, ownJump);
        setReplays(rs);
      })
      .catch((e: Error) => setError(`Replay control failed (${e.message}).`));
  };
  const jump = (delta: number) => {
    const done = control((r) => ({ seek_s: Math.max(0, r.position_s + delta) }), true);
    const to = Math.min(Math.max(0, first.position_s + delta), first.duration_s);
    const playing = new Set(first.paused ? [] : replays.flatMap( // paused, no frame comes until Play
      (r) => playingAt(r, Math.min(Math.max(0, r.position_s + delta), r.duration_s))));
    onSeek(to - first.position_s, playing);
    return done;
  };
  const commit = (value: number) => {
    void jump(value - first.position_s).finally(() => setDraft(null)); // until the reply, show where it goes
  };
  const position = draft ?? first.position_s;
  const switchTo = (rec: Recording) => {
    startReplay(rec)
      .then(async (id) => {
        if (id === session.id) return; // already playing it
        await send("POST", `/sessions/${session.id}/stop`);
        onSwitch(id);
      })
      .catch((e: Error) => setError(`Cannot switch to ${rec.scene ?? rec.id} (${e.message}).`));
  };

  return (
    <div className="replaybar">
      <label>
        <span className="muted">Recording </span>
        <select value={first.recording} onChange={(e) => switchTo(recordings.find((r) => r.id === e.target.value)!)}>
          {!recordings.some((r) => r.id === first.recording) && (
            <option value={first.recording}>{first.recording}</option>
          )}
          {recordings.map((r) => (
            <option key={r.id} value={r.id} disabled={r.recording}>{recordingName(r)}</option>
          ))}
        </select>
      </label>
      <div className="replay-buttons">
        <button onClick={() => jump(-JUMP_S)}>Back {JUMP_S} s</button>
        <button className="primary" onClick={() => control(() => ({ paused: !first.paused }))}>
          {first.paused ? "Play" : "Pause"}
        </button>
        <button onClick={() => jump(JUMP_S)}>Forward {JUMP_S} s</button>
        {first.paused && <button onClick={() => control(() => ({ step: 1 }))}>Next frame</button>}
      </div>
      <span className="mono nowrap">{mmss(position)} of {mmss(first.duration_s)}</span>
      <input className="timeline" type="range" min="0" max={first.duration_s} step="any" value={position}
        aria-label="Position in the recording" onChange={(e) => setDraft(Number(e.target.value))}
        ref={(el) => { // the native change event comes once, when the value is let go (React's onChange is "input")
          if (!el) return;
          const done = () => commit(Number(el.value));
          el.addEventListener("change", done);
          return () => el.removeEventListener("change", done);
        }} />
      <div className="switch" role="group" aria-label="Speed">
        {SPEEDS.map((v) => (
          <button key={v} aria-pressed={first.speed === v} onClick={() => control(() => ({ speed: v }))}>
            ×{v}
          </button>
        ))}
      </div>
      <label className="inline">
        <input type="checkbox" checked={first.loop} onChange={(e) => void control(() => ({ loop: e.target.checked }))} />
        Loop
      </label>
      {error && <span className="error">{error}</span>}
    </div>
  );
}
