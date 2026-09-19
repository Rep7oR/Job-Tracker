-- JobSync Online Presence
-- Run this once in Supabase SQL Editor for project bldrwjsgrpbyiaowkpqs.
-- This table contains presence metadata only: no passwords, emails, CVs, jobs, or profile data.

create table if not exists public.jobsync_presence (
    presence_id text primary key,
    display_name text not null default 'User',
    avatar_seed text not null default 'User',
    last_seen timestamptz not null default now()
);

-- v1.8: adds the account email so "Online now" can be clicked to start a
-- direct message. Safe to re-run on an existing table.
alter table public.jobsync_presence add column if not exists contact_email text not null default '';

alter table public.jobsync_presence enable row level security;

drop policy if exists "jobsync_presence_select" on public.jobsync_presence;
drop policy if exists "jobsync_presence_insert" on public.jobsync_presence;
drop policy if exists "jobsync_presence_update" on public.jobsync_presence;

create policy "jobsync_presence_select"
on public.jobsync_presence
for select
to anon, authenticated
using (true);

create policy "jobsync_presence_insert"
on public.jobsync_presence
for insert
to anon, authenticated
with check (true);

create policy "jobsync_presence_update"
on public.jobsync_presence
for update
to anon, authenticated
using (true)
with check (true);

create index if not exists jobsync_presence_last_seen_idx
on public.jobsync_presence (last_seen);
