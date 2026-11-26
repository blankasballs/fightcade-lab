"""Unified moment annotations, set-level notes, and automatic Takeover Drill baking.

Every mobile annotation is anchored to an exact deterministic `game_frame` (`0x02007F00`),
with an optional `[start_gf .. end_gf]` range, structured Milestone 5 quick-tags, freeform note,
and a `queued_drill` toggle.

When `queued_drill` is enabled, this module automatically bakes an instant `<1s` `.fs` + `.inputs`
savestate offline from `data/replay_cache/<qid>/rNN_gfXXXX.fs` and upserts it into
`data/selections/queue.jsonl` so it appears immediately on the desktop Takeover Queue with the
user's mobile note & tags attached.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Callable
import uuid

from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR
from fightcade_analysis.round_renderer import bake_drill_savestate, mobile_manifest_path
from fightcade_analysis.select_queue import (
    _read_jsonl,
    _write_jsonl_atomic,
    update_queue_item_status,
    upsert_queue_items,
)

PRESET_TAGS: tuple[str, ...] = (
    "Anti-Air",
    "Whiff Punish",
    "Throw / Tech",
    "Wakeup / Oki",
    "Corner",
    "Parry",
    "Meter / Super",
    "Footsies",
    "Combo / Confirm",
    "Good Decision",
)

VALID_KINDS: frozenset[str] = frozenset({"mistake", "question", "good_play", "opp_habit", "note"})
_QUARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_ANN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")

_ANN_LOCK = threading.Lock()


def annotations_jsonl_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "selections" / "annotations.jsonl"


def set_notes_jsonl_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "selections" / "set_notes.jsonl"


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


def _sanitize_note(raw: Any, *, max_len: int = 2000) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:max_len]


def _sanitize_id_list(raw_ids: Any) -> list[str]:
    if not raw_ids:
        return []
    if isinstance(raw_ids, str):
        raw_ids = [s.strip() for s in raw_ids.split(",")]
    if not isinstance(raw_ids, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in raw_ids:
        clean = str(x or "").strip()
        if clean and _ANN_ID_RE.match(clean) and clean not in seen:
            seen.add(clean)
            out.append(clean)
        if len(out) >= 24:
            break
    return out


def _lookup_round_meta(quark_id: str, round_id: int, data_dir: Path) -> dict[str, Any]:
    mf_path = mobile_manifest_path(quark_id, data_dir=data_dir)
    if not mf_path.exists():
        return {}
    try:
        doc = json.loads(mf_path.read_text(encoding="utf-8"))
        for r in doc.get("rounds", []):
            if int(r.get("round_id", 0)) == int(round_id):
                return {
                    "round_start_gf": int(r.get("start_gf", 0)),
                    "round_end_gf": int(r.get("end_gf", 0)),
                    "p1_char": int(r.get("p1_char", doc.get("p1_char", 0))),
                    "p2_char": int(r.get("p2_char", doc.get("p2_char", 0))),
                    "perspective": int(doc.get("perspective", 1)),
                }
        return {
            "p1_char": int(doc.get("p1_char", 0)),
            "p2_char": int(doc.get("p2_char", 0)),
            "perspective": int(doc.get("perspective", 1)),
        }
    except Exception:  # noqa: BLE001
        return {}


def list_annotations(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    quark_id: str | None = None,
    round_id: int | None = None,
) -> list[dict[str, Any]]:
    with _ANN_LOCK:
        items = _read_jsonl(annotations_jsonl_path(data_dir))
    if quark_id:
        items = [it for it in items if str(it.get("quark_id")) == str(quark_id)]
    if round_id is not None:
        items = [it for it in items if int(it.get("round_id", 0)) == int(round_id)]
    for it in items:
        it.setdefault("owner", "")
        it.setdefault("goal_ids", [])
        it.setdefault("playlist_ids", [])
    items.sort(key=lambda it: (str(it.get("quark_id", "")), int(it.get("round_id", 0)), int(it.get("anchor_gf", 0))))
    return items


def get_annotation(ann_id: str, data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any] | None:
    with _ANN_LOCK:
        items = _read_jsonl(annotations_jsonl_path(data_dir))
    for it in items:
        if str(it.get("id")) == str(ann_id):
            return it
    return None


def _update_annotation_drill_state(
    ann_id: str,
    *,
    drill_status: str,
    drill_error: str | None = None,
    savestate: str | None = None,
    inputs: str | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> None:
    with _ANN_LOCK:
        path = annotations_jsonl_path(data_dir)
        items = _read_jsonl(path)
        changed = False
        for it in items:
            if str(it.get("id")) == str(ann_id):
                it["drill_status"] = drill_status
                it["drill_error"] = drill_error
                if savestate is not None:
                    it["savestate"] = savestate
                if inputs is not None:
                    it["inputs"] = inputs
                it["updated_at"] = datetime.now().isoformat(timespec="seconds")
                changed = True
                break
        if changed:
            _write_jsonl_atomic(path, items)


def _sync_annotation_to_queue(
    ann: dict[str, Any],
    *,
    data_dir: Path,
    bake_fn: Callable[..., dict[str, Any]] | None = None,
    async_bake: bool = True,
) -> dict[str, Any]:
    """Ensure a drill-flagged annotation has a `.fs` savestate + `queue.jsonl` entry."""
    data_root = data_dir.resolve()
    ann_id = str(ann["id"])
    qid = str(ann["quark_id"])
    persp = int(ann.get("perspective", 1))
    rmeta = _lookup_round_meta(qid, int(ann.get("round_id", 0)), data_root)
    p1_cid = int(rmeta.get("p1_char", 0))
    p2_cid = int(rmeta.get("p2_char", 0))
    self_cid = p1_cid if persp == 1 else p2_cid
    opp_cid = p2_cid if persp == 1 else p1_cid

    sel_dir = data_root / "selections"
    expected_fs = sel_dir / f"{ann_id}.fs"
    expected_inp = sel_dir / f"{ann_id}.inputs"

    tag_prefix = f"[{', '.join(ann.get('tags') or [])}] " if ann.get("tags") else ""
    note_summary = f"{tag_prefix}{ann.get('note') or ''}".strip() or f"Review drill ({ann.get('kind', 'note')})"

    queue_item = {
        "id": ann_id,
        "source": "mobile_note",
        "quark_id": qid,
        "perspective": persp,
        "round_id": int(ann.get("round_id", 0)),
        "start_gf": int(ann["start_gf"]),
        "end_gf": int(ann["end_gf"]),
        "press_gf": int(ann["anchor_gf"]),
        "takeover_gf": int(ann.get("takeover_gf") or (int(ann["start_gf"]) + 20)),
        "self_char": self_cid,
        "opp_char": opp_cid,
        "kind": ann.get("kind", "note"),
        "tags": list(ann.get("tags") or []),
        "note": note_summary[:180],
        "savestate": str(expected_fs),
        "inputs": str(expected_inp),
        "status": "pending",
    }

    # If item already existed in queue (e.g. re-enabled after dismiss), ensure status=pending
    upsert_queue_items([queue_item], data_dir=data_root)
    try:
        update_queue_item_status(ann_id, "pending", data_dir=data_root)
    except Exception:  # noqa: BLE001
        pass

    baker = bake_fn or bake_drill_savestate

    def _run_bake() -> None:
        try:
            res = baker(
                qid,
                int(ann["start_gf"]),
                int(ann["end_gf"]),
                selection_id=ann_id,
                data_dir=data_root,
            )
            _update_annotation_drill_state(
                ann_id,
                drill_status="ready",
                drill_error=None,
                savestate=str(res.get("savestate") or expected_fs),
                inputs=str(res.get("inputs") or expected_inp),
                data_dir=data_root,
            )
        except Exception as exc:  # noqa: BLE001
            _update_annotation_drill_state(
                ann_id,
                drill_status="error",
                drill_error=str(exc),
                data_dir=data_root,
            )

    if async_bake:
        _update_annotation_drill_state(ann_id, drill_status="baking", drill_error=None, data_dir=data_root)
        ann = dict(ann)
        ann["drill_status"] = "baking"
        threading.Thread(target=_run_bake, name=f"fc-bake-{ann_id[:16]}", daemon=True).start()
    else:
        _run_bake()
        ann = get_annotation(ann_id, data_dir=data_root) or ann
    return ann


def create_annotation(
    payload: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    bake_fn: Callable[..., dict[str, Any]] | None = None,
    async_bake: bool = True,
) -> dict[str, Any]:
    """Validate and store a new moment annotation; optionally bake `.fs` into Takeover Queue."""
    if not isinstance(payload, dict):
        raise ValueError("Annotation payload must be a JSON object")

    qid = str(payload.get("quark_id") or "").strip()
    if not _QUARK_ID_RE.match(qid):
        raise ValueError("Invalid quark_id format")

    try:
        round_id = int(payload.get("round_id", 0))
        anchor_gf = int(payload.get("anchor_gf", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("round_id and anchor_gf must be integers") from exc
    if round_id < 1 or anchor_gf < 1:
        raise ValueError("round_id and anchor_gf must be positive integers")

    data_root = data_dir.resolve()
    rmeta = _lookup_round_meta(qid, round_id, data_root)
    round_start_gf = int(rmeta.get("round_start_gf") or 1)
    round_end_gf = int(rmeta.get("round_end_gf") or (anchor_gf + 90))

    persp_raw = payload.get("perspective", rmeta.get("perspective", 1))
    try:
        perspective = int(persp_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("perspective must be 1 or 2") from exc
    if perspective not in (1, 2):
        raise ValueError("perspective must be 1 or 2")

    # Optional explicit range [start_gf .. end_gf]; otherwise smart default around anchor_gf
    raw_start = payload.get("start_gf")
    raw_end = payload.get("end_gf")
    if raw_start not in (None, ""):
        start_gf = max(round_start_gf, int(raw_start))
    else:
        start_gf = max(round_start_gf, anchor_gf - 180)
    if raw_end not in (None, ""):
        end_gf = max(start_gf + 1, int(raw_end))
    else:
        end_gf = max(start_gf + 30, min(round_end_gf, anchor_gf + 90))

    takeover_gf = max(start_gf + 20, min(end_gf, anchor_gf - 30)) if (anchor_gf - start_gf) >= 50 else (start_gf + 20)

    kind = str(payload.get("kind") or "note").strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(VALID_KINDS))}")

    tags = _sanitize_tags(payload.get("tags"))
    note = _sanitize_note(payload.get("note"))
    queued_drill = bool(payload.get("queued_drill", False))
    is_range = bool(payload.get("is_range", raw_start not in (None, "") or raw_end not in (None, "")))
    owner = str(payload.get("owner") or "").strip()
    goal_ids = _sanitize_id_list(payload.get("goal_ids"))
    playlist_ids = _sanitize_id_list(payload.get("playlist_ids"))

    short_qid = qid.split("-")[-1] if "-" in qid else qid[-6:]
    ann_id = f"mob_{short_qid}_r{round_id:02d}_gf{anchor_gf}_{uuid.uuid4().hex[:4]}"
    now_iso = datetime.now().isoformat(timespec="seconds")

    record: dict[str, Any] = {
        "id": ann_id,
        "owner": owner,
        "quark_id": qid,
        "round_id": round_id,
        "perspective": perspective,
        "anchor_gf": anchor_gf,
        "start_gf": start_gf,
        "end_gf": end_gf,
        "takeover_gf": takeover_gf,
        "is_range": is_range,
        "kind": kind,
        "tags": tags,
        "goal_ids": goal_ids,
        "playlist_ids": playlist_ids,
        "note": note,
        "queued_drill": queued_drill,
        "drill_status": "baking" if queued_drill else "none",
        "drill_error": None,
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    with _ANN_LOCK:
        path = annotations_jsonl_path(data_root)
        items = _read_jsonl(path)
        items.append(record)
        _write_jsonl_atomic(path, items)

    if queued_drill:
        record = _sync_annotation_to_queue(record, data_dir=data_root, bake_fn=bake_fn, async_bake=async_bake)

    return record


def update_annotation(
    ann_id: str,
    patch: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    bake_fn: Callable[..., dict[str, Any]] | None = None,
    async_bake: bool = True,
) -> dict[str, Any] | None:
    """Patch note/tags/kind/range/queued_drill on an existing annotation."""
    if not _ANN_ID_RE.match(str(ann_id or "")):
        raise ValueError("Invalid annotation id")
    if not isinstance(patch, dict):
        raise ValueError("Patch payload must be a JSON object")

    data_root = data_dir.resolve()
    need_bake = False
    need_dismiss = False
    updated: dict[str, Any] | None = None

    with _ANN_LOCK:
        path = annotations_jsonl_path(data_root)
        items = _read_jsonl(path)
        for it in items:
            if str(it.get("id")) != str(ann_id):
                continue
            old_queued = bool(it.get("queued_drill", False))
            old_start = int(it.get("start_gf", 0))
            old_end = int(it.get("end_gf", 0))

            if "note" in patch:
                it["note"] = _sanitize_note(patch["note"])
            if "tags" in patch:
                it["tags"] = _sanitize_tags(patch["tags"])
            if "kind" in patch:
                k = str(patch["kind"] or "note").strip().lower()
                if k not in VALID_KINDS:
                    raise ValueError(f"Invalid kind: {k}")
                it["kind"] = k
            if "start_gf" in patch and patch["start_gf"] not in (None, ""):
                it["start_gf"] = max(1, int(patch["start_gf"]))
                it["is_range"] = True
            if "end_gf" in patch and patch["end_gf"] not in (None, ""):
                it["end_gf"] = max(int(it["start_gf"]) + 1, int(patch["end_gf"]))
                it["is_range"] = True
            if "queued_drill" in patch:
                it["queued_drill"] = bool(patch["queued_drill"])
            if "owner" in patch:
                it["owner"] = str(patch["owner"] or "").strip()
            if "goal_ids" in patch:
                it["goal_ids"] = _sanitize_id_list(patch["goal_ids"])
            if "playlist_ids" in patch:
                it["playlist_ids"] = _sanitize_id_list(patch["playlist_ids"])
            it.setdefault("owner", "")
            it.setdefault("goal_ids", [])
            it.setdefault("playlist_ids", [])

            new_queued = bool(it.get("queued_drill", False))
            range_changed = (int(it["start_gf"]) != old_start) or (int(it["end_gf"]) != old_end)
            if new_queued and (not old_queued or range_changed or it.get("drill_status") in ("none", "error")):
                need_bake = True
                it["drill_status"] = "baking"
                it["drill_error"] = None
            elif not new_queued and old_queued:
                need_dismiss = True
                it["drill_status"] = "none"
                it["drill_error"] = None

            it["updated_at"] = datetime.now().isoformat(timespec="seconds")
            updated = dict(it)
            break

        if updated is not None:
            _write_jsonl_atomic(path, items)

    if updated is None:
        return None

    if need_bake:
        updated = _sync_annotation_to_queue(updated, data_dir=data_root, bake_fn=bake_fn, async_bake=async_bake)
    elif need_dismiss:
        try:
            update_queue_item_status(ann_id, "dismissed", data_dir=data_root)
        except Exception:  # noqa: BLE001
            pass
    return updated


def delete_annotation(ann_id: str, *, data_dir: Path = DEFAULT_DATA_DIR) -> bool:
    if not _ANN_ID_RE.match(str(ann_id or "")):
        raise ValueError("Invalid annotation id")
    data_root = data_dir.resolve()
    removed = False
    with _ANN_LOCK:
        path = annotations_jsonl_path(data_root)
        items = _read_jsonl(path)
        kept = [it for it in items if str(it.get("id")) != str(ann_id)]
        if len(kept) != len(items):
            removed = True
            _write_jsonl_atomic(path, kept)
    if removed:
        try:
            update_queue_item_status(ann_id, "dismissed", data_dir=data_root)
        except Exception:  # noqa: BLE001
            pass
    return removed


def get_set_notes_map(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, dict[str, Any]]:
    with _ANN_LOCK:
        items = _read_jsonl(set_notes_jsonl_path(data_dir))
    return {str(it.get("quark_id")): it for it in items if it.get("quark_id")}


def get_set_note(quark_id: str, data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    existing = get_set_notes_map(data_dir).get(str(quark_id))
    if existing:
        return existing
    return {"quark_id": str(quark_id), "note": "", "tags": [], "updated_at": None}


def upsert_set_note(
    quark_id: str,
    *,
    note: str = "",
    tags: list[str] | None = None,
    owner: str = "",
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    qid = str(quark_id or "").strip()
    if not _QUARK_ID_RE.match(qid):
        raise ValueError("Invalid quark_id format")
    rec = {
        "quark_id": qid,
        "owner": str(owner or "").strip(),
        "note": _sanitize_note(note, max_len=4000),
        "tags": _sanitize_tags(tags),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _ANN_LOCK:
        path = set_notes_jsonl_path(data_dir)
        items = _read_jsonl(path)
        by_qid = {str(it.get("quark_id")): it for it in items if it.get("quark_id")}
        by_qid[qid] = rec
        _write_jsonl_atomic(path, list(by_qid.values()))
    return rec
