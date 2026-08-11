import { useEffect, useState } from 'react'
import { onQueueChange } from '@/lib/offlineQueue'

/** navigator.onLine, as React state. */
export function useOnline(): boolean {
  const [online, setOnline] = useState(navigator.onLine)

  useEffect(() => {
    const up = () => setOnline(true)
    const down = () => setOnline(false)
    window.addEventListener('online', up)
    window.addEventListener('offline', down)
    return () => {
      window.removeEventListener('online', up)
      window.removeEventListener('offline', down)
    }
  }, [])

  return online
}

/** How many scans are waiting to sync. Drives the pending badge. */
export function usePendingScans(): number {
  const [count, setCount] = useState(0)
  useEffect(() => onQueueChange((queue) => setCount(queue.length)), [])
  return count
}
