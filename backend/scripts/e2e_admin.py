"""End-to-end HTTP walkthrough of the Admin screen, driven with real tokens.

    python scripts/e2e_admin.py [api_base_url]

Why this exists alongside tests/test_admin.py: those tests connect to Postgres
directly, so they prove the triggers and the service guards hold, but they
cannot prove that `require_admin` is actually wired onto the routes, or that a
provisioned account can genuinely sign in. Both of those are only true over
HTTP, and DECISIONS.md Part D is a list of things that were only false over
HTTP.

Not idempotent: it provisions EMP-P03 and then deactivates it, and it rotates
the badges of the accounts it touches. Run `supabase db reset` afterwards to get
the demo state back.
"""

import os
import sys

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


def ok(label, condition, extra=""):
    mark = "PASS" if condition else "FAIL"
    checks["pass" if condition else "fail"] += 1
    print(f"  [{mark}] {label}" + (f"  <- {extra}" if extra and not condition else ""))


def anon_key():
    """Read the anon key from the running stack rather than an env file.

    A stale key in .env produces a 401 from GoTrue that looks exactly like a
    wrong password, which is a needlessly confusing place to start debugging.
    """
    if os.environ.get("SUPABASE_ANON_KEY"):
        return os.environ["SUPABASE_ANON_KEY"]

    import subprocess

    out = subprocess.run(
        ["supabase", "status", "-o", "env"],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(PROJECT_DIR),
    ).stdout
    for line in out.splitlines():
        if line.startswith("ANON_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit(
        "Could not read the anon key. Is the stack running? `supabase start`"
    )


def login(client, email, password="Warehouse@123"):
    r = client.post(
        f"{SUPA}/auth/v1/token?grant_type=password",
        headers={"apikey": ANON, "Content-Type": "application/json"},
        json={"email": email, "password": password},
    )
    r.raise_for_status()
    return r.json()["access_token"]


