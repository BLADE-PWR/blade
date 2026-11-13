// View 2: every drone of the session. The main panel is the map (with the drone list, alerts and service state
// beside it) or the video (drone previews and the chosen drone's video, the small map, state and settings).
import { useEffect, useState } from "react";

import type { MainPanel } from "./App";
import type { Alert, Vec2 } from "./contracts";
import { getJSON } from "./gateway";
import { AlertList } from "./Lists";
import { type Background, MapView } from "./MapView";
import {
  allAlerts,
  type Drone,
  type Filter,
  latencyS,
  shownFrame,
  type Situation,
  stageTimes,
  transmitting,
} from "./situation";
import { Video } from "./Video";

const ALL: Filter = { hidden: new Set(), stale: true, minConf: 0 };

type Props = {
  sit: Situation;
  sids: string[];
  sid: string | null;
  main: MainPanel;
  onMain: (p: MainPanel) => void;
  onDrone: (sid: string) => void;
  onOperator: () => void;
};

export function Command({ sit, sids, sid, main, onMain, onDrone, onOperator }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [alertId, setAlertId] = useState<string | null>(null);
  const [centre, setCentre] = useState<{ xy: Vec2; at: number } | null>(null);
  const [layer, setLayer] = useState<Background>("auto");
  const [settings, setSettings] = useState(false);
  const now = Date.now() / 1000;
  const alerts = allAlerts(sit);
  const alert = alerts.find((a) => a.aid === alertId) ?? null;
  const drones = sids.map((s) => sit.drones.get(s)).filter((d): d is Drone => d !== undefined);
  const live = drones.filter((d) => transmitting(d, now)).length;
  const drone = sid ? sit.drones.get(sid) : undefined;
  const pickDrone = (s: string) => {
    onDrone(s);
    const c = sit.drones.get(s)?.geo?.centre;
    if (c) setCentre({ xy: [c[0], c[1]], at: Date.now() });
  };
  const pickAlert = (a: Alert) => {
    setAlertId(a.aid === alertId ? null : a.aid);
    if (a.xy) setCentre({ xy: a.xy, at: Date.now() });
  };
  const map = (
    <MapView sit={sit} focus={sid} selected={selected} onSelect={setSelected} centre={centre} alert={alert}
      filter={ALL} overview layer={layer} onExpand={main === "video" ? () => onMain("map") : undefined} />
  );

  if (main === "video") {
    return (
      <div className="operator">
        <aside className="side-col">
          <section className="panel map-panel" aria-label="Widok: mapa przeglądowa">{map}</section>
          <StatePanel sit={sit} sid={sid} />
          <Settings sit={sit} drone={drone} layer={layer} onLayer={setLayer} />
        </aside>
        <div className="main-col">
          <div className="thumbs" role="group" aria-label="Wybór drona">
            {sids.map((s) => (
              <button key={s} className="thumb-card" aria-pressed={s === sid} onClick={() => pickDrone(s)}>
                <span className="panel-head"><DroneName sit={sit} sid={s} now={now} /></span>
                <DroneVideo sit={sit} sid={s} thumb />
              </button>
            ))}
          </div>
          {sid && (
            <section className="panel" aria-label="Wideo wybranego drona">
              <div className="panel-head">
                <strong>Dron {sid}</strong>
                {drone?.geo && (
                  <span className="muted">
                    wysokość <span className="mono value">{drone.geo.camera[2].toFixed(0)} m</span>
                  </span>
                )}
              </div>
              <DroneVideo sit={sit} sid={sid} />
            </section>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="operator">
      <section className="panel command-map" aria-label="Mapa: wszystkie drony">
        {map}
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
            {sids.map((s) => (
              <li key={s}>
                <button className="row drone-row" aria-pressed={s === sid} onClick={() => pickDrone(s)}>
                  <DroneThumb sit={sit} sid={s} />
                  <span className="row-text"><DroneName sit={sit} sid={s} now={now} /></span>
                </button>
                {s === sid && (
                  <button className="primary open-operator" onClick={onOperator}>Otwórz w widoku operatora</button>
                )}
              </li>
            ))}
          </ul>
        </section>
        <section className="panel tabs" aria-label="Alerty">
          <div className="panel-head"><strong>Alerty</strong> <span className="mono muted">{alerts.length}</span></div>
          <AlertList alerts={alerts} picked={alert?.aid ?? null} onPick={pickAlert} now={now} />
        </section>
        <ServiceLine sit={sit} drone={drone} onSettings={() => setSettings(!settings)} />
        {settings && <Settings sit={sit} drone={drone} layer={layer} onLayer={setLayer} />}
      </aside>
    </div>
  );
}

function DroneVideo({ sit, sid, thumb = false }: { sit: Situation; sid: string; thumb?: boolean }) {
  const d = sit.drones.get(sid);
  const shown = d ? shownFrame(d, Date.now() / 1000) : { preview: null, dets: null };
  return <Video sid={sid} shown={shown} geo={thumb ? null : d?.geo} objects={sit.objects} labels={!thumb} />;
}

function DroneThumb({ sit, sid }: { sit: Situation; sid: string }) {
  const preview = sit.drones.get(sid)?.previews.at(-1);
  if (!preview) return <span className="thumb empty" />;
  return <img className="thumb" src={preview.url} alt={`Podgląd z drona ${sid}`} />;
}

function DroneName({ sit, sid, now }: { sit: Situation; sid: string; now: number }) {
  const d = sit.drones.get(sid);
  const fps = sit.diag.get("ingest")?.streams[sid]?.fps_in;
  const on = d !== undefined && transmitting(d, now);
  const silent = d?.lastFrameAt ? ` od ${(now - d.lastFrameAt).toFixed(0)} s` : "";
  return (
    <>
      <span className="inline-dot"><strong>Dron {sid}</strong><span className={on ? "dot ok" : "dot warn"} /></span>
      {on ? <span className="mono muted">{fps === undefined ? "" : `${fps.toFixed(0)} fps`}</span>
        : <span className="slow">brak klatek{silent}</span>}
      {on && <span className="muted">obiekty w kadrze: {d.geo?.pts.length ?? 0}</span>}
    </>
  );
}

type LineProps = { sit: Situation; drone?: Drone; onSettings: () => void };

function ServiceLine({ sit, drone, onSettings }: LineProps) {
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
      <button className="push" onClick={onSettings}>Ustawienia</button>
    </section>
  );
}

function StatePanel({ sit, sid }: { sit: Situation; sid: string | null }) {
  const drone = sid ? sit.drones.get(sid) : undefined;
  const latency = drone ? latencyS(drone) : null;
  return (
    <section className="panel" aria-label="Stan">
      <div className="panel-head">
        <strong>Stan</strong>
        {latency !== null && (
          <span className="muted push">
            opóźnienie <span className="mono value">{(latency * 1000).toFixed(0)} ms</span>
          </span>
        )}
      </div>
      <div className="state-grid mono">
        {[...sit.diag.values()].map((d) => {
          const st = sid ? d.streams[sid] : undefined;
          const ms = st?.latency_ms == null ? "" : `, ${st.latency_ms.toFixed(0)} ms`;
          return (
            <span key={d.service} className="inline-dot">
              <span className={st ? "dot ok" : "dot warn"} />{d.service}
              <span className="muted push">{st ? `${st.fps_out.toFixed(0)} fps${ms}` : "brak"}</span>
            </span>
          );
        })}
      </div>
    </section>
  );
}

type Rules = { vehicle_group?: { enabled: boolean; min_count: number; radius_m: number } };

type SettingsProps = { sit: Situation; drone?: Drone; layer: Background; onLayer: (l: Background) => void };

function Settings({ sit, drone, layer, onLayer }: SettingsProps) {
  const [maps, setMaps] = useState<{ id: string }[]>([]);
  const [rules, setRules] = useState<Rules | null>(null);
  const [error, setError] = useState<string | null>(null);
  const wid = sit.session?.streams[0]?.wid;

  useEffect(() => {
    getJSON<{ id: string }[]>("/api/maps").then(setMaps)
      .catch((e: Error) => setError(`Nie udało się pobrać listy map (${e.message}).`));
  }, []);

  useEffect(() => {
    if (!wid) return;
    getJSON<Rules>(`/api/decision/${wid}/rules`).then(setRules)
      .catch((e: Error) => setError(`Nie udało się pobrać reguł alertów (${e.message}).`));
  }, [wid]);

  const group = rules?.vehicle_group;
  const groupText = group ? `grupa pojazdów: co najmniej ${group.min_count} w promieniu ${group.radius_m} m` : "brak";
  return (
    <section className="panel settings" aria-label="Ustawienia">
      <div className="panel-head"><strong>Ustawienia</strong></div>
      <label className="setting">
        <span className="muted">Mapa</span>
        <select disabled value={sit.session?.map ?? ""}>
          {maps.map((m) => <option key={m.id}>{m.id}</option>)}
        </select>
      </label>
      <p className="muted setting-note">Zmiana mapy wymaga restartu serwera.</p>
      <div className="setting">
        <span className="muted">Podkład</span>
        <div className="switch" role="group" aria-label="Podkład mapy">
          <button aria-pressed={layer === "auto"} onClick={() => onLayer("auto")}>Ortofotomapa</button>
          <button aria-pressed={layer === "hillshade"} onClick={() => onLayer("hillshade")}>Cieniowanie</button>
        </div>
      </div>
      <div className="setting">
        <span className="muted">Model</span>
        <span className="mono">{drone?.dets?.model ?? "brak ramek"}</span>
      </div>
      <div className="setting">
        <span className="muted">Alerty</span>
        <span>{groupText}</span>
      </div>
      {error && <p className="error setting-note">{error}</p>}
    </section>
  );
}
