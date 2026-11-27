// The alert, object and timeline lists (tabs in view 1, the alert list in view 2).
import { colorOf, nameOf, SEVERITY_COLORS } from "./classes";
import type { Alert, Vec2, WorldEvent, WorldObject } from "./contracts";
import { ago, clock } from "./format";
import { symbolUrl } from "./MapView";
import type { ShownAlert } from "./situation";

const SEVERITY_NAMES: Record<Alert["severity"], string> = {
  warning: "Warning",
  notice: "Notice",
  info: "Info",
};

function SeverityIcon({ severity }: { severity: Alert["severity"] }) {
  const color = SEVERITY_COLORS[severity];
  return (
    <svg className="severity" width="16" height="16" viewBox="0 0 16 16" role="img"
      aria-label={SEVERITY_NAMES[severity]}>
      {severity === "warning" && <path d="M8 1.5 L15 14 H1 Z" fill={color} />}
      {severity === "notice" && <circle cx="8" cy="8" r="6.5" fill={color} />}
      {severity === "info" && <circle cx="8" cy="8" r="6.5" fill="none" stroke={color} strokeWidth="1.5" />}
      <path d={severity === "info" ? "M8 7 V11.5 M8 4.5 V5" : "M8 5 V9 M8 11 V11.5"}
        stroke={severity === "info" ? color : "#0d1015"} strokeWidth="1.6" />
    </svg>
  );
}

type AlertProps = { alerts: ShownAlert[]; picked: string | null; onPick: (a: ShownAlert) => void; now: number };

export function AlertList({ alerts, picked, onPick, now }: AlertProps) {
  if (alerts.length === 0) return <p className="muted hint">No active alerts.</p>;
  return (
    <ul className="rows">
      {alerts.map((a) => (
        <li key={a.key}>
          <button className="row" aria-pressed={a.key === picked} onClick={() => onPick(a)}>
            <SeverityIcon severity={a.severity} />
            <span className="row-text">
              <strong>{a.title}</strong>
              <span className="muted">{a.explanation}</span>
            </span>
            <span className="mono muted push nowrap">{ago(a.t, now)}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

type ObjectProps = { objects: WorldObject[]; selected: string | null; onSelect: (oid: string) => void; now: number };

export function ObjectList({ objects, selected, onSelect, now }: ObjectProps) {
  if (objects.length === 0) return <p className="muted hint">No objects.</p>;
  const sorted = [...objects].sort((a, b) => a.state.localeCompare(b.state) || a.oid.localeCompare(b.oid));
  return (
    <ul className="rows">
      {sorted.map((o) => (
        <li key={o.oid}>
          <button className={o.state === "stale" ? "row stale" : "row"} aria-pressed={o.oid === selected}
            onClick={() => onSelect(o.oid)}>
            <img src={symbolUrl(o.grp)} alt="" />
            <span className="row-text">
              <strong>{nameOf(o.grp)} <span className="mono muted">{o.oid}</span></strong>
              <span className="muted">
                {o.state === "active" ? "active" : "not seen now"}, last seen {ago(o.last, now)}
                {o.sigma_m !== null && `, ± ${o.sigma_m.toFixed(1)} m`}
              </span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

type Entry = { key: string; t: number; title: string; detail: string; xy: Vec2 | null; kind?: string; alert?: Alert };

const EventDot = ({ kind }: { kind: string }) => (
  <svg className="severity" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
    <circle cx="8" cy="8" r="5" fill={colorOf(kind)} />
  </svg>
);
type TimelineProps = { events: WorldEvent[]; alerts: Alert[]; onPick: (xy: Vec2) => void; now: number;
  name: (sid: string) => string };

/** Events and alerts (raised and cleared) in one list, newest first. */
export function Timeline({ events, alerts, onPick, now, name }: TimelineProps) {
  const entries: Entry[] = [
    ...events.map((e) => ({ key: e.eid, t: e.t, kind: e.kind, title: nameOf(e.kind), detail: `drone ${name(e.sid)}`, xy: e.xy })),
    ...alerts.map((a) => ({ key: `${a.aid} ${a.state}`, t: a.t, title: a.title, alert: a, xy: a.xy ?? null,
      detail: a.state === "active" ? "alert raised" : "alert cleared" })),
  ].filter((x) => x.t <= now) // after a replay jump back: what has not happened yet
    .sort((a, b) => b.t - a.t);
  if (entries.length === 0) return <p className="muted hint">Nothing has happened yet.</p>;
  return (
    <ul className="rows">
      {entries.map((x) => (
        <li key={x.key}>
          <button className={x.alert?.state === "cleared" ? "row stale" : "row"}
            onClick={() => x.xy && onPick(x.xy)}>
            {x.alert ? <SeverityIcon severity={x.alert.severity} /> : <EventDot kind={x.kind!} />}
            <span className="row-text">
              <strong>{x.title}</strong>
              <span className="muted">{clock(x.t)}, {ago(x.t, now)}, {x.detail}</span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
