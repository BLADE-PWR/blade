// The map in Arma metres (CRS.Simple, lat = y, lng = x): orthophoto tiles, APP-6 symbols, drones and motion.
import L from "leaflet";
import ms, { type Symbol as MilSymbol } from "milsymbol";
import { useEffect, useRef, useState } from "react";

import { colorOf, GROUPS, nameOf, SEVERITY_COLORS } from "./classes";
import type { Alert, Cluster, Prediction, Vec2, WorldObject } from "./contracts";
import { inside, loadWeights, MAX_WEIGHT, saveWeights, strength, type Weights, weightOf } from "./force";
import { getJSON } from "./gateway";
import { type Drone, droneName, type Filter, type Situation, shownMap, visible, worldNow } from "./situation";

const TILE_PX = 256;
const MIN_ZOOM = -5; // 32 m/px
const MAX_ZOOM = 2; // 0.25 m/px
const START_ZOOM = 1;
const FIT_PAD_PX = 40;
const SYMBOL_PX = 20;
const STACK_PX = 24; // closer symbols are drawn as one
const ARROW_S = 10; // s
const CONE_K = 2; // sigmas
const HORIZON_S = 30; // s, the selected object shows every horizon
const UNCERTAIN_M = 15; // m, like the uncertain_position rule
const ALERT_RING_PX = 16;
const STACK_ORDER = ["tank", "military_vehicle", "soldier"];
const CIRCLE_POINTS = 24;
const HEAT_RADIUS_M = 60;
const HEAT_COLOR = "255, 110, 0";
const GROUP_COLOR = "#ffd166";
const CLICK_PX = 5; // a press and release this close are a click
const STILL_MS = 1; // m/s, like the world model: slower is standing still
const SIDC: Record<string, string> = {
  soldier: "SHGPUCI--------",
  tank: "SHGPUCA--------",
  military_vehicle: "SHGPUCAW-------",
};
const DRONE_SIDC = "SFAPMFQ--------";

type Layers = { drones: boolean; motion: boolean; uncertainty: boolean; truth: boolean; groups: boolean;
  density: boolean };
const LAYER_NAMES: [keyof Layers, string][] = [
  ["drones", "Drones and camera footprints"],
  ["motion", "Motion and prediction"],
  ["uncertainty", "Uncertainty"],
  ["groups", "Force groups"],
  ["density", "Density of forces"],
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
    const side = TILE_PX / 2 ** c.z; // m
    const x0 = c.x * side;
    const y1 = -c.y * side;
    const q = new URLSearchParams({ x0: `${x0}`, y0: `${y1 - side}`, x1: `${x0 + side}`, y1: `${y1}`,
      mpp: `${side / TILE_PX}`, layer: this.layer });
    return `/api/maps/${this.mapId}/crop?${q}`;
  }
}

const symbols = new Map<string, MilSymbol>(); // drawing one is slow
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

function droneIcon(name: string, focus: boolean): L.DivIcon {
  return L.divIcon({ className: `sym drone${focus ? " focus" : ""}`, iconSize: [SYMBOL_PX * 1.5, SYMBOL_PX * 1.5],
    html: symbolSvg(DRONE_SIDC) + `<span class="sym-label">${name}</span>` });
}

function eventIcon(kind: string, age: number): L.DivIcon {
  const star = "30,16 21.5,18.3 25.9,25.9 18.3,21.5 16,30 13.7,21.5 6.1,25.9 10.5,18.3 2,16 10.5,13.7 6.1,6.1 " +
    "13.7,10.5 16,2 18.3,10.5 25.9,6.1 21.5,13.7";
  return L.divIcon({ className: "sym", iconSize: [18, 18], html:
    `<svg width="18" height="18" viewBox="0 0 32 32"><polygon points="${star}" fill="${colorOf(kind)}" ` +
    `stroke="#000" stroke-width="1.5"/></svg><span class="sym-label">${nameOf(kind)}, ${age.toFixed(0)} s</span>` });
}

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
  // pred is not republished while stale: hide the horizons already passed
  const preds = (o.pred ?? []).filter((p) => o.last + p.dt_s > now && (selected || p.dt_s <= HORIZON_S));
  if (!o.vel || preds.length === 0) return;
  const color = colorOf(o.grp);
  const pts = [o.xy, ...preds.map((p) => p.xy)].flatMap((xy, i) =>
    circle(xy, CONE_K * (i === 0 ? (o.sigma_m ?? 0) : preds[i - 1].sigma_m)));
  L.polygon(hull(pts).map(ll), { color, weight: 1, opacity: 0.7, dashArray: "4 4", fillOpacity: 0.12 })
    .addTo(group);
  const tip: Vec2 = [o.xy[0] + o.vel[0] * ARROW_S, o.xy[1] + o.vel[1] * ARROW_S];
  L.polyline([ll(o.xy), ll(tip)], { color, weight: 3 }).addTo(group);
  L.polyline([o.xy, ...preds.map((p) => p.xy)].map(ll), { color, weight: 1, opacity: 0.6, dashArray: "2 5" })
    .addTo(group);
  for (const p of preds) horizonMarker(p, color, selected).addTo(group);
}

