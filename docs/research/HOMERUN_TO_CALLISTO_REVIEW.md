# Homerun → Callisto: Initial Feasibility and Architecture Review

**Review date:** 2026-08-04  
**Homerun commit reviewed:** `389d24699479daf102bfa9c22882375ca9ebb07a`  
**Callisto venue:** Kalshi  
**Lefty-v5 status:** read-only; no files modified

## Executive conclusion

**Yes, Callisto can recreate Homerun's functional scope for Kalshi. No, Homerun's present execution engine cannot simply be “converted” or pointed at Kalshi.**

Homerun is a large, serious research/trading platform, but its actual live execution path is Polymarket-specific. Its Kalshi client currently supports useful discovery and read-side normalization, yet its authenticated order path uses legacy authentication and the legacy `/portfolio/orders` request shape. Its shared live executor imports the Polymarket client directly, expects Polymarket token IDs, and delegates submit/cancel to a Polymarket CLOB service. There is no equivalent, tested Kalshi live lifecycle wired into that executor.[1]

Kalshi's current V2 event-order surface differs materially: RSA-PSS request signing, a single YES-book `bid`/`ask` model, fixed-point dollar prices and contract counts, idempotent `client_order_id`, and explicit create, cancel, amend, decrease, batch, order-group, user-order, fill, position, and lifecycle facilities. Kalshi states that the old `/portfolio/orders` endpoint was to be deprecated no earlier than May 6, 2026; the current create path is `/portfolio/events/orders`.[3][5][11]

The recommended path is therefore:

1. Use Homerun as a **functional requirements and UX reference**.
2. Use Lefty-v5 as a **read-only source of proven operational lessons**: RSA-PSS signing, live arming, server-side write gates, idempotency, fresh-position verification, sell locks, cancel-before-replace, stop-loss escalation, take-profit placement, and emergency-stop behavior.
3. Build a **new Kalshi-native venue and execution core** inside Callisto against the current Kalshi OpenAPI/AsyncAPI specifications.
4. Port only venue-neutral ideas: strategy SDK shape, research studio, replay/backtesting, opportunity review, risk gates, journaling, reconciliation, observability, and UI workflows.
5. Keep Callisto paper/demo-only until deterministic order-lifecycle and reconciliation tests pass.

## What Homerun actually is

The README describes an end-to-end platform spanning strategy authoring, market scanning, live market data, backtesting, simulated and live trading, portfolio/risk controls, wallet intelligence, AI research, and system observability.[1]

The reviewed checkout is not a small bot:

- Python/FastAPI backend with a large service layer and extensive SQL models.
- React/TypeScript frontend.
- DB-managed Python strategy classes that can be edited, validated, backtested, enabled, and hot-reloaded.
- Dedicated orchestration, risk, execution, reconciliation, backtesting, replay, recording, wallet-analysis, AI, reporting, and monitoring components.
- A substantial automated-test surface, particularly around Polymarket execution and safety.
- A venue-neutral ambition, but a strongly Polymarket-shaped production implementation.

### Functional scope worth recreating

Callisto should target functional parity in these areas:

1. **Market universe and discovery**
   - Search/filter markets and events.
   - Surface liquidity, spread, volume, open interest, lifecycle, and settlement rules.
   - Maintain normalized live and historical catalogs.

2. **Strategy laboratory**
   - Strategy registry and versioning.
   - Editable configuration schemas.
   - Validation, replay, parameter studies, walk-forward testing, and promotion states.
   - Shadow/paper/live modes using the same decision contract.

3. **Opportunity workflow**
   - Scanner-generated candidates.
   - Evidence, assumptions, fair probability, contradiction/adversarial review, execution feasibility, and trade/pass reasoning.
   - Human review and autonomous policies as separate modes.

4. **Execution and risk**
   - Submit, cancel, amend/decrease, replace, partial-fill handling, timeout/escalation, close, stop-loss, and take-profit.
   - Idempotency, freshness checks, stale-order protection, exposure accounting, sell locks, and emergency halt.
   - Exchange-to-local reconciliation as the source of truth.

5. **Data and replay**
   - Event/market metadata, order books, public trades, user orders/fills, positions, settlements, strategy decisions, order intents, and venue acknowledgements.
   - Deterministic replay and realistic matching rather than close-price-only backtests.

6. **Operations and observability**
   - Worker health, feed status, lag, rate limits, rejected actions, reconciliation drift, P&L, and audit trails.

7. **Research/AI**
   - Source-linked research, independent probability estimates, thesis/anti-thesis review, calibration, journals, and postmortems.
   - AI may propose and analyze; deterministic policy controls authorization and venue mutation.

