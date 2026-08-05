import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import KalshiAuthoritativePortfolio from '../src/components/KalshiAuthoritativePortfolio'
import { getKalshiPrincipalChoices, type KalshiPortfolioSnapshot } from '../src/services/api'

const firstPrincipal = '1'.repeat(64)
const secondPrincipal = '2'.repeat(64)
const snapshot: KalshiPortfolioSnapshot = {
  principal_fingerprint: firstPrincipal,
  retry_allowed: false,
  readiness: 'healthy',
  reason: 'authoritative_projection_current',
  scope: {
    orders_and_fills: { kind: 'all_subaccounts' },
    balance: { kind: 'account_aggregate' },
    positions: { kind: 'subaccount', subaccount_numbers: [0] },
    settlements: { kind: 'subaccount', subaccount_numbers: [0] },
  },
  projection_id: 'projection-1',
  last_healthy_as_of: '2026-08-05T00:00:00.000000Z',
  balance: {
    balance_cents: '123',
    balance_dollars: '1.230000',
    portfolio_value_cents: '123',
    updated_ts: '9007199254740993',
    balance_breakdown: [],
  },
  positions: { subaccount_number: '0', market_positions: [], event_positions: [] },
  settlements: { subaccount_number: '0', settlements: [] },
  orders: [],
  fills: [],
  unknown_activity: { order_ids: [], client_order_ids: [], fill_ids: [] },
  components: {},
  component_skew_seconds: '0.000000',
  gaps: [],
  latest_attempt: {
    projection_id: 'projection-1',
    status: 'complete',
    reason: 'authoritative_components_complete',
    completed_at: '2026-08-05T00:00:00.000000Z',
  },
  sync_runtime: {
    running: true,
    ready: true,
    degraded: false,
    principal_matches: true,
    fresh: true,
    updated_at: '2026-08-05T00:00:00.000000Z',
    error_type: null,
  },
}

const ambiguityError = {
  response: {
    status: 409,
    data: {
      detail: {
        code: 'principal_ambiguous',
        principal_fingerprints: [firstPrincipal, secondPrincipal],
      },
    },
  },
}
let requestCount = 0
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const observer = new QueryObserver<KalshiPortfolioSnapshot>(queryClient, {
  queryKey: ['kalshi-portfolio-snapshot', null],
  queryFn: async () => {
    requestCount += 1
    if (requestCount === 1) return snapshot
    throw ambiguityError
  },
})
const unsubscribe = observer.subscribe(() => undefined)
const initial = await observer.refetch()
assert.equal(initial.data, snapshot)
assert.equal(initial.error, null)
const failedRefetch = await observer.refetch()
unsubscribe()
queryClient.clear()

assert.equal(failedRefetch.data, snapshot)
assert.equal(failedRefetch.error, ambiguityError)
const principalChoices = getKalshiPrincipalChoices(failedRefetch.error)
assert.deepEqual(principalChoices, [firstPrincipal, secondPrincipal])

const markup = renderToStaticMarkup(
  <KalshiAuthoritativePortfolio
    snapshot={failedRefetch.data}
    isLoading={failedRefetch.isLoading}
    error={failedRefetch.error}
    isFetching={failedRefetch.isFetching}
    onRefresh={() => undefined}
    principalChoices={principalChoices}
    selectedPrincipal={null}
    onSelectPrincipal={() => undefined}
  />,
)

assert.match(markup, /cached · query failed/)
assert.match(markup, /Cached evidence is retained only as historical data/)
assert.match(markup, /Select a Kalshi principal/)
assert.match(markup, /<select/)
assert.match(markup, new RegExp(firstPrincipal))
assert.match(markup, new RegExp(secondPrincipal))
assert.doesNotMatch(markup, />healthy</)

console.log('Kalshi authoritative portfolio component state checks passed')
