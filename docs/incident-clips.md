# Incident clips

HomeCam captures a short H.264/fMP4 video around the **first event of a new
incident** from the same private HLS relay the camera's ingestion leader
already reads. No Azure component opens a LAN RTSP connection or receives
the camera's credentials; MediaMTX on the Dahua edge host or go2rtc for Eufy
continues to terminate camera streams locally. No additional RTSP session
or unbounded continuous recording is added.

When the API has a fresh compatible fMP4 segment, it snapshots up to 8
seconds of pre-roll at detection time, waits for 8 seconds of post-roll,
and stores at most 16 MB for that incident in the shared PostgreSQL
database. Each reader's rolling segments consume at most 8 MB, expire
after the pre-roll window, and are discarded when the ingestion leader
releases the camera. At most two simultaneous candidate captures run per
camera; excess incidents degrade to unavailable rather than accumulating
in-memory video. The reader checks for playlist gaps and init changes;
an outage, stale feed, unsupported MPEG-TS/RTSP stream, oversized clip or
restart during capture yields `clip.status: "unavailable"` instead of a
misleading partial video. New clips start only for newly opened incidents;
additional events merged into an incident do not multiply video storage.
Older incidents have no clip. Camera-offline incidents cannot have a clip.
Ordinary events now get their own short clips too (see **Event clips**
below), so a clip is no longer only available for the first event of an
armed incident.

**Aggregate capacity guard:** At most 10 clips are admitted per UTC day
across all cameras, and stored clip payloads (including held clips) may
occupy at most 4,000,000,000 bytes in total. PostgreSQL serializes admissions
across API replicas with a transaction-scoped advisory lock; the count and
the sum of the stored `size_bytes` values are checked immediately before
each insert. This bounds *clip payloads*, not the entire PostgreSQL database
(indices, WAL, backups, other media and metadata need separate headroom).
For a 32 GB Flexible Server with roughly 5 GB already used, the default
clip budget is under 4 GB plus storage overhead. Ten 16 MB clips per day
for 30 days consume up to 4.8 GB without the cap; the hard aggregate guard
prevents reaching that amount. With retention disabled or held evidence
accumulating, capacity fills and subsequent incident clips are explicitly
`skipped`; nothing evicts a kept clip. Operators should monitor database
storage and consider increasing capacity before changing the limit.
Resolved unheld clips purged by retention show `expired`, not `unavailable`.

`GET /api/v1/security/incidents/{id}/clip` requires a household session,
returns `video/mp4` with `private, no-store`, and never exposes a private
HLS URL or camera secret. Add `?download=true` for an attachment response.
The UI fetches on demand with the user's session and uses a temporary blob
URL for playback/download rather than appending a bearer token to a URL.
Incident JSON includes `{clip:{status,url}, clip_hold}`. A signed-in human
can set `PUT /api/v1/security/incidents/{id}/clip/hold` with
`{"hold":true}` to protect a ready clip; clearing the hold restores normal
retention. There is no public blob container or pre-signed link.
The hold request locks the incident before verifying that the clip still
exists, so a concurrent purge either sees the hold and preserves the clip
or finishes first and causes the hold request to return HTTP 409. A missing
clip can never be reported as successfully kept.

The existing zone editor also offers **Alert for activity in this zone**.
It defaults on; switching it off suppresses *incident creation* for events
labelled with that zone, without discarding events, changing detections or
muting unzoned camera-health incidents. It can be re-enabled without
redrawing the zone.

Configuration (API environment; all values capped by schema):

| Variable | Default | Meaning |
| --- | --- | --- |
| `INCIDENT_CLIPS_ENABLED` | `true` | Enable HLS segment buffering and capture |
| `INCIDENT_CLIP_PRE_SECONDS` | `8` | Maximum lookback before detection |
| `INCIDENT_CLIP_POST_SECONDS` | `8` | Capture after detection |
| `INCIDENT_CLIP_POST_GRACE_SECONDS` | `12` | Extra bounded wait for the HLS segment covering the post-roll deadline (segments publish only once complete; the reader can lag under load). Without it every real 8 s post-roll failed as "insufficient post-roll". |
| `INCIDENT_CLIP_BUFFER_BYTES` | `8000000` | Per-reader rolling segment cap |
| `INCIDENT_CLIP_MAX_BYTES` | `16000000` | Per-incident stored clip cap |
| `INCIDENT_CLIP_DAILY_LIMIT` | `10` | New clips admitted per UTC day, across replicas |
| `INCIDENT_CLIP_STORAGE_LIMIT_BYTES` | `4000000000` | Aggregate stored clip payload cap, including holds |

