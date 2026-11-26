# uplink

Runs on the Arma PC (Windows) and turns the game into a drone stream. It captures the screen, reads the
frame number from the bar in the top-left corner, pairs the frame with the telemetry the game sent for
it, and sends H.264 + telemetry as MPEG-TS to ingest. Sim truth from the game is forwarded as well.

```bash
uv sync --package blade-uplink
```
```bash
uv run blade-uplink --drone uav1 --mission <mission> --server <server-ip>
```

Arma must be borderless and in the foreground, BattlEye off, and the `zpi_telemetry` extension sending
to `127.0.0.1:5005`.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--drone` | required | The drone's name, e.g. `uav1`; the server gives it an id. If another connected uplink of the mission already flies under it, the server shows this one as `uav1-2` |
| `--mission` | required | The mission's name, e.g. `arma-live1`: drones of one mission on one map share one map picture. Both names are sent in every telemetry record and with the sim truth |
| `--server` | required | Ingest host |
| `--transport` | `udp` | `udp` for a wired LAN; `tcp` for internet, Tailscale or Wi-Fi (resends lost packets, skips to the next keyframe when more than 0.5 s behind) |
| `--fps` | 60 | Stream frame rate |
| `--bitrate` | `8M` | Must fit the PC's upload |
| `--bar-bits` | 12 | Data squares in the frame bar |
| `--bar-size` | 4 | Bar square side in screen pixels |

## Network

| Direction | Port | Content |
|---|---|---|
| in | UDP 5005 (localhost) | Telemetry and sim truth from the game |
| out | UDP or TCP 9000 | MPEG-TS: H.264 + one KLV telemetry packet per frame, same PTS. The packet holds two KLV items: blade's JSON record (what the server reads) and the same telemetry as a MISB ST 0601.8 UAS Datalink Local Set, for standard tools (see below) |
| out | UDP 9100 | Sim truth as JSON: `{"sid", "kind": "gt" \| "spawn" \| "evt", "rec"}` |

## MISB ST 0601

Each frame's telemetry is also sent as a MISB ST 0601.8 UAS Datalink Local Set (`libs/common/blade_common/misb.py`),
so standard drone video tools can place the drone and its view on a map. Positions become WGS84 latitude / longitude
around a fixed anchor per map (`ANCHORS`: the south-west corner; approximate, from the real islands the maps are
modelled on). A map without an anchor gets only blade's record.

| Tag | Item | From the tel record |
|---|---|---|
| 2 | UNIX Time Stamp | `t_capture` (first item) |
| 3, 10 | Mission ID, Platform Designation | `mission`, `drone` |
| 5, 6, 7 | Platform Heading, Pitch, Roll | 0: the camera's whole orientation is in 18-20 |
| 13, 14, 15 | Sensor Latitude, Longitude, True Altitude | `camera.pos_asl` |
| 16, 17 | Sensor HFOV, VFOV | `camera.hfov_deg`, `camera.vfov_deg` |
| 18, 19, 20 | Sensor Relative Azimuth, Elevation, Roll | `camera.x/y/z_axis` |
| 21 | Slant Range | `range_m` (only with a range) |
| 23, 24, 25 | Frame Center Latitude, Longitude, Elevation | `pos_asl + range_m * z_axis` (only with a range) |
| 65 | UAS LS Version Number | 8 |
| 1 | Checksum | last item |

`frame`, `t_game`, `t_tick`, `image`, `scene`, `map`, `seed` and `region` have no ST 0601 item and travel only in blade's
record. Altitudes are kept to ~0.3 m and angles to ~0.0001 deg or better.
