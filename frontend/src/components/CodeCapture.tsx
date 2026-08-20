import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { Scanner } from '@/components/Scanner'
import { UnreadableFile, filesToPages, isPdf } from '@/lib/pageImages'
import { isWorkerReady, readOrderNoFromPages } from '@/lib/readOrderNo'
import { cn } from '@/lib/utils'

/**
 * Identify an invoice, by camera or by file.
 *
 * The two halves answer the same question by different routes, because the
 * document arrives in two different ways.
 *
 * **Camera** reads the barcode on the paper the matcher is holding, and yields an
 * invoice number.
 *
 * **File** is for a challan that turned up as a photo in a message, a scan in an
 * email, or a PDF from the courier — and for the day a station's camera breaks. It
 * reads the *Order No* with OCR and yields that instead. There is no barcode
 * search on this path: a photographed barcode is often the part of a page that
 * survives compression worst, and the Order No is the field this document actually
 * has a strict format for.
 *
 * ---------------------------------------------------------------------------
 * WHY THE FILE PATH NEEDS NO CONFIRMATION STEP
 * ---------------------------------------------------------------------------
 *
 * The camera reader for the Order No makes the operator check the value against a
 * cropped image, because there it is being *written* to an invoice already
 * identified some other way — a misread would attach a wrong order to a real
 * shipment, and nothing else would catch it.
 *
 * Here the value is being used to *find* the invoice, and that inverts the
 * problem: a misread does not silently succeed, it fails to match anything. The
 * lookup is itself the check, and a stricter one than a human comparing digits —
 * `CP002458380_0001` misread as `CP002458381_0001` returns "no invoice booked
 * against that order" rather than the wrong invoice. So the read goes straight to
 * the lookup, and what was read is shown only when it fails.
 */

type Phase = 'idle' | 'working' | 'miss' | 'badfile' | 'ocrfailed'

