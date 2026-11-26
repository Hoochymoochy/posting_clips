"""
Disk-backed editor projects.

Layout:
  projects/{id}/
    project.json
    assets/{asset_id}.{ext}
"""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from worker import get_clips_dir

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]+$")


def get_projects_dir() -> Path:
    env_dir = os.environ.get("PROJECTS_DIR", "projects").strip()
    p = Path(env_dir).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value: str, label: str = "id") -> str:
    clean = (value or "").strip()
    if not _SAFE_ID.fullmatch(clean):
        raise ValueError(f"Invalid {label}: {value!r}")
    return clean


def project_folder(project_id: str) -> Path:
    return get_projects_dir() / _safe_id(project_id, "project id")


def assets_dir(project_id: str) -> Path:
    folder = project_folder(project_id) / "assets"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def project_json_path(project_id: str) -> Path:
    return project_folder(project_id) / "project.json"


def _default_project(project_id: str, name: str = "Untitled project") -> dict[str, Any]:
    now = _now_iso()
    return {
        "id": project_id,
        "name": name or "Untitled project",
        "description": "",
        "source_clip_id": None,
        "owner_id": None,
        "created_at": now,
        "updated_at": now,
        "assets": [],
        "timeline": {
            "laneCount": 2,
            "audioLaneCount": 1,
            "clips": [],
            "captions": [],
            "hookFont": None,
            "hookStyle": None,
            "hookFontSize": None,
            "hookTextY": None,
        },
    }


