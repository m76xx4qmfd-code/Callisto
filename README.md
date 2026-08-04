# Callisto

Callisto is a Kalshi-first prediction-market research, strategy, paper-trading, and execution platform forked from [Homerun](https://github.com/braedonsaunders/homerun).

## Fork and license

- Upstream: https://github.com/braedonsaunders/homerun
- Callisto repository: https://github.com/m76xx4qmfd-code/Callisto
- License: GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`)
- Callisto is an independent modified version and is not affiliated with or endorsed by the upstream Homerun authors.
- Existing copyright, attribution, warranty, and license notices are preserved. Callisto modifications are tracked in git history.

## Project constraints

- **Lefty-v5 is read-only source material.** Callisto must not modify `/Users/aituring/Madigan Development Dropbox/Ai Turing/Loogle.ai/1. MarvinAI/Lefty-v5`.
- **Kalshi first.** Venue models, authentication, order lifecycle, market data, historical data, fills, positions, settlements, and WebSockets must follow current Kalshi specifications.
- **Functionality, not a blind port.** Polymarket-specific assumptions must not leak into Callisto.
- **Strategy quality is measurable.** Strategies must support realistic paper execution, backtesting, calibration, journaling, and postmortems before live promotion.
- **No model-added finite sizing caps.** Correctness guards are required; finite policy limits require Lou's explicit direction.

## Current phase

The AGPL fork baseline is established. The first engineering phase is to introduce a Kalshi-native venue boundary and current V2 API support before enabling live execution. See `docs/research/`.
