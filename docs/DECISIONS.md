# Architecture & Product Decisions

This file records decisions taken while building the app, including answers to the
open questions in PRD §13. **Every decision here is overrulable** — tell me and I
change it. Config-driven items name the setting that controls them.

---

## Part A — Answers to PRD §13 Open Questions

### 1. Packing lady ID — badge card, QR on badge, or fingerprint?

**Decision: QR code printed on a laminated badge card.**

- Fingerprint requires dedicated hardware at every packing station and stores
  biometric data, which raises the compliance bar sharply under the DPDP Act
  2023. Not worth it for attribution.
- A plain badge card (no QR) can't be *scanned*, so attribution would be typed —
  and typed IDs get shared, guessed, and mistyped.
- QR on a badge reuses the exact same scanner the app already needs for stickers.
  One interaction model to train, one piece of hardware.

Implementation: `profiles.badge_code` holds an opaque, random, non-guessable
token (`BDG-` + 16 hex chars). The QR encodes that token, never the name or the
employee number. Badges are revocable (`profiles.badge_active`) so a lost badge
is killed without touching the user account.

> Note: this is *attribution*, not *authentication*. The badge proves "this
> packer handled this invoice". It does not grant login. Login is a separate
> Supabase Auth session. This distinction matters — it means a shared badge
> can't be used to escalate privileges.

**Phase 3 item.** Schema is in place from day one so Phase 1 audit records can
already reference it.

### 2. First-time ID photo — every visit or only first time?

**Decision: first visit only, with a forced re-capture every 180 days.**

Visitors are deduplicated on mobile number in a `visitors` table. The gate entry
form shows the stored photo and asks the guard to confirm identity rather than
re-shoot it. This keeps the <2 min/entry target realistic — photo capture is the
slowest step in the form.

The 180-day expiry exists because a photo from two years ago verifies nothing.
Controlled by `ID_PHOTO_REVALIDATION_DAYS` (default 180).

Photos live in a **private** Supabase Storage bucket (`identity-photos`), served
only through short-lived signed URLs to Ops/Admin. Guards can upload but cannot
list or read back other visitors' photos — see §Storage RLS.

### 3. Partial mismatch — if 8 of 10 units arrive, do 8 enter or is the box rejected?

**Decision: the whole box is held, not rejected, and nothing enters until Ops
decides.** Ops then picks one of three explicit outcomes:

| Outcome | Meaning | Effect |
|---|---|---|
| `ACCEPT_SHORT` | Short supply is real and agreed | 8 units enter, PO line's received qty = 8, shortfall logged against vendor |
| `RECOUNT` | Suspected scan/count error | Box returns to scanning state, counters reset, both attempts kept in audit |
| `REJECT_BOX` | Refuse the box | 0 units enter, box marked rejected, vendor debit note trail |

Rationale: "reject the whole box" as an automatic rule destroys good stock over
what is usually a scanning slip, and "let the 8 in" silently normalises shortfall
— which is exactly the leak the vendor accuracy report is meant to catch. Neither
is safe as a default, so the system refuses to guess and escalates. The hard stop
holds: **no units enter the warehouse on a mismatch without a named human
decision recorded against the exception.**

The units already scanned are *not* discarded while held — they stay attached to
the box in `scan_events`, so `RECOUNT` and `ACCEPT_SHORT` both have evidence.

### 4. Ops approval SLA before auto-escalation?

**Decision: 15 minutes to a backup approver, 30 minutes to Admin. Never
auto-approve.**

- T+0: approval request created, realtime alert + email to Ops Manager.
- T+15m: escalates to any user with `ops_manager` role marked `is_backup_approver`, re-alerted.
- T+30m: escalates to `admin`, and the entry is flagged `sla_breached` for the daily report.

Auto-approval on timeout would make the gate control point decorative — the
single easiest way to defeat it would be to submit at lunchtime and wait. So the
timer escalates the *notification*, never the *decision*. A truck can wait; an
unapproved truck entering cannot be undone.

