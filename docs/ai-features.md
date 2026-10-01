# AI security features

This document covers the modern consumer-security AI capabilities in HomeCam AI:
natural-language search, loitering detection, package-theft escalation, an
unusual-activity baseline, the daily home digest, smart notification priority,
and confirmation-gated deterrence.

It also states the **responsible-AI limits** the system will not cross. Those
limits are enforced in code and asserted by tests, not just described here.

Related reading: [`ai-pipeline.md`](./ai-pipeline.md) for detection/enrichment,
and [`security.md`](./security.md) for arming modes, incidents and the audit
trail these features build on.

---

## Responsible-AI limits (hard)

These are product guarantees. Each has tests that assert the refusal.

| Limit | How it is enforced |
| --- | --- |
| **No face recognition, no identity claims.** The system never infers *who* someone is. | `app/ai/query_moderation.py` refuses search queries that ask for identity. No scoring input in `app/services/priority.py` is an identity attribute. |
| **No gender, ethnicity, age or other protected-attribute inference.** | Query moderation refuses these categories outright. No model output for them is requested, stored or displayed. |
| **Trust is human-set only.** A person's trust level comes from a household member marking them, never from a model. | Unchanged from the existing people/trust model; none of the features here write trust. |
| **No autonomous deterrence.** A siren, light or voice prompt is only ever executed after an explicit, authenticated human confirmation. | `app/services/deterrence.py` has no detection → execution path. `tests/test_deterrence.py` scans the service tree to prove no other module calls it. |
| **No autonomous emergency dispatch.** The system never contacts police, fire, ambulance or a monitoring centre. | `deterrence.ACTIONS` is a closed tuple of `siren`/`light`/`voice`. Anything else is a `422`. |
| **No licence-plate recognition** in this batch. | Not implemented; vehicle events carry no plate text. |

Search queries that ask for identity are refused with a plain-language message.
Age questions are refused the same way, including "is that a child?",
"kid or adult?" and "is she elderly?". Age descriptors used in passing are
stripped: child/children, kid(s), teen/teenager, adult, senior, elderly,
"N year old", and young/old/older when they describe a person ("old man",
"young woman", "older adult"). The same words describing objects are left
alone ("old shed").
Queries that merely *mention* a protected attribute in passing have that term
stripped and are answered with a notice, so a household is never silently given
results for a question the system did not actually answer.

---

## 1. Natural-language event search

`GET /api/v1/search?q=...&camera_id=&since=&until=&limit=`

Comparable to Ring Smart Video Search, Nest "Ask Home" and the UniFi AI Key's
natural-language search.

The query is moderated first, then embedded with the active AI provider and
cosine-ranked against the per-event embeddings the AI pipeline already stores.
That semantic score is **blended** with a plain keyword match over event
summaries, types, tags and zones, so the feature degrades to keyword search
rather than to nothing when an event was never embedded (or when the mock
provider is active). Camera and time filters are applied in SQL first.

Ranking runs in Python over a bounded candidate window, which keeps one code
path working on both SQLite (tests) and PostgreSQL.

Response:

```jsonc
{
  "query": "package left at the front door",  // possibly stripped
  "refused": false,
  "notice": null,                              // set when terms were stripped
  "blocked_categories": [],
  "results": [ { "id": "...", "score": 0.81, "semantic_score": 0.74, "keyword_score": 0.9, ... } ]
}
```

When `refused` is `true`, `results` is always empty and `notice` explains why.
The dashboard renders that refusal verbatim.

| Setting | Default | Meaning |
| --- | --- | --- |
| `search_enabled` | `true` | Feature flag; `503` when off. |
| `search_embedding_weight` | `0.6` | Share of the blended score from the embedding. |
| `search_min_score` | `0.05` | Results below this are dropped, not padded. |
| `search_candidate_limit` | `500` | Newest events considered before ranking. |
| `search_default_limit` | `20` | Default result count. |

## 2. Loitering detection

A person present continuously in one zone for longer than that zone's dwell
threshold is tagged `loitering`, and the event is routed through the incident
pipeline like any other — so arming mode still decides whether it matters.

Presence is tracked in a `zone_presence` **database** row, not in process
memory, because the API can run as two replicas; updates take a
`pg_advisory_xact_lock` (a no-op on SQLite) so two replicas seeing the same
person cannot double-count or race the dwell clock.

A gap longer than `loitering_gap_seconds` ends the visit and restarts the clock:
"came back twice" is deliberately not "stayed".

Per-zone `dwell_seconds` (added by migration `0011`, editable in the Zone
Editor) overrides the global default. Leave it blank to inherit.

