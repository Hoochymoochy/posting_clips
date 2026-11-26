"""Template library: multiple Discover intro templates + shared style config."""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_TEMPLATES_ROOT = Path(__file__).resolve().parent
_LIBRARY_DIR = _TEMPLATES_ROOT / "library"
_EDITOR_BINDING = _TEMPLATES_ROOT / "editor_binding.json"
_LEGACY_DEFAULT_ID = "default"


def templates_dir() -> Path:
    return _TEMPLATES_ROOT


def library_dir() -> Path:
    _LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    return _LIBRARY_DIR


_DEFAULT_TEMPLATE_CONFIG: dict[str, Any] = {
    "intro": {
        "bumper_path": "assets/intro.mp4",
        "loudnorm": True,
        "body_title": False,
        "text_style": "minimal",
        "font": "bahnschrift",
        "font_size": 48,
        "title_y": "h*0.25",
        "color": "none",
    },
    "header": {
        "loudnorm": True,
        "text_style": "neon",
        "font": "impact",
        "font_size": 56,
        "title_y": "h*0.22",
        "color": "none",
    },
}


def load_template_config() -> dict[str, Any]:
    path = _TEMPLATES_ROOT / "config.json"
    if not path.is_file():
        return dict(_DEFAULT_TEMPLATE_CONFIG)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("templates/config.json must be a JSON object")
    return data


SELECT_TEMPLATE_MESSAGE = (
    "Select a template first — turn one on in Templates (and Save template in the editor)."
)


def require_active_templates() -> list[dict[str, Any]]:
    """Return active+ready templates, or raise ValueError with a clear UI message."""
    templates = active_templates()
    if not templates:
        raise ValueError(SELECT_TEMPLATE_MESSAGE)
    return templates


def _legacy_bumper_path(config: dict[str, Any] | None = None) -> Path:
    """Configured path for the single shared bumper (templates/assets/intro.mp4)."""
    cfg = config if config is not None else load_template_config()
    intro = cfg.get("intro") or {}
    raw = str(intro.get("bumper_path") or "assets/intro.mp4").strip()
    p = Path(raw)
    if not p.is_absolute():
        p = (_TEMPLATES_ROOT / p).resolve()
    return p