Controlled by `GATE_APPROVAL_SLA_MINUTES` (15) and `GATE_ESCALATION_MINUTES` (30).

### 5. Damaged goods — visual inspection step at offloading?

**Decision: yes — a mandatory damage checkpoint per box, but it does not block
the line.**

At unit scanning the offloader must answer one question per box before the box
can close: **"Any visible damage?"** → `NONE` / `PACKAGING` / `PRODUCT`. Anything
other than `NONE` requires at least one photo and raises a `DAMAGE` exception
tagged to the vendor and PO, but the good units still flow through. Damaged units
are scanned into a `QUARANTINE` disposition instead of stock.

Rationale: damage is a commercial dispute, not an integrity failure. Blocking
putaway on it would stall the warehouse over something Ops settles with the
vendor days later. But making it optional means it never gets recorded, and then
there's no evidence when the claim is raised. Mandatory to answer, cheap to
answer, non-blocking.

Full damage-claims workflow is explicitly out of scope (PRD §12); this only
captures the evidence.

### 6. Rack numbering scheme?

**Decision: `Z-AA-RR-LL-BB`** — Zone, Aisle, Rack, Level, Bin.

Example: `A-01-04-02-03` = Zone A, Aisle 01, Rack 04, Level 02, Bin 03.

- Fixed width, so it sorts correctly as a string and QR labels are uniform.
- Zone as a letter separates functionally different areas (A = fast-moving,
  B = bulk, C = high-value/caged, Q = quarantine, R = returns) without renumbering.
- Five levels means a picker reads the location left-to-right in the same order
  they physically walk it: find the aisle, find the rack, look up, reach in.

Validated by regex `^[A-Z]-\d{2}-\d{2}-\d{2}-\d{2}$` in
`locations.code`. Locations are seeded, not free-typed, so a typo can't invent a
rack. **Phase 2 item**; the table and validation ship now so Phase 1 exceptions
can reference a quarantine location.

---

## Part B — Technical Decisions

### B1. Supabase Auth + RLS, enforced *through* the API

Chosen over "FastAPI owns auth". The consequence people usually miss: if the
backend connects to Postgres as the `postgres` superuser or with the
`service_role` key, **RLS is bypassed entirely** and the policies are decorative.

So the backend connects as a dedicated, non-superuser login role `api_user`, and
every request runs inside a transaction that does:

```sql
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claims', '<verified claims json>', true);
```

`auth.uid()` and `auth.jwt()` then resolve exactly as they do for a direct
PostgREST call, and the same policies protect both paths. The JWT is verified by
FastAPI *before* those claims are trusted (HS256 against `SUPABASE_JWT_SECRET`),
so a forged claims blob can't be injected.

This is defence in depth, not belt-and-braces theatre: a bug in a FastAPI route
handler that forgets a permission check still cannot read a guard's identity
photos, because the database refuses.

A separate `service_role`-equivalent connection exists for exactly three jobs —
running migrations, the SLA escalation worker, and the audit writer — and is
never reachable from a request path.

### B2. No-deletion policy is enforced by the database, not by convention

`REVOKE DELETE` on every business table from `authenticated`, plus a
`raise_no_delete()` trigger on each, so even a superuser mistake surfaces as an
error rather than silent data loss. Corrections are **reversing entries**: a new
row with `reverses_id` pointing at the original and a mandatory reason. The
original stays visible and stays in reports.

Mutable status columns are the one exception (a box legitimately moves
`pending → verified`), and every such change is captured by the audit trigger
with full before/after JSONB.

### B3. Control points live in the database

Each of the seven hard stops in PRD §4 is a Postgres trigger or `CHECK`, not just
an `if` in a service. A hard stop that only exists in application code is one
hotfix away from being bypassed, and the PRD's first success metric is *zero
manual overrides*. Service-layer checks still exist — they produce the friendly
error message — but the database is what makes the guarantee.

### B4. Offline scanning: client-generated idempotency keys

Every scan carries a `client_event_id` (UUID minted on the device) with a unique
constraint. The device queues scans in IndexedDB and replays them on reconnect;
replays collide on that constraint and are absorbed as no-ops. This makes sync
safe to retry indefinitely, which is what gets the <1% data loss target — the
device never has to reason about whether a scan "went through".

