# Kalshi V2 Historical Portfolio Read Boundary Plan

**Branch:** `callisto/kalshi-historical-read-boundary`
**Date:** 2026-08-04

## Objective

Add strict, immutable, authenticated GET-only models and transport methods for Kalshi's historical cutoff, archived orders, and archived fills. This is the protocol-accurate prerequisite for a later portfolio-wide reconciliation sweep; it does not change reconciliation outcomes or claim complete private-stream coverage.

The increment cannot submit, retry, cancel, amend, or decrease an order. It does not mount a route, start a worker, load credentials, open a socket, alter application startup, or make private WebSocket health publishable.

## Verified protocol

Kalshi's official `openapi.yaml` was fetched on 2026-08-04 with SHA-256 `3893685a723b981beb0de406df8bfce415da5cb519ff548633fdd9b990be4e67`.

- `GET /trade-api/v2/historical/cutoff` returns required RFC3339 `market_settled_ts`, `trades_created_ts`, and `orders_updated_ts`, with optional `market_positions_last_updated_ts`.
- `GET /trade-api/v2/historical/orders` accepts only `ticker`, `max_ts`, `limit`, and `cursor` in the current specification and reuses the strict `Order` response model.
- `GET /trade-api/v2/historical/fills` accepts only `ticker`, `max_ts`, `limit`, and `cursor` in the current specification and reuses the strict `Fill` response model.
- The current historical fills endpoint does not document an `order_id` filter, and neither historical endpoint documents `min_ts`.
- Historical orders require `client_order_id`; historical fills require immutable `fill_id` and `order_id` identities and fixed-point contract counts.
- Current portfolio descriptions explicitly state that orders and fills age out to these historical endpoints at the reported cutoffs.

## Committee disposition

The first Opus/Kimi/GLM committee review rejected a combined per-intent historical reconciliation change because the proposed endpoint filters did not match the resolved current OpenAPI and because archive traversal completeness, overlap identity, and durable coverage semantics were underspecified. Local schema inspection confirmed the criticism was correct and stricter than the initial context: historical reads expose only a maximum timestamp, not a minimum timestamp, and historical fills expose no order filter.

The plan was narrowed to this transport-only prerequisite. A second committee request could not reach any provider because of a DNS failure. No committee claim was treated as fact; the implemented signatures and tests follow the locally fetched official schema.

## State and safety contract

1. Historical cutoff timestamps must be present where required, valid RFC3339 strings with explicit timezones, and are normalized to aware UTC `datetime` values. Explicit `null` and fractional precision beyond Python's exact microsecond representation fail closed instead of being silently altered.
2. Historical order and fill methods expose exactly the documented filters. Unsupported current-portfolio filters are not copied into the historical methods, and query values fail before transport unless they match their documented string or signed-int64 boundary.
3. Existing strict order/fill parsing preserves fixed-point values as `Decimal`; no float conversion is introduced.
4. All three methods use authenticated GET transport only. `allow_writes` is neither required nor changed.
5. Existing current portfolio order wording is corrected so `/portfolio/orders` is not falsely described as a historical source.
6. `KalshiReadReconciliationService`, immutable execution ledgers, retry authorization, WebSocket health, routes, workers, and runtime startup are unchanged.

## TDD and verification

1. Added focused historical read tests and confirmed RED at collection because `KalshiHistoricalCutoff` did not exist.
2. Implemented the minimum parser, GET methods, and package export.
3. The first GREEN attempt exposed a field-specific missing-cutoff diagnostic mismatch; the parser was tightened to identify the missing required wire field.
4. Focused historical suite passed after the fix.
5. Independent exact-snapshot review found three medium fail-closed boundary issues: explicit `null` on an optional non-nullable cutoff, silent sub-microsecond truncation, and loose runtime query types/int64 bounds. RED regressions reproduced all three and the shared read-query boundary was tightened.
6. Final focused historical/client/read-model suite passed after the review fixes.

Final regression, static, security, independent review, commit, and PR evidence are recorded in the pull request.

## Next increment

Design the portfolio-wide authoritative current-plus-historical order/fill sweep and durable coverage checkpoint. Because historical endpoints do not expose lower time bounds or per-order fill filtering, that design must explicitly distinguish complete, incomplete, and failed traversal; reconcile unknown venue activity; and never let per-intent evidence alone restore private-stream health or authorize a submission retry.
