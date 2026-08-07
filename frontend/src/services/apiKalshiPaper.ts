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

export type KalshiPaperTestRunStatus =
  | 'starting'
  | 'monitoring'
  | 'paused'
  | 'entry_unfilled'
  | 'stopped'
  | 'completed'
  | 'blocked'

export type KalshiPaperTestEventType =
  | 'started'
  | 'entry_filled'
  | 'entry_unfilled'
  | 'no_bid'
  | 'hold'
  | 'take_profit_triggered'
  | 'stop_loss_triggered'
  | 'exit_filled'
  | 'exit_partial'
  | 'exit_no_fill'
  | 'paused'
  | 'resumed'
  | 'stopped'
  | 'completed'
  | 'blocked'

export interface KalshiPaperTestRunInput {
  run_id: string
  account_id: string
  opportunity_id: string
  opportunity_revision: string
  quantity: string
  entry_limit_price: string
  take_profit_price: string
  stop_loss_price: string
  stop_loss_minimum_price: string
}

export interface KalshiPaperTestEvent {
  run_id: string
  account_id: string
  sequence: number
  event_type: KalshiPaperTestEventType
  position_id: string | null
  best_bid: string | null
  trigger_price: string | null
  exit_decision_id: string | null
  market_observed_at: string | null
  book_observed_at: string | null
  quote_evidence_hash: string | null
  quote_evidence_json: string | null
  remaining_quantity: string | null
  realized_pnl: string | null
  reason: string | null
  created_at: string
}

export interface KalshiPaperTestRun extends KalshiPaperTestRunInput {
  ticker: string
  outcome: 'yes' | 'no'
  entry_decision_id: string
  position_id: string | null
  status: KalshiPaperTestRunStatus
  next_event_sequence: number
  remaining_quantity: string
  realized_pnl: string
  last_error: string | null
  last_reason: string | null
  created_at: string
  updated_at: string
}

export interface KalshiPaperTestRunDetail {
  run: KalshiPaperTestRun
  events: KalshiPaperTestEvent[]
}

const TEST_RUN_STATUSES = [
  'starting', 'monitoring', 'paused', 'entry_unfilled', 'stopped', 'completed', 'blocked',
] as const
const TEST_EVENT_TYPES = [
  'started', 'entry_filled', 'entry_unfilled', 'no_bid', 'hold', 'take_profit_triggered',
  'stop_loss_triggered', 'exit_filled', 'exit_partial', 'exit_no_fill', 'paused', 'resumed',
  'stopped', 'completed', 'blocked',
] as const

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

export function requireKalshiPaperTestRunInput(value: unknown): KalshiPaperTestRunInput {
  const input = requireObject(value, 'pending_test_run')
  rejectUnknownFields(input, [
    'run_id', 'account_id', 'opportunity_id', 'opportunity_revision', 'quantity',
    'entry_limit_price', 'take_profit_price', 'stop_loss_price', 'stop_loss_minimum_price',
  ], 'pending_test_run')
  requireTextValue(input, 'run_id', 'pending_test_run')
  requireTextValue(input, 'account_id', 'pending_test_run')
  requireTextValue(input, 'opportunity_id', 'pending_test_run')
  requireSha256(input, 'opportunity_revision', 'pending_test_run')
  const quantity = requireDecimalText(input, 'quantity', 'pending_test_run')
  if (!/^(?:0|[1-9]\d*)\.\d{2}$/.test(quantity) || quantity === '0.00') {
    throw new Error('pending_test_run.quantity must be positive and use exact quantity scale')
  }
  const priceKeys = [
    'entry_limit_price', 'take_profit_price', 'stop_loss_price', 'stop_loss_minimum_price',
  ] as const
  const prices = priceKeys.map((key) => {
    const price = requireDecimalText(input, key, 'pending_test_run')
    if (!/^0\.\d{6}$/.test(price) || price === '0.000000') {
      throw new Error(`pending_test_run.${key} must be between zero and one and use exact price scale`)
    }
    return price
  })
  const [, takeProfit, stopLoss, stopLossMinimum] = prices
  if (!(stopLossMinimum <= stopLoss && stopLoss < takeProfit)) {
    throw new Error('pending_test_run price threshold ordering is invalid')
  }
  return input as unknown as KalshiPaperTestRunInput
}

