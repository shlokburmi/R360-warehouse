import { useCallback, useEffect, useRef, useState } from 'react'
import { BrowserMultiFormatReader } from '@zxing/browser'
import { DecodeHintType, BarcodeFormat } from '@zxing/library'

type Props = {
  onScan: (code: string) => void
  /** Ignore a repeat of the same code within this window (ms). */
  debounceMs?: number
  paused?: boolean
}

/**
 * Camera scanner for sticker QR codes.
 *
 * Two behaviours here are worth knowing about:
 *
 * 1. The same sticker sitting in frame decodes many times a second. Without the
 *    repeat guard the operator would fire dozens of identical scans by holding
 *    the camera still, so an identical code is ignored for `debounceMs`.
 *
 * 2. There is always a manual entry fallback. Cameras fail — cracked lenses,
 *    denied permissions, a sticker scuffed in transit — and a warehouse cannot
 *    stop because of it. The typed path produces exactly the same scan record,
 *    and the server judges it by exactly the same rules.
 */
export function QrScanner({ onScan, debounceMs = 2000, paused = false }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const controlsRef = useRef<{ stop: () => void } | null>(null)
  const lastScan = useRef<{ code: string; at: number }>({ code: '', at: 0 })

  const [status, setStatus] = useState<'starting' | 'running' | 'denied' | 'unavailable'>(
    'starting',
  )
  const [manual, setManual] = useState('')
  const [showManual, setShowManual] = useState(false)

  const handleCode = useCallback(
    (code: string) => {
      const clean = code.trim().toUpperCase()
      if (!clean) return

      const now = Date.now()
      if (lastScan.current.code === clean && now - lastScan.current.at < debounceMs) return
      lastScan.current = { code: clean, at: now }

      // Short haptic confirmation. On a noisy warehouse floor this is the only
      // feedback the operator reliably notices without looking at the screen.
      if ('vibrate' in navigator) navigator.vibrate(40)

      onScan(clean)
    },
    [onScan, debounceMs],
  )

  useEffect(() => {
    if (paused) return

    let cancelled = false
    let watchdog: number | undefined
    const hints = new Map()
    hints.set(DecodeHintType.POSSIBLE_FORMATS, [
      BarcodeFormat.QR_CODE,
      BarcodeFormat.CODE_128,
      BarcodeFormat.DATA_MATRIX,
    ])

    const reader = new BrowserMultiFormatReader(hints, { delayBetweenScanAttempts: 150 })

    // Two attempts, in order of preference.
    //
    // The first asks for the rear camera at a resolution high enough to read a
    // small code at arm's length without dropping the frame rate — right for
    // the phone or tablet this is actually used on.
    //
    // The second asks for nothing at all. A laptop has only a front camera and
    // may not offer 1280x720 on it; depending on the browser that surfaces as
    // an OverconstrainedError, or worse, as a stream that resolves and then
    // never produces a frame. Retrying bare means the desktop case degrades to
    // "the wrong camera, working" instead of "no camera", which matters because
    // desktop is where this gets demonstrated and tested.
    const attempts: MediaStreamConstraints[] = [
      {
        video: {
          facingMode: { ideal: 'environment' },
          width: { ideal: 1280 },
          height: { ideal: 720 },
        },
      },
      { video: true },
    ]

    /** Has the element actually painted a frame, rather than merely been handed a stream? */
    const painting = () => (videoRef.current?.videoWidth ?? 0) > 0

    async function start() {
      if (!videoRef.current) return

      let lastError: unknown = null

      for (const constraints of attempts) {
        if (cancelled) return

        try {
          const controls = await reader.decodeFromConstraints(
            constraints,
            videoRef.current,
            (result) => {
              if (result && !cancelled) handleCode(result.getText())
            },
          )

          if (cancelled) {
            controls.stop()
            return
          }

          controlsRef.current = controls

          // Acquiring a stream is not the same as painting it. Safari rejects
          // video.play() in situations it considers un-gestured, which leaves a
          // live MediaStream attached to an element that never renders a frame
          // while the component reports success. That is the one failure this
          // component must not have — its whole design is "cameras fail, so
          // always offer the typed path", and a silent black rectangle offers
          // nothing. So wait for a real frame before believing it worked.
          const ok = await new Promise<boolean>((resolve) => {
            const started = Date.now()
            const poll = () => {
              if (cancelled) return resolve(false)
              if (painting()) return resolve(true)
              if (Date.now() - started > 4000) return resolve(false)
              watchdog = window.setTimeout(poll, 200)
            }
            poll()
          })

          if (cancelled) return

          if (ok) {
            setStatus('running')
            return
          }

          // Nothing painted. Release this camera before trying the next set of
          // constraints, or the retry competes with a device we still hold.
          controls.stop()
          controlsRef.current = null
        } catch (error) {
          lastError = error
          if (cancelled) return
          // A denied permission will not be fixed by relaxing constraints, so
          // stop asking — retrying would just prompt the user again.
          if ((error as Error)?.name === 'NotAllowedError') break
        }
      }

      if (cancelled) return
      setStatus((lastError as Error)?.name === 'NotAllowedError' ? 'denied' : 'unavailable')
      setShowManual(true)
    }

    void start()

    return () => {
      cancelled = true
      if (watchdog) window.clearTimeout(watchdog)
      controlsRef.current?.stop()
      controlsRef.current = null
    }
  }, [handleCode, paused])

  return (
    <div className="space-y-3">
      {status !== 'denied' && status !== 'unavailable' && (
        <div className="viewfinder">
          {/* `autoPlay` is required, not decorative. Without it the element
              depends entirely on ZXing's internal play() call succeeding, and
              when that is rejected the stream is live but no frame is ever
              painted. `muted` is what makes autoplay permissible at all. */}
          <video ref={videoRef} autoPlay muted playsInline />
          {status === 'starting' && (
            <p className="absolute inset-0 flex items-center justify-center text-white">
              Starting camera…
            </p>
          )}
        </div>
      )}

      {status === 'denied' && (
        <div className="rounded-xl bg-warn-bg p-4 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
          <p className="font-bold">Camera permission is blocked.</p>
          <p className="mt-1 text-base">
            Allow camera access in your browser settings, or type the sticker code below.
          </p>
        </div>
      )}

      {status === 'unavailable' && (
        <div className="rounded-xl bg-warn-bg p-4 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
          <p className="font-bold">No camera available on this device.</p>
          <p className="mt-1 text-base">Type the sticker code below instead.</p>
        </div>
      )}

      {showManual ? (
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            handleCode(manual)
            setManual('')
          }}
        >
          <input
            className="input font-mono uppercase"
            placeholder="BOX-1A2B3C4D"
            value={manual}
            onChange={(event) => setManual(event.target.value)}
            autoCapitalize="characters"
            autoCorrect="off"
            spellCheck={false}
            aria-label="Sticker code"
          />
          <button type="submit" className="btn-primary" disabled={manual.trim().length < 3}>
            Add
          </button>
        </form>
      ) : (
        <button type="button" className="btn-ghost w-full" onClick={() => setShowManual(true)}>
          Type code instead
        </button>
      )}
    </div>
  )
}
