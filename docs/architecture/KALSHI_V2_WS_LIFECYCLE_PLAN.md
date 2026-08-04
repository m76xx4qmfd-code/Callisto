# Kalshi V2 WebSocket Connection Lifecycle Plan

**Branch:** `callisto/kalshi-ws-lifecycle`
**Date:** 2026-08-04

## Objective

Add a pure, disconnected, connection-scoped Kalshi V2 WebSocket lifecycle boundary. It creates fresh authenticated connection instructions, strict subscription commands and response models, binds one orderbook to the current connection epoch, retracts publication synchronously on disconnect or continuity failure, and reports private-stream coverage as degraded until a later authoritative portfolio-wide REST sweep exists.

The increment opens no socket, loads no credential, calls no authenticated endpoint, and cannot submit, cancel, amend, decrease, or retry an order.

## Verified protocol

Kalshi's official `asyncapi.yaml` was fetched on 2026-08-04 with SHA-256 `12a4d282d04541295a5cf85f1bc10bf7f1016d8c3598b5f97fd2ee2ca9e490bc`.

- Production handshake: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`.
- The authenticated handshake signs `GET /trade-api/ws/v2`.
- `subscribe` requires a positive command `id`, channels, and optional market filters.
- `subscribed` includes the echoed command `id`, channel, and positive `sid`.
- `orderbook_delta` sends a snapshot followed by sequenced deltas.
- `user_orders` and `fill` are unsequenced private channels.
- Channel errors 10, 17, and 25 are terminal and require resubscription.
- Server ping frames are WebSocket control frames; transport-level pong handling is deliberately deferred with the socket transport.

## Committee disposition

Opus and Kimi required changes; GLM returned an empty response. The design incorporates the actionable findings:

- acknowledgements correlate only by the echoed command ID and expected channel; ticker/UUID are asserted from the pending command and then proven by the first strict snapshot;
- command IDs are globally monotonic across epochs, so delayed old acknowledgements cannot alias current commands;
- connection timestamps must strictly advance;
- disconnect and terminal errors synchronously clear the current epoch and retract publication;
- snapshots may arrive before acknowledgement and remain unpublished until the exact orderbook ack arrives;
- private stream health never becomes healthy in this slice: per-intent reconciliation cannot detect venue orders unknown locally, including orders created and filled entirely during a gap;
- restart/cold-start therefore also remains private-degraded rather than falsely claiming continuity;
- this connection-scoped increment emits one global `user_orders` subscription and one global `fill` subscription, plus one market-scoped orderbook subscription;
- retry authorization is a constant false and is not derived from health or reconciliation.

## State contract

1. `begin_connection(timestamp_ms)` requires a strictly increasing positive timestamp, creates a new epoch, generates fresh RSA-PSS handshake headers, and returns exactly three subscribe commands with unique IDs.
2. Beginning a new epoch or disconnecting invalidates all prior state immediately. Old-epoch command IDs, frames, and snapshots cannot make the new epoch publishable.
3. Each acknowledgement must match one pending current-epoch command ID and channel exactly once. Duplicate, unknown, or mismatched acknowledgements terminate the epoch fail-closed.
4. The first matching strict orderbook snapshot binds the acknowledged sid, configured ticker, and immutable market UUID. It may arrive before the ack but is not publishable before acknowledgement.
5. Contiguous deltas update the view. Any gap or identity failure suppresses publication and exposes the existing exact `get_snapshot` recovery command. No queued delta is replayed.
6. A recovery snapshot must match the current epoch binding and reach at least the highest sequence observed across the gap. Every same-identity frame observed while invalid advances that high-watermark; delayed snapshots below it cannot restore publication.
7. Any wrong-sid/ticker/market identity frame or unsolicited conflicting snapshot terminates the epoch and synchronously retracts the prior view.
8. Terminal errors 10, 17, and 25 terminate the epoch. Other command errors also fail the bounded epoch closed because no safe automatic recovery policy is implemented here.
9. `orderbook_publishable` concerns only sequenced public orderbook continuity. `private_stream_healthy` and `connection_healthy` remain false with reason `authoritative_portfolio_reconciliation_not_implemented`.
10. `retry_allowed` is always false.

## TDD and verification

1. Added the focused lifecycle tests and confirmed RED at collection with `ModuleNotFoundError`.
2. Implemented the pure lifecycle and confirmed the initial focused GREEN (`11 passed`).
3. Independent review found stale recovery publication, identity violations leaving a prior book visible, and Ruff failures. Added RED regressions, high-watermark recovery, terminal identity handling, and formatting fixes.
4. A second independent review found that post-gap frames did not advance the recovery watermark and unsolicited same-identity snapshots left publication active. Added RED regressions and fixed both transitions.
5. Final focused lifecycle suite: `13 passed`.
6. Selected lifecycle/orderbook/client/read-model/reconciliation/ledger/safe-runtime suite using an isolated local PostgreSQL base: `97 passed` after the final review fixes.
7. Ruff check and format check, `compileall`, `git diff --check`, added-line danger scan, and independent final blocker review passed.

## Safety boundary and next increment

This increment cannot authenticate by itself, open a socket, publish into application state, call REST, load credentials, mutate venue state, or authorize a retry. Private-stream and overall connection health deliberately remain degraded.

The next increment should add historical Kalshi order/fill GET models and a portfolio-wide authoritative reconciliation sweep with durable checkpoints. Only that exhaustive coverage can close cold-start and unsequenced private-channel gaps; per-intent reconciliation alone must not make private health true.
