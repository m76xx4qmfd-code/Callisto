# Kalshi V2 WebSocket Frame Session Plan

**Branch:** `callisto/kalshi-ws-frame-session`
**Date:** 2026-08-04

## Objective

Add a deterministic decoded-frame session between a future socket transport and the existing pure Kalshi V2 WebSocket lifecycle. One current epoch accepts decoded mappings and returns a closed set of immutable outcomes: no-op, publishable orderbook, one in-session snapshot-recovery request, unconsumed private-frame observation, or terminal failure.

This increment opens no socket, calls no REST endpoint, loads no setting or credential, installs no route/worker/startup hook, and cannot submit, retry, cancel, amend, or decrease an order. The inherited `KalshiWSFeed` remains untouched and unmounted.

## Protocol authority

Kalshi's official `https://docs.kalshi.com/asyncapi.yaml` was fetched again on 2026-08-04 and remained byte-identical to the pinned source used by the prior lifecycle increment:

`12a4d282d04541295a5cf85f1bc10bf7f1016d8c3598b5f97fd2ee2ca9e490bc`

The schema states that `orderbook_delta` sends a snapshot followed by incremental deltas and that `update_subscription/get_snapshot` returns a snapshot without changing the subscription. It identifies private order and fill notifications as `user_order` and `fill` frames. Unknown top-level fields are tolerated because this boundary consumes only modeled required fields and additive provider fields carry no authority; missing or ill-typed modeled fields fail closed.

## State contract

1. `begin` creates one fresh lifecycle epoch and resets all session-local acknowledgement and recovery state.
2. Every receive call carries the transport-owned epoch ID. A delayed frame tagged with an old epoch is a no-op and cannot terminate or publish into the current epoch.
3. Subscription acknowledgements route through the lifecycle's command/channel correlation. The session records acknowledged private-channel SIDs. An early orderbook snapshot remains unpublished until all three requested channels (`orderbook_delta`, `user_orders`, and `fill`) are acknowledged, preserving the lifecycle contract.
4. Strict orderbook snapshots and deltas route through the existing fixed-point parsers and identity/sequence state machine. A publishable outcome contains the exact immutable reconstructed view.
5. The first continuity failure that leaves the epoch recoverable retracts publication synchronously and returns exactly one `get_snapshot` recovery request. Further same-epoch deltas advance the lifecycle high-watermark but return no-op until recovery. No delta is queued or replayed.
6. A same-identity recovery snapshot below the highest sequence observed across the gap is a no-op and remains unpublished. A snapshot exactly at the high-watermark restores publication. An identity conflict terminates the epoch.
7. `user_order` and `fill` frames are envelope-checked against the acknowledged channel SID and surfaced as typed **unconsumed** observations. Their payloads are not parsed, persisted, correlated, or used to promote health in this slice. This prevents legitimate subscribed frames from being mislabeled unknown without treating unsequenced data as authoritative evidence.
8. Malformed current-epoch frames, unknown frame types, wrong private SIDs, command-correlation failures, and orderbook identity violations terminate the epoch and retract publication in the same call.
9. Recovery requests are structurally distinct from reconnect/submission retry authorization. `retry_allowed`, `private_stream_healthy`, and `connection_healthy` remain false.

## Committee disposition

Opus approved with required changes, GLM requested changes, and Kimi returned an empty response. The final design adopts their shared blockers:

- a public pure `terminate_epoch` lifecycle transition supports generic fail-closed routing without private-state access;
- legitimate private frames receive a first-class typed outcome and cannot alter health;
- session outcomes are a discriminated union rather than optional view/command fields;
- one recovery command is emitted per invalid-to-recovery transition and re-armed only by a publishable recovery snapshot;
- stale epoch frames are non-publishing no-ops and do not kill the current epoch;
- the exact restore threshold and all-three-ack publication gate are regression-tested.

## Verification

TDD began with collection failing because `services.venues.kalshi_v2_ws_session` did not exist. A separate public-export test failed before the package export was added. Focused session, lifecycle, and orderbook tests then passed. Independent blocker review found three fail-open/recovery cases: a contiguous semantic delta failure did not initialize the recovery high-watermark, a successfully applied delta could be returned as published before all three acknowledgements, and recovery completed before private acknowledgements did not re-arm a later recovery request. RED regressions reproduced all three; the lifecycle now retains every observed recovery sequence, the session returns a non-publishing outcome until the lifecycle publication gate is true, and every successfully applied recovery snapshot re-arms the next recovery cycle even when it is not yet publishable. Final regression/static/review evidence is recorded in the pull request.

## Next increment

Add a fake-transport-tested async socket coordinator that serializes session-generated commands, binds receive queues to session epochs, disposes old queues on reconnect, and invokes durable portfolio-wide REST coverage after every cold start or disconnect. It must not restore private health unless authoritative coverage is proven, and it must never infer submission retry authorization from orderbook recovery or connection state.
