-- Migration: 005_discovery_runs_one_url_per_utc_day
-- At most one pending/success run per youtube_url per UTC calendar day.
-- Failed rows are excluded so a same-day retry after failure is allowed.

create unique index if not exists idx_discovery_runs_url_per_utc_day
  on public.discovery_runs (
    youtube_url,
    ((created_at at time zone 'utc')::date)
  )
  where status in ('pending', 'success');

comment on index public.idx_discovery_runs_url_per_utc_day is
  'One pending/success discovery check per URL per UTC day; failed rows may retry.';
