import { AlertTriangle, CheckCircle2, Clock3, Database, RefreshCw, Shield } from 'lucide-react'
import { cn } from '../lib/utils'
import type { KalshiPortfolioSnapshot } from '../services/api'
import { Badge } from './ui/badge'
import { Button } from './ui/button'
import { ScrollArea } from './ui/scroll-area'

interface KalshiAuthoritativePortfolioProps {
  snapshot: KalshiPortfolioSnapshot | undefined
  isLoading: boolean
  error: unknown
  isFetching: boolean
  onRefresh: () => void
  principalChoices: string[]
  selectedPrincipal: string | null
  onSelectPrincipal: (fingerprint: string) => void
}

function text(value: unknown): string {
  if (value == null || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function first(record: object, keys: string[]): string {
  const values = record as Record<string, unknown>
  for (const key of keys) {
    if (values[key] != null && values[key] !== '') return text(values[key])
  }
  return '—'
}

function exactDollars(value: unknown): string {
  return value == null ? '—' : `$${text(value)}`
}

function timestamp(value: string | null | undefined): string {
  if (!value) return '—'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString()
}

function readinessTone(readiness: KalshiPortfolioSnapshot['readiness'], queryFailed: boolean): string {
  if (queryFailed) return 'border-red-500/40 bg-red-500/10 text-red-300'
  if (readiness === 'healthy') return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300'
  if (readiness === 'degraded') return 'border-red-500/40 bg-red-500/10 text-red-300'
  return 'border-amber-500/40 bg-amber-500/10 text-amber-300'
}

function recordId(record: object, index: number): string {
  return first(record, ['order_id', 'fill_id', 'ticker', 'market_ticker', 'event_ticker', 'settlement_id']) + `:${index}`
}

export default function KalshiAuthoritativePortfolio({
  snapshot,
  isLoading,
  error,
  isFetching,
  onRefresh,
  principalChoices,
  selectedPrincipal,
  onSelectPrincipal,
}: KalshiAuthoritativePortfolioProps) {
  if (isLoading && !snapshot) {
    return <div className="rounded-lg border border-border/70 bg-card/80 p-6 text-sm text-muted-foreground">Loading durable portfolio evidence…</div>
  }

  const principalSelector = principalChoices.length > 1 ? (
      <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-4">
        <p className="font-medium text-amber-200">Select a Kalshi principal</p>
        <p className="mt-1 text-xs text-muted-foreground">Multiple durable principals exist. None is selected or merged automatically.</p>
        <select
          className="mt-3 w-full rounded-md border border-border bg-background px-2 py-2 font-mono text-xs"
          value={selectedPrincipal ?? ''}
          onChange={(event) => {
            if (event.target.value) onSelectPrincipal(event.target.value)
          }}
        >
          <option value="">Choose a principal fingerprint…</option>
          {principalChoices.map((fingerprint) => (
            <option key={fingerprint} value={fingerprint}>{fingerprint}</option>
          ))}
        </select>
      </div>
  ) : null

  if (principalSelector && !snapshot) return principalSelector

  if (error && !snapshot) {
    return (
      <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-4">
        <p className="font-medium text-red-300">Authoritative snapshot unavailable</p>
        <p className="mt-1 text-xs text-muted-foreground">
          No account values are shown because durable evidence could not be read. This does not authorize an order retry.
        </p>
        <Button variant="outline" size="sm" className="mt-3" onClick={onRefresh}>Refresh database snapshot</Button>
      </div>
    )
  }

  if (!snapshot) return null

  const marketPositions = snapshot.positions?.market_positions ?? []
  const eventPositions = snapshot.positions?.event_positions ?? []
  const settlements = snapshot.settlements?.settlements ?? []
  const unknownCount = snapshot.unknown_activity.order_ids.length
    + snapshot.unknown_activity.client_order_ids.length
    + snapshot.unknown_activity.fill_ids.length
  const hasHealthyProjection = snapshot.projection_id !== null
  const queryFailed = Boolean(error)
  const current = snapshot.readiness === 'healthy' && !queryFailed
  const balanceDollars = snapshot.balance?.balance_dollars
  const portfolioValueCents = snapshot.balance?.portfolio_value_cents
  const stateIcon = current ? CheckCircle2 : (snapshot.readiness === 'degraded' || queryFailed) ? AlertTriangle : Clock3
  const StateIcon = stateIcon

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      {principalSelector}
      <div className={cn('rounded-lg border p-3', readinessTone(snapshot.readiness, queryFailed))}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <StateIcon className="h-4 w-4" />
              <p className="text-sm font-semibold capitalize">{queryFailed ? 'cached · query failed' : snapshot.readiness.replace('_', ' ')}</p>
              <Badge variant="outline" className="border-current/30 text-[9px] uppercase">Read-only authoritative projection</Badge>
            </div>
            <p className="mt-1 text-xs text-current/90">
              {queryFailed ? 'The latest snapshot query failed. Cached evidence is retained only as historical data.' : snapshot.reason.replace(/_/g, ' ')}
            </p>
            {!current && hasHealthyProjection && (
              <p className="mt-1 text-xs text-muted-foreground">Values below are the last healthy historical projection, not current account state.</p>
            )}
            {!hasHealthyProjection && (
              <p className="mt-1 text-xs text-muted-foreground">No account values are available. Empty lists do not prove an empty portfolio.</p>
            )}
          </div>
          <Button variant="outline" size="sm" onClick={onRefresh} disabled={isFetching} className="gap-1.5 bg-background/40">
            <RefreshCw className={cn('h-3.5 w-3.5', isFetching && 'animate-spin')} />
            Refresh snapshot
          </Button>
        </div>
      </div>

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <EvidenceMetric label="Last healthy as of" value={timestamp(snapshot.last_healthy_as_of)} />
        <EvidenceMetric label="Balance dollars" value={hasHealthyProjection ? exactDollars(balanceDollars) : 'Unavailable'} />
        <EvidenceMetric label="Balance cents" value={hasHealthyProjection && snapshot.balance ? `${snapshot.balance.balance_cents}¢` : 'Unavailable'} />
        <EvidenceMetric label="Portfolio value (cents)" value={hasHealthyProjection && portfolioValueCents != null ? `${portfolioValueCents}¢` : 'Unavailable'} />
        <EvidenceMetric label="Balance updated ts" value={hasHealthyProjection && snapshot.balance ? snapshot.balance.updated_ts : 'Unavailable'} />
        <EvidenceMetric label="Balance breakdown" value={hasHealthyProjection && snapshot.balance ? text(snapshot.balance.balance_breakdown) : 'Unavailable'} />
        <EvidenceMetric label="Principal" value={snapshot.principal_fingerprint ? `${snapshot.principal_fingerprint.slice(0, 10)}…${snapshot.principal_fingerprint.slice(-8)}` : 'Not established'} />
      </div>

      <ScrollArea className="min-h-0 flex-1 rounded-lg border border-border/70 bg-card/80">
        <div className="space-y-4 p-3">
          <section>
            <SectionHeading icon={Shield} title="Evidence health" detail={`retry authorized: ${snapshot.retry_allowed ? 'yes' : 'no'}`} />
            <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
              <EvidenceMetric label="Private sync" value={snapshot.sync_runtime.ready ? 'Ready' : snapshot.sync_runtime.running ? 'Running, not ready' : 'Not ready'} />
              <EvidenceMetric label="Latest attempt" value={snapshot.latest_attempt ? `${snapshot.latest_attempt.status}: ${snapshot.latest_attempt.reason}` : 'None'} />
              <EvidenceMetric label="Component skew" value={snapshot.component_skew_seconds == null ? 'Unavailable' : `${snapshot.component_skew_seconds}s`} />
              <EvidenceMetric label="Unknown activity" value={String(unknownCount)} />
            </div>
            <div className="mt-2 grid gap-2 lg:grid-cols-2">
              <div className="rounded-md border border-border/50 bg-background/30 p-2 text-[11px]">
                <p className="font-medium">Component observations</p>
                {Object.keys(snapshot.components).length === 0 ? (
                  <p className="mt-1 text-muted-foreground">No component evidence</p>
                ) : Object.entries(snapshot.components).map(([name, component]) => (
                  <div key={name} className="mt-1 flex justify-between gap-3"><span className="capitalize text-muted-foreground">{name}</span><span className="font-mono">{timestamp(component.observed_at)}</span></div>
                ))}
              </div>
              <div className="rounded-md border border-border/50 bg-background/30 p-2 text-[11px]">
                <p className="font-medium">Scope and gaps</p>
                <p className="mt-1 text-muted-foreground">Orders/fills: all subaccounts · balance: account aggregate</p>
                <p className="text-muted-foreground">Positions/settlements subaccounts: {snapshot.scope?.positions.subaccount_numbers.join(', ') || 'unavailable'}</p>
                <p className={cn('mt-1', snapshot.gaps.length ? 'text-red-300' : 'text-emerald-300')}>Gaps: {snapshot.gaps.join(', ') || 'none reported'}</p>
              </div>
            </div>
          </section>

          <RecordTable
            title="Positions"
            rows={[...marketPositions, ...eventPositions]}
            columns={[
              ['Ticker', ['ticker', 'market_ticker', 'event_ticker']],
              ['Position', ['position', 'total_cost_shares']],
              ['Exposure', ['market_exposure', 'event_exposure']],
              ['Realized P&L', ['realized_pnl']],
              ['Fees', ['fees_paid']],
            ]}
            empty={current ? 'Current authoritative projection contains no positions in the selected subaccount.' : 'No current position conclusion; historical evidence contains no records.'}
          />
          <RecordTable
            title="Orders"
            rows={snapshot.orders}
            columns={[
              ['Order', ['order_id']],
              ['Ticker', ['ticker']],
              ['Outcome', ['outcome_side']],
              ['Book side', ['book_side']],
              ['Status', ['status']],
              ['Initial', ['initial_count']],
              ['Filled', ['fill_count']],
              ['Remaining', ['remaining_count']],
            ]}
            empty={current ? 'Current authoritative coverage contains no orders.' : 'No current order conclusion; historical evidence contains no records.'}
          />
          <RecordTable
            title="Fills"
            rows={snapshot.fills}
            columns={[
              ['Fill', ['fill_id']],
              ['Order', ['order_id']],
              ['Ticker', ['ticker']],
              ['Outcome', ['outcome_side']],
              ['Book side', ['book_side']],
              ['Count', ['count']],
            ]}
            empty={current ? 'Current authoritative coverage contains no fills.' : 'No current fill conclusion; historical evidence contains no records.'}
          />
          <RecordTable
            title="Settlements"
            rows={settlements}
            columns={[
              ['Ticker', ['ticker', 'market_ticker']],
              ['Result', ['market_result', 'result']],
              ['Revenue (cents)', ['revenue_cents']],
              ['Settled', ['settled_time']],
            ]}
            empty={current ? 'Current authoritative projection contains no settlements in the selected subaccount.' : 'No current settlement conclusion; historical evidence contains no records.'}
          />

          <section>
            <SectionHeading icon={AlertTriangle} title="Unknown venue activity" detail={`${unknownCount} unresolved identifiers`} />
            <div className="mt-2 grid gap-2 lg:grid-cols-3">
              <IdList title="Order IDs" values={snapshot.unknown_activity.order_ids} current={current} />
              <IdList title="Client order IDs" values={snapshot.unknown_activity.client_order_ids} current={current} />
              <IdList title="Fill IDs" values={snapshot.unknown_activity.fill_ids} current={current} />
            </div>
          </section>
        </div>
      </ScrollArea>
    </div>
  )
}

function SectionHeading({ icon: Icon, title, detail }: { icon: React.ElementType; title: string; detail: string }) {
  return <div className="flex items-center justify-between gap-2 border-b border-border/50 pb-1.5"><div className="flex items-center gap-1.5"><Icon className="h-3.5 w-3.5 text-muted-foreground" /><p className="text-[11px] font-semibold uppercase tracking-wider">{title}</p></div><p className="text-[10px] text-muted-foreground">{detail}</p></div>
}

function EvidenceMetric({ label, value }: { label: string; value: string }) {
  return <div className="rounded-md border border-border/60 bg-background/50 px-2.5 py-2"><p className="text-[9px] uppercase tracking-wider text-muted-foreground">{label}</p><p className="mt-0.5 truncate font-mono text-[11px]" title={value}>{value}</p></div>
}

function RecordTable({ title, rows, columns, empty }: { title: string; rows: object[]; columns: Array<[string, string[]]>; empty: string }) {
  return (
    <section>
      <SectionHeading icon={Database} title={title} detail={`${rows.length} exact records`} />
      {rows.length === 0 ? <p className="py-3 text-xs text-muted-foreground">{empty}</p> : (
        <div className="mt-2 overflow-x-auto rounded-md border border-border/50">
          <table className="w-full text-[10px]"><thead><tr className="border-b border-border/60 text-muted-foreground">{columns.map(([label]) => <th key={label} className="px-2 py-1.5 text-left">{label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={recordId(row, index)} className="border-b border-border/30">{columns.map(([label, keys]) => <td key={label} className="max-w-[260px] truncate px-2 py-1.5 font-mono" title={first(row, keys)}>{first(row, keys)}</td>)}</tr>)}</tbody></table>
        </div>
      )}
    </section>
  )
}

function IdList({ title, values, current }: { title: string; values: string[]; current: boolean }) {
  return <div className="rounded-md border border-border/50 bg-background/30 p-2"><p className="text-[10px] font-medium">{title}</p>{values.length === 0 ? <p className="mt-1 text-[10px] text-muted-foreground">{current ? 'None observed in current coverage' : 'No current conclusion'}</p> : values.map((value) => <p key={value} className="mt-1 truncate font-mono text-[9px] text-red-300" title={value}>{value}</p>)}</div>
}
