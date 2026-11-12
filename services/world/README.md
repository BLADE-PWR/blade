# world

Builds the map picture: it fuses the per-stream points into objects with stable ids, one Kalman filter
per object, and merges the same object seen by several drones of one world. An object is published
after `CONFIRM_N` observations, becomes `stale` when unseen and expires after `EXPIRE_S`.

A moving object (speed at least 1 m/s and over twice its velocity sigma) also carries `pred`: straight-line
positions 30 s and 60 s after `last`, each with a 1-sigma radius that grows with the velocity uncertainty and
with a tenth of the distance travelled (`HORIZONS_S`, `MANEUVER` in `fusion.py`). `vel_sigma` is the
1-sigma of `vel`.

## Bus

| | Subject |
|---|---|
| subscribes | `geo.<sid>` |
| publishes | `world.<wid>.objects`: changed objects, expired ids, counts per group |
| publishes | `world.<wid>.events`: events such as explosions |
| publishes | `ack.<sid>`: frame done, for replays with `delivery: all` |

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Object count per world |
| GET | `/worlds` | List worlds |
| GET | `/worlds/{wid}/snapshot` | All current objects and events of a world |
| GET | `/worlds/{wid}/objects/{oid}` | One object with its position history |

Config: `CONFIRM_N` (5), `STALE_S` (3), `EXPIRE_S` (600), `EVENT_TTL_S` (5), `EVENT_CLASSES`
(`explosion,tank_fire`).
