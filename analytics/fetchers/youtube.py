#!/usr/bin/env python3
"""YouTube Shorts / video stats via Data API + Analytics API (retention)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .base import Metrics, extract_youtube_video_id
from .retry import is_rate_limit_error, with_rate_limit_retry

YT_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"


def _parse_iso_duration_seconds(iso: str | None) -> float | None:
    """Parse ISO-8601 duration (PT1M23S) to seconds."""
    if not iso or not isinstance(iso, str):
        return None
    import re

    m = re.fullmatch(
        r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?",
        iso.strip(),
    )
    if not m:
        return None
    days, hours, mins, secs = m.groups()
    total = (
        int(days or 0) * 86400
        + int(hours or 0) * 3600
        + int(mins or 0) * 60
        + float(secs or 0)
    )
    return total


def _load_oauth_credentials():
    """Load YouTube OAuth creds from clients.json / token.json (refresh if needed)."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        return None

    from ..config import get_settings, load_clients_config

    settings = get_settings()
    clients = load_clients_config()
    yt = clients.get("youtube") or {}
    token_data = yt.get("token") if isinstance(yt.get("token"), dict) else None
    token_file = settings["posting_clips_dir"] / (
        (yt.get("token_file") if isinstance(yt.get("token_file"), str) else None)
        or "token.json"
    )

    creds = None
    if token_data:
        try:
            creds = Credentials.from_authorized_user_info(token_data)
        except Exception:
            creds = None
    if not creds and token_file.is_file():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file))
        except Exception:
            creds = None
    if not creds:
        return None

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            try:
                from uploader import _persist_youtube_creds

                _persist_youtube_creds(
                    creds,
                    token_file=str(token_file),
                    config_path=str(settings["clients_json_path"]),
                )
            except Exception:
                pass
        except Exception:
            return None

    if not creds.valid:
        return None
    return creds


def _token_has_analytics_scope(creds) -> bool:
    scopes = set(creds.scopes or [])
    if not scopes:
        # Older tokens omit scopes; attempt Analytics calls anyway.
        return True
    return YT_ANALYTICS_SCOPE in scopes


def _from_api_key(video_id: str, api_key: str) -> Metrics | None:
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return None

    try:
        youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
        resp = (
            youtube.videos()
            .list(part="statistics,contentDetails,snippet", id=video_id)
            .execute()
        )
        items = resp.get("items") or []
        if not items:
            return Metrics(error=f"YouTube video not found: {video_id}")
        item = items[0]
        stats = item.get("statistics") or {}
        details = item.get("contentDetails") or {}
        snippet = item.get("snippet") or {}
        duration = _parse_iso_duration_seconds(details.get("duration"))
        return Metrics(
            views=int(stats.get("viewCount") or 0),
            likes=int(stats.get("likeCount") or 0),
            comments=int(stats.get("commentCount") or 0),
            shares=0,
            saves=0,
            raw={
                "source": "youtube_data_api",
                "statistics": stats,
                "duration_seconds": duration,
                "published_at": snippet.get("publishedAt"),
            },
        )
    except Exception as e:
        return Metrics(error=f"YouTube Data API error: {e}")


def _from_oauth_data_api(video_id: str, creds) -> Metrics | None:
    """Same public stats via OAuth (useful when API key is missing)."""
    try:
        from googleapiclient.discovery import build
    except ImportError:
        return None
    try:
        youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)
        resp = (
            youtube.videos()
            .list(part="statistics,contentDetails,snippet", id=video_id)
            .execute()
        )
        items = resp.get("items") or []
        if not items:
            return Metrics(error=f"YouTube video not found: {video_id}")
        item = items[0]
        stats = item.get("statistics") or {}
        details = item.get("contentDetails") or {}
        snippet = item.get("snippet") or {}
        duration = _parse_iso_duration_seconds(details.get("duration"))
        return Metrics(
            views=int(stats.get("viewCount") or 0),
            likes=int(stats.get("likeCount") or 0),
            comments=int(stats.get("commentCount") or 0),
            shares=0,
            saves=0,
            raw={
                "source": "youtube_data_api_oauth",
                "statistics": stats,
                "duration_seconds": duration,
                "published_at": snippet.get("publishedAt"),
            },
        )
    except Exception as e:
        return Metrics(error=f"YouTube Data API (OAuth) error: {e}")


def _date_range_for_video(published_at: str | None) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=28)
    if published_at:
        try:
            pub = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
            # Analytics often lags ~1–2 days; start at publish day.
            start = pub
        except Exception:
            pass
    if start > end:
        start = end
    return start.isoformat(), end.isoformat()


