# AI event-analysis pipeline

HomeCam analyses every event the same way, no matter which provider produced
it. A Dahua NVR channel, the Eufy T8210 doorbell and a mock camera all flow
through the same stages, because the pipeline only ever consumes **normalized
events and snapshot bytes** — never provider-specific data structures.

Implements SPEC sections 9, 12, 13, 14, 15, 18, 19, 31, 32 and the API shape
suggested by section 33.

## Pipeline

```
normalize -> persist -> snapshot -> local detector -> semantics (zones + dwell)
          -> best photo -> AI analysis -> embedding -> activity correlation
          -> realtime (SSE)
```

| Stage | Module | Notes |
| --- | --- | --- |
| Normalize + persist | `app/services/events.py` | Unchanged event contract (SPEC 9). |
| Snapshot sampling | `app/services/ai_pipeline.py` | Uses each provider's existing `get_snapshot`; no new streaming infra. |
| Local detection | `app/ai/detector.py` | `person, car, truck, bicycle, motorcycle, dog, cat, package` (SPEC 13). |
| Zones | `app/ai/zones.py` | Normalized rectangles + overlap math only. |
| Dwell | `app/ai/dwell.py` | Distinguishes a passing car from a parked one. |
| Semantics | `app/ai/semantics.py` | Derives type/zone/tags/description. |
| Best photo | `app/ai/best_photo.py` | One sharp, cropped representative frame. |
| AI analysis | `app/ai/provider.py` | SPEC 14/15 structured output + embedding. |
| Activity correlation | `app/services/activities.py` | SPEC 18/19 grouping across cameras. |

Every stage is defensive (SPEC 43): a detector, snapshot or AI failure is
logged and the event survives unenriched. Analysis never breaks ingestion.

## Event model extension (deliberate, backward compatible)

`HomeCamEvent.type` keeps its original enum
(`motion/person/vehicle/animal/package/doorbell/intrusion/unknown`). Richer
meaning rides on two **new nullable fields** instead of new top-level types:

- `zone` — the name of the zone the primary detection falls in.
- `tags` — a JSON list such as `["car", "parked", "driveway"]`.

So "a car parked on the driveway" is `type: vehicle`, `zone: driveway`,
`tags: [car, parked, driveway]`, with a human-readable `description`. Existing
consumers that only understand `type` keep working.

Derivation rules:

| Situation | Result |
| --- | --- |
| Person overlapping a `driveway` zone | `person`, tag `driveway-access` |
| Vehicle in a `driveway`/`parking` zone, stationary ≥ `PARKED_VEHICLE_SECONDS` | `vehicle`, tag `parked` |
| Vehicle in the same zone but moving | `vehicle`, tag `passing` |
| Any detection overlapping a `mailbox` zone | `package`, tag `mailbox` |
| `dog`/`cat` anywhere | `animal` |
| Doorbell press | stays `doorbell` (never overwritten) |
| No detection | unchanged (e.g. stays `motion`) |

People outrank vehicles and animals when several objects share a frame.

## Configuring zones

Zones are labelled rectangles in **normalized** image coordinates (`0.0–1.0`,
origin top-left), so they are resolution independent and work across providers
with different snapshot sizes. There is no computer-vision zone detection —
only overlap-with-bounding-box math, with a configurable minimum overlap
(`ZONE_MIN_OVERLAP`, default `0.3` of the detection box).

Admin UI: sign in to the **Admin** panel → *Detection zones*, pick a camera,
name the zone, choose a kind, and enter `x1/y1/x2/y2`.

Admin API (authenticated, same auth model as provider configuration):

```
GET    /api/v1/admin/cameras/{camera_id}/zones
POST   /api/v1/admin/cameras/{camera_id}/zones
PUT    /api/v1/admin/cameras/{camera_id}/zones/{zone_id}
DELETE /api/v1/admin/cameras/{camera_id}/zones/{zone_id}
```

```bash
curl -X POST http://localhost:8000/api/v1/admin/cameras/mock-front-door/zones \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"driveway","kind":"driveway","x1":0.0,"y1":0.45,"x2":0.65,"y2":1.0}'
```

Semantically meaningful kinds: `driveway`, `parking`, `mailbox`, `entry`,
`street`, `garden`, `other`. Any other name is stored and displayed but carries
no extra meaning.

## Best photo

When a target class is detected (`person`, `package`, vehicles, animals) the
pipeline samples `BEST_PHOTO_FRAMES` snapshots around the trigger, scores each
one as `0.6 × detection confidence + 0.4 × sharpness`, and stores the winning
frame — cropped to the detection box with 8% padding — under
`MEDIA_ROOT/best-photos/`. It is exposed as `best_photo_path` and
`thumbnail_path` on the event, and is distinct from any raw motion snapshot.

Sharpness uses a Laplacian-variance blur metric via Pillow + numpy when those
are importable. Without them the module falls back to a deterministic
pure-Python byte-delta proxy and stores the frame uncropped, so the default
install needs no extra dependencies.

## AI analysis and embeddings

