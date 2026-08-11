import { useState } from 'react'
import { get, post } from '@/lib/api'
import { Banner, Card, Field } from '@/components/ui'
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
          title={person.visitor_role === 'driver' ? 'Driver' : `Person ${index + 1}`}
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
          <Field label="Mobile number" required>
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
              placeholder="10 digits"
            />
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

          <Field label="Full name" required>
            <input
              className="input"
              value={person.full_name}
              onChange={(event) => update(person.key, { full_name: event.target.value })}
              placeholder="As on the ID card"
              autoCapitalize="words"
            />
          </Field>

          <Field label="Role" required>
            <div className="flex gap-2">
              {(['driver', 'laborer', 'supervisor'] as const).map((role) => (
                <button
                  key={role}
                  type="button"
                  onClick={() => update(person.key, { visitor_role: role })}
                  className={`flex-1 rounded-xl border-2 px-3 py-3 text-base font-bold capitalize ${
                    person.visitor_role === role
                      ? 'border-blue-600 bg-blue-600 text-white'
                      : 'border-slate-300 dark:border-slate-700'
                  }`}
                >
                  {role}
                </button>
              ))}
            </div>
          </Field>

          {person.lookup?.photo_required && (
            <Field
              label="Identity photo"
              required
              hint="Required for first-time visitors, and whenever the photo on file is over 180 days old."
            >
              <PhotoCapture
                mobile={person.mobile}
                path={person.id_photo_path}
                onUploaded={(path) => update(person.key, { id_photo_path: path })}
              />
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

/**
 * Capture-and-upload for an ID photo.
 *
 * The file goes straight from the device to Supabase Storage using a one-shot
 * signed URL, never through the API. A guard on a weak mobile connection should
 * not be holding an API worker open for the length of a 4MB upload.
 */
function PhotoCapture({
  mobile,
  path,
  onUploaded,
}: {
  mobile: string
  path?: string
  onUploaded: (path: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function upload(file: File) {
    setBusy(true)
    setError(null)
    try {
      const ticket = await post<{ path: string; upload_url: string }>(
        '/uploads/identity-photo',
        { mobile },
      )

      const response = await fetch(ticket.upload_url, {
        method: 'PUT',
        body: file,
        headers: { 'Content-Type': file.type || 'image/jpeg' },
      })

      if (!response.ok) throw new Error('Upload failed')
      onUploaded(ticket.path)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed. Please retry.')
    } finally {
      setBusy(false)
    }
  }

  if (path) {
    return (
      <div className="flex items-center gap-3 rounded-xl bg-ok-bg p-4 text-ok dark:bg-ok-darkbg dark:text-ok-dark">
        <span className="text-2xl">✓</span>
        <span className="font-bold">Photo captured</span>
      </div>
    )
  }

  return (
    <div>
      <label className="btn-ghost w-full cursor-pointer">
        {busy ? 'Uploading…' : '📷 Take photo'}
        <input
          type="file"
          accept="image/*"
          // `capture` opens the rear camera directly rather than the photo
          // library — the guard wants the person in front of them, now.
          capture="environment"
          className="sr-only"
          disabled={busy}
          onChange={(event) => {
            const file = event.target.files?.[0]
            if (file) void upload(file)
          }}
        />
      </label>
      {error && (
        <p className="mt-2 text-sm font-semibold text-bad dark:text-bad-dark">{error}</p>
      )}
    </div>
  )
}