def _column_index(headers: list[dict], name: str) -> int | None:
    for i, h in enumerate(headers or []):
        if (h.get("name") or "").lower() == name.lower():
            return i
    return None


def _friendly_analytics_error(exc: BaseException | str) -> str:
    text = str(exc)
    low = text.lower()
    if "accessnotconfigured" in low or "has not been used in project" in low:
        return (
            "YouTube Analytics API is disabled for this Google Cloud project. "
            "Enable it: https://console.developers.google.com/apis/api/"
            "youtubeanalytics.googleapis.com/overview?project=303012426859 "
            "then wait a few minutes and re-run."
        )
    if "insufficient" in low and "scope" in low:
        return (
            "YouTube OAuth is missing Analytics scopes. "
            "Re-run: python uploader.py --setup-youtube"
        )
    # Keep HttpError blobs short for exports.
    if "HttpError" in text and len(text) > 280:
        return text[:277] + "..."
    return text


def _fetch_retention(video_id: str, creds, *, published_at: str | None) -> dict[str, Any]:
    """
    Pull average view % / duration and audience retention curve via YouTube Analytics API.
    Requires yt-analytics.readonly (+ typically youtube.readonly) OAuth scopes.
    """
    out: dict[str, Any] = {}
    try:
        from googleapiclient.discovery import build
    except ImportError:
        out["error"] = "google-api-python-client not installed"
        return out

    if not _token_has_analytics_scope(creds):
        out["error"] = (
            "YouTube OAuth token is missing yt-analytics.readonly. "
            "Re-connect YouTube (python uploader.py --setup-youtube) after enabling "
            "YouTube Analytics API in Google Cloud Console."
        )
        return out

    start_date, end_date = _date_range_for_video(published_at)
    out["start_date"] = start_date
    out["end_date"] = end_date

    try:
        analytics = build(
            "youtubeAnalytics", "v2", credentials=creds, cache_discovery=False
        )

        # Aggregate retention metrics for this video over the date range.
        summary = (
            analytics.reports()
            .query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                metrics=(
                    "views,estimatedMinutesWatched,"
                    "averageViewDuration,averageViewPercentage"
                ),
                filters=f"video=={video_id}",
            )
            .execute()
        )
        headers = summary.get("columnHeaders") or []
        rows = summary.get("rows") or []
        if rows:
            row = rows[0]

            def _num(col: str) -> float | None:
                idx = _column_index(headers, col)
                if idx is None or idx >= len(row):
                    return None
                try:
                    return float(row[idx])
                except (TypeError, ValueError):
                    return None

            avg_dur = _num("averageViewDuration")
            avg_pct = _num("averageViewPercentage")
            minutes = _num("estimatedMinutesWatched")
            if avg_dur is not None:
                out["average_view_duration_seconds"] = avg_dur
            if avg_pct is not None:
                # YouTube's averageViewPercentage is already "% of video length watched".
                out["average_watch_percent"] = round(avg_pct, 2)
            if minutes is not None:
                out["estimated_minutes_watched"] = minutes

        # Audience retention curve (one video only).
        curve_resp = (
            analytics.reports()
            .query(
                ids="channel==MINE",
                startDate=start_date,
                endDate=end_date,
                dimensions="elapsedVideoTimeRatio",
                metrics="audienceWatchRatio,relativeRetentionPerformance",
                filters=f"video=={video_id}",
            )
            .execute()
        )
        c_headers = curve_resp.get("columnHeaders") or []
        c_rows = curve_resp.get("rows") or []
        ratio_i = _column_index(c_headers, "elapsedVideoTimeRatio")
        watch_i = _column_index(c_headers, "audienceWatchRatio")
        rel_i = _column_index(c_headers, "relativeRetentionPerformance")
        curve: list[dict[str, float]] = []
        for crow in c_rows:
            point: dict[str, float] = {}
            if ratio_i is not None and ratio_i < len(crow):
                point["elapsed_ratio"] = float(crow[ratio_i])
            if watch_i is not None and watch_i < len(crow):
                point["audience_watch_ratio"] = float(crow[watch_i])
            if rel_i is not None and rel_i < len(crow):
                point["relative_retention"] = float(crow[rel_i])
            if point:
                curve.append(point)
        if curve:
            out["curve"] = curve

        if (
            "average_view_duration_seconds" not in out
            and "average_watch_percent" not in out
            and not curve
        ):
            out["error"] = (
                "No Analytics retention rows returned "
                f"(range {start_date}..{end_date}). Data can lag 24–48h after publish."
            )
    except Exception as e:
        out["error"] = _friendly_analytics_error(e)
    return out


