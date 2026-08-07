# Kalshi live readiness: permanently blocked v1

`GET /api/kalshi/live-readiness` is an evidence-defined, static read model. It
always reports `assessment: "permanently_blocked"`,
`effective_execution: "disabled"`, and `live_ready: false`. Version 1 is not
promotion logic. The route performs no database, credential, environment,
client, executor, control-state, worker, or venue access.

Credentials, a successful public read, and portfolio data do **not** authorize
writes. Portfolio readiness is explicitly `not_assessed`; it is never omitted
or described as healthy. Unknown/null, `absent`, `not_implemented`, `disabled`,
`not_assessed`, and boolean `false` have distinct meanings and must not be
collapsed. No numeric risk value is emitted because no operator policy exists.

## Blocker evidence

| ID | Status | Precise claim | Repository evidence |
| --- | --- | --- | --- |
| LR-01 | `absent` | No approved operator policy exists for live Kalshi execution. | `docs/architecture/KALSHI_LIVE_READINESS.md` records the absence; no values or limits are invented. |
| LR-02 | `not_implemented` | No authorization distinct from paper operation authorizes live Kalshi writes. | This document and the static route contain no authorization transition. |
| LR-03 | `not_implemented` | No live arm is bound to both the current runtime instance and operator session. | `docs/architecture/SAFE_LOCAL_RUNTIME_PLAN.md` describes runtime safety, not an implemented live Kalshi arm. |
| LR-04 | `not_implemented` | The final venue-write boundary has no current runtime write lease. | `backend/services/venues/kalshi_v2.py` has a constructor flag, not a runtime/session lease. |
| LR-05 | `not_assessed` | Portfolio readiness is neither read here nor connected to activation. | `docs/architecture/KALSHI_AUTHORITATIVE_PORTFOLIO_PROJECTION_PLAN.md` is separate from this static assessment. |
| LR-06 | `not_implemented` | No complete reviewed submit, acknowledgement, fill, cancel, reconciliation, and position lifecycle exists. | `backend/services/venues/kalshi_v2.py` provides isolated primitives, not the complete lifecycle. |
| LR-07 | `unsafe` | Mounted legacy account routes can initialize stored Kalshi credentials on read requests and are not a live-readiness or authorization boundary. | `backend/api/routes_kalshi.py` mounts `/status`, `/balance`, `/positions`, and `/orders`; those handlers may call `_try_auto_login()`. |
| LR-08 | `disabled` | Dormant legacy/write-capable paths would violate this contract if enabled. | `backend/services/venues/kalshi_v2.py` contains the dormant primitive described below. |
| LR-09 | `absent` | The mandatory future-live safety regression matrix is missing. | This v1 suite tests permanent blocking and isolation, not a future live lifecycle. |

## Dormant write primitive truth

`KalshiV2Client.create_order(...)` exists, and constructing that client with
`allow_writes=True` makes it write-capable. It is dormant and disabled by
default. It is not wired to a production route or production execution runtime.
The readiness route does not import or initialize it. Its existence is reported
rather than hidden, but it supplies no readiness evidence and grants no
authority.

## Public final-result boundary

The production result client is structurally separate from the validation CLI.
It has no origin selector and performs one unauthenticated production
`GET /trade-api/v2/markets/{ticker}`. The CLI owns its own validation-only
production/demo allowlist and reuses only the pure parser. Demo observations
cannot enter paper routes, workers, services, or durable accounting evidence.
Both paths disable redirects and environment proxies and rely on platform CA/TLS
verification. Missing, blank, malformed, or more-than-five-seconds divergent
response `Date` values fail closed.
