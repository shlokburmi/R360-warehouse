import { useTranslation } from 'react-i18next'
import { useAuth } from '@/hooks/useAuth'
import { Card } from '@/components/ui'
import { MyBadgeQr } from '@/components/MyBadgeQr'

/**
 * A staff member's own profile — the same name, role and employee code an
 * Admin set on the Staff screen, plus the QR off her own badge (MyBadgeQr)
 * so a colleague can scan it straight off this screen. `/me` already backs
 * the header (Layout.tsx) app-wide, so this reuses that instead of a new
 * endpoint.
 */
export function AboutMe() {
  const { t } = useTranslation()
  const { me } = useAuth()

  if (!me) return null

  return (
    <Card title={t('badge.about_me_title')}>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 text-base">
        <dt className="text-slate-500 dark:text-slate-400">{t('badge.about_me_name')}</dt>
        <dd className="font-semibold">{me.full_name}</dd>

        <dt className="text-slate-500 dark:text-slate-400">{t('badge.about_me_role')}</dt>
        <dd className="font-semibold">{t(`roles.${me.role}`, { defaultValue: me.role_label })}</dd>

        {me.employee_code && (
          <>
            <dt className="text-slate-500 dark:text-slate-400">{t('badge.about_me_code')}</dt>
            <dd className="font-mono font-semibold">{me.employee_code}</dd>
          </>
        )}
      </dl>

      <div className="mt-4 border-t border-slate-200 pt-4 text-center dark:border-slate-700">
        <p className="mb-3 text-base text-slate-600 dark:text-slate-400">
          {t('badge.mine_hint')}
        </p>
        <MyBadgeQr bare />
      </div>
    </Card>
  )
}
