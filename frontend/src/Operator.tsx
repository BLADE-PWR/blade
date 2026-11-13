// View 1: one drone. The main panel (video or map) on the left with the object details under it, the other panel
// and the alert tabs on the right, filters and diagnostics at the bottom.
import type { MainPanel } from "./App";
import { shownFrame, type Situation } from "./situation";
import { Video } from "./Video";

export function Operator({ sit, sid, main }: { sit: Situation; sid: string | null; main: MainPanel }) {
  const drone = sid ? sit.drones.get(sid) : undefined;
  const shown = drone ? shownFrame(drone, Date.now() / 1000) : { preview: null, dets: null };
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
      {sid && <Video sid={sid} shown={shown} geo={drone?.geo} objects={sit.objects} />}
    </section>
  );
  const map = <section className="panel map-panel" aria-label="Mapa">{sit.objects.size} obiektów</section>;
  return (
    <>
      <div className="operator">
        <div className="main-col">
          {main === "video" ? video : map}
          <section className="panel details" aria-label="Szczegóły obiektu" />
        </div>
        <aside className="side-col">
          {main === "video" ? map : video}
          <section className="panel tabs" aria-label="Alerty i obiekty" />
        </aside>
      </div>
      <footer className="bottombar" />
    </>
  );
}
