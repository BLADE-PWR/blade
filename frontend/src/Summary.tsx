// The Summary tab: the situation of one world in three sentences, from what the browser already has.
import { nameOf } from "./classes";
import type { Cluster, Vec2 } from "./contracts";
import type { WorldView } from "./situation";

const RECENT_S = 300;
const MOVING_MPS = 1;
const DIRECTIONS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];

const speed = (v: Vec2) => Math.hypot(v[0], v[1]);
const moving = (v: Vec2 | null | undefined): v is Vec2 => !!v && speed(v) >= MOVING_MPS;
// x is east and y north, so atan2(x, y) is the bearing: 0 deg north, 90 east
const direction = (v: Vec2) => DIRECTIONS[Math.round((((Math.atan2(v[0], v[1]) * 180) / Math.PI + 360) % 360) / 45) % 8];
const count = (n: number, name: string) => `${n} ${name}${n === 1 ? "" : "s"}`;

function largestGroup(w: WorldView): string {
  const g = w.clusters.reduce<Cluster | null>((a, b) => (!a || b.objects.length > a.objects.length ? b : a), null);
  if (!g) return "No groups.";
  const what = Object.entries(g.composition).map(([grp, n]) => count(n, nameOf(grp))).join(", ");
  const move = moving(g.vel) ? `moving ${direction(g.vel)} at ${(speed(g.vel) * 3.6).toFixed(0)} km/h` : "not moving";
  return `${count(g.objects.length, "object")} (${what}), ${move}.`;
}

function mainMovement(w: WorldView): string {
  const active = [...w.objects.values()].filter((o) => o.state === "active");
  const going = active.flatMap((o) => (moving(o.vel) ? [direction(o.vel)] : []));
  if (active.length === 0) return "No objects seen now.";
  if (going.length === 0) return `${count(active.length, "object")} seen now, none moving.`;
  const per = new Map<string, number>();
  for (const d of going) per.set(d, (per.get(d) ?? 0) + 1);
  const [dir, n] = [...per].sort((a, b) => b[1] - a[1])[0];
  return `${going.length} of ${count(active.length, "object")} moving, ${n} of them heading ${dir}.`;
}

function recentChanges(w: WorldView, now: number): string {
  const since = now - RECENT_S;
  const objects = [...w.objects.values()];
  return [
    count(objects.filter((o) => o.first >= since).length, "new object"),
    `${objects.filter((o) => o.state === "stale" && o.last >= since).length} lost from view`,
    count([...w.events.values()].filter((e) => e.t >= since).length, "event"),
    count([...w.alerts.values()].filter((a) => a.t >= since).length, "new alert"),
  ].join(", ") + ".";
}

export function Summary({ world, now }: { world: WorldView; now: number }) {
  return (
    <dl className="summary">
      <dt>Largest group</dt>
      <dd>{largestGroup(world)}</dd>
      <dt>Main movement</dt>
      <dd>{mainMovement(world)}</dd>
      <dt>Last {RECENT_S / 60} minutes</dt>
      <dd>{recentChanges(world, now)}</dd>
    </dl>
  );
}
