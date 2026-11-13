// The map: orthophoto tiles from the terrain service in flat Arma metres (Leaflet CRS.Simple, lat = y north,
// lng = x east), APP-6 symbols from milsymbol, drones with their camera footprints, motion and uncertainty.
import L from "leaflet";
import ms, { type Symbol as MilSymbol } from "milsymbol";
import { useEffect, useRef, useState } from "react";

import { colorOf, nameOf, SEVERITY_COLORS } from "./classes";
import type { Alert, Vec2, WorldObject } from "./contracts";
import { getJSON } from "./gateway";
import { type Filter, type Situation, visible, worldNow } from "./situation";

const TILE_PX = 256;
const MIN_ZOOM = -5; // 32 m/px, the coarsest orthophoto level
const MAX_ZOOM = 2; // 0.25 m/px
const START_ZOOM = 1;
const FIT_PAD_PX = 40;
const SYMBOL_PX = 20;
const STACK_PX = 24; // symbols closer than this on screen are drawn as one, with a count
const ARROW_S = 10; // s of travel the motion arrow shows
const CONE_K = 2; // cone and uncertainty circles in sigmas
const HORIZON_S = 30; // prediction shown for every moving object; the selected one shows all
const UNCERTAIN_M = 15; // others get an uncertainty circle from this sigma (decision rule uncertain_position)
const ALERT_RING_PX = 16;
const STACK_ORDER = ["tank", "military_vehicle", "soldier"];
const CIRCLE_POINTS = 24;
const SIDC: Record<string, string> = {
  soldier: "SHGPUCI--------",
  tank: "SHGPUCA--------",
  military_vehicle: "SHGPUCAW-------",
};
const DRONE_SIDC = "SFAPMFQ--------";

type Layers = { drones: boolean; motion: boolean; uncertainty: boolean; truth: boolean };
const LAYER_NAMES: [keyof Layers, string][] = [
  ["drones", "Drones and camera footprints"],
  ["motion", "Motion and prediction"],
  ["uncertainty", "Uncertainty"],
  ["truth", "True positions"],
];

type Meta = { extent: [number, number, number, number] };

const ll = (xy: Vec2 | number[]) => L.latLng(xy[1], xy[0]);

export type Background = "auto" | "hillshade";

class OrthoTiles extends L.TileLayer {
  constructor(private mapId: string, private layer: Background, bounds: L.LatLngBounds) {
    super("", { tileSize: TILE_PX, minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM, bounds });
  }

  getTileUrl(c: L.Coords): string {
    const side = TILE_PX / 2 ** c.z; // metres
    const x0 = c.x * side;
    const y1 = -c.y * side;
    const q = new URLSearchParams({ x0: `${x0}`, y0: `${y1 - side}`, x1: `${x0 + side}`, y1: `${y1}`,
      mpp: `${side / TILE_PX}`, layer: this.layer });
    return `/api/maps/${this.mapId}/crop?${q}`;
  }
}

const symbols = new Map<string, MilSymbol>(); // drawing a symbol is slow, and the same few are drawn every frame
function symbol(sidc: string): MilSymbol {
  let s = symbols.get(sidc);
  if (!s) {
    s = new ms.Symbol(sidc, { size: SYMBOL_PX });
    symbols.set(sidc, s);
  }
  return s;
}

const symbolSvg = (sidc: string) => symbol(sidc).asSVG();

export const symbolUrl = (grp: string) => symbol(SIDC[grp] ?? SIDC.soldier).toDataURL();

function objectIcon(o: WorldObject, count: number, selected: boolean): L.DivIcon {
  const badge = count > 1 ? `<span class="badge">+${count - 1}</span>` : "";
  const label = selected ? `<span class="sym-label selected">${o.oid}</span>` : "";
  const cls = `sym${o.state === "stale" ? " stale" : ""}`;
  return L.divIcon({ className: cls, html: symbolSvg(SIDC[o.grp] ?? SIDC.soldier) + badge + label,
    iconSize: [SYMBOL_PX * 1.5, SYMBOL_PX * 1.5] });
}

function droneIcon(sid: string, focus: boolean): L.DivIcon {
  return L.divIcon({ className: `sym drone${focus ? " focus" : ""}`, iconSize: [SYMBOL_PX * 1.5, SYMBOL_PX * 1.5],
    html: symbolSvg(DRONE_SIDC) + `<span class="sym-label">${sid}</span>` });
}

