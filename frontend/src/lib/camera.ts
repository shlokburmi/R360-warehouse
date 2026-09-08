/**
 * Why the camera is not available — when the answer is knowable.
 *
 * `navigator.mediaDevices` only exists in a **secure context**. Served over
 * plain http (a laptop's LAN address during device testing, or any internal
 * http deployment) the browser withholds it entirely, and every camera surface
 * in this app then reports "no camera available on this device" — about a phone
 * that plainly has one. The operator's next move is to look for a hardware
 * fault, which is the wrong hunt.
 *
 * Returns a translation key, so the caller keeps saying it in the user's
 * language.
 */
export function noCameraReasonKey(): 'scanner.needs_https' | 'scanner.unavailable_hint' {
  const secure = typeof window !== 'undefined' && window.isSecureContext !== false
  const hasApi = typeof navigator !== 'undefined' && Boolean(navigator.mediaDevices)
  return !secure && !hasApi ? 'scanner.needs_https' : 'scanner.unavailable_hint'
}
