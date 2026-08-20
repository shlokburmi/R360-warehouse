import { cropRegion } from '@/lib/pageImages'

/**
 * Reading the Order No off a challan, as pixels in and a string out.
 *
 * Extracted from the camera component because two places now need it: the live
 * scanner, where the operator frames a single line inside a guide box, and the
 * file upload, where nobody framed anything. Both want the same engine settings
 * and the same refusal to guess.
 */

/** Two letters, nine digits, underscore, four digits. Mirrors the DB constraint. */
const ORDER_NO_RE = /CP\d{9}_\d{4}/

/**
 * Pull an Order No out of a block of OCR text.
 *
 * Deliberately does *not* repair near-misses. Mapping O→0 to rescue
 * `CP0O2458380_0001` is the obvious next step and it is the wrong one: it converts
 * a detectable failure into an undetectable one, since the repaired string
 * satisfies every check downstream while nobody knows a guess was made. The engine
 * is constrained to a digit-only alphabet instead (see `tessedit_char_whitelist`
 * below), which prevents the substitution rather than papering over it.
 */
export function parseOrderNo(text: string): string | null {
  const match = ORDER_NO_RE.exec(text.toUpperCase().replace(/\s+/g, ''))
  return match ? match[0] : null
}

export function isOrderNo(value: string): boolean {
  const clean = value.trim().toUpperCase()
  return clean.length === 16 && ORDER_NO_RE.test(clean)
}

/**
 * What to try on an unframed page, in order, stopping at the first match.
 *
 * Both dimensions of this table were measured, not guessed, by running these
 * engine settings over a rasterised challan at varying crops and scales:
 *
 *  band @ 1000px  -> read correctly        band @ 1400px -> failed
 *  band @  650px  -> read correctly        band @  400px -> failed
 *  page @ 1200px  -> read correctly        page @ 1700px -> failed
 *  page @ 2400px  -> read correctly        page @ 3000px -> failed
 *
 * Two things follow. First, Tesseract has a resolution *window* rather than a
 * bigger-is-better curve — it wants roughly 25-35px of character height, and both
 * sides of that fail. Second, where that window sits depends on the crop, so a
 * single scale guess is a coin flip. Hence a cascade: the common case matches on
 * the first attempt and costs one pass, and the awkward case keeps trying instead
 * of reporting a miss that better framing would have solved.
 *
 * The band comes first because this template puts the Order No in the top-right
 * header block, beside the Delivery Challan Date, and because less text reaching
 * the regex means less to be confused by.
 */
export const PAGE_ATTEMPTS = [
  { box: { x: 0.42, y: 0.0, w: 0.58, h: 0.34 }, width: 1000 },
  { box: { x: 0.42, y: 0.0, w: 0.58, h: 0.34 }, width: 650 },
  { box: { x: 0, y: 0, w: 1, h: 1 }, width: 1200 },
  { box: { x: 0, y: 0, w: 1, h: 1 }, width: 2400 },
] as const

/** The camera's guide box, as a fraction of the video frame. */
export const GUIDE_BOX = { x: 0.1, y: 0.38, w: 0.8, h: 0.24 } as const

type Worker = {
  recognize: (image: unknown) => Promise<any>
  setParameters: (params: Record<string, unknown>) => Promise<unknown>
  terminate: () => void
}

let worker: Worker | null = null

/**
 * Prepare an image for Tesseract.
 *
 * Three operations, each earning its cost on a photographed page:
 *
 * - **Scale to a target.** A blind multiplier was the original behaviour and it is
 *   wrong for an upload: a scan is already high-resolution, and tripling it pushed
 *   the text straight past the top of the resolution window. Camera frames still
 *   want enlarging, so a multiplier remains the fallback when no target is given.
 * - **Greyscale.** Removes the colour noise a phone sensor adds under warehouse
 *   sodium lighting, which otherwise survives thresholding as speckle.
 * - **Contrast stretch, not a hard threshold.** A fixed cut-off destroys characters
 *   wherever a fold or a shadow crosses the line — and challans arrive folded.
 *   Stretching keeps the mid-tones a shadowed digit lives in.
 */
