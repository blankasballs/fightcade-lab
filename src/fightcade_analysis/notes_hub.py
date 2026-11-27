"""Centralized Notes Hub collation across moment annotations, set takeaways, in-game Select marks, and bookmarks.

Joins all disparate note sources against match history & replay manifests (projected through
`perspective.py`) so the centralized Notes view can search, filter by matchup/tag/kind, group,
and play inline looping video previews of every timestamped moment.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from fightcade_analysis.annotations import get_set_notes_map, list_annotations
from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR
from fightcade_analysis.coaching import pretty_char
from fightcade_analysis.match_catalog import detect_default_username, list_matches
from fightcade_analysis.naming import extract_match_date, format_rank_letter, lookup_replay_metadata
from fightcade_analysis.perspective import (
    SidePair,
    extract_p1_p2_from_manifest,
    is_placeholder_name,
    normalize_side,
    resolve_viewer_side,
)
from fightcade_analysis.round_renderer import ensure_manifest_matches, mobile_manifest_path
from fightcade_analysis.select_queue import _read_jsonl, bookmarks_jsonl_path, load_queue

_DEFAULT_SELECT_NOTE_RE = re.compile(r"^In-game Select mark #\d+\b", re.IGNORECASE)


def _format_iso_from_any(val: Any, fallback_date: str = "") -> str:
    if val is None or val == "":
        return fallback_date
    if isinstance(val, (int, float)):
        ts = float(val) / 1000.0 if float(val) > 10_000_000_000 else float(val)
        if ts > 0:
            return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")
        return fallback_date
    return str(val)


def _sort_key_ts(created_at: Any, updated_at: Any, match_date_ms: int) -> float:
    for candidate in (updated_at, created_at):
        if isinstance(candidate, (int, float)) and candidate > 0:
            return float(candidate) / 1000.0 if candidate > 10_000_000_000 else float(candidate)
        if isinstance(candidate, str) and candidate.strip():
            try:
                return datetime.fromisoformat(candidate.strip().replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    if match_date_ms > 0:
        return float(match_date_ms) / 1000.0 if match_date_ms > 10_000_000_000 else float(match_date_ms)
    return 0.0


def _round_video_preview(
    round_doc: dict[str, Any] | None,
    *,
    quark_id: str,
    start_gf: int | None,
    end_gf: int | None,
    anchor_gf: int | None,
    data_root: Path,
) -> dict[str, Any]:
    """Compute inline MP4 preview URL and clamped `[clip_start_s .. clip_end_s]` for a round."""
    empty = {
        "has_video": False,
        "video_url": None,
        "overlay_url": None,
        "round_start_gf": None,
        "round_end_gf": None,
        "round_fps": 60.0,
        "clip_start_s": None,
        "clip_end_s": None,
        "anchor_s": None,
        "match_id": None,
        "match_round": None,
    }
    if not round_doc:
        return empty

    r_start = int(round_doc.get("start_gf") or 0)
    r_end = int(round_doc.get("end_gf") or r_start)
    frame_count = max(1, int(round_doc.get("frame_count") or (r_end - r_start + 1)))
    fps = float(round_doc.get("fps") or 60.0)
    if fps <= 0:
        fps = 60.0
    duration_s = max(0.2, (frame_count - 1) / fps)

    vid_url = str(round_doc.get("video_url") or "").strip()
    fname = str(round_doc.get("filename") or "").strip()
    if not vid_url and fname:
        vid_url = f"/replays/{quark_id}/{fname}"
    has_mp4 = False
    if vid_url:
        mp4_name = vid_url.rsplit("/", 1)[-1]
        has_mp4 = (data_root / "replays" / quark_id / mp4_name).is_file()

    # Compute clamped clip window inside this round's video
    eff_anchor = int(anchor_gf) if anchor_gf is not None else (int(start_gf) if start_gf is not None else r_start)
    eff_start = int(start_gf) if start_gf is not None else max(r_start, eff_anchor - 150)
    eff_end = int(end_gf) if end_gf is not None else min(r_end, eff_anchor + 90)
    if eff_end <= eff_start:
        eff_start = max(r_start, eff_anchor - 150)
        eff_end = min(r_end, eff_anchor + 90)

    start_off = max(0, min(frame_count - 1, eff_start - r_start))
    end_off = max(start_off, min(frame_count - 1, eff_end - r_start))
    anchor_off = max(0, min(frame_count - 1, eff_anchor - r_start))

    clip_start_s = round(start_off / fps, 3)
    clip_end_s = round(end_off / fps, 3)
    # Ensure at least a ~1.5s viewable loop span when possible
    if clip_end_s - clip_start_s < 1.0:
        clip_end_s = round(min(duration_s, clip_start_s + 1.5), 3)
        if clip_end_s - clip_start_s < 0.8:
            clip_start_s = round(max(0.0, clip_end_s - 1.5), 3)

    ov_url = str(round_doc.get("overlay_url") or "").strip() or None
    if ov_url:
        ov_name = ov_url.rsplit("/", 1)[-1]
        if not (data_root / "replays" / quark_id / ov_name).is_file():
            ov_url = None
    elif round_doc.get("round_id"):
        ov_name = f"r{int(round_doc['round_id']):02d}_overlay.json"
        if (data_root / "replays" / quark_id / ov_name).is_file():
            ov_url = f"/replays/{quark_id}/{ov_name}"

    return {
        "has_video": has_mp4,
        "video_url": vid_url if has_mp4 else None,
        "overlay_url": ov_url,
        "round_start_gf": r_start,
        "round_end_gf": r_end,
        "round_fps": fps,
        "clip_start_s": clip_start_s,
        "clip_end_s": clip_end_s,
        "anchor_s": round(anchor_off / fps, 3),
        "match_id": round_doc.get("match_id"),
        "match_round": round_doc.get("match_round"),
    }


def _resolve_context_for_item(
    *,
    quark_id: str,
    item_perspective: int | None,
    round_id: int | None,
    username: str,
    match_by_qid: dict[str, dict[str, Any]],
    manifest_cache: dict[str, dict[str, Any] | None],
    data_root: Path,
    fallback_self_char: str = "",
    fallback_opp_char: str = "",
    fallback_self_player: str = "",
    fallback_opp_player: str = "",
) -> dict[str, Any]:
    """Resolve perspective-safe matchup, opponent, date, and round preview context."""
    mrow = match_by_qid.get(quark_id) or {}
    if quark_id not in manifest_cache:
        mf_path = mobile_manifest_path(quark_id, data_dir=data_root)
        if mf_path.exists():
            try:
                raw_mf = json.loads(mf_path.read_text(encoding="utf-8"))
                manifest_cache[quark_id] = ensure_manifest_matches(
                    raw_mf,
                    data_dir=data_root,
                    username=username,
                )
            except Exception:  # noqa: BLE001
                manifest_cache[quark_id] = None
        else:
            manifest_cache[quark_id] = None
    mf = manifest_cache[quark_id]

    meta = lookup_replay_metadata(quark_id, data_dir=data_root)
    mf_p1, mf_p2 = extract_p1_p2_from_manifest(mf) if mf else ({}, {})

    p1_name = str(meta.get("p1_name") or "").strip()
    if is_placeholder_name(p1_name) and not is_placeholder_name(mf_p1.get("name")):
        p1_name = str(mf_p1["name"]).strip()
    p2_name = str(meta.get("p2_name") or "").strip()
    if is_placeholder_name(p2_name) and not is_placeholder_name(mf_p2.get("name")):
        p2_name = str(mf_p2["name"]).strip()

    eff_persp = normalize_side(
        item_perspective
        if item_perspective in (1, 2)
        else resolve_viewer_side(
            username=username,
            p1_name=p1_name,
            p2_name=p2_name,
            fallback_side=mrow.get("perspective") or (mf.get("perspective") if mf else 1),
        )
    )

    names_pair = SidePair(p1_name, p2_name)
    ranks_pair = SidePair(
        format_rank_letter(meta.get("p1_rank")) or str(mf_p1.get("rank_letter") or ""),
        format_rank_letter(meta.get("p2_rank")) or str(mf_p2.get("rank_letter") or ""),
    )
    self_player, opp_player = names_pair.split(eff_persp)
    self_rank, opp_rank = ranks_pair.split(eff_persp)

    if is_placeholder_name(self_player):
        if not is_placeholder_name(fallback_self_player):
            self_player = str(fallback_self_player).strip()
        elif mrow.get("perspective") == eff_persp and not is_placeholder_name(mrow.get("self_player_name")):
            self_player = str(mrow["self_player_name"]).strip()
        else:
            self_player = f"P{eff_persp}"
    if is_placeholder_name(opp_player):
        if not is_placeholder_name(fallback_opp_player):
            opp_player = str(fallback_opp_player).strip()
        elif mrow.get("perspective") == eff_persp and not is_placeholder_name(mrow.get("opp_player_name")):
            opp_player = str(mrow["opp_player_name"]).strip()
        else:
            opp_player = "P2" if eff_persp == 1 else "P1"

    round_doc: dict[str, Any] | None = None
    if mf and round_id is not None:
        for r in mf.get("rounds") or []:
            if int(r.get("round_id", 0)) == int(round_id):
                round_doc = r
                break

    # Character names for this exact round (or set fallback) projected for eff_persp
    if round_doc:
        rc_p1 = str(round_doc.get("p1_char_name") or pretty_char(int(round_doc.get("p1_char", 0) or 0)))
        rc_p2 = str(round_doc.get("p2_char_name") or pretty_char(int(round_doc.get("p2_char", 0) or 0)))
        self_char, opp_char = SidePair(rc_p1, rc_p2).split(eff_persp)
    elif mf:
        self_char, opp_char = SidePair(str(mf_p1.get("char_name") or ""), str(mf_p2.get("char_name") or "")).split(eff_persp)
    elif mrow and mrow.get("perspective") == eff_persp:
        self_char = str(mrow.get("self_char_name") or "")
        opp_char = str(mrow.get("opp_char_name") or "")
    else:
        self_char = ""
        opp_char = ""

    if not self_char:
        self_char = str(fallback_self_char or "")
    if not opp_char:
        opp_char = str(fallback_opp_char or "")

    date_ms = int(mrow.get("date") or meta.get("date") or 0)
    date_str = str(mrow.get("date_str") or "")
    if not date_str:
        date_str = extract_match_date(quark_id, meta)

    return {
        "perspective": eff_persp,
        "self_player_name": self_player,
        "opp_player_name": opp_player,
        "self_rank_letter": self_rank,
        "opp_rank_letter": opp_rank,
        "self_char_name": self_char,
        "opp_char_name": opp_char,
        "date": date_ms,
        "date_str": date_str,
        "round_doc": round_doc,
        "has_set_video": bool(mf and len(mf.get("rounds") or []) > 0),
    }


def list_collated_notes(
    *,
    username: str | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    include_select_marks: bool = False,
) -> dict[str, Any]:
    """Collate moment notes, set takeaways, in-game Select marks, and manual bookmarks into one unified feed."""
    data_root = data_dir.resolve()
    uname = (username or "").strip() or detect_default_username(data_root)

    cat = list_matches(username=uname, data_dir=data_root)
    match_by_qid = {str(m["quark_id"]): m for m in cat.get("matches") or []}
    manifest_cache: dict[str, dict[str, Any] | None] = {}

    notes: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # 1. Moment Annotations (annotations.jsonl)
    for ann in list_annotations(data_root):
        ann_id = str(ann.get("id") or "")
        qid = str(ann.get("quark_id") or "")
        if not ann_id or not qid:
            continue
        seen_ids.add(ann_id)
        rid = int(ann.get("round_id") or 0) or None
        persp = int(ann["perspective"]) if ann.get("perspective") in (1, 2) else None
        ctx = _resolve_context_for_item(
            quark_id=qid,
            item_perspective=persp,
            round_id=rid,
            username=uname,
            match_by_qid=match_by_qid,
            manifest_cache=manifest_cache,
            data_root=data_root,
        )
        prev = _round_video_preview(
            ctx["round_doc"],
            quark_id=qid,
            start_gf=ann.get("start_gf"),
            end_gf=ann.get("end_gf"),
            anchor_gf=ann.get("anchor_gf"),
            data_root=data_root,
        )
        ann_source = str(ann.get("source") or "annotation").strip().lower()
        resp_clip_url = str(ann.get("response_clip_url") or "").strip() or None
        resp_ov_url = str(ann.get("response_overlay_url") or "").strip() or None
        is_response_clip = (ann_source == "takeover_response") or bool(resp_clip_url)
        parent_moment_id = str(ann.get("parent_moment_id") or "").strip() or None

        if is_response_clip and resp_clip_url:
            resp_rel = resp_clip_url.lstrip("/")
            resp_mp4_path = data_root / Path(resp_rel)
            has_resp_mp4 = resp_mp4_path.is_file()
            has_resp_ov = False
            if resp_ov_url:
                resp_ov_path = data_root / Path(resp_ov_url.lstrip("/"))
                has_resp_ov = resp_ov_path.is_file()
            n_frames = max(1, int(ann.get("frame_count") or (int(ann.get("end_gf") or 0) - int(ann.get("start_gf") or 0) + 1)))
            resp_dur_s = round(max(0.4, (n_frames - 1) / 60.0), 3)
            eff_has_video = has_resp_mp4
            eff_video_url = resp_clip_url if has_resp_mp4 else None
            eff_overlay_url = resp_ov_url if has_resp_ov else None
            eff_round_start_gf = int(ann.get("start_gf") or 0)
            eff_round_end_gf = int(ann.get("end_gf") or eff_round_start_gf)
            eff_clip_start_s = 0.0
            eff_clip_end_s = resp_dur_s
            eff_anchor_s = 0.0
        else:
            eff_has_video = prev["has_video"]
            eff_video_url = prev["video_url"]
            eff_overlay_url = prev.get("overlay_url")
            eff_round_start_gf = prev["round_start_gf"]
            eff_round_end_gf = prev["round_end_gf"]
            eff_clip_start_s = prev["clip_start_s"]
            eff_clip_end_s = prev["clip_end_s"]
            eff_anchor_s = prev["anchor_s"]

        r_label = (
            f"M{prev['match_id']} · R{prev['match_round']}"
            if prev["match_id"]
            else (f"R{rid}" if rid else "Round")
        )
        note_text = str(ann.get("note") or "").strip()
        fs_path = Path(str(ann.get("savestate") or "")) if ann.get("savestate") else (data_root / "selections" / f"{ann_id}.fs")
        fs_exists = fs_path.exists() and fs_path.stat().st_size > 4096
        fs_ready = (is_response_clip and fs_exists) or (
            bool(ann.get("queued_drill")) and (ann.get("drill_status") == "ready" or fs_exists)
        )
        notes.append(
            {
                "id": ann_id,
                "source": "takeover_response" if is_response_clip else "annotation",
                "entry_type": "moment",
                "kind": str(ann.get("kind") or ("response" if is_response_clip else "note")),
                "parent_moment_id": parent_moment_id,
                "is_response_clip": is_response_clip,
                "response_clip_url": resp_clip_url,
                "response_overlay_url": resp_ov_url,
                "hp_delta_self": ann.get("hp_delta_self"),
                "hp_delta_opp": ann.get("hp_delta_opp"),
                "frame_count": ann.get("frame_count"),
                "tags": list(ann.get("tags") or []),
                "owner": str(ann.get("owner") or ""),
                "goal_ids": list(ann.get("goal_ids") or []),
                "playlist_ids": list(ann.get("playlist_ids") or []),
                "note": note_text,
                "needs_note": (not bool(note_text)) and (not is_response_clip),
                "quark_id": qid,
                "date": ctx["date"],
                "date_str": ctx["date_str"],
                "perspective": ctx["perspective"],
                "self_player_name": ctx["self_player_name"],
                "opp_player_name": ctx["opp_player_name"],
                "self_rank_letter": ctx["self_rank_letter"],
                "opp_rank_letter": ctx["opp_rank_letter"],
                "self_char_name": ctx["self_char_name"],
                "opp_char_name": ctx["opp_char_name"],
                "round_id": rid,
                "match_id": prev["match_id"],
                "match_round": prev["match_round"],
                "round_label": r_label,
                "anchor_gf": int(ann.get("anchor_gf") or 0),
                "start_gf": int(ann.get("start_gf") or 0),
                "end_gf": int(ann.get("end_gf") or 0),
                "is_range": bool(ann.get("is_range")),
                "has_video": eff_has_video,
                "has_set_video": ctx["has_set_video"],
                "video_url": eff_video_url,
                "overlay_url": eff_overlay_url,
                "round_start_gf": eff_round_start_gf,
                "round_end_gf": eff_round_end_gf,
                "round_fps": prev["round_fps"],
                "clip_start_s": eff_clip_start_s,
                "clip_end_s": eff_clip_end_s,
                "anchor_s": eff_anchor_s,
                "queued_drill": bool(ann.get("queued_drill")),
                "drill_status": str(ann.get("drill_status") or ("ready" if fs_ready else "none")),
                "can_launch_drill": fs_ready,
                "editable": True,
                "deletable": True,
                "cloud_moment_id": ann.get("cloud_moment_id"),
                "cloud_share_token": ann.get("cloud_share_token"),
                "cloud_share_url": ann.get("cloud_share_url"),
                "cloud_visibility": ann.get("cloud_visibility"),
                "shared_caption": str(ann.get("shared_caption") or ""),
                "include_note_on_share": bool(ann.get("include_note_on_share", False)),
                "hide_player_names": bool(ann.get("hide_player_names", False)),
                "created_at": _format_iso_from_any(ann.get("created_at"), ctx["date_str"]),
                "updated_at": _format_iso_from_any(ann.get("updated_at"), ctx["date_str"]),
                "sort_ts": _sort_key_ts(ann.get("created_at"), ann.get("updated_at"), ctx["date"]),
            }
        )

    # 2. Set Takeaway Notes (set_notes.jsonl)
    for qid, sn in get_set_notes_map(data_root).items():
        note_text = str(sn.get("note") or "").strip()
        tags = list(sn.get("tags") or [])
        if not note_text and not tags:
            continue
        sn_id = f"setnote_{qid}"
        seen_ids.add(sn_id)
        ctx = _resolve_context_for_item(
            quark_id=qid,
            item_perspective=None,
            round_id=None,
            username=uname,
            match_by_qid=match_by_qid,
            manifest_cache=manifest_cache,
            data_root=data_root,
        )
        notes.append(
            {
                "id": sn_id,
                "source": "set_note",
                "entry_type": "set_note",
                "kind": "set_takeaway",
                "tags": tags,
                "note": note_text,
                "needs_note": not bool(note_text),
                "quark_id": qid,
                "date": ctx["date"],
                "date_str": ctx["date_str"],
                "perspective": ctx["perspective"],
                "self_player_name": ctx["self_player_name"],
                "opp_player_name": ctx["opp_player_name"],
                "self_rank_letter": ctx["self_rank_letter"],
                "opp_rank_letter": ctx["opp_rank_letter"],
                "self_char_name": ctx["self_char_name"],
                "opp_char_name": ctx["opp_char_name"],
                "round_id": None,
                "match_id": None,
                "match_round": None,
                "round_label": "Set Takeaway",
                "anchor_gf": None,
                "start_gf": None,
                "end_gf": None,
                "is_range": False,
                "has_video": False,
                "has_set_video": ctx["has_set_video"],
                "video_url": None,
                "round_start_gf": None,
                "round_end_gf": None,
                "round_fps": 60.0,
                "clip_start_s": None,
                "clip_end_s": None,
                "anchor_s": None,
                "queued_drill": False,
                "drill_status": "none",
                "can_launch_drill": False,
                "editable": True,
                "deletable": True,
                "created_at": _format_iso_from_any(sn.get("updated_at"), ctx["date_str"]),
                "updated_at": _format_iso_from_any(sn.get("updated_at"), ctx["date_str"]),
                "sort_ts": _sort_key_ts(sn.get("updated_at"), sn.get("updated_at"), ctx["date"]),
            }
        )

    # 3. In-Game Select Marks (queue.jsonl, excluding mobile_note copies & dismissed items)
    queue_items = load_queue(data_root, include_dismissed=False)
    queue_ids: set[str] = {str(it.get("id") or "") for it in queue_items}
    if include_select_marks:
        for qi in queue_items:
            qid_item = str(qi.get("id") or "")
            qid = str(qi.get("quark_id") or "")
            if not qid_item or not qid or qid_item in seen_ids:
                continue
            if qi.get("source") == "mobile_note":
                continue
            seen_ids.add(qid_item)
            rid = int(qi.get("round_id") or 0) or None
            persp = int(qi["perspective"]) if qi.get("perspective") in (1, 2) else None
            ctx = _resolve_context_for_item(
                quark_id=qid,
                item_perspective=persp,
                round_id=rid,
                username=uname,
                match_by_qid=match_by_qid,
                manifest_cache=manifest_cache,
                data_root=data_root,
                fallback_self_char=str(qi.get("self_char_name") or ""),
                fallback_opp_char=str(qi.get("opp_char_name") or ""),
                fallback_self_player=str(qi.get("self_player_name") or ""),
                fallback_opp_player=str(qi.get("opp_player_name") or ""),
            )
            anchor_gf = int(qi.get("press_gf") or qi.get("end_gf") or qi.get("start_gf") or 0)
            start_gf = int(qi.get("start_gf") or anchor_gf)
            end_gf = int(qi.get("end_gf") or anchor_gf)
            prev = _round_video_preview(
                ctx["round_doc"],
                quark_id=qid,
                start_gf=start_gf,
                end_gf=end_gf,
                anchor_gf=anchor_gf,
                data_root=data_root,
            )
            r_label = (
                f"M{prev['match_id']} · R{prev['match_round']}"
                if prev["match_id"]
                else (f"R{rid}" if rid else "Round")
            )
            raw_note = str(qi.get("note") or "").strip()
            is_default_sel = (not raw_note) or bool(_DEFAULT_SELECT_NOTE_RE.match(raw_note))
            fs_ready = bool(qi.get("savestate_ready"))
            notes.append(
                {
                    "id": qid_item,
                    "source": "select_mark",
                    "entry_type": "select_mark",
                    "kind": str(qi.get("kind") or "select_mark"),
                    "tags": list(qi.get("tags") or []),
                    "owner": str(qi.get("owner") or ""),
                    "goal_ids": list(qi.get("goal_ids") or []),
                    "playlist_ids": list(qi.get("playlist_ids") or []),
                    "note": raw_note,
                    "needs_note": is_default_sel,
                    "is_default_select_note": is_default_sel,
                    "quark_id": qid,
                    "date": ctx["date"],
                    "date_str": ctx["date_str"],
                    "perspective": ctx["perspective"],
                    "self_player_name": ctx["self_player_name"],
                    "opp_player_name": ctx["opp_player_name"],
                    "self_rank_letter": ctx["self_rank_letter"],
                    "opp_rank_letter": ctx["opp_rank_letter"],
                    "self_char_name": ctx["self_char_name"],
                    "opp_char_name": ctx["opp_char_name"],
                    "round_id": rid,
                    "match_id": prev["match_id"],
                    "match_round": prev["match_round"],
                    "round_label": r_label,
                    "anchor_gf": anchor_gf,
                    "start_gf": start_gf,
                    "end_gf": end_gf,
                    "is_range": end_gf > start_gf,
                    "has_video": prev["has_video"],
                    "has_set_video": ctx["has_set_video"],
                    "video_url": prev["video_url"],
                    "overlay_url": prev.get("overlay_url"),
                    "round_start_gf": prev["round_start_gf"],
                    "round_end_gf": prev["round_end_gf"],
                    "round_fps": prev["round_fps"],
                    "clip_start_s": prev["clip_start_s"],
                    "clip_end_s": prev["clip_end_s"],
                    "anchor_s": prev["anchor_s"],
                    "queued_drill": True,
                    "drill_status": "ready" if fs_ready else "none",
                    "can_launch_drill": fs_ready,
                    "queue_status": str(qi.get("status") or "pending"),
                    "editable": True,
                    "deletable": True,
                    "cloud_moment_id": qi.get("cloud_moment_id"),
                    "cloud_share_token": qi.get("cloud_share_token"),
                    "cloud_share_url": qi.get("cloud_share_url"),
                    "cloud_visibility": qi.get("cloud_visibility"),
                    "shared_caption": str(qi.get("shared_caption") or ""),
                    "include_note_on_share": bool(qi.get("include_note_on_share", False)),
                    "hide_player_names": bool(qi.get("hide_player_names", False)),
                    "created_at": _format_iso_from_any(qi.get("created_at"), ctx["date_str"]),
                    "updated_at": _format_iso_from_any(qi.get("updated_at") or qi.get("created_at"), ctx["date_str"]),
                    "sort_ts": _sort_key_ts(qi.get("created_at"), qi.get("updated_at"), ctx["date"]),
                }
            )

    # 4. Manual Lua Bookmarks (bookmarks.jsonl items not from mobile_note or in_game_select)
    for bm in _read_jsonl(bookmarks_jsonl_path(data_root)):
        bm_id = str(bm.get("id") or "")
        qid = str(bm.get("quark_id") or "")
        src = str(bm.get("source") or "")
        if not bm_id or not qid or bm_id in seen_ids or bm_id in queue_ids:
            continue
        if src in ("mobile_note", "in_game_select"):
            continue
        note_text = str(bm.get("note") or "").strip()
        if not note_text:
            continue
        seen_ids.add(bm_id)
        persp = int(bm["perspective"]) if bm.get("perspective") in (1, 2) else None
        p1_cid = int(bm.get("p1_char") or 0)
        p2_cid = int(bm.get("p2_char") or 0)
        fb_self, fb_opp = (
            SidePair(pretty_char(p1_cid) if p1_cid else "", pretty_char(p2_cid) if p2_cid else "").split(
                normalize_side(persp or 1)
            )
        )
        rid = int(bm.get("round_id") or 0) or None
        ctx = _resolve_context_for_item(
            quark_id=qid,
            item_perspective=persp,
            round_id=rid,
            username=uname,
            match_by_qid=match_by_qid,
            manifest_cache=manifest_cache,
            data_root=data_root,
            fallback_self_char=fb_self,
            fallback_opp_char=fb_opp,
        )
        start_gf = int(bm.get("start_gf") or 0)
        end_gf = int(bm.get("end_gf") or start_gf)
        prev = _round_video_preview(
            ctx["round_doc"],
            quark_id=qid,
            start_gf=start_gf,
            end_gf=end_gf,
            anchor_gf=start_gf,
            data_root=data_root,
        )
        fs_path = Path(str(bm.get("savestate") or "")) if bm.get("savestate") else None
        fs_ready = bool(fs_path and fs_path.exists() and fs_path.stat().st_size > 4096)
        r_label = (
            f"M{prev['match_id']} · R{prev['match_round']}"
            if prev["match_id"]
            else (f"R{rid}" if rid else "Bookmark")
        )
        notes.append(
            {
                "id": bm_id,
                "source": "bookmark",
                "entry_type": "bookmark",
                "kind": "note",
                "tags": list(bm.get("tags") or []),
                "note": note_text,
                "needs_note": False,
                "quark_id": qid,
                "date": ctx["date"],
                "date_str": ctx["date_str"],
                "perspective": ctx["perspective"],
                "self_player_name": ctx["self_player_name"],
                "opp_player_name": ctx["opp_player_name"],
                "self_rank_letter": ctx["self_rank_letter"],
                "opp_rank_letter": ctx["opp_rank_letter"],
                "self_char_name": ctx["self_char_name"],
                "opp_char_name": ctx["opp_char_name"],
                "round_id": rid,
                "match_id": prev["match_id"],
                "match_round": prev["match_round"],
                "round_label": r_label,
                "anchor_gf": start_gf,
                "start_gf": start_gf,
                "end_gf": end_gf,
                "is_range": end_gf > start_gf,
                "has_video": prev["has_video"],
                "has_set_video": ctx["has_set_video"],
                "video_url": prev["video_url"],
                "overlay_url": prev.get("overlay_url"),
                "round_start_gf": prev["round_start_gf"],
                "round_end_gf": prev["round_end_gf"],
                "round_fps": prev["round_fps"],
                "clip_start_s": prev["clip_start_s"],
                "clip_end_s": prev["clip_end_s"],
                "anchor_s": prev["anchor_s"],
                "queued_drill": fs_ready,
                "drill_status": "ready" if fs_ready else "none",
                "can_launch_drill": fs_ready,
                "editable": False,
                "deletable": False,
                "created_at": _format_iso_from_any(bm.get("created_at"), ctx["date_str"]),
                "updated_at": _format_iso_from_any(bm.get("created_at"), ctx["date_str"]),
                "sort_ts": _sort_key_ts(bm.get("created_at"), None, ctx["date"]),
            }
        )

    # Sort newest first
    notes.sort(key=lambda n: (float(n.get("sort_ts") or 0.0), str(n.get("id") or "")), reverse=True)

    legacy_kind_to_tag = {
        "mistake": "Mistake",
        "question": "Question",
        "good_play": "Good Play",
        "opp_habit": "Opp Habit",
    }
    by_kind: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    for n in notes:
        k = str(n.get("kind") or "note")
        by_kind[k] = by_kind.get(k, 0) + 1
        seen_tags_lower: set[str] = set()
        for t in n.get("tags") or []:
            tag_counts[t] = tag_counts.get(t, 0) + 1
            seen_tags_lower.add(str(t).lower())
        legacy_lbl = legacy_kind_to_tag.get(k)
        if legacy_lbl and legacy_lbl.lower() not in seen_tags_lower:
            tag_counts[legacy_lbl] = tag_counts.get(legacy_lbl, 0) + 1

    by_tag = [
        {"tag": t, "count": c}
        for t, c in sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    ]
    avail_self = sorted({str(n["self_char_name"]) for n in notes if n.get("self_char_name")})
    avail_opp = sorted({str(n["opp_char_name"]) for n in notes if n.get("opp_char_name")})

    matchup_keys = {
        (str(n.get("self_char_name") or ""), str(n.get("opp_char_name") or ""))
        for n in notes
        if n.get("self_char_name") or n.get("opp_char_name")
    }
    counts = {
        "total": len(notes),
        "moments": sum(1 for n in notes if n["entry_type"] == "moment"),
        "set_notes": sum(1 for n in notes if n["entry_type"] == "set_note"),
        "select_marks": sum(1 for n in notes if n["entry_type"] == "select_mark"),
        "bookmarks": sum(1 for n in notes if n["entry_type"] == "bookmark"),
        "drills": sum(1 for n in notes if n.get("can_launch_drill") or n.get("queued_drill")),
        "needs_note": sum(1 for n in notes if n.get("needs_note")),
        "matchups_count": len(matchup_keys),
        "by_kind": by_kind,
        "by_tag": by_tag,
    }

    return {
        "ok": True,
        "username": uname,
        "notes": notes,
        "counts": counts,
        "available_chars": {"self": avail_self, "opp": avail_opp},
    }
