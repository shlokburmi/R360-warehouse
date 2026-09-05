import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
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
 * 1. The same sticker sitting in frame decodes many times a second, so an
 *    identical code is ignored for `debounceMs`. This has to be time-based,
 *    not "seen once, never again" — this component has no visibility into
 *    whether a scan it already forwarded was later accepted or rejected
 *    (that is decided asynchronously by the server), so permanently
 *    blacklisting a code would also permanently block a legitimate retry
 *    (e.g. a scan rejected because a different box was still open — once
 *    that box is closed, the same sticker needs to be scannable again).
 *    `debounceMs` defaults long enough that holding the phone briefly
 *    unsteady on one sticker does not re-fire it.
 *
 * 2. There is always a manual entry fallback. Cameras fail — cracked lenses,
 *    denied permissions, a sticker scuffed in transit — and a warehouse cannot
 *    stop because of it. The typed path produces exactly the same scan record,
 *    and the server judges it by exactly the same rules.
 */
export function QrScanner({ onScan, debounceMs = 5000, paused = false }: Props) {
  const { t } = useTranslation()
  const videoRef = useRef<HTMLVideoElement>(null)
  const controlsRef = useRef<{ stop: () => void } | null>(null)
  const lastScan = useRef<{ code: string; at: number }>({ code: '', at: 0 })

  const [status, setStatus] = useState<'starting' | 'running' | 'denied' | 'unavailable'>(
    'starting',
  )
  const [manual, setManual] = useState('')
  const [showManual, setShowManual] = useState(false)

  // Callers routinely pass an inline `onScan` closure that changes identity on
  // every render (e.g. `onScan={(code) => submit(code, ...)}`). `handleCode`
  // must NOT depend on that identity, because it is also the trigger for the
  // camera-acquisition effect below — if it changed every render, the camera
  // stream would be torn down and reacquired every render too, and a sticker
  // presented to the lens during that teardown window decodes to nothing.
  // Routing through a ref keeps `handleCode` (and the camera) stable while
  // still always calling the latest `onScan`.
  const onScanRef = useRef(onScan)
  useEffect(() => {
    onScanRef.current = onScan
  }, [onScan])

  const handleCode = useCallback(
    (code: string) => {
      const clean = code.trim().toUpperCase()
      if (!clean) return

      // Short haptic confirmation on every decode, debounced or not. Without
      // this, a rescan inside `debounceMs` produces literally no signal —
      // not even a buzz — which reads as "the scanner is dead" rather than
      // "already counted". On a noisy warehouse floor this is the only
      // feedback the operator reliably notices without looking at the screen.
      if ('vibrate' in navigator) navigator.vibrate(40)

      const now = Date.now()
      if (lastScan.current.code === clean && now - lastScan.current.at < debounceMs) return
      lastScan.current = { code: clean, at: now }

      onScanRef.current(clean)
    },
    [debounceMs],
  )

  useEffect(() => {
    if (paused) return

    let cancelled = false
    let watchdog: number | undefined
    let cropInterval: number | undefined
    const hints = new Map()
    // Every code this app ever generates is a QR (qrcode_util.py is segno,
    // QR-only, for badges and every sticker type) — CODE_128 and DATA_MATRIX
    // were never actually produced anywhere. Restricting the search to the
    // one format that can ever match means every single decode attempt does
    // less work, which is the single biggest lever on how fast a scan lands.
    hints.set(DecodeHintType.POSSIBLE_FORMATS, [BarcodeFormat.QR_CODE])
    // TRY_HARDER trades a little per-attempt time for a more exhaustive
    // search — worth spending now that dropping two whole formats above
    // freed up that budget. It matters most for exactly the harder real
    // cases (a badge held at a slight angle, or read off a glossy phone
    // screen instead of a printed card): most "it won't scan" time is spent
    // repositioning after a missed read, not in the decode call itself, so
    // fewer misses is the actual speedup an operator feels.
    hints.set(DecodeHintType.TRY_HARDER, true)

    const reader = new BrowserMultiFormatReader(hints, { delayBetweenScanAttempts: 80 })

    // Three attempts, in order of preference.
    //
    // The first asks for the rear camera at a resolution high enough to read a
    // small code at arm's length without dropping the frame rate — right for
    // the phone or tablet this is actually used on. `ideal` is a soft
    // preference, not a requirement, so asking for more than a camera can do
    // never throws — it just negotiates down to whatever the camera actually
    // offers, same as it always did at the lower number. A sheet with several
    // small unit stickers packed close together needs more pixels per code
    // than a single box sticker at the same distance to resolve at all.
    //
    // The second drops only `facingMode` and keeps the same resolution/frame-
    // rate ask. A laptop has one front camera, no `environment` match at all,
    // so the first attempt's `exact` throws — and without this middle step
    // that fell straight through to the bare, unconstrained third attempt,
    // silently handing the browser's low default resolution (often 640x480)
    // to a case that specifically needs more pixels: a laptop scanning
    // someone else's *phone screen* held up to it (About Me's on-screen
    // badge) is already fighting moiré and glare, and a soft camera makes
    // that strictly harder for no reason — the laptop can ask for 1080p same
    // as a phone can, it just can't ask for a *rear* one.
    //
    // The third asks for nothing at all, for whatever neither preference
    // above negotiates — a stream that resolves and then never produces a
    // frame on some laptop/browser combinations, which the painting() check
    // further down exists to detect and fall through from.
    const attempts: MediaStreamConstraints[] = [
      // `exact`, not `ideal`: a phone or tablet always has a rear camera, and
      // the front one is never the right choice for scanning a sticker —
      // `ideal` is only a preference the browser can (and on some devices
      // does) ignore. `exact` throws OverconstrainedError instead of
      // silently picking the front camera, which is exactly what should
      // happen: the next attempt below still catches a laptop with only a
      // front camera.
      {
        video: {
          facingMode: { exact: 'environment' },
          width: { ideal: 1920 },
          height: { ideal: 1080 },
          // More frames per second is more chances to land a decode inside
          // the same half-second an operator holds a badge under the lens.
          // `ideal`, so a camera that only offers less still connects.
          frameRate: { ideal: 30 },
        },
      },
      {
        video: {
          width: { ideal: 1920 },
          height: { ideal: 1080 },
          frameRate: { ideal: 30 },
        },
      },
      { video: true },
    ]

    /** Has the element actually painted a frame, rather than merely been handed a stream? */
    const painting = () => (videoRef.current?.videoWidth ?? 0) > 0

    // Second, narrower decode pass, additive to the full-frame one above —
    // it never replaces it, only runs alongside it, so a single isolated
    // code (every scan type except a dense unit-sticker sheet) keeps working
    // exactly as it always has even if this crop math is ever wrong for some
    // device. Either loop calling handleCode is debounced the same way.
    //
    // The crop matches what the viewfinder guide actually highlights (see
    // .viewfinder / .viewfinder::after in index.css: a 4:3 box, video at
    // `object-fit: cover`, guide inset 18% top/bottom and 12% left/right) —
    // cropping to just that region and upscaling it is equivalent to zooming
    // in on the sticker the operator is actually aiming at, which a full-frame
    // decode cannot do. A sheet with several small stickers close together can
    // be too fine-grained for zxing to resolve at full-frame scale at all.
    const cropCanvas = document.createElement('canvas')
    const cropCtx = cropCanvas.getContext('2d', { willReadFrequently: true })

    function decodeCroppedFrame() {
      const video = videoRef.current
      if (!video || !cropCtx || video.videoWidth === 0 || cancelled) return

      const containerAspect = 4 / 3
      const videoAspect = video.videoWidth / video.videoHeight

      let visibleW: number
      let visibleH: number
      let offsetX: number
      let offsetY: number
      if (videoAspect > containerAspect) {
        // Video wider than the 4:3 box: full height visible, sides cropped.
        visibleH = video.videoHeight
        visibleW = video.videoHeight * containerAspect
        offsetX = (video.videoWidth - visibleW) / 2
        offsetY = 0
      } else {
        // Video narrower/taller than the box: full width visible, top/bottom cropped.
        visibleW = video.videoWidth
        visibleH = video.videoWidth / containerAspect
        offsetX = 0
        offsetY = (video.videoHeight - visibleH) / 2
      }

      const cropX = offsetX + 0.12 * visibleW
      const cropY = offsetY + 0.18 * visibleH
      const cropW = 0.76 * visibleW
      const cropH = 0.64 * visibleH
      if (cropW <= 0 || cropH <= 0) return

      // Upscale so a small crop still hands the decoder a decode-friendly
      // number of pixels, capped so this stays cheap on a low-end phone.
      const targetW = Math.min(1000, Math.round(cropW * 2))
      const targetH = Math.round(targetW * (cropH / cropW))
      if (cropCanvas.width !== targetW || cropCanvas.height !== targetH) {
        cropCanvas.width = targetW
        cropCanvas.height = targetH
      }

      cropCtx.drawImage(video, cropX, cropY, cropW, cropH, 0, 0, targetW, targetH)

      try {
        const result = reader.decodeFromCanvas(cropCanvas)
        if (result && !cancelled) handleCode(result.getText())
      } catch {
        // No code in this crop this tick — expected on almost every tick.
      }
    }

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
            cropInterval = window.setInterval(decodeCroppedFrame, 150)

            // Best-effort: a badge is now sometimes held up as a phone
            // screen a few centimetres from the lens (0037) rather than a
            // sticker at arm's length, which is closer than this camera's
            // default autofocus range is tuned for.
            //
            // Only attempted when the camera's own reported capabilities
            // list 'continuous' focusMode support — most laptop webcams
            // (fixed-focus) don't, and asking anyway risks the browser
            // renegotiating the track into a broken state (observed as a
            // black video element) rather than cleanly rejecting, which is
            // why this is gated on capabilities rather than just caught.
            // `streamVideoConstraintsApply` is also async, so a bare
            // try/catch around the call site would not catch a rejection
            // anyway — `.catch()` on the promise it returns does.
            try {
              // @zxing/browser's own .d.ts mistypes both of these (a
              // `find`-style boolean predicate as one returning
              // `MediaStreamTrack[]`, and an async applier as returning
              // `void`) — casts here match what the implementation
              // actually does, not what its types claim.
              const capsGet = controls.streamVideoCapabilitiesGet as
                | ((trackFilter: (track: MediaStreamTrack) => boolean) => MediaTrackCapabilities)
                | undefined
              const capabilities = capsGet?.(() => true) as
                | (MediaTrackCapabilities & { focusMode?: string[] })
                | undefined

              if (capabilities?.focusMode?.includes('continuous')) {
                const applyConstraints = controls.streamVideoConstraintsApply as
                  | ((constraints: MediaTrackConstraints) => Promise<void>)
                  | undefined
                void applyConstraints
                  ?.({ advanced: [{ focusMode: 'continuous' } as MediaTrackConstraintSet] })
                  .catch(() => {
                    // Fixed/auto focus is what this camera had anyway.
                  })
              }
            } catch {
              // getCapabilities() itself can throw on some browsers/devices.
            }

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
      if (cropInterval) window.clearInterval(cropInterval)
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
              {t('scanner.starting')}
            </p>
          )}
        </div>
      )}

      {status === 'denied' && (
        <div className="rounded-xl bg-warn-bg p-4 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
          <p className="font-bold">{t('scanner.permission_blocked')}</p>
          <p className="mt-1 text-base">
            {t('scanner.permission_hint')}
          </p>
        </div>
      )}

      {status === 'unavailable' && (
        <div className="rounded-xl bg-warn-bg p-4 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
          <p className="font-bold">{t('scanner.unavailable')}</p>
          <p className="mt-1 text-base">{t('scanner.unavailable_hint')}</p>
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
            aria-label={t('scanner.sticker_code')}
          />
          <button type="submit" className="btn-primary" disabled={manual.trim().length < 3}>
            {t('common.add')}
          </button>
        </form>
      ) : (
        <button type="button" className="btn-ghost w-full" onClick={() => setShowManual(true)}>
          {t('common.type_code_instead')}
        </button>
      )}
    </div>
  )
}
