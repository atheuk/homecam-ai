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
| Person at a `mailbox` zone | stays `person`, tag `mailbox` (a delivery is decided by the mailbox watcher, below) |
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

The photo is always of **the event's own subject**: a person event is cropped
to the person, an animal event to the animal, a vehicle event to the vehicle,
however confident any other object in the frame is. A frame that contains the
subject beats a sharper frame that doesn't; other classes are used only when
the subject is absent from every sampled frame. Appearance analysis and
re-identification therefore never receive a car crop for a person event.
`best_photo.boxes` (drawn as `photo_boxes`) are in the stored photo's
coordinates; `best_photo.frame_boxes` keep every detection in full-frame
coordinates.

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

## Continuous event ingestion (real, not just `/mock/events`)

Historically nothing in this codebase *triggered* the pipeline from real
camera activity — events only ever appeared through the `/mock/events` test
endpoint. `app/services/ingestion.py` closes that gap: an opt-in background
loop (started from the FastAPI `lifespan`, alongside the API's own
Tailscale-connected provider access) periodically snapshots every online,
snapshot-capable camera across every provider, runs it through the configured
local detector, and creates a real event whenever the detector actually sees
something. Each subject present in the frame — person, animal, vehicle,
package — raises its own event, so a person walking past a parked car yields a
person event *and* a vehicle event. The cooldown (`EVENT_COOLDOWN_SECONDS`) is
per camera **and subject**: continued presence doesn't create a new event
every poll, but a car parked in view all day never silences the people or
animals that pass it. Events raised from the same moment share one frame
sample, to spare the NVR's small concurrent-session budget.

### Frame source: the sub-stream, not `snapshot.cgi`

The Dahua NVR behind the edge connector sustains only ~1-2 concurrent CGI
sessions. In production its 4K `snapshot.cgi` was refused 69-82% of the time,
so ingestion saw each camera roughly once every ~100s and a person crossing in
5-10s was almost never sampled. Cameras whose provider sets
`supports_stream_frames` (Dahua edge mode) are therefore sampled from the
H.264 sub-stream (704x576) that the edge already relays through MediaMTX as
LL-HLS. MediaMTX holds a single RTSP session per channel however many clients
read it, so this adds no NVR CGI load and needs no edge add-on update.

