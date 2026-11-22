// View 2: every drone in view, with the map or the video on the main panel.
import { useEffect, useState } from "react";

import type { MainPanel } from "./App";
import type { Alert, Vec2 } from "./contracts";
import { getJSON, send } from "./gateway";
import { AlertList } from "./Lists";
import { type Background, MapView } from "./MapView";
import {
  allAlerts,
  type Drone,
  droneName,
  type Filter,
  latencyS,
  offMap,
  seeking,
  shownFrame,
  shownMap,
  type Situation,
  stageTimes,
  transmitting,
  worldNow,
} from "./situation";
import { Video } from "./Video";

const ALL: Filter = { hidden: new Set(), stale: true };

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
          <section className="panel map-panel" aria-label="Overview map">{map}</section>
          <StatePanel sit={sit} sid={sid} />
          <Settings sit={sit} drone={drone} layer={layer} onLayer={setLayer} />
        </aside>
        <div className="main-col">
          <div className="thumbs" role="group" aria-label="Drone choice">
            {sids.map((s) => (
              <button key={s} className="thumb-card" aria-pressed={s === sid} onClick={() => pickDrone(s)}>
                <span className="panel-head"><DroneName sit={sit} sid={s} now={now} /></span>
                <DroneVideo sit={sit} sid={s} thumb />
              </button>
            ))}
          </div>
          {sid && (
            <section className="panel main-video" aria-label="Video of the chosen drone">
              <div className="panel-head">
                <strong>Drone {droneName(sit, sid)}</strong>
                {drone?.geo && (
                  <span className="muted">
                    altitude <span className="mono value">{drone.geo.camera[2].toFixed(0)} m</span>
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
      <section className="panel command-map" aria-label="Map of all drones">
        {map}
        <div className="map-summary">
          <span>objects: <span className="mono value">{sit.objects.size}</span></span>
          <span>drones sending: <span className="mono value">{live} of {sids.length}</span></span>
          <span>alerts: <span className="mono value">{alerts.length}</span></span>
        </div>
      </section>
      <aside className="side-col">
        <section className="panel" aria-label="Drones">
          <div className="panel-head"><strong>Drones</strong></div>
          <ul className="rows">
            {sids.map((s) => (
              <li key={s}>
                <button className="row drone-row" aria-pressed={s === sid} onClick={() => pickDrone(s)}>
                  <DroneThumb sit={sit} sid={s} />
                  <span className="row-text"><DroneName sit={sit} sid={s} now={now} /></span>
                </button>
                {s === sid && (
                  <button className="primary open-operator" onClick={onOperator}>Open in operator view</button>
                )}
              </li>
            ))}
          </ul>
        </section>
        <section className="panel tabs" aria-label="Alerts">
          <div className="panel-head"><strong>Alerts</strong> <span className="mono muted">{alerts.length}</span></div>
          <AlertList alerts={alerts} picked={alert?.aid ?? null} onPick={pickAlert} now={worldNow(sit)} />
        </section>
        <ServiceLine sit={sit} drone={drone} onSettings={() => setSettings(!settings)} />
        {settings && <Settings sit={sit} drone={drone} layer={layer} onLayer={setLayer} />}
      </aside>
    </div>
  );
}

function DroneVideo({ sit, sid, thumb = false }: { sit: Situation; sid: string; thumb?: boolean }) {
  const d = sit.drones.get(sid);
  const shown = d ? shownFrame(d) : { preview: null, dets: null };
  return <Video sid={sid} shown={shown} geo={thumb ? null : d?.geo} objects={sit.objects} labels={!thumb}
    offMap={offMap(sit, d)} seeking={seeking(d, Date.now() / 1000)} />;
}

function DroneThumb({ sit, sid }: { sit: Situation; sid: string }) {
  const preview = sit.drones.get(sid)?.previews.at(-1);
  if (!preview) return <span className="thumb empty" />;
  return <img className="thumb" src={preview.url} alt={`Preview from drone ${droneName(sit, sid)}`} />;
}

function DroneName({ sit, sid, now }: { sit: Situation; sid: string; now: number }) {
  const d = sit.drones.get(sid);
  const fps = sit.diag.get("ingest")?.streams[sid]?.fps_in;
  const on = d !== undefined && transmitting(d, now);
  const silent = d?.lastFrameAt ? ` for ${(now - d.lastFrameAt).toFixed(0)} s` : "";
  return (
    <>
      <span className="inline-dot"><strong>Drone {droneName(sit, sid)}</strong><span className={on ? "dot ok" : "dot warn"} /></span>
      {on ? <span className="mono muted">{fps === undefined ? "" : `${fps.toFixed(0)} fps`}</span>
        : <span className="slow">no frames{silent}</span>}
      {on && <span className="muted">objects in frame: {d.geo?.pts.length ?? 0}</span>}
    </>
  );
}

type LineProps = { sit: Situation; drone?: Drone; onSettings: () => void };

function ServiceLine({ sit, drone, onSettings }: LineProps) {
  const detector = drone ? stageTimes(drone).find(([stage]) => stage === "detector")?.[1] : undefined;
  const latency = drone ? latencyS(drone) : null;
  return (
    <section className="panel service-line mono" aria-label="State">
      <strong>State</strong>
      <span className="inline-dot">
        <span className={sit.diag.size ? "dot ok" : "dot warn"} />services reporting: {sit.diag.size}
      </span>
      {detector !== undefined && <span>detector {(detector * 1000).toFixed(0)} ms</span>}
      {latency !== null && <span className="muted">total {(latency * 1000).toFixed(0)} ms</span>}
      <button className="push" onClick={onSettings}>Settings</button>
    </section>
  );
}

function StatePanel({ sit, sid }: { sit: Situation; sid: string | null }) {
  const drone = sid ? sit.drones.get(sid) : undefined;
  const latency = drone ? latencyS(drone) : null;
  return (
    <section className="panel" aria-label="State">
      <div className="panel-head">
        <strong>State</strong>
        {latency !== null && (
          <span className="muted push">
            latency <span className="mono value">{(latency * 1000).toFixed(0)} ms</span>
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
              <span className="muted push">{st ? `${st.fps_out.toFixed(0)} fps${ms}` : "none"}</span>
            </span>
          );
        })}
      </div>
    </section>
  );
}

