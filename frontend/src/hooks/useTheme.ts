import { useEffect, useState } from 'react'

const STORAGE_KEY = 'r360-theme'

/**
 * Light/dark, persisted per device.
 *
 * Defaults to **light**. It used to default to dark on the reasoning that the
 * gate is outdoors — but the pastel wash the app is built on is a light-mode
 * design, and a first-run user should see the design as intended rather than a
 * variant of it. The choice still sticks per device, so a guard working nights
 * sets it once.
 *
 * Lives in a hook rather than inside Layout because Login has no header to put
 * the control in, and both screens need to read and write the same value.
 */
export function useTheme() {
  const [dark, setDark] = useState(() => localStorage.getItem(STORAGE_KEY) === 'dark')

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem(STORAGE_KEY, dark ? 'dark' : 'light')
  }, [dark])

  return { dark, setDark, toggle: () => setDark((d) => !d) }
}
