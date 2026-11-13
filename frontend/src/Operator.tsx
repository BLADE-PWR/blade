// View 1: one drone. The main panel (video or map) on the left with the object details under it, the other panel
// and the alert tabs on the right, filters and diagnostics at the bottom.
import type { MainPanel } from "./App";
import type { Situation } from "./situation";

export function Operator({ sit, sid, main }: { sit: Situation; sid: string | null; main: MainPanel }) {
  const video = <section className="panel" aria-label="Wideo z drona">{sid}</section>;
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
