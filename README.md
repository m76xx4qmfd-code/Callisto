# Callisto

Callisto is a Kalshi-first prediction-market research, strategy, paper-trading, and execution platform inspired by the functional scope of Homerun.

## Project constraints

- **Lefty-v5 is read-only source material.** Callisto must not modify `/Users/aituring/Madigan Development Dropbox/Ai Turing/Loogle.ai/1. MarvinAI/Lefty-v5`.
- **Kalshi first.** Venue models, authentication, order lifecycle, market data, historical data, fills, positions, settlements, and WebSockets must follow current Kalshi specifications.
- **Functionality, not a blind port.** Polymarket-specific assumptions must not leak into Callisto.
- **Strategy quality is measurable.** Strategies must support realistic paper execution, backtesting, calibration, journaling, and postmortems before live promotion.
- **No model-added finite sizing caps.** Correctness guards are required; finite policy limits require Lou's explicit direction.

## Current phase

Research and architecture review. See `docs/research/`.
