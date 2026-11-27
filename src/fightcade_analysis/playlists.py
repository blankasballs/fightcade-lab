"""Unified Playlists & Pinned Focus Goals ("Focus Goals are special Playlists").

Persisted atomically in `data/selections/playlists.jsonl`.
Every collection supports:
  - Reference Playlists (`is_focus=False, focus_status=None`)
  - Active Focuses (`is_focus=True, focus_status="active"`, capped at 3 per owner)
  - Graduated Focuses (`is_focus=True, focus_status="graduated"`, clips preserved)
  - 0-clip pure mental-cue Focus Goals (`items=[]`)

Automatically migrates legacy `data/selections/focus_goals.jsonl` once into
`playlists.jsonl` and archives the legacy file to `focus_goals.jsonl.migrated`.
"""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable
import uuid

from fightcade_analysis.annotations import list_annotations
from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR
from fightcade_analysis.match_catalog import detect_default_username, list_matches
from fightcade_analysis.notes_hub import _resolve_context_for_item, _round_video_preview
from fightcade_analysis.round_renderer import bake_drill_savestate, list_cached_round_savestates
from fightcade_analysis.select_queue import _read_jsonl, _write_jsonl_atomic, load_queue

MAX_ACTIVE_FOCUSES = 3
VALID_FOCUS_STATUSES: frozenset[str] = frozenset({"active", "graduated"})
VALID_PLAYLIST_KINDS: frozenset[str] = frozenset({"drill", "study", "matchup", "pro_reel"})
VALID_ITEM_SOURCES: frozenset[str] = frozenset({"annotation", "select_mark", "pro_clip", "moment", "takeover_response"})
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_QUARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_PLAYLISTS_LOCK = threading.Lock()


def playlists_jsonl_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "selections" / "playlists.jsonl"


def legacy_focus_goals_jsonl_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "selections" / "focus_goals.jsonl"


def _sanitize_text(raw: Any, *, max_len: int = 160) -> str:
    return " ".join(str(raw or "").replace("\r", " ").replace("\n", " ").split())[:max_len]


def _sanitize_multiline(raw: Any, *, max_len: int = 1200) -> str:
    return str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()[:max_len]


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


