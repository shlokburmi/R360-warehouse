-- 0041_packer_can_attach_damage_photos.sql
--
-- Restores `packer` to damage_photos_insert.
--
-- Symptom: a packer answering the mandatory damage question with anything
-- other than "No damage" got "You are not allowed to do this" on Record
-- damage check, having already taken the photo the same screen tells them is
-- required. Everything else in that request was permitted — the boxes update,
-- the exception, the Ops notification — so the one refused write was the
-- evidence itself.
--
-- ===========================================================================
-- WHY THIS IS NOT A REVERT OF ANYTHING IN THIS REPO
-- ===========================================================================
--
-- The policy in production read
--
--     has_role('offloading', 'ops_manager', 'admin') and uploaded_by = auth.uid()
--
-- which is not what any migration here creates: 0005_rls.sql wrote
-- `has_role('packer', 'admin')` and nothing since has touched it. So that
-- version was applied to production by hand and never captured as a
-- migration, and the drift only surfaced when someone actually walked the
-- damage path on the floor. This migration is the repo catching up and
-- becoming the source of truth again, which is why it states the whole role
-- list explicitly rather than editing one name.
--
-- The list keeps every role production currently grants and adds `packer`
-- back. Dropping offloading/ops_manager would be the same unrecorded
-- decision in the other direction — whoever widened it had a reason that is
-- not written down, and a packer being blocked is not evidence that an
-- offloader should be.
--
-- `packer` belongs here on the process: DECISIONS.md §5 makes the damage
-- answer a step in unit scanning, WORKFLOW.md step 7 assigns unit scanning to
-- the packer, and §CG6 records that move from offloading. The photo is not a
-- separate privilege from the answer — the answer is refused without it
-- (record_damage_check raises `photo_required`), so a role that may record a
-- damage check and may not attach its photo cannot complete the step at all.
--
-- `uploaded_by = auth.uid()` is deliberately kept: attribution of the
-- evidence stays with whoever actually took it.

drop policy if exists damage_photos_insert on damage_photos;

create policy damage_photos_insert on damage_photos
  for insert to authenticated
  with check (
    has_role('packer', 'offloading', 'ops_manager', 'admin')
    and uploaded_by = auth.uid()
  );