Scans also carry `scanned_at` from the device clock *and* `recorded_at` from the
server, because a phone that was offline for an hour must not backdate into a
closed batch, and a report that says "8:00pm" when the shift ended at 6pm is a
question no one can answer later.

### B5. Python 3.11 / Node 20 targets

The code targets Python 3.11 (Render's default) and Node 20 (Vercel's). Both are
installed locally, along with the Supabase CLI and **Colima** rather than Docker
Desktop — Colima installs and runs without admin rights and exposes the same
Docker API, which is all the Supabase CLI needs.

### B6. JWT verification accepts ES256 (JWKS) and HS256

Supabase now signs access tokens with a rotating **ES256** key published at
`/auth/v1/.well-known/jwks.json`. Older and some self-hosted projects still use
HS256 with a shared secret. The backend supports both, but reads the algorithm
from the token header only to *select the verification path*, never to decide
whether verification happens: each branch passes exactly one algorithm to
`jwt.decode`, so a token claiming HS256 can only ever be checked against the
shared secret. That closes the classic confusion attack where an HS256 token is
signed with an asymmetric pair's public key.

### B7. Failed control points return 409 with a body, they do not raise

A failed hard stop is not a read-only event — it holds the box, writes an
exception against the vendor and PO, and alerts Ops. Raising out of the request
would roll back the transaction containing the very record that makes the hold
enforceable and auditable.

So the four control-point endpoints set a 409 on the response and return a full
body (`closed: false`, `verified: false`, the exception code). `postControlPoint()`
on the frontend is the matching half, treating 409 as data rather than an error.

---

## Part C — Phase 2: putaway

### C1. Putaway is blocked until inbound reconciliation passes

`fn_putaway_guard` refuses to shelve anything whose gate entry is not
`reconciled`. This is what makes CONTROL POINT 4 more than advisory: failing it
does not merely show a warning, it stops the goods physically reaching a rack.

### C2. A box may be split across racks

Ten units can go six to one bin and four to another. The alternative — forcing
one location per box — sounds tidier but means lying about where half the stock
is the first time a bin is full, and a location record that is wrong is worse
than one that is granular.

The box is only marked `emptied` once every unit, in both dispositions, has a
home.

### C3. Damaged units can only go to a quarantine rack

`locations.is_quarantine` is a generated column derived from the zone letter, so
a quarantine rack cannot be relabelled into a stock rack. Placing `quarantine`
units in a non-Q location is refused, and so is placing `stock` units in a Q
location.

This is a hard stop rather than a warning because the failure mode is damaged
goods being picked and sold as new — the kind of error that surfaces as a
customer complaint weeks later, with no way to trace it.

### C4. Putaway cannot exceed what arrived

Placement totals are checked against the scan ledger. Without this, putaway
would be a second, unaudited route to creating inventory — one that bypasses
every count in Phase 1.

### C5. Warehouse staff *and* the offloading team can shelve goods

On a busy shift the people unloading also move cartons to racks. Restricting
putaway to `warehouse_staff` alone would produce exactly the workaround this
system exists to prevent: one person's login being used by three people.

---

## Part CC — Phase 3: matching, packing, out-scan

### CC1. The carton label is the invoice number

PRD §5.6 says Ops scans "each carton's invoice label", so out-scan resolves
against `invoices.invoice_number` rather than against a sticker. `scan_events`
now carries either a `sticker_id` or an `invoice_id` and the resolver picks by
scan type; the accepted-scan constraint became "one of the two".

The alternative — minting a sticker per carton at packing time — would have added
a printing step to a station that does not have a printer, to gain nothing.

### CC2. Badges are unreadable, and resolved one-way

`profiles.badge_code` is revoked from `authenticated` **at the column level**
(the table-level `SELECT` grant had to be replaced by a column list for that to
take effect). Badges are resolved through `resolve_badge_holder(code)`, a
`SECURITY DEFINER` function that takes a code and returns the person. Nothing in
the system performs the inverse.

