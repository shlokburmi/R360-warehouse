import { useCallback, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { ApiError, post } from '@/lib/api'
import { deviceLabel, enqueue, newScanId } from '@/lib/offlineQueue'
import type { ScanResult } from '@/types'

export type ScanFeedback = {
  id: string
  code: string
  tone: 'ok' | 'bad' | 'warn'
  message: string
  at: number
}

/**
 * The scanning loop shared by the box-count and unit-count pages.
 *
 * Two things it deliberately does not do:
 *
 * - It does not block the operator while the request is in flight. Someone
 *   scanning 200 units cannot wait 300ms per scan, so feedback is optimistic
 *   and corrected the moment the server answers.
 *
 * - It does not treat "offline" as an error. A network failure queues the scan
 *   and tells the operator it is saved, because on the warehouse floor the wifi
 *   dropping is a normal event, not an exception.
 */
export type ScanContext =
  | 'box_verify'
  | 'unit_verify'
  /** Product-in-hand confirmation at invoice matching, before the badge scan. */
  | 'match_unit'
  /** Product boxes going into a carton at the packing bench. */
  | 'pack_unit'
  | 'out_scan'
  | 'gate_exit'

/**
 * `contextId` is whichever aggregate the scan counts against: the gate entry for
 * box/unit scans, the invoice for packing scans, the batch for out-scans, the
 * pickup for gate exit.
 */
export function useScanning(contextId: string, scanType: ScanContext) {
  const entryId = contextId
  const queryClient = useQueryClient()
  const [feedback, setFeedback] = useState<ScanFeedback[]>([])
  const [busy, setBusy] = useState(false)

  // Codes that can never succeed or fail differently on a re-scan: already
  // accepted, or already rejected specifically as a duplicate. QrScanner's
  // own debounce is time-based and same-code-only by design (a rejection
  // like "a different box is still open" can genuinely become scannable
  // again once that resolves, so it must not be permanently blocked there —
  // see QrScanner.tsx's own comment). But "already scanned" is not one of
  // those resolvable cases: a sticker that has been recorded never becomes
  // un-recorded. A dense sheet of small stickers held a little too long in
  // frame re-crosses QrScanner's 5s window repeatedly, so without this, the
  // exact same already-settled outcome gets re-submitted over the network
  // every 5 seconds for as long as the camera keeps re-reading it — this
  // catches that below the network, not by changing the debounce.
  const settledCodes = useRef<Set<string>>(new Set())

  const push = useCallback((item: Omit<ScanFeedback, 'id' | 'at'>) => {
    setFeedback((current) => [
      { ...item, id: crypto.randomUUID(), at: Date.now() },
      // Keep a short visible history. Enough to notice a run of rejects, not so
      // much that the screen becomes a log to read.
      ...current.slice(0, 7),
    ])
  }, [])

  const submit = useCallback(
    async (code: string, disposition?: 'stock' | 'quarantine') => {
      const clean = code.trim().toUpperCase()

      if (settledCodes.current.has(clean)) {
        // Same tone the server would answer with for this exact case (accepted
        // = false, duplicate = false) — this is a client-side shortcut to that
        // answer, not a different signal.
        push({ code, tone: 'bad', message: 'Already scanned.' })
        return null
      }

      const clientEventId = newScanId()
      const scannedAt = new Date().toISOString()
      setBusy(true)

      try {
        const endpoint =
          scanType === 'box_verify'
            ? `/entries/${entryId}/scan/box`
            : scanType === 'unit_verify'
              ? `/entries/${entryId}/scan/unit`
              : scanType === 'match_unit'
                ? `/invoices/${entryId}/match-scan`
                : scanType === 'pack_unit'
                  ? `/invoices/${entryId}/pack-scan`
                  : scanType === 'out_scan'
                    ? `/batches/${entryId}/scan`
                    : `/pickups/${entryId}/scan`

        const result = await post<ScanResult>(endpoint, {
          client_event_id: clientEventId,
          raw_code: code,
          scanned_at: scannedAt,
          was_offline: false,
          device_label: deviceLabel(),
          disposition,
        })

        push({
          code,
          tone: result.accepted ? 'ok' : result.duplicate ? 'warn' : 'bad',
          message: result.message,
        })

        // Accepted, or rejected specifically as already-scanned: neither
        // outcome changes on a re-scan of this exact code, unlike a
        // business-state rejection (e.g. "a different box is still open").
        if (result.accepted || result.reject_reason === 'already_scanned') {
          settledCodes.current.add(clean)
        }

        void queryClient.invalidateQueries({ queryKey: ['box-progress', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['unit-progress', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['boxes', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['batch', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['pickup', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['packing-state', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['matching-state', entryId] })

        return result
      } catch (error) {
        if (error instanceof ApiError && error.isOffline) {
          await enqueue({
            client_event_id: clientEventId,
            raw_code: code,
            scanned_at: scannedAt,
            was_offline: true,
            device_label: deviceLabel(),
            disposition,
            scan_type: scanType,
            entry_id: entryId,
            attempts: 0,
          })
          push({ code, tone: 'warn', message: 'Saved on device — will sync when online' })
          return null
        }

        push({
          code,
          tone: 'bad',
          message: error instanceof Error ? error.message : 'Scan failed',
        })
        return null
      } finally {
        setBusy(false)
      }
    },
    [entryId, scanType, push, queryClient],
  )

  return { feedback, submit, busy }
}
