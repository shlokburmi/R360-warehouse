import { useTranslation } from 'react-i18next'
import { get } from '@/lib/api'
import { Banner, Card, Field } from '@/components/ui'
import { CameraCapture } from '@/components/CameraCapture'
import type { VisitorLookup } from '@/types'

/**
 * Registering the people on a vehicle.
 *
 * Shared by the inbound gate entry and the outbound pickup, because the identity
 * rules are identical and the visitor registry is the same one — a driver who
 * both delivers and collects should be one person in the system, not two.
 *
 * The lookup fires on the tenth digit of the mobile, so a returning driver's
 * name arrives before the guard has finished typing it. That is most of what
 * makes the two-minute target in PRD §11 reachable.
 */
export type PersonDraft = {
  key: string
  full_name: string
  mobile: string
  visitor_role: 'driver' | 'laborer' | 'supervisor'
  id_photo_path?: string
  lookup?: VisitorLookup
}

export const blankPerson = (role: PersonDraft['visitor_role']): PersonDraft => ({
  key: crypto.randomUUID(),
  full_name: '',
  mobile: '',
  visitor_role: role,
})

export function PersonFields({
  persons,
  setPersons,
  allowAdd = true,
}: {
  persons: PersonDraft[]
  setPersons: React.Dispatch<React.SetStateAction<PersonDraft[]>>
  allowAdd?: boolean
}) {
  const { t } = useTranslation()

  function update(key: string, patch: Partial<PersonDraft>) {
    setPersons((current) => current.map((p) => (p.key === key ? { ...p, ...patch } : p)))
  }

  async function checkVisitor(key: string, mobile: string) {
    try {
      const lookup = await get<VisitorLookup>(`/gate/visitors/lookup?mobile=${mobile}`)
      update(key, {
        lookup,
        // Pre-fill for a known visitor, but never overwrite what the guard has
        // already typed — they may be correcting a spelling.
        ...(lookup.found && lookup.full_name ? { full_name: lookup.full_name } : {}),
      })
    } catch {
      // A failed lookup must not block registration. The backend re-checks the
      // photo requirement on submit, so the worst case is one extra photo.
    }
  }

  return (
    <>
      {persons.map((person, index) => (
        <Card
          key={person.key}
          title={
            person.visitor_role === 'driver'
              ? t('person.driver')
              : t('person.person_n', { n: index + 1 })
          }
          action={
            persons.length > 1 && (
              <button
                type="button"
                className="text-base font-semibold text-bad dark:text-bad-dark"
                onClick={() => setPersons((c) => c.filter((p) => p.key !== person.key))}
              >
                Remove
              </button>
            )
          }
        >
          <Field label={t('person.mobile')} required>
            <input
              className="input font-mono"
              type="tel"
              inputMode="numeric"
              value={person.mobile}
              maxLength={10}
              onChange={(event) => {
                const digits = event.target.value.replace(/\D/g, '').slice(0, 10)
                update(person.key, { mobile: digits, lookup: undefined })
                if (digits.length === 10) void checkVisitor(person.key, digits)
              }}
              placeholder={t('person.mobile_placeholder')}
            />
            {/* Immediate feedback on the one thing that's wrong the moment it's
                wrong — a bad first digit doesn't need nine more keystrokes
                before the guard finds out. */}
            {person.mobile.length > 0 && !/^[6-9]/.test(person.mobile) && (
              <p className="mt-1 text-sm font-semibold text-bad dark:text-bad-dark">
                {t('person.mobile_bad_start')}
              </p>
            )}
          </Field>

          {person.lookup && (
            <div className="-mt-2 mb-4">
              <Banner
                tone={
                  person.lookup.is_blocked
                    ? 'bad'
                    : person.lookup.photo_required
                      ? 'warn'
                      : 'ok'
                }
                title={person.lookup.reason}
              >
                {person.lookup.is_blocked && person.lookup.blocked_reason}
              </Banner>
            </div>
          )}

          <Field label={t('person.full_name')} required>
            <input
              className="input"
              value={person.full_name}
              onChange={(event) => update(person.key, { full_name: event.target.value })}
              placeholder={t('person.name_placeholder')}
              autoCapitalize="words"
            />
          </Field>

          <Field label={t('person.role')} required>
            {/* A grid, not `flex-1`. A flex item will not shrink below its
                content, so at 320px the three buttons together measured 19px
                wider than the viewport and slid the whole page sideways. Grid
                columns are equal fractions of the available width and cannot. */}
            <div className="grid grid-cols-3 gap-2">
              {(['driver', 'laborer', 'supervisor'] as const).map((role) => (
                <button
                  key={role}
                  type="button"
                  onClick={() => update(person.key, { visitor_role: role })}
                  className={`min-w-0 break-words rounded-xl border-2 px-2 py-3 text-sm font-bold sm:px-3 sm:text-base ${
                    person.visitor_role === role
                      ? 'border-blue-600 bg-blue-600 text-white'
                      : 'border-slate-300 dark:border-slate-700'
                  }`}
                >
                  {t(`person.${role}`)}
                </button>
              ))}
            </div>
          </Field>

          {person.lookup?.photo_required && (
            <Field
              label={t('person.id_photo')}
              required
              hint={t('person.id_photo_hint')}
            >
              {person.id_photo_path ? (
                <div className="flex items-center gap-3 rounded-xl bg-ok-bg p-4 text-ok dark:bg-ok-darkbg dark:text-ok-dark">
                  <span className="text-2xl">✓</span>
                  <span className="font-bold">{t('person.photo_captured')}</span>
                </div>
              ) : (
                <CameraCapture
                  mobile={person.mobile}
                  onUploaded={(path) => update(person.key, { id_photo_path: path })}
                />
              )}
            </Field>
          )}
        </Card>
      ))}

      {allowAdd && (
        <button
          type="button"
          className="btn-ghost w-full"
          onClick={() => setPersons((c) => [...c, blankPerson('laborer')])}
        >
          + Add another person
        </button>
      )}
    </>
  )
}

