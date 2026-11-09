# terrain

Serves map packages (`maps/<id>/` with `meta.json`, `dem.tif` and `ortho/`): metadata, terrain height
and north-up image crops for the map background. It does not use the bus.

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Available maps |
| GET | `/maps` | List map packages |
| GET | `/maps/{id}/meta` | Extent, height range, orthophoto levels |
| GET | `/maps/{id}/height?x=&y=` | Terrain height at a map position |
| GET | `/maps/{id}/crop?x0=&y0=&x1=&y1=&mpp=&layer=` | PNG of the area at `mpp` metres per pixel (at most 2048 px per side) |

`layer` is `auto` (orthophoto if one covers the area, else hillshade), `ortho` or `hillshade`. The
response headers `X-Layer` and `X-Extent` say what was used.

Config: `MAPS_DIR` (`maps`).
