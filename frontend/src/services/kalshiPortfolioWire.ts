function requireObject(value: unknown, path: string): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Invalid authoritative Kalshi portfolio payload at ${path}`)
  }
  return value as Record<string, unknown>
}

function requireArray(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`Invalid authoritative Kalshi portfolio payload at ${path}`)
  return value
}

function requireExactText(record: Record<string, unknown>, keys: string[], path: string): void {
  for (const key of keys) {
    if (typeof record[key] !== 'string') {
      throw new Error(`Authoritative Kalshi exact value must be text at ${path}.${key}`)
    }
  }
}

function requireNullableExactText(record: Record<string, unknown>, keys: string[], path: string): void {
  for (const key of keys) {
    if (record[key] !== null && typeof record[key] !== 'string') {
      throw new Error(`Authoritative Kalshi exact value must be text or null at ${path}.${key}`)
    }
  }
}

export function requireKalshiExactWireValues<T>(value: unknown): T {
  const snapshot = requireObject(value, 'snapshot')
  if (snapshot.component_skew_seconds !== null && typeof snapshot.component_skew_seconds !== 'string') {
    throw new Error('Authoritative Kalshi exact value must be text or null at snapshot.component_skew_seconds')
  }
  if (snapshot.balance !== null) {
    const balance = requireObject(snapshot.balance, 'snapshot.balance')
    requireExactText(balance, ['balance_cents', 'balance_dollars', 'portfolio_value_cents', 'updated_ts'], 'snapshot.balance')
    requireArray(balance.balance_breakdown, 'snapshot.balance.balance_breakdown').forEach((entry, index) => {
      requireExactText(
        requireObject(entry, `snapshot.balance.balance_breakdown[${index}]`),
        ['exchange_index', 'balance'],
        `snapshot.balance.balance_breakdown[${index}]`,
      )
    })
  }
  if (snapshot.positions !== null) {
    const positions = requireObject(snapshot.positions, 'snapshot.positions')
    requireExactText(positions, ['subaccount_number'], 'snapshot.positions')
    requireArray(positions.market_positions, 'snapshot.positions.market_positions').forEach((entry, index) => {
      requireExactText(
        requireObject(entry, `snapshot.positions.market_positions[${index}]`),
        ['total_traded', 'position', 'market_exposure', 'realized_pnl', 'fees_paid'],
        `snapshot.positions.market_positions[${index}]`,
      )
    })
    requireArray(positions.event_positions, 'snapshot.positions.event_positions').forEach((entry, index) => {
      requireExactText(
        requireObject(entry, `snapshot.positions.event_positions[${index}]`),
        ['total_cost', 'total_cost_shares', 'event_exposure', 'realized_pnl', 'fees_paid'],
        `snapshot.positions.event_positions[${index}]`,
      )
    })
  }
  if (snapshot.settlements !== null) {
    const settlements = requireObject(snapshot.settlements, 'snapshot.settlements')
    requireExactText(settlements, ['subaccount_number'], 'snapshot.settlements')
    requireArray(settlements.settlements, 'snapshot.settlements.settlements').forEach((entry, index) => {
      const settlement = requireObject(entry, `snapshot.settlements.settlements[${index}]`)
      requireExactText(
        settlement,
        ['yes_count', 'yes_total_cost', 'no_count', 'no_total_cost', 'revenue_cents', 'fee_cost'],
        `snapshot.settlements.settlements[${index}]`,
      )
      requireNullableExactText(settlement, ['settlement_value_cents'], `snapshot.settlements.settlements[${index}]`)
    })
  }
  requireArray(snapshot.orders, 'snapshot.orders').forEach((entry, index) => {
    const order = requireObject(entry, `snapshot.orders[${index}]`)
    requireExactText(
      order,
      [
        'yes_price',
        'no_price',
        'initial_count',
        'fill_count',
        'remaining_count',
        'taker_fees',
        'maker_fees',
        'taker_fill_cost',
        'maker_fill_cost',
      ],
      `snapshot.orders[${index}]`,
    )
    requireNullableExactText(order, ['subaccount_number', 'exchange_index'], `snapshot.orders[${index}]`)
  })
  requireArray(snapshot.fills, 'snapshot.fills').forEach((entry, index) => {
    const fill = requireObject(entry, `snapshot.fills[${index}]`)
    requireExactText(fill, ['count', 'yes_price', 'no_price', 'fee_cost'], `snapshot.fills[${index}]`)
    requireNullableExactText(fill, ['subaccount_number', 'ts'], `snapshot.fills[${index}]`)
  })
  return snapshot as T
}
