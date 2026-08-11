# Reward360 Warehouse Management

Tracks goods from the moment a truck arrives at the gate to the moment it leaves
with packed cartons. Nothing moves without being counted, and every count is
attributable to a named person at a recorded time.

**Stack:** FastAPI · React + TypeScript · Supabase (Postgres, Auth, Storage) ·
Render · Vercel

**All four phases are implemented and verified running.** Gate entry, Ops
approval, box counting, unit scanning, inbound reconciliation, putaway, rack
locations, stock lookup, invoice matching, packing attribution, out-scan, batch
release, pickup verification, gate exit, exceptions, dashboard and reports —
plus staff provisioning and badge issue, which were SQL jobs until Phase 5.

**All seven control points in PRD §4 are enforced by the database.** 112 automated
tests and three end-to-end walkthroughs (134, 41 and 14 checks) pass against the
local stack, covering the whole path from a truck arriving at the gate to a
different truck leaving with packed cartons.

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

Password for all: `Warehouse@123`

| Email | Role |
|---|---|
| `guard@r360.local` | Security Guard |
| `boopathi@r360.local` | Ops Manager |
| `offload@r360.local` | Offloading Team |
| `inbound@r360.local` | Inbound Team |
| `store@r360.local` | Warehouse Staff (putaway) |
| `match1@r360.local`, `match2@r360.local` | Invoice Matching |
| `pack1@r360.local`, `pack2@r360.local` | Packing |
| `admin@r360.local` | Admin |

Matchers, packers and Ops managers also have **attribution badges**. The API
never returns the code of an existing badge — that would be equivalent to
handing over the badge — so there are two ways to get one to try the flow with.

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
2. **Ops** → *Approvals*. Approve it. (Try approving as the same guard who filed
   it — the database refuses.)
3. **Guard** → *Trucks* → open gate → *Count boxes*. PO-2026-0001 is 60 units
   across three SKUs at 10 per box, so the correct count is **6**.
4. **Ops** → same page → generate the sticker sheet, print it. Enter a different
   number at step 3 to see the count mismatch stop the line instead.
5. **Guard** → scan all six box stickers, then verify. Scan one twice to watch it
   be rejected rather than double-counted.
6. **Ops** → *Scan units* → generate unit stickers.
7. **Offloader** → pick a box, scan its units, answer the damage check, close it.
   Deliberately scan only 8 of 10 to see the box held and an exception raised.
8. **Ops** → *Exceptions* → accept short / recount / reject.
9. **Inbound** → *Verify inbound counts*. Enter a wrong number to see putaway
   blocked.
10. **Warehouse staff** (`store@r360.local`) → *Putaway*. The boxes appear only
    now, because step 9 had to pass first. Scan or type a rack code
    (`A-01-01-01-01`), place part of a box, then the rest — splitting across
    racks is allowed. Try `Q-01-01-01-01` to watch good stock be refused from a
    quarantine rack.
11. **Warehouse staff** → *Stock*. Where everything ended up, grouped by SKU.
12. **Matcher** (`match1@r360.local`) → *Matching*. Scan `INV-2026-0001`; the page
    tells you which rack the stock is on. Confirm the match, then scan your
    badge. Try a packer's badge to see it refused.
13. **Packer** (`pack1@r360.local`) → *Packing*. The invoice now appears. Scan
    your badge to record the pack. An Ops manager can verify *and* try to pack
    the same invoice — CP5 refuses, because it must be two different people.
14. **Ops** → *Out-Scan*. Select packed cartons, create a batch, then scan each
    carton's invoice label. Try completing with one unscanned to see CP6 stop it,
    then finish and release.
15. **Guard** → *Pickup*. The released batch appears. Register the collecting
    vehicle — note that the driver from step 1 is recognised and not
    re-photographed. Scan cartons onto the vehicle; try releasing with one still
    missing to see CP7 refuse, then load the last one and open the gate.

