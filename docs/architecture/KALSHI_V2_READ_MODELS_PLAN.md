# Kalshi V2 Read Models Plan

**Branch:** `callisto/kalshi-read-models`
**Date:** 2026-08-04

## Objective

Add strict, immutable, signed read-only models for Kalshi orders, fills, positions, and settlements. This increment supplies the authoritative venue state needed by later persistence and reconciliation work without exposing an API route, starting a worker, loading credentials at application startup, or enabling a write.

## Authoritative protocol snapshot

Implementation was checked against Kalshi's official `https://docs.kalshi.com/openapi.yaml`, version `3.27.0`, on 2026-08-04.

Read endpoints:

- `GET /trade-api/v2/portfolio/orders`
- `GET /trade-api/v2/portfolio/fills`
- `GET /trade-api/v2/portfolio/positions`
- `GET /trade-api/v2/portfolio/settlements`

The client uses canonical `outcome_side` and `book_side` fields rather than the deprecated `action` and `side` fields, and verifies that both representations describe the same exposure. Fixed-point contract counts are accepted only from JSON strings and parsed as `Decimal` with two-decimal precision; fixed-point dollar values are likewise string-only and permit up to six decimal places. Integer wire fields must remain JSON integers. Order responses accept `exchange_index=-1` as Kalshi's documented auto-route sentinel and reject values below it.

## Scope

- Immutable order, fill, market-position, event-position, and settlement records.
- Immutable cursor-bearing response pages.
- Strict required-field, enum, integer, boolean, fixed-point, and pagination validation.
- Signed authenticated GET methods with filters defined by the current OpenAPI schema.
- Cursor preservation for explicit caller-controlled pagination.
- Origin allowlisting and existing RSA-PSS request signing remain unchanged.

## Deliberately disconnected

- No FastAPI route.
- No frontend account-value integration.
- No application-startup credential loading.
- No worker or strategy integration.
- No database persistence yet.
- No automatic pagination loop.
- No cancellation, amendment, decrease, or order submission changes.
- No live or demo write.

## Safety properties

1. All methods in this increment use `GET`.
2. `allow_writes=False` remains compatible with every read method.
3. Query strings are excluded from the signed message under the existing signer contract.
4. Invalid filters fail before transport.
5. Financial values use `Decimal`; responses are never silently rounded.
6. The client returns the venue cursor rather than concealing incomplete pagination.
7. No account values or credential material are logged.
8. Canonical direction aliases, fill aliases, and complementary prices are checked before records are accepted.

## TDD record

1. Added tests importing the planned read models and methods.
2. The suite failed during collection because those models did not exist.
3. Added immutable models, signed GET methods, filter validation, and cursor-bearing page types.
4. Independent review found permissive response types, unchecked canonical aliases, and an uncaught pathological-decimal exception.
5. Added failing regressions for each finding, then made all response wire types and financial aliases fail closed.
6. The new read-model suite, existing Kalshi V2 foundation suite, and safe-runtime suite passed together.

## Next increment

Persist venue-neutral order intents and immutable execution events before transmission. Then use these read models to reconcile `SUBMIT_UNKNOWN`, provider acknowledgements, fills, cancellations, and terminal order state by stable `client_order_id` and provider order ID.
