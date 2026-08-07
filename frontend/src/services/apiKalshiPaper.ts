import { api, unwrapApiData } from './apiClient'

export interface KalshiPaperAccount {
  id: string
  name: string
  currency: 'USD'
  starting_cash: string
  cash_balance: string
  reserved_cash: string
  available_cash: string
  journal_sequence: number
  created_at: string
  updated_at: string
}

export interface KalshiPaperEligibility {
  opportunity_id: string
  opportunity_stable_id: string
  opportunity_revision: string
  strategy_key: string
  strategy_version: number | null
  ticker: string
  outcome: 'yes' | 'no'
  order_side: 'buy'
  time_in_force: 'immediate_or_cancel'
}

export interface KalshiPaperFill {
  sequence: number
  quantity: string
  price: string
  notional: string
  fee: string
  source_bid_price: string
  source_side: 'yes' | 'no'
}

export interface KalshiPaperDecision {
  account_id: string
  decision_id: string
  account_sequence: number
  action: 'execute' | 'pass'
  opportunity_id: string
  opportunity_stable_id: string
  opportunity_revision: string
  strategy_key: string
  strategy_version: number | null
  ticker: string
  event_ticker: string | null
  outcome: 'yes' | 'no'
  order_side: 'buy' | 'sell' | null
  position_id: string | null
  time_in_force: 'immediate_or_cancel' | 'good_till_canceled' | null
  requested_quantity: string | null
  limit_price: string | null
  status: 'filled' | 'partial' | 'no_fill' | 'passed' | 'rejected'
  reason: string
  filled_quantity: string
  remaining_quantity: string
  average_fill_price: string | null
  notional: string
  fee: string
  position_cost_basis: string | null
  realized_pnl: string | null
  cash_before: string
  cash_after: string
  order_id: string | null
  reserved_cash: string
  fill_formula_version: string
  fee_rule_version: string
  fee_provenance: Record<string, unknown>
  market_evidence_hash: string | null
  book_evidence_hash: string | null
  opportunity_snapshot: Record<string, unknown>
  fills: KalshiPaperFill[]
  created_at: string
}

export interface KalshiPaperDecisionInput {
  account_id: string
  decision_id: string
  opportunity_id: string
  opportunity_revision: string
  action: 'execute' | 'pass'
  quantity?: string
  limit_price?: string
  time_in_force?: 'immediate_or_cancel' | 'good_till_canceled'
}

export interface KalshiPaperOrder {
  account_id: string
  order_id: string
  decision_id: string
  ticker: string
  outcome: 'yes' | 'no'
  side: 'buy'
  time_in_force: 'good_till_canceled'
  requested_quantity: string
  filled_quantity: string
  open_quantity: string
  limit_price: string
  reserved_cash: string
  cancelable: boolean
  status: 'open' | 'cancelled' | 'filled'
  cancellation_id: string | null
  later_matching_supported: false
  created_at: string
}

export interface KalshiPaperCancellationInput {
  account_id: string
  order_id: string
  cancellation_id: string
}

export interface KalshiPaperCancellation extends KalshiPaperCancellationInput {
  status: 'cancelled'
  released_cash: string
  created_at: string
}

export interface KalshiPaperPosition {
  account_id: string
  position_id: string
  entry_decision_id: string
  ticker: string
  outcome: 'yes' | 'no'
  entry_quantity: string
  entry_notional: string
  entry_fee: string
  sold_quantity: string
  remaining_quantity: string
  exit_notional: string
  exit_fee: string
  allocated_entry_cost: string
  realized_pnl: string
  status: 'open' | 'closed'
  closable: boolean
  exit_decision_ids: string[]
  created_at: string
}

export interface KalshiPaperPositionExitInput {
  account_id: string
  position_id: string
  decision_id: string
  quantity: string
  minimum_price: string
}

const DECIMAL_PATTERN = /^(?:0|[1-9]\d*)(?:\.\d+)?$/
const SIGNED_DECIMAL_PATTERN = /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/
const SHA256_PATTERN = /^[0-9a-f]{64}$/