function horizonMarker(p: Prediction, color: string, labelled: boolean): L.Marker {
  const ring = p.dt_s > HORIZON_S ? " ring" : "";
  const label = labelled ? `<span class="horizon-label">${Math.round(p.dt_s)} s</span>` : "";
  const icon = L.divIcon({ className: "horizon", iconSize: [12, 12],
    html: `<span class="horizon-dot${ring}" style="--c:${color}"></span>${label}` });
  return L.marker(ll(p.xy), { icon, interactive: false, keyboard: false });
}

const onMap = (d: Drone, mapId: string | undefined) => !!d.geo && d.geo.map === mapId;

function drawDrones(group: L.LayerGroup, sit: Situation, focus: string | null, mapId: string | undefined): void {
  for (const d of sit.drones.values()) {
    if (!d.geo || !onMap(d, mapId)) continue;
    const own = d.sid === focus;
    const fp = d.geo.footprint.filter((p): p is Vec2 => p !== null);
    L.polygon(fp.map(ll), { color: own ? "#ffffff" : "#c9d1d9", weight: own ? 2 : 1.5,
      dashArray: own ? undefined : "6 4", fillOpacity: own ? 0.08 : 0.03 }).addTo(group);
    L.polyline(d.route.map(ll), { color: "#80e0ff", weight: 2, dashArray: "6 5" }).addTo(group);
    if (d.geo.centre) {
      L.polyline([ll(d.geo.camera), ll(d.geo.centre)], { color: "#ffffff", weight: 1.2, opacity: 0.5,
        dashArray: "4 5" }).addTo(group);
    }
    L.marker(ll(d.geo.camera), { interactive: false, icon: droneIcon(droneName(sit, d.sid), own) }).addTo(group);
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

/** The density of forces: a soft spot per object, as strong as its class weight; overlapping spots add up. */
class HeatLayer extends L.Layer {
  private canvas = L.DomUtil.create("canvas", "heat") as HTMLCanvasElement;
  private spots: { xy: Vec2; w: number }[] = [];
  private map: L.Map | null = null;

  onAdd(map: L.Map): this {
    this.map = map;
    map.getPanes().overlayPane.appendChild(this.canvas);
    map.on("moveend zoomend resize", this.draw, this);
    this.draw();
    return this;
  }

  onRemove(map: L.Map): this {
    map.off("moveend zoomend resize", this.draw, this);
    this.canvas.remove();
    this.map = null;
    return this;
  }

  set(spots: { xy: Vec2; w: number }[]): void {
    this.spots = spots;
    this.draw();
  }

  private draw(): void {
    const map = this.map;
    if (!map) return;
    const size = map.getSize();
    const c = this.canvas;
    c.width = size.x;
    c.height = size.y;
    L.DomUtil.setPosition(c, map.containerPointToLayerPoint([0, 0]));
    const ctx = c.getContext("2d")!;
    const a = map.latLngToContainerPoint(ll([0, 0]));
    const b = map.latLngToContainerPoint(ll([1, 0]));
    const r = Math.max(8, Math.hypot(b.x - a.x, b.y - a.y) * HEAT_RADIUS_M); // px
    ctx.globalCompositeOperation = "lighter";
    for (const s of this.spots) {
      const p = map.latLngToContainerPoint(ll(s.xy));
      if (p.x < -r || p.y < -r || p.x > size.x + r || p.y > size.y + r) continue;
      const alpha = Math.min(1, s.w / MAX_WEIGHT) * 0.45;
      const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r);
      g.addColorStop(0, `rgba(${HEAT_COLOR}, ${alpha})`);
      g.addColorStop(1, `rgba(${HEAT_COLOR}, 0)`);
      ctx.fillStyle = g;
      ctx.fillRect(p.x - r, p.y - r, 2 * r, 2 * r);
    }
  }
}

function drawClusters(group: L.LayerGroup, clusters: Cluster[], weights: Weights, picked: string | null): void {
  const top = Math.max(1, ...clusters.map((c) => strength(c, weights)));
  for (const c of clusters) {
    const sel = c.cid === picked;
    const poly = L.polygon(c.hull.map(ll), { color: GROUP_COLOR, weight: sel ? 3 : 1.5, dashArray: "6 4",
      fillOpacity: 0.06 + 0.3 * (strength(c, weights) / top), interactive: false }); // the map takes the click
    poly.addTo(group);
    if (c.vel && Math.hypot(...c.vel) >= STILL_MS) {
      const tip: Vec2 = [c.centre[0] + c.vel[0] * ARROW_S, c.centre[1] + c.vel[1] * ARROW_S];
      L.polyline([ll(c.centre), ll(tip)], { color: GROUP_COLOR, weight: 4, interactive: false }).addTo(group);
    }
    L.marker(ll(c.centre), { interactive: false, icon: L.divIcon({ className: "group-label", iconSize: [0, 0],
      html: `<span class="sym-label">${c.objects.length}</span>` }) }).addTo(group);
  }
}

function drawTruth(group: L.LayerGroup, sit: Situation): void {
  for (const t of sit.truth) {
    const match = t.oid ? sit.objects.get(t.oid) : undefined;
    if (match) L.polyline([ll(t.xy), ll(match.xy)], { color: "#ffffff", weight: 1, dashArray: "3 3" }).addTo(group);
    const html = t.err_m === undefined ? "" : `<span class="sym-label">${t.err_m.toFixed(0)} m</span>`;
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

// markers are moved, not redrawn, so a click is not lost
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

/** How much each class counts in the strength of a group and in the density of forces. */
function WeightPicker({ weights, onWeights }: { weights: Weights; onWeights: (w: Weights) => void }) {
  return (
    <div className="weights" role="group" aria-label="Class weights">
      <span className="muted">Weight of a class</span>
      {GROUPS.map((grp) => (
        <label key={grp}>
          {nameOf(grp)}
          <input type="range" min="0" max={MAX_WEIGHT} step="1" value={weightOf(weights, grp)}
            onChange={(e) => onWeights({ ...weights, [grp]: Number(e.target.value) })} />
          <span className="mono">{weightOf(weights, grp)}</span>
        </label>
      ))}
    </div>
  );
}

function motion(c: Cluster): string {
  const speed = c.vel ? Math.hypot(...c.vel) : 0;
  if (!c.vel || speed < STILL_MS) return "Standing still";
  const heading = (Math.atan2(c.vel[0], c.vel[1]) * 180 / Math.PI + 360) % 360;
  return `Moving ${(speed * 3.6).toFixed(0)} km/h, heading ${heading.toFixed(0)} deg`;
}

function GroupPanel({ group, sit, weights, onClose }: { group: Cluster; sit: Situation; weights: Weights;
  onClose: () => void }) {
  const stale = group.objects.filter((oid) => sit.objects.get(oid)?.state === "stale").length;
  const composition = Object.entries(group.composition).map(([grp, n]) => `${nameOf(grp)} ${n}`).join(", ");
  return (
    <div className="map-group panel" role="dialog" aria-label="Force group">
      <div className="panel-head">
        <strong>Group of {group.objects.length}</strong>
        <button onClick={onClose}>Close</button>
      </div>
      <p>{composition}</p>
      <p className="muted">{motion(group)}</p>
      <p className="muted">
        Strength {strength(group, weights).toFixed(0)}, {(group.area_m2 / 10000).toFixed(1)} ha
        {stale > 0 && `, ${stale} not seen lately`}
      </p>
    </div>
  );
}

export function MapView(props: Props) {
  const { sit, focus, selected, onSelect, onExpand, centre, alert, filter, overview, layer = "auto" } = props;
  const box = useRef<HTMLDivElement>(null);
  const markers = useRef<Markers>(new Map());
  const [map, setMap] = useState<L.Map | null>(null);
  const [vectors] = useState(() => L.layerGroup());
  const [heat] = useState(() => new HeatLayer());
  const [layers, setLayers] = useState<Layers>({ drones: true, motion: true, uncertainty: true, truth: false,
    groups: true, density: false });
  const [pickedGroup, setPickedGroup] = useState<string | null>(null);
  const shownGroups = useRef<Cluster[]>([]); // the layers are redrawn all the time and lose clicks: a group is found by where the press ends
  const [weights, setWeights] = useState<Weights>(loadWeights);
  const changeWeights = (w: Weights) => {
    setWeights(w);
    saveWeights(w);
  };
  const [showLayers, setShowLayers] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [centredOn, setCentredOn] = useState<string | undefined>(); // the map the view was last centred on
  const [, setMoved] = useState(0);
  const mapId = shownMap(sit, focus); // none until a world has reported

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
    let down: L.Point | null = null;
    const press = (e: L.LeafletMouseEvent) => {
      down = e.containerPoint;
    };
    const release = (e: L.LeafletMouseEvent) => {
      const click = down !== null && down.distanceTo(e.containerPoint) < CLICK_PX; // not a drag of the map
      down = null;
      const hit = click && shownGroups.current.find((c) => inside(c.hull, [e.latlng.lng, e.latlng.lat]));
      if (hit) setPickedGroup(hit.cid);
    };
    map.on("mousedown", press);
    map.on("mouseup", release);
    return () => {
      map.off("mousedown", press);
      map.off("mouseup", release);
    };
  }, [map]);

  useEffect(() => {
    if (!map || !mapId) return;
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

  const fit = (pts: L.LatLng[]) => {
    if (map && pts.length) map.fitBounds(L.latLngBounds(pts), { padding: [FIT_PAD_PX, FIT_PAD_PX] });
    return pts.length > 0;
  };
  const fitDrones = () => fit([...sit.drones.values()].filter((d) => onMap(d, mapId))
    .flatMap((d) => (d.geo ? [d.geo.camera, ...d.geo.footprint] : [])).flatMap((p) => (p ? [ll(p)] : [])));

  const centred = !!mapId && centredOn === mapId;
  useEffect(() => {
    if (!map || !mapId || centred) return;
    if (!overview && focus) {
      if (!focusGeo?.centre || focusGeo.map !== mapId) return;
      map.setView(ll(focusGeo.centre), START_ZOOM);
    } else if (!fitDrones() && !fit([...sit.objects.values()].map((o) => ll(o.xy)))) return;
    setCentredOn(mapId);
  });

  useEffect(() => {
    if (map && centred && centre) map.panTo(ll(centre.xy));
  }, [map, centred, centre]);

  useEffect(() => {
    if (!map || !centred) return;
    const objects = [...sit.objects.values()].filter((o) => visible(o, filter));
    vectors.clearLayers();
    const clusters = [...sit.clusters].filter(([wid]) => sit.maps.get(wid) === mapId).flatMap(([, list]) => list);
    if (layers.density) {
      heat.addTo(map);
      heat.set(objects.map((o) => ({ xy: o.xy, w: weightOf(weights, o.grp) })));
    } else {
      heat.remove();
    }
    shownGroups.current = layers.groups ? clusters : [];
    if (layers.groups) drawClusters(vectors, clusters, weights, pickedGroup);
    if (layers.drones) drawDrones(vectors, sit, focus, mapId);
    drawObjects(vectors, objects, selected, layers, now);
    if (layers.truth) drawTruth(vectors, sit);
    drawEvents(vectors, sit, filter, now);
    if (alert) drawAlert(vectors, sit, alert);
    syncMarkers(map, markers.current, objects, selected, onSelect);
  });

  const group = [...sit.clusters.values()].flat().find((c) => c.cid === pickedGroup);
  const away = map && centred && !overview && focusGeo && layers.drones &&
    !map.getBounds().contains(ll(focusGeo.camera)) ? map.getCenter() : null;

  return (
    <div className="map">
      <div ref={box} className="map-box" />
      {error && <p className="error map-error">{error}</p>}
      {layers.groups && group && (
        <GroupPanel group={group} sit={sit} weights={weights} onClose={() => setPickedGroup(null)} />
      )}
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
            {(layers.groups || layers.density) && <WeightPicker weights={weights} onWeights={changeWeights} />}
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
