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
| Zones | `app/ai/zones.py` | Normalized rectangles/polygons + overlap math only. |
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

Zones are labelled **shapes** in **normalized** image coordinates (`0.0–1.0`,
origin top-left), so they are resolution independent and work across providers
with different snapshot sizes. There is no computer-vision zone detection —
only overlap-with-bounding-box math, with a configurable minimum overlap
(`ZONE_MIN_OVERLAP`, default `0.3` of the detection box).

A zone is either a plain **rectangle** (`x1/y1/x2/y2`) or a **polygon** of 3–64
points (`points`, a list of `[x, y]` pairs) drawn on a still from the camera.
A polygon always also stores its bounding box, which is derived server-side, so
existing rectangle-only behaviour is unchanged and every zone still has a
well-formed box.

Admin UI: sign in to the **Admin** panel → *Detection zones*, pick a camera,
then draw the zone directly on the current picture from that camera: click or
tap to drop points around the area, undo or clear while drawing, name it, pick
its kind and save. Existing zones are outlined on the picture and can be
re-drawn with *Edit shape* or removed with *Delete*. The manual `x1/y1/x2/y2`
rectangle form is still available underneath, and points can also be typed as
coordinates for keyboard-only use.

The picture comes from an authenticated admin endpoint that reuses the normal
frame path — a frame the ingestion stream reader has already decoded if there is
one, otherwise a single provider snapshot:

```
GET /api/v1/admin/cameras/{camera_id}/still
```

It returns the JPEG as a `data:` URL (so camera imagery is never loaded from an
unauthenticated `<img src>`) plus `width`/`height` when readable and `source`
(`stream` or `snapshot`). A camera that is known-offline is never contacted, and
a camera whose capture just failed is left alone for
`camera_stills.FAILURE_COOLDOWN_SECONDS` (10s), so repeatedly retrying a
disconnected channel cannot become a request storm. Offline or unreachable
cameras return `503` with a readable reason, which the editor shows with a
retry button.

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

Sending `"points": []` on a `PUT` turns a polygon back into its plain rectangle.
A polygon zone is created by sending `points` instead of `x1/y1/x2/y2`, for
example `{"name":"mailbox","kind":"mailbox","points":[[0.70,0.30],[0.95,0.34],[0.92,0.68],[0.71,0.63]]}`.

Semantically meaningful kinds: `driveway`, `parking`, `mailbox`, `bin`, `entry`,
`street`, `garden`, `other`. Any other name is stored and displayed but carries
no extra meaning.

**Polygon nuance.** Only *overlap matching* (`zones_for_bbox`, which decides
whether a detection "is in" a zone) uses the exact polygon, via
Sutherland–Hodgman clipping of the shape against the detection box. The
stateful mailbox/bin detectors in `app/services/scene_state.py` crop and compare
*rectangular* image regions, so they keep using the zone's bounding box. Drawing
a tight polygon therefore makes zone membership more precise without changing
how those region comparisons work.

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
are set by `STATIONARY_SUBJECTS`; people are never suppressed.

With `VEHICLE_TRACKING_ENABLED=true` (default) vehicles no longer use this
cooldown/suppression path at all; they are handled by the persistent scene
state below. Animals and packages still are.

### Scene state: vehicles, mailbox deliveries, bins

`app/services/scene_state.py` runs on **every** sampled frame (independently
of event cooldowns) and keeps per-camera state in two tables,
`vehicle_tracks` and `scene_states` (migration `0008_scene_state`), so it
survives restarts. It emits only *transitions*, as ordinary events of an
existing SPEC 9 type; what happened is in `tags`, `metadata.scene` (also
returned top-level as `scene` by the events API) and a kind-specific
metadata block. Enrichment keeps a scene event's type, description and tags
and only adds to them.

**Vehicles** (`metadata.vehicle_track`: `track_id`, `state`,
`observation_count`, `first_seen`, `stationary_since`, `transition`, `box`)

