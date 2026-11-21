# gateway

The one entry point for clients (the React frontend). It keeps replay sessions, proxies `/api/...` to the
other services and sends live data over one WebSocket. Starting a session assigns its streams to detector
replicas and starts its replays. Live drones need no session: they join the world of their uplink's `--mission`.
Sessions are kept in memory only. When the last WebSocket viewer of a session leaves, its replays are paused;
whoever opens it next (maybe someone else) finds it paused and presses play. One nobody has watched for
`SESSION_IDLE_S` (30 s) is deleted with its replays, so closing the tab ends the replay and a restart starts with
no sessions.

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Service state and service URLs |
| POST | `/sessions` | Create a session (body below) |
| GET | `/sessions` | List sessions |
| GET | `/sessions/{id}` | One session |
| POST | `/sessions/{id}/start` | Assign detectors, start the replays |
| POST | `/sessions/{id}/stop` | Stop the session's replays |
| DELETE | `/sessions/{id}` | Stop the session's replays and delete it |
| PATCH | `/sessions/{id}/replays/{sid}` | Replay control: `{"paused", "speed", "seek_s", "step"}` |
| GET | `/detectors` | Live detector replicas and stream assignments |
| GET | `/streams` | Streams ingest is receiving |

Session body:
```json
{"name": "session", "model": null,
 "streams": [{"sid": "rp-1", "wid": "rp-1", "recording": "<id>",
              "speed": 1.0, "loop": false, "offset_s": 0, "pacing": "realtime", "delivery": "latest"}]}
```
Each stream is one replay: `sid`, `wid` and `recording` are required.

## Proxy

`/api/<area>/...` forwards to a service; see that service's README for the paths.

| Prefix | Service | Path there |
|---|---|---|
| `/api/models` | [detector](../detector/README.md) | `/models` |
| `/api/detector` | [detector](../detector/README.md) | `/streams` (model and threshold of a stream) |
| `/api/recordings` | [sessions](../sessions/README.md) | `/recordings` |
| `/api/replays` | [sessions](../sessions/README.md) | `/replays` |
| `/api/maps` | [terrain](../terrain/README.md) | `/maps` |
| `/api/worlds` | [world](../world/README.md) | `/worlds` |
| `/api/decision` | [decision](../decision/README.md) | `/worlds` |
| `/api/eval` | [evaluator](../evaluator/README.md) | `/worlds` |

## WebSocket

`/ws?session=<id>` or `/ws?wids=x,y`: every drone in those worlds, also ones that join later. The server only sends.

Text messages are JSON: `{"type", "sid", "wid", "frame", "ts", "data"}`.

| `type` | Content |
|---|---|
| `session` | The session, once at the start (with `?session=`) |
| `snapshot` | All current objects of a world, once at the start |
| `detections` | Boxes of one frame |
| `geo` | Map points of one frame |
| `world.objects`, `world.events` | Changed objects, events |
| `alerts` | Active alerts |
| `eval` | Evaluation numbers |
| `diag` | Per-service fps, latency, drops |

Binary messages are video previews: `"ZPIF"` + uint32 big-endian header length + header JSON
(`{"type": "frame", "sid", "frame", "w", "h", "src"}`) + JPEG. Previews are limited to
`PREVIEW_FPS` and scaled to `PREVIEW_W` wide. A slow client loses messages instead of slowing the bus.

Config: `PREVIEW_FPS` (15), `PREVIEW_W` (960), `SESSION_IDLE_S` (30), and `<SERVICE>_URL` for each service.
