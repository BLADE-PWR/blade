// Times and recordings as text.
import type { Recording } from "./contracts";

export const clock = (t: number) => new Date(t * 1000).toLocaleTimeString("en-GB");

export function ago(t: number, now: number): string {
  const s = Math.max(0, now - t);
  if (s < 10) return `${s.toFixed(1)} s ago`;
  if (s < 120) return `${s.toFixed(0)} s ago`;
  return `${(s / 60).toFixed(0)} min ago`;
}

export const recordingName = (r: Recording) =>
  `${r.scene ?? r.id}, ${new Date(r.started * 1000).toLocaleDateString("en-CA")} ${clock(r.started).slice(0, 5)}, ` +
  (r.duration_s == null ? "still recording" : `${r.duration_s.toFixed(0)} s`);
