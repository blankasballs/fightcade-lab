"""Unit tests for silent RAM audio muxing, encoder resolution, benchmark history, and non-blocking stream cooldown."""

from __future__ import annotations

import math
from pathlib import Path
import struct
import subprocess
import time
from typing import Any

import imageio_ffmpeg
import pandas as pd

from fightcade_analysis.benchmark import (
    append_benchmark_record,
    format_history_table,
    load_benchmark_history,
    probe_video_streams,
)
from fightcade_analysis.match_catalog import ProcessingQueue, list_matches
from fightcade_analysis.round_renderer import (
    finalize_single_pass_mobile_rounds,
    mobile_replay_dir,
    mux_video_and_pcm_to_mp4,
    publish_progressive_rounds,
    resolve_video_encoder,
)
from fightcade_analysis.share_export import build_share_clip_ffmpeg_cmd


def _make_synthetic_video_mp4(out_mp4: Path, *, duration_s: float = 1.0) -> None:
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    res = subprocess.run(
        [
            ff,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=navy:s=768x448:r=60:d={duration_s}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(out_mp4),
        ],
        capture_output=True,
    )
    assert res.returncode == 0, res.stderr.decode(errors="replace")


def _make_synthetic_pcm(out_pcm: Path, *, frames: int = 60, spf: int = 740) -> None:
    # 440 Hz stereo s16le sine wave
    samples = []
    rate = spf * 60
    for i in range(frames * spf):
        val = int(12000 * math.sin(2.0 * math.pi * 440.0 * i / rate))
        samples.append(struct.pack("<hh", val, val))
    out_pcm.write_bytes(b"".join(samples))


def test_mux_video_and_pcm_to_mp4_produces_synced_aac_audio(tmp_path: Path) -> None:
    vid = tmp_path / "r01_vid.mp4"
    pcm = tmp_path / "r01.pcm"
    out = tmp_path / "r01.mp4"
    _make_synthetic_video_mp4(vid, duration_s=1.0)
    _make_synthetic_pcm(pcm, frames=60, spf=740)

    mux_s = mux_video_and_pcm_to_mp4(vid, pcm, out, samples_per_frame=740, fps=60)
    assert mux_s >= 0.0
    assert out.is_file() and out.stat().st_size > 2048

    info = probe_video_streams(out)
    assert info["has_video"] is True
    assert info["has_audio"] is True
    assert "aac" in info["audio_line"].lower()


def test_finalize_single_pass_muxes_vid_and_pcm_and_cleans_temps(tmp_path: Path) -> None:
    qid = "1799999999111-2222"
    data_dir = tmp_path / "data"
    (data_dir / "personal").mkdir(parents=True)
    rows = [
        {
            "emu_frame": 100 + i,
            "game_frame": 1000 + i,
            "round_id": 1,
            "in_match": 1,
            "match_state": 2,
            "timer": 99,
            "screen_x": 0,
            "p1_char": 14,
            "p2_char": 1,
            "p1_hp": 160,
            "p2_hp": 140,
            "p1_input_mask": 0,
            "p2_input_mask": 0,
        }
        for i in range(60)
    ]
    pd.DataFrame(rows).to_parquet(data_dir / "personal" / f"{qid}.parquet", index=False)

    out_dir = mobile_replay_dir(qid, data_dir=data_dir)
    out_dir.mkdir(parents=True)
    _make_synthetic_video_mp4(out_dir / "_r01_vid.mp4", duration_s=1.0)
    _make_synthetic_pcm(out_dir / "_r01.pcm", frames=60, spf=740)
    (out_dir / "r01.mp4.gf").write_text("1000,1059,60,44100,740\n", encoding="utf-8")

    mf = finalize_single_pass_mobile_rounds(qid, data_dir=data_dir, perspective=1, scale=2, elapsed_s=3.5)
    assert mf is not None
    assert mf["round_count"] == 1
    assert mf["has_audio"] is True
    assert mf["rounds"][0]["has_audio"] is True
    assert (out_dir / "r01.mp4").is_file()
    assert not (out_dir / "_r01_vid.mp4").exists()
    assert not (out_dir / "_r01.pcm").exists()
    assert not (out_dir / "r01.mp4.gf").exists()

    info = probe_video_streams(out_dir / "r01.mp4")
    assert info["has_audio"] is True


