# detector

Finds objects in each frame with a YOLO model (ONNX Runtime) and tracks them with one BoT-SORT tracker
per stream. Several replicas can run; each stream is assigned to one of them. `MODEL=oracle` skips the
model and makes boxes from sim truth, to measure the rest of the pipeline alone.

A model package is a folder `models/<name>_v<version>/` with `model.yaml`, `tracker.yaml` and
`model.onnx`; its id is `<name>@<version>`.

## Bus

| | Subject |
|---|---|
| subscribes | `frames.<sid>` |
| subscribes | `truth.<sid>`, `registry.<sid>` (oracle only) |
| publishes | `detections.<sid>`: boxes with `cls`, `grp`, `conf`, `tid` (track id), plus the frame's telemetry |

Replica heartbeats and stream assignments are kept in the JetStream KV buckets `detectors` and `assign`.

## REST

| Method | Path | Meaning |
|---|---|---|
| GET | `/health` | Default model, loaded models, model per stream |
| GET | `/models` | List model packages |
| GET | `/models/{id}` | One package: classes, image size, thresholds |
| POST | `/models` | Upload a model package as a zip (must contain `model.yaml`) |
| PUT | `/streams/{sid}/model` | Body `{"model"}`: choose the model for a stream |
| GET | `/cluster` | Live replicas and which one owns each stream |
| POST | `/detect?model=&conf=` | Run the model on one uploaded image, without tracking |

Config: `MODEL` (`arma_detector_v1@1`), `MODELS_DIR` (`models`), `MAX_BATCH` (8), `MAX_FPS` (0 = every
frame), `STREAMS` (`frames.>`), `INSTANCE`.
