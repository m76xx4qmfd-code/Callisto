# Callisto Research Scope

## Objective

Determine how to reproduce Homerun's functional platform scope as a Kalshi-first application while preserving Lefty-v5 unchanged.

## Source baselines

- Homerun repository: `braedonsaunders/homerun`
- Reviewed commit: `389d24699479daf102bfa9c22882375ca9ebb07a`
- Lefty-v5: read-only local inspection
- Kalshi: current official documentation, OpenAPI/AsyncAPI specifications, and official SDK guidance
- Competitive landscape: official Kalshi stack plus third-party analytics, research, historical-data, and automation products

## Questions

1. What does Homerun actually implement versus what its README advertises?
2. Which strategies and research/data-retention facilities exist?
3. Is Homerun's live entry, exit, cancel, amend, fill, and reconciliation engine portable to Kalshi?
4. Which Polymarket assumptions must be replaced rather than adapted?
5. Which Lefty execution lessons can be reimplemented in Callisto without changing Lefty?
6. Should Callisto be an AGPL derivative or an independent clean-room implementation?
7. What should Callisto build first to reach a verified paper-trading system, then safe live execution?

## Non-goals during review

- No live trades.
- No credential setup.
- No modifications to Lefty-v5.
- No unverified claim that a strategy makes money.
