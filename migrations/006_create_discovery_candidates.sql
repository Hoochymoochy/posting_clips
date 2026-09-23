-- Migration: 006_create_discovery_candidates
-- Preview/review queue for discovery clips (cheap preview → mobile review → full render).

-- Allow claiming a run while it is being processed.
alter table public.discovery_runs
  drop constraint if exists discovery_runs_status_check;

alter table public.discovery_runs
  add constraint discovery_runs_status_check
  check (status in ('pending', 'processing', 'success', 'failed'));

create table public.discovery_candidates (
  id               uuid primary key default gen_random_uuid(),
  discovery_run_id uuid references public.discovery_runs(id) on delete set null,
  youtube_url      text not null,
  start_time       text,
  end_time         text,
  title            text,
  caption          text,
  source_comments  jsonb default '[]'::jsonb,
  preview_path     text,
  preview_url      text,
  segment_path     text,
  status           text not null default 'previewing'
                   check (status in (
                     'previewing',
                     'awaiting_review',
                     'approved',
                     'rejected_clip',
                     'rejected_caption',
                     'rendering',
                     'queued',
                     'failed'
                   )),
  decline_reason   text,
  clip_id          uuid,
  scheduled_at     timestamptz,
  error_message    text,
  created_at       timestamptz default now(),
  updated_at       timestamptz default now()
);

comment on table public.discovery_candidates is
  'Discovery preview candidates awaiting mobile swipe review before full render/queue.';

create index idx_discovery_candidates_status on public.discovery_candidates (status);
create index idx_discovery_candidates_run_id on public.discovery_candidates (discovery_run_id);
create index idx_discovery_candidates_created_at on public.discovery_candidates (created_at);
