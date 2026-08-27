import { del, get as idbGet, set as idbSet } from 'idb-keyval'
import { ApiError, post } from './api'

/**
 * Offline scan queue (PRD §6, <1% data loss).
 *
 * The device is the source of truth for "did this scan happen". Each scan gets
 * a UUID minted here, before it leaves, and the server treats a repeat of that
 * id as a no-op. That inversion is what makes the queue simple: it can retry
 * blindly and forever without ever needing to work out what landed.
 *
 * IndexedDB rather than localStorage because a shift can produce thousands of
 * scans and localStorage is both size-limited and synchronous — blocking the
 * main thread mid-scan would drop frames in the camera preview.
 */

export type QueuedScan = {
  client_event_id: string
  raw_code: string
  scanned_at: string
  was_offline: boolean
  device_label?: string
  disposition?: 'stock' | 'quarantine'
  scan_type: 'box_verify' | 'unit_verify' | 'match_unit' | 'pack_unit' | 'out_scan' | 'gate_exit'
  /** Gate entry for box/unit scans; batch for out-scans; pickup for gate exit. */
  entry_id: string
  attempts: number
  last_error?: string
}

const KEY = 'r360-scan-queue'

type Listener = (queue: QueuedScan[]) => void
const listeners = new Set<Listener>()

async function read(): Promise<QueuedScan[]> {
  return (await idbGet<QueuedScan[]>(KEY)) ?? []
}

async function write(queue: QueuedScan[]): Promise<void> {
  if (queue.length === 0) {
    await del(KEY)
  } else {
    await idbSet(KEY, queue)
  }
  listeners.forEach((fn) => fn(queue))
}

export function onQueueChange(fn: Listener): () => void {
  listeners.add(fn)
  void read().then(fn)
  return () => listeners.delete(fn)
}

export async function queueSize(): Promise<number> {
  return (await read()).length
}

export async function enqueue(scan: QueuedScan): Promise<void> {
  const queue = await read()
  // Guard against the same physical scan being enqueued twice by a double tap.
  if (queue.some((q) => q.client_event_id === scan.client_event_id)) return
  queue.push(scan)
  await write(queue)
}

export function newScanId(): string {
  return crypto.randomUUID()
}

export function deviceLabel(): string {
  const ua = navigator.userAgent
  if (/iPad|Tablet/i.test(ua)) return 'Tablet'
  if (/Mobile|Android|iPhone/i.test(ua)) return 'Phone'
  return 'Desktop'
}

/**
 * Drain the queue. Safe to call at any time, including concurrently with new
 * scans arriving — anything queued mid-flush stays for the next pass.
 *
 * Scans are grouped by (entry, type) because that is the shape of the sync
 * endpoint, and sent oldest-first so the server sees them in the order they
 * physically happened.
 */
export async function flushQueue(): Promise<{ sent: number; failed: number }> {
  const queue = await read()
  if (queue.length === 0) return { sent: 0, failed: 0 }

  const groups = new Map<string, QueuedScan[]>()
  for (const scan of queue) {
    const key = `${scan.entry_id}::${scan.scan_type}`
    const group = groups.get(key)
    if (group) group.push(scan)
    else groups.set(key, [scan])
  }

  const sentIds = new Set<string>()
  let failed = 0

  for (const [key, group] of groups) {
    const [, scanType] = key.split('::')
    const ordered = [...group].sort((a, b) => a.scanned_at.localeCompare(b.scanned_at))

    try {
      // pack_unit and match_unit both carry the invoice as well as the type: a
      // product sticker knows which box it arrived in, not which order or
      // carton it is bound for, so the invoice cannot be re-derived
      // server-side the way the others can.
      const [entryId] = key.split('::')
      const query =
        scanType === 'pack_unit' || scanType === 'match_unit'
          ? `scan_type=${scanType}&invoice_id=${entryId}`
          : `scan_type=${scanType}`

      await post(`/scan/sync?${query}`, {
        scans: ordered.map((s) => ({
          client_event_id: s.client_event_id,
          raw_code: s.raw_code,
          scanned_at: s.scanned_at,
          was_offline: true,
          device_label: s.device_label,
          disposition: s.disposition,
        })),
      })
      ordered.forEach((s) => sentIds.add(s.client_event_id))
    } catch (error) {
      // Still offline, or the server is down. Leave the group queued and try
      // again on the next reconnect — nothing is dropped.
      if (error instanceof ApiError && error.isOffline) {
        failed += ordered.length
        continue
      }

      // A real server-side refusal. Retrying forever would wedge the queue
      // behind a scan that will never be accepted, so it is dropped after a few
      // attempts and the reason is kept for the operator to see.
      ordered.forEach((s) => {
        s.attempts += 1
        s.last_error = error instanceof Error ? error.message : 'Unknown error'
        if (s.attempts >= 3) sentIds.add(s.client_event_id)
      })
      failed += ordered.length
    }
  }

  const remaining = (await read()).filter((s) => !sentIds.has(s.client_event_id))
  await write(remaining)

  return { sent: sentIds.size, failed }
}

/** Flush whenever the device comes back online, and once at startup. */
export function startAutoFlush(): () => void {
  const flush = () => {
    void flushQueue()
  }

  window.addEventListener('online', flush)
  const timer = window.setInterval(() => {
    if (navigator.onLine) flush()
  }, 30_000)

  flush()

  return () => {
    window.removeEventListener('online', flush)
    window.clearInterval(timer)
  }
}
