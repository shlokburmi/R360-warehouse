import { useTranslation } from 'react-i18next'
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
 *
 * ---------------------------------------------------------------------------
 * WHY THIS IS SIZED IN MILLIMETRES
 * ---------------------------------------------------------------------------
 *
 * It used to be laid out in CSS pixels — a 340px card around a 220px QR — which
 * a printer renders at roughly 90mm wide with a 58mm QR: most of an A4 sheet for
 * one badge, and far too big to stick onto an ID card, which is what actually
 * happens to it. Pixels are the wrong unit for something whose whole job is to
 * end up a specific physical size, so the label is specified in mm and the
 * on-screen view is the same element at the same size — what you see is the
 * sticker you get.
 *
 * The QR is 24mm square including its quiet zone. A badge code (`BDG-` + 16 hex)
 * is a 29-module version-3 symbol at error correction H, so with the 4-module
 * border segno draws, 24mm works out at ~0.65mm per module — comfortably above
 * the ~0.5mm where phone cameras start to struggle, so shrinking the label does
 * not cost scan reliability. Error correction stays at H (qrcode_util.py): a
 * laminated card that lives in a pocket for a year gets scuffed, and that is
 * exactly what H is for.
 *
 * The policy sentences stay on screen only. On a 52mm label there is no room for
 * them, and the person who needs to read them is the Admin doing the issuing,
 * who is looking at this screen.
 */
export function BadgeCardPrint({
  issued,
  onDone,
}: {
  issued: BadgeIssued
  onDone: () => void
}) {
  const { t } = useTranslation()

  return (
    <div>
      <style>{`
        #badge-label {
          width: 52mm;
          box-sizing: border-box;
          display: flex;
          align-items: center;
          gap: 2.5mm;
          padding: 2.5mm;
          border: 0.3mm solid #000;
          border-radius: 1.5mm;
          background: #fff;
          color: #000;
          font-family: ui-sans-serif, system-ui, sans-serif;
        }
        #badge-label .qr { width: 24mm; height: 24mm; display: block; flex: none; }
        #badge-label .name { font-size: 9pt; font-weight: 800; line-height: 1.15; }
        #badge-label .role { font-size: 6.5pt; font-weight: 600; line-height: 1.2; }
        #badge-label .code { font-size: 7pt; font-family: ui-monospace, monospace; }
        #badge-label .org {
          font-size: 5pt;
          font-weight: 700;
          letter-spacing: 0.04em;
          text-transform: uppercase;
        }

        @media print {
          /* A small margin only — the label is 52mm, so the rest of the sheet
             is deliberately left blank rather than the label being scaled to
             fill it. */
          @page { margin: 10mm; }
          body * { visibility: hidden; }
          #badge-label, #badge-label * { visibility: visible; }
          #badge-label { position: absolute; left: 0; top: 0; }
          /* Without this some browsers drop the border and lighten the QR to
             save ink, and a QR printed grey is a QR that does not scan. */
          #badge-label, #badge-label * {
            -webkit-print-color-adjust: exact;
            print-color-adjust: exact;
          }
          .no-print { display: none !important; }
        }
      `}</style>

      <div id="badge-label">
        <img className="qr" src={issued.badge_qr} alt="" />
        <div className="min-w-0">
          <p className="org">Reward360 · not a login</p>
          <p className="name">{issued.staff.full_name}</p>
          <p className="role">{issued.staff.role_label}</p>
          <p className="code">{issued.staff.employee_code}</p>
        </div>
      </div>

      {/* Screen-only controls translate; the label above deliberately does not —
          it is ink on paper read by couriers and auditors, and we cannot assume
          the printer has a Kannada face loaded. */}
      <div className="no-print mt-4 space-y-3">
        <p className="text-base text-slate-600 dark:text-slate-400">
          Prints at 52 × 29mm — cut along the border and stick it on the ID card.
          Scanning this badge records who handled an item. It grants no access.
          If lost, tell an Admin — the badge is replaced, not recovered.
        </p>

        <div className="flex flex-col gap-3 sm:flex-row">
          <button type="button" className="btn-primary flex-1" onClick={() => window.print()}>
            {t('badge.print')}
          </button>
          <button type="button" className="btn-ghost flex-1" onClick={onDone}>
            {t('badge.printed_done')}
          </button>
        </div>
      </div>
    </div>
  )
}
