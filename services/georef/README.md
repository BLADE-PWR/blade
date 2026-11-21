# georef

Turns each detected box into a map position. It casts a ray from the camera through the box's anchor
point onto the elevation model of the map the drone is on (`map` in its telemetry) and adds an uncertainty (σ)
and the camera's ground footprint. The service is stateless.

## Bus

| | Subject |
|---|---|
| subscribes | `detections.<sid>` |
| publishes | `geo.<sid>`: one map point per box, the footprint and ground sample distance |

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Loaded maps and their extents |
| POST | `/project` | Body `{"tel", "dets", "pixels"}`: project boxes or plain pixels `[u, v]` for one telemetry record, without the bus |

Config: `MAPS_DIR` (`maps`): every map package in it is loaded at startup.
