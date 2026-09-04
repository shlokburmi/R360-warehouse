/**
 * Turn an uploaded file into page canvases, whatever the file is.
 *
 * Both the invoice capture and the Order No reader need the same thing: a photo,
 * a scan or a PDF reduced to a list of bitmaps they can crop and analyse. Keeping
 * that in one place means a PDF quirk gets fixed once — and there are several,
 * documented below.
 */

/**
 * Longest edge an uploaded *image* is held at.
 *
 * A modern phone photo is 12MP or more. Every consumer rescales from this, so
 * holding the original would make each pass slower for no accuracy.
 */
export const MAX_EDGE = 3000

/**
 * How wide a PDF page is rasterised.
 *
 * A PDF is vector text at 72dpi nominal, so unlike a photo there is no inherent
 * resolution to respect — we pick one. 2000px across A4 is ~240dpi, above every
 * scale the OCR cascade then targets, so it is always scaling *down*. Scaling
 * down preserves stroke shape; scaling up invents it.
 */
export const PDF_RENDER_WIDTH = 2000

/** Pages to look at before giving up. Each one costs a full analysis pass. */
export const PDF_MAX_PAGES = 5

export class UnreadableFile extends Error {
  constructor(readonly kind: 'pdf' | 'image') {
    super(`unreadable ${kind}`)
  }
}

export function isPdf(file: File): boolean {
  return file.type === 'application/pdf' || /\.pdf$/i.test(file.name)
}

async function renderPdf(
  file: File,
  onPage?: (n: number, total: number) => void,
): Promise<HTMLCanvasElement[]> {
  // Dynamically imported: pdf.js is ~424KB plus a 1.2MB worker, and a guard
  // scanning boxes at a gate must not download a PDF engine to do it.
  const pdfjs = await import('pdfjs-dist')
  // Served from our own origin rather than a CDN, so this keeps working on a
  // warehouse network with no route to the internet.
  pdfjs.GlobalWorkerOptions.workerSrc = '/tesseract/pdf.worker.min.mjs'

  const doc = await pdfjs.getDocument({ data: await file.arrayBuffer() }).promise
  const total = Math.min(doc.numPages, PDF_MAX_PAGES)
  const pages: HTMLCanvasElement[] = []

  for (let n = 1; n <= total; n++) {
    onPage?.(n, total)
    const page = await doc.getPage(n)
    const base = page.getViewport({ scale: 1 })
    const viewport = page.getViewport({ scale: PDF_RENDER_WIDTH / base.width })
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(viewport.width)
    canvas.height = Math.round(viewport.height)
    // `background` is not cosmetic. A PDF page is transparent wherever nothing is
    // drawn, and transparent composites to black on a fresh canvas — which would
    // give white-on-black and read as a blank page. pdf.js has this option
    // precisely for rasterising, so use it rather than pre-filling the context
    // and hoping the renderer does not clear it.
    //
    // `canvas` rather than `canvasContext`: in pdf.js v5 the context form is kept
    // only for backwards compatibility.
    await page.render({ canvas, viewport, background: '#ffffff' }).promise
    pages.push(canvas)
  }

  void doc.destroy()
  return pages
}

async function loadImage(file: File): Promise<HTMLCanvasElement> {
  // createImageBitmap honours EXIF orientation, which matters: a phone photo of a
  // challan is very often flagged rotated rather than stored rotated, and an
  // <img> drawn to canvas would come out sideways.
  const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
  const scale = Math.min(1, MAX_EDGE / Math.max(bitmap.width, bitmap.height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(bitmap.width * scale)
  canvas.height = Math.round(bitmap.height * scale)
  canvas.getContext('2d')!.drawImage(bitmap, 0, 0, canvas.width, canvas.height)
  bitmap.close()
  return canvas
}

/**
 * A PDF or an image, as page canvases.
 *
 * Throws `UnreadableFile` for a password-protected or truncated PDF, for HEIC in
 * a browser with no decoder for it, and for anything that is not really the type
 * its extension claims. Callers are expected to say which of those happened
 * rather than showing a generic failure.
 */
export async function filesToPages(
  file: File,
  onPage?: (n: number, total: number) => void,
): Promise<HTMLCanvasElement[]> {
  const pdf = isPdf(file)
  try {
    const pages = pdf ? await renderPdf(file, onPage) : [await loadImage(file)]
    if (pages.length === 0) throw new Error('no pages')
    return pages
  } catch {
    throw new UnreadableFile(pdf ? 'pdf' : 'image')
  }
}

/**
 * Map a guide box authored as a fraction of a `.viewfinder`-style container
 * (a fixed `containerAspect`, video shown with `object-fit: cover`) into a
 * fraction of the *raw* media frame.
 *
 * A guide box like `GUIDE_BOX` in readOrderNo.ts is drawn over what the
 * operator sees, which equals the raw frame's own fractions only when the
 * camera happens to negotiate exactly `containerAspect`. Any other aspect
 * ratio — routine on a phone's rear camera — means `object-fit: cover` is
 * itself cropping off-centre strips the operator never sees, so applying
 * the guide box's fractions straight to the raw frame reads pixels outside
 * what was actually framed (extra header/date-line text above or below the
 * Order No, in practice). QrScanner.tsx's `decodeCroppedFrame` solves the
 * same problem inline for the barcode path; this is the same geometry,
 * shared so OrderNoScanner doesn't reimplement it slightly differently.
 */
export function coverCropBox(
  mediaWidth: number,
  mediaHeight: number,
  containerAspect: number,
  box: { x: number; y: number; w: number; h: number },
): { x: number; y: number; w: number; h: number } {
  const mediaAspect = mediaWidth / mediaHeight

  let visibleW: number
  let visibleH: number
  let offsetX: number
  let offsetY: number
  if (mediaAspect > containerAspect) {
    // Media wider than the container: full height visible, sides cropped.
    visibleH = mediaHeight
    visibleW = mediaHeight * containerAspect
    offsetX = (mediaWidth - visibleW) / 2
    offsetY = 0
  } else {
    // Media narrower/taller than the container: full width visible, top/bottom cropped.
    visibleW = mediaWidth
    visibleH = mediaWidth / containerAspect
    offsetX = 0
    offsetY = (mediaHeight - visibleH) / 2
  }

  return {
    x: (offsetX + box.x * visibleW) / mediaWidth,
    y: (offsetY + box.y * visibleH) / mediaHeight,
    w: (box.w * visibleW) / mediaWidth,
    h: (box.h * visibleH) / mediaHeight,
  }
}

/** Crop a fractional region out of a canvas or video frame. */
export function cropRegion(
  from: CanvasImageSource,
  width: number,
  height: number,
  box: { x: number; y: number; w: number; h: number },
): HTMLCanvasElement {
  const out = document.createElement('canvas')
  out.width = Math.max(1, Math.round(width * box.w))
  out.height = Math.max(1, Math.round(height * box.h))
  out
    .getContext('2d')!
    .drawImage(
      from,
      Math.round(width * box.x),
      Math.round(height * box.y),
      out.width,
      out.height,
      0,
      0,
      out.width,
      out.height,
    )
  return out
}
