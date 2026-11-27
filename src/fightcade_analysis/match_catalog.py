"""Shared Fightcade Match History Catalog & Netplay-Safe Sequential Processing Queue.

Merges cached Fightcade `searchquarks` match history (`data/match_history_<username>.json` +
legacy `data/<username>_replays.json`) with live local artifact checks across `data/` so both
the Mobile Companion (`/mobile`) and desktop Reports Hub share one source of truth:
  - `has_telemetry`   (`data/personal/<qid>.parquet` or `data/parquet/<qid>.parquet`)
  - `has_round_cache` (`data/replay_cache/<qid>/rNN_gfXXXX.fs`)
  - `has_mobile_video` (`data/replays/<qid>/manifest.json` + round MP4s)
  - `has_report`      (`data/reports/*<short_qid>*.html`)
  - `notes_count` / `drills_count` (`annotations.jsonl` + `queue.jsonl`)

Also provides `ProcessingQueue`, a single sequential worker thread (`concurrency = 1` for
Fightcade streams) that handles BOTH live auto-watched matches and on-demand mobile/API
processing requests while yielding automatically whenever a live Fightcade match is active.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import random
import re
import threading
import time
from typing import Any, Callable

import pandas as pd

from fightcade_analysis.annotations import get_set_notes_map, list_annotations
from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR, find_parquet_for_quark
from fightcade_analysis.coaching import pretty_char
from fightcade_analysis.fetch_replays import FightcadeRateLimitError, fetch_quarks, summarize_quark
from fightcade_analysis.naming import format_rank_letter, lookup_replay_metadata
from fightcade_analysis.perspective import (
    SidePair,
    extract_p1_p2_from_manifest,
    is_placeholder_name,
    resolve_viewer_side,
)
from fightcade_analysis.round_renderer import (
    ensure_manifest_matches,
    list_cached_round_savestates,
    mobile_manifest_path,
    prune_old_mobile_replays,
    render_replay_rounds,
)
from fightcade_analysis.select_queue import load_queue, scan_replay_for_select_marks

_QUARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_FC_URL_QID_RE = re.compile(r"replay\.fightcade\.com/[^/]+/[^/]+/([A-Za-z0-9_-]{6,64})", re.IGNORECASE)
_PLAIN_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")

# Fightcade Stewardship Safeguards
DEFAULT_REFRESH_COOLDOWN_S: float = 60.0
DEFAULT_STREAM_COOLDOWN_RANGE: tuple[float, float] = (10.0, 15.0)
DEFAULT_MAX_BACKFILL_PER_HOUR: int = 12

_REFRESH_LOCK = threading.Lock()
_REFRESH_TIMESTAMPS: dict[tuple[str, str], float] = {}


class RefreshCooldownError(RuntimeError):
    """Raised when `Refresh` is clicked before the 60s Fightcade API cooldown has elapsed."""

    def __init__(self, message: str, *, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = max(1.0, float(retry_after_s))


class HourlyBackfillCapError(RuntimeError):
    """Raised when manual Fightcade replay streams hit the hourly stewardship cap (10–15/hr)."""

    def __init__(self, message: str, *, retry_after_s: float, cap: int, used: int) -> None:
        super().__init__(message)
        self.retry_after_s = max(1.0, float(retry_after_s))
        self.cap = int(cap)
        self.used = int(used)


def reset_refresh_cooldowns() -> None:
    """Clear in-memory refresh cooldown timestamps (used by unit tests)."""
    with _REFRESH_LOCK:
        _REFRESH_TIMESTAMPS.clear()


def validate_username(username: Any) -> str:
    """Validate a Fightcade handle (allows punctuation like `!!M_DAVE!!`, rejects control chars / path separators)."""
    clean = str(username or "").strip()
    if not clean or len(clean) > 64:
        raise ValueError(f"Invalid Fightcade username: {username!r}")
    if "/" in clean or "\\" in clean or ".." in clean:
        raise ValueError(f"Invalid Fightcade username: {username!r}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in clean):
        raise ValueError(f"Invalid Fightcade username: {username!r}")
    return clean


def parse_quark_id_or_url(raw: str) -> str:
    """Accept either a bare `quarkid` or a Fightcade replay URL and return the clean `quark_id`."""
    text = str(raw or "").strip()
    m = _FC_URL_QID_RE.search(text)
    if m:
        return m.group(1)
    # Strip trailing .<ver> if copied from fcade://stream/.../<qid>.7,7100
    text = text.split(",")[0].split(".")[0].strip()
    if not _QUARK_ID_RE.match(text):
        raise ValueError(f"Invalid quark_id or Fightcade replay URL: {raw!r}")
    return text


def detect_default_username(data_dir: Path = DEFAULT_DATA_DIR) -> str:
    """Pick default Fightcade handle from `data/config.json` or existing `<user>_replays.json`."""
    data_root = data_dir.resolve()
    cfg_path = data_root / "config.json"
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            u = str(cfg.get("username") or "").strip()
            if u:
                return u
        except Exception:  # noqa: BLE001
            pass
    for p in sorted(data_root.glob("match_history_*.json")):
        stem = p.stem.replace("match_history_", "", 1)
        if stem:
            return stem
    for p in sorted(data_root.glob("*_replays.json")):
        stem = p.stem[:-len("_replays")]
        if stem and stem.lower() not in ("replays", "starter10"):
            return stem
    return ""


def _has_configured_username(data_dir: Path = DEFAULT_DATA_DIR) -> bool:
    cfg_path = data_dir.resolve() / "config.json"
    if not cfg_path.exists():
        return False
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return bool(str(cfg.get("username") or "").strip())
    except Exception:  # noqa: BLE001
        return False


def save_default_username(username: str, data_dir: Path = DEFAULT_DATA_DIR) -> None:
    try:
        clean = validate_username(username)
    except ValueError:
        return
    cfg_path = data_dir.resolve() / "config.json"
    doc: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            doc = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            doc = {}
    doc["username"] = clean
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _history_cache_path(username: str, data_dir: Path) -> Path:
    clean = str(username or "").strip()
    if _PLAIN_USER_RE.match(clean) and clean not in (".", ".."):
        return data_dir.resolve() / f"match_history_{clean}.json"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", clean).strip("._") or "user"
    digest = hashlib.sha1(clean.lower().encode("utf-8")).hexdigest()[:8]
    return data_dir.resolve() / f"match_history_{slug}_{digest}.json"


def _load_history_summaries(username: str, data_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load merged cached Fightcade summaries from `match_history_<user>.json` & `<user>_replays.json`."""
    data_root = data_dir.resolve()
    by_qid: dict[str, dict[str, Any]] = {}
    meta: dict[str, Any] = {"last_refreshed": None, "next_offset": 0}

    candidates: list[Path] = []
    clean = str(username or "").strip()
    if _PLAIN_USER_RE.match(clean) and clean not in (".", ".."):
        candidates.append(data_root / f"{clean}_replays.json")
    candidates.append(_history_cache_path(clean, data_root))
    for path in candidates:
        if not path.exists():
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if path.name.startswith("match_history_"):
            meta["last_refreshed"] = doc.get("last_refreshed")
            meta["next_offset"] = int(doc.get("next_offset") or 0)
        for s in doc.get("summaries") or []:
            if isinstance(s, dict) and s.get("quarkid"):
                qid = str(s["quarkid"])
                prev = by_qid.get(qid, {})
                merged = dict(prev)
                merged.update({k: v for k, v in s.items() if v is not None})
                by_qid[qid] = merged
        # Also recover raw entries if summaries weren't saved
        for r in doc.get("raw") or []:
            if isinstance(r, dict) and r.get("quarkid"):
                qid = str(r["quarkid"])
                if qid not in by_qid:
                    by_qid[qid] = summarize_quark(r)
    return by_qid, meta


