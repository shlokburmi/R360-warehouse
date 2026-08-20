import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, get, post, postControlPoint } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { useAuth } from '@/hooks/useAuth'
import { useScanning } from '@/hooks/useScanning'
import { Scanner } from '@/components/Scanner'
import { StickerSheetPrint } from '@/components/StickerSheetPrint'
import { Banner, Card, ProgressCounter, Spinner, StatusChip } from '@/components/ui'
import type { Box, GateEntry, Progress, StickerSheet } from '@/types'

/**
 * PRD §5.3 — Unit sticker scanning. CONTROL POINT 3.
 *
 * The count that matters is per box, not per truck, so the page keeps one box
 * "open" at a time and shows its progress at the size of a scoreboard. When a
 * box's count comes up short, nothing enters the warehouse: the box is held and
 * Ops decides (accept short / recount / reject).
 */
export function UnitScanningPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const { entryId = '' } = useParams()
  const queryClient = useQueryClient()
  const { me } = useAuth()

  const [activeBoxId, setActiveBoxId] = useState<string | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [quarantine, setQuarantine] = useState(false)

  const entry = useQuery({
    queryKey: ['entry', entryId],
    queryFn: () => get<GateEntry>(`/gate/entries/${entryId}`),
  })

  const boxes = useQuery({
    queryKey: ['boxes', entryId],
    queryFn: () => get<Box[]>(`/entries/${entryId}/boxes`),
    refetchInterval: 15_000,
  })

  const progress = useQuery({
    queryKey: ['unit-progress', entryId],
    queryFn: () => get<Progress>(`/entries/${entryId}/unit-progress`),
    refetchInterval: 10_000,
  })

  const sheets = useQuery({
    queryKey: ['sheets', entryId],
    queryFn: () => get<StickerSheet[]>(`/entries/${entryId}/sticker-sheets`),
  })

  const unitSheetId = sheets.data?.find((s) => s.sticker_type === 'unit')?.id

  const unitSheet = useQuery({
    queryKey: ['sheet', unitSheetId],
    queryFn: () => get<StickerSheet>(`/sticker-sheets/${unitSheetId}`),
    enabled: Boolean(unitSheetId),
  })

  const { feedback, submit } = useScanning(entryId, 'unit_verify')

  const issueUnitStickers = useMutation({
    mutationFn: () => post<StickerSheet>(`/entries/${entryId}/stickers/unit`),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['sheets', entryId] })
      void queryClient.invalidateQueries({ queryKey: ['entry', entryId] })
    },
    onError: (err) => setError(err as ApiError),
  })

  const finish = useMutation({
    mutationFn: () => post<GateEntry>(`/entries/${entryId}/finish-offloading`),
    onSuccess: () => {
      setError(null)
      void queryClient.invalidateQueries({ queryKey: ['entry', entryId] })
    },
    onError: (err) => setError(err as ApiError),
  })

  if (entry.isLoading) return <Spinner />
  if (!entry.data) return <Banner tone="bad" title={t('units.truck_not_found')} />

  const isOps = me?.role === 'ops_manager' || me?.role === 'admin'
  const activeBox = boxes.data?.find((b) => b.id === activeBoxId) ?? null
  const openBoxes = boxes.data?.filter((b) => ['verified', 'scanning'].includes(b.status)) ?? []
  const heldBoxes = boxes.data?.filter((b) => b.status === 'held') ?? []
  const hasUnitStickers = Boolean(unitSheetId)

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-2xl font-black">{entry.data.vehicle_number}</h1>
          <p className="text-base text-slate-500 dark:text-slate-400">
            {entry.data.entry_code} · {entry.data.vendor_name}
          </p>
        </div>
        <StatusChip status={entry.data.status} />
      </div>

      {error && (
        <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {heldBoxes.length > 0 && (
        <Banner tone="bad" title={`${heldBoxes.length} box(es) held — Ops decision needed`}>
          {t('units.held_body')}
        </Banner>
      )}

      {!hasUnitStickers && (
        <Card title={t('units.unit_stickers')} subtitle={t('units.one_per_unit')}>
          {isOps ? (
            <button
              type="button"
              className="btn-primary w-full"
              disabled={issueUnitStickers.isPending}
              onClick={() => issueUnitStickers.mutate()}
            >
              {issueUnitStickers.isPending ? 'Generating…' : 'Generate unit stickers'}
            </button>
          ) : (
            <Banner tone="warn" title={t('units.waiting_ops')} />
          )}
        </Card>
      )}

      {hasUnitStickers && unitSheet.data && isOps && (
        <details className="card">
          <summary className="cursor-pointer text-lg font-bold">
            Unit sticker sheet ({unitSheet.data.quantity})
          </summary>
          <div className="mt-4">
            <StickerSheetPrint sheet={unitSheet.data} />
          </div>
        </details>
      )}

      {hasUnitStickers && (
        <>
          <ProgressCounter
            label={t('units.units_scanned')}
            scanned={progress.data?.scanned ?? 0}
            total={progress.data?.total ?? 0}
          />

          <Card title={t('units.choose_box')}>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
              {openBoxes.map((box) => (
                <button
                  key={box.id}
                  type="button"
                  onClick={() => setActiveBoxId(box.id)}
                  className={`rounded-xl border-2 p-3 text-left ${
                    activeBoxId === box.id
                      ? 'border-blue-600 bg-blue-600 text-white'
                      : 'border-slate-300 dark:border-slate-700'
                  }`}
                >
                  <p className="text-lg font-black">Box {box.box_number}</p>
                  <p className="text-sm">
                    {box.scanned_units} / {box.expected_units}
                  </p>
                  <p className="truncate text-xs opacity-75">{box.sku}</p>
                </button>
              ))}
            </div>
            {openBoxes.length === 0 && (
              <p className="text-base text-slate-500">{t('units.none_open')}</p>
            )}
          </Card>

          {activeBox && (
            <>
              <ProgressCounter
                label={`Box ${activeBox.box_number} · ${activeBox.sku ?? ''}`}
                scanned={activeBox.scanned_units}
                total={activeBox.expected_units}
              />

              <Card title={t('units.title')}>
                <label className="mb-3 flex items-center gap-3 rounded-xl border-2 border-slate-300 p-3 dark:border-slate-700">
                  <input
                    type="checkbox"
                    className="h-6 w-6"
                    checked={quarantine}
                    onChange={(event) => setQuarantine(event.target.checked)}
                  />
                  <span className="text-base font-semibold">
                    {t('units.quarantine_scan')}
                  </span>
                </label>

                <Scanner
                  onScan={(code) => void submit(code, quarantine ? 'quarantine' : 'stock')}
                />

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

              <BoxCloseCard box={activeBox} onDone={() => setActiveBoxId(null)} />
            </>
          )}

          {progress.data?.complete && entry.data.status === 'offloading' && (
            <button
              type="button"
              className="btn-success w-full"
              disabled={finish.isPending}
              onClick={() => finish.mutate()}
            >
              {t('units.finish')}
            </button>
          )}
        </>
      )}

      {boxes.data && (
        <Card title={t('units.all_boxes')}>
          <ul className="divide-y divide-slate-200 dark:divide-slate-800">
            {boxes.data.map((box) => (
              <li key={box.id} className="flex items-center justify-between gap-3 py-3">
                <div className="min-w-0">
                  <p className="font-bold">
                    Box {box.box_number} · {box.scanned_units}/{box.expected_units}
                  </p>
                  <p className="truncate text-sm text-slate-500 dark:text-slate-400">
                    {box.sku}
                    {box.quarantined_units > 0 && ` · ${box.quarantined_units} quarantined`}
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

/**
 * The damage check and the close attempt, in that order.
 *
 * The damage question is compulsory — the box will not close without an answer
 * — but "none" is a perfectly good answer and takes one tap. Making it
 * mandatory-to-answer rather than mandatory-to-report is what stops it becoming
 * the field nobody fills in.
 */
function BoxCloseCard({ box, onDone }: { box: Box; onDone: () => void }) {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const queryClient = useQueryClient()
  const [damage, setDamage] = useState<'none' | 'packaging' | 'product' | null>(
    (box.damage_level as 'none' | 'packaging' | 'product' | null) ?? null,
  )
  const [note, setNote] = useState(box.damage_note ?? '')
  const [photoPaths, setPhotoPaths] = useState<string[]>([])
  const [error, setError] = useState<ApiError | null>(null)
  const [closeMessage, setCloseMessage] = useState<{
    closed: boolean
    exception_code: string | null
    message: string
  } | null>(null)

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['boxes'] })
    void queryClient.invalidateQueries({ queryKey: ['unit-progress'] })
    void queryClient.invalidateQueries({ queryKey: ['exceptions'] })
  }

  const saveDamage = useMutation({
    mutationFn: () =>
      post<Box>(`/boxes/${box.id}/damage-check`, {
        damage_level: damage,
        note: note || null,
        photo_paths: photoPaths,
      }),
    onSuccess: () => {
      setError(null)
      invalidate()
    },
    onError: (err) => setError(err as ApiError),
  })

  // CONTROL POINT 3 answers 409 with the held box and its exception code, so a
  // refusal is rendered here rather than thrown away as an error.
  const close = useMutation({
    mutationFn: () =>
      postControlPoint<{ closed: boolean; exception_code: string | null; message: string }>(
        `/boxes/${box.id}/close`,
      ),
    onSuccess: (result) => {
      setError(null)
      setCloseMessage(result)
      invalidate()
      if (result.closed) onDone()
    },
    onError: (err) => setError(err as ApiError),
  })

  const uploadPhoto = async (file: File) => {
    const ticket = await post<{ path: string; upload_url: string }>('/uploads/damage-photo', {
      box_id: box.id,
    })
    await fetch(ticket.upload_url, {
      method: 'PUT',
      body: file,
      headers: { 'Content-Type': file.type || 'image/jpeg' },
    })
    setPhotoPaths((current) => [...current, ticket.path])
  }

  const damageRecorded = box.damage_level !== null
  const countMatches = box.scanned_units === box.expected_units

  return (
    <Card title={`Close box ${box.box_number}`}>
      {error && (
        <div className="mb-4">
          <Banner tone={error.isControlPoint ? 'bad' : 'warn'} title={errorText(error).title}>
            {error.hint}
          </Banner>
        </div>
      )}

      {closeMessage && !closeMessage.closed && (
        <div className="mb-4">
          <Banner tone="bad" title={closeMessage.message}>
            Exception {closeMessage.exception_code} raised. Ops must decide: accept short,
            recount, or reject the box. Nothing enters the warehouse until then.
          </Banner>
        </div>
      )}

      {!damageRecorded ? (
        <>
          <p className="label">{t('units.any_damage')}</p>
          <div className="mb-4 flex gap-2">
            {(['none', 'packaging', 'product'] as const).map((level) => (
              <button
                key={level}
                type="button"
                onClick={() => setDamage(level)}
                className={`flex-1 rounded-xl border-2 px-3 py-4 text-base font-bold capitalize ${
                  damage === level
                    ? level === 'none'
                      ? 'border-ok bg-ok text-white'
                      : 'border-warn bg-warn text-white'
                    : 'border-slate-300 dark:border-slate-700'
                }`}
              >
                {level === 'none' ? 'No damage' : level}
              </button>
            ))}
          </div>

          {damage && damage !== 'none' && (
            <>
              <textarea
                className="input mb-3"
                rows={2}
                value={note}
                onChange={(event) => setNote(event.target.value)}
                placeholder={t('units.describe_damage')}
              />
              <label className="btn-ghost mb-3 w-full cursor-pointer">
                📷 Add photo ({photoPaths.length})
                <input
                  type="file"
                  accept="image/*"
                  capture="environment"
                  className="sr-only"
                  onChange={(event) => {
                    const file = event.target.files?.[0]
                    if (file) void uploadPhoto(file)
                  }}
                />
              </label>
              <p className="mb-3 text-sm text-slate-500 dark:text-slate-400">
                {t('units.photo_required')}
              </p>
            </>
          )}

          <button
            type="button"
            className="btn-primary w-full"
            disabled={
              !damage ||
              saveDamage.isPending ||
              (damage !== 'none' && (!note.trim() || photoPaths.length === 0))
            }
            onClick={() => saveDamage.mutate()}
          >
            {t('units.record_damage')}
          </button>
        </>
      ) : (
        <>
          <div className="mb-4">
            <Banner
              tone={box.damage_level === 'none' ? 'ok' : 'warn'}
              title={
                box.damage_level === 'none'
                  ? 'Damage check: no damage'
                  : `Damage check: ${box.damage_level}`
              }
            >
              {box.damage_note}
            </Banner>
          </div>

          {!countMatches && (
            <div className="mb-4">
              <Banner
                tone="bad"
                title={`${box.scanned_units} of ${box.expected_units} units scanned`}
              >
                {t('units.closing_holds')}
              </Banner>
            </div>
          )}

          <button
            type="button"
            className={countMatches ? 'btn-success w-full' : 'btn-danger w-full'}
            disabled={close.isPending}
            onClick={() => close.mutate()}
          >
            {countMatches ? 'Close box' : 'Close box (will be held)'}
          </button>
        </>
      )}
    </Card>
  )
}
