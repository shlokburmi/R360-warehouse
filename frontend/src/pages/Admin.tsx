import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, api, get, post } from '@/lib/api'
import { useErrorText } from '@/hooks/useErrorText'
import { Banner, Card, EmptyState, Field, Spinner, StatusChip } from '@/components/ui'
import { BadgeCardPrint } from '@/components/BadgeCardPrint'
import { useAuth } from '@/hooks/useAuth'
import type {
  AccountHistoryEntry,
  AdminMeta,
  BadgeIssued,
  PhotoRetention,
  Staff,
  StaffCreated,
} from '@/types'

/**
 * PRD §2 "Admin" / §8 — staff accounts and attribution badges.
 *
 * Until this page existed, provisioning a user and issuing a badge were SQL
 * jobs run by hand. That is worth naming as the reason it exists: the one
 * operation here with a security invariant attached — issuing a badge — was
 * being performed by whoever had a psql prompt, with no audit actor recorded
 * against it.
 *
 * Two things on this page are shown exactly once and then gone: a new account's
 * temporary password, and a freshly minted badge code. Neither is stored
 * anywhere, on the server or here, and the layout leans on that rather than
 * hiding it — a panel you have to dismiss, not a value in a table cell.
 */
export function AdminPage() {
  const { t } = useTranslation()
  const errorText = useErrorText()
  const { me } = useAuth()
  const queryClient = useQueryClient()

  const [error, setError] = useState<ApiError | null>(null)
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<StaffCreated | null>(null)
  const [issued, setIssued] = useState<BadgeIssued | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [confirmRevoke, setConfirmRevoke] = useState<string | null>(null)
  const [showInactive, setShowInactive] = useState(true)

  const meta = useQuery({
    queryKey: ['admin-meta'],
    queryFn: () => get<AdminMeta>('/admin/meta'),
    staleTime: Infinity,
  })

  const staff = useQuery({
    queryKey: ['admin-staff'],
    queryFn: () => get<Staff[]>('/admin/staff'),
  })

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['admin-staff'] })

  const create = useMutation({
    mutationFn: (body: unknown) => post<StaffCreated>('/admin/staff', body),
    onSuccess: (result) => {
      setError(null)
      setCreating(false)
      setCreated(result)
      void refresh()
    },
    onError: (err) => setError(err as ApiError),
  })

  const update = useMutation({
    mutationFn: ({ id, ...body }: { id: string } & Record<string, unknown>) =>
      api<Staff>(`/admin/staff/${id}`, { method: 'PATCH', body }),
    onSuccess: () => {
      setError(null)
      void refresh()
    },
    onError: (err) => setError(err as ApiError),
  })

  const issueBadge = useMutation({
    mutationFn: (id: string) => post<BadgeIssued>(`/admin/staff/${id}/badge`),
    onSuccess: (result) => {
      setError(null)
      setIssued(result)
      void refresh()
    },
    onError: (err) => setError(err as ApiError),
  })

  const revokeBadge = useMutation({
    mutationFn: (id: string) => post<Staff>(`/admin/staff/${id}/badge/revoke`),
    onSuccess: () => {
      setError(null)
      setConfirmRevoke(null)
      void refresh()
    },
    onError: (err) => setError(err as ApiError),
  })

  if (staff.isLoading || meta.isLoading) return <Spinner label={t('admin.loading_staff')} />

  // A freshly minted badge is the only thing that can be lost by navigating
  // away, so it takes over the screen until it is dealt with.
  if (issued) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-black">{t('admin.print_badge_now')}</h1>
        <Banner tone="warn" title={t('admin.shown_once')}>
          {t('admin.shown_once_body')}
          {issued.staff.has_badge && (
            <p className="mt-2 font-semibold">
              {t('admin.badge_replaced_body', { name: issued.staff.full_name })}
            </p>
          )}
        </Banner>
        <Card>
          <BadgeCardPrint issued={issued} onDone={() => setIssued(null)} />
        </Card>
      </div>
    )
  }

  if (created) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-black">{t('admin.account_created')}</h1>
        <Banner tone="ok" title={t('admin.can_sign_in_now', { name: created.staff.full_name })}>
          {t('admin.password_body')}
        </Banner>
        <Card title={t('admin.temp_password')}>
          <p className="select-all break-all rounded-xl bg-slate-100 p-4 text-center font-mono text-3xl font-black dark:bg-slate-800">
            {created.temporary_password}
          </p>
          <dl className="mt-4 grid grid-cols-1 gap-3 text-base sm:grid-cols-2">
            <div>
              <dt className="text-slate-500 dark:text-slate-400">{t('admin.role')}</dt>
              <dd className="font-bold">{created.staff.role_label}</dd>
            </div>
            <div>
              <dt className="text-slate-500 dark:text-slate-400">{t('admin.employee_code')}</dt>
              <dd className="font-mono font-bold">{created.staff.employee_code}</dd>
            </div>
          </dl>
          <div className="mt-4 flex flex-col gap-3 sm:flex-row">
            {created.staff.can_hold_badge && (
              <button
                type="button"
                className="btn-primary flex-1"
                disabled={issueBadge.isPending}
                onClick={() => {
                  const id = created.staff.id
                  setCreated(null)
                  issueBadge.mutate(id)
                }}
              >
                {t('admin.issue_badge')}
              </button>
            )}
            <button type="button" className="btn-ghost flex-1" onClick={() => setCreated(null)}>
              Done
            </button>
          </div>
        </Card>
      </div>
    )
  }

  const rows = (staff.data ?? []).filter((s) => showInactive || s.is_active)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-black">{t('admin.title')}</h1>
        <button
          type="button"
          className="btn-primary"
          onClick={() => {
            setCreating(true)
            setError(null)
          }}
        >
          {t('admin.add_person')}
        </button>
      </div>

      {error && (
        <Banner tone="bad" title={errorText(error).title}>
          {error.hint}
        </Banner>
      )}

      {creating && (
        <NewStaffForm
          roles={meta.data?.roles ?? []}
          pending={create.isPending}
          onCancel={() => setCreating(false)}
          onSubmit={(body) => create.mutate(body)}
        />
      )}

      <label className="flex items-center gap-3 text-base font-semibold">
        <input
          type="checkbox"
          className="h-6 w-6 rounded"
          checked={showInactive}
          onChange={(event) => setShowInactive(event.target.checked)}
        />
        {t('admin.show_deactivated')}
      </label>

      <RetentionPanel />

      {rows.length === 0 && <EmptyState title={t('admin.no_staff')} />}

      {rows.map((person) => {
        const isSelf = person.id === me?.id
        const open = expanded === person.id
        const busy =
          update.isPending || issueBadge.isPending || revokeBadge.isPending

        return (
          <Card
            key={person.id}
            title={person.full_name}
            subtitle={`${person.role_label}${
              person.employee_code ? ` · ${person.employee_code}` : ''
            }`}
            action={
              <div className="flex flex-col items-end gap-1">
                {!person.is_active && <StatusChip status="cancelled" />}
                {person.badge_usable && (
                  <span className="chip bg-ok-bg text-ok dark:bg-ok-darkbg dark:text-ok-dark">
                    badge active
                  </span>
                )}
                {person.has_badge && !person.badge_active && (
                  <span className="chip bg-warn-bg text-warn dark:bg-warn-darkbg dark:text-warn-dark">
                    badge revoked
                  </span>
                )}
                {person.is_backup_approver && (
                  <span className="chip bg-info-bg text-info dark:bg-info-darkbg dark:text-info-dark">
                    backup approver
                  </span>
                )}
              </div>
            }
          >
            {/* Attribution counts, not productivity figures — those live on the
                Reports page. Here they exist so that "deactivate" is a decision
                made while looking at how much work is attributed to the person. */}
            {(person.invoices_verified > 0 || person.cartons_packed > 0) && (
              <p className="mb-4 text-base text-slate-600 dark:text-slate-400">
                {person.invoices_verified > 0 && (
                  <>{person.invoices_verified} invoices verified</>
                )}
                {person.invoices_verified > 0 && person.cartons_packed > 0 && ' · '}
                {person.cartons_packed > 0 && <>{person.cartons_packed} cartons packed</>}
                {person.last_attributed_at && (
                  <> · last {new Date(person.last_attributed_at).toLocaleDateString()}</>
                )}
              </p>
            )}

            <div className="flex flex-wrap gap-3">
              {person.can_hold_badge && person.is_active && (
                <button
                  type="button"
                  className={person.badge_usable ? 'btn-ghost' : 'btn-primary'}
                  disabled={busy}
                  onClick={() => issueBadge.mutate(person.id)}
                >
                  {person.badge_usable ? 'Reissue badge' : 'Issue badge'}
                </button>
              )}

              {person.badge_usable &&
                (confirmRevoke === person.id ? (
                  <button
                    type="button"
                    className="btn-danger"
                    disabled={busy}
                    onClick={() => revokeBadge.mutate(person.id)}
                  >
                    {t('admin.confirm_revoke')}
                  </button>
                ) : (
                  <button
                    type="button"
                    className="btn-ghost"
                    disabled={busy}
                    onClick={() => setConfirmRevoke(person.id)}
                  >
                    {t('admin.revoke_badge')}
                  </button>
                ))}

              <button
                type="button"
                className="btn-ghost"
                onClick={() => setExpanded(open ? null : person.id)}
              >
                {open ? 'Close' : 'Change role or access'}
              </button>
            </div>

            {open && (
              <div className="mt-4 space-y-4 border-t border-slate-200 pt-4 dark:border-slate-800">
                <Field
                  label={t('admin.role')}
                  hint={
                    person.badge_usable
                      ? 'Moving off a badge role deactivates their badge.'
                      : undefined
                  }
                >
                  <select
                    className="input"
                    value={person.role}
                    disabled={busy || isSelf}
                    onChange={(event) =>
                      update.mutate({ id: person.id, role: event.target.value })
                    }
                  >
                    {(meta.data?.roles ?? []).map((role) => (
                      <option key={role.value} value={role.value}>
                        {role.label}
                      </option>
                    ))}
                  </select>
                </Field>

                {isSelf && (
                  <Banner tone="info" title={t('admin.own_account')}>
                    {t('admin.self_body')}
                  </Banner>
                )}

                {person.role === 'admin' && (
                  <label className="flex items-center gap-3 text-base font-semibold">
                    <input
                      type="checkbox"
                      className="h-6 w-6 rounded"
                      checked={person.is_backup_approver}
                      disabled={busy}
                      onChange={(event) =>
                        update.mutate({
                          id: person.id,
                          is_backup_approver: event.target.checked,
                        })
                      }
                    />
                    {t('admin.escalation_note')}
                  </label>
                )}

                <div className="flex flex-wrap gap-3">
                  {person.is_active ? (
                    <button
                      type="button"
                      className="btn-danger"
                      disabled={busy || isSelf}
                      onClick={() => update.mutate({ id: person.id, is_active: false })}
                    >
                      {t('admin.deactivate')}
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="btn-success"
                      disabled={busy}
                      onClick={() => update.mutate({ id: person.id, is_active: true })}
                    >
                      {t('admin.reactivate')}
                    </button>
                  )}
                </div>

                <AccountHistory profileId={person.id} />
              </div>
            )}
          </Card>
        )
      })}
    </div>
  )
}

