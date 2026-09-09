# Reward360 Warehouse Management

Tracks goods from the moment a truck arrives at the gate to the moment it leaves
with packed cartons. Nothing moves without being counted, and every count is
attributable to a named person at a recorded time.

**Stack:** FastAPI · React + TypeScript · Supabase (Postgres, Auth, Storage) ·
Render · Vercel

**The whole process is implemented and verified running.** Gate entry, Ops
approval, box counting, unit scanning, inbound reconciliation, invoice matching,
packing attribution, out-scan, batch release, pickup verification, gate exit,
exceptions, dashboard and reports — plus staff provisioning and badge issue.

Putaway, rack locations and the stock lookup were removed in 0042 at the user's
request: the app no longer tracks where cartons are shelved, so receiving ends
at CONTROL POINT 4 (see DECISIONS.md §E7).

**Eleven hard stops are enforced by the database** — the seven control points in
PRD §4, plus the four outbound gates added in Phase 5 (packing assignment,
product-box reconciliation, the guard's carton count, and Admin approval of the
truck leaving). 166 automated tests and four end-to-end walkthroughs (134, 45, 40
and 14 checks) pass against the local stack, covering the whole path from a truck
arriving at the gate to a different truck leaving with packed cartons.

---

## Setup

The toolchain is already installed on this machine (Node 20, Python 3.11,
Supabase CLI, and Colima for containers). On a fresh machine:

```bash
brew install node@20 python@3.11 supabase/tap/supabase colima docker docker-compose
brew link --overwrite --force node@20
```

Colima is used instead of Docker Desktop because it installs and runs without
admin rights. It provides the same Docker API, which is all the Supabase CLI
needs.

```bash
# 0 — container runtime (once per reboot)
colima start --cpu 4 --memory 8 --disk 60

# 1 — database, auth and storage
supabase start                    # first run pulls several GB of images
supabase db reset                 # applies migrations + seed data
supabase status                   # prints the anon key and JWT secret

# 2 — backend
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env              # paste the keys from `supabase status`
uvicorn app.main:app --reload     # http://127.0.0.1:8000/docs

# 3 — worker (separate terminal): SLA sweeps, email, photo retention
cd backend && source .venv/bin/activate && python -m app.worker

# 4 — frontend (separate terminal)
cd frontend
npm install
cp .env.example .env.local        # paste the same URL + anon key
npm run dev                       # http://localhost:5173
```

`supabase db reset` also sets the password for `api_user`, the non-superuser
role the API connects as. That distinction matters more than it looks: a
superuser connection bypasses row level security entirely and every policy in
the database silently stops applying. The API logs which role it connected as at
startup, warns in development, and refuses to boot in production if it is a
superuser.

### Demo accounts

Each account has its own password — they are in `supabase/seed.sql` next to
the account, and in `supabase/manual/production_staff_accounts.sql` for a real
deployment. There is no single shared one.

Six roles: `security_guard`, `ops_manager`, `offloading`, `invoice_matcher`,
`packer`, `admin`. A seventh, `warehouse_staff`, was retired with putaway
(0042) — `store@r360.local` still exists and still signs in, but the role now
grants nothing beyond About me.

| Email | Role |
|---|---|
| `guard@r360.local` | Security Guard |
| `boopathi@r360.local` | Ops Manager · `opsbackup@r360.local` Admin |
| `offload@r360.local`, `inbound@r360.local` | Offloading Team |
| `store@r360.local` | Warehouse Staff — *retired role, kept to prove it grants nothing* |
| `match1@r360.local`, `match2@r360.local` | Invoice Matcher |
| `pack1@r360.local`, `pack2@r360.local` | Packing |
| `admin@r360.local` | Admin |

Packers and Admins also have **attribution badges**. The API never returns
the code of an existing badge — that would be equivalent to handing over the
badge — so there are two ways to get one to try the flow with.

The realistic one: sign in as `admin@r360.local` → *Staff* → **Issue badge**.
The code is shown once, as a printable QR card, and cannot be looked up again.
That is the whole point, and it is also how badges are made in production.

