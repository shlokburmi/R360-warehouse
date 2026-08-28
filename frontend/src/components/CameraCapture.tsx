import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { post } from '@/lib/api'

type Props = {
  mobile: string
  onUploaded: (path: string) => void
}

/**
 * ID-photo capture that actually opens the camera, not the photo gallery.
 *
 * The previous version was `<input type="file" accept="image/*"
 * capture="environment">`. That attribute is only a *hint* — depending on
 * the browser/OS combination it can open a chooser with "Camera" as one
 * option among several, rather than the camera itself, which is exactly the
 * "opens gallery" complaint. This instead asks for the camera stream
 * directly via getUserMedia, the same mechanism QrScanner.tsx already uses
 * for sticker scanning, including its "a stream was granted but never
 * painted a frame" safety check and its "cameras fail, always offer a
 * fallback" philosophy — copied here rather than re-invented, since that
 * component has already survived real devices in the field.
 */
export function CameraCapture({ mobile, onUploaded }: Props) {
  const { t } = useTranslation()
  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const [status, setStatus] = useState<'starting' | 'running' | 'denied' | 'unavailable'>(
    'starting',
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    let watchdog: number | undefined

    const attempts: MediaStreamConstraints[] = [
      { video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 }, height: { ideal: 960 } } },
      { video: true },
    ]

    const painting = () => (videoRef.current?.videoWidth ?? 0) > 0

    async function start() {
      let lastError: unknown = null

      for (const constraints of attempts) {
        if (cancelled) return
        try {
          const stream = await navigator.mediaDevices.getUserMedia(constraints)
          if (cancelled) {
            stream.getTracks().forEach((tr) => tr.stop())
            return
          }
          streamRef.current = stream
          if (videoRef.current) videoRef.current.srcObject = stream

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

          stream.getTracks().forEach((tr) => tr.stop())
          streamRef.current = null
        } catch (err) {
          lastError = err
          if (cancelled) return
          if ((err as Error)?.name === 'NotAllowedError') break
        }
      }

      if (cancelled) return
      setStatus((lastError as Error)?.name === 'NotAllowedError' ? 'denied' : 'unavailable')
    }

    void start()

    return () => {
      cancelled = true
      if (watchdog) window.clearTimeout(watchdog)
      streamRef.current?.getTracks().forEach((tr) => tr.stop())
      streamRef.current = null
    }
  }, [])

  async function upload(blob: Blob) {
    setBusy(true)
    setError(null)
    try {
      const ticket = await post<{ path: string; upload_url: string }>(
        '/uploads/identity-photo',
        { mobile },
      )
      const response = await fetch(ticket.upload_url, {
        method: 'PUT',
        body: blob,
        headers: { 'Content-Type': 'image/jpeg' },
      })
      if (!response.ok) throw new Error('Upload failed')
      onUploaded(ticket.path)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed. Please retry.')
    } finally {
      setBusy(false)
    }
  }

  function capture() {
    const video = videoRef.current
    if (!video || !video.videoWidth) return

    const canvas = document.createElement('canvas')
    canvas.width = video.videoWidth
    canvas.height = video.videoHeight
    const ctx = canvas.getContext('2d')
    if (!ctx) return
    ctx.drawImage(video, 0, 0)

    canvas.toBlob(
      (blob) => {
        if (blob) void upload(blob)
      },
      'image/jpeg',
      0.85,
    )
  }

  if (status === 'denied' || status === 'unavailable') {
    return (
      <div className="space-y-2">
        <div className="rounded-xl bg-warn-bg p-4 text-warn dark:bg-warn-darkbg dark:text-warn-dark">
          <p className="font-bold">
            {status === 'denied' ? t('scanner.permission_blocked') : t('scanner.unavailable')}
          </p>
          <p className="mt-1 text-base">
            {status === 'denied' ? t('scanner.permission_hint') : t('person.photo_gallery_fallback_hint')}
          </p>
        </div>
        {/* The one honest fallback: if the camera itself cannot be reached,
            the alternative is explicitly labelled as choosing an existing
            photo, not presented as if it were the camera. */}
        <label className="btn-ghost block w-full cursor-pointer text-center">
          {t('person.choose_from_gallery')}
          <input
            type="file"
            accept="image/*"
            className="sr-only"
            disabled={busy}
            onChange={(event) => {
              const file = event.target.files?.[0]
              if (file) void upload(file)
            }}
          />
        </label>
      </div>
    )
  }

  return (
    <div className="space-y-3">
      <div className="viewfinder">
        <video ref={videoRef} autoPlay muted playsInline />
        {status === 'starting' && (
          <p className="absolute inset-0 flex items-center justify-center text-white">
            {t('scanner.starting')}
          </p>
        )}
      </div>

      <button
        type="button"
        className="btn-primary w-full"
        disabled={status !== 'running' || busy}
        onClick={capture}
      >
        {busy ? t('common.saving') : t('person.capture_photo')}
      </button>

      {error && (
        <p className="text-sm font-semibold text-bad dark:text-bad-dark">{error}</p>
      )}
    </div>
  )
}