## Homerun strategy catalog

The screenshot supplied by Lou shows the strategy-library interface and strategy cards. The current checkout contains **30 strategy classes**; the earlier screenshot/README may show fewer because the repository has evolved. The extracted machine-readable inventory is stored in `homerun-strategy-catalog.json`.

Current classes:

1. BTC/ETH Convergence
2. BTC/ETH Directional Edge
3. BTC/ETH Maker Quote
4. Basic Arbitrage
5. CTF Basic Arb
6. Certainty Shock
7. Combinatorial Arbitrage
8. Cross-Platform Oracle
9. Crypto 5m Midcycle
10. Crypto Digital Sigma Edge
11. Crypto Distance Edge
12. Crypto Entropy Maker
13. Crypto Spike Reversion
14. Flash Crash Reversion
15. Holding Reward Yield
16. Manual Manage Hold
17. Market Making
18. NegRisk / One-of-Many
19. News Edge
20. News Momentum Breakout
21. Probability Surface Arb
22. Settlement Lag
23. Sports Overreaction Fader
24. Statistical Arbitrage
25. Tail-End Carry
26. Temporal Decay
27. Traders Confluence
28. Traders Copy Trade
29. VPIN Toxicity
30. Weather Distribution

These are **cataloged for functional understanding, not adopted as Callisto investment strategies**. Several are structurally Polymarket-only:

- CTF split/merge arbitrage.
- Polymarket holding rewards.
- NegRisk conversion mechanics.
- Wallet-copy/confluence logic built around on-chain wallet activity.
- Polymarket token-level order books and wallet attribution.

Others express venue-neutral concepts that could be independently implemented for Kalshi after empirical validation: probability-surface consistency, weather distribution modeling, news/fair-value gaps, settlement-lag detection, market making, flow toxicity, statistical relationships, and tail-end carry.

## Data retention and backtesting findings

Homerun's retention design is not one database table or one TTL. It combines:

- SQL state for markets, strategies, opportunities, traders, orders, executions, risk/audit records, experiments, and reports.
- Parquet storage for high-volume order-book snapshots and deltas.
- Event recording/replay adapters.
- Strategy-specific persistent key/value state.
- Configurable opportunity retention, including count and age controls.
- Live/historical feeds, trade tape, snapshot throttling, staleness rejection, and data-quality metrics.
- Replay books injected into the Strategy SDK so strategy logic sees market state during backtests.
- Simulated fill logic with spreads, latency, queue progress, cancellations, impact, fees, and partial fills.
- Walk-forward testing and parameter sweeps.

Important Polymarket assumptions are embedded in this design: 25-level books, token IDs, Polygon/CTF transaction costs, wallet/on-chain identities, specific websocket cadence, and Polymarket fee/reward mechanics. Callisto should reproduce the **capabilities**, not those assumptions.

Kalshi itself partitions older exchange data into dedicated historical endpoints. The documentation says live data targets roughly a three-month window, while older markets, candlesticks, trades, user fills, completed orders, and settled positions move behind historical endpoints and cutoff timestamps.[6] Callisto therefore needs its own durable canonical event store and a fetcher that understands both live and historical Kalshi tiers; relying on repeated calls to only the live endpoints would create silent holes.

## Why Homerun's Kalshi execution is not production-ready

### 1. Authentication mismatch

Homerun's `kalshi_client.py` builds `Authorization: Bearer ...` headers. Current Kalshi authenticated requests use `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, and `KALSHI-ACCESS-SIGNATURE` produced through RSA-PSS signing.[3][5]

### 2. Legacy order endpoint and model

Homerun submits to `/portfolio/orders` with legacy `yes`/`no` semantics and cent prices. Current V2 uses `/portfolio/events/orders`, a `bid`/`ask` side on the YES book, fixed-point dollar strings, and explicit modern fields such as `time_in_force`, `post_only`, `cancel_order_on_pause`, `reduce_only`, `subaccount`, and `exchange_index`.[11]

### 3. No Kalshi-native cancel/amend lifecycle

The Kalshi client contains place/list helpers but no complete, tested cancel/amend/decrease/reconcile implementation connected to Homerun's live order manager. Kalshi's current surface supports separate cancel, amend, decrease, batch, and order-group operations; these need explicit semantics rather than a Polymarket-shaped wrapper.[11]

### 4. Shared live executor is Polymarket-only

`live_execution_adapter.py` imports the Polymarket client directly, prices orders via Polymarket, requires Polymarket token identifiers, places through `live_execution_service`, and cancels CLOB order IDs through that same service. Its tests mock Polymarket client/service behavior. Passing a Kalshi ticker to this path does not turn it into a Kalshi order.

