# Kalshi V2 WebSocket Orderbook Continuity Plan

**Branch:** `callisto/kalshi-ws-orderbook-continuity`
**Date:** 2026-08-04

## Objective

Add a strict, immutable, disconnected Kalshi V2 orderbook WebSocket boundary. The increment parses current fixed-point snapshot and delta messages, reconstructs one acknowledged orderbook subscription, invalidates the book on any continuity or identity failure, and produces the exact read-only `get_snapshot` recovery command required by a later connector.

This increment performs no network I/O and is not connected to FastAPI, application lifespan, workers, settings, credential loading, demo execution, live execution, or the inherited `KalshiWSFeed`.

## Authoritative protocol snapshot

Implementation was checked against Kalshi's official `https://docs.kalshi.com/asyncapi.yaml`, fetched on 2026-08-04. The document identifies itself as AsyncAPI `3.0.0` with API information version `2.0.0`.

- `orderbook_snapshot` requires positive integer `sid` and `seq`, plus `market_ticker`, UUID `market_id`, and optional `yes_dollars_fp` and `no_dollars_fp` arrays.
- Each snapshot level is exactly `[price_in_dollars, contract_count_fp]`, represented as two JSON strings.
- `orderbook_delta` requires the same envelope and market identity plus string `price_dollars`, string `delta_fp`, and `side` equal to `yes` or `no`.
- The schema's `marketTicker` regex excludes periods, but its official orderbook snapshot and delta examples use `FED-23DEC-T3.00`. Callisto accepts uppercase letters, digits, hyphens, and periods so the documented wire example remains valid while whitespace and other punctuation fail closed.
- The specification describes `seq` as the number used to guarantee complete delivery and snapshot/delta consistency.
- `update_subscription` with one `sid`, `action: get_snapshot`, and target market tickers requests a fresh orderbook snapshot without changing the subscription.

The AsyncAPI schema labels dollar prices as fixed-point strings but does not state their maximum scale in the WebSocket schema. Callisto therefore deliberately applies the current V2 `FixedPointDollars` boundary already used by its strict REST models: finite strings with at most six decimal places and values from zero through one. Contract counts and deltas use the documented two-decimal fixed-point boundary. These are fail-closed Callisto invariants, not claims that the AsyncAPI schema independently guarantees those scales; a future protocol change must update the parser and tests together.

## Continuity contract

`KalshiOrderbookState` models one acknowledged orderbook subscription and one market identity.

1. A valid snapshot atomically replaces all prior levels and establishes `sid`, `seq`, ticker, and UUID.
2. The first delta must have `seq == snapshot.seq + 1`; every later delta must be exactly one greater than the last accepted sequence.
3. Duplicate, regressed, or skipped sequence numbers invalidate the book. They are not silently deduplicated because equality cannot prove that the payload is identical.
4. Matching-`sid` ticker/UUID conflicts and deltas that would create a negative quantity also invalidate the book.
5. While invalid, the state exposes no usable view and accepts no delta until a fresh snapshot is applied.
6. A zero resulting quantity deletes the level; no value is clamped or rounded.
7. The state is bound at construction to the connector-acknowledged `sid`, ticker, and UUID. Delayed or unsolicited snapshots from another subscription or market are rejected without changing the active view or recovery state. A reconnect creates a newly bound state.
8. A valid recovery snapshot must match the binding and advance beyond the last accepted sequence. An exact repeat of the current valid snapshot is idempotent; any other snapshot cannot rewind or replace valid state.
9. Recovery produces a positive, correlatable command ID and the official `update_subscription` / `get_snapshot` shape.

The official schema does not explicitly state whether sequence numbers are global to a socket or scoped to `sid`. Callisto evaluates continuity inside one acknowledged subscription because every sequenced message carries `sid`. Before a connector is merged, this assumption must be validated against official clarification or observed read-only traffic. Until then, the connector must not multiplex this state across multiple `sid` values or multiple market identities. If the assumption is wrong, the fail-closed result is spurious invalidation rather than silently corrupted market state.

Requested recovery snapshots can race already queued deltas. This slice therefore only emits the recovery requirement and refuses further deltas; it does not guess at in-place queue ordering. The connector increment must control resubscription, queued-frame disposal, and publication gates before accepting the recovered snapshot.

## Committee disposition

The Opus and GLM reviewers approved the bounded no-I/O scope with required changes; Kimi returned an empty provider response.

Agreements:

- keep the slice disconnected and do not modify or wire the inherited legacy feed;
- reject numeric JSON values for financial fixed-point fields rather than coercing through float;
- invalidate on forward gaps, regressions, duplicates, identity conflicts, and negative resulting levels;
- refuse post-gap deltas until a new snapshot;
- defer in-place recovery ordering until the connector owns the receive queue.

Dissent and resolution:

- Reviewers identified sequence scope as unverified. The implementation explicitly models one `sid` and one market, documents the assumption and safe failure mode, and makes read-only validation a blocker for the connector slice.
- Reviewers identified precision as inferred rather than declared by the WebSocket schema. The plan now labels the six-decimal price and two-decimal count limits as Callisto fail-closed invariants and regression-tests the exact boundary and rejection behavior.
- Reviewers requested explicit level semantics, stale-`sid`, snapshot boundary, empty side, market UUID mismatch, Decimal purity, negative level, replacement snapshot, and post-invalidation tests. All are included.

## TDD and verification evidence

1. Added `backend/tests/test_kalshi_v2_ws_orderbook.py` importing the planned module.
2. Confirmed RED during collection with `ModuleNotFoundError: services.venues.kalshi_v2_ws`.
3. Implemented the immutable parser and state machine in `backend/services/venues/kalshi_v2_ws.py`.
4. Added a public venue-package export test, confirmed RED with `AttributeError`, then exported the shared boundaries.
5. Focused GREEN before independent review: `29 passed` using the repository backend virtual environment.
6. Independent logic/security review found two medium issues: official dotted market tickers were rejected, and unsolicited snapshots could replace acknowledged state.
7. Added RED regressions using the exact official dotted ticker plus stale/wrong-identity and non-advancing recovery snapshots, then bound state to acknowledged identity and sequence epoch.
8. Final focused GREEN after review fixes: `30 passed`.

Final regression, static, security, independent review, commit, and PR evidence will be recorded in the pull request.

## Safety boundary and next increment

This code cannot authenticate, connect, subscribe, publish prices, persist execution evidence, mutate venue state, or place/cancel/amend/decrease an order.

The next increment should add a disconnected authenticated connector lifecycle with fresh per-reconnect RSA-PSS handshake headers, strict subscription acknowledgement, bounded reconnect, publication gating, and read-only recovery. Private `user_orders` and `fill` channels are unsequenced; every disconnect must therefore trigger stable-attempt REST reconciliation for tracked immutable intents before health can return. An inconclusive REST result must keep the lifecycle degraded and must never authorize submission retry.
