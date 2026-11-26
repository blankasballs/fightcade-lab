"""Unit tests for Section 6 Pipeline Optimizations:
- Opt 1: Native 384x224 pixel-art encoding + CSS `image-rendering: pixelated`
- Opt 2: Invisible `-headless` launch flag + direct C++ RGB24 pipe encoding
- Opt 3: Single-pass ingestion + video + overlay capture (`ProcessingQueue` skipping Pass 2)
"""

from __future__ import annotations

import json
from pathlib import Path
import time
from typing import Any

import numpy as np
import pandas as pd
import pytest

from fightcade_analysis.clip_renderer import build_ffmpeg_rgb24_cmd, encode_rgb24_to_mp4
from fightcade_analysis.match_catalog import ProcessingQueue
from fightcade_analysis.round_renderer import (
    DEFAULT_MOBILE_CRF,
    DEFAULT_MOBILE_SCALE,
    finalize_single_pass_mobile_rounds,
    mobile_manifest_path,
    mobile_replay_dir,
)


def _make_round_df(round_id: int = 1, start_gf: int = 1000, n: int = 60) -> pd.DataFrame:
    rows = []
    for i in range(n):
        gf = start_gf + i
        rows.append(
            {
                "emu_frame": gf + 100,
                "game_frame": gf,
                "round_id": round_id,
                "in_match": 1,
                "match_state": 2,
                "timer": 99,
                "screen_x": 0,
                "p1_char": 14,
                "p1_sa": 2,
                "p1_x": 120,
                "p1_y": 0,
                "p1_facing": 1,
                "p1_hp": 160 - (20 if i >= 30 else 0),
                "p1_meter_gauge": 0,
                "p1_meter_max_gauge": 64,
                "p1_meter_stock": 0,
                "p1_meter_max_stock": 1,
                "p1_stun": 0,
                "p1_stun_max": 64,
                "p1_action": 0,
                "p1_anim": 0,
                "p1_posture": 0,
                "p1_attacking": 0,
                "p1_blocking": 0,
                "p1_combo": 0,
                "p1_freeze": 0,
                "p1_superfreeze": 0,
                "p1_proj_count": 0,
                "p1_input_mask": 0,
                "p2_char": 1,
                "p2_sa": 3,
                "p2_x": 260,
                "p2_y": 0,
                "p2_facing": 0,
                "p2_hp": 160,
                "p2_meter_gauge": 0,
                "p2_meter_max_gauge": 64,
                "p2_meter_stock": 0,
                "p2_meter_max_stock": 2,
                "p2_stun": 0,
                "p2_stun_max": 64,
                "p2_action": 0,
                "p2_anim": 0,
                "p2_posture": 0,
                "p2_attacking": 0,
                "p2_blocking": 0,
                "p2_combo": 0,
                "p2_freeze": 0,
                "p2_superfreeze": 0,
                "p2_proj_count": 0,
                "p2_input_mask": 0,
            }
        )
    return pd.DataFrame(rows)


def test_opt1_default_768x448_encoding_and_ffmpeg_cmd(tmp_path: Path) -> None:
    assert DEFAULT_MOBILE_SCALE == 2
    assert DEFAULT_MOBILE_CRF == 26

    out_mp4 = tmp_path / "r01.mp4"
    cmd2, w2, h2 = build_ffmpeg_rgb24_cmd(out_mp4, width=384, height=224, fps=60, scale=2, crf=26, gop_size=30, ffmpeg_exe="ffmpeg")
    assert (w2, h2) == (768, 448)
    assert "-vf" in cmd2
    assert "scale=768:448:flags=neighbor" in cmd2
    assert "-aspect" in cmd2 and "4:3" in cmd2
    assert "-g" in cmd2 and "30" in cmd2

    cmd1, w1, h1 = build_ffmpeg_rgb24_cmd(out_mp4, width=384, height=224, fps=60, scale=1, crf=23, gop_size=None, ffmpeg_exe="ffmpeg")
    assert (w1, h1) == (384, 224)
    assert "-vf" not in cmd1


def test_opt1_css_no_pixelated_video_override() -> None:
    css_path = Path(__file__).resolve().parents[1] / "src" / "fightcade_analysis" / "web" / "styles.css"
    css = css_path.read_text(encoding="utf-8")
    assert "image-rendering: pixelated" not in css


def test_opt2_encode_rgb24_to_mp4_768x448(tmp_path: Path) -> None:
    rgb_path = tmp_path / "sample.rgb24"
    mp4_path = tmp_path / "sample.mp4"
    # 6 synthetic frames of 384x224 RGB24
    frame = np.zeros((224, 384, 3), dtype=np.uint8)
    frame[:, :, 0] = 40
    frame[40:180, 80:300, 1] = 200
    frame[90:140, 160:220, 2] = 240
    rgb_path.write_bytes(frame.tobytes() * 6)

    n_frames, out_w, out_h = encode_rgb24_to_mp4(rgb_path, mp4_path, width=384, height=224, fps=60, scale=2, crf=26, gop_size=30)
    assert n_frames == 6
    assert (out_w, out_h) == (768, 448)
    assert mp4_path.exists() and mp4_path.stat().st_size > 1024