function eventIcon(kind: string, age: number): L.DivIcon {
  const star = "30,16 21.5,18.3 25.9,25.9 18.3,21.5 16,30 13.7,21.5 6.1,25.9 10.5,18.3 2,16 10.5,13.7 6.1,6.1 " +
    "13.7,10.5 16,2 18.3,10.5 25.9,6.1 21.5,13.7";
  return L.divIcon({ className: "sym", iconSize: [18, 18], html:
    `<svg width="18" height="18" viewBox="0 0 32 32"><polygon points="${star}" fill="${colorOf(kind)}" ` +
    `stroke="#000" stroke-width="1.5"/></svg><span class="sym-label">${nameOf(kind)}, ${age.toFixed(0)} s</span>` });
}

/** -> objects grouped by screen distance, the most important group first in each stack */
function stacks(map: L.Map, objects: WorldObject[], selected: string | null): WorldObject[][] {
  const rank = (o: WorldObject) => (o.oid === selected ? -1 : STACK_ORDER.indexOf(o.grp));
  const sorted = [...objects].sort((a, b) => rank(a) - rank(b));
  const out: { at: L.Point; members: WorldObject[] }[] = [];
  for (const o of sorted) {
    const at = map.latLngToContainerPoint(ll(o.xy));
    const near = out.find((s) => s.at.distanceTo(at) < STACK_PX);
    if (near) near.members.push(o);
    else out.push({ at, members: [o] });
  }
  return out.map((s) => s.members);
}

/** -> convex hull (monotone chain) of points */
function hull(pts: Vec2[]): Vec2[] {
  const p = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o: Vec2, a: Vec2, b: Vec2) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const half = (list: Vec2[]) => {
    const h: Vec2[] = [];
    for (const q of list) {
      while (h.length >= 2 && cross(h[h.length - 2], h[h.length - 1], q) <= 0) h.pop();
      h.push(q);
    }
    return h.slice(0, -1);
  };
  return [...half(p), ...half([...p].reverse())];
}

function circle(xy: Vec2, r: number): Vec2[] {
  return Array.from({ length: CIRCLE_POINTS }, (_, i) => {
    const a = (i / CIRCLE_POINTS) * 2 * Math.PI;
    return [xy[0] + r * Math.cos(a), xy[1] + r * Math.sin(a)] as Vec2;
  });
}

function drawMotion(group: L.LayerGroup, o: WorldObject, selected: boolean, now: number): void {
  // pred is anchored at `last` and not republished while stale: hide horizons already in the past
  const preds = (o.pred ?? []).filter((p) => o.last + p.dt_s > now && (selected || p.dt_s <= HORIZON_S));
  if (!o.vel || preds.length === 0) return;
  const color = colorOf(o.grp);
  const pts = [o.xy, ...preds.map((p) => p.xy)].flatMap((xy, i) =>
    circle(xy, CONE_K * (i === 0 ? (o.sigma_m ?? 0) : preds[i - 1].sigma_m)));
  L.polygon(hull(pts).map(ll), { color, weight: 1, opacity: 0.7, dashArray: "4 4", fillOpacity: 0.12 })
    .addTo(group);
  const tip: Vec2 = [o.xy[0] + o.vel[0] * ARROW_S, o.xy[1] + o.vel[1] * ARROW_S];
  L.polyline([ll(o.xy), ll(tip)], { color, weight: 3 }).addTo(group);
  for (const p of preds) L.circleMarker(ll(p.xy), { radius: 3, color, fillOpacity: 1, weight: 0 }).addTo(group);
}

function drawDrones(group: L.LayerGroup, sit: Situation, focus: string | null): void {
  for (const d of sit.drones.values()) {
    if (!d.geo) continue;
    const own = d.sid === focus;
    const fp = d.geo.footprint.filter((p): p is Vec2 => p !== null);
    L.polygon(fp.map(ll), { color: own ? "#ffffff" : "#c9d1d9", weight: own ? 2 : 1.5,
      dashArray: own ? undefined : "6 4", fillOpacity: own ? 0.08 : 0.03 }).addTo(group);
    L.polyline(d.route.map(ll), { color: "#80e0ff", weight: 2, dashArray: "6 5" }).addTo(group);
    if (d.geo.centre) {
      L.polyline([ll(d.geo.camera), ll(d.geo.centre)], { color: "#ffffff", weight: 1.2, opacity: 0.5,
        dashArray: "4 5" }).addTo(group);
    }
    L.marker(ll(d.geo.camera), { interactive: false, icon: droneIcon(d.sid, own) }).addTo(group);
  }
}

