// Times as text.

export const clock = (t: number) => new Date(t * 1000).toLocaleTimeString("en-GB");

export function ago(t: number, now: number): string {
  const s = Math.max(0, now - t);
  if (s < 10) return `${s.toFixed(1)} s ago`;
  if (s < 120) return `${s.toFixed(0)} s ago`;
  return `${(s / 60).toFixed(0)} min ago`;
}
