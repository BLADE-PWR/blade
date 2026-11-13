// The replay bar: position, pause, step, jumps and speed for every replay of the session (kept in step).
import { useEffect, useState } from "react";

import type { Replay, Session } from "./contracts";
import { pl } from "./format";
import { getJSON, patchJSON } from "./gateway";

const POLL_MS = 1000;
const JUMP_S = 10;
const SPEEDS = [0.5, 1, 2, 4];
const REBUILD_NOTE_MS = 5000; // how long the "picture rebuilds" note stays after a jump

const mmss = (s: number) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${pl(s % 60, 1).padStart(4, "0")}`;

export function ReplayBar({ session, onJump }: { session: Session; onJump: () => void }) {
  const sids = session.streams.filter((st) => st.source === "replay").map((st) => st.sid).join(",");
  const [replays, setReplays] = useState<Replay[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<number | null>(null); // the slider position while it is being dragged
  const [jumpedAt, setJumpedAt] = useState(0);

  useEffect(() => {
    let stopped = false;
    const mine = sids.split(",");
    const poll = () => getJSON<Replay[]>("/api/replays")
      .then((all) => {
        if (!stopped) setReplays(all.filter((r) => mine.includes(r.sid)));
      })
      .catch((e: Error) => setError(`Nie można odczytać stanu powtórki (${e.message}).`));
    poll();
    const timer = setInterval(poll, POLL_MS);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [sids]);

  const first = replays[0];
  if (!first) {
    return <div className="replaybar muted">Powtórka {sids}: czekam na jej stan.{error && ` ${error}`}</div>;
  }

  const control = (body: (r: Replay) => object) => {
    setError(null);
    Promise.all(replays.map((r) => patchJSON(`/sessions/${session.id}/replays/${r.sid}`, body(r))))
      .catch((e: Error) => setError(`Sterowanie powtórką nie zadziałało (${e.message}).`));
  };
  const jump = (delta: number) => {
    control((r) => ({ seek_s: Math.max(0, r.position_s + delta) }));
    onJump();
    setJumpedAt(Date.now());
  };
  const commit = () => {
    if (draft === null) return;
    jump(draft - first.position_s);
    setDraft(null);
  };
  const position = draft ?? first.position_s;

  return (
    <div className="replaybar">
      <span className="muted">Nagranie <span className="mono value">{first.recording}</span></span>
      <div className="replay-buttons">
        <button onClick={() => jump(-JUMP_S)}>{JUMP_S} s wstecz</button>
        <button className="primary" onClick={() => control(() => ({ paused: !first.paused }))}>
          {first.paused ? "Odtwarzaj" : "Pauza"}
        </button>
        <button onClick={() => jump(JUMP_S)}>{JUMP_S} s naprzód</button>
        {first.paused && <button onClick={() => control(() => ({ step: 1 }))}>Następna klatka</button>}
      </div>
      <span className="mono nowrap">{mmss(position)} z {mmss(first.duration_s)}</span>
      <input className="timeline" type="range" min="0" max={first.duration_s} step="0.1" value={position}
        aria-label="Pozycja w nagraniu" onChange={(e) => setDraft(Number(e.target.value))} onPointerUp={commit}
        onKeyUp={commit} />
      <div className="switch" role="group" aria-label="Prędkość">
        {SPEEDS.map((v) => (
          <button key={v} aria-pressed={first.speed === v} onClick={() => control(() => ({ speed: v }))}>
            ×{pl(v, v % 1 ? 1 : 0)}
          </button>
        ))}
      </div>
      <label className="inline muted"><input type="checkbox" checked={first.loop} disabled />Pętla</label>
      {Date.now() - jumpedAt < REBUILD_NOTE_MS && (
        <span className="note">Obraz sytuacji odbudowuje się od tej chwili. Obiekty sprzed skoku wyczyszczone.</span>
      )}
      {error && <span className="error">{error}</span>}
    </div>
  );
}