export function requireKalshiPaperTestEvent(value: unknown): KalshiPaperTestEvent {
  const event = requireObject(value, 'test_event')
  rejectUnknownFields(event, [
    'run_id', 'account_id', 'sequence', 'event_type', 'position_id', 'best_bid', 'trigger_price',
    'exit_decision_id', 'market_observed_at', 'book_observed_at', 'quote_evidence_hash',
    'quote_evidence_json', 'remaining_quantity', 'realized_pnl', 'reason', 'created_at',
  ], 'test_event')
  requireTextValue(event, 'run_id', 'test_event')
  requireTextValue(event, 'account_id', 'test_event')
  const sequence = requireInteger(event, 'sequence', 'test_event')
  if (sequence < 1) throw new Error('Kalshi paper test event sequence must be positive')
  requireEnum(event, 'event_type', TEST_EVENT_TYPES, 'test_event')
  requireNullableTextValue(event, 'position_id', 'test_event')
  const bestBid = requireNullableDecimalText(event, 'best_bid', 'test_event')
  const triggerPrice = requireNullableDecimalText(event, 'trigger_price', 'test_event')
  if (bestBid !== null && (!/^0\.\d{6}$/.test(bestBid) || bestBid === '0.000000')) {
    throw new Error('test_event.best_bid must use exact nonzero price scale')
  }
  if (triggerPrice !== null && (!/^0\.\d{6}$/.test(triggerPrice) || triggerPrice === '0.000000')) {
    throw new Error('test_event.trigger_price must use exact nonzero price scale')
  }
  requireNullableTextValue(event, 'exit_decision_id', 'test_event')
  requireNullableTextValue(event, 'market_observed_at', 'test_event')
  requireNullableTextValue(event, 'book_observed_at', 'test_event')
  requireNullableSha256(event, 'quote_evidence_hash', 'test_event')
  requireNullableTextValue(event, 'quote_evidence_json', 'test_event')
  const remaining = requireNullableDecimalText(event, 'remaining_quantity', 'test_event')
  if (remaining !== null && !/^(?:0|[1-9]\d*)\.\d{2}$/.test(remaining)) {
    throw new Error('test_event.remaining_quantity must use exact quantity scale')
  }
  const realizedPnl = requireNullableSignedDecimalText(event, 'realized_pnl', 'test_event')
  if (realizedPnl !== null && !/^-?(?:0|[1-9]\d*)\.\d{18}$/.test(realizedPnl)) {
    throw new Error('test_event.realized_pnl must use exact money scale')
  }
  requireNullableTextValue(event, 'reason', 'test_event')
  requireTextValue(event, 'created_at', 'test_event')
  return event as unknown as KalshiPaperTestEvent
}

export function requireKalshiPaperTestRun(value: unknown): KalshiPaperTestRun {
  const run = requireObject(value, 'test_run')
  rejectUnknownFields(run, [
    'run_id', 'account_id', 'opportunity_id', 'opportunity_revision', 'quantity',
    'entry_limit_price', 'take_profit_price', 'stop_loss_price', 'stop_loss_minimum_price',
    'ticker', 'outcome', 'entry_decision_id', 'position_id', 'status', 'next_event_sequence',
    'remaining_quantity', 'realized_pnl', 'last_error', 'last_reason', 'created_at', 'updated_at',
  ], 'test_run')
  const immutable = requireKalshiPaperTestRunInput({
    run_id: run.run_id,
    account_id: run.account_id,
    opportunity_id: run.opportunity_id,
    opportunity_revision: run.opportunity_revision,
    quantity: run.quantity,
    entry_limit_price: run.entry_limit_price,
    take_profit_price: run.take_profit_price,
    stop_loss_price: run.stop_loss_price,
    stop_loss_minimum_price: run.stop_loss_minimum_price,
  })
  requireTextValue(run, 'ticker', 'test_run')
  requireEnum(run, 'outcome', ['yes', 'no'] as const, 'test_run')
  const entryDecisionId = requireTextValue(run, 'entry_decision_id', 'test_run')
  if (entryDecisionId !== `paper-test-entry:${immutable.run_id}`) {
    throw new Error('Kalshi paper test run entry identity mismatch')
  }
  requireNullableTextValue(run, 'position_id', 'test_run')
  requireEnum(run, 'status', TEST_RUN_STATUSES, 'test_run')
  const nextSequence = requireInteger(run, 'next_event_sequence', 'test_run')
  if (nextSequence < 1) throw new Error('Kalshi paper test run next sequence must be positive')
  const remaining = requireDecimalText(run, 'remaining_quantity', 'test_run')
  if (!/^(?:0|[1-9]\d*)\.\d{2}$/.test(remaining)) {
    throw new Error('test_run.remaining_quantity must use exact quantity scale')
  }
  const realizedPnl = requireSignedDecimalText(run, 'realized_pnl', 'test_run')
  if (!/^-?(?:0|[1-9]\d*)\.\d{18}$/.test(realizedPnl)) {
    throw new Error('test_run.realized_pnl must use exact money scale')
  }
  requireNullableTextValue(run, 'last_error', 'test_run')
  requireNullableTextValue(run, 'last_reason', 'test_run')
  requireTextValue(run, 'created_at', 'test_run')
  requireTextValue(run, 'updated_at', 'test_run')
  return run as unknown as KalshiPaperTestRun
}

