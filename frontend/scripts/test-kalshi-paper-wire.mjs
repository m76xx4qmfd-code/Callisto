import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { api } from '../src/services/apiClient.ts'
import {
  cancelKalshiPaperOrder,
  getKalshiPaperDecisions,
  getKalshiPaperOrders,
  recordKalshiPaperDecision,
  requireKalshiPaperAccount,
  requireKalshiPaperCancellation,
  requireKalshiPaperCancellationInput,
  requireKalshiPaperDecision,
  requireKalshiPaperDecisionInput,
  requireKalshiPaperEligibility,
  requireKalshiPaperOrder,
} from '../src/services/apiKalshiPaper.ts'

const account = {
  id: 'account',
  name: 'Paper',
  currency: 'USD',
  starting_cash: '100.000000000000000000',
  cash_balance: '97.700000000000000000',
  reserved_cash: '0.600000000000000000',
  available_cash: '97.100000000000000000',
  journal_sequence: 1,
  created_at: '2026-08-05T12:00:00Z',
  updated_at: '2026-08-05T12:00:00Z',
}
const decision = {
  account_id: 'account',
  decision_id: 'decision',
  account_sequence: 1,
  action: 'execute',
  opportunity_id: 'opportunity',
  opportunity_stable_id: 'stable',
  opportunity_revision: 'a'.repeat(64),
  strategy_key: 'news_edge',
  strategy_version: 7,
  ticker: 'KXTEST-26',
  event_ticker: 'KXTEST',
  outcome: 'yes',
  order_side: 'buy',
  time_in_force: 'immediate_or_cancel',
  requested_quantity: '4.00',
  limit_price: '0.600000',
  status: 'filled',
  reason: 'displayed_depth_filled_ioc',
  filled_quantity: '4.00',
  remaining_quantity: '0.00',
  average_fill_price: '0.575000000000000000',
  notional: '2.30000000',
  fee: '0.000000000000000000',
  cash_before: '100.000000000000000000',
  cash_after: '97.700000000000000000',
  order_id: null,
  reserved_cash: '0.000000000000000000',
  fill_formula_version: 'kalshi-complementary-depth-ioc-v1',
  fee_rule_version: 'kalshi-market-fee-waiver-v1',
  fee_provenance: {},
  market_evidence_hash: 'b'.repeat(64),
  book_evidence_hash: 'c'.repeat(64),
  opportunity_snapshot: {},
  fills: [{
    sequence: 1,
    quantity: '4.00',
    price: '0.575000',
    notional: '2.30000000',
    fee: '0.000000000000000000',
    source_bid_price: '0.425000',
    source_side: 'no',
  }],
  created_at: '2026-08-05T12:00:00Z',
}
const eligibility = {
  opportunity_id: 'opportunity',
  opportunity_stable_id: 'stable',
  opportunity_revision: 'a'.repeat(64),
  strategy_key: 'news_edge',
  strategy_version: 7,
  ticker: 'KXTEST-26',
  outcome: 'yes',
  order_side: 'buy',
  time_in_force: 'immediate_or_cancel',
}
const pendingAttempt = {
  account_id: 'account',
  decision_id: 'decision',
  opportunity_id: 'opportunity',
  opportunity_revision: 'a'.repeat(64),
  action: 'execute',
  quantity: '4.00',
  limit_price: '0.600000',
  time_in_force: 'immediate_or_cancel',
}
const order = {
  account_id: 'account',
  order_id: 'paper-order:abc',
  decision_id: 'decision-gtc',
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
const cancellationInput = {
  account_id: 'account',
  order_id: 'paper-order:abc',
  cancellation_id: 'paper-cancel:stable',
}
const cancellation = {
  ...cancellationInput,
  status: 'cancelled',
  released_cash: '0.600000000000000000',
  created_at: '2026-08-05T12:01:00Z',
}

assert.equal(requireKalshiPaperAccount(account).cash_balance, '97.700000000000000000')
assert.equal(requireKalshiPaperDecision(decision).fills[0].notional, '2.30000000')
assert.equal(requireKalshiPaperEligibility(eligibility).order_side, 'buy')
assert.deepEqual(requireKalshiPaperDecisionInput(pendingAttempt), pendingAttempt)
const legacyPendingAttempt = { ...pendingAttempt }
delete legacyPendingAttempt.time_in_force
assert.deepEqual(requireKalshiPaperDecisionInput(legacyPendingAttempt), legacyPendingAttempt)
assert.equal(requireKalshiPaperOrder(order).cancelable, true)
assert.deepEqual(requireKalshiPaperCancellationInput(cancellationInput), cancellationInput)
assert.equal(requireKalshiPaperCancellation(cancellation).released_cash, '0.600000000000000000')
assert.throws(() => requireKalshiPaperAccount({ ...account, cash_balance: 97.7 }), /text is required|exact decimal/i)
assert.throws(() => requireKalshiPaperAccount({ ...account, currency: 'EUR' }), /enum/i)
assert.throws(() => requireKalshiPaperDecision({ ...decision, requested_quantity: 4 }), /text or null/i)
assert.throws(() => requireKalshiPaperDecision({ ...decision, status: 'unknown' }), /enum/i)
assert.throws(() => requireKalshiPaperDecision({ ...decision, reason: null }), /text is required/i)
assert.throws(() => requireKalshiPaperDecision({ ...decision, book_evidence_hash: 'bad' }), /sha-256/i)
assert.throws(() => requireKalshiPaperEligibility({ ...eligibility, order_side: 'sell' }), /enum/i)
assert.throws(() => requireKalshiPaperDecisionInput({ ...pendingAttempt, action: 'pass' }), /cannot include/i)
assert.throws(() => requireKalshiPaperDecisionInput({ ...pendingAttempt, unexpected: 'value' }), /unknown field/i)
assert.throws(() => requireKalshiPaperOrder({ ...order, later_matching_supported: true }), /later matching/i)
assert.throws(() => requireKalshiPaperOrder({ ...order, status: 'cancelled' }), /contradictory/i)
assert.throws(() => requireKalshiPaperOrder({ ...order, open_quantity: 1 }), /text is required|exact decimal/i)
assert.throws(() => requireKalshiPaperCancellationInput({ ...cancellationInput, extra: true }), /unknown field/i)
assert.throws(
  () => requireKalshiPaperDecision({ ...decision, fills: [{ ...decision.fills[0], price: 0.575 }] }),
  /text is required|exact decimal/i,
)

const originalPost = api.post
api.post = async () => ({ data: { ...cancellation, order_id: 'paper-order:different' } })
await assert.rejects(() => cancelKalshiPaperOrder(cancellationInput), /identity mismatch/i)
api.post = originalPost

const originalGet = api.get
api.get = async () => ({ data: [{ ...order, account_id: 'foreign-account' }] })
await assert.rejects(() => getKalshiPaperOrders('account'), /account identity mismatch/i)
api.get = async () => ({ data: [{ ...decision, account_id: 'foreign-account' }] })
await assert.rejects(() => getKalshiPaperDecisions('account'), /account identity mismatch/i)
api.get = originalGet

api.post = async () => ({ data: { ...decision, decision_id: 'different-decision' } })
await assert.rejects(() => recordKalshiPaperDecision(pendingAttempt), /identity mismatch/i)
api.post = originalPost

const panelSource = readFileSync(new URL('../src/components/KalshiPaperPanel.tsx', import.meta.url), 'utf8')
const accountSelectTag = panelSource.match(/<select[\s\S]*?>/)?.[0] ?? ''
const createAccountLabelIndex = panelSource.indexOf('Create paper account')
const createAccountTag = panelSource.slice(panelSource.lastIndexOf('<Button', createAccountLabelIndex), createAccountLabelIndex)
assert.match(accountSelectTag, /disabled=\{inputFrozen\}/)
assert.match(createAccountTag, /disabled=\{inputFrozen \|\|/)
assert.match(panelSource, /callisto:kalshi-paper:pending-attempt/)
assert.match(panelSource, /callisto:kalshi-paper:pending-cancellation/)
assert.match(panelSource, /localStorage\.setItem\(PENDING_ATTEMPT_KEY, JSON\.stringify\(payload\)\)/)
assert.match(panelSource, /return recordKalshiPaperDecision\(payload\)/)
assert.match(panelSource, /Displayed cash is cached and new decisions are disabled/)
assert.match(panelSource, /disabled=\{pendingCancellation !== null \|\| recordDecision\.isPending \|\| !lastDecision\}/)
assert.match(panelSource, /Retry same immutable decision/)
assert.match(panelSource, /Retry same immutable cancellation/)
assert.match(panelSource, /Order refresh failed\. Cached orders are hidden and cancellation is disabled/)

console.log('Kalshi paper exact-wire validation passed')
