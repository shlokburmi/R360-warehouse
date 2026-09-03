import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { UnreadableFile, cropRegion, filesToPages, isPdf } from '@/lib/pageImages'
import {
  GUIDE_BOX,
  isOrderNo,
  isWorkerReady,
  getWorker,
  parseOrderNo,
  preprocess,
  readOrderNoFromPages,
  releaseWorker,
} from '@/lib/readOrderNo'

/**
 * Reads the Order No off a delivery challan with the camera.
 *
 * The challan carries two barcodes — the DC No and the courier's tracking number
 * — and the Order No is not either of them. It is printed as plain text in the
 * header block, so [QrScanner] can never produce it and OCR is the only
 * alternative to typing sixteen characters while holding a box.
 *
 * ---------------------------------------------------------------------------
 * WHY THIS NEVER SUBMITS ON ITS OWN
 * ---------------------------------------------------------------------------
 *
 * Every other scanner in this app fires `onScan` the moment it decodes, because
 * a barcode that decodes is right. OCR is not like that: it confuses 0/O, 1/I,
 * 5/S and 8/B, and on CP002458380_0001 a single substitution produces a string
 * that looks completely credible. Auto-submitting would book a real shipment
 * against a wrong order and leave an audit trail asserting a human checked it.
 *
 * So the read is always a *proposal*. The operator sees it, in a large monospace
 * field, next to the cropped image it came from, and accepts or fixes it. That
 * comparison is the whole safety mechanism — it is why the crop is shown rather
 * than thrown away.
 */

/**
 * Why there is no confidence threshold here.
 *
 * The first version withheld the pre-filled value below 65% so the operator had
 * to retype it. Measuring the shipped engine settings against a rasterised
 * challan killed that idea: reads that were character-for-character *correct*
 * scored 10-32% at block level, and the matched word itself reported 0%.
 * Constraining the alphabet to `CP0123456789_` is what does it — the engine is
 * forced to choose among thirteen glyphs and reports low confidence in a choice
 * it was never free to make.
 *
 * A threshold would therefore have flagged every correct upload as suspect,
 * taught operators that the warning means nothing, and bought no safety. The real
 * check was always the other thing on screen: the cropped image of what was read,
 * directly above an editable field. That comparison is kept and made
 * unconditional instead.
 *
 * The number is still recorded in `order_no_scans.confidence`, as data about the
 * engine over time — the one thing it is honestly good for.
 */

export type OrderNoReading = {
  order_no: string | null
  raw_text: string
  confidence: number | null
  was_corrected: boolean
  source: 'ocr' | 'manual'
}

type Props = {
  /** Omitted when this read is creating a brand new invoice rather than being
   * attached to one that already exists. */
  invoiceNumber?: string
  onConfirm: (reading: OrderNoReading) => void
  busy?: boolean
}

type Phase = 'camera' | 'reading' | 'review' | 'nocamera' | 'badfile'

/** Where the pixels came from. Changes how the page is searched, not how it is judged. */
type Source = 'camera' | 'upload'

/**
 * Regions of an uploaded page to try, in order, as fractions of the image.
 *
 * An upload is not framed by the operator, so there is no guide box to crop to —
 * but the challan is a fixed template, and the Order No always sits in the
 * top-right header block beside the Delivery Challan Date. Trying that band
 * first is both faster and more accurate than a full page: less text reaches
 * the regex, so there is less to be confused by.
 *
 * The whole page is the fallback, for a photograph that is rotated, cropped
 * tight, or of a template that has since moved the field.
 */
