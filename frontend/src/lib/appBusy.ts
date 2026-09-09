/**
 * "Is the operator in the middle of something that a page reload would
 * destroy?"
 *
 * This exists for the PWA auto-update path (UpdatePrompt.tsx). A new deploy
 * should apply on its own — nobody on a warehouse floor should have to notice
 * a banner, let alone tap it — but the first version of that reloaded the
 * instant an update was detected, which threw away whatever was in flight: a
 * photo just picked for an OCR read, an Order No read but not yet confirmed, a
 * gate entry form with an identity photo already captured.
 *
 * React Query already knows about in-flight *mutations*, so those need no help
 * here. What it cannot see is local work: a camera stream the operator is
 * aiming, an OCR pass running in a worker, a read sitting on screen waiting to
 * be confirmed. Anything holding one of those marks itself busy, and the
 * updater waits for a quieter moment instead.
 *
 * Deliberately a plain counter rather than React state: the holders are spread
 * across unrelated components, several of them inside effects that must
 * release on unmount, and none of them need to re-render when the count
 * changes — only the updater cares, and it polls.
 */

let holders = 0

/** Mark the app busy. Returns the release — call it exactly once. */
export function markBusy(): () => void {
  holders += 1
  let released = false
  return () => {
    // Guarded because React can invoke a cleanup more than once in
    // development (StrictMode double-invokes effects), and a double release
    // would leave the count negative and the app permanently "not busy".
    if (released) return
    released = true
    holders = Math.max(0, holders - 1)
  }
}

export function isAppBusy(): boolean {
  return holders > 0
}
