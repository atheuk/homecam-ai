# Azure Deployment Plan

> **Status:** Ready for Validation

Generated: 2026-10-01T12:50:00+02:00
Updated: 2026-10-01T12:50:00+02:00

---

## 1. Project Overview

**Goal:** Release HomeCam AI's modern security, mailbox, suspicious-behaviour, vehicle/wildlife recognition, and higher-resolution event-photo updates to the existing Azure development environment.

**Path:** Modify Existing Application

**Release source tree:** `492b4da` (integration branch `atheuk-release-homecam-ai-security-updates`).
PRs #20 and #21 are already on `main`. PRs #22-#25 are integrated on the release branch from merge base `7543e75207da13851be16acf5e967b745f26b526`; the integration PR to `main` must contain only those four PRs.

**Authorization:** The user's explicit "go live" instruction authorizes this release to the existing development target.

---

## 2. Requirements

| Attribute | Value |
|-----------|-------|
| Classification | Development |
| Scale | Small; existing Container Apps and database |
| Budget | Cost-Optimized; no new Azure resources |
| Subscription | Ate Lab (`83288725-b5b5-44ee-bb33-b1b0a38539cd`), current default |
| Location | North Europe (`northeurope`) |
| Resource group | `rg-homecam-ai` |
| Container Apps environment | `cae-homecam-ai-dev-82ac` |

The release updates existing application images and applies additive database migrations. It does not create a production target or provision new resources.

---

## 3. Components Detected

| Component | Type | Technology | Azure resource |
|-----------|------|------------|----------------|
| HomeCam API | API | Python 3.12 / FastAPI | `ca-api-homecam-ai-dev-82ac` |
| Web application | SSR frontend | Next.js | `ca-web-homecam-ai-dev-82ac` |
| Background worker | Worker | Python | `ca-worker-homecam-ai-dev-82ac` |
| Database migration | Job | Alembic | `job-migrate-homecam-ai-dev-82ac` |
| PostgreSQL | Database | Azure Database for PostgreSQL Flexible Server | `psql-homecam-ai-dev-82ac` |
| Image registry | Registry | Azure Container Registry | `crhomecamaidev82ac` |
| Home Assistant edge connector | Edge add-on | Python / Home Assistant | Outside this Azure deployment |

---

## 4. Recipe Selection

**Selected:** Existing Bicep-managed Azure Container Apps deployment with a targeted image rollout.

**Rationale:** The application already runs in the named Container Apps environment. This release changes application code and database schema, not Azure resource topology. Do not redeploy the subscription-wide template or alter unrelated resources.

---

## 5. Architecture and Release Settings

### Existing service mapping

| Component | Existing Azure service | Release action |
|-----------|------------------------|----------------|
| API | Azure Container Apps | Update only container `api`; preserve the `tailscale` sidecar, all environment values, and secret references |
| Web | Azure Container Apps | Update web image; compile with the public API URL below |
| Migrations | Azure Container Apps Job | Run `alembic upgrade head` using the release API image before API rollout |
| PostgreSQL / ACR / Key Vault / Redis / monitoring | Existing resources | No topology or secret changes |

### Public endpoints

- API: `https://ca-api-homecam-ai-dev-82ac.icywave-dfee8ac8.northeurope.azurecontainerapps.io`
- Web: `https://ca-web-homecam-ai-dev-82ac.icywave-dfee8ac8.northeurope.azurecontainerapps.io`

Build the web image with `NEXT_PUBLIC_API_URL` set to the API URL above. Do not pass secrets as build arguments or write them to logs.

### Current rollback baseline (read-only inventory)

| Component | Image | Latest ready revision |
|-----------|-------|-----------------------|
| API | `crhomecamaidev82ac.azurecr.io/api:main-6be4098` | `ca-api-homecam-ai-dev-82ac--0000034` |
| Web | `crhomecamaidev82ac.azurecr.io/web:ui-6be4098` | `ca-web-homecam-ai-dev-82ac--0000014` |

The API currently has `api` and `tailscale` containers. Existing database, Redis, application, Foundry, and Tailscale secret references are present; only secret names were inspected. Keep them unchanged and do not read or print secret values.

### Migrations

Final Alembic head: `0012_ingestion_leases`.

Required chain: `0009_zone_polygon` → `0010_security_essentials` → `0011_modern_ai_security` → `0012_ingestion_leases`. PRs #24 and #25 add no Alembic revisions. Run the existing migration job's `alembic upgrade head` with the release API image and its existing `DATABASE_URL` secret reference before updating the API image. Confirm the job succeeds and the database reports head `0012_ingestion_leases`. The schema changes are additive; do not downgrade or run destructive data operations.

### Runtime configuration

- Preserve the existing `FOUNDRY_VISION_DEPLOYMENT`, Foundry endpoint, API key reference, and AI feature flags.
- The live API has no explicit `MAILBOX_*`, `INGESTION_LEASE_*`, `SUSPICIOUS_*`, `HOME_*`, or bird-threshold environment overrides. The release's safe application defaults therefore apply: mailbox detection and ingestion leases enabled, lease TTL 30 seconds, suspicious scoring enabled, home region `Netherlands, Northern Europe`, time zone `Europe/Amsterdam`, and bird confidence threshold `0.25`.
- API best-photo defaults are `BEST_PHOTO_MIN_CROP_PIXELS=720`, `BEST_PHOTO_JPEG_QUALITY=94`, and `BEST_PHOTO_SNAPSHOT_TIMEOUT_SECONDS=4`. These are provided by the release settings defaults; do not replace existing app environment values.
- `DAHUA_EVIDENCE_SNAPSHOT_TIMEOUT_SECONDS=3` is consumed by the Home Assistant edge add-on, not by an Azure Container App. Do not add it to the API app; the add-on itself is outside this Azure deployment.
- Local Docker is unavailable. Require green integration-PR `docker-validate` CI and rely on that image validation.

