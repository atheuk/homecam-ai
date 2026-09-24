# Azure Deployment Plan

> **Status:** Deployed

Generated: 2026-09-24T09:14:40+02:00
Last updated: 2026-09-24T15:35:00+02:00 (private tailnet connectivity, admin bootstrap, Dahua edge provider persistence, and public HLS relay all deployed and validated)

---

## 1. Project Overview

**Goal:** Add private outbound connectivity from the existing HomeCam AI API Container App to the Home Assistant Dahua edge connector through Tailscale, without exposing the edge connector or Dahua endpoints publicly.

**Path:** Add Components (MODIFY)

---

## 2. Requirements

| Attribute | Value |
|-----------|-------|
| Classification | Development |
| Scale | Small (0-2 API replicas) |
| Budget | Cost-Optimized |
| Subscription | Ate Lab (`83288725-b5b5-44ee-bb33-b1b0a38539cd`) |
| Location | North Europe (`northeurope`) |
| Resource group | `rg-homecam-ai` |
| Authorization | Deployment explicitly approved through the creator chat |

---

## 3. Components Detected

| Component | Type | Technology | Path |
|-----------|------|------------|------|
| API | API service | Python 3.12 / FastAPI / httpx | `apps/api` |
| Web | SSR web app | Next.js | `apps/web` |
| Worker | Background service | Python | `apps/api` |
| Dahua edge connector | Private edge API | Python / FastAPI | `apps/edge`, `homecam-edge` |
| Infrastructure | Azure IaC | Bicep | `infra` |

Existing Azure resources include Container Apps, Container Apps Environment, ACR, PostgreSQL Flexible Server, Key Vault, Redis, Log Analytics, Application Insights, and a user-assigned managed identity.

---

## 4. Recipe Selection

**Selected:** Bicep

**Rationale:** The application is already deployed and managed with modular Bicep. This change updates the existing API Container App revision and adds no new Azure resource type.

---

## 5. Architecture

**Stack:** Existing Azure Container Apps

```text
HomeCam API container
  |
  | HTTP CONNECT via 127.0.0.1:1055
  v
Tailscale userspace sidecar (tag:homecam-azure)
  |
  | encrypted tailnet connection
  v
svc:homecam-edge (HTTPS 8443)
  |
  v
Home Assistant edge connector -> Dahua NVR on private LAN
```

### Service Mapping

| Component | Azure Service | Change |
|-----------|---------------|--------|
| HomeCam API | Existing Azure Container App | Add a Tailscale userspace proxy sidecar and a proxy-only API setting |
| Tailscale auth key | Existing Azure Key Vault | Add one secret; expose only as a Container Apps secret reference |
| Dahua runtime config | Existing PostgreSQL-backed admin API | Persist only if the preconfigured edge token can be securely obtained |

### Security Decisions

- Preserve the existing external HTTPS API ingress configuration; add no ingress for the Tailscale sidecar.
- Keep the edge connector and Dahua endpoints private; do not use Tailscale Funnel or public port forwarding.
- Run Tailscale in userspace mode without `/dev/net/tun`, privileged mode, or `NET_ADMIN`.
- Route only Dahua edge-provider requests through the localhost HTTP proxy; do not set global `HTTP_PROXY` or `HTTPS_PROXY`.
- Store the Tailscale auth key in Key Vault and reference it from the Container App; never write its value to source, parameters, logs, or deployment output.
- Authenticate the Azure node with the tagged `tag:homecam-azure` identity.
- Pin the Tailscale image to `tailscale/tailscale:v1.102.4`.

---

## 6. Provisioning Limit Checklist

No new Azure resource instance is created. The existing API Container App gains one sidecar per replica.

| Resource Type | Number to Deploy | Total After Deployment | Limit/Quota | Notes |
|---------------|------------------|------------------------|-------------|-------|
| `Microsoft.App/managedEnvironments` | 0 | 1 | 20 | Azure quota CLI: `ManagedEnvironmentCount`, North Europe |
| `Microsoft.App/containerApps` | 0 | Existing app updated | Existing resource | No new Container App |
| Container Apps sandbox vCPU | Up to 0.5 vCPU (0.25 x 2 replicas) | Existing workload + 0.5 vCPU maximum | 50 | Azure quota CLI: `SandboxCores`, North Europe |

**Status:** All required capacity is available.

---

## 7. Execution Checklist

### Phase 1: Planning
- [x] Analyze workspace
- [x] Gather requirements
- [x] Confirm subscription and location from explicit creator authorization and existing target
- [x] Prepare resource inventory
- [x] Fetch quotas and validate capacity with the Azure quota CLI
- [x] Scan codebase
- [x] Select Bicep recipe
- [x] Plan architecture
- [x] User approved deployment

