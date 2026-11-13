// One drone's video preview with the detector's boxes and, optionally, the mark at the centre of the frame.
import { colorOf, nameOf } from "./classes";
import type { GeoData, WorldObject } from "./contracts";
import type { Drone, Preview } from "./situation";

const pct = (v: number, of: number) => `${(100 * v) / of}%`;

/** -> "sid:tid" of every track behind a world object, mapped to the object's id */
function trackOids(objects: Map<string, WorldObject>): Map<string, string> {
  const oids = new Map<string, string>();
  for (const o of objects.values()) for (const [sid, tid] of o.tracks) oids.set(`${sid}:${tid}`, o.oid);
  return oids;
}

function centreLabel(geo: GeoData): string | null {
  if (!geo.centre) return null;
  const [cx, cy, cz] = geo.camera;
  const range = Math.hypot(geo.centre[0] - cx, geo.centre[1] - cy, geo.centre[2] - cz);
  const gsd = geo.gsd_m === null ? "" : `, ${geo.gsd_m.toFixed(2).replace(".", ",")} m/px`;
  return `Środek kadru: ${range.toFixed(0)} m${gsd}`;
}

type Props = {
  sid: string;
  shown: { preview: Preview | null; dets: Drone["dets"] };
  geo?: GeoData | null;
  objects: Map<string, WorldObject>;
  selected?: string | null;
};

export function Video({ sid, shown, geo, objects, selected }: Props) {
  const { preview, dets } = shown;
  if (!preview) return <div className="video empty">Brak obrazu z drona {sid}</div>;
  const oids = trackOids(objects);
  const [w, h] = dets?.img ?? preview.src;
  const centre = geo ? centreLabel(geo) : null;
  return (
    <div className="video">
      <img src={preview.url} alt={`Obraz z drona ${sid}, klatka ${preview.frame ?? "?"}`} />
      {dets?.dets.map((d, i) => {
        const oid = d.tid === null ? undefined : oids.get(`${sid}:${d.tid}`);
        const [x0, y0, x1, y1] = d.box;
        const color = oid && oid === selected ? "#ffffff" : colorOf(d.grp);
        return (
          <div key={i} className="box" style={{ left: pct(x0, w), top: pct(y0, h), width: pct(x1 - x0, w),
            height: pct(y1 - y0, h), borderColor: color }}>
            <span className="box-label" style={{ background: color }}>
              {oid} {nameOf(d.grp)} {d.conf.toFixed(2)}
            </span>
          </div>
        );
      })}
      {centre && (
        <>
          <svg className="centre" width="64" height="64" viewBox="0 0 64 64" aria-hidden="true">
            <path d="M0 32 H24 M40 32 H64 M32 0 V24 M32 40 V64" stroke="#000" strokeOpacity="0.55" strokeWidth="3.5" />
            <path d="M0 32 H24 M40 32 H64 M32 0 V24 M32 40 V64" stroke="#fff" strokeWidth="1.5" />
          </svg>
          <span className="centre-label mono">{centre}</span>
        </>
      )}
    </div>
  );
}
