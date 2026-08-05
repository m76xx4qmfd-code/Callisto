import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import type {
  KalshiPaperAccount,
  KalshiPaperDecision,
  KalshiPaperDecisionInput,
  KalshiPaperEligibility,
} from '../src/services/apiKalshiPaper'

const recordCalls: KalshiPaperDecisionInput[] = []
let recordResult: 'ambiguous' | 'success' = 'ambiguous'

const accounts: KalshiPaperAccount[] = [
  {
    id: 'paper-default',
    name: 'Default paper',
    currency: 'USD',
    starting_cash: '10000.000000000000000000',
    cash_balance: '10000.000000000000000000',
    journal_sequence: 0,
    created_at: '2026-08-05T12:00:00Z',
    updated_at: '2026-08-05T12:00:00Z',
  },
  {
    id: 'paper-secondary',
    name: 'Secondary paper',
    currency: 'USD',
    starting_cash: '500.000000000000000000',
    cash_balance: '500.000000000000000000',
    journal_sequence: 0,
    created_at: '2026-08-05T12:00:00Z',
    updated_at: '2026-08-05T12:00:00Z',
  },
]

const eligibility: KalshiPaperEligibility = {
  opportunity_id: 'opp-live',
  opportunity_stable_id: 'opp-stable',
  opportunity_revision: 'a'.repeat(64),
  strategy_key: 'basic',
  strategy_version: null,
  ticker: 'KXTEST-26',
  outcome: 'yes',
  order_side: 'buy',
  time_in_force: 'immediate_or_cancel',
}

const successDecision: KalshiPaperDecision = {
  account_id: 'paper-secondary',
  decision_id: '',
  account_sequence: 1,
  request_hash: 'b'.repeat(64),
  action: 'execute',
  opportunity_id: 'opp-live',
  opportunity_stable_id: 'opp-stable',
  opportunity_revision: 'a'.repeat(64),
  opportunity_snapshot: {},
  strategy_key: 'basic',
  strategy_version: null,
  ticker: 'KXTEST-26',
  event_ticker: 'KXTEST',
  outcome: 'yes',
  order_side: 'buy',
  time_in_force: 'immediate_or_cancel',
  requested_quantity: '2.00',
  limit_price: '0.600000',
  status: 'filled',
  reason: 'displayed_depth_filled_ioc',
  source_origin: 'https://external-api.kalshi.com',
  market_observed_at: '2026-08-05T12:00:00Z',
  market_fetched_at: '2026-08-05T12:00:00Z',
  market_evidence_hash: 'c'.repeat(64),
  book_observed_at: '2026-08-05T12:00:00Z',
  book_fetched_at: '2026-08-05T12:00:00Z',
  book_evidence_hash: 'd'.repeat(64),
  fill_formula_version: 'kalshi-complementary-depth-ioc-v1',
  fee_rule_version: 'kalshi-market-fee-waiver-v1',
  fee_provenance: {},
  filled_quantity: '2.00',
  remaining_quantity: '0.00',
  average_fill_price: '0.550000000000000000',
  notional: '1.10000000',
  fee: '0.000000000000000000',
  cash_before: '500.000000000000000000',
  cash_after: '498.900000000000000000',
  fills: [],
  created_at: '2026-08-05T12:00:01Z',
}

vi.mock('../src/services/apiKalshiPaper', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/services/apiKalshiPaper')>()
  return {
    ...actual,
    getKalshiPaperAccounts: vi.fn(async () => accounts),
    getKalshiPaperDecisions: vi.fn(async () => []),
    getKalshiPaperEligibility: vi.fn(async () => eligibility),
    createKalshiPaperAccount: vi.fn(async () => accounts[0]),
    recordKalshiPaperDecision: vi.fn(async (input: KalshiPaperDecisionInput) => {
      recordCalls.push(JSON.parse(JSON.stringify(input)))
      if (recordResult === 'ambiguous') {
        throw new Error('Network Error')
      }
      return { ...successDecision, decision_id: input.decision_id }
    }),
  }
})

import KalshiPaperPanel from '../src/components/KalshiPaperPanel'

function mount() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <KalshiPaperPanel />
    </QueryClientProvider>,
  )
}

describe('KalshiPaperPanel ambiguous replay across reload', () => {
  beforeEach(() => {
    localStorage.clear()
    recordCalls.length = 0
    recordResult = 'ambiguous'
  })

  afterEach(() => {
    cleanup()
  })

  it('replays the exact immutable payload for a non-default account after a simulated browser reload', async () => {
    const user = userEvent.setup()
    const first = mount()

    const select = await screen.findByRole('combobox')
    await user.selectOptions(select, 'paper-secondary')

    await user.type(screen.getByPlaceholderText('Opportunity ID or stable ID'), 'opp-live')
    await user.click(screen.getByRole('button', { name: 'Validate' }))
    await screen.findByText('KXTEST-26')

    await user.click(screen.getByRole('button', { name: 'Simulate buy IOC' }))
    await waitFor(() => expect(recordCalls.length).toBe(1))

    const firstPayload = recordCalls[0]
    expect(firstPayload.account_id).toBe('paper-secondary')
    expect(firstPayload.action).toBe('execute')
    expect(firstPayload.quantity).toBe('1.00')
    expect(firstPayload.limit_price).toBe('0.500000')

    const stored = localStorage.getItem('callisto:kalshi-paper:pending-attempt')
    expect(stored).not.toBeNull()
    expect(JSON.parse(stored as string)).toEqual(firstPayload)

    await screen.findByText(/The same decision ID is retained for a safe retry\./)

    first.unmount()

    recordResult = 'success'
    mount()

    const retry = await screen.findByRole('button', { name: /Retry same immutable decision/ })
    await user.click(retry)
    await waitFor(() => expect(recordCalls.length).toBe(2))

    expect(recordCalls[1]).toEqual(firstPayload)
    expect(recordCalls[1].account_id).toBe('paper-secondary')
    expect(recordCalls[1].decision_id).toBe(firstPayload.decision_id)

    await screen.findByText('filled')
  })

  it('discards a stored pending attempt containing unknown fields instead of replaying it', async () => {
    localStorage.setItem(
      'callisto:kalshi-paper:pending-attempt',
      JSON.stringify({
        account_id: 'paper-secondary',
        decision_id: 'paper:stale',
        opportunity_id: 'opp-live',
        opportunity_revision: 'a'.repeat(64),
        action: 'pass',
        tampered: 'value',
      }),
    )

    mount()

    await screen.findByRole('combobox')
    expect(localStorage.getItem('callisto:kalshi-paper:pending-attempt')).toBeNull()
    expect(screen.queryByText(/Retry same immutable decision/)).toBeNull()
  })
})
