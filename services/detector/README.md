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
| GET | `/streams/{sid}/model` | The stream's model (default: `MODEL`) |
| PUT | `/streams/{sid}/model` | Body `{"model"}`: choose the model for a stream, also mid-flight: it starts a new tracker whose track ids continue after the old one's, so the world does not take a new track for an old object |
| GET | `/streams/{sid}/conf` | The stream's box confidence threshold (default: the model's `conf.track`) |
| PUT | `/streams/{sid}/conf` | Body `{"conf"}` (0-1): boxes below it are not published. Untracked boxes also need `conf.detect`; below `conf.track` it changes nothing. Choosing a model resets it |
| GET | `/cluster` | Live replicas and which one owns each stream |
| POST | `/detect?model=&conf=` | Run the model on one uploaded image, without tracking |

Config: `MODEL` (`arma_detector_v1@1`), `MODELS_DIR` (`models`), `MAX_BATCH` (8), `MAX_FPS` (0 = every
frame), `STREAMS` (`frames.>`), `INSTANCE`.
