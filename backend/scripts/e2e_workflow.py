"""End-to-end HTTP walkthrough of the Phase 5 workflow changes.

    python scripts/e2e_workflow.py [api_base_url]

Covers the four steps added in migrations 0016-0019, over real HTTP with real
tokens: assigning a carton by scanning a packer's badge, scanning product boxes
into it, the guard's carton count and the Ops decision on it, and the exit
approval before the gate opens.

Why this exists alongside tests/test_workflow_v2.py: those drive Postgres
directly, so they prove the triggers hold but not that the routes are wired, the
role guards are attached, or the refusals arrive as messages an operator can
read. DECISIONS.md Part D is a list of things that were only false over HTTP.

Not idempotent — it consumes seeded stickers and closes invoices. Run
`supabase db reset` afterwards.
"""

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import httpx

API = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.environ.get("API_BASE_URL", "http://127.0.0.1:8000/api/v1")
)
SUPA = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54321")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANON = None

checks = {"pass": 0, "fail": 0}


def ok(label, cond, extra=""):
    checks["pass" if cond else "fail"] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  <- {extra}" if extra and not cond else ""))


def anon_key():
    if os.environ.get("SUPABASE_ANON_KEY"):
        return os.environ["SUPABASE_ANON_KEY"]
    out = subprocess.run(
        ["supabase", "status", "-o", "env"],
        capture_output=True, text=True, cwd=os.path.dirname(PROJECT_DIR),
    ).stdout
    for line in out.splitlines():
        if line.startswith("ANON_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("Could not read the anon key. Is the stack running?")


def login(client, email, password="Warehouse@123"):
    r = client.post(
        f"{SUPA}/auth/v1/token?grant_type=password",
        headers={"apikey": ANON, "Content-Type": "application/json"},
        json={"email": email, "password": password},
    )
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def badge_of(employee_code):
    """Read a badge code straight from Postgres.

    The API deliberately has no route that returns one (DECISIONS.md §CC2), so a
    test standing in for a physical card has to go round the outside — which is
    itself a check that the invariant still holds.
    """
    out = subprocess.run(
        [
            "docker", "exec", "-i", "supabase_db_r360-warehouse",
            "psql", "-U", "postgres", "-d", "postgres", "-Xqt", "-c",
            f"select badge_code from profiles where employee_code = '{employee_code}'",
        ],
        capture_output=True, text=True,
    ).stdout.strip()
    if not out:
        raise SystemExit(f"No badge for {employee_code}")
    return out


def scan(client, url, headers, code):
    return client.post(
        url,
        headers=headers,
        json={
            "client_event_id": str(uuid.uuid4()),
            "raw_code": code,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def sql(query):
    return subprocess.run(
        [
            "docker", "exec", "-i", "supabase_db_r360-warehouse",
            "psql", "-U", "postgres", "-d", "postgres", "-Xqt", "-A", "-c", query,
        ],
        capture_output=True, text=True,
    ).stdout.strip()


def setup_stock():
    """Build receiving history so there are real product boxes to pack.

    Runs scripts/e2e_workflow_setup.sql, which is deliberately a separate file:
    it is setup, not assertion, and keeping it out of here makes clear which part
    of this script is the thing being tested.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "e2e_workflow_setup.sql")
    with open(path) as fh:
        return sql(fh.read())


def main():
    global ANON
    ANON = anon_key()

    with httpx.Client(timeout=40.0) as client:
        ops = login(client, "boopathi@r360.local")
        guard = login(client, "guard@r360.local")
        matcher = login(client, "match1@r360.local")
        packer = login(client, "pack1@r360.local")
        packer_b = login(client, "pack2@r360.local")

        packer_badge = badge_of("EMP-P01")
        matcher_badge = badge_of("EMP-M01")

        invoice_number = setup_stock()
        if not invoice_number:
            print("\nSetup failed — could not build receiving history.")
            raise SystemExit(2)

        print(f"\nUsing invoice {invoice_number}")

        print("\n1. Assignment by scanning a colleague's badge")
        r = client.post(
            f"{API}/invoices/assign",
            headers=matcher,
            json={"invoice_number": invoice_number, "badge_code": packer_badge},
        )
        ok("unverified invoice cannot be assigned", r.status_code == 409, r.text[:200])

        r = client.post(
            f"{API}/invoices/verify",
            headers=matcher,
            json={"invoice_number": invoice_number, "badge_code": matcher_badge},
        )
        ok("matcher verifies the invoice", r.status_code == 200, r.text[:250])

        # Refused by CONTROL POINT 5's self-assignment check: Admin does both
        # matching and packing, so the role check alone would not catch this —
        # the same person cannot be both the verifier and the assignee.
        r = client.post(
            f"{API}/invoices/assign",
            headers=matcher,
            json={"invoice_number": invoice_number, "badge_code": matcher_badge},
        )
        ok("the matcher's own badge cannot be assigned the pack",
           r.status_code in (403, 409), r.text[:200])

        r = client.post(
            f"{API}/invoices/assign",
            headers=matcher,
            json={"invoice_number": invoice_number, "badge_code": packer_badge},
        )
        ok("assigning to a packer works", r.status_code == 200, r.text[:250])
        if r.status_code != 200:
            raise SystemExit(1)
        assigned = r.json()
        invoice_id = assigned["packing"]["invoice_id"]
        required = assigned["packing"]["required_units"]
        ok("the assignee is named back", assigned["assigned_to"]["full_name"] == "Kavitha S",
           str(assigned["assigned_to"]))
        ok("no badge code is echoed", "BDG-" not in r.text)

        r = client.get(f"{API}/packing/assigned-to-me", headers=packer)
        ok("it appears in the packer's own queue",
           r.status_code == 200
           and any(i["invoice_number"] == invoice_number for i in r.json()),
           r.text[:200])

        r = client.get(f"{API}/packing/assigned-to-me", headers=packer_b)
        ok("and not in another packer's queue",
           r.status_code == 200
           and all(i["invoice_number"] != invoice_number for i in r.json()),
           r.text[:200])

        print("\n2. Scanning product boxes into the carton")
        codes = sql(
            f"""
            select string_agg(s.code, ',')
              from stickers s
              join purchase_order_lines pol on pol.id = s.purchase_order_line_id
              join invoices i on i.purchase_order_line_id = pol.id
              join scan_events se on se.sticker_id = s.id
                   and se.scan_type = 'unit_verify' and se.accepted
             where i.invoice_number = '{invoice_number}'
               and not exists (
                     select 1 from scan_events p
                      where p.sticker_id = s.id and p.scan_type = 'pack_unit' and p.accepted
                   )
            """
        ).split(",")
        codes = [c for c in codes if c][:required]
        ok(f"found {required} received product boxes to pack", len(codes) == required,
           f"got {len(codes)}")

        r = client.post(
            f"{API}/invoices/pack",
            headers=packer,
            json={"invoice_number": invoice_number, "badge_code": packer_badge},
        )
        ok("carton cannot close before the boxes are scanned in", r.status_code == 409,
           r.text[:220])

        for n, code in enumerate(codes, start=1):
            r = scan(client, f"{API}/invoices/{invoice_id}/pack-scan", packer, code)
            body = r.json()
            ok(f"product box {n}/{required} scanned in",
               r.status_code == 200 and body.get("accepted") is True, r.text[:200])

        r = scan(client, f"{API}/invoices/{invoice_id}/pack-scan", packer, codes[0])
        ok("the same box cannot be scanned twice", r.json().get("reject_reason") == "already_scanned",
           r.text[:200])

        r = client.get(f"{API}/invoices/{invoice_id}/packing", headers=packer)
        state = r.json()
        ok("the carton reports itself ready", state["ready_to_close"] is True, str(state))
        ok("counts line up", state["packed_units"] == state["required_units"], str(state))

        r = client.post(
            f"{API}/invoices/pack",
            headers=packer,
            json={"invoice_number": invoice_number, "badge_code": packer_badge},
        )
        ok("now the carton closes", r.status_code == 200, r.text[:250])

        print("\n3. The guard's carton count, and Ops's decision")
        r = client.post(f"{API}/batches", headers=ops, json={"invoice_ids": [invoice_id]})
        ok("Ops creates a batch", r.status_code == 201, r.text[:250])
        batch_id = r.json()["batch_id"]

        r = scan(client, f"{API}/batches/{batch_id}/scan", ops, invoice_number)
        ok("carton out-scanned", r.json().get("accepted") is True, r.text[:200])
        r = client.post(f"{API}/batches/{batch_id}/complete", headers=ops)
        ok("batch completes (CP6)", r.status_code == 200, r.text[:250])

        r = client.post(f"{API}/batches/{batch_id}/release", headers=ops)
        ok("release is refused before a count exists", r.status_code == 409, r.text[:220])

        r = client.get(f"{API}/loading/awaiting-count", headers=guard)
        ok("the batch shows up as needing a count",
           r.status_code == 200 and any(b["batch_id"] == batch_id for b in r.json()),
           r.text[:200])

        r = client.post(
            f"{API}/loading/batches/{batch_id}/count", headers=guard,
            json={"counted_cartons": 1},
        )
        ok("guard files the count", r.status_code == 200, r.text[:250])
        ok("expected count is filled in by the database", r.json()["expected_cartons"] == 1,
           r.text[:200])
        ok("and it matches", r.json()["matches"] is True, r.text[:200])

        r = client.post(f"{API}/batches/{batch_id}/release", headers=ops)
        ok("release still refused while undecided", r.status_code == 409, r.text[:220])

        r = client.post(
            f"{API}/loading/batches/{batch_id}/decision", headers=guard,
            json={"approve": True},
        )
        ok("the guard cannot approve their own count", r.status_code in (403, 409), r.text[:220])

        r = client.get(f"{API}/loading/pending", headers=ops)
        ok("it is in the Ops queue",
           r.status_code == 200 and any(a["batch_id"] == batch_id for a in r.json()),
           r.text[:200])

        r = client.post(
            f"{API}/loading/batches/{batch_id}/decision", headers=ops,
            json={"approve": True},
        )
        ok("Ops approves the count", r.status_code == 200, r.text[:250])

        r = client.post(f"{API}/batches/{batch_id}/release", headers=ops)
        ok("now the batch releases", r.status_code == 200, r.text[:250])

        print("\n4. Exit approval before the gate opens")
        r = client.post(
            f"{API}/pickups", headers=guard,
            json={
                "batch_id": batch_id,
                "vehicle_number": "KA09XX1234",
                "persons": [{
                    "full_name": "Exit Test Driver", "mobile": "9812345678",
                    "visitor_role": "driver",
                }],
            },
        )
        ok("guard registers the collecting vehicle", r.status_code == 201, r.text[:250])
        pickup_id = r.json()["pickup_id"]

        r = scan(client, f"{API}/pickups/{pickup_id}/scan", guard, invoice_number)
        ok("carton loaded onto the vehicle", r.json().get("accepted") is True, r.text[:200])

        r = client.post(f"{API}/pickups/{pickup_id}/verify", headers=guard)
        ok("CP7 passes", r.status_code == 200 and r.json()["verified"] is True, r.text[:250])

        r = client.post(f"{API}/pickups/{pickup_id}/release", headers=guard)
        ok("verified is no longer enough to leave", r.status_code == 409, r.text[:220])

        r = client.post(f"{API}/pickups/{pickup_id}/request-exit", headers=guard)
        ok("guard requests exit", r.status_code == 200, r.text[:250])
        ok("status becomes exit_pending", r.json()["pickup"]["status"] == "exit_pending",
           r.text[:200])

        r = client.post(f"{API}/pickups/{pickup_id}/release", headers=guard)
        ok("gate stays shut without an approval", r.status_code == 409, r.text[:220])

        r = client.post(
            f"{API}/pickups/{pickup_id}/exit-decision", headers=guard,
            json={"approve": True},
        )
        ok("the guard cannot approve their own request", r.status_code == 403, r.text[:220])

        r = client.get(f"{API}/pickups/awaiting-exit", headers=ops)
        ok("it is in the Ops exit queue",
           r.status_code == 200 and any(p["pickup_id"] == pickup_id for p in r.json()),
           r.text[:200])

        r = client.post(
            f"{API}/pickups/{pickup_id}/exit-decision", headers=ops,
            json={"approve": False},
        )
        ok("holding without a reason is refused", r.status_code == 422, r.text[:220])

        r = client.post(
            f"{API}/pickups/{pickup_id}/exit-decision", headers=ops,
            json={"approve": False, "note": "Seal number not recorded"},
        )
        ok("Ops can hold the vehicle", r.status_code == 200, r.text[:250])
        ok("it goes back to verified so the guard can re-request",
           r.json()["pickup"]["status"] == "verified", r.text[:200])

        client.post(f"{API}/pickups/{pickup_id}/request-exit", headers=guard)
        r = client.post(
            f"{API}/pickups/{pickup_id}/exit-decision", headers=ops,
            json={"approve": True},
        )
        ok("Ops approves the exit", r.status_code == 200, r.text[:250])

        r = client.post(f"{API}/pickups/{pickup_id}/release", headers=guard)
        ok("the guard opens the gate", r.status_code == 200, r.text[:250])
        ok("time out is stamped", r.json()["time_out"] is not None, r.text[:200])

        print("\n5. Reconciliation")
        entry = sql(
            """
            select gate_entry_id::text from v_sticker_reconciliation
             where packed_into_cartons > 0 order by packed_into_cartons desc limit 1
            """
        )
        ok("the reconciliation view has data", bool(entry), entry)

    print(f"\n{checks['pass']} passed, {checks['fail']} failed")
    sys.exit(1 if checks["fail"] else 0)


main()