// The detector model of one drone; switching takes effect on its next frame and resets its box threshold.
const RULE_NAMES: Record<string, string> = {
  vehicle_group: "Vehicle group", lost_contact: "Lost contact", uncertain_position: "Uncertain position",
  infantry_resolution: "Infantry resolution", explosion_near: "Explosion nearby", zone: "Zone",
};

type Rules = Record<string, { enabled?: boolean }>;

/** A checkbox per decision rule of the world; shows what the server holds, so a failed change unticks nothing. */
function RulePicker({ wid }: { wid: string }) {
  const [rules, setRules] = useState<Rules | null>(null);
  const [error, setError] = useState<string | null>(null);
  const path = `/api/decision/${encodeURIComponent(wid)}/rules`;

  useEffect(() => {
    let current = true; // a late answer for the previous world must not overwrite this one
    setRules(null);
    setError(null);
    getJSON<Rules>(path).then((r) => current && setRules(r))
      .catch((e: Error) => current && setError(`Cannot read the alert rules: ${e.message}`));
    return () => {
      current = false;
    };
  }, [path]);

  const toggle = (name: string, enabled: boolean) =>
    send<Rules>("PUT", path, { [name]: { enabled } })
      .then((r) => {
        setRules(r);
        setError(null);
      })
      .catch((e: Error) => setError(`Rule not changed: ${e.message}`));

  if (rules === null) return error ? <span className="error">{error}</span> : null;
  return (
    <div className="rules">
      {Object.entries(rules).map(([name, p]) => (
        <label key={name}>
          <input type="checkbox" checked={p.enabled !== false} onChange={(e) => toggle(name, e.target.checked)} />
          {" "}{RULE_NAMES[name] ?? name}
        </label>
      ))}
      {error && <span className="error">{error}</span>}
    </div>
  );
}

function ModelPicker({ sid }: { sid: string }) {
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const path = `/api/detector/${encodeURIComponent(sid)}/model`;

  useEffect(() => {
    let current = true; // a late answer for the previous drone must not overwrite this one
    setModel(null);
    setError(null);
    Promise.all([getJSON<{ id: string }[]>("/api/models"), getJSON<{ model: string }>(path)])
      .then(([all, mine]) => {
        if (!current) return;
        setModels(all.map((m) => m.id));
        setModel(mine.model);
      })
      .catch((e: Error) => current && setError(`Cannot read the models: ${e.message}`));
    return () => {
      current = false;
    };
  }, [path]);

  const choose = (id: string) =>
    send<{ model: string }>("PUT", path, { model: id })
      .then((r) => {
        setModel(r.model);
        setError(null);
      })
      .catch((e: Error) => setError(`Model not switched: ${e.message}`));

  if (model === null) return error ? <span className="error">{error}</span> : null;
  return (
    <>
      <select value={model} onChange={(e) => choose(e.target.value)} aria-label="Detector model">
        {models.map((m) => <option key={m}>{m}</option>)}
      </select>
      {error && <span className="error">{error}</span>}
    </>
  );
}

type SettingsProps = { sit: Situation; drone?: Drone; layer: Background; onLayer: (l: Background) => void };

function Settings({ sit, drone, layer, onLayer }: SettingsProps) {
  const wid = drone?.wid ?? sit.session?.streams[0]?.wid; // the shown drone's world
  return (
    <section className="panel settings" aria-label="Settings">
      <div className="panel-head"><strong>Settings</strong></div>
      <div className="setting">
        <span className="muted">Map</span>
        <span className="mono">{shownMap(sit, drone?.sid ?? null) ?? "waiting for a drone"}</span>
      </div>
      <p className="muted setting-note">Each world is on the map its first drone reports.</p>
      <div className="setting">
        <span className="muted">Background</span>
        <div className="switch" role="group" aria-label="Map background">
          <button aria-pressed={layer === "auto"} onClick={() => onLayer("auto")}>Orthophoto</button>
          <button aria-pressed={layer === "hillshade"} onClick={() => onLayer("hillshade")}>Hillshade</button>
        </div>
      </div>
      <div className="setting">
        <span className="muted">Alerts</span>
        {wid ? <RulePicker wid={wid} /> : <span className="muted">waiting for a drone</span>}
      </div>
      <p className="muted setting-note">Rules that raise alerts, for this world until a restart.</p>
      <div className="panel-head settings-head">
        <strong>Drone settings</strong>
        {drone && <span className="mono muted">{drone.sid}</span>}
      </div>
      <div className="setting">
        <span className="muted">Model</span>
        {drone ? <ModelPicker sid={drone.sid} /> : <span className="muted">choose a drone</span>}
      </div>
    </section>
  );
}
