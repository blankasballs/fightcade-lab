"""Headless 60fps MP4 clip renderer + instant `.fs` savestate & `.inputs` generator for Fightcade replays.

Uses `lua/capture_clip.lua` inside hidden `fcadefbneo.exe` to:
  1. Group multiple clip requests per replay (`quark_id`) into ONE single turbo pass
  2. Drop cached `.fs` savestates (`data/savestates/<quark_id>_gf<frame>.fs`) + `.inputs` sidecars
  3. Capture raw GD truecolor frames (`gui.gdscreenshot()`) across `[t0 - 210, t0 + 150]` (6.0s default)
  4. Encode crisp 60fps H.264 MP4s (`data/clips/<quark_id>_gf<frame>.mp4`) via bundled `imageio-ffmpeg`
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
import time
import uuid

import imageio_ffmpeg
import numpy as np
import pandas as pd

from fightcade_analysis.audio_mute import start_mute_watcher
from fightcade_analysis.env_config import (
    DEFAULT_DATA_DIR,
    REPO_ROOT,
    build_fbneo_cmdline,
    discover_grouflon_dir,
    ensure_fbneo_config_inheritance,
    resolve_default_fast_fbneo_exe,
    resolve_default_fbneo_exe,
)
from fightcade_analysis.fetch_replays import resolve_stream_target

DEFAULT_FBNEO_EXE = resolve_default_fbneo_exe()
DEFAULT_FAST_FBNEO_EXE = resolve_default_fast_fbneo_exe(stock_exe=DEFAULT_FBNEO_EXE)
DEFAULT_GROUFLON_DIR = DEFAULT_FBNEO_EXE.parent / "3rd_training_lua-master"
DEFAULT_CAPTURE_LUA = REPO_ROOT / "lua" / "capture_clip.lua"


def resolve_grouflon_dir(fbneo_exe: Path = DEFAULT_FBNEO_EXE) -> Path | None:
    """Locate Grouflon's `3rd_training_lua-master` next to `fbneo_exe` (or discovered FBNeo dir)."""
    return discover_grouflon_dir(fbneo_exe)

_ROUND_FS_RE = re.compile(r"^r(\d+)_gf(\d+)\.fs$", re.IGNORECASE)


def resolve_capture_fbneo_exe(fbneo_exe: Path = DEFAULT_FBNEO_EXE) -> tuple[Path, bool]:
    """Return `(exe_path, use_maxspeed)` — automatically prefers `fcadefbneo_fast.exe` when present."""
    if fbneo_exe == DEFAULT_FBNEO_EXE:
        fast = resolve_default_fast_fbneo_exe(stock_exe=fbneo_exe)
        if fast.exists():
            return fast, True
        if DEFAULT_FAST_FBNEO_EXE.exists():
            return DEFAULT_FAST_FBNEO_EXE, True
    if fbneo_exe.name.lower() == "fcadefbneo_fast.exe" and fbneo_exe.exists():
        return fbneo_exe, True
    sibling_fast = fbneo_exe.parent / "fcadefbneo_fast.exe"
    if sibling_fast.exists() and fbneo_exe.name.lower() == "fcadefbneo.exe":
        return sibling_fast, True
    return fbneo_exe, False


def resolve_takeover_fbneo_exe(fbneo_exe: Path = DEFAULT_FBNEO_EXE) -> Path:
    """Return exe for interactive Replay Takeover — prefers `fcadefbneo_fast.exe` for native speed throttle."""
    if fbneo_exe == DEFAULT_FBNEO_EXE:
        fast = resolve_default_fast_fbneo_exe(stock_exe=fbneo_exe)
        if fast.exists():
            return fast
        if DEFAULT_FAST_FBNEO_EXE.exists():
            return DEFAULT_FAST_FBNEO_EXE
    sibling_fast = fbneo_exe.parent / "fcadefbneo_fast.exe"
    if sibling_fast.exists() and fbneo_exe.name.lower() == "fcadefbneo.exe":
        return sibling_fast
    return fbneo_exe