def refresh_match_history(
    username: str,
    *,
    game_id: str = "sfiii3nr1",
    load_older: bool = False,
    page_size: int = 25,
    max_pages: int = 2,
    data_dir: Path = DEFAULT_DATA_DIR,
    fetch_fn: Callable[..., list[dict[str, Any]]] = fetch_quarks,
    cooldown_seconds: float = DEFAULT_REFRESH_COOLDOWN_S,
    clock_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Fetch Fightcade match history for `username` and merge into `data/match_history_<user>.json`.

    Enforces a 60-second per-user `Refresh` cooldown and incremental stop-at-known-`quark_id`
    pagination so Fightcade's public `searchquarks` API is never hammered.
    """
    uname = validate_username(username)

    data_root = data_dir.resolve()
    cd_key = (str(data_root).lower(), uname.lower())
    now_mono = float(clock_fn())
    if cooldown_seconds > 0 and not load_older:
        with _REFRESH_LOCK:
            last_ts = _REFRESH_TIMESTAMPS.get(cd_key)
            if last_ts is not None:
                elapsed = now_mono - last_ts
                if elapsed < cooldown_seconds:
                    remaining = max(1.0, cooldown_seconds - elapsed)
                    raise RefreshCooldownError(
                        f"Refresh cooldown active — wait {int(round(remaining))}s before querying Fightcade again.",
                        retry_after_s=remaining,
                    )

    by_qid, meta = _load_history_summaries(uname, data_root)
    start_offset = int(meta.get("next_offset") or 0) if load_older else 0

    fetch_kwargs: dict[str, Any] = {
        "gameid": game_id,
        "username": uname,
        "best": False,
        "limit": page_size * max_pages,
        "offset": start_offset,
        "max_pages": max_pages,
        "page_delay": 1.0,
    }
    if not load_older and by_qid:
        try:
            sig = inspect.signature(fetch_fn)
            has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
            if "stop_at_known_ids" in sig.parameters or has_varkw:
                fetch_kwargs["stop_at_known_ids"] = set(by_qid.keys())
        except (TypeError, ValueError):
            pass

    raw_items = fetch_fn(**fetch_kwargs)
    if cooldown_seconds > 0 and not load_older:
        with _REFRESH_LOCK:
            _REFRESH_TIMESTAMPS[cd_key] = float(clock_fn())
    added = 0
    for item in raw_items:
        summ = summarize_quark(item)
        qid = str(summ.get("quarkid") or "")
        if not qid:
            continue
        if qid not in by_qid:
            added += 1
        prev = by_qid.get(qid, {})
        merged = dict(prev)
        merged.update({k: v for k, v in summ.items() if v is not None})
        by_qid[qid] = merged

    sorted_summaries = sorted(by_qid.values(), key=lambda s: int(s.get("date") or 0), reverse=True)
    new_offset = max(int(meta.get("next_offset") or 0), start_offset + len(raw_items))
    now_iso = datetime.now().isoformat(timespec="seconds")

    cache_path = _history_cache_path(uname, data_root)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(f".tmp_{os.getpid()}")
    tmp.write_text(
        json.dumps(
            {
                "username": uname,
                "game_id": game_id,
                "last_refreshed": now_iso,
                "next_offset": new_offset,
                "total": len(sorted_summaries),
                "summaries": sorted_summaries,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    tmp.replace(cache_path)
    if not _has_configured_username(data_root):
        save_default_username(uname, data_dir=data_root)

    return {
        "ok": True,
        "username": uname,
        "fetched": len(raw_items),
        "added": added,
        "total": len(sorted_summaries),
        "next_offset": new_offset,
        "last_refreshed": now_iso,
    }


# Lightweight in-memory cache of Parquet summary stats keyed by (path, mtime_ns)
_PQ_META_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


def _read_parquet_summary(pq_path: Path) -> dict[str, Any]:
    try:
        st = pq_path.stat()
    except OSError:
        return {}
    key = (str(pq_path), int(st.st_mtime_ns))
    cached = _PQ_META_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        df = pd.read_parquet(pq_path, columns=["round_id", "p1_char", "p2_char"])
        p1_cid = int(df["p1_char"].mode().iloc[0]) if len(df) else 0
        p2_cid = int(df["p2_char"].mode().iloc[0]) if len(df) else 0
        n_rounds = int(df["round_id"].nunique()) if len(df) else 0
        p1_chars: list[str] = []
        p2_chars: list[str] = []
        for _, grp in df.groupby("round_id", sort=True):
            c1 = pretty_char(int(grp["p1_char"].mode().iloc[0]))
            c2 = pretty_char(int(grp["p2_char"].mode().iloc[0]))
            if c1 and c1 not in p1_chars:
                p1_chars.append(c1)
            if c2 and c2 not in p2_chars:
                p2_chars.append(c2)
        info = {
            "p1_char": p1_cid,
            "p2_char": p2_cid,
            "p1_char_name": pretty_char(p1_cid),
            "p2_char_name": pretty_char(p2_cid),
            "p1_chars": p1_chars,
            "p2_chars": p2_chars,
            "round_count": n_rounds,
        }
        _PQ_META_CACHE[key] = info
        return info
    except Exception:  # noqa: BLE001
        return {}


def _index_reports_by_qid(data_dir: Path) -> dict[str, str]:
    reports_dir = data_dir / "reports"
    out: dict[str, str] = {}
    if not reports_dir.is_dir():
        return out
    for jf in reports_dir.glob("*.json"):
        if jf.name.startswith("gemini_") and jf.name.endswith("_cache.json"):
            continue
        try:
            doc = json.loads(jf.read_text(encoding="utf-8"))
            qid = str(doc.get("target_quark_id") or "")
            if qid and doc.get("report_type", "full_set") == "full_set":
                out[qid] = f"/#report/{jf.stem}"
        except Exception:  # noqa: BLE001
            continue
    return out


def _format_date_iso(epoch_ms: Any) -> str:
    try:
        val = int(epoch_ms or 0)
        if val <= 0:
            return ""
        ts = val / 1000.0 if val > 10_000_000_000 else float(val)
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


def _epoch_ms_from_quark_id(quark_id: str, fallback_mtime_s: float) -> int:
    m = re.match(r"^(\d{12,14})(?:-|$)", str(quark_id or ""))
    if m:
        return int(m.group(1))
    return int(fallback_mtime_s * 1000)


def list_matches(
    *,
    username: str | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    job_queue: ProcessingQueue | None = None,
    status_filter: str = "all",
    self_char: str | None = None,
    opp_char: str | None = None,
) -> dict[str, Any]:
    """Return merged Fightcade match history + local artifact status indicators + job states."""
    data_root = data_dir.resolve()
    default_user = detect_default_username(data_root)
    uname = validate_username(username) if (username or "").strip() else default_user
    is_default_user = uname.lower() == default_user.lower()
    uname_low = uname.lower()
    by_qid, hist_meta = _load_history_summaries(uname, data_root)

    def _local_matches_user(p1_n: Any, p2_n: Any) -> bool:
        p1_s = str(p1_n or "").strip()
        p2_s = str(p2_n or "").strip()
        p1_known = not is_placeholder_name(p1_s)
        p2_known = not is_placeholder_name(p2_s)
        if p1_known or p2_known:
            return (p1_known and p1_s.lower() == uname_low) or (p2_known and p2_s.lower() == uname_low)
        return is_default_user

    # Include locally present personal parquets / mobile replays ONLY when they belong to `uname`
    # (or have unknown player names when viewing the configured default user).
    personal_dir = data_root / "personal"
    if personal_dir.is_dir():
        for pq in personal_dir.glob("*.parquet"):
            qid = pq.stem
            if qid not in by_qid:
                meta = lookup_replay_metadata(qid, data_dir=data_root)
                p1_n = meta.get("p1_name") or ""
                p2_n = meta.get("p2_name") or ""
                if not _local_matches_user(p1_n, p2_n):
                    continue
                by_qid[qid] = {
                    "quarkid": qid,
                    "date": meta.get("date") or _epoch_ms_from_quark_id(qid, pq.stat().st_mtime),
                    "p1_name": p1_n,
                    "p2_name": p2_n,
                    "p1_rank": meta.get("p1_rank"),
                    "p2_rank": meta.get("p2_rank"),
                    "p1_score": meta.get("p1_score"),
                    "p2_score": meta.get("p2_score"),
                    "ranked": meta.get("ranked"),
                    "duration": meta.get("duration") or 0,
                }
    replays_dir = data_root / "replays"
    if replays_dir.is_dir():
        for d in replays_dir.iterdir():
            if d.is_dir() and (d / "manifest.json").exists():
                qid = d.name
                if qid not in by_qid:
                    meta = lookup_replay_metadata(qid, data_dir=data_root)
                    p1_n = str(meta.get("p1_name") or "").strip()
                    p2_n = str(meta.get("p2_name") or "").strip()
                    if is_placeholder_name(p1_n) or is_placeholder_name(p2_n):
                        try:
                            mf_doc_tmp = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
                            mf_p1_tmp, mf_p2_tmp = extract_p1_p2_from_manifest(mf_doc_tmp)
                            if is_placeholder_name(p1_n) and not is_placeholder_name(mf_p1_tmp.get("name")):
                                p1_n = str(mf_p1_tmp["name"]).strip()
                            if is_placeholder_name(p2_n) and not is_placeholder_name(mf_p2_tmp.get("name")):
                                p2_n = str(mf_p2_tmp["name"]).strip()
                        except Exception:  # noqa: BLE001
                            pass
                    if not _local_matches_user(p1_n, p2_n):
                        continue
                    by_qid[qid] = {
                        "quarkid": qid,
                        "date": meta.get("date") or _epoch_ms_from_quark_id(qid, (d / "manifest.json").stat().st_mtime),
                        "p1_name": p1_n,
                        "p2_name": p2_n,
                        "p1_rank": meta.get("p1_rank"),
                        "p2_rank": meta.get("p2_rank"),
                        "p1_score": meta.get("p1_score"),
                        "p2_score": meta.get("p2_score"),
                        "ranked": meta.get("ranked"),
                        "duration": meta.get("duration") or 0,
                    }

    # Pre-index annotations, set notes, drills, reports, and jobs
    all_anns = list_annotations(data_root)
    ann_counts: dict[str, int] = {}
    for a in all_anns:
        q = str(a.get("quark_id") or "")
        ann_counts[q] = ann_counts.get(q, 0) + 1

    queue_items = load_queue(data_root)
    drill_counts: dict[str, int] = {}
    select_marks_by_qid: dict[str, list[dict[str, Any]]] = {}
    for qi in queue_items:
        q = str(qi.get("quark_id") or "")
        drill_counts[q] = drill_counts.get(q, 0) + 1
        if qi.get("source") != "mobile_note" and qi.get("status") != "done":
            select_marks_by_qid.setdefault(q, []).append(qi)

    set_notes = get_set_notes_map(data_root)
    reports_by_qid = _index_reports_by_qid(data_root)
    jobs_by_qid = job_queue.jobs_by_quark() if job_queue else {}

    matches: list[dict[str, Any]] = []
    for qid, summ in by_qid.items():
        pq_path = find_parquet_for_quark(qid, data_dir=data_root)
        has_telemetry = pq_path is not None and pq_path.exists()
        cached_rounds = list_cached_round_savestates(qid, data_dir=data_root)
        has_round_cache = len(cached_rounds) > 0

        mf_path = mobile_manifest_path(qid, data_dir=data_root)
        manifest_doc: dict[str, Any] | None = None
        if mf_path.exists():
            try:
                manifest_doc = json.loads(mf_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                manifest_doc = None
        has_mobile_video = bool(manifest_doc and len(manifest_doc.get("rounds") or []) > 0)

        # Resolve P1/P2 raw facts symmetrically via the canonical perspective boundary
        mf_p1, mf_p2 = extract_p1_p2_from_manifest(manifest_doc) if manifest_doc else ({}, {})
        p1_name = str(summ.get("p1_name") or "").strip()
        if is_placeholder_name(p1_name) and not is_placeholder_name(mf_p1.get("name")):
            p1_name = str(mf_p1["name"]).strip()
        p2_name = str(summ.get("p2_name") or "").strip()
        if is_placeholder_name(p2_name) and not is_placeholder_name(mf_p2.get("name")):
            p2_name = str(mf_p2["name"]).strip()

        persp = resolve_viewer_side(
            username=uname,
            p1_name=p1_name,
            p2_name=p2_name,
            fallback_side=manifest_doc.get("perspective") if manifest_doc else None,
        )

        names_pair = SidePair(p1_name or "P1", p2_name or "P2")
        ranks_pair = SidePair(
            format_rank_letter(summ.get("p1_rank")) or str(mf_p1.get("rank_letter") or ""),
            format_rank_letter(summ.get("p2_rank")) or str(mf_p2.get("rank_letter") or ""),
        )
        scores_pair = SidePair(summ.get("p1_score"), summ.get("p2_score"))

        self_name, opp_name = names_pair.split(persp)
        if is_placeholder_name(self_name) and uname:
            self_name = uname
        self_rank, opp_rank = ranks_pair.split(persp)
        self_score, opp_score = scores_pair.split(persp)

        # Character lists + round count from manifest or cached Parquet summary
        char_info: dict[str, Any] = {}
        if manifest_doc:
            char_info = {
                "p1_char_name": str(mf_p1.get("char_name") or ""),
                "p2_char_name": str(mf_p2.get("char_name") or ""),
                "p1_chars": list(mf_p1.get("chars") or []),
                "p2_chars": list(mf_p2.get("chars") or []),
                "round_count": int(manifest_doc.get("round_count") or len(manifest_doc.get("rounds") or [])),
            }
        elif has_telemetry and pq_path is not None:
            char_info = _read_parquet_summary(pq_path)

        p1_char_name = str(char_info.get("p1_char_name") or "")
        p2_char_name = str(char_info.get("p2_char_name") or "")
        char_names_pair = SidePair(p1_char_name, p2_char_name)
        chars_lists_pair = SidePair(
            list(char_info.get("p1_chars") or ([p1_char_name] if p1_char_name else [])),
            list(char_info.get("p2_chars") or ([p2_char_name] if p2_char_name else [])),
        )
        self_char_name, opp_char_name = char_names_pair.split(persp)
        self_chars, opp_chars = chars_lists_pair.split(persp)

        job = jobs_by_qid.get(qid)
        if job and job.get("state") in ("queued", "running", "paused"):
            status = "processing" if job.get("state") == "running" else "queued"
        elif has_mobile_video:
            status = "ready"
        elif has_telemetry and has_round_cache:
            status = "cached_no_video"
        elif has_telemetry:
            status = "telemetry_only"
        else:
            status = "unprocessed"

        notes_n = ann_counts.get(qid, 0)
        drills_n = drill_counts.get(qid, 0)
        sel_marks = sorted(
            select_marks_by_qid.get(qid, []),
            key=lambda x: (int(x.get("round_id") or 0), int(x.get("press_gf") or x.get("start_gf") or 0)),
        )
        sel_rounds = sorted({int(x.get("round_id") or 0) for x in sel_marks if int(x.get("round_id") or 0) > 0})
        first_sel = None
        if sel_marks:
            fm = sel_marks[0]
            first_sel = {
                "id": str(fm.get("id") or ""),
                "round_id": int(fm.get("round_id") or 1),
                "anchor_gf": int(fm.get("press_gf") or fm.get("end_gf") or fm.get("start_gf") or 0),
                "start_gf": int(fm.get("start_gf") or 0),
                "end_gf": int(fm.get("end_gf") or 0),
            }

        has_overlay_data = False
        preview_video_url = None
        if manifest_doc and (manifest_doc.get("rounds") or []):
            r0 = manifest_doc["rounds"][0]
            v0 = str(r0.get("video_url") or "").strip()
            if not v0 and r0.get("filename"):
                v0 = f"/replays/{qid}/{r0['filename']}"
            if v0:
                preview_video_url = v0
            for r in manifest_doc.get("rounds") or []:
                rid_r = int(r.get("round_id") or 0)
                if r.get("overlay_url") or (rid_r > 0 and (data_root / "replays" / qid / f"r{rid_r:02d}_overlay.json").is_file()):
                    has_overlay_data = True
                    break

        sn = set_notes.get(qid)
        set_note_text = str(sn.get("note") or "") if sn else ""

        row = {
            "quark_id": qid,
            "date": int(summ.get("date") or 0),
            "date_str": _format_date_iso(summ.get("date")),
            "duration_s": round(float(summ.get("duration") or 0.0), 1),
            "ranked": bool(summ.get("ranked")),
            "perspective": persp,
            "self_player_name": self_name,
            "opp_player_name": opp_name,
            "self_rank_letter": self_rank,
            "opp_rank_letter": opp_rank,
            "self_score": self_score,
            "opp_score": opp_score,
            "self_char_name": self_char_name,
            "opp_char_name": opp_char_name,
            "self_chars": self_chars,
            "opp_chars": opp_chars,
            "round_count": int(char_info.get("round_count") or 0),
            "status": status,
            "has_telemetry": has_telemetry,
            "has_round_cache": has_round_cache,
            "round_cache_count": len(cached_rounds),
            "has_mobile_video": has_mobile_video,
            "in_progress": bool(manifest_doc and manifest_doc.get("in_progress")),
            "mobile_round_count": len(manifest_doc.get("rounds") or []) if manifest_doc else 0,
            "has_overlay_data": has_overlay_data,
            "preview_video_url": preview_video_url,
            "has_report": qid in reports_by_qid,
            "report_url": reports_by_qid.get(qid),
            "notes_count": notes_n,
            "drills_count": drills_n,
            "select_marks_count": 0,
            "select_mark_rounds": [],
            "first_select_mark": None,
            "has_set_note": bool(set_note_text),
            "set_note_preview": set_note_text[:100],
            "job": job,
        }
        matches.append(row)

    matches.sort(key=lambda m: (int(m["date"]), str(m["quark_id"])), reverse=True)

    avail_self = sorted({c for m in matches for c in (m.get("self_chars") or []) if c})
    avail_opp = sorted({c for m in matches for c in (m.get("opp_chars") or []) if c})

    sf = (status_filter or "all").strip().lower()
    if sf == "ready":
        filtered = [m for m in matches if m["status"] == "ready"]
    elif sf == "unprocessed":
        filtered = [m for m in matches if m["status"] in ("unprocessed", "telemetry_only", "cached_no_video")]
    elif sf == "notes":
        filtered = [m for m in matches if m["notes_count"] > 0 or m["has_set_note"] or m["drills_count"] > 0]
    else:
        filtered = matches

    sc_f = (self_char or "").strip().lower()
    oc_f = (opp_char or "").strip().lower()
    if sc_f:
        filtered = [m for m in filtered if any(c.lower() == sc_f for c in (m.get("self_chars") or []))]
    if oc_f:
        filtered = [m for m in filtered if any(c.lower() == oc_f for c in (m.get("opp_chars") or []))]

    counts = {
        "total": len(matches),
        "ready": sum(1 for m in matches if m["status"] == "ready"),
        "unprocessed": sum(1 for m in matches if m["status"] in ("unprocessed", "telemetry_only", "cached_no_video")),
        "processing": sum(1 for m in matches if m["status"] in ("queued", "processing")),
        "with_notes": sum(1 for m in matches if m["notes_count"] > 0 or m["has_set_note"]),
    }

    return {
        "ok": True,
        "username": uname,
        "last_refreshed": hist_meta.get("last_refreshed"),
        "counts": counts,
        "available_chars": {"self": avail_self, "opp": avail_opp},
        "matches": filtered,
    }


class ProcessingQueue:
    """Netplay-safe sequential background queue (concurrency = 1) for replay ingestion + offline rendering.

    Fightcade stewardship safeguards:
      - Strict concurrency = 1 (single worker thread)
      - Jittered 10–15s cooldown between back-to-back online Fightcade replay streams
      - Hourly backfill cap (default 12 streams/hr; offline `replay_cache` renders are exempt)
    """

    def __init__(
        self,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        is_live_match_fn: Callable[[], bool] | None = None,
        scan_fn: Callable[..., dict[str, Any]] = scan_replay_for_select_marks,
        render_fn: Callable[..., dict[str, Any]] = render_replay_rounds,
        max_mobile_sets: int = 30,
        stream_cooldown_s: float | None = None,
        stream_cooldown_range: tuple[float, float] = DEFAULT_STREAM_COOLDOWN_RANGE,
        max_streams_per_hour: int | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        jitter_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self._is_live_match_fn = is_live_match_fn or (lambda: False)
        self._scan_fn = scan_fn
        self._render_fn = render_fn
        self.max_mobile_sets = max_mobile_sets
        self.stream_cooldown_s = stream_cooldown_s
        self.stream_cooldown_range = (
            float(stream_cooldown_range[0]),
            float(stream_cooldown_range[1]),
        )
        if max_streams_per_hour is None:
            env_cap = (os.environ.get("FC_MAX_BACKFILL_PER_HOUR") or "").strip()
            try:
                max_streams_per_hour = int(env_cap) if env_cap else DEFAULT_MAX_BACKFILL_PER_HOUR
            except ValueError:
                max_streams_per_hour = DEFAULT_MAX_BACKFILL_PER_HOUR
        self.max_streams_per_hour = max(0, int(max_streams_per_hour))
        self._clock_fn = clock_fn
        self._jitter_fn = jitter_fn

        self._lock = threading.Lock()
        self._wake_evt = threading.Event()
        self._stop_evt = threading.Event()
        self._pending: deque[dict[str, Any]] = deque()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._stream_timestamps: deque[float] = deque()
        self._next_stream_allowed_at: float | None = None
        self._thread: threading.Thread | None = None

    def _prune_stream_window_locked(self, now: float) -> None:
        cutoff = now - 3600.0
        while self._stream_timestamps and self._stream_timestamps[0] <= cutoff:
            self._stream_timestamps.popleft()

    def _needs_online_stream(self, qid: str) -> bool:
        pq_path = find_parquet_for_quark(qid, data_dir=self.data_dir)
        has_pq = pq_path is not None and pq_path.exists()
        has_cache = len(list_cached_round_savestates(qid, data_dir=self.data_dir)) > 0
        return (not has_pq) or (not has_cache)

    def stream_quota_status(self) -> dict[str, Any]:
        now = float(self._clock_fn())
        with self._lock:
            self._prune_stream_window_locked(now)
            used = len(self._stream_timestamps)
            next_in = 0.0
            if self.max_streams_per_hour > 0 and used >= self.max_streams_per_hour and self._stream_timestamps:
                next_in = max(1.0, 3600.0 - (now - self._stream_timestamps[0]))
        return {
            "used_last_hour": used,
            "max_per_hour": self.max_streams_per_hour,
            "remaining": max(0, self.max_streams_per_hour - used) if self.max_streams_per_hour > 0 else None,
            "next_available_in_s": round(next_in, 1),
        }

    def _compute_stream_cooldown(self) -> float:
        if self.stream_cooldown_s is not None:
            return max(0.0, float(self.stream_cooldown_s))
        lo, hi = self.stream_cooldown_range
        if hi <= 0:
            return 0.0
        lo = max(0.0, lo)
        hi = max(lo, hi)
        return float(self._jitter_fn(lo, hi))

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._loop, name="fc-process-queue", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake_evt.set()
        with self._lock:
            t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

    def enqueue(
        self,
        quark_id: str,
        *,
        perspective: int = 1,
        force: bool = False,
        source: str = "manual",
        stream_target: str | None = None,
    ) -> dict[str, Any]:
        qid = parse_quark_id_or_url(quark_id)
        if int(perspective) not in (1, 2):
            raise ValueError("perspective must be 1 or 2")
        now_iso = datetime.now().isoformat(timespec="seconds")
        needs_stream = self._needs_online_stream(qid)

        with self._lock:
            existing = self._jobs.get(qid)
            if existing and existing.get("state") in ("queued", "running", "paused"):
                return dict(existing)
            if needs_stream and source != "live_watcher" and self.max_streams_per_hour > 0:
                now_mono = float(self._clock_fn())
                self._prune_stream_window_locked(now_mono)
                pending_online = sum(
                    1
                    for j in self._pending
                    if j.get("source") != "live_watcher" and j.get("_needs_stream")
                )
                used = len(self._stream_timestamps) + pending_online
                if used >= self.max_streams_per_hour:
                    retry_s = (
                        max(60.0, 3600.0 - (now_mono - self._stream_timestamps[0]))
                        if self._stream_timestamps
                        else 3600.0
                    )
                    mins = max(1, int(round(retry_s / 60.0)))
                    raise HourlyBackfillCapError(
                        f"Hourly Fightcade replay stream cap reached ({self.max_streams_per_hour}/hr) "
                        f"to protect Fightcade servers — try again in ~{mins}m.",
                        retry_after_s=retry_s,
                        cap=self.max_streams_per_hour,
                        used=len(self._stream_timestamps),
                    )
            job = {
                "quark_id": qid,
                "perspective": int(perspective),
                "force": bool(force),
                "source": source,
                "stream_target": stream_target,
                "_needs_stream": needs_stream,
                "state": "queued",  # queued | paused | running | done | error
                "phase": "queued",
                "message": "Queued for processing",
                "progress_done": 0,
                "progress_total": 0,
                "error": None,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            self._jobs[qid] = job
            self._pending.append(job)

        self.start()
        self._wake_evt.set()
        return dict(job)

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            vals = [dict(j) for j in self._jobs.values()]
        for j in vals:
            qid = str(j.get("quark_id") or "")
            if not qid:
                continue
            if j.get("state") in ("running", "queued", "paused"):
                mf_p = mobile_manifest_path(qid, data_dir=self.data_dir)
                playable = 0
                if mf_p.is_file():
                    try:
                        doc = json.loads(mf_p.read_text(encoding="utf-8"))
                        playable = len(doc.get("rounds") or [])
                    except Exception:  # noqa: BLE001
                        playable = 0
                j["playable_rounds"] = playable
                if not j.get("opp_name"):
                    try:
                        meta = lookup_replay_metadata(qid, data_dir=self.data_dir)
                        persp = int(j.get("perspective") or 1)
                        opp = str(meta.get("p2_name" if persp == 1 else "p1_name") or "").strip()
                        if opp and not is_placeholder_name(opp):
                            j["opp_name"] = opp
                    except Exception:  # noqa: BLE001
                        pass
            else:
                j.setdefault("playable_rounds", int(j.get("progress_done") or 0) if j.get("state") == "done" else 0)
        vals.sort(key=lambda j: str(j.get("updated_at") or ""), reverse=True)
        return vals[:50]

    def jobs_by_quark(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._jobs.items()}

    def has_running_job(self) -> bool:
        with self._lock:
            return any(j.get("state") == "running" for j in self._jobs.values())

    def _update(self, qid: str, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(qid)
            if job is not None:
                job.update(fields)
                job["updated_at"] = datetime.now().isoformat(timespec="seconds")

    def _wait_if_live_match(self, qid: str) -> bool:
        """Yield politely whenever a live Fightcade match is running. Returns False if stopped."""
        while not self._stop_evt.is_set() and self._is_live_match_fn():
            self._update(qid, state="paused", phase="waiting_netplay", message="Paused while live Fightcade match is active")
            self._stop_evt.wait(2.0)
        return not self._stop_evt.is_set()

    def _run_job(self, job: dict[str, Any]) -> None:
        qid = str(job["quark_id"])
        persp = int(job["perspective"])
        force = bool(job.get("force", False))

        if not self._wait_if_live_match(qid):
            return

        try:
            pq_path = find_parquet_for_quark(qid, data_dir=self.data_dir)
            has_pq = pq_path is not None and pq_path.exists()
            has_cache = len(list_cached_round_savestates(qid, data_dir=self.data_dir)) > 0

            # Step 1: Stream & capture rounds when Parquet or per-round replay_cache is missing
            if not has_pq or not has_cache:
                if job.get("source") != "live_watcher" and self.max_streams_per_hour > 0:
                    now_mono = float(self._clock_fn())
                    with self._lock:
                        self._prune_stream_window_locked(now_mono)
                        if len(self._stream_timestamps) >= self.max_streams_per_hour:
                            retry_s = max(60.0, 3600.0 - (now_mono - self._stream_timestamps[0]))
                            mins = max(1, int(round(retry_s / 60.0)))
                            raise HourlyBackfillCapError(
                                f"Hourly Fightcade replay stream cap reached ({self.max_streams_per_hour}/hr) — try again in ~{mins}m.",
                                retry_after_s=retry_s,
                                cap=self.max_streams_per_hour,
                                used=len(self._stream_timestamps),
                            )
                # Wait any remaining Fightcade stewardship cooldown BEFORE starting the next online stream
                # (never stall the user for 10–15s AFTER a finished match when no next stream is running!)
                if self._next_stream_allowed_at is not None:
                    wait_rem = self._next_stream_allowed_at - float(self._clock_fn())
                    if wait_rem > 0:
                        self._update(
                            qid,
                            state="running",
                            phase="cooldown",
                            message=f"Waiting {int(round(wait_rem))}s stream cooldown...",
                        )
                        if self._stop_evt.wait(wait_rem):
                            return

                self._update(
                    qid,
                    state="running",
                    phase="processing",
                    message="Processing replay...",
                    progress_done=0,
                    progress_total=0,
                )

                def _on_scan_progress(done: int, total: int, msg: str) -> None:
                    self._update(
                        qid,
                        state="running",
                        phase="processing",
                        message=msg,
                        progress_done=done,
                        progress_total=total,
                    )

                scan_kwargs: dict[str, Any] = {
                    "perspective": persp,
                    "stream_target": job.get("stream_target"),
                    "data_dir": self.data_dir,
                }
                try:
                    sig = inspect.signature(self._scan_fn)
                    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                    if "render_mobile_video" in sig.parameters or has_varkw:
                        scan_kwargs["render_mobile_video"] = True
                    if "progress_cb" in sig.parameters or has_varkw:
                        scan_kwargs["progress_cb"] = _on_scan_progress
                except (TypeError, ValueError):
                    pass
                with self._lock:
                    self._stream_timestamps.append(float(self._clock_fn()))
                self._scan_fn(qid, **scan_kwargs)
                cooldown_wait = self._compute_stream_cooldown()
                if cooldown_wait > 0:
                    self._next_stream_allowed_at = float(self._clock_fn()) + cooldown_wait
                else:
                    self._next_stream_allowed_at = None

            if not self._wait_if_live_match(qid):
                return

            # Ensure Fightcade player handles/ranks are synced for newly finished matches before rendering manifest
            meta = lookup_replay_metadata(qid, data_dir=self.data_dir)
            if is_placeholder_name(meta.get("p1_name")) and is_placeholder_name(meta.get("p2_name")):
                default_user = detect_default_username(self.data_dir)
                if default_user:
                    try:
                        refresh_match_history(
                            default_user,
                            page_size=5,
                            max_pages=1,
                            data_dir=self.data_dir,
                            cooldown_seconds=DEFAULT_REFRESH_COOLDOWN_S,
                        )
                    except (RefreshCooldownError, FightcadeRateLimitError, Exception):  # noqa: BLE001
                        pass

            # Check if Pass 1 single-pass capture already produced a complete manifest + all round MP4s!
            mf_path = mobile_manifest_path(qid, data_dir=self.data_dir)
            single_pass_mf: dict[str, Any] | None = None
            if not force and mf_path.is_file():
                try:
                    doc = json.loads(mf_path.read_text(encoding="utf-8"))
                    rounds_doc = doc.get("rounds") or []
                    out_dir = mf_path.parent
                    cached_cnt = len(list_cached_round_savestates(qid, data_dir=self.data_dir))
                    if (
                        not doc.get("in_progress")
                        and len(rounds_doc) > 0
                        and (cached_cnt == 0 or len(rounds_doc) >= cached_cnt)
                        and all(
                            (out_dir / str(r.get("filename") or f"r{int(r.get('round_id', 0)):02d}.mp4")).is_file()
                            and (out_dir / str(r.get("filename") or f"r{int(r.get('round_id', 0)):02d}.mp4")).stat().st_size > 1024
                            for r in rounds_doc
                        )
                    ):
                        single_pass_mf = doc
                except Exception:  # noqa: BLE001
                    single_pass_mf = None

            if single_pass_mf is not None:
                if self.max_mobile_sets > 0:
                    prune_old_mobile_replays(self.data_dir, max_sets=self.max_mobile_sets)
                rcnt = int(single_pass_mf.get("round_count") or len(single_pass_mf.get("rounds") or []))
                self._update(
                    qid,
                    state="done",
                    phase="ready",
                    message=f"Ready ({rcnt} rounds)",
                    progress_done=rcnt,
                    progress_total=rcnt,
                    error=None,
                )
                self._trigger_byoc_auto_sync(qid)
                return

            # Step 2: Offline headless per-round silent MP4 render (for already-cached sets or force re-render)
            self._update(
                qid,
                state="running",
                phase="rendering",
                message="Rendering round videos offline...",
            )

            def _on_progress(done: int, total: int, msg: str) -> None:
                self._update(
                    qid,
                    state="running",
                    phase="rendering",
                    message=msg,
                    progress_done=done,
                    progress_total=total,
                )

            mf = self._render_fn(
                qid,
                data_dir=self.data_dir,
                perspective=persp,
                force=force,
                progress_cb=_on_progress,
                should_pause_fn=lambda: (not self._stop_evt.is_set()) and bool(self._is_live_match_fn()),
            )
            if self.max_mobile_sets > 0:
                prune_old_mobile_replays(self.data_dir, max_sets=self.max_mobile_sets)

            self._update(
                qid,
                state="done",
                phase="ready",
                message=f"Ready ({mf.get('round_count', 0)} rounds)",
                progress_done=int(mf.get("round_count", 0)),
                progress_total=int(mf.get("round_count", 0)),
                error=None,
            )
            self._trigger_byoc_auto_sync(qid)
        except Exception as exc:  # noqa: BLE001
            self._update(
                qid,
                state="error",
                phase="error",
                message=f"Failed: {exc}",
                error=str(exc),
            )

    def _trigger_byoc_auto_sync(self, qid: str) -> None:
        """Non-blocking background R2 sync + cloud note pull when BYOC auto-sync is enabled."""
        try:
            from fightcade_analysis.byoc_r2 import trigger_background_byoc_sync

            trigger_background_byoc_sync(self.data_dir, quark_id=qid, only_if_auto=True)
        except Exception:  # noqa: BLE001
            pass

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            next_job: dict[str, Any] | None = None
            with self._lock:
                if self._pending:
                    next_job = self._pending.popleft()
            if next_job is None:
                self._wake_evt.wait(1.0)
                self._wake_evt.clear()
                continue
            self._run_job(next_job)
