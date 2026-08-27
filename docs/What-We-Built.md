% Reward360 Warehouse Management App
% What's Been Built — A Plain-Language Summary
% 27 August 2026

# Overview

This app manages every item that moves through the Reward360 warehouse — from
the moment a truck arrives at the gate, to the moment packed goods leave for
the customer. Nothing moves without being counted. Everyone entering or
leaving is registered by name. Every box and every product carries a scannable
sticker. Every packed order is linked to the specific person who packed it,
with a timestamp.

The app is built so that important steps **cannot be skipped or faked** — the
system itself blocks the next step until the current one is done correctly.
This is not just a suggestion on a screen; it is enforced by the database
underneath the app, so there is no way to bypass it by clicking around.

---

# The Roles, and What Each One Can Do

There are seven types of user accounts. Each person only sees the screens
relevant to their job — a Security Guard cannot see the Admin's reports, and a
Packer cannot approve a truck at the gate.

## 1. Security Guard

- Registers every person arriving at the gate: name, mobile number, vehicle
  number, vendor/transporter name, and a photo of their ID (only required the
  first time someone visits; after that the app remembers them)
- Cannot let a truck in — has to send the request to the Ops Manager and wait
  for approval
- Counts the boxes physically on the truck and enters that number
- Verifies goods leaving at pickup, and registers who is collecting them
  (same name/mobile/vehicle details as at entry)
- Records the time the vehicle came in and the time it left

## 2. Ops Manager

- Approves or rejects every truck at the gate — no truck enters without this
- Generates the exact number of box stickers and unit stickers needed,
  based on the purchase order
- Scans packed cartons out before they're released for pickup
- Approves the carton count before a truck can leave with an order
- Approves the final gate-exit request before the truck can physically leave
- Can view and act on exceptions/problems (see "Exceptions" below) and view
  every report

## 3. Offloading Team

- Receives goods off the truck
- Matches what the warehouse counted against what the separate inbound system
  recorded — if the two numbers disagree, nothing moves forward until it's
  sorted out

## 4. Warehouse Staff

- Moves received goods from the receiving area to their assigned storage
  rack
- The app tells them exactly which rack a product is meant to go to, and
  won't let damaged goods be placed in a normal stock rack (or good stock be
  placed in the damaged-goods area)

## 5. Invoice Matching

- Takes a printed invoice and finds the matching product
- **Scans every individual unit sticker** on the product to confirm the
  product physically matches the invoice (this is a genuine physical check —
  not just typing in a number)
- Only once every unit is scanned can she scan her own ID badge to confirm
  the match is complete and hand the product to a Packer
- The app records exactly who confirmed which invoice, and when

## 6. Packing

- Receives the product and invoice from Invoice Matching
- Scans each individual box into the correct carton
- Scans her own ID badge to seal the record — this permanently links this
  specific invoice, this specific carton, and this specific person together
- **Important rule**: the person who matched the invoice and the person who
  packed it must be two different people. The app will not allow the same
  person to do both — this prevents one person quietly skipping the check.

## 7. Admin

- Creates and manages staff accounts, and issues/replaces ID badges
- Reviews and resolves exceptions (problems/mismatches) that come up anywhere
  in the process, and can escalate serious ones by email to the superadmin
- Can view every report
- Is the only account that can see anyone's uploaded ID photo (kept private
  and secure, in line with data protection law)
- Can act as a stand-in for any of the other roles if someone is out — this
  is deliberate, so the warehouse doesn't grind to a halt if one person is on
  leave

---

# How Goods Move Through the Warehouse — Step by Step

1. **Gate Entry** — Guard registers everyone on the truck. Ops Manager must
   approve before the gate opens. No exceptions, no manual override.
2. **Box Counting** — Guard counts the boxes; the app checks this against
   the stickers Ops Manager issued. If the numbers don't match, the boxes
   don't move inside.
3. **Unit Stickers** — Every individual product gets a sticker and is
   scanned in. If the scanned count doesn't match what the purchase order
   says should be in that box, those goods are held and a problem is logged.
4. **Empty boxes** are moved to an outside storage rack (a physical step,
   nothing for the app to check here).
5. **Inbound Reconciliation** — The separate inbound-tracking team's numbers
   are matched against what the warehouse recorded. Both must agree before
   goods can be shelved.
6. **Putaway** — Warehouse Staff move goods to their assigned racks.
7. **Invoice Matching** — the product is physically matched to its invoice,
   confirmed by scanning every unit, then the badge scan.
8. **Packing** — the packer scans every box into a carton and confirms with
   her own badge, permanently linking her to that order.
9. **Out-Scan & Release** — Ops Manager scans every finished carton before
   it's released for pickup. A batch cannot be released with even one carton
   missing.
10. **Pickup & Gate Exit** — Guard verifies every released carton is
    physically present, registers who is collecting them, and the truck can
    only leave once the Ops Manager approves the exit.

---

# Built-in Safety Checks (Nothing Can Be Faked)

These are the seven points in the process where the app **physically stops**
the wrong thing from happening — they are not just warning messages that can
be clicked past:

1. A truck cannot enter without a named approval from the Ops Manager.
2. Boxes cannot go inside unless the scanned count matches what was issued.
3. Products cannot enter stock unless the scanned unit count matches the
   purchase order.
4. Goods cannot be shelved until the inbound team's numbers agree with the
   warehouse's numbers.
5. A carton cannot be marked as packed unless both the invoice-matcher's
   scan and the packer's scan are recorded — and they must be two different
   people.
6. A batch of cartons cannot be released for pickup until every single
   carton has been scanned out.
7. A truck cannot leave with pickup goods unless the count verified at the
   gate matches the count that was released.

---

# Other Features

- **No deletions, ever.** If a mistake is made, the system records a
  correction on top of the original entry — the original is never erased.
  This means there is always a complete, honest history of what happened.
- **Full history/audit trail.** Every action anyone takes is recorded with
  who did it and exactly when.
- **Works offline.** If the wifi drops at the gate or on the warehouse
  floor, scans are saved on the device and automatically sent once the
  connection comes back — nothing is lost.
- **Mobile-friendly.** Built to be used on a phone or tablet, outdoors, by
  guards and floor staff.
- **Reports.** Five built-in reports: which vendors cause the most errors,
  how many orders each packer has completed (and how accurately), a log of
  every mismatch/exception, a full log of everyone who entered or left the
  gate, and a daily summary of everything processed.
- **Notifications.** The Ops Manager and Admin get emailed automatically
  whenever something needs their attention — a new truck waiting for
  approval, a count that doesn't match, an exception that's been raised.

---

# What's Not Finished Yet

Being upfront about what's still outstanding:

- **In-app alerts.** Right now, Ops Manager/Admin are notified by **email**
  when something needs attention, but there is no bell/notification icon
  inside the app itself showing these in real time. This is a planned next
  step, not yet built.
- **Visual polish.** The interface is fully functional but hasn't had a
  final design pass (consistent colors, spacing, styling across every
  screen) — that was deliberately left for later so the working parts could
  be prioritised first.

Everything else described above is built, and has been tested end-to-end —
including every role, every control point, and the complete gate-to-departure
process — using real, automated tests that simulate the actual app being
used, not just a read-through of the code.