function requireObject(value: unknown, path: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Invalid Kalshi paper payload at ${path}`)
  }
  return value as Record<string, unknown>
}

function requireTextValue(record: Record<string, unknown>, key: string, path: string): string {
  const value = record[key]
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`Kalshi paper text is required at ${path}.${key}`)
  }
  return value
}

function requireNullableTextValue(record: Record<string, unknown>, key: string, path: string): string | null {
  const value = record[key]
  if (value !== null && typeof value !== 'string') {
    throw new Error(`Kalshi paper value must be text or null at ${path}.${key}`)
  }
  return value
}

function requireDecimalText(record: Record<string, unknown>, key: string, path: string): string {
  const value = requireTextValue(record, key, path)
  if (!DECIMAL_PATTERN.test(value)) {
    throw new Error(`Kalshi paper exact decimal is invalid at ${path}.${key}`)
  }
  return value
}

function requireNullableDecimalText(record: Record<string, unknown>, key: string, path: string): string | null {
  const value = requireNullableTextValue(record, key, path)
  if (value !== null && !DECIMAL_PATTERN.test(value)) {
    throw new Error(`Kalshi paper exact decimal is invalid at ${path}.${key}`)
  }
  return value
}

function requireSignedDecimalText(record: Record<string, unknown>, key: string, path: string): string {
  const value = requireTextValue(record, key, path)
  if (!SIGNED_DECIMAL_PATTERN.test(value)) {
    throw new Error(`Kalshi paper signed exact decimal is invalid at ${path}.${key}`)
  }
  return value
}

function requireNullableSignedDecimalText(
  record: Record<string, unknown>,
  key: string,
  path: string,
): string | null {
  const value = requireNullableTextValue(record, key, path)
  if (value !== null && !SIGNED_DECIMAL_PATTERN.test(value)) {
    throw new Error(`Kalshi paper signed exact decimal is invalid at ${path}.${key}`)
  }
  return value
}

function requireInteger(record: Record<string, unknown>, key: string, path: string): number {
  const value = record[key]
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) {
    throw new Error(`Kalshi paper integer is required at ${path}.${key}`)
  }
  return value
}

function requireBoolean(record: Record<string, unknown>, key: string, path: string): boolean {
  const value = record[key]
  if (typeof value !== 'boolean') throw new Error(`Kalshi paper boolean is required at ${path}.${key}`)
  return value
}

function rejectUnknownFields(record: Record<string, unknown>, allowed: readonly string[], path: string): void {
  const unknown = Object.keys(record).find((key) => !allowed.includes(key))
  if (unknown !== undefined) throw new Error(`Unknown Kalshi paper field at ${path}.${unknown}`)
}

function requireNullableInteger(record: Record<string, unknown>, key: string, path: string): number | null {
  const value = record[key]
  if (value !== null && (typeof value !== 'number' || !Number.isSafeInteger(value))) {
    throw new Error(`Kalshi paper integer or null is required at ${path}.${key}`)
  }
  return value
}

function requireEnum<T extends string>(
  record: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
  path: string,
): T {
  const value = record[key]
  if (typeof value !== 'string' || !allowed.includes(value as T)) {
    throw new Error(`Invalid Kalshi paper enum at ${path}.${key}`)
  }
  return value as T
}

function requireNullableEnum<T extends string>(
  record: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
  path: string,
): T | null {
  if (record[key] === null) return null
  return requireEnum(record, key, allowed, path)
}

function requireSha256(record: Record<string, unknown>, key: string, path: string): string {
  const value = requireTextValue(record, key, path)
  if (!SHA256_PATTERN.test(value)) throw new Error(`Invalid SHA-256 at ${path}.${key}`)
  return value
}

function requireNullableSha256(record: Record<string, unknown>, key: string, path: string): string | null {
  const value = requireNullableTextValue(record, key, path)
  if (value !== null && !SHA256_PATTERN.test(value)) throw new Error(`Invalid SHA-256 at ${path}.${key}`)
  return value
}

export function requireKalshiPaperAccount(value: unknown): KalshiPaperAccount {
  const account = requireObject(value, 'account')
  rejectUnknownFields(account, [
    'id', 'name', 'currency', 'starting_cash', 'cash_balance', 'reserved_cash', 'available_cash',
    'journal_sequence', 'created_at', 'updated_at',
  ], 'account')
  requireTextValue(account, 'id', 'account')
  requireTextValue(account, 'name', 'account')
  requireEnum(account, 'currency', ['USD'] as const, 'account')
  requireDecimalText(account, 'starting_cash', 'account')
  requireDecimalText(account, 'cash_balance', 'account')
  requireDecimalText(account, 'reserved_cash', 'account')
  requireDecimalText(account, 'available_cash', 'account')
  requireInteger(account, 'journal_sequence', 'account')
  requireTextValue(account, 'created_at', 'account')
  requireTextValue(account, 'updated_at', 'account')
  return account as unknown as KalshiPaperAccount
}

export function requireKalshiPaperEligibility(value: unknown): KalshiPaperEligibility {
  const eligibility = requireObject(value, 'eligibility')
  rejectUnknownFields(eligibility, [
    'opportunity_id', 'opportunity_stable_id', 'opportunity_revision', 'strategy_key',
    'strategy_version', 'ticker', 'outcome', 'order_side', 'time_in_force',
  ], 'eligibility')
  requireTextValue(eligibility, 'opportunity_id', 'eligibility')
  requireTextValue(eligibility, 'opportunity_stable_id', 'eligibility')
  requireSha256(eligibility, 'opportunity_revision', 'eligibility')
  requireTextValue(eligibility, 'strategy_key', 'eligibility')
  requireNullableInteger(eligibility, 'strategy_version', 'eligibility')
  requireTextValue(eligibility, 'ticker', 'eligibility')
  requireEnum(eligibility, 'outcome', ['yes', 'no'] as const, 'eligibility')
  requireEnum(eligibility, 'order_side', ['buy'] as const, 'eligibility')
  requireEnum(eligibility, 'time_in_force', ['immediate_or_cancel'] as const, 'eligibility')
  return eligibility as unknown as KalshiPaperEligibility
}

function requireKalshiPaperFill(value: unknown, path: string): KalshiPaperFill {
  const fill = requireObject(value, path)
  rejectUnknownFields(fill, [
    'sequence', 'quantity', 'price', 'notional', 'fee', 'source_bid_price', 'source_side',
  ], path)
  requireInteger(fill, 'sequence', path)
  requireDecimalText(fill, 'quantity', path)
  requireDecimalText(fill, 'price', path)
  requireDecimalText(fill, 'notional', path)
  requireDecimalText(fill, 'fee', path)
  requireDecimalText(fill, 'source_bid_price', path)
  requireEnum(fill, 'source_side', ['yes', 'no'] as const, path)
  return fill as unknown as KalshiPaperFill
}

export function requireKalshiPaperDecision(value: unknown): KalshiPaperDecision {
  const decision = requireObject(value, 'decision')
  rejectUnknownFields(decision, [
    'account_id', 'decision_id', 'account_sequence', 'action', 'opportunity_id',
    'opportunity_stable_id', 'opportunity_revision', 'strategy_key', 'strategy_version', 'ticker',
    'event_ticker', 'outcome', 'order_side', 'position_id', 'time_in_force', 'requested_quantity', 'limit_price',
    'status', 'reason', 'filled_quantity', 'remaining_quantity', 'average_fill_price', 'notional',
    'fee', 'position_cost_basis', 'realized_pnl', 'cash_before', 'cash_after', 'order_id', 'reserved_cash',
    'fill_formula_version',
    'fee_rule_version', 'fee_provenance', 'market_evidence_hash', 'book_evidence_hash',
    'opportunity_snapshot', 'fills', 'created_at',
  ], 'decision')
  requireTextValue(decision, 'account_id', 'decision')
  requireTextValue(decision, 'decision_id', 'decision')
  requireInteger(decision, 'account_sequence', 'decision')
  requireEnum(decision, 'action', ['execute', 'pass'] as const, 'decision')
  requireTextValue(decision, 'opportunity_id', 'decision')
  requireTextValue(decision, 'opportunity_stable_id', 'decision')
  requireSha256(decision, 'opportunity_revision', 'decision')
  requireTextValue(decision, 'strategy_key', 'decision')
  requireNullableInteger(decision, 'strategy_version', 'decision')
  requireTextValue(decision, 'ticker', 'decision')
  requireNullableTextValue(decision, 'event_ticker', 'decision')
  requireEnum(decision, 'outcome', ['yes', 'no'] as const, 'decision')
  requireNullableEnum(decision, 'order_side', ['buy', 'sell'] as const, 'decision')
  requireNullableTextValue(decision, 'position_id', 'decision')
  requireNullableEnum(decision, 'time_in_force', ['immediate_or_cancel', 'good_till_canceled'] as const, 'decision')
  requireNullableDecimalText(decision, 'requested_quantity', 'decision')
  requireNullableDecimalText(decision, 'limit_price', 'decision')
  requireEnum(decision, 'status', ['filled', 'partial', 'no_fill', 'passed', 'rejected'] as const, 'decision')
  requireTextValue(decision, 'reason', 'decision')
  requireDecimalText(decision, 'filled_quantity', 'decision')
  requireDecimalText(decision, 'remaining_quantity', 'decision')
  requireNullableDecimalText(decision, 'average_fill_price', 'decision')
  requireDecimalText(decision, 'notional', 'decision')
  requireDecimalText(decision, 'fee', 'decision')
  requireNullableDecimalText(decision, 'position_cost_basis', 'decision')
  requireNullableSignedDecimalText(decision, 'realized_pnl', 'decision')
  requireDecimalText(decision, 'cash_before', 'decision')
  requireDecimalText(decision, 'cash_after', 'decision')
  requireNullableTextValue(decision, 'order_id', 'decision')
  requireDecimalText(decision, 'reserved_cash', 'decision')
  requireTextValue(decision, 'fill_formula_version', 'decision')
  requireTextValue(decision, 'fee_rule_version', 'decision')
  requireObject(decision.fee_provenance, 'decision.fee_provenance')
  requireNullableSha256(decision, 'market_evidence_hash', 'decision')
  requireNullableSha256(decision, 'book_evidence_hash', 'decision')
  requireObject(decision.opportunity_snapshot, 'decision.opportunity_snapshot')
  if (!Array.isArray(decision.fills)) throw new Error('Invalid Kalshi paper payload at decision.fills')
  decision.fills.forEach((fill, index) => requireKalshiPaperFill(fill, `decision.fills[${index}]`))
  requireTextValue(decision, 'created_at', 'decision')
  if (decision.order_side === 'sell') {
    if (
      decision.action !== 'execute'
      || decision.time_in_force !== 'immediate_or_cancel'
      || decision.position_id === null
      || decision.position_cost_basis === null
      || decision.realized_pnl === null
    ) {
      throw new Error('Contradictory Kalshi paper SELL decision')
    }
  } else if (decision.position_cost_basis !== null || decision.realized_pnl !== null) {
    throw new Error('Non-SELL decision cannot carry realized position accounting')
  }
  return decision as unknown as KalshiPaperDecision
}

export function requireKalshiPaperDecisionInput(value: unknown): KalshiPaperDecisionInput {
  const input = requireObject(value, 'pending_attempt')
  requireTextValue(input, 'account_id', 'pending_attempt')
  requireTextValue(input, 'decision_id', 'pending_attempt')
  requireTextValue(input, 'opportunity_id', 'pending_attempt')
  requireSha256(input, 'opportunity_revision', 'pending_attempt')
  const action = requireEnum(input, 'action', ['execute', 'pass'] as const, 'pending_attempt')
  if (action === 'execute') {
    requireDecimalText(input, 'quantity', 'pending_attempt')
    requireDecimalText(input, 'limit_price', 'pending_attempt')
    if (input.time_in_force !== undefined) {
      requireEnum(input, 'time_in_force', ['immediate_or_cancel', 'good_till_canceled'] as const, 'pending_attempt')
    }
  } else if (input.quantity !== undefined || input.limit_price !== undefined || input.time_in_force !== undefined) {
    throw new Error('Pass pending attempt cannot include quantity, limit_price, or time_in_force')
  }
  const allowedKeys = action === 'execute'
    ? new Set(['account_id', 'decision_id', 'opportunity_id', 'opportunity_revision', 'action', 'quantity', 'limit_price', 'time_in_force'])
    : new Set(['account_id', 'decision_id', 'opportunity_id', 'opportunity_revision', 'action'])
  const unknownKey = Object.keys(input).find((key) => !allowedKeys.has(key))
  if (unknownKey !== undefined) throw new Error(`Pending attempt contains unknown field ${unknownKey}`)
  return input as unknown as KalshiPaperDecisionInput
}

export function requireKalshiPaperOrder(value: unknown): KalshiPaperOrder {
  const order = requireObject(value, 'order')
  rejectUnknownFields(order, [
    'account_id', 'order_id', 'decision_id', 'ticker', 'outcome', 'side', 'time_in_force',
    'requested_quantity', 'filled_quantity', 'open_quantity', 'limit_price', 'reserved_cash',
    'cancelable', 'status', 'cancellation_id', 'later_matching_supported', 'created_at',
  ], 'order')
  requireTextValue(order, 'account_id', 'order')
  requireTextValue(order, 'order_id', 'order')
  requireTextValue(order, 'decision_id', 'order')
  requireTextValue(order, 'ticker', 'order')
  requireEnum(order, 'outcome', ['yes', 'no'] as const, 'order')
  requireEnum(order, 'side', ['buy'] as const, 'order')
  requireEnum(order, 'time_in_force', ['good_till_canceled'] as const, 'order')
  requireDecimalText(order, 'requested_quantity', 'order')
  requireDecimalText(order, 'filled_quantity', 'order')
  requireDecimalText(order, 'open_quantity', 'order')
  requireDecimalText(order, 'limit_price', 'order')
  requireDecimalText(order, 'reserved_cash', 'order')
  requireBoolean(order, 'cancelable', 'order')
  const status = requireEnum(order, 'status', ['open', 'cancelled', 'filled'] as const, 'order')
  const cancellationId = requireNullableTextValue(order, 'cancellation_id', 'order')
  if (order.later_matching_supported !== false) throw new Error('Kalshi paper later matching flag must be false')
  if (
    (status === 'open' && (order.cancelable !== true || cancellationId !== null))
    || (status === 'cancelled' && (order.cancelable !== false || cancellationId === null))
    || (status === 'filled' && (order.cancelable !== false || cancellationId !== null || order.open_quantity !== '0.00'))
  ) {
    throw new Error('Contradictory Kalshi paper order lifecycle')
  }
  requireTextValue(order, 'created_at', 'order')
  return order as unknown as KalshiPaperOrder
}

export function requireKalshiPaperCancellationInput(value: unknown): KalshiPaperCancellationInput {
  const input = requireObject(value, 'pending_cancellation')
  requireTextValue(input, 'account_id', 'pending_cancellation')
  requireTextValue(input, 'order_id', 'pending_cancellation')
  requireTextValue(input, 'cancellation_id', 'pending_cancellation')
  const unknownKey = Object.keys(input).find((key) => !['account_id', 'order_id', 'cancellation_id'].includes(key))
  if (unknownKey !== undefined) throw new Error(`Pending cancellation contains unknown field ${unknownKey}`)
  return input as unknown as KalshiPaperCancellationInput
}

export function requireKalshiPaperCancellation(value: unknown): KalshiPaperCancellation {
  const cancellation = requireObject(value, 'cancellation')
  rejectUnknownFields(cancellation, [
    'account_id', 'order_id', 'cancellation_id', 'status', 'released_cash', 'created_at',
  ], 'cancellation')
  requireKalshiPaperCancellationInput({
    account_id: cancellation.account_id,
    order_id: cancellation.order_id,
    cancellation_id: cancellation.cancellation_id,
  })
  requireEnum(cancellation, 'status', ['cancelled'] as const, 'cancellation')
  requireDecimalText(cancellation, 'released_cash', 'cancellation')
  requireTextValue(cancellation, 'created_at', 'cancellation')
  return cancellation as unknown as KalshiPaperCancellation
}

export function requireKalshiPaperPosition(value: unknown): KalshiPaperPosition {
  const position = requireObject(value, 'position')
  rejectUnknownFields(position, [
    'account_id', 'position_id', 'entry_decision_id', 'ticker', 'outcome', 'entry_quantity',
    'entry_notional', 'entry_fee', 'sold_quantity', 'remaining_quantity', 'exit_notional',
    'exit_fee', 'allocated_entry_cost', 'realized_pnl', 'status', 'closable',
    'exit_decision_ids', 'created_at',
  ], 'position')
  requireTextValue(position, 'account_id', 'position')
  requireTextValue(position, 'position_id', 'position')
  requireTextValue(position, 'entry_decision_id', 'position')
  requireTextValue(position, 'ticker', 'position')
  requireEnum(position, 'outcome', ['yes', 'no'] as const, 'position')
  requireDecimalText(position, 'entry_quantity', 'position')
  requireDecimalText(position, 'entry_notional', 'position')
  requireDecimalText(position, 'entry_fee', 'position')
  requireDecimalText(position, 'sold_quantity', 'position')
  const remaining = requireDecimalText(position, 'remaining_quantity', 'position')
  requireDecimalText(position, 'exit_notional', 'position')
  requireDecimalText(position, 'exit_fee', 'position')
  requireDecimalText(position, 'allocated_entry_cost', 'position')
  requireSignedDecimalText(position, 'realized_pnl', 'position')
  const status = requireEnum(position, 'status', ['open', 'closed'] as const, 'position')
  requireBoolean(position, 'closable', 'position')
  const exitDecisionIds = position.exit_decision_ids
  if (!Array.isArray(exitDecisionIds) || exitDecisionIds.some((value) => typeof value !== 'string' || value.length === 0)) {
    throw new Error('Invalid position.exit_decision_ids')
  }
  requireTextValue(position, 'created_at', 'position')
  const remainingIsZero = /^0(?:\.0+)?$/.test(remaining)
  if ((status === 'closed') !== remainingIsZero || position.closable !== !remainingIsZero) {
    throw new Error('Contradictory Kalshi paper position status')
  }
  return position as unknown as KalshiPaperPosition
}

export function requireKalshiPaperPositionExitInput(value: unknown): KalshiPaperPositionExitInput {
  const input = requireObject(value, 'pending_position_exit')
  rejectUnknownFields(input, [
    'account_id', 'position_id', 'decision_id', 'quantity', 'minimum_price',
  ], 'pending_position_exit')
  requireTextValue(input, 'account_id', 'pending_position_exit')
  requireTextValue(input, 'position_id', 'pending_position_exit')
  requireTextValue(input, 'decision_id', 'pending_position_exit')
  const quantity = requireDecimalText(input, 'quantity', 'pending_position_exit')
  const minimumPrice = requireDecimalText(input, 'minimum_price', 'pending_position_exit')
  if (!/^(?:0|[1-9]\d*)\.\d{2}$/.test(quantity)) {
    throw new Error('pending_position_exit.quantity must use exact quantity scale')
  }
  if (!/^(?:0|[1-9]\d*)\.\d{6}$/.test(minimumPrice)) {
    throw new Error('pending_position_exit.minimum_price must use exact price scale')
  }
  return input as unknown as KalshiPaperPositionExitInput
}

export async function createKalshiPaperAccount(input: {
  name: string
  starting_cash: string
}): Promise<KalshiPaperAccount> {
  const { data } = await api.post('/kalshi/paper/accounts', input)
  return requireKalshiPaperAccount(unwrapApiData(data))
}

export async function getKalshiPaperAccounts(): Promise<KalshiPaperAccount[]> {
  const { data } = await api.get('/kalshi/paper/accounts')
  const payload = unwrapApiData(data)
  if (!Array.isArray(payload)) throw new Error('Invalid Kalshi paper accounts payload')
  return payload.map(requireKalshiPaperAccount)
}

export async function getKalshiPaperEligibility(opportunityId: string): Promise<KalshiPaperEligibility> {
  const { data } = await api.get(`/kalshi/paper/opportunities/${encodeURIComponent(opportunityId)}/eligibility`)
  return requireKalshiPaperEligibility(unwrapApiData(data))
}

export async function getKalshiPaperDecisions(
  accountId: string,
  limit = 100,
): Promise<KalshiPaperDecision[]> {
  const { data } = await api.get(`/kalshi/paper/accounts/${encodeURIComponent(accountId)}/decisions`, {
    params: { limit },
  })
  const payload = unwrapApiData(data)
  if (!Array.isArray(payload)) throw new Error('Invalid Kalshi paper decisions payload')
  const decisions = payload.map(requireKalshiPaperDecision)
  if (decisions.some((decision) => decision.account_id !== accountId)) {
    throw new Error('Kalshi paper decision account identity mismatch')
  }
  return decisions
}

export async function recordKalshiPaperDecision(input: KalshiPaperDecisionInput): Promise<KalshiPaperDecision> {
  const { data } = await api.post('/kalshi/paper/decisions', input)
  const decision = requireKalshiPaperDecision(unwrapApiData(data))
  if (
    decision.account_id !== input.account_id
    || decision.decision_id !== input.decision_id
    || decision.opportunity_id !== input.opportunity_id
    || decision.opportunity_revision !== input.opportunity_revision
    || decision.action !== input.action
  ) {
    throw new Error('Kalshi paper decision response identity mismatch')
  }
  return decision
}

export async function getKalshiPaperOrders(accountId: string, limit = 100): Promise<KalshiPaperOrder[]> {
  const { data } = await api.get(`/kalshi/paper/accounts/${encodeURIComponent(accountId)}/orders`, { params: { limit } })
  const payload = unwrapApiData(data)
  if (!Array.isArray(payload)) throw new Error('Invalid Kalshi paper orders payload')
  const orders = payload.map(requireKalshiPaperOrder)
  if (orders.some((order) => order.account_id !== accountId)) {
    throw new Error('Kalshi paper order account identity mismatch')
  }
  return orders
}

export async function cancelKalshiPaperOrder(input: KalshiPaperCancellationInput): Promise<KalshiPaperCancellation> {
  const { data } = await api.post('/kalshi/paper/cancellations', input)
  const cancellation = requireKalshiPaperCancellation(unwrapApiData(data))
  if (
    cancellation.account_id !== input.account_id
    || cancellation.order_id !== input.order_id
    || cancellation.cancellation_id !== input.cancellation_id
  ) {
    throw new Error('Kalshi paper cancellation response identity mismatch')
  }
  return cancellation
}

export async function getKalshiPaperPositions(accountId: string, limit = 100): Promise<KalshiPaperPosition[]> {
  const { data } = await api.get(`/kalshi/paper/accounts/${encodeURIComponent(accountId)}/positions`, {
    params: { limit },
  })
  const payload = unwrapApiData(data)
  if (!Array.isArray(payload)) throw new Error('Invalid Kalshi paper positions payload')
  const positions = payload.map(requireKalshiPaperPosition)
  if (positions.some((position) => position.account_id !== accountId)) {
    throw new Error('Kalshi paper position account identity mismatch')
  }
  return positions
}

export async function exitKalshiPaperPosition(input: KalshiPaperPositionExitInput): Promise<KalshiPaperDecision> {
  const payload = requireKalshiPaperPositionExitInput(input)
  const { data } = await api.post(
    `/kalshi/paper/positions/${encodeURIComponent(payload.position_id)}/exits`,
    {
      account_id: payload.account_id,
      decision_id: payload.decision_id,
      quantity: payload.quantity,
      minimum_price: payload.minimum_price,
    },
  )
  const decision = requireKalshiPaperDecision(unwrapApiData(data))
  if (
    decision.account_id !== payload.account_id
    || decision.position_id !== payload.position_id
    || decision.decision_id !== payload.decision_id
    || decision.action !== 'execute'
    || decision.order_side !== 'sell'
    || decision.time_in_force !== 'immediate_or_cancel'
    || decision.requested_quantity !== payload.quantity
    || decision.limit_price !== payload.minimum_price
  ) {
    throw new Error('Kalshi paper position exit response identity mismatch')
  }
  return decision
}
