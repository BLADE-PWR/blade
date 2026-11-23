// View 1: one drone, with the video or the map on the main panel.
import { useEffect, useRef, useState } from "react";

import type { MainPanel } from "./App";
import { colorOf, EVENT_KINDS, GROUPS, nameOf } from "./classes";
import type { Vec2, WorldObject } from "./contracts";
import { ago, clock } from "./format";
import { getJSON, send } from "./gateway";
import { AlertList, EventList, ObjectList } from "./Lists";
import { MapView, symbolUrl } from "./MapView";
import {
  allAlerts,
  boxVisible,
  droneIn,
  droneName,
  type Filter,
  seeking,
  type ShownAlert,
  shownFrame,
  shownWorld,
  type Situation,
  stageTimes,
  visible,
  type WorldView,
  worldNow,
} from "./situation";
import { Video } from "./Video";

type Tab = "alerts" | "objects" | "events";
const TABS: [Tab, string, (s: Situation, w: WorldView) => number][] = [
  ["alerts", "Alerts", (s) => allAlerts(s).length], // every world's: an alert on another map matters too
  ["objects", "Objects", (_, w) => w.objects.size],
  ["events", "Events", (_, w) => w.events.size],
];

const SLOW_STAGE_S = 0.1;
const CROP_PX = 112;
const CROP_MARGIN = 1.6; // crop side / box side
const CROP_MIN_PX = 40; // preview px

type Props = {
  sit: Situation;
  sid: string | null;
  main: MainPanel;
  onMain: (p: MainPanel) => void;
  onDrone: (sid: string) => void;
  filter: Filter;
  onFilter: (f: Filter) => void;
};

export function Operator({ sit, sid, main, onMain, onDrone, filter, onFilter }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [centre, setCentre] = useState<{ xy: Vec2; at: number } | null>(null);
  const [tab, setTab] = useState<Tab>("alerts");
  const [alertId, setAlertId] = useState<string | null>(null);
  const world = shownWorld(sit, sid); // the drone's world: its objects, events and clock
  const now = worldNow(sit, world);
  const drone = sid ? sit.drones.get(sid) : undefined;
  const shown = drone ? shownFrame(drone) : { preview: null, dets: null };
  const object = selected ? world.objects.get(selected) : undefined;
  const alerts = allAlerts(sit);
  const alert = alerts.find((a) => a.key === alertId) ?? null;
  const pickAlert = (a: ShownAlert) => {
    setAlertId(a.key === alertId ? null : a.key);
    const other = a.wid !== world.wid ? droneIn(sit, a.wid) : undefined; // on another map: go to that drone
    if (other) onDrone(other);
    setSelected(a.objects[0] ?? null);
    if (a.xy) setCentre({ xy: a.xy, at: Date.now() });
  };
  const video = (
    <section className="panel" aria-label="Drone video">
      <div className="panel-head">
        <strong>Camera</strong>
        <span className="mono muted">{sid && droneName(sit, sid)}{shown.preview?.frame != null && `, frame ${shown.preview.frame}`}</span>
        {drone?.geo && (
          <span className="muted">altitude <span className="mono value">{drone.geo.camera[2].toFixed(0)} m</span></span>
        )}
        <span className="muted push">
          boxes <span className="mono value">{shown.dets?.dets.filter((d) => boxVisible(d, filter)).length ?? 0}</span>
        </span>
      </div>
      {sid && (
        <Video sid={sid} name={droneName(sit, sid)} shown={shown} geo={drone?.geo} objects={world.objects}
          selected={selected} filter={filter} seeking={seeking(drone, Date.now() / 1000)} />
      )}
    </section>
  );
  const map = (
    <section className={main === "map" ? "panel map-main" : "panel map-panel"} aria-label="Map">
      <MapView sit={sit} focus={sid} selected={selected} onSelect={setSelected} centre={centre} alert={alert}
        filter={filter} />
    </section>
  );
  return (
    <>
      <div className="operator">
        <div className="main-col">
          {main === "video" ? video : map}
          <section className="panel details" aria-label="Object details">
            {object ? (
              <Details sit={sit} object={object} onCentre={() => setCentre({ xy: object.xy, at: Date.now() })}
                onVideo={(s) => {
                  onDrone(s);
                  onMain("video");
                }} />
            ) : (
              <p className="muted hint">Pick an object on the map or in the list.</p>
            )}
          </section>
        </div>
        <aside className="side-col">
          {main === "video" ? map : video}
          <section className="panel tabs" aria-label="Alerts and objects">
            <div className="tablist" role="tablist">
              {TABS.map(([key, name, count]) => (
                <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>
                  {name} <span className="mono muted">{count(sit, world)}</span>
                </button>
              ))}
            </div>
            {tab === "alerts" && (
              <AlertList alerts={alerts} picked={alert?.key ?? null} onPick={pickAlert} now={worldNow(sit)} />
            )}
            {tab === "objects" && (
              <ObjectList objects={[...world.objects.values()].filter((o) => visible(o, filter))} selected={selected}
                onSelect={setSelected} now={now} />
            )}
            {tab === "events" && (
              <EventList events={[...world.events.values()].filter((e) => !filter.hidden.has(e.kind))} now={now}
                name={(s) => droneName(sit, s)}
                onPick={(e) => setCentre({ xy: e.xy, at: Date.now() })} />
            )}
          </section>
        </aside>
      </div>
      <BottomBar sit={sit} sid={sid} filter={filter} onFilter={onFilter} />
    </>
  );
}