### 5. Data/feed assumptions differ

Homerun's hot path is designed around Polymarket token subscriptions and order-book behavior. Kalshi has event/market tickers, a single YES book representation, fixed-point fields, exchange lifecycle events, user fills, user-order streams, market-position streams, and current V2 protocol rules. A translation layer must be deliberate and lossless.

### 6. Rate-limit handling must be current

Kalshi now documents token-based read/write buckets, endpoint-specific costs, burst capacity, and an authoritative `/account/endpoint_costs` endpoint. A `429` currently has no `Retry-After` header; clients must manage local token budgets and exponential backoff. Batch requests are charged per item and must fit the available bucket.[7] Static old request-per-second assumptions are insufficient.

## Can the engine be converted?

### Direct code conversion: **not recommended**

A direct mechanical conversion would leave Polymarket concepts distributed across strategy inputs, order identifiers, pricing, fee models, market ingestion, live execution, exit handling, tests, and data schemas. That would be expensive to prove safe and easy to get subtly wrong.

### Functional conversion: **recommended and feasible**

Extract a venue-independent contract and implement a fresh Kalshi adapter:

```text
Strategy Decision
    ↓
Order Intent (venue-neutral)
    ↓
Deterministic Risk + Freshness + Position Gates
    ↓
Kalshi Intent Translator
    ↓
Signed Kalshi V2 Client
    ↓
Order State Machine
    ↓
User-order/fill/position streams + REST reconciliation
    ↓
Canonical Journal and Position Ledger
```

The adapter must own:

- Ticker/event/subaccount/exchange-index identity.
- YES-book `bid`/`ask` translation.
- Fixed-point price/count conversion without floating-point ambiguity.
- RSA-PSS signing and clock-skew handling.
- `client_order_id` idempotency and duplicate-request recovery.
- GTC/IOC and post-only semantics.
- Cancel, amend, decrease, batch, and order-group behavior.
- Partial fills and cancel/fill races.
- `reduce_only`/fresh-position safeguards where supported.
- Pause/maintenance behavior and `cancel_order_on_pause` policy.
- WebSocket sequence handling, reconnect, gap detection, and REST repair.
- Local read/write token buckets and endpoint-cost refresh.
- Live/historical data cutover.

## What Lefty contributes without being changed

Lefty-v5 was inspected only as a read-only reference. Its most valuable contribution is not code reuse but hard-earned invariants:

- RSA-PSS signing with Kalshi access headers.
- Server-side live-arming and write allowlists.
- Server-side order guards that cannot be bypassed by UI state.
- Fresh-position fetches before exits.
- Per-ticker sell and stop-loss locks.
- Cancel-before-replace; abort replacement if cancellation cannot be established.
- Progressive stop-loss escalation and post-action position verification.
- Resting take-profit orders and prevention of competing exit paths.
- Negative-position detection and emergency stop.
- Audit logging and order-count accounting.

However, Lefty also currently references the legacy `/portfolio/orders` path. Callisto should preserve its safety lessons while implementing the current V2 interface. Lefty itself remains untouched.

## Kalshi tool/platform landscape

A 2026 ecosystem overview identifies the official Kalshi stack plus Bot for Kalshi, Kalshi Analytics, Kairos, Stand, Oddpool, Cobot, Alphascope, TurbineFi, and Adjacent News. The same overview also discusses DeltaBase for data and Tenki for sourced research.[9]

These names are a **research shortlist, not proof of profitability**. The useful competitive categories are:

| Category | What Callisto should evaluate |
|---|---|
| Official exchange stack | API, SDKs, OpenAPI/AsyncAPI, demo, WebSockets, FIX, historical data |
| Market analytics | Search, liquidity/spread/open-interest views, price history, event comparison |
| Historical datasets | Completeness, granularity, provenance, adjustment/version history |
| Research terminals | Source-linked evidence, resolution-rule analysis, bullish/bearish cases |
| Strategy builders/bots | Paper mode, signals, configurable automation, auditability |
| Portfolio/risk tools | Cost basis, realized/unrealized P&L, exposure, fill and order review |
| Execution infrastructure | Idempotency, rate limits, retries, cancel/fill races, monitoring |

The competitive conclusion is not “copy one of these products.” Callisto's differentiator should be the integrated loop Homerun aspires to provide: **source-grounded research → independent probability → contradiction review → realistic simulation → deterministic execution → reconciliation → calibration/postmortem**.

## Licensing decision

Homerun is licensed under GNU AGPL v3.[1][10]

