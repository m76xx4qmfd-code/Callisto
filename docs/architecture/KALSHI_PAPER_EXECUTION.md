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

Paper entries are BUY limit orders against one displayed-depth snapshot. The operator chooses immediate-or-cancel (IOC) or local good-till-cancelled (GTC). Filled BUY evidence creates one immutable paper-position lot per entry decision. An operator may close owned quantity from one lot with a paper-only SELL IOC; SELL never creates a short position and cannot exceed quantity remaining before that exit.

Kalshi publishes YES and NO bids. A buy consumes the opposite-side bid ladder using the binary complement:

```text
buy YES execution price = 1.000000 - NO bid
buy NO  execution price = 1.000000 - YES bid
```

Opposite bids are consumed from highest bid to lowest bid. A level fills only when its complementary execution price is less than or equal to the operator's maximum price. The result is `filled`, `partial`, or `no_fill`. IOC unfilled quantity is terminally cancelled. A GTC unfilled remainder becomes an immutable local paper order and reserves `open quantity × limit price` from available paper cash.

This release has no post-placement matching worker. A local GTC remainder never consumes a later book; it stays open until the operator cancels it. This limitation is explicit in the order API and UI.

Price arithmetic uses integer micro-dollars, quantity arithmetic uses integer hundredths, and notional uses their exact integer product. Ambient Python `Decimal` precision cannot change fill or cash results. The deterministic formula version is `kalshi-complementary-depth-ioc-v1`.

A SELL YES consumes displayed YES bids and a SELL NO consumes displayed NO bids, highest qualifying bid first and never below the operator's minimum price. SELL proceeds credit paper cash after the exact exit fee. This capability does not model settlement as a SELL.

## Durable evidence and replay

Each paper account owns exact USD cash and a monotonic journal sequence. Every operator pass, fill, partial fill, no-fill, and fail-closed market-data rejection creates one immutable decision row. Each displayed-depth fill is an immutable child row.

Before any Kalshi GET, the service commits an immutable request intent containing the canonical input hash and resolved local opportunity snapshot. A PostgreSQL session advisory lock serializes `(account_id, decision_id)` across processes without holding a database transaction over network I/O. The lock and every database transaction use one explicitly checked-out connection; cancellation-safe cleanup invalidates that connection so a session lock can never return to the pool. PostgreSQL releases the lock if the process or connection dies.

The request key is `(account_id, decision_id)`. A replay with the same canonical immutable input hash returns the committed decision before any market-data GET. A different request under the same key returns a conflict. If a process dies after committing the intent but before committing a terminal decision, the next same-ID retry appends one `expired_after_restart` rejection without a new market-data read or cash mutation. Before the first request leaves the browser, the UI durably freezes the complete payload—including account, opportunity revision, action, quantity, and limit—in local storage. Navigation and reload restore that exact retry payload, and a new decision ID is permitted only after a canonical terminal response resolves the ambiguity.

The final BUY transaction locks the account row, revalidates both market and order-book source freshness plus the fee waiver at the commit timestamp, allocates the next sequence, inserts evidence, validates aggregate fills, debits exact cash, and adds any GTC reservation using 18-decimal scaled integers. Concurrent different decisions for one account serialize at the account row; concurrent identical first requests perform one read-only market-data evaluation and cannot double-debit or over-reserve.

SELL uses the same immutable intent, decision, fill, account-cash, and journal-sequence evidence. It acquires stable decision and position advisory locks in sorted order, commits the intent before the GET-only quote, and locks both the account and immutable position lot for finalization. A same-hash replay returns terminal evidence before another GET. An orphaned SELL intent becomes a durable rejection without evaluating a newer book. Manual close and future stop-loss, take-profit, and settlement controllers must all use this position lock and ledger path.

## Position and realized-P&L accounting

`kalshi_paper_positions` stores immutable opening facts from actual committed BUY fills. It is not a mutable balance table. Current quantity, allocated basis, proceeds, fees, and realized P&L are projections over immutable SELL decisions:

```text
remaining quantity = entry filled quantity - cumulative SELL filled quantity
SELL cash after = cash before + gross proceeds - exit fee
realized P&L = gross proceeds - exit fee - allocated entry cost
```

Entry cost is BUY notional plus BUY fee. For entry quantity `Q`, entry cost `C`, and cumulative sold quantity `q`, cumulative allocated basis is `C` at full close and otherwise `trunc(C × q / Q, 18)`. Each SELL receives the difference between the new and prior cumulative allocations, so the terminal exit receives the exact residual and total allocated basis equals entry cost. PostgreSQL validates requested quantity against pre-exit ownership, every per-exit allocation in journal order, cumulative quantity and basis conservation, source-side bid evidence, minimum price, fill aggregates, cash, and realized P&L. Closed lots remain visible as historical evidence.

The position API and UI preserve all financial values as canonical decimal strings and are explicitly labeled `PAPER ONLY`. Before a manual SELL leaves the browser, account, position, decision ID, quantity, and minimum price are persisted as one immutable retry identity. An ambiguous response freezes account switching and other paper mutations until an exactly correlated response resolves the attempt.

## Local cancellation lifecycle

`kalshi_paper_orders` stores immutable GTC opening facts. `kalshi_paper_order_events` and `kalshi_paper_cancellations` are append-only lifecycle evidence; current cancelability is derived from those rows rather than a mutable order projection. A full local cancellation performs no Kalshi request. One transaction locks the paper account and target order, appends the unique cancellation result and terminal event, and releases the order's persisted reservation exactly once.

The cancellation replay key is `(account_id, cancellation_id)`. Repeating the same key and order returns the canonical committed result. Reusing the key for another order conflicts, and concurrent cancellation IDs cannot both release one reservation. The browser persists account, order, and cancellation identity before calling the API; after response loss, reload offers only a byte-equivalent retry and freezes other paper actions until a canonical response arrives.

Local cancellation is not venue cancellation. The service has no authenticated Kalshi transport or provider mutation capability.

PostgreSQL enforces finite fixed-point scale and sign checks without model-added sizing caps, quantity and cash conservation, fill notional equality, contiguous fill sequences, parent/child aggregate equality, deferred account/journal cash-chain validation, and immutable UPDATE/DELETE/TRUNCATE triggers. The migration's downgrade is a deliberate no-op — financial evidence tables are never dropped by policy — and the upgrade path is tested from the actual previous revision.

## Explicitly not included

This capability is not a live-readiness claim. It does not include venue writes, demo authentication, fee-bearing markets, paper GTC decreases or amendments, post-placement fills, settlement accounting, stop-loss/take-profit controllers, autonomous paper workers, approved risk policy, session-bound live arming, server-side venue-write leases, or live execution readiness.
