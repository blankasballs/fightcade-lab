"""Offline per-round silent 60fps mobile MP4 renderer, telemetry timeline extractor, and instant `.fs` drill baker.

Reuses the cached per-round savestates (`data/replay_cache/<qid>/rNN_gfXXXX.fs`) and Parquet
inputs created during unified single-pass ingestion so rendering runs completely offline via
`fcadefbneo_fast.exe -maxspeed` (~19x real-time, natively silent, zero Fightcade re-streaming).

Each rendered round outputs:
  - `data/replays/<qid>/rNN.mp4` (libx264, CRF 23, dense `-g 15` keyframes for snappy mobile scrubbing)
  - `data/replays/<qid>/manifest.json` (exact `start_gf` / `end_gf` / `frame_count`, characters,
    HP curves, and clickable telemetry event markers for 1-to-1 frame-exact mobile annotation)
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
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
import uuid

import imageio_ffmpeg
import numpy as np
import pandas as pd

from fightcade_analysis.audio_mute import start_mute_watcher
from fightcade_analysis.clip_renderer import (
    DEFAULT_CAPTURE_LUA,
    DEFAULT_DATA_DIR,
    DEFAULT_FBNEO_EXE,
    encode_gdframes_to_mp4,
    export_takeover_inputs,
    find_cached_round_savestate,
    find_parquet_for_quark,
    resolve_capture_fbneo_exe,
    resolve_grouflon_dir,
)
from fightcade_analysis.coaching import pretty_char
from fightcade_analysis.env_config import (
    build_fbneo_cmdline,
    ensure_fbneo_config_inheritance,
    get_disk_cache_cap_bytes,
    get_disk_cache_cap_gb,
)
from fightcade_analysis.features import POSTURE_KNOCKDOWN
from fightcade_analysis.naming import resolve_replay_players_and_ranks
from fightcade_analysis.perspective import (
    Side,
    group_rounds_into_matches as _group_rounds_into_matches_persp,
    normalize_side,
    ordered_distinct_chars,
    project_manifest,
    project_marker,
)
from fightcade_analysis.select_queue import load_queue

_ROUND_FS_RE = re.compile(r"^r(\d+)_gf(\d+)\.fs$", re.IGNORECASE)

DEFAULT_MOBILE_CRF: int = 26
DEFAULT_MOBILE_GOP: int = 30  # 0.5s keyframe interval at 60fps for snappy mobile seeking + compact size
DEFAULT_MOBILE_SCALE: int = 2  # 768x448 nearest-neighbor upscaled prior to yuv420p for crisp chroma
MANIFEST_VERSION: int = 3


def group_rounds_into_matches(
    rounds: list[dict[str, Any]],
    *,
    perspective: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Delegate FT2 match grouping to the canonical `perspective.py` boundary."""
    return _group_rounds_into_matches_persp(rounds, viewer_side=normalize_side(perspective))


def ensure_manifest_matches(
    manifest: dict[str, Any],
    *,
    data_dir: Path | None = None,
    viewer_side: int | None = None,
    username: str | None = None,
) -> dict[str, Any]:
    """Project a loaded raw/legacy replay manifest cleanly through the canonical `perspective.py` layer."""
    return project_manifest(
        manifest,
        viewer_side=viewer_side,
        username=username,
        data_dir=data_dir,
    )