type BarProps = { sit: Situation; sid: string | null; filter: Filter; onFilter: (f: Filter) => void };

function BottomBar({ sit, sid, filter, onFilter }: BarProps) {
  const world = shownWorld(sit, sid);
  const count = (kind: string) => [...world.objects.values()].filter((o) => o.grp === kind).length +
    [...world.events.values()].filter((e) => e.kind === kind).length;
  const toggle = (kind: string) => {
    const hidden = new Set(filter.hidden);
    if (!hidden.delete(kind)) hidden.add(kind);
    onFilter({ ...filter, hidden });
  };
  const drone = sid ? sit.drones.get(sid) : undefined;
  const dropped = sid ? sit.diag.get("detector")?.streams[sid]?.dropped : undefined;
  return (
    <footer className="bottombar">
      <div className="filters" role="group" aria-label="Class filter">
        {[...GROUPS, ...EVENT_KINDS].map((kind) => (
          <button key={kind} className="filter" aria-pressed={!filter.hidden.has(kind)} onClick={() => toggle(kind)}>
            <span className="swatch" style={{ background: colorOf(kind) }} />
            {nameOf(kind)} <span className="mono muted">{count(kind)}</span>
          </button>
        ))}
      </div>
      {sid && <ConfSlider sid={sid} />}
      <label className="inline">
        <input type="checkbox" checked={filter.stale} onChange={() => onFilter({ ...filter, stale: !filter.stale })} />
        Not seen now
      </label>
      <div className="diag mono push" aria-label="Stage times">
        {drone && stageTimes(drone).map(([stage, s]) => (
          <span key={stage}>
            {stage} <span className={s > SLOW_STAGE_S ? "slow" : "value"}>{(s * 1000).toFixed(0)} ms</span>
          </span>
        ))}
        {dropped !== undefined && <span>dropped <span className="value">{dropped}</span></span>}
      </div>
    </footer>
  );
}

// The detector's threshold for this stream: boxes below it reach neither the video nor the map.
function ConfSlider({ sid }: { sid: string }) {
  const [conf, setConf] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const path = `/api/detector/${encodeURIComponent(sid)}/conf`;

  useEffect(() => {
    let current = true; // a late answer for the previous drone must not overwrite this one
    setConf(null);
    setError(null);
    getJSON<{ conf: number }>(path)
      .then((r) => current && setConf(r.conf))
      .catch((e: Error) => current && setError(`Cannot read the detector threshold: ${e.message}`));
    return () => {
      current = false;
    };
  }, [path]);

  const save = (v: number) =>
    send("PUT", path, { conf: v })
      .then(() => setError(null))
      .catch((e: Error) => setError(`Threshold not saved: ${e.message}`));

  if (conf === null) return error ? <span className="error">{error}</span> : null;
  return (
    <label className="inline">
      Box confidence from
      <input type="range" min="0" max="1" step="0.05" value={conf} onChange={(e) => setConf(Number(e.target.value))}
        onPointerUp={(e) => save(Number(e.currentTarget.value))} onKeyUp={(e) => save(Number(e.currentTarget.value))} />
      <span className="mono">{conf.toFixed(2)}</span>
      {error && <span className="error">{error}</span>}
    </label>
  );
}

