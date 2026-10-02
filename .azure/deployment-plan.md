# Event clips and richer recognition - Azure development release plan

> **Current release status:** Deployed (2026-10-02) - PR #41 merge `d6ebf37`, follow-ups PR #42 (`d261c49`, API rev 0000043) and PR #43 (`d3fd009`, Eufy edge add-on 1.0.3); web rev 0000022 unchanged.

## Current release scope and authorization

The user explicitly authorized implementing, reviewing, releasing and
verifying working short clips for the Eufy T8210 doorbell and the Dahua
cameras, richer animal recognition and richer *observable* person
descriptions. Release path: independent review, exact-head green PR CI,
pinned squash merge, main CI, then an **additive migration job run**
(`0016_event_clips`, after `0015_incident_clips`), an API image update for
**only** `--container-name api` (preserving the `tailscale:v1.102.4`
sidecar, env, secrets, identity and ingress) and a web image update. The HA
Eufy edge add-on moves to 1.0.2 (new options `event_clips_enabled`,
`event_clip_daily_limit`). No new Azure resources, roles, secrets or
full-stack Bicep deployment.

Recipe: scoped AZCLI image-only updates through existing ACR build and the
existing migration job. Targets: Ate Lab `83288725-b5b5-44ee-bb33-b1b0a38539cd`,
`rg-homecam-ai`, North Europe, `crhomecamaidev82ac`,
`job-migrate-homecam-ai-dev-82ac`, `ca-api-homecam-ai-dev-82ac`,
`ca-web-homecam-ai-dev-82ac`. Build API and web from an archive of the
verified merge commit; web uses
`NEXT_PUBLIC_API_URL=https://ca-api-homecam-ai-dev-82ac.icywave-dfee8ac8.northeurope.azurecontainerapps.io`.
Order: migration job with the new API image → prove head `0016_event_clips` →
API → web. Capture rollback baselines (API `0000041`, web `0000021`)
immediately before update. On failure restore previous revisions; keep the
additive schema and do not delete media.

Capacity: event clips add at most 40 clips/day and a 1 GB sub-cap that is
**also counted** in the existing 4 GB combined clip cap, so database clip
payload growth is unchanged from the approved incident-clip budget.

### Current release: checklist

- [x] Local API/edge/web tests, lint, typecheck, build.
- [x] Independent review, exact-head CI green, pinned squash merge, main CI.
- [x] Scoped `azure-validate` (existing-resource, no-op what-if).
- [x] Migration job → head `0016_event_clips`; API; web; healthy revisions.
- [x] Anonymous `GET /api/v1/events/{id}/clip` → 401.
- [x] Real clip bytes (duration/format) captured from actual Dahua events (rev 0000043, see follow-up evidence).
- [ ] Real doorbell clip from a natural ring/motion event on add-on 1.0.3 (fix deployed, awaiting event).
- [ ] Owner browser playback of a real clip (no owner session available to the agent).

### All validation checks pass

- [x] 1. Core Validation (CLI, auth, build, validate, what-if) — scoped existing-resource template
- [x] 2. Docker Build (if containerized)
- [x] 3. Azure Policy Validation

---

### Current release: Section 7: Validation Proof

| Check | Command | Result | Timestamp |
|---|---|---|---|
| Core validation (CLI, auth, bicep build, validate, what-if) | `validate-deployment.ps1 -Scope group -ResourceGroup rg-homecam-ai -Template <session>/clips-release-validation.bicep -Parameters <empty params>` | PASS; what-if Create 0 / Modify 0 / Delete 0 | 2026-10-02 |
| Docker build | CI `docker-validate` on exact PR head `ab86408` | PASS | 2026-10-02 |
| Azure Policy | `az policy assignment list --scope .../resourceGroups/rg-homecam-ai` | No assignments; nothing to block | 2026-10-02 |

The scoped template only references the existing API, web, migration job and ACR (outputs only). It is not a full-stack validation: full-stack `infra/main.bicep` needs secure parameters, which were deliberately not retrieved.

### Current release: Deployment evidence (2026-10-02)

