-- Migration: 010_create_templates
-- Postgres source of truth for Discover/editor template metadata.
-- Intro bumper binaries stay in Storage (editor-projects/templates/{id}/intro.mp4)
-- with a local disk cache under templates/library/{id}/.

create table if not exists public.templates (
  id                text primary key,
  name              text not null default 'Untitled template',
  project_id        text,
  owner_id          text,
  active            boolean not null default true,
  caption_prompt    text,
  has_bumper        boolean not null default false,
  intro_bytes       bigint not null default 0,
  intro_storage_path text,
  -- Lightweight editor snapshot so Edit can recover after a wiped project folder.
  layout            jsonb,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

comment on table public.templates is
  'Discover intro templates: name, binding, caption prompt, bumper flags. Media lives in Storage.';
comment on column public.templates.project_id is
  'Bound editor project id (projects/{id}) used when opening Edit.';
comment on column public.templates.intro_storage_path is
  'Path inside the editor-projects bucket, e.g. templates/{id}/intro.mp4.';
comment on column public.templates.layout is
  'Optional JSON snapshot of timeline/captions/hook style for recovery.';

create index if not exists templates_active_idx on public.templates (active);
create index if not exists templates_owner_id_idx on public.templates (owner_id);
create index if not exists templates_updated_at_idx on public.templates (updated_at desc);
create index if not exists templates_project_id_idx on public.templates (project_id);

drop trigger if exists templates_set_updated_at on public.templates;
create trigger templates_set_updated_at
  before update on public.templates
  for each row
  execute function public.set_updated_at();

alter table public.templates enable row level security;

-- Backend uses the service role (bypasses RLS). No anon/authenticated policies:
-- templates are managed only through the FastAPI poster API.
