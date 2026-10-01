# Data retention

A camera system accumulates footage, stills, embeddings and AI analyses
indefinitely unless something deletes them. Before this feature
`GET /api/v1/settings` *claimed* `retention_days: 30` while nothing ever
deleted anything — the reported number is now derived from configuration
and actually enforced.

## Policy

Each category has its own cutoff so the useful metadata can outlive the
bulky media:

| Setting | Default | Covers |
| --- | --- | --- |
| `RETENTION_EVENT_DAYS` | 30 | `events` rows (and their children) |
| `RETENTION_MEDIA_DAYS` | 30 | `event_photos`, `event_evidence` blobs and resolved incident clips |
| `RETENTION_AI_ANALYSIS_DAYS` | 30 | `ai_analyses` rows |
| `RETENTION_EMBEDDING_DAYS` | 30 | face/person embedding vectors |
| `RETENTION_INCIDENT_DAYS` | 90 | **resolved** incidents only |
| `RETENTION_AUDIT_DAYS` | 365 | `audit_log` |

The audit trail is deliberately kept far longer than camera content: it
contains no imagery (see `docs/security.md`), and it is the record you need
*after* an incident's media has aged out.

`GET /api/v1/settings` reports `retention_days` as
`min(RETENTION_EVENT_DAYS, RETENTION_MEDIA_DAYS)` — the shortest window
after which a user can no longer expect to see an event with its image —
plus a full `retention` block with every category and whether enforcement
is enabled.

## What is never deleted

Two protections outrank every cutoff:

1. **Events referenced by an incident.** Any event id appearing in an
   `incidents.event_ids` list is protected, regardless of the incident's
   status. Resolved incidents are purged *first* (by
   `last_seen_at < incident cutoff`); only on a later run do their events
   become purgeable. Evidence attached to an open or unresolved incident
   can therefore never be deleted — the incident must be resolved and then
   itself age out first. (`last_seen_at` is used rather than `resolved_at`
   because it is always populated.)
2. **Explicit holds.** `events.retention_hold` marks an event as "keep"
   and excludes it and all of its children forever. Set it with
   `PUT /api/v1/events/{event_id}/retention-hold` (`{"retention_hold":
   true}`, audit-logged); the flag is returned in the event payload so the
   UI can show it.
3. **Kept incident clips.** A signed-in household member can set
   `PUT /api/v1/security/incidents/{id}/clip/hold` with `{"hold": true}`
   on a ready clip. This protects the clip and its incident from both
   media and incident retention until the hold is cleared. A resolved
   unheld clip ages out after `RETENTION_MEDIA_DAYS`; open/acknowledged
   incident clips remain available while the incident is active. Stored
   clips also count toward the aggregate budget in `docs/incident-clips.md`;
   reaching it skips new clips rather than deleting held evidence.

## The purge job

`app/services/retention.py` is called by `RetentionScheduler` every
`RETENTION_INTERVAL_SECONDS` (default 1h). It is deliberately boring:

- **Batched.** Every category deletes at most `RETENTION_BATCH_SIZE`
  (default 500) rows per statement and at most
  `RETENTION_MAX_BATCHES_PER_RUN` (default 20) batches per run, committing
  as it goes. A first run against a long-neglected database can therefore
  not hold a single enormous transaction, and the report sets
  `truncated: true` so you know more remains for the next tick.
- **Ordered children-first.** Resolved incidents (and their clips) → media → embeddings →
  AI analyses → events (NULL-ing `people.cover_event_id` on the way) →
  audit log. No FK is ever orphaned.
- **Embeddings are cleared, not deleted.** The vector is set to `[]` with
  `embedding_dimensions = 0`, so an old event keeps its person assignment
  and description while the biometric-ish data is gone.
- **Single-run safety.** Running in several replicas is harmless: deletes
  are idempotent and keyed on ids selected inside the same transaction.
- **Holds are re-checked under a row lock.** Candidates are selected in one
  statement and deleted in another, so under PostgreSQL's READ COMMITTED a
  hold set in between would otherwise be missed. Before each delete the
  purge re-reads the candidate events `FOR UPDATE` and drops any that have
  since been held, which also makes a concurrent `PUT /retention-hold` wait
  rather than race.
- **Dry runs count in SQL.** Counts come from `SELECT count(*)`, never from
  materialising ids, so the first dry run against a long-neglected database
  stays cheap no matter how large the backlog is.

## Rolling it out conservatively

`RETENTION_ENABLED` defaults to **false** and `RETENTION_DRY_RUN` defaults
to **true**. The recommended rollout is:

1. Deploy with `RETENTION_ENABLED=true` and `RETENTION_DRY_RUN=true`. Each
   tick logs exactly what it *would* delete (`retention dry-run report`)
   and deletes nothing.
2. Inspect `GET /api/v1/admin/retention` (authenticated), which returns the
   effective policy plus live dry-run counts per category, and confirm the
   numbers match expectations.
3. Only then set `RETENTION_DRY_RUN=false`.

Deletion is irreversible, so the dry-run step is not optional ceremony —
it is the only chance to notice a mis-set cutoff before the data is gone.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/admin/retention` | Effective policy + dry-run counts |
| POST | `/api/v1/admin/retention/purge` | Run a purge now; `{"dry_run": true}` by default (audit-logged) |
| PUT | `/api/v1/events/{id}/retention-hold` | Mark/unmark an event as keep-forever (audit-logged) |
| PUT | `/api/v1/security/incidents/{id}/clip/hold` | Protect/unprotect a ready clip (audit-logged) |

Both admin endpoints require authentication. The project has no role model
yet — any authenticated user is effectively an admin — which is a known
limitation shared with the other `/api/v1/admin/*` routes, not something
introduced here.

## Tests

`apps/api/tests/test_retention.py` covers cutoff selection per category,
dry-run counting without deletion, batching/truncation, incident-protected
events surviving a purge, `retention_hold` surviving a purge, resolved
incidents ageing out before their events, embeddings being cleared rather
than orphaned, audit rows outliving event rows, and the admin endpoints'
auth + audit behaviour.
