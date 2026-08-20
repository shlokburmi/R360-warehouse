import { useTranslation } from 'react-i18next'

import type { ApiError } from '@/lib/api'

/**
 * Turn an ApiError into text the operator can read in their language.
 *
 * The server sends English prose with the specifics interpolated in — "Invoice
 * INV-2026-0001 is closed." Translating that sentence would mean either shipping
 * a translation layer into all fifty Python raise sites, or machine-translating
 * a sentence containing an identifier, which is how identifiers get mangled.
 *
 * Instead the error's `code` is translated and the interpolated detail is
 * dropped. That is affordable precisely because of where these banners appear:
 * the invoice number, box code or truck the refusal is about is already on the
 * screen next to it, usually in a heading. The banner's job is to say what went
 * wrong, not to re-identify what it went wrong on.
 *
 * An unrecognised code falls through to the server's English sentence, which is
 * strictly better than a blank banner or a raw key.
 */
export function useErrorText() {
  const { t } = useTranslation()

  return (error: ApiError | null | undefined) => {
    if (!error) return { title: '', hint: undefined as string | undefined }

    const key = `errors.${error.code}`
    const translated = t(key)
    return {
      title: translated === key ? error.message : translated,
      hint: error.hint,
    }
  }
}
