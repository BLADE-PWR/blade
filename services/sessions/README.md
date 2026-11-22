# sessions

Records live streams to disk, imports recordings and replays them. A replay plays a recording into a world of
its own: every drone of it is pushed back into ingest under a new stream id, so from ingest onwards it looks like a
live stream, except that its frames carry their recording time: the world model runs the replay on it, so pausing it
does not age the world and other speeds keep the objects' speeds right (world README, "Time").

All drones of a replay play on one clock (`clock.py`): a drone that joined the mission later starts that much later
on the timeline, and pause, speed, seek, step and loop move them together. A recording of a single stream is one
drone starting at 0 (`GET /recordings` lists each recording's `drones`, with `name` and `start_s`).

A recording is a folder `recordings/<id>/` with `manifest.json`, `stream.ts`, `index.jsonl` and
`truth.jsonl`.

## Bus

| | Subject |
|---|---|
| subscribes | `raw.<sid>` (video bytes to record) |
| subscribes | `truth.<sid>`, `registry.<sid>`, `events.<sid>` (truth to record) |

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Service state |
| GET | `/recordings` | List recordings |
| GET | `/recordings/{id}` | One recording's manifest |
| POST | `/recordings?name=` | Upload a recording as a zip (must contain `stream.ts`) |
| POST | `/recordings/register` | Body `{"path", "name"}`: link a folder on the server as a recording |
| GET | `/recording/active` | What is being recorded now |
| PUT | `/recording/{sid}` | Body `{"on": bool}`: turn recording of a stream on or off |
| POST | `/replays` | Start a replay (body below); 409 for a recording that is still being written |
| GET | `/replays` | List replays with state, position on the timeline and drones (`name`, `sid`, `start_s`) |
| PATCH | `/replays/{wid}` | Body `{"paused", "speed", "seek_s", "step", "loop"}`, any subset. `step` pauses and sends the next N frames of every drone that has joined by then |
| DELETE | `/replays/{wid}` | Stop a replay |

Replay body:

| Field | Default | Meaning |
|---|---|---|
| `recording` | required | Recording id |
| `wid` | required | World it plays into (not the mission it was recorded with); one replay per world |
| `sids` | required | Drone name in the recording -> the stream id it plays under, for every drone of it |
| `speed` | 1.0 | Playback speed |
| `loop` | false | Start again at the end; else the replay pauses there (a seek goes on from the target, Play starts over). Can be changed with PATCH |
| `offset_s` | 0 | Start position on the timeline |
| `truth` | true | Also replay the sim truth |

The dataset generator's pictures can be turned into a replayable recording:
```bash
uv run blade-import-stills <dataset> --split val --map <map name> --out recordings/stills_val
```

Config: `RECORDINGS_DIR` (`recordings`), `RECORD_ALL` (1), `RECORD_IDLE_S`, `INGEST_URL`,
`INGEST_TRUTH_HOST`, `INGEST_TRUTH_PORT`.