function trackCrop(sit: Situation, o: WorldObject) {
  for (const [sid, tid] of [...o.tracks].reverse()) {
    const drone = sit.drones.get(sid);
    if (!drone) continue;
    const { preview, dets } = shownFrame(drone);
    const det = dets?.dets.find((d) => d.tid === tid);
    if (preview && dets && det) return { sid, preview, box: det.box, conf: det.conf, img: dets.img };
  }
  return null;
}

type DetailsProps = { sit: Situation; object: WorldObject; onCentre: () => void; onVideo: (sid: string) => void };

function Details({ sit, object: o, onCentre, onVideo }: DetailsProps) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const crop = trackCrop(sit, o);
  const now = worldNow(sit);
  const k = crop ? crop.preview.w / crop.img[0] : 1;
  const [x0, y0, x1, y1] = crop ? crop.box.map((v) => v * k) : [0, 0, 0, 0];
  const side = Math.max(CROP_MIN_PX, CROP_MARGIN * Math.max(x1 - x0, y1 - y0));
  const [sx, sy] = [(x0 + x1 - side) / 2, (y0 + y1 - side) / 2];
  const url = crop?.preview.url;

  useEffect(() => {
    canvas.current?.getContext("2d")?.clearRect(0, 0, CROP_PX, CROP_PX);
  }, [o.oid]);

  useEffect(() => {
    if (!url) return;
    const img = new Image();
    img.onload = () => canvas.current?.getContext("2d")?.drawImage(img, sx, sy, side, side, 0, 0, CROP_PX, CROP_PX);
    img.src = url;
    return () => {
      img.onload = null;
    };
  }, [url, sx, sy, side]);

  const speed = o.vel && o.pred?.length ? Math.hypot(o.vel[0], o.vel[1]) * 3.6 : null;
  const tracks = o.tracks.map(([sid, tid]) => `${droneName(sit, sid)} #${tid}`).join(", ");
  return (
    <div className="details-body">
      <canvas ref={canvas} width={CROP_PX} height={CROP_PX} role="img"
        aria-label={`Frame crop of ${o.oid}`} />
      <div className="details-main">
        <div className="details-head">
          <img src={symbolUrl(o.grp)} alt={`Symbol: ${nameOf(o.grp)}`} />
          <strong>{nameOf(o.grp)}</strong>
          <span className="mono muted">{o.oid}</span>
          <span className={o.state === "active" ? "state ok" : "state stale"}>
            {o.state === "active" ? "active" : "not seen now"}
          </span>
          <div className="push details-actions">
            <button onClick={onCentre}>Centre map</button>
            <button onClick={() => onVideo(crop?.sid ?? o.seen_by[0])}>Show in video</button>
          </div>
        </div>
        <dl className="fields">
          <div><dt>Position</dt><dd className="mono">{o.xy[0].toFixed(0)} E, {o.xy[1].toFixed(0)} N</dd></div>
          <div>
            <dt>Uncertainty</dt>
            <dd className="mono">{o.sigma_m === null ? "none" : `± ${o.sigma_m.toFixed(1)} m (1σ)`}</dd>
          </div>
          <div><dt>Box confidence</dt><dd className="mono">{crop ? crop.conf.toFixed(2) : "not in frame"}</dd></div>
          <div><dt>Motion</dt><dd>{speed === null ? "still" : `${speed.toFixed(0)} km/h`}</dd></div>
          <div><dt>Observations</dt><dd className="mono">{o.n_obs}, {tracks}</dd></div>
          <div><dt>Seen</dt><dd className="mono">since {clock(o.first)}, last {ago(o.last, now)}</dd></div>
        </dl>
      </div>
    </div>
  );
}