def _normalize_item(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Playlist item must be an object")
    qid = str(raw.get("quark_id") or "").strip()
    if not _QUARK_ID_RE.match(qid):
        raise ValueError(f"Invalid quark_id in playlist item: {qid!r}")
    source = str(raw.get("source") or "moment").strip().lower()
    if source not in VALID_ITEM_SOURCES:
        source = "moment"
    try:
        round_id = int(raw.get("round_id") or 0)
        persp = int(raw.get("perspective") or 1)
        start_gf = int(raw.get("start_gf") or raw.get("anchor_gf") or 0)
        anchor_gf = int(raw.get("anchor_gf") or start_gf or 0)
        end_gf = int(raw.get("end_gf") or (anchor_gf + 90 if anchor_gf > 0 else 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("round_id, perspective, start_gf, end_gf, anchor_gf must be integers") from exc
    if persp not in (1, 2):
        persp = 1
    if anchor_gf <= 0 and start_gf > 0:
        anchor_gf = start_gf
    if start_gf <= 0 and anchor_gf > 0:
        start_gf = max(1, anchor_gf - 180)
    if end_gf <= start_gf and start_gf > 0:
        end_gf = start_gf + 120

    item_id = str(raw.get("item_id") or raw.get("id") or "").strip()
    if not item_id:
        short_qid = qid.split("-")[-1] if "-" in qid else qid[-6:]
        prefix = "pro" if source == "pro_clip" else "item"
        item_id = f"{prefix}_{short_qid}_gf{anchor_gf}_{uuid.uuid4().hex[:4]}"

    item_out: dict[str, Any] = {
        "item_id": item_id,
        "source": source,
        "quark_id": qid,
        "round_id": round_id,
        "perspective": persp,
        "start_gf": start_gf,
        "end_gf": end_gf,
        "anchor_gf": anchor_gf,
        "label": _sanitize_text(raw.get("label"), max_len=160),
        "note": _sanitize_multiline(raw.get("note"), max_len=1000),
        "tags": _sanitize_tags(raw.get("tags")),
        "added_at": str(raw.get("added_at") or datetime.now().isoformat(timespec="seconds")),
    }
    if raw.get("parent_moment_id"):
        item_out["parent_moment_id"] = str(raw["parent_moment_id"]).strip()
    if raw.get("response_clip_url"):
        item_out["response_clip_url"] = str(raw["response_clip_url"]).strip()
    if raw.get("response_overlay_url"):
        item_out["response_overlay_url"] = str(raw["response_overlay_url"]).strip()
    return item_out


def _normalize_playlist(raw: dict[str, Any]) -> dict[str, Any]:
    now_iso = datetime.now().isoformat(timespec="seconds")
    kind = str(raw.get("kind") or "drill").strip().lower()
    if kind not in VALID_PLAYLIST_KINDS:
        kind = "drill"

    is_focus = bool(raw.get("is_focus", False))
    raw_fs = raw.get("focus_status")
    focus_status: str | None = None
    if is_focus:
        fs_str = str(raw_fs or "active").strip().lower()
        focus_status = fs_str if fs_str in VALID_FOCUS_STATUSES else "active"
    else:
        focus_status = None

    graduated_at = str(raw.get("graduated_at") or "").strip() or None
    if not is_focus or focus_status != "graduated":
        graduated_at = None

    raw_items = raw.get("items") or []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw_items, list):
        for it in raw_items:
            if not isinstance(it, dict):
                continue
            try:
                norm = _normalize_item(it)
            except ValueError:
                continue
            if norm["item_id"] not in seen:
                seen.add(norm["item_id"])
                items.append(norm)

    return {
        "id": str(raw.get("id") or ""),
        "owner": str(raw.get("owner") or "").strip(),
        "name": _sanitize_text(raw.get("name") or raw.get("title"), max_len=160),
        "description": _sanitize_multiline(raw.get("description"), max_len=1200),
        "kind": kind,
        "shareable": bool(raw.get("shareable", False)),
        "is_focus": is_focus,
        "focus_status": focus_status,
        "matchup_char": _sanitize_text(raw.get("matchup_char"), max_len=40),
        "tags": _sanitize_tags(raw.get("tags")),
        "source_insight_id": str(raw.get("source_insight_id") or "").strip() or None,
        "trend_hint": _sanitize_text(raw.get("trend_hint"), max_len=64),
        "graduated_at": graduated_at,
        "items": items,
        "created_at": str(raw.get("created_at") or now_iso),
        "updated_at": str(raw.get("updated_at") or now_iso),
    }


def _owner_matches(rec_owner: str, req_owner: str | None) -> bool:
    if not req_owner:
        return True
    if not rec_owner:
        return True
    return rec_owner.strip().lower() == req_owner.strip().lower()


def _active_focus_count_for_owner(records: list[dict[str, Any]], owner: str) -> int:
    owner_low = (owner or "").strip().lower()
    count = 0
    for it in records:
        if not bool(it.get("is_focus", False)):
            continue
        if str(it.get("focus_status") or "") != "active":
            continue
        it_owner = str(it.get("owner") or "").strip().lower()
        if not owner_low or not it_owner or it_owner == owner_low:
            count += 1
    return count


def _migrate_legacy_focus_goals_unlocked(data_root: Path) -> bool:
    """One-time idempotent migration from `focus_goals.jsonl` into `playlists.jsonl`.

    Must be called while holding `_PLAYLISTS_LOCK`.
    Returns True if migration ran and archived `focus_goals.jsonl` -> `focus_goals.jsonl.migrated`.
    """
    legacy_path = legacy_focus_goals_jsonl_path(data_root)
    if not legacy_path.is_file():
        return False

    legacy_goals = _read_jsonl(legacy_path)
    pl_path = playlists_jsonl_path(data_root)
    records = [_normalize_playlist(r) for r in _read_jsonl(pl_path) if r.get("id")]
    idx_by_id: dict[str, int] = {str(r["id"]): i for i, r in enumerate(records)}

    now_iso = datetime.now().isoformat(timespec="seconds")

    for g in legacy_goals:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("id") or "").strip()
        g_title = _sanitize_text(g.get("title") or g.get("name"), max_len=160)
        if not gid and not g_title:
            continue
        if not gid:
            gid = f"goal_{uuid.uuid4().hex[:8]}"

        raw_status = str(g.get("status") or "active").strip().lower()
        if raw_status == "graduated":
            is_focus = True
            focus_status: str | None = "graduated"
        elif raw_status == "archived":
            is_focus = False
            focus_status = None
        else:
            is_focus = True
            focus_status = "active"

        g_desc = _sanitize_multiline(g.get("description"), max_len=1200)
        g_matchup = _sanitize_text(g.get("matchup_char"), max_len=40)
        g_tags = _sanitize_tags(g.get("tags"))
        g_src_ins = str(g.get("source_insight_id") or "").strip() or None
        g_trend = _sanitize_text(g.get("trend_hint"), max_len=64)
        g_grad = str(g.get("graduated_at") or "").strip() or (now_iso if focus_status == "graduated" else None)
        g_owner = str(g.get("owner") or "").strip()
        g_created = str(g.get("created_at") or now_iso)
        g_updated = str(g.get("updated_at") or g_created)

        linked_pid = str(g.get("playlist_id") or "").strip()
        if linked_pid and linked_pid in idx_by_id:
            # Merge focus fields onto the existing linked playlist (zero duplication!)
            cur = records[idx_by_id[linked_pid]]
            cur["is_focus"] = is_focus
            cur["focus_status"] = focus_status
            if g_title and (not cur["name"] or len(g_title) > len(cur["name"])):
                cur["name"] = g_title
            if g_desc and (not cur["description"] or len(g_desc) > len(cur["description"])):
                cur["description"] = g_desc
            if g_matchup and not cur["matchup_char"]:
                cur["matchup_char"] = g_matchup
            if g_tags:
                cur["tags"] = _sanitize_tags([*(cur.get("tags") or []), *g_tags])
            if g_src_ins and not cur["source_insight_id"]:
                cur["source_insight_id"] = g_src_ins
            if g_trend and not cur["trend_hint"]:
                cur["trend_hint"] = g_trend
            if focus_status == "graduated":
                cur["graduated_at"] = g_grad
            else:
                cur["graduated_at"] = None
            if g_owner and not cur["owner"]:
                cur["owner"] = g_owner
            records[idx_by_id[linked_pid]] = cur
        elif gid not in idx_by_id:
            rec = _normalize_playlist(
                {
                    "id": gid,
                    "owner": g_owner,
                    "name": g_title or "Focus Goal",
                    "description": g_desc,
                    "kind": "drill",
                    "shareable": False,
                    "is_focus": is_focus,
                    "focus_status": focus_status,
                    "matchup_char": g_matchup,
                    "tags": g_tags,
                    "source_insight_id": g_src_ins,
                    "trend_hint": g_trend,
                    "graduated_at": g_grad,
                    "items": [],
                    "created_at": g_created,
                    "updated_at": g_updated,
                }
            )
            idx_by_id[gid] = len(records)
            records.append(rec)

    _write_jsonl_atomic(pl_path, records)
    migrated_path = legacy_path.with_name("focus_goals.jsonl.migrated")
    os.replace(legacy_path, migrated_path)
    return True


def _load_records_unlocked(data_root: Path) -> list[dict[str, Any]]:
    _migrate_legacy_focus_goals_unlocked(data_root)
    raw_items = _read_jsonl(playlists_jsonl_path(data_root))
    return [_normalize_playlist(it) for it in raw_items if it.get("id")]


def _is_valid_fs(path: Path | None) -> bool:
    return bool(path and path.exists() and path.stat().st_size > 4096)


def _enrich_playlist_items(
    playlists: list[dict[str, Any]],
    *,
    username: str,
    data_root: Path,
) -> list[dict[str, Any]]:
    cat = list_matches(username=username, data_dir=data_root)
    match_by_qid = {str(m["quark_id"]): m for m in cat.get("matches") or []}
    manifest_cache: dict[str, dict[str, Any] | None] = {}
    round_cache_exists: dict[str, bool] = {}

    ann_by_id = {str(a.get("id")): a for a in list_annotations(data_root) if a.get("id")}
    queue_by_id = {str(q.get("id")): q for q in load_queue(data_root, include_dismissed=True) if q.get("id")}

    enriched_playlists: list[dict[str, Any]] = []
    for pl in playlists:
        enriched_items: list[dict[str, Any]] = []
        ready_count = 0
        pro_count = 0

        for raw_it in pl.get("items") or []:
            it = dict(raw_it)
            iid = str(it["item_id"])
            qid = str(it["quark_id"])
            src = str(it.get("source") or "moment")
            if src == "pro_clip":
                pro_count += 1

            ann = ann_by_id.get(iid)
            qi = queue_by_id.get(iid)

            rid = (
                int(it.get("round_id") or 0)
                or (int(ann.get("round_id") or 0) if ann else 0)
                or (int(qi.get("round_id") or 0) if qi else 0)
                or None
            )
            persp = int(it.get("perspective") or 1)
            ctx = _resolve_context_for_item(
                quark_id=qid,
                item_perspective=persp,
                round_id=rid,
                username=username,
                match_by_qid=match_by_qid,
                manifest_cache=manifest_cache,
                data_root=data_root,
            )
            prev = _round_video_preview(
                ctx["round_doc"],
                quark_id=qid,
                start_gf=it.get("start_gf"),
                end_gf=it.get("end_gf"),
                anchor_gf=it.get("anchor_gf"),
                data_root=data_root,
            )

            has_video = bool(prev["has_video"])
            video_url = prev["video_url"]
            overlay_url = prev.get("overlay_url")
            clip_start_s = prev["clip_start_s"]
            clip_end_s = prev["clip_end_s"]
            round_start_gf = prev["round_start_gf"]
            round_end_gf = prev["round_end_gf"]
            round_fps = prev["round_fps"]
            anchor_s = prev["anchor_s"]
            is_standalone_clip = False
            is_response_clip = False

            # First-class Takeover Response clip: plays self-contained response MP4 + overlay.json from 0.0s
            resp_clip_url = str((ann or {}).get("response_clip_url") or it.get("response_clip_url") or "").strip() or None
            resp_ov_url = str((ann or {}).get("response_overlay_url") or it.get("response_overlay_url") or "").strip() or None
            if (src == "takeover_response" or (ann and ann.get("source") == "takeover_response") or resp_clip_url) and resp_clip_url:
                resp_mp4 = data_root / Path(resp_clip_url.lstrip("/"))
                if resp_mp4.is_file():
                    is_response_clip = True
                    has_video = True
                    video_url = resp_clip_url
                    overlay_url = (
                        resp_ov_url
                        if (resp_ov_url and (data_root / Path(resp_ov_url.lstrip("/"))).is_file())
                        else None
                    )
                    n_frames = max(
                        1,
                        int(
                            (ann or {}).get("frame_count")
                            or (int((ann or it).get("end_gf") or 0) - int((ann or it).get("start_gf") or 0) + 1)
                        ),
                    )
                    round_start_gf = int((ann or it).get("start_gf") or 0)
                    round_end_gf = int((ann or it).get("end_gf") or round_start_gf)
                    round_fps = 60.0
                    clip_start_s = 0.0
                    clip_end_s = round(max(0.5, (n_frames - 1) / 60.0), 3)
                    anchor_s = 0.0

            # Fallback to 6s standalone clip (e.g. pro clips from /api/pro-search or reports)
            if not has_video:
                for gf_cand in (int(it.get("anchor_gf") or 0), int(it.get("start_gf") or 0)):
                    if gf_cand <= 0:
                        continue
                    clip_p = data_root / "clips" / f"{qid}_gf{gf_cand}.mp4"
                    if clip_p.is_file():
                        has_video = True
                        video_url = f"/clips/{clip_p.name}"
                        clip_start_s = 0.0
                        clip_end_s = 6.0
                        is_standalone_clip = True
                        break

            # Savestate / drill readiness
            sel_fs = data_root / "selections" / f"{iid}.fs"
            sav_anchor = data_root / "savestates" / f"{qid}_gf{int(it.get('anchor_gf') or 0)}.fs"
            sav_start = data_root / "savestates" / f"{qid}_gf{int(it.get('start_gf') or 0)}.fs"
            ann_fs = Path(str(ann["savestate"])) if (ann and ann.get("savestate")) else None
            q_fs = Path(str(qi["savestate"])) if (qi and qi.get("savestate")) else None

            savestate_ready = any(
                _is_valid_fs(p) for p in (sel_fs, ann_fs, q_fs, sav_anchor, sav_start)
            )
            selection_id = iid if (_is_valid_fs(sel_fs) or _is_valid_fs(ann_fs) or _is_valid_fs(q_fs)) else None

            if qid not in round_cache_exists:
                round_cache_exists[qid] = len(list_cached_round_savestates(qid, data_dir=data_root)) > 0
            can_bake_drill = bool(round_cache_exists[qid])
            can_launch_drill = bool(savestate_ready or can_bake_drill)
            if savestate_ready:
                ready_count += 1

            if ann is not None:
                note_text = str(ann.get("note") or "").strip()
            elif qi is not None:
                note_text = str(qi.get("note") or "").strip()
            else:
                note_text = str(it.get("note") or "").strip()

            if ann is not None:
                tags = list(ann.get("tags") or [])
            elif qi is not None:
                tags = list(qi.get("tags") or [])
            else:
                tags = list(it.get("tags") or [])

            edit_source = (
                "takeover_response"
                if (ann is not None and (is_response_clip or ann.get("source") == "takeover_response"))
                else ("annotation" if ann is not None else ("select_mark" if qi is not None else None))
            )

            r_label = (
                f"M{prev['match_id']} · R{prev['match_round']}"
                if prev["match_id"]
                else (f"R{rid}" if rid else ("Top-Rank Clip" if src == "pro_clip" else "Moment"))
            )

            it.update(
                {
                    "round_id": rid,
                    "round_label": r_label,
                    "perspective": ctx["perspective"],
                    "self_player_name": ctx["self_player_name"],
                    "opp_player_name": ctx["opp_player_name"],
                    "self_rank_letter": ctx["self_rank_letter"],
                    "opp_rank_letter": ctx["opp_rank_letter"],
                    "self_char_name": ctx["self_char_name"],
                    "opp_char_name": ctx["opp_char_name"],
                    "date_str": ctx["date_str"],
                    "note": note_text,
                    "tags": tags,
                    "kind": str((ann or qi or {}).get("kind") or ("response" if is_response_clip else ("pro_clip" if src == "pro_clip" else "note"))),
                    "is_response_clip": is_response_clip,
                    "parent_moment_id": (ann or {}).get("parent_moment_id") or it.get("parent_moment_id"),
                    "hp_delta_self": (ann or {}).get("hp_delta_self"),
                    "hp_delta_opp": (ann or {}).get("hp_delta_opp"),
                    "editable": edit_source is not None,
                    "edit_source": edit_source,
                    "cloud_moment_id": (ann or qi or {}).get("cloud_moment_id"),
                    "cloud_share_token": (ann or qi or {}).get("cloud_share_token"),
                    "cloud_share_url": (ann or qi or {}).get("cloud_share_url"),
                    "cloud_visibility": (ann or qi or {}).get("cloud_visibility"),
                    "shared_caption": str((ann or qi or {}).get("shared_caption") or ""),
                    "include_note_on_share": bool((ann or qi or {}).get("include_note_on_share", False)),
                    "hide_player_names": bool((ann or qi or {}).get("hide_player_names", False)),
                    "has_video": has_video,
                    "video_url": video_url,
                    "overlay_url": overlay_url,
                    "is_standalone_clip": is_standalone_clip,
                    "round_start_gf": round_start_gf,
                    "round_end_gf": round_end_gf,
                    "round_fps": round_fps,
                    "clip_start_s": clip_start_s,
                    "clip_end_s": clip_end_s,
                    "anchor_s": anchor_s,
                    "savestate_ready": savestate_ready,
                    "can_bake_drill": can_bake_drill,
                    "can_launch_drill": can_launch_drill,
                    "selection_id": selection_id,
                }
            )
            enriched_items.append(it)

        pl_copy = dict(pl)
        pl_copy["items"] = enriched_items
        pl_copy["item_count"] = len(enriched_items)
        pl_copy["takeover_ready_count"] = ready_count
        pl_copy["ready_drills_count"] = ready_count
        pl_copy["pro_clips_count"] = pro_count
        enriched_playlists.append(pl_copy)

    return enriched_playlists


def list_playlists(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    owner: str | None = None,
    username: str | None = None,
    enrich: bool = True,
    is_focus: bool | None = None,
    focus_status: str | None = None,
) -> list[dict[str, Any]]:
    data_root = data_dir.resolve()
    with _PLAYLISTS_LOCK:
        pls = _load_records_unlocked(data_root)
    if owner:
        pls = [p for p in pls if _owner_matches(p["owner"], owner)]
    if is_focus is not None:
        pls = [p for p in pls if bool(p.get("is_focus", False)) is bool(is_focus)]
    if focus_status is not None:
        fs_low = str(focus_status).strip().lower()
        if fs_low not in VALID_FOCUS_STATUSES:
            raise ValueError(f"Invalid focus_status filter: {focus_status!r}")
        pls = [p for p in pls if p.get("is_focus") and p.get("focus_status") == fs_low]

    if is_focus is True and focus_status == "active":
        pls.sort(key=lambda p: str(p.get("created_at") or ""))
    elif is_focus is True and focus_status == "graduated":
        pls.sort(key=lambda p: str(p.get("graduated_at") or p.get("updated_at") or ""), reverse=True)
    else:
        pls.sort(key=lambda p: str(p.get("updated_at") or p.get("created_at") or ""), reverse=True)

    if not enrich:
        for p in pls:
            p["item_count"] = len(p.get("items") or [])
            p.setdefault("ready_drills_count", 0)
            p.setdefault("takeover_ready_count", 0)
        return pls
    uname = (username or owner or "").strip() or detect_default_username(data_root)
    return _enrich_playlist_items(pls, username=uname, data_root=data_root)


def get_playlist(
    playlist_id: str,
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    username: str | None = None,
    enrich: bool = True,
) -> dict[str, Any] | None:
    data_root = data_dir.resolve()
    with _PLAYLISTS_LOCK:
        records = _load_records_unlocked(data_root)
    found = None
    for it in records:
        if str(it.get("id")) == str(playlist_id):
            found = it
            break
    if found is None:
        return None
    if not enrich:
        found["item_count"] = len(found.get("items") or [])
        found.setdefault("ready_drills_count", 0)
        found.setdefault("takeover_ready_count", 0)
        return found
    uname = (username or found.get("owner") or "").strip() or detect_default_username(data_root)
    return _enrich_playlist_items([found], username=uname, data_root=data_root)[0]


def create_playlist(
    payload: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    id_prefix: str = "pl",
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Playlist payload must be a JSON object")
    name = _sanitize_text(payload.get("name") or payload.get("title"), max_len=160)
    if not name:
        raise ValueError("Playlist name is required")
    kind = str(payload.get("kind") or "drill").strip().lower()
    if kind not in VALID_PLAYLIST_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(VALID_PLAYLIST_KINDS))}")

    is_focus = bool(payload.get("is_focus", False))
    raw_fs = payload.get("focus_status")
    if is_focus:
        fs_val = str(raw_fs or "active").strip().lower()
        if fs_val not in VALID_FOCUS_STATUSES:
            raise ValueError(f"focus_status must be one of: {', '.join(sorted(VALID_FOCUS_STATUSES))}")
        focus_status: str | None = fs_val
    else:
        if raw_fs not in (None, ""):
            fs_val = str(raw_fs).strip().lower()
            if fs_val not in VALID_FOCUS_STATUSES:
                raise ValueError(f"focus_status must be one of: {', '.join(sorted(VALID_FOCUS_STATUSES))}")
            is_focus = True
            focus_status = fs_val
        else:
            focus_status = None

    owner = str(payload.get("owner") or "").strip()
    now_iso = datetime.now().isoformat(timespec="seconds")
    pl_id = f"{id_prefix}_{uuid.uuid4().hex[:8]}"

    raw_items = payload.get("items")
    if raw_items is not None and not isinstance(raw_items, list):
        raise ValueError("items must be a list")

    record = _normalize_playlist(
        {
            "id": pl_id,
            "owner": owner,
            "name": name,
            "description": payload.get("description") or "",
            "kind": kind,
            "shareable": bool(payload.get("shareable", False)),
            "is_focus": is_focus,
            "focus_status": focus_status,
            "matchup_char": payload.get("matchup_char") or "",
            "tags": payload.get("tags") or [],
            "source_insight_id": payload.get("source_insight_id") or None,
            "trend_hint": payload.get("trend_hint") or "",
            "graduated_at": now_iso if (is_focus and focus_status == "graduated") else None,
            "items": raw_items or [],
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )

    data_root = data_dir.resolve()
    with _PLAYLISTS_LOCK:
        records = _load_records_unlocked(data_root)
        if record["is_focus"] and record["focus_status"] == "active":
            if _active_focus_count_for_owner(records, owner) >= MAX_ACTIVE_FOCUSES:
                raise ValueError(
                    f"Maximum of {MAX_ACTIVE_FOCUSES} active Focus Goals reached. "
                    "Graduate or unpin an active focus before pinning a new one."
                )
        records.append(record)
        _write_jsonl_atomic(playlists_jsonl_path(data_root), records)
    return get_playlist(pl_id, data_dir=data_root, username=record["owner"] or None) or record


def update_playlist(
    playlist_id: str,
    patch: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any] | None:
    if not _PLAYLIST_ID_RE.match(str(playlist_id or "")):
        raise ValueError("Invalid playlist id")
    if not isinstance(patch, dict):
        raise ValueError("Patch payload must be a JSON object")

    data_root = data_dir.resolve()
    now_iso = datetime.now().isoformat(timespec="seconds")
    updated_owner = ""
    found = False

    with _PLAYLISTS_LOCK:
        path = playlists_jsonl_path(data_root)
        records = _load_records_unlocked(data_root)
        for idx, cur in enumerate(records):
            if str(cur.get("id")) != str(playlist_id):
                continue

            old_is_focus = bool(cur.get("is_focus", False))
            old_focus_status = cur.get("focus_status")

            if "name" in patch or "title" in patch:
                raw_name = patch["name"] if "name" in patch else patch["title"]
                new_name = _sanitize_text(raw_name, max_len=160)
                if not new_name:
                    raise ValueError("Playlist name cannot be empty")
                cur["name"] = new_name
            if "description" in patch:
                cur["description"] = _sanitize_multiline(patch["description"], max_len=1200)
            if "kind" in patch:
                k = str(patch["kind"] or "drill").strip().lower()
                if k not in VALID_PLAYLIST_KINDS:
                    raise ValueError(f"Invalid kind: {k}")
                cur["kind"] = k
            if "shareable" in patch:
                cur["shareable"] = bool(patch["shareable"])
            if "owner" in patch:
                cur["owner"] = str(patch["owner"] or "").strip()
            if "matchup_char" in patch:
                cur["matchup_char"] = _sanitize_text(patch["matchup_char"], max_len=40)
            if "tags" in patch:
                cur["tags"] = _sanitize_tags(patch["tags"])
            if "source_insight_id" in patch:
                cur["source_insight_id"] = str(patch["source_insight_id"] or "").strip() or None
            if "trend_hint" in patch:
                cur["trend_hint"] = _sanitize_text(patch["trend_hint"], max_len=64)
            if "items" in patch:
                if not isinstance(patch["items"], list):
                    raise ValueError("items must be a list")
                cur["items"] = [_normalize_item(it) for it in patch["items"]]

            # Unified Focus promotion / demotion / graduation / reactivation
            has_is_focus = "is_focus" in patch
            has_focus_status = "focus_status" in patch
            if has_is_focus or has_focus_status:
                new_is_focus = bool(patch["is_focus"]) if has_is_focus else old_is_focus
                raw_fs = patch.get("focus_status") if has_focus_status else old_focus_status

                if has_is_focus and not new_is_focus:
                    # Demote / unpin back to Reference Playlist (preserving items[])
                    cur["is_focus"] = False
                    cur["focus_status"] = None
                    cur["graduated_at"] = None
                elif has_focus_status and raw_fs in (None, ""):
                    # focus_status: null -> demote to Reference Playlist
                    cur["is_focus"] = False
                    cur["focus_status"] = None
                    cur["graduated_at"] = None
                else:
                    # Active or Graduated Focus
                    fs_val = str(raw_fs or "active").strip().lower()
                    if fs_val not in VALID_FOCUS_STATUSES:
                        raise ValueError(f"Invalid focus_status: {fs_val}")
                    was_active_focus = old_is_focus and old_focus_status == "active"
                    if fs_val == "active" and not was_active_focus:
                        other_records = [r for j, r in enumerate(records) if j != idx]
                        if _active_focus_count_for_owner(other_records, cur["owner"]) >= MAX_ACTIVE_FOCUSES:
                            raise ValueError(
                                f"Maximum of {MAX_ACTIVE_FOCUSES} active Focus Goals reached. "
                                "Graduate or unpin an active focus first."
                            )
                        cur["graduated_at"] = None
                    elif fs_val == "graduated":
                        if not (old_is_focus and old_focus_status == "graduated"):
                            cur["graduated_at"] = now_iso
                    cur["is_focus"] = True
                    cur["focus_status"] = fs_val

            cur["updated_at"] = now_iso
            records[idx] = cur
            updated_owner = cur["owner"]
            found = True
            break
        if found:
            _write_jsonl_atomic(path, records)

    if not found:
        return None
    return get_playlist(playlist_id, data_dir=data_root, username=updated_owner or None)


def mutate_playlist_items(
    playlist_id: str,
    payload: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any] | None:
    """Add, remove, or reorder items on a playlist or Focus Goal."""
    if not _PLAYLIST_ID_RE.match(str(playlist_id or "")):
        raise ValueError("Invalid playlist id")
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object")

    action = str(payload.get("action") or "").strip().lower()
    if action not in ("add", "remove", "reorder"):
        raise ValueError("action must be 'add', 'remove', or 'reorder'")

    data_root = data_dir.resolve()
    now_iso = datetime.now().isoformat(timespec="seconds")
    updated_owner = ""
    found = False

    with _PLAYLISTS_LOCK:
        path = playlists_jsonl_path(data_root)
        records = _load_records_unlocked(data_root)
        for idx, cur in enumerate(records):
            if str(cur.get("id")) != str(playlist_id):
                continue
            cur_items = list(cur["items"])

            if action == "add":
                raw_item = payload.get("item")
                if not isinstance(raw_item, dict):
                    raise ValueError("action='add' requires an 'item' object")
                norm = _normalize_item(raw_item)
                existing_idx = next((i for i, x in enumerate(cur_items) if x["item_id"] == norm["item_id"]), -1)
                if existing_idx >= 0:
                    cur_items[existing_idx] = norm
                else:
                    cur_items.append(norm)
            elif action == "remove":
                iid = str(payload.get("item_id") or "").strip()
                if not iid:
                    raise ValueError("action='remove' requires 'item_id'")
                cur_items = [x for x in cur_items if x["item_id"] != iid]
            elif action == "reorder":
                order = payload.get("order")
                if not isinstance(order, list):
                    raise ValueError("action='reorder' requires an 'order' list of item_ids")
                by_id = {x["item_id"]: x for x in cur_items}
                reordered: list[dict[str, Any]] = []
                used: set[str] = set()
                for oid in order:
                    s_id = str(oid)
                    if s_id in by_id and s_id not in used:
                        reordered.append(by_id[s_id])
                        used.add(s_id)
                for x in cur_items:
                    if x["item_id"] not in used:
                        reordered.append(x)
                cur_items = reordered

            cur["items"] = cur_items
            cur["updated_at"] = now_iso
            records[idx] = cur
            updated_owner = cur["owner"]
            found = True
            break
        if found:
            _write_jsonl_atomic(path, records)

    if not found:
        return None
    return get_playlist(playlist_id, data_dir=data_root, username=updated_owner or None)


def delete_playlist(playlist_id: str, *, data_dir: Path = DEFAULT_DATA_DIR) -> bool:
    if not _PLAYLIST_ID_RE.match(str(playlist_id or "")):
        raise ValueError("Invalid playlist id")
    data_root = data_dir.resolve()
    removed = False
    with _PLAYLISTS_LOCK:
        path = playlists_jsonl_path(data_root)
        records = _load_records_unlocked(data_root)
        kept = [r for r in records if str(r.get("id")) != str(playlist_id)]
        if len(kept) != len(records):
            removed = True
            _write_jsonl_atomic(path, kept)
    return removed


def ensure_playlist_drills(
    playlist_id: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    bake_fn: Callable[..., dict[str, Any]] | None = None,
    async_bake: bool = True,
) -> dict[str, Any]:
    """Bake offline `.fs` savestates for any playlist/focus items lacking a ready savestate."""
    data_root = data_dir.resolve()
    pl = get_playlist(playlist_id, data_dir=data_root, enrich=True)
    if pl is None:
        raise FileNotFoundError(f"Playlist '{playlist_id}' not found")

    to_bake = [
        it for it in (pl.get("items") or [])
        if not it.get("savestate_ready") and it.get("can_bake_drill")
    ]
    baker = bake_fn or bake_drill_savestate

    def _run() -> dict[str, Any]:
        baked: list[str] = []
        errors: list[dict[str, str]] = []
        for it in to_bake:
            iid = str(it["item_id"])
            try:
                baker(
                    str(it["quark_id"]),
                    int(it["start_gf"]),
                    int(it["end_gf"]),
                    selection_id=iid,
                    data_dir=data_root,
                )
                baked.append(iid)
            except Exception as exc:  # noqa: BLE001
                errors.append({"item_id": iid, "error": str(exc)})
        return {"baked": baked, "errors": errors}

    if not to_bake:
        return {
            "ok": True,
            "playlist_id": playlist_id,
            "queued": 0,
            "already_ready": int(pl.get("takeover_ready_count") or 0),
            "baked": [],
            "errors": [],
        }

    if async_bake:
        threading.Thread(target=_run, name=f"fc-pl-bake-{playlist_id[:12]}", daemon=True).start()
        return {
            "ok": True,
            "playlist_id": playlist_id,
            "queued": len(to_bake),
            "already_ready": int(pl.get("takeover_ready_count") or 0),
            "async": True,
        }

    res = _run()
    return {
        "ok": True,
        "playlist_id": playlist_id,
        "queued": len(to_bake),
        "already_ready": int(pl.get("takeover_ready_count") or 0),
        "baked": res["baked"],
        "errors": res["errors"],
        "async": False,
    }
