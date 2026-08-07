import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import type {
  KalshiPaperAccount,
  KalshiPaperCancellationInput,
  KalshiPaperDecision,
  KalshiPaperDecisionInput,
  KalshiPaperEligibility,
  KalshiPaperOrder,
  KalshiPaperPosition,
  KalshiPaperPositionExitInput,
  KalshiPaperTestRun,
  KalshiPaperTestRunInput,
} from '../src/services/apiKalshiPaper'

const recordCalls: KalshiPaperDecisionInput[] = []
let recordResult: 'ambiguous' | 'success' | 'deferred' = 'ambiguous'
let resolveRecord: ((value: KalshiPaperDecision) => void) | null = null
const cancellationCalls: KalshiPaperCancellationInput[] = []
let cancellationResult: 'ambiguous' | 'success' | 'deferred' = 'ambiguous'
let resolveCancellation: ((value: unknown) => void) | null = null
const positionExitCalls: KalshiPaperPositionExitInput[] = []
let positionExitResult: 'ambiguous' | 'success' | 'deferred' = 'ambiguous'
let resolvePositionExit: ((value: KalshiPaperDecision) => void) | null = null
const testRunCalls: KalshiPaperTestRunInput[] = []
let testRunResult: 'ambiguous' | 'success' | 'mismatch' | 'deferred' = 'ambiguous'
let resolveTestRun: ((value: unknown) => void) | null = null
const testControlCalls: Array<{ runId: string; action: 'pause' | 'resume' | 'stop' }> = []
let testRunStatus: KalshiPaperTestRun['status'] = 'monitoring'

