// Numbers and times as Polish text.

export const pl = (n: number, digits = 0) => n.toFixed(digits).replace(".", ",");

export const clock = (t: number) => new Date(t * 1000).toLocaleTimeString("pl-PL");

export function ago(t: number, now: number): string {
  const s = Math.max(0, now - t);
  if (s < 10) return `${pl(s, 1)} s temu`;
  if (s < 120) return `${s.toFixed(0)} s temu`;
  return `${(s / 60).toFixed(0)} min temu`;
}
