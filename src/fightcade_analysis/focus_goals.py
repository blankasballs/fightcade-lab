"""Pinned Focus Goals (max 3 active per owner) & Graduated Progress History.

Persisted atomically in `data/selections/focus_goals.jsonl`.
Supports optional `owner` scoping so multi-user / hosted tiers require zero schema rewrites.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import threading
from typing import Any
import uuid

from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR
from fightcade_analysis.select_queue import _read_jsonl, _write_jsonl_atomic

MAX_ACTIVE_GOALS = 3
VALID_GOAL_STATUSES: frozenset[str] = frozenset({"active", "graduated", "archived"})
_GOAL_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_GOALS_LOCK = threading.Lock()


def focus_goals_jsonl_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "selections" / "focus_goals.jsonl"


def _sanitize_tags(raw_tags: Any) -> list[str]:
    if not raw_tags:
        return []
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",")]
    if not isinstance(raw_tags, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for t in raw_tags:
        clean = " ".join(str(t or "").strip().split())[:40]
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            out.append(clean)
        if len(out) >= 12:
            break
    return out


def _sanitize_text(raw: Any, *, max_len: int = 160) -> str:
    return " ".join(str(raw or "").replace("\r", " ").replace("\n", " ").split())[:max_len]


def _sanitize_multiline(raw: Any, *, max_len: int = 1200) -> str:
    return str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:max_len]


def _normalize_goal(raw: dict[str, Any]) -> dict[str, Any]:
    now_iso = datetime.now().isoformat(timespec="seconds")
    status = str(raw.get("status") or "active").strip().lower()
    if status not in VALID_GOAL_STATUSES:
        status = "active"
    return {
        "id": str(raw.get("id") or ""),
        "owner": str(raw.get("owner") or "").strip(),
        "title": _sanitize_text(raw.get("title"), max_len=160),
        "description": _sanitize_multiline(raw.get("description"), max_len=1200),
        "status": status,
        "matchup_char": _sanitize_text(raw.get("matchup_char"), max_len=40),
        "tags": _sanitize_tags(raw.get("tags")),
        "playlist_id": str(raw.get("playlist_id") or "").strip() or None,
        "source_insight_id": str(raw.get("source_insight_id") or "").strip() or None,
        "trend_hint": _sanitize_text(raw.get("trend_hint"), max_len=64),
        "created_at": str(raw.get("created_at") or now_iso),
        "updated_at": str(raw.get("updated_at") or now_iso),
        "graduated_at": str(raw.get("graduated_at") or "").strip() or None,
    }


def _owner_matches(rec_owner: str, req_owner: str | None) -> bool:
    if not req_owner:
        return True
    if not rec_owner:
        return True
    return rec_owner.strip().lower() == req_owner.strip().lower()


def _active_count_for_owner(items: list[dict[str, Any]], owner: str) -> int:
    owner_low = (owner or "").strip().lower()
    count = 0
    for it in items:
        if str(it.get("status") or "active") != "active":
            continue
        it_owner = str(it.get("owner") or "").strip().lower()
        if not owner_low or not it_owner or it_owner == owner_low:
            count += 1
    return count


def list_focus_goals(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    status: str = "all",
    owner: str | None = None,
) -> list[dict[str, Any]]:
    st_filter = (status or "all").strip().lower()
    if st_filter != "all" and st_filter not in VALID_GOAL_STATUSES:
        raise ValueError(f"Invalid status filter: {status!r}")
    with _GOALS_LOCK:
        raw_items = _read_jsonl(focus_goals_jsonl_path(data_dir))
    goals = [_normalize_goal(it) for it in raw_items if it.get("id")]
    if owner:
        goals = [g for g in goals if _owner_matches(g["owner"], owner)]
    if st_filter != "all":
        goals = [g for g in goals if g["status"] == st_filter]
    # Active goals sorted oldest-first (stable pin order); graduated newest-first
    if st_filter == "graduated":
        goals.sort(key=lambda g: str(g.get("graduated_at") or g.get("updated_at") or ""), reverse=True)
    else:
        status_rank = {"active": 0, "graduated": 1, "archived": 2}
        goals.sort(key=lambda g: (status_rank.get(g["status"], 9), str(g.get("created_at") or "")))
    return goals


def get_focus_goal(goal_id: str, data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any] | None:
    with _GOALS_LOCK:
        raw_items = _read_jsonl(focus_goals_jsonl_path(data_dir))
    for it in raw_items:
        if str(it.get("id")) == str(goal_id):
            return _normalize_goal(it)
    return None


def create_focus_goal(
    payload: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Focus goal payload must be a JSON object")
    title = _sanitize_text(payload.get("title"), max_len=160)
    if not title:
        raise ValueError("Focus goal title is required")

    status = str(payload.get("status") or "active").strip().lower()
    if status not in VALID_GOAL_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(VALID_GOAL_STATUSES))}")

    owner = str(payload.get("owner") or "").strip()
    now_iso = datetime.now().isoformat(timespec="seconds")
    goal_id = f"goal_{uuid.uuid4().hex[:8]}"

    record = _normalize_goal(
        {
            "id": goal_id,
            "owner": owner,
            "title": title,
            "description": payload.get("description") or "",
            "status": status,
            "matchup_char": payload.get("matchup_char") or "",
            "tags": payload.get("tags") or [],
            "playlist_id": payload.get("playlist_id") or None,
            "source_insight_id": payload.get("source_insight_id") or None,
            "trend_hint": payload.get("trend_hint") or "",
            "created_at": now_iso,
            "updated_at": now_iso,
            "graduated_at": now_iso if status == "graduated" else None,
        }
    )

    with _GOALS_LOCK:
        path = focus_goals_jsonl_path(data_dir)
        items = _read_jsonl(path)
        if status == "active" and _active_count_for_owner(items, owner) >= MAX_ACTIVE_GOALS:
            raise ValueError(
                f"Maximum of {MAX_ACTIVE_GOALS} active Focus Goals reached. "
                "Graduate or archive an active goal before pinning a new one."
            )
        items.append(record)
        _write_jsonl_atomic(path, items)
    return record


def update_focus_goal(
    goal_id: str,
    patch: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any] | None:
    if not _GOAL_ID_RE.match(str(goal_id or "")):
        raise ValueError("Invalid focus goal id")
    if not isinstance(patch, dict):
        raise ValueError("Patch payload must be a JSON object")

    now_iso = datetime.now().isoformat(timespec="seconds")
    with _GOALS_LOCK:
        path = focus_goals_jsonl_path(data_dir)
        items = _read_jsonl(path)
        updated: dict[str, Any] | None = None
        for idx, raw in enumerate(items):
            if str(raw.get("id")) != str(goal_id):
                continue
            cur = _normalize_goal(raw)
            old_status = cur["status"]

            if "title" in patch:
                new_title = _sanitize_text(patch["title"], max_len=160)
                if not new_title:
                    raise ValueError("Focus goal title cannot be empty")
                cur["title"] = new_title
            if "description" in patch:
                cur["description"] = _sanitize_multiline(patch["description"], max_len=1200)
            if "matchup_char" in patch:
                cur["matchup_char"] = _sanitize_text(patch["matchup_char"], max_len=40)
            if "tags" in patch:
                cur["tags"] = _sanitize_tags(patch["tags"])
            if "playlist_id" in patch:
                cur["playlist_id"] = str(patch["playlist_id"] or "").strip() or None
            if "source_insight_id" in patch:
                cur["source_insight_id"] = str(patch["source_insight_id"] or "").strip() or None
            if "trend_hint" in patch:
                cur["trend_hint"] = _sanitize_text(patch["trend_hint"], max_len=64)
            if "owner" in patch:
                cur["owner"] = str(patch["owner"] or "").strip()
            if "status" in patch:
                new_status = str(patch["status"] or "").strip().lower()
                if new_status not in VALID_GOAL_STATUSES:
                    raise ValueError(f"Invalid status: {new_status}")
                if new_status == "active" and old_status != "active":
                    other_items = [it for j, it in enumerate(items) if j != idx]
                    if _active_count_for_owner(other_items, cur["owner"]) >= MAX_ACTIVE_GOALS:
                        raise ValueError(
                            f"Maximum of {MAX_ACTIVE_GOALS} active Focus Goals reached. "
                            "Graduate or archive an active goal first."
                        )
                    cur["graduated_at"] = None
                elif new_status == "graduated" and old_status != "graduated":
                    cur["graduated_at"] = now_iso
                cur["status"] = new_status

            cur["updated_at"] = now_iso
            items[idx] = cur
            updated = dict(cur)
            break

        if updated is not None:
            _write_jsonl_atomic(path, items)
    return updated


def delete_focus_goal(goal_id: str, *, data_dir: Path = DEFAULT_DATA_DIR) -> bool:
    if not _GOAL_ID_RE.match(str(goal_id or "")):
        raise ValueError("Invalid focus goal id")
    with _GOALS_LOCK:
        path = focus_goals_jsonl_path(data_dir)
        items = _read_jsonl(path)
        kept = [it for it in items if str(it.get("id")) != str(goal_id)]
        if len(kept) == len(items):
            return False
        _write_jsonl_atomic(path, kept)
    return True


def clear_playlist_reference(playlist_id: str, *, data_dir: Path = DEFAULT_DATA_DIR) -> int:
    """Clear dangling `playlist_id` references on Focus Goals when a playlist is deleted."""
    pid = str(playlist_id or "").strip()
    if not pid:
        return 0
    cleared = 0
    now_iso = datetime.now().isoformat(timespec="seconds")
    with _GOALS_LOCK:
        path = focus_goals_jsonl_path(data_dir)
        items = _read_jsonl(path)
        for it in items:
            if str(it.get("playlist_id") or "") == pid:
                it["playlist_id"] = None
                it["updated_at"] = now_iso
                cleared += 1
        if cleared:
            _write_jsonl_atomic(path, items)
    return cleared