| Step | Evidence |
|---|---|
| Source | `git archive d6ebf37` (main, CI success) built with `az acr build` |
| API image | `api@sha256:865078ab06ad320096659bd4e07a784773483388d68416cd0629bb8bcc0662cc` (ACR run cg2b) |
| Web image | `web@sha256:99d174d4c566bedc150f111d5a8e9f6eb91eaf28d8b58e784b3e327f117b621e` (ACR run cg2c, `NEXT_PUBLIC_API_URL` = API URL) |
| Migration | Job execution `job-migrate-homecam-ai-dev-82ac-yqk320p` Succeeded; log: `Running upgrade 0015_incident_clips -> 0016_event_clips` |
| API | `--container-name api` only; rev `--0000042` Healthy, 100% traffic; `tailscale/tailscale:v1.102.4` sidecar unchanged; `/health` 200 |
| Auth | Anonymous `GET /api/v1/events/{id}/clip` -> 401 `Not authenticated` |
| Ingestion lease | No `INGESTION_LEASE_ENABLED` override (default on); rev 0000042 logs `ingestion lease ... acquired` for eufy and dahua cameras, so only one replica records each camera |
| Web | rev `--0000022` Healthy, 100% traffic; served bundle contains the new clip/filter UI |
| Eufy edge | HA add-on `d6af6ef6_homecam_eufy_edge` 1.0.1 -> 1.0.2 via `ha apps update`; started, connected to eufy-security-ws (1 device); no secret-like strings in logs |
| Rollback | API `api@sha256:463fe292...` (rev 0000041), web `web@sha256:d464fd03...` (rev 0000021), migration image `api@sha256:24cf886a...`; 0016 is additive (new table only) |

### Current release: Follow-up fixes and real-device evidence (2026-10-02)

