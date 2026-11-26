"""Unit & HTTP tests for Track 2 local share-bundle exporter (`share_export.py`) and unified hosting UI."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import re
import subprocess
import threading

import imageio_ffmpeg
import numpy as np
import pytest
import requests

from fightcade_analysis.server import create_server
from fightcade_analysis.share_export import (
    MAX_SHARE_CLIP_BYTES,
    THUMB_HEIGHT,
    THUMB_WIDTH,
    draw_hitboxes_on_rgb,
    prepare_share_bundle,
    slice_overlay_json,
)


def _create_synthetic_round_mp4(out_mp4: Path, *, duration_s: float = 3.0) -> None:
    """Create a real valid 60fps 768x448 H.264 MP4 via bundled imageio-ffmpeg."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=768x448:rate=60:duration={duration_s}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-pix_fmt",
        "yuv420p",
        str(out_mp4),
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert res.returncode == 0, res.stderr.decode(errors="replace")


def _probe_video_stream_info(mp4_path: Path) -> str:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    res = subprocess.run([ffmpeg, "-i", str(mp4_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return res.stderr.decode(errors="replace")


def test_draw_hitboxes_on_rgb_renders_visible_colored_boxes() -> None:
    base = np.zeros((THUMB_HEIGHT, THUMB_WIDTH, 3), dtype=np.uint8)
    boxes = [
        [0, 100, 80, 40, 60],  # blue hurtbox
        [1, 140, 90, 50, 20],  # red hitbox
    ]
    out = draw_hitboxes_on_rgb(base, boxes, src_w=384, src_h=224)
    assert out.shape == (THUMB_HEIGHT, THUMB_WIDTH, 3)
    # Border pixel of red hitbox at (x=140*2=280, y=round(90*576/224)=231) should be bright red
    sample_red = out[int(round(90 * THUMB_HEIGHT / 224)) + 1, 140 * 2 + 1]
    assert int(sample_red[0]) >= 200
    assert int(sample_red[1]) <= 90


def test_slice_overlay_json_preserves_exact_frame_window() -> None:
    ov = {
        "v": 1,
        "w": 384,
        "h": 224,
        "start_gf": 1000,
        "frame_count": 5,
        "frames": [[i, 0, [], [], []] for i in range(5)],
    }
    sliced = slice_overlay_json(ov, start_gf=1001, end_gf=1003)
    assert sliced["start_gf"] == 1001
    assert sliced["frame_count"] == 3
    assert [f[0] for f in sliced["frames"]] == [1, 2, 3]

    empty = slice_overlay_json(None, start_gf=500, end_gf=600)
    assert empty["start_gf"] == 500
    assert empty["frame_count"] == 0
    assert empty["frames"] == []


def test_prepare_share_bundle_builds_three_r2_artifacts_with_4_3_dar_and_hitboxes(tmp_path: Path) -> None:
    qid = "1795443936010-2584"
    rep_dir = tmp_path / "replays" / qid
    rep_dir.mkdir(parents=True)
    _create_synthetic_round_mp4(rep_dir / "r01.mp4", duration_s=3.0)

    # 180 frames of overlay starting at gf=1000 with visible attack & hurt boxes
    frames = [[16, 4, [160, 12, 96, 0, 2, 0, 72], [140, 8, 112, 0, 2, 10, 56], [[0, 110, 100, 36, 64], [1, 146, 110, 44, 18]]] for _ in range(180)]
    (rep_dir / "r01_overlay.json").write_text(
        json.dumps({"v": 1, "w": 384, "h": 224, "start_gf": 1000, "frame_count": 180, "frames": frames}),
        encoding="utf-8",
    )
    (rep_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 3,
                "quark_id": qid,
                "perspective": 2,
                "p1": {"side": 1, "name": "inertial97", "rank_letter": "B", "char_id": 13, "char_name": "Chun-Li", "chars": ["Chun-Li"]},
                "p2": {"side": 2, "name": "player1", "rank_letter": "D", "char_id": 14, "char_name": "Akuma", "chars": ["Akuma"]},
                "round_count": 1,
                "rounds": [
                    {
                        "round_id": 1,
                        "filename": "r01.mp4",
                        "video_url": f"/replays/{qid}/r01.mp4",
                        "overlay_url": f"/replays/{qid}/r01_overlay.json",
                        "start_gf": 1000,
                        "end_gf": 1179,
                        "frame_count": 180,
                        "fps": 60,
                        "p1_char": 13,
                        "p2_char": 14,
                        "p1_char_name": "Chun-Li",
                        "p2_char_name": "Akuma",
                        "winner": 2,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    res = prepare_share_bundle(
        qid,
        round_id=1,
        start_gf=1020,
        end_gf=1140,
        anchor_gf=1080,
        perspective=2,
        question="Should I b+HP here?",
        data_dir=tmp_path,
    )
    assert res["ok"] is True
    assert res["dar"] == "4:3"
    assert res["sar"] == "7:9"
    assert res["self_char"] == "Akuma"
    assert res["opp_char"] == "Chun-Li"
    assert res["has_hitboxes"] is True
    assert res["has_overlay"] is True

    # 1. Verify clip.mp4 magic bytes, <= 4 MB, and 4:3 DAR / 7:9 SAR stamped in stream!
    clip_p = Path(res["artifacts"]["clip"]["path"])
    assert clip_p.is_file()
    clip_bytes = clip_p.read_bytes()
    assert clip_bytes[4:8] == b"ftyp"
    assert 0 < len(clip_bytes) <= MAX_SHARE_CLIP_BYTES
    probe_txt = _probe_video_stream_info(clip_p)
    assert re.search(r"SAR\s+7:9\s+DAR\s+4:3", probe_txt), f"Expected SAR 7:9 DAR 4:3 in ffmpeg output:\n{probe_txt}"

    # 2. Verify overlay.json.gz magic bytes and exact sliced frame range
    ov_p = Path(res["artifacts"]["overlay"]["path"])
    assert ov_p.is_file()
    ov_raw = ov_p.read_bytes()
    assert ov_raw[:2] == b"\x1f\x8b"
    ov_unpacked = json.loads(gzip.decompress(ov_raw).decode("utf-8"))
    assert ov_unpacked["start_gf"] == 1020
    assert ov_unpacked["frame_count"] == 121

    # 3. Verify hitbox-rendered thumb.jpg magic bytes & 768x576 dimensions
    thumb_p = Path(res["artifacts"]["thumb"]["path"])
    assert thumb_p.is_file()
    thumb_raw = thumb_p.read_bytes()
    assert thumb_raw[:3] == b"\xff\xd8\xff"
    thumb_probe = _probe_video_stream_info(thumb_p)
    assert "768x576" in thumb_probe


def test_http_share_prepare_and_unified_frontend_hosting_markers(tmp_path: Path) -> None:
    qid = "1795443936010-2584"
    rep_dir = tmp_path / "replays" / qid
    rep_dir.mkdir(parents=True)
    _create_synthetic_round_mp4(rep_dir / "r01.mp4", duration_s=2.0)
    (rep_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 3,
                "quark_id": qid,
                "perspective": 1,
                "p1": {"side": 1, "name": "player1", "char_id": 14, "char_name": "Akuma", "chars": ["Akuma"]},
                "p2": {"side": 2, "name": "opp", "char_id": 4, "char_name": "Ken", "chars": ["Ken"]},
                "round_count": 1,
                "rounds": [
                    {
                        "round_id": 1,
                        "filename": "r01.mp4",
                        "video_url": f"/replays/{qid}/r01.mp4",
                        "start_gf": 500,
                        "end_gf": 619,
                        "frame_count": 120,
                        "fps": 60,
                        "p1_char": 14,
                        "p2_char": 4,
                        "p1_char_name": "Akuma",
                        "p2_char_name": "Ken",
                        "winner": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    srv = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        r_prep = requests.post(
            f"{base}/api/share/prepare",
            json={"quark_id": qid, "round_id": 1, "start_gf": 510, "end_gf": 590, "anchor_gf": 550, "perspective": 1},
            headers={"Origin": base},
            timeout=15,
        )
        assert r_prep.status_code == 200
        body = r_prep.json()
        assert body["ok"] is True

        # Artifacts are safely servable via HTTP (including 206 Range on clip.mp4)
        clip_url = body["artifacts"]["clip"]["url"]
        r_clip = requests.get(f"{base}{clip_url}", headers={"Range": "bytes=0-63"}, timeout=5)
        assert r_clip.status_code == 206
        assert len(r_clip.content) == 64

        thumb_url = body["artifacts"]["thumb"]["url"]
        r_thumb = requests.get(f"{base}{thumb_url}", timeout=5)
        assert r_thumb.status_code == 200
        assert r_thumb.content[:3] == b"\xff\xd8\xff"

        # Verify shared routes + unified frontend hosting & legal markers
        html_home = requests.get(f"{base}/", timeout=5).text
        assert "Feedback / Bug Report" in html_home
        assert "Source Code (AGPL-3.0)" in html_home
        assert "Terms &amp; DMCA" in html_home
        assert "/assets/js/shared.js" in html_home

        html_m = requests.get(f"{base}/m/demo_moment_1", timeout=5).text
        assert '"initialRoute": "m/demo_moment_1"' in html_m

        state_js = requests.get(f"{base}/assets/js/state.js", timeout=5).text
        assert "API_BASE" in state_js
        assert "COMPANION_BASE" in state_js

        shared_js = requests.get(f"{base}/assets/js/shared.js", timeout=5).text
        assert "openSharedMoment(" in shared_js
        assert "openSharedPlaylist(" in shared_js
        assert "seekSharedToGf(" in shared_js
        assert "Practice in Takeover" in shared_js
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)
