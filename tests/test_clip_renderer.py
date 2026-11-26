"""Unit tests for GD truecolor frame parsing and ffmpeg MP4 encoding."""

from __future__ import annotations

from pathlib import Path
import struct

import numpy as np

from fightcade_analysis.clip_renderer import (
    encode_gdframes_to_mp4,
    export_takeover_inputs,
    gd_frame_to_rgb24,
    parse_gd_header,
)


def _make_gd_frame(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    header = struct.pack(">HHHBBBBB", 65534, width, height, 1, 255, 255, 255, 255)
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[:, :, 1] = rgb[0]
    pixels[:, :, 2] = rgb[1]
    pixels[:, :, 3] = rgb[2]
    return header + pixels.tobytes()


def test_gd_header_and_rgb24_decode() -> None:
    raw = _make_gd_frame(16, 8, (12, 34, 56))
    w, h = parse_gd_header(raw[:11])
    assert (w, h) == (16, 8)
    rgb = gd_frame_to_rgb24(raw, w, h)
    assert rgb.shape == (8, 16, 3)
    assert tuple(rgb[0, 0]) == (12, 34, 56)


def test_encode_gdframes_to_mp4(tmp_path: Path) -> None:
    raw_path = tmp_path / "sample.gdframes"
    mp4_path = tmp_path / "sample.mp4"
    with raw_path.open("wb") as f:
        for i in range(15):
            f.write(_make_gd_frame(32, 24, (i * 15, 80, 200 - i * 10)))

    n_frames, out_w, out_h = encode_gdframes_to_mp4(raw_path, mp4_path, fps=60, scale=2)
    assert n_frames == 15
    assert (out_w, out_h) == (64, 48)
    assert mp4_path.exists() and mp4_path.stat().st_size > 200


def test_export_takeover_inputs_from_parquet(tmp_path: Path) -> None:
    import pandas as pd

    pq_dir = tmp_path / "personal"
    pq_dir.mkdir()
    pq_path = pq_dir / "sample-qid.parquet"
    df = pd.DataFrame(
        {
            "game_frame": [100, 101, 102, 103, 104],
            "p1_input_mask": [0, 4, 16, 0, 8],
            "p2_input_mask": [2, 2, 64, 64, 0],
        }
    )
    df.to_parquet(pq_path, index=False)

    out_inp = tmp_path / "savestates" / "sample-qid_gf102.inputs"
    written = export_takeover_inputs(
        "sample-qid",
        102,
        out_inp,
        data_dir=tmp_path,
        pre_frames=1,
        post_frames=1,
    )
    assert written == out_inp
    lines = out_inp.read_text(encoding="utf-8").strip().splitlines()
    assert lines == [
        "game_frame,p1_mask,p2_mask",
        "101,4,2",
        "102,16,64",
        "103,0,64",
    ]
