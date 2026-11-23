// View 2: every drone in view, with the map or the video on the main panel.
import { useEffect, useState } from "react";

import type { MainPanel } from "./App";
import type { Vec2 } from "./contracts";
import { getJSON, send } from "./gateway";
import { AlertList } from "./Lists";
import { type Background, MapView } from "./MapView";
import {
  allAlerts,
  type Drone,
  droneIn,
  droneName,
  type Filter,
  latencyS,
  seeking,
  shownFrame,
  shownMap,
  type ShownAlert,
  shownWorld,
  type Situation,
  stageTimes,
  transmitting,
  worldNow,
  worldOf,
} from "./situation";
import { Video } from "./Video";

const ALL: Filter = { hidden: new Set(), stale: true };

type Props = {
  sit: Situation;
  sids: string[];
  sid: string | null;
  main: MainPanel;
  onDrone: (sid: string) => void;
  onOperator: () => void;
};

export function Command({ sit, sids, sid, main, onDrone, onOperator }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [alertId, setAlertId] = useState<string | null>(null);
  const [centre, setCentre] = useState<{ xy: Vec2; at: number } | null>(null);
  const [layer, setLayer] = useState<Background>("auto");
  const [settings, setSettings] = useState(false);
  const now = Date.now() / 1000;
  const alerts = allAlerts(sit);
  const alert = alerts.find((a) => a.key === alertId) ?? null;
  const drones = sids.map((s) => sit.drones.get(s)).filter((d): d is Drone => d !== undefined);
  const live = drones.filter((d) => transmitting(d, now)).length;
  const drone = sid ? sit.drones.get(sid) : undefined;
  const pickDrone = (s: string) => {
    if (sit.drones.get(s)?.wid !== drone?.wid) setSelected(null); // another world: its object ids are its own
    onDrone(s);
    const c = sit.drones.get(s)?.geo?.centre;
    if (c) setCentre({ xy: [c[0], c[1]], at: Date.now() });
  };
  const pickAlert = (a: ShownAlert) => {
    setAlertId(a.key === alertId ? null : a.key);
    const other = a.wid !== drone?.wid ? droneIn(sit, a.wid) : undefined; // on another map: show that map
    if (other) {
      setSelected(null);
      onDrone(other);
    }
    if (a.xy) setCentre({ xy: a.xy, at: Date.now() });
  };
  const map = (
    <MapView sit={sit} focus={sid} selected={selected} onSelect={setSelected} centre={centre} alert={alert}
      filter={ALL} overview layer={layer} />
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
          <span>objects: <span className="mono value">{shownWorld(sit, sid).objects.size}</span></span>
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
          <AlertList alerts={alerts} picked={alert?.key ?? null} onPick={pickAlert} now={worldNow(sit)} />
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
  return <Video sid={sid} name={droneName(sit, sid)} shown={shown} geo={thumb ? null : d?.geo}
    objects={worldOf(sit, d).objects}
    labels={!thumb} seeking={seeking(d, Date.now() / 1000)} />;
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
  group_motion: "Group motion",
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

type StreamModel = { model: string; active?: string | null; px?: number | null };

/** What "auto" runs for a stream: the model its scale picked, or why nothing runs. */
function autoState(s: StreamModel): string {
  if (s.model !== "auto" || s.active === undefined) return "";
  const px = s.px == null ? "" : `, ${s.px} px`;
  return s.active ? `${s.active}${px}` : `off, no model covers this scale${px}`;
}

function ModelPicker({ sid }: { sid: string }) {
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState<StreamModel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const path = `/api/detector/${encodeURIComponent(sid)}/model`;

  useEffect(() => {
    let current = true; // a late answer for the previous drone must not overwrite this one
    setModel(null);
    setError(null);
    Promise.all([getJSON<{ id: string }[]>("/api/models"), getJSON<StreamModel>(path)])
      .then(([all, mine]) => {
        if (!current) return;
        setModels(all.map((m) => m.id));
        setModel(mine);
      })
      .catch((e: Error) => current && setError(`Cannot read the models: ${e.message}`));
    return () => {
      current = false;
    };
  }, [path]);

  const auto = model?.model === "auto";
  useEffect(() => {
    if (!auto) return;
    const poll = () => getJSON<StreamModel>(path).then((m) => setModel((old) => old?.model === "auto" ? m : old))
      .catch(() => undefined); // the label is a hint: a missed answer leaves the last one
    const timer = setInterval(poll, 2000);
    return () => clearInterval(timer);
  }, [auto, path]);

  const choose = (id: string) =>
    send<StreamModel>("PUT", path, { model: id })
      .then((r) => {
        setModel(r);
        setError(null);
      })
      .catch((e: Error) => setError(`Model not switched: ${e.message}`));

  if (model === null) return error ? <span className="error">{error}</span> : null;
  return (
    <>
      <select value={model.model} onChange={(e) => choose(e.target.value)} aria-label="Detector model">
        <option value="auto">auto (by scale)</option>
        {models.map((m) => <option key={m}>{m}</option>)}
      </select>
      {auto && <span className="muted mono">{autoState(model)}</span>}
      {error && <span className="error">{error}</span>}
    </>
  );
}

type RouteRow = {
  model: string;
  loaded: boolean;
  min_px?: number;
  max_px?: number | null;
  yaml?: { min_px: number | null; max_px: number | null };
};
type Route = { models: RouteRow[]; margin: number; hold_s: number };
type Draft = { model: string; loaded: boolean; yaml?: RouteRow["yaml"]; min: string; max: string };

// An empty field is the model.yaml value, so only what differs from it is an override.
const overridden = (value: number | null | undefined, fromYaml: number | null | undefined) =>
  value != null && value !== fromYaml ? String(value) : "";

function toDraft(r: RouteRow): Draft {
  return { model: r.model, loaded: r.loaded, yaml: r.yaml, min: overridden(r.min_px, r.yaml?.min_px),
    max: overridden(r.max_px, r.yaml?.max_px) };
}

const orNull = (text: string) => (text.trim() === "" ? null : Number(text));

/** The models "auto" chooses from, in priority order, with the object size range (px on the model input) of each. */
function AutoModels() {
  const [rows, setRows] = useState<Draft[] | null>(null);
  const [margin, setMargin] = useState("");
  const [holdS, setHoldS] = useState("");
  const [available, setAvailable] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  const show = (r: Route) => {
    setRows(r.models.map(toDraft));
    setMargin(String(r.margin));
    setHoldS(String(r.hold_s));
    setError(null);
  };
  useEffect(() => {
    Promise.all([getJSON<Route>("/api/routing"), getJSON<{ id: string }[]>("/api/models")])
      .then(([r, all]) => {
        show(r);
        setAvailable(all.map((m) => m.id).filter((id) => !id.startsWith("oracle")));
      })
      .catch((e: Error) => setError(`Cannot read the auto models: ${e.message}`));
  }, []);

  if (rows === null) return error ? <span className="error">{error}</span> : null;
  const edit = (i: number, patch: Partial<Draft>) => setRows(rows.map((r, k) => (k === i ? { ...r, ...patch } : r)));
  const move = (i: number, by: number) => {
    const next = [...rows];
    [next[i], next[i + by]] = [next[i + by], next[i]];
    setRows(next);
  };
  const apply = () =>
    send<Route>("PUT", "/api/routing", {
      models: rows.map((r) => ({ model: r.model, min_px: orNull(r.min), max_px: orNull(r.max) })),
      margin: Number(margin), hold_s: Number(holdS),
    }).then(show).catch((e: Error) => setError(`Not saved: ${e.message}`));
  const reset = () =>
    send<Route>("DELETE", "/api/routing").then(show).catch((e: Error) => setError(`Not reset: ${e.message}`));
  const addable = available.filter((id) => !rows.some((r) => r.model === id));

  return (
    <div className="auto-models">
      {rows.map((r, i) => (
        <div className="auto-model" key={r.model}>
          <span className="mono">{r.model}{!r.loaded && <span className="error"> (not loaded)</span>}</span>
          <div className="auto-row">
            <label>min px <input type="number" min={0} value={r.min} placeholder={String(r.yaml?.min_px ?? "–")}
              onChange={(e) => edit(i, { min: e.target.value })} aria-label={`${r.model} smallest object, px`} /></label>
            <label>max px <input type="number" min={0} value={r.max} placeholder={String(r.yaml?.max_px ?? "–")}
              onChange={(e) => edit(i, { max: e.target.value })} aria-label={`${r.model} largest object, px`} /></label>
          </div>
          <div className="auto-row">
            <button onClick={() => move(i, -1)} disabled={i === 0} aria-label={`${r.model} higher priority`}>Up</button>
            <button onClick={() => move(i, 1)} disabled={i === rows.length - 1}
              aria-label={`${r.model} lower priority`}>Down</button>
            <button onClick={() => setRows(rows.filter((_, k) => k !== i))}
              aria-label={`Remove ${r.model}`}>Remove</button>
          </div>
        </div>
      ))}
      <div className="auto-row">
        <select value="" onChange={(e) => e.target.value && setRows([...rows, { model: e.target.value, loaded: true,
          min: "", max: "" }])} aria-label="Add a model" disabled={addable.length === 0}>
          <option value="">Add a model</option>
          {addable.map((id) => <option key={id}>{id}</option>)}
        </select>
        <label>margin <input type="number" min={0} max={0.99} step={0.05} value={margin}
          onChange={(e) => setMargin(e.target.value)} /></label>
        <label>hold s <input type="number" min={0} step={0.5} value={holdS}
          onChange={(e) => setHoldS(e.target.value)} /></label>
      </div>
      <div className="auto-row">
        <button onClick={apply}>Apply</button>
        <button onClick={reset}>Default</button>
        {error && <span className="error">{error}</span>}
      </div>
    </div>
  );
}

type SettingsProps = { sit: Situation; drone?: Drone; layer: Background; onLayer: (l: Background) => void };

function Settings({ sit, drone, layer, onLayer }: SettingsProps) {
  const wid = drone?.wid ?? sit.session?.replays[0]?.wid; // the shown drone's world
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
      <p className="muted setting-note">
        Rules that raise alerts. Live, they are the mission's: every map of it, also when it starts again, until the
        server restarts.
      </p>
      <div className="setting setting-top">
        <span className="muted">Auto models</span>
        <AutoModels />
      </div>
      <p className="muted setting-note">
        What a drone on "auto" chooses from, first match wins. Min px and max px are the smallest and largest size,
        in pixels on the model's input, of a 1.8 m object where the camera aims: zoom and range decide it. An empty
        field is the model's own range (grey). When no model covers the scale, nothing is detected, so no flickering
        boxes.
      </p>
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
