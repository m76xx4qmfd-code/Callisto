# Deep-Audit Addendum

**Date:** 2026-08-04  
**Homerun commit:** `389d24699479daf102bfa9c22882375ca9ebb07a`

This addendum records findings from three independent follow-up workstreams: repository audit, current Kalshi API/tooling research, and licensing/portability analysis. It supplements `HOMERUN_TO_CALLISTO_REVIEW.md`.

## 1. Corrections and material additions

### Homerun's “unified Polymarket + Kalshi” claim is materially inaccurate

Homerun's Kalshi integration supports discovery, parsing, historical candles, account reads, and cross-platform signal generation, but it is not wired into the production order lifecycle.[1]

Specific evidence in the reviewed source:

- `backend/services/live_execution_adapter.py` imports and calls Polymarket clients/services directly and has no venue router.
- `backend/services/trader_orchestrator/order_manager.py` ignores a leg's platform and falls back to Polymarket token lookup.
- `backend/services/live_execution_service.py` initializes Polymarket CLOB credentials/client state.
- `backend/services/kalshi_client.py` uses legacy Bearer/login auth and exposes no cancel implementation.
- `backend/api/routes_kalshi.py` exposes account/read routes but no place/cancel API route.
- Live execution tests mock `polymarket_client.get_price`; no equivalent Kalshi lifecycle suite was found.

The cross-platform strategy can create a logical Kalshi leg, but the orchestrator cannot execute that leg through a real Kalshi adapter. It therefore cannot execute a genuine two-venue arbitrage lifecycle.

### Homerun's standard deployment omits important worker planes

The worker host defines separate `jobs`, `recording`, `detection`, `reconciliation`, and `services` planes in addition to `trading`, `news`, and `discovery`. The checked-in Docker Compose starts only the latter three.

Consequences include:

- Scanner/universe tasks assigned to `detection` may not run.
- Queued backtests assigned to `jobs` may remain queued.
- L2 recording disabled in `trading` has no `recording` replacement.
- Cold reconciliation and platform-service loops are absent.

This makes the README's simple `docker compose up -d` claim unsafe as a completeness assumption.[1]

### Strategy hot-reload is powerful but unsafe as designed

Homerun stores strategy source in database rows and dynamically compiles/executes enabled source with full Python builtins. Existing DB strategy source is create-only and is not automatically upgraded when shipped strategy code changes.

This creates two risks:

1. Anyone able to modify strategy source effectively has backend code-execution privilege.
2. Safety or correctness fixes in shipped strategy files may never reach existing strategy rows.

Callisto should instead use immutable, version-addressed strategy code, separately versioned operator parameters, explicit migrations, and sandbox/capability boundaries.

## 2. Data-retention and replay corrections

The README implies broadly replayable persistence. Actual L2 persistence has bounded retention and failure modes:[1]

- L2 books moved from SQL to Parquet.
- Default L2 retention is **7 days** with a **40 GiB** total limit.
- Rolling windows, age/size pruning, and emergency disk-pressure pruning apply.
- Buffered rows may be dropped at high-water limits.
- Entire buffered batches may be discarded under disk pressure.
- Running backtests pin input paths only during the run; they do not preserve the dataset afterward.
- A separate recorded-event pruning system can produce a different replay horizon from L2 data.
- Remote OHLC/candle history is cached locally without a clear TTL or provenance/version manifest.
- Some coverage-resolution failures deliberately allow a backtest to proceed without warning.
- The dedicated exit “backtest” evaluates current shadow positions once; it is not historical lifecycle replay.

### Callisto requirement

Callisto needs immutable, manifest-addressed datasets with:

- Venue and schema version.
- Capture method and source.
- Start/end coverage and market universe.
- Sequence gaps and reconnect intervals.
- Checksums and provenance.
- Retention class and legal rights.
- Explicit degraded/fail-closed backtest status.

A research result must never silently outlive or lose the dataset needed to reproduce it.

## 3. Homerun order-lifecycle risks not to inherit

The Polymarket execution system is substantial, but the audit found material hazards:[1]

