# Kalshi V2 Foundation Plan

**Branch:** `callisto/kalshi-v2-foundation`  
**Date:** 2026-08-04

## Objective

Introduce a current, testable Kalshi Predictions API V2 boundary without changing Homerun's existing Polymarket live executor, exposing a live-order route, or treating possession of credentials as authorization to trade.

## Increment 1 — completed in this branch

### Venue-neutral intent

`backend/services/venues/contracts.py`

- Immutable `VenueOrderIntent`.
- Venue, instrument identifier, client order ID, bid/ask side, quantity, limit price, time-in-force, and post-only intent.
- Boundary validation without arbitrary exposure or order-count caps.

### Kalshi V2 wire boundary

`backend/services/venues/kalshi_v2.py`

- RSA-PSS/SHA-256 request signing.
- `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, and `KALSHI-ACCESS-SIGNATURE` headers.
- Query exclusion from the signed path.
- Exact event-order endpoint: `/trade-api/v2/portfolio/events/orders`.
- Credential-bearing requests restricted to documented Kalshi production and demo origins.
- Decimal fixed-point quantities and prices.
- Six-decimal `FixedPointDollars` price support and `exchange_index=-1` auto-routing.
- Exact self-trade-prevention enum and integer-field validation.
- Venue-intent translation.
- Direct V2 acknowledgement parsing.
- Explicit fail-closed write arming.
- No automatic POST retry.
- Ambiguous transport, server-error, malformed-success, or mismatched-acknowledgement outcomes become `KalshiSubmissionUnknown`, requiring reconciliation by `client_order_id`.

### Deliberately not connected

- No FastAPI order-placement route.
- No UI live-order control.
- No worker integration.
- No modification to `live_execution_adapter.py`.
- No migration of legacy stored email/password/Bearer fields yet.
- No live order submission.

## RED/GREEN record

1. Tests failed because `services.venues.kalshi_v2` did not exist.
2. Signing and V2 order boundary implemented.
3. Five tests passed.
4. Venue-neutral intent/translator tests added and failed because the contract/translator did not exist.
5. Contract and translator implemented.
6. Seven tests passed.
7. A signed read-only balance test was added RED-first and failed because the method/model did not exist.
8. The read-only balance model and signed transport were implemented.
9. Eight focused tests passed.
10. Origin-allowlist and explicit-rejection/no-retry tests were added RED-first.
11. The untrusted-origin test failed until credential-bearing requests were restricted to documented Kalshi hosts.
12. Ten focused tests passed.
13. Independent financial/protocol review identified acknowledgement ambiguity and V2 schema mismatches.
14. Regression tests were added for optional response `client_order_id`, malformed/mismatched successful acknowledgements, six-decimal prices, `exchange_index=-1`, self-trade-prevention enums, integer fields, and server errors.
15. Fifteen focused tests passed after the corrections.

## Authenticated read-only verification

The externally stored Kalshi credential was rendered from its RTF container in process memory, structurally validated as a 2048-bit RSA private key plus key ID, and used only for:

```text
GET /trade-api/v2/portfolio/balance
```

The signed request succeeded, the response matched the current documented schema, and write arming remained disabled. No key material, signed headers, balances, portfolio values, or response body was printed, logged, copied into Callisto, or committed.

## Migration sequence

1. **Completed:** signed read-only balance method and authenticated read-only credential validation.
2. **REST component completed:** current orders, fills, positions, and settlements use strict immutable read models. WebSocket user-order/fill models remain outstanding. See `KALSHI_V2_READ_MODELS_PLAN.md`.
3. Add durable order-intent and execution-event persistence.
4. Add reconciliation by `client_order_id` before any retry.
5. Add cancel, decrease, and amend with final-state verification.
6. Add demo-only execution adapter behind independent server-side arming.
7. Exercise entry, partial fill, cancellation race, exit, stop-loss, take-profit, pause, reconnect, and settlement scenarios in demo/paper mode.
8. Only after explicit authorization, introduce separately armed production execution.

## Credential boundary

Credentials remain outside the repository. Callisto must load the key ID and RSA private key from a secure external file or process environment, never store them in tracked files, print them, include them in exception text, or emit signed headers to logs.

## Verification commands

```bash
PYTHONPATH=backend python -m pytest -q backend/tests/test_kalshi_v2_client.py
python -m compileall -q backend
cd frontend && npm ci && npm run build
```

The focused test may use an isolated test environment. No credentials are required for the unit suite.
