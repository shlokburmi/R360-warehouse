-- 0032_admin_hard_delete_staff.sql
-- Admin: permanently remove a staff account (as opposed to Deactivate, which
-- only flips is_active).
--
-- profiles carries the same blanket "no deletions" trigger and revoked DELETE
-- grant as every other business table (0003, 0005) — PRD §7's audit trail is
-- built on that. A staff account is not a business record in that sense: an
-- Admin removing a mistaken, duplicate, or test account is a legitimate
-- operation, and Deactivate does not remove the login.
--
-- This only reopens the door on `profiles` itself. Every other table's FK to
-- profiles(id) is untouched and still blocks the delete (mostly NOT NULL,
-- since it names who performed a physical action) — so an account with any
-- real activity still cannot be hard-deleted; the API turns that FK failure
-- into "deactivate instead." RLS policy `profiles_admin_all` (0005) already
-- restricts this to admins.

drop trigger if exists trg_profiles_no_delete on profiles;
grant delete on profiles to authenticated;
