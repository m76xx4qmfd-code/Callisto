import { api, unwrapApiData } from './apiClient'

export interface KalshiPaperAccount {
  id: string
  name: string
  currency: 'USD'
  starting_cash: string
  cash_balance: string
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
  order_side: 'buy' | null
  time_in_force: 'immediate_or_cancel' | null
  requested_quantity: string | null
  limit_price: string | null
  status: 'filled' | 'partial' | 'no_fill' | 'passed' | 'rejected'
  reason: string
  filled_quantity: string
  remaining_quantity: string
  average_fill_price: string | null
  notional: string
  fee: string
  cash_before: string
  cash_after: string
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
}

const DECIMAL_PATTERN = /^(?:0|[1-9]\d*)(?:\.\d+)?$/
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

function requireInteger(record: Record<string, unknown>, key: string, path: string): number {
  const value = record[key]
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) {
    throw new Error(`Kalshi paper integer is required at ${path}.${key}`)
  }
  return value
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
  requireTextValue(account, 'id', 'account')
  requireTextValue(account, 'name', 'account')
  requireEnum(account, 'currency', ['USD'] as const, 'account')
  requireDecimalText(account, 'starting_cash', 'account')
  requireDecimalText(account, 'cash_balance', 'account')
  requireInteger(account, 'journal_sequence', 'account')
  requireTextValue(account, 'created_at', 'account')
  requireTextValue(account, 'updated_at', 'account')
  return account as unknown as KalshiPaperAccount
}

export function requireKalshiPaperEligibility(value: unknown): KalshiPaperEligibility {
  const eligibility = requireObject(value, 'eligibility')
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
  requireNullableEnum(decision, 'order_side', ['buy'] as const, 'decision')
  requireNullableEnum(decision, 'time_in_force', ['immediate_or_cancel'] as const, 'decision')
  requireNullableDecimalText(decision, 'requested_quantity', 'decision')
  requireNullableDecimalText(decision, 'limit_price', 'decision')
  requireEnum(decision, 'status', ['filled', 'partial', 'no_fill', 'passed', 'rejected'] as const, 'decision')
  requireTextValue(decision, 'reason', 'decision')
  requireDecimalText(decision, 'filled_quantity', 'decision')
  requireDecimalText(decision, 'remaining_quantity', 'decision')
  requireNullableDecimalText(decision, 'average_fill_price', 'decision')
  requireDecimalText(decision, 'notional', 'decision')
  requireDecimalText(decision, 'fee', 'decision')
  requireDecimalText(decision, 'cash_before', 'decision')
  requireDecimalText(decision, 'cash_after', 'decision')
  requireTextValue(decision, 'fill_formula_version', 'decision')
  requireTextValue(decision, 'fee_rule_version', 'decision')
  requireObject(decision.fee_provenance, 'decision.fee_provenance')
  requireNullableSha256(decision, 'market_evidence_hash', 'decision')
  requireNullableSha256(decision, 'book_evidence_hash', 'decision')
  requireObject(decision.opportunity_snapshot, 'decision.opportunity_snapshot')
  if (!Array.isArray(decision.fills)) throw new Error('Invalid Kalshi paper payload at decision.fills')
  decision.fills.forEach((fill, index) => requireKalshiPaperFill(fill, `decision.fills[${index}]`))
  requireTextValue(decision, 'created_at', 'decision')
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
  } else if (input.quantity !== undefined || input.limit_price !== undefined) {
    throw new Error('Pass pending attempt cannot include quantity or limit_price')
  }
  const allowedKeys = action === 'execute'
    ? new Set(['account_id', 'decision_id', 'opportunity_id', 'opportunity_revision', 'action', 'quantity', 'limit_price'])
    : new Set(['account_id', 'decision_id', 'opportunity_id', 'opportunity_revision', 'action'])
  const unknownKey = Object.keys(input).find((key) => !allowedKeys.has(key))
  if (unknownKey !== undefined) throw new Error(`Pending attempt contains unknown field ${unknownKey}`)
  return input as unknown as KalshiPaperDecisionInput
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
  return payload.map(requireKalshiPaperDecision)
}

export async function recordKalshiPaperDecision(input: KalshiPaperDecisionInput): Promise<KalshiPaperDecision> {
  const { data } = await api.post('/kalshi/paper/decisions', input)
  return requireKalshiPaperDecision(unwrapApiData(data))
}
