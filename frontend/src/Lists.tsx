// The alert, object and event lists (alert tabs in view 1, the alert list in view 2).
import { nameOf, SEVERITY_COLORS } from "./classes";
import type { Alert, WorldEvent, WorldObject } from "./contracts";
import { ago, clock } from "./format";
import { symbolUrl } from "./MapView";

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

type AlertProps = { alerts: Alert[]; picked: string | null; onPick: (a: Alert) => void; now: number };

export function AlertList({ alerts, picked, onPick, now }: AlertProps) {
  if (alerts.length === 0) return <p className="muted hint">No active alerts.</p>;
  return (
    <ul className="rows">
      {alerts.map((a) => (
        <li key={a.aid}>
          <button className="row" aria-pressed={a.aid === picked} onClick={() => onPick(a)}>
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

type EventProps = { events: WorldEvent[]; onPick: (e: WorldEvent) => void; now: number; name: (sid: string) => string };

export function EventList({ events, onPick, now, name }: EventProps) {
  if (events.length === 0) return <p className="muted hint">No events.</p>;
  return (
    <ul className="rows">
      {[...events].reverse().map((e) => (
        <li key={e.eid}>
          <button className="row" onClick={() => onPick(e)}>
            <span className="row-text">
              <strong>{nameOf(e.kind)}</strong>
              <span className="muted">{clock(e.t)}, {ago(e.t, now)}, drone {name(e.sid)}</span>
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}
