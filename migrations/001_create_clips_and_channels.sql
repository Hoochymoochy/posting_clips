-- Migration: 001_create_clips_and_channels
-- Creates the clips table and a channels table for per-platform post status.

-- Platforms we can post to
create type public.platform_type as enum ('youtube', 'instagram', 'tiktok');

-- Outcome of a post attempt on a given platform
create type public.post_status as enum ('pending', 'success', 'failed');

-- ---------------------------------------------------------------------------
-- clips: one row per rendered short
-- ---------------------------------------------------------------------------
create table public.clips (
  id            uuid primary key default gen_random_uuid(),
  youtube_url   text not null,                 -- normalized source URL (same video_id => same value)
  youtube_video_id text,                       -- extracted YouTube id for duplicate checks
  title         text,                          -- overlay / display title
  start_time    text,                          -- clip start e.g. 00:29:03
  end_time      text,                          -- clip end e.g. 00:29:33
  storage_url   text,                          -- link to rendered clip in storage bucket (or local path until uploaded)
  posted        boolean not null default false, -- true once posted to at least one platform
  -- null / past / now => post ASAP; future timestamp => wait until that time
  scheduled_at  timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

comment on table public.clips is 'Rendered vertical shorts ready for (or already sent to) social platforms.';
comment on column public.clips.youtube_url is 'Normalized source YouTube URL the clip was cut from.';
comment on column public.clips.youtube_video_id is 'YouTube video id used to detect duplicate source URLs.';
comment on column public.clips.storage_url is 'Public/signed URL in the storage bucket, or local relative path until uploaded.';
comment on column public.clips.posted is 'Overall flag: true if the clip has been posted to any channel.';
comment on column public.clips.scheduled_at is
  'When to post. NULL or <= now() = post immediately; a future timestamptz = schedule for that time.';

create index clips_posted_idx on public.clips (posted);
create index clips_created_at_idx on public.clips (created_at desc);
create index clips_youtube_video_id_idx on public.clips (youtube_video_id);
create index clips_youtube_url_idx on public.clips (youtube_url);

-- Scheduler poll: unposted clips that are due (immediate or scheduled time reached)
create index clips_due_for_post_idx
  on public.clips (scheduled_at nulls first)
  where posted = false;

-- Keep updated_at in sync
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create trigger clips_set_updated_at
  before update on public.clips
  for each row
  execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- channels: one row per platform attempt for a clip
-- Lets us see e.g. YouTube succeeded but Instagram/TikTok failed.
-- ---------------------------------------------------------------------------
create table public.channels (
  id            uuid primary key default gen_random_uuid(),
  clip_id       uuid not null references public.clips (id) on delete cascade,
  platform      public.platform_type not null,
  status        public.post_status not null default 'pending',
  post_url      text,                          -- live URL on that platform after success
  error_message text,                          -- failure reason when status = 'failed'
  posted_at     timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),

  constraint channels_clip_platform_unique unique (clip_id, platform)
);

comment on table public.channels is 'Per-platform post status for a clip (YouTube, Instagram, TikTok).';
comment on column public.channels.status is 'pending | success | failed — use this to spot upload failures.';
comment on column public.channels.post_url is 'URL of the published short on that platform.';

create index channels_clip_id_idx on public.channels (clip_id);
create index channels_status_idx on public.channels (status);
create index channels_platform_idx on public.channels (platform);

create trigger channels_set_updated_at
  before update on public.channels
  for each row
  execute function public.set_updated_at();
