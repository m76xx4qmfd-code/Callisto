import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, BookOpen, Plus, RotateCcw, ShieldCheck } from 'lucide-react'
import { Button } from './ui/button'
import { Card, CardContent } from './ui/card'
import { Input } from './ui/input'
import { Badge } from './ui/badge'
import {
  cancelKalshiPaperOrder,
  createKalshiPaperAccount,
  exitKalshiPaperPosition,
  getKalshiPaperAccounts,
  getKalshiPaperDecisions,
  getKalshiPaperEligibility,
  getKalshiPaperOrders,
  getKalshiPaperPositions,
  getKalshiPaperTestRuns,
  pauseKalshiPaperTestRun,
  requireKalshiPaperCancellationInput,
  requireKalshiPaperDecisionInput,
  requireKalshiPaperPositionExitInput,
  requireKalshiPaperTestRunInput,
  recordKalshiPaperDecision,
  resumeKalshiPaperTestRun,
  startKalshiPaperTestRun,
  stopKalshiPaperTestRun,
} from '../services/apiKalshiPaper'
import type {
  KalshiPaperCancellationInput,
  KalshiPaperDecision,
  KalshiPaperDecisionInput,
  KalshiPaperEligibility,
  KalshiPaperPositionExitInput,
  KalshiPaperTestRunInput,
} from '../services/apiKalshiPaper'

const PENDING_ATTEMPT_KEY = 'callisto:kalshi-paper:pending-attempt'
const PENDING_CANCELLATION_KEY = 'callisto:kalshi-paper:pending-cancellation'
const PENDING_POSITION_EXIT_KEY = 'callisto:kalshi-paper:pending-position-exit'
const PENDING_TEST_RUN_KEY = 'callisto:kalshi-paper:pending-test-run'
const PENDING_KEYS = [
  PENDING_ATTEMPT_KEY,
  PENDING_CANCELLATION_KEY,
  PENDING_POSITION_EXIT_KEY,
  PENDING_TEST_RUN_KEY,
] as const

function assertPendingNamespaceEmpty(): void {
  if (PENDING_KEYS.some((key) => localStorage.getItem(key) !== null)) {
    throw new Error('Another paper mutation has an unresolved persisted identity')
  }
}

function assertExclusivePendingIdentity(key: string, expectedSerialized: string): void {
  if (
    localStorage.getItem(key) !== expectedSerialized
    || PENDING_KEYS.some((pendingKey) => pendingKey !== key && localStorage.getItem(pendingKey) !== null)
  ) {
    throw new Error('Persisted paper mutation identity changed; retry remains frozen')
  }
}

function removePendingIfExact(key: string, expectedSerialized: string): boolean {
  if (localStorage.getItem(key) !== expectedSerialized) return false
  localStorage.removeItem(key)
  return true
}

async function withPendingMutationLock<T>(operation: () => Promise<T>): Promise<T> {
  if (!navigator.locks) throw new Error('Browser financial-mutation locking is unavailable')
  return navigator.locks.request(
    'callisto:kalshi-paper:financial-mutation',
    { mode: 'exclusive', ifAvailable: true },
    async (lock) => {
      if (lock === null) throw new Error('Another tab is starting a paper financial mutation')
      return operation()
    },
  )
}

function canonicalDecimalInput(value: string, scale: number, field: string): string {
  const match = /^(0|[1-9]\d*)(?:\.(\d*))?$/.exec(value.trim())
  if (!match || (match[2]?.length ?? 0) > scale) {
    throw new Error(`${field} must be an exact nonnegative decimal with at most ${scale} places`)
  }
  return `${match[1]}.${(match[2] ?? '').padEnd(scale, '0')}`
}

function newDecisionId(): string {
  return `paper:${crypto.randomUUID()}`
}

function newTestRunId(): string {
  return `paper-test-run:${crypto.randomUUID()}`
}

function loadPendingAttempt(): KalshiPaperDecisionInput | null {
  const stored = localStorage.getItem(PENDING_ATTEMPT_KEY)
  if (stored === null) return null
  try {
    return requireKalshiPaperDecisionInput(JSON.parse(stored))
  } catch {
    return null
  }
}

function loadPendingCancellation(): KalshiPaperCancellationInput | null {
  const stored = localStorage.getItem(PENDING_CANCELLATION_KEY)
  if (stored === null) return null
  try {
    return requireKalshiPaperCancellationInput(JSON.parse(stored))
  } catch {
    return null
  }
}

function loadPendingPositionExit(): KalshiPaperPositionExitInput | null {
  const stored = localStorage.getItem(PENDING_POSITION_EXIT_KEY)
  if (stored === null) return null
  try {
    return requireKalshiPaperPositionExitInput(JSON.parse(stored))
  } catch {
    return null
  }
}

function loadPendingTestRun(): KalshiPaperTestRunInput | null {
  const stored = localStorage.getItem(PENDING_TEST_RUN_KEY)
  if (stored === null) return null
  try {
    return requireKalshiPaperTestRunInput(JSON.parse(stored))
  } catch {
    return null
  }
}

function errorMessage(error: unknown): string {
  if (error && typeof error === 'object' && 'response' in error) {
    const response = (error as { response?: { data?: { detail?: unknown } } }).response
    if (typeof response?.data?.detail === 'string') return response.data.detail
  }
  return error instanceof Error ? error.message : 'Request failed'
}

function statusTone(status: string): string {
  if (status === 'filled' || status === 'completed') return 'border-emerald-500/40 text-emerald-300'
  if (status === 'starting' || status === 'monitoring') return 'border-blue-500/40 text-blue-300'
  if (status === 'partial' || status === 'no_fill' || status === 'passed' || status === 'paused') {
    return 'border-amber-500/40 text-amber-300'
  }
  return 'border-red-500/40 text-red-300'
}

