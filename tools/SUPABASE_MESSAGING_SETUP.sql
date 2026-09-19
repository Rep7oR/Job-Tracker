-- JobSync Messaging (chat between registered users)
-- Run this once in the Supabase SQL Editor for your project.
-- Same trust model as SUPABASE_PRESENCE_SETUP.sql: JobSync does not use
-- Supabase Auth, so every request uses the one shared publishable key and
-- rows are not restricted per signed-in user by Postgres itself. Anyone who
-- has the app's publishable key can technically read any row in this table.
-- Do not store anything here you would not want visible to any JobSync
-- installation that shares this Supabase project.

create table if not exists public.jobsync_messages (
    id bigint generated always as identity primary key,
    thread_id text not null,
    sender_email text not null,
    sender_name text not null default 'User',
    recipient_email text not null,
    body text not null default '',
    attachment_url text not null default '',
    attachment_name text not null default '',
    attachment_type text not null default '',
    created_at timestamptz not null default now()
);

alter table public.jobsync_messages enable row level security;

drop policy if exists "jobsync_messages_select" on public.jobsync_messages;
drop policy if exists "jobsync_messages_insert" on public.jobsync_messages;

create policy "jobsync_messages_select"
on public.jobsync_messages
for select
to anon, authenticated
using (true);

create policy "jobsync_messages_insert"
on public.jobsync_messages
for insert
to anon, authenticated
with check (true);

create index if not exists jobsync_messages_thread_idx
on public.jobsync_messages (thread_id, created_at);

create index if not exists jobsync_messages_recipient_idx
on public.jobsync_messages (recipient_email, created_at);

-- Storage bucket for shared PDF/JPG/PNG attachments. Public read so a
-- recipient's browser can load the file directly from the returned URL;
-- uploads are allowed with the publishable key, same trust model as above.
insert into storage.buckets (id, name, public)
values ('jobsync-attachments', 'jobsync-attachments', true)
on conflict (id) do nothing;

drop policy if exists "jobsync_attachments_read" on storage.objects;
drop policy if exists "jobsync_attachments_write" on storage.objects;

create policy "jobsync_attachments_read"
on storage.objects
for select
to anon, authenticated
using (bucket_id = 'jobsync-attachments');

create policy "jobsync_attachments_write"
on storage.objects
for insert
to anon, authenticated
with check (bucket_id = 'jobsync-attachments');