export function CodeCapture({
  onInvoiceNumber,
  onOrderNo,
  scanPaused,
}: {
  /** A barcode from the camera. */
  onInvoiceNumber: (code: string) => void
  /** An Order No read out of an uploaded file. */
  onOrderNo: (orderNo: string) => void
  scanPaused?: boolean
}) {
  const { t } = useTranslation()
  const [mode, setMode] = useState<'scan' | 'upload'>('scan')
  const [phase, setPhase] = useState<Phase>('idle')
  const [progress, setProgress] = useState<string | null>(null)
  const [badKind, setBadKind] = useState<'pdf' | 'image'>('image')
  /** The crop and text behind a failed read, so a miss can be diagnosed. */
  const [missImage, setMissImage] = useState<string | null>(null)

  function reset() {
    setPhase('idle')
    setProgress(null)
    setMissImage(null)
  }

  async function handleFile(file: File) {
    reset()
    setPhase('working')
    setProgress(isPdf(file) ? t('capture.opening') : t('capture.reading_order_no'))

    let pages: HTMLCanvasElement[]
    try {
      pages = await filesToPages(file, (n, total) =>
        setProgress(t('capture.page', { n, total })),
      )
    } catch (error) {
      setBadKind(error instanceof UnreadableFile ? error.kind : 'image')
      setPhase('badfile')
      setProgress(null)
      return
    }

    // Wrapped, because the engine load is the one step here that can fail for
    // reasons outside this file — a missing core asset, a blocked request. An
    // unhandled rejection here left the button reading "Loading OCR engine…"
    // forever with nothing in the UI to say otherwise, which is indistinguishable
    // from "slow" and is the worst way for this to break.
    let read
    try {
      if (!isWorkerReady()) setProgress(t('orderno.loading_engine'))
      // The cascade's attempt number is deliberately not shown. It is an internal
      // retry strategy, and "attempt 1 of 4" invites the operator to conclude
      // something is going wrong on a read that is proceeding normally. Page
      // number for a multi-page PDF is different — that is real progress through
      // their document, not through our guesses.
      read = await readOrderNoFromPages(pages, (info) =>
        setProgress(
          info.pages > 1
            ? t('capture.page', { n: info.page, total: info.pages })
            : t('capture.reading_order_no'),
        ),
      )
    } catch {
      setPhase('ocrfailed')
      setProgress(null)
      return
    }
    setProgress(null)

    if (!read.orderNo) {
      // The crop is kept and shown. A miss is nearly always a framing or focus
      // problem, and the operator can only tell which by seeing what the engine
      // was looking at.
      setMissImage(read.image.toDataURL('image/png'))
      setPhase('miss')
      return
    }

    reset()
    onOrderNo(read.orderNo)
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-col gap-2 sm:flex-row" role="group">
        {(['scan', 'upload'] as const).map((option) => (
          <button
            key={option}
            type="button"
            onClick={() => {
              setMode(option)
              reset()
            }}
            aria-pressed={mode === option}
            className={cn(
              'min-h-touch flex-1 rounded-xl border-2 px-4 py-3 text-base font-bold transition-colors',
              mode === option
                ? 'border-blue-600 bg-blue-50 text-blue-800 dark:border-blue-400 dark:bg-blue-500/15 dark:text-blue-200'
                : 'border-slate-300 text-slate-700 dark:border-white/15 dark:text-slate-200',
            )}
          >
            {option === 'scan' ? t('capture.scan') : t('capture.upload')}
          </button>
        ))}
      </div>

      {/* Unmounted rather than hidden while uploading: a mounted scanner holds the
          camera open, and an indicator light on for no reason is the kind of thing
          people notice and distrust. */}
      {mode === 'scan' && <Scanner onScan={onInvoiceNumber} paused={scanPaused} />}

      {mode === 'upload' && (
        <>
          {(phase === 'idle' || phase === 'working') && (
            <div className="rounded-2xl border-2 border-dashed border-slate-300 p-4 text-center dark:border-white/15">
              <label className="btn-primary inline-flex cursor-pointer">
                {/* No `capture` attribute: without it, mobile browsers offer the
                    gallery, the files app AND the camera, which is a superset of
                    what forcing any one of them could do. */}
                <input
                  type="file"
                  accept="image/*,application/pdf"
                  className="sr-only"
                  disabled={phase === 'working'}
                  onChange={(event) => {
                    const file = event.target.files?.[0]
                    // Cleared so picking the same file again fires change.
                    event.target.value = ''
                    if (file) void handleFile(file)
                  }}
                />
                {phase === 'working'
                  ? (progress ?? t('capture.reading_order_no'))
                  : t('capture.choose_file')}
              </label>
              <p className="mt-3 text-sm text-slate-600 dark:text-slate-400">
                {t('capture.upload_hint')}
              </p>
            </div>
          )}

          {phase === 'ocrfailed' && (
            <div className="space-y-3">
              <div className="rounded-xl bg-bad-bg p-4 text-bad dark:bg-bad-darkbg dark:text-bad-dark">
                <p className="font-bold">{t('capture.ocr_failed')}</p>
                <p className="mt-1 text-base">{t('capture.ocr_failed_hint')}</p>
              </div>
              <button type="button" className="btn-ghost w-full" onClick={reset}>
                {t('capture.choose_another')}
              </button>
            </div>
          )}

          {(phase === 'miss' || phase === 'badfile') && (
            <div className="space-y-3">
              <div className="rounded-xl bg-warn-bg p-4 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
                <p className="font-bold">
                  {phase === 'miss'
                    ? t('capture.no_order_no')
                    : badKind === 'pdf'
                      ? t('capture.bad_pdf')
                      : t('capture.bad_image')}
                </p>
                <p className="mt-1 text-base">
                  {phase === 'miss'
                    ? t('capture.no_order_no_hint')
                    : badKind === 'pdf'
                      ? t('capture.bad_pdf_hint')
                      : t('capture.bad_image_hint')}
                </p>
              </div>

              {missImage && (
                <figure>
                  <img
                    src={missImage}
                    alt={t('capture.what_was_read')}
                    className="w-full rounded-lg border border-slate-300 dark:border-white/15"
                  />
                  <figcaption className="mt-1 text-sm text-slate-600 dark:text-slate-400">
                    {t('capture.what_was_read')}
                  </figcaption>
                </figure>
              )}

              <button type="button" className="btn-ghost w-full" onClick={reset}>
                {t('capture.choose_another')}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  )
}
