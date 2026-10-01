# Incident video clips — Azure development release plan

> **Status:** Code prepared; release validation and deployment blocked pending coordination.

## Scope and authorization

Add short incident clips captured from the existing private HLS streams, retained
in shared storage and served only through authenticated API routes. This is a
code-and-additive-schema release to the existing North Europe development
environment; no new public stream endpoints, credentials, or Azure resources.
The user requested deployment eventually, but the coordinating session explicitly
withheld authorization to merge or deploy until the rest of this release is ready.
**Do not run Azure deployment operations, merge the PR, or reuse the previous
release's authorization.**

## Existing target (verify again before deployment)

- Subscription: Ate Lab (`83288725-b5b5-44ee-bb33-b1b0a38539cd`).
- Resource group: `rg-homecam-ai`; location: `northeurope`.
- Azure Container Apps: `ca-api-homecam-ai-dev-82ac`,
  `ca-web-homecam-ai-dev-82ac`; existing migration job
  `job-migrate-homecam-ai-dev-82ac`.
- Preserve the API's Tailscale sidecar, existing secrets, ingress and private
  access to the edge HLS relays; do not assume Azure can reach LAN RTSP.
- Edge connectors and MediaMTX/go2rtc run outside Azure; coordinate their
  availability independently of the Azure image rollout.

## Architecture and configuration

The existing ingestion lease holder reads private fMP4 HLS from MediaMTX
(Dahua) or go2rtc (Eufy), retains at most 8 MB of segments per camera and
captures at most 16 MB per newly opened incident (8s pre/post by default).
The completed clip is stored in PostgreSQL, accessible on both API replicas
only via an authenticated `private, no-store` endpoint. A signed-in user
can download it or place an audited retention hold; resolved unheld clips
age out after 30 days. Incompatible/offline streams expose an unavailable
status, not a clip. No new Azure resource is planned; check PostgreSQL
capacity and private HLS connectivity before rollout.
Additive migration head: `0015_incident_clips` (after `0014_notifications`).
The same migration adds `camera_zones.alerts_enabled` defaulting true to
preserve existing behavior; the zone editor exposes an optional alert toggle.

## Release checklist

- [x] Implement and test clip capture, retention, authenticated playback, UI,
      configuration, documentation and migration.
- [x] Review security, multi-replica behavior and failure handling locally.
- [ ] Obtain green PR CI, including Linux Docker image builds and migration validation.
- [ ] Open PR; **do not merge** until coordinator authorizes.
- [ ] Obtain coordinator's explicit go-ahead and confirm subscription/location,
      edge connectivity, storage headroom and existing image/revision baseline.
- [ ] Update this plan to `Ready for Validation`; run `azure-validate` and resolve
      any issues before invoking `azure-deploy`.
- [ ] Apply additive migration through existing job, then API and web images;
      preserve sidecar, secrets, and rollback-ready revisions.
- [ ] Verify authenticated clip playback, unavailable fallback, retention,
      application health and rollback path in development.

## Validation and rollback

Local results (2026-10-01): API 786 passed/3 skipped; Dahua edge 33 passed;
Eufy edge 21 passed; web 195 passed, lint/typecheck/production build green;
Python Ruff green; isolated SQLite `alembic upgrade head` reached
`0015_incident_clips`. A targeted review identified two capture/hold races,
both corrected before final regression testing. Linux CI and Docker image
builds are pending PR creation. Do not mark this release `Validated` using
earlier release results; invoke `azure-validate` once deployment is authorized.
The release must preserve the API's `tailscale` sidecar and existing secret
references, run the migration job with the new API image before switching the
API/web image, and confirm both healthy revisions before traffic cutover.
Capture an up-to-date rollback image/revision baseline before deployment.
On failure restore the previous healthy API/web Container Apps revisions; keep
the additive schema intact and do not delete media during rollback.
