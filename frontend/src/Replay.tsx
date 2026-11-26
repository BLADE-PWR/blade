// The replay bar: recording, position, pause, step, jumps and speed. One replay plays every drone of a mission
// recording on one clock; the bar drives every replay of the session together.
import { useEffect, useRef, useState } from "react";

import type { Recording, Replay, Session } from "./contracts";
import { clock, day, recordingName } from "./format";
import { getJSON, send, startReplay } from "./gateway";

const POLL_MS = 1000;
const TICK_MS = 100; // the slider and the clock move this often while playing, between two reads of the state
const JUMP_S = 10;
const SPEEDS = [0.5, 1, 2, 4];
const JOIN_SLACK_S = 0.5; // a drone starting this soon after a jump's target counts as playing there

/** The drones of r with a segment at t (or starting just after it). */
const playingAt = (r: Replay, t: number) =>
  r.drones.filter((d) => d.segments.some(([a, b]) => t >= a - JOIN_SLACK_S && t <= b)).map((d) => d.sid);

/** Keys typed here belong to the element, not to the replay shortcuts (the map pans with the arrows). */
const ownsKeys = (t: EventTarget | null) => t instanceof HTMLElement && (t.isContentEditable
  || !!t.closest("select, textarea, .leaflet-container")
  || (t instanceof HTMLInputElement && !["range", "checkbox"].includes(t.type)));

