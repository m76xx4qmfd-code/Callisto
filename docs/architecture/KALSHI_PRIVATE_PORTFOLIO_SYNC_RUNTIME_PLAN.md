# Kalshi Private Portfolio Sync Runtime Plan

**Branch:** `callisto/kalshi-private-sync-runtime`
**Date:** 2026-08-04

## Milestone context

This is the first of three coherent PRs delivering one vertical read-only authoritative portfolio synchronization milestone:

1. production authenticated private WebSocket runtime and REST invalidation/recovery loop;
2. principal-scoped projection persistence, default-disabled worker, and DB-only API;
3. frontend authoritative account and portfolio surface.

The split is for safety reviewability, not a change in milestone scope. No slice enables a venue write.

## Verified protocol authority

Kalshi's official `https://docs.kalshi.com/asyncapi.yaml` was fetched on 2026-08-04. It identifies AsyncAPI `3.0.0`, API version `2.0.0`, and now hashes:

`2a1449ecdc6dd7cd6498863016fe5ccc62cb64a090149d311733d61175196f78`

The reviewed bytes are vendored at `docs/research/kalshi_asyncapi_20260804.yaml`; tests pin that file's hash. This differs from both the `12a4d...` source fetched earlier the same day and an intermediate `70362c...` response observed during planning. The reviewed schema adds canonical `outcome_side` and `book_side`, millisecond timestamps, richer private payloads, and the authenticated `market_positions` channel.

`user_orders`, `fill`, and `market_positions` carry no sequence number. Callisto therefore never treats their frames as proof of complete delivery. A private frame is only a validated invalidation trigger that marks the last REST evidence dirty.

## Design

1. Add strict immutable parsers for current `user_order`, `fill`, and `market_position` payloads. Fixed-point quantities and dollars remain `Decimal`; UUIDs, timestamp ranges, side equivalence, counts, and subaccounts are validated. Provider user IDs are consumed only for correlation and are never logged.
2. Add a private-only signed lifecycle because the existing lifecycle's state, commands, publication, and recovery are intrinsically bound to one orderbook ticker/UUID. The private lifecycle subscribes only to `user_orders`, `fill`, and `market_positions`, requires exact command/channel/SID acknowledgements, rejects stale epochs, and exposes no order command surface.
3. Add a production `websockets==12` transport factory. It accepts fresh signed connection instructions, uses bounded open/close and ping settings, enforces the configured message-size limit, decodes only UTF-8 JSON objects, and never logs headers or payloads.
4. Add a reconnecting runner with one generation-local reader. After all private subscriptions are acknowledged it performs an immediate full REST synchronization through an injected GET-only synchronizer. Any frame before or during that synchronization sets a dirty flag and forces one follow-up synchronization.
5. In steady state, private frames coalesce behind a debounce and minimum synchronization interval. A frame arriving during an in-flight synchronization sets the dirty flag and requires one later synchronization; it is never discarded as covered.
6. A disconnect, malformed frame, wrong SID, principal mismatch, timeout, or buffer-independent transport failure retracts readiness synchronously and reconnects with bounded exponential backoff. Every reconnect performs another full synchronization. No WebSocket frame can restore readiness.
7. Runtime readiness means only: private socket connected, all three subscriptions acknowledged, a full GET-only synchronization succeeded recently, no later dirty trigger exists, and the principal fingerprint remained constant. It does not claim transactional REST consistency or private-stream completeness. A max-staleness TTL degrades readiness even if the socket remains live.
8. `retry_allowed` is permanently false. The only outbound WebSocket frames are the three subscribe commands. The synchronizer protocol exposes only `principal_fingerprint` and `synchronize`; no venue mutation method is reachable.

## Committee disposition

Opus, Kimi, and GLM all requested changes before implementation. The plan adopts their shared requirements:

- deliver the vertical milestone as three reviewable PRs rather than one monolith;
- state explicitly that REST recency plus socket liveness defines readiness and unsequenced WebSocket frames provide invalidation only;
- treat `market_positions` as unsequenced because the current schema supplies no sequence field;
- replace a complex bootstrap buffer with a dirty flag and mandatory follow-up synchronization;
- debounce/coalesce trigger bursts and suppress overlapping synchronizations;
- use immutable copied projection membership, versioned healthy/degraded attempts, and a principal-scoped PostgreSQL lease in the persistence/worker PR;
- pin the current protocol hash and regression fixtures;
- preserve a permanently false retry authorization and a subscribe-only outbound WebSocket surface.

The existing orderbook lifecycle was inspected before choosing a separate private lifecycle. Its constructor requires a market ticker/UUID, its command set always includes `orderbook_delta`, and its health/publication/recovery state is coupled to sequenced snapshots and deltas. Parameterizing it for zero orderbooks would add conditionals throughout unrelated safety-critical logic. The private lifecycle reuses only the signer and common decoded transport contract; it does not duplicate orderbook state.

## TDD and verification

Tests are written RED-first for:

- strict current-schema private order/fill/position parsing;
- stale epoch, duplicate/wrong command acknowledgement, wrong SID, malformed and unknown frames;
- signed private-only subscription instructions;
- strict text JSON transport and binary/non-object/malformed rejection;
- subscribe-before-bootstrap, dirty-during-bootstrap follow-up, trigger coalescing, staleness, reconnect, principal mismatch, stale generation, and cancellation;
- permanently false retry authorization and subscribe-only outbound frames;
- no credential/header/payload logging.

Before landing: focused and adjacent WebSocket regressions, Ruff, formatting, compilation, secret scan, LICENSE/NOTICE guard, `git diff --check`, exact-snapshot independent review, and current-head GitHub checks.
