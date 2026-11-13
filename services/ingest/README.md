# ingest

The server's only way in for video, live or replayed. It demuxes the MPEG-TS, decodes each frame,
pairs it with its telemetry and publishes it as a JPEG; sim truth is passed on to the bus.

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
| publishes | `raw.<sid>` (live MPEG-TS bytes, for the recorder) |
| subscribes | `ack.<sid>` (for `delivery=all`) |

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Service state |
| GET | `/streams?all=` | Streams being received: sid, kind, wid, size, frames, fps. `all=true` includes ended ones |
| PUT | `/streams/{sid}` | Body `{"wid"}`: put the stream into a world |
| POST | `/push/{sid}?wid=&delivery=` | Stream an MPEG-TS body in (used by replays). `delivery=all` holds each frame until the world model has acked the previous one |

Config: `VIDEO_PORT`, `TRUTH_PORT`, `DEFAULT_WID` (`live`), `SOURCE_IDLE_S`, `ACK_TIMEOUT_S`.
