"""Repeatable performance benchmark suite & production timing logger for Fightcade Lab.

Records structured benchmark & real-world pipeline timings to `data/benchmarks/history.jsonl`
so every optimization (custom FBNeo, audio capture, GPU encoding, batched workers, single-pass
stream ingestion) can be verified with exact before/after numbers.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any

import imageio_ffmpeg
import pandas as pd

from fightcade_analysis.clip_renderer import (
    DEFAULT_DATA_DIR,
    DEFAULT_FBNEO_EXE,
    REPO_ROOT,
    find_parquet_for_quark,
    resolve_capture_fbneo_exe,
)
from fightcade_analysis.round_renderer import (
    list_cached_round_savestates,
    render_round_mp4,
    render_replay_rounds,
)


def benchmark_history_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "benchmarks" / "history.jsonl"


def _git_short_head() -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def probe_video_streams(mp4_path: Path, *, ffmpeg_exe: str | None = None) -> dict[str, Any]:
    """Inspect an MP4 using ffmpeg stderr header (no ffprobe binary required)."""
    ff = ffmpeg_exe or imageio_ffmpeg.get_ffmpeg_exe()
    res = subprocess.run(
        [ff, "-hide_banner", "-i", str(mp4_path)],
        capture_output=True,
        text=True,
        timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    txt = res.stderr or ""
    has_video = "Video:" in txt
    has_audio = "Audio:" in txt
    audio_line = ""
    video_line = ""
    duration_line = ""
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("Duration:") and not duration_line:
            duration_line = s
        elif "Video:" in s and not video_line:
            video_line = s
        elif "Audio:" in s and not audio_line:
            audio_line = s
    return {
        "has_video": has_video,
        "has_audio": has_audio,
        "duration_line": duration_line,
        "video_line": video_line,
        "audio_line": audio_line,
    }


def append_benchmark_record(
    record: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Path:
    """Append one structured timing record to `data/benchmarks/history.jsonl`."""
    path = benchmark_history_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(record)
    entry.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    entry.setdefault("cpu_count", os.cpu_count() or 1)
    if "git_commit" not in entry:
        entry["git_commit"] = _git_short_head()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def load_benchmark_history(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    limit: int = 50,
) -> list[dict[str, Any]]:
    path = benchmark_history_path(data_dir)
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                rows.append(obj)
        except json.JSONDecodeError:
            continue
    return rows[-max(1, int(limit)) :]


def pick_default_benchmark_quark(data_dir: Path = DEFAULT_DATA_DIR) -> str:
    """Pick a cached replay with 6–10 rounds for consistent benchmarking."""
    rc_root = data_dir.resolve() / "replay_cache"
    if not rc_root.is_dir():
        raise FileNotFoundError(f"No replay_cache directory found at {rc_root}")
    candidates: list[tuple[int, str]] = []
    for d in sorted(rc_root.iterdir()):
        if not d.is_dir():
            continue
        qid = d.name
        pq = find_parquet_for_quark(qid, data_dir=data_dir)
        rounds = list_cached_round_savestates(qid, data_dir=data_dir)
        if pq and pq.is_file() and len(rounds) >= 2:
            candidates.append((len(rounds), qid))
    if not candidates:
        raise FileNotFoundError("No cached replays with Parquet + replay_cache found.")
    # Prefer 6–10 round sets (closest to 6 rounds)
    candidates.sort(key=lambda x: (abs(x[0] - 6), x[1]))
    return candidates[0][1]


def benchmark_single_round(
    quark_id: str,
    *,
    label: str = "manual",
    round_index: int = 0,
    export_overlay: bool = True,
    capture_audio: bool | None = None,
    video_encoder: str | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    save_record: bool = True,
) -> dict[str, Any]:
    """Benchmark offline rendering of a single cached round into a scratch temp directory."""
    data_root = data_dir.resolve()
    pq = find_parquet_for_quark(quark_id, data_dir=data_root)
    if pq is None or not pq.exists():
        raise FileNotFoundError(f"Parquet not found for {quark_id}")
    cached = list_cached_round_savestates(quark_id, data_dir=data_root)
    if not cached:
        raise FileNotFoundError(f"No cached rounds for {quark_id}")
    rid, snap_gf, fs_p = cached[min(round_index, len(cached) - 1)]
    df = pd.read_parquet(pq)
    df_r = df[df["round_id"] == rid]

    kwargs: dict[str, Any] = {
        "fbneo_exe": fbneo_exe,
        "export_overlay": export_overlay,
    }
    import inspect

    sig = inspect.signature(render_round_mp4)
    if capture_audio is not None and "capture_audio" in sig.parameters:
        kwargs["capture_audio"] = capture_audio
    if video_encoder is not None and "video_encoder" in sig.parameters:
        kwargs["video_encoder"] = video_encoder

    with tempfile.TemporaryDirectory(prefix="fclab_bench_r1_") as td:
        out_dir = Path(td)
        t0 = time.perf_counter()
        res = render_round_mp4(quark_id, rid, fs_p, snap_gf, df_r, out_dir, **kwargs)
        elapsed_s = round(time.perf_counter() - t0, 3)
        mp4_p = out_dir / str(res["filename"])
        probe = probe_video_streams(mp4_p) if mp4_p.is_file() else {"has_audio": False, "has_video": False}

    dur_s = float(res.get("duration_s") or 0.0)
    frames = int(res.get("frame_count") or 0)
    rec: dict[str, Any] = {
        "label": label,
        "mode": "single_round",
        "quark_id": quark_id,
        "round_id": rid,
        "round_count": 1,
        "frames": frames,
        "gameplay_s": round(dur_s, 2),
        "elapsed_s": elapsed_s,
        "speedup_x": round(dur_s / elapsed_s, 2) if elapsed_s > 0 else 0.0,
        "fps": round(frames / elapsed_s, 1) if elapsed_s > 0 else 0.0,
        "total_size_bytes": int(res.get("size_bytes") or 0),
        "has_audio": bool(probe.get("has_audio")),
        "export_overlay": bool(export_overlay),
        "encoder": str(res.get("encoder") or video_encoder or "libx264:fast"),
        "workers": 1,
        "batched": False,
        "stages": res.get("stages") or {},
    }
    if save_record:
        append_benchmark_record(rec, data_dir=data_root)
    return rec


def benchmark_full_set_offline(
    quark_id: str,
    *,
    label: str = "manual",
    workers: int = 2,
    export_overlay: bool = True,
    capture_audio: bool | None = None,
    video_encoder: str | None = None,
    batched: bool | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    save_record: bool = True,
) -> dict[str, Any]:
    """Benchmark full-set offline rendering into an isolated temp workspace (never touches real data/replays)."""
    data_root = data_dir.resolve()
    pq = find_parquet_for_quark(quark_id, data_dir=data_root)
    if pq is None or not pq.exists():
        raise FileNotFoundError(f"Parquet not found for {quark_id}")
    cached = list_cached_round_savestates(quark_id, data_dir=data_root)
    if not cached:
        raise FileNotFoundError(f"No cached rounds for {quark_id}")

    with tempfile.TemporaryDirectory(prefix="fclab_bench_set_") as td:
        tmp_data = Path(td)
        # Symlink / copy minimal pointers: personal parquet + replay_cache
        (tmp_data / "personal").mkdir(parents=True, exist_ok=True)
        (tmp_data / "replay_cache" / quark_id).mkdir(parents=True, exist_ok=True)
        os.link(pq, tmp_data / "personal" / pq.name)
        for _, _, fs_p in cached:
            os.link(fs_p, tmp_data / "replay_cache" / quark_id / fs_p.name)

        kwargs: dict[str, Any] = {
            "data_dir": tmp_data,
            "fbneo_exe": fbneo_exe,
            "max_workers": int(workers),
            "force": True,
            "export_overlay": export_overlay,
        }
        import inspect

        sig = inspect.signature(render_replay_rounds)
        if capture_audio is not None and "capture_audio" in sig.parameters:
            kwargs["capture_audio"] = capture_audio
        if video_encoder is not None and "video_encoder" in sig.parameters:
            kwargs["video_encoder"] = video_encoder
        if batched is not None and "batched" in sig.parameters:
            kwargs["batched"] = batched

        t0 = time.perf_counter()
        mf = render_replay_rounds(quark_id, **kwargs)
        elapsed_s = round(time.perf_counter() - t0, 3)

        rounds = mf.get("rounds") or []
        first_mp4 = tmp_data / "replays" / quark_id / str(rounds[0]["filename"]) if rounds else None
        probe = probe_video_streams(first_mp4) if (first_mp4 and first_mp4.is_file()) else {"has_audio": False}

    dur_s = float(mf.get("total_duration_s") or 0.0)
    frames = sum(int(r.get("frame_count") or 0) for r in rounds)
    rec: dict[str, Any] = {
        "label": label,
        "mode": "full_set_offline",
        "quark_id": quark_id,
        "round_count": len(rounds),
        "frames": frames,
        "gameplay_s": round(dur_s, 2),
        "elapsed_s": elapsed_s,
        "speedup_x": round(dur_s / elapsed_s, 2) if elapsed_s > 0 else 0.0,
        "fps": round(frames / elapsed_s, 1) if elapsed_s > 0 else 0.0,
        "total_size_bytes": int(mf.get("total_size_bytes") or 0),
        "has_audio": bool(probe.get("has_audio")),
        "export_overlay": bool(export_overlay),
        "encoder": str(mf.get("video_encoder") or video_encoder or "libx264:fast"),
        "workers": int(workers),
        "batched": bool(mf.get("batched_workers", batched or False)),
        "stages": mf.get("perf") or {},
    }
    if save_record:
        append_benchmark_record(rec, data_dir=data_root)
    return rec


def format_history_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No benchmark records yet."
    lines = [
        f"{'Timestamp':<19} {'Label':<24} {'Mode':<16} {'Rnds':>4} {'Game(s)':>7} {'Wall(s)':>7} {'Speed':>7} {'SizeMB':>6} {'Aud':>3} {'Enc':<16} {'W':>2}",
        "-" * 124,
    ]
    for r in rows:
        ts = str(r.get("timestamp") or "")[:19]
        lbl = str(r.get("label") or "")[:24]
        mode = str(r.get("mode") or "")[:16]
        rnds = int(r.get("round_count") or 0)
        gs = float(r.get("gameplay_s") or 0.0)
        ws = float(r.get("elapsed_s") or 0.0)
        sp = float(r.get("speedup_x") or 0.0)
        mb = float(r.get("total_size_bytes") or 0) / (1024 * 1024)
        aud = "Y" if r.get("has_audio") else "N"
        enc = str(r.get("encoder") or "")[:16]
        w = int(r.get("workers") or 1)
        lines.append(
            f"{ts:<19} {lbl:<24} {mode:<16} {rnds:>4} {gs:>7.1f} {ws:>7.2f} {sp:>6.1f}x {mb:>6.1f} {aud:>3} {enc:<16} {w:>2}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fightcade Lab performance benchmark harness.")
    parser.add_argument("--quark-id", default=None, help="Cached quark_id to benchmark")
    parser.add_argument("--label", default="manual", help="Benchmark run tag (e.g. baseline, after-opt)")
    parser.add_argument("--suite", choices=["quick", "single", "full", "compare"], default="quick")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--encoder", default=None)
    parser.add_argument("--no-audio", action="store_true")
    parser.add_argument("--no-overlay", action="store_true")
    parser.add_argument("--no-batch", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)

    if args.suite == "compare":
        print(format_history_table(load_benchmark_history(data_dir=args.data_dir, limit=40)))
        return 0

    qid = args.quark_id or pick_default_benchmark_quark(args.data_dir)
    print(f"[benchmark] Target: {qid} | Label: {args.label} | Suite: {args.suite}")
    results: list[dict[str, Any]] = []

    if args.suite in ("quick", "single", "full"):
        r1 = benchmark_single_round(
            qid,
            label=f"{args.label}:r1_ov",
            export_overlay=not args.no_overlay,
            capture_audio=False if args.no_audio else None,
            video_encoder=args.encoder,
            data_dir=args.data_dir,
        )
        results.append(r1)
        if args.suite in ("quick", "full") and not args.no_overlay:
            r1_no = benchmark_single_round(
                qid,
                label=f"{args.label}:r1_no_ov",
                export_overlay=False,
                capture_audio=False if args.no_audio else None,
                video_encoder=args.encoder,
                data_dir=args.data_dir,
            )
            results.append(r1_no)

    if args.suite in ("quick", "full"):
        fs = benchmark_full_set_offline(
            qid,
            label=f"{args.label}:set_w{args.workers}",
            workers=args.workers,
            export_overlay=not args.no_overlay,
            capture_audio=False if args.no_audio else None,
            video_encoder=args.encoder,
            batched=False if args.no_batch else None,
            data_dir=args.data_dir,
        )
        results.append(fs)

    print()
    print(format_history_table(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