`app/services/stream_frames.py` runs one reader per camera. Every
`STREAM_SAMPLE_INTERVAL_SECONDS` it re-reads the media playlist, downloads
only the newest complete segment (~2s, starting on a keyframe), decodes it
with OpenCV/FFmpeg and keeps the last few frames in memory (they double as
the event's best-photo candidates). The anamorphic D1 picture is stretched to
the camera's real aspect ratio, learned from its last real snapshot or set
with `STREAM_FRAME_ASPECT_RATIO`. Readers back off exponentially on failure,
re-resolve the stream URL after repeated failures, and stop when idle.
Ingestion only uses a frame younger than `STREAM_FRAME_MAX_AGE_SECONDS` and
never runs detection twice on the same segment. Without one it falls back to
a CGI snapshot, at most once per `EVENT_POLL_INTERVAL_SECONDS` per camera, so
a stream outage never puts more load on the NVR than before. Each event
records `metadata.frame_source` (`stream` or `snapshot`). The dashboard
`/cameras/{id}/snapshot` also serves a fresh cached stream frame when one
exists (response header `X-Frame-Source`).

The 4s default costs RT-DETR r18 ~225ms plus ~100ms of segment decoding per
camera per sample, about 0.33 of a core for four cameras. Cooldowns still bound
the number of events, and Foundry calls are made per event, never per frame.
Every `INGESTION_STATS_LOG_SECONDS` the API logs a per-camera
`ingestion frames …` line (frames, success %, cadence, sources, suppressions)
and a per-reader `stream frames …` line.

### Stationary objects

A parked car used to re-emit a vehicle event every cooldown, all day. Each
emitted event now adds its subject's boxes to a per-camera, per-subject set
of known objects. Each object in the set expires on its own after
`STATIONARY_SUPPRESS_SECONDS` from when it was first reported. A later
detection where every box matches a known object (IoU, or containment in
it, of at least `STATIONARY_IOU_THRESHOLD`) is treated as the same objects,
not moved, and is suppressed. The set is cumulative on purpose. A
half-out-of-frame car that the detector only sometimes finds is learned
once. It does not re-emit the parked car beside it each time it
reappears. While at least one known object is still in view, an unmatched
box also needs `STATIONARY_NEW_OBJECT_MIN_CONFIDENCE` (0.7) to count as a new
object. Live, the unmatched boxes beside the parked car were distant street
traffic and edge flicker at 0.51-0.65. Each one re-emitted an event whose
best photo was the parked car. A new or moved object that is detected
confidently still emits. The subjects this applies to
are set by `STATIONARY_SUBJECTS` (default `animal,package`); people are never
suppressed. Vehicles use the scene engine below instead.

## Temporal scene engine (parked cars, mail, bins)

Some things worth knowing are not visible in any single frame: a car that
has been parked all day, post dropped in the letterbox, bins put out or
emptied. `app/services/scene_state.py` keeps a small per-camera state across
frames and emits an event only when that state *changes*. The state is
persisted in the `scene_states` table (version 1, ignored when older than
`SCENE_STATE_MAX_AGE_SECONDS`), so a redeploy does not re-announce everything
in view. Every transition event carries `metadata.temporal` (what changed,
the basis, and anything that could not be confirmed under `unknowns`), and
the before/during/after crops are stored in `event_evidence` and served at
`GET /api/v1/events/{id}/evidence/{before|during|after}`.
`GET /api/v1/cameras/{id}/scene-state` shows the live state.

### Parked vehicles (no zone needed)

Each vehicle box is matched to a track (IoU ≥ `VEHICLE_TRACK_MATCH_IOU`, or
containment ≥ `VEHICLE_TRACK_CONTAINMENT`, so detector jitter and a half-seen
car stay one track). The first event for a car is raised once it is
confirmed (two frames, or confidence ≥ `VEHICLE_CONFIRM_CONFIDENCE`, and at
least `VEHICLE_MIN_AREA` of the frame, so distant street traffic never
counts). After `VEHICLE_PARKED_OBSERVATIONS` unmoved frames the track is
`parked` and that same event is tagged `vehicle_parked` (plus
`vehicle_arrived` if the car was seen driving in, rather than being there
when the camera was first watched). From then on it is silent. Tags:

| Tag | When |
| --- | --- |
| `vehicle_arrived`, `vehicle_parked` | A new car came to rest (tags the car's first event, no new event). |
| `vehicle_moved` | A parked car's box moved by more than `VEHICLE_MOVE_THRESHOLD` (centre) and below `VEHICLE_MOVED_IOU`. |
| `vehicle_departed` | A parked car was absent for `VEHICLE_ABSENCE_FRAMES` processed frames *and* `VEHICLE_ABSENCE_SECONDS`. A camera outage produces no frames, so it is never a departure. |
| `vehicle_returned` | A car reappears in a spot a car departed from within `VEHICLE_RETURN_WINDOW_SECONDS`. |
| `vehicle_interaction` | On a *person* event: the person overlapped a parked car (≥ `VEHICLE_INTERACTION_OVERLAP` of their box) for two frames in a row. The car then counts afresh, so a drive-off is reported. |

Person events are never suppressed by any of this. All vehicle transitions
still respect the per-camera vehicle `EVENT_COOLDOWN_SECONDS`.

### Mail delivery (needs a `mailbox` zone)

Draw a tight `mailbox` zone around the letterbox. A visit starts when a
person covers ≥ `MAILBOX_ZONE_COVER` of the zone and ends when they leave;
visits shorter than `MAILBOX_MIN_FRAMES` frames are walk-bys and are
dropped. After a visit, a before/during/after set of zone crops goes to
Foundry as one closed question ("was an item put in, letter or parcel?").
The prompt describes the mailbox only and never the person. A confident
yes (≥ `MAILBOX_MIN_CONFIDENCE`) gives a `package` event tagged `mailbox`,
`mailbox_delivery`, `letter`/`parcel`. Anything less gives a `motion` event
tagged `mailbox_activity` with the reason in `unknowns`. A parcel the
detector itself sees left in the zone counts without the vision check.
`MAILBOX_COOLDOWN_SECONDS` separates deliveries.

### Bins (needs a `bins` zone)

Draw a `bins` zone where the bins stand when they are out. The zone keeps a
lighting-normalised 32×32 fingerprint. When it changes by more than
`BIN_CHANGE_THRESHOLD` for `BIN_SETTLE_FRAMES` frames with nothing in front
of it, a before/after pair is assessed by Foundry (bin counts only). Frames
where a person or vehicle covers the zone, and dark/blank frames, are
skipped. Transitions: `bin_placed_out` (none → some), and `bin_emptied`,
which needs evidence: a truck at the bins within
`BIN_COLLECTION_WINDOW_SECONDS`, or a confident "emptied" from the vision
check. Bins simply disappearing only counts as emptied if
`BIN_EMPTIED_ON_DISAPPEARANCE=true`. The first look after setup only learns
the state and emits nothing.

### Limitations

- Mail and bins need a zone per camera and Foundry (`TEMPORAL_VISION_ENABLED`);
  without them those watchers do nothing rather than guess.
- RT-DETR's COCO classes have no "bin" or "parcel", so these rely on the zone
  fingerprint and the vision check, not the detector.
- A car hidden behind another for longer than the absence window can be
  reported as departed; a car that moves by less than the move threshold is
  still "parked".
- None of this identifies anyone. It never infers gender or ethnicity, and
  trust stays a human decision.

Every ingested event still flows through the same
`create_and_broadcast_event` → `enrich_event` pipeline as any other event
source, so its final `type`/`zone`/`tags`/best photo/AI analysis is
recomputed from a fresh snapshot rather than trusted verbatim.

Because `MockDetector` cannot see pixels and intentionally returns no
detections for a bare/unlabeled poll, this loop is a safe no-op under the
default `mock` backend. Enabling `EVENT_INGESTION_ENABLED` only becomes
useful once a pixel-aware backend (`rtdetr`, or the legacy `opencv`/`onnx`) is also configured.

```bash
export EVENT_INGESTION_ENABLED=true
export AI_DETECTOR_BACKEND=rtdetr   # recommended; see below
```

## Enabling the RT-DETR detector (recommended, shipped in the image)

`rtdetr` is the **production detection backend** and the one deployed to
Azure. It runs [RT-DETR](https://github.com/lyuwenyu/RT-DETR) (Baidu), a
DETR-family end-to-end detector, through ONNX Runtime on CPU.

```bash
pip install onnxruntime numpy pillow
export AI_DETECTOR_BACKEND=rtdetr
export AI_DETECTOR_MODEL_PATH=/app/models/rtdetr.onnx   # default; baked into the image
```

The model file is downloaded and verified **at image build time** by
`apps/api/Dockerfile`, so a running container never needs outbound access to
huggingface.co and startup is deterministic. The build fails loudly if the
download is truncated.

### Why RT-DETR and not YOLO

This is a licensing decision as much as a technical one.

* **Ultralytics YOLO is AGPL-3.0** — both the code *and* the pretrained
  weights. AGPL section 13 extends copyleft to *network* use, and
  Ultralytics' own FAQ states that serving it "through a SaaS platform, API,
  or other private system" still requires their paid Enterprise licence.
  Hiding it behind an HTTP microservice is **not** a reliable escape hatch.
* **RT-DETR upstream (`PekingU/rtdetr_r18vd`) is Apache-2.0 for code and
  weights**, so it can be baked into a container image and shipped without
  contaminating this codebase.
* YOLO-NAS (`Deci-AI/super-gradients`) was also rejected: its code is
  Apache-2.0 but `LICENSE.YOLONAS.md` explicitly forbids commercial and
  production use of the weights.

### Measured behaviour

Validated on this project's own camera frames plus COCO control images,
using the INT8 export (~21 MB) actually shipped:

| Image | Result |
| --- | --- |
| COCO `000000000785` (skier) | `person` @ 0.949 |
| COCO `000000039769` (two cats) | `cat` @ 0.952, `cat` @ 0.948 |
| 6 real HomeCam "person" event frames | **0 person detections** |

Those six frames are the important row. The previous `opencv` backend
reported 1–3 `person` boxes on *every one* of them, while the Azure vision
model independently captioned them "No clear view of a person." RT-DETR
agrees with the vision model. The FP32 export was run as a control at a
lowered 0.25 threshold and also found no person, confirming the empty result
is the scene being empty rather than INT8 quantization damage (INT8 tracked
FP32 within 0.001 on the control images).

Mean latency was ~180 ms/frame on a CPU-only ARM64 dev host.

### Implementation notes

RT-DETR differs from the YOLO path in two ways that the decoder must respect:

* It emits a fixed **300 object queries** trained with one-to-one matching,
  so it is already duplicate-free and needs **no NMS**. `refine_detections`
  still runs for the geometric plausibility floor and the per-frame cap, but
  suppression is effectively a no-op on well-formed output.
* Class scores are **sigmoid** logits (focal loss), not softmax, and there is
  no separate objectness term to multiply in.

The decoder asserts the head exposes exactly 80 classes and returns nothing
if it does not, rather than guessing at a shifted COCO-91 label map.

## Enabling the OpenCV detector (legacy, not recommended)

> **Superseded.** `opencv` was the original "no model download required"
> backend. It is retained for offline/air-gapped use, but it was measured
> producing phantom `person` boxes on empty frames (see the table above) and
> should not be used in production. Prefer `rtdetr`.

`opencv` uses the HOG + linear-SVM pedestrian detector bundled inside the
`opencv-python-headless` wheel itself
(`cv2.HOGDescriptor_getDefaultPeopleDetector()`), so there is no external
model file to source, license or download.

```bash
pip install opencv-python-headless numpy
export AI_DETECTOR_BACKEND=opencv
```

Detection confidences come from the HOG SVM's decision-function weights,
passed through a sigmoid and thresholded at `confidence_threshold=0.35` — a
reasonable default, not calibrated against a labelled dataset. Only the
`person` class is produced by this backend today; it does not detect
vehicles/animals/packages the way the mock or ONNX/RT-DETR backends can.

`opencv-python-headless` has no prebuilt wheel for Windows ARM64 as of this
writing; `requirements.txt` gates the dependency with a PEP 508 environment
marker so local dev/CI on that platform simply skips it and stays on the
mock backend, while CI (`ubuntu-latest`) and the deployed Linux containers
install and use it normally. Like every opt-in backend here, a missing/failed
`cv2` import falls back to the mock backend rather than crashing a request.

## Enabling the legacy YOLO ONNX detector (opt-in, discouraged)

> **Licensing warning.** See "Why RT-DETR and not YOLO" above. The obvious
> models for this backend are AGPL-3.0 Ultralytics exports. Use `rtdetr`
> unless you have specifically licensed something else.

The default backend is `mock`: deterministic, zero extra dependencies, used by
CI and every test.

```bash
pip install onnxruntime numpy pillow
# You must source a COCO-pretrained YOLO model yourself and satisfy its
# licence. No model weights for this backend are committed to this repo.
export AI_DETECTOR_BACKEND=onnx
export AI_DETECTOR_MODEL_PATH=/path/to/model.onnx
```

If the runtime, numpy, Pillow or the model file are missing or unreadable, the
detector logs a warning and falls back to the mock backend. It never crashes a
request.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AI_DETECTOR_BACKEND` | `mock` | `mock`, `rtdetr`, `opencv`, or `onnx`. `rtdetr` is the production backend (deployed to Azure); `mock` is the zero-dependency default used by CI and tests. |
| `AI_DETECTOR_MODEL_PATH` | *(empty)* | ONNX model path for `rtdetr` or `onnx`. For `rtdetr`, empty means `/app/models/rtdetr.onnx`, which `apps/api/Dockerfile` bakes into the image. |
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
| `EVENT_INGESTION_ENABLED` | `false` | Background loop that creates real events from live camera snapshots. |
| `EVENT_POLL_INTERVAL_SECONDS` | `20` | Minimum spacing between CGI snapshots of one camera (snapshot-only cameras, and the fallback for stream cameras). |
| `STREAM_FRAMES_ENABLED` | `true` | Sample stream-capable cameras (Dahua edge) from their relayed sub-stream. |
| `STREAM_SAMPLE_INTERVAL_SECONDS` | `4` | Stream sampling interval and ingestion loop tick. |
| `STREAM_FRAME_MAX_AGE_SECONDS` | `15` | Older stream frames are stale; ingestion falls back to a snapshot. |
| `STREAM_READER_IDLE_SECONDS` | `300` | A stream reader nobody asked for a frame within this long stops. |
| `STREAM_FRAME_ASPECT_RATIO` | learned | Display width/height for stream frames (default: learned from a real snapshot). |
| `STATIONARY_SUPPRESS_SECONDS` | `1800` | How long an unmoved object (same camera+subject, matching box) is not re-reported. |
| `STATIONARY_IOU_THRESHOLD` | `0.8` | IoU/containment at which a box counts as the same, unmoved object. |
| `STATIONARY_SUBJECTS` | `animal,package` | Subjects subject to stationary suppression (never `person`; vehicles use the scene engine). |
| `STATIONARY_NEW_OBJECT_MIN_CONFIDENCE` | `0.7` | While a known object is still in view, the confidence an unmatched box needs to count as a new object. |
| `VEHICLE_TRACKING_ENABLED` | `true` | Parked-vehicle state machine (arrived/parked/moved/departed/returned). |
| `VEHICLE_PARKED_OBSERVATIONS` | `5` | Unmoved frames before a car counts as parked and goes silent. |
| `VEHICLE_TRACK_MATCH_IOU` / `VEHICLE_TRACK_CONTAINMENT` | `0.3` / `0.8` | Box match to an existing track. |
| `VEHICLE_MOVE_THRESHOLD` / `VEHICLE_MOVED_IOU` | `0.05` / `0.7` | A parked car moved: centre shift above, and IoU with its parked box below. |
| `VEHICLE_ABSENCE_FRAMES` / `VEHICLE_ABSENCE_SECONDS` | `10` / `300` | Both needed before a parked car is reported departed. |
| `VEHICLE_MIN_AREA` / `VEHICLE_CONFIRM_CONFIDENCE` | `0.02` / `0.8` | A vehicle is announced only above this size, after two frames or at this confidence. |
| `VEHICLE_RETURN_WINDOW_SECONDS` | `86400` | A car back in a departed spot within this is `vehicle_returned`. |
| `VEHICLE_INTERACTION_OVERLAP` | `0.3` | Share of a person's box on a parked car for `vehicle_interaction`. |
| `SCENE_STATE_MAX_AGE_SECONDS` | `86400` | Persisted scene state older than this is ignored on start. |
| `MAILBOX_ZONE_COVER` / `MAILBOX_MIN_FRAMES` | `0.25` / `2` | A mailbox visit: share of the zone covered, and minimum frames. |
| `MAILBOX_MAX_EPISODE_SECONDS` / `MAILBOX_BEFORE_MAX_AGE_SECONDS` | `120` / `60` | Longest visit, and oldest usable "before" frame. |
| `MAILBOX_COOLDOWN_SECONDS` / `MAILBOX_MIN_CONFIDENCE` | `600` / `0.6` | Between deliveries; vision confidence for a confirmed delivery. |
| `BIN_CHANGE_THRESHOLD` / `BIN_SETTLE_FRAMES` / `BIN_BASELINE_ALPHA` | `0.35` / `3` / `0.1` | Zone fingerprint change, frames it must hold, baseline adaptation. |
| `BIN_COOLDOWN_SECONDS` | `900` | Minimum time between two identical bin transitions. |
| `BIN_COLLECTION_WINDOW_SECONDS` | `14400` | How long a truck at the bins counts as collection evidence. |
| `BIN_EMPTIED_ON_DISAPPEARANCE` | `false` | Treat bins vanishing (no truck, no vision "emptied") as emptied. |
| `TEMPORAL_VISION_ENABLED` | `true` | Foundry closed-question checks for mail and bins. |
| `INGESTION_STATS_LOG_SECONDS` | `300` | Interval of the per-camera frame acquisition log lines. |
| `EVENT_COOLDOWN_SECONDS` | `120` | Minimum time between two created events for the same camera *and subject* (person/animal/vehicle/package). |
| `MOCK_CAMERAS_ENABLED` | auto | Scripted `mock-*` demo cameras. Auto: shown unless `APP_ENV=production` and a real provider is configured. |

## Known limitations

- **No voice transcription or speaker identification.** Only speech-like audio
  activity detection, and only where a provider exposes audio (none today).
- **No person identity or face clustering.** "The same person as yesterday" is
  not supported; correlation is temporal only.
- **No ONNX model bundled**, and the ONNX backend is not hardware-verified in
  this repository.
- **Mock detector cannot see the frame.** It derives detections from the event
  type and reports nothing for a bare motion trigger rather than inventing an
  object class. Real recognition for unlabelled motion (including continuous
  event ingestion, see above) requires the `opencv` or `onnx` backend.
- **The `opencv` backend only detects people today.** It is the HOG+SVM
  pedestrian detector bundled in the OpenCV wheel; vehicles/animals/packages
  still need the `onnx` backend with a suitable multi-class model, or remain
  scripted-only via `/mock/events`.
- **Embeddings are JSON arrays, not a native pgvector column** (see above), so
  similarity search is not yet index-accelerated.
- **Dwell state is in-process**, so a restart costs at most one dwell window
  before a parked car is recognised again.
