# Source Manifest

## Homerun

- Repository: https://github.com/braedonsaunders/homerun
- Reviewed commit: `389d24699479daf102bfa9c22882375ca9ebb07a`
- License: GNU Affero General Public License v3.0
- Local review checkout: `/tmp/homerun-review` (temporary, not part of Callisto)

## Kalshi primary sources

- Documentation: https://docs.kalshi.com/
- Market data quick start: https://docs.kalshi.com/getting_started/quick_start_market_data
- Authentication: https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
- Order lifecycle: https://docs.kalshi.com/getting_started/quick_start_create_order
- Historical data: https://docs.kalshi.com/getting_started/historical_data
- Rate limits: https://docs.kalshi.com/getting_started/rate_limits
- WebSockets: https://docs.kalshi.com/getting_started/quick_start_websockets
- SDK guidance: https://docs.kalshi.com/sdks/overview
- Current event-order V2 reference: https://docs.kalshi.com/api-reference/orders/create-order-v2
- OpenAPI source of truth: https://docs.kalshi.com/openapi.yaml
  - Retrieved: 2026-08-05
  - Declared version: `3.27.0`
  - SHA-256: `41d93050bf3f692cf3a898ba3a1a033f3e857fee56370ddcb18af6a4225f41cb`
  - Paper scope: unauthenticated production market and fixed-point orderbook GET schemas only; no venue-write endpoint is exposed.
- Resolution OpenAPI source of truth:
  - Source URL: `https://docs.kalshi.com/openapi.yaml`
  - First retrieval: `2026-08-07T17:35:22Z`
  - Second retrieval: `2026-08-07T18:15:43.660581Z`
  - Final resolved URL: `https://docs.kalshi.com/openapi.yaml`
  - Vendored bytes: `kalshi_openapi_3_27_0_20260807.yaml`
  - OpenAPI dialect: `3.0.0`
  - Declared version: `3.27.0`
  - SHA-256: `bd80e9d42fec2f9cddd5e498ef53cf34bc79effec8fe39031b327c9d483741e2`
  - Root `security`: absent
  - `GET /markets/{ticker}` `security`: absent
  - Resolution scope: strict selected settlement projection from the unauthenticated public operation only. This provenance is separate from, and does not replace, historical paper evidence bound to `41d930…`.
- AsyncAPI source of truth: https://docs.kalshi.com/asyncapi.yaml

## Competitive landscape starting source

- QuickNode tools overview: https://www.quicknode.com/builders-guide/best/top-10-kalshi-trading-tools-bots
- Kalshi BackTest: https://kalshibacktest.com/
- Lychee Kalshi historical data: https://lycheedata.com/kalshi-historical-data
- DepthFeed / Kalshi Backtesting: https://kalshibacktesting.com/
- Pathfinder: https://www.pathfinderv1.com/

## Licensing and functional-reimplementation sources

- GNU AGPL v3: https://www.gnu.org/licenses/agpl-3.0.en.html
- GNU GPL FAQ: https://www.gnu.org/licenses/gpl-faq.html
- U.S. Copyright Office FAQ: https://www.copyright.gov/help/faq/faq-protect.html
- U.S. Copyright Office Circular 33: https://www.copyright.gov/circs/circ33.pdf
- *Google LLC v. Oracle America, Inc.*: https://www.supremecourt.gov/opinions/20pdf/18-956_d18f.pdf
- USPTO trademark basics: https://www.uspto.gov/trademarks/basics/what-trademark

All third-party feature descriptions must be treated as vendor/editorial claims until independently verified.