Separately, as **Admin** → *Staff*: add a packer, note the temporary password,
sign in as them. Issue their badge and print the card. Reissue it and watch the
first code stop working at the packing station. Try issuing a badge to the guard
— refused, because a badge on a guard would attribute nothing. Then open
*Change role or access* and read the account's history: the reissue is there,
with your name against it, and the code is not.

## Tests

```bash
cd backend && source .venv/bin/activate
pytest                          # 112 tests; skips cleanly with no database
python scripts/e2e_admin.py     # 41 checks over real HTTP; needs uvicorn running
python scripts/e2e_retention.py # 14 checks against real Supabase Storage
```

The tests run against a real Postgres, because what they test *is* the database:
triggers, constraints and RLS policies. `test_control_points.py` asserts that
each hard stop in PRD §4 is refused by the database rather than by application
code, `test_putaway.py`, `test_packing.py` and `test_pickup.py` cover Phases 2-4,
and `test_rls.py` assumes each role's identity the same way a request does and
checks what it can and cannot see. `test_admin.py` is mostly about read paths
that must *not* exist — including the one through the audit log, which is where
badge codes were actually leaking.

`test_worker.py` covers the background sweeps, and exists because the SLA
escalation in DECISIONS §4 had never run once — it raised on its first statement
every cycle, logged "retrying next cycle", and stayed silent. A job that catches
its own exceptions to stay alive looks identical to a job with nothing to do, so
it needs a test that calls it.

The two scripts cover what only becomes true outside the database.
`e2e_admin.py`: that `require_admin` is genuinely wired onto the routes, and that
an account created on that screen can actually sign in. `e2e_retention.py`: that
an identity photo really leaves Supabase Storage — the one thing a retention job
has to do, and the half a database test cannot see. Neither is idempotent, so
`supabase db reset` afterwards.

Each test creates the rows it needs and rolls them back, so the suite is
independent of whatever state the database happens to be in. That matters more
than it sounds: an earlier version reused the seeded invoices and started failing
the moment the end-to-end walkthrough consumed them, for reasons that had nothing
to do with the code being tested.

One caveat worth knowing: most of these tests connect as the table owner, so RLS
is bypassed and they are testing triggers, not policies. That is deliberate —
but it means a policy gap can hide behind a green suite. Two of the bugs found
while bringing this up were exactly that shape, so the tests that matter for
access control — `test_rls.py`, `test_storeman_putaway_empties_the_box_under_rls`
and `test_guard_can_register_and_scan_under_rls` — explicitly `set role
authenticated` first.

## How it is put together

```
supabase/migrations/   0001-0006 schema, audit, control points, RLS, storage
                       0007      putaway
                       0008-0009 packing, batches, out-scan
                       0010      badge protection + view security
                       0011-0012 pickup, gate exit
                       0013      admin provisioning, badge issue, audit redaction
                       0014      identity photo retention
backend/app/
  db/session.py        RLS-aware transaction — the important file
  core/errors.py       Postgres refusals → messages a guard can act on
  core/security.py     JWT verification (ES256 via JWKS, or HS256)
  services/            business flows; the database enforces, this explains
  api/v1/              routes
  worker.py            SLA escalation, email, photo retention — separate process
  services/retention.py identity photos are destroyed at 180 days, not just hidden
  scripts/e2e_admin.py the Admin flow over real HTTP
frontend/src/
  lib/offlineQueue.ts  IndexedDB scan queue, idempotent replay
  hooks/useScanning.ts the scan loop shared by all three scanning pages
  components/Scanner   lazy-loaded camera scanner (keeps 414kB off first load)
  components/BadgeScan attribution capture, with the "this is not a login" framing
  components/BadgeCardPrint the printable badge — QR only, deliberately no text
  components/PersonFields visitor registration, shared by inbound and outbound
  pages/               one page per PRD §5 screen
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
*writes* — it holds the box, logs an exception against the vendor, alerts Ops.
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