| Transition | When | Tags |
|---|---|---|
| `arrived` | a new track confirmed after `VEHICLE_CONFIRM_OBSERVATIONS` (2) samples, and the camera had been watching the spot for `VEHICLE_ABSENCE_SECONDS` before it appeared | `vehicle_arrived` |
| `first_seen` | confirmed, but it was already there when observation began (startup, after an outage) — no arrival is claimed | — |
| parked | `VEHICLE_STABLE_OBSERVATIONS` (5) matching samples (IoU with the anchor box ≥ `VEHICLE_STABLE_IOU`): **no new event**; the reporting event is tagged | `vehicle_parked` |
| `moved` | a parked track's box changes materially | `vehicle_moved` |
| `interaction` | a person overlaps a tracked vehicle for `VEHICLE_INTERACTION_OBSERVATIONS` samples (cooldown `VEHICLE_INTERACTION_COOLDOWN_SECONDS`); the count restarts | `vehicle_interaction` |
| `departed` | unseen for `VEHICLE_ABSENCE_SECONDS` of camera time that actually delivered frames (an outage is not absence) | `vehicle_departed` |
| `returned` | a new track in the place of a departed one within `VEHICLE_RETURN_WINDOW_SECONDS`, with a compatible colour signature | `vehicle_arrived`, `vehicle_returned` |

A car seen in a single sample (passing traffic) is never reported. Each
vehicle is its own track; a partial second box on the same car and
low-confidence boxes (`VEHICLE_NEW_TRACK_MIN_CONFIDENCE`) do not create
tracks. After parking, a vehicle costs no events and no Foundry calls.
`GET /api/v1/admin/cameras/{id}/scene-state` shows the tracks and zone states.

**Mailbox** — add a zone of kind `mailbox` (off until you do). Every
qualifying mailbox interaction emits exactly one `package` event (subject to
dedup) whose tags start with `mailbox` plus one of:

| Transition / tag | When | Priority |
| --- | --- | --- |
| `mailbox_delivery` | an item was put in (package appeared, or Foundry `action=deposited`) | normal (never lower) |
| `mailbox_retrieval` | an item was taken out (package present before and gone after, or Foundry `action=retrieved`) | normal; high while armed away/night; a locally seen parcel removal also carries `package_removed` (high/critical) |
| `mailbox_opened` | the mailbox was opened/checked with no item change, including with nobody in view | normal |
| `mailbox_visit` | a person was at the mailbox but the outcome is unknown | low |

*Opening detection (no person needed).* The tracker keeps a rolling
reference crop of the idle mailbox (zone expanded slightly), updated only
when it is stable and nobody is near. Each frame's crop is compared with a
brightness/contrast-normalised grid signature (the same helper as the bin
detector, `region_signature`); a difference ≥ `MAILBOX_OPEN_THRESHOLD` for
`MAILBOX_OPEN_MIN_FRAMES` frames is an opening (one frame is enough while a
person is near), and dropping below 70 % of the threshold is closed. A
4×4 grid of context tiles away from the mailbox guards against whole-frame
changes (IR switch, auto exposure): when more than half of them change too,
the reference is rebased instead of reporting an opening. Frames where a
person or vehicle covers the mailbox are skipped. Standalone openings are
rate-limited by `MAILBOX_OPEN_COOLDOWN_SECONDS`.

*Visits.* A person is "near" when their box covers ≥
`MAILBOX_MIN_ZONE_OVERLAP` of the zone expanded by
`MAILBOX_PROXIMITY_MARGIN` (so reaching in from the side counts). A single
near frame counts as a visit when the lid or a package changed; otherwise at
least `MAILBOX_MIN_OBSERVATIONS` (default 1) near frames are needed, or it
is a walk-by (logged, no event). When the person has left, the
before/during/after evidence decides: a local package change classifies
immediately; otherwise the Foundry vision deployment answers one closed
JSON question about the BEFORE/DURING/AFTER crops (`action:
deposited|retrieved|opened_only|none`, plus `item_deposited`, `item_type`,
`person_interacted`, `confidence`), rate-limited by
`SCENE_VERIFIER_MIN_INTERVAL_SECONDS`. Without Foundry the deterministic
fallback reports `mailbox_opened` when the lid changed and `mailbox_visit`
otherwise. A package seen only during the visit was carried past
(`mailbox_visit`). Identity is never inferred: descriptions say "someone".