The reason is CONTROL POINT 5. If one packer could read another's badge code,
they could attribute work to them — and an Ops manager could satisfy both halves
of the two-person rule alone. Codes are 64 bits of randomness, so the function is
not an enumeration oracle.

Names and roles *are* readable by all staff, because they appear on badges,
rosters and the wall, and because every screen showing "verified by X" needs
them. Treating them as secret would break the UI while protecting nothing.

### CC3. Ops managers carry badges too

Ops covers the matching and packing stations during breaks. This is also the only
way the two-person rule is reachable at all: a matcher's badge cannot pack and a
packer's badge cannot verify, so the same-person case only arises for someone
permitted to do both.

### CC4. Batches are planned before they are scanned

Ops selects packed cartons into a batch, and only then out-scans them. CONTROL
POINT 6 compares cartons assigned against cartons physically scanned — the same
shape as the box count at the gate.

A batch assembled from whatever happened to get scanned could never fail its own
check, which would make the control point decorative. For the same reason,
cartons cannot be moved out of a batch once scanning has begun: otherwise "all
cartons scanned" could be satisfied by quietly dropping the missing one.

### CC5. Invoices close on batch release

Releasing a batch sets `is_open = false` on its invoices. The goods have left the
packing area, so the floor should no longer be able to act on them. The invoice
row stays visible — nothing is deleted — and the reason is recorded in
`closed_reason`.

---

## Part CD — Phase 4: pickup and gate exit

### CD1. Outbound gets its own table

An inbound gate entry is about a vendor, a PO and a box count. An outbound one is
about a batch and a carton count. Reusing `gate_entries` with a direction flag
would leave half the columns permanently null and put a filter on every query
that someone will eventually forget.

`pickups` reuses the *visitor registry* though, so a driver who both delivers and
collects is one person with one identity photo, not two. That is the part worth
sharing; the rest is genuinely different.

### CD2. One pickup per batch

`pickups.batch_id` is `unique`. Two vehicles collecting one batch would make "all
cartons present" ambiguous about which cartons belong to which truck, and
CONTROL POINT 7 would have nothing meaningful to compare.

A consequence: cancelling a pickup does not free the batch for a new one without
Ops involvement. That is deliberate — the goods are still in the pickup area, and
silently allowing a second attempt is how a carton goes out twice.

### CD3. A carton cannot be loaded unless its batch was released

The gate-exit resolver refuses any carton whose batch is not `released`. This is
what stops a carton being carried straight from the packing bench onto a waiting
truck, bypassing the out-scan entirely.

### CD4. Verification is an explicit act, not the last scan

Scanning the final carton moves the pickup to `verifying`, not `verified`. The
guard has to confirm. The reason is that CP7 is the last chance to catch a
problem, and "the system decided we were done because you scanned something"
is a worse basis for opening a gate than "a named person said so at 18:42".

The same pattern is used for batch completion in Phase 3, for the same reason.

---

## Part CE — Phase 5: the Admin screen

### CE1. Provisioning and badge issue belong to Admin, not Ops

Ops can see everything operational, but not this. An Ops Manager who could
issue a badge could issue themselves a second one under a packer's name, and
CONTROL POINT 5 is satisfied by two *badges*, not two *people* — the two-person
rule is only real because nobody can manufacture the second person.

This is the one place the codebase distinguishes Admin from Ops as a capability
rather than a job title, which is why `require_admin` is named in `deps.py`
instead of written inline: `require_roles("admin")` reads like a mistake, given
admin is silently added to every other guard.

### CE2. A badge code is returned once, at the moment it is minted

§CC2 says badge codes are unreadable. But somebody has to print the QR, so the
invariant needs stating more precisely than "unreadable":

> No operation tells anyone the code of a badge that is currently in someone's
> pocket.

`admin_issue_badge()` satisfies that while still being useful. It mints a fresh
code and returns it to the Admin who asked, once. There is no operation that
reads an existing code, and "reissue" means *replace* — so a lost badge is
replaced rather than looked up, and the found badge stops working the moment the
replacement is printed. Losing the code before printing costs a reissue and
nothing else, which is why the screen refuses to be navigated away from
casually rather than storing the value somewhere convenient.

