# Kalshi-Native Paper Execution

Status: implemented, paper-only, operator-driven, default off

## Boundary

The paper executor is a separate local financial ledger. It cannot initialize a Kalshi account, sign a request, submit/cancel/amend a venue order, or write to the existing live venue execution ledger. It exposes no worker and runs only when an operator explicitly records a decision through the Accounts → Kalshi Paper desk or its API.

The only remote capability is unauthenticated `GET` access to the approved production market-data origin:

- `GET /trade-api/v2/markets/{ticker}`
- `GET /trade-api/v2/markets/{ticker}/orderbook?depth=100`

The client has no credential input, authentication headers, signer, or mutation method.

## Supported contract

A paper execution is eligible only when all of these are true:

- the persisted opportunity revision is still active and matches the operator-confirmed SHA-256 revision;
- the opportunity contains exactly one Kalshi market and exactly one `buy` position;
- the outcome is exactly `yes` or `no`;
- Kalshi returns the same ticker identity;
- the market is active, binary, and has a one-dollar notional;
- the HTTP `Date` evidence for both market and book responses is within five seconds of the decision read;
- the market has a future `fee_waiver_expiration_time` at the observed source time;
- all price, quantity, notional, fee, and cash fields pass strict fixed-point string parsing.

Anything outside this narrow contract is rejected without a paper fill or cash mutation. General Kalshi fee modeling is not claimed by this vertical.

## Fill model

Paper orders are buy-only, immediate-or-cancel limit orders against displayed depth.

Kalshi publishes YES and NO bids. A buy consumes the opposite-side bid ladder using the binary complement:

```text
buy YES execution price = 1.000000 - NO bid
buy NO  execution price = 1.000000 - YES bid
```

Opposite bids are consumed from highest bid to lowest bid. A level fills only when its complementary execution price is less than or equal to the operator's maximum price. The result is `filled`, `partial`, or `no_fill`; unfilled quantity is cancelled by IOC semantics.

Price arithmetic uses integer micro-dollars, quantity arithmetic uses integer hundredths, and notional uses their exact integer product. Ambient Python `Decimal` precision cannot change fill or cash results. The deterministic formula version is `kalshi-complementary-depth-ioc-v1`.

## Durable evidence and replay

Each paper account owns exact USD cash and a monotonic journal sequence. Every operator pass, fill, partial fill, no-fill, and fail-closed market-data rejection creates one immutable decision row. Each displayed-depth fill is an immutable child row.

Before any Kalshi GET, the service commits an immutable request intent containing the canonical input hash and resolved local opportunity snapshot. A PostgreSQL session advisory lock serializes `(account_id, decision_id)` across processes without holding a database transaction over network I/O. The lock and every database transaction use one explicitly checked-out connection; cancellation-safe cleanup invalidates that connection so a session lock can never return to the pool. PostgreSQL releases the lock if the process or connection dies.

The request key is `(account_id, decision_id)`. A replay with the same canonical immutable input hash returns the committed decision before any market-data GET. A different request under the same key returns a conflict. If a process dies after committing the intent but before committing a terminal decision, the next same-ID retry appends one `expired_after_restart` rejection without a new market-data read or cash mutation. Before the first request leaves the browser, the UI durably freezes the complete payload—including account, opportunity revision, action, quantity, and limit—in local storage. Navigation and reload restore that exact retry payload, and a new decision ID is permitted only after a canonical terminal response resolves the ambiguity.

The final transaction locks the account row, revalidates both market and order-book source freshness plus the fee waiver at the commit timestamp, allocates the next sequence, inserts evidence, validates aggregate fills, and debits exact cash using 18-decimal scaled integers. Concurrent different decisions for one account serialize at the account row; concurrent identical first requests perform one read-only market-data evaluation and cannot double-debit.

PostgreSQL enforces finite fixed-point scale and sign checks without model-added sizing caps, quantity and cash conservation, fill notional equality, contiguous fill sequences, parent/child aggregate equality, deferred account/journal cash-chain validation, and immutable UPDATE/DELETE/TRUNCATE triggers. The migration's downgrade is a deliberate no-op — financial evidence tables are never dropped by policy — and the upgrade path is tested from the actual previous revision.

## Explicitly not included

This capability is not a live-readiness claim. It does not include venue writes, demo authentication, fee-bearing markets, cancels/decreases/amends, autonomous paper workers, approved risk policy, session-bound live arming, server-side venue-write leases, or live execution readiness.
