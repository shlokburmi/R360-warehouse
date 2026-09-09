"""The whole process, over real HTTP, each step as the role whose button it is.

    python scripts/e2e_full_flow.py [api_base_url]

A truck arrives and is registered, approved, admitted, counted, stickered,
scanned, closed and reconciled; then a carton is invoiced, assigned, packed,
batched, out-scanned, counted, released, loaded and driven out. One continuous
chain, six roles, the same sequence the screens walk an operator through.

Why this exists alongside the pytest suite: those tests drive Postgres directly,
so they prove the triggers and policies hold but not that the routes are wired,
the role guards match the screens, or that a refusal arrives as a sentence
somebody can read. DECISIONS.md Part D is a list of things that were only false
over HTTP.

`scripts/e2e_role_access.py` is the other half of this: it checks who is
*refused*, endpoint by endpoint. This one checks that the people who are allowed
can actually get all the way through.

Not idempotent — it consumes a seeded PO's stickers and closes an invoice. Run
`supabase db reset` before and after.
"""

import os
import random
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
DB_CONTAINER = os.environ.get("DB_CONTAINER", "supabase_db_r360-warehouse")

ACCOUNTS = {
    "guard": ("guard@r360.local", "Guard@2026!"),
    "ops": ("boopathi@r360.local", "OpsMgr@2026!"),
    "offload": ("offload@r360.local", "Offload@2026!"),
    "packer": ("pack1@r360.local", "Pack1@2026!"),
    "packer_b": ("pack2@r360.local", "Pack2@2026!"),
    "admin": ("admin@r360.local", "Adm!n#2026$Xk9Qz"),
}

checks = {"pass": 0, "fail": 0}
first_failure = None


def ok(label, cond, extra=""):
    global first_failure
    checks["pass" if cond else "fail"] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"\n         {extra}" if extra and not cond else ""))
    if not cond and first_failure is None:
        first_failure = label


def sql(query):
    return subprocess.run(
        ["docker", "exec", "-i", DB_CONTAINER, "psql", "-U", "postgres", "-d", "postgres",
         "-Xqt", "-A", "-c", query],
        capture_output=True, text=True,
    ).stdout.strip()


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


def badge_of(employee_code):
    """A badge code, read straight from Postgres.

    No route returns one (DECISIONS.md §CC2), so a script standing in for a
    physical card has to go round the outside — which is itself a check that the
    invariant still holds.
    """
    return sql(f"select badge_code from profiles where employee_code = '{employee_code}'")


def scan(client, url, headers, code, disposition=None):
    body = {
        "client_event_id": str(uuid.uuid4()),
        "raw_code": code,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "was_offline": False,
        "device_label": "e2e",
    }
    if disposition:
        body["disposition"] = disposition
    return client.post(url, headers=headers, json=body)


def id_photo(client, headers, mobile):
    """Do what the guard's phone does: get a signed URL, PUT the photo, keep the path.

    A first-time visitor cannot be registered without one (PRD §5.1), so this
    also exercises the storage half of that step rather than working around it.
    """
    r = client.post(f"{API}/uploads/identity-photo", headers=headers,
                    json={"mobile": mobile})
    if r.status_code != 200:
        return None, r
    ticket = r.json()
    # A 1x1 JPEG is enough: nothing downstream reads the pixels, and a real
    # photo would only make this script slower to run.
    jpeg = bytes.fromhex(
        "ffd8ffe000104a46494600010100000100010000ffdb004300ff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
        "ffffffffffffffffffffffffffffffffffffffffffffffc20011"
        "0800010001010100ffc40014000100000000000000000000000000000009"
        "ffda0008010100013f10"
    )
    up = client.put(ticket["upload_url"], content=jpeg,
                    headers={"Content-Type": "image/jpeg"})
    return (ticket["path"] if up.status_code < 400 else None), up