- **Possible duplicate submissions:** GTC and SELL transport failures can be retried even when the first venue request may have succeeded; whole-adapter retries can add a second layer.
- **Post-ack persistence gap:** venue acknowledgement is followed by asynchronous local persistence, leaving a crash window where the order exists remotely but not locally.
- **Optional recoverability metadata:** reconciliation cannot reliably recover submissions without provider-discoverable metadata.
- **False local cancellation:** Telegram cancellation can mark local state cancelled even when provider cancellation fails.
- **Unsafe force-delete:** a trader can be deleted locally while live venue orders remain capable of filling.

Callisto's invariants must be stricter:

1. Persist intent and `client_order_id` before sending.
2. Treat timeout/unknown acknowledgement as `SUBMIT_UNKNOWN`, never rejected or safe-to-retry by assumption.
3. Resolve unknown submissions through idempotent lookup/reconciliation before another submit.
4. Mark cancellation complete only after provider state or authoritative reconciliation proves it.
5. Never delete ownership while orders, positions, or uncertain mutations exist; archive instead.
6. Make every destructive administrative action venue-aware and reconciliation-gated.

## 4. Current Kalshi capabilities

The official API is sufficient for a Kalshi-native execution platform and is richer than Homerun's partial client:[3][5][11]

- Create and batch-create orders.
- Single and batch cancellation.
- Native amend and quantity decrease.
- Client-order IDs for deduplication/idempotency.
- Order groups and group limit/reset behavior.
- Queue-position queries.
- Subaccounts and account transfers/netting.
- Fills, orders, positions, and settlements.[13][14][15]
- User-order, fill, position, lifecycle, order-group, ticker, trade, and order-book WebSocket channels.
- Separate demo and production endpoints and credentials.[12]
- Historical partitions for markets, candlesticks, public trades, user fills, orders, and positions.[6]

Kalshi explicitly warns that SDKs may lag and recommends its OpenAPI and AsyncAPI specifications as sources of truth.[5]

### Largest official-data gap

The official historical interfaces do not document a complete historical L2 order-book archive. Bar/trade research is possible, but realistic queue, fill, slippage, amend, and cancellation replay requires one of:

1. Callisto's own durable WebSocket L2 recorder.
2. A licensed dataset that passes provenance and sequence-integrity diligence.
3. A deliberately lower-fidelity backtest labeled as such.

## 5. Third-party historical-data/backtesting candidates

These products are diligence candidates only; all stated coverage remains vendor marketing until sampled and reconciled:

| Product | Public claim | Required validation |
|---|---|---|
| Kalshi BackTest | High-frequency L2/trades for selected short-duration crypto markets.[16] | Market/date breadth, true cadence, sequence recovery, licensing |
| Lychee | Large Kalshi market/trade/order-book dataset and exports.[17] | Capture provenance, completeness, timestamp quality, revisions |
| DepthFeed / Kalshi Backtesting | Deep YES/NO books for selected crypto assets.[18] | Continuity, universe, missing-message policy, redistribution rights |
| Pathfinder | Natural-language backtesting, calibration, data, and live-bot export.[19] | Fill realism, fees, survivorship, trade-count claims, safety controls |

Before purchase or integration, require:

- Exact market/date coverage, including voided, corrected, and delisted markets.
- Source/capture method and timestamp clock.
- Sequence-gap and duplicate policy.
- Full depth versus periodic snapshots.
- YES/NO normalization.
- Fee, latency, queue, partial-fill, amend, and cancel models.
- Settlement revisions.
- Export/retention/redistribution rights.
- Reconciliation of a sample against official trades and candlesticks.

## 6. Licensing decision that must be made before implementation

Homerun is AGPLv3-or-later.[1][10]

### Path A — acknowledged AGPL fork

Use when source availability is acceptable.

Advantages:

- Fastest route to Homerun's existing UI and platform breadth.
- License expressly permits copying, modifying, operating, and charging, subject to its conditions.[10]
- Retains repository history and an auditable origin.

Requirements include:

