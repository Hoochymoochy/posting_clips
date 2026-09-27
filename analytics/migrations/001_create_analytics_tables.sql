-- Migration: 001_create_analytics_tables.sql
-- Run this script in the Supabase SQL Editor (Dashboard -> SQL Editor -> New Query)
-- to enable social analytics tracking across YouTube, Instagram, and TikTok.

-- 1. Create clip_analytics time-series snapshots table
create table if not exists public.clip_analytics (
  id uuid primary key default gen_random_uuid(),
  clip_id uuid not null references public.clips(id) on delete cascade,
  channel_id uuid references public.channels(id) on delete cascade,
  platform text not null, -- 'youtube', 'instagram', 'tiktok'
  post_url text,
  views bigint not null default 0,
  likes bigint not null default 0,
  comments bigint not null default 0,
  shares bigint not null default 0,
  saves bigint not null default 0,
  watch_time_seconds numeric default 0,
  raw_metrics jsonb default '{}'::jsonb,
  error text,
  captured_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

comment on table public.clip_analytics is 'Time-series performance snapshots of published clips across social platforms.';

-- 2. Indexes for efficient dashboard lookups & historical trends
create index if not exists clip_analytics_clip_id_idx on public.clip_analytics(clip_id);
create index if not exists clip_analytics_channel_id_idx on public.clip_analytics(channel_id);
create index if not exists clip_analytics_platform_idx on public.clip_analytics(platform);
create index if not exists clip_analytics_captured_at_idx on public.clip_analytics(captured_at desc);

-- 3. Add latest snapshot metrics to channels table for direct O(1) access
alter table public.channels
  add column if not exists views bigint default 0,
  add column if not exists likes bigint default 0,
  add column if not exists comments bigint default 0,
  add column if not exists shares bigint default 0,
  add column if not exists saves bigint default 0,
  add column if not exists last_analytics_at timestamptz;

comment on column public.channels.views is 'Latest recorded view or play count on this platform.';
comment on column public.channels.likes is 'Latest recorded like count on this platform.';
comment on column public.channels.comments is 'Latest recorded comment count on this platform.';
comment on column public.channels.shares is 'Latest recorded share count on this platform.';
comment on column public.channels.saves is 'Latest recorded bookmark or save count on this platform.';
comment on column public.channels.last_analytics_at is 'Timestamp of the latest successful analytics capture.';

-- 4. Add total performance stats to clips table
alter table public.clips
  add column if not exists total_views bigint default 0,
  add column if not exists total_likes bigint default 0,
  add column if not exists total_comments bigint default 0,
  add column if not exists total_shares bigint default 0,
  add column if not exists last_analytics_at timestamptz;

-- 5. Create a view for latest analytics per channel
create or replace view public.latest_channel_analytics as
select distinct on (channel_id)
  id,
  clip_id,
  channel_id,
  platform,
  post_url,
  views,
  likes,
  comments,
  shares,
  saves,
  watch_time_seconds,
  raw_metrics,
  captured_at
from public.clip_analytics
where channel_id is not null
order by channel_id, captured_at desc;

-- 6. Create an overview view joining clips and channels
create or replace view public.clips_with_analytics as
select
  c.id,
  c.title,
  c.youtube_url,
  c.created_at,
  c.posted,
  coalesce(c.total_views, 0) as total_views,
  coalesce(c.total_likes, 0) as total_likes,
  coalesce(c.total_comments, 0) as total_comments,
  coalesce(c.total_shares, 0) as total_shares,
  c.last_analytics_at,
  (
    select json_agg(
      json_build_object(
        'id', ch.id,
        'platform', ch.platform,
        'status', ch.status,
        'post_url', ch.post_url,
        'views', coalesce(ch.views, 0),
        'likes', coalesce(ch.likes, 0),
        'comments', coalesce(ch.comments, 0),
        'shares', coalesce(ch.shares, 0),
        'saves', coalesce(ch.saves, 0),
        'last_analytics_at', ch.last_analytics_at
      )
    )
    from public.channels ch
    where ch.clip_id = c.id
  ) as channels
from public.clips c;
