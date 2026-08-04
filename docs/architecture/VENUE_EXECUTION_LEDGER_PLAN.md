# Venue-Neutral Execution Ledger Plan

**Branch:** `callisto/venue-order-persistence`
**Date:** 2026-08-04

## Objective

Create the first logged, venue-neutral persistence boundary for eventual execution. An immutable order intent and its initial audit event are committed together before any future venue transmission. Provider acknowledgements and later lifecycle observations are durable, exact, idempotent evidence rather than updates to inherited Polymarket-oriented tables.

This increment is deliberately disconnected from FastAPI routes, workers, credentials, startup, and `KalshiV2Client.create_order`. It performs no HTTP request and enables no write surface.

## Design

### `venue_order_intents`

- One immutable instruction identified by `(venue, client_order_id)`.
- Exact `NUMERIC(38,18)` quantity and limit price; values outside that envelope are rejected rather than rounded.
- Venue, instrument, book side, time-in-force, post-only, strategy/decision/source provenance, and trace identity.
- No lifecycle status, provider ID, fill totals, `updated_at`, or error fields. Lifecycle state is derived from events.

### `venue_execution_events`

- Append-only, per-intent ordered evidence.
- Unique `(intent_id, sequence)` and `(intent_id, dedupe_key)` constraints.
- Stable dedupe keys encode the durable attempt/provider identity, for example:
  - `intent_recorded:v1`
  - `submission_started:<stable-attempt-id>`
  - `submission_unknown:<stable-attempt-id>`
  - `submission_rejected:<stable-attempt-id>`
  - `submission_acknowledged:<provider-order-id>`
  - `fill_observed:<provider-fill-id>`
- Per-intent event allocation locks the intent row, preventing competing writers from assigning the same sequence.
- A committed `submission_started` event without conclusive later evidence is submission-unknown after restart. It must be reconciled by stable client order ID before any retry.

### `venue_provider_acknowledgements`

- Exactly one immutable initial usable acknowledgement per intent.
- Provider order ID is unique within a venue.
- Venue identity is tied to the intent through a composite foreign key.
- Filled and remaining quantities are exact and must sum to the intended quantity.
- Later resting, fill, cancellation, and terminal observations are events; the initial acknowledgement is never mutated into a status snapshot.

All three tables are logged. PostgreSQL triggers reject UPDATE, DELETE, and TRUNCATE so immutability is a database invariant rather than an application convention. The acknowledgement table also has a composite identity foreign key and an insert trigger that independently verifies filled plus remaining quantity equals the intended quantity.

## Transaction and idempotency rules

1. `record_intent` inserts the intent and `intent_recorded` event in one caller-owned transaction.
2. An exact replay returns the canonical intent without a second event.
3. Reusing a client order ID for different immutable facts raises `VenueExecutionConflictError`.
4. `record_event` serializes on the intent, returns an identical existing event for exact replay, and rejects same-key/different-evidence collisions.
5. `record_initial_acknowledgement` atomically inserts the acknowledgement and `submission_acknowledged` event.
6. Acknowledgement replay is idempotent only when every immutable fact matches.
7. JSON financial evidence converts `Decimal` to fixed-point strings and rejects binary floats.
8. Persistence methods never commit independently and never instantiate a venue client.

## Committee review disposition

The Opus/GLM committee required changes before implementation; Kimi returned an empty provider response. The implemented plan incorporates the substantive dissent:

- fixed numeric precision/scale with reject-not-round validation;
- deterministic event dedupe keys;
- per-intent locking for ordered concurrent event writes;
- one immutable initial acknowledgement, with later state represented as events;
- explicit foreign-key indexes and provider uniqueness;
- restart semantics for dangling `submission_started` evidence;
- database-enforced immutability;
- real PostgreSQL integration and migration round-trip tests rather than metadata-only claims.

## TDD record

1. Added PostgreSQL integration tests importing the planned models/service.
2. Confirmed RED at collection because the ledger models did not exist.
3. Added models, service, and migration; six initial persistence tests passed.
4. Added RED coverage for generic execution events, event collisions, DB-level immutability, and numeric-envelope rejection.
5. Independent blocker review found missing database-level acknowledgement identity/quantity enforcement and a TRUNCATE gap in immutability. Added RED raw-SQL regressions, strengthened the composite foreign key, added acknowledgement validation and statement-level truncate triggers, and reached ten passing ledger tests.
6. Final snapshot review found that provider lifecycle events could reference the wrong acknowledged order, reuse one provider event across intents, and reject valid upper-envelope `NUMERIC(38,18)` values. Added RED service/database regressions, venue-bound provider identity constraints and validation, and context-independent decimal-envelope checks.
7. Late review found two remaining ambient Decimal-context dependencies in envelope and acknowledgement-total checks. Added RED low-precision-context regressions and replaced both operations with context-independent or explicitly exact arithmetic.
8. Final database-boundary review proved PostgreSQL accepts numeric `NaN` through a positive-only quantity check. Added a RED direct-SQL regression and matching ORM/migration finite-value constraints; fifteen ledger tests pass.

## Safety boundary and next increment

No execution path consumes this ledger yet. The next increment should build a read-only reconciliation service over the existing Kalshi GET models. It must derive restart state from events, resolve `SUBMIT_UNKNOWN` by stable `client_order_id`, append provider observations idempotently, and never retry an ambiguous submission automatically.
