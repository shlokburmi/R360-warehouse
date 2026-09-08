"""Every endpoint, against every role — is the role gate what the UI assumes?

    python scripts/e2e_role_access.py [api_base_url]

The frontend decides which *pages* a role sees from `/me`'s `nav_pages`, and
which buttons appear from role checks inside each page. Neither of those is the
authority: `require_roles` is, and RLS behind it. When the two disagree the
result is a button that is visible, tappable, and answers 403 — which on a
warehouse floor is indistinguishable from a broken app.

So this asks the API directly, for all seven roles: it sends every route a
request shaped like the real one but pointed at a nonexistent id, and records
whether the answer was "not your role" or anything else. A 404/409/422 counts as
allowed — the role got past the guard and the request then failed on its own
merits, which is all this is measuring. Nothing is created, so it is idempotent
and safe to run against a live stack.

The expectations below are the *intended* access matrix. A FAIL means the code
and this table disagree; one of the two is wrong and both are worth reading.
"""

import os
import subprocess
import sys
import uuid

import httpx

API = (
    sys.argv[1]
    if len(sys.argv) > 1
    else os.environ.get("API_BASE_URL", "http://127.0.0.1:8000/api/v1")
)
SUPA = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54321")
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every seeded account, with the password from supabase/seed.sql. Two roles have
# two accounts each; one of each is enough for an access check.
ACCOUNTS = {
    "security_guard": ("guard@r360.local", "Guard@2026!"),
    "ops_manager": ("boopathi@r360.local", "OpsMgr@2026!"),
    "offloading": ("offload@r360.local", "Offload@2026!"),
    "warehouse_staff": ("store@r360.local", "Store@2026!"),
    "invoice_matcher": ("match1@r360.local", "Match1@2026!"),
    "packer": ("pack1@r360.local", "Pack1@2026!"),
    "admin": ("admin@r360.local", "Adm!n#2026$Xk9Qz"),
}

ALL = set(ACCOUNTS)
GUARD = {"security_guard", "admin"}
OPS = {"ops_manager", "admin"}
ADMIN = {"admin"}
PACKER = {"packer", "admin"}
MATCHER = {"packer", "invoice_matcher", "admin"}
STORE = {"warehouse_staff", "admin"}
OFFLOAD = {"offloading", "admin"}

NIL = str(uuid.UUID(int=0))