export default function KalshiPaperPanel() {
  const queryClient = useQueryClient()
  const [pendingAttempt, setPendingAttempt] = useState<KalshiPaperDecisionInput | null>(loadPendingAttempt)
  const [pendingAttemptBytes, setPendingAttemptBytes] = useState(() => localStorage.getItem(PENDING_ATTEMPT_KEY))
  const [pendingCancellation, setPendingCancellation] = useState<KalshiPaperCancellationInput | null>(loadPendingCancellation)
  const [pendingCancellationBytes, setPendingCancellationBytes] = useState(() => localStorage.getItem(PENDING_CANCELLATION_KEY))
  const [pendingPositionExit, setPendingPositionExit] = useState<KalshiPaperPositionExitInput | null>(loadPendingPositionExit)
  const [pendingPositionExitBytes, setPendingPositionExitBytes] = useState(() => localStorage.getItem(PENDING_POSITION_EXIT_KEY))
  const [pendingTestRun, setPendingTestRun] = useState<KalshiPaperTestRunInput | null>(loadPendingTestRun)
  const [pendingTestRunBytes, setPendingTestRunBytes] = useState(() => localStorage.getItem(PENDING_TEST_RUN_KEY))
  const [selectedAccountId, setSelectedAccountId] = useState(
    pendingAttempt?.account_id
      ?? pendingCancellation?.account_id
      ?? pendingPositionExit?.account_id
      ?? pendingTestRun?.account_id
      ?? '',
  )
  const [accountName, setAccountName] = useState('Kalshi Paper')
  const [startingCash, setStartingCash] = useState('10000.00')
  const [opportunityId, setOpportunityId] = useState(pendingAttempt?.opportunity_id ?? pendingTestRun?.opportunity_id ?? '')
  const [eligibility, setEligibility] = useState<KalshiPaperEligibility | null>(null)
  const [quantity, setQuantity] = useState(pendingAttempt?.quantity ?? '1.00')
  const [limitPrice, setLimitPrice] = useState(pendingAttempt?.limit_price ?? '0.500000')
  const [testRunId, setTestRunId] = useState(pendingTestRun?.run_id ?? newTestRunId)
  const [testQuantity, setTestQuantity] = useState(pendingTestRun?.quantity ?? '1.00')
  const [testEntryLimitPrice, setTestEntryLimitPrice] = useState(pendingTestRun?.entry_limit_price ?? '0.600000')
  const [takeProfitPrice, setTakeProfitPrice] = useState(pendingTestRun?.take_profit_price ?? '0.700000')
  const [stopLossPrice, setStopLossPrice] = useState(pendingTestRun?.stop_loss_price ?? '0.400000')
  const [stopLossMinimumPrice, setStopLossMinimumPrice] = useState(
    pendingTestRun?.stop_loss_minimum_price ?? '0.300000',
  )
  const [timeInForce, setTimeInForce] = useState<'immediate_or_cancel' | 'good_till_canceled'>(
    pendingAttempt?.time_in_force ?? 'immediate_or_cancel',
  )
  const [decisionId, setDecisionId] = useState(pendingAttempt?.decision_id ?? newDecisionId)
  const [lastDecision, setLastDecision] = useState<KalshiPaperDecision | null>(null)
  const [lastDecisionAttemptBytes, setLastDecisionAttemptBytes] = useState<string | null>(null)
  const [selectedPositionId, setSelectedPositionId] = useState(pendingPositionExit?.position_id ?? '')
  const [exitQuantity, setExitQuantity] = useState(pendingPositionExit?.quantity ?? '')
  const [minimumExitPrice, setMinimumExitPrice] = useState(pendingPositionExit?.minimum_price ?? '0.010000')

  useEffect(() => {
    const synchronizePendingStorage = (event: StorageEvent) => {
      if (event.key === PENDING_ATTEMPT_KEY) {
        if (event.newValue === null) {
          if (localStorage.getItem(PENDING_ATTEMPT_KEY) !== null) return
          setPendingAttemptBytes(null)
          setPendingAttempt(null)
          setDecisionId(newDecisionId())
          return
        }
        setPendingAttemptBytes(event.newValue)
        try {
          const payload = requireKalshiPaperDecisionInput(JSON.parse(event.newValue))
          setPendingAttempt(payload)
          setSelectedAccountId(payload.account_id)
          setOpportunityId(payload.opportunity_id)
          setEligibility(null)
          setDecisionId(payload.decision_id)
          setQuantity(payload.quantity ?? '1.00')
          setLimitPrice(payload.limit_price ?? '0.500000')
          setTimeInForce(payload.time_in_force ?? 'immediate_or_cancel')
          setLastDecision(null)
          setLastDecisionAttemptBytes(null)
          recordDecision.reset()
        } catch {
          setPendingAttempt(null)
        }
        return
      }
      if (event.key === PENDING_CANCELLATION_KEY) {
        if (event.newValue === null) {
          if (localStorage.getItem(PENDING_CANCELLATION_KEY) !== null) return
          setPendingCancellationBytes(null)
          setPendingCancellation(null)
          return
        }
        setPendingCancellationBytes(event.newValue)
        try {
          const payload = requireKalshiPaperCancellationInput(JSON.parse(event.newValue))
          setPendingCancellation(payload)
          setSelectedAccountId(payload.account_id)
        } catch {
          setPendingCancellation(null)
        }
        return
      }
      if (event.key === PENDING_POSITION_EXIT_KEY) {
        if (event.newValue === null) {
          if (localStorage.getItem(PENDING_POSITION_EXIT_KEY) !== null) return
          setPendingPositionExitBytes(null)
          setPendingPositionExit(null)
          return
        }
        setPendingPositionExitBytes(event.newValue)
        try {
          const payload = requireKalshiPaperPositionExitInput(JSON.parse(event.newValue))
          setPendingPositionExit(payload)
          setSelectedAccountId(payload.account_id)
          setSelectedPositionId(payload.position_id)
          setExitQuantity(payload.quantity)
          setMinimumExitPrice(payload.minimum_price)
        } catch {
          setPendingPositionExit(null)
        }
        return
      }
      if (event.key !== PENDING_TEST_RUN_KEY) return
      if (event.newValue === null) {
        if (localStorage.getItem(PENDING_TEST_RUN_KEY) !== null) return
        setPendingTestRunBytes(null)
        setPendingTestRun(null)
        setTestRunId(newTestRunId())
        return
      }
      setPendingTestRunBytes(event.newValue)
      try {
        const payload = requireKalshiPaperTestRunInput(JSON.parse(event.newValue))
        setPendingTestRun(payload)
        setSelectedAccountId(payload.account_id)
        setOpportunityId(payload.opportunity_id)
        setEligibility(null)
        setTestRunId(payload.run_id)
        setTestQuantity(payload.quantity)
        setTestEntryLimitPrice(payload.entry_limit_price)
        setTakeProfitPrice(payload.take_profit_price)
        setStopLossPrice(payload.stop_loss_price)
        setStopLossMinimumPrice(payload.stop_loss_minimum_price)
      } catch {
        setPendingTestRun(null)
      }
    }
    window.addEventListener('storage', synchronizePendingStorage)
    return () => window.removeEventListener('storage', synchronizePendingStorage)
  }, [])

  const accountsQuery = useQuery({
    queryKey: ['kalshi-paper-accounts'],
    queryFn: getKalshiPaperAccounts,
    refetchInterval: 10000,
  })
  const accounts = accountsQuery.data ?? []
  const activeAccountId = selectedAccountId || accounts[0]?.id || ''
  const activeAccount = useMemo(
    () => accounts.find((account) => account.id === activeAccountId) ?? null,
    [accounts, activeAccountId],
  )
  const decisionsQuery = useQuery({
    queryKey: ['kalshi-paper-decisions', activeAccountId],
    queryFn: () => getKalshiPaperDecisions(activeAccountId),
    enabled: Boolean(activeAccountId),
    refetchInterval: 10000,
  })
  const ordersQuery = useQuery({
    queryKey: ['kalshi-paper-orders', activeAccountId],
    queryFn: () => getKalshiPaperOrders(activeAccountId),
    enabled: Boolean(activeAccountId),
    refetchInterval: 10000,
  })
  const positionsQuery = useQuery({
    queryKey: ['kalshi-paper-positions', activeAccountId],
    queryFn: () => getKalshiPaperPositions(activeAccountId),
    enabled: Boolean(activeAccountId),
    refetchInterval: 10000,
  })
  const testRunsQuery = useQuery({
    queryKey: ['kalshi-paper-test-runs', activeAccountId],
    queryFn: () => getKalshiPaperTestRuns(activeAccountId),
    enabled: Boolean(activeAccountId),
    refetchInterval: 5000,
  })

  const createAccount = useMutation({
    mutationFn: () => {
      assertPendingNamespaceEmpty()
      return createKalshiPaperAccount({ name: accountName, starting_cash: startingCash })
    },
    onSuccess: async (account) => {
      if (
        localStorage.getItem(PENDING_ATTEMPT_KEY) === null
        && localStorage.getItem(PENDING_CANCELLATION_KEY) === null
        && localStorage.getItem(PENDING_POSITION_EXIT_KEY) === null
        && localStorage.getItem(PENDING_TEST_RUN_KEY) === null
      ) setSelectedAccountId(account.id)
      await queryClient.invalidateQueries({ queryKey: ['kalshi-paper-accounts'] })
    },
  })
  const checkEligibility = useMutation({
    mutationFn: () => getKalshiPaperEligibility(opportunityId.trim()),
    onSuccess: (result) => {
      if (
        localStorage.getItem(PENDING_ATTEMPT_KEY) !== null
        || localStorage.getItem(PENDING_CANCELLATION_KEY) !== null
        || localStorage.getItem(PENDING_POSITION_EXIT_KEY) !== null
        || localStorage.getItem(PENDING_TEST_RUN_KEY) !== null
      ) return
      setEligibility(result)
      setDecisionId(newDecisionId())
    },
  })
  const recordDecision = useMutation({
    mutationFn: (action: 'execute' | 'pass') => withPendingMutationLock(async () => {
      let payload = pendingAttempt
      let submittedBytes = pendingAttemptBytes
      if (payload === null) {
        if (!activeAccountId || !eligibility) throw new Error('Select an account and validate an opportunity first')
        assertPendingNamespaceEmpty()
        payload = {
          account_id: activeAccountId,
          decision_id: decisionId,
          opportunity_id: opportunityId.trim(),
          opportunity_revision: eligibility.opportunity_revision,
          action,
          ...(action === 'execute' ? {
            quantity: canonicalDecimalInput(quantity, 2, 'Quantity'),
            limit_price: canonicalDecimalInput(limitPrice, 6, 'Maximum price'),
            time_in_force: timeInForce,
          } : {}),
        }
        submittedBytes = JSON.stringify(payload)
        localStorage.setItem(PENDING_ATTEMPT_KEY, submittedBytes)
        setPendingAttempt(payload)
        setPendingAttemptBytes(submittedBytes)
      }
      if (submittedBytes === null) throw new Error('Pending decision bytes are unavailable')
      assertExclusivePendingIdentity(PENDING_ATTEMPT_KEY, submittedBytes)
      return { decision: await recordKalshiPaperDecision(payload), submittedBytes }
    }),
    onSuccess: async ({ decision, submittedBytes }) => {
      if (localStorage.getItem(PENDING_ATTEMPT_KEY) === submittedBytes) {
        setLastDecision(decision)
        setLastDecisionAttemptBytes(submittedBytes)
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-accounts'] }),
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-decisions', activeAccountId] }),
      ])
    },
  })
  const startTestRun = useMutation({
    mutationFn: () => withPendingMutationLock(async () => {
      let payload = pendingTestRun
      let submittedBytes = pendingTestRunBytes
      if (payload === null) {
        if (!activeAccountId || !eligibility) throw new Error('Select an account and validate an opportunity first')
        assertPendingNamespaceEmpty()
        payload = requireKalshiPaperTestRunInput({
          run_id: testRunId,
          account_id: activeAccountId,
          opportunity_id: opportunityId.trim(),
          opportunity_revision: eligibility.opportunity_revision,
          quantity: testQuantity,
          entry_limit_price: testEntryLimitPrice,
          take_profit_price: takeProfitPrice,
          stop_loss_price: stopLossPrice,
          stop_loss_minimum_price: stopLossMinimumPrice,
        })
        submittedBytes = JSON.stringify(payload)
        localStorage.setItem(PENDING_TEST_RUN_KEY, submittedBytes)
        setPendingTestRun(payload)
        setPendingTestRunBytes(submittedBytes)
      }
      if (submittedBytes === null) throw new Error('Pending test-run bytes are unavailable')
      assertExclusivePendingIdentity(PENDING_TEST_RUN_KEY, submittedBytes)
      return { detail: await startKalshiPaperTestRun(payload), submittedBytes }
    }),
    onSuccess: async ({ detail: { run }, submittedBytes }) => {
      if (removePendingIfExact(PENDING_TEST_RUN_KEY, submittedBytes)) {
        setPendingTestRun(null)
        setPendingTestRunBytes(null)
        setTestRunId(newTestRunId())
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-test-runs', run.account_id] }),
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-accounts'] }),
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-decisions', run.account_id] }),
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-positions', run.account_id] }),
      ])
    },
  })
  const controlTestRun = useMutation({
    mutationFn: ({ runId, action }: { runId: string; action: 'pause' | 'resume' | 'stop' }) => {
      assertPendingNamespaceEmpty()
      if (action === 'pause') return pauseKalshiPaperTestRun(runId)
      if (action === 'resume') return resumeKalshiPaperTestRun(runId)
      return stopKalshiPaperTestRun(runId)
    },
    onSuccess: async ({ run }) => {
      await queryClient.invalidateQueries({ queryKey: ['kalshi-paper-test-runs', run.account_id] })
    },
  })
  const cancelOrder = useMutation({
    mutationFn: (orderId?: string) => withPendingMutationLock(async () => {
      let payload = pendingCancellation
      let submittedBytes = pendingCancellationBytes
      if (payload === null) {
        if (!activeAccountId || !orderId) throw new Error('An authoritative open order is required')
        assertPendingNamespaceEmpty()
        payload = {
          account_id: activeAccountId,
          order_id: orderId,
          cancellation_id: `paper-cancel:${crypto.randomUUID()}`,
        }
        submittedBytes = JSON.stringify(payload)
        localStorage.setItem(PENDING_CANCELLATION_KEY, submittedBytes)
        setPendingCancellation(payload)
        setPendingCancellationBytes(submittedBytes)
      }
      if (submittedBytes === null) throw new Error('Pending cancellation bytes are unavailable')
      assertExclusivePendingIdentity(PENDING_CANCELLATION_KEY, submittedBytes)
      return { cancellation: await cancelKalshiPaperOrder(payload), submittedBytes }
    }),
    onSuccess: async ({ submittedBytes }) => {
      if (removePendingIfExact(PENDING_CANCELLATION_KEY, submittedBytes)) {
        setPendingCancellation(null)
        setPendingCancellationBytes(null)
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-accounts'] }),
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-orders', activeAccountId] }),
      ])
    },
  })
  const exitPosition = useMutation({
    mutationFn: (positionId?: string) => withPendingMutationLock(async () => {
      let payload = pendingPositionExit
      let submittedBytes = pendingPositionExitBytes
      if (payload === null) {
        if (!activeAccountId || !positionId) throw new Error('An authoritative open position is required')
        assertPendingNamespaceEmpty()
        payload = {
          account_id: activeAccountId,
          position_id: positionId,
          decision_id: `paper-exit:${crypto.randomUUID()}`,
          quantity: exitQuantity,
          minimum_price: minimumExitPrice,
        }
        requireKalshiPaperPositionExitInput(payload)
        submittedBytes = JSON.stringify(payload)
        localStorage.setItem(PENDING_POSITION_EXIT_KEY, submittedBytes)
        setPendingPositionExit(payload)
        setPendingPositionExitBytes(submittedBytes)
      }
      if (submittedBytes === null) throw new Error('Pending position-exit bytes are unavailable')
      assertExclusivePendingIdentity(PENDING_POSITION_EXIT_KEY, submittedBytes)
      return { decision: await exitKalshiPaperPosition(payload), submittedBytes }
    }),
    onSuccess: async ({ submittedBytes }) => {
      if (removePendingIfExact(PENDING_POSITION_EXIT_KEY, submittedBytes)) {
        setPendingPositionExit(null)
        setPendingPositionExitBytes(null)
        setSelectedPositionId('')
        setExitQuantity('')
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-accounts'] }),
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-decisions', activeAccountId] }),
        queryClient.invalidateQueries({ queryKey: ['kalshi-paper-positions', activeAccountId] }),
      ])
    },
  })

  const startNewDecision = () => {
    if (lastDecisionAttemptBytes !== null && removePendingIfExact(PENDING_ATTEMPT_KEY, lastDecisionAttemptBytes)) {
      setPendingAttempt(null)
      setPendingAttemptBytes(null)
      setDecisionId(newDecisionId())
      setLastDecision(null)
      setLastDecisionAttemptBytes(null)
      recordDecision.reset()
    }
  }
  const attemptAction = pendingAttempt?.action ?? null
  const unreadableRetryState = (pendingAttemptBytes !== null && pendingAttempt === null)
    || (pendingCancellationBytes !== null && pendingCancellation === null)
    || (pendingPositionExitBytes !== null && pendingPositionExit === null)
    || (pendingTestRunBytes !== null && pendingTestRun === null)
  const inputFrozen = pendingAttempt !== null
    || pendingCancellation !== null
    || pendingPositionExit !== null
    || pendingTestRun !== null
    || unreadableRetryState
  const decisionRetryBlocked = pendingAttempt === null || pendingAttemptBytes === null
    || pendingCancellationBytes !== null || pendingPositionExitBytes !== null || pendingTestRunBytes !== null
  const cancellationRetryBlocked = pendingCancellation === null || pendingCancellationBytes === null
    || pendingAttemptBytes !== null || pendingPositionExitBytes !== null || pendingTestRunBytes !== null
  const positionExitRetryBlocked = pendingPositionExit === null || pendingPositionExitBytes === null
    || pendingAttemptBytes !== null || pendingCancellationBytes !== null || pendingTestRunBytes !== null
  const testRunRetryBlocked = pendingTestRun === null || pendingTestRunBytes === null
    || pendingAttemptBytes !== null || pendingCancellationBytes !== null || pendingPositionExitBytes !== null
  const authoritativeError = Boolean(
    accountsQuery.error || ordersQuery.error || positionsQuery.error || testRunsQuery.error,
  )
  const canEvaluate = Boolean(activeAccountId && eligibility && !authoritativeError)
    && !inputFrozen
    && !recordDecision.isPending

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 overflow-auto">
      <div className="rounded-lg border border-blue-500/30 bg-blue-500/10 p-3">
        <div className="flex items-start gap-2">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-blue-300" />
          <div>
            <p className="text-sm font-semibold text-blue-200">Kalshi-native paper execution — PAPER ONLY</p>
            <p className="mt-1 text-xs text-blue-100/80">
              This desk performs read-only production market-data GETs and writes only local paper evidence.
              It has no signer, credential input, venue order route, worker, or automatic startup behavior.
            </p>
            <p className="mt-1 text-xs text-amber-200">
              Current scope is intentionally narrow: local BUY IOC/GTC openings and owned-position SELL IOC exits
              against one current depth snapshot under an active market-specific fee waiver. GTC remainders do not match later.
            </p>
          </div>
        </div>
      </div>

      {unreadableRetryState && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 p-3 text-xs text-red-200">
          Persisted retry identity cannot be decoded. Financial actions and account switching remain frozen;
          preserve browser storage for operator recovery.
        </div>
      )}

      <div className="grid gap-3 xl:grid-cols-[320px_minmax(0,1fr)]">
        <Card className="border-border bg-card/50 shadow-none">
          <CardContent className="space-y-3 p-3">
            <div>
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground">Paper account</p>
              <p className="text-xs text-muted-foreground">Exact USD cash is persisted independently of live accounts.</p>
            </div>
            {accounts.length > 0 && (
              <select
                aria-label="Paper account"
                value={activeAccountId}
                disabled={inputFrozen}
                onChange={(event) => setSelectedAccountId(event.target.value)}
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              >
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>{account.name}</option>
                ))}
              </select>
            )}
            {activeAccount && (
              <div className="rounded-md border border-border/60 bg-background/60 p-2.5 text-xs">
                <div className="flex justify-between gap-3">
                  <span className="text-muted-foreground">Settled cash</span>
                  <span className="font-mono">${activeAccount.cash_balance}</span>
                </div>
                <div className="mt-1 flex justify-between gap-3">
                  <span className="text-muted-foreground">Reserved</span>
                  <span className="font-mono">${activeAccount.reserved_cash}</span>
                </div>
                <div className="mt-1 flex justify-between gap-3">
                  <span className="text-muted-foreground">Available</span>
                  <span className="font-mono">${activeAccount.available_cash}</span>
                </div>
                <div className="mt-1 flex justify-between gap-3">
                  <span className="text-muted-foreground">Journal sequence</span>
                  <span className="font-mono">{activeAccount.journal_sequence}</span>
                </div>
              </div>
            )}
            {accountsQuery.error && (
              <p className="text-xs text-amber-300">
                Account refresh failed. Displayed cash is cached and new decisions are disabled until refresh recovers.
              </p>
            )}
            <div className="space-y-2 border-t border-border/60 pt-3">
              <Input value={accountName} onChange={(event) => setAccountName(event.target.value)} placeholder="Account name" />
              <Input value={startingCash} onChange={(event) => setStartingCash(event.target.value)} placeholder="Starting cash" inputMode="decimal" />
              <Button
                variant="outline"
                className="w-full gap-1.5"
                disabled={inputFrozen || !accountName.trim() || !startingCash.trim() || createAccount.isPending}
                onClick={() => createAccount.mutate()}
              >
                <Plus className="h-3.5 w-3.5" />
                Create paper account
              </Button>
              {createAccount.error && <p className="text-xs text-red-300">{errorMessage(createAccount.error)}</p>}
            </div>
          </CardContent>
        </Card>

        <Card className="border-border bg-card/50 shadow-none">
          <CardContent className="space-y-3 p-3">
            <div>
              <p className="text-[10px] uppercase tracking-wider text-muted-foreground">Operator decision</p>
              <p className="text-xs text-muted-foreground">Validate a current opportunity revision before passing or simulating execution.</p>
            </div>
            <div className="flex gap-2">
              <Input
                value={opportunityId}
                disabled={inputFrozen}
                onChange={(event) => {
                  setOpportunityId(event.target.value)
                  setEligibility(null)
                  checkEligibility.reset()
                }}
                placeholder="Opportunity ID or stable ID"
              />
              <Button
                variant="outline"
                disabled={inputFrozen || !opportunityId.trim() || checkEligibility.isPending}
                onClick={() => checkEligibility.mutate()}
              >
                Validate
              </Button>
            </div>
            {checkEligibility.error && <p className="text-xs text-red-300">{errorMessage(checkEligibility.error)}</p>}
            {eligibility && (
              <div className="grid gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-2.5 text-xs sm:grid-cols-3">
                <div><span className="text-muted-foreground">Ticker</span><p className="font-mono">{eligibility.ticker}</p></div>
                <div><span className="text-muted-foreground">Outcome</span><p className="font-mono uppercase">{eligibility.outcome}</p></div>
                <div><span className="text-muted-foreground">Strategy</span><p className="font-mono">{eligibility.strategy_key}</p></div>
                <div className="sm:col-span-3"><span className="text-muted-foreground">Revision</span><p className="break-all font-mono text-[10px]">{eligibility.opportunity_revision}</p></div>
              </div>
            )}
            <div className="grid gap-2 sm:grid-cols-2">
              <div>
                <label className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">Quantity</label>
                <Input aria-label="Quantity" value={quantity} disabled={inputFrozen} onChange={(event) => setQuantity(event.target.value)} inputMode="decimal" />
              </div>
              <div>
                <label className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">Maximum price</label>
                <Input aria-label="Maximum price" value={limitPrice} disabled={inputFrozen} onChange={(event) => setLimitPrice(event.target.value)} inputMode="decimal" />
              </div>
            </div>
            <div>
              <label className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">Time in force</label>
              <select
                aria-label="Time in force"
                value={timeInForce}
                disabled={inputFrozen}
                onChange={(event) => setTimeInForce(event.target.value as typeof timeInForce)}
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
              >
                <option value="immediate_or_cancel">IOC — cancel any remainder immediately</option>
                <option value="good_till_canceled">GTC — reserve local remainder until cancelled</option>
              </select>
            </div>
            <div className="rounded-md border border-border/60 bg-background/50 p-2 text-[10px] text-muted-foreground">
              Decision ID: <span className="break-all font-mono text-foreground">{decisionId}</span>
            </div>
            {attemptAction === null ? (
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" disabled={!canEvaluate} onClick={() => recordDecision.mutate('pass')}>Journal pass</Button>
                <Button disabled={!canEvaluate || !quantity || !limitPrice} onClick={() => recordDecision.mutate('execute')}>
                  Simulate buy {timeInForce === 'good_till_canceled' ? 'GTC' : 'IOC'}
                </Button>
              </div>
            ) : (
              <div className="flex flex-wrap items-center gap-2">
                {!lastDecision && (
                  <Button
                    disabled={recordDecision.isPending || decisionRetryBlocked}
                    onClick={() => recordDecision.mutate(attemptAction)}
                  >
                    <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                    Retry same immutable decision
                  </Button>
                )}
                <Button
                  variant="outline"
                  disabled={pendingCancellation !== null || recordDecision.isPending || !lastDecision}
                  onClick={startNewDecision}
                >
                  New decision
                </Button>
              </div>
            )}
            {recordDecision.error && (
              <div className="flex gap-2 rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-200">
                <AlertTriangle className="h-4 w-4 shrink-0" />
                <span>{errorMessage(recordDecision.error)} The same decision ID is retained for a safe retry.</span>
              </div>
            )}
            {lastDecision && (
              <div className="rounded-md border border-border/70 bg-background/60 p-2.5 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <Badge variant="outline" className={statusTone(lastDecision.status)}>{lastDecision.status}</Badge>
                  <span className="font-mono">cash ${lastDecision.cash_before} → ${lastDecision.cash_after}</span>
                </div>
                <div className="mt-2 grid gap-1 sm:grid-cols-3">
                  <span>Filled {lastDecision.filled_quantity}</span>
                  <span>Notional ${lastDecision.notional}</span>
                  <span>Fee ${lastDecision.fee}</span>
                </div>
                <p className="mt-1 text-muted-foreground">{lastDecision.reason.replace(/_/g, ' ')}</p>
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card className="border-blue-500/30 bg-card/50 shadow-none">
        <CardContent className="p-0">
          <div className="border-b border-blue-500/30 bg-blue-500/5 px-3 py-2.5">
            <p className="text-sm font-semibold text-blue-200">TEST TRADES — PAPER ONLY</p>
            <p className="text-[11px] text-muted-foreground">
              An operator-started paper BUY IOC is monitored against fresh public bids. No venue mutation is available.
            </p>
            <p className="mt-1 text-[11px] text-amber-200">
              Stop leaves any residual paper position open; settlement and manual paper SELL actions remain separate.
            </p>
          </div>
          <div className="space-y-3 p-3">
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
              <div>
                <label className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">Test quantity</label>
                <Input
                  aria-label="Test quantity"
                  value={testQuantity}
                  disabled={inputFrozen}
                  onChange={(event) => setTestQuantity(event.target.value)}
                  inputMode="decimal"
                />
              </div>
              <div>
                <label className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">Entry limit price</label>
                <Input
                  aria-label="Entry limit price"
                  value={testEntryLimitPrice}
                  disabled={inputFrozen}
                  onChange={(event) => setTestEntryLimitPrice(event.target.value)}
                  inputMode="decimal"
                />
              </div>
              <div>
                <label className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">Take profit bid</label>
                <Input
                  aria-label="Take profit bid"
                  value={takeProfitPrice}
                  disabled={inputFrozen}
                  onChange={(event) => setTakeProfitPrice(event.target.value)}
                  inputMode="decimal"
                />
              </div>
              <div>
                <label className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">Stop loss trigger bid</label>
                <Input
                  aria-label="Stop loss trigger bid"
                  value={stopLossPrice}
                  disabled={inputFrozen}
                  onChange={(event) => setStopLossPrice(event.target.value)}
                  inputMode="decimal"
                />
              </div>
              <div>
                <label className="mb-1 block text-[10px] uppercase tracking-wider text-muted-foreground">Stop loss minimum bid</label>
                <Input
                  aria-label="Stop loss minimum bid"
                  value={stopLossMinimumPrice}
                  disabled={inputFrozen}
                  onChange={(event) => setStopLossMinimumPrice(event.target.value)}
                  inputMode="decimal"
                />
              </div>
            </div>
            <div className="rounded-md border border-border/60 bg-background/50 p-2 text-[10px] text-muted-foreground">
              Run ID: <span className="break-all font-mono text-foreground">{testRunId}</span>
            </div>
            {pendingTestRun ? (
              <div className="rounded-md border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
                <p>Test-run start outcome unknown. The same run ID and immutable configuration are retained; no request is sent automatically.</p>
                <Button
                  className="mt-2"
                  size="sm"
                  disabled={startTestRun.isPending || testRunRetryBlocked}
                  onClick={() => startTestRun.mutate()}
                >
                  <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                  Retry same immutable test run
                </Button>
              </div>
            ) : (
              <Button
                disabled={
                  inputFrozen
                  || authoritativeError
                  || !activeAccountId
                  || !eligibility
                  || startTestRun.isPending
                  || !testQuantity
                  || !testEntryLimitPrice
                  || !takeProfitPrice
                  || !stopLossPrice
                  || !stopLossMinimumPrice
                }
                onClick={() => startTestRun.mutate()}
              >
                Start paper test run
              </Button>
            )}
            {startTestRun.error && (
              <p className="rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-200">
                {errorMessage(startTestRun.error)} The same run ID and immutable configuration are retained for an explicit safe retry.
              </p>
            )}
            {controlTestRun.error && (
              <p className="rounded-md border border-red-500/30 bg-red-500/10 p-2 text-xs text-red-200">
                {errorMessage(controlTestRun.error)}
              </p>
            )}
          </div>
          {testRunsQuery.error ? (
            <p className="border-t border-border/60 p-4 text-xs text-amber-300">
              Test-run refresh failed. Cached runs and events are hidden and controls are disabled until refresh recovers.
            </p>
          ) : (testRunsQuery.data?.length ?? 0) === 0 ? (
            <p className="border-t border-border/60 p-6 text-center text-xs text-muted-foreground">
              No paper test runs for this account.
            </p>
          ) : (
            <div className="space-y-3 border-t border-border/60 p-3">
              {(testRunsQuery.data ?? []).map(({ run, events }) => (
                <div key={run.run_id} className="rounded-md border border-border/70 bg-background/50 p-3 text-xs">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <p className="font-mono">{run.ticker} {run.outcome.toUpperCase()}</p>
                      <p className="break-all text-[10px] text-muted-foreground">{run.run_id}</p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge variant="outline" className={statusTone(run.status)}>{run.status}</Badge>
                      {run.status === 'monitoring' && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={inputFrozen || controlTestRun.isPending}
                          onClick={() => controlTestRun.mutate({ runId: run.run_id, action: 'pause' })}
                        >
                          Pause
                        </Button>
                      )}
                      {run.status === 'paused' && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={inputFrozen || controlTestRun.isPending}
                          onClick={() => controlTestRun.mutate({ runId: run.run_id, action: 'resume' })}
                        >
                          Resume
                        </Button>
                      )}
                      {(run.status === 'monitoring' || run.status === 'paused') && (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={inputFrozen || controlTestRun.isPending}
                          onClick={() => controlTestRun.mutate({ runId: run.run_id, action: 'stop' })}
                        >
                          Stop monitoring
                        </Button>
                      )}
                    </div>
                  </div>
                  <div className="mt-2 grid gap-1 font-mono sm:grid-cols-4">
                    <span>entry max {run.entry_limit_price}</span>
                    <span>TP {run.take_profit_price}</span>
                    <span>SL {run.stop_loss_price}</span>
                    <span>open {run.remaining_quantity}</span>
                  </div>
                  <p className="mt-1 text-muted-foreground">
                    Realized P&amp;L ${run.realized_pnl} · {run.last_reason?.replace(/_/g, ' ') ?? 'no projection reason'}
                  </p>
                  {run.last_error && <p className="mt-1 text-red-300">{run.last_error}</p>}
                  <div className="mt-3 overflow-auto">
                    <table className="w-full min-w-[760px] text-[11px]">
                      <thead className="border-b border-border/60 text-muted-foreground">
                        <tr>
                          <th className="py-1 text-left">Seq</th>
                          <th className="py-1 text-left">Event</th>
                          <th className="py-1 text-right">Best bid</th>
                          <th className="py-1 text-right">Trigger</th>
                          <th className="py-1 text-right">Remaining</th>
                          <th className="py-1 text-left">Reason</th>
                          <th className="py-1 text-left">Time</th>
                        </tr>
                      </thead>
                      <tbody>
                        {events.map((event) => (
                          <tr key={`${event.run_id}:${event.sequence}`} className="border-b border-border/30">
                            <td className="py-1 font-mono">{event.sequence}</td>
                            <td className="py-1">{event.event_type.replace(/_/g, ' ')}</td>
                            <td className="py-1 text-right font-mono">{event.best_bid ?? '—'}</td>
                            <td className="py-1 text-right font-mono">{event.trigger_price ?? '—'}</td>
                            <td className="py-1 text-right font-mono">{event.remaining_quantity ?? '—'}</td>
                            <td className="py-1 text-muted-foreground">{event.reason?.replace(/_/g, ' ') ?? '—'}</td>
                            <td className="py-1 text-muted-foreground">{new Date(event.created_at).toLocaleString()}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="border-border bg-card/50 shadow-none">
        <CardContent className="p-0">
          <div className="border-b border-border/60 px-3 py-2.5">
            <p className="text-sm font-medium">Authoritative paper positions — PAPER ONLY</p>
            <p className="text-[11px] text-muted-foreground">
              Every row is one immutable BUY lot. SELL IOC exits consume only the held outcome&apos;s current bids.
            </p>
          </div>
          {pendingPositionExit && (
            <div className="border-b border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
              <p>SELL IOC outcome unknown. All paper mutations and account switching remain frozen.</p>
              <Button
                className="mt-2"
                size="sm"
                disabled={exitPosition.isPending || positionExitRetryBlocked}
                onClick={() => exitPosition.mutate(undefined)}
              >
                <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                Retry same immutable SELL IOC
              </Button>
            </div>
          )}
          {exitPosition.error && (
            <p className="border-b border-red-500/30 bg-red-500/10 p-3 text-xs text-red-200">
              {errorMessage(exitPosition.error)} The same position, quantity, price, and decision ID are retained for a safe retry.
            </p>
          )}
          {positionsQuery.error ? (
            <p className="p-4 text-xs text-amber-300">
              Position refresh failed. Cached positions are hidden and SELL is disabled until refresh recovers.
            </p>
          ) : (positionsQuery.data?.length ?? 0) === 0 ? (
            <p className="p-6 text-center text-xs text-muted-foreground">No paper positions for this account.</p>
          ) : (
            <div className="overflow-auto">
              <table className="w-full min-w-[1050px] text-xs">
                <thead className="border-b border-border/60 text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left">Market</th>
                    <th className="px-3 py-2 text-left">State</th>
                    <th className="px-3 py-2 text-right">Entry</th>
                    <th className="px-3 py-2 text-right">Sold</th>
                    <th className="px-3 py-2 text-right">Open</th>
                    <th className="px-3 py-2 text-right">Entry notional</th>
                    <th className="px-3 py-2 text-right">Realized P&amp;L</th>
                    <th className="px-3 py-2 text-right">Paper action</th>
                  </tr>
                </thead>
                <tbody>
                  {(positionsQuery.data ?? []).map((position) => (
                    <tr key={position.position_id} className="border-b border-border/40">
                      <td className="px-3 py-2 font-mono">{position.ticker} {position.outcome.toUpperCase()}</td>
                      <td className="px-3 py-2"><Badge variant="outline">{position.status}</Badge></td>
                      <td className="px-3 py-2 text-right font-mono">{position.entry_quantity}</td>
                      <td className="px-3 py-2 text-right font-mono">{position.sold_quantity}</td>
                      <td className="px-3 py-2 text-right font-mono">{position.remaining_quantity}</td>
                      <td className="px-3 py-2 text-right font-mono" title={`Entry fee $${position.entry_fee}`}>${position.entry_notional}</td>
                      <td className="px-3 py-2 text-right font-mono">${position.realized_pnl}</td>
                      <td className="px-3 py-2 text-right">
                        {position.status === 'open' && selectedPositionId !== position.position_id && (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={inputFrozen || authoritativeError}
                            onClick={() => {
                              setSelectedPositionId(position.position_id)
                              setExitQuantity(position.remaining_quantity)
                            }}
                          >
                            Prepare SELL IOC
                          </Button>
                        )}
                        {position.status === 'open' && selectedPositionId === position.position_id && (
                          <div className="ml-auto grid w-[360px] grid-cols-[90px_110px_1fr] gap-1.5">
                            <Input
                              aria-label={`SELL quantity ${position.position_id}`}
                              value={exitQuantity}
                              disabled={inputFrozen}
                              onChange={(event) => setExitQuantity(event.target.value)}
                              inputMode="decimal"
                            />
                            <Input
                              aria-label={`Minimum SELL price ${position.position_id}`}
                              value={minimumExitPrice}
                              disabled={inputFrozen}
                              onChange={(event) => setMinimumExitPrice(event.target.value)}
                              inputMode="decimal"
                            />
                            <Button
                              size="sm"
                              disabled={inputFrozen || authoritativeError || !exitQuantity || !minimumExitPrice || exitPosition.isPending}
                              onClick={() => exitPosition.mutate(position.position_id)}
                            >
                              SELL IOC — PAPER ONLY
                            </Button>
                          </div>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="border-border bg-card/50 shadow-none">
        <CardContent className="p-0">
          <div className="border-b border-border/60 px-3 py-2.5">
            <p className="text-sm font-medium">Authoritative local GTC orders</p>
            <p className="text-[11px] text-muted-foreground">
              Remainders are reserved locally and never receive later fills in this release.
            </p>
          </div>
          {pendingCancellation && (
            <div className="border-b border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
              <p>Cancellation outcome unknown. Account switching and new actions remain frozen.</p>
              <Button
                className="mt-2"
                size="sm"
                disabled={cancelOrder.isPending || cancellationRetryBlocked}
                onClick={() => cancelOrder.mutate(undefined)}
              >
                <RotateCcw className="mr-1.5 h-3.5 w-3.5" />
                Retry same immutable cancellation
              </Button>
            </div>
          )}
          {cancelOrder.error && (
            <p className="border-b border-red-500/30 bg-red-500/10 p-3 text-xs text-red-200">
              {errorMessage(cancelOrder.error)} The same cancellation ID is retained for a safe retry.
            </p>
          )}
          {ordersQuery.error ? (
            <p className="p-4 text-xs text-amber-300">
              Order refresh failed. Cached orders are hidden and cancellation is disabled until refresh recovers.
            </p>
          ) : (ordersQuery.data?.length ?? 0) === 0 ? (
            <p className="p-6 text-center text-xs text-muted-foreground">No local GTC orders for this account.</p>
          ) : (
            <div className="overflow-auto">
              <table className="w-full min-w-[760px] text-xs">
                <thead className="border-b border-border/60 text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left">Market</th>
                    <th className="px-3 py-2 text-left">State</th>
                    <th className="px-3 py-2 text-right">Open</th>
                    <th className="px-3 py-2 text-right">Limit</th>
                    <th className="px-3 py-2 text-right">Reserved</th>
                    <th className="px-3 py-2 text-right">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {(ordersQuery.data ?? []).map((order) => (
                    <tr key={order.order_id} className="border-b border-border/40">
                      <td className="px-3 py-2 font-mono">{order.ticker} {order.outcome.toUpperCase()}</td>
                      <td className="px-3 py-2"><Badge variant="outline">{order.status}</Badge></td>
                      <td className="px-3 py-2 text-right font-mono">{order.open_quantity}</td>
                      <td className="px-3 py-2 text-right font-mono">${order.limit_price}</td>
                      <td className="px-3 py-2 text-right font-mono">${order.reserved_cash}</td>
                      <td className="px-3 py-2 text-right">
                        {order.cancelable && (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={inputFrozen || authoritativeError || cancelOrder.isPending}
                            onClick={() => cancelOrder.mutate(order.order_id)}
                          >
                            Cancel remainder
                          </Button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="min-h-[260px] border-border bg-card/50 shadow-none">
        <CardContent className="p-0">
          <div className="flex items-center gap-2 border-b border-border/60 px-3 py-2.5">
            <BookOpen className="h-4 w-4 text-muted-foreground" />
            <div>
              <p className="text-sm font-medium">Immutable paper decision journal</p>
              <p className="text-[11px] text-muted-foreground">Passes, fills, partial fills, no-fills, and fail-closed rejections survive restarts.</p>
            </div>
          </div>
          {decisionsQuery.error ? (
            <p className="p-4 text-xs text-red-300">{errorMessage(decisionsQuery.error)}</p>
          ) : (decisionsQuery.data?.length ?? 0) === 0 ? (
            <p className="p-6 text-center text-xs text-muted-foreground">No paper decisions recorded for this account.</p>
          ) : (
            <div className="overflow-auto">
              <table className="w-full min-w-[900px] text-xs">
                <thead className="border-b border-border/60 text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left">Seq</th>
                    <th className="px-3 py-2 text-left">Market</th>
                    <th className="px-3 py-2 text-left">Decision</th>
                    <th className="px-3 py-2 text-right">Requested</th>
                    <th className="px-3 py-2 text-right">Filled</th>
                    <th className="px-3 py-2 text-right">Notional</th>
                    <th className="px-3 py-2 text-right">Cash after</th>
                    <th className="px-3 py-2 text-left">Time</th>
                  </tr>
                </thead>
                <tbody>
                  {(decisionsQuery.data ?? []).map((decision) => (
                    <tr key={decision.decision_id} className="border-b border-border/40">
                      <td className="px-3 py-2 font-mono">{decision.account_sequence}</td>
                      <td className="px-3 py-2"><p className="font-mono">{decision.ticker}</p><p className="text-[10px] uppercase text-muted-foreground">{decision.outcome}</p></td>
                      <td className="px-3 py-2"><Badge variant="outline" className={statusTone(decision.status)}>{decision.status}</Badge><p className="mt-1 text-[10px] text-muted-foreground">{decision.reason.replace(/_/g, ' ')}</p></td>
                      <td className="px-3 py-2 text-right font-mono">{decision.requested_quantity ?? '—'}</td>
                      <td className="px-3 py-2 text-right font-mono">{decision.filled_quantity}</td>
                      <td className="px-3 py-2 text-right font-mono">${decision.notional}</td>
                      <td className="px-3 py-2 text-right font-mono">${decision.cash_after}</td>
                      <td className="px-3 py-2 text-muted-foreground">{new Date(decision.created_at).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