def test_opt3_finalize_single_pass_mobile_rounds(tmp_path: Path) -> None:
    qid = "1799999999000-1234"
    data_dir = tmp_path / "data"
    (data_dir / "personal").mkdir(parents=True)
    df = _make_round_df(round_id=1, start_gf=1000, n=60)
    df.to_parquet(data_dir / "personal" / f"{qid}.parquet", index=False)

    rc_dir = data_dir / "replay_cache" / qid
    rc_dir.mkdir(parents=True)
    (rc_dir / "r01_gf1000.fs").write_bytes(b"0" * 150_000)

    out_dir = mobile_replay_dir(qid, data_dir=data_dir)
    out_dir.mkdir(parents=True)
    (out_dir / "r01.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"A" * 8192)
    (out_dir / "r01.mp4.gf").write_text("1000,1059,60\n", encoding="utf-8")
    (out_dir / "_r01.overlay.tsv").write_text(
        "1000\t0\t0\t0\t0\t0,10,20,30,40\n1001\t16\t0\t0\t0\t1,12,22,32,42\n",
        encoding="utf-8",
    )

    mf = finalize_single_pass_mobile_rounds(qid, data_dir=data_dir, perspective=1, scale=1, elapsed_s=12.5)
    assert mf is not None
    assert mf["round_count"] == 1
    assert mf["rounds"][0]["width"] == 384
    assert mf["rounds"][0]["height"] == 224
    assert mf["rounds"][0]["frame_count"] == 60
    assert mf["rounds"][0]["overlay_url"] == f"/replays/{qid}/r01_overlay.json"
    assert (out_dir / "r01_overlay.json").is_file()
    assert mobile_manifest_path(qid, data_dir=data_dir).is_file()
    # Temp sidecars cleaned up
    assert not (out_dir / "r01.mp4.gf").exists()
    assert not (out_dir / "_r01.overlay.tsv").exists()


def test_opt3_processing_queue_skips_pass2_when_single_pass_rendered(tmp_path: Path) -> None:
    qid = "1799999999001-5678"
    data_dir = tmp_path / "data"
    scan_calls: list[dict[str, Any]] = []
    render_calls: list[str] = []

    def fake_scan(
        quark_id: str,
        *,
        perspective: int = 1,
        stream_target: str | None = None,
        render_mobile_video: bool = False,
        progress_cb: Any = None,
        data_dir: Path = data_dir,
    ) -> dict[str, Any]:
        scan_calls.append({"quark_id": quark_id, "render_mobile_video": render_mobile_video, "has_progress_cb": progress_cb is not None})
        if progress_cb is not None:
            progress_cb(1, 0, "Processing R1...")
        (data_dir / "personal").mkdir(parents=True, exist_ok=True)
        _make_round_df(1, 1000, 60).to_parquet(data_dir / "personal" / f"{quark_id}.parquet", index=False)
        rc_dir = data_dir / "replay_cache" / quark_id
        rc_dir.mkdir(parents=True, exist_ok=True)
        (rc_dir / "r01_gf1000.fs").write_bytes(b"0" * 150_000)
        if render_mobile_video:
            out_dir = mobile_replay_dir(quark_id, data_dir=data_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "r01.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"B" * 8192)
            (out_dir / "r01.mp4.gf").write_text("1000,1059,60\n", encoding="utf-8")
            finalize_single_pass_mobile_rounds(quark_id, data_dir=data_dir, perspective=perspective, scale=1, elapsed_s=5.0)
        return {"ok": True, "mobile_video_rendered": render_mobile_video}

    def fake_render(quark_id: str, **kwargs: Any) -> dict[str, Any]:
        render_calls.append(quark_id)
        return {"round_count": 1}

    q = ProcessingQueue(
        data_dir=data_dir,
        scan_fn=fake_scan,
        render_fn=fake_render,
        stream_cooldown_s=0.0,
    )
    try:
        q.enqueue(qid, perspective=1)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            job = q.jobs_by_quark().get(qid)
            if job and job["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        job = q.jobs_by_quark()[qid]
        assert job["state"] == "done", job
        assert job["message"] == "Ready (1 rounds)"
        assert len(scan_calls) == 1 and scan_calls[0]["render_mobile_video"] is True and scan_calls[0]["has_progress_cb"] is True
        assert render_calls == []
    finally:
        q.stop()


def test_opt3_processing_queue_falls_back_to_pass2_when_force_or_cached(tmp_path: Path) -> None:
    qid = "1799999999002-9999"
    data_dir = tmp_path / "data"
    (data_dir / "personal").mkdir(parents=True, exist_ok=True)
    _make_round_df(1, 1000, 60).to_parquet(data_dir / "personal" / f"{qid}.parquet", index=False)
    rc_dir = data_dir / "replay_cache" / qid
    rc_dir.mkdir(parents=True, exist_ok=True)
    (rc_dir / "r01_gf1000.fs").write_bytes(b"0" * 150_000)

    render_calls: list[str] = []

    def legacy_scan(quark_id: str, *, perspective: int = 1, stream_target: str | None = None, data_dir: Path = data_dir) -> dict[str, Any]:
        return {"ok": True}

    def fake_render(quark_id: str, **kwargs: Any) -> dict[str, Any]:
        render_calls.append(quark_id)
        return {"round_count": 1}

    q = ProcessingQueue(
        data_dir=data_dir,
        scan_fn=legacy_scan,
        render_fn=fake_render,
        stream_cooldown_s=0.0,
    )
    try:
        q.enqueue(qid, perspective=1)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            job = q.jobs_by_quark().get(qid)
            if job and job["state"] in ("done", "error"):
                break
            time.sleep(0.05)
        job = q.jobs_by_quark()[qid]
        assert job["state"] == "done", job
        assert render_calls == [qid]
    finally:
        q.stop()