def find_cached_round_savestate(
    quark_id: str,
    target_frame: int,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> tuple[Path, int, Path] | None:
    """Locate `(round_fs_path, round_start_gf, parquet_path)` for offline 19x clip rendering.

    Matches the exact `round_id` containing `target_frame` in Parquet so playback never crosses
    a round/character-select transition.
    """
    rc_dir = data_dir / "replay_cache" / quark_id
    if not rc_dir.is_dir():
        return None
    pq = find_parquet_for_quark(quark_id, data_dir=data_dir)
    if pq is None or not pq.exists():
        return None

    round_files: dict[int, tuple[int, Path]] = {}
    for p in rc_dir.glob("r*_gf*.fs"):
        m = _ROUND_FS_RE.match(p.name)
        if m and p.stat().st_size > 100_000:
            rid = int(m.group(1))
            sgf = int(m.group(2))
            round_files[rid] = (sgf, p)
    if not round_files:
        return None

    df = pd.read_parquet(pq, columns=["game_frame", "round_id"])
    hit = df[df["game_frame"] == int(target_frame)]
    if len(hit) == 0:
        # Closest frame within 5 frames
        diffs = (df["game_frame"].astype("int64") - int(target_frame)).abs()
        min_idx = int(diffs.idxmin())
        if int(diffs.iloc[min_idx]) > 15:
            return None
        rid = int(df["round_id"].iloc[min_idx])
    else:
        rid = int(hit["round_id"].iloc[0])

    entry = round_files.get(rid)
    if entry is None:
        return None
    snap_gf, fs_path = entry
    if snap_gf > int(target_frame):
        return None
    return fs_path, snap_gf, pq

DEFAULT_PRE_FRAMES: int = 210
DEFAULT_POST_FRAMES: int = 150
DEFAULT_SAVESTATE_LEAD: int = 120


@dataclass(frozen=True)
class ClipRequest:
    quark_id: str
    target_frame: int
    label: str = ""
    game_id: str = "sfiii3nr1"
    pre_frames: int = DEFAULT_PRE_FRAMES
    post_frames: int = DEFAULT_POST_FRAMES
    savestate_lead: int = DEFAULT_SAVESTATE_LEAD
    stream_target: str | None = None


@dataclass(frozen=True)
class ClipResult:
    quark_id: str
    target_frame: int
    mp4_path: Path
    savestate_path: Path | None
    frames_encoded: int
    width: int
    height: int
    cached: bool = False


def parse_gd_header(header: bytes) -> tuple[int, int]:
    """Parse FBNeo's 11-byte GD truecolor header -> (width, height)."""
    if len(header) < 11:
        raise ValueError(f"GD header truncated ({len(header)} bytes)")
    magic, width, height, truecolor = struct.unpack(">HHHB", header[:7])
    if magic != 65534 or truecolor != 1:
        raise ValueError(f"Unexpected GD header: magic={magic}, truecolor={truecolor}")
    return int(width), int(height)


def gd_frame_to_rgb24(frame_bytes: bytes, width: int, height: int) -> np.ndarray:
    """Convert one FBNeo GD truecolor frame (11 + w*h*4 bytes, pixel layout [0, R, G, B]) to RGB24."""
    expected = 11 + width * height * 4
    if len(frame_bytes) != expected:
        raise ValueError(f"Expected {expected} bytes for {width}x{height} GD frame, got {len(frame_bytes)}")
    argb = np.frombuffer(frame_bytes, dtype=np.uint8, offset=11).reshape((height, width, 4))
    return np.ascontiguousarray(argb[:, :, 1:4])


def build_ffmpeg_rgb24_cmd(
    mp4_path: Path,
    *,
    width: int = 384,
    height: int = 224,
    fps: int = 60,
    scale: int = 2,
    crf: int = 26,
    gop_size: int | None = None,
    input_path: str = "-",
    ffmpeg_exe: str | None = None,
) -> tuple[list[str], int, int]:
    """Build ffmpeg command list for encoding raw RGB24 video to crisp H.264 MP4 (4:3 DAR).

    When `scale == 1`, encodes at native pixel-art resolution (`384x224`) without an extra
    swscale pass so the browser scales cleanly with `image-rendering: pixelated`.
    """
    eff_ffmpeg = ffmpeg_exe or imageio_ffmpeg.get_ffmpeg_exe()
    s = max(1, int(scale))
    out_w = int(width) * s
    out_h = int(height) * s
    out_w += out_w % 2
    out_h += out_h % 2

    cmd = [
        eff_ffmpeg,
        "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{int(width)}x{int(height)}",
        "-r", str(int(fps)),
        "-i", str(input_path),
    ]
    if out_w != int(width) or out_h != int(height):
        cmd.extend(["-vf", f"scale={out_w}:{out_h}:flags=neighbor"])
    cmd.extend([
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", str(int(crf)),
        "-pix_fmt", "yuv420p",
        "-aspect", "4:3",
    ])
    if gop_size is not None and int(gop_size) > 0:
        g = str(int(gop_size))
        cmd.extend(["-g", g, "-keyint_min", g, "-sc_threshold", "0"])
    cmd.extend(["-movflags", "+faststart", str(mp4_path)])
    return cmd, out_w, out_h


def encode_rgb24_to_mp4(
    rgb24_path: Path,
    mp4_path: Path,
    *,
    width: int = 384,
    height: int = 224,
    fps: int = 60,
    scale: int = 2,
    crf: int = 26,
    gop_size: int | None = None,
) -> tuple[int, int, int]:
    """Encode raw concatenated RGB24 frames (from C++ `emu.capture_open`) directly via ffmpeg."""
    total_size = rgb24_path.stat().st_size
    frame_size = int(width) * int(height) * 3
    if frame_size <= 0 or total_size < frame_size:
        raise RuntimeError(f"Raw RGB24 frame dump is empty or truncated: {rgb24_path}")
    n_frames = total_size // frame_size
    mp4_path.parent.mkdir(parents=True, exist_ok=True)
    cmd, out_w, out_h = build_ffmpeg_rgb24_cmd(
        mp4_path,
        width=width,
        height=height,
        fps=fps,
        scale=scale,
        crf=crf,
        gop_size=gop_size,
        input_path=str(rgb24_path),
    )
    res = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {res.returncode}): {res.stderr.decode(errors='replace')}")
    return int(n_frames), out_w, out_h


