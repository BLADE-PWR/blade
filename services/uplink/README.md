# uplink

Runs on the Arma PC (Windows) and turns the game into a drone stream. It captures the screen, reads the
frame number from the bar in the top-left corner, pairs the frame with the telemetry the game sent for
it, and sends H.264 + telemetry as MPEG-TS to ingest. Sim truth from the game is forwarded as well.

```bash
uv sync --package blade-uplink
```
```bash
uv run blade-uplink --sid pc1-uav1 --server <server-ip>
```

Arma must be borderless and in the foreground, BattlEye off, and the `zpi_telemetry` extension sending
to `127.0.0.1:5005`.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--sid` | required | Stream id, e.g. `pc1-uav1` |
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
| out | UDP or TCP 9000 | MPEG-TS: H.264 + one KLV telemetry packet per frame, same PTS |
| out | UDP 9100 | Sim truth as JSON: `{"sid", "kind": "gt" \| "spawn" \| "evt", "rec"}` |
