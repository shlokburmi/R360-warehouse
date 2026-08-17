import { useCallback, useEffect, useRef, useState } from 'react'

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

/** Two letters, nine digits, underscore, four digits. Mirrors the DB constraint. */
const ORDER_NO_RE = /CP\d{9}_\d{4}/

/**
 * Below this, the value is offered but not pre-trusted: the field is flagged and
 * starts empty-ish rather than looking like a confirmed answer. Chosen because a
 * plausible-looking wrong string is the failure that matters, and a number the
 * operator has to re-read is cheap by comparison.
 */
const CONFIDENCE_FLOOR = 65

export type OrderNoReading = {
  order_no: string | null
  raw_text: string
  confidence: number | null
  was_corrected: boolean
  source: 'ocr' | 'manual'
}

/**
 * Pull an Order No out of a block of OCR text.
 *
 * Deliberately does *not* repair near-misses. Mapping O→0 to rescue
 * `CP0O2458380_0001` is the obvious next step and it is the wrong one: it
 * converts a detectable failure into an undetectable one, since the repaired
 * string satisfies every check downstream while nobody knows a guess was made.
 * The engine is constrained to a digit-only alphabet instead (see
 * `tessedit_char_whitelist`), which prevents the substitution rather than
 * papering over it.
 */
export function parseOrderNo(text: string): string | null {
  const match = ORDER_NO_RE.exec(text.toUpperCase().replace(/\s+/g, ''))
  return match ? match[0] : null
}

type Props = {
  invoiceNumber: string
  onConfirm: (reading: OrderNoReading) => void
  busy?: boolean
}

type Phase = 'camera' | 'reading' | 'review' | 'nocamera'