`AIProvider` (`analyze_image`, `create_embedding`, `answer_question`) is the
only AI surface HomeCam Core talks to. The default `MockAIProvider` is
deterministic and offline, and its output is validated against a Pydantic
schema (`summary`, `objects`, `actions`, `event_category`, `importance`,
`confidence`) and **grounding-checked**: an analysis that mentions an object
that was never detected is rejected rather than persisted (SPEC 15).

Each analysed event gets one row in `ai_analyses` (SPEC 32) with the provider,
model, summary, objects, actions, category, confidence, the raw detections and
an embedding.

> **Deviation worth knowing:** the embedding is stored as a JSON float array
> plus an `embedding_dimensions` column rather than a native pgvector `vector`
> column, so the same schema runs on SQLite (tests, dev) and on the pgvector
> Postgres image used by Compose. `EMBEDDING_DIMENSIONS` is configurable; a
> native `vector` column can be swapped in later without changing the
> provider interface.

## Activity correlation

Temporally close events — across cameras and providers — are grouped into an
`activities` row with a category (`arrival/departure/delivery/visitor/vehicle/
unknown`), a summary, the linked event ids and the cameras involved. Only real
persisted events and their real timestamps are used; no intermediate action is
ever invented (SPEC 18). Visual re-identification is explicitly out of scope.

```
GET /api/v1/activities
GET /api/v1/activities/{id}
```

## Voice / audio activity

`audioDetection` is a first-class capability key in the existing
`SUPPORTED/UNSUPPORTED/UNAVAILABLE/UNKNOWN` map (SPEC 5/8.3/43), because most
cameras and providers do not expose a microphone feed to HomeCam.

The implementation (`app/ai/audio.py`) is a classic energy + zero-crossing-rate
voice-activity heuristic over mono 16-bit PCM. It detects **speech-like audio
activity only** — it is not transcription, not speech recognition and not
speaker identification, and must never be presented as such.

Enable with `AUDIO_DETECTION_ENABLED=true`, then:

```
POST /api/v1/cameras/{camera_id}/audio/analyze   {"pcm_base64": "..."}
```

Cameras whose provider advertises `audioDetection` as `UNAVAILABLE`/
`UNSUPPORTED` return `503` rather than HomeCam inventing audio. Dahua and Eufy
currently report `UNAVAILABLE`; the extension point is the provider's
capability map plus a call into `analyze_pcm`.

## Enabling the real ONNX detector (opt-in)

The default backend is `mock`: deterministic, zero extra dependencies, used by
CI and every test.

```bash
pip install onnxruntime numpy pillow
# Download a small COCO-pretrained YOLO model yourself, e.g. YOLOv8n exported
# to ONNX from https://github.com/ultralytics/ultralytics (AGPL-3.0 — check
# that the licence suits your deployment before using the pretrained weights).
export AI_DETECTOR_BACKEND=onnx
export AI_DETECTOR_MODEL_PATH=/path/to/yolov8n.onnx
```

No model weights are committed to this repository. `onnxruntime` does publish
a `win_arm64` wheel for CPython 3.12, so this host can run the backend, but the
backend has **not** been verified against real hardware or a real model here.

If the runtime, numpy, Pillow or the model file are missing or unreadable, the
detector logs a warning and falls back to the mock backend. It never crashes a
request.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AI_DETECTOR_BACKEND` | `mock` | `mock` or `onnx`. |
| `AI_DETECTOR_MODEL_PATH` | *(empty)* | ONNX model path (opt-in backend only). |
| `AI_ANALYSIS_ENABLED` | `true` | Master switch for the enrichment stages. |
| `EMBEDDING_DIMENSIONS` | `384` | Width of stored embeddings. |
| `BEST_PHOTO_ENABLED` | `true` | Persist a best photo per detected event. |
| `BEST_PHOTO_FRAMES` | `3` | Candidate snapshots sampled per event. |
| `MEDIA_ROOT` | `./media` | Where best photos are written. |
| `ZONE_MIN_OVERLAP` | `0.3` | Detection-box fraction required to be "in" a zone. |
| `PARKED_VEHICLE_SECONDS` | `60` | Stationary time before a vehicle counts as parked. |
| `AUDIO_DETECTION_ENABLED` | `false` | Enables the audio analysis endpoint. |
| `AUDIO_ENERGY_THRESHOLD` | `0.02` | RMS floor for speech-like audio. |
| `ACTIVITY_CORRELATION_ENABLED` | `true` | Group related events into activities. |
| `ACTIVITY_CORRELATION_WINDOW_SECONDS` | `120` | Temporal grouping window. |

## Known limitations

- **No voice transcription or speaker identification.** Only speech-like audio
  activity detection, and only where a provider exposes audio (none today).
- **No person identity or face clustering.** "The same person as yesterday" is
  not supported; correlation is temporal only.
- **No ONNX model bundled**, and the ONNX backend is not hardware-verified in
  this repository.
- **Mock detector cannot see the frame.** It derives detections from the event
  type and reports nothing for a bare motion trigger rather than inventing an
  object class. Real recognition for unlabelled motion requires the ONNX
  backend.
- **Embeddings are JSON arrays, not a native pgvector column** (see above), so
  similarity search is not yet index-accelerated.
- **Dwell state is in-process**, so a restart costs at most one dwell window
  before a parked car is recognised again.
