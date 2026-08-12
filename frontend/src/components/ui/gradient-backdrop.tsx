import { useEffect, useRef } from 'react'
import { gsap } from 'gsap'

import { cn } from '@/lib/utils'

/**
 * The page wash from the reference design's hero, as an app-wide backdrop.
 *
 * The gradient itself lives in CSS (`.page-wash`, styles/index.css) rather than
 * an inline style, so it can be theme-aware and can be switched off for print
 * in one place. GSAP is used only for the entrance, which is what the source
 * component used it for.
 *
 * Two things this deliberately does *not* do:
 *
 * It does not animate on every route change. The backdrop is mounted once by
 * `Layout`, above the router outlet, so navigating between pages leaves it
 * alone — a wash that re-fades on every tap would be noticeable in exactly the
 * way a backdrop should not be.
 *
 * It does not intercept pointer events. `.page-wash` is `pointer-events-none`
 * and negative z-index; a full-viewport fixed element that swallowed taps would
 * break every button on every screen, and it would do so only on touch.
 */
export function GradientBackdrop({ className, drift = true }: { className?: string; drift?: boolean }) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!ref.current) return

    // Respect the OS setting here as well as in CSS. The stylesheet clamps
    // animation *duration*, but GSAP writes inline styles directly, so it has
    // to be asked separately or it would animate regardless.
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      gsap.set(ref.current, { opacity: 1, y: 0 })
      return
    }

    const tween = gsap.fromTo(
      ref.current,
      { opacity: 0, y: -30 },
      { opacity: 1, y: 0, duration: 1.6, ease: 'power3.out' },
    )

    return () => {
      tween.kill()
    }
  }, [])

  return <div ref={ref} aria-hidden className={cn('page-wash', drift && 'page-wash-drift', className)} />
}
