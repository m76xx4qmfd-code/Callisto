# Kalshi Authoritative Portfolio Projection Plan

**Branch:** `callisto/kalshi-authoritative-projection`  
**Date:** 2026-08-04

## Milestone context

This is the second coherent PR in the read-only authoritative portfolio synchronization milestone. PR #14 delivered the production authenticated private WebSocket and REST invalidation runtime. This slice connects that runtime to durable projection persistence, an explicit default-disabled worker plane, a DB-only API, and the existing Accounts workspace. The frontend removes legacy live-I/O Kalshi account reads from portfolio presentation and renders exact durable evidence with explicit disabled, never-synchronized, healthy, degraded, and stale states.

No code in this slice enables a Kalshi POST, cancel, amend, decrease, retry, credential mutation, or other venue write. The production client is always constructed with `allow_writes=False`; private WebSocket output remains limited to subscription commands.

## Projection invariants

1. A coverage attempt stores exact order and fill observation membership in the same transaction as its immutable coverage checkpoint. Projection reads select by `(principal_fingerprint, coverage_id)`, never by a global-latest observation query.
2. Balance, positions, settlements, and order/fill coverage are independent GET traversals. The attempt stores each component's observation time and measured skew. A complete attempt requires every traversal and requires skew within the configured correctness freshness bound; it does not claim a venue-wide transactional snapshot.
3. Immutable attempts distinguish `complete`, `incomplete`, and `failed`. The mutable principal head points separately to the latest attempt and last complete projection. A later degraded attempt cannot replace last healthy data.
4. Authoritative empty membership is represented by a complete version with zero membership rows. It is distinct from `never_synchronized`.
5. Every attempt commit verifies the exact lease owner, monotonically increasing fence token, and unexpired lease while holding the lease row lock in the same database transaction. Cancellation alone is never treated as split-brain protection.
6. Unknown provider activity remains principal-scoped and `retry_allowed` remains permanently false.

## Runtime and activation invariants

1. The `kalshi_portfolio_sync` worker is missing-row default disabled. The disabled/paused gate executes before any credential environment read, path resolution/stat, venue client construction, or network action.
2. Enabling is a persistent operator action through the worker control API. `run-once` cannot bypass a disabled master switch.
3. The enabled path reads an external manifest and private-key file, validates an approved Kalshi origin and explicit subaccount, and composes the existing private lifecycle, redacted transport, reconnecting runner, and projection synchronizer.
4. Lease renewal preserves the fence. Disable, pause, cancellation, or lease loss cancels and awaits the private runner before releasing the lease and closing the GET-only client.
5. The `kalshi_portfolio` process plane contains only this worker. It loads no strategies, event bus, feed, market runtime, intent runtime, copy trading, position monitor, or recording service. Desktop and Compose topology start the plane idle; default-off control prevents credential access.

## API truthfulness

`GET /api/kalshi/portfolio/snapshot` reads only PostgreSQL and never constructs a venue client. It returns:

- explicit opaque principal identity, with a conflict when multiple persisted principals exist;
- `disabled`, `never_synchronized`, `healthy`, `degraded`, or `stale` readiness;
- private synchronization runtime health;
- latest-attempt metadata and last-healthy projection identity/as-of time;
- component timestamps/skew and gaps, with account-wide order/fill coverage,
  account-aggregate balance, and explicit positions/settlements subaccount scope;
- exact orders, fills, positions, settlements, and fixed-point balance payloads;
- unknown activity and permanently false retry authorization.

When disabled after prior synchronization, the API preserves the last healthy data while labeling readiness disabled. Before any principal has synchronized, an unscoped request truthfully returns disabled with no principal or account values.

## Committee disposition

The Opus committee review conditionally approved the plan after requiring four blockers to be made explicit. This implementation adopts them as follows:

- exact per-coverage membership rows eliminate global-latest reconstruction;
- per-component timestamps, skew, and a correctness bound prevent non-atomic GET streams from being mislabeled complete;
- fenced transactional commits make lease loss fail closed even if task cancellation races;
- credential resolution is strictly inside the already-enabled path, and disabled-path tests prohibit composition and credential access.

Kimi and GLM returned empty provider responses, so they supplied no additional claims to adopt. Committee advice was checked against the repository source and tested locally.

## Verification targets

- PostgreSQL exact membership, principal isolation, authoritative empty, degraded-head preservation, fixed-point fidelity, lease renewal/release/fencing, and migration round-trip;
- private runner regression tests including bootstrap, gap/reconnect, cancellation, and principal binding;
- disabled worker no-credential/no-network tests, enabled generated-key fake composition, lease-loss cancellation, and run-once rejection;
- DB-only route no-network, ambiguity, runtime health, disabled, degraded, stale, and unknown-activity behavior;
- host, desktop, and Compose topology isolation;
- Ruff/format/compile, secret scan, LICENSE/NOTICE guard, `git diff --check`, and exact-snapshot independent review.