The shortcut, for poking at things locally:

```bash
docker exec supabase_db_r360-warehouse psql -U postgres -d postgres \
  -c "select employee_code, full_name, badge_code from profiles where badge_code is not null"
```

Then type the code into the "Type badge code instead" field on the matching or
packing page. Note that this query works only as `postgres`; the application's
own role cannot read that column at all.

Outbound email is captured by the local mail catcher at http://127.0.0.1:54324 —
nothing leaves the machine. Note that `[local_smtp].port` in
`supabase/config.toml` publishes only that web UI; `smtp_port = 54325` is what
publishes the SMTP listener the worker actually connects to. Without it every
email is recorded as failed with `Connection refused`, which reads like a broken
mail setup rather than a missing port mapping.

## Walking the whole process end to end

Two browsers (or one plus a private window) makes this much easier, because the
whole point of CONTROL POINT 1 is that one person cannot do both halves.

1. **Guard** → *Gate Entry*. Register a driver against vendor "Acme Electronics"
   and PO-2026-0001. Send for approval. The gate is now locked.
2. **Admin** → *Approvals*. Approve it. (Try approving as the same guard who
   filed it — the database refuses.)
3. **Guard** → *Trucks* → open gate → *Count boxes*. PO-2026-0001 is 60 units
   across three SKUs at 10 per box, so the correct count is **6**.
4. **Admin** → same page → generate the sticker sheet, print it. Enter a
   different number at step 3 to see the count mismatch stop the line instead.
5. **Packer** (`pack1@r360.local`) → scan all six box stickers, then verify.
   Scan one twice to watch it be rejected rather than double-counted.
6. **Admin** → *Scan units* → generate unit stickers.
7. **Packer** → pick a box, scan its units, answer the damage check, close it.
   Deliberately scan only 8 of 10 to see the box held and an exception raised.
8. **Admin** → *Exceptions* → accept short / recount / reject.
9. **Offloader** (`offload@r360.local`) → *Verify inbound counts*. Enter a
   wrong number to see the entry held open and an exception raised, then the
   right one. This is the last step of receiving — putaway was retired in 0042.
10. **Packer** (`pack1@r360.local`) → *Matching*. Photograph the invoice; OCR
    reads the Order No and creates the invoice from it. Then scan a *different*
    packing lady's badge to hand the carton over — try your own to see it
    refused, because the person handing it over cannot also pack it (CP5).
11. **Packer B** (`pack2@r360.local`) → *Packing*. The carton is in her queue.
    Scan her own badge to confirm she packed it.
12. **Ops** → *Out-Scan*. Select packed cartons, create a batch, then scan each
    carton's invoice label. Try completing with one unscanned to see CP6 stop it,
    then complete. Now try to release — refused: nobody has counted the cartons.
13. **Guard** → *Carton Count*. Type how many cartons are physically on the bay.
    The system's number appears only after you commit to yours. Enter a wrong
    number to see the mismatch flagged, then send it to Admin.
14. **Ops** → *Approvals*. The count is at the top. Approve it (try as the
    guard who counted — refused), then release the batch.
15. **Guard** → *Pickup*. The released batch appears. Register the collecting
    vehicle — note that the driver from step 1 is recognised and not
    re-photographed. Scan cartons onto the vehicle; try verifying with one still
    missing to see CP7 refuse, then load the last one and verify.
16. **Guard** → *Request permission to leave*. The gate does not open yet.
17. **Ops** → *Approvals*. Hold the vehicle with a reason and watch it come back
    to the guard's screen with that reason on it. Then approve.
18. **Guard** → *Open gate*. Time out is stamped, and the vehicle leaves.

Separately, as **Admin** → *Staff*: add a packer, note the temporary password,
sign in as them. Issue their badge and print the card. Reissue it and watch the
first code stop working at the packing station. Try issuing a badge to the guard
— refused, because a badge on a guard would attribute nothing. Then open
*Change role or access* and read the account's history: the reissue is there,
with your name against it, and the code is not.

