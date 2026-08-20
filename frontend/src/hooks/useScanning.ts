import { useCallback, useState } from 'react'
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
      const clientEventId = newScanId()
      const scannedAt = new Date().toISOString()
      setBusy(true)

      try {
        const endpoint =
          scanType === 'box_verify'
            ? `/entries/${entryId}/scan/box`
            : scanType === 'unit_verify'
              ? `/entries/${entryId}/scan/unit`
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

        void queryClient.invalidateQueries({ queryKey: ['box-progress', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['unit-progress', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['boxes', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['batch', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['pickup', entryId] })
        void queryClient.invalidateQueries({ queryKey: ['packing-state', entryId] })

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
