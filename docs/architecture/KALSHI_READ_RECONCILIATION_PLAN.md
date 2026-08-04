# Kalshi Read-Only Reconciliation Plan

**Branch:** `callisto/read-only-reconciliation`
**Date:** 2026-08-04

## Objective

Add the first disconnected reconciliation slice over Callisto's immutable venue execution ledger and current Kalshi authenticated GET models. The service can resolve a persisted ambiguous or dangling submission when current Kalshi order evidence contains the stable `client_order_id`, then atomically persist provider identity, order snapshots, and fills. It never submits, retries, cancels, decreases, amends, or enables a route, worker, credential loader, or startup task.

## Authoritative protocol snapshot

The implementation was checked against Kalshi's official `https://docs.kalshi.com/openapi.yaml`, version `3.27.0`, fetched on 2026-08-04.

- `GET /trade-api/v2/portfolio/orders` has ticker, event, status, timestamp, cursor, and subaccount filters, but no `client_order_id` filter.
- `GET /trade-api/v2/portfolio/fills` supports provider `order_id` and cursor filters.
- Canceled/executed orders and older fills age out of these current endpoints into historical endpoints.
- Consequently, a complete current-endpoint miss is audit evidence but does not prove that submission never reached Kalshi and never makes retry safe.

## Reconciliation contract

`KalshiReadReconciliationService.reconcile_intent` requires a caller-stable `reconciliation_id` and timezone-aware observation timestamp.

1. Read the immutable Kalshi intent and any existing provider acknowledgement.
2. Before opening a persistence transaction, exhaust current order pages for the intent ticker and collect exact `client_order_id` matches.
3. Detect repeated cursors and conflicting duplicate provider records.
4. If no match exists, atomically append `reconciliation_inconclusive` with the stable key `reconciliation_attempt:<id>`. The result has `retry_allowed=False` and explicitly records that historical search was not performed.
5. If one current provider order matches, verify ticker, book side, initial quantity, and side-effective limit price against the immutable intent.
6. Exhaust current fills by provider order ID, validating provider order, ticker, side, cursor progress, and duplicate fill identity.
7. In one transaction, record the first usable provider evidence proving acceptance when no acknowledgement exists, append idempotent fill observations, append a content-addressed canonical order snapshot, and append `reconciliation_matched` using the same attempt key. The provider creation timestamp anchors acknowledgement chronology; the mutable current status remains frozen only as evidence in the payload.
8. A repeated logical attempt returns its already committed canonical result before any venue read. A new venue observation therefore requires a new `reconciliation_id`.

The provider snapshot key hashes every field in Callisto's strict canonical order model except provider `user_id`, which is intentionally excluded as account identity rather than order state. It does not claim to hash Kalshi's unmodeled raw response, a monotonic transition, or globally unique `client_order_id`; changed modeled provider evidence produces a new snapshot event. Fill IDs remain venue-global immutable event identities, and fill evidence includes both canonical ticker aliases.

## Transaction and restart behavior

No database transaction or row lock is held during venue network reads. The found-path persistence writes are all-or-nothing. Every individual record also has a stable idempotency key, and a committed attempt is read back before another venue call, so restart replay returns the canonical outcome without duplicate acknowledgements, fills, snapshots, or attempt outcomes. Conflicting concurrent evidence fails closed through the existing per-intent locks and database constraints.

## Deliberate limitations

- Current-endpoint absence remains inconclusive until historical GET models and a documented retention-aware search are added.
- A current match proves provider evidence for that order, not global uniqueness across aged-out history.
- When no initial acknowledgement exists, the current provider snapshot must satisfy the ledger's exact intended quantity invariant. Terminal cancellation snapshots that no longer expose the unfilled quantity cannot be synthesized into an initial acknowledgement; they require a later provider-identity observation design or historical initial-order evidence.
- Position snapshots, settlements, reconciliation checkpoints, cancellation, amendment, and WebSocket lifecycle remain later increments.
- The service is not connected to FastAPI, application lifespan, workers, settings, credentials, demo writes, or live writes.

## Committee review disposition

Opus and GLM required changes; Kimi returned an empty provider response. The implementation incorporates their substantive findings:

- a caller-stable reconciliation attempt identity distinguishes separate audits while making one logical attempt idempotent;
- matched and inconclusive outcomes use the same dedupe key, preventing contradictory outcomes for one attempt;
- order observations are content-addressed full snapshots rather than status-only pseudo-transitions;
- current matches are evidence, not a claim of global uniqueness;
- absence remains explicitly inconclusive due to the historical cutoff;
- venue reads happen outside the persistence transaction, while found-path writes commit atomically;
- cursor cycles, duplicate provider identities, and immutable-fact conflicts fail closed.
- committed attempt results are returned before fresh venue reads, making response-loss restart replay stable;
- market-order collisions and timestamps beyond PostgreSQL's exact microsecond precision fail closed;
- an aged-out current-order miss rechecks and retains any already-known immutable provider identity while holding the intent lock;
- cumulative fill quantity cannot exceed the immutable order quantity, and non-null order/fill subaccounts must agree.

## TDD record

1. Added PostgreSQL integration tests importing the planned service.
2. Confirmed RED during collection because `services.kalshi_read_reconciliation` did not exist.
3. Implemented current-order and fill pagination, exact validation, atomic ledger writes, stable attempt outcomes, and frozen results.
4. Confirmed the focused suite passes against an isolated real PostgreSQL database.
5. Independent review found unstable response-loss replay, ambiguous acknowledgement chronology, missing order-type validation, timestamp truncation, and loss of known identity on current-endpoint misses. Replays now return committed attempt evidence before network reads, acknowledgement evidence uses provider creation time, market-order collisions and overprecision timestamps fail closed, and misses preserve known provider identity.

## Next increment

Add historical Kalshi order/fill GET models and retention-aware search, then extend the read-only state machine for terminal cancellations and restart checkpoints without weakening the rule that ambiguous submissions are never retried automatically.