## Tests

```bash
cd backend && source .venv/bin/activate
pytest                            # 167 tests; skips cleanly with no database
python scripts/e2e_full_flow.py   # 122 checks: the whole process over real HTTP
python scripts/e2e_role_access.py # 582 checks: every route against every role
python scripts/e2e_admin.py       # 40 checks over real HTTP
python scripts/e2e_retention.py   # 14 checks against real Supabase Storage
```

The two flow scripts need `uvicorn` running and the local stack up. Sign-in uses
the per-account passwords in `supabase/seed.sql`.

The tests run against a real Postgres, because what they test *is* the database:
triggers, constraints and RLS policies. `test_control_points.py` asserts that
each hard stop in PRD §4 is refused by the database rather than by application
code, `test_packing.py` and `test_pickup.py` cover the outbound half,
and `test_rls.py` assumes each role's identity the same way a request does and
checks what it can and cannot see. `test_admin.py` is mostly about read paths
that must *not* exist — including the one through the audit log, which is where
badge codes were actually leaking.

`test_worker.py` covers the background sweeps, and exists because the SLA
escalation in DECISIONS §4 had never run once — it raised on its first statement
every cycle, logged "retrying next cycle", and stayed silent. A job that catches
its own exceptions to stay alive looks identical to a job with nothing to do, so
it needs a test that calls it.

The scripts cover what only becomes true outside the database.

`e2e_full_flow.py` walks the entire process — truck registered, approved,
admitted, counted, stickered, scanned, closed, reconciled, shelved; carton
invoiced, assigned, packed, batched, out-scanned, counted, released, loaded and
driven out — with each step performed by the role whose button it is. It exists
because a control point can hold perfectly in Postgres while the route in front
of it is unreachable: the last thing it caught was `POST /pickups` answering 500
for every invoice the current flow can produce, because a response model still
required the `sku` that 0036 stopped collecting.

`e2e_role_access.py` is the other half: it points every route at a nonexistent id
as all seven roles and records whether the answer was "not your role" or
something else, then compares that against the intended access matrix. The
frontend decides which buttons to draw from the same matrix (`/me`'s
`nav_pages`), so a disagreement here is a button somebody can press that can only
ever answer 403 — which on a warehouse floor is indistinguishable from a broken
app. Nothing is created, so it is safe to run against a live stack.

`e2e_admin.py`: that `require_admin` is genuinely wired onto the routes, and that
an account created on that screen can actually sign in. `e2e_retention.py`: that
an identity photo really leaves Supabase Storage — the one thing a retention job
has to do, and the half a database test cannot see. Those two are not idempotent,
so `supabase db reset` afterwards.

Each test creates the rows it needs and rolls them back, so the suite is
independent of whatever state the database happens to be in. That matters more
than it sounds: an earlier version reused the seeded invoices and started failing
the moment the end-to-end walkthrough consumed them, for reasons that had nothing
to do with the code being tested.

One caveat worth knowing: most of these tests connect as the table owner, so RLS
is bypassed and they are testing triggers, not policies. That is deliberate —
but it means a policy gap can hide behind a green suite. Two of the bugs found
while bringing this up were exactly that shape, so the tests that matter for
access control — `test_rls.py`, `test_role_split.py` and
`test_guard_can_register_and_scan_under_rls` — explicitly `set role
authenticated` first.

## Devices and browsers

The floor runs this on mid-range Android phones, so the app is built for one and
degrades rather than breaks on anything older: touch targets are 56px, the
layout wraps instead of scrolling sideways, the safe-area inset is respected,
and every camera surface has a typed fallback next to it.

What it needs, and what happens below that:

| | Floor | Below it |
|---|---|---|
| The app itself | Chrome/WebView 80, Safari 14 (es2020) | — |
| QR scanning | any camera, **served over https** | the code can be typed |
| Order-No OCR from the camera | same | photograph-and-upload, or type it |
| Reading a challan **PDF** | Chrome 119 / Safari 17.4 | reported as unreadable; photograph it instead |
| Installed as an app (PWA) | Android Chrome, iOS 16.4+ | it still works in a browser tab |

