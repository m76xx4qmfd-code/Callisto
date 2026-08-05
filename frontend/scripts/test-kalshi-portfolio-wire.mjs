import assert from 'node:assert/strict'
import { requireKalshiExactWireValues } from '../src/services/kalshiPortfolioWire.ts'

const payload = {
  component_skew_seconds: '0.000000',
  balance: {
    balance_cents: '9007199254740993',
    balance_dollars: '90071992547409.930000',
    portfolio_value_cents: '9007199254740993',
    updated_ts: '9007199254740993',
    balance_breakdown: [{ exchange_index: '0', balance: '90071992547409.930000' }],
  },
  positions: {
    subaccount_number: '0',
    market_positions: [],
    event_positions: [],
  },
  settlements: {
    subaccount_number: '0',
    settlements: [],
  },
  orders: [],
  fills: [],
}

const accepted = requireKalshiExactWireValues(structuredClone(payload))
assert.equal(accepted.balance.balance_cents, '9007199254740993')

const drifted = structuredClone(payload)
drifted.balance.balance_cents = Number(drifted.balance.balance_cents)
assert.throws(
  () => requireKalshiExactWireValues(drifted),
  /must be text at snapshot\.balance\.balance_cents/,
)

console.log('Kalshi authoritative exact-wire validation passed')
