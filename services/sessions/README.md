# sessions

Records live missions to disk, imports recordings and replays them. A replay plays a recording into a world of
its own: every drone of it is pushed back into ingest under a new stream id, so from ingest onwards it looks like a
live stream, except that its frames carry their recording time: the world model runs the replay on it, so pausing it
does not age the world and other speeds keep the objects' speeds right (world README, "Time").

All drones of a replay play on one clock (`clock.py`): a drone that joined the mission later, or came back after a
gap, does so on the timeline as it did live, and pause, speed, seek, step and loop move them together.

A live world is recorded as one mission recording, `recordings/<time>_<mission>_<map>/`:

```
manifest.json          mission, map, wid, start, end, and drones: [{name, start_s, segments: [{dir, start_s, ...}]}]
uav1/000/              one segment: stream.ts (the MPEG-TS as it arrived), index.jsonl, truth.jsonl, manifest.json
uav1/001/              uav1 connected again: a new MPEG-TS stream, whose timestamps start over
uav2/000/
```

A drone's bytes go to a new segment when they come from a new connection. The recording is finalized when ingest
ends the world (`worlds.<wid>` with `ended`: no frame for 10 minutes, or Stop mission); until then it is listed with
`recording: true` and cannot be replayed. Each segment is replayed in a push of its own, since two MPEG-TS streams
one after the other would pair frames with the wrong telemetry (ingest pairs them by timestamp). A recording made of
one segment (an import, or one recorded before mission recordings) is one drone starting at 0.

## Bus

| | Subject |
|---|---|
| subscribes | `raw.<sid>` (video bytes to record) |
| subscribes | `truth.<sid>`, `registry.<sid>`, `events.<sid>` (truth to record) |
| subscribes | `worlds.<wid>` (an `ended` world's recording is finalized) |

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
| GET | `/replays` | List replays with state, position on the timeline, `generation` (goes up with every jump: seek or loop) and drones (`name`, `sid`, `start_s`, and `segments`: [from, to] on the timeline) |
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

Config: `RECORDINGS_DIR` (`recordings`), `RECORD_ALL` (1), `RECORD_IDLE_S` (660: a world ingest never ended, e.g.
because it restarted, is finalized after this long without bytes), `INGEST_URL`,
`INGEST_TRUTH_HOST`, `INGEST_TRUTH_PORT`.