def _write_project(data: dict[str, Any]) -> dict[str, Any]:
    project_id = _safe_id(str(data["id"]), "project id")
    folder = project_folder(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    assets_dir(project_id)
    data["updated_at"] = _now_iso()
    path = project_json_path(project_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    try:
        from cloud_store import upload_project_asset, upsert_project

        upsert_project(data)
        # Keep asset binaries in sync (new uploads + clip copies on create).
        for row in data.get("assets") or []:
            if not isinstance(row, dict):
                continue
            filename = str(row.get("filename") or "").strip()
            if not filename:
                continue
            asset_path = assets_dir(project_id) / filename
            if asset_path.is_file():
                upload_project_asset(project_id, filename, asset_path.read_bytes())
    except Exception as exc:
        print(f"  [projects] cloud upsert failed: {exc}")
    return data


def load_project(project_id: str) -> dict[str, Any]:
    pid = _safe_id(project_id, "project id")
    path = project_json_path(pid)
    if not path.is_file():
        # Pull from Supabase Storage when this host never saw the project (e.g. remote poster).
        try:
            from cloud_store import hydrate_project_to_disk

            hydrate_project_to_disk(pid, project_folder(pid))
        except Exception as exc:
            print(f"  [projects] cloud hydrate failed: {exc}")
    if not path.is_file():
        raise FileNotFoundError(
            f"Project folder missing (id {project_id}). "
            "It may have been deleted — open the template again to create a fresh project."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid project.json")
    data.setdefault("assets", [])
    data.setdefault(
        "timeline",
        {
            "laneCount": 2,
            "audioLaneCount": 1,
            "clips": [],
            "captions": [],
        },
    )
    # Ensure asset files exist locally (timeline media for the editor).
    try:
        from cloud_store import hydrate_project_to_disk

        missing = False
        for row in data.get("assets") or []:
            if not isinstance(row, dict):
                continue
            filename = str(row.get("filename") or "")
            if filename and not (assets_dir(pid) / filename).is_file():
                missing = True
                break
        if missing:
            hydrate_project_to_disk(pid, project_folder(pid))
            data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [projects] asset hydrate failed: {exc}")
    return data


def list_projects() -> list[dict[str, Any]]:
    root = get_projects_dir()
    by_id: dict[str, dict[str, Any]] = {}
    if root.is_dir():
        for child in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
            if not child.is_dir():
                continue
            meta_path = child / "project.json"
            if not meta_path.is_file():
                continue
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            assets = data.get("assets") or []
            pid = str(data.get("id") or child.name)
            by_id[pid] = {
                "id": pid,
                "name": data.get("name") or "Untitled project",
                "description": data.get("description") or "",
                "source_clip_id": data.get("source_clip_id"),
                "owner_id": data.get("owner_id"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "asset_count": len(assets) if isinstance(assets, list) else 0,
            }

    # Include projects that only exist in Supabase yet.
    try:
        from cloud_store import fetch_project, list_cloud_project_ids

        for pid in list_cloud_project_ids():
            if pid in by_id:
                continue
            data = fetch_project(pid)
            if not data:
                continue
            assets = data.get("assets") or []
            by_id[pid] = {
                "id": pid,
                "name": data.get("name") or "Untitled project",
                "description": data.get("description") or "",
                "source_clip_id": data.get("source_clip_id"),
                "owner_id": data.get("owner_id"),
                "created_at": data.get("created_at"),
                "updated_at": data.get("updated_at"),
                "asset_count": len(assets) if isinstance(assets, list) else 0,
            }
    except Exception as exc:
        print(f"  [projects] cloud list failed: {exc}")

    items = list(by_id.values())
    items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return items


def filter_projects_for_owner(
    items: list[dict[str, Any]], owner_id: str | None
) -> list[dict[str, Any]]:
    """
    When owner_id is set, return that creator's projects plus legacy unowned rows
    (single-tenant back-compat). When owner_id is None, return everything.
    """
    if not owner_id:
        return items
    oid = str(owner_id).strip()
    return [
        row
        for row in items
        if not row.get("owner_id") or str(row.get("owner_id")) == oid
    ]


def assert_project_access(project: dict[str, Any], owner_id: str | None) -> None:
    """Raise PermissionError if a signed-in owner cannot access this project."""
    if not owner_id:
        return
    existing = str(project.get("owner_id") or "").strip()
    if existing and existing != str(owner_id).strip():
        raise PermissionError("Project belongs to another creator")


def create_project(
    *,
    name: str | None = None,
    description: str | None = None,
    source_clip_id: str | None = None,
    copy_clip: bool = True,
    timeline: dict[str, Any] | None = None,
    asset_name: str | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    project_id = uuid.uuid4().hex
    data = _default_project(project_id, name or "Untitled project")
    if description is not None:
        data["description"] = str(description)
    if owner_id:
        data["owner_id"] = str(owner_id).strip() or None
    if timeline:
        data["timeline"] = {**data["timeline"], **timeline}

    clip_id = (source_clip_id or "").strip() or None
    if clip_id:
        data["source_clip_id"] = _safe_id(clip_id, "clip id")
        if copy_clip:
            src = get_clips_dir() / data["source_clip_id"] / "clip.mp4"
            if not src.is_file():
                raise FileNotFoundError(f"Clip file not found for {clip_id}")
            asset_id = uuid.uuid4().hex[:12]
            filename = f"{asset_id}.mp4"
            dest = assets_dir(project_id) / filename
            shutil.copy2(src, dest)
            display_name = asset_name or f"clip-{data['source_clip_id']}.mp4"
            data["assets"].append(
                {
                    "id": asset_id,
                    "name": display_name,
                    "kind": "video",
                    "filename": filename,
                    "duration": 0,
                    "width": 0,
                    "height": 0,
                    "source": "clip",
                    "source_clip_id": data["source_clip_id"],
                }
            )

    return _write_project(data)


def update_project(project_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    data = load_project(project_id)
    if "name" in patch and patch["name"] is not None:
        data["name"] = str(patch["name"]).strip() or data["name"]
    if "description" in patch and patch["description"] is not None:
        data["description"] = str(patch["description"])
    if "owner_id" in patch:
        raw = patch.get("owner_id")
        data["owner_id"] = str(raw).strip() if raw else None
    if "timeline" in patch and isinstance(patch["timeline"], dict):
        data["timeline"] = patch["timeline"]
    if "assets" in patch and isinstance(patch["assets"], list):
        # Metadata-only updates (duration/width/height/name); keep filenames from disk rows
        by_id = {a["id"]: a for a in data.get("assets") or [] if isinstance(a, dict) and a.get("id")}
        merged = []
        for row in patch["assets"]:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            existing = by_id.get(row["id"])
            if not existing:
                continue
            updated = {**existing}
            for key in ("name", "kind", "duration", "width", "height", "templateSlot"):
                if key in row and row[key] is not None:
                    updated[key] = row[key]
            # Allow clearing templateSlot when client omits / nulls it after remap.
            if "templateSlot" in row and row["templateSlot"] is None:
                updated.pop("templateSlot", None)
            merged.append(updated)
        # Preserve assets not mentioned in patch
        kept_ids = {a["id"] for a in merged}
        for existing in data.get("assets") or []:
            if isinstance(existing, dict) and existing.get("id") and existing["id"] not in kept_ids:
                merged.append(existing)
        data["assets"] = merged
    return _write_project(data)


def delete_project(project_id: str) -> None:
    folder = project_folder(project_id)
    if not folder.is_dir():
        # Still try cloud cleanup if disk copy is already gone.
        try:
            from cloud_store import delete_cloud_project

            delete_cloud_project(project_id)
        except Exception:
            pass
        raise FileNotFoundError(f"Project {project_id} not found")
    shutil.rmtree(folder)
    try:
        from cloud_store import delete_cloud_project

        delete_cloud_project(project_id)
    except Exception as exc:
        print(f"  [projects] cloud delete failed: {exc}")


def _guess_kind(filename: str, content_type: str | None = None) -> str:
    name = filename.lower()
    ct = (content_type or "").lower()
    if ct.startswith("audio/") or re.search(r"\.(mp3|wav|m4a|aac|flac|ogg|opus)$", name):
        return "audio"
    return "video"


def _ext_for(filename: str, content_type: str | None, kind: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix and re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    if kind == "audio":
        if content_type and "wav" in content_type:
            return ".wav"
        return ".mp3"
    return ".mp4"


def add_asset_bytes(
    project_id: str,
    *,
    raw: bytes,
    filename: str,
    content_type: str | None = None,
    name: str | None = None,
    duration: float = 0,
    width: int = 0,
    height: int = 0,
) -> dict[str, Any]:
    data = load_project(project_id)
    kind = _guess_kind(filename, content_type)
    asset_id = uuid.uuid4().hex[:12]
    ext = _ext_for(filename, content_type, kind)
    disk_name = f"{asset_id}{ext}"
    dest = assets_dir(project_id) / disk_name
    dest.write_bytes(raw)
    asset = {
        "id": asset_id,
        "name": (name or filename or disk_name).strip() or disk_name,
        "kind": kind,
        "filename": disk_name,
        "duration": float(duration or 0),
        "width": int(width or 0),
        "height": int(height or 0),
        "source": "upload",
        "source_clip_id": None,
    }
    data.setdefault("assets", []).append(asset)
    _write_project(data)
    try:
        from cloud_store import upload_project_asset

        upload_project_asset(project_id, disk_name, raw, content_type)
    except Exception as exc:
        print(f"  [projects] cloud asset upload failed: {exc}")
    return asset


def delete_asset(project_id: str, asset_id: str) -> dict[str, Any]:
    data = load_project(project_id)
    aid = _safe_id(asset_id, "asset id")
    assets = list(data.get("assets") or [])
    keep = []
    removed = None
    for row in assets:
        if isinstance(row, dict) and str(row.get("id")) == aid:
            removed = row
            continue
        keep.append(row)
    if removed is None:
        raise FileNotFoundError(f"Asset {asset_id} not found")
    data["assets"] = keep
    # Drop timeline clips that referenced it
    tl = data.get("timeline") or {}
    clips = tl.get("clips") or []
    if isinstance(clips, list):
        tl["clips"] = [c for c in clips if not (isinstance(c, dict) and c.get("assetId") == aid)]
        data["timeline"] = tl
    filename = str(removed.get("filename") or "")
    if filename and _SAFE_FILENAME.fullmatch(filename):
        path = assets_dir(project_id) / filename
        if path.is_file():
            path.unlink()
    return _write_project(data)


def resolve_asset_path(project_id: str, asset_id: str) -> Path:
    data = load_project(project_id)
    aid = _safe_id(asset_id, "asset id")
    for row in data.get("assets") or []:
        if not isinstance(row, dict) or str(row.get("id")) != aid:
            continue
        filename = str(row.get("filename") or "")
        if not filename or not _SAFE_FILENAME.fullmatch(filename):
            raise FileNotFoundError("Asset filename missing")
        path = assets_dir(project_id) / filename
        if not path.is_file():
            raise FileNotFoundError(f"Asset file missing: {filename}")
        return path
    raise FileNotFoundError(f"Asset {asset_id} not found")