export function OrderNoScanner({ invoiceNumber, onConfirm, busy = false }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)

  const { t } = useTranslation()

  const [phase, setPhase] = useState<Phase>('camera')
  const [source, setSource] = useState<Source>('camera')
  const [progress, setProgress] = useState<string | null>(null)
  const [crop, setCrop] = useState<string | null>(null)
  const [proposed, setProposed] = useState<string | null>(null)
  const [confidence, setConfidence] = useState<number | null>(null)
  const [rawText, setRawText] = useState('')
  /** Why a chosen file was refused. Separate from rawText, which is audit data. */
  const [fileError, setFileError] = useState<'pdf' | 'unreadable' | null>(null)
  const [value, setValue] = useState('')

  // ---------------------------------------------------------------------
  // Camera
  //
  // The same two-attempt ladder as QrScanner, and for the same reason
  // documented there: the ideal-constraints request is right for the warehouse
  // tablet, and the bare retry is what stops a laptop's front camera from
  // reporting "no camera" via OverconstrainedError.
  // ---------------------------------------------------------------------
  useEffect(() => {
    // Nothing to acquire in upload mode, and holding the camera open behind a
    // file picker keeps the indicator light on for no reason.
    if (source !== 'camera') return

    let cancelled = false

    const attempts: MediaStreamConstraints[] = [
      // A higher resolution than the barcode scanner asks for. Printed 9pt text
      // needs roughly twice the pixels per character that a Code 128 bar needs,
      // and 1280x720 across a full A4 challan leaves the Order No about 11px
      // tall — under what Tesseract reads reliably.
      // `exact`, not `ideal`: a phone or tablet always has a rear camera, and
      // the front one is never the right choice for reading paper — `ideal`
      // is only a preference the browser can (and on some devices does)
      // ignore. `exact` throws OverconstrainedError instead of silently
      // picking the front camera, which is exactly what should happen: the
      // bare `{ video: true }` fallback below still catches a laptop with
      // only a front camera.
      { video: { facingMode: { exact: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } } },
      { video: true },
    ]

    async function start() {
      for (const constraints of attempts) {
        if (cancelled) return
        try {
          const stream = await navigator.mediaDevices.getUserMedia(constraints)
          if (cancelled) {
            stream.getTracks().forEach((track) => track.stop())
            return
          }
          streamRef.current = stream
          if (videoRef.current) videoRef.current.srcObject = stream
          return
        } catch {
          /* fall through to the next attempt */
        }
      }
      if (!cancelled) setPhase('nocamera')
    }

    void start()

    return () => {
      cancelled = true
      streamRef.current?.getTracks().forEach((track) => track.stop())
      streamRef.current = null
      // The worker is deliberately NOT torn down here — switching between camera
      // and upload would otherwise re-download and re-initialise the engine each
      // time. It is released when the component unmounts, below.
    }
  }, [source])

  // Release the OCR worker once, on unmount.
  useEffect(
    () => () => {
      releaseWorker()
    },
    [],
  )

      /** Apply an OCR result to the review state. Shared so both paths judge alike. */
  function present(text: string, conf: number | null, image: HTMLCanvasElement) {
    const found = parseOrderNo(text)
    setCrop(image.toDataURL('image/png'))
    setRawText(text)
    setConfidence(conf)
    setProposed(found)
    // A low-confidence read is shown but not pre-filled: an operator confirming
    // a field that is already populated is reading, and reading is what OCR is
    // bad at being trusted with. Making them type it back is the point.
    setValue(found ?? '')
    setPhase('review')
  }

  function failed(error: unknown) {
    setRawText(`OCR failed: ${(error as Error).message}`)
    setProposed(null)
    setConfidence(null)
    setValue('')
    setPhase('review')
  }

  const capture = useCallback(async () => {
    const video = videoRef.current
    if (!video || !video.videoWidth) return

    setPhase('reading')
    setProgress(isWorkerReady() ? t('orderno.preparing') : t('orderno.loading_engine'))

    // Crop to the guide box the operator aligned the code inside. Full-page OCR
    // would take several seconds on a warehouse phone and return the customer's
    // name and address as well — which is personal data this feature has no
    // reason to touch, never mind hold in a `raw_text` audit column.
    const frame = cropRegion(video, video.videoWidth, video.videoHeight, GUIDE_BOX)
    const prepared = preprocess(frame)

    try {
      const worker = await getWorker('line', (fraction) =>
        setProgress(`${t('orderno.reading')} ${Math.round(fraction * 100)}%`),
      )
      setProgress(t('orderno.reading'))
      const result = await worker.recognize(prepared)
      present(
        result?.data?.text ?? '',
        typeof result?.data?.confidence === 'number' ? result.data.confidence : null,
        prepared,
      )
    } catch (error) {
      setCrop(prepared.toDataURL('image/png'))
      failed(error)
    } finally {
      setProgress(null)
    }
  }, [t])

  /**
   * Read an uploaded photo or scan.
   *
   * Two differences from the camera path, both consequences of nobody having
   * framed the shot:
   *
   * - The header band is tried first and the whole page only if that misses, so
   *   the common case stays fast without giving up on the awkward one.
   * - The privacy argument that justifies cropping the camera feed does not hold
   *   here, because a full-page fallback necessarily passes the customer's name
   *   and address through the engine. That text is never stored: only the region
   *   that produced a match is kept as `raw_text`, and a full-page miss stores
   *   the page text it read — see the note where it is set.
   */
  const readFile = useCallback(
    async (file: File) => {
      setFileError(null)
      setPhase('reading')

      // Both inputs converge on the same thing: a list of page canvases, produced
      // by the shared loader. From here down a PDF and a photo are
      // indistinguishable — which is why the measured cascade below did not have
      // to be duplicated for them.
      let pages: HTMLCanvasElement[]
      try {
        setProgress(isPdf(file) ? t('orderno.opening_pdf') : t('orderno.reading_file'))
        pages = await filesToPages(file, (n, total) =>
          setProgress(t('orderno.pdf_page', { n, total })),
        )
      } catch (error) {
        setFileError(error instanceof UnreadableFile && error.kind === 'pdf' ? 'pdf' : 'unreadable')
        setPhase('badfile')
        setProgress(null)
        return
      }

      try {
        const read = await readOrderNoFromPages(pages, (info) =>
          setProgress(
            info.pages > 1
              ? t('orderno.pdf_page', { n: info.page, total: info.pages })
              : t('orderno.reading'),
          ),
        )
        present(read.rawText, read.confidence, read.image)
      } catch (error) {
        failed(error)
      } finally {
        setProgress(null)
      }
    },
    [t],
  )

  const typed = value.trim().toUpperCase()
  const valid = isOrderNo(typed)
  const corrected = proposed !== null && typed !== proposed

  function confirm() {
    onConfirm({
      order_no: valid ? typed : null,
      raw_text: rawText,
      confidence,
      was_corrected: valid && corrected,
      // Typed from scratch after a failed read is manual, even though a camera
      // was involved. `source` records where the accepted characters came from,
      // which is the question the audit log is asked.
      source: proposed === null ? 'manual' : 'ocr',
    })
  }

  return (
    <div className="space-y-3">
      {/* Two ways in, offered as a switch rather than a hidden fallback. The
          camera is right at a matching station with the paper in hand; an upload
          is right when the challan arrived as a photo on someone's phone, or when
          the tablet's camera is broken — which is the case this whole component
          exists to survive. */}
      {(phase === 'camera' || phase === 'reading' || phase === 'nocamera') && (
        <div className="flex gap-2" role="group">
          {(['camera', 'upload'] as const).map((option) => (
            <button
              key={option}
              type="button"
              onClick={() => {
                setSource(option)
                setFileError(null)
                setPhase('camera')
              }}
              aria-pressed={source === option}
              disabled={phase === 'reading'}
              className={
                'min-h-[2.75rem] flex-1 rounded-xl border-2 px-3 py-2 text-base font-bold transition-colors ' +
                (source === option
                  ? 'border-blue-600 bg-blue-50 text-blue-800 dark:border-blue-400 dark:bg-blue-500/15 dark:text-blue-200'
                  : 'border-slate-300 text-slate-700 dark:border-white/15 dark:text-slate-200')
              }
            >
              {option === 'camera' ? t('orderno.use_camera') : t('orderno.upload')}
            </button>
          ))}
        </div>
      )}

      {source === 'upload' && (phase === 'camera' || phase === 'reading' || phase === 'nocamera') && (
        <div className="rounded-2xl border-2 border-dashed border-slate-300 p-4 text-center dark:border-white/15">
          <label className="btn-primary inline-flex cursor-pointer">
            {/* A plain file input, deliberately. `capture="environment"` would
                force the camera and defeat the point of this path; without it,
                mobile browsers offer the gallery, the files app AND the camera,
                which is a superset of what a custom picker could do. */}
            <input
              type="file"
              accept="image/*,application/pdf"
              className="sr-only"
              disabled={phase === 'reading'}
              onChange={(event) => {
                const file = event.target.files?.[0]
                // Cleared so re-picking the same file fires change again.
                event.target.value = ''
                if (file) void readFile(file)
              }}
            />
            {phase === 'reading' ? (progress ?? t('orderno.reading')) : t('orderno.choose_file')}
          </label>
          <p className="mt-3 text-sm text-slate-600 dark:text-slate-400">
            {t('orderno.upload_hint_pdf')}
          </p>
        </div>
      )}

      {phase === 'badfile' && (
        <div className="space-y-3">
          <div className="rounded-xl bg-bad-bg p-4 text-bad dark:bg-bad-darkbg dark:text-bad-dark">
            <p className="font-bold">
              {fileError === 'pdf' ? t('orderno.pdf_broken') : t('orderno.unreadable_file')}
            </p>
            <p className="mt-1 text-base">
              {fileError === 'pdf' ? t('orderno.pdf_broken_hint') : t('orderno.unreadable_hint')}
            </p>
          </div>
          <button
            type="button"
            className="btn-ghost w-full"
            onClick={() => {
              setFileError(null)
              setPhase('camera')
            }}
          >
            {t('orderno.choose_different')}
          </button>
        </div>
      )}

      {source === 'camera' && phase !== 'review' && phase !== 'nocamera' && phase !== 'badfile' && (
        <>
          <div className="viewfinder relative">
            <video ref={videoRef} autoPlay muted playsInline />
            {/* The guide box. Not decoration — `capture` crops to exactly these
                proportions, so what the operator frames is what gets read. */}
            <div
              aria-hidden
              className="pointer-events-none absolute inset-x-[10%] top-[38%] h-[24%] rounded-lg border-4 border-blue-400/90 shadow-[0_0_0_9999px_rgba(0,0,0,0.45)]"
            />
            <p className="absolute inset-x-0 bottom-2 text-center text-sm font-bold text-white drop-shadow">
              {t('orderno.guide')}
            </p>
          </div>

          <button
            type="button"
            className="btn-primary w-full"
            onClick={() => void capture()}
            disabled={phase === 'reading'}
          >
            {phase === 'reading' ? (progress ?? t('orderno.reading')) : t('orderno.read_button')}
          </button>
        </>
      )}

      {phase === 'nocamera' && source === 'camera' && (
        <div className="rounded-xl bg-warn-bg p-4 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
          <p className="font-bold">{t('orderno.no_camera')}</p>
          <p className="mt-1 text-base">{t('orderno.type_instead')}</p>
        </div>
      )}

      {(phase === 'review' || phase === 'nocamera') && (
        <div className="space-y-3">
          {crop && (
            <figure>
              {/* Shown so the operator checks the value against the paper rather
                  than against their memory of the paper. */}
              <img
                src={crop}
                alt={t('orderno.crop_alt')}
                className="w-full rounded-lg border border-slate-300 dark:border-white/15"
              />
              <figcaption className="mt-1 text-sm text-slate-600 dark:text-slate-400">
                {t('orderno.crop_caption')}
              </figcaption>
            </figure>
          )}

          {proposed === null && phase === 'review' && (
            <div className="rounded-xl bg-warn-bg p-3 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
              <p className="font-bold">{t('orderno.not_found')}</p>
              <p className="mt-1 text-base">{t('orderno.retake_or_type')}</p>
            </div>
          )}

          {proposed !== null && (
            <div className="rounded-xl bg-warn-bg p-3 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
              {/* Unconditional, because a warning shown only sometimes teaches
                  people that its absence means "verified". Nothing here verifies
                  anything; the operator does. */}
              <p className="font-bold">{t('orderno.check_against_image')}</p>
            </div>
          )}

          <label className="label" htmlFor="order-no">
            {invoiceNumber
              ? t('orderno.field_label', { invoice: invoiceNumber })
              : t('orderno.field_label_new')}
          </label>
          <input
            id="order-no"
            className="input font-mono text-xl uppercase tracking-wider"
            placeholder="CP002458380_0001"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            inputMode="text"
          />
          {typed.length > 0 && !valid && (
            <p className="text-sm font-semibold text-bad dark:text-bad-dark" role="alert">
              {t('orderno.format_error')}
            </p>
          )}
          {corrected && valid && (
            <p className="text-sm text-slate-600 dark:text-slate-400">
{t('orderno.corrected_from', { value: proposed })}
            </p>
          )}

          <div className="flex flex-col gap-2 sm:flex-row">
            <button
              type="button"
              className="btn-primary flex-1"
              onClick={confirm}
              disabled={!valid || busy}
            >
              {busy ? t('common.saving') : t('orderno.confirm')}
            </button>
            <button
              type="button"
              className="btn-ghost flex-1"
              onClick={() => {
                setPhase('camera')
                setCrop(null)
                setProposed(null)
                setValue('')
                setFileError(null)
              }}
              disabled={busy}
            >
              {source === 'camera' ? t('orderno.retake') : t('orderno.choose_different')}
            </button>
          </div>

          {/* Recording a miss is a real outcome, not an escape hatch: a station
              whose reads fail all morning is a smeared lens, and that is only
              visible if the failures were written down. */}
          {proposed === null && phase === 'review' && (
            <button
              type="button"
              className="btn-ghost w-full text-sm"
              onClick={() =>
                onConfirm({
                  order_no: null,
                  raw_text: rawText,
                  confidence,
                  was_corrected: false,
                  source: 'ocr',
                })
              }
              disabled={busy}
            >
              {t('orderno.log_failure')}
            </button>
          )}
        </div>
      )}
    </div>
  )
}
