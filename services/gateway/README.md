# gateway

The one entry point for clients (the viewer now, the React frontend later). It keeps operator sessions,
proxies `/api/...` to the other services and sends live data over one WebSocket. Starting a session
assigns its streams to detector replicas and starts recording or replays.

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Service state and service URLs |
| POST | `/sessions` | Create a session (body below) |
| GET | `/sessions` | List sessions |
| GET | `/sessions/{id}` | One session |
| POST | `/sessions/{id}/start` | Assign detectors, set worlds, start recording or replays |
| POST | `/sessions/{id}/stop` | Stop the session's replays |
| PATCH | `/sessions/{id}/replays/{sid}` | Replay control: `{"paused", "speed", "seek_s", "step"}` |
| GET | `/detectors` | Live detector replicas and stream assignments |
| GET | `/streams` | Streams ingest is receiving |

Session body:
```json
{"name": "session", "model": null,
 "streams": [{"sid": "pc1-uav1", "source": "live", "wid": "live", "record": true},
             {"sid": "rp-1", "source": "replay", "wid": "rp-1", "recording": "<id>",
              "speed": 1.0, "loop": false, "offset_s": 0, "pacing": "realtime", "delivery": "latest"}]}
```

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

`/ws?session=<id>` or `/ws?sids=a,b&wids=x,y`. The server only sends.

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
(`{"type": "frame", "sid", "frame", "w", "h", "src", "ts"}`) + JPEG. Previews are limited to
`PREVIEW_FPS` and scaled to `PREVIEW_W` wide. A slow client loses messages instead of slowing the bus.

Config: `PREVIEW_FPS` (15), `PREVIEW_W` (960), `SESSIONS_STORE`, and `<SERVICE>_URL` for each service.
