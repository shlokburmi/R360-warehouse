import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import QRCode from 'qrcode'
import type { StickerSheet } from '@/types'

/**
 * Printable sticker sheet (PRD Step 2/3).
 *
 * Each sticker carries the code as a QR *and* as human-readable text. The text
 * is not decoration: a scuffed or partly-peeled QR still needs to be typeable,
 * and the manual-entry fallback in the scanner exists precisely for that case.
 *
 * Box stickers also print the expected unit count on the face, so an offloader
 * can sanity-check a box without opening the app at all.
 */
export function StickerSheetPrint({ sheet }: { sheet: StickerSheet }) {
  const { t } = useTranslation()
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const canvases = containerRef.current?.querySelectorAll<HTMLCanvasElement>('canvas[data-code]')
    canvases?.forEach((canvas) => {
      const code = canvas.dataset.code
      if (!code) return
      void QRCode.toCanvas(canvas, code, {
        width: 128,
        margin: 1,
        // High error correction: these stickers get scuffed in transit, and a
        // code that survives a torn corner is worth the extra density.
        errorCorrectionLevel: 'H',
      })
    })
  }, [sheet])

  return (
    <div ref={containerRef}>
      <style>{`
        @media print {
          body * { visibility: hidden; }
          #sticker-sheet, #sticker-sheet * { visibility: visible; }
          #sticker-sheet { position: absolute; left: 0; top: 0; width: 100%; }
          .sticker { break-inside: avoid; page-break-inside: avoid; }
          .no-print { display: none !important; }
        }
      `}</style>

      <div className="no-print mb-4 flex items-center justify-between gap-3">
        <div className="min-w-0">
          <p className="text-lg font-bold">
            {sheet.quantity} {sheet.sticker_type} sticker{sheet.quantity === 1 ? '' : 's'}
          </p>
          <p className="text-base text-slate-500 dark:text-slate-400">
            Issued by {sheet.generated_by_name} ·{' '}
            {new Date(sheet.generated_at).toLocaleString()}
          </p>
        </div>
        <button type="button" className="btn-primary" onClick={() => window.print()}>
          {t('stickers.print_sheet')}
        </button>
      </div>

      <div
        id="sticker-sheet"
        className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4"
      >
        {sheet.stickers.map((sticker) => (
          <div
            key={sticker.id}
            className="sticker flex flex-col items-center rounded-lg border-2 border-slate-900 bg-white p-3 text-center text-black"
          >
            <canvas data-code={sticker.code} />
            <p className="mt-2 font-mono text-sm font-bold">{sticker.code}</p>

            {sticker.sticker_type === 'box' ? (
              <>
                <p className="mt-1 text-2xl font-black leading-none">
                  {sticker.expected_units} units
                </p>
                <p className="text-xs font-semibold">Box {sticker.box_number}</p>
              </>
            ) : (
              <p className="mt-1 text-xs font-semibold">Box {sticker.box_number}</p>
            )}

            {sticker.sku && <p className="mt-0.5 text-[10px] leading-tight">{sticker.sku}</p>}
          </div>
        ))}
      </div>
    </div>
  )
}