# (method, path, body, roles that must get past the role guard)
ROUTES = [
    # ---- session and master data: everyone signed in
    ("GET", "/me", None, ALL),
    ("GET", "/vendors", None, ALL),
    ("GET", "/purchase-orders", None, ALL),
    ("GET", f"/purchase-orders/{NIL}/lines", None, ALL),
    ("GET", "/notifications", None, ALL),
    ("POST", f"/notifications/{NIL}/read", None, ALL),
    ("GET", "/badges/mine", None, ALL),
    ("GET", "/dashboard", None, ALL),

    # ---- master data writes: Ops Manager (+admin)
    ("POST", "/purchase-orders", {"po_number": "PO-9999-9999", "vendor_id": NIL, "lines": []}, OPS),
    ("POST", f"/purchase-orders/{NIL}/lines",
     {"sku": "X", "description": "X", "expected_units": 1, "units_per_box": 1}, OPS),
    ("PATCH", f"/purchase-order-lines/{NIL}", {"sku": "X"}, OPS),

    # ---- gate: the guard's own screens
    ("GET", "/gate/visitors/lookup?mobile=9999999999", None, GUARD),
    ("POST", "/gate/vendors/propose", {"name": "Nobody"}, GUARD),
    ("POST", "/gate/entries",
     {"vehicle_number": "KA01AB0001", "vendor_id": NIL, "persons": []}, GUARD),
    ("POST", f"/gate/entries/{NIL}/admit", None, GUARD),
    ("POST", f"/gate/entries/{NIL}/box-count", {"box_count": 1}, GUARD),
    ("POST", "/uploads/identity-photo", {"mobile": "9999999999"}, GUARD),

    # ---- gate: shared reads
    ("GET", "/gate/entries", None, ALL),
    ("GET", f"/gate/entries/{NIL}", None, ALL),
    ("GET", f"/gate/entries/{NIL}/box-progress", None, ALL),

    # ---- gate: the Ops decision (CP1) and PO linking
    ("GET", "/gate/entries/pending", None, OPS),
    ("POST", f"/gate/entries/{NIL}/decision", {"approve": True}, OPS),
    ("POST", f"/gate/entries/{NIL}/cancel", {"reason": "access probe"}, OPS),
    ("POST", f"/gate/entries/{NIL}/link-po", {"purchase_order_id": NIL}, OPS),
    ("GET", "/uploads/identity-photo/view?path=nope", None, OPS),

    # ---- intake: sticker sheets are Ops, scanning is the Packer
    ("POST", f"/entries/{NIL}/stickers/box", {}, OPS),
    ("POST", f"/entries/{NIL}/stickers/unit", None, OPS),
    ("POST", f"/sticker-sheets/{NIL}/void", {"reason": "access probe"}, OPS),
    ("GET", f"/entries/{NIL}/sticker-sheets", None, ALL),
    ("GET", f"/sticker-sheets/{NIL}", None, ALL),
    ("POST", f"/entries/{NIL}/scan/box",
     {"client_event_id": NIL, "raw_code": "BOX-0000", "scanned_at": "2026-01-01T00:00:00Z"}, PACKER),
    ("POST", f"/entries/{NIL}/scan/unit",
     {"client_event_id": NIL, "raw_code": "UNT-0000", "scanned_at": "2026-01-01T00:00:00Z"}, PACKER),
    ("POST", f"/gate/entries/{NIL}/verify-boxes", None, PACKER),
    ("POST", f"/boxes/{NIL}/damage-check", {"damage_level": "none"}, PACKER),
    ("POST", f"/boxes/{NIL}/close", None, PACKER),
    ("POST", f"/entries/{NIL}/finish-offloading", None, PACKER),
    ("POST", "/uploads/damage-photo", {"box_id": NIL}, PACKER),
    ("GET", f"/entries/{NIL}/boxes", None, ALL),
    ("GET", f"/entries/{NIL}/unit-progress", None, ALL),
    ("POST", "/scan/sync?scan_type=box_verify&entry_id=" + NIL, {"scans": []}, ALL),

    # ---- exceptions: anyone raises, Ops decides
    ("GET", "/exceptions", None, ALL),
    ("POST", "/exceptions",
     {"exception_type": "other", "title": "access probe", "gate_entry_id": NIL}, ALL),
    ("GET", f"/exceptions/{NIL}", None, ALL),
    ("POST", f"/exceptions/{NIL}/resolve", {"resolution": "accept", "note": "probe"}, OPS),
    ("POST", f"/exceptions/{NIL}/escalate", {"email_superadmin": False}, OPS),

    # ---- reconciliation (CP4): the inbound team
    ("GET", f"/entries/{NIL}/reconciliation", None, ALL),
    ("POST", f"/entries/{NIL}/reconciliation", {"lines": []}, OFFLOAD),

    # ---- putaway and stock: warehouse staff
    ("GET", "/putaway/queue", None, ALL),
    ("GET", "/locations", None, ALL),
    ("GET", "/locations/resolve?code=A-01-01-01-01", None, ALL),
    ("GET", f"/boxes/{NIL}/putaway", None, ALL),
    ("GET", f"/boxes/{NIL}/putaway/history", None, ALL),
    ("GET", "/stock", None, ALL),
    ("POST", f"/boxes/{NIL}/putaway",
     {"location_code": "A-01-01-01-01", "units": 1, "disposition": "stock"}, STORE),

    # ---- matching and packing
    ("POST", "/badges/resolve", {"badge_code": "BDG-NOPE"}, MATCHER),
    ("POST", "/invoices/from-order-no",
     {"order_no": "CP000000000_0000", "raw_text": "x", "confidence": 1.0,
      "was_corrected": False, "source": "camera"}, MATCHER),
    ("GET", "/invoices/lookup?order_no=CP000000000_0000", None, MATCHER),
    ("POST", "/invoices/assign", {"invoice_number": "INV-NOPE", "badge_code": "BDG-NOPE"}, MATCHER),
    ("GET", "/invoices", None, ALL),
    ("GET", f"/invoices/{NIL}/packing", None, ALL),
    ("POST", "/invoices/pack", {"invoice_number": "INV-NOPE", "badge_code": "BDG-NOPE"}, PACKER),
    ("GET", "/packing/assigned-to-me", None, PACKER),
    ("GET", "/packing/ready", None, ALL),

    # ---- out-scan and release: Ops
    ("GET", "/batches", None, ALL),
    ("GET", f"/batches/{NIL}", None, ALL),
    ("POST", "/batches", {"invoice_ids": []}, OPS),
    ("POST", f"/batches/{NIL}/scan",
     {"client_event_id": NIL, "raw_code": "INV-NOPE", "scanned_at": "2026-01-01T00:00:00Z"}, OPS),
    ("POST", f"/batches/{NIL}/complete", None, OPS),
    ("POST", f"/batches/{NIL}/release", None, OPS),

    # ---- the guard's carton count, the Ops decision on it
    ("GET", "/loading/awaiting-count", None, ALL),
    ("GET", "/loading/pending", None, ALL),
    ("GET", f"/loading/batches/{NIL}", None, ALL),
    ("POST", f"/loading/batches/{NIL}/count", {"counted_cartons": 1}, GUARD),
    ("POST", f"/loading/batches/{NIL}/decision", {"approve": True}, OPS),

    # ---- pickup and gate exit (CP7)
    ("GET", "/pickups", None, ALL),
    ("GET", "/pickups/awaiting", None, ALL),
    ("GET", "/pickups/awaiting-exit", None, ALL),
    ("GET", f"/pickups/{NIL}", None, ALL),
    ("POST", "/pickups",
     {"batch_id": NIL, "vehicle_number": "KA01AB0001", "persons": []}, GUARD),
    ("POST", f"/pickups/{NIL}/scan",
     {"client_event_id": NIL, "raw_code": "INV-NOPE", "scanned_at": "2026-01-01T00:00:00Z"}, GUARD),
    ("POST", f"/pickups/{NIL}/verify", None, GUARD),
    ("POST", f"/pickups/{NIL}/request-exit", None, GUARD),
    ("POST", f"/pickups/{NIL}/release", None, GUARD),
    ("POST", f"/pickups/{NIL}/cancel", {"reason": "access probe"}, GUARD),
    ("POST", f"/pickups/{NIL}/exit-decision", {"approve": True}, OPS),

    # ---- reports
    ("GET", "/reports/vendor-accuracy", None, OPS),
    ("GET", "/reports/exception-log", None, OPS),
    ("GET", "/reports/gate-register", None, OPS),
    ("GET", "/reports/outbound-register", None, OPS),
    ("GET", "/reports/daily-activity", None, OPS),
    ("GET", "/reports/operator-productivity", None, OPS),
    ("GET", "/reports/audit-trail", None, OPS),
    ("GET", "/reports/packer-productivity", None, OPS),

    # ---- the Staff screen
    ("GET", "/admin/meta", None, OPS),
    ("GET", "/admin/staff", None, OPS),
    ("POST", "/admin/staff",
     {"full_name": "Access Probe", "employee_code": "EMP-ZZ9", "role": "packer",
      "mobile": "9999999999"}, OPS),
    ("PATCH", f"/admin/staff/{NIL}", {"is_active": True}, OPS),
    ("POST", f"/admin/staff/{NIL}/badge", None, OPS),
    ("POST", f"/admin/staff/{NIL}/badge/revoke", None, OPS),
    ("DELETE", f"/admin/staff/{NIL}", None, OPS),
    ("POST", f"/admin/staff/{NIL}/reset-password", {"new_password": "Nope@2026!"}, ADMIN),
    ("GET", f"/admin/staff/{NIL}/history", None, ADMIN),
    ("GET", "/admin/retention", None, ADMIN),
]