Validate that the private HLS relay serves fMP4 (`EXT-X-MAP` and `moof`
fragments), that the API Tailscale sidecar can reach the relay, and that
PostgreSQL has room for the expected incident count before enabling this
in development. Retention removes resolved unheld clips after
`RETENTION_MEDIA_DAYS`; a held clip protects its incident even after
`RETENTION_INCIDENT_DAYS`. Keep retention dry-run enabled for the initial
rollout and inspect the media counts before allowing deletion.

## Event clips (all cameras, including the Eufy doorbell)

Root cause of "I can't watch any clips": incident clips were only captured
for the *first event of a newly opened incident* while armed, so normal
person/animal/doorbell events never had video; the Dahua HLS reader could
also splice across a playlist gap; and the Eufy battery doorbell had no clip
path at all. Migration `0016_event_clips` adds an `event_clips` table and
each event's JSON now carries
`clip:{status,url,source,duration_seconds,pre_roll_seconds,width,height,codec,size_bytes,reason}`.

* **Streamed cameras (Dahua via MediaMTX):** the same per-camera fMP4 reader
  as incident clips supplies up to 8 s pre-roll and 8 s post-roll. The reader
  is now media-sequence aware and refuses to splice over a gap. Overlapping
  events on one camera share one capture rather than storing duplicate video.
* **Eufy T8210 battery doorbell:** it does not stream while asleep, so there
  is **no pre-roll** and none is invented. The edge add-on (1.0.2) records a
  bounded post-roll (default 15 s) from its private go2rtc when the doorbell
  rings or detects motion/a person, reusing the existing eufy-security-ws
  connection and stopping the stream afterwards (120 s cooldown, 24
  recordings/day, in-memory only with a short TTL). The API polls the edge
  for up to `EVENT_CLIP_EDGE_WAIT_SECONDS`, matches a complete clip that
  started near the event and stores it; the card says *"starts when the
  camera woke (no earlier footage)"* and shows the real duration.
* Every stored clip is validated as MP4 (`ftyp`/`moov`, H.264 `avc1`),
  with its true duration and resolution recorded from the file.

Statuses are distinct in the API and UI: `pending` (recording),
`ready` (playable), `skipped` (daily or storage budget reached),
`unavailable` + reason (e.g. no buffered video, capture interrupted),
`expired` (removed by retention) and `unsupported` (the camera cannot
provide clips). `none` means the event type is not clipped.

`GET /api/v1/events/{id}/clip` (`?download=true` for an attachment) has the
same household-auth, `video/mp4`, `private, no-store` contract as incident
clips; the Events and People cards fetch it on demand as an authenticated
blob. The Events tab also has a **With video clip** filter and a
**doorbell** type filter.

Budget: event clips are admitted under the same PostgreSQL advisory lock as
incident clips. Each admission checks the event sub-cap (40/day,
1,000,000,000 bytes) **and** the combined incident+event 4 GB cap, so event
clips can never push held incident evidence out or exceed the existing
aggregate. Event clips expire after `EVENT_CLIP_RETENTION_DAYS`; held
incident clips are never evicted.

| Variable | Default | Meaning |
| --- | --- | --- |
| `EVENT_CLIPS_ENABLED` | `true` | Capture clips for ordinary events |
| `EVENT_CLIP_TYPES` | `person,animal,vehicle,doorbell,package,suspicious_activity` | Event types that get a clip |
| `EVENT_CLIP_DAILY_LIMIT` | `40` | Event clips per UTC day, across replicas |
| `EVENT_CLIP_STORAGE_LIMIT_BYTES` | `1000000000` | Event clip sub-cap (also counted in the 4 GB combined cap) |
| `EVENT_CLIP_RETENTION_DAYS` | `7` | Event clips are deleted after this |
| `EVENT_CLIP_EDGE_WAIT_SECONDS` | `60` | How long to wait for a camera-side (Eufy) recording |
