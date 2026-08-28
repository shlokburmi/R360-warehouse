/**
 * Mirrors backend/app/schemas/gate.py's VEHICLE_RE/PO_REFERENCE_RE/MOBILE_RE
 * exactly, so a guard sees the same rule on the form that the server will
 * enforce anyway — the client-side copy exists only to fail fast and in
 * plain language, not as the actual boundary.
 */

// Exactly the shape of KA01AB1234 — 2 letters, 2 digits, 2 letters, 4 digits.
// Real plates occasionally vary; this is deliberately exact, per instruction:
// one fixed, compulsory format rather than accommodating every real-world one.
export const VEHICLE_RE = /^[A-Z]{2}[0-9]{2}[A-Z]{2}[0-9]{4}$/

export function cleanVehicleNumber(raw: string): string {
  return raw
    .toUpperCase()
    .split('')
    .filter((ch) => /[A-Z0-9]/.test(ch))
    .join('')
    .slice(0, 10)
}

export const MOBILE_RE = /^[6-9][0-9]{9}$/

// Same shape as real purchase_orders.po_number values (e.g. PO-2026-0001).
export const PO_REFERENCE_RE = /^PO-[0-9]{4}-[0-9]{4}$/