### Provisioning limit checklist

| Resource type | Number to deploy | Total after release | Limit/quota | Notes |
|---------------|------------------|---------------------|-------------|-------|
| `Microsoft.App/managedEnvironments` | 0 | 1 existing | 20 | Previously validated in North Europe; no new environment |
| `Microsoft.App/containerApps` | 0 | 5 existing | No quota change | Targeted image-only revisions |
| `Microsoft.App/jobs` | 0 | 1 existing | No quota change | Reuse existing migration job |
| `Microsoft.DBforPostgreSQL/flexibleServers` | 0 | 1 existing | No quota change | Additive schema migrations only |
| `Microsoft.ContainerRegistry/registries` | 0 | 1 existing | No quota change | Reuse existing ACR |

**Status:** No resource creation or quota increase is planned; existing capacity was previously validated.

---

## 6. Execution Checklist

### Preparation
- [x] Analyze the existing deployment and choose the existing Bicep/Container Apps path.
- [x] Confirm the subscription, region, resource group, current app images/revisions, containers, and secret-reference names.
- [x] Confirm the release merge base and scope; exclude changes already on `main` from PRs #20/#21.
- [x] Inspect the complete migration chain and release configuration defaults.
- [x] Record rollback baseline and keep all deployment operations non-destructive.
- [x] Obtain explicit user authorization to go live.
- [x] Update this plan to `Ready for Validation`.

### Required validation before deployment
- [ ] Run `azure-validate` to completion against this release; do not deploy if any required check fails.
- [ ] Validate Bicep build/lint and targeted what-if with no unrelated resource changes.
- [ ] Run application tests/build checks and review static RBAC; confirm integration-PR CI is green.
- [ ] Record actual commands, results, and timestamps in Section 7; only the validation workflow may set status `Validated`.

### All validation checks pass
- [ ] Core validation: Azure CLI/authentication, `az bicep build`, `az deployment sub validate`, and `az deployment sub what-if` using `infra/main.bicep` and release parameters.
- [ ] Bicep lint and Azure Policy assignment review for the target subscription.
- [ ] Container image validation from integration-PR CI; local Docker is unavailable.
- [ ] Static RBAC review of `infra/modules/role-assignments.bicep`.

**Validation constraint:** `infra/main.bicep` requires secure `administratorLoginPassword` and `secretKey` parameters. The committed parameter file supplies neither; the existing Key Vault contains `secret-key` but no PostgreSQL administrator password. The template also defaults `isPlaceholder=true`, which would preview placeholder app images unless overridden. Do not use dummy secret values or proceed with a what-if that does not accurately represent the existing deployment.

### Deployment (only after validation succeeds)
- [ ] Use `azure-deploy` for execution; do not run deployment commands outside that workflow.
- [ ] Build and push API and web images tagged `release-492b4da`; build the web image with the public API URL.
- [ ] Update the migration job to the release API image and run it; verify `alembic upgrade head` succeeds at `0012_ingestion_leases`.
- [ ] Update the API image with `--container-name api` only; preserve the Tailscale sidecar, app settings, ingress, and secret references.
- [ ] Update the web image after the API revision is healthy.
- [ ] Verify the deployed revisions, 100% traffic, public API/web health, API URL in the frontend bundle, live streams, and photo authentication/cache behavior.
- [ ] Update deployment and verification results in this plan.

---

## 7. Validation Proof

> Release-specific validation is pending. Do not mark this plan `Validated` until the `azure-validate` workflow has completed successfully.

| Check | Command run | Result | Timestamp |
|-------|-------------|--------|-----------|
| API tests on resolved integration merge | `python -m pytest apps/api/tests -q` | 652 passed, 3 skipped | 2026-10-01 |
| Ruff on resolved files | `python -m ruff check apps/api/app/api/routes.py apps/api/app/services/ingestion.py` | Pass | 2026-10-01 |
| Bicep and Azure release validation | Pending `azure-validate` | Pending | Pending |
| Integration PR CI, including Docker validation | Pending | Pending | Pending |

**Validated by:** Pending `azure-validate`

---

## 8. Rollback

If the release causes an outage, stop further rollout and use the existing healthy Container Apps revision or restore the previous API/web image tags recorded above. Preserve the Tailscale sidecar and all current secrets. The migrations are additive; leave the database schema in place and do not downgrade it. No rollback is needed if the release is healthy.

---

## 9. Post-Deployment Verification

- Public HTTPS API `/health` and `/ready` respond successfully.
- Database migration head is `0012_ingestion_leases`.
- API and web revisions are healthy and receive expected traffic; API has both `api` and unchanged `tailscale` containers.
- Web responds successfully and its built JavaScript targets the public API FQDN.
- Camera `/live` endpoints, public HLS manifest/sub-manifest, and a live segment work over public HTTPS; do not use the Tailscale sidecar namespace as browser-access proof.
- Crop/full-frame photo routes reject unauthenticated requests and return private, no-store responses; perform an authenticated download if credentials are safely available.
- Check ingestion/log counters without exposing tokens, secret values, or private image data. Real inference results require new real detections and cannot be asserted without them.

---

## 10. Historical Baseline

The prior development deployment was validated and completed on 2026-09-24. It established the existing Container Apps environment, managed identity permissions, Tailscale sidecar, and public HLS relay. Those historical validation results are not substitutes for release-specific `azure-validate` checks.