Two things are worth knowing because they look like faults and are not:

**The camera needs https.** `navigator.mediaDevices` only exists in a secure
context, so on `http://<laptop-ip>:5173` — which is how `vite --host` serves a
phone on the office wifi — the browser withholds the camera entirely. The app
now says so instead of reporting "no camera on this device" about a phone that
plainly has one. `localhost` is exempt; a LAN address is not.

**Ids and stored preferences have fallbacks.** `crypto.randomUUID` is also
secure-context-only (and Chrome 92+), and it minted every scan's idempotency
key — over http it threw and took the scanning loop with it. `localStorage`
throws outright, not returns null, in a private window or a WebView with site
data blocked, and the theme and language are read before the first render, so
that crashed at boot. Both are wrapped now (`lib/ids.ts`, `lib/deviceStorage.ts`).

## Security posture

The short version: authentication is Supabase-issued JWTs verified with one
algorithm pinned per branch; authorisation is checked three times over
(navigation, `require_roles` on the route, RLS in the database, which is
`force`d and which the API refuses to boot without); both photo buckets are
private with mime allowlists, and identity photos are a write-only drop box that
the guard who fills it cannot read back; badge codes never leave the database and
are redacted from the audit log; identity photos are destroyed at 180 days by a
worker that holds the only credential able to delete them.

`docs/DECISIONS.md` Part E is the review that produced that summary, including
the five things it found — an Ops Manager who could make themselves Admin, a
`DELETE` that could never pass CORS, an unvalidated path reflected into a
service-role request, Postgres DETAIL leaking conflicting values to clients, and
an unvalidated post-login redirect — and what was done about each.

Two things a reader should know are *not* done:

- **No Content-Security-Policy.** It is the biggest remaining hardening step and
  the one that cannot be written from inside this repo: `connect-src` has to name
  the real API and Supabase origins, which live in deployment environment
  variables. A starting point, with those substituted in and set in
  `frontend/vercel.json`: `default-src 'self'; connect-src 'self' <api-origin>
  <supabase-origin> wss://<supabase-host>; img-src 'self' data: blob:;
  script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline';
  worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'`. Test it
  against the OCR and scanning pages before shipping it — those are the parts a
  wrong policy breaks silently.
- **No application-level rate limiting.** Sign-in goes straight to Supabase,
  which has its own limits; the API itself will answer as fast as it is asked.
  Fine behind a warehouse's own network, worth revisiting if it is ever exposed
  more widely.

## How it is put together

```
supabase/migrations/   0001-0006 schema, audit, control points, RLS, storage
                       0007      putaway (retired by 0042)
                       0008-0009 packing, batches, out-scan
                       0010      badge protection + view security
                       0011-0012 pickup, gate exit
                       0013      admin provisioning, badge issue, audit redaction
                       0014      identity photo retention
                       0015      order-no OCR
                       0016-0019 packing assignment, carton count and exit
                                 approvals, product-sticker reconciliation
                       0039      role grants are Admin-only
                       0042      putaway retired
backend/app/
  db/session.py        RLS-aware transaction — the important file
  core/errors.py       Postgres refusals → messages a guard can act on
  core/security.py     JWT verification (ES256 via JWKS, or HS256)
  services/            business flows; the database enforces, this explains
  api/v1/              routes
  worker.py            SLA escalation, email, photo retention — separate process
  services/retention.py identity photos are destroyed at 180 days, not just hidden
  services/loading.py  the guard's carton count and Admin's decision on it
  scripts/e2e_full_flow.py the whole process over real HTTP, role by role
  scripts/e2e_role_access.py every route against every role, plus the
                       security invariants that are about request shape
  scripts/e2e_admin.py the Admin flow over real HTTP
frontend/src/
  lib/offlineQueue.ts  IndexedDB scan queue, idempotent replay
  hooks/useScanning.ts the scan loop shared by all three scanning pages
  components/Scanner   lazy-loaded camera scanner (keeps 414kB off first load)
  components/BadgeScan attribution capture, with the "this is not a login" framing
  components/BadgeCardPrint the printable badge — QR only, deliberately no text
  components/PersonFields visitor registration, shared by inbound and outbound
  pages/               one page per PRD §5 screen
docs/WORKFLOW.md       the process end to end: who does what, and what it refuses
docs/DECISIONS.md      answers to PRD §13, and why
```

