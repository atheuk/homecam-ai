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