def main():
    global ANON
    ANON = anon_key()

    with httpx.Client(timeout=30.0) as client:
        admin = {"Authorization": f"Bearer {login(client, 'admin@r360.local')}"}
        guard = {"Authorization": f"Bearer {login(client, 'guard@r360.local')}"}

        print("\n1. Access control")
        r = client.get(f"{API}/admin/staff", headers=guard)
        ok("Guard is refused the staff list", r.status_code == 403, r.text[:200])

        r = client.get(f"{API}/admin/staff", headers=admin)
        ok("Admin can read the staff list", r.status_code == 200, r.text[:300])
        staff = r.json()
        ok("every account is listed", len(staff) >= 11, f"{len(staff)}")
        ok(
            "no badge code appears anywhere in the payload",
            "BDG-" not in r.text and "badge_code" not in r.text,
        )

        packer = next(s for s in staff if s["role"] == "packer")
        r = client.post(f"{API}/admin/staff/{packer['id']}/badge", headers=guard)
        ok("Guard cannot issue a badge", r.status_code == 403, r.text[:200])

        print("\n2. Provisioning")
        r = client.post(
            f"{API}/admin/staff",
            headers=admin,
            json={
                "email": "pack3@r360.local",
                "full_name": "Meena Krishnan",
                "role": "packer",
                "employee_code": "EMP-P03",
                "mobile": "9876500012",
            },
        )
        ok("Admin creates a packer", r.status_code == 201, r.text[:400])
        if r.status_code != 201:
            return
        created = r.json()
        new_id = created["staff"]["id"]
        temp_password = created["temporary_password"]
        ok("the profile came back with the right role", created["staff"]["role"] == "packer")
        ok("the employee code is set", created["staff"]["employee_code"] == "EMP-P03")
        ok("a temporary password is returned", len(temp_password) >= 12)
        ok("the new packer has no badge yet", created["staff"]["has_badge"] is False)

        ok(
            "the new account can actually sign in",
            bool(login(client, "pack3@r360.local", temp_password)),
        )

        r = client.post(
            f"{API}/admin/staff",
            headers=admin,
            json={
                "email": "someoneelse@r360.local",
                "full_name": "Clashing Code",
                "role": "packer",
                "employee_code": "EMP-P03",
            },
        )
        ok("a duplicate employee code is refused", r.status_code == 409, r.text[:200])

        r = client.post(
            f"{API}/admin/staff",
            headers=admin,
            json={
                "email": "pack3@r360.local",
                "full_name": "Second Account",
                "role": "packer",
                "employee_code": "EMP-P04",
            },
        )
        ok("a duplicate email is refused", r.status_code == 409, r.text[:200])

        r = client.post(
            f"{API}/admin/staff",
            headers=admin,
            json={
                "email": "bad@r360.local",
                "full_name": "Bad Mobile",
                "role": "packer",
                "employee_code": "EMP-P05",
                "mobile": "123",
            },
        )
        ok("a malformed mobile is refused before the account exists", r.status_code == 422)

        print("\n3. Badges")
        r = client.post(f"{API}/admin/staff/{new_id}/badge", headers=admin)
        ok("Admin issues the badge", r.status_code == 200, r.text[:300])
        first_code = r.json()["badge_code"]
        ok("the code looks like a badge token", first_code.startswith("BDG-"))
        ok("the profile now shows a usable badge", r.json()["staff"]["badge_usable"] is True)

        r = client.post(f"{API}/admin/staff/{new_id}/badge", headers=admin)
        second_code = r.json()["badge_code"]
        ok("reissuing produces a different code", second_code != first_code)

        r = client.get(f"{API}/admin/staff", headers=admin)
        ok("the list still never returns a code", first_code not in r.text and second_code not in r.text)

        guard_row = next(s for s in staff if s["role"] == "security_guard")
        r = client.post(f"{API}/admin/staff/{guard_row['id']}/badge", headers=admin)
        ok("a guard cannot be given a badge", r.status_code == 409, r.text[:200])

        r = client.post(f"{API}/admin/staff/{new_id}/badge/revoke", headers=admin)
        ok("Admin revokes the badge", r.status_code == 200, r.text[:200])
        ok("the badge is no longer usable", r.json()["badge_usable"] is False)
        ok("the record is superseded, not erased", r.json()["has_badge"] is True)

        print("\n4. Role and access changes")
        r = client.patch(
            f"{API}/admin/staff/{new_id}", headers=admin, json={"role": "admin"}
        )
        ok("role change works", r.status_code == 200 and r.json()["role"] == "admin", r.text[:200])

        r = client.patch(
            f"{API}/admin/staff/{new_id}", headers=admin, json={"is_backup_approver": True}
        )
        ok("an Admin can be a backup approver", r.status_code == 200, r.text[:200])

        r = client.patch(
            f"{API}/admin/staff/{new_id}",
            headers=admin,
            json={"role": "packer", "is_backup_approver": True},
        )
        ok("a packer cannot be a backup approver", r.status_code == 422, r.text[:200])

        me = client.get(f"{API}/me", headers=admin).json()
        r = client.patch(
            f"{API}/admin/staff/{me['id']}", headers=admin, json={"is_active": False}
        )
        ok("Admin cannot deactivate themselves", r.status_code == 409, r.text[:200])
        r = client.patch(f"{API}/admin/staff/{me['id']}", headers=admin, json={"role": "packer"})
        ok("Admin cannot demote themselves", r.status_code == 409, r.text[:200])

        r = client.patch(
            f"{API}/admin/staff/{new_id}", headers=admin, json={"is_active": False}
        )
        ok("deactivating someone else works", r.status_code == 200, r.text[:200])
        r = client.post(f"{API}/admin/staff/{new_id}/badge", headers=admin)
        ok("a deactivated account cannot be issued a badge", r.status_code == 409, r.text[:200])

        print("\n5. The account can no longer be used")
        token = login(client, "pack3@r360.local", temp_password)
        r = client.get(f"{API}/me", headers={"Authorization": f"Bearer {token}"})
        ok("a deactivated user is refused by the API", r.status_code == 403, r.text[:200])

        print("\n6. Audit trail")
        r = client.get(f"{API}/admin/staff/{new_id}/history", headers=admin)
        ok("the history is readable", r.status_code == 200, r.text[:200])
        events = r.json()
        keys = [k for e in events for k in (e["changed_keys"] or [])]
        ok("the badge reissue is recorded", keys.count("badge_code") >= 2, str(keys))
        ok("no code leaked into the history", "BDG-" not in r.text)
        ok(
            "the Admin is named as the actor",
            any(e["actor_name"] == "Warehouse Admin" for e in events),
            str(events[:2]),
        )

        print("\n7. Photo retention posture")
        r = client.get(f"{API}/admin/retention", headers=guard)
        ok("Guard is refused the retention report", r.status_code == 403, r.text[:200])

        r = client.get(f"{API}/admin/retention", headers=admin)
        ok("Admin can read it", r.status_code == 200, r.text[:300])
        posture = r.json()
        ok(
            "it reports the configured window",
            posture["retention_days"] == 180,
            str(posture),
        )
        ok("it says whether the job can run", "enabled" in posture, str(posture))
        ok(
            "nothing is overdue on a fresh database",
            posture["overdue"] == 0,
            str(posture),
        )

    print(f"\n{checks['pass']} passed, {checks['fail']} failed")
    sys.exit(1 if checks["fail"] else 0)


main()
