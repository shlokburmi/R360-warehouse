import i18next from 'i18next'
import { initReactI18next } from 'react-i18next'

import en from './en.json'
import kn from './kn.json'

/**
 * Two languages, chosen at sign-in and remembered.
 *
 * ---------------------------------------------------------------------------
 * WHY THE LANGUAGE IS PICKED BEFORE LOGIN, NOT IN A SETTINGS PAGE
 * ---------------------------------------------------------------------------
 *
 * The stations that need Kannada are the ones a settings page is hardest to
 * reach from: a guard at a gate, a counter at an offloading bay. Both are
 * shared tablets, so the choice cannot live on a user profile either — the next
 * person to hold the device may read the other language.
 *
 * So it is a device preference, stored in localStorage, offered on the login
 * screen where it is unmissable. `useAuth` does not know about it and neither
 * does the server.
 */

export const LANGUAGES = [
  { code: 'en', label: 'English', native: 'English' },
  { code: 'kn', label: 'Kannada', native: 'ಕನ್ನಡ' },
] as const

export type LanguageCode = (typeof LANGUAGES)[number]['code']

const STORAGE_KEY = 'r360.lang'

export function storedLanguage(): LanguageCode {
  const saved = localStorage.getItem(STORAGE_KEY)
  return saved === 'kn' || saved === 'en' ? saved : 'en'
}

/**
 * Kannada glyphs need a font that has them, and this is an offline-first PWA —
 * fetching one from a CDN would leave a warehouse with no wifi rendering boxes.
 * So the stack leans on what the device already has (Android and iOS both ship
 * a Kannada face) and the class below lets CSS name those explicitly.
 */
export function applyLanguage(code: LanguageCode): void {
  document.documentElement.lang = code
  document.documentElement.classList.toggle('lang-kn', code === 'kn')
}

export async function setLanguage(code: LanguageCode): Promise<void> {
  localStorage.setItem(STORAGE_KEY, code)
  applyLanguage(code)
  await i18next.changeLanguage(code)
}

void i18next.use(initReactI18next).init({
  resources: { en: { translation: en }, kn: { translation: kn } },
  lng: storedLanguage(),
  fallbackLng: 'en',
  interpolation: { escapeValue: false },
  // A missing Kannada key falls back to the English string rather than showing
  // the raw key. An operator seeing "putaway.scan_location" learns nothing; the
  // English words at least name the thing in front of them.
  returnEmptyString: false,
})

applyLanguage(storedLanguage())

export default i18next
