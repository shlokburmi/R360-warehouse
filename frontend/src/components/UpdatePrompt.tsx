import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router-dom'
import { useIsMutating } from '@tanstack/react-query'
import { useRegisterSW } from 'virtual:pwa-register/react'

import { isAppBusy } from '@/lib/appBusy'

/**
 * Applies a new deploy on its own, without a banner and without asking.
 *
 * Two earlier versions of this were both wrong in opposite directions:
 *
 * - `registerType: 'autoUpdate'` reloaded the page the instant an update was
 *   detected. Since Vercel rebuilds the whole app on every push — including
 *   backend-only pushes — that meant a deploy could wipe whatever an operator
 *   was mid-way through, with no warning: a photo just picked for an OCR read,
 *   an Order No read but not yet confirmed, a half-filled gate entry form with
 *   the identity photo already taken.
 * - A "New version — reload now" banner fixed that by making it a choice, but
 *   a choice is a thing to notice and tap, and an operator with a box in one
 *   hand does neither. Updates then simply didn't get applied.
 *
 * So: automatic, but at a moment that costs nothing. Reload immediately while
 * the tab is hidden — the operator is not looking at it, which is the safest
 * moment there is and, on a phone that gets locked and pocketed between
 * trucks, arrives constantly. While the tab *is* visible, wait for the app to
 * be quiet: no in-flight mutation (React Query knows), nothing holding a
 * camera or an unconfirmed OCR read (`appBusy`), and no interaction for a
 * while, so a reload cannot land between typing a field and saving it.
 *
 * The service worker itself stays on `registerType: 'prompt'` so that it never
 * reloads on its own timing — this component owns when it happens.
 */

/** How long the tab must be idle before a visible-tab reload is allowed. */
const IDLE_BEFORE_RELOAD_MS = 60_000

/** How often to re-check whether it has become safe to reload. */
const CHECK_INTERVAL_MS = 5_000

export function UpdatePrompt() {
  const {
    needRefresh: [needRefresh],
    updateServiceWorker,
  } = useRegisterSW()

  // Any write in flight — a scan being submitted, an approval being decided —
  // is work the reload would abandon halfway.
  const mutating = useIsMutating()

  // Changing page is the most common safe moment there is: the screen being
  // left is unmounting anyway, so its in-progress state is already gone and
  // a reload costs nothing extra. Without this, an operator working steadily
  // on one visible screen keeps resetting the idle timer below and never
  // gets the update at all — which is exactly what happened while testing a
  // fix that had already shipped.
  const { pathname } = useLocation()

  const lastInteraction = useRef(Date.now())

  // The route this component last saw. Needed because the effect below also
  // re-runs when `needRefresh` first flips true — which can happen while the
  // operator is mid-task on a page they have not left. Treating that as a
  // navigation would reload out from under them, the exact interruption this
  // component exists to avoid, so only a genuine change of path counts.
  const seenPath = useRef(pathname)

  useEffect(() => {
    const touch = () => {
      lastInteraction.current = Date.now()
    }
    // pointerdown/keydown rather than mousemove: the question is "did the
    // operator just *do* something", not "is the cursor moving".
    window.addEventListener('pointerdown', touch, { passive: true })
    window.addEventListener('keydown', touch)
    return () => {
      window.removeEventListener('pointerdown', touch)
      window.removeEventListener('keydown', touch)
    }
  }, [])

  useEffect(() => {
    if (!needRefresh) return

    let done = false

    const attempt = ({ navigated = false } = {}) => {
      if (done) return
      if (mutating > 0 || isAppBusy()) return

      // A page change needs no idle wait — the screen being left is
      // unmounting regardless, so there is nothing left to interrupt.
      const idleFor = Date.now() - lastInteraction.current
      if (!navigated && !document.hidden && idleFor < IDLE_BEFORE_RELOAD_MS) return

      done = true
      // `true` reloads the page once the new service worker takes control.
      void updateServiceWorker(true)
    }

    // On arrival at a *different* route, on every tab hide/show, and on a
    // slow timer for the case where the operator simply puts the phone down
    // on a visible screen.
    const navigated = seenPath.current !== pathname
    seenPath.current = pathname
    attempt({ navigated })
    const onVisibility = () => attempt()
    const timer = window.setInterval(() => attempt(), CHECK_INTERVAL_MS)
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [needRefresh, mutating, pathname, updateServiceWorker])

  // Nothing to render: the whole point is that this is invisible.
  return null
}
