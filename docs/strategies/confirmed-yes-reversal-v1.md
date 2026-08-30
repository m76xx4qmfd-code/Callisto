# Confirmed YES Reversal — Version 1

## Status

- **Strategy ID:** `strategy_1_max_roi_confirmed_yes_reversal`
- **Version:** `1`
- **Execution authority:** paper-only
- **Live/demo exchange writes:** prohibited
- **Uploaded specification SHA-256:** `a467de43aaa661a4d1097a5e333c6541e86c28910c02412494cb397d0bb7e1ca`
- **Historical status:** retrospectively selected; no untouched holdout

This Callisto component is a deterministic evidence evaluator. It accepts retained hourly Kalshi candle evidence and point-in-time conference-call schedule evidence, then emits one paper decision per word market. It does not fetch credentials, initialize an authenticated venue client, create an account entry, submit an order, cancel an order, or amend an order.

It is deliberately separate from Callisto's generic opportunity strategy catalog because that catalog is not a paper-only capability boundary and its scanner history does not retain every bid, ask, and volume fact required by the frozen rule.

## API

The existing authenticated Callisto paper router exposes:

```text
GET  /api/kalshi/paper/strategies/confirmed-yes-reversal/specification
POST /api/kalshi/paper/strategies/confirmed-yes-reversal/evaluate
```

The evaluator request contains no account ID, execution mode, order side, limit override, credential, or venue mutation option. Unknown fields are rejected.

## Evidence payload

```json
{
  "schedule": {
    "call_start": "2026-09-01T14:00:00Z",
    "published_at": "2026-08-01T12:00:00Z",
    "observed_at": "2026-08-01T12:01:00Z",
    "source_url": "https://investor.example.com/call",
    "source_content_sha256": "<64 lowercase hex characters>",
    "supersedes_source_content_sha256": null
  },
  "markets": [
    {
      "ticker": "KXEARNINGSMENTIONEXAMPLE-26SEP01-WORD",
      "market_open": "2026-08-01T00:00:00Z",
      "market_close": "2026-09-01T16:00:00Z",
      "candles": [
        {
          "end_time": "2026-08-30T02:00:00Z",
          "yes_bid": "0.240000",
          "yes_ask": "0.260000",
          "volume": "5.00"
        }
      ],
      "settlement": null
    }
  ]
}
```

Invalid or missing candle quote fields remain representable as `null` or boundary decimal strings. This is necessary for the frozen latest-invalid rule; the engine does not silently delete adverse invalid observations.

Settlement fallback, when present, requires final public-result provenance:

```json
{
  "result": "yes",
  "observed_at": "2026-09-01T17:00:00Z",
  "source_url": "https://external-api.kalshi.com/trade-api/v2/markets/<ticker>",
  "evidence_sha256": "<64 lowercase hex characters>",
  "final": true
}
```

A bare caller-asserted result is not accepted.

## Frozen behavior

The implementation preserves the supplied Version 1 rule:

- search `T−168h` through `T−6h`, inclusive;
- reference target `S−12h`, no more than six hours stale;
- valid reference and signal spread no greater than 10¢;
- absolute midpoint decline at least 20 points;
- signal midpoint in `(0, 25¢]`;
- positive signal and delayed-entry candle volume;
- use only the next retained candle for delayed entry;
- delayed entry no more than two hours after signal;
- buy one YES at entry ask;
- separately round entry and exit fees upward to `$0.0001`;
- normal exit uses latest strict quote at or before `T−1h`, no more than one hour stale, selling at YES bid;
- missing/invalid normal exit becomes settlement fallback or remains settlement-pending;
- no take-profit, stop-loss, or intermediate-path optimization.

## Response

Every response includes `request_sha256`, the SHA-256 of the canonical JSON request, so the evaluated candle/schedule/settlement snapshot can be frozen and reproduced.

Every market returns either:

- `selected`, with signal/reference/entry/exit evidence and exact economics; or
- `abstain`, with a deterministic reason.

All selected decisions declare:

```json
{
  "execution_authority": "paper_only",
  "live_exchange_writes": "prohibited",
  "quantity": "1.00"
}
```

## Limitations and next milestone

This is a complete paper evaluator, not an autonomous deployed bot. It intentionally does not:

- discover upcoming call schedules;
- ingest or persist public Kalshi candles;
- create durable paper-account ledger entries;
- run a background scheduler;
- reconstruct historical depth or queue position;
- authorize live execution.

A prospective autonomous paper worker requires a separately reviewed vertical with durable schedule/candle evidence, default-disabled worker control, exact account journal integration, current/historical API cutover, and proof that authenticated venue mutation methods remain unreachable.
