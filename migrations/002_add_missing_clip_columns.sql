-- Migration: 002_add_missing_clip_columns
-- Your live clips table was created without columns the app now writes.
-- Run this in the Supabase SQL Editor (Dashboard → SQL → New query).

alter table public.clips
  add column if not exists youtube_video_id text,
  add column if not exists title text,
  add column if not exists start_time text,
  add column if not exists end_time text;

comment on column public.clips.youtube_video_id is
  'YouTube video id used to detect duplicate source URLs.';
comment on column public.clips.title is
  'Overlay / display title for the short.';
comment on column public.clips.start_time is
  'Clip start timestamp e.g. 00:29:03';
comment on column public.clips.end_time is
  'Clip end timestamp e.g. 00:29:33';

create index if not exists clips_youtube_video_id_idx
  on public.clips (youtube_video_id);