- If Callisto copies or modifies Homerun code, it should be treated as an AGPL derivative unless qualified counsel concludes otherwise.
- Section 13 requires a modified network-interactive version to prominently offer its corresponding source to users interacting with it remotely.[10]
- Purely local/private use has a different practical posture from offering a hosted service, but future deployment plans matter.

**Recommended engineering posture:** create an independent Callisto repository, write functional specifications from observed behavior, and implement Kalshi-native components without copying Homerun source. This reduces venue-coupling and licensing risk. This is an engineering recommendation, not legal advice; counsel should review before commercial distribution or hosted multi-user deployment.

## Initial Callisto architecture

```text
apps/
  web/                    React operator/research UI
  api/                    FastAPI control plane
services/
  kalshi_gateway/         generated/current V2 REST + WS client
  market_data/            live + historical ingest and normalization
  research/               evidence, probability, contradiction review
  strategy_runtime/       versioned strategy plugins and scheduling
  simulation/             deterministic replay and matching
  execution/              intent state machine and reconciler
  risk/                   deterministic pre-submit and lifecycle gates
  reporting/              journal, calibration, P&L, postmortems
packages/
  domain/                 money, price, contracts, events, orders, fills
  strategy_sdk/           venue-neutral strategy interface
  observability/          metrics, audit events, health
```

Core rule: strategy code never calls Kalshi directly. It emits a typed `Decision`/`OrderIntent`. Only the execution service may mutate the venue, after risk authorization.

## Build phases

### Phase 0 — Specification and safety contracts

- Freeze Homerun functional matrix and strategy/data catalog.
- Freeze Kalshi V2 order/stream schema from OpenAPI/AsyncAPI.
- Define integer/fixed-point money and contract types.
- Define safety invariants and order-state transitions.
- Decide AGPL derivative versus clean-room implementation; default to clean-room.

### Phase 1 — Read-only Kalshi terminal

- Public market/event catalog.
- Order books, trades, lifecycle, settlement rules, candlesticks.
- Historical cutoff routing.
- Local normalized event store and data-quality dashboard.
- No credentials and no order writes.

### Phase 2 — Demo/paper execution core

- Auth signer tested against Kalshi demo.
- Create/cancel/amend/decrease and user-order/fill/position streams.
- Idempotency and deterministic reconciliation.
- Realistic matching/replay and full decision/order journal.
- Failure-injection tests for timeouts, duplicate ACKs, partial fills, gaps, reconnects, cancel/fill races, pauses, and stale positions.

### Phase 3 — Strategy/research studio

- Strategy registry, configuration schemas, backtests, walk-forward runs, and paper promotion.
- Source-preserving evidence bundles.
- Blind probability estimate before revealing market price.
- Mandatory contradiction review and trade/pass rationale.
- Calibration and postmortem reports.

### Phase 4 — Guarded live execution

- Explicit live arming.
- Server-side write allowlist and policy snapshot.
- Fresh-position and exposure reconciliation before every mutation.
- Stops/take-profits built on the tested state machine.
- Canary-size operational validation chosen by Lou, not a model-imposed permanent cap.
- Emergency halt and immutable audit trail.

### Phase 5 — Functional parity expansion

- Multi-strategy scheduling and worker control.
- Opportunity queues and review dashboards.
- Portfolio analytics and risk aggregation.
- Research/AI agents with deterministic authorization boundaries.
- Additional Kalshi-native strategies only after evidence and paper validation.

## Immediate recommendation

Proceed with a **clean-room, Kalshi-native implementation**, not a Homerun fork and not a modification of Lefty. Homerun provides the product map; Lefty provides execution-safety lessons; the current Kalshi V2 specification controls the implementation.

The first executable milestone should be a read-only market/data terminal plus a demo-environment order-lifecycle harness. Strategy development should wait until the data and execution state machine are measurable and replayable.


## Sources

[1] https://github.com/braedonsaunders/homerun — Homerun repository and README
[3] https://docs.kalshi.com/getting_started/quick_start_authenticated_requests — Kalshi Quick Start: Authenticated Requests
[5] https://docs.kalshi.com/sdks/overview — Kalshi SDKs
[6] https://docs.kalshi.com/getting_started/historical_data — Kalshi Historical Data
[7] https://docs.kalshi.com/getting_started/rate_limits — Kalshi Rate Limits
[9] https://www.quicknode.com/builders-guide/best/top-10-kalshi-trading-tools-bots — QuickNode: Top Kalshi Trading Tools and Bots
[10] https://www.gnu.org/licenses/agpl-3.0.en.html — GNU AGPL v3
[11] https://docs.kalshi.com/api-reference/orders/create-order-v2 — Kalshi API Reference: Create Order V2
