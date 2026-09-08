/**
 * localStorage that cannot take the app down with it.
 *
 * Reading or writing it throws, not returns null, in more situations than is
 * comfortable for code that runs at module load: Safari's private mode has
 * historically thrown on *write* (quota 0), and a WebView or browser with site
 * data blocked throws a SecurityError on *access to the property itself*. Both
 * of those crashed this app at boot — the theme and language are read before
 * anything renders — and a blank screen is the least diagnosable failure a
 * warehouse phone can have.
 *
 * Everything stored here is a per-device preference. Losing it costs one tap.
 */
export function readSetting(key: string): string | null {
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeSetting(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // Nothing to do and nothing worth saying: the preference applies for this
    // session and is asked again next time.
  }
}

export function removeSetting(key: string): void {
  try {
    window.localStorage.removeItem(key)
  } catch {
    /* as above */
  }
}