def _from_yt_dlp(url: str) -> Metrics:
    try:
        import yt_dlp
    except ImportError:
        return Metrics(error="yt-dlp is not installed")

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return Metrics(error="yt-dlp returned no info")
        return Metrics(
            views=int(info.get("view_count") or 0),
            likes=int(info.get("like_count") or 0),
            comments=int(info.get("comment_count") or 0),
            shares=0,
            saves=0,
            raw={
                "source": "yt-dlp",
                "id": info.get("id"),
                "title": info.get("title"),
                "duration_seconds": info.get("duration"),
            },
        )
    except Exception as e:
        return Metrics(error=f"yt-dlp YouTube error: {e}")


def _attach_retention(metrics: Metrics, video_id: str, creds) -> Metrics:
    published_at = None
    duration = None
    if isinstance(metrics.raw, dict):
        published_at = metrics.raw.get("published_at")
        duration = metrics.raw.get("duration_seconds")
    retention = _fetch_retention(video_id, creds, published_at=published_at)
    metrics.raw = dict(metrics.raw or {})
    metrics.raw["retention"] = retention

    avg_dur = retention.get("average_view_duration_seconds")
    avg_pct = retention.get("average_watch_percent")

    # Prefer Analytics %; else derive from avg duration / video length.
    if not isinstance(avg_pct, (int, float)) and isinstance(avg_dur, (int, float)):
        try:
            dur = float(duration) if duration else 0.0
            if dur > 0:
                avg_pct = round((float(avg_dur) / dur) * 100.0, 2)
                retention["average_watch_percent"] = avg_pct
                retention["average_watch_percent_source"] = "derived_from_duration"
        except (TypeError, ValueError):
            pass

    if isinstance(avg_dur, (int, float)):
        metrics.watch_time_seconds = float(avg_dur)
        metrics.average_view_duration_seconds = float(avg_dur)
    if isinstance(avg_pct, (int, float)):
        metrics.average_view_percentage = float(avg_pct)
    curve = retention.get("curve")
    if isinstance(curve, list):
        metrics.retention_curve = curve
    return metrics


def fetch_youtube_metrics(post_url: str, *, api_key: str | None = None) -> Metrics:
    video_id = extract_youtube_video_id(post_url)
    if not video_id:
        return Metrics(error=f"Could not parse YouTube video id from URL: {post_url}")

    def _once() -> Metrics:
        return _fetch_youtube_metrics_once(post_url, video_id, api_key=api_key)

    try:
        return with_rate_limit_retry(
            "youtube",
            _once,
            label=f"video {video_id}",
        )
    except Exception as e:
        if is_rate_limit_error(e):
            print(f"[youtube] Giving up after retries — still rate limited: {e}")
            return Metrics(
                error=f"YouTube rate limited (429): {e}. Wait a few minutes and retry."
            )
        return Metrics(error=f"YouTube fetch error: {e}")


def _fetch_youtube_metrics_once(
    post_url: str, video_id: str, *, api_key: str | None = None
) -> Metrics:
    creds = _load_oauth_credentials()
    result: Metrics | None = None

    if api_key:
        result = _from_api_key(video_id, api_key)
        if result is not None and result.error and is_rate_limit_error(result.error):
            raise RuntimeError(result.error)

    if (result is None or result.error) and creds:
        oauth_stats = _from_oauth_data_api(video_id, creds)
        if oauth_stats is not None and oauth_stats.error and is_rate_limit_error(
            oauth_stats.error
        ):
            raise RuntimeError(oauth_stats.error)
        if oauth_stats is not None and not oauth_stats.error:
            result = oauth_stats

    if result is not None and not result.error:
        if creds:
            attached = _attach_retention(result, video_id, creds)
            retention = (attached.raw or {}).get("retention") or {}
            if isinstance(retention, dict) and is_rate_limit_error(retention.get("error")):
                raise RuntimeError(retention["error"])
            return attached
        result.raw = dict(result.raw or {})
        result.raw["retention"] = {
            "error": (
                "YouTube OAuth token not found. Retention requires channel OAuth "
                "(re-run: python uploader.py --setup-youtube)."
            )
        }
        return result

    fallback = _from_yt_dlp(post_url)
    if not fallback.error:
        if result and result.error:
            fallback.raw["api_error"] = result.error
        if creds:
            attached = _attach_retention(fallback, video_id, creds)
            retention = (attached.raw or {}).get("retention") or {}
            if isinstance(retention, dict) and is_rate_limit_error(retention.get("error")):
                raise RuntimeError(retention["error"])
            return attached
        fallback.raw["retention"] = {
            "error": (
                "YouTube OAuth token not found. Retention requires channel OAuth "
                "(re-run: python uploader.py --setup-youtube)."
            )
        }
        return fallback
    if result and result.error:
        return result
    return fallback