*Sampling boost.* With stream frames enabled, a person near a mailbox zone
asks the relayed stream for one sample every
`MAILBOX_BOOST_INTERVAL_SECONDS` for `MAILBOX_BOOST_SECONDS`, so a 3–6 s
mail drop is seen in several frames. Snapshot-only cameras are not boosted
(the NVR CGI budget is fixed); for those, the single-frame rules above apply.

*Diagnostics.* Each finished visit logs one INFO line
`mailbox <camera>/<zone>: visit <id> observations=… cover=… diff=…
package_before=… package_after=… lid_changed=… outcome=…`, and lid
open/close changes are logged too. The periodic `ingestion frames` stats
line adds `boosted=yes|no` and `mailbox_visits`, `mailbox_walk_by`,
`mailbox_events`, `mailbox_opened`, `mailbox_deduped` for the window.

*Dedup.* Each transition is deduplicated per zone in memory and, across
replicas, by a DB claim on `<transition>:<camera>:<zone>` (window
`MAILBOX_DEDUPE_SECONDS` for deliveries/retrievals, the open cooldown for
openings/visits). The pre-existing "person at the mailbox" event from zone
semantics is unchanged.

*One ingester per camera.* Scene state (vehicle tracks, bin and mailbox
records, the lid reference crop) is cached in memory and written back after
every frame, so two replicas ingesting the same camera would overwrite each
other's newer state. A per-camera lease in `ingestion_leases`
(`app/services/ingestion_lease.py`) prevents this. Only the holder samples the
camera and advances its scene state. It renews the lease every
`INGESTION_LEASE_TTL_SECONDS / 2` with a conditional `UPDATE`, and other
replicas stand by. A standby takes over once the lease is older than the TTL,
or immediately after a clean shutdown releases it. Gaining or losing a lease
drops the cached scene, so the new holder resumes from the database. A
replica that loses a lease also stops its stream reader for that camera.
Expiry is judged only by the database clock (`clock_timestamp()` on Postgres,
`julianday('now')` on SQLite), so clock skew between replicas cannot produce
two holders. Every acquisition increments the lease `epoch`, a fencing token. A
frame can outlive its lease (a slow detector or Foundry check), so each
scene-state save and each scene-event insert first runs a fencing `UPDATE` in
the same transaction. It matches only while this replica still holds that
epoch unexpired, and it locks the lease row until the write commits.
Otherwise the write and its events are discarded and the stale cache dropped.
Subject events from a frame whose lease has lapsed are dropped too.

**Bins** — add a zone of kind `bin` around the curb spot (off until you do).
Every `BIN_CHECK_INTERVAL_SECONDS`, when no person/vehicle occludes it, the
zone is compared with a brightness-normalized baseline; a change stable for
`BIN_CHANGE_CONFIRM_CHECKS` checks is a candidate. Presence before/now comes
from local labels (`BIN_LOCAL_LABELS`, if your model has a bin class) or a
closed Foundry question on the BEFORE/(DURING)/NOW crops. absent → present
emits `bin_placed_out`. `bin_emptied` needs present before **and** after, a
recent interaction (a truck at the zone, or a person with Foundry saying the
bin was moved/tipped), and no camera outage in between. A bin disappearing
is never "emptied" unless `BIN_REMOVAL_COUNTS_AS_EMPTIED=true` and a
collection vehicle was seen and there was no outage. Bin events are `motion`
events tagged `bin` + the transition, deduplicated for `BIN_DEDUPE_SECONDS`.

The Foundry prompts (`app/ai/scene_verifier.py`) are closed questions about
the object only; they never describe people, and never infer identity,
gender or ethnicity. Answers outside `yes/no/unknown` become `unknown`.