The badge card prints the QR and **not** the code as text, which is the opposite
of the box and unit stickers. On a sticker, human-readable text is the fallback
for a scuffed QR. On a badge it would be a fallback for the security property —
anyone who glanced at the card could type the code at a station.

### CE3. The last Admin cannot be removed, and no Admin can remove themselves

Both are refused in the service layer rather than the database, because neither
is an integrity rule: a *second* Admin is entirely allowed to demote or
deactivate the first. What they protect against is a warehouse whose only route
back in is a psql prompt — which is the thing this screen exists to remove the
need for.

Moving someone off a badge-carrying role deactivates their badge as part of the
same change, rather than refusing the change. The alternative is two steps in an
order the Admin has to know, and the failure mode of forgetting the second one
is a live badge on someone who no longer works that station.

### CE4. Accounts are created in GoTrue over HTTP, using the service-role key

§B1 keeps the service-role credential away from the request path. That rule is
about the *database* connection: a service-role Postgres connection bypasses
every RLS policy, so no route may hold one. Creating a login is a different
thing — a scoped HTTP call to GoTrue's admin API, which can create an account
and nothing else — and there is no other way to do it.

`trg_auth_user_created` (0003) then builds the profile from the account's
metadata, so the role is correct from the moment the account exists rather than
being patched a statement later.

The consequence worth knowing: GoTrue commits on its own connection, so the new
account cannot be rolled back with the request transaction. Anything that could
fail *after* it — a duplicate employee code, in practice — is therefore checked
*before* it. Otherwise the failure leaves a login with no profile, which
`get_current_user` refuses and no Admin screen would ever show.

---

## Part CF — Phase 5: identity photo retention

### CF1. Retention reuses the revalidation window rather than adding a number

0006_storage.sql made both buckets private and deferred retention to "a
scheduled job". Private forever is still forever, and the DPDP Act's
storage-limitation principle is about how long personal data is held, not who
can see it.

The threshold is `ID_PHOTO_REVALIDATION_DAYS` — the same 180 days that forces a
re-capture — not a second, independent retention setting. §2 already establishes
that a photo past that age verifies nothing, and data held with no purpose left
is precisely what the Act asks you not to hold. A separate retention number
would create a window in which a photo is simultaneously useless and retained,
and there is no argument for the existence of that window.

Clearing the path also clears `id_photo_captured_at`, because
`visitors_photo_consistent` requires the pair to travel together. That is the
right behaviour anyway: a visitor with no capture date is treated as needing a
fresh photo, which is exactly what "expired" means.

### CF2. A blocked visitor keeps their photo

Purpose limitation cuts both ways: data is destroyed when its purpose ends, and
kept while the purpose lasts. A block is enforced on the mobile number, and a
number is trivially borrowed — so for a blocked visitor the photo is still doing
the exact job it was captured for, which is letting a guard confirm that the
person at the gate is the person who was blocked.

Enforced inside `purge_identity_photo()`, not only in the sweep's `WHERE`
clause, so a future second caller cannot quietly bypass it. Blocked photos are
reported as `retained_for_block` rather than counted as `overdue`, because a
deliberately retained photo appearing in a breach count makes the number an
auditor reads stop meaning anything.

### CF3. Storage is deleted before the row is cleared

The reverse order has a failure mode with no recovery: clear the row, fail the
delete, and the bytes survive with nothing pointing at them and no way to find
them again. In this order a crash leaves a file already gone and a row still
pointing at it, which the next sweep retries and storage answers with 404 —
treated as success, because it is.

### CF4. The purge has no manual trigger

The sweep runs only in the worker, which holds the one connection privileged
enough to delete from a bucket that grants `DELETE` to nobody. A "run it now"
button on the Admin screen would mean handing a request handler that connection,
which is the single thing `app/db/session.py` exists to prevent. The screen is
read-only and shows posture instead.