export function preprocess(source: HTMLCanvasElement, targetWidth?: number): HTMLCanvasElement {
  const scale = targetWidth ? targetWidth / source.width : 3
  const out = document.createElement('canvas')
  out.width = Math.max(1, Math.round(source.width * scale))
  out.height = Math.max(1, Math.round(source.height * scale))

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

/**
 * Get the OCR worker, creating it on first use, set up for one of two jobs.
 *
 * `line` is the camera path: the operator has framed a single line inside the
 * guide box, so SINGLE_LINE is exactly right.
 *
 * `page` is the upload path, where the image is a whole A4 challan with a table,
 * a barcode and an address block. SINGLE_LINE on that finds one imaginary line
 * across the whole sheet and returns mush; SPARSE_TEXT looks for text wherever it
 * happens to be, which is what an unframed page needs.
 *
 * The character whitelist is identical in both cases, and it is doing the same job
 * throughout: with O, S, B and I absent from the alphabet the engine cannot emit
 * the substitutions that would otherwise need "repairing".
 */
export async function getWorker(
  job: 'line' | 'page',
  onProgress?: (fraction: number) => void,
): Promise<Worker> {
  // Dynamically imported so the engine is fetched the first time someone actually
  // reads a challan — not in the bundle every guard downloads at the gate.
  const { createWorker, PSM } = await import('tesseract.js')

  if (!worker) {
    worker = (await createWorker('eng', 1, {
      workerPath: '/tesseract/worker.min.js',
      corePath: '/tesseract/',
      langPath: '/tesseract',
      logger: (m: { status: string; progress: number }) => {
        if (m.status === 'recognizing text') onProgress?.(m.progress)
      },
    })) as unknown as Worker
  }

  await worker.setParameters({
    tessedit_char_whitelist: 'CP0123456789_',
    tessedit_pageseg_mode: job === 'line' ? PSM.SINGLE_LINE : PSM.SPARSE_TEXT,
  })

  return worker
}

/**
 * Whether the engine is already resident.
 *
 * The first `getWorker` call downloads ~6MB of wasm and model data; every call
 * after it is instant. Callers use this to show "loading the OCR engine" for that
 * first one, because "Reading…" against a thirty-second download reads as a hang.
 */
export function isWorkerReady(): boolean {
  return worker !== null
}

/** Released on teardown. Kept alive between reads so the engine loads once. */
export function releaseWorker(): void {
  worker?.terminate()
  worker = null
}

export type OrderNoRead = {
  orderNo: string | null
  rawText: string
  confidence: number | null
  /** The crop the value came from, for the operator to check against. */
  image: HTMLCanvasElement
}

/**
 * Search page images for an Order No.
 *
 * Walks every page through the measured cascade and stops at the first match. On a
 * total miss it returns the first attempt's crop and text, because the header band
 * of page one is the most useful thing to show someone whose upload failed.
 */
export async function readOrderNoFromPages(
  pages: HTMLCanvasElement[],
  onProgress?: (info: { page: number; pages: number; attempt: number; attempts: number }) => void,
): Promise<OrderNoRead> {
  const engine = await getWorker('page')
  let first: OrderNoRead | null = null

  for (const [pageIndex, page] of pages.entries()) {
    for (const [index, attempt] of PAGE_ATTEMPTS.entries()) {
      onProgress?.({
        page: pageIndex + 1,
        pages: pages.length,
        attempt: index + 1,
        attempts: PAGE_ATTEMPTS.length,
      })

      const prepared = preprocess(
        cropRegion(page, page.width, page.height, attempt.box),
        attempt.width,
      )
      const result = await engine.recognize(prepared)
      const rawText: string = result?.data?.text ?? ''
      const confidence =
        typeof result?.data?.confidence === 'number' ? result.data.confidence : null
      const orderNo = parseOrderNo(rawText)

      if (orderNo) return { orderNo, rawText, confidence, image: prepared }
      first ??= { orderNo: null, rawText, confidence, image: prepared }
    }
  }

  return first!
}
