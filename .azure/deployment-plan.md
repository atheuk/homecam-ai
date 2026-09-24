# Azure Deployment Plan

> **Status:** Deployment Blocked

Generated: 2026-09-24T09:14:40+02:00

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
- [ ] Validate tagged Tailscale identity and private edge connectivity
- [ ] Persist Dahua edge runtime configuration only if the local edge token is securely available
- [ ] Update status to `Deployed`

### Deployment Blocker

- The supplied Tailscale auth key was a one-time key and has been consumed.
- The node initially joined as a user-owned node with no `tag:homecam-azure`; explicitly requesting that tag was rejected by the tailnet as invalid/not permitted.
- Container Apps replicas use non-persistent local Tailscale state, so a reusable (preferably reusable + ephemeral) key authorized for `tag:homecam-azure` is required.
- The replacement key staging file requested at `C:\Users\atheukel\tailscale_auth_key.txt` is not available.
- HomeCam admin credentials were also not available through a secure handoff, so the existing authenticated admin API cannot yet persist the edge runtime config. The edge bearer token itself is securely available at the host path and has not been printed.

### Deployed and Verified Portions

- API image `crhomecamaidev82ac.azurecr.io/api:tailnet-ee7eaeb` built successfully.
- API module deployed as revision `ca-api-homecam-ai-dev-82ac--0000005`.
- Public API ingress remains HTTPS-only (`allowInsecure: false`); no edge or Dahua public ingress was added.
- API health returned `{"status":"ok"}`.
- Live RBAC confirmed `AcrPull` on ACR and `Key Vault Secrets User` on Key Vault for the existing user-assigned identity.
- Key Vault secret reference `tailscale-auth-key` is present; the original staging file was securely removed after storage.
- Sidecar image and userspace proxy configuration were deployed and validated, including Container Apps-specific `TS_KUBE_SECRET=""`.
- Private connectivity cannot be claimed until the correctly tagged reusable key is supplied and the sidecar stays authenticated.

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
