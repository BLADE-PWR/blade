# world

Builds the map picture: it fuses the per-stream points into objects with stable ids, one Kalman filter
per object, and merges the same object seen by several drones of one world. An object is published
after `CONFIRM_N` observations, becomes `stale` when unseen and expires after `EXPIRE_S`.

A moving object (speed at least 1 m/s and over twice its velocity sigma) also carries `pred`: straight-line
positions 30 s and 60 s after `last`, each with a 1-sigma radius that grows with the velocity uncertainty and
with a tenth of the distance travelled (`HORIZONS_S`, `MANEUVER` in `fusion.py`). `vel_sigma` is the
1-sigma of `vel`.

## Drawing motion on the map

For any map client (`tools/viewer` does this):

- Draw an arrow and a cone only for objects that have `pred`; the rest stand still within the noise.
- Arrow: from `xy` to `xy + vel * N s`, so its length scales with speed the same way for every class
  (the viewer uses 10 s).
- Cone: the hull of the circles of radius `k * sigma_m` around `xy` and around each `pred[].xy`. With k = 2
  a circle holds the object ~86 % of the time in 2-D; k = 1 holds only ~39 %, which looks more certain than it is.
- `pred` is anchored at `last` and not republished while the object is `stale`, so the client must hide a
  horizon once `last + dt_s` is in the past, and the arrow and cone with the last one.

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
