# Kalshi V2 WebSocket Transport Coordinator Plan

**Branch:** `callisto/kalshi-ws-coordinator`
**Date:** 2026-08-04

## Objective

Add an asynchronous transport coordinator around the existing pure Kalshi V2 WebSocket frame session. The coordinator accepts an already-created abstract transport, invokes authenticated-principal portfolio coverage before each new epoch and after explicit disconnect, serializes subscription and recovery sends, permits only one transport receive at a time, and binds every received mapping to one immutable generation/epoch pair.

This increment adds no production WebSocket connector, route, worker, startup hook, setting, credential loader, or venue mutation. It cannot submit, retry, cancel, amend, decrease, or authorize an order. The inherited `KalshiWSFeed` remains untouched and unmounted.

## Authority and existing boundaries

PR #12 pinned the official Kalshi AsyncAPI fetched on 2026-08-04 at SHA-256 `12a4d282d04541295a5cf85f1bc10bf7f1016d8c3598b5f97fd2ee2ca9e490bc`. This increment introduces no provider frame fields or protocol interpretation. It coordinates the already-tested `KalshiV2WSFrameSession` outcomes and calls the existing `KalshiPortfolioCoverageService.sweep` shape through a narrow protocol.

A coverage result, including `status="complete"`, remains bounded audit evidence. It does not prove a transactional portfolio snapshot, historical subaccount completeness, private-stream continuity, connection health, or retry safety. The coordinator's `private_stream_healthy`, `connection_healthy`, and `retry_allowed` properties are therefore permanently false.

## State and concurrency contract

1. `start` owns its candidate transport immediately. It serializes generation transitions, retracts the old lifecycle before closing the old transport, invokes coverage, starts a fresh frame-session epoch, sends exactly three subscriptions in deterministic order, and publishes the new active generation only after all sends succeed.
2. Coverage failure or start cancellation leaves no new epoch and closes the candidate transport. A later start remains valid.
3. Transition, state, receive, and send locks have separate roles. State mutation is synchronous under a short state lock. No transport close or coverage sweep occurs under that state lock.
4. At most one `transport.receive()` is active per generation. The active receive task is registered under the state lock and cancelled when its generation is detached, so a transport whose `close()` does not wake `receive()` cannot starve the replacement generation. A delayed old receive becomes a stale-generation no-op and cannot route.
5. Session recovery commands are sent only while the bound generation remains active. Reconnect cannot interleave new subscriptions ahead of an in-flight recovery send.
6. Any transport receive failure, unexpected decoded-mapping failure, session terminal result, or recovery-send failure retracts publication, clears the active generation, and closes the transport.
7. Cancelling a receive is fail closed, including while it is queued behind another receive or recovery send. Cancellation propagates only after the active epoch has been retracted and transport close has been initiated.
8. Transport close tasks are retained by the coordinator and shielded from caller cancellation. They execute outside the state lock, avoiding a close-versus-reader lock cycle.
9. Explicit disconnect retracts the epoch and closes the transport before invoking coverage. Coverage cancellation propagates and releases the transition lock; the next cold start performs a new coverage sweep. No unfinished sweep can retain a coordinator lock in the background.
10. Stale generation receives and disconnects cannot affect the current transport.

## Committee disposition

Opus required changes; Kimi and GLM returned empty responses. The final design adopts Opus's substantive concerns:

- generation transitions are serialized separately from short state mutation;
- every received frame and recovery send revalidates the exact active object identity;
- receive does not hold a send or state lock while awaiting transport input;
- candidate transports close when cancellation occurs before transition-lock acquisition;
- disconnect coverage is not shielded as an unbounded lock-holding background task;
- coverage is named `audit_coverage_evidence` and cannot promote health.

Independent adversarial review then found cancellation gaps, concurrent reads, close-under-state-lock deadlock risk, malformed mapping escape, and an overclaim in the cancellation test name. RED regressions reproduced these concerns. The implementation now has a single-reader lock, short state critical sections, closes outside the state lock, fail-closed unexpected mapping handling, candidate ownership cleanup, and audit-only terminology.

## TDD and verification

The first focused run failed during collection because `services.venues.kalshi_v2_ws_coordinator` did not exist. The initial GREEN implementation passed six tests. Independent review drove four additional regressions covering concurrent reads, malformed decoded mappings after publication, cancelled reconnect candidates, and cancellation of a queued receive behind in-flight recovery.

Verification commands and final outcomes are recorded in PR #13.

## Next increment

Add an injected fake-tested production transport factory/runner with explicit reconnect policy and decoded JSON boundary, then integrate private order/fill observations with principal-scoped durable coverage and known-intent reconciliation. Do not promote private or overall health until an authoritative continuity contract exists; current bounded REST coverage remains audit-only and never authorizes submission retry.
