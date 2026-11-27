"""In-Game Select (Coin) Mark Scanner, Takeover Practice Queue & Live Fightcade Match Watcher.

Workflow:
  1. While playing a live Fightcade match (`sfiii3nr1`), tap Select (`Coin`) whenever an interaction
     gives you trouble. GGPO records that exact frame in the official Fightcade replay stream
     with zero Lua/memory hooks during netplay and zero gameplay disruption.
  2. Immediately after the match ends, either:
     - `LiveMatchWatcher` (running automatically inside `server.py`) detects the finished
       `quark:served,sfiii3nr1,<quarkid>.<player>` session and scans it silently in ~15-30s, OR
     - You click "Scan Latest Match" on the Reports Hub (or run `scripts/scan_marks.ps1`).
  3. `lua/capture_select_marks.lua` runs a single muted turbo pass with a rolling 8s savestate ring
     buffer, copies the `.fs` savestate anchored 6.0s before each Select press + deterministic
     `.inputs`, and appends them to `data/selections/queue.jsonl` (and `bookmarks.jsonl`).
  4. The Reports Hub displays your live Takeover Practice Queue for instant 1-click drilling.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from fightcade_analysis.audio_mute import start_mute_watcher
from fightcade_analysis.clip_renderer import (
    DEFAULT_DATA_DIR,
    DEFAULT_FBNEO_EXE,
    REPO_ROOT,
    lookup_cached_stream_target,
    resolve_capture_fbneo_exe,
    resolve_grouflon_dir,
)
from fightcade_analysis.coaching import pretty_char
from fightcade_analysis.csv_to_parquet import convert_csv_to_parquet
from fightcade_analysis.env_config import build_fbneo_cmdline, ensure_fbneo_config_inheritance
from fightcade_analysis.fetch_replays import (
    fetch_quarks,
    resolve_stream_target,
    summarize_quark,
)
from fightcade_analysis.naming import lookup_replay_metadata

DEFAULT_SELECT_MARKS_LUA = REPO_ROOT / "lua" / "dump_telemetry.lua"
DEFAULT_LOOKBACK_FRAMES = 360  # 6.0 seconds at 60fps
DEFAULT_TAIL_FRAMES = 90       # 1.5 seconds aftermath
DEFAULT_DEBOUNCE_FRAMES = 150  # 2.5 seconds between distinct marks

_SERVED_CMD_RE = re.compile(
    r"quark:served,([A-Za-z0-9_-]+),([A-Za-z0-9_-]+)\.([01])(?:,|\s|$)",
    re.IGNORECASE,
)


def queue_jsonl_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "selections" / "queue.jsonl"


def bookmarks_jsonl_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "selections" / "bookmarks.jsonl"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out


def _write_jsonl_atomic(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp_{os.getpid()}")
    body = "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items)
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)


def _is_placeholder_player(name: Any) -> bool:
    s = str(name or "").strip()
    return not s or s.upper() in ("P1", "P2")


def enrich_queue_item(item: dict[str, Any], *, data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Attach human-readable char names, opponent handle, and savestate readiness."""
    out = dict(item)
    qid = str(out.get("quark_id", ""))
    persp = int(out.get("perspective", 1))
    self_cid = int(out.get("self_char", 0))
    opp_cid = int(out.get("opp_char", 0))
    out["self_char_name"] = out.get("self_char_name") or pretty_char(self_cid)
    out["opp_char_name"] = out.get("opp_char_name") or pretty_char(opp_cid)

    if _is_placeholder_player(out.get("opp_player_name")) or _is_placeholder_player(out.get("self_player_name")):
        meta = lookup_replay_metadata(qid, data_dir=data_dir)
        if meta:
            p1_name = str(meta.get("p1_name") or "").strip()
            p2_name = str(meta.get("p2_name") or "").strip()
            if persp == 1:
                if _is_placeholder_player(out.get("self_player_name")) and p1_name:
                    out["self_player_name"] = p1_name
                if _is_placeholder_player(out.get("opp_player_name")) and p2_name:
                    out["opp_player_name"] = p2_name
            else:
                if _is_placeholder_player(out.get("self_player_name")) and p2_name:
                    out["self_player_name"] = p2_name
                if _is_placeholder_player(out.get("opp_player_name")) and p1_name:
                    out["opp_player_name"] = p1_name
    out.setdefault("self_player_name", f"P{persp}")
    out.setdefault("opp_player_name", "P2" if persp == 1 else "P1")

    fs_path = Path(str(out.get("savestate", ""))) if out.get("savestate") else None
    out["savestate_ready"] = bool(fs_path and fs_path.exists() and fs_path.stat().st_size > 4096)
    out.setdefault("status", "pending")
    out.setdefault("owner", "")
    out.setdefault("goal_ids", [])
    out.setdefault("playlist_ids", [])
    out.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    return out


_QUEUE_FILE_LOCK = threading.Lock()