checks = {"pass": 0, "fail": 0}
failures = []


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


def main():
    anon = anon_key()

    with httpx.Client(timeout=40.0) as client:
        tokens = {}
        for role, (email, password) in ACCOUNTS.items():
            r = client.post(
                f"{SUPA}/auth/v1/token?grant_type=password",
                headers={"apikey": anon, "Content-Type": "application/json"},
                json={"email": email, "password": password},
            )
            if r.status_code != 200:
                raise SystemExit(f"Could not sign in as {email}: {r.status_code} {r.text[:200]}")
            tokens[role] = {"Authorization": f"Bearer {r.json()['access_token']}"}

        print(f"Signed in as all {len(tokens)} roles\n")
        print(f"{'route':60} " + " ".join(f"{r[:5]:>5}" for r in ACCOUNTS))

        for method, path, body, allowed in ROUTES:
            cells = []
            for role in ACCOUNTS:
                r = client.request(
                    method, f"{API}{path}", headers=tokens[role],
                    json=body if body is not None else None,
                )
                refused = r.status_code == 403 and "wrong_role" in r.text
                expected_allowed = role in allowed
                good = expected_allowed != refused
                checks["pass" if good else "fail"] += 1
                if not good:
                    failures.append(
                        f"{method} {path} as {role}: "
                        + ("refused but should be allowed" if expected_allowed
                           else f"allowed but should be refused (got {r.status_code})")
                        + f" <- {r.text[:120]}"
                    )
                cells.append(("ok " if good else "BAD") + ("·" if refused else "✓"))
            print(f"{method[:4]:4} {path[:55]:55} " + " ".join(f"{c:>5}" for c in cells))

    print(f"\n{checks['pass']} passed, {checks['fail']} failed")
    if failures:
        print("\nDisagreements between the code and the intended access matrix:")
        for f in failures:
            print("  -", f)
    print("\nLegend: ✓ = got past the role guard, · = refused as wrong_role")
    sys.exit(1 if checks["fail"] else 0)


main()
