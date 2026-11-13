// View 1: one drone. The main panel (video or map) on the left with the object details under it, the other panel
// and the alert tabs on the right, filters and diagnostics at the bottom.
import { useEffect, useRef, useState } from "react";

import type { MainPanel } from "./App";
import { nameOf } from "./classes";
import type { Alert, Vec2, WorldObject } from "./contracts";
import { ago, clock, pl } from "./format";
import { AlertList, EventList, ObjectList } from "./Lists";
import { MapView, symbolUrl } from "./MapView";
import { allAlerts, shownFrame, type Situation } from "./situation";
import { Video } from "./Video";

type Tab = "alerts" | "objects" | "events";
const TABS: [Tab, string, (s: Situation) => number][] = [
  ["alerts", "Alerty", (s) => allAlerts(s).length],
  ["objects", "Obiekty", (s) => s.objects.size],
  ["events", "Zdarzenia", (s) => s.events.size],
];

const CROP_PX = 112;
const CROP_MARGIN = 1.6; // crop side as a multiple of the box's longer side
const CROP_MIN_PX = 40; // in preview pixels

type Props = {
  sit: Situation;
  sid: string | null;
  main: MainPanel;
  onMain: (p: MainPanel) => void;
  onDrone: (sid: string) => void;
};

export function Operator({ sit, sid, main, onMain, onDrone }: Props) {
  const [selected, setSelected] = useState<string | null>(null);
  const [centre, setCentre] = useState<{ xy: Vec2; at: number } | null>(null);
  const [tab, setTab] = useState<Tab>("alerts");
  const [alertId, setAlertId] = useState<string | null>(null);
  const now = Date.now() / 1000;
  const drone = sid ? sit.drones.get(sid) : undefined;
  const shown = drone ? shownFrame(drone, now) : { preview: null, dets: null };
  const object = selected ? sit.objects.get(selected) : undefined;
  const alerts = allAlerts(sit);
  const alert = alerts.find((a) => a.aid === alertId) ?? null;
  const pickAlert = (a: Alert) => {
    setAlertId(a.aid === alertId ? null : a.aid);
    if (a.objects.length) setSelected(a.objects[0]);
    if (a.xy) setCentre({ xy: a.xy, at: Date.now() });
  };
  const video = (
    <section className="panel" aria-label="Wideo z drona">
      <div className="panel-head">
        <strong>Kamera</strong>
        <span className="mono muted">{sid}{shown.preview?.frame != null && `, klatka ${shown.preview.frame}`}</span>
        {drone?.geo && (
          <span className="muted">wysokość <span className="mono value">{drone.geo.camera[2].toFixed(0)} m</span></span>
        )}
        <span className="muted push">ramki <span className="mono value">{shown.dets?.dets.length ?? 0}</span></span>
      </div>
      {sid && <Video sid={sid} shown={shown} geo={drone?.geo} objects={sit.objects} selected={selected} />}
    </section>
  );
  const map = (
    <section className={main === "map" ? "panel map-main" : "panel map-panel"} aria-label="Mapa">
      <MapView sit={sit} focus={sid} selected={selected} onSelect={setSelected} centre={centre} alert={alert}
        onExpand={main === "video" ? () => onMain("map") : undefined} />
    </section>
  );
  return (
    <>
      <div className="operator">
        <div className="main-col">
          {main === "video" ? video : map}
          <section className="panel details" aria-label="Szczegóły obiektu">
            {object ? (
              <Details sit={sit} object={object} onCentre={() => setCentre({ xy: object.xy, at: Date.now() })}
                onVideo={() => {
                  onDrone(object.tracks.at(-1)?.[0] ?? object.seen_by[0]);
                  onMain("video");
                }} />
            ) : (
              <p className="muted hint">Wybierz obiekt na mapie albo na liście.</p>
            )}
          </section>
        </div>
        <aside className="side-col">
          {main === "video" ? map : video}
          <section className="panel tabs" aria-label="Alerty i obiekty">
            <div className="tablist" role="tablist">
              {TABS.map(([key, name, count]) => (
                <button key={key} role="tab" aria-selected={tab === key} onClick={() => setTab(key)}>
                  {name} <span className="mono muted">{count(sit)}</span>
                </button>
              ))}
            </div>
            {tab === "alerts" && <AlertList alerts={alerts} picked={alert?.aid ?? null} onPick={pickAlert} now={now} />}
            {tab === "objects" && (
              <ObjectList objects={[...sit.objects.values()]} selected={selected} onSelect={setSelected} now={now} />
            )}
            {tab === "events" && (
              <EventList events={[...sit.events.values()]} now={now}
                onPick={(e) => setCentre({ xy: e.xy, at: Date.now() })} />
            )}
          </section>
        </aside>
      </div>
      <footer className="bottombar" />
    </>
  );
}

/** -> the picture and box of the object's newest track that is in the frame shown now */
function trackCrop(sit: Situation, o: WorldObject) {
  for (const [sid, tid] of [...o.tracks].reverse()) {
    const drone = sit.drones.get(sid);
    if (!drone) continue;
    const { preview, dets } = shownFrame(drone, Date.now() / 1000);
    const det = dets?.dets.find((d) => d.tid === tid);
    if (preview && dets && det) return { preview, box: det.box, conf: det.conf, img: dets.img };
  }
  return null;
}

type DetailsProps = { sit: Situation; object: WorldObject; onCentre: () => void; onVideo: () => void };

function Details({ sit, object: o, onCentre, onVideo }: DetailsProps) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const crop = trackCrop(sit, o);
  const now = Date.now() / 1000;
  // the crop square in preview pixels; the canvas keeps the last one while the object is out of the frame
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
  const tracks = o.tracks.map(([sid, tid]) => `${sid} #${tid}`).join(", ");
  return (
    <div className="details-body">
      <canvas ref={canvas} width={CROP_PX} height={CROP_PX} role="img"
        aria-label={`Wycinek kadru z obiektem ${o.oid}`} />
      <div className="details-main">
        <div className="details-head">
          <img src={symbolUrl(o.grp)} alt={`Symbol: ${nameOf(o.grp)}`} />
          <strong>{nameOf(o.grp)}</strong>
          <span className="mono muted">{o.oid}</span>
          <span className={o.state === "active" ? "state ok" : "state stale"}>
            {o.state === "active" ? "aktywny" : "nieobserwowany"}
          </span>
          <div className="push details-actions">
            <button onClick={onCentre}>Wycentruj mapę</button>
            <button onClick={onVideo}>Pokaż w wideo</button>
          </div>
        </div>
        <dl className="fields">
          <div><dt>Pozycja</dt><dd className="mono">{o.xy[0].toFixed(0)} E, {o.xy[1].toFixed(0)} N</dd></div>
          <div>
            <dt>Niepewność</dt>
            <dd className="mono">{o.sigma_m === null ? "brak" : `± ${pl(o.sigma_m, 1)} m (1σ)`}</dd>
          </div>
          <div><dt>Pewność ramki</dt><dd className="mono">{crop ? pl(crop.conf, 2) : "brak w kadrze"}</dd></div>
          <div><dt>Ruch</dt><dd>{speed === null ? "stoi" : `${speed.toFixed(0)} km/h`}</dd></div>
          <div><dt>Obserwacje</dt><dd className="mono">{o.n_obs}, {tracks}</dd></div>
          <div><dt>Widziany</dt><dd className="mono">od {clock(o.first)}, ostatnio {ago(o.last, now)}</dd></div>
        </dl>
      </div>
    </div>
  );
}
