-- Migration: 007_hook_archetypes
-- Store which caption hook archetype was used, clip duration for confound control,
-- and denormalize YouTube average_watch_percent onto channels for selector queries.

alter table public.clips
  add column if not exists hook_archetype text,
  add column if not exists duration_seconds numeric;

comment on column public.clips.hook_archetype is
  'Caption hook style selected at discovery time (e.g. question, curiosity_gap).';
comment on column public.clips.duration_seconds is
  'Rendered short length in seconds (set at register time).';

alter table public.discovery_candidates
  add column if not exists hook_archetype text;

comment on column public.discovery_candidates.hook_archetype is
  'Hook archetype chosen when the caption was generated; reused on regen and passed to clips.';

alter table public.channels
  add column if not exists average_watch_percent numeric;

comment on column public.channels.average_watch_percent is
  'Latest average % of the video watched (YouTube Analytics). Null until metrics mature.';

create index if not exists clips_hook_archetype_idx
  on public.clips (hook_archetype)
  where hook_archetype is not null;
