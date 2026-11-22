# decision

Runs rules over the world state and raises alerts that tell the operator where to look and why. No
rule ranks targets or suggests an action. An alert clears after its condition has been false for 5 s, or at once
when all its objects are `forgotten` (a replay looped or went back: they have not happened yet).

Rules run on the world's clock (`t` in `world.<wid>.objects`, see the world README, "Time"), so a paused
replay raises no lost contact; objects in `forgotten` (merged, or their replay went back) are not lost.
A world that sends no clock runs on the wall clock.

Rules set for a live world (`PUT /worlds/{wid}/rules`) are its mission's: they apply to every world of the mission
(one per map) and to the ones it starts later, until decision restarts. A replay world's rules are its own.

## Rules

| Rule | Fires when | Parameters |
|---|---|---|
| `vehicle_group` | Vehicles are close together | `min_count` 3, `radius_m` 200 |
| `lost_contact` | A well-observed object has not been seen | `after_s` 60, `min_obs` 30, `groups` |
| `uncertain_position` | An object's position is uncertain | `sigma_m` 15 |
| `infantry_resolution` | Infantry is likely missed at this zoom | `gsd_m` 0.06, `hold_s` 10 |
| `explosion_near` | An explosion is near known objects | `radius_m` 100 |
| `zone` | Objects are in or heading into a marked zone | `zones`, `eta_s` 60, `groups` |

Every rule also has `enabled`.

## Bus

| | Subject |
|---|---|
| subscribes | `world.<wid>.objects`, `world.<wid>.events`, `geo.<sid>` |
| subscribes | `worlds.<wid>`: which mission a live world is (its rules), and when it `ended` (dropped) |
| publishes | `alerts.<wid>`: active alerts with rule, objects, confidence and explanation |

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Active alert count per world |
| GET | `/worlds/{wid}/rules` | Rule parameters of a world |
| PUT | `/worlds/{wid}/rules` | Body `{"<rule>": {<parameters>}}`: change parameters; others stay |
| GET | `/worlds/{wid}/alerts?all=` | Active alerts; `all=true` gives the history |

Config: `PERIOD_S` (1), `CLEAR_AFTER_S` (5), `GONE_KEEP_S` (600).
