-- JobSync shared Users Online + role/presence metadata
-- Run this entire script in Supabase SQL Editor.
-- JobSync uses its existing local login system, so requests arrive as the
-- Supabase anon role. The publishable key is public by design; RLS controls access.

create table if not exists public.user_presence (
    presence_id text primary key,
    display_name text not null,
    avatar_seed text not null,
    email text not null default '',
    role text not null default 'member',
    last_seen timestamptz not null
);

alter table public.user_presence add column if not exists email text not null default '';
alter table public.user_presence add column if not exists role text not null default 'member';

create index if not exists user_presence_last_seen_idx
    on public.user_presence (last_seen);

create index if not exists user_presence_email_idx
    on public.user_presence (email);

alter table public.user_presence enable row level security;

grant select, insert, update, delete on public.user_presence to anon, authenticated;

drop policy if exists "JobSync presence read" on public.user_presence;
drop policy if exists "JobSync presence insert" on public.user_presence;
drop policy if exists "JobSync presence update" on public.user_presence;
drop policy if exists "JobSync presence delete" on public.user_presence;

create policy "JobSync presence read"
    on public.user_presence for select
    to anon, authenticated
    using (true);

create policy "JobSync presence insert"
    on public.user_presence for insert
    to anon, authenticated
    with check (true);

create policy "JobSync presence update"
    on public.user_presence for update
    to anon, authenticated
    using (true)
    with check (true);

create policy "JobSync presence delete"
    on public.user_presence for delete
    to anon, authenticated
    using (true);