export function requireKalshiPaperTestRunDetail(value: unknown): KalshiPaperTestRunDetail {
  const detail = requireObject(value, 'test_run_detail')
  rejectUnknownFields(detail, ['run', 'events'], 'test_run_detail')
  const run = requireKalshiPaperTestRun(detail.run)
  if (!Array.isArray(detail.events)) throw new Error('Invalid Kalshi paper test run events payload')
  const events = detail.events.map(requireKalshiPaperTestEvent)
  if (events.some((event) => event.run_id !== run.run_id || event.account_id !== run.account_id)) {
    throw new Error('Kalshi paper test event identity mismatch')
  }
  if (events.some((event, index) => index > 0 && event.sequence <= events[index - 1].sequence)) {
    throw new Error('Kalshi paper test events are not strictly ordered')
  }
  if (events.some((event) => event.sequence >= run.next_event_sequence)) {
    throw new Error('Kalshi paper test event sequence exceeds run projection')
  }
  return { run, events }
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

function correlateKalshiPaperTestRun(
  detail: KalshiPaperTestRunDetail,
  input: KalshiPaperTestRunInput,
): KalshiPaperTestRunDetail {
  const { run } = detail
  if (
    run.run_id !== input.run_id
    || run.account_id !== input.account_id
    || run.opportunity_id !== input.opportunity_id
    || run.opportunity_revision !== input.opportunity_revision
    || run.quantity !== input.quantity
    || run.entry_limit_price !== input.entry_limit_price
    || run.take_profit_price !== input.take_profit_price
    || run.stop_loss_price !== input.stop_loss_price
    || run.stop_loss_minimum_price !== input.stop_loss_minimum_price
    || run.entry_decision_id !== `paper-test-entry:${input.run_id}`
  ) {
    throw new Error('Kalshi paper test run response identity mismatch')
  }
  return detail
}

export async function startKalshiPaperTestRun(input: KalshiPaperTestRunInput): Promise<KalshiPaperTestRunDetail> {
  const payload = requireKalshiPaperTestRunInput(input)
  const { data } = await api.post('/kalshi/paper/test-runs', payload)
  return correlateKalshiPaperTestRun(requireKalshiPaperTestRunDetail(unwrapApiData(data)), payload)
}

export async function getKalshiPaperTestRuns(accountId: string): Promise<KalshiPaperTestRunDetail[]> {
  const { data } = await api.get(`/kalshi/paper/accounts/${encodeURIComponent(accountId)}/test-runs`)
  const payload = unwrapApiData(data)
  if (!Array.isArray(payload)) throw new Error('Invalid Kalshi paper test runs payload')
  const details = payload.map(requireKalshiPaperTestRunDetail)
  if (details.some(({ run }) => run.account_id !== accountId)) {
    throw new Error('Kalshi paper test run account identity mismatch')
  }
  return details
}

export async function getKalshiPaperTestRun(runId: string): Promise<KalshiPaperTestRunDetail> {
  const { data } = await api.get(`/kalshi/paper/test-runs/${encodeURIComponent(runId)}`)
  const detail = requireKalshiPaperTestRunDetail(unwrapApiData(data))
  if (detail.run.run_id !== runId) throw new Error('Kalshi paper test run identity mismatch')
  return detail
}

async function controlKalshiPaperTestRun(
  runId: string,
  action: 'pause' | 'resume' | 'stop',
): Promise<KalshiPaperTestRunDetail> {
  const { data } = await api.post(`/kalshi/paper/test-runs/${encodeURIComponent(runId)}/${action}`)
  const detail = requireKalshiPaperTestRunDetail(unwrapApiData(data))
  if (detail.run.run_id !== runId) throw new Error('Kalshi paper test run control identity mismatch')
  return detail
}

export function pauseKalshiPaperTestRun(runId: string): Promise<KalshiPaperTestRunDetail> {
  return controlKalshiPaperTestRun(runId, 'pause')
}

export function resumeKalshiPaperTestRun(runId: string): Promise<KalshiPaperTestRunDetail> {
  return controlKalshiPaperTestRun(runId, 'resume')
}

export function stopKalshiPaperTestRun(runId: string): Promise<KalshiPaperTestRunDetail> {
  return controlKalshiPaperTestRun(runId, 'stop')
}
