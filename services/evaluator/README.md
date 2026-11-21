# evaluator

Compares the pipeline's output with the game's sim truth, live. It reports position error (CEP50/90),
detection rate and false objects at two levels: `geo` (every georef point) and `world` (objects after
fusion). Every matched sample is also written to `state/eval/eval_<wid>_<start>.csv`. A drone on another map than
its world (`map` in `geo` and `world.<wid>.objects`) is left out.

## Bus

| | Subject |
|---|---|
| subscribes | `truth.<sid>`, `registry.<sid>`, `geo.<sid>`, `world.<wid>.objects` |
| publishes | `eval.<wid>`: current numbers, twice a second |

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Worlds being evaluated, CSV folder |
| GET | `/worlds/{wid}/eval` | Numbers over the last `WINDOW_S` seconds |
| GET | `/worlds/{wid}/eval/report` | Numbers over the whole run |
| POST | `/worlds/{wid}/eval/reset` | Start the evaluation of a world again |

Tables from the CSVs:
```bash
uv run python tools/eval_report.py "state/eval/eval_<wid>_*.csv"
```

Config: `EVAL_DIR` (`state/eval`), `WINDOW_S` (30).
