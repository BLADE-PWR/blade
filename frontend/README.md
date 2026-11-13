# frontend

The operator app (module 8 in `docs/opis_projektu.md`): React + TypeScript, built with Vite. It talks only
to the [gateway](../services/gateway/README.md): REST for control, one WebSocket for live data.

Two views, both in Polish, switched in the top bar. The layout follows the accepted mockups on the
"Blade: widoki UI" canvas.

- Operator: one drone. Video with the detector's boxes, the map, the chosen object's details, the alert,
  object and event tabs, class filters and the time of each pipeline stage.
- Command: every drone of the session. Either the map on the main panel (drone list, alerts, state) or the
  video (previews of all drones, the chosen drone, a small map, state per service, settings).

Both views can put the map or the video on the main panel. A session with replays gets the replay bar
(pause, next frame, 10 s jumps, speed, slider).

Libraries besides React: `leaflet` (the map) and `milsymbol` (APP-6 symbols), both named in module 8.

It is not part of the uv workspace; it has its own `package.json`. Needs Node.js 22 or newer
(Fedora: `sudo dnf install nodejs`, Windows: `winget install OpenJS.NodeJS.LTS`).

## Run

Start the server (`uv run python tools/dev.py --map altis`, without an NVIDIA card add `--detect-fps 5`). For
data, create a gateway session with a looping replay and start it (the replay bar needs a session; a replay
started with `POST /api/replays` only shows up through "Albo strumienie i światy"):
```bash
curl -XPOST localhost:8000/sessions -H 'content-type: application/json' -d '{"name": "Demo", "map": "altis",
  "streams": [{"sid": "rp-1", "source": "replay", "wid": "rp-1", "recording": "<id>", "loop": true}]}'
```
```bash
curl -XPOST localhost:8000/sessions/<session id from the answer>/start
```
A second stream with the same `wid` (and e.g. `"offset_s": 40`) gives the command view two drones.

Then:
```bash
cd frontend
```
```bash
npm install
```
```bash
npm run dev
```

Open http://localhost:5173 and pick the session. In dev, Vite forwards `/api`, `/sessions`, `/detectors`, `/streams` and the
`/ws` WebSocket to the gateway on `127.0.0.1:8000`. For a gateway on another machine:
```bash
GATEWAY_URL=http://<server-ip>:8000 npm run dev
```

## Scripts

| Script | What it does |
|---|---|
| `npm run dev` | Dev server with the gateway proxy |
| `npm run typecheck` | TypeScript check only |
| `npm run build` | Typecheck, then a production build in `dist/` |
| `npm run preview` | Serve `dist/` locally |

## Code

| File | What it is |
|---|---|
| `src/gateway.ts` | WebSocket client: JSON messages and `ZPIF` video previews (`b"ZPIF"` + uint32 BE header length + header JSON + JPEG); small REST helpers |
| `src/contracts.ts` | Types of the message payloads the UI reads, copied by hand from `libs/common/blade_common/contracts.py` |
| `src/situation.ts` | The live picture of one connection (drones, objects, events, alerts, diag), updated by each message |
| `src/App.tsx` | Session picker, the connection (redrawn at most once per screen frame) and the top bar |
| `src/Operator.tsx` | View 1: one drone; also the object details (crop from the latest box of its track) |
| `src/Command.tsx` | View 2: every drone of the session; map on the main panel (drone list, alerts, state) or video (drone previews, the chosen drone, small map, state per service, settings) |
| `src/Video.tsx` | One drone's video preview with the boxes of the same frame and the mark at the centre of the frame |
| `src/MapView.tsx` | The map: orthophoto tiles in Arma metres (Leaflet `CRS.Simple`), APP-6 symbols (milsymbol) stacked when close, drones, camera footprints, motion, uncertainty, events, true positions |
| `src/Replay.tsx` | The replay bar: position, pause, next frame, 10 s jumps, speed; drives every replay of the session together |
| `src/Lists.tsx` | The alert, object and event lists (clicking an alert selects its object and rings it on the map) |
| `src/classes.ts` | Polish names and fixed colours of the object groups and event kinds, alert colours |
| `src/format.ts` | Numbers and times as Polish text |
| `src/index.css` | All styles, one dark theme |
| `vite.config.ts` | The dev proxy to the gateway |