| Setting | Default | Meaning |
| --- | --- | --- |
| `loitering_detection_enabled` | `true` | Feature flag. |
| `zone_default_dwell_seconds` | `60` | Dwell threshold when a zone sets none. |
| `loitering_gap_seconds` | `45` | Gap that ends a visit. |
| `loitering_repeat_seconds` | `300` | Cool-off before the same zone flags again. |

## 3. Package theft alert

The existing scene-state logic already reports a package appearing in and
disappearing from a mailbox/porch zone. When a removal happens while the
household is armed **away** or **night**, it is escalated to a `package_theft`
incident carrying the before/after evidence the scene state already captured.

The before/after JPEG crops are stored in the database as `event_evidence` rows
(not on a replica's ephemeral filesystem). The event's
`metadata.mailbox.before/after` and the incident's `evidence.before/after` each
carry an `image_url` (`/api/v1/events/{event_id}/evidence/{before|after}`)
that returns the image to an authenticated user. The `image_url` metadata is
written in the same transaction as the `event_evidence` rows. If storing the
evidence fails, the event is kept without URLs, so it never advertises evidence
that returns 404.

Both API replicas run ingestion with their own in-process scene caches, so both
can observe the same removal. Before a mailbox event (removal, delivery,
retrieval, opening or visit) is emitted, the replica must win a database
claim on `<transition>:<camera>:<zone>` (for example
`mailbox_retrieval:<camera>:<zone>`) in `scene_dedup_claims`
(`app/services/scene_dedup.py`). The claim is a
conditional `UPDATE` that only succeeds once the previous claim is older than
the transition's window (`mailbox_dedupe_seconds` for deliveries/retrievals,
`mailbox_open_cooldown_seconds` for openings/visits), falling back to a primary-key `INSERT`. Exactly one
replica emits the event, so the grouped incident's `event_count` is not
inflated by duplicates. The claim is flushed but not committed on its own: it
commits in the same transaction as the event row. If the claimant errors or
crashes before the event is persisted, the claim rolls back with it, and the
other replica (blocked on the uncommitted row) wins and emits the removal.

While **home** or **disarmed**, a removal is recorded as an ordinary event: you
collecting your own parcel is not a theft.

Repeat removals on the same camera group into the existing open incident rather
than opening a second one.

### Mailbox opened, delivery, retrieval and visits