### CF5. `overdue` exists because nothing else would report a stopped job

Every other failure in this system is loud: a refused scan, a rejected badge, a
held box. A retention job that stops running produces no error anywhere, because
**not deleting breaks nothing**. `photo_retention_status()` therefore reports
`overdue` as a first-class number, and `enabled` separately — a backlog because
the worker is behind and a backlog because the service-role key was never set
need different fixes, so they cannot be one field.

---

## Part D — What running the stack changed

These are defects found by actually booting the system, not by reading it. They
are recorded because each one points at a class of mistake worth watching for.

**RLS turns a forbidden write into a silent no-op, not an error.** Twice, an
`UPDATE` matched zero rows because the policy hid the row, and nothing failed:

* Admitting an unapproved truck returned HTTP 200 with an unchanged entry. The
  control point held — the truck did not get in — but the API reported success,
  which for a guard at a gate is arguably worse than a clear refusal.
* `fn_putaway_close_box` could not mark a carton `emptied` because
  warehouse staff were missing from the box-update policy. No error anywhere;
  the box would simply have looked unshelved forever.

The lesson applied throughout: **any update that must change something checks
`rowcount` and refuses to report success otherwise.**

**A test suite that runs as the table owner cannot see policy gaps.** Both bugs
above sat behind a fully green unit suite, because those tests connect as
`postgres` and RLS is bypassed for superusers. Access-control tests now
explicitly `set role authenticated`, and the end-to-end walkthrough drives real
HTTP with real tokens — which is what caught both.

**`INSERT ... RETURNING` also applies the SELECT policy.** A guard raising an
alert addressed to Ops could not read it back, so asking for the id returned made
a legitimate write fail. The policy is correct; the `RETURNING` was unnecessary.

**Two Supabase config switches are not what their names suggest.**
`[auth.email].enable_signup = false` does not merely block new signups — it maps
to `GOTRUE_EXTERNAL_EMAIL_ENABLED` and disables password login for everyone.
Blocking self-service signup is `[auth].enable_signup = false`. Both are now
commented in `supabase/config.toml`.

**asyncpg cannot infer a parameter's type from `:p is null`.** Every optional
filter needs an explicit `cast(:p as <type>)`, and `:p::type` does not work
either — SQLAlchemy's `text()` reads the `::` as part of the bind name and never
substitutes the parameter at all.

**Views were bypassing RLS entirely.** A Postgres view runs with its *owner's*
privileges unless `security_invoker` is set, and these views are owned by
`postgres`. So every policy on the underlying tables was being ignored for anyone
who queried a view. Nothing sensitive leaked — the views only expose operational
aggregates — but "RLS is enforced" has to be true everywhere or it is not
something you can reason about. All seven views are now `security_invoker = true`.

**A specific error message beats a correct one.** Four times, a generic
transition check fired before the check that knew *why* the transition was
refused, so an operator got "Illegal batch transition: scanning -> released"
instead of "cannot be released until every carton is out-scanned (CONTROL POINT
6)". The rule held either way; the message was useless. The specific check now
runs first in the gate, box, batch and pickup guards — which is why all four
guards open with the control-point case before consulting their transition table.

**Adding a role to a step means revisiting the policy for what that step writes.**
Twice: warehouse staff could insert a putaway but not let the trigger mark the box
`emptied`, and guards could insert a gate-exit scan but not let the trigger stamp
the carton. Both would have failed silently under RLS. The lesson generalises —
when a new role gains an action, check every table that action's *triggers* touch,
not just the table it inserts into.

**The SLA escalation had never run once.** `escalate_overdue_approvals` bound its
interval as a string into `cast(:sla as interval)`. That cast tells asyncpg the
parameter *is* an interval, so it tries to encode `'900 seconds'` as one and
refuses outright. The statement was the first in the sweep, so every cycle raised
before doing anything, the worker logged `Sweep failed; retrying next cycle`, and
did that once a minute indefinitely. Nothing in §4 worked: no approval was ever
escalated to a backup, nothing was ever flagged `sla_breached`, and because the
raise happened before `dispatch_emails()`, **no email had ever been sent by this
system at all.** `make_interval(mins => :n)` takes an integer and encodes
unambiguously.

