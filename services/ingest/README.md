# ingest

The server's only way in for video, live or replayed. It demuxes the MPEG-TS, decodes each frame,
pairs it with its telemetry and publishes it as a JPEG; sim truth is passed on to the bus. It also gives each live
drone its world and stream id (`worlds.py`).

## Network

| Port | Content |
|---|---|
| UDP + TCP 9000 | MPEG-TS from an uplink |
| UDP 9100 | Sim truth JSON from an uplink or a replay |
| HTTP 8001 | REST below |

## Bus

| | Subject |
|---|---|
| publishes | `frames.<sid>` (telemetry + JPEG); `ts.capture` is the grab time live and the arrival in a replay, which also carries its recording time `ts.media` |
| publishes | `truth.<sid>`, `registry.<sid>`, `events.<sid>` (sim truth) |
| publishes | `raw.<sid>` (live MPEG-TS bytes, for the recorder; headers `t`, `drone`, `mission`) |
| publishes | `worlds.<wid>` (a world started, a drone joined it, or it `ended`: mission, map, `kind` live or replay, drone names by sid, and `previous`: the sid each drone had before) |

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Service state |
| GET | `/streams?all=` | Streams being received: sid, drone, mission, kind, wid, size, frames, fps. `all=true` includes ended ones |
| GET | `/worlds?mission=` | The live worlds of a mission now (one per map), as in `worlds.<wid>` |
| POST | `/missions/{mission}/stop` | End every live world of the mission now; drones still sending start it afresh |
| POST | `/push/{sid}?wid=` | Stream an MPEG-TS body into world `wid` (required; used by replays) |

A live stream gets its ids from the names in its telemetry (`tel.mission`, `tel.map`, `tel.drone`): one world
per mission and map, created by its first drone. A drone that reconnects keeps its `sid`; a name another connected
uplink of the world already flies under gets a suffix (`uav1-2`), which is written into its frames' `tel.drone`. A
drone that goes to another map moves to that map's world, with another `sid`. Ingest forgets its ids when it
restarts.

A live world ends when none of its drones has sent a frame for `EXPIRE_S` (600 s, the world model's object expiry:
nothing of it is left on the map), or when the mission is stopped; a replay world ends when its last push closes.
`worlds.<wid>` then carries `ended`, and the world model, decision, evaluator, detector and gateway drop it. A drone
still sending starts a new world with its next frame. A drone that gets a new `sid` (a new world, another map)
lists its old one in `previous`, so the detector moves its model and threshold along.

A replay keeps the ids it is pushed with (`/push/{sid}?wid=`) and its recorded names, and is registered as a
`replay` world. An uplink's sim truth names its drone and mission, and goes to that drone's current ids (with two
uplinks under one name, the one on the truth's host); truth from before the drone's first frame is dropped (the
uplink resends spawns). A replay's truth carries its ids.

Config: `VIDEO_PORT`, `TRUTH_PORT`, `SOURCE_IDLE_S`, `EXPIRE_S` (600; keep it equal to the world model's).