A `mailbox` zone reports four outcomes, one event per qualifying visit (see
[ai-pipeline.md](ai-pipeline.md#scene-state-vehicles-mailbox-deliveries-bins)
for the detector): `mailbox_delivery` (item put in), `mailbox_retrieval`
(item taken out), `mailbox_opened` (opened or checked with no item change,
detected from the lid even with nobody in view) and `mailbox_visit` (someone
was at the mailbox, outcome unknown). The Foundry scene verifier answers
`action: deposited|retrieved|opened_only|none` from before/during/after
crops; a deterministic local fallback is used without Foundry. Identity is
never inferred.

Routing through priority and incidents:

| Outcome | Priority | Incident |
| --- | --- | --- |
| Delivery | normal (floor) | never |
| Retrieval (mail, verifier) | normal; high while away/night | `mailbox_retrieval`, severity medium, while away/night |
| Retrieval of a locally seen parcel | high/critical (`package_removed`) | `package_theft` while away/night |
| Opened | normal | never |
| Visit | low | never |

Each visit logs an INFO line (zone, observations, cover, diff score,
outcome) and the periodic ingestion stats line carries mailbox counters, so
"nothing detected" can be diagnosed from production logs.

| Setting | Default | Meaning |
| --- | --- | --- |
| `mailbox_min_observations` | `1` | Near frames needed for a visit without a lid/package change. |
| `mailbox_min_zone_overlap` / `mailbox_proximity_margin` | `0.2` / `0.1` | Person cover over the expanded zone. |
| `mailbox_open_detection_enabled` / `mailbox_open_threshold` | `true` / `0.4` | Lid change detection / difference threshold. |
| `mailbox_open_min_frames` / `mailbox_open_cooldown_seconds` | `2` / `300` | Persistence with nobody near / repeat suppression. |
| `mailbox_boost_seconds` / `mailbox_boost_interval_seconds` | `60` / `1.0` | Faster stream sampling while someone is near (stream cameras only). |

| Setting | Default | Meaning |
| --- | --- | --- |
| `package_theft_detection_enabled` | `true` | Feature flag. |

## 4. Unusual-activity baseline

Per camera, HomeCam learns an hour-of-week activity profile from recent history
(one cheap SQL aggregate over a rolling window) and flags events that land in a
historically quiet slot with an `unusual_activity` tag and a priority bump.

Statistics are computed over slots that have *ever* been active, not over all
168 slots — otherwise the ~140 permanently-empty slots of a quiet camera drag
the mean toward zero and nothing is ever unusual. A camera with an established
routine (at least three active slots) that fires in a slot it has never used
before is unusual by definition; otherwise a z-score threshold applies.

Below `unusual_activity_min_history` events there is no baseline worth
trusting, so nothing is flagged at all.

This is purely a **timing** signal. It never involves identity.

| Setting | Default | Meaning |
| --- | --- | --- |
| `unusual_activity_enabled` | `true` | Feature flag. |
| `unusual_activity_window_days` | `28` | Rolling history window. |
| `unusual_activity_min_history` | `50` | Minimum events before flagging anything. |
| `unusual_activity_z_threshold` | `1.5` | How far below the mean counts as quiet. |
| `unusual_activity_max_samples` | `5000` | Aggregate bound. |

## 5. Daily home digest

`GET /api/v1/digest?date=YYYY-MM-DD&refresh=true`

A day-in-review summary: event and incident counts per camera and per type,
notable items (high/critical priority, loitering, unusual activity, package
removal) and the unusual-activity picture.

A deterministic template summary is **always** produced from the real counts.
The AI provider is then asked to write a nicer paragraph; if it is unavailable
or fails, the template stands. The digest is therefore never wrong and never
blocked on a model.

Digests are cached per date in `daily_digests`, keyed by the date primary key —
so two replicas generating the same day at the same time resolve to a single
row (the loser adopts the winner) rather than duplicating. `refresh=true`
regenerates.

An optional background generator keeps today's and yesterday's digests warm.
It is opt-in so tests and dev runs never spin a timer.

| Setting | Default | Meaning |
| --- | --- | --- |
| `digest_enabled` | `true` | Feature flag; `503` when off. |
| `digest_scheduler_enabled` | `false` | Background generation. |
| `digest_scheduler_interval_seconds` | `3600` | Generator tick. |
| `digest_max_notable_items` | `8` | Cap on highlighted items. |

## 6. Smart notification priority

Every event is scored `critical` / `high` / `normal` / `low` from: event type,
arming mode, the zone it happened in, whether it was flagged loitering or
unusual, whether a package was removed, and detector confidence.

The score is exposed on events as `notification_priority` with a
`priority_reasons` list, so the household can always see *why* something was
ranked the way it was. (The pre-existing `priority` field remains the reporting
source's own level and is unchanged.)

Events below `incident_min_priority` do not open an incident, which keeps the
incident feed actionable. The rule deliberately **fails open**: when the
feature is disabled, or the configured minimum is `low`, nothing is suppressed.

No input to the score is an identity attribute — `tests/test_priority.py`
asserts this by introspecting the scoring signature.

| Setting | Default | Meaning |
| --- | --- | --- |
| `notification_priority_enabled` | `true` | Feature flag. |
| `incident_min_priority` | `normal` | Minimum priority that may open an incident. |

## 7. Deterrence hooks (confirmation-gated)

`POST /api/v1/security/deterrence/request` → `POST .../{id}/confirm`

A `DeterrenceAction` is a **request** to sound a siren, flash a light or play a
voice prompt. Requesting one executes nothing. It is executed only when an
authenticated human explicitly confirms it, and the confirming user is recorded
on the action and in the audit trail (`deterrence.requested`,
`deterrence.executed`).

Unconfirmed requests expire after `deterrence_confirmation_ttl_seconds`. A
confirmed, cancelled or expired request cannot be confirmed again. Provider
capability is checked before execution, and the shipped provider is a mock
no-op.

There is no code path from a detection to an execution, and a test scans the
service tree to keep it that way.

`ACTIONS` is a closed tuple — `siren`, `light`, `voice`. There is no emergency
or dispatch action and requesting one is a `422`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `deterrence_enabled` | `false` | Feature flag; requests refused when off. |
| `deterrence_confirmation_ttl_seconds` | `300` | How long a request stays confirmable. |

---

## Migration

`0011_modern_ai_security` adds `camera_zones.dwell_seconds`,
`incidents.evidence`, and the `zone_presence`, `daily_digests`,
`deterrence_actions`, `scene_dedup_claims` and `event_evidence` tables.

## Authentication

Every endpoint added in this batch requires an authenticated session
(`Authorization: Bearer <token>` or the session cookie): `/api/v1/search`,
`/api/v1/digest`, `/api/v1/events/{id}/evidence/{label}` and all
`/api/v1/security/deterrence/*` routes. They return `401` otherwise. Search
results, digests and evidence expose event summaries, incident IDs and images,
so none of them is public. The dashboard therefore shows the search box and
Digest card inside the signed-in **Security** tab, not on the public Overview.
Notification priority and the unusual-activity baseline have no endpoints of
their own. They are the `notification_priority`/`priority_reasons` fields and
the `unusual_activity` tag on the existing `/api/v1/events` payload.