Two lessons. The narrow one extends the `cast(:p as type)` note further down this
list: that idiom fixes asyncpg's *inability* to infer a type, and in doing so
pins the type — which turns a working string literal into a hard error. The
general one is worse, and is the reason `test_worker.py` now exists: **a
background job that logs its own failure and retries is indistinguishable from a
background job with nothing to do.** 112 green tests never touched the sweep,
and the log line that would have given it away scrolled past once a minute in a
terminal nobody was reading. Anything that catches its own exceptions to stay
alive needs a test that calls it.

**The local mail catcher's SMTP port was never published.** `[local_smtp].port`
in `config.toml` maps the *web UI*; the SMTP listener needs `smtp_port` as well.
Without it the worker's `SMTP_HOST=127.0.0.1:54325` gets `Connection refused`,
which is recorded in `notifications.send_error` and looks exactly like a broken
mail configuration rather than a missing port mapping. This is the third entry on
this list about a Supabase config key not meaning what its name suggests, which
is now a pattern worth expecting rather than a coincidence.

**A column-level revoke does not cover the audit trail.** §CC2 revokes
`select (badge_code)` from `authenticated`, because reading someone's badge code
is equivalent to holding their badge. But `fn_audit` stores `to_jsonb(new)` for
every write, and `audit_read` lets anyone passing `is_ops()` read `audit_log` —
so an Ops Manager could lift any packer's badge code straight out of the trail.
An Ops Manager carries a badge of their own (§CC3), which makes that exactly the
pair CONTROL POINT 5 forbids. The grant was the door; this was the window beside
it.

Two things generalise. First: **when a value must not be readable, enumerate
every table it lands in, not just the one it is declared in.** An audit trigger
that copies whole rows is a second copy of every column in the schema, with its
own policy. Second: the fix could not be to scrub the old rows, because
`audit_log` is append-only and `fn_no_update` refused — correctly. A trail that
can be edited to remove an inconvenient row is not a trail. So the leak was
remediated the way a leaked credential always is: **rotate, don't try to
un-publish.** 0013 replaces every exposed code, which makes the copies in the
historical trail worthless while leaving the trail intact. Every badge in
existence at that point has to be reprinted, which is the honest price.

Going forward `fn_audit_redact()` replaces `badge_code` and `mobile` with
`[redacted]` at write time, and deliberately leaves `changed_keys` computed from
the raw rows — so a badge reissue is still visible *as* a badge reissue, with
its actor and timestamp. Redacting the value must not redact the fact.

**A derived boolean needs its own grant, not a view.** The Admin screen has to
show whether a badge is outstanding — one bit of a column nobody may read. A
view computing `badge_code is not null` solves nothing: with `security_invoker`
the caller still needs `SELECT` on `badge_code` to evaluate the expression, and
without it the view bypasses RLS on `profiles` entirely, which is the bug 0010
was written to fix. A generated column derives the bit in the table, where it
can be granted separately from the value it came from.

**`SECURITY DEFINER` plus `set search_path = public` cannot reach pgcrypto.**
Supabase installs pgcrypto into the `extensions` schema, so the
`create extension if not exists` in 0001 is a no-op and `gen_random_bytes` never
lands in `public`. `generate_badge_code()` is plain `language sql` with no
search_path of its own, so it resolved fine for two phases — inheriting it from
whoever called it — and then failed the moment it was called from a definer
function pinned to `public` alone. The error surfaces at the point of minting a
code, a long way from where the mistake looks like it is.

**Tests that depend on seed state fail for the wrong reasons.** `test_packing.py`
originally consumed the seeded invoices, so it broke the moment the end-to-end
walkthrough consumed them first — ten red tests, none of them about the code.
Tests now create the rows they need inside their own transaction. The end-to-end
harness, which genuinely cannot be idempotent (it closes the seeded invoices),
checks for a fresh database up front and says so instead of failing halfway.