def resolve_bumper_path(config: dict[str, Any] | None = None) -> Path:
    """
    Prefer the first active+ready library template intro; fall back to
    legacy templates/assets/intro.mp4 from config.
    """
    root = library_dir()
    try:
        for child in sorted(root.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            meta = _read_meta(child / "meta.json") or {}
            if meta and not bool(meta.get("active", True)):
                continue
            intro = child / "intro.mp4"
            if intro.is_file() and intro.stat().st_size > 20_000:
                return intro
    except OSError:
        pass
    return _legacy_bumper_path(config)


def _safe_id(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", (raw or "").strip())[:48].strip("-")
    return cleaned or uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def template_dir(template_id: str) -> Path:
    return library_dir() / _safe_id(template_id)


def template_meta_path(template_id: str) -> Path:
    return template_dir(template_id) / "meta.json"


def template_intro_path(template_id: str) -> Path:
    return template_dir(template_id) / "intro.mp4"


def _read_meta(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _enrich_bumper_fields(template_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Attach has_bumper / intro_bytes / storage path from local intro.mp4 when present."""
    payload = dict(meta)
    tid = _safe_id(template_id)
    intro = template_intro_path(tid)
    if intro.is_file() and intro.stat().st_size > 1000:
        size = intro.stat().st_size
        payload["has_bumper"] = True
        payload["intro_bytes"] = size
        payload["intro_storage_path"] = f"templates/{tid}/intro.mp4"
    else:
        # Keep explicit DB/cloud flags when the local cache is empty.
        payload.setdefault("has_bumper", bool(payload.get("has_bumper", False)))
        payload.setdefault("intro_bytes", int(payload.get("intro_bytes") or 0))
    return payload


def _write_meta(template_id: str, meta: dict[str, Any]) -> dict[str, Any]:
    folder = template_dir(template_id)
    folder.mkdir(parents=True, exist_ok=True)
    payload = _enrich_bumper_fields(template_id, meta)
    payload["id"] = _safe_id(template_id)
    payload["updated_at"] = _now_iso()
    if not payload.get("created_at"):
        payload["created_at"] = payload["updated_at"]
    with open(folder / "meta.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    try:
        from cloud_store import upsert_template_meta

        upsert_template_meta(payload)
    except Exception as exc:
        print(f"  [templates] cloud meta upsert failed: {exc}")
    return payload


def _public_template(meta: dict[str, Any]) -> dict[str, Any]:
    tid = str(meta.get("id") or "")
    intro = template_intro_path(tid)
    project_id = str(meta.get("project_id") or "").strip() or None
    disk_intro = intro.is_file() and intro.stat().st_size > 20_000
    meta_bumper = bool(meta.get("has_bumper")) or int(meta.get("intro_bytes") or 0) > 20_000
    has_intro = disk_intro or meta_bumper
    # Hook-only templates (no bumper) are ready once a project is bound.
    ready = has_intro or bool(project_id)
    caption_prompt = str(meta.get("caption_prompt") or "").strip() or None
    intro_bytes = (
        intro.stat().st_size
        if intro.is_file()
        else int(meta.get("intro_bytes") or 0)
    )
    out = {
        "id": tid,
        "name": str(meta.get("name") or "Untitled template"),
        "project_id": project_id,
        "owner_id": str(meta.get("owner_id") or "").strip() or None,
        "active": bool(meta.get("active", True)),
        "ready": ready,
        "has_bumper": has_intro,
        "intro_bytes": intro_bytes,
        "caption_prompt": caption_prompt,
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
    }
    if isinstance(meta.get("layout"), dict):
        out["has_layout"] = True
    return out


def migrate_legacy_template() -> dict[str, Any] | None:
    """
    One-time: fold legacy assets/intro.mp4 + editor_binding into library/default.
    Skips if the library already has other templates (e.g. default was deleted).
    """
    legacy_intro = _legacy_bumper_path()
    binding = load_editor_binding()
    legacy_project = str(binding.get("project_id") or "").strip() or None
    dest_meta = template_meta_path(_LEGACY_DEFAULT_ID)
    dest_intro = template_intro_path(_LEGACY_DEFAULT_ID)

    if dest_meta.is_file() or dest_intro.is_file():
        # Already migrated — do not call get_template (it re-enters migrate).
        meta = _read_meta(dest_meta) or {
            "id": _LEGACY_DEFAULT_ID,
            "name": "Intro template",
            "active": True,
            "project_id": legacy_project,
        }
        return _public_template(meta)

    # Do not resurrect "default" once the user deleted it or already has a library.
    try:
        has_other = any(
            p.is_dir() and p.name != _LEGACY_DEFAULT_ID for p in library_dir().iterdir()
        )
    except OSError:
        has_other = False
    if has_other:
        return None

    has_legacy = (legacy_intro.is_file() and legacy_intro.stat().st_size > 20_000) or legacy_project
    if not has_legacy:
        return None

    folder = template_dir(_LEGACY_DEFAULT_ID)
    folder.mkdir(parents=True, exist_ok=True)
    if legacy_intro.is_file() and legacy_intro.stat().st_size > 1000:
        shutil.copy2(legacy_intro, dest_intro)
    meta = _write_meta(
        _LEGACY_DEFAULT_ID,
        {
            "id": _LEGACY_DEFAULT_ID,
            "name": "Intro template",
            "project_id": legacy_project,
            "active": True,
        },
    )
    return _public_template(meta)


def list_templates() -> list[dict[str, Any]]:
    migrate_legacy_template()
    by_id: dict[str, dict[str, Any]] = {}

    # 1) Postgres is the tracking source of truth.
    try:
        from cloud_store import hydrate_template_to_disk, list_template_db_rows

        for meta in list_template_db_rows():
            tid = str(meta.get("id") or "").strip()
            if not tid:
                continue
            # Ensure local cache (meta + intro) exists for render/edit.
            disk_meta = _read_meta(template_meta_path(tid))
            intro = template_intro_path(tid)
            needs_hydrate = not disk_meta or (
                bool(meta.get("has_bumper"))
                and (not intro.is_file() or intro.stat().st_size < 1000)
            )
            if needs_hydrate:
                try:
                    hydrate_template_to_disk(tid, template_dir(tid))
                except Exception as exc:
                    print(f"  [templates] hydrate {tid}: {exc}")
            # Prefer freshest disk meta when present (local edits), else DB row.
            merged = disk_meta or meta
            if disk_meta and meta:
                merged = dict(meta)
                merged.update(disk_meta)
                # DB bumper flags win when disk cache was wiped.
                if meta.get("has_bumper") and not merged.get("has_bumper"):
                    merged["has_bumper"] = True
                    merged["intro_bytes"] = meta.get("intro_bytes") or 0
                    merged["intro_storage_path"] = meta.get("intro_storage_path")
                if isinstance(meta.get("layout"), dict) and "layout" not in merged:
                    merged["layout"] = meta["layout"]
            by_id[tid] = _public_template(merged)
            # Push any disk-only fields back so DB stays current.
            if disk_meta and (
                not meta.get("has_bumper")
                and (intro.is_file() and intro.stat().st_size > 20_000)
            ):
                _write_meta(tid, disk_meta)
    except Exception as exc:
        print(f"  [templates] db list failed: {exc}")

    # 2) Disk folders not yet in DB (legacy / offline) — upsert them.
    root = library_dir()
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name in by_id:
            continue
        meta = _read_meta(child / "meta.json")
        if not meta:
            intro = child / "intro.mp4"
            if intro.is_file():
                meta = _write_meta(
                    child.name,
                    {
                        "id": child.name,
                        "name": child.name.replace("-", " ").title(),
                        "active": True,
                    },
                )
            else:
                continue
        else:
            # First time this host sees DB — persist disk row.
            meta = _write_meta(child.name, meta)
        by_id[child.name] = _public_template(meta)

    rows = list(by_id.values())
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return rows


def get_template(template_id: str) -> dict[str, Any] | None:
    migrate_legacy_template()
    tid = _safe_id(template_id)
    meta = _read_meta(template_meta_path(tid))
    if not meta:
        try:
            from cloud_store import hydrate_template_to_disk

            meta = hydrate_template_to_disk(tid, template_dir(tid))
        except Exception as exc:
            print(f"  [templates] cloud hydrate failed: {exc}")
            meta = None
    if not meta:
        try:
            from cloud_store import fetch_template_db

            meta = fetch_template_db(tid)
        except Exception as exc:
            print(f"  [templates] db fetch failed: {exc}")
            meta = None
    if not meta:
        return None
    return _public_template(meta)


def ensure_template(
    template_id: str,
    *,
    name: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Get or create a template folder/meta for the given id."""
    existing = get_template(template_id)
    if existing:
        return existing
    tid = _safe_id(template_id)
    meta = _write_meta(
        tid,
        {
            "id": tid,
            "name": (name or "").strip() or tid,
            "project_id": (project_id or "").strip() or None,
            "active": True,
        },
    )
    return _public_template(meta)


def create_template(
    *,
    name: str | None = None,
    project_id: str | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    migrate_legacy_template()
    tid = uuid.uuid4().hex[:12]
    label = (name or "").strip() or f"Template {len(list_templates()) + 1}"
    meta = _write_meta(
        tid,
        {
            "id": tid,
            "name": label,
            "project_id": (project_id or "").strip() or None,
            "owner_id": (owner_id or "").strip() or None,
            "active": True,
        },
    )
    return _public_template(meta)


def filter_templates_for_owner(
    items: list[dict[str, Any]], owner_id: str | None
) -> list[dict[str, Any]]:
    if not owner_id:
        return items
    oid = str(owner_id).strip()
    return [
        row
        for row in items
        if not row.get("owner_id") or str(row.get("owner_id")) == oid
    ]


def assert_template_access(template: dict[str, Any], owner_id: str | None) -> None:
    if not owner_id:
        return
    existing = str(template.get("owner_id") or "").strip()
    if existing and existing != str(owner_id).strip():
        raise PermissionError("Template belongs to another creator")


def snapshot_layout_from_project(project_id: str | None) -> dict[str, Any] | None:
    """Pull a compact editor layout snapshot from a bound project (for DB recovery)."""
    pid = (project_id or "").strip()
    if not pid:
        return None
    try:
        from projects_store import load_project

        project = load_project(pid)
    except Exception:
        return None
    timeline = project.get("timeline") if isinstance(project.get("timeline"), dict) else {}
    assets = project.get("assets") if isinstance(project.get("assets"), list) else []
    return {
        "name": project.get("name"),
        "timeline": {
            "laneCount": timeline.get("laneCount"),
            "audioLaneCount": timeline.get("audioLaneCount"),
            "clips": timeline.get("clips") or [],
            "captions": timeline.get("captions") or [],
            "hookFont": timeline.get("hookFont"),
            "hookStyle": timeline.get("hookStyle"),
            "hookFontSize": timeline.get("hookFontSize"),
            "hookTextY": timeline.get("hookTextY"),
        },
        "assets": [
            {
                "id": a.get("id"),
                "name": a.get("name"),
                "filename": a.get("filename"),
                "duration": a.get("duration"),
                "kind": a.get("kind"),
                "templateSlot": a.get("templateSlot"),
            }
            for a in assets
            if isinstance(a, dict)
        ],
        "snapshotted_at": _now_iso(),
    }


def update_template(
    template_id: str,
    *,
    name: str | None = None,
    project_id: str | None = None,
    active: bool | None = None,
    caption_prompt: str | None = None,
    owner_id: str | None = None,
    layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tid = _safe_id(template_id)
    meta = _read_meta(template_meta_path(tid))
    if not meta:
        try:
            from cloud_store import fetch_template_meta

            meta = fetch_template_meta(tid)
        except Exception:
            meta = None
    meta = meta or {
        "id": tid,
        "name": tid,
        "active": True,
    }
    if name is not None:
        clean = name.strip()
        if clean:
            meta["name"] = clean
    if project_id is not None:
        meta["project_id"] = project_id.strip() or None
        # Refresh layout snapshot whenever the bound project changes.
        snap = snapshot_layout_from_project(meta.get("project_id"))
        if snap:
            meta["layout"] = snap
    if active is not None:
        meta["active"] = bool(active)
    if caption_prompt is not None:
        # Empty string clears the custom prompt (falls back to default Ollama voice).
        meta["caption_prompt"] = caption_prompt.strip() or None
    if owner_id is not None:
        meta["owner_id"] = owner_id.strip() or None
    if layout is not None:
        meta["layout"] = layout if isinstance(layout, dict) else None
    saved = _write_meta(tid, meta)
    return _public_template(saved)


def active_caption_prompt() -> str | None:
    """First active+ready template caption prompt, if any."""
    for row in active_templates():
        prompt = str(row.get("caption_prompt") or "").strip()
        if prompt:
            return prompt
    return None


def delete_template(template_id: str) -> bool:
    tid = _safe_id(template_id)
    folder = template_dir(tid)
    if not folder.exists():
        return False
    shutil.rmtree(folder, ignore_errors=True)
    try:
        from cloud_store import delete_cloud_template

        delete_cloud_template(tid)
    except Exception as exc:
        print(f"  [templates] cloud delete failed: {exc}")
    return True


def save_template_intro(
    template_id: str,
    *,
    source_path: Path,
    project_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Copy an uploaded/exported intro MP4 into the template folder."""
    tid = _safe_id(template_id)
    folder = template_dir(tid)
    folder.mkdir(parents=True, exist_ok=True)
    dest = template_intro_path(tid)
    shutil.copy2(source_path, dest)

    meta = _read_meta(template_meta_path(tid))
    if not meta:
        try:
            from cloud_store import fetch_template_meta

            meta = fetch_template_meta(tid)
        except Exception:
            meta = None
    meta = meta or {
        "id": tid,
        "name": name or tid,
        "active": True,
    }
    if name and name.strip():
        meta["name"] = name.strip()
    if project_id is not None:
        meta["project_id"] = project_id.strip() or None
    meta["has_bumper"] = True
    meta["intro_bytes"] = dest.stat().st_size if dest.is_file() else 0
    meta["intro_storage_path"] = f"templates/{tid}/intro.mp4"
    snap = snapshot_layout_from_project(meta.get("project_id"))
    if snap:
        meta["layout"] = snap
    saved = _write_meta(tid, meta)

    try:
        from cloud_store import upload_template_intro

        upload_template_intro(tid, dest)
    except Exception as exc:
        print(f"  [templates] cloud intro upload failed: {exc}")

    # Keep legacy bumper in sync with the first/active template for old render paths.
    _sync_legacy_default(tid, dest, meta.get("project_id"))
    return _public_template(saved)


def _sync_legacy_default(template_id: str, intro_path: Path, project_id: Any) -> None:
    """Mirror into assets/intro.mp4 so older paths still see a bumper file."""
    try:
        legacy = _legacy_bumper_path()
        legacy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(intro_path, legacy)
        if project_id:
            save_editor_binding(project_id=str(project_id))
    except OSError as exc:
        print(f"  [templates] legacy sync failed: {exc}")


def active_templates() -> list[dict[str, Any]]:
    return [t for t in list_templates() if t.get("active") and t.get("ready")]


def load_editor_binding() -> dict[str, Any]:
    """Editor project id linked to the Discover intro template (legacy)."""
    if not _EDITOR_BINDING.is_file():
        return {}
    try:
        with open(_EDITOR_BINDING, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_editor_binding(*, project_id: str | None) -> dict[str, Any]:
    clean = (project_id or "").strip()
    payload = {"project_id": clean} if clean else {}
    with open(_EDITOR_BINDING, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return payload