### Phase 2: Execution
- [x] Research Azure Container Apps and Tailscale userspace proxy behavior
- [x] Add proxy-aware Dahua edge client behavior
- [x] Add the Tailscale sidecar and Key Vault reference to Bicep
- [x] Update edge connector documentation
- [x] Run targeted tests and Bicep validation
- [x] Update status to `Ready for Validation`

### Phase 3: Validation
- [x] Invoke `azure-validate`
- [x] All validation checks pass
  - [x] Core Bicep validation: CLI, authentication, build, ARM validation, and what-if
  - [x] Bicep lint
  - [x] Azure Policy assignments reviewed
  - [x] Application tests and build verification
  - [x] Static RBAC role verification
- [x] Record validation proof
- [x] Update status to `Validated`

### Phase 4: Deployment
- [x] Commit source and IaC changes
- [x] Invoke `azure-deploy`
- [x] Deploy the API image and Container App revision
- [x] Validate API health and live Azure RBAC
- [x] Validate tagged Tailscale identity and private edge connectivity
- [x] Persist Dahua edge runtime configuration via the admin API
- [x] Add a public HLS relay so the browser never needs private tailnet/localhost addresses
- [x] Deploy the frontend hls.js fix and update status to `Deployed`

### Resolution Summary (superseding the earlier blocker below)

- A user-confirmed reusable + ephemeral Tailscale auth key authorized for `tag:homecam-azure` was supplied via a securely staged, non-committed file, rotated into Key Vault, and confirmed via `tailscale status --json` (`Tags: ["tag:homecam-azure"]`, `BackendState: Running`), surviving container restarts.
- The first HomeCam admin account was bootstrapped via the existing `/api/v1/auth/register` endpoint using user-supplied credentials (never hardcoded/logged), then used to persist the Dahua edge provider config (`dahua_mode=edge`, edge connector base URL over the tailnet, edge bearer token read only into memory from the staged host file).
- All 4 Dahua channels report online via the edge connector; channels 1-2 have real physically-connected cameras (channels 3-4 are confirmed physically disconnected NVR ports, not a bug).
- **Root cause of a later "nothing works in the app" regression**: earlier validation had used `az containerapp exec` into the Tailscale sidecar's own network namespace (which is on the tailnet) to fetch HLS manifests — this is not equivalent to a real browser, which has no tailnet route. The true public `/live` endpoint was returning a private `*.ts.net` URL directly to the browser, and the frontend `<video src=...>` element had no HLS.js (only Safari plays HLS natively that way).
- Fixed with two changes, now both deployed together: (1) a new public HLS relay route (`GET /api/v1/cameras/{id}/hls/{path}`) that streams the manifest/segments through the API's own tailnet-connected path and rewrites `/live`'s `stream_url`/`hls_url` to this public path whenever the upstream is private-only; (2) hls.js added to the frontend Live tab so non-Safari browsers can actually decode the HLS stream.
- All staged credential handoff files (`tailscale_auth_key.txt`, `homecam_admin.json`) were securely deleted from the host after successful use; no secret values were ever printed, logged, or committed.

### Deployed and Verified Portions

- API images built and deployed successively: `crhomecamaidev82ac.azurecr.io/api:tailnet-ee7eaeb` (initial tailnet sidecar) → `api:tailnet-7f1095f` (HLS proxy route) → `api:tailnet-3bacc3b` (final, includes rebased frontend snapshot media-type fix). Final revision: `ca-api-homecam-ai-dev-82ac--0000007`, Healthy/Running, 100% traffic.
- Web image `crhomecamaidev82ac.azurecr.io/web:tailnet-3bacc3b` (hls.js-enabled bundle, built with `NEXT_PUBLIC_API_URL` pointed at the public API FQDN). Final revision: `ca-web-homecam-ai-dev-82ac--0000003`, Healthy/Running, 100% traffic.
- Public API ingress remains HTTPS-only (`allowInsecure: false`); no edge or Dahua public ingress was added at any point — only outbound connectivity from the API's Tailscale sidecar.
- Live RBAC confirmed `AcrPull` on ACR and `Key Vault Secrets User` on Key Vault for the existing user-assigned identity.
- Key Vault secret reference `tailscale-auth-key` holds the final rotated key; no staging file remains on the host.
- Verified purely via true public HTTPS calls (no container-exec/tailnet shortcuts): `GET /api/v1/cameras/dahua-channel-{1,2}/live` return a `stream_url` on the API's own public domain (`https://ca-api-homecam-ai-dev-82ac.../api/v1/cameras/.../hls/index.m3u8`); the proxied master playlist, media sub-playlist, and a live `.mp4` segment (up to ~1.9 MB) for both channels all returned HTTP 200 over plain public internet; CORS on the new route correctly scopes `Access-Control-Allow-Origin` to the web app's own origin; the deployed web JS bundle contains the hls.js code.