**Dahua post-roll (PR #42, merge `d261c49`).** On rev 0000042 four real
Dahua events (14:10-14:34Z) failed with "insufficient post-roll": the capture
ran as soon as the event closed, before the post-roll window had been
recorded. The fix waits up to `INCIDENT_CLIP_POST_GRACE_SECONDS` (default 12)
and refreshes the buffer before deciding. It was deployed by updating the
`api` container only. The result is rev `--0000043`, image
`api@sha256:e34414ea7f32a048ec893f38e8b1c656c915089a52dcb9349ee2b47eff1a3224`,
at 100% traffic with the `tailscale/tailscale:v1.102.4` sidecar unchanged.
The migration job uses the same image. The schema is unchanged (head
`0016_event_clips`). Web stays on `--0000022`.

Real Dahua clips stored on rev 0000043. Source: Log Analytics
`ContainerAppConsoleLogs_CL`, `app.services.event_clips`. The API stores a
clip only after MP4 validation (ftyp/moov, H.264 `avc1`).

| Time (UTC) | Camera | Duration | Pre-roll | Bytes |
|---|---|---|---|---|
| 14:44:03 | dahua-channel-1 | 18.0 s | 8.8 s | 2,312,724 |
| 14:54:29 | dahua-channel-1 | 16.0 s | 8.4 s | 2,057,950 |
| 15:05:05 | dahua-channel-1 | 16.0 s | 7.7 s | 2,059,010 |

All 3 Dahua photo events on rev 0000043 produced a stored clip. No
post-roll failures have occurred since the fix.

**Doorbell keyframe wait (PR #43, merge `d3fd009`, edge only).** The first
real T8210 event on the new release (`evt-b496ad04886348ff`, 14:54Z) failed.
Edge 1.0.2 logged `go2rtc refused the MP4 recording: HTTP 500`. Root cause:
recording was requested immediately after `start_livestream`, before the
waking doorbell had sent SPS/PPS and an IDR frame, and subscribers that
joined early received mid-GOP data. Fix:

- The livestream gates subscribers until the first keyframe and prefixes it
  with SPS/PPS.
- The recorder waits up to `live_ready_timeout` (15 s). On timeout it stops
  only the stream it started, and only if nobody else is watching.

The HA add-on `d6af6ef6_homecam_eufy_edge` was updated 1.0.2 -> 1.0.3 via
`ha store reload` and `ha apps update`. It started and connected to
eufy-security-ws (1 device known). No Azure change was needed.

**Remaining owner acceptance**

1. Ring the doorbell (or walk past it) once, wait about 1 minute, then open the event in Events and play the clip.
2. Play a Dahua event clip in the browser.

Doorbell events are rare (1 in 3 days), and the agent does not create
synthetic production events. Doorbell clips have no pre-roll by design; the
UI reports the actual duration.

# Previous release: Live view overlay fix

> **Status:** Validated and deployed (web-only, PR #39).

## Live view fix scope and authorization

The user explicitly authorized fixing live HLS autoplay and controls, web
validation, independent review, exact-head green PR CI, pinned squash merge to
main, then a web-only deployment. No API, migration, HA, infrastructure or
secret changes are authorized. The incident-clip release history below is
retained for reference; its validation is not evidence for this release.

Recipe: scoped AZCLI image-only update, using the existing ACR build process.
Targets: Ate Lab subscription `83288725-b5b5-44ee-bb33-b1b0a38539cd`,
`rg-homecam-ai`, North Europe, registry `crhomecamaidev82ac`, and only
`ca-web-homecam-ai-dev-82ac`. Existing Next.js Dockerfile and lockfile are reused;
no resources or additional capacity are needed. Docker is unavailable locally;
exact-head CI Docker validation is required before merge.

Build `apps/web` from an archive of the verified merge commit with
`NEXT_PUBLIC_API_URL=https://ca-api-homecam-ai-dev-82ac.icywave-dfee8ac8.northeurope.azurecontainerapps.io`.
Resolve the resulting ACR digest, capture current rollback revision, and update
only the web container image by digest. Preserve environment, secrets, identity,
scale and ingress. Verify healthy revision/100% traffic, HTTP 200, and the live
JavaScript's API origin. Coordinate with the event-photo release to avoid stale
web rollouts.

### Live view fix: All validation checks pass

- [x] Core scoped validation: CLI/auth, existing-resource Bicep build,
      group validation and no-op what-if for web/ACR only.
- [x] Docker build on PR head `95472e4` CI (Docker unavailable locally).
      Final exact-head CI must be green before merge.
- [x] Azure Policy validation at the target scope.
- [x] Web lint, typecheck, Vitest and Next production build.
- [x] Static role verification: existing ACR-scoped AcrPull for app identity;
      no new roles or data-plane access required.
- [x] Independent code review: no significant findings in live-player changes.

### Live view fix: Section 7: Validation Proof

On 2026-10-02, local web lint, typecheck, 201 Vitest tests and Next production
build passed. An independent code-review agent reported no significant findings.
Initial PR #39 head `95472e45bfa52b3986a04cffdf66a6311294f917` passed
frontend and Docker validation CI (both push and PR runs); final-head CI remains
a merge gate. Incident clips retain their native controls.

The standard `azure-validate` helper ran with `-Scope group -ResourceGroup
rg-homecam-ai -Subscription 83288725-b5b5-44ee-bb33-b1b0a38539cd` against
a temporary existing-resource Bicep in session artifacts declaring only the web
app and ACR. CLI, Ate Lab authentication, Bicep compilation, group validation,
and what-if all passed: create 0, modify 0, delete 0. This is a scoped no-op
target check, not a preview of the image update or full-stack deployment.
No full-stack Bicep deployment is authorized. Policy checks found no assignments
at the resource-group query scope and the subscription SecurityCenterBuiltIn
assignment; this image-only update adds no policy-conflicting resources.
Static RBAC confirms resource-scoped AcrPull in the existing role module.

Baseline: web revision `ca-web-homecam-ai-dev-82ac--0000019`,
image `crhomecamaidev82ac.azurecr.io/web@sha256:c7e6322298799f22f2095e36a625cf6a6d534584017011a57e7d176dfca948db`,
Single revision mode, latest revision 100% traffic, HTTPS ingress port 3000.
Recheck this baseline immediately before updating to catch concurrent releases.

---

# Previous release: Incident video clips

## Event-photo image-only release (2026-10-02)

**Status: Validated for the scoped image-only rollout.** User authorized exact-head
CI, independent review, squash merge to main and digest-pinned deployment.
The historical incident-clip release record below remains unchanged.

Deploy only the existing API `api` container and existing web app from an
archive of this release's merge commit. No schema change or migration job;
no Bicep resource deployment. Preserve Tailscale, environment/secret
references, identity and ingress. Coordinate web rollout with the live-player
fix and rebase on its merged main before release.

### All validation checks pass (this release)

- [x] Core AZCLI validation: CLI/auth/build/validate/what-if using a temporary
  existing-only template for the API, web and ACR (zero resource changes).
- [x] Docker build contexts and CI container builds; final head must also pass
  CI immediately before merge.
- [x] Azure Policy assignments at the target scope; no new resource, SKU,
  network, identity or role configuration is proposed.
- [x] API Ruff/pytest, Eufy provider/edge suites, web lint/typecheck/vitest/build.
- [x] Static RBAC review: existing app identity's ACR Pull and Key Vault
  Secrets User scopes remain unchanged; no new data service permissions.
- [x] Independent code review, findings corrected with regression tests.
- [ ] Final exact-head CI and pinned squash merge (release gate).

### Section 7: Validation Proof (this release)

Local API: 805 passed, 3 skipped; Ruff clean. Eufy provider: 12 passed;
Eufy edge: 62 passed, 1 skipped. Web: 198 passed; lint, typecheck and
production build passed. Azure account read confirmed the requested enabled
subscription. Initial API baseline: revision `0000040`, `api` digest
`sha256:2b79ed2023a38fc8c27bc811272768cf56af7da45e26531cdc725173931f2ffc`,
`tailscale/tailscale:v1.102.4`, latest-revision traffic at 100%.
Scoped validation, exact-head CI and review evidence will be recorded below
before this release is marked Validated.

The AZCLI helper passed CLI/auth/Bicep compilation/group validation and
what-if (Create 0, Modify 0, Delete 0) for the existing-only API/web/ACR
template on 2026-10-02. No template deployment is authorized. Resource-group
policy listing was empty; subscription listing returned the existing
`SecurityCenterBuiltIn` audit initiative. This image-only release changes no
policy-governed resource configuration. Static
`infra/modules/role-assignments.bicep` review confirms resource-scoped ACR Pull
and Key Vault Secrets User for the app identity; PostgreSQL photo rows use
the existing secret-referenced application database connection.
Independent review identified and fixed three real issues: late subject
selection, cached frames starved by remote-acquisition slots, and capture
age incorrectly based on delayed provider event timestamps. Dedicated
regressions were added; updated targeted API tests: 43 passed.

Rebased on PR #39 merge `f68c1e353c9b9c1f1b2e4695a889528e36ad931c`;
the coordinated web deployment completed Healthy/Running at 100% on
revision `0000020`, digest
`sha256:73e83bf47c96b79a2f23896eda7723902f8b1dbdd0cebfdc9c005cb8fc5275ad`.
This is the web rollback baseline. Rebased full API: 808 passed, 3 skipped;
combined web: 204 passed plus lint/types/build. Follow-up review verified
the three corrections and found a wrong-subject selection issue; both
initial and late subject selection now reject subjects outside the event's
target set and save a box-free, unverified fallback instead. Final focused
API/recognition regressions: 68 passed. All six CI jobs, including Docker
and add-on builds, passed on head `0f3ac1b5198ac532b217ef062178e2dd9a6b8bd2`;
the final guard/documentation commit will be gated on its own CI before merge.

> **Status:** Validated and deployed to the existing Azure dev environment
> from PR #34 head `588e2fc`, merge commit `fb20890` (no full-stack Bicep deployment).

## Scope and authorization

Add short incident clips captured from the existing private HLS streams, retained
in shared storage and served only through authenticated API routes. This is a
code-and-additive-schema release to the existing North Europe development
environment; no new public stream endpoints, credentials, or Azure resources.
The coordinating session granted release signoff and explicitly requested deployment
of PR #34 head `588e2fc` on 2026-10-01. Validation must pass before merge or
deployment. Do not substitute another PR head or use an earlier release's validation.

## Existing target

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
PostgreSQL admission allows at most 10 clips per UTC day and
4,000,000,000 stored clip payload bytes total across replicas, including
held evidence. Exhaustion marks new clips `skipped`; purged clips are `expired`.
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
- [x] Obtain green PR CI, including Linux Docker image builds on exact head
      `588e2fc` (backend, frontend, edge and Docker checks).
- [x] Open draft PR and obtain coordinator's explicit release signoff.
- [x] Confirm subscription/location and storage headroom: Ate Lab,
      `rg-homecam-ai`, North Europe, 32 GB allocated and ~5.03 GB used at 18:08Z.
      Private edge connectivity remains unverified; the previous image/revision
      baseline is recorded below. Monitor headroom for clip payloads, indexes,
      WAL and other media.
- [x] Update this plan to `Ready for Validation`.
- [x] Run scoped `azure-validate` and resolve validation blockers before
      invoking `azure-deploy`.
- [x] Apply additive migration through existing job, then API and web images;
      preserve sidecar, secrets, and rollback-ready revisions.
- [x] Verify application health, anonymous authorization, revision traffic
      and post-deploy Log Analytics signals.
- [ ] Verify an actual authenticated clip playback/hold and retention purge
      against a live private HLS source; not proven by anonymous or synthetic checks.

## Validation and rollback

Current local results (2026-10-01): API 787 passed/3 skipped; Dahua edge 33 passed;
Eufy edge 21 passed; web 195 passed, lint/typecheck/production build green;
Python Ruff green; isolated SQLite `alembic upgrade head` reached
`0015_incident_clips`. Aggregate admission and the two-order transactional
clip-hold/purge regression passed. Exact-head PR CI passed, including Linux
Docker image builds. A real private HLS playback check still requires the
development edge environment. The scoped release-specific `azure-validate` workflow marked this plan
`Validated`; that status does not imply full-stack Bicep validation.
The release must preserve the API's `tailscale` sidecar and existing secret
references, run the migration job with the new API image before switching the
API/web image, and confirm both healthy revisions before traffic cutover.
Capture an up-to-date rollback image/revision baseline before deployment.
On failure restore the previous healthy API/web Container Apps revisions; keep
the additive schema intact and do not delete media during rollback.

## All validation checks pass

- [x] Bicep recipe: scoped CLI/auth/build/resource-group validation/what-if
      for the three existing release targets; 0 create/modify/delete.
- [x] Bicep lint (main template, API module, scoped target template).
- [x] Azure Policy assignments reviewed for the target resource group.
- [x] Build verification for the release code (local web build and exact-head CI).
- [x] Static role assignment verification.

## Section 7: Validation Proof

On 2026-10-01, the original full-stack Bicep recipe's
`validate-deployment.ps1 -Scope sub -Location northeurope -Subscription
83288725-b5b5-44ee-bb33-b1b0a38539cd` confirmed Azure CLI installed,
authenticated to Ate Lab and `az bicep build` passed. Subscription validation
did not complete after 180 seconds and was stopped; the committed parameter
file lacks the required secure `administratorLoginPassword` and `secretKey`
parameters. No secret values were supplied or logged. The script never reached
what-if. The full-stack template defaults to placeholder images and would
preview unrelated infrastructure changes; it is **not** the deployment path
for this image-only release, and its validation is not claimed as passed.

The coordinator approved a scoped, read-only equivalent for this release.
Read-only `az resource show` confirmed that the API, web and migration job
exist in `rg-homecam-ai`, North Europe. A temporary Bicep template declared
those three resources as `existing` and output only their IDs; its temporary
parameter file contained only their names. The standard
`validate-deployment.ps1 -Scope group -ResourceGroup rg-homecam-ai
-Subscription 83288725-b5b5-44ee-bb33-b1b0a38539cd -Template
./infra/incident-clips-release-validation.bicep -Parameters
./infra/incident-clips-release-validation.parameters.json` passed Azure CLI,
auth, compilation, group validation and what-if (create 0, modify 0, delete
0). Temporary templates/parameters were removed after the check. This proves
the exact target scope and that validation itself proposes **no changes**;
it does **not** preview the later container image updates or assert the
full-stack Bicep would safely apply. No Bicep deployment is authorized.

`az bicep lint --file` passed for `infra/main.bicep`,
`infra/modules/api.bicep` and the temporary scoped template. Azure Policy
assignments at the target scope include management-group MFA write/delete
enforcement and the subscription Security Center audit initiative; the
scoped no-op preview contains no policy-conflicting changes. Static RBAC:
`infra/modules/role-assignments.bicep` scopes ACR Pull to the existing ACR,
Key Vault Secrets User to the existing vault for the app's user-assigned
identity, and Key Vault Secrets Officer to the deployer. The clip payload
uses the existing application PostgreSQL connection, not Azure Blob RBAC;
there is no new identity, role or Azure resource in this release. Live API
metadata confirmed user-assigned identity, `api` + `tailscale` containers,
seven existing secret names and revision `ca-api-homecam-ai-dev-82ac--0000038`;
web revision was `ca-web-homecam-ai-dev-82ac--0000018`. Image update must
preserve the sidecar and all existing configuration.

`npm --prefix apps/web run build` passed locally (using a non-deployment
placeholder API URL). PR #34 exact head `588e2fc` passed backend, edge,
frontend and Docker validation CI; local API 787 passed/3 skipped, Dahua
33 passed, Eufy 21 passed, web 195 passed. The release web image was
subsequently built from the merge commit with the real `NEXT_PUBLIC_API_URL`.

## Development deployment and verification (2026-10-01)

PR #34 merged exact signed-off head `588e2fc` into `main` at
`fb20890731cb57eee690061c40d10ea9d5ffc022`. A temporary archive of
**that merge commit**, not the local uncommitted deployment plan, supplied
both ACR build contexts. `az acr build` produced API run `cg25`, image digest
`sha256:24cf886aeebe32eb21cdb6e8be5e27c1f9ecda2c97fa826c26b630d333eded3c`;
web run `cg26`, image digest
`sha256:c7e6322298799f22f2095e36a625cf6a6d534584017011a57e7d176dfca948db`.
The web image used build argument
`NEXT_PUBLIC_API_URL=https://ca-api-homecam-ai-dev-82ac.icywave-dfee8ac8.northeurope.azurecontainerapps.io`.
Both ACR builds succeeded.

The existing manual `job-migrate-homecam-ai-dev-82ac` retained its
`alembic upgrade head` command and secret references while its sole
`db-migrate` image was changed to the merged API image digest. Execution
`job-migrate-homecam-ai-dev-82ac-2718bhs` succeeded; its sanitized log
contained the `0015_incident_clips` upgrade marker. A follow-up job execution
`job-migrate-homecam-ai-dev-82ac-akpq28i` also succeeded, but its attempted
`alembic current` override was not reflected in the execution template, so
it is **not** independent database-head proof. Separately, a genuinely
read-only `az containerapp exec --container api --command 'alembic current'`
on live revision `ca-api-homecam-ai-dev-82ac--0000039` exited 0 and
reported **`0015_incident_clips (head)`**. Only the revision marker was
printed; no environment variables, credentials, media or database content
were retrieved.

Only the API container image was updated: previous API revision
`ca-api-homecam-ai-dev-82ac--0000038` used `api:release-69afa76`;
new `ca-api-homecam-ai-dev-82ac--0000039` is healthy, running and receives
100% of traffic. The `tailscale` sidecar configuration, API environment,
seven secret names, ingress and user-assigned identity were preserved.
Only then was web updated: previous
`ca-web-homecam-ai-dev-82ac--0000018` used `web:release-d2f73da`;
new `ca-web-homecam-ai-dev-82ac--0000019` is healthy, running and receives
100% of traffic. Web environment and secrets were preserved. Live role
checks confirmed AcrPull on the ACR and Key Vault Secrets User on the vault
for the existing user-assigned identity.

`GET /health`, `GET /ready` and web root returned HTTP 200.
The live web JavaScript bundle contains the intended public API origin.
Anonymous clip playback, clip download (`?download=true`), clip hold and
zone PUT returned HTTP 401. Log Analytics for API revision `0000039`
observed two camera ingestion leases acquired after rollout; the API
container showed zero clip failures, zero camera ingestion failures and zero
retention failures in that observation interval. The coordinator's
independent log review found no actual API errors; earlier keyword matches
in stream-frame summary counters were benign. Both revisions stabilized
healthy. No actual new
incident clip or authenticated playback was observed, and private HLS
reachability/codec compatibility remain unverified; do not claim playback
success. Continued monitoring after a real camera event and an authenticated
clip test are needed to prove those behaviors. Roll back images to the
recorded previous revisions if live verification exposes a regression;
leave the additive schema and stored evidence intact.
