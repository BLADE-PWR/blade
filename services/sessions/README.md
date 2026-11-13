# sessions

Records live streams to disk, imports recordings and replays them. A replay pushes the recorded
`stream.ts` back into ingest under a new stream id, so from ingest onwards it looks like a live stream,
except that its frames carry their recording time: the world model runs the replay on it, so pausing it
does not age the world and other speeds keep the objects' speeds right (world README, "Time").

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
| POST | `/replays` | Start a replay (body below) |
| GET | `/replays` | List replays with state and position |
| PATCH | `/replays/{sid}` | Body `{"paused", "speed", "seek_s", "step"}`, any subset. `step` plays N frames and pauses |
| DELETE | `/replays/{sid}` | Stop a replay |

Replay body:

| Field | Default | Meaning |
|---|---|---|
| `recording` | required | Recording id |
| `sid` | required | Stream id the replay appears under |
| `wid` | | World to put it in |
| `speed` | 1.0 | Playback speed |
| `loop` | false | Start again at the end |
| `offset_s` | 0 | Start position |
| `pacing` | `realtime` | `realtime`, or `max` to send as fast as the pipeline takes it |
| `delivery` | `latest` | `latest` lets stages drop frames; `all` sends every frame through every stage (repeatable experiments) |
| `truth` | true | Also replay the sim truth |

The dataset generator's pictures can be turned into a replayable recording:
```bash
uv run blade-import-stills <dataset> --split val --out recordings/stills_val
```

Config: `RECORDINGS_DIR` (`recordings`), `RECORD_ALL` (1), `RECORD_IDLE_S`, `MAP`, `INGEST_URL`,
`INGEST_TRUTH_HOST`, `INGEST_TRUTH_PORT`.