def main():
    anon = anon_key()

    with httpx.Client(timeout=60.0) as client:
        who = {}
        for name, (email, password) in ACCOUNTS.items():
            r = client.post(
                f"{SUPA}/auth/v1/token?grant_type=password",
                headers={"apikey": anon, "Content-Type": "application/json"},
                json={"email": email, "password": password},
            )
            if r.status_code != 200:
                raise SystemExit(f"Could not sign in as {email}: {r.status_code} {r.text[:200]}")
            who[name] = {"Authorization": f"Bearer {r.json()['access_token']}"}
        print(f"Signed in as {', '.join(who)}\n")

        # ------------------------------------------------------------------
        print("1. Guard registers the truck (PRD §5.1)")
        r = client.get(f"{API}/vendors", headers=who["guard"])
        ok("vendor list loads", r.status_code == 200, r.text[:200])
        # Without include_pending, /vendors returns confirmed vendors only, so
        # the first is fine — a guard-proposed, still-unconfirmed one cannot
        # appear here and be picked by accident.
        vendor = r.json()[0]

        r = client.get(
            f"{API}/purchase-orders", headers=who["guard"],
            params={"vendor_id": vendor["id"]},
        )
        ok("their open POs load", r.status_code == 200 and len(r.json()) > 0, r.text[:200])
        po = r.json()[0]

        mobile = f"9{random.randint(100000000, 999999999)}"
        r = client.get(f"{API}/gate/visitors/lookup", headers=who["guard"],
                       params={"mobile": mobile})
        ok("visitor lookup answers for an unknown mobile",
           r.status_code == 200 and r.json()["found"] is False, r.text[:200])
        ok("and says a photo is required for a first-time visitor",
           r.json()["photo_required"] is True, r.text[:200])

        r = client.post(
            f"{API}/gate/entries", headers=who["guard"],
            json={
                "vehicle_number": f"KA01AB{random.randint(1000, 9999)}",
                "vendor_id": vendor["id"],
                "persons": [{
                    "full_name": "E2E Driver", "mobile": mobile, "visitor_role": "driver",
                }],
            },
        )
        ok("a first-time visitor cannot be registered without a photo",
           r.status_code == 422 and "photo" in r.text, r.text[:250])

        photo_path, up = id_photo(client, who["guard"], mobile)
        ok("the identity photo uploads to storage", photo_path is not None,
           getattr(up, "text", "")[:250])

        vehicle = f"KA01AB{random.randint(1000, 9999)}"
        r = client.post(
            f"{API}/gate/entries", headers=who["guard"],
            json={
                "vehicle_number": vehicle,
                "vendor_id": vendor["id"],
                "purchase_order_id": po["id"],
                "transporter_name": "E2E Transport",
                "persons": [{
                    "full_name": "E2E Driver", "mobile": mobile, "visitor_role": "driver",
                    "id_photo_path": photo_path,
                }],
            },
        )
        ok("entry is created and waiting on Ops", r.status_code == 201, r.text[:300])
        if r.status_code != 201:
            raise SystemExit(1)
        entry = r.json()
        entry_id = entry["id"]
        ok("gate is locked until decided", entry["status"] == "pending_approval", str(entry["status"]))

        # ------------------------------------------------------------------
        print("\n2. Ops decides it (CONTROL POINT 1)")
        r = client.get(f"{API}/gate/entries/pending", headers=who["ops"])
        ok("it is in the Ops queue",
           r.status_code == 200 and any(e["id"] == entry_id for e in r.json()), r.text[:200])

        r = client.post(f"{API}/gate/entries/{entry_id}/admit", headers=who["guard"])
        ok("an undecided truck cannot be admitted", r.status_code in (403, 409), r.text[:250])

        r = client.post(
            f"{API}/gate/entries/{entry_id}/decision", headers=who["ops"],
            json={"approve": True, "note": "e2e"},
        )
        ok("Ops approves", r.status_code == 200 and r.json()["status"] == "approved", r.text[:300])

        print("\n3. Guard opens the gate")
        r = client.post(f"{API}/gate/entries/{entry_id}/admit", headers=who["guard"])
        ok("vehicle is inside and time_in stamped",
           r.status_code == 200 and r.json()["status"] == "inside"
           and r.json()["time_in"] is not None, r.text[:300])

        # ------------------------------------------------------------------
        print("\n4. Guard declares the box count (PRD §5.2 step 1)")
        po_boxes = int(sql(
            f"""select coalesce(sum(ceil(expected_units::numeric / units_per_box)), 0)::int
                  from purchase_order_lines where purchase_order_id = '{po["id"]}'"""
        ))
        r = client.post(
            f"{API}/gate/entries/{entry_id}/box-count", headers=who["guard"],
            json={"box_count": po_boxes},
        )
        ok(f"{po_boxes} boxes declared",
           r.status_code == 200 and r.json()["declared_box_count"] == po_boxes, r.text[:300])

        # ------------------------------------------------------------------
        print("\n5. Ops issues exactly that many stickers (step 2)")
        r = client.post(f"{API}/entries/{entry_id}/stickers/box", headers=who["ops"], json={})
        ok("sheet is issued", r.status_code == 201 and r.json()["issued"] is True, r.text[:300])
        ok("one sticker per declared box", r.json()["sheet"]["quantity"] == po_boxes,
           str(r.json()["sheet"]["quantity"]))

        r = client.get(f"{API}/entries/{entry_id}/sticker-sheets", headers=who["ops"])
        ok("the sheet is listed back", r.status_code == 200 and len(r.json()) == 1, r.text[:200])

        # ------------------------------------------------------------------
        print("\n6. Packer scans every box sticker (CONTROL POINT 2)")
        box_codes = [c for c in sql(
            f"""select string_agg(code, ',' order by sequence_no) from stickers
                 where gate_entry_id = '{entry_id}' and sticker_type = 'box'"""
        ).split(",") if c]
        ok(f"{po_boxes} box stickers exist", len(box_codes) == po_boxes, str(len(box_codes)))

        r = client.post(f"{API}/gate/entries/{entry_id}/verify-boxes", headers=who["packer"])
        ok("CP2 refuses before the boxes are scanned",
           r.status_code == 409 and r.json()["verified"] is False, r.text[:250])

        for n, code in enumerate(box_codes, start=1):
            r = scan(client, f"{API}/entries/{entry_id}/scan/box", who["packer"], code)
            ok(f"box {n}/{po_boxes} scanned",
               r.status_code == 200 and r.json()["accepted"] is True, r.text[:200])

        r = scan(client, f"{API}/entries/{entry_id}/scan/box", who["packer"], box_codes[0])
        ok("a re-scan is rejected, not double-counted",
           r.json().get("reject_reason") == "already_scanned", r.text[:200])

        r = client.get(f"{API}/gate/entries/{entry_id}/box-progress", headers=who["packer"])
        ok("progress reports complete", r.json()["complete"] is True, r.text[:200])

        r = client.post(f"{API}/gate/entries/{entry_id}/verify-boxes", headers=who["packer"])
        ok("CP2 passes and the boxes move inside",
           r.status_code == 200 and r.json()["verified"] is True
           and r.json()["entry"]["status"] == "box_verified", r.text[:300])

        # ------------------------------------------------------------------
        print("\n7. Ops issues unit stickers (step 3)")
        r = client.post(f"{API}/entries/{entry_id}/stickers/unit", headers=who["ops"])
        ok("unit sheet is issued", r.status_code == 201, r.text[:300])
        unit_total = r.json()["quantity"]
        ok("one per expected unit", unit_total > 0, str(unit_total))

        # ------------------------------------------------------------------
        print("\n8. Packer scans units into each box, closes each one (CONTROL POINT 3)")
        r = client.get(f"{API}/entries/{entry_id}/boxes", headers=who["packer"])
        ok("boxes list loads", r.status_code == 200, r.text[:200])
        boxes = r.json()

        for box in boxes:
            codes = [c for c in sql(
                f"""select string_agg(code, ',') from stickers
                     where box_id = '{box["id"]}' and sticker_type = 'unit'
                       and status <> 'void'"""
            ).split(",") if c]
            for code in codes:
                r = scan(client, f"{API}/entries/{entry_id}/scan/unit", who["packer"], code)
                if r.status_code != 200 or r.json().get("accepted") is not True:
                    ok(f"unit {code} into box {box['box_number']}", False, r.text[:250])
                    break
            else:
                ok(f"box {box['box_number']}: all {len(codes)} units scanned", True)

            r = client.post(
                f"{API}/boxes/{box['id']}/close", headers=who["packer"],
            )
            ok(f"box {box['box_number']} will not close without a damage answer",
               r.status_code == 422 and "damage" in r.text, r.text[:200])

            r = client.post(
                f"{API}/boxes/{box['id']}/damage-check", headers=who["packer"],
                json={"damage_level": "none", "note": None, "photo_paths": []},
            )
            ok(f"box {box['box_number']} damage check recorded", r.status_code == 200, r.text[:250])

            r = client.post(f"{API}/boxes/{box['id']}/close", headers=who["packer"])
            ok(f"box {box['box_number']} closes (CP3 passed)",
               r.status_code == 200 and r.json()["closed"] is True, r.text[:250])

        r = client.post(f"{API}/entries/{entry_id}/finish-offloading", headers=who["packer"])
        ok("offloading completes", r.status_code == 200 and r.json()["status"] == "offloaded",
           r.text[:300])

        # ------------------------------------------------------------------
        print("\n9. Offloading team's own count (CONTROL POINT 4)")
        r = client.get(f"{API}/entries/{entry_id}/reconciliation", headers=who["offload"])
        ok("the comparison loads", r.status_code == 200, r.text[:250])
        recon = r.json()

        wrong = [{"purchase_order_line_id": line["purchase_order_line_id"],
                  "inbound_count": line["warehouse_count"] + 1} for line in recon["lines"]]
        r = client.post(f"{API}/entries/{entry_id}/reconciliation", headers=who["offload"],
                        json={"lines": wrong})
        ok("a disagreement holds the entry open and raises an exception",
           r.status_code == 409 and r.json().get("exception_code"), r.text[:300])

        right = [{"purchase_order_line_id": line["purchase_order_line_id"],
                  "inbound_count": line["warehouse_count"]} for line in recon["lines"]]
        r = client.post(f"{API}/entries/{entry_id}/reconciliation", headers=who["offload"],
                        json={"lines": right})
        ok("agreeing counts pass CP4",
           r.status_code == 200 and r.json()["all_matched"] is True, r.text[:300])

        # ------------------------------------------------------------------
        print("\n10. Packer scans the physical invoice (PRD §5.4, 0035/0036)")
        order_no = f"CP{random.randint(100000000, 999999999)}_{random.randint(1000, 9999)}"
        r = client.post(
            f"{API}/invoices/from-order-no", headers=who["packer"],
            json={"order_no": order_no, "raw_text": f"Order No {order_no}",
                  "confidence": 0.97, "was_corrected": False, "source": "ocr"},
        )
        ok("the invoice is created from the Order No", r.status_code == 201, r.text[:300])
        invoice = r.json()
        invoice_number = invoice["invoice_number"]
        invoice_id = invoice["invoice_id"]

        r = client.get(f"{API}/invoices/lookup", headers=who["packer"],
                       params={"order_no": order_no})
        ok("and is found by the same Order No next time",
           r.status_code == 200 and r.json()["invoice_id"] == invoice_id, r.text[:250])

        r = client.post(
            f"{API}/invoices/from-order-no", headers=who["packer"],
            json={"order_no": order_no, "raw_text": "x", "confidence": 0.9,
                  "was_corrected": False, "source": "ocr"},
        )
        ok("the same Order No cannot create a second invoice", r.status_code == 409, r.text[:250])

        # ------------------------------------------------------------------
        print("\n11. Handover to a different packing lady (CONTROL POINT 5)")
        own_badge = badge_of("EMP-P01")
        other_badge = badge_of("EMP-P02")
        ok("both badges were read from the database", bool(own_badge and other_badge))

        r = client.post(
            f"{API}/invoices/assign", headers=who["packer"],
            json={"invoice_number": invoice_number, "badge_code": own_badge},
        )
        ok("she cannot assign the carton to herself", r.status_code == 409, r.text[:250])

        r = client.post(
            f"{API}/invoices/assign", headers=who["packer"],
            json={"invoice_number": invoice_number, "badge_code": other_badge},
        )
        ok("assigning to a second packer works", r.status_code == 200, r.text[:300])
        ok("no badge code is echoed back", "BDG-" not in r.text)

        r = client.get(f"{API}/packing/assigned-to-me", headers=who["packer_b"])
        ok("it lands in her queue",
           r.status_code == 200 and any(i["invoice_number"] == invoice_number for i in r.json()),
           r.text[:250])

        r = client.get(f"{API}/packing/assigned-to-me", headers=who["packer"])
        ok("and not in the assigner's",
           all(i["invoice_number"] != invoice_number for i in r.json()), r.text[:250])

        r = client.post(
            f"{API}/invoices/pack", headers=who["packer"],
            json={"invoice_number": invoice_number, "badge_code": own_badge},
        )
        ok("the assigner cannot also pack it (CP5)", r.status_code == 409, r.text[:250])

        r = client.post(
            f"{API}/invoices/pack", headers=who["packer_b"],
            json={"invoice_number": invoice_number, "badge_code": other_badge},
        )
        ok("the assignee packs it", r.status_code == 200, r.text[:300])

        # ------------------------------------------------------------------
        print("\n12. Ops batches and out-scans it (CONTROL POINT 6)")
        r = client.get(f"{API}/packing/ready", headers=who["ops"])
        ok("it is ready to batch",
           r.status_code == 200 and any(i["invoice_id"] == invoice_id for i in r.json()),
           r.text[:250])

        r = client.post(f"{API}/batches", headers=who["ops"], json={"invoice_ids": [invoice_id]})
        ok("batch created", r.status_code == 201, r.text[:300])
        batch = r.json()
        batch_id = batch["batch_id"]

        r = client.post(f"{API}/batches/{batch_id}/complete", headers=who["ops"])
        ok("CP6 refuses before the carton is out-scanned",
           r.status_code == 409 and r.json()["completed"] is False, r.text[:250])

        r = scan(client, f"{API}/batches/{batch_id}/scan", who["ops"], invoice_number)
        ok("carton out-scanned", r.status_code == 200 and r.json()["accepted"] is True, r.text[:250])

        r = client.post(f"{API}/batches/{batch_id}/complete", headers=who["ops"])
        ok("CP6 passes", r.status_code == 200 and r.json()["completed"] is True, r.text[:250])

        # ------------------------------------------------------------------
        print("\n13. Guard counts the bay, Ops decides (A1/A2)")
        r = client.post(f"{API}/batches/{batch_id}/release", headers=who["ops"])
        ok("release is refused before a count exists", r.status_code == 409, r.text[:250])

        r = client.get(f"{API}/loading/awaiting-count", headers=who["guard"])
        ok("the batch is waiting for a count",
           r.status_code == 200 and any(b["batch_id"] == batch_id for b in r.json()), r.text[:250])

        r = client.post(f"{API}/loading/batches/{batch_id}/count", headers=who["guard"],
                        json={"counted_cartons": 1})
        ok("the guard files their count", r.status_code == 200, r.text[:300])
        ok("the expected figure comes from the database", r.json()["expected_cartons"] == 1,
           r.text[:200])

        r = client.post(f"{API}/loading/batches/{batch_id}/decision", headers=who["guard"],
                        json={"approve": True})
        ok("the guard cannot approve their own count", r.status_code in (403, 409), r.text[:250])

        r = client.post(f"{API}/loading/batches/{batch_id}/decision", headers=who["ops"],
                        json={"approve": True})
        ok("Ops approves the count", r.status_code == 200, r.text[:300])

        r = client.post(f"{API}/batches/{batch_id}/release", headers=who["ops"])
        ok("now the batch releases", r.status_code == 200, r.text[:300])

        # ------------------------------------------------------------------
        print("\n14. Collection vehicle, load, exit (CONTROL POINT 7 + A4)")
        pickup_mobile = f"9{random.randint(100000000, 999999999)}"
        pickup_photo, up = id_photo(client, who["guard"], pickup_mobile)
        ok("the collector's identity photo uploads", pickup_photo is not None,
           getattr(up, "text", "")[:250])
        r = client.post(
            f"{API}/pickups", headers=who["guard"],
            json={
                "batch_id": batch_id,
                "vehicle_number": f"KA09XX{random.randint(1000, 9999)}",
                "courier_name": "E2E Courier",
                "persons": [{
                    "full_name": "E2E Collector", "mobile": pickup_mobile,
                    "visitor_role": "driver", "id_photo_path": pickup_photo,
                }],
            },
        )
        ok("guard registers the vehicle", r.status_code == 201, r.text[:300])
        pickup_id = r.json()["pickup_id"]

        r = client.post(f"{API}/pickups/{pickup_id}/verify", headers=who["guard"])
        ok("CP7 refuses with cartons still unloaded",
           r.status_code == 409 and r.json()["verified"] is False, r.text[:250])

        r = scan(client, f"{API}/pickups/{pickup_id}/scan", who["guard"], invoice_number)
        ok("carton loaded onto the vehicle",
           r.status_code == 200 and r.json()["accepted"] is True, r.text[:250])

        r = client.post(f"{API}/pickups/{pickup_id}/verify", headers=who["guard"])
        ok("CP7 passes", r.status_code == 200 and r.json()["verified"] is True, r.text[:250])

        r = client.post(f"{API}/pickups/{pickup_id}/release", headers=who["guard"])
        ok("verified alone does not open the gate", r.status_code == 409, r.text[:250])

        r = client.post(f"{API}/pickups/{pickup_id}/request-exit", headers=who["guard"])
        ok("guard requests exit", r.status_code == 200, r.text[:300])

        r = client.get(f"{API}/pickups/awaiting-exit", headers=who["ops"])
        ok("it reaches the Ops exit queue",
           r.status_code == 200 and any(p["pickup_id"] == pickup_id for p in r.json()),
           r.text[:250])

        r = client.post(f"{API}/pickups/{pickup_id}/exit-decision", headers=who["ops"],
                        json={"approve": True})
        ok("Ops approves the exit", r.status_code == 200, r.text[:300])

        r = client.post(f"{API}/pickups/{pickup_id}/release", headers=who["guard"])
        ok("the guard opens the gate and time_out is stamped",
           r.status_code == 200 and r.json()["time_out"] is not None, r.text[:300])

        # ------------------------------------------------------------------
        # Every remaining read a screen performs, against the rows this run just
        # produced. A response model that is stricter than its data answers 500,
        # not a wrong number — POST /pickups did exactly that for months because
        # `sku` stopped being collected in 0036 — and a 500 is invisible until
        # something asks for the real shape.
        print("\n15. Every screen's own reads, against real rows")
        r = client.get(f"{API}/gate/entries/{entry_id}", headers=who["ops"])
        ok("truck detail (Trucks, Box counting, Scan units)", r.status_code == 200, r.text[:200])

        r = client.get(f"{API}/entries/{entry_id}/sticker-sheets", headers=who["ops"])
        sheets = r.json()
        for sheet in sheets:
            r = client.get(f"{API}/sticker-sheets/{sheet['id']}", headers=who["ops"])
            ok(f"{sheet['sticker_type']} sticker sheet renders", r.status_code == 200,
               r.text[:200])

        r = client.get(f"{API}/entries/{entry_id}/unit-progress", headers=who["packer"])
        ok("unit progress (Scan units)", r.status_code == 200, r.text[:200])

        r = client.get(f"{API}/batches/{batch_id}", headers=who["ops"])
        ok("batch detail (Out-scan)", r.status_code == 200, r.text[:200])
        r = client.get(f"{API}/loading/batches/{batch_id}", headers=who["ops"])
        ok("carton-count approval detail", r.status_code == 200, r.text[:200])

        r = client.get(f"{API}/pickups/{pickup_id}", headers=who["guard"])
        ok("pickup detail (Pickup) — the response that used to answer 500",
           r.status_code == 200, r.text[:250])
        r = client.get(f"{API}/pickups", headers=who["guard"])
        ok("pickup list (Pickup)", r.status_code == 200, r.text[:200])

        r = client.get(f"{API}/invoices/{invoice_id}/packing", headers=who["packer_b"])
        ok("packing state (Packing)", r.status_code == 200, r.text[:200])
        r = client.get(f"{API}/invoices", headers=who["ops"])
        ok("invoice list", r.status_code == 200, r.text[:200])
        r = client.get(f"{API}/badges/mine", headers=who["packer"])
        ok("her own badge QR (About me)", r.status_code == 200, r.text[:200])
        r = client.get(f"{API}/admin/meta", headers=who["ops"])
        ok("role options (Staff)", r.status_code == 200, r.text[:200])

        r = client.get(f"{API}/notifications", headers=who["ops"])
        ok("notification list (the bell)", r.status_code == 200, r.text[:200])
        if r.json():
            note_id = r.json()[0]["id"]
            r = client.post(f"{API}/notifications/{note_id}/read", headers=who["ops"])
            ok("and one can be dismissed", r.status_code in (200, 204), r.text[:200])

        # ------------------------------------------------------------------
        print("\n16. What the oversight screens show afterwards")
        r = client.get(f"{API}/dashboard", headers=who["ops"])
        ok("the dashboard loads", r.status_code == 200, r.text[:200])

        for report in ("vendor-accuracy", "exception-log", "gate-register", "outbound-register",
                       "daily-activity", "operator-productivity", "audit-trail",
                       "packer-productivity"):
            r = client.get(f"{API}/reports/{report}", headers=who["ops"])
            ok(f"report {report}", r.status_code == 200, r.text[:200])

        r = client.get(f"{API}/exceptions", headers=who["ops"])
        ok("the reconciliation mismatch is on the exception list",
           r.status_code == 200 and any(e["exception_type"] == "inbound_mismatch"
                                        for e in r.json()), r.text[:250])

        mismatch = next(e for e in r.json()
                        if e["exception_type"] == "inbound_mismatch"
                        and e["entry_code"] == entry["entry_code"])

        r = client.post(f"{API}/exceptions/{mismatch['id']}/resolve", headers=who["ops"],
                        json={"resolution": "accept", "note": "close enough"})
        ok("a control point cannot be approved away (CP4)",
           r.status_code == 422 and "cannot_accept" in r.text, r.text[:250])

        r = client.post(f"{API}/exceptions/{mismatch['id']}/resolve", headers=who["ops"],
                        json={"resolution": "reject",
                              "note": "counted again after the goods were shelved"})
        ok("and a decision that can be recorded, is",
           r.status_code == 200 and r.json()["status"] == "resolved", r.text[:250])

        r = client.post(f"{API}/exceptions/{mismatch['id']}/resolve", headers=who["ops"],
                        json={"resolution": "reject", "note": "twice"})
        ok("the same exception cannot be decided twice", r.status_code == 409, r.text[:250])

        r = client.get(f"{API}/admin/staff", headers=who["ops"])
        ok("the staff screen loads", r.status_code == 200 and len(r.json()) > 5, r.text[:200])

        r = client.get(f"{API}/notifications", headers=who["ops"])
        ok("notifications loaded", r.status_code == 200, r.text[:200])

        r = client.get(f"{API}/me", headers=who["offload"])
        me = r.json()
        ok("/me hands the app its page lists",
           r.status_code == 200 and "reconciliation" in me["nav_pages"]
           and me["can_hold_badge"] is False, r.text[:250])
        r = client.get(f"{API}/me", headers=who["admin"])
        ok("and Admin can open every page while keeping a short nav",
           len(r.json()["allowed_pages"]) > len(r.json()["nav_pages"]), r.text[:250])

    print(f"\n{checks['pass']} passed, {checks['fail']} failed")
    if first_failure:
        print(f"First failure: {first_failure}")
    sys.exit(1 if checks["fail"] else 0)


main()
