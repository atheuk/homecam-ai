# Google sign-in

HomeCam supports "Continue with Google" **alongside** the existing local
email/password login. Local accounts and their passwords keep working
unchanged. Google sign-in is **disabled** until an operator configures a
Google OAuth client; while it is disabled, `GET /api/v1/auth/google/status`
returns `{"enabled": false}`, the sign-in page shows no Google button, and
`/start` answers `503` without redirecting.

## How it works

- Server-side OpenID Connect **authorization-code flow** with **PKCE (S256)**,
  a single-use `state`, a `nonce`, and an HttpOnly browser-binding cookie
  (`homecam_google_oauth`, path `/api/v1/auth/google`, `SameSite=Lax`,
  `Secure` in production). The client secret, PKCE verifier and tokens never reach
  the browser.
- `state` rows are stored hashed in `oauth_login_states`, expire after
  `GOOGLE_OAUTH_STATE_TTL_SECONDS` (default 600) and are consumed atomically
  (`DELETE … RETURNING`), so a replayed callback fails with `invalid_state`
  even across replicas.
- The ID token is verified with Google's published JWKS: RS256 only,
  signature, `iss` (`https://accounts.google.com`), `aud` = client id, `azp`,
  `exp`/`iat`, and `nonce`. Only `sub` (stable identity) and the email are
  used, and the email only when `email_verified` is true.
- Accounts are matched on Google's `sub`, never on email. Signing in issues a
  fresh HomeCam session (any previous session cookie in that browser is
  revoked first, preventing fixation) using the same `homecam_session`
  HttpOnly cookie as password login.
- Query strings on `/api/v1/auth/google/*` are redacted from the access log,
  and the callback redirects to the web app with only a fixed error code.

## Roles and account rules

| Situation | Result |
| --- | --- |
| Accounts that existed before this release | Kept as `admin` (backfilled by migration `0017_google_auth`). |
| Verified Google email exactly in `GOOGLE_ADMIN_EMAILS` (case-insensitive) | New account created as `admin`, or an existing Google-linked account is promoted. Never demotes anyone, never touches other accounts. |
| Any other verified Google account | New account created with role `pending`; no session is issued (`pending_approval`) until an admin approves it. |
| `email_verified` false/missing | Rejected (`email_not_verified`); nothing is created. |
| Google email matches an existing **local** account | Rejected (`account_exists`). No silent linking: sign in with the password, then use **System → Account → Link Google account**. |
| Account disabled (`disabled_at` set) | Rejected for both Google and password login (`account_disabled` / `403`). |
| Linking | Requires an authenticated session plus a CSRF-protected `POST /api/v1/auth/google/link`, which mints a 2-minute single-use ticket; the callback additionally requires the same user's session cookie. |

`GOOGLE_ADMIN_EMAILS` accepts exact addresses only; domain-only or wildcard
entries are ignored. For this deployment it must contain exactly
`a.heukels@gmail.com`.

Google-created accounts have no password (password login is impossible for
them). Error codes returned to the web app as `?google_error=`:
`invalid_state`, `expired_state`, `access_denied`, `token_exchange_failed`,
`invalid_token`, `email_not_verified`, `account_exists`, `account_conflict`,
`account_disabled`, `pending_approval`, `link_expired`,
`link_requires_session`, `already_linked_other`, `google_account_in_use`.

## Configuration

| Variable | Secret | Notes |
| --- | --- | --- |
| `GOOGLE_OAUTH_CLIENT_ID` | no | OAuth "Web application" client id. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | **yes** | Key Vault `google-oauth-client-secret`, mounted as Container App secret. |
| `GOOGLE_ADMIN_EMAILS` | no | Exact comma-separated allowlist. |
| `GOOGLE_OAUTH_REDIRECT_URI` | no | Optional; defaults to `PUBLIC_API_BASE_URL` + `/api/v1/auth/google/callback`. Must be `https` in production. |
| `WEB_APP_BASE_URL` | no | Optional; defaults to the first `CORS_ORIGINS` entry. |
| `GOOGLE_OAUTH_STATE_TTL_SECONDS` | no | 60–1800, default 600. |

If the configuration is incomplete Google sign-in stays disabled and `/start`
answers `503` naming the missing or invalid variable (never its value);
password login is unaffected.

## Owner setup (Azure dev environment)

Values for the deployed environment:

- Authorized redirect URI:
  `https://ca-api-homecam-ai-dev-82ac.icywave-dfee8ac8.northeurope.azurecontainerapps.io/api/v1/auth/google/callback`
- Authorized JavaScript origin:
  `https://ca-web-homecam-ai-dev-82ac.icywave-dfee8ac8.northeurope.azurecontainerapps.io`

1. In [Google Cloud Console](https://console.cloud.google.com/) create or pick
   a project.
2. **Google Auth Platform → Branding / OAuth consent screen**: user type
   **External**, app name "HomeCam", your support email. Scopes: `openid`,
   `email`, `profile` only (non-sensitive; no verification needed).
3. **Audience**: leave publishing status **Testing** and add
   `a.heukels@gmail.com` (and anyone else who should be able to try) as a
   **test user**. In Testing mode only listed test users can sign in.
4. **Clients → Create client → Web application**, name "HomeCam dev". Add the
   JavaScript origin and redirect URI above exactly (no trailing slash).
5. Copy the client id. Store the client secret straight into Key Vault without
   pasting it into chat, tickets or shell history, for example:

   ```powershell
   $kv = "<key-vault-name>"   # az keyvault list -g rg-homecam-ai --query "[].name" -o tsv
   $secret = Read-Host "Google client secret" -AsSecureString
   $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret))
   az keyvault secret set --vault-name $kv --name google-oauth-client-secret --value $plain --output none
   Remove-Variable plain, secret
   ```

6. Reference the secret from the API Container App (this only adds a secret
   reference; the Tailscale sidecar, ingress and identity are untouched):

   ```powershell
   $rg = "rg-homecam-ai"; $app = "ca-api-homecam-ai-dev-82ac"
   $identity = az containerapp show -g $rg -n $app --query "keys(identity.userAssignedIdentities)[0]" -o tsv
   az containerapp secret set -g $rg -n $app --secrets "google-oauth-client-secret=keyvaultref:https://$kv.vault.azure.net/secrets/google-oauth-client-secret,identityref:$identity" --output none
   az containerapp update -g $rg -n $app --container-name api `
     --set-env-vars "GOOGLE_OAUTH_CLIENT_ID=<client-id>.apps.googleusercontent.com" "GOOGLE_OAUTH_CLIENT_SECRET=secretref:google-oauth-client-secret" "GOOGLE_ADMIN_EMAILS=a.heukels@gmail.com" `
     --output none
   ```

   For Bicep deployments pass `googleOAuthClientId` and `googleAdminEmails`
   instead; the Key Vault secret must exist first, otherwise the revision
   cannot provision.
7. Verify: `GET /api/v1/auth/google/status` returns `{"enabled": true}`, the
   sign-in page shows **Continue with Google**, and signing in as
   `a.heukels@gmail.com` lands on the dashboard as admin.

Rotate the secret by adding a new client secret in Google, updating the Key
Vault secret, restarting the active revision, and then deleting the old
secret in Google.

## Approving or disabling accounts

There is no admin UI for user management yet. An operator can approve a
pending Google account or disable an account directly in the database, e.g.
`UPDATE users SET role = 'admin' WHERE email = '<email>';` or
`UPDATE users SET disabled_at = now() WHERE email = '<email>';`. Disabled or
non-admin users are rejected on every request and their live event stream is
closed at the next recheck.