def load_queue(data_dir: Path = DEFAULT_DATA_DIR, *, include_dismissed: bool = False) -> list[dict[str, Any]]:
    with _QUEUE_FILE_LOCK:
        items = _read_jsonl(queue_jsonl_path(data_dir))
    enriched = [enrich_queue_item(it, data_dir=data_dir) for it in items]
    if not include_dismissed:
        enriched = [it for it in enriched if it.get("status") != "dismissed"]
    return enriched


def upsert_queue_items(new_items: list[dict[str, Any]], *, data_dir: Path = DEFAULT_DATA_DIR) -> list[dict[str, Any]]:
    """Merge new queue marks into `queue.jsonl` (dedup by `id`) and append to `bookmarks.jsonl`."""
    if not new_items:
        return []
    now_iso = datetime.now().isoformat(timespec="seconds")
    added: list[dict[str, Any]] = []
    with _QUEUE_FILE_LOCK:
        qpath = queue_jsonl_path(data_dir)
        existing = _read_jsonl(qpath)
        existing_ids = {str(it.get("id", "")) for it in existing}

        bpath = bookmarks_jsonl_path(data_dir)
        bm_existing = _read_jsonl(bpath)
        bm_ids = {str(it.get("id", "")) for it in bm_existing}
        bm_new: list[dict[str, Any]] = []

        for raw in new_items:
            mid = str(raw.get("id", "")).strip()
            if not mid or mid in existing_ids:
                continue
            item = dict(raw)
            item.setdefault("status", "pending")
            item.setdefault("created_at", now_iso)
            item = enrich_queue_item(item, data_dir=data_dir)
            existing.append(item)
            existing_ids.add(mid)
            added.append(item)
            if mid not in bm_ids:
                bm_new.append(item)
                bm_ids.add(mid)

        if added:
            _write_jsonl_atomic(qpath, existing)
        if bm_new:
            bpath.parent.mkdir(parents=True, exist_ok=True)
            with bpath.open("a", encoding="utf-8") as bf:
                for b in bm_new:
                    bf.write(json.dumps(b, ensure_ascii=False) + "\n")
    return added


def update_queue_item_status(
    item_id: str,
    status: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any] | None:
    if status not in ("pending", "done", "dismissed"):
        raise ValueError("status must be 'pending', 'done', or 'dismissed'")
    with _QUEUE_FILE_LOCK:
        qpath = queue_jsonl_path(data_dir)
        items = _read_jsonl(qpath)
        updated_item: dict[str, Any] | None = None
        for it in items:
            if str(it.get("id", "")) == item_id:
                it["status"] = status
                it["updated_at"] = datetime.now().isoformat(timespec="seconds")
                updated_item = enrich_queue_item(it, data_dir=data_dir)
                break
        if updated_item is not None:
            _write_jsonl_atomic(qpath, items)
        return updated_item