def parse_overlay_tsv_to_json(
    tsv_path: Path,
    out_json_path: Path,
    *,
    start_gf: int | None = None,
) -> dict[str, Any]:
    """Convert raw Lua per-frame overlay TSV into a compact JSON sidecar for browser `<canvas>` rendering."""
    rows_by_gf: dict[int, list[Any]] = {}
    min_gf: int | None = None
    max_gf: int = 0

    for raw_line in tsv_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not raw_line:
            continue
        parts = raw_line.split("\t")
        if len(parts) < 5:
            continue
        try:
            gf = int(parts[0])
            m1 = int(parts[1])
            m2 = int(parts[2])
        except ValueError:
            continue
        g1 = [int(x) for x in parts[3].split(",") if x] if parts[3] and parts[3] != "0" else []
        g2 = [int(x) for x in parts[4].split(",") if x] if parts[4] and parts[4] != "0" else []
        boxes: list[list[int]] = []
        if len(parts) >= 6 and parts[5]:
            for item in parts[5].split(";"):
                if not item:
                    continue
                b = item.split(",")
                if len(b) == 5:
                    try:
                        boxes.append([int(b[0]), int(b[1]), int(b[2]), int(b[3]), int(b[4])])
                    except ValueError:
                        continue
        rows_by_gf[gf] = [m1, m2, g1, g2, boxes]
        if min_gf is None or gf < min_gf:
            min_gf = gf
        if gf > max_gf:
            max_gf = gf

    if min_gf is None:
        raise RuntimeError(f"Overlay TSV dump was empty: {tsv_path}")

    base_gf = int(start_gf) if start_gf is not None else min_gf
    frames: list[list[Any]] = []
    last_row: list[Any] = [0, 0, [], [], []]
    for gf in range(base_gf, max_gf + 1):
        row = rows_by_gf.get(gf)
        if row is not None:
            last_row = row
        frames.append(last_row)

    payload = {
        "v": 1,
        "w": 384,
        "h": 224,
        "start_gf": base_gf,
        "frame_count": len(frames),
        "frames": frames,
    }
    out_json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_json_path.with_suffix(f".tmp_{os.getpid()}_{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(out_json_path)
    return {
        "path": str(out_json_path),
        "start_gf": base_gf,
        "frame_count": len(frames),
        "size_bytes": int(out_json_path.stat().st_size),
    }


def mobile_replay_dir(quark_id: str, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "replays" / str(quark_id)


def mobile_manifest_path(quark_id: str, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return mobile_replay_dir(quark_id, data_dir) / "manifest.json"


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp_{os.getpid()}_{uuid.uuid4().hex[:6]}")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def list_cached_round_savestates(quark_id: str, data_dir: Path = DEFAULT_DATA_DIR) -> list[tuple[int, int, Path]]:
    """Return sorted `[(round_id, snap_gf, fs_path), ...]` for cached per-round `.fs` files."""
    rc_dir = data_dir.resolve() / "replay_cache" / str(quark_id)
    if not rc_dir.is_dir():
        return []
    found: list[tuple[int, int, Path]] = []
    for p in rc_dir.glob("r*_gf*.fs"):
        m = _ROUND_FS_RE.match(p.name)
        if m and p.stat().st_size > 100_000:
            found.append((int(m.group(1)), int(m.group(2)), p.resolve()))
    found.sort(key=lambda x: x[0])
    return found


def _extract_hp_episodes(
    gfs: np.ndarray,
    hps: np.ndarray,
    *,
    min_loss: int = 14,
    merge_gap: int = 45,
) -> list[tuple[int, int]]:
    """Find `(start_gf, total_hp_lost)` damage episodes on a single player's HP series."""
    if len(gfs) < 2:
        return []
    diffs = np.diff(hps.astype(np.int32))
    drop_indices = np.flatnonzero(diffs < 0)
    if len(drop_indices) == 0:
        return []

    episodes: list[tuple[int, int, int]] = []  # (first_idx, last_idx, total_drop)
    for idx in drop_indices:
        drop = int(-diffs[idx])
        if episodes and (int(gfs[idx]) - int(gfs[episodes[-1][1]])) <= merge_gap:
            f_i, _, tot = episodes[-1]
            episodes[-1] = (f_i, int(idx), tot + drop)
        else:
            episodes.append((int(idx), int(idx), drop))

    out: list[tuple[int, int]] = []
    for f_i, _, tot in episodes:
        if tot >= min_loss:
            out.append((int(gfs[f_i]), int(tot)))
    return out


def extract_round_timeline_markers(
    df_round: pd.DataFrame,
    *,
    perspective: int | None = None,
    select_marks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extract objective raw (`side: 1|2`) telemetry markers & raw `[offset_f, p1_hp, p2_hp]` curve.

    If `perspective` is explicitly provided (for legacy callers/tests), markers are also projected
    through `perspective.project_marker` while preserving raw `kind` & `side`.
    """
    if len(df_round) == 0:
        return {"markers": [], "hp_curve": []}

    sub = df_round.drop_duplicates(subset=["game_frame"]).sort_values("game_frame")
    gfs = sub["game_frame"].to_numpy(dtype=np.int64)
    p1_hp = sub["p1_hp"].to_numpy(dtype=np.int32)
    p2_hp = sub["p2_hp"].to_numpy(dtype=np.int32)
    start_gf = int(gfs[0])
    p1_max = max(1, int(np.max(p1_hp)))
    p2_max = max(1, int(np.max(p2_hp)))

    raw_markers: list[dict[str, Any]] = []

    # 1. Objective damage episodes (`side` = player who TOOK the damage)
    for side, hp_arr in ((1, p1_hp), (2, p2_hp)):
        for gf, dmg in _extract_hp_episodes(gfs, hp_arr, min_loss=14):
            raw_markers.append({"gf": gf, "offset_f": gf - start_gf, "kind": "dmg", "side": side, "value": dmg})

    # 2. Superfreeze rising edges
    for side, col in ((1, "p1_superfreeze"), (2, "p2_superfreeze")):
        if col not in sub.columns:
            continue
        vals = (sub[col].to_numpy(dtype=np.int32) > 0).astype(np.int8)
        rising = np.flatnonzero(np.diff(np.concatenate(([0], vals))) == 1)
        last_gf = -9999
        for idx in rising:
            gf = int(gfs[idx])
            if gf - last_gf < 60:
                continue
            last_gf = gf
            raw_markers.append({"gf": gf, "offset_f": gf - start_gf, "kind": "super", "side": side, "value": side})

    # 3. Knockdown rising edges
    for side, col in ((1, "p1_posture"), (2, "p2_posture")):
        if col not in sub.columns:
            continue
        vals = (sub[col].to_numpy(dtype=np.int32) == POSTURE_KNOCKDOWN).astype(np.int8)
        rising = np.flatnonzero(np.diff(np.concatenate(([0], vals))) == 1)
        last_gf = -9999
        for idx in rising:
            gf = int(gfs[idx])
            if gf - last_gf < 60:
                continue
            last_gf = gf
            raw_markers.append({"gf": gf, "offset_f": gf - start_gf, "kind": "kd", "side": side, "value": side})

    # 4. In-game Select marks belonging to this round
    end_gf = int(gfs[-1])
    for mk in select_marks or []:
        mk_gf = int(mk.get("press_gf") or mk.get("end_gf") or mk.get("start_gf") or 0)
        if start_gf <= mk_gf <= end_gf:
            mk_side = normalize_side(mk.get("perspective"), default=1)
            raw_markers.append(
                {
                    "gf": mk_gf,
                    "offset_f": mk_gf - start_gf,
                    "kind": "select_mark",
                    "side": mk_side,
                    "value": 1,
                    "note": str(mk.get("note") or "In-game Select Mark"),
                }
            )

    raw_markers.sort(key=lambda m: (int(m["gf"]), str(m["kind"]), int(m["side"])))

    if perspective is not None:
        vs: Side = normalize_side(perspective)
        markers_out = [project_marker(m, vs) for m in raw_markers]
    else:
        markers_out = raw_markers

    # 5. Raw downsampled HP curve (`[offset_f, p1_hp_pct, p2_hp_pct]`)
    step = 15
    indices = list(range(0, len(gfs), step))
    if indices[-1] != len(gfs) - 1:
        indices.append(len(gfs) - 1)
    hp_curve: list[list[float | int]] = []
    for i in indices:
        hp_curve.append(
            [
                int(gfs[i] - start_gf),
                round(float(p1_hp[i]) / float(p1_max), 3),
                round(float(p2_hp[i]) / float(p2_max), 3),
            ]
        )

    return {"markers": markers_out, "hp_curve": hp_curve}


def _export_round_inputs(df_round: pd.DataFrame, out_path: Path) -> Path:
    sub = df_round.drop_duplicates(subset=["game_frame"]).sort_values("game_frame")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["game_frame,p1_mask,p2_mask\n"]
    for gf, m1, m2 in zip(
        sub["game_frame"].to_numpy(),
        sub["p1_input_mask"].to_numpy(),
        sub["p2_input_mask"].to_numpy(),
    ):
        lines.append(f"{int(gf)},{int(m1)},{int(m2)}\n")
    out_path.write_text("".join(lines), encoding="utf-8")
    return out_path


def render_round_mp4(
    quark_id: str,
    round_id: int,
    fs_path: Path,
    snap_gf: int,
    df_round: pd.DataFrame,
    out_dir: Path,
    *,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    capture_lua: Path = DEFAULT_CAPTURE_LUA,
    scale: int = DEFAULT_MOBILE_SCALE,
    crf: int = DEFAULT_MOBILE_CRF,
    gop_size: int = DEFAULT_MOBILE_GOP,
    timeout_seconds: int = 180,
    grouflon_overlay: bool = False,
    export_overlay: bool = True,
) -> dict[str, Any]:
    """Render one complete round offline via `fcadefbneo_fast.exe -maxspeed` to clean `rNN.mp4`
    + toggleable `rNN_overlay.json` sidecar in a single pass.
    """
    eff_exe, use_maxspeed = resolve_capture_fbneo_exe(fbneo_exe)
    ensure_fbneo_config_inheritance(eff_exe)
    lua_path = capture_lua.resolve()
    fs_resolved = fs_path.resolve()
    flags = ["-w", "-headless"] if use_maxspeed else ["-w"]
    cmd_str = build_fbneo_cmdline(eff_exe, flags, fs_resolved, lua_path, target_is_path=True)

    sub = df_round.drop_duplicates(subset=["game_frame"]).sort_values("game_frame")
    if len(sub) < 2:
        raise ValueError(f"Round {round_id} of {quark_id} has fewer than 2 telemetry frames")

    min_gf = int(sub["game_frame"].iloc[0])
    max_gf = int(sub["game_frame"].iloc[-1])
    start_gf = max(snap_gf, min_gf)
    end_gf = max(start_gf + 1, max_gf)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"r{round_id:02d}"
    mp4_path = out_dir / f"{stem}.mp4"
    overlay_json_path = out_dir / f"{stem}_overlay.json"
    tmp_tag = f"_rnd_{quark_id}_{stem}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    use_direct_pipe = bool(use_maxspeed and not grouflon_overlay)
    raw_path = out_dir / f"{tmp_tag}.gdframes"
    target_out_path = mp4_path if use_direct_pipe else raw_path
    overlay_tsv_path = out_dir / f"{tmp_tag}.overlay.tsv"
    gf_range_path = Path(f"{target_out_path}.gf")
    jobs_path = out_dir / f"{tmp_tag}.tsv"
    inp_path = out_dir / f"{tmp_tag}.inputs"

    _export_round_inputs(sub, inp_path)

    # Single job spanning [start_gf .. end_gf] with NO savestate output and NO t0 banner
    pre_frames = 0
    post_frames = max(1, end_gf - start_gf)
    jobs_path.write_text(
        f"{start_gf}\t{pre_frames}\t{post_frames}\t0\t\t{target_out_path}\t\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["FC_JOBS_FILE"] = str(jobs_path)
    env["FC_OFFLINE_INPUTS"] = str(inp_path)
    env["FC_HIDE_T0_BANNER"] = "1"
    env["FC_WRITE_GF_RANGE"] = "1"
    if use_direct_pipe:
        env["FC_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
        env["FC_VIDEO_SCALE"] = str(max(1, int(scale)))
        env["FC_VIDEO_CRF"] = str(int(crf))
        env["FC_VIDEO_GOP"] = str(int(gop_size) if gop_size else 0)
    g_dir = resolve_grouflon_dir(eff_exe)
    env["FC_GROUFLON_OVERLAY"] = "1" if (grouflon_overlay and g_dir is not None) else "0"
    if g_dir is not None:
        env["FC_GROUFLON_DIR"] = str(g_dir)
        if export_overlay:
            env["FC_OVERLAY_DUMP_OUT"] = str(overlay_tsv_path)

    popen_kwargs: dict[str, Any] = {"cwd": str(eff_exe.parent), "env": env}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        popen_kwargs["startupinfo"] = si

    t0 = time.perf_counter()
    proc: subprocess.Popen[bytes] | None = None
    try:
        for _ in range(2):
            target_out_path.unlink(missing_ok=True)
            gf_range_path.unlink(missing_ok=True)
            proc = subprocess.Popen(cmd_str, **popen_kwargs)
            mute_stop = threading.Event()
            if not use_maxspeed:
                start_mute_watcher(proc.pid, mute_stop)
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise
            finally:
                mute_stop.set()
            min_bytes = 1024 if use_direct_pipe else 12
            if proc.returncode == 0 and target_out_path.exists() and target_out_path.stat().st_size > min_bytes:
                break

        err_file = Path(f"{jobs_path}.err")
        if err_file.exists():
            err_file.unlink(missing_ok=True)

        min_bytes = 1024 if use_direct_pipe else 12
        if proc is None or proc.returncode != 0 or not target_out_path.exists() or target_out_path.stat().st_size <= min_bytes:
            rc = proc.returncode if proc is not None else -1
            raise RuntimeError(f"Offline round capture failed for {quark_id} {stem} (exit={rc})")

        actual_first_gf = start_gf
        actual_last_gf = end_gf
        pipe_frames = 0
        if gf_range_path.exists():
            try:
                parts = gf_range_path.read_text(encoding="utf-8").strip().split(",")
                if len(parts) >= 2:
                    actual_first_gf = int(parts[0])
                    actual_last_gf = int(parts[1])
                if len(parts) >= 3:
                    pipe_frames = int(parts[2])
            except Exception:  # noqa: BLE001
                pass

        if use_direct_pipe:
            s = max(1, int(scale))
            out_w = 384 * s
            out_h = 224 * s
            n_frames = pipe_frames if pipe_frames > 0 else max(1, actual_last_gf - actual_first_gf + 1)
        else:
            n_frames, out_w, out_h = encode_gdframes_to_mp4(
                raw_path,
                mp4_path,
                fps=60,
                scale=scale,
                crf=crf,
                gop_size=gop_size,
            )
        overlay_url: str | None = None
        if export_overlay and overlay_tsv_path.exists() and overlay_tsv_path.stat().st_size > 16:
            parse_overlay_tsv_to_json(overlay_tsv_path, overlay_json_path, start_gf=actual_first_gf)
            overlay_url = f"/replays/{quark_id}/{stem}_overlay.json"
    finally:
        raw_path.unlink(missing_ok=True)
        overlay_tsv_path.unlink(missing_ok=True)
        gf_range_path.unlink(missing_ok=True)
        jobs_path.unlink(missing_ok=True)
        inp_path.unlink(missing_ok=True)

    return {
        "round_id": int(round_id),
        "filename": f"{stem}.mp4",
        "video_url": f"/replays/{quark_id}/{stem}.mp4",
        "overlay_url": overlay_url,
        "snap_gf": int(snap_gf),
        "start_gf": int(actual_first_gf),
        "end_gf": int(actual_last_gf),
        "frame_count": int(n_frames),
        "fps": 60,
        "duration_s": round(float(n_frames) / 60.0, 2),
        "width": int(out_w),
        "height": int(out_h),
        "size_bytes": int(mp4_path.stat().st_size) if mp4_path.exists() else 0,
        "render_s": round(time.perf_counter() - t0, 2),
    }


def render_round_overlay_only(
    quark_id: str,
    round_id: int,
    fs_path: Path,
    snap_gf: int,
    df_round: pd.DataFrame,
    out_dir: Path,
    *,
    start_gf_override: int | None = None,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    capture_lua: Path = DEFAULT_CAPTURE_LUA,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    """Ultra-fast (~1s) turbo pass (no screenshots, no ffmpeg) to dump `rNN_overlay.json` for an existing MP4."""
    eff_exe, use_maxspeed = resolve_capture_fbneo_exe(fbneo_exe)
    g_dir = resolve_grouflon_dir(eff_exe)
    if g_dir is None:
        raise FileNotFoundError(
            "Grouflon 3rd_training_lua-master not found next to fcadefbneo.exe — "
            "Replay Review, Inputs, HP timeline, Notes, and basic Takeover work without it. "
            "Install 3rd_training_lua-master alongside fcadefbneo.exe to enable Hitbox overlays."
        )

    ensure_fbneo_config_inheritance(eff_exe)
    lua_path = capture_lua.resolve()
    fs_resolved = fs_path.resolve()
    flags = ["-w", "-headless"] if use_maxspeed else ["-w"]
    cmd_str = build_fbneo_cmdline(eff_exe, flags, fs_resolved, lua_path, target_is_path=True)

    sub = df_round.drop_duplicates(subset=["game_frame"]).sort_values("game_frame")
    if len(sub) < 2:
        raise ValueError(f"Round {round_id} of {quark_id} has fewer than 2 telemetry frames")

    min_gf = int(sub["game_frame"].iloc[0])
    max_gf = int(sub["game_frame"].iloc[-1])
    start_gf = int(start_gf_override) if start_gf_override is not None else max(snap_gf, min_gf)
    end_gf = max(start_gf + 1, max_gf)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"r{round_id:02d}"
    overlay_json_path = out_dir / f"{stem}_overlay.json"
    tmp_tag = f"_ov_{quark_id}_{stem}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    overlay_tsv_path = out_dir / f"{tmp_tag}.overlay.tsv"
    jobs_path = out_dir / f"{tmp_tag}.tsv"
    inp_path = out_dir / f"{tmp_tag}.inputs"

    _export_round_inputs(sub, inp_path)
    # Empty raw_out + non-empty FC_OVERLAY_DUMP_OUT -> stays in pure turbo (-maxspeed), ~19x real-time!
    jobs_path.write_text(f"{start_gf}\t0\t{max(1, end_gf - start_gf)}\t0\t\t\t\n", encoding="utf-8")

    env = os.environ.copy()
    env["FC_JOBS_FILE"] = str(jobs_path)
    env["FC_OFFLINE_INPUTS"] = str(inp_path)
    env["FC_HIDE_T0_BANNER"] = "1"
    env["FC_GROUFLON_OVERLAY"] = "0"
    env["FC_GROUFLON_DIR"] = str(g_dir)
    env["FC_OVERLAY_DUMP_OUT"] = str(overlay_tsv_path)

    popen_kwargs: dict[str, Any] = {"cwd": str(eff_exe.parent), "env": env}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        popen_kwargs["startupinfo"] = si

    t0 = time.perf_counter()
    try:
        proc = subprocess.Popen(cmd_str, **popen_kwargs)
        mute_stop = threading.Event()
        if not use_maxspeed:
            start_mute_watcher(proc.pid, mute_stop)
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise
        finally:
            mute_stop.set()

        Path(f"{jobs_path}.err").unlink(missing_ok=True)
        if proc.returncode != 0 or not overlay_tsv_path.exists() or overlay_tsv_path.stat().st_size <= 16:
            raise RuntimeError(f"Overlay dump failed for {quark_id} {stem} (exit={proc.returncode})")

        info = parse_overlay_tsv_to_json(overlay_tsv_path, overlay_json_path, start_gf=start_gf)
        info["round_id"] = int(round_id)
        info["overlay_url"] = f"/replays/{quark_id}/{stem}_overlay.json"
        info["elapsed_s"] = round(time.perf_counter() - t0, 2)
        return info
    finally:
        overlay_tsv_path.unlink(missing_ok=True)
        jobs_path.unlink(missing_ok=True)
        inp_path.unlink(missing_ok=True)


def generate_replay_overlays(
    quark_id: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    capture_lua: Path = DEFAULT_CAPTURE_LUA,
    max_workers: int = 3,
    force: bool = False,
) -> dict[str, Any]:
    """Fast no-reencode backfill: generate `rNN_overlay.json` sidecars for an already-rendered replay."""
    eff_exe, _ = resolve_capture_fbneo_exe(fbneo_exe)
    if resolve_grouflon_dir(eff_exe) is None:
        raise FileNotFoundError(
            "Grouflon 3rd_training_lua-master not found — Hitbox overlay generation requires "
            "3rd_training_lua-master installed in your Fightcade FBNeo folder."
        )
    data_root = data_dir.resolve()
    pq_path = find_parquet_for_quark(quark_id, data_dir=data_root)
    if pq_path is None or not pq_path.exists():
        raise FileNotFoundError(f"Parquet telemetry not found for {quark_id}")
    cached_rounds = list_cached_round_savestates(quark_id, data_dir=data_root)
    if not cached_rounds:
        raise FileNotFoundError(f"No cached per-round savestates in replay_cache/{quark_id}")

    out_dir = mobile_replay_dir(quark_id, data_dir=data_root)
    m_path = mobile_manifest_path(quark_id, data_dir=data_root)
    if not m_path.exists():
        raise FileNotFoundError(f"Replay manifest not found for {quark_id}")

    mf = json.loads(m_path.read_text(encoding="utf-8"))
    round_start_by_id = {int(r["round_id"]): int(r["start_gf"]) for r in mf.get("rounds", []) if r.get("round_id")}
    df = pd.read_parquet(pq_path)

    work: list[tuple[int, int, Path, pd.DataFrame, int | None]] = []
    for rid, snap_gf, fs_p in cached_rounds:
        if rid not in round_start_by_id:
            continue
        ov_p = out_dir / f"r{rid:02d}_overlay.json"
        if not force and ov_p.is_file() and ov_p.stat().st_size > 32:
            continue
        df_r = df[df["round_id"] == rid]
        if len(df_r) < 5:
            continue
        work.append((rid, snap_gf, fs_p, df_r, round_start_by_id.get(rid)))

    t0 = time.perf_counter()
    generated = 0
    if work:
        workers = max(1, min(int(max_workers), len(work)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [
                pool.submit(
                    render_round_overlay_only,
                    quark_id,
                    rid,
                    fs_p,
                    snap_gf,
                    df_r,
                    out_dir,
                    start_gf_override=sgf,
                    fbneo_exe=fbneo_exe,
                    capture_lua=capture_lua,
                )
                for (rid, snap_gf, fs_p, df_r, sgf) in work
            ]
            for fut in as_completed(futs):
                fut.result()
                generated += 1

    # Keep manifest.json raw on disk (update overlay_url only), return viewer-projected view to API callers
    for r in mf.get("rounds", []):
        rid = int(r.get("round_id", 0))
        ov_name = f"r{rid:02d}_overlay.json"
        if (out_dir / ov_name).is_file():
            r["overlay_url"] = f"/replays/{quark_id}/{ov_name}"
    _write_json_atomic(m_path, mf)
    projected = ensure_manifest_matches(mf, data_dir=data_root)
    return {
        "ok": True,
        "quark_id": str(quark_id),
        "generated": generated,
        "elapsed_s": round(time.perf_counter() - t0, 2),
        "manifest": projected,
    }


def render_replay_rounds(
    quark_id: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    perspective: int = 1,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    capture_lua: Path = DEFAULT_CAPTURE_LUA,
    scale: int = DEFAULT_MOBILE_SCALE,
    crf: int = DEFAULT_MOBILE_CRF,
    gop_size: int = DEFAULT_MOBILE_GOP,
    max_workers: int = 2,
    force: bool = False,
    grouflon_overlay: bool = False,
    export_overlay: bool = True,
    progress_cb: Callable[[int, int, str], None] | None = None,
    should_pause_fn: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Render all cached rounds of `quark_id` into `data/replays/<qid>/rNN.mp4` + `manifest.json`."""
    data_root = data_dir.resolve()
    pq_path = find_parquet_for_quark(quark_id, data_dir=data_root)
    if pq_path is None or not pq_path.exists():
        raise FileNotFoundError(f"Parquet telemetry not found for {quark_id}")

    cached_rounds = list_cached_round_savestates(quark_id, data_dir=data_root)
    if not cached_rounds:
        raise FileNotFoundError(
            f"No cached per-round savestates in {data_root / 'replay_cache' / quark_id}. "
            "Stream this replay once to populate replay_cache."
        )

    out_dir = mobile_replay_dir(quark_id, data_dir=data_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    m_path = mobile_manifest_path(quark_id, data_dir=data_root)

    existing_rounds: dict[int, dict[str, Any]] = {}
    if not force and m_path.exists():
        try:
            old_doc = json.loads(m_path.read_text(encoding="utf-8"))
            for r in old_doc.get("rounds", []):
                rid = int(r.get("round_id", 0))
                mp4_p = out_dir / str(r.get("filename") or f"r{rid:02d}.mp4")
                if rid > 0 and mp4_p.exists() and mp4_p.stat().st_size > 1024:
                    existing_rounds[rid] = r
        except Exception:  # noqa: BLE001
            pass

    df = pd.read_parquet(pq_path)
    queue_items = [q for q in load_queue(data_root) if str(q.get("quark_id")) == str(quark_id)]

    # Build per-round work items
    work: list[tuple[int, int, Path, pd.DataFrame]] = []
    for rid, snap_gf, fs_p in cached_rounds:
        df_r = df[df["round_id"] == rid]
        if len(df_r) < 5:
            continue
        work.append((rid, snap_gf, fs_p, df_r))

    if not work:
        raise RuntimeError(f"No matching telemetry rounds found in {pq_path} for cached savestates")

    total = len(work)
    done_count = 0
    round_entries: dict[int, dict[str, Any]] = {}
    to_render: list[tuple[int, int, Path, pd.DataFrame]] = []

    for rid, snap_gf, fs_p, df_r in work:
        if not force and rid in existing_rounds:
            entry = dict(existing_rounds[rid])
            # Refresh objective raw timeline markers (in case new Select marks were scanned)
            tl = extract_round_timeline_markers(df_r, select_marks=queue_items)
            actual_start = int(entry.get("start_gf", 0))
            if actual_start > 0:
                for mk in tl["markers"]:
                    mk["offset_f"] = max(0, int(mk["gf"]) - actual_start)
            entry["markers"] = tl["markers"]
            entry["hp_curve"] = tl["hp_curve"]
            round_entries[rid] = entry
            done_count += 1
        else:
            to_render.append((rid, snap_gf, fs_p, df_r))

    if progress_cb:
        progress_cb(done_count, total, f"Rendering rounds ({done_count}/{total})")

    t0 = time.perf_counter()

    def _render_one(item: tuple[int, int, Path, pd.DataFrame]) -> dict[str, Any]:
        rid, snap_gf, fs_p, df_r = item
        while should_pause_fn and should_pause_fn():
            time.sleep(2.0)
        res = render_round_mp4(
            quark_id,
            rid,
            fs_p,
            snap_gf,
            df_r,
            out_dir,
            fbneo_exe=fbneo_exe,
            capture_lua=capture_lua,
            scale=scale,
            crf=crf,
            gop_size=gop_size,
            grouflon_overlay=grouflon_overlay,
            export_overlay=export_overlay,
        )
        p1_cid = int(df_r["p1_char"].mode().iloc[0]) if "p1_char" in df_r.columns else 0
        p2_cid = int(df_r["p2_char"].mode().iloc[0]) if "p2_char" in df_r.columns else 0
        end_p1_hp = int(df_r["p1_hp"].iloc[-1]) if "p1_hp" in df_r.columns else 0
        end_p2_hp = int(df_r["p2_hp"].iloc[-1]) if "p2_hp" in df_r.columns else 0
        winner = 1 if end_p1_hp > end_p2_hp else (2 if end_p2_hp > end_p1_hp else 0)
        tl = extract_round_timeline_markers(df_r, select_marks=queue_items)
        actual_start = int(res["start_gf"])
        for mk in tl["markers"]:
            mk["offset_f"] = max(0, int(mk["gf"]) - actual_start)
        res.update(
            {
                "p1_char": p1_cid,
                "p2_char": p2_cid,
                "p1_char_name": pretty_char(p1_cid),
                "p2_char_name": pretty_char(p2_cid),
                "winner": winner,
                "markers": tl["markers"],
                "hp_curve": tl["hp_curve"],
            }
        )
        return res

    if to_render:
        workers = max(1, min(int(max_workers), len(to_render)))
        if workers == 1:
            for item in to_render:
                entry = _render_one(item)
                round_entries[entry["round_id"]] = entry
                done_count += 1
                if progress_cb:
                    progress_cb(done_count, total, f"Rendered R{entry['round_id']} ({done_count}/{total})")
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_render_one, it): it[0] for it in to_render}
                for fut in as_completed(futs):
                    entry = fut.result()
                    round_entries[entry["round_id"]] = entry
                    done_count += 1
                    if progress_cb:
                        progress_cb(done_count, total, f"Rendered R{entry['round_id']} ({done_count}/{total})")

    ordered_rounds = [round_entries[k] for k in sorted(round_entries)]
    return build_and_write_replay_manifest(
        quark_id,
        ordered_rounds,
        df,
        data_dir=data_root,
        perspective=perspective,
        grouflon_overlay=bool(grouflon_overlay and resolve_grouflon_dir(fbneo_exe) is not None),
        elapsed_s=round(time.perf_counter() - t0, 2),
    )


def build_and_write_replay_manifest(
    quark_id: str,
    ordered_rounds: list[dict[str, Any]],
    df: pd.DataFrame,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    perspective: int = 1,
    grouflon_overlay: bool = False,
    elapsed_s: float = 0.0,
) -> dict[str, Any]:
    """Build and atomically persist raw v3 `manifest.json`, returning the viewer-projected view."""
    data_root = data_dir.resolve()
    m_path = mobile_manifest_path(quark_id, data_dir=data_root)
    p1_cid_main = int(df["p1_char"].mode().iloc[0]) if "p1_char" in df.columns and len(df) else 0
    p2_cid_main = int(df["p2_char"].mode().iloc[0]) if "p2_char" in df.columns and len(df) else 0
    p1_chars = ordered_distinct_chars(ordered_rounds, 1)
    p2_chars = ordered_distinct_chars(ordered_rounds, 2)
    p1_meta = resolve_replay_players_and_ranks(quark_id, 1, data_dir=data_root)

    raw_manifest = {
        "version": MANIFEST_VERSION,
        "quark_id": str(quark_id),
        "perspective": normalize_side(perspective),
        "p1": {
            "side": 1,
            "name": p1_meta.get("self_player_name") or "P1",
            "rank_letter": p1_meta.get("self_rank_letter") or "",
            "char_id": p1_cid_main,
            "char_name": pretty_char(p1_cid_main),
            "chars": p1_chars,
        },
        "p2": {
            "side": 2,
            "name": p1_meta.get("opp_player_name") or "P2",
            "rank_letter": p1_meta.get("opp_rank_letter") or "",
            "char_id": p2_cid_main,
            "char_name": pretty_char(p2_cid_main),
            "chars": p2_chars,
        },
        "p1_char": p1_cid_main,
        "p2_char": p2_cid_main,
        "p1_char_name": pretty_char(p1_cid_main),
        "p2_char_name": pretty_char(p2_cid_main),
        "p1_chars": p1_chars,
        "p2_chars": p2_chars,
        "round_count": len(ordered_rounds),
        "total_duration_s": round(sum(float(r.get("duration_s", 0.0)) for r in ordered_rounds), 2),
        "total_size_bytes": sum(int(r.get("size_bytes", 0)) for r in ordered_rounds),
        "rendered_at": datetime.now().isoformat(timespec="seconds"),
        "render_elapsed_s": round(float(elapsed_s), 2),
        "grouflon_overlay": bool(grouflon_overlay),
        "rounds": ordered_rounds,
    }
    _write_json_atomic(m_path, raw_manifest)
    return project_manifest(raw_manifest, viewer_side=normalize_side(perspective), data_dir=data_root)


def finalize_single_pass_mobile_rounds(
    quark_id: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    perspective: int = 1,
    scale: int = DEFAULT_MOBILE_SCALE,
    elapsed_s: float = 0.0,
) -> dict[str, Any] | None:
    """Finalize per-round MP4s + overlay TSVs written during single-pass Pass 1 into `manifest.json`.

    Returns the projected manifest if all telemetry rounds were captured cleanly, else `None`
    (so Pass 2 can seamlessly render any missing rounds).
    """
    data_root = data_dir.resolve()
    pq_path = find_parquet_for_quark(quark_id, data_dir=data_root)
    if pq_path is None or not pq_path.exists():
        return None
    out_dir = mobile_replay_dir(quark_id, data_dir=data_root)
    if not out_dir.is_dir():
        return None

    df = pd.read_parquet(pq_path)
    if len(df) == 0 or "round_id" not in df.columns:
        return None

    cached_by_rid = {rid: snap_gf for (rid, snap_gf, _) in list_cached_round_savestates(quark_id, data_dir=data_root)}
    queue_items = [q for q in load_queue(data_root) if str(q.get("quark_id")) == str(quark_id)]
    s = max(1, int(scale))
    out_w = 384 * s
    out_h = 224 * s

    ordered_rounds: list[dict[str, Any]] = []
    expected_rids = sorted(int(r) for r in df["round_id"].dropna().unique() if int(r) > 0)
    for rid in expected_rids:
        df_r = df[df["round_id"] == rid]
        if len(df_r) < 5:
            continue
        stem = f"r{rid:02d}"
        mp4_path = out_dir / f"{stem}.mp4"
        gf_path = out_dir / f"{stem}.mp4.gf"
        ov_tsv_path = out_dir / f"_{stem}.overlay.tsv"
        ov_json_path = out_dir / f"{stem}_overlay.json"

        try:
            if not (mp4_path.exists() and mp4_path.stat().st_size > 1024 and gf_path.exists()):
                continue
            parts = gf_path.read_text(encoding="utf-8").strip().split(",")
            if len(parts) < 3:
                continue
            actual_first_gf = int(parts[0])
            actual_last_gf = int(parts[1])
            n_frames = int(parts[2])
            if n_frames < 2:
                continue

            overlay_url: str | None = None
            if ov_tsv_path.exists() and ov_tsv_path.stat().st_size > 16:
                parse_overlay_tsv_to_json(ov_tsv_path, ov_json_path, start_gf=actual_first_gf)
                overlay_url = f"/replays/{quark_id}/{stem}_overlay.json"
            elif ov_json_path.exists() and ov_json_path.stat().st_size > 32:
                overlay_url = f"/replays/{quark_id}/{stem}_overlay.json"

            p1_cid = int(df_r["p1_char"].mode().iloc[0]) if "p1_char" in df_r.columns else 0
            p2_cid = int(df_r["p2_char"].mode().iloc[0]) if "p2_char" in df_r.columns else 0
            end_p1_hp = int(df_r["p1_hp"].iloc[-1]) if "p1_hp" in df_r.columns else 0
            end_p2_hp = int(df_r["p2_hp"].iloc[-1]) if "p2_hp" in df_r.columns else 0
            winner = 1 if end_p1_hp > end_p2_hp else (2 if end_p2_hp > end_p1_hp else 0)
            tl = extract_round_timeline_markers(df_r, select_marks=queue_items)
            for mk in tl["markers"]:
                mk["offset_f"] = max(0, int(mk["gf"]) - actual_first_gf)

            ordered_rounds.append(
                {
                    "round_id": rid,
                    "filename": f"{stem}.mp4",
                    "video_url": f"/replays/{quark_id}/{stem}.mp4",
                    "overlay_url": overlay_url,
                    "snap_gf": int(cached_by_rid.get(rid, actual_first_gf)),
                    "start_gf": actual_first_gf,
                    "end_gf": actual_last_gf,
                    "frame_count": n_frames,
                    "fps": 60,
                    "duration_s": round(float(n_frames) / 60.0, 2),
                    "width": out_w,
                    "height": out_h,
                    "size_bytes": int(mp4_path.stat().st_size),
                    "render_s": 0.0,
                    "p1_char": p1_cid,
                    "p2_char": p2_cid,
                    "p1_char_name": pretty_char(p1_cid),
                    "p2_char_name": pretty_char(p2_cid),
                    "winner": winner,
                    "markers": tl["markers"],
                    "hp_curve": tl["hp_curve"],
                }
            )
        finally:
            gf_path.unlink(missing_ok=True)
            ov_tsv_path.unlink(missing_ok=True)

    # Clean up any leftover temp sidecars
    for leftover in list(out_dir.glob("*.mp4.gf")) + list(out_dir.glob("_r*.overlay.tsv")):
        leftover.unlink(missing_ok=True)

    if not ordered_rounds:
        return None

    return build_and_write_replay_manifest(
        quark_id,
        ordered_rounds,
        df,
        data_dir=data_root,
        perspective=perspective,
        grouflon_overlay=False,
        elapsed_s=elapsed_s,
    )


def bake_drill_savestate(
    quark_id: str,
    start_gf: int,
    end_gf: int,
    *,
    selection_id: str,
    data_dir: Path = DEFAULT_DATA_DIR,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    capture_lua: Path = DEFAULT_CAPTURE_LUA,
    tail_frames: int = 90,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Bake an instant `<1s` `.fs` savestate + `.inputs` sidecar offline for a mobile-selected drill."""
    data_root = data_dir.resolve()
    sel_dir = data_root / "selections"
    sel_dir.mkdir(parents=True, exist_ok=True)
    out_fs = sel_dir / f"{selection_id}.fs"
    out_inp = sel_dir / f"{selection_id}.inputs"

    rc = find_cached_round_savestate(quark_id, int(start_gf), data_dir=data_root)
    if rc is None:
        raise FileNotFoundError(
            f"No cached round savestate covering gf{start_gf} for {quark_id}. "
            "Process the replay first to populate data/replay_cache."
        )
    round_fs, snap_gf, pq_path = rc

    # Always export exact takeover inputs covering [start_gf .. end_gf + tail_frames]
    span = max(240, (int(end_gf) - int(start_gf)) + int(tail_frames) + 30)
    written_inp = export_takeover_inputs(
        quark_id,
        int(start_gf),
        out_inp,
        data_dir=data_root,
        parquet_path=pq_path,
        pre_frames=10,
        post_frames=span,
    )
    if written_inp is None:
        raise RuntimeError(f"Could not export takeover inputs for {quark_id} @ gf{start_gf}")

    # Fast-path: if drill starts right at the cached round start frame, direct file copy (<5ms!)
    if abs(int(start_gf) - int(snap_gf)) <= 1:
        shutil.copy2(round_fs, out_fs)
        return {
            "savestate": str(out_fs),
            "inputs": str(out_inp),
            "method": "round_cache_copy",
            "elapsed_s": 0.01,
        }

    eff_exe, use_maxspeed = resolve_capture_fbneo_exe(fbneo_exe)
    ensure_fbneo_config_inheritance(eff_exe)
    lua_path = capture_lua.resolve()
    fs_resolved = round_fs.resolve()
    flags = ["-w", "-headless"] if use_maxspeed else ["-w"]
    cmd_str = build_fbneo_cmdline(eff_exe, flags, fs_resolved, lua_path, target_is_path=True)

    tmp_tag = f"_bake_{selection_id}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    jobs_path = sel_dir / f"{tmp_tag}.tsv"
    off_inp = sel_dir / f"{tmp_tag}.inputs"

    # Export offline replay inputs from round start through target start_gf + small margin
    export_takeover_inputs(
        quark_id,
        int(snap_gf),
        off_inp,
        data_dir=data_root,
        parquet_path=pq_path,
        pre_frames=0,
        post_frames=max(60, int(start_gf) - int(snap_gf) + 30),
    )

    # Savestate-only job (`raw_out == ""`): capture_clip.lua stays in turbo, saves .fs at start_gf, exits immediately!
    jobs_path.write_text(f"{int(start_gf)}\t0\t0\t0\t{out_fs}\t\t\n", encoding="utf-8")

    env = os.environ.copy()
    env["FC_JOBS_FILE"] = str(jobs_path)
    env["FC_OFFLINE_INPUTS"] = str(off_inp)
    env["FC_HIDE_T0_BANNER"] = "1"

    popen_kwargs: dict[str, Any] = {"cwd": str(eff_exe.parent), "env": env}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        popen_kwargs["startupinfo"] = si

    t0 = time.perf_counter()
    try:
        proc = subprocess.Popen(cmd_str, **popen_kwargs)
        mute_stop = threading.Event()
        if not use_maxspeed:
            start_mute_watcher(proc.pid, mute_stop)
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise
        finally:
            mute_stop.set()
    finally:
        jobs_path.unlink(missing_ok=True)
        Path(f"{jobs_path}.err").unlink(missing_ok=True)
        off_inp.unlink(missing_ok=True)

    if not out_fs.exists() or out_fs.stat().st_size < 4096:
        raise RuntimeError(f"Offline drill savestate bake failed for {selection_id} (gf{start_gf})")

    return {
        "savestate": str(out_fs),
        "inputs": str(out_inp),
        "method": "offline_fast_bake",
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                try:
                    total += int(entry.stat().st_size)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def get_protected_replay_quark_ids(data_dir: Path = DEFAULT_DATA_DIR) -> set[str]:
    """Return `quark_id`s referenced by any Saved Moment, Select mark, Set Note, or Playlist.

    These matches are NEVER auto-pruned by the rolling local disk cache cap.
    """
    sel_dir = data_dir.resolve() / "selections"
    protected: set[str] = set()
    if not sel_dir.is_dir():
        return protected

    def _scan_jsonl(filename: str) -> list[dict[str, Any]]:
        p = sel_dir / filename
        if not p.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        rows.append(obj)
                except json.JSONDecodeError:
                    continue
        except OSError:
            return []
        return rows

    for fname in ("annotations.jsonl", "set_notes.jsonl", "bookmarks.jsonl"):
        for rec in _scan_jsonl(fname):
            qid = str(rec.get("quark_id") or "").strip()
            if qid:
                protected.add(qid)

    for rec in _scan_jsonl("queue.jsonl"):
        if str(rec.get("status") or "").lower() == "dismissed":
            continue
        qid = str(rec.get("quark_id") or "").strip()
        if qid:
            protected.add(qid)

    for pl in _scan_jsonl("playlists.jsonl"):
        for it in pl.get("items") or []:
            if isinstance(it, dict):
                qid = str(it.get("quark_id") or "").strip()
                if qid:
                    protected.add(qid)

    return protected


def get_mobile_replays_disk_usage(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Return local full-match MP4 cache usage, match count, protected count, and configured cap."""
    data_root = data_dir.resolve()
    replays_root = data_root / "replays"
    protected = get_protected_replay_quark_ids(data_root)
    total_bytes = 0
    match_count = 0
    protected_present = 0
    if replays_root.is_dir():
        for d in replays_root.iterdir():
            if not d.is_dir():
                continue
            match_count += 1
            total_bytes += _dir_size_bytes(d)
            if d.name in protected:
                protected_present += 1
    cap_gb = get_disk_cache_cap_gb(data_root)
    cap_bytes = get_disk_cache_cap_bytes(data_root)
    return {
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / (1024 ** 3), 2),
        "match_count": match_count,
        "protected_count": protected_present,
        "cap_gb": cap_gb,
        "cap_bytes": cap_bytes,
    }


def prune_old_mobile_replays(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    max_sets: int = 30,
    max_cache_bytes: int | None = None,
) -> list[str]:
    """Delete oldest unprotected full-round MP4 folders beyond disk cap (default `5 GB`) or `max_sets`.

    Matches referenced by Saved Moments (`annotations.jsonl`), Select Marks (`queue.jsonl`),
    Set Notes (`set_notes.jsonl`), or Playlists (`playlists.jsonl`) are NEVER pruned.
    """
    data_root = data_dir.resolve()
    replays_root = data_root / "replays"
    if not replays_root.is_dir():
        return []

    eff_max_bytes = get_disk_cache_cap_bytes(data_root) if max_cache_bytes is None else int(max_cache_bytes)
    if max_sets <= 0 and eff_max_bytes <= 0:
        return []

    protected = get_protected_replay_quark_ids(data_root)
    entries: list[tuple[float, int, bool, Path]] = []
    total_bytes = 0
    for d in replays_root.iterdir():
        if not d.is_dir():
            continue
        mf = d / "manifest.json"
        mtime = mf.stat().st_mtime if mf.exists() else d.stat().st_mtime
        size_b = _dir_size_bytes(d)
        total_bytes += size_b
        entries.append((mtime, size_b, d.name in protected, d))

    # Sort oldest-first so we always evict the stalest unprotected sets first
    entries.sort(key=lambda x: x[0])
    remaining_count = len(entries)
    removed: list[str] = []

    for _, size_b, is_protected, old_dir in entries:
        over_sets = max_sets > 0 and remaining_count > max_sets
        over_bytes = eff_max_bytes > 0 and total_bytes > eff_max_bytes
        if not over_sets and not over_bytes:
            break
        if is_protected:
            continue
        shutil.rmtree(old_dir, ignore_errors=True)
        removed.append(old_dir.name)
        remaining_count -= 1
        total_bytes = max(0, total_bytes - size_b)

    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render silent 60fps per-round mobile MP4s from replay_cache.")
    parser.add_argument("--quark-id", required=True, help="Fightcade quarkid")
    parser.add_argument("--player", type=int, choices=[1, 2], default=1, help="Your player side (1 or 2)")
    parser.add_argument("--workers", type=int, default=2, help="Parallel round workers (default: 2)")
    parser.add_argument("--crf", type=int, default=DEFAULT_MOBILE_CRF)
    parser.add_argument("--gop", type=int, default=DEFAULT_MOBILE_GOP)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--burn-overlay", action="store_true", help="Burn Grouflon overlay directly into MP4 pixels")
    parser.add_argument("--overlays-only", action="store_true", help="Fast turbo pass to generate toggleable overlay JSON sidecars only")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)

    if args.overlays_only:
        res = generate_replay_overlays(
            args.quark_id,
            data_dir=args.data_dir,
            max_workers=args.workers,
            force=args.force,
        )
        print(f"Generated {res['generated']} overlay sidecar(s) for {res['quark_id']} in {res['elapsed_s']}s")
        return 0

    def _prog(done: int, total: int, msg: str) -> None:
        print(f"[round_renderer] {msg} ({done}/{total})")

    mf = render_replay_rounds(
        args.quark_id,
        data_dir=args.data_dir,
        perspective=args.player,
        crf=args.crf,
        gop_size=args.gop,
        max_workers=args.workers,
        force=args.force,
        grouflon_overlay=args.burn_overlay,
        export_overlay=True,
        progress_cb=_prog,
    )
    print(
        f"Rendered {mf['round_count']} rounds for {mf['quark_id']} "
        f"({mf['total_duration_s']}s total, {mf['total_size_bytes'] / (1024 * 1024):.1f} MB) "
        f"in {mf['render_elapsed_s']}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
