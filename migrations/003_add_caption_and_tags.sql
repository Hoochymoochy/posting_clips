-- Migration: 003_add_caption_and_tags
-- Store post caption/tags in Supabase (source of truth; no metadata.json needed).

alter table public.clips
  add column if not exists caption text,
  add column if not exists tags text[];

comment on column public.clips.caption is
  'Social caption / description for Instagram, TikTok, YouTube.';
comment on column public.clips.tags is
  'Hashtag keywords without # (e.g. {DJ,EDM,BoilerRoom}).';