Foundry checks never run on the frame path. Each poll emits person/animal/
vehicle subject events first, then advances the scene state; a mailbox or bin
candidate that needs Foundry starts a background check (bounded by
`FOUNDRY_TIMEOUT_SECONDS`) and the zone reports `verifying` until a later
frame picks up the answer and decides. If the persisted scene state for a
camera cannot be used (corrupt or incompatible rows), that camera's scene
state is reset and rebuilt from new frames instead of failing every poll.

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
| `STATIONARY_SUBJECTS` | `vehicle,animal,package` | Subjects subject to stationary suppression (never `person`). |
| `STATIONARY_NEW_OBJECT_MIN_CONFIDENCE` | `0.7` | While a known object is still in view, the confidence an unmatched box needs to count as a new object. |
| `INGESTION_STATS_LOG_SECONDS` | `300` | Interval of the per-camera frame acquisition log lines. |
| `VEHICLE_TRACKING_ENABLED` | `true` | Persistent vehicle tracks (arrived/parked/moved/departed/returned) instead of cooldown events. |
| `VEHICLE_CONFIRM_OBSERVATIONS` | `2` | Samples before a new vehicle is reported (1 = report first sighting). |
| `VEHICLE_STABLE_OBSERVATIONS` | `5` | Matching samples after which a vehicle is parked and goes quiet. |
| `VEHICLE_STABLE_IOU` / `VEHICLE_MATCH_IOU` | `0.7` / `0.3` | Same-place threshold / association threshold. |
| `VEHICLE_NEW_TRACK_MIN_CONFIDENCE` | `0.7` | Confidence needed to start a new vehicle track. |
| `VEHICLE_ABSENCE_SECONDS` | `180` | Unseen (with frames arriving) before a vehicle has departed. |
| `VEHICLE_RETURN_WINDOW_SECONDS` | `86400` | How long a departed vehicle can be recognised as returned. |
| `SCENE_OUTAGE_SECONDS` | `60` | A frame gap longer than this is a camera outage. |
| `MAILBOX_DELIVERY_ENABLED` | `true` | Mailbox detection (only acts on `mailbox` zones). |
| `MAILBOX_MIN_OBSERVATIONS` / `MAILBOX_DEDUPE_SECONDS` | `1` / `900` | Near frames for a visit without a lid/package change / one delivery or retrieval per window. |
| `MAILBOX_MIN_ZONE_OVERLAP` / `MAILBOX_PROXIMITY_MARGIN` | `0.2` / `0.1` | Person cover needed over the zone expanded by the margin (normalised). |
| `MAILBOX_OPEN_DETECTION_ENABLED` / `MAILBOX_OPEN_THRESHOLD` | `true` / `0.4` | Lid/door change detection / normalised crop difference that means open. |
| `MAILBOX_OPEN_MIN_FRAMES` / `MAILBOX_OPEN_COOLDOWN_SECONDS` | `2` / `300` | Frames an opening must persist with nobody near / one opened (or visit) event per window. |
| `MAILBOX_BOOST_SECONDS` / `MAILBOX_BOOST_INTERVAL_SECONDS` | `60` / `1.0` | Faster stream sampling while someone is at the mailbox (stream cameras only; `0` disables). |
| `BIN_DETECTION_ENABLED` | `true` | Bin detection (only acts on `bin` zones). |
| `BIN_CHECK_INTERVAL_SECONDS` / `BIN_CHANGE_CONFIRM_CHECKS` | `20` / `3` | Region check cadence / checks a change must persist. |
| `BIN_REMOVAL_COUNTS_AS_EMPTIED` | `false` | Explicit rule: a bin removed right after a collection vehicle counts as emptied. |
| `BIN_LOCAL_LABELS` | empty | Detector labels that mean "bin" (none in COCO). |
| `SCENE_VERIFIER_ENABLED` / `SCENE_VERIFIER_MIN_INTERVAL_SECONDS` | `true` / `120` | Closed Foundry questions for mailbox/bin candidates (needs Foundry), rate limit per zone. |
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
  before a parked car is recognised again. (Vehicle scene tracks, mailbox and
  bin states are persisted; only frame continuity is in memory, so a restart
  counts as an outage. In-flight Foundry checks are also memory-only: a
  restart during one drops that candidate.)
- **Mailbox contents depend on Foundry in practice.** RT-DETR (COCO)
  has no envelope or wheelie-bin class, so without Foundry mail deliveries and
  retrievals are only recognised from a locally detected package; otherwise a
  visit is reported as `mailbox_opened` (lid change) or `mailbox_visit`, and
  bins stay `unknown`. Verification runs only on candidate sequences, never
  per frame. Opening detection is local and needs no Foundry.
- **Vehicle identity is colour-based only.** "Returned" means a compatible
  colour signature in the same place, not a recognised vehicle; greyscale
  (night IR) crops compare brightness only. There is no make/model or plate
  recognition.
