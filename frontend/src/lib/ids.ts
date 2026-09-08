/**
 * A v4 UUID, on every device this app has to run on.
 *
 * `crypto.randomUUID()` is not always there:
 *
 * - It requires a **secure context**. Served over plain http — which is exactly
 *   how this app gets tested from a phone on the office wifi (`vite --host`),
 *   and how it would be served by any internal http deployment — `crypto`
 *   exists but `randomUUID` does not. Calling it threw, which took out the
 *   whole scanning loop and the gate-entry form on first render.
 * - It only arrived in Chrome/WebView 92, Safari 15.4 and Firefox 95. An older
 *   Android WebView is a plausible warehouse phone.
 *
 * The ids this mints are not secrets — they are idempotency keys for scans and
 * React list keys — but they do have to be unique and shaped like a UUID,
 * because the API validates `client_event_id` as one.
 */
export function newUuid(): string {
  const c: Crypto | undefined = globalThis.crypto

  if (typeof c?.randomUUID === 'function') return c.randomUUID()

  const bytes = new Uint8Array(16)

  if (typeof c?.getRandomValues === 'function') {
    c.getRandomValues(bytes)
  } else {
    // No crypto at all: an ancient WebView, or a context that has locked it
    // down. Math.random is not a source of unguessable values, and does not
    // need to be here — a duplicate id would be absorbed by the scan queue's
    // own idempotency rather than doing harm. The timestamp keeps two ids
    // minted in the same millisecond apart.
    const stamp = Date.now()
    for (let i = 0; i < 16; i++) {
      bytes[i] = (Math.random() * 256) ^ ((stamp >>> (i % 4) * 8) & 0xff)
    }
  }

  // Version 4, variant 1, per RFC 4122 §4.4.
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80

  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