Three design decisions carry most of the weight:

**The control points live in the database.** Each of the seven hard stops in
PRD §4 is a Postgres trigger or constraint, not just an `if` in a service. The
first success metric is zero manual overrides, and a rule that exists only in a
FastAPI handler is one hotfix away from not being a rule. The service layer
still checks — that is where the friendly message comes from — but the database
is what makes it a guarantee.

**RLS applies through the API.** Every request runs in a transaction that assumes
the `authenticated` role and installs the caller's verified JWT claims, so
`auth.uid()` resolves inside policies exactly as it would for a direct
supabase-js call. A route handler that forgets a permission check still cannot
read a guard's identity photos.

**Counts are derived, never written.** `boxes.scanned_units` is maintained
exclusively by a trigger on the scan ledger and rejects direct updates. If
application code could set it, "scanned equals expected" would prove nothing.

**A badge is attribution, not a credential.** Scanning a badge records who
handled an item; it grants nothing and cannot be exchanged for a session. The
invariant, stated precisely: *no operation tells anyone the code of a badge that
is currently in someone's pocket.* `profiles.badge_code` is revoked from
`authenticated` at the column level, redacted out of the audit trail, and
resolved only through a `SECURITY DEFINER` function that takes a code and returns
a person. Exactly one operation returns a code — issuing one, to the Admin who
asked, for a badge minted in that request. "Reissue" therefore means *replace*,
so a lost badge is never looked up and the found badge stops working immediately.

All of that exists because being able to read someone's badge code is equivalent
to holding their badge, which would let one person satisfy both halves of
CONTROL POINT 5 alone.

A consequence worth knowing when reading the code: a failed control point
*writes* — it holds the box, logs an exception against the vendor, alerts Admin.
So those endpoints return `409` with a full body rather than raising, because
raising would roll back the transaction containing the very record that makes
the hold enforceable. `postControlPoint()` on the frontend is the matching half.

## Deployment

- **Backend + worker** → Render, via `render.yaml`. Set every `sync: false` env
  var in the dashboard; `DATABASE_URL` must use `api_user`.
- **Frontend** → Vercel, via `frontend/vercel.json`. Set `VITE_SUPABASE_URL`,
  `VITE_SUPABASE_ANON_KEY` and `VITE_API_URL`.
- **Database** → a hosted Supabase project. `supabase link` then
  `supabase db push`. **Do not run `seed.sql` against production** — it creates
  demo accounts with a known password, and refuses to run if
  `app.environment` is set to `production`.

## Out of scope

Per PRD §12: returns/RTO, cycle counting, the damage *claims* workflow (evidence
is captured, the claim process is not), external vendor integrations, and barcode
generation for goods that arrive pre-labelled.

Worth knowing about for a real deployment, and not required by the PRD: push
notifications rather than in-app plus email. That is the last item on the list —
identity photo retention was the other one and now runs in the worker, destroying
photos at `ID_PHOTO_REVALIDATION_DAYS` and reporting its own posture on the
*Staff* screen. `overdue` there is the number to watch: a retention job that
stops running breaks nothing, which is why it needs somewhere to be visible.

One migration note for an existing deployment: **0013 rotates every badge code,
so every badge has to be reprinted.** That is the remediation for badge codes
having been readable out of the audit trail (see DECISIONS.md Part D) — the
codes were exposed, and `audit_log` is append-only by design, so they are
replaced rather than un-published. Reissue from the *Staff* screen.

