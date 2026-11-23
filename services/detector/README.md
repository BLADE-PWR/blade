# detector

Finds objects in each frame with a YOLO model (ONNX Runtime) and tracks them with one BoT-SORT tracker
per stream. Several replicas can run; each stream is assigned to one of them. `MODEL=oracle` skips the
model and makes boxes from sim truth, to measure the rest of the pipeline alone.

A model package is a folder `models/<name>_v<version>/` with `model.yaml`, `tracker.yaml` and
`model.onnx`; its id is `<name>@<version>`.

Optional `max_size_m` in `model.yaml` (group -> metres, e.g. `{soldier: 3, tank: 25}`) drops boxes whose longer side
is longer on the ground than their group can be, before tracking. The size comes from the frame's telemetry: pixel angle
times the distance to where the box's bottom meets flat ground through the rangefinder point. Frames without `range_m`
are not filtered, and neither are frames where most confident boxes come out too big (telemetry of another scene or
behind a zoom change).

Optional `auto_imgsz: {max: 1920, object_m: 7, min_px: 60}` raises the model input above `imgsz`, up to `max`, until an
object of `object_m` metres at the rangefinder point is `min_px` px at the input: far scenes get the object sizes the
model was trained on, close ones stay cheap.

## Auto model

A stream in `auto` mode (the default, `MODEL=auto`) is not tied to one model: each frame goes to the first model of the
*routing* whose size range covers it. The range is in pixels an object of `object_m` metres takes at the model's input
(`imgsz`, or `auto_imgsz.max`), measured at the rangefinder point (the aim point, as `gsd_m` is): it follows the
zoom (`hfov`) and `range_m`, not the stream's resolution. An oblique view has a worse scale at the top of the frame.

A package declares its range in `model.yaml` (a package without it covers every scale):

```yaml
scale: {object_m: 1.8, min_px: 15, max_px: null}   # a 1.8 m soldier at the aim point is 15 px or more
```

Take `min_px` from the sizes of the training data (`arma_detector_v2_auto`: standing soldiers' long side p2 = 15.5 px).
Where no model covers the frame, the stream gets `detections.<sid>` with `model: "none"` and no boxes (so the map, the
camera footprint and `infantry_resolution` keep working) and `diag.detector` counts it as `skipped`: no phantom boxes
of objects too small for the model. To detect smaller objects, add a model trained for them (`POST /models`, then add it
to the routing, ahead of or behind the others).

The routing is the ordered list of models with optional overrides of `min_px` / `max_px`, `margin` and `hold_s`. It is
the same for every drone and replica (JetStream KV `routing`; without JetStream, per replica), set from the frontend
(Settings, "Auto models") or by `PUT /routing`; until then it is `AUTO_MODELS`. The running model keeps a stream while
its range, widened by `margin` (0.15), holds it, and another model (or none) takes over only after the frame has been
outside the running one for `hold_s` (1 s): no flicker at the edge. Choosing a model for a stream by hand
(`PUT /streams/{sid}/model`) takes it out of auto mode.

## Bus

| | Subject |
|---|---|
| subscribes | `frames.<sid>` |
| subscribes | `truth.<sid>`, `registry.<sid>` (oracle only) |
| subscribes | `worlds.<wid>`: a drone's new sid takes the model and threshold of its `previous` one; the streams of an `ended` world are forgotten |
| publishes | `detections.<sid>`: boxes with `cls`, `grp`, `conf`, `tid` (track id), plus the frame's telemetry |

Replica heartbeats and stream assignments are kept in the JetStream KV buckets `detectors` and `assign`.

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Default model, loaded models, model per stream |
| GET | `/models` | List model packages |
| GET | `/models/{id}` | One package: classes, image size, thresholds |
| POST | `/models` | Upload a model package as a zip (must contain `model.yaml`) |
| GET | `/streams/{sid}/model` | The stream's model (default: `MODEL`); in `auto` also `active` (the model running it, null if none) and `px` |
| PUT | `/streams/{sid}/model` | Body `{"model"}` (a package id, `auto` or `oracle`): choose the model for a stream, also mid-flight: it starts a new tracker whose track ids continue after the old one's, so the world does not take a new track for an old object |
| GET | `/streams/{sid}/conf` | The stream's box confidence threshold (default: the model's `conf.track`) |
| PUT | `/streams/{sid}/conf` | Body `{"conf"}` (0-1): boxes below it are not published. Untracked boxes also need `conf.detect`; below `conf.track` it changes nothing. Choosing a model resets it |
| GET | `/routing` | The auto routing: per model the range in force (`min_px`, `max_px`), the one of `model.yaml` (`yaml`), whether it is overridden, whether the package is loaded |
| PUT | `/routing` | Body `{"models": [{"model", "min_px"?, "max_px"?}], "margin"?, "hold_s"?}`: the models auto chooses from, in priority order. 404 for a model not loaded, 400 for `min_px >= max_px` |
| DELETE | `/routing` | Back to `AUTO_MODELS` and the ranges of `model.yaml` |
| GET | `/cluster` | Live replicas and which one owns each stream |
| POST | `/detect?model=&conf=` | Run the model on one uploaded image, without tracking |

Config: `MODEL` (`auto`), `AUTO_MODELS` (`arma_detector_v2_auto@2`, comma-separated routing until it is set), `MODELS_DIR` (`models`), `MAX_BATCH` (8), `MAX_FPS` (0 = every
frame), `STREAMS` (`frames.>`), `INSTANCE`.