def encode_gdframes_to_mp4(
    gdframes_path: Path,
    mp4_path: Path,
    *,
    fps: int = 60,
    scale: int = 2,
    crf: int = 18,
    gop_size: int | None = None,
) -> tuple[int, int, int]:
    """Stream-decode raw concatenated GD frames and encode to crisp nearest-neighbor H.264 MP4."""
    total_size = gdframes_path.stat().st_size
    if total_size < 12:
        raise RuntimeError(f"Raw GD frame dump is empty: {gdframes_path}")

    with gdframes_path.open("rb") as f:
        header = f.read(11)
        width, height = parse_gd_header(header)
        frame_size = 11 + width * height * 4
        n_frames = total_size // frame_size
        if n_frames == 0:
            raise RuntimeError(f"Incomplete GD frame dump ({total_size} < {frame_size})")

        f.seek(0)
        mp4_path.parent.mkdir(parents=True, exist_ok=True)
        cmd, out_w, out_h = build_ffmpeg_rgb24_cmd(
            mp4_path,
            width=width,
            height=height,
            fps=fps,
            scale=scale,
            crf=crf,
            gop_size=gop_size,
            input_path="-",
        )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert proc.stdin is not None
        encoded = 0
        try:
            for _ in range(n_frames):
                raw = f.read(frame_size)
                if len(raw) != frame_size:
                    break
                rgb = gd_frame_to_rgb24(raw, width, height)
                proc.stdin.write(rgb.tobytes())
                encoded += 1
            proc.stdin.close()
        except BrokenPipeError:
            pass

        _, stderr_bytes = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {stderr_bytes.decode(errors='replace')}")

    return encoded, out_w, out_h


