"""
Supabase Storage sync for editor projects + Discover templates.

Bucket: editor-projects
  projects/{id}/project.json
  projects/{id}/assets/{filename}
  templates/{id}/meta.json
  templates/{id}/intro.mp4

Disk remains the working cache on each host; Storage is the shared source of truth
so Edit works on the remote poster API after saving locally (or vice versa).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BUCKET = "editor-projects"


def _client():
    try:
        from db import get_supabase, is_configured

        if not is_configured():
            return None
        return get_supabase()
    except Exception as exc:
        print(f"  [cloud] supabase unavailable: {exc}")
        return None


def _bucket():
    sb = _client()
    if sb is None:
        return None
    try:
        return sb.storage.from_(BUCKET)
    except Exception as exc:
        print(f"  [cloud] bucket error: {exc}")
        return None


def ensure_bucket() -> bool:
    sb = _client()
    if sb is None:
        return False
    try:
        names = {b.name for b in (sb.storage.list_buckets() or [])}
        if BUCKET not in names:
            sb.storage.create_bucket(
                BUCKET,
                options={"public": False, "file_size_limit": 104857600},
            )
        return True
    except Exception as exc:
        print(f"  [cloud] ensure_bucket: {exc}")
        return False


def _upload(path: str, data: bytes, content_type: str) -> bool:
    b = _bucket()
    if b is None:
        return False
    ensure_bucket()
    try:
        # Prefer upsert; if the object already exists and upsert is ignored,
        # update() overwrites without touching sibling files.
        try:
            b.upload(
                path,
                data,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            return True
        except Exception:
            b.update(
                path,
                data,
                file_options={"content-type": content_type},
            )
            return True
    except Exception as exc:
        print(f"  [cloud] upload {path}: {exc}")
        return False


def _download(path: str) -> bytes | None:
    b = _bucket()
    if b is None:
        return None
    try:
        return b.download(path)
    except Exception:
        return None


def _list(prefix: str) -> list[dict[str, Any]]:
    b = _bucket()
    if b is None:
        return []
    try:
        rows = b.list(prefix) or []
        return [r for r in rows if isinstance(r, dict)]
    except Exception as exc:
        print(f"  [cloud] list {prefix}: {exc}")
        return []


def _remove(paths: list[str]) -> None:
    b = _bucket()
    if b is None or not paths:
        return
    try:
        b.remove(paths)
    except Exception as exc:
        print(f"  [cloud] remove: {exc}")


# --- projects -----------------------------------------------------------------


def upsert_project(project: dict[str, Any]) -> bool:
    pid = str(project.get("id") or "").strip()
    if not pid:
        return False
    payload = json.dumps(project, ensure_ascii=False, indent=2).encode("utf-8")
    return _upload(f"projects/{pid}/project.json", payload, "application/json")


def upload_project_asset(project_id: str, filename: str, raw: bytes, content_type: str | None = None) -> bool:
    pid = str(project_id or "").strip()
    name = str(filename or "").strip()
    if not pid or not name:
        return False
    ct = content_type or "application/octet-stream"
    if name.lower().endswith(".mp4"):
        ct = "video/mp4"
    elif name.lower().endswith(".mp3"):
        ct = "audio/mpeg"
    elif name.lower().endswith(".wav"):
        ct = "audio/wav"
    elif name.lower().endswith(".json"):
        ct = "application/json"
    return _upload(f"projects/{pid}/assets/{name}", raw, ct)


def fetch_project(project_id: str) -> dict[str, Any] | None:
    raw = _download(f"projects/{project_id}/project.json")
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def hydrate_project_to_disk(project_id: str, folder: Path) -> dict[str, Any] | None:
    """Download project.json + assets into the local projects/{id} folder."""
    data = fetch_project(project_id)
    if not data:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    assets_folder = folder / "assets"
    assets_folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    for row in data.get("assets") or []:
        if not isinstance(row, dict):
            continue
        filename = str(row.get("filename") or "").strip()
        if not filename:
            continue
        dest = assets_folder / filename
        if dest.is_file() and dest.stat().st_size > 0:
            continue
        blob = _download(f"projects/{project_id}/assets/{filename}")
        if blob:
            dest.write_bytes(blob)
    return data


def list_cloud_project_ids() -> list[str]:
    rows = _list("projects")
    ids: list[str] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        # folder entries often have id/name as the folder name
        if name and name not in (".", "..") and not name.endswith(".json"):
            ids.append(name)
    return ids


def delete_cloud_project(project_id: str) -> None:
    pid = str(project_id or "").strip()
    if not pid:
        return
    paths = [f"projects/{pid}/project.json"]
    for row in _list(f"projects/{pid}/assets"):
        name = str(row.get("name") or "").strip()
        if name:
            paths.append(f"projects/{pid}/assets/{name}")
    _remove(paths)


# --- templates ----------------------------------------------------------------


def upsert_template_meta(meta: dict[str, Any]) -> bool:
    tid = str(meta.get("id") or "").strip()
    if not tid:
        return False
    payload = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
    return _upload(f"templates/{tid}/meta.json", payload, "application/json")


def upload_template_intro(template_id: str, intro_path: Path) -> bool:
    tid = str(template_id or "").strip()
    if not tid or not intro_path.is_file():
        return False
    return _upload(f"templates/{tid}/intro.mp4", intro_path.read_bytes(), "video/mp4")


def fetch_template_meta(template_id: str) -> dict[str, Any] | None:
    raw = _download(f"templates/{template_id}/meta.json")
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def hydrate_template_to_disk(template_id: str, folder: Path) -> dict[str, Any] | None:
    meta = fetch_template_meta(template_id)
    if not meta:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    intro = folder / "intro.mp4"
    if not intro.is_file() or intro.stat().st_size < 1000:
        blob = _download(f"templates/{template_id}/intro.mp4")
        if blob:
            intro.write_bytes(blob)
    return meta


def list_cloud_template_ids() -> list[str]:
    rows = _list("templates")
    ids: list[str] = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        if name and name not in (".", ".."):
            ids.append(name)
    return ids


def delete_cloud_template(template_id: str) -> None:
    tid = str(template_id or "").strip()
    if not tid:
        return
    _remove([f"templates/{tid}/meta.json", f"templates/{tid}/intro.mp4"])
