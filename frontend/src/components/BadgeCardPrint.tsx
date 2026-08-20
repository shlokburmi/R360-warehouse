import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import QRCode from 'qrcode'
import type { BadgeIssued } from '@/types'

/**
 * The printable attribution badge (DECISIONS.md §1).
 *
 * This component is the only place in the frontend that ever holds a badge
 * code, and it holds it until the Admin navigates away. That is deliberate and
 * it is the whole reason the page nags about printing: there is no endpoint
 * that reads a code back, so a card that is not printed now must be reissued.
 *
 * The code is printed as a QR and **not** as text, which is the opposite of the
 * box and unit stickers. On a sticker the human-readable code is a fallback for
 * a scuffed QR. On a badge it would be a fallback for the security property —
 * anyone who glanced at the card could type the code at a station and attribute
 * work to its holder. A damaged badge is reissued instead.
 */
export function BadgeCardPrint({
  issued,
  onDone,
}: {
  issued: BadgeIssued
  onDone: () => void
}) {
  const { t } = useTranslation()
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    if (!canvasRef.current) return
    void QRCode.toCanvas(canvasRef.current, issued.badge_code, {
      width: 220,
      margin: 1,
      // A badge lives in a pocket for a year. Same reasoning as the stickers,
      // more so.
      errorCorrectionLevel: 'H',
    })
  }, [issued.badge_code])

  return (
    <div>
      <style>{`
        @media print {
          body * { visibility: hidden; }
          #badge-card, #badge-card * { visibility: visible; }
          #badge-card { position: absolute; left: 0; top: 0; }
          .no-print { display: none !important; }
        }
      `}</style>

      <div
        id="badge-card"
        className="mx-auto w-[340px] max-w-full rounded-xl border-2 border-slate-900 bg-white p-5 text-center text-black"
      >
        <p className="text-xs font-bold uppercase tracking-widest">Reward360 Warehouse</p>
        <p className="mt-0.5 text-[10px] font-semibold uppercase tracking-wider">
          Attribution badge · not a login
        </p>

        <canvas ref={canvasRef} className="mx-auto mt-3" />

        <p className="mt-3 text-2xl font-black leading-tight">{issued.staff.full_name}</p>
        <p className="text-base font-bold">{issued.staff.role_label}</p>
        <p className="mt-0.5 font-mono text-sm">{issued.staff.employee_code}</p>

        <p className="mt-3 border-t border-slate-300 pt-2 text-[10px] leading-snug">
          Scanning this badge records who handled an item. It grants no access.
          If lost, tell an Admin — the badge is replaced, not recovered.
        </p>
      </div>

      {/* Screen-only controls translate; the card above deliberately does not —
          it is ink on paper read by couriers and auditors, and we cannot assume
          the printer has a Kannada face loaded. */}
      <div className="no-print mt-4 flex flex-col gap-3 sm:flex-row">
        <button type="button" className="btn-primary flex-1" onClick={() => window.print()}>
          {t('badge.print')}
        </button>
        <button type="button" className="btn-ghost flex-1" onClick={onDone}>
          {t('badge.printed_done')}
        </button>
      </div>
    </div>
  )
}