export function OrderNoScanner({ invoiceNumber, onConfirm, busy = false }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const workerRef = useRef<{ recognize: (i: unknown) => Promise<any>; terminate: () => void } | null>(
    null,
  )

  const [phase, setPhase] = useState<Phase>('camera')
  const [progress, setProgress] = useState<string | null>(null)
  const [crop, setCrop] = useState<string | null>(null)
  const [proposed, setProposed] = useState<string | null>(null)
  const [confidence, setConfidence] = useState<number | null>(null)
  const [rawText, setRawText] = useState('')
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
    let cancelled = false

    const attempts: MediaStreamConstraints[] = [
      // A higher resolution than the barcode scanner asks for. Printed 9pt text
      // needs roughly twice the pixels per character that a Code 128 bar needs,
      // and 1280x720 across a full A4 challan leaves the Order No about 11px
      // tall — under what Tesseract reads reliably.
      { video: { facingMode: { ideal: 'environment' }, width: { ideal: 1920 }, height: { ideal: 1080 } } },
      { video: true },
    ]

    async function start() {
      for (const constraints of attempts) {
        if (cancelled) return
        try {
          const stream = await navigator.mediaDevices.getUserMedia(constraints)
          if (cancelled) {
            stream.getTracks().forEach((t) => t.stop())
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
      streamRef.current?.getTracks().forEach((t) => t.stop())
      streamRef.current = null
      workerRef.current?.terminate()
      workerRef.current = null
    }
  }, [])

  /**
   * Prepare the crop for Tesseract.
   *
   * Three operations, each earning its cost on a photographed page:
   *
   * - **Upscale 3x.** Tesseract's models want ~30px of character height. A
   *   challan photographed at arm's length gives well under that, and it reads
   *   an upscaled blur far better than a crisp too-small glyph.
   * - **Greyscale.** Removes the colour noise a phone sensor adds under warehouse
   *   sodium lighting, which otherwise survives thresholding as speckle.
   * - **Contrast stretch, not a hard threshold.** A fixed cut-off destroys
   *   characters wherever a fold or a shadow crosses the line — and challans
   *   arrive folded. Stretching keeps the mid-tones a shadowed digit lives in.
   */
  function preprocess(source: HTMLCanvasElement): HTMLCanvasElement {
    const scale = 3
    const out = document.createElement('canvas')
    out.width = source.width * scale
    out.height = source.height * scale

    const ctx = out.getContext('2d')!
    ctx.imageSmoothingEnabled = true
    ctx.imageSmoothingQuality = 'high'
    ctx.drawImage(source, 0, 0, out.width, out.height)

    const image = ctx.getImageData(0, 0, out.width, out.height)
    const px = image.data

    let min = 255
    let max = 0
    for (let i = 0; i < px.length; i += 4) {
      const grey = (px[i] * 0.299 + px[i + 1] * 0.587 + px[i + 2] * 0.114) | 0
      px[i] = px[i + 1] = px[i + 2] = grey
      if (grey < min) min = grey
      if (grey > max) max = grey
    }

    const span = Math.max(1, max - min)
    for (let i = 0; i < px.length; i += 4) {
      const stretched = ((px[i] - min) * 255) / span
      px[i] = px[i + 1] = px[i + 2] = stretched
    }

    ctx.putImageData(image, 0, 0)
    return out
  }

  const capture = useCallback(async () => {
    const video = videoRef.current
    if (!video || !video.videoWidth) return

    setPhase('reading')
    setProgress('Preparing…')

    // Crop to the guide box the operator aligned the code inside. Full-page OCR
    // would take several seconds on a warehouse phone and return the customer's
    // name and address as well — which is personal data this feature has no
    // reason to touch, never mind hold in a `raw_text` audit column.
    const box = { x: 0.1, y: 0.38, w: 0.8, h: 0.24 }
    const source = document.createElement('canvas')
    source.width = Math.round(video.videoWidth * box.w)
    source.height = Math.round(video.videoHeight * box.h)
    source
      .getContext('2d')!
      .drawImage(
        video,
        Math.round(video.videoWidth * box.x),
        Math.round(video.videoHeight * box.y),
        source.width,
        source.height,
        0,
        0,
        source.width,
        source.height,
      )

    const prepared = preprocess(source)
    setCrop(prepared.toDataURL('image/png'))

    try {
      if (!workerRef.current) {
        setProgress('Loading OCR engine…')
        // Imported here rather than at module scope so the engine is fetched the
        // first time a matcher actually reads a challan — not in the bundle every
        // guard downloads at the gate.
        // `PSM` is pulled from the dynamic import rather than imported at module
        // scope: it is a real runtime enum, so a top-level import would drag
        // tesseract.js into the main bundle and undo the lazy load.
        const { createWorker, PSM } = await import('tesseract.js')
        const worker = await createWorker('eng', 1, {
          workerPath: '/tesseract/worker.min.js',
          corePath: '/tesseract/',
          langPath: '/tesseract',
          logger: (m: { status: string; progress: number }) => {
            if (m.status === 'recognizing text') {
              setProgress(`Reading… ${Math.round(m.progress * 100)}%`)
            }
          },
        })

        // Constraining the alphabet is the single most effective accuracy
        // measure available here, and it is what makes refusing to "repair"
        // near-misses viable: with O, S, B and I not in the alphabet at all, the
        // engine cannot emit the substitutions that would need repairing.
        await worker.setParameters({
          tessedit_char_whitelist: 'CP0123456789_',
          // Treat the image as a single text line, which is what the guide box is
          // shaped to produce. The default hunts for paragraphs and finds them in
          // the challan's table rules.
          tessedit_pageseg_mode: PSM.SINGLE_LINE,
        })

        workerRef.current = worker as unknown as typeof workerRef.current
      }

      setProgress('Reading…')
      const result = await workerRef.current!.recognize(prepared)
      const text: string = result?.data?.text ?? ''
      const conf: number | null =
        typeof result?.data?.confidence === 'number' ? result.data.confidence : null

      const found = parseOrderNo(text)

      setRawText(text)
      setConfidence(conf)
      setProposed(found)
      // A low-confidence read is shown but not pre-filled: an operator confirming
      // a field that is already populated is reading, and reading is what OCR is
      // bad at being trusted with. Making them type it back is the point.
      setValue(found && (conf === null || conf >= CONFIDENCE_FLOOR) ? found : '')
      setPhase('review')
    } catch (error) {
      setRawText(`OCR failed: ${(error as Error).message}`)
      setProposed(null)
      setConfidence(null)
      setValue('')
      setPhase('review')
    } finally {
      setProgress(null)
    }
  }, [])

  const typed = value.trim().toUpperCase()
  const valid = ORDER_NO_RE.test(typed) && typed.length === 16
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
      {phase !== 'review' && phase !== 'nocamera' && (
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
              Line up the Order No inside the box
            </p>
          </div>

          <button
            type="button"
            className="btn-primary w-full"
            onClick={() => void capture()}
            disabled={phase === 'reading'}
          >
            {phase === 'reading' ? (progress ?? 'Reading…') : 'Read Order No'}
          </button>
        </>
      )}

      {phase === 'nocamera' && (
        <div className="rounded-xl bg-warn-bg p-4 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
          <p className="font-bold">No camera available.</p>
          <p className="mt-1 text-base">Type the Order No from the challan instead.</p>
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
                alt="The part of the challan that was read"
                className="w-full rounded-lg border border-slate-300 dark:border-white/15"
              />
              <figcaption className="mt-1 text-sm text-slate-600 dark:text-slate-400">
                What the camera read — check the digits against this.
              </figcaption>
            </figure>
          )}

          {proposed === null && phase === 'review' && (
            <div className="rounded-xl bg-warn-bg p-3 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
              <p className="font-bold">No Order No found.</p>
              <p className="mt-1 text-base">Retake the photo, or type it in.</p>
            </div>
          )}

          {proposed !== null && confidence !== null && confidence < CONFIDENCE_FLOOR && (
            <div className="rounded-xl bg-warn-bg p-3 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
              <p className="font-bold">Low confidence read ({Math.round(confidence)}%).</p>
              <p className="mt-1 text-base">
                Read <span className="font-mono font-bold">{proposed}</span> — type it in to
                confirm.
              </p>
            </div>
          )}

          <label className="label" htmlFor="order-no">
            Order No for invoice {invoiceNumber}
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
              Must look like CP002458380_0001 — CP, nine digits, underscore, four digits.
            </p>
          )}
          {corrected && valid && (
            <p className="text-sm text-slate-600 dark:text-slate-400">
              Corrected from <span className="font-mono">{proposed}</span> — the change will be
              recorded.
            </p>
          )}

          <div className="flex flex-col gap-2 sm:flex-row">
            <button
              type="button"
              className="btn-primary flex-1"
              onClick={confirm}
              disabled={!valid || busy}
            >
              {busy ? 'Saving…' : 'Confirm Order No'}
            </button>
            <button
              type="button"
              className="btn-ghost flex-1"
              onClick={() => {
                setPhase('camera')
                setCrop(null)
                setProposed(null)
                setValue('')
              }}
              disabled={busy}
            >
              Retake
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
              Log the failed read and continue without an Order No
            </button>
          )}
        </div>
      )}
    </div>
  )
}
