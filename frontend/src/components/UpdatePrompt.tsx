import { useTranslation } from 'react-i18next'
import { useRegisterSW } from 'virtual:pwa-register/react'

/**
 * Tells the operator a new version has been deployed instead of the old
 * `registerType: 'autoUpdate'` behaviour of reloading the page out from under
 * her the moment any deploy went live, anywhere in the app — including a
 * backend-only deploy that never touched a line of frontend code, since
 * Vercel rebuilds the whole app on every push regardless of which files
 * changed. That reload interrupted whatever she was mid-way through (a badge
 * scan, an OCR upload) with no warning and no way to finish it first.
 *
 * Rendered once, near the app root, so it survives regardless of which page
 * or auth state is currently showing.
 */
export function UpdatePrompt() {
  const { t } = useTranslation()
  const {
    needRefresh: [needRefresh],
    updateServiceWorker,
  } = useRegisterSW()

  if (!needRefresh) return null

  return (
    <div className="fixed inset-x-3 bottom-3 z-50 mx-auto max-w-md rounded-2xl bg-slate-900 p-4 text-white shadow-2xl dark:bg-slate-800">
      <p className="text-lg font-bold">{t('update.available')}</p>
      <p className="mt-1 text-base text-slate-300">{t('update.hint')}</p>
      <button
        type="button"
        className="btn-primary mt-3 w-full"
        onClick={() => void updateServiceWorker(true)}
      >
        {t('update.reload')}
      </button>
    </div>
  )
}