function drawObjects(group: L.LayerGroup, objects: WorldObject[], selected: string | null, layers: Layers,
  now: number): void {
  for (const o of objects) {
    const sel = o.oid === selected;
    if (layers.motion) drawMotion(group, o, sel, now);
    if (layers.uncertainty && o.sigma_m !== null && (sel || o.sigma_m >= UNCERTAIN_M)) {
      L.circle(ll(o.xy), { radius: CONE_K * o.sigma_m, color: "#ffffff", weight: 1.5, dashArray: "3 3",
        fillOpacity: 0.08 }).addTo(group);
    }
  }
}

function drawTruth(group: L.LayerGroup, sit: Situation): void {
  for (const t of sit.truth) {
    const match = t.oid ? sit.objects.get(t.oid) : undefined;
    if (match) L.polyline([ll(t.xy), ll(match.xy)], { color: "#ffffff", weight: 1, dashArray: "3 3" }).addTo(group);
    const html = t.err_m === null ? "" : `<span class="sym-label">${t.err_m.toFixed(0)} m</span>`;
    L.marker(ll(t.xy), { interactive: false, icon: L.divIcon({ className: "truth", iconSize: [14, 14], html }) })
      .addTo(group);
  }
}

function drawEvents(group: L.LayerGroup, sit: Situation, filter: Filter, now: number): void {
  for (const e of sit.events.values()) {
    const age = now - e.t;
    if (age >= e.ttl_s || filter.hidden.has(e.kind)) continue;
    L.marker(ll(e.xy), { interactive: false, opacity: 1 - (0.7 * age) / e.ttl_s, icon: eventIcon(e.kind, age) })
      .addTo(group);
  }
}

function drawAlert(group: L.LayerGroup, sit: Situation, a: Alert): void {
  const color = SEVERITY_COLORS[a.severity];
  const known = a.objects.map((oid) => sit.objects.get(oid)?.xy).filter((xy): xy is Vec2 => xy !== undefined);
  const rings = known.length ? known : a.xy ? [a.xy] : [];
  for (const xy of rings) {
    L.circleMarker(ll(xy), { radius: ALERT_RING_PX, color, weight: 3, fill: false, interactive: false }).addTo(group);
  }
}

type Markers = Map<string, { marker: L.Marker; key: string }>;

/** Moves the object markers in place (new ones only for new stacks), so a click is not lost to a redraw. */
function syncMarkers(map: L.Map, markers: Markers, objects: WorldObject[], selected: string | null,
  onSelect: (oid: string) => void): void {
  const seen = new Set<string>();
  for (const members of stacks(map, objects, selected)) {
    const top = members[0];
    const sel = top.oid === selected;
    const key = `${top.grp}|${top.state}|${members.length}|${sel}`;
    seen.add(top.oid);
    const known = markers.get(top.oid);
    if (known) {
      known.marker.setLatLng(ll(top.xy));
      if (known.key !== key) known.marker.setIcon(objectIcon(top, members.length, sel));
      known.key = key;
    } else {
      const marker = L.marker(ll(top.xy), { icon: objectIcon(top, members.length, sel),
        title: `${nameOf(top.grp)} ${top.oid}`, keyboard: true }).on("click", () => onSelect(top.oid)).addTo(map);
      markers.set(top.oid, { marker, key });
    }
  }
  for (const [oid, { marker }] of markers) {
    if (!seen.has(oid)) {
      marker.remove();
      markers.delete(oid);
    }
  }
}

type Props = {
  sit: Situation;
  focus: string | null;
  selected: string | null;
  onSelect: (oid: string) => void;
  onExpand?: () => void;
  centre?: { xy: Vec2; at: number } | null;
  alert?: Alert | null;
  filter: Filter;
  overview?: boolean;
  layer?: Background;
};