/**
 * Identity photo retention (PRD §8, DPDP Act 2023).
 *
 * Read-only, and that is the design rather than a gap. The purge runs in the
 * worker, which holds the only connection privileged enough to delete from a
 * bucket that grants DELETE to nobody; a "run it now" button here would mean
 * handing a request handler that connection.
 *
 * The number that matters is `overdue`. Everything else on this page fails
 * loudly — a rejected badge, a refused role change. A retention job that stops
 * running produces no error anywhere, because not deleting breaks nothing. This
 * panel is the only place that failure becomes visible.
 */
function RetentionPanel() {
  const { t } = useTranslation()
  const retention = useQuery({
    queryKey: ['admin-retention'],
    queryFn: () => get<PhotoRetention>('/admin/retention'),
  })

  if (retention.isLoading || !retention.data) return null
  const data = retention.data

  return (
    <Card
      title={t('admin.identity_photos')}
      subtitle={`Destroyed automatically after ${data.retention_days} days`}
    >
      {!data.enabled && (
        <div className="mb-4">
          <Banner tone="bad" title={t('admin.retention_off')}>
            {t('admin.retention_off_body', { days: data.retention_days })}
          </Banner>
        </div>
      )}

      {data.enabled && data.overdue > 0 && (
        <div className="mb-4">
          <Banner tone="warn" title={t('admin.photos_overdue', { count: data.overdue })}>
            {t('admin.overdue_body')}
          </Banner>
        </div>
      )}

      <dl className="grid grid-cols-2 gap-3 text-base sm:grid-cols-4">
        <div>
          <dt className="text-slate-500 dark:text-slate-400">{t('admin.held_now')}</dt>
          <dd className="text-2xl font-black tabular-nums">{data.photos_held}</dd>
        </div>
        <div>
          <dt className="text-slate-500 dark:text-slate-400">{t('admin.destroyed')}</dt>
          <dd className="text-2xl font-black tabular-nums">{data.photos_purged}</dd>
        </div>
        <div>
          <dt className="text-slate-500 dark:text-slate-400">{t('admin.oldest_held')}</dt>
          <dd className="text-lg font-bold">
            {data.oldest_held_at
              ? new Date(data.oldest_held_at).toLocaleDateString()
              : '—'}
          </dd>
        </div>
        <div>
          <dt className="text-slate-500 dark:text-slate-400">{t('admin.last_destroyed')}</dt>
          <dd className="text-lg font-bold">
            {data.last_purge_at ? new Date(data.last_purge_at).toLocaleDateString() : '—'}
          </dd>
        </div>
      </dl>

      {data.retained_for_block > 0 && (
        <p className="mt-4 text-base text-slate-600 dark:text-slate-400">
          {data.retained_for_block} kept for blocked visitors. A block is
          enforced on a mobile number, which is easily borrowed, so the photo is
          still doing the job it was captured for.
        </p>
      )}
    </Card>
  )
}

