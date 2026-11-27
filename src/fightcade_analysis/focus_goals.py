"""Thin Focus Goals adapter backed by unified `playlists.py` (`is_focus=True`).

Preserves `/api/focus-goals*` API ergonomics while storing all Focus Goals and
Playlists in one single source of truth (`data/selections/playlists.jsonl`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR
from fightcade_analysis.playlists import (
    MAX_ACTIVE_FOCUSES,
    VALID_FOCUS_STATUSES,
    create_playlist,
    delete_playlist,
    get_playlist,
    legacy_focus_goals_jsonl_path,
    list_playlists,
    update_playlist,
)

MAX_ACTIVE_GOALS = MAX_ACTIVE_FOCUSES
VALID_GOAL_STATUSES: frozenset[str] = frozenset({"active", "graduated", "archived"})


def focus_goals_jsonl_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    """Path to the legacy `focus_goals.jsonl` file (auto-migrated into `playlists.jsonl`)."""
    return legacy_focus_goals_jsonl_path(data_dir)


def _playlist_to_goal_view(pl: dict[str, Any]) -> dict[str, Any]:
    items = list(pl.get("items") or [])
    ready_cnt = int(pl.get("ready_drills_count", pl.get("takeover_ready_count", 0)) or 0)
    status = str(pl.get("focus_status") or ("active" if pl.get("is_focus") else "archived"))
    return {
        "id": pl["id"],
        "owner": pl.get("owner", ""),
        "title": pl.get("name", ""),
        "name": pl.get("name", ""),
        "description": pl.get("description", ""),
        "status": status,
        "focus_status": pl.get("focus_status"),
        "is_focus": bool(pl.get("is_focus", False)),
        "kind": pl.get("kind", "drill"),
        "shareable": bool(pl.get("shareable", False)),
        "matchup_char": pl.get("matchup_char", ""),
        "tags": list(pl.get("tags") or []),
        "source_insight_id": pl.get("source_insight_id"),
        "trend_hint": pl.get("trend_hint", ""),
        "items": items,
        "item_count": int(pl.get("item_count", len(items))),
        "ready_drills_count": ready_cnt,
        "takeover_ready_count": ready_cnt,
        "pro_clips_count": int(pl.get("pro_clips_count", 0) or 0),
        "created_at": pl.get("created_at"),
        "updated_at": pl.get("updated_at"),
        "graduated_at": pl.get("graduated_at"),
    }


def list_focus_goals(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    status: str = "all",
    owner: str | None = None,
    username: str | None = None,
    enrich: bool = True,
) -> list[dict[str, Any]]:
    st_filter = (status or "all").strip().lower()
    if st_filter != "all" and st_filter not in VALID_GOAL_STATUSES:
        raise ValueError(f"Invalid status filter: {status!r}")
    if st_filter == "archived":
        # Archived goals are unpinned to Reference Playlists
        return []

    fs_param = st_filter if st_filter in VALID_FOCUS_STATUSES else None
    pls = list_playlists(
        data_dir=data_dir,
        owner=owner,
        username=username or owner,
        enrich=enrich,
        is_focus=True,
        focus_status=fs_param,
    )
    goals = [_playlist_to_goal_view(p) for p in pls]

    if st_filter == "graduated":
        goals.sort(key=lambda g: str(g.get("graduated_at") or g.get("updated_at") or ""), reverse=True)
    else:
        status_rank = {"active": 0, "graduated": 1}
        goals.sort(key=lambda g: (status_rank.get(g["status"], 9), str(g.get("created_at") or "")))
    return goals


def get_focus_goal(
    goal_id: str,
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    username: str | None = None,
    enrich: bool = True,
) -> dict[str, Any] | None:
    pl = get_playlist(goal_id, data_dir=data_dir, username=username, enrich=enrich)
    if pl is None or not pl.get("is_focus"):
        return None
    return _playlist_to_goal_view(pl)


def create_focus_goal(
    payload: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Focus goal payload must be a JSON object")
    title = str(payload.get("title") or payload.get("name") or "").strip()
    if not title:
        raise ValueError("Focus goal title is required")

    raw_status = str(payload.get("status") or payload.get("focus_status") or "active").strip().lower()
    if raw_status not in VALID_GOAL_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(VALID_GOAL_STATUSES))}")

    is_focus = raw_status != "archived"
    focus_status: str | None = raw_status if is_focus else None

    pl_payload: dict[str, Any] = {
        "owner": payload.get("owner") or "",
        "name": title,
        "description": payload.get("description") or "",
        "kind": payload.get("kind") or "drill",
        "shareable": bool(payload.get("shareable", False)),
        "is_focus": is_focus,
        "focus_status": focus_status,
        "matchup_char": payload.get("matchup_char") or "",
        "tags": payload.get("tags") or [],
        "source_insight_id": payload.get("source_insight_id") or None,
        "trend_hint": payload.get("trend_hint") or "",
        "items": payload.get("items") or [],
    }
    created = create_playlist(pl_payload, data_dir=data_dir, id_prefix="goal")
    return _playlist_to_goal_view(created)


def update_focus_goal(
    goal_id: str,
    patch: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any] | None:
    if not isinstance(patch, dict):
        raise ValueError("Patch payload must be a JSON object")

    pl_patch: dict[str, Any] = {}
    if "title" in patch:
        pl_patch["name"] = patch["title"]
    elif "name" in patch:
        pl_patch["name"] = patch["name"]

    for field in ("description", "matchup_char", "tags", "source_insight_id", "trend_hint", "owner", "items", "kind", "shareable"):
        if field in patch:
            pl_patch[field] = patch[field]

    if "is_focus" in patch:
        pl_patch["is_focus"] = patch["is_focus"]

    if "status" in patch or "focus_status" in patch:
        raw_st = patch["status"] if "status" in patch else patch["focus_status"]
        if raw_st is None or str(raw_st).strip() == "":
            pl_patch["is_focus"] = False
            pl_patch["focus_status"] = None
        else:
            st = str(raw_st).strip().lower()
            if st not in VALID_GOAL_STATUSES:
                raise ValueError(f"Invalid status: {st}")
            if st == "archived":
                pl_patch["is_focus"] = False
                pl_patch["focus_status"] = None
            else:
                pl_patch["is_focus"] = True
                pl_patch["focus_status"] = st

    updated = update_playlist(goal_id, pl_patch, data_dir=data_dir)
    if updated is None:
        return None
    return _playlist_to_goal_view(updated)


def delete_focus_goal(goal_id: str, *, data_dir: Path = DEFAULT_DATA_DIR) -> bool:
    return delete_playlist(goal_id, data_dir=data_dir)


def clear_playlist_reference(playlist_id: str, *, data_dir: Path = DEFAULT_DATA_DIR) -> int:
    """No-op compatibility stub (Focus Goals are unified directly into Playlists)."""
    return 0