export function MapView(props: Props) {
  const { sit, focus, selected, onSelect, onExpand, centre, alert, filter, overview, layer = "auto" } = props;
  const box = useRef<HTMLDivElement>(null);
  const markers = useRef<Markers>(new Map());
  const [map, setMap] = useState<L.Map | null>(null);
  const [vectors] = useState(() => L.layerGroup());
  const [layers, setLayers] = useState<Layers>({ drones: true, motion: true, uncertainty: true, truth: false });
  const [showLayers, setShowLayers] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [centred, setCentred] = useState(false);
  const [, setMoved] = useState(0);
  const mapId = sit.session?.map ?? "altis";

  useEffect(() => {
    const m = L.map(box.current!, { crs: L.CRS.Simple, minZoom: MIN_ZOOM, maxZoom: MAX_ZOOM, zoomControl: false,
      attributionControl: false });
    L.control.zoom({ position: "topright", zoomInTitle: "Zoom in", zoomOutTitle: "Zoom out" }).addTo(m);
    L.control.scale({ position: "bottomright", imperial: false }).addTo(m);
    vectors.addTo(m);
    const moved = () => setMoved((n) => n + 1);
    m.on("zoomend moveend", moved);
    const resize = new ResizeObserver(() => m.invalidateSize());
    resize.observe(box.current!);
    setMap(m);
    const kept = markers.current;
    return () => {
      resize.disconnect();
      m.off("zoomend moveend", moved);
      kept.clear();
      vectors.remove();
      m.remove();
    };
  }, [vectors]);

  useEffect(() => {
    if (!map) return;
    let tiles: OrthoTiles | null = null;
    let stopped = false;
    getJSON<Meta>(`/api/maps/${mapId}/meta`)
      .then(({ extent: [x0, y0, x1, y1] }) => {
        if (stopped) return;
        tiles = new OrthoTiles(mapId, layer, L.latLngBounds(ll([x0, y0]), ll([x1, y1]))).addTo(map);
      })
      .catch((e: Error) => setError(`Could not load map ${mapId} (${e.message}).`));
    return () => {
      stopped = true;
      tiles?.remove();
    };
  }, [map, mapId, layer]);

  const now = worldNow(sit);
  const focusGeo = focus ? sit.drones.get(focus)?.geo : null;

  const fitDrones = () => {
    const pts = [...sit.drones.values()].flatMap((d) => (d.geo ? [d.geo.camera, ...d.geo.footprint] : []))
      .flatMap((p) => (p ? [ll(p)] : []));
    if (map && pts.length) map.fitBounds(L.latLngBounds(pts), { padding: [FIT_PAD_PX, FIT_PAD_PX] });
  };

  useEffect(() => {
    if (!map || centred || !focusGeo?.centre) return;
    if (overview) fitDrones();
    else map.setView(ll(focusGeo.centre), START_ZOOM);
    setCentred(true);
  });

  useEffect(() => {
    if (map && centred && centre) map.panTo(ll(centre.xy));
  }, [map, centred, centre]);

  useEffect(() => {
    if (!map || !centred) return;
    const objects = [...sit.objects.values()].filter((o) => visible(o, filter));
    vectors.clearLayers();
    if (layers.drones) drawDrones(vectors, sit, focus);
    drawObjects(vectors, objects, selected, layers, now);
    if (layers.truth) drawTruth(vectors, sit);
    drawEvents(vectors, sit, filter, now);
    if (alert) drawAlert(vectors, sit, alert);
    syncMarkers(map, markers.current, objects, selected, onSelect);
  });

  const away = map && centred && !overview && focusGeo && layers.drones &&
    !map.getBounds().contains(ll(focusGeo.camera)) ? map.getCenter() : null;

  return (
    <div className="map">
      <div ref={box} className="map-box" />
      {error && <p className="error map-error">{error}</p>}
      {away && focusGeo && (
        <span className="map-away">
          Drone off the map, {Math.hypot(focusGeo.camera[0] - away.lng, focusGeo.camera[1] - away.lat).toFixed(0)} m
          from the centre
        </span>
      )}
      <div className="map-layers">
        <button aria-expanded={showLayers} onClick={() => setShowLayers(!showLayers)}>Layers</button>
        {showLayers && (
          <div className="map-layer-list">
            {LAYER_NAMES.map(([k, name]) => (
              <label key={k}>
                <input type="checkbox" checked={layers[k]} onChange={() => setLayers({ ...layers, [k]: !layers[k] })} />
                {name}
              </label>
            ))}
          </div>
        )}
      </div>
      <div className="map-actions">
        {overview && <button onClick={fitDrones}>Show all drones</button>}
        {onExpand && <button onClick={onExpand}>Map to the main panel</button>}
      </div>
    </div>
  );
}