def update_queue_item(
    item_id: str,
    patch: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any] | None:
    """Update editable fields (`note`, `tags`, `kind`, `status`) on an existing queue item."""
    if not isinstance(patch, dict):
        raise ValueError("Patch payload must be a JSON object")
    valid_kinds = {"mistake", "question", "good_play", "opp_habit", "note", "select_mark"}
    with _QUEUE_FILE_LOCK:
        qpath = queue_jsonl_path(data_dir)
        items = _read_jsonl(qpath)
        updated_item: dict[str, Any] | None = None
        for it in items:
            if str(it.get("id", "")) != str(item_id):
                continue
            if "note" in patch:
                text = str(patch["note"] or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                it["note"] = text[:2000]
            if "tags" in patch:
                raw_tags = patch["tags"]
                if isinstance(raw_tags, str):
                    raw_tags = [t.strip() for t in raw_tags.split(",")]
                clean_tags: list[str] = []
                seen: set[str] = set()
                if isinstance(raw_tags, list):
                    for t in raw_tags:
                        c = " ".join(str(t or "").strip().split())[:40]
                        if c and c.lower() not in seen:
                            seen.add(c.lower())
                            clean_tags.append(c)
                        if len(clean_tags) >= 12:
                            break
                it["tags"] = clean_tags
            if "kind" in patch:
                k = str(patch["kind"] or "select_mark").strip().lower()
                if k not in valid_kinds:
                    raise ValueError(f"Invalid kind: {k}")
                it["kind"] = k
            if "status" in patch:
                st = str(patch["status"] or "").strip().lower()
                if st not in ("pending", "done", "dismissed"):
                    raise ValueError("status must be 'pending', 'done', or 'dismissed'")
                it["status"] = st
            if "owner" in patch:
                it["owner"] = str(patch["owner"] or "").strip()
            has_start = "start_gf" in patch and patch["start_gf"] not in (None, "")
            has_end = "end_gf" in patch and patch["end_gf"] not in (None, "")
            if has_start or has_end:
                if isinstance(patch.get("start_gf"), bool) or isinstance(patch.get("end_gf"), bool):
                    raise ValueError("start_gf and end_gf must be integers")
                try:
                    new_start = int(patch["start_gf"]) if has_start else int(it.get("start_gf", 0))
                    new_end = int(patch["end_gf"]) if has_end else int(it.get("end_gf", 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("start_gf and end_gf must be integers") from exc
                if new_start < 1 or new_end < 1:
                    raise ValueError("start_gf and end_gf must be positive integers")
                if new_end <= new_start:
                    raise ValueError("end_gf must be greater than start_gf")
                it["start_gf"] = new_start
                it["end_gf"] = new_end
            if "takeover_gf" in patch and patch["takeover_gf"] not in (None, ""):
                if isinstance(patch["takeover_gf"], bool):
                    raise ValueError("takeover_gf must be an integer")
                try:
                    it["takeover_gf"] = max(1, int(patch["takeover_gf"]))
                except (TypeError, ValueError) as exc:
                    raise ValueError("takeover_gf must be an integer") from exc
            for list_field in ("goal_ids", "playlist_ids"):
                if list_field in patch:
                    raw_ids = patch[list_field]
                    if isinstance(raw_ids, str):
                        raw_ids = [s.strip() for s in raw_ids.split(",")]
                    clean_ids: list[str] = []
                    seen_ids: set[str] = set()
                    if isinstance(raw_ids, list):
                        for x in raw_ids:
                            cx = str(x or "").strip()
                            if cx and len(cx) <= 128 and cx not in seen_ids:
                                seen_ids.add(cx)
                                clean_ids.append(cx)
                            if len(clean_ids) >= 24:
                                break
                    it[list_field] = clean_ids
            if "cloud_moment_id" in patch:
                val = str(patch["cloud_moment_id"] or "").strip()[:128]
                it["cloud_moment_id"] = val or None
            if "cloud_share_token" in patch:
                val = str(patch["cloud_share_token"] or "").strip()[:128]
                it["cloud_share_token"] = val or None
            if "cloud_share_url" in patch:
                val = str(patch["cloud_share_url"] or "").strip()[:512]
                it["cloud_share_url"] = val or None
            if "cloud_visibility" in patch:
                raw_vis = str(patch["cloud_visibility"] or "").strip().lower()
                if raw_vis and raw_vis not in ("private", "unlisted"):
                    raise ValueError("cloud_visibility must be 'private' or 'unlisted'")
                it["cloud_visibility"] = raw_vis or None
            if "shared_caption" in patch:
                cap = str(patch["shared_caption"] or "").replace("\r\n", "\n").replace("\r", "\n").strip()
                it["shared_caption"] = cap[:1000]
            if "include_note_on_share" in patch:
                it["include_note_on_share"] = bool(patch["include_note_on_share"])
            if "hide_player_names" in patch:
                it["hide_player_names"] = bool(patch["hide_player_names"])
            it["updated_at"] = datetime.now().isoformat(timespec="seconds")
            updated_item = enrich_queue_item(it, data_dir=data_dir)
            break
        if updated_item is not None:
            _write_jsonl_atomic(qpath, items)
        return updated_item


def clear_done_items(data_dir: Path = DEFAULT_DATA_DIR) -> int:
    with _QUEUE_FILE_LOCK:
        qpath = queue_jsonl_path(data_dir)
        items = _read_jsonl(qpath)
        kept = [it for it in items if it.get("status") == "pending"]
        removed = len(items) - len(kept)
        if removed > 0:
            _write_jsonl_atomic(qpath, kept)
        return removed


def parse_served_match_cmdline(cmdline: str) -> dict[str, Any] | None:
    """Parse `quark:served,<game>,<quarkid>.<0|1>,...` from a live fcadefbneo.exe command line.

    Fightcade uses 0-indexed player slots in `quark:served` (verified in `vid_overlay.cpp:1478`):
      `.0` -> Player 1, `.1` -> Player 2.
    """
    m = _SERVED_CMD_RE.search(str(cmdline or ""))
    if not m:
        return None
    return {
        "game_id": m.group(1),
        "quark_id": m.group(2),
        "player": int(m.group(3)) + 1,
    }


def resolve_latest_replay_for_username(
    username: str,
    *,
    game_id: str = "sfiii3nr1",
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Fetch the most recent replay for `username` via Fightcade API and determine P1/P2 side."""
    raw = fetch_quarks(
        gameid=game_id,
        username=username,
        best=False,
        limit=5,
        max_pages=1,
        page_delay=0.5,
    )
    if not raw:
        raise RuntimeError(f"No Fightcade replays found for '{username}' ({game_id})")
    summaries = [summarize_quark(r) for r in raw]
    summaries.sort(key=lambda s: int(s.get("date") or 0), reverse=True)
    latest = summaries[0]
    qid = str(latest["quarkid"])
    uname_l = username.strip().lower()
    p1_l = str(latest.get("p1_name") or "").strip().lower()
    p2_l = str(latest.get("p2_name") or "").strip().lower()
    player = 2 if (p2_l == uname_l and p1_l != uname_l) else 1

    stream_target = resolve_stream_target(game_id, qid)
    latest["stream_target"] = stream_target

    # Persist into user manifest so names/ranks stay cached for queue enrichment
    manifest_path = data_dir.resolve() / f"{username}_replays.json"
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {"summaries": []}
        existing = doc.get("summaries") or []
        by_qid = {str(s.get("quarkid")): s for s in existing if isinstance(s, dict) and s.get("quarkid")}
        by_qid[qid] = latest
        doc["summaries"] = sorted(by_qid.values(), key=lambda s: int(s.get("date") or 0), reverse=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    return {
        "quark_id": qid,
        "player": player,
        "stream_target": stream_target,
        "p1_name": latest.get("p1_name") or "P1",
        "p2_name": latest.get("p2_name") or "P2",
        "duration": latest.get("duration") or 0,
    }


def scan_replay_for_select_marks(
    quark_id: str,
    *,
    perspective: int = 1,
    game_id: str = "sfiii3nr1",
    stream_target: str | None = None,
    lookback_frames: int = DEFAULT_LOOKBACK_FRAMES,
    tail_frames: int = DEFAULT_TAIL_FRAMES,
    debounce_frames: int = DEFAULT_DEBOUNCE_FRAMES,
    idle_finish_seconds: float = 6.0,
    max_wait_seconds: float = 240.0,
    resolve_retries: int = 4,
    render_mobile_video: bool = False,
    scale: int = 2,
    crf: int = 26,
    gop_size: int = 30,
    progress_cb: Callable[[int, int, str], None] | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    lua_script: Path = DEFAULT_SELECT_MARKS_LUA,
) -> dict[str, Any]:
    """Run one invisible headless FBNeo pass to extract telemetry, round cache, Select marks,
    and optionally per-round MP4s + overlay JSONs (single-pass ingestion).
    """
    data_root = data_dir.resolve()
    sel_dir = data_root / "selections"
    sel_dir.mkdir(parents=True, exist_ok=True)
    lua_path = lua_script.resolve()

    eff_exe, use_maxspeed = resolve_capture_fbneo_exe(fbneo_exe)
    if not eff_exe.exists():
        raise FileNotFoundError(f"fcadefbneo executable not found: {eff_exe}")
    if not lua_path.exists():
        raise FileNotFoundError(f"Lua script not found: {lua_path}")

    target = stream_target or lookup_cached_stream_target(quark_id, data_dir=data_root)
    if not target:
        last_err: Exception | None = None
        for attempt in range(max(1, resolve_retries)):
            try:
                target = resolve_stream_target(game_id, quark_id)
                if target:
                    break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt + 1 < resolve_retries:
                    time.sleep(5.0 * (attempt + 1))
        if not target:
            raise RuntimeError(f"Could not resolve Fightcade replay stream for {quark_id}: {last_err}")

    ensure_fbneo_config_inheritance(eff_exe, game_id=game_id, data_dir=data_root)
    flags = ["-w", "-headless"] if use_maxspeed else ["-w"]
    cmd_str = build_fbneo_cmdline(eff_exe, flags, target, lua_path, target_is_path=False)

    run_tag = f"_selscan_{quark_id}_{os.getpid()}_{int(time.time())}"
    ring_dir = sel_dir / f"{run_tag}_ring"
    marks_tmp = sel_dir / f"{run_tag}_marks.jsonl"
    hb_path = sel_dir / f"{run_tag}_hb.txt"
    csv_tmp = data_root / "tmp_csv" / f"{run_tag}.csv"
    round_cache_dir = data_root / "replay_cache" / quark_id
    ring_dir.mkdir(parents=True, exist_ok=True)
    csv_tmp.parent.mkdir(parents=True, exist_ok=True)
    round_cache_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["FC_TELEMETRY_OUT"] = str(csv_tmp)
    env["FC_TURBO"] = "1"
    env["FC_AUTO_EXIT"] = "1"
    env["FC_ROUND_CACHE_DIR"] = str(round_cache_dir)
    env["FC_QUARK_ID"] = str(quark_id)
    env["FC_USER_PLAYER"] = str(int(perspective))
    env["FC_SELECTIONS_DIR"] = str(sel_dir)
    env["FC_RING_DIR"] = str(ring_dir)
    env["FC_MARKS_OUT"] = str(marks_tmp)
    env["FC_HEARTBEAT_PATH"] = str(hb_path)
    env["FC_LOOKBACK_FRAMES"] = str(int(lookback_frames))
    env["FC_TAIL_FRAMES"] = str(int(tail_frames))
    env["FC_DEBOUNCE_FRAMES"] = str(int(debounce_frames))

    mobile_out_dir = data_root / "replays" / str(quark_id)
    single_pass_enabled = bool(render_mobile_video and use_maxspeed)
    eff_encoder = ""
    if single_pass_enabled:
        import imageio_ffmpeg
        from fightcade_analysis.round_renderer import resolve_video_encoder

        eff_encoder = resolve_video_encoder()
        mobile_out_dir.mkdir(parents=True, exist_ok=True)
        env["FC_MOBILE_OUT_DIR"] = str(mobile_out_dir)
        env["FC_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
        env["FC_VIDEO_SCALE"] = str(max(1, int(scale)))
        env["FC_VIDEO_CRF"] = str(int(crf))
        env["FC_VIDEO_GOP"] = str(int(gop_size) if gop_size else 0)
        env["FC_VIDEO_ENCODER"] = eff_encoder
        env["FC_CAPTURE_AUDIO"] = "1"
        env["FC_PIPE_TAG"] = f"sp_{os.getpid()}_{int(time.time())}"
        g_dir = resolve_grouflon_dir(eff_exe)
        if g_dir is not None:
            env["FC_GROUFLON_DIR"] = str(g_dir)

    popen_kwargs: dict[str, Any] = {"cwd": str(eff_exe.parent), "env": env}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        popen_kwargs["startupinfo"] = si

    t0 = time.perf_counter()
    t0_mono = time.monotonic()
    proc = subprocess.Popen(cmd_str, **popen_kwargs)
    mute_stop = threading.Event()
    if not use_maxspeed:
        start_mute_watcher(proc.pid, mute_stop)

    last_hb_text = ""
    last_hb_change = time.monotonic()
    saw_hb = False
    saw_rounds = False
    last_reported_round = 0
    published_rounds: dict[int, dict[str, Any]] = {}
    last_pub_cnt = 0
    last_pub_check = 0.0
    try:
        while proc.poll() is None:
            now = time.monotonic()
            if hb_path.exists():
                try:
                    txt = hb_path.read_text(encoding="utf-8", errors="ignore").strip()
                except OSError:
                    txt = last_hb_text
                if txt and txt != last_hb_text:
                    last_hb_text = txt
                    last_hb_change = now
                    saw_hb = True
                    parts = txt.split(",")
                    if len(parts) >= 4:
                        cur_rid = int(parts[3] or 0)
                        if cur_rid > 0:
                            saw_rounds = True
                            if cur_rid > last_reported_round:
                                last_reported_round = cur_rid
                                if progress_cb is not None:
                                    try:
                                        msg = (
                                            f"{last_pub_cnt}R ready · streaming R{cur_rid}..."
                                            if last_pub_cnt > 0
                                            else f"Processing R{cur_rid}..."
                                        )
                                        progress_cb(max(cur_rid, last_pub_cnt), 0, msg)
                                    except Exception:  # noqa: BLE001
                                        pass
            # Progressive per-round publishing: make completed rounds playable while stream continues
            if single_pass_enabled and saw_rounds and (now - last_pub_check) >= 1.0:
                last_pub_check = now
                try:
                    from fightcade_analysis.round_renderer import publish_progressive_rounds

                    pub_cnt = publish_progressive_rounds(
                        quark_id,
                        csv_tmp,
                        published_rounds=published_rounds,
                        data_dir=data_root,
                        perspective=int(perspective),
                        scale=max(1, int(scale)),
                        video_encoder=eff_encoder,
                    )
                    if pub_cnt > last_pub_cnt:
                        last_pub_cnt = pub_cnt
                        if progress_cb is not None:
                            try:
                                next_r = max(last_reported_round, pub_cnt + 1)
                                progress_cb(pub_cnt, 0, f"{pub_cnt}R ready · streaming R{next_r}...")
                            except Exception:  # noqa: BLE001
                                pass
                except Exception:  # noqa: BLE001
                    pass
            # Stream EOF quiescence: once match ran and heartbeat stops advancing for idle_finish_seconds
            idle_limit = idle_finish_seconds if saw_rounds else max(25.0, idle_finish_seconds * 4.0)
            if saw_hb and (now - last_hb_change) >= idle_limit:
                proc.terminate()
                try:
                    proc.wait(timeout=4.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3.0)
                break
            if (now - t0_mono) >= max_wait_seconds:
                proc.kill()
                proc.wait(timeout=3.0)
                break
            time.sleep(0.5)
    finally:
        mute_stop.set()
        raw_marks = _read_jsonl(marks_tmp)
        shutil.rmtree(ring_dir, ignore_errors=True)
        marks_tmp.unlink(missing_ok=True)
        hb_path.unlink(missing_ok=True)

    stream_s = round(time.perf_counter() - t0, 2)

    # Convert unified single-pass telemetry CSV into personal Parquet so analyze_match.ps1 skips ingestion!
    t_pq0 = time.perf_counter()
    parquet_saved = False
    personal_pq = data_root / "personal" / f"{quark_id}.parquet"
    if csv_tmp.exists() and csv_tmp.stat().st_size > 1024 and not personal_pq.exists():
        try:
            convert_csv_to_parquet(csv_tmp, personal_pq, quark_id=quark_id, delete_csv=True)
            parquet_saved = personal_pq.exists()
        except Exception as exc:  # noqa: BLE001
            print(f"[select_queue] WARNING: Parquet conversion failed for {quark_id}: {exc}")
    if csv_tmp.exists():
        csv_tmp.unlink(missing_ok=True)
    parquet_s = round(time.perf_counter() - t_pq0, 2)

    # Bake exact .fs + .inputs offline ONLY for actual Select marks pressed (avoids ~700 wasted disk savestates during stream!)
    t_bake0 = time.perf_counter()
    if raw_marks and (parquet_saved or personal_pq.exists()):
        from fightcade_analysis.round_renderer import bake_drill_savestate

        for mk in raw_marks:
            if mk.get("needs_bake") or not mk.get("savestate"):
                try:
                    baked = bake_drill_savestate(
                        quark_id,
                        int(mk.get("start_gf") or 0),
                        int(mk.get("end_gf") or mk.get("press_gf") or 0),
                        selection_id=str(mk["id"]),
                        data_dir=data_root,
                        fbneo_exe=fbneo_exe,
                    )
                    mk["savestate"] = baked["savestate"]
                    mk["inputs"] = baked["inputs"]
                    mk.pop("needs_bake", None)
                except Exception as exc:  # noqa: BLE001
                    print(f"[select_queue] WARNING: Could not bake Select mark {mk.get('id')}: {exc}")
    bake_marks_s = round(time.perf_counter() - t_bake0, 2)

    round_cache_count = len(list(round_cache_dir.glob("r*_gf*.fs")))
    added = upsert_queue_items(raw_marks, data_dir=data_root)

    t_fin0 = time.perf_counter()
    mobile_manifest: dict[str, Any] | None = None
    if single_pass_enabled and (parquet_saved or personal_pq.exists()):
        try:
            from fightcade_analysis.round_renderer import finalize_single_pass_mobile_rounds

            mobile_manifest = finalize_single_pass_mobile_rounds(
                quark_id,
                data_dir=data_root,
                perspective=int(perspective),
                scale=max(1, int(scale)),
                elapsed_s=round(time.perf_counter() - t0, 2),
                video_encoder=eff_encoder,
                perf={
                    "mode": "single_pass_stream",
                    "stream_s": stream_s,
                    "parquet_s": parquet_s,
                    "bake_marks_s": bake_marks_s,
                    "video_encoder": eff_encoder,
                },
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[select_queue] WARNING: Single-pass mobile video finalize failed for {quark_id}: {exc}")
    finalize_s = round(time.perf_counter() - t_fin0, 2)
    elapsed = round(time.perf_counter() - t0, 2)

    # Append live performance telemetry to data/benchmarks/history.jsonl
    try:
        from fightcade_analysis.benchmark import append_benchmark_record

        dur_s = float(mobile_manifest.get("total_duration_s") or 0.0) if mobile_manifest else 0.0
        rnds = int(mobile_manifest.get("round_count") or round_cache_count) if mobile_manifest else round_cache_count
        append_benchmark_record(
            {
                "label": "live_single_pass" if single_pass_enabled else "live_scan_only",
                "mode": "live_stream_ingest",
                "quark_id": quark_id,
                "round_count": rnds,
                "gameplay_s": round(dur_s, 2),
                "elapsed_s": elapsed,
                "speedup_x": round(dur_s / elapsed, 2) if (elapsed > 0 and dur_s > 0) else 0.0,
                "total_size_bytes": int(mobile_manifest.get("total_size_bytes") or 0) if mobile_manifest else 0,
                "has_audio": bool(mobile_manifest.get("has_audio")) if mobile_manifest else False,
                "encoder": eff_encoder,
                "workers": 1,
                "batched": False,
                "stages": {
                    "stream_s": stream_s,
                    "parquet_s": parquet_s,
                    "bake_marks_s": bake_marks_s,
                    "finalize_s": finalize_s,
                },
            },
            data_dir=data_root,
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "quark_id": quark_id,
        "perspective": int(perspective),
        "marks_found": len(raw_marks),
        "marks_added": len(added),
        "parquet_saved": parquet_saved,
        "round_cache_count": round_cache_count,
        "mobile_video_rendered": bool(mobile_manifest and int(mobile_manifest.get("round_count") or 0) > 0),
        "mobile_round_count": int(mobile_manifest.get("round_count") or 0) if mobile_manifest else 0,
        "items": added,
        "elapsed_s": elapsed,
        "stages": {
            "stream_s": stream_s,
            "parquet_s": parquet_s,
            "bake_marks_s": bake_marks_s,
            "finalize_s": finalize_s,
        },
    }


def _list_fbneo_served_processes() -> list[dict[str, Any]]:
    """Return active live-match `fcadefbneo.exe` processes (`quark:served,sfiii3nr1,...`) with low overhead."""
    if sys.platform != "win32":
        return []
    # Fast check via tasklist first (~30ms, zero PowerShell/WMI overhead when Fightcade isn't running)
    try:
        tl = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq fcadefbneo.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if "fcadefbneo.exe" not in (tl.stdout or "").lower():
            return []
    except Exception:  # noqa: BLE001
        return []

    # Only query command lines when fcadefbneo.exe is actually running!
    try:
        ps_cmd = (
            "Get-CimInstance Win32_Process -Filter \"Name='fcadefbneo.exe'\" | "
            "Select-Object ProcessId, CommandLine | ConvertTo-Json -Compress"
        )
        res = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = (res.stdout or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
        rows = parsed if isinstance(parsed, list) else [parsed]
        found: list[dict[str, Any]] = []
        for r in rows:
            info = parse_served_match_cmdline(str(r.get("CommandLine") or ""))
            if info and info["game_id"].lower() == "sfiii3nr1":
                info["pid"] = int(r["ProcessId"])
                found.append(info)
        return found
    except Exception:  # noqa: BLE001
        return []


class LiveMatchWatcher:
    """Watches for live Fightcade `sfiii3nr1` matches and auto-scans them for Select marks on exit."""

    def __init__(
        self,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        poll_interval: float = 3.0,
        post_exit_delay: float = 8.0,
        min_match_seconds: float = 15.0,
        list_matches_fn: Callable[[], list[dict[str, Any]]] = _list_fbneo_served_processes,
        scan_fn: Callable[..., dict[str, Any]] = scan_replay_for_select_marks,
        on_match_scanned: Callable[[str, int], None] | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.poll_interval = poll_interval
        self.post_exit_delay = post_exit_delay
        self.min_match_seconds = min_match_seconds
        self._list_matches_fn = list_matches_fn
        self._scan_fn = scan_fn
        self._on_match_scanned = on_match_scanned

        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._active_matches: dict[int, dict[str, Any]] = {}
        self._scanned_qids: set[str] = set()
        self._paused = False
        self._state: dict[str, Any] = {
            "enabled": False,
            "paused": False,
            "phase": "idle",  # idle | paused | in_match | waiting_replay | scanning | error
            "message": "Watching for live Fightcade matches (tap Select/Coin in-game to mark sequences)",
            "active_quark_id": None,
            "active_player": None,
            "last_scan": None,
        }

    def is_paused(self) -> bool:
        with self._lock:
            return bool(self._paused)

    def set_paused(self, paused: bool) -> dict[str, Any]:
        with self._lock:
            self._paused = bool(paused)
            self._state["paused"] = self._paused
            if self._paused:
                self._state["phase"] = "paused"
                self._state["message"] = "Auto-capture paused (resume from Tray or Status menu)"
            else:
                self._state["phase"] = "idle"
                self._state["message"] = "Watching for live Fightcade matches (tap Select/Coin in-game to mark sequences)"
            return dict(self._state)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def is_in_match(self) -> bool:
        """Return True while any live Fightcade `quark:served` match is running."""
        with self._lock:
            return bool(self._active_matches)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_evt.clear()
            self._state["enabled"] = True
            self._thread = threading.Thread(target=self._loop, name="fc-match-watcher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        with self._lock:
            self._state["enabled"] = False
            t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def trigger_manual_scan(
        self,
        *,
        username: str | None = None,
        quark_id: str | None = None,
        player: int = 1,
        lookback_frames: int = DEFAULT_LOOKBACK_FRAMES,
    ) -> dict[str, Any]:
        """Start an async scan (called from `POST /api/queue/scan`) if not already scanning."""
        with self._lock:
            if self._state["phase"] == "scanning":
                return {"ok": True, "already_scanning": True, "watcher": dict(self._state)}
            self._state["phase"] = "scanning"
            self._state["message"] = f"Scanning {quark_id or username or 'latest match'} for Select marks..."

        def _worker() -> None:
            try:
                qid = (quark_id or "").strip()
                persp = int(player)
                stream_target: str | None = None
                if not qid:
                    if not username:
                        raise ValueError("Provide username or quark_id")
                    resolved = resolve_latest_replay_for_username(username, data_dir=self.data_dir)
                    qid = resolved["quark_id"]
                    persp = int(resolved["player"])
                    stream_target = resolved["stream_target"]
                with self._lock:
                    self._state["message"] = f"Scanning {qid} (P{persp}) for Select marks..."
                res = self._scan_fn(
                    qid,
                    perspective=persp,
                    stream_target=stream_target,
                    lookback_frames=lookback_frames,
                    data_dir=self.data_dir,
                )
                with self._lock:
                    self._scanned_qids.add(qid)
                    self._state["phase"] = "idle"
                    self._state["last_scan"] = res
                    n = res.get("marks_found", 0)
                    self._state["message"] = (
                        f"Scanned {qid} (P{persp}): found {n} Select mark(s) in {res.get('elapsed_s', 0)}s"
                    )
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._state["phase"] = "error"
                    self._state["message"] = f"Scan failed: {exc}"

        threading.Thread(target=_worker, name="fc-manual-scan", daemon=True).start()
        return {"ok": True, "started": True, "watcher": self.status()}

    def _poll_once(self) -> list[dict[str, Any]]:
        """Inspect live matches once; returns list of newly finished matches ready to scan."""
        now = time.monotonic()
        live = self._list_matches_fn()
        live_by_pid = {int(m["pid"]): m for m in live}

        finished: list[dict[str, Any]] = []
        with self._lock:
            for pid, m in live_by_pid.items():
                if pid not in self._active_matches:
                    self._active_matches[pid] = {
                        "pid": pid,
                        "quark_id": m["quark_id"],
                        "player": int(m["player"]),
                        "game_id": m["game_id"],
                        "first_seen": now,
                    }
                    print(f"[watcher] Detected live Fightcade match: {m['quark_id']} (P{m['player']}, PID={pid})")

            ended_pids = [pid for pid in self._active_matches if pid not in live_by_pid]
            for pid in ended_pids:
                rec = self._active_matches.pop(pid)
                dur = now - rec["first_seen"]
                if dur >= self.min_match_seconds and rec["quark_id"] not in self._scanned_qids:
                    finished.append(rec)

            if self._active_matches:
                first = next(iter(self._active_matches.values()))
                if self._state["phase"] != "scanning":
                    self._state["phase"] = "in_match"
                    self._state["active_quark_id"] = first["quark_id"]
                    self._state["active_player"] = first["player"]
                    self._state["message"] = (
                        f"In live match {first['quark_id']} (P{first['player']}) — "
                        "tap Select (Coin) in-game to mark takeover sequences!"
                    )
            elif not finished and self._state["phase"] == "in_match":
                self._state["phase"] = "idle"
                self._state["active_quark_id"] = None
                self._state["active_player"] = None
                self._state["message"] = "Watching for live Fightcade matches (tap Select/Coin in-game to mark sequences)"

        return finished

    def _scan_finished_match(self, rec: dict[str, Any]) -> None:
        qid = rec["quark_id"]
        persp = int(rec["player"])
        with self._lock:
            self._scanned_qids.add(qid)
            self._state["phase"] = "waiting_replay"
            self._state["active_quark_id"] = qid
            self._state["active_player"] = persp
            self._state["message"] = f"Match {qid} finished — waiting for Fightcade replay..."

        if self.post_exit_delay > 0 and self._stop_evt.wait(self.post_exit_delay):
            return

        with self._lock:
            self._state["phase"] = "scanning"
            self._state["message"] = f"Scanning finished match {qid} (P{persp}) for Select marks..."
        try:
            import inspect

            scan_kwargs: dict[str, Any] = {"perspective": persp, "data_dir": self.data_dir}
            try:
                sig = inspect.signature(self._scan_fn)
                if "render_mobile_video" in sig.parameters or any(
                    p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                ):
                    scan_kwargs["render_mobile_video"] = True
            except (TypeError, ValueError):
                pass
            res = self._scan_fn(qid, **scan_kwargs)
            print(f"[watcher] Auto-scanned {qid} (P{persp}): {res['marks_found']} mark(s) in {res['elapsed_s']}s")
            with self._lock:
                self._state["phase"] = "idle"
                self._state["active_quark_id"] = None
                self._state["active_player"] = None
                self._state["last_scan"] = res
                self._state["message"] = (
                    f"Auto-scanned {qid} (P{persp}): queued {res['marks_found']} Select mark(s)!"
                )
            if self._on_match_scanned is not None:
                try:
                    self._on_match_scanned(qid, persp)
                except Exception as cb_exc:  # noqa: BLE001
                    print(f"[watcher] Post-scan callback error for {qid}: {cb_exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[watcher] Auto-scan failed for {qid}: {exc}")
            with self._lock:
                self._state["phase"] = "error"
                self._state["active_quark_id"] = None
                self._state["active_player"] = None
                self._state["message"] = f"Auto-scan failed for {qid}: {exc}"

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                if not self.is_paused():
                    finished = self._poll_once()
                    for rec in finished:
                        if self._stop_evt.is_set() or self.is_paused():
                            break
                        self._scan_finished_match(rec)
            except Exception as exc:  # noqa: BLE001
                print(f"[watcher] Poll error: {exc}")
            self._stop_evt.wait(self.poll_interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan a Fightcade replay for in-game Select (Coin) marks into the Takeover Queue.")
    parser.add_argument("--username", default=None, help="Fightcade username (auto-scans latest match)")
    parser.add_argument("--quark-id", default=None, help="Specific Fightcade quarkid to scan")
    parser.add_argument("--player", type=int, choices=[1, 2], default=1, help="Your player side (1 or 2; auto-detected when --username is used)")
    parser.add_argument("--lookback-seconds", type=float, default=6.0, help="Seconds before Select press to start takeover (default: 6.0)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)

    qid = args.quark_id
    persp = args.player
    stream_target: str | None = None
    if not qid:
        if not args.username:
            print("Provide either --username <handle> or --quark-id <id>", file=sys.stderr)
            return 2
        resolved = resolve_latest_replay_for_username(args.username, data_dir=args.data_dir)
        qid = resolved["quark_id"]
        persp = int(resolved["player"])
        stream_target = resolved["stream_target"]
        print(f"Latest match for {args.username}: {qid} ({resolved['p1_name']} vs {resolved['p2_name']}) -> P{persp}")

    lookback_f = max(60, int(round(args.lookback_seconds * 60)))
    print(f"Scanning {qid} (P{persp}, lookback={args.lookback_seconds:.1f}s / {lookback_f}f) for Select marks...")
    res = scan_replay_for_select_marks(
        qid,
        perspective=persp,
        stream_target=stream_target,
        lookback_frames=lookback_f,
        data_dir=args.data_dir,
    )
    print(f"Done in {res['elapsed_s']}s: found {res['marks_found']} mark(s), added {res['marks_added']} new item(s) to queue.")
    for it in res["items"]:
        print(f"  - {it['id']} | R{it['round_id']} | gf{it['start_gf']}..{it['press_gf']} | {it['self_char_name']} vs {it['opp_char_name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