def find_parquet_for_quark(quark_id: str, data_dir: Path = DEFAULT_DATA_DIR) -> Path | None:
    """Locate `<quark_id>.parquet` under `data/personal/` or `data/parquet/`."""
    for sub in ("personal", "parquet"):
        cand = data_dir / sub / f"{quark_id}.parquet"
        if cand.exists():
            return cand
    return None


def export_takeover_inputs(
    quark_id: str,
    target_frame: int,
    out_path: Path,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    parquet_path: Path | None = None,
    pre_frames: int = 180,
    post_frames: int = 240,
) -> Path | None:
    """Export deterministic `(game_frame, p1_input_mask, p2_input_mask)` CSV for instant savestate Replay Takeover."""
    pq = parquet_path or find_parquet_for_quark(quark_id, data_dir=data_dir)
    if pq is None or not pq.exists():
        return None

    df = pd.read_parquet(pq, columns=["game_frame", "p1_input_mask", "p2_input_mask"])
    lo = max(0, int(target_frame) - int(pre_frames))
    hi = int(target_frame) + int(post_frames)
    sub = df[(df["game_frame"] >= lo) & (df["game_frame"] <= hi)].drop_duplicates(subset=["game_frame"]).sort_values("game_frame")
    if len(sub) == 0:
        return None

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


def lookup_cached_stream_target(quark_id: str, data_dir: Path = DEFAULT_DATA_DIR) -> str | None:
    if not data_dir.exists():
        return None
    for jf in sorted(data_dir.glob("*.json")):
        try:
            doc = json.loads(jf.read_text(encoding="utf-8"))
            for s in doc.get("summaries", []):
                if s.get("quarkid") == quark_id and s.get("stream_target"):
                    return str(s["stream_target"])
        except Exception:  # noqa: BLE001
            continue
    return None


def _clip_meta_matches(meta_path: Path, pre_frames: int, post_frames: int, savestate_lead: int) -> bool:
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return (
            int(meta.get("pre_frames", -1)) == pre_frames
            and int(meta.get("post_frames", -1)) == post_frames
            and int(meta.get("savestate_lead", -1)) == savestate_lead
        )
    except Exception:  # noqa: BLE001
        return False


def _sanitize_tsv(text: str) -> str:
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")