/**
 * What has happened to this account, and who did it.
 *
 * Reads the audit trail rather than a summary, because the entry an Admin comes
 * looking for is "when was this badge last reissued, and by whom" — which is a
 * `changed_keys` containing `badge_code`. The values themselves are redacted
 * before they are ever written (see 0013 §1); this is who and when, never what.
 */
function AccountHistory({ profileId }: { profileId: string }) {
  const { t } = useTranslation()
  const history = useQuery({
    queryKey: ['admin-history', profileId],
    queryFn: () => get<AccountHistoryEntry[]>(`/admin/staff/${profileId}/history`),
  })

  if (history.isLoading) return <Spinner label={t('admin.loading_history')} />
  if (!history.data?.length) return null

  const describe = (entry: AccountHistoryEntry) => {
    if (entry.action === 'INSERT') return 'Account created'
    const keys = (entry.changed_keys ?? []).filter((k) => k !== 'updated_at')
    if (keys.includes('badge_code')) return 'Badge issued or reissued'
    if (keys.includes('badge_active')) return 'Badge revoked or restored'
    if (keys.includes('role')) return 'Role changed'
    if (keys.includes('is_active')) return 'Account activated or deactivated'
    if (keys.includes('is_backup_approver')) return 'Backup approver changed'
    return keys.join(', ') || 'Updated'
  }

  return (
    <div>
      <p className="label">{t('admin.history')}</p>
      <ul className="space-y-2 text-base">
        {history.data.map((entry, index) => (
          <li key={index} className="flex flex-wrap justify-between gap-2">
            <span className="font-semibold">{describe(entry)}</span>
            <span className="text-slate-500 dark:text-slate-400">
              {entry.actor_name ?? entry.actor_source} ·{' '}
              {new Date(entry.occurred_at).toLocaleString()}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function NewStaffForm({
  roles,
  pending,
  onCancel,
  onSubmit,
}: {
  roles: { value: string; label: string; carries_badge: boolean }[]
  pending: boolean
  onCancel: () => void
  onSubmit: (body: Record<string, unknown>) => void
}) {
  const { t } = useTranslation()
  const [email, setEmail] = useState('')
  const [fullName, setFullName] = useState('')
  const [role, setRole] = useState('security_guard')
  const [employeeCode, setEmployeeCode] = useState('')
  const [mobile, setMobile] = useState('')

  const mobileDigits = mobile.replace(/\D/g, '')
  const mobileValid = mobileDigits === '' || /^[6-9]\d{9}$/.test(mobileDigits)

  // PRD §6: submit stays disabled until every required field is filled, so the
  // failure arrives before the account is created rather than after.
  const ready =
    email.includes('@') &&
    email.includes('.') &&
    fullName.trim().length >= 2 &&
    employeeCode.trim().length >= 2 &&
    mobileValid

  return (
    <Card title={t('admin.add_a_person')} subtitle={t('admin.can_sign_in')}>
      <Field label={t('admin.full_name')} required>
        <input
          className="input"
          value={fullName}
          onChange={(event) => setFullName(event.target.value)}
          placeholder={t('admin.name_hint')}
          autoComplete="off"
        />
      </Field>

      <Field label={t('admin.email')} required hint={t('admin.email_hint')}>
        <input
          className="input"
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          placeholder="name@r360.local"
          autoComplete="off"
        />
      </Field>

      <Field label={t('admin.employee_code')} required>
        <input
          className="input font-mono"
          value={employeeCode}
          onChange={(event) => setEmployeeCode(event.target.value.toUpperCase())}
          placeholder="EMP-P03"
          autoComplete="off"
        />
      </Field>

      <Field label={t('admin.role')} required>
        <select className="input" value={role} onChange={(event) => setRole(event.target.value)}>
          {roles.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
              {option.carries_badge ? ' — carries a badge' : ''}
            </option>
          ))}
        </select>
      </Field>

      <Field
        label={t('admin.mobile')}
        hint={t('admin.optional_dot')}
        error={mobileValid ? undefined : 'Must be 10 digits starting 6-9.'}
      >
        <input
          className={`input ${mobileValid ? '' : 'input-error'}`}
          inputMode="numeric"
          value={mobile}
          onChange={(event) => setMobile(event.target.value)}
          placeholder={t('person.mobile_placeholder')}
        />
      </Field>

      <div className="flex flex-col gap-3 sm:flex-row">
        <button type="button" className="btn-ghost flex-1" onClick={onCancel}>
          {t('common.cancel')}
        </button>
        <button
          type="button"
          className="btn-primary flex-1"
          disabled={!ready || pending}
          onClick={() =>
            onSubmit({
              email: email.trim().toLowerCase(),
              full_name: fullName.trim(),
              role,
              employee_code: employeeCode.trim(),
              mobile: mobileDigits || null,
            })
          }
        >
          {pending ? 'Creating…' : 'Create account'}
        </button>
      </div>
    </Card>
  )
}