- Preserve copyright, license, attribution, and warranty notices.
- License the derivative as AGPLv3-or-later.
- Identify modifications and dates.
- Provide Corresponding Source when conveying covered builds.
- Prominently offer the running Corresponding Source to remote network users of a modified version.
- Replace Homerun branding/assets while preserving legal attribution.

This is not merely a rebrand: the deeply Polymarket-shaped domain and execution layers still require major refactoring.

### Path B — independent proprietary-capable implementation

Use when Callisto may need to remain proprietary.

Advantages:

- New Kalshi-native architecture without inherited Polymarket coupling.
- No AGPL derivative dependency if independently implemented.

Costs and controls:

- Slower than a fork.
- Functionality and methods may be independently implemented, but source expression, tests, prompts, schemas, detailed structure, screenshots, copy, and assets should not be copied.[21][22]
- A defensible process uses a separate functional specification, new architecture/naming/UI, provenance logs, independent fixtures/tests, and code-similarity review.
- Because current reviewers have examined Homerun source, describing their implementation as a pristine “clean room” would be imprecise; separation of specification and implementation roles would strengthen the record.
- The *Google v. Oracle* decision is fact-specific and not a general license to clone implementation expression.[23]

Brand identity should be Callisto; Homerun logos, screenshots, and distinctive marketing assets should not be used. Trademark clearance for “Callisto” should precede commercial launch.[24]

### Decision matrix

| Question | If yes | Recommended path |
|---|---|---|
| Can all network users receive the running source? | Yes | AGPL fork is fastest |
| Must the hosted product remain proprietary? | Yes | Independent implementation |
| Is a separate commercial license available from Homerun rights holders? | Yes | Evaluate negotiated-license fork |
| Is this permanently local/internal with no external distribution? | Yes | Either path can work operationally, but future deployment plans still matter |

This is a technical/licensing assessment, not formal legal advice.

## 7. Revised recommendation

Do not start application code until Lou chooses **AGPL fork** or **independent proprietary-capable implementation**.

Whichever path is selected:

- Lefty-v5 remains untouched.
- Kalshi OpenAPI/AsyncAPI control all venue behavior.
- Homerun's current Kalshi client is test input, not authority.
- The first operational milestone remains a read-only market/data layer plus demo-environment order lifecycle.
- Callisto must begin recording its own L2 data immediately if realistic future replay is a goal.
## Sources

[1] https://github.com/braedonsaunders/homerun — Homerun repository and README
[3] https://docs.kalshi.com/getting_started/quick_start_authenticated_requests — Kalshi Quick Start: Authenticated Requests
[5] https://docs.kalshi.com/sdks/overview — Kalshi SDKs
[6] https://docs.kalshi.com/getting_started/historical_data — Kalshi Historical Data
[10] https://www.gnu.org/licenses/agpl-3.0.en.html — GNU AGPL v3
[11] https://docs.kalshi.com/api-reference/orders/create-order-v2 — Kalshi API Reference: Create Order V2
[12] https://docs.kalshi.com/getting_started/api_environments — Kalshi API Environments
[13] https://docs.kalshi.com/api-reference/portfolio/get-fills — Kalshi Get Fills
[14] https://docs.kalshi.com/api-reference/portfolio/get-positions — Kalshi Get Positions
[15] https://docs.kalshi.com/api-reference/portfolio/get-settlements — Kalshi Get Settlements
[16] https://kalshibacktest.com — Kalshi BackTest
[17] https://lycheedata.com/kalshi-historical-data — Lychee Kalshi Historical Data
[18] https://kalshibacktesting.com — DepthFeed / Kalshi Backtesting
[19] https://www.pathfinderv1.com — Pathfinder
[21] https://www.copyright.gov/help/faq/faq-protect.html — US Copyright Office FAQ
[22] https://www.copyright.gov/circs/circ33.pdf — US Copyright Office Circular 33
[23] https://www.supremecourt.gov/opinions/20pdf/18-956_d18f.pdf — Google LLC v. Oracle America, Inc.
[24] https://www.uspto.gov/trademarks/basics/what-trademark — USPTO Trademark Basics