def _render_single_replay_group(
    quark_id: str,
    requests: list[ClipRequest],
    boot_target: str,
    *,
    clips_dir: Path,
    savestates_dir: Path,
    fbneo_exe: Path,
    capture_lua: Path,
    scale: int,
    timeout_seconds: int,
    offline_inputs_path: Path | None = None,
    grouflon_overlay: bool = True,
) -> list[ClipResult]:
    """Capture ALL requested clips for one `quark_id` (or cached round `.fs`) in ONE single FBNeo pass."""
    eff_exe, use_maxspeed = resolve_capture_fbneo_exe(fbneo_exe)
    ensure_fbneo_config_inheritance(eff_exe, game_id=requests[0].game_id if requests else "sfiii3nr1")
    flags = ["-w", "-headless"] if use_maxspeed else ["-w"]
    cmd_str = build_fbneo_cmdline(
        eff_exe,
        flags,
        boot_target,
        capture_lua,
        target_is_path=(offline_inputs_path is not None),
    )

    sorted_reqs = sorted(requests, key=lambda r: r.target_frame)
    jobs_path = clips_dir / f"_jobs_{quark_id}_{os.getpid()}_{uuid.uuid4().hex[:8]}.tsv"
    job_entries: list[tuple[ClipRequest, Path, Path, Path, Path]] = []

    lines: list[str] = []
    for req in sorted_reqs:
        stem = f"{req.quark_id}_gf{req.target_frame}"
        mp4_path = clips_dir / f"{stem}.mp4"
        meta_path = clips_dir / f"{stem}.meta.json"
        savestate_path = savestates_dir / f"{stem}.fs"
        raw_frames_path = clips_dir / f"{stem}.gdframes"
        if raw_frames_path.exists():
            raw_frames_path.unlink()
        job_entries.append((req, mp4_path, meta_path, savestate_path, raw_frames_path))
        lines.append(
            "\t".join(
                [
                    str(req.target_frame),
                    str(req.pre_frames),
                    str(req.post_frames),
                    str(req.savestate_lead),
                    str(savestate_path),
                    str(raw_frames_path),
                    _sanitize_tsv(req.label),
                ]
            )
        )

    jobs_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env["FC_JOBS_FILE"] = str(jobs_path)
    if offline_inputs_path is not None:
        env["FC_OFFLINE_INPUTS"] = str(offline_inputs_path)
    g_dir = resolve_grouflon_dir(eff_exe) if grouflon_overlay else None
    if g_dir is not None:
        env["FC_GROUFLON_OVERLAY"] = "1"
        env["FC_GROUFLON_DIR"] = str(g_dir)
    else:
        env["FC_GROUFLON_OVERLAY"] = "0"

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE

    # Scale timeout slightly with max target frame (deep FT10 frames take ~2-3 min)
    max_tf = max(r.target_frame for r in sorted_reqs)
    eff_timeout = max(timeout_seconds, int(max_tf / 180) + 120)

    proc: subprocess.Popen[bytes] | None = None
    try:
        for _ in range(2):
            for _, _, _, _, raw_p in job_entries:
                if raw_p.exists():
                    raw_p.unlink()
            proc = subprocess.Popen(
                cmd_str,
                cwd=str(eff_exe.parent),
                env=env,
                startupinfo=startupinfo,
            )
            mute_stop = threading.Event()
            if not use_maxspeed:
                start_mute_watcher(proc.pid, mute_stop)
            try:
                proc.wait(timeout=eff_timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise
            finally:
                mute_stop.set()
            all_ok = (
                proc.returncode == 0
                and all(raw_p.exists() and raw_p.stat().st_size > 12 for _, _, _, _, raw_p in job_entries)
            )
            if all_ok:
                break

        err_file = Path(f"{jobs_path}.err")
        if err_file.exists():
            err_msg = err_file.read_text(encoding="utf-8", errors="replace").strip()
            if err_msg:
                print(f"WARNING: capture_clip.lua logged error for {quark_id}: {err_msg}", file=sys.stderr)
            err_file.unlink(missing_ok=True)
    finally:
        jobs_path.unlink(missing_ok=True)
        if offline_inputs_path is not None:
            offline_inputs_path.unlink(missing_ok=True)

    if proc is None or proc.returncode != 0:
        rc = proc.returncode if proc is not None else -1
        for _, _, _, _, raw_p in job_entries:
            raw_p.unlink(missing_ok=True)
        raise RuntimeError(f"fcadefbneo.exe batch capture failed for {quark_id} (exit={rc})")

    # Encode all captured raw GD dumps for this replay (parallel ffmpeg when multiple clips!)
    def _encode_one(entry: tuple[ClipRequest, Path, Path, Path, Path]) -> ClipResult:
        req, mp4_path, meta_path, savestate_path, raw_p = entry
        if not raw_p.exists() or raw_p.stat().st_size <= 12:
            raise RuntimeError(f"Missing raw GD frames for {req.quark_id}_gf{req.target_frame}")
        try:
            n_frames, out_w, out_h = encode_gdframes_to_mp4(raw_p, mp4_path, fps=60, scale=scale)
        finally:
            raw_p.unlink(missing_ok=True)

        meta_path.write_text(
            json.dumps(
                {
                    "quark_id": req.quark_id,
                    "target_frame": req.target_frame,
                    "pre_frames": req.pre_frames,
                    "post_frames": req.post_frames,
                    "savestate_lead": req.savestate_lead,
                    "frames_encoded": n_frames,
                    "width": out_w,
                    "height": out_h,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return ClipResult(
            quark_id=req.quark_id,
            target_frame=req.target_frame,
            mp4_path=mp4_path,
            savestate_path=savestate_path if savestate_path.exists() else None,
            frames_encoded=n_frames,
            width=out_w,
            height=out_h,
            cached=False,
        )

    results: list[ClipResult] = []
    if len(job_entries) == 1:
        results.append(_encode_one(job_entries[0]))
    else:
        with ThreadPoolExecutor(max_workers=min(3, len(job_entries))) as enc_pool:
            futs = [enc_pool.submit(_encode_one, e) for e in job_entries]
            for fut in as_completed(futs):
                results.append(fut.result())
    return results


def render_clips_batch(
    requests: list[ClipRequest],
    *,
    clips_dir: Path | None = None,
    savestates_dir: Path | None = None,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    capture_lua: Path = DEFAULT_CAPTURE_LUA,
    scale: int = 2,
    timeout_seconds: int = 240,
    max_workers: int = 3,
    stagger_seconds: float = 1.0,
    force: bool = False,
    grouflon_overlay: bool = True,
) -> tuple[dict[str, ClipResult], dict[str, int]]:
    """Render a batch of clips using single-pass per-replay capture + bounded replay parallelism.

    Returns:
        (results_by_key, stats_dict)
    """
    clips_dir = (clips_dir or (DEFAULT_DATA_DIR / "clips")).resolve()
    savestates_dir = (savestates_dir or (DEFAULT_DATA_DIR / "savestates")).resolve()
    capture_lua = capture_lua.resolve()
    clips_dir.mkdir(parents=True, exist_ok=True)
    savestates_dir.mkdir(parents=True, exist_ok=True)

    # Deduplicate by (quark_id, target_frame)
    unique_reqs: dict[str, ClipRequest] = {}
    for req in requests:
        key = f"{req.quark_id}_gf{req.target_frame}"
        if key not in unique_reqs:
            unique_reqs[key] = req

    results: dict[str, ClipResult] = {}
    pending_by_qid: dict[str, list[ClipRequest]] = defaultdict(list)

    for key, req in unique_reqs.items():
        mp4_path = clips_dir / f"{key}.mp4"
        meta_path = clips_dir / f"{key}.meta.json"
        savestate_path = savestates_dir / f"{key}.fs"
        inputs_path = savestates_dir / f"{key}.inputs"

        if force or not inputs_path.exists():
            export_takeover_inputs(
                req.quark_id,
                req.target_frame,
                inputs_path,
                data_dir=clips_dir.parent,
                pre_frames=max(req.savestate_lead + 30, req.pre_frames),
                post_frames=max(req.post_frames + 90, 240),
            )

        if (
            not force
            and mp4_path.exists()
            and savestate_path.exists()
            and _clip_meta_matches(meta_path, req.pre_frames, req.post_frames, req.savestate_lead)
        ):
            cached_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            results[key] = ClipResult(
                quark_id=req.quark_id,
                target_frame=req.target_frame,
                mp4_path=mp4_path,
                savestate_path=savestate_path,
                frames_encoded=int(cached_meta.get("frames_encoded", 0)),
                width=int(cached_meta.get("width", 0)),
                height=int(cached_meta.get("height", 0)),
                cached=True,
            )
        else:
            pending_by_qid[req.quark_id].append(req)

    stats = {
        "requested": len(unique_reqs),
        "cached": len(results),
        "rendered": 0,
        "failed": 0,
        "fbneo_passes": len(pending_by_qid),
    }

    if not pending_by_qid:
        return results, stats

    # Partition pending clips into:
    #   (A) Offline fast-path groups (cached round `.fs` + Parquet inputs -> 19x speed, ZERO Fightcade stream!)
    #   (B) Online stream fallback groups (for older replays without round cache)
    data_root = clips_dir.parent
    offline_groups: dict[tuple[str, Path, int, Path], list[ClipRequest]] = defaultdict(list)
    online_by_qid: dict[str, list[ClipRequest]] = defaultdict(list)
    for qid, qreqs in pending_by_qid.items():
        for req in qreqs:
            rc = find_cached_round_savestate(qid, req.target_frame, data_dir=data_root)
            if rc is not None:
                fs_p, snap_gf, pq_p = rc
                offline_groups[(qid, fs_p.resolve(), snap_gf, pq_p)].append(req)
                continue
            online_by_qid[qid].append(req)

    # Pre-resolve stream targets politely (sequentially) ONLY for online fallback groups
    resolved_targets: dict[str, str] = {}
    for qid, qreqs in online_by_qid.items():
        first = qreqs[0]
        target = (
            first.stream_target
            or lookup_cached_stream_target(qid, data_dir=data_root)
            or resolve_stream_target(first.game_id, qid)
        )
        resolved_targets[qid] = target

    # Build unified work list: (group_id, quark_id, reqs, boot_target, offline_inputs_or_None, sort_depth)
    work_items: list[tuple[str, str, list[ClipRequest], str, Path | None, int]] = []
    for (qid, fs_p, snap_gf, pq_p), greqs in offline_groups.items():
        max_end = max(r.target_frame + r.post_frames for r in greqs) + 30
        tmp_inp = clips_dir / f"_offinp_{qid}_{fs_p.stem}_{os.getpid()}_{uuid.uuid4().hex[:8]}.inputs"
        written = export_takeover_inputs(
            qid,
            snap_gf,
            tmp_inp,
            data_dir=data_root,
            parquet_path=pq_p,
            pre_frames=0,
            post_frames=max(600, max_end - snap_gf),
        )
        if written is not None:
            work_items.append((f"{qid}:{fs_p.stem}", qid, greqs, str(fs_p), written, max_end - snap_gf))
        else:
            # Fallback to online stream if inputs export unexpectedly failed
            online_by_qid[qid].extend(greqs)
            if qid not in resolved_targets:
                first = greqs[0]
                resolved_targets[qid] = (
                    first.stream_target
                    or lookup_cached_stream_target(qid, data_dir=data_root)
                    or resolve_stream_target(first.game_id, qid)
                )

    for qid, greqs in online_by_qid.items():
        work_items.append((qid, qid, greqs, resolved_targets[qid], None, max(r.target_frame for r in greqs)))

    stats["fbneo_passes"] = len(work_items)
    stats["offline_passes"] = sum(1 for w in work_items if w[4] is not None)
    work_items.sort(key=lambda w: w[5], reverse=True)

    def _run_work(idx: int, item: tuple[str, str, list[ClipRequest], str, Path | None, int]) -> list[ClipResult]:
        _, qid, greqs, boot_t, off_inp, _ = item
        if idx > 0 and stagger_seconds > 0 and off_inp is None:
            time.sleep(min(idx, max_workers) * stagger_seconds)
        return _render_single_replay_group(
            qid,
            greqs,
            boot_t,
            clips_dir=clips_dir,
            savestates_dir=savestates_dir,
            fbneo_exe=fbneo_exe,
            capture_lua=capture_lua,
            scale=scale,
            timeout_seconds=timeout_seconds,
            offline_inputs_path=off_inp,
            grouflon_overlay=grouflon_overlay,
        )

    workers = max(1, min(max_workers, len(work_items)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fut_to_item = {pool.submit(_run_work, i, item): item for i, item in enumerate(work_items)}
        for fut in as_completed(fut_to_item):
            item = fut_to_item[fut]
            try:
                group_res = fut.result()
                for cr in group_res:
                    results[f"{cr.quark_id}_gf{cr.target_frame}"] = cr
                    stats["rendered"] += 1
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += len(item[2])
                print(f"WARNING: Failed to render replay batch {item[0]}: {exc}", file=sys.stderr)

    return results, stats


def render_clip_and_savestate(
    quark_id: str,
    target_frame: int,
    *,
    game_id: str = "sfiii3nr1",
    pre_frames: int = DEFAULT_PRE_FRAMES,
    post_frames: int = DEFAULT_POST_FRAMES,
    savestate_lead: int = DEFAULT_SAVESTATE_LEAD,
    label: str = "",
    stream_target: str | None = None,
    clips_dir: Path | None = None,
    savestates_dir: Path | None = None,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    capture_lua: Path = DEFAULT_CAPTURE_LUA,
    scale: int = 2,
    timeout_seconds: int = 240,
    force: bool = False,
) -> ClipResult:
    """Capture a headless 60fps MP4 clip + instant `.fs` savestate for `(quark_id, target_frame)`."""
    req = ClipRequest(
        quark_id=quark_id,
        target_frame=target_frame,
        label=label,
        game_id=game_id,
        pre_frames=pre_frames,
        post_frames=post_frames,
        savestate_lead=savestate_lead,
        stream_target=stream_target,
    )
    res_map, _ = render_clips_batch(
        [req],
        clips_dir=clips_dir,
        savestates_dir=savestates_dir,
        fbneo_exe=fbneo_exe,
        capture_lua=capture_lua,
        scale=scale,
        timeout_seconds=timeout_seconds,
        max_workers=1,
        stagger_seconds=0.0,
        force=force,
    )
    key = f"{quark_id}_gf{target_frame}"
    if key not in res_map:
        raise RuntimeError(f"Clip capture failed for {key}")
    return res_map[key]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a headless 60fps MP4 clip + .fs savestate for a Fightcade moment.")
    parser.add_argument("--quark-id", required=True, help="Fightcade quarkid (e.g. 1728857242828-5771)")
    parser.add_argument("--frame", type=int, required=True, help="Target game_frame (t0)")
    parser.add_argument("--game-id", default="sfiii3nr1")
    parser.add_argument("--pre-frames", type=int, default=DEFAULT_PRE_FRAMES)
    parser.add_argument("--post-frames", type=int, default=DEFAULT_POST_FRAMES)
    parser.add_argument("--savestate-lead", type=int, default=DEFAULT_SAVESTATE_LEAD)
    parser.add_argument("--label", default="", help="Optional HUD overlay text on the clip")
    parser.add_argument("--scale", type=int, default=2, help="Nearest-neighbor upscale factor (default: 2)")
    parser.add_argument("--force", action="store_true", help="Re-render even if cached MP4/savestate exist")
    parser.add_argument("--export-inputs-only", action="store_true", help="Only export .inputs takeover sidecar from Parquet")
    args = parser.parse_args(argv)

    if args.export_inputs_only:
        out_inp = DEFAULT_DATA_DIR / "savestates" / f"{args.quark_id}_gf{args.frame}.inputs"
        written = export_takeover_inputs(
            args.quark_id,
            args.frame,
            out_inp,
            data_dir=DEFAULT_DATA_DIR,
            pre_frames=max(args.savestate_lead + 30, args.pre_frames),
            post_frames=max(args.post_frames + 90, 240),
        )
        if written is None:
            print(f"ERROR: Could not export takeover inputs for {args.quark_id} @ gf{args.frame}", file=sys.stderr)
            return 1
        print(f"Exported takeover inputs: {written}")
        return 0

    res = render_clip_and_savestate(
        quark_id=args.quark_id,
        target_frame=args.frame,
        game_id=args.game_id,
        pre_frames=args.pre_frames,
        post_frames=args.post_frames,
        savestate_lead=args.savestate_lead,
        label=args.label,
        scale=args.scale,
        force=args.force,
    )
    print(
        f"Rendered {res.mp4_path} ({res.frames_encoded} frames, {res.width}x{res.height}, "
        f"{res.mp4_path.stat().st_size:,} bytes) | savestate={res.savestate_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
