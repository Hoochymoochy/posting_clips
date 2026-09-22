-- Migration: 004_create_discovery_runs
-- Tracks discovery pipeline runs with a daily quota (pending/success count).

create table public.discovery_runs (
  id          uuid primary key default gen_random_uuid(),
  youtube_url text not null,
  status      text not null default 'pending'
              check (status in ('pending', 'success', 'failed')),
  created_at  timestamptz default now(),
  updated_at  timestamptz default now()
);

comment on table public.discovery_runs is
  'Discovery pipeline runs; daily cap counts pending + success only.';

create index idx_discovery_runs_created_at on public.discovery_runs (created_at);
create index idx_discovery_runs_status on public.discovery_runs (status);
