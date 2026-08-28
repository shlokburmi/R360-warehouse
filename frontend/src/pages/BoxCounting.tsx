import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post, postControlPoint } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { useAuth } from '@/hooks/useAuth'
import { useScanning } from '@/hooks/useScanning'
import { useRealtimeInvalidate } from '@/hooks/useRealtimeInvalidate'
import { Scanner } from '@/components/Scanner'
import { StickerSheetPrint } from '@/components/StickerSheetPrint'
import { Banner, Card, Field, ProgressCounter, Spinner, StatusChip } from '@/components/ui'
import type { Box, GateEntry, Progress, StickerSheet } from '@/types'

type VerifyResult = {
  verified: boolean
  entry: GateEntry
  exception_code: string | null
  message: string
}

type StickerIssueResult = {
  issued: boolean
  sheet: StickerSheet | null
  exception_code: string | null
  message: string
}

/**
 * PRD §5.2 — Box counting. CONTROL POINT 2.
 *
 * Three numbers have to agree before boxes move inside: what the guard counted,
 * what Admin issued, and what was physically scanned. The page walks that in
 * order and refuses to skip: the count is declared before stickers exist, and
 * the stickers exist before anything can be scanned.
 */
export function BoxCountingPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const { entryId = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { me } = useAuth()

  const [declared, setDeclared] = useState('')
  const [error, setError] = useState<ApiError | null>(null)
  const [verifyResult, setVerifyResult] = useState<VerifyResult | null>(null)
  const [issueResult, setIssueResult] = useState<StickerIssueResult | null>(null)

  const entry = useQuery({
    queryKey: ['entry', entryId],
    queryFn: () => get<GateEntry>(`/gate/entries/${entryId}`),
  })

  const progress = useQuery({
    queryKey: ['box-progress', entryId],
    queryFn: () => get<Progress>(`/gate/entries/${entryId}/box-progress`),
    refetchInterval: 10_000,
  })

  const sheets = useQuery({
    queryKey: ['sheets', entryId],
    queryFn: () => get<StickerSheet[]>(`/entries/${entryId}/sticker-sheets`),
  })

  const boxSheetId = sheets.data?.find((s) => s.sticker_type === 'box')?.id

  const sheet = useQuery({
    queryKey: ['sheet', boxSheetId],
    queryFn: () => get<StickerSheet>(`/sticker-sheets/${boxSheetId}`),
    enabled: Boolean(boxSheetId),
  })

  const boxes = useQuery({
    queryKey: ['boxes', entryId],
    queryFn: () => get<Box[]>(`/entries/${entryId}/boxes`),
    refetchInterval: 15_000,
  })

  useRealtimeInvalidate(
    'gate_entries',
    [['entry', entryId], ['box-progress', entryId]],
    `id=eq.${entryId}`,
  )
  useRealtimeInvalidate(
    'boxes',
    [['boxes', entryId], ['box-progress', entryId]],
    `gate_entry_id=eq.${entryId}`,
  )

  const { feedback, submit } = useScanning(entryId, 'box_verify')

  const declareCount = useMutation({
    mutationFn: () =>
      post<GateEntry>(`/gate/entries/${entryId}/box-count`, { box_count: Number(declared) }),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['entry', entryId] })
    },
    onError: (err) => setError(err as ApiError),
  })

  const issueStickers = useMutation({
    mutationFn: () =>
      postControlPoint<StickerIssueResult>(`/entries/${entryId}/stickers/box`, {}),
    onSuccess: (result) => {
      setError(null)
      setIssueResult(result)
      void queryClient.invalidateQueries({ queryKey: ['sheets', entryId] })
      void queryClient.invalidateQueries({ queryKey: ['entry', entryId] })
      void queryClient.invalidateQueries({ queryKey: ['box-progress', entryId] })
      void queryClient.invalidateQueries({ queryKey: ['exceptions'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  // CONTROL POINT 2 answers 409 with a full body on mismatch, so the refusal
  // arrives here as data — the exception code included — rather than as a throw.
  const verify = useMutation({
    mutationFn: () =>
      postControlPoint<VerifyResult>(`/gate/entries/${entryId}/verify-boxes`),
    onSuccess: (result) => {
      setVerifyResult(result)
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['entry', entryId] })
      void queryClient.invalidateQueries({ queryKey: ['box-progress', entryId] })
      void queryClient.invalidateQueries({ queryKey: ['exceptions'] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (entry.isLoading) return <Spinner />
  if (!entry.data) return <Banner tone="bad" title={t('boxcount.truck_not_found')} />

  const isOps = me?.role === 'admin'
  const isGuard = me?.role === 'security_guard'
  const hasCount = entry.data.declared_box_count !== null
  const hasStickers = entry.data.issued_box_sticker_count > 0
  const verified = ['box_verified', 'offloading', 'offloaded', 'reconciled', 'departed'].includes(
    entry.data.status,
  )

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-black">{entry.data.vehicle_number}</h1>
          <p className="text-base text-slate-500 dark:text-slate-400">
            {entry.data.entry_code} · {entry.data.vendor_name} · {entry.data.po_number ?? 'No PO'}
          </p>
        </div>
        <StatusChip status={entry.data.status} />
      </div>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {/* Step 1 — the guard's physical count */}
      <Card title={t('boxcount.step1')} subtitle={t('boxcount.how_many')}>
        {hasCount ? (
          <Banner tone="ok" title={`${entry.data.declared_box_count} boxes declared`}>
            {t('boxcount.before_stickers')}
          </Banner>
        ) : isGuard || isOps ? (
          <>
            <Field label={t('boxcount.number_of_boxes')} required>
              <input
                className="input text-center text-3xl font-black"
                type="number"
                inputMode="numeric"
                min={1}
                value={declared}
                onChange={(event) => setDeclared(event.target.value)}
              />
            </Field>
            <button
              type="button"
              className="btn-primary w-full"
              disabled={!declared || Number(declared) < 1 || declareCount.isPending}
              onClick={() => declareCount.mutate()}
            >
              {t('boxcount.confirm_count')}
            </button>
          </>
        ) : (
          <p className="text-base text-slate-500">{t('boxcount.waiting_guard_count')}</p>
        )}
      </Card>

      {/* Step 2 — Admin issues exactly that many stickers */}
      {hasCount && (
        <Card
          title={t('boxcount.step2')}
          subtitle={`Admin issues exactly ${entry.data.declared_box_count} stickers`}
        >
          {hasStickers && sheet.data ? (
            <StickerSheetPrint sheet={sheet.data} />
          ) : isOps ? (
            <>
              {issueResult && !issueResult.issued && (
                <div className="mb-4">
                  <Banner tone="bad" title={issueResult.message}>
                    No stickers were issued. Exception {issueResult.exception_code} has been
                    logged against the vendor. Recount the truck, or amend the PO.
                  </Banner>
                </div>
              )}
              <button
                type="button"
                className="btn-primary w-full"
                disabled={issueStickers.isPending}
                onClick={() => issueStickers.mutate()}
              >
                {issueStickers.isPending
                  ? 'Generating…'
                  : `Generate ${entry.data.declared_box_count} box stickers`}
              </button>
            </>
          ) : (
            <Banner tone="warn" title={t('boxcount.waiting_ops_sheet')}>
              {t('boxcount.no_stickers_yet')}
            </Banner>
          )}
        </Card>
      )}

      {/* Step 3 — scan them back */}
      {hasStickers && !verified && (
        <>
          <ProgressCounter
            label={t('boxcount.boxes_scanned')}
            scanned={progress.data?.scanned ?? 0}
            total={progress.data?.total ?? 0}
          />

          <Card title={t('boxcount.step3')}>
            <Scanner onScan={(code) => void submit(code)} />

            {feedback.length > 0 && (
              <ul className="mt-4 space-y-2">
                {feedback.map((item) => (
                  <li
                    key={item.id}
                    className={`flex flex-wrap items-center justify-between gap-3 rounded-lg px-3 py-2 text-base ${
                      item.tone === 'ok'
                        ? 'bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark'
                        : item.tone === 'warn'
                          ? 'bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark'
                          : 'bg-bad-bg text-bad dark:bg-bad-darkbg dark:text-bad-dark'
                    }`}
                  >
                    <span className="font-mono text-sm">{item.code}</span>
                    <span className="font-bold">{item.message}</span>
                  </li>
                ))}
              </ul>
            )}
          </Card>

          {verifyResult && !verifyResult.verified && (
            <Banner tone="bad" title={verifyResult.message}>
              Boxes cannot move inside. Exception {verifyResult.exception_code} has been raised
              for Admin.
            </Banner>
          )}

          {progress.data?.complete ? (
            <>
              <Banner tone="ok" title={t('boxcount.all_verified')} />
              <button
                type="button"
                className="btn-success w-full"
                disabled={verify.isPending}
                onClick={() => verify.mutate()}
              >
                {t('boxcount.confirm_inside')}
              </button>
            </>
          ) : (
            <Banner tone="warn" title={progress.data?.message ?? 'Scanning in progress'}>
              {t('boxcount.must_scan_all')}
            </Banner>
          )}
        </>
      )}

      {verified && (
        <>
          <Banner tone="ok" title={t('boxcount.count_verified')}>
            {entry.data.declared_box_count} boxes counted, issued and scanned.
          </Banner>
          <button
            type="button"
            className="btn-primary w-full"
            onClick={() => navigate(`/entries/${entryId}/units`)}
          >
            {t('boxcount.go_unit_scanning')}
          </button>
        </>
      )}

      {boxes.data && boxes.data.length > 0 && (
        <Card title={t('boxcount.boxes')}>
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {boxes.data.map((box) => (
              <li key={box.id} className="flex items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <p className="font-bold">Box {box.box_number}</p>
                  <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                    {box.sku} · {box.expected_units} units · {box.sticker_code}
                  </p>
                </div>
                <StatusChip status={box.status} />
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  )
}