def test_encoder_resolution_and_share_clip_preserves_audio(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.delenv("FC_VIDEO_ENCODER", raising=False)
    assert resolve_video_encoder("auto") == "libx264:veryfast"
    assert resolve_video_encoder("h264_nvenc") == "h264_nvenc"
    assert resolve_video_encoder("libx264") == "libx264:fast"

    cmd = build_share_clip_ffmpeg_cmd(
        tmp_path / "r01.mp4",
        tmp_path / "clip.mp4",
        start_s=1.0,
        duration_s=5.0,
        ffmpeg_exe="ffmpeg",
    )
    assert "-an" not in cmd
    assert "-c:a" in cmd and "aac" in cmd


def test_benchmark_history_append_load_and_format(tmp_path: Path) -> None:
    append_benchmark_record(
        {
            "label": "baseline",
            "mode": "full_set_offline",
            "quark_id": "1795654905316-7421",
            "round_count": 6,
            "gameplay_s": 220.4,
            "elapsed_s": 23.3,
            "speedup_x": 9.5,
            "total_size_bytes": 60_000_000,
            "has_audio": False,
            "encoder": "libx264:fast",
            "workers": 2,
        },
        data_dir=tmp_path,
    )
    rows = load_benchmark_history(data_dir=tmp_path)
    assert len(rows) == 1
    assert rows[0]["label"] == "baseline"
    tbl = format_history_table(rows)
    assert "baseline" in tbl and "9.5x" in tbl


def test_processing_queue_does_not_sleep_after_single_stream_job(tmp_path: Path) -> None:
    # Even with a 30s stream cooldown configured, a SINGLE match must finish immediately without waiting 30s!
    t_clock = [1000.0]

    def fake_scan(qid: str, **kw: Any) -> dict[str, Any]:
        return {"ok": True, "quark_id": qid}

    def fake_render(qid: str, **kw: Any) -> dict[str, Any]:
        return {"round_count": 1}

    q = ProcessingQueue(
        data_dir=tmp_path,
        scan_fn=fake_scan,
        render_fn=fake_render,
        stream_cooldown_s=30.0,
        clock_fn=lambda: t_clock[0],
    )
    t0 = time.perf_counter()
    try:
        q.enqueue("1795500000099-1111")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            job = q.jobs_by_quark().get("1795500000099-1111")
            if job and job["state"] == "done":
                break
            time.sleep(0.02)
        elapsed = time.perf_counter() - t0
        assert q.jobs_by_quark()["1795500000099-1111"]["state"] == "done"
        assert elapsed < 2.0, f"Single job stalled for {elapsed:.2f}s waiting on post-stream cooldown!"
    finally:
        q.stop()


def test_finalize_single_pass_inline_dual_pipe_av(tmp_path: Path) -> None:
    qid = "1799999999333-4444"
    data_dir = tmp_path / "data"
    (data_dir / "personal").mkdir(parents=True)
    rows = [
        {
            "emu_frame": 100 + i,
            "game_frame": 2000 + i,
            "round_id": 1,
            "in_match": 1,
            "match_state": 2,
            "timer": 99,
            "screen_x": 0,
            "p1_char": 14,
            "p2_char": 1,
            "p1_hp": 160,
            "p2_hp": 140,
            "p1_input_mask": 0,
            "p2_input_mask": 0,
        }
        for i in range(60)
    ]
    pd.DataFrame(rows).to_parquet(data_dir / "personal" / f"{qid}.parquet", index=False)

    out_dir = mobile_replay_dir(qid, data_dir=data_dir)
    out_dir.mkdir(parents=True)
    _make_synthetic_video_mp4(out_dir / "r01.mp4", duration_s=1.0)
    # 6-field .gf with muxed_inline=1 -> zero post-round remux
    (out_dir / "r01.mp4.gf").write_text("2000,2059,60,44100,740,1\n", encoding="utf-8")

    mf = finalize_single_pass_mobile_rounds(qid, data_dir=data_dir, perspective=1, scale=2, elapsed_s=2.1)
    assert mf is not None
    assert mf["in_progress"] is False
    assert mf["has_audio"] is True
    assert mf["rounds"][0]["has_audio"] is True
    assert mf["rounds"][0]["render_s"] == 0.0
    assert not (out_dir / "r01.mp4.gf").exists()


def test_publish_progressive_rounds_and_catalog_in_progress(tmp_path: Path) -> None:
    qid = "1799999999555-6666"
    data_dir = tmp_path / "data"
    out_dir = mobile_replay_dir(qid, data_dir=data_dir)
    out_dir.mkdir(parents=True)

    # Flushed live CSV during stream
    csv_path = tmp_path / "live_stream.csv"
    rows = [
        {
            "game_frame": 3000 + i,
            "round_id": 1,
            "p1_char": 14,
            "p2_char": 1,
            "p1_hp": 160,
            "p2_hp": 160 - (i // 3),
            "p1_superfreeze": 0,
            "p2_superfreeze": 0,
            "p1_posture": 0,
            "p2_posture": 0,
        }
        for i in range(60)
    ]
    pd.DataFrame(rows).to_csv(csv_path, index=False)

    _make_synthetic_video_mp4(out_dir / "r01.mp4", duration_s=1.0)
    (out_dir / "r01.mp4.gf").write_text("3000,3059,60,44100,740,1\n", encoding="utf-8")
    (out_dir / "_r01.overlay.tsv").write_text(
        "3000\t0\t0\t160,160,0,0,1,0,64\t160,160,0,0,1,0,64\t4,80,120,40,60\n",
        encoding="utf-8",
    )

    published: dict[int, dict[str, Any]] = {}
    cnt = publish_progressive_rounds(
        qid,
        csv_path,
        published_rounds=published,
        data_dir=data_dir,
        perspective=1,
        scale=2,
        video_encoder="libx264:veryfast",
    )
    assert cnt == 1
    assert (out_dir / "manifest.json").is_file()
    assert (out_dir / "r01_overlay.json").is_file()

    (data_dir / "config.json").write_text('{"username": "tester"}', encoding="utf-8")
    cat = list_matches(username="tester", data_dir=data_dir)
    match_row = next((m for m in cat["matches"] if m["quark_id"] == qid), None)
    assert match_row is not None
    assert match_row["in_progress"] is True
    assert match_row["mobile_round_count"] == 1

    # Finalizing stream flips in_progress -> False and preserves overlay_url
    (data_dir / "personal").mkdir(parents=True)
    pd.DataFrame(rows).to_parquet(data_dir / "personal" / f"{qid}.parquet", index=False)
    final_mf = finalize_single_pass_mobile_rounds(qid, data_dir=data_dir, perspective=1, scale=2)
    assert final_mf is not None
    assert final_mf["in_progress"] is False
    assert final_mf["rounds"][0]["overlay_url"] == f"/replays/{qid}/r01_overlay.json"


def test_processing_queue_does_not_skip_pass2_for_in_progress_manifest(tmp_path: Path) -> None:
    qid = "1799999999777-8888"
    data_dir = tmp_path / "data"
    (data_dir / "personal").mkdir(parents=True)
    (data_dir / "replay_cache" / qid).mkdir(parents=True)
    (data_dir / "personal" / f"{qid}.parquet").write_bytes(b"PAR1")
    (data_dir / "replay_cache" / qid / "r01_gf1000.fs").write_bytes(b"\x00" * 120_000)

    out_dir = mobile_replay_dir(qid, data_dir=data_dir)
    out_dir.mkdir(parents=True)
    (out_dir / "r01.mp4").write_bytes(b"\x00" * 2048)
    (out_dir / "manifest.json").write_text(
        '{"version":3,"quark_id":"1799999999777-8888","in_progress":true,"round_count":1,"rounds":[{"round_id":1,"filename":"r01.mp4"}]}',
        encoding="utf-8",
    )

    render_called = []

    def fake_render(q: str, **kw: Any) -> dict[str, Any]:
        render_called.append(q)
        return {"round_count": 1}

    q = ProcessingQueue(data_dir=data_dir, render_fn=fake_render)
    try:
        q.enqueue(qid)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            job = q.jobs_by_quark().get(qid)
            if job and job["state"] == "done":
                break
            time.sleep(0.02)
        assert render_called == [qid], "ProcessingQueue must run Pass 2 fallback when manifest has in_progress=True"
    finally:
        q.stop()
