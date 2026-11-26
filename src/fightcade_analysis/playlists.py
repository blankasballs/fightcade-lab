"""Curated Study & Training Playlists with perspective-safe enrichment and `.fs` drill readiness.

Persisted atomically in `data/selections/playlists.jsonl`.
Supports Watch Mode (phone/desktop looping clip study reel with Grouflon overlays) and
Train Mode (desktop Replay Takeover runner via `POST /api/takeover`).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import threading
from typing import Any, Callable
import uuid

from fightcade_analysis.annotations import list_annotations
from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR
from fightcade_analysis.focus_goals import clear_playlist_reference, list_focus_goals
from fightcade_analysis.match_catalog import detect_default_username, list_matches
from fightcade_analysis.notes_hub import _resolve_context_for_item, _round_video_preview
from fightcade_analysis.round_renderer import bake_drill_savestate, list_cached_round_savestates
from fightcade_analysis.select_queue import _read_jsonl, _write_jsonl_atomic, load_queue

VALID_PLAYLIST_KINDS: frozenset[str] = frozenset({"drill", "study", "matchup", "pro_reel"})
VALID_ITEM_SOURCES: frozenset[str] = frozenset({"annotation", "select_mark", "pro_clip", "moment"})
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_QUARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_PLAYLISTS_LOCK = threading.Lock()


def playlists_jsonl_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "selections" / "playlists.jsonl"


def _sanitize_text(raw: Any, *, max_len: int = 120) -> str:
    return " ".join(str(raw or "").replace("\r", " ").replace("\n", " ").split())[:max_len]


def _sanitize_multiline(raw: Any, *, max_len: int = 1000) -> str:
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

    return {
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


def _normalize_playlist(raw: dict[str, Any]) -> dict[str, Any]:
    now_iso = datetime.now().isoformat(timespec="seconds")
    kind = str(raw.get("kind") or "drill").strip().lower()
    if kind not in VALID_PLAYLIST_KINDS:
        kind = "drill"
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
        "name": _sanitize_text(raw.get("name"), max_len=120),
        "description": _sanitize_multiline(raw.get("description"), max_len=1000),
        "kind": kind,
        "shareable": bool(raw.get("shareable", False)),
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
    goals = list_focus_goals(data_root, status="active", owner=username)
    goals_by_pl: dict[str, list[dict[str, Any]]] = {}
    for g in goals:
        pid = g.get("playlist_id")
        if pid:
            goals_by_pl.setdefault(str(pid), []).append({"id": g["id"], "title": g["title"]})

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

            rid = int(it.get("round_id") or 0) or (int(ann.get("round_id") or 0) if ann else 0) or (int(qi.get("round_id") or 0) if qi else 0) or None
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
            is_standalone_clip = False

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

            note_text = str(it.get("note") or "").strip()
            if not note_text and ann:
                note_text = str(ann.get("note") or "").strip()
            if not note_text and qi:
                note_text = str(qi.get("note") or "").strip()

            tags = list(it.get("tags") or [])
            if not tags and ann:
                tags = list(ann.get("tags") or [])
            elif not tags and qi:
                tags = list(qi.get("tags") or [])

            r_label = (
                f"M{prev['match_id']} · R{prev['match_round']}"
                if prev["match_id"]
                else (f"R{rid}" if rid else ("Pro Clip" if src == "pro_clip" else "Moment"))
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
                    "kind": str((ann or qi or {}).get("kind") or ("pro_clip" if src == "pro_clip" else "note")),
                    "has_video": has_video,
                    "video_url": video_url,
                    "overlay_url": overlay_url,
                    "is_standalone_clip": is_standalone_clip,
                    "round_start_gf": prev["round_start_gf"],
                    "round_end_gf": prev["round_end_gf"],
                    "round_fps": prev["round_fps"],
                    "clip_start_s": clip_start_s,
                    "clip_end_s": clip_end_s,
                    "anchor_s": prev["anchor_s"],
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
        pl_copy["pro_clips_count"] = pro_count
        pl_copy["linked_goals"] = goals_by_pl.get(pl["id"], [])
        enriched_playlists.append(pl_copy)

    return enriched_playlists


def list_playlists(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    owner: str | None = None,
    username: str | None = None,
    enrich: bool = True,
) -> list[dict[str, Any]]:
    data_root = data_dir.resolve()
    with _PLAYLISTS_LOCK:
        raw_items = _read_jsonl(playlists_jsonl_path(data_root))
    pls = [_normalize_playlist(it) for it in raw_items if it.get("id")]
    if owner:
        pls = [p for p in pls if _owner_matches(p["owner"], owner)]
    pls.sort(key=lambda p: str(p.get("updated_at") or p.get("created_at") or ""), reverse=True)
    if not enrich:
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
        raw_items = _read_jsonl(playlists_jsonl_path(data_root))
    found = None
    for it in raw_items:
        if str(it.get("id")) == str(playlist_id):
            found = _normalize_playlist(it)
            break
    if found is None:
        return None
    if not enrich:
        return found
    uname = (username or found.get("owner") or "").strip() or detect_default_username(data_root)
    return _enrich_playlist_items([found], username=uname, data_root=data_root)[0]


def create_playlist(
    payload: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Playlist payload must be a JSON object")
    name = _sanitize_text(payload.get("name"), max_len=120)
    if not name:
        raise ValueError("Playlist name is required")
    kind = str(payload.get("kind") or "drill").strip().lower()
    if kind not in VALID_PLAYLIST_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(VALID_PLAYLIST_KINDS))}")

    now_iso = datetime.now().isoformat(timespec="seconds")
    pl_id = f"pl_{uuid.uuid4().hex[:8]}"
    record = _normalize_playlist(
        {
            "id": pl_id,
            "owner": str(payload.get("owner") or "").strip(),
            "name": name,
            "description": payload.get("description") or "",
            "kind": kind,
            "shareable": bool(payload.get("shareable", False)),
            "items": payload.get("items") or [],
            "created_at": now_iso,
            "updated_at": now_iso,
        }
    )

    data_root = data_dir.resolve()
    with _PLAYLISTS_LOCK:
        path = playlists_jsonl_path(data_root)
        items = _read_jsonl(path)
        items.append(record)
        _write_jsonl_atomic(path, items)
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
        records = _read_jsonl(path)
        for idx, raw in enumerate(records):
            if str(raw.get("id")) != str(playlist_id):
                continue
            cur = _normalize_playlist(raw)
            if "name" in patch:
                new_name = _sanitize_text(patch["name"], max_len=120)
                if not new_name:
                    raise ValueError("Playlist name cannot be empty")
                cur["name"] = new_name
            if "description" in patch:
                cur["description"] = _sanitize_multiline(patch["description"], max_len=1000)
            if "kind" in patch:
                k = str(patch["kind"] or "drill").strip().lower()
                if k not in VALID_PLAYLIST_KINDS:
                    raise ValueError(f"Invalid kind: {k}")
                cur["kind"] = k
            if "shareable" in patch:
                cur["shareable"] = bool(patch["shareable"])
            if "owner" in patch:
                cur["owner"] = str(patch["owner"] or "").strip()
            if "items" in patch:
                if not isinstance(patch["items"], list):
                    raise ValueError("items must be a list")
                cur["items"] = [_normalize_item(it) for it in patch["items"]]
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
    """Add, remove, or reorder items on a playlist."""
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
        records = _read_jsonl(path)
        for idx, raw in enumerate(records):
            if str(raw.get("id")) != str(playlist_id):
                continue
            cur = _normalize_playlist(raw)
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
        records = _read_jsonl(path)
        kept = [r for r in records if str(r.get("id")) != str(playlist_id)]
        if len(kept) != len(records):
            removed = True
            _write_jsonl_atomic(path, kept)
    if removed:
        clear_playlist_reference(playlist_id, data_dir=data_root)
    return removed


def ensure_playlist_drills(
    playlist_id: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    bake_fn: Callable[..., dict[str, Any]] | None = None,
    async_bake: bool = True,
) -> dict[str, Any]:
    """Bake offline `.fs` savestates for any playlist items lacking a ready savestate."""
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
