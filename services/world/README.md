# world

Builds the map picture: it fuses the per-stream points into objects with stable ids, one Kalman filter
per object, and merges the same object seen by several drones of one world. An object is published
after `CONFIRM_N` observations, becomes `stale` when unseen and expires after `EXPIRE_S`.

A world is on one map: the first drone that reports into it sets it. A drone on another map is left out (both
maps' coordinates start at 0, 0, so its objects would mix with real ones) and logged once. Once no drone on the
world's map has reported for `MAP_IDLE_S` (30 s), the next drone on another map moves the world there and it starts
afresh: its old objects are published as `forgotten`, and decision clears the world's alerts.

A moving object (speed at least 1 m/s and over twice its velocity sigma, but below `PRED_VMAX`, above which the
filter is still catching up with a jump) also carries `pred`: straight-line positions 30 s and 60 s after `last`,
each with a 1-sigma radius that grows with the velocity uncertainty and with a tenth of the distance travelled
(`HORIZONS_S`, `MANEUVER` in `fusion.py`). `vel_sigma` is the 1-sigma of `vel`.

## Time

Each world runs on its own clock (`clock.py`), and `first`, `last`, event and alert times are on it:

- A live stream runs on the wall clock, so the objects of a drone that stops sending go stale as before.
- A replay carries its recording time (`ts.media`, set by ingest), and the world runs it on that time: a
  paused replay stands still (nothing goes stale, no lost contact), a replay at x2 runs twice as fast
  (speeds come out the same as at x1), a seek ahead is time passing.
- A replay that goes back in its recording (a seek back, a loop) joins again at the world's current time, and
  what that stream saw is dropped and published in `forgotten`.
- A world with a live stream follows the wall clock for all its streams (ahead of it by as much as a fast
  replay had taken the world before). Replays in one world should play together: the clock is the furthest of
  them, so a stream paused on its own ages, and joins at the world's time when it plays on.

Every `world.<wid>.objects` message carries the clock as `t`, and the sweeper publishes it once a second
while it moves, so clients and decision see time pass; while a replay is paused nothing is published.

## Drawing motion on the map

For any map client (`tools/viewer` does this):

- Draw an arrow and a cone only for objects that have `pred`; the rest stand still within the noise.
- Arrow: from `xy` to `xy + vel * N s`, so its length scales with speed the same way for every class
  (the viewer uses 10 s).
- Cone: the hull of the circles of radius `k * sigma_m` around `xy` and around each `pred[].xy`. With k = 2
  a circle holds the object ~86 % of the time in 2-D; k = 1 holds only ~39 %, which looks more certain than it is.
- `pred` is anchored at `last` and not republished while the object is `stale`, so the client must hide a
  horizon once `last + dt_s` is behind the world's clock (`t`, not the client's clock: a paused replay keeps
  its predictions), and the arrow and cone with the last one.

## Bus

| | Subject |
|---|---|
| subscribes | `geo.<sid>` |
| publishes | `world.<wid>.objects`: changed objects, expired and forgotten ids, counts per group, the world's clock `t` |
| publishes | `world.<wid>.events`: events such as explosions |
| publishes | `ack.<sid>`: frame done, for replays with `delivery: all` |

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Object count per world |
| GET | `/worlds` | List worlds |
| GET | `/worlds/{wid}/snapshot` | All current objects and events of a world |
| GET | `/worlds/{wid}/objects/{oid}` | One object with its position history |

Config: `CONFIRM_N` (5), `STALE_S` (3), `EXPIRE_S` (600), `MAP_IDLE_S` (30), `EVENT_TTL_S` (5), `EVENT_CLASSES`
(`explosion,tank_fire`).
