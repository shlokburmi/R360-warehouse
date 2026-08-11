-- 0006_storage.sql
-- PRD §8 Data Privacy / DPDP Act 2023.
--
-- Both buckets are private. Nothing is ever served by public URL; the API mints
-- short-lived signed URLs and only for roles allowed to view.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values
  ('identity-photos', 'identity-photos', false, 5 * 1024 * 1024,
   array['image/jpeg', 'image/png', 'image/webp']),
  ('damage-photos', 'damage-photos', false, 10 * 1024 * 1024,
   array['image/jpeg', 'image/png', 'image/webp'])
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- identity-photos
--
-- A guard can PUT a photo and can never GET one — not even the one they just
-- uploaded. This is the write-only-drop-box pattern, and it is deliberate: the
-- gate needs to capture identity documents, but a guard has no operational
-- reason to browse the identity records of every visitor who has ever arrived.
-- ---------------------------------------------------------------------------

create policy identity_upload on storage.objects
  for insert to authenticated
  with check (
    bucket_id = 'identity-photos'
    and has_role('security_guard', 'ops_manager', 'admin')
  );

create policy identity_read on storage.objects
  for select to authenticated
  using (bucket_id = 'identity-photos' and is_ops());

-- Retention is handled by a scheduled job, not by users. No update, no delete
-- policy is created here, so both are denied.

-- ---------------------------------------------------------------------------
-- damage-photos
-- Evidence for vendor claims. Visible to the whole operation; a damage photo is
-- not personal data and hiding it slows down the dispute.
-- ---------------------------------------------------------------------------

create policy damage_upload on storage.objects
  for insert to authenticated
  with check (
    bucket_id = 'damage-photos'
    and has_role('offloading', 'security_guard', 'ops_manager', 'admin')
  );

create policy damage_read on storage.objects
  for select to authenticated
  using (bucket_id = 'damage-photos');