const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${(s % 60).toFixed(1).padStart(4, "0")}`;

type Props = {
  session: Session;
  onSwitch: (sessionId: string) => void;
  onSeek: (deltaS: number, playing: Set<string>) => void; // playing: the drones with a segment at the target
  onState: (replays: Replay[]) => void;
  onGone: () => void; // the session has no replay any more (stopped, deleted, or the server restarted)
};

export function ReplayBar({ session, onSwitch, onSeek, onState, onGone }: Props) {
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
  const seen = useRef(new Map<string, { generation: number; position: number }>()); // wid -> as last read
  const readAt = useRef(0); // performance.now() when replays was last read
  const [, setTick] = useState(0);
  const keys = useRef<Record<string, () => unknown> | null>(null); // the shortcuts, with this render's state
  const seekRef = useRef(onSeek);
  seekRef.current = onSeek; // the latest: it knows every drone shown now
  const goneRef = useRef(onGone);
  goneRef.current = onGone;

  useEffect(() => {
    let stopped = false;
    const mine = wids.split(",");
    const poll = () => getJSON<Replay[]>("/api/replays")
      .then((all) => {
        if (stopped) return;
        const rs = all.filter((r) => mine.includes(r.wid));
        if (rs.length === 0) {
          goneRef.current();
          return;
        }
        update(rs);
        readAt.current = performance.now();
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

  useEffect(() => { // space: play / pause, ".": next frame, left / right: 10 s
    const down = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.altKey || e.metaKey || ownsKeys(e.target)) return;
      const act = keys.current?.[e.key];
      if (!act) return;
      e.preventDefault(); // no page scroll, no slider step
      if (!e.repeat) act(); // held down: once
    };
    const up = (e: KeyboardEvent) => { // a focused button would also click itself on space
      if (e.key === " " && !ownsKeys(e.target)) e.preventDefault();
    };
    addEventListener("keydown", down);
    addEventListener("keyup", up);
    return () => {
      removeEventListener("keydown", down);
      removeEventListener("keyup", up);
    };
  }, []);

  const playing = replays.length > 0 && !replays[0].paused;
  useEffect(() => { // redraw between reads, so the slider and the clock move smoothly
    if (!playing) return;
    const timer = setInterval(() => setTick((t) => t + 1), TICK_MS);
    return () => clearInterval(timer);
  }, [playing]);

  const first = replays[0];
  keys.current = null;
  if (!first) {
    return <div className="replaybar muted">Replay {wids}: waiting for its state.{error && ` ${error}`}</div>;
  }

  const control = (body: (r: Replay) => object, ownJump = false) => {
    setError(null);
    return Promise.all(replays.map((r) => send<Replay>("PATCH", `/sessions/${session.id}/replays/${r.wid}`,
      body(r))))
      .then((rs) => {
        update(rs, ownJump);
        readAt.current = performance.now();
        setReplays(rs);
      })
      .catch((e: Error) => setError(`Replay control failed (${e.message}).`));
  };
  /** Where r is now: as last read, moved on by the time since at its speed while playing (the next read corrects it). */
  const now = (r: Replay) => r.paused ? r.position_s
    : Math.min(r.position_s + ((performance.now() - readAt.current) / 1000) * r.speed, r.duration_s);
  const jump = (delta: number) => {
    const done = control((r) => ({ seek_s: Math.max(0, now(r) + delta) }), true);
    const to = Math.min(Math.max(0, now(first) + delta), first.duration_s);
    const there = new Set(first.paused ? [] : replays.flatMap( // paused, no frame comes until Play
      (r) => playingAt(r, Math.min(Math.max(0, now(r) + delta), r.duration_s))));
    onSeek(to - now(first), there);
    return done;
  };
  const commit = (value: number) => {
    void jump(value - now(first)).finally(() => setDraft(null)); // until the reply, show where it goes
  };
  const position = draft ?? now(first);
  // a new session of its own; the one left is not stopped (others may watch it): the gateway pauses it when nobody
  // does, and deletes it after a while
  const switchTo = (rec: Recording) => {
    startReplay(rec)
      .then(onSwitch)
      .catch((e: Error) => setError(`Cannot switch to ${rec.scene ?? rec.id} (${e.message}).`));
  };

  const playPause = () => control(() => ({ paused: !first.paused }));
  const nextFrame = () => control(() => ({ step: 1 })); // while playing too: a step pauses first
  keys.current = { " ": playPause, ".": nextFrame, ArrowLeft: () => jump(-JUMP_S), ArrowRight: () => jump(JUMP_S) };

  return (
    <div className="replaybar">
      <RecordingSelect current={first.recording} onPick={switchTo} />
      <div className="replay-buttons">
        <button onClick={() => jump(-JUMP_S)} title={`Back ${JUMP_S} s (left arrow)`}>« {JUMP_S} s</button>
        <button className="primary play" onClick={playPause} title="Play / pause (space)">
          {first.paused ? "Play" : "Pause"}
        </button>
        <button onClick={nextFrame} title="Next frame, pausing (.)">Frame ›</button>
        <button onClick={() => jump(JUMP_S)} title={`Forward ${JUMP_S} s (right arrow)`}>{JUMP_S} s »</button>
      </div>
      <span className="mono nowrap">{mmss(position)} of {mmss(first.duration_s)}</span>
      {first.started != null && (
        <span className="topbar-clock mono" title="Real time of this moment of the recording">
          <span className="muted">{day(first.started + position)}</span>
          <span>{clock(first.started + position)}</span>
        </span>
      )}
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

/** The recordings to replay; current: the one playing (none when Replay was just switched to). */
export function RecordingSelect({ current, onPick }: { current?: string; onPick: (rec: Recording) => void }) {
  const [recordings, setRecordings] = useState<Recording[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getJSON<Recording[]>("/api/recordings").then(setRecordings)
      .catch((e: Error) => setError(`Cannot list the recordings (${e.message}).`));
  }, []);

  return (
    <label>
      <span className="muted">Recording </span>
      <select value={current ?? ""} onChange={(e) => onPick(recordings.find((r) => r.id === e.target.value)!)}>
        {!current && <option value="" disabled>{recordings.length ? "choose a recording" : "no recordings"}</option>}
        {current && !recordings.some((r) => r.id === current) && <option value={current}>{current}</option>}
        {recordings.map((r) => (
          <option key={r.id} value={r.id} disabled={r.recording}>{recordingName(r)}</option>
        ))}
      </select>
      {error && <span className="error"> {error}</span>}
    </label>
  );
}