---

## 8. Validation Proof

| Check | Command Run | Result | Timestamp |
|-------|-------------|--------|-----------|
| Targeted provider tests | `python -m pytest apps\api\tests\test_dahua_edge_provider.py apps\api\tests\test_admin_provider_configs.py -q` | Pass: 21 tests | 2026-09-24T09:29+02:00 |
| Python lint | `python -m ruff check ...` | Pass | 2026-09-24T09:29+02:00 |
| Full API suite | `python -m pytest apps\api\tests -q` | Pass: 125 passed, 2 skipped | 2026-09-24T09:34+02:00 |
| Bicep build and lint | `az bicep build` and `az bicep lint` | Pass; existing type-definition warnings only | 2026-09-24T09:34+02:00 |
| ARM validation | `az deployment sub validate` with secure values held only in process memory | Pass | 2026-09-24T09:36+02:00 |
| ARM what-if | `az deployment sub what-if` with secure values held only in process memory | Pass: no create/delete; API update included | 2026-09-24T09:37+02:00 |
| Azure Policy | `az policy assignment list` | Pass: one assignment reviewed, no blocking validation result | 2026-09-24T09:36+02:00 |
| Static RBAC | Review `infra/modules/role-assignments.bicep` | Pass: user-assigned identity has resource-scoped Key Vault Secrets User and ACR Pull | 2026-09-24T09:37+02:00 |
| Full API suite (post-rebase) | `python -m pytest apps\api\tests -q` | Pass: 125 passed, 2 skipped | 2026-09-24T15:20+02:00 |
| Public HLS proxy end-to-end (real public HTTPS, no container-exec) | `Invoke-WebRequest` against `https://ca-api-homecam-ai-dev-82ac.icywave-dfee8ac8.northeurope.azurecontainerapps.io/api/v1/cameras/dahua-channel-{1,2}/live`, `.../hls/index.m3u8`, `.../hls/video1_stream.m3u8`, and a live `.mp4` segment | Pass: 200 on every hop, real video bytes (up to 1.9MB) fetched over public internet only | 2026-09-24T15:30+02:00 |
| Web bundle contains hls.js | Fetched deployed `/_next/static/chunks/...js` and grepped for `hls`/`Hls` | Pass: found in `app/page-*.js` and a vendor chunk | 2026-09-24T15:32+02:00 |
| Cross-origin proxy access | `Invoke-WebRequest` with `Origin` header set to the web app's public origin against the new `/hls/index.m3u8` route | Pass: `Access-Control-Allow-Origin` correctly echoes only the web app's own origin | 2026-09-24T15:32+02:00 |

**Validated by:** `azure-validate`

### Role Assignment Verification

- **Status:** Verified
- **Identity:** `id-homecam-ai-dev-82ac`
- **Roles:** Key Vault Secrets User scoped to `kv-homecam-ai-dev-82ac`; AcrPull scoped to `crhomecamaidev82ac`
- **Tailscale impact:** The sidecar reads the new Key Vault-backed Container Apps secret through the existing least-privilege Key Vault role. No new management-plane permission is required.
- **Deployment safety:** Subscription what-if reports provider/API-version normalization on unrelated existing resources. Deployment will target only the API module rather than redeploying the full subscription template.

---

## 9. Files to Change

| File | Purpose | Status |
|------|---------|--------|
| `.azure/deployment-plan.md` | Deployment source of truth | In progress |
| `infra/modules/api.bicep` | Tailscale sidecar, secret reference, and proxy setting | Complete |
| `infra/main.bicep` | Wire the existing Key Vault secret URI to the API module | Complete |
| `apps/api/app/providers/dahua/edge_provider.py` | Apply the proxy only to edge requests | Complete |
| `apps/api/tests/test_dahua_edge_provider.py` | Verify proxy wiring | Complete |
| `docs/edge-connector.md` | Document Azure sidecar deployment and private service URL | Complete |

---

## 10. Rollback

Redeploy the previous API Container App template or revision, removing the sidecar and `TAILSCALE_HTTP_PROXY` setting. The existing API ingress and all other Azure resources remain unchanged. Disable or delete the Tailscale node and rotate/revoke its auth key after rollback.

---

## 11. Next Step

Supply a reusable, preferably ephemeral Tailscale auth key authorized for `tag:homecam-azure`, rotate the Key Vault secret, restart the revision, and complete private endpoint/admin API verification.
