// View 2: every drone of the session. The map on the main panel, the drones, alerts and service state beside it.
import { useState } from "react";

import type { Alert, Vec2 } from "./contracts";
import { AlertList } from "./Lists";
import { MapView } from "./MapView";
import { allAlerts, type Drone, type Filter, latencyS, type Situation, stageTimes, transmitting } from "./situation";

const ALL: Filter = { hidden: new Set(), stale: true, minConf: 0 };

type Props = {
  sit: Situation;
  sids: string[];
  sid: string | null;
  onDrone: (sid: string) => void;
  onOperator: () => void;
};

export function Command({ sit, sids, sid, onDrone, onOperator }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [alertId, setAlertId] = useState<string | null>(null);
  const [centre, setCentre] = useState<{ xy: Vec2; at: number } | null>(null);
  const now = Date.now() / 1000;
  const alerts = allAlerts(sit);
  const alert = alerts.find((a) => a.aid === alertId) ?? null;
  const drones = sids.map((s) => sit.drones.get(s)).filter((d): d is Drone => d !== undefined);
  const live = drones.filter((d) => transmitting(d, now)).length;
  const pickDrone = (d: Drone) => {
    onDrone(d.sid);
    if (d.geo?.centre) setCentre({ xy: [d.geo.centre[0], d.geo.centre[1]], at: Date.now() });
  };
  const pickAlert = (a: Alert) => {
    setAlertId(a.aid === alertId ? null : a.aid);
    if (a.xy) setCentre({ xy: a.xy, at: Date.now() });
  };

  return (
    <div className="operator">
      <section className="panel command-map" aria-label="Mapa: wszystkie drony">
        <MapView sit={sit} focus={sid} selected={selected} onSelect={setSelected} centre={centre} alert={alert}
          filter={ALL} overview />
        <div className="map-summary">
          <span>obiekty: <span className="mono value">{sit.objects.size}</span></span>
          <span>drony nadające: <span className="mono value">{live} z {sids.length}</span></span>
          <span>alerty: <span className="mono value">{alerts.length}</span></span>
        </div>
      </section>
      <aside className="side-col">
        <section className="panel" aria-label="Drony">
          <div className="panel-head"><strong>Drony</strong></div>
          <ul className="rows">
            {sids.map((s) => {
              const d = sit.drones.get(s);
              return (
                <li key={s}>
                  <button className="row drone-row" aria-pressed={s === sid} onClick={() => d && pickDrone(d)}>
                    <DroneThumb sit={sit} sid={s} />
                    <span className="row-text">
                      <span className="inline-dot">
                        <strong>Dron {s}</strong>
                        <span className={d && transmitting(d, now) ? "dot ok" : "dot warn"} />
                      </span>
                      <DroneState sit={sit} sid={s} now={now} />
                    </span>
                  </button>
                  {s === sid && (
                    <button className="primary open-operator" onClick={onOperator}>Otwórz w widoku operatora</button>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
        <section className="panel tabs" aria-label="Alerty">
          <div className="panel-head"><strong>Alerty</strong> <span className="mono muted">{alerts.length}</span></div>
          <AlertList alerts={alerts} picked={alert?.aid ?? null} onPick={pickAlert} now={now} />
        </section>
        <ServiceLine sit={sit} drone={sid ? sit.drones.get(sid) : undefined} />
      </aside>
    </div>
  );
}

function DroneThumb({ sit, sid }: { sit: Situation; sid: string }) {
  const preview = sit.drones.get(sid)?.previews.at(-1);
  if (!preview) return <span className="thumb empty" />;
  return <img className="thumb" src={preview.url} alt={`Podgląd z drona ${sid}`} />;
}

function DroneState({ sit, sid, now }: { sit: Situation; sid: string; now: number }) {
  const d = sit.drones.get(sid);
  const fps = sit.diag.get("ingest")?.streams[sid]?.fps_in;
  if (!d || !transmitting(d, now)) {
    const silent = d?.lastFrameAt ? ` od ${(now - d.lastFrameAt).toFixed(0)} s` : "";
    return <span className="slow">brak klatek{silent}</span>;
  }
  return (
    <>
      <span className="mono muted">{fps === undefined ? sid : `${fps.toFixed(0)} fps`}</span>
      <span className="muted">obiekty w kadrze: {d.geo?.pts.length ?? 0}</span>
    </>
  );
}

function ServiceLine({ sit, drone }: { sit: Situation; drone?: Drone }) {
  const detector = drone ? stageTimes(drone).find(([stage]) => stage === "detector")?.[1] : undefined;
  const latency = drone ? latencyS(drone) : null;
  return (
    <section className="panel service-line mono" aria-label="Stan">
      <strong>Stan</strong>
      <span className="inline-dot">
        <span className={sit.diag.size ? "dot ok" : "dot warn"} />serwisy z diagnostyką: {sit.diag.size}
      </span>
      {detector !== undefined && <span>detektor {(detector * 1000).toFixed(0)} ms</span>}
      {latency !== null && <span className="muted">całość {(latency * 1000).toFixed(0)} ms</span>}
    </section>
  );
}