const accounts: KalshiPaperAccount[] = [
  {
    id: 'paper-default',
    name: 'Default paper',
    currency: 'USD',
    starting_cash: '10000.000000000000000000',
    cash_balance: '10000.000000000000000000',
    reserved_cash: '0.000000000000000000',
    available_cash: '10000.000000000000000000',
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
    reserved_cash: '0.600000000000000000',
    available_cash: '499.400000000000000000',
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
  position_id: null,
  time_in_force: 'immediate_or_cancel',
  requested_quantity: '2.00',
  limit_price: '0.600000',
  status: 'filled',
  reason: 'displayed_depth_filled_ioc',
  market_evidence_hash: 'c'.repeat(64),
  book_evidence_hash: 'd'.repeat(64),
  fill_formula_version: 'kalshi-complementary-depth-ioc-v1',
  fee_rule_version: 'kalshi-market-fee-waiver-v1',
  fee_provenance: {},
  filled_quantity: '2.00',
  remaining_quantity: '0.00',
  average_fill_price: '0.550000000000000000',
  notional: '1.10000000',
  fee: '0.000000000000000000',
  position_cost_basis: null,
  realized_pnl: null,
  cash_before: '500.000000000000000000',
  cash_after: '498.900000000000000000',
  order_id: null,
  reserved_cash: '0.000000000000000000',
  fills: [],
  created_at: '2026-08-05T12:00:01Z',
}

const openOrder: KalshiPaperOrder = {
  account_id: 'paper-secondary',
  order_id: 'paper-order:stable',
  decision_id: 'paper:gtc',
  ticker: 'KXTEST-26',
  outcome: 'yes',
  side: 'buy',
  time_in_force: 'good_till_canceled',
  requested_quantity: '6.00',
  filled_quantity: '5.00',
  open_quantity: '1.00',
  limit_price: '0.600000',
  reserved_cash: '0.600000000000000000',
  cancelable: true,
  status: 'open',
  cancellation_id: null,
  later_matching_supported: false,
  created_at: '2026-08-05T12:00:00Z',
}

const openPosition: KalshiPaperPosition = {
  account_id: 'paper-secondary',
  position_id: 'paper-position:abc',
  entry_decision_id: 'paper:entry',
  ticker: 'KXTEST-26',
  outcome: 'yes',
  entry_quantity: '4.00',
  entry_notional: '2.300000000000000000',
  entry_fee: '0.000000000000000000',
  sold_quantity: '0.00',
  remaining_quantity: '4.00',
  exit_notional: '0.000000000000000000',
  exit_fee: '0.000000000000000000',
  allocated_entry_cost: '0.000000000000000000',
  realized_pnl: '0.000000000000000000',
  status: 'open',
  closable: true,
  exit_decision_ids: [],
  created_at: '2026-08-06T12:00:00Z',
}

const testRun: KalshiPaperTestRun = {
  run_id: 'paper-test-run:fixture',
  account_id: 'paper-secondary',
  opportunity_id: eligibility.opportunity_id,
  opportunity_revision: eligibility.opportunity_revision,
  ticker: eligibility.ticker,
  outcome: eligibility.outcome,
  quantity: '2.00',
  entry_limit_price: '0.600000',
  take_profit_price: '0.700000',
  stop_loss_price: '0.400000',
  stop_loss_minimum_price: '0.300000',
  entry_decision_id: 'paper-test-entry:paper-test-run:fixture',
  position_id: openPosition.position_id,
  status: 'monitoring',
  next_event_sequence: 2,
  remaining_quantity: '2.00',
  realized_pnl: '0.000000000000000000',
  last_error: null,
  last_reason: 'entry_filled',
  created_at: '2026-08-07T12:00:00Z',
  updated_at: '2026-08-07T12:00:00Z',
}

const testRunDetail = {
  run: testRun,
  events: [{
    run_id: 'paper-test-run:fixture',
    account_id: 'paper-secondary',
    sequence: 1,
    event_type: 'entry_filled',
    position_id: openPosition.position_id,
    best_bid: null,
    trigger_price: null,
    exit_decision_id: null,
    market_observed_at: null,
    book_observed_at: null,
    quote_evidence_hash: null,
    quote_evidence_json: null,
    remaining_quantity: '2.00',
    realized_pnl: '0.000000000000000000',
    reason: 'entry_filled',
    created_at: '2026-08-07T12:00:00Z',
  }],
}

vi.mock('../src/services/apiKalshiPaper', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/services/apiKalshiPaper')>()
  return {
    ...actual,
    getKalshiPaperAccounts: vi.fn(async () => accounts),
    getKalshiPaperDecisions: vi.fn(async () => []),
    getKalshiPaperOrders: vi.fn(async (accountId: string) => accountId === 'paper-secondary' ? [openOrder] : []),
    getKalshiPaperPositions: vi.fn(async (accountId: string) => accountId === 'paper-secondary' ? [openPosition] : []),
    getKalshiPaperEligibility: vi.fn(async () => eligibility),
    getKalshiPaperTestRuns: vi.fn(async (accountId: string) => accountId === 'paper-secondary'
      ? [{ ...testRunDetail, run: { ...testRun, status: testRunStatus } }]
      : []),
    getKalshiPaperTestRun: vi.fn(async () => testRunDetail),
    createKalshiPaperAccount: vi.fn(async () => accounts[0]),
    startKalshiPaperTestRun: vi.fn(async (input: KalshiPaperTestRunInput) => {
      testRunCalls.push(JSON.parse(JSON.stringify(input)))
      if (testRunResult === 'ambiguous') throw new Error('Network Error')
      if (testRunResult === 'mismatch') throw new Error('Kalshi paper test run response identity mismatch')
      if (testRunResult === 'deferred') return new Promise((resolve) => { resolveTestRun = resolve })
      return {
        run: { ...testRun, ...input, entry_decision_id: `paper-test-entry:${input.run_id}` },
        events: testRunDetail.events.map((event) => ({ ...event, run_id: input.run_id, account_id: input.account_id })),
      }
    }),
    pauseKalshiPaperTestRun: vi.fn(async (runId: string) => {
      testControlCalls.push({ runId, action: 'pause' })
      testRunStatus = 'paused'
      return { ...testRunDetail, run: { ...testRun, run_id: runId, status: 'paused' as const } }
    }),
    resumeKalshiPaperTestRun: vi.fn(async (runId: string) => {
      testControlCalls.push({ runId, action: 'resume' })
      testRunStatus = 'monitoring'
      return { ...testRunDetail, run: { ...testRun, run_id: runId, status: 'monitoring' as const } }
    }),
    stopKalshiPaperTestRun: vi.fn(async (runId: string) => {
      testControlCalls.push({ runId, action: 'stop' })
      testRunStatus = 'stopped'
      return { ...testRunDetail, run: { ...testRun, run_id: runId, status: 'stopped' as const } }
    }),
    recordKalshiPaperDecision: vi.fn(async (input: KalshiPaperDecisionInput) => {
      recordCalls.push(JSON.parse(JSON.stringify(input)))
      if (recordResult === 'ambiguous') {
        throw new Error('Network Error')
      }
      if (recordResult === 'deferred') return new Promise((resolve) => { resolveRecord = resolve })
      return { ...successDecision, decision_id: input.decision_id }
    }),
    cancelKalshiPaperOrder: vi.fn(async (input: KalshiPaperCancellationInput) => {
      cancellationCalls.push(JSON.parse(JSON.stringify(input)))
      if (cancellationResult === 'ambiguous') throw new Error('Network Error')
      if (cancellationResult === 'deferred') return new Promise((resolve) => { resolveCancellation = resolve })
      return {
        ...input,
        status: 'cancelled' as const,
        released_cash: '0.600000000000000000',
        created_at: '2026-08-05T12:01:00Z',
      }
    }),
    exitKalshiPaperPosition: vi.fn(async (input: KalshiPaperPositionExitInput) => {
      positionExitCalls.push(JSON.parse(JSON.stringify(input)))
      if (positionExitResult === 'ambiguous') throw new Error('Network Error')
      if (positionExitResult === 'deferred') return new Promise((resolve) => { resolvePositionExit = resolve })
      return {
        ...successDecision,
        account_id: input.account_id,
        decision_id: input.decision_id,
        account_sequence: 2,
        position_id: input.position_id,
        order_side: 'sell' as const,
        time_in_force: 'immediate_or_cancel' as const,
        requested_quantity: input.quantity,
        limit_price: input.minimum_price,
        filled_quantity: input.quantity,
        remaining_quantity: '0.00',
        average_fill_price: '0.400000000000000000',
        notional: '1.600000000000000000',
        position_cost_basis: '2.300000000000000000',
        realized_pnl: '-0.700000000000000000',
        cash_before: '497.700000000000000000',
        cash_after: '499.300000000000000000',
        fill_formula_version: 'kalshi-direct-bid-ioc-v1',
      }
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
    Object.defineProperty(navigator, 'locks', {
      configurable: true,
      value: {
        request: async (
          _name: string,
          _options: LockOptions,
          callback: (lock: Lock | null) => Promise<unknown>,
        ) => callback({ name: 'callisto:kalshi-paper:financial-mutation', mode: 'exclusive' } as Lock),
      },
    })
    localStorage.clear()
    recordCalls.length = 0
    recordResult = 'ambiguous'
    resolveRecord = null
    cancellationCalls.length = 0
    cancellationResult = 'ambiguous'
    resolveCancellation = null
    positionExitCalls.length = 0
    positionExitResult = 'ambiguous'
    resolvePositionExit = null
    testRunCalls.length = 0
    testRunResult = 'ambiguous'
    resolveTestRun = null
    testControlCalls.length = 0
    testRunStatus = 'monitoring'
  })

  afterEach(() => {
    cleanup()
  })

  it('replays the exact immutable payload for a non-default account after a simulated browser reload', async () => {
    const user = userEvent.setup()
    const first = mount()

    const select = await screen.findByRole('combobox', { name: 'Paper account' })
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

  it('replays a pre-upgrade IOC payload without changing its canonical identity', async () => {
    const legacyPayload = {
      account_id: 'paper-secondary',
      decision_id: 'paper:legacy-ioc',
      opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64),
      action: 'execute' as const,
      quantity: '1.00',
      limit_price: '0.500000',
    }
    localStorage.setItem('callisto:kalshi-paper:pending-attempt', JSON.stringify(legacyPayload))
    recordResult = 'success'

    const user = userEvent.setup()
    mount()
    await user.click(await screen.findByRole('button', { name: /Retry same immutable decision/ }))
    await waitFor(() => expect(recordCalls.length).toBe(1))
    expect(recordCalls[0]).toEqual(legacyPayload)
  })

  it('quarantines an unreadable stored pending attempt and freezes financial actions', async () => {
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

    const accountSelect = await screen.findByRole('combobox', { name: 'Paper account' })
    expect(localStorage.getItem('callisto:kalshi-paper:pending-attempt')).not.toBeNull()
    expect(accountSelect.hasAttribute('disabled')).toBe(true)
    expect(screen.getByText(/Persisted retry identity cannot be decoded/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Create paper account' }).hasAttribute('disabled')).toBe(true)
    expect(screen.queryByText(/Retry same immutable decision/)).toBeNull()
  })

  it('replays a byte-equivalent cancellation after response loss and remount', async () => {
    const user = userEvent.setup()
    const first = mount()
    const accountSelect = await screen.findByRole('combobox', { name: 'Paper account' })
    await user.selectOptions(accountSelect, 'paper-secondary')
    const cancel = await screen.findByRole('button', { name: 'Cancel remainder' })
    await user.click(cancel)
    await waitFor(() => expect(cancellationCalls.length).toBe(1))

    const firstPayload = cancellationCalls[0]
    expect(firstPayload.account_id).toBe('paper-secondary')
    expect(firstPayload.order_id).toBe(openOrder.order_id)
    expect(JSON.parse(localStorage.getItem('callisto:kalshi-paper:pending-cancellation') as string)).toEqual(firstPayload)
    await screen.findByText(/Cancellation outcome unknown/)
    expect(screen.getByRole('combobox', { name: 'Paper account' }).hasAttribute('disabled')).toBe(true)

    first.unmount()
    cancellationResult = 'success'
    mount()
    const retry = await screen.findByRole('button', { name: /Retry same immutable cancellation/ })
    await user.click(retry)
    await waitFor(() => expect(cancellationCalls.length).toBe(2))
    expect(cancellationCalls[1]).toEqual(firstPayload)
    await waitFor(() => expect(localStorage.getItem('callisto:kalshi-paper:pending-cancellation')).toBeNull())
  })

  it('replays an exact paper SELL IOC after response loss and remount', async () => {
    const user = userEvent.setup()
    const first = mount()
    const accountSelect = await screen.findByRole('combobox', { name: 'Paper account' })
    await user.selectOptions(accountSelect, 'paper-secondary')
    await user.click(await screen.findByRole('button', { name: 'Prepare SELL IOC' }))

    const quantityInput = screen.getByRole('textbox', { name: `SELL quantity ${openPosition.position_id}` })
    const priceInput = screen.getByRole('textbox', { name: `Minimum SELL price ${openPosition.position_id}` })
    await user.clear(quantityInput)
    await user.type(quantityInput, '2.00')
    await user.clear(priceInput)
    await user.type(priceInput, '0.400000')
    await user.click(screen.getByRole('button', { name: 'SELL IOC — PAPER ONLY' }))
    await waitFor(() => expect(positionExitCalls.length).toBe(1))

    const firstPayload = positionExitCalls[0]
    expect(firstPayload).toMatchObject({
      account_id: 'paper-secondary',
      position_id: openPosition.position_id,
      quantity: '2.00',
      minimum_price: '0.400000',
    })
    expect(JSON.parse(localStorage.getItem('callisto:kalshi-paper:pending-position-exit') as string)).toEqual(firstPayload)
    await screen.findByText(/SELL IOC outcome unknown/)
    expect(screen.getByRole('combobox', { name: 'Paper account' }).hasAttribute('disabled')).toBe(true)

    first.unmount()
    positionExitResult = 'success'
    mount()
    await user.click(await screen.findByRole('button', { name: /Retry same immutable SELL IOC/ }))
    await waitFor(() => expect(positionExitCalls.length).toBe(2))
    expect(positionExitCalls[1]).toEqual(firstPayload)
    await waitFor(() => expect(localStorage.getItem('callisto:kalshi-paper:pending-position-exit')).toBeNull())
  })

  it('quarantines malformed pending SELL identity without deleting it', async () => {
    localStorage.setItem('callisto:kalshi-paper:pending-position-exit', JSON.stringify({
      account_id: 'paper-secondary',
      position_id: openPosition.position_id,
      decision_id: 'paper-exit:stale',
      quantity: 2,
      minimum_price: '0.400000',
    }))
    mount()
    const accountSelect = await screen.findByRole('combobox', { name: 'Paper account' })
    expect(accountSelect.hasAttribute('disabled')).toBe(true)
    expect(localStorage.getItem('callisto:kalshi-paper:pending-position-exit')).not.toBeNull()
    expect(screen.getByText(/Persisted retry identity cannot be decoded/)).toBeTruthy()
  })

  it('persists and explicitly replays one byte-equivalent paper test run after response loss and remount', async () => {
    const user = userEvent.setup()
    const first = mount()
    const accountSelect = await screen.findByRole('combobox', { name: 'Paper account' })
    await user.selectOptions(accountSelect, 'paper-secondary')
    await user.type(screen.getByPlaceholderText('Opportunity ID or stable ID'), 'opp-live')
    await user.click(screen.getByRole('button', { name: 'Validate' }))
    await screen.findByText('KXTEST-26')

    await user.clear(screen.getByRole('textbox', { name: 'Test quantity' }))
    await user.type(screen.getByRole('textbox', { name: 'Test quantity' }), '2.00')
    await user.clear(screen.getByRole('textbox', { name: 'Entry limit price' }))
    await user.type(screen.getByRole('textbox', { name: 'Entry limit price' }), '0.600000')
    await user.clear(screen.getByRole('textbox', { name: 'Take profit bid' }))
    await user.type(screen.getByRole('textbox', { name: 'Take profit bid' }), '0.700000')
    await user.clear(screen.getByRole('textbox', { name: 'Stop loss trigger bid' }))
    await user.type(screen.getByRole('textbox', { name: 'Stop loss trigger bid' }), '0.400000')
    await user.clear(screen.getByRole('textbox', { name: 'Stop loss minimum bid' }))
    await user.type(screen.getByRole('textbox', { name: 'Stop loss minimum bid' }), '0.300000')
    await user.click(screen.getByRole('button', { name: 'Start paper test run' }))
    await waitFor(() => expect(testRunCalls.length).toBe(1))

    const firstPayload = testRunCalls[0]
    const firstBytes = JSON.stringify(firstPayload)
    expect(firstPayload).toMatchObject({
      account_id: 'paper-secondary',
      opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64),
      quantity: '2.00',
      entry_limit_price: '0.600000',
      take_profit_price: '0.700000',
      stop_loss_price: '0.400000',
      stop_loss_minimum_price: '0.300000',
    })
    expect(firstPayload.run_id).toMatch(/^paper-test-run:/)
    expect(localStorage.getItem('callisto:kalshi-paper:pending-test-run')).toBe(firstBytes)
    expect(screen.getByRole('combobox', { name: 'Paper account' }).hasAttribute('disabled')).toBe(true)
    expect(screen.getByPlaceholderText('Opportunity ID or stable ID').hasAttribute('disabled')).toBe(true)
    expect(screen.getByRole('textbox', { name: 'Test quantity' }).hasAttribute('disabled')).toBe(true)
    await screen.findByText(/Test-run start outcome unknown/)

    first.unmount()
    testRunResult = 'success'
    mount()
    await screen.findByRole('button', { name: 'Retry same immutable test run' })
    expect(testRunCalls.length).toBe(1)
    await user.click(screen.getByRole('button', { name: 'Retry same immutable test run' }))
    await waitFor(() => expect(testRunCalls.length).toBe(2))
    expect(JSON.stringify(testRunCalls[1])).toBe(firstBytes)
    await waitFor(() => expect(localStorage.getItem('callisto:kalshi-paper:pending-test-run')).toBeNull())
  })

  it('retains a pending test run on a shape-valid but identity-mismatched acknowledgement', async () => {
    const pending: KalshiPaperTestRunInput = {
      run_id: 'paper-test-run:mismatch',
      account_id: 'paper-secondary',
      opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64),
      quantity: '2.00',
      entry_limit_price: '0.600000',
      take_profit_price: '0.700000',
      stop_loss_price: '0.400000',
      stop_loss_minimum_price: '0.300000',
    }
    localStorage.setItem('callisto:kalshi-paper:pending-test-run', JSON.stringify(pending))
    testRunResult = 'mismatch'
    const user = userEvent.setup()
    mount()
    await user.click(await screen.findByRole('button', { name: 'Retry same immutable test run' }))
    await screen.findByText(/response identity mismatch/)
    expect(localStorage.getItem('callisto:kalshi-paper:pending-test-run')).toBe(JSON.stringify(pending))
  })

  it('quarantines a persisted test run whose entry is outside its stop and take-profit thresholds', async () => {
    localStorage.setItem('callisto:kalshi-paper:pending-test-run', JSON.stringify({
      run_id: 'paper-test-run:inverted-thresholds',
      account_id: 'paper-secondary',
      opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64),
      quantity: '2.00',
      entry_limit_price: '0.800000',
      take_profit_price: '0.700000',
      stop_loss_price: '0.400000',
      stop_loss_minimum_price: '0.300000',
    }))

    mount()

    expect((await screen.findByRole('combobox', { name: 'Paper account' })).hasAttribute('disabled')).toBe(true)
    expect(screen.getByText(/Persisted retry identity cannot be decoded/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Retry same immutable test run' })).toBeNull()
    expect(localStorage.getItem('callisto:kalshi-paper:pending-test-run')).not.toBeNull()
  })

  it('quarantines malformed test-run cache and synchronizes valid pending identity from another tab without replay', async () => {
    localStorage.setItem('callisto:kalshi-paper:pending-test-run', JSON.stringify({
      run_id: 'paper-test-run:bad',
      account_id: 'paper-secondary',
      quantity: 2,
    }))
    const first = mount()
    expect((await screen.findByRole('combobox', { name: 'Paper account' })).hasAttribute('disabled')).toBe(true)
    expect(screen.getByText(/Persisted retry identity cannot be decoded/)).toBeTruthy()
    expect(localStorage.getItem('callisto:kalshi-paper:pending-test-run')).not.toBeNull()
    first.unmount()

    localStorage.removeItem('callisto:kalshi-paper:pending-test-run')
    mount()
    const pending: KalshiPaperTestRunInput = {
      run_id: 'paper-test-run:other-tab',
      account_id: 'paper-secondary',
      opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64),
      quantity: '2.00',
      entry_limit_price: '0.600000',
      take_profit_price: '0.700000',
      stop_loss_price: '0.400000',
      stop_loss_minimum_price: '0.300000',
    }
    localStorage.setItem('callisto:kalshi-paper:pending-test-run', JSON.stringify(pending))
    window.dispatchEvent(new StorageEvent('storage', {
      key: 'callisto:kalshi-paper:pending-test-run',
      newValue: JSON.stringify(pending),
    }))
    await screen.findByRole('button', { name: 'Retry same immutable test run' })
    expect(testRunCalls).toHaveLength(0)
    expect(screen.getByRole('combobox', { name: 'Paper account' }).hasAttribute('disabled')).toBe(true)

    localStorage.removeItem('callisto:kalshi-paper:pending-test-run')
    window.dispatchEvent(new StorageEvent('storage', {
      key: 'callisto:kalshi-paper:pending-test-run',
      oldValue: JSON.stringify(pending),
      newValue: null,
    }))
    const user = userEvent.setup()
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Paper account' }).hasAttribute('disabled')).toBe(false))
    await user.click(screen.getByRole('button', { name: 'Validate' }))
    await screen.findByText('KXTEST-26')
    await user.click(screen.getByRole('button', { name: 'Start paper test run' }))
    await waitFor(() => expect(testRunCalls).toHaveLength(1))
    expect(testRunCalls[0].run_id).not.toBe(pending.run_id)
  })

  it('reactively freezes two mounted instances for every valid pending namespace without replaying', async () => {
    const pendingDecision: KalshiPaperDecisionInput = {
      account_id: 'paper-secondary',
      decision_id: 'paper:other-tab',
      opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64),
      action: 'execute',
      quantity: '1.00',
      limit_price: '0.500000',
      time_in_force: 'immediate_or_cancel',
    }
    const pendingCancellation: KalshiPaperCancellationInput = {
      account_id: 'paper-secondary',
      order_id: openOrder.order_id,
      cancellation_id: 'paper-cancel:other-tab',
    }
    const pendingExit: KalshiPaperPositionExitInput = {
      account_id: 'paper-secondary',
      position_id: openPosition.position_id,
      decision_id: 'paper-exit:other-tab',
      quantity: '2.00',
      minimum_price: '0.400000',
    }
    const pendingRun: KalshiPaperTestRunInput = {
      run_id: 'paper-test-run:other-tab-all',
      account_id: 'paper-secondary',
      opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64),
      quantity: '2.00',
      entry_limit_price: '0.600000',
      take_profit_price: '0.700000',
      stop_loss_price: '0.400000',
      stop_loss_minimum_price: '0.300000',
    }
    const cases = [
      ['callisto:kalshi-paper:pending-attempt', pendingDecision, /Retry same immutable decision/],
      ['callisto:kalshi-paper:pending-cancellation', pendingCancellation, /Retry same immutable cancellation/],
      ['callisto:kalshi-paper:pending-position-exit', pendingExit, /Retry same immutable SELL IOC/],
      ['callisto:kalshi-paper:pending-test-run', pendingRun, /Retry same immutable test run/],
    ] as const

    for (const [key, payload, retryName] of cases) {
      cleanup()
      localStorage.clear()
      const first = mount()
      const second = mount()
      await within(first.container).findByRole('combobox', { name: 'Paper account' })
      await within(second.container).findByRole('combobox', { name: 'Paper account' })
      const serialized = JSON.stringify(payload)
      act(() => {
        localStorage.setItem(key, serialized)
        window.dispatchEvent(new StorageEvent('storage', { key, newValue: serialized }))
      })
      await within(first.container).findByRole('button', { name: retryName })
      await within(second.container).findByRole('button', { name: retryName })
      expect(within(first.container).getByRole('combobox', { name: 'Paper account' }).hasAttribute('disabled')).toBe(true)
      expect(within(second.container).getByRole('combobox', { name: 'Paper account' }).hasAttribute('disabled')).toBe(true)
      expect(recordCalls).toHaveLength(0)
      expect(cancellationCalls).toHaveLength(0)
      expect(positionExitCalls).toHaveLength(0)
      expect(testRunCalls).toHaveLength(0)

      act(() => {
        localStorage.removeItem(key)
        window.dispatchEvent(new StorageEvent('storage', { key, oldValue: serialized, newValue: null }))
      })
      await waitFor(() => {
        expect(within(first.container).getByRole('combobox', { name: 'Paper account' }).hasAttribute('disabled')).toBe(false)
        expect(within(second.container).getByRole('combobox', { name: 'Paper account' }).hasAttribute('disabled')).toBe(false)
      })
    }
  })

  it('reactively quarantines malformed bytes in every pending namespace and retains them', async () => {
    const keys = [
      'callisto:kalshi-paper:pending-attempt',
      'callisto:kalshi-paper:pending-cancellation',
      'callisto:kalshi-paper:pending-position-exit',
      'callisto:kalshi-paper:pending-test-run',
    ]
    for (const key of keys) {
      cleanup()
      localStorage.clear()
      const mounted = mount()
      const serialized = JSON.stringify({ account_id: 'paper-secondary', malformed: key })
      act(() => {
        localStorage.setItem(key, serialized)
        window.dispatchEvent(new StorageEvent('storage', { key, newValue: serialized }))
      })
      expect(await within(mounted.container).findByText(/Persisted retry identity cannot be decoded/)).toBeTruthy()
      expect(within(mounted.container).getByRole('button', { name: 'Create paper account' }).hasAttribute('disabled')).toBe(true)
      expect(localStorage.getItem(key)).toBe(serialized)
    }
  })

  it('never lets stale success callbacks clear or overwrite newer pending identities', async () => {
    const user = userEvent.setup()

    const oldDecision: KalshiPaperDecisionInput = {
      account_id: 'paper-secondary', decision_id: 'paper:old', opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64), action: 'execute', quantity: '1.00',
      limit_price: '0.500000', time_in_force: 'immediate_or_cancel',
    }
    localStorage.setItem('callisto:kalshi-paper:pending-attempt', JSON.stringify(oldDecision))
    recordResult = 'deferred'
    let mounted = mount()
    await user.click(await screen.findByRole('button', { name: /Retry same immutable decision/ }))
    await waitFor(() => expect(resolveRecord).not.toBeNull())
    const newerDecision = { ...oldDecision, account_id: 'paper-default', decision_id: 'paper:newer' }
    const newerDecisionBytes = JSON.stringify(newerDecision)
    act(() => {
      localStorage.setItem('callisto:kalshi-paper:pending-attempt', newerDecisionBytes)
      window.dispatchEvent(new StorageEvent('storage', { key: 'callisto:kalshi-paper:pending-attempt', newValue: newerDecisionBytes }))
      resolveRecord?.({ ...successDecision, account_id: oldDecision.account_id, decision_id: oldDecision.decision_id })
    })
    await screen.findByRole('button', { name: /Retry same immutable decision/ })
    expect(localStorage.getItem('callisto:kalshi-paper:pending-attempt')).toBe(newerDecisionBytes)
    mounted.unmount()

    const staleCases = [
      {
        key: 'callisto:kalshi-paper:pending-cancellation',
        old: { account_id: 'paper-secondary', order_id: openOrder.order_id, cancellation_id: 'paper-cancel:old' },
        newer: { account_id: 'paper-default', order_id: 'paper-order:newer', cancellation_id: 'paper-cancel:newer' },
        retry: /Retry same immutable cancellation/,
        defer: () => { cancellationResult = 'deferred' },
        ready: () => resolveCancellation,
        resolve: () => resolveCancellation?.({}),
      },
      {
        key: 'callisto:kalshi-paper:pending-position-exit',
        old: { account_id: 'paper-secondary', position_id: openPosition.position_id, decision_id: 'paper-exit:old', quantity: '2.00', minimum_price: '0.400000' },
        newer: { account_id: 'paper-default', position_id: 'paper-position:newer', decision_id: 'paper-exit:newer', quantity: '1.00', minimum_price: '0.300000' },
        retry: /Retry same immutable SELL IOC/,
        defer: () => { positionExitResult = 'deferred' },
        ready: () => resolvePositionExit,
        resolve: () => resolvePositionExit?.({ ...successDecision, decision_id: 'paper-exit:old' }),
      },
      {
        key: 'callisto:kalshi-paper:pending-test-run',
        old: { run_id: 'paper-test-run:old', account_id: 'paper-secondary', opportunity_id: 'opp-live', opportunity_revision: 'a'.repeat(64), quantity: '2.00', entry_limit_price: '0.600000', take_profit_price: '0.700000', stop_loss_price: '0.400000', stop_loss_minimum_price: '0.300000' },
        newer: { run_id: 'paper-test-run:newer', account_id: 'paper-default', opportunity_id: 'opp-live', opportunity_revision: 'a'.repeat(64), quantity: '1.00', entry_limit_price: '0.500000', take_profit_price: '0.800000', stop_loss_price: '0.300000', stop_loss_minimum_price: '0.200000' },
        retry: /Retry same immutable test run/,
        defer: () => { testRunResult = 'deferred' },
        ready: () => resolveTestRun,
        resolve: () => resolveTestRun?.({ ...testRunDetail, run: { ...testRun, run_id: 'paper-test-run:old' } }),
      },
    ] as const

    for (const stale of staleCases) {
      cleanup()
      localStorage.clear()
      stale.defer()
      localStorage.setItem(stale.key, JSON.stringify(stale.old))
      mounted = mount()
      await user.click(await within(mounted.container).findByRole('button', { name: stale.retry }))
      await waitFor(() => expect(stale.ready()).not.toBeNull())
      const newerBytes = JSON.stringify(stale.newer)
      act(() => {
        localStorage.setItem(stale.key, newerBytes)
        window.dispatchEvent(new StorageEvent('storage', { key: stale.key, newValue: newerBytes }))
        stale.resolve()
      })
      await waitFor(() => expect(localStorage.getItem(stale.key)).toBe(newerBytes))
    }
  })

  it('prevents a stale mounted account from overwriting a newer cross-account identity before submit', async () => {
    const original: KalshiPaperDecisionInput = {
      account_id: 'paper-secondary', decision_id: 'paper:original', opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64), action: 'execute', quantity: '1.00',
      limit_price: '0.500000', time_in_force: 'immediate_or_cancel',
    }
    localStorage.setItem('callisto:kalshi-paper:pending-attempt', JSON.stringify(original))
    mount()
    const retry = await screen.findByRole('button', { name: /Retry same immutable decision/ })
    const newer = { ...original, account_id: 'paper-default', decision_id: 'paper:newer-account' }
    const newerBytes = JSON.stringify(newer)
    localStorage.setItem('callisto:kalshi-paper:pending-attempt', newerBytes)
    await userEvent.setup().click(retry)
    await screen.findByText(/Persisted paper mutation identity changed/)
    expect(recordCalls).toHaveLength(0)
    expect(localStorage.getItem('callisto:kalshi-paper:pending-attempt')).toBe(newerBytes)
  })

  it('ignores a delayed deletion event when newer pending bytes still exist', async () => {
    const original: KalshiPaperDecisionInput = {
      account_id: 'paper-secondary', decision_id: 'paper:old-delete', opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64), action: 'execute', quantity: '1.00',
      limit_price: '0.500000', time_in_force: 'immediate_or_cancel',
    }
    const newer = { ...original, decision_id: 'paper:new-delete' }
    const originalBytes = JSON.stringify(original)
    const newerBytes = JSON.stringify(newer)
    localStorage.setItem('callisto:kalshi-paper:pending-attempt', originalBytes)
    mount()
    act(() => {
      localStorage.setItem('callisto:kalshi-paper:pending-attempt', newerBytes)
      window.dispatchEvent(new StorageEvent('storage', {
        key: 'callisto:kalshi-paper:pending-attempt',
        oldValue: originalBytes,
        newValue: null,
      }))
    })
    expect(await screen.findByRole('button', { name: /Retry same immutable decision/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Create paper account' }).hasAttribute('disabled')).toBe(true)
    expect(localStorage.getItem('callisto:kalshi-paper:pending-attempt')).toBe(newerBytes)
  })

  it('fails closed when another tab owns the financial mutation lock', async () => {
    const pending: KalshiPaperDecisionInput = {
      account_id: 'paper-secondary', decision_id: 'paper:locked', opportunity_id: 'opp-live',
      opportunity_revision: 'a'.repeat(64), action: 'execute', quantity: '1.00',
      limit_price: '0.500000', time_in_force: 'immediate_or_cancel',
    }
    localStorage.setItem('callisto:kalshi-paper:pending-attempt', JSON.stringify(pending))
    Object.defineProperty(navigator, 'locks', {
      configurable: true,
      value: {
        request: async (
          _name: string,
          _options: LockOptions,
          callback: (lock: Lock | null) => Promise<unknown>,
        ) => callback(null),
      },
    })
    mount()
    await userEvent.setup().click(await screen.findByRole('button', { name: /Retry same immutable decision/ }))
    await screen.findByText(/Another tab is starting a paper financial mutation/)
    expect(recordCalls).toHaveLength(0)
  })

  it('canonicalizes new decision decimals before persistence and submission', async () => {
    const user = userEvent.setup()
    mount()
    await user.type(await screen.findByPlaceholderText('Opportunity ID or stable ID'), 'opp-live')
    await user.click(screen.getByRole('button', { name: 'Validate' }))
    await screen.findByText('KXTEST-26')
    const quantityInput = screen.getByLabelText('Quantity')
    const priceInput = screen.getByLabelText('Maximum price')
    await user.clear(quantityInput)
    await user.type(quantityInput, '1')
    await user.clear(priceInput)
    await user.type(priceInput, '0.5')
    await user.click(screen.getByRole('button', { name: /Simulate buy IOC/ }))
    await waitFor(() => expect(recordCalls).toHaveLength(1))
    expect(recordCalls[0].quantity).toBe('1.00')
    expect(recordCalls[0].limit_price).toBe('0.500000')
    expect(localStorage.getItem('callisto:kalshi-paper:pending-attempt')).toBe(JSON.stringify(recordCalls[0]))
  })

  it('renders authoritative events and invokes pause, resume, and stop only on operator clicks', async () => {
    const user = userEvent.setup()
    mount()
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Paper account' }), 'paper-secondary')
    await waitFor(() => expect(screen.getAllByText('entry filled').length).toBeGreaterThan(0))
    expect(screen.getByText(/Stop leaves any residual paper position open/)).toBeTruthy()
    expect(testControlCalls).toHaveLength(0)

    await user.click(await screen.findByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(testControlCalls).toEqual([{ runId: testRun.run_id, action: 'pause' }]))
    await user.click(await screen.findByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(testControlCalls).toHaveLength(2))
    expect(testControlCalls[1]).toEqual({ runId: testRun.run_id, action: 'resume' })
    await user.click(await screen.findByRole('button', { name: 'Stop monitoring' }))
    await waitFor(() => expect(testControlCalls).toHaveLength(3))
    expect(testControlCalls[2]).toEqual({ runId: testRun.run_id, action: 'stop' })
  })
})
