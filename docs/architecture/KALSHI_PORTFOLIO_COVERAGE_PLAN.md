# Kalshi Authenticated-Principal Portfolio Coverage Plan

## Scope and authority

This increment is a **disconnected, GET-only, authenticated-principal bounded evidence coverage sweep**. It adds no route, worker, startup hook, credential handling, venue mutation, private-stream health promotion, or retry authorization.

Protocol authority is Kalshi OpenAPI **3.27.0**, fetched **2026-08-04**, SHA-256:

`4cee66247882abf501f12a405b2a7d7569038f7b5c7bf9baf2d10f08aacd5ce8`

The resolved schema says historical `max_ts` selects records before a Unix timestamp. Historical orders and fills accept only `ticker`, `max_ts`, `limit`, and `cursor`. Current calls with omitted subaccount cover all subaccounts. Historical endpoints authenticate the principal/member, but their subaccount breadth is not explicit. Accordingly, this design does **not** describe historical coverage as guaranteed member-wide or use it to restore private-stream health.

## Sweep semantics

For one caller-stable `coverage_id`, timezone-aware `observed_at`, and authenticated-client principal fingerprint:

1. Return an already committed terminal checkpoint before any venue call.
2. Read the historical cutoff.
3. Traverse current orders, current fills, historical orders, and historical fills with `limit=1000` until an empty cursor. There is no page or record cap. A repeated cursor is a protocol failure.
4. Current calls omit subaccount and every optional filter. Historical calls use only `max_ts`, `limit`, and `cursor`; `max_ts` is the mathematical ceiling of the respective order/fill cutoff epoch seconds.
5. Read the cutoff again.
6. Validate and deduplicate observations, then persist observations and one checkpoint atomically under the opaque SHA-256 fingerprint of the approved origin and credential key ID. The raw key ID and provider user ID are not persisted.

Transport, parsing/protocol, divergent-fill, and repeated-cursor failures raise and commit no checkpoint. The same `coverage_id` may therefore retry after those failures. A committed `complete` or `incomplete` checkpoint is terminal and replays; a new ID is required for a new sweep. `retry_allowed` is always false.

## Evidence modeling

Orders deduplicate by `order_id`. Immutable identity fields, including an opaque SHA-256 of the provider `user_id`, must agree; the raw provider user identity is never persisted. Mutable snapshots use the later strictly parsed `last_update_time` (falling back to `created_time`). Equal timestamps plus identical modeled payload are idempotent. Equal timestamps with divergence, or immutable identity divergence, produce a durable `incomplete` result. Selection in that conflict case is deterministic solely so the incomplete evidence can be persisted.

Fills deduplicate by provider `fill_id` and require exact modeled equality. Divergence fails closed with no checkpoint. Every observed fill must link to an observed order. Linked fill ticker, side, and subaccount identity must agree with the order, and aggregate fill quantity cannot exceed the order's immutable initial quantity. An orphan, misbound, or overfilled observation makes the terminal checkpoint incomplete.

Canonical JSON writes every `Decimal` as a fixed string and preserves provider timestamp strings exactly. Order observation identity is `(principal_fingerprint, order_id, evidence_hash)`, allowing content-addressed immutable snapshots without copying every payload into every run. Fill observations and checkpoints are likewise principal-scoped. Checkpoints reference evidence through a deterministic SHA-256 over sorted `(identity, evidence_hash)` pairs.

The venue-neutral intent ledger now supports an optional authenticated-principal fingerprint. A sweep suppresses an observed identity from the unknown lists only when exact principal-bound intent, acknowledgement, immutable order facts, and fill-event ownership agree. Unbound and conflicting evidence remains unknown. See `KALSHI_PRINCIPAL_BOUND_LEDGER_CORRELATION_PLAN.md`.

The checkpoint evidence hash is an **observation identity**, not proof of a reproducible or transactional snapshot.

## Complete versus incomplete

`complete` means only:

- all four traversals reached an empty cursor;
- modeled order/fill evidence was internally consistent;
- every fill linked to an observed order; and
- relevant order/fill cutoff markers were exactly equal before and after traversal.

Cutoff equality is only a **boundary-stability predicate**. It does not make independent REST pages point-in-time consistent. Cutoff drift, order snapshot conflict, or orphan fills produce terminal `incomplete` checkpoints. Unknown venue order IDs, client order IDs, and fill IDs are durably surfaced but do not by themselves change traversal completeness.

Neither status claims transactional current state, historical subaccount completeness, private WebSocket continuity/health, or safety to submit/retry an order.

## Persistence invariants

Three logged tables are added in ORM metadata and Alembic revision `202606160005`:

- canonical immutable order observations;
- immutable fills keyed by provider fill identity; and
- one immutable terminal checkpoint keyed by caller-stable `coverage_id`.

Database checks constrain nonblank identities, lowercase SHA-256 values, positive page counts, nonnegative unique counts, `complete|incomplete` status, nonblank reason, and `retry_allowed=false`. PostgreSQL triggers reject update, delete, and truncate on all three tables. Validated observations and checkpoint commit in one transaction. A concurrent same-ID insert returns only if the committed result is exactly identical; different evidence fails closed.

## Committee dissent and disposition

- **Cross-principal replay or identity collision.** Rejected. Checkpoints, order snapshots, and fills are all keyed by an opaque origin-and-credential fingerprint. Unscoped local ledger rows cannot mark principal activity known.
- **Overclaim: “portfolio/member-wide snapshot.”** Rejected. Name and documentation use authenticated-principal bounded evidence coverage; historical subaccount breadth is not guaranteed by the pinned schema.
- **Overclaim: exact cutoffs imply point-in-time consistency.** Rejected. Equality is boundary stability only; page reads remain independently timed.
- **Finite page cap.** Rejected. Every source traverses to natural terminus with repeated-cursor detection.
- **Use sweep to heal private WS or authorize retry.** Rejected and explicitly out of scope. No WS health or retry behavior changes.
- **Fail all order conflicts without a checkpoint.** Disposed in favor of deterministic evidence plus durable terminal `incomplete`, preserving operator-visible coverage failure. Divergent fill identity remains fail closed with no checkpoint.
