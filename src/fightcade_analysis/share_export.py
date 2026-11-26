"""Local Share-Bundle Exporter: builds the 3 immutable R2 share artifacts for any Moment.

Artifacts written to `data/share_exports/<bundle_id>/` (~0.6–2.1 MB total):
  1. `clip.mp4`        — 60fps H.264 clip (`<= 15s`, `<= 4 MB`) with exact `4:3` DAR / `7:9` SAR stamped
                         in both H.264 bitstream and MP4 container so Discord, QuickTime, and browsers
                         render proper 3rd Strike arcade proportions.
  2. `overlay.json.gz` — gzipped per-frame Grouflon hitbox/input/stick/gauge sidecar sliced strictly
                         to `[start_gf .. end_gf]`.
  3. `thumb.jpg`       — `768x576` (exact `4:3`) anchor frame with Grouflon hitboxes rendered onto
                         the JPEG pixels for Discord `summary_large_image` & playlist cards.
"""

from __future__ import annotations

from datetime import datetime
import gzip
import json
from pathlib import Path
import re
import subprocess
from typing import Any
import uuid

import imageio_ffmpeg
import numpy as np

from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR
from fightcade_analysis.perspective import normalize_side
from fightcade_analysis.round_renderer import ensure_manifest_matches, mobile_manifest_path, mobile_replay_dir

MAX_SHARE_CLIP_BYTES: int = 4 * 1024 * 1024  # Hard $0 R2 per-clip ceiling (4 MiB)
MAX_SHARE_DURATION_S: float = 15.0
MIN_SHARE_DURATION_S: float = 1.0
SHARE_CLIP_WIDTH: int = 768
SHARE_CLIP_HEIGHT: int = 448
SHARE_CLIP_SAR: str = "7/9"  # (768 * 7) / (448 * 9) == 4 / 3
SHARE_CLIP_DAR: str = "4:3"
THUMB_WIDTH: int = 768
THUMB_HEIGHT: int = 576  # Exact 4:3 arcade display aspect ratio

_QUARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")

# Matches `OV_BOX_STYLES` in `src/fightcade_analysis/web/js/overlays.js`
BOX_STYLES_RGB: dict[int, tuple[tuple[int, int, int], float]] = {
    0: ((59, 130, 246), 0.14),   # vulnerability (blue)
    1: ((239, 68, 68), 0.22),    # attack (red)
    2: ((34, 197, 94), 0.14),    # throwable (green)
    3: ((250, 204, 21), 0.22),   # throw (yellow)
    4: ((232, 121, 249), 0.12),  # push (magenta)
    5: ((34, 211, 238), 0.14),   # ext. vulnerability (cyan)
}


def share_exports_root(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "share_exports"


def slice_overlay_json(
    overlay_doc: dict[str, Any] | None,
    *,
    start_gf: int,
    end_gf: int,
) -> dict[str, Any]:
    """Slice a full-round `rNN_overlay.json` strictly to `[start_gf .. end_gf]`."""
    s_gf = int(start_gf)
    e_gf = max(s_gf, int(end_gf))
    if not overlay_doc or not isinstance(overlay_doc.get("frames"), list) or not overlay_doc["frames"]:
        return {
            "v": 1,
            "w": 384,
            "h": 224,
            "start_gf": s_gf,
            "frame_count": 0,
            "frames": [],
        }

    ov_start = int(overlay_doc.get("start_gf") or s_gf)
    raw_frames = overlay_doc["frames"]
    n_raw = len(raw_frames)
    i0 = max(0, min(n_raw - 1, s_gf - ov_start))
    i1 = max(i0, min(n_raw - 1, e_gf - ov_start))
    sliced = raw_frames[i0 : i1 + 1]
    return {
        "v": int(overlay_doc.get("v") or 1),
        "w": int(overlay_doc.get("w") or 384),
        "h": int(overlay_doc.get("h") or 224),
        "start_gf": ov_start + i0,
        "frame_count": len(sliced),
        "frames": sliced,
    }


def write_overlay_gzip(overlay_slice: dict[str, Any], out_path: Path) -> int:
    """Write compact gzip-compressed `overlay.json.gz` and return byte size."""
    raw = json.dumps(overlay_slice, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(compressed)
    return len(compressed)


def draw_hitboxes_on_rgb(
    frame_rgb: np.ndarray,
    boxes: list[list[int]] | None,
    *,
    src_w: int = 384,
    src_h: int = 224,
    border_px: int = 2,
) -> np.ndarray:
    """Alpha-blend Grouflon hitbox rectangles + crisp outlines onto an RGB24 numpy image."""
    img = np.array(frame_rgb, dtype=np.uint8, copy=True)
    if not boxes or img.ndim != 3 or img.shape[2] != 3:
        return img

    h, w, _ = img.shape
    sx = float(w) / float(max(1, src_w))
    sy = float(h) / float(max(1, src_h))
    bw_px = max(1, int(border_px))

    for box in boxes:
        if not isinstance(box, (list, tuple)) or len(box) < 5:
            continue
        btype = int(box[0])
        rgb, alpha = BOX_STYLES_RGB.get(btype, BOX_STYLES_RGB[0])
        x0 = max(0, min(w - 1, int(round(float(box[1]) * sx))))
        y0 = max(0, min(h - 1, int(round(float(box[2]) * sy))))
        x1 = max(x0 + 1, min(w, int(round((float(box[1]) + float(box[3])) * sx))))
        y1 = max(y0 + 1, min(h, int(round((float(box[2]) + float(box[4])) * sy))))
        if x1 <= x0 or y1 <= y0:
            continue

        color = np.array(rgb, dtype=np.float32)
        region = img[y0:y1, x0:x1].astype(np.float32)
        blended = region * (1.0 - alpha) + color * alpha
        img[y0:y1, x0:x1] = np.clip(blended, 0, 255).astype(np.uint8)

        # Crisp outline
        c_u8 = np.array(rgb, dtype=np.uint8)
        img[y0 : min(y1, y0 + bw_px), x0:x1] = c_u8
        img[max(y0, y1 - bw_px) : y1, x0:x1] = c_u8
        img[y0:y1, x0 : min(x1, x0 + bw_px)] = c_u8
        img[y0:y1, max(x0, x1 - bw_px) : x1] = c_u8

    return img


def build_share_clip_ffmpeg_cmd(
    src_mp4: Path,
    out_mp4: Path,
    *,
    start_s: float,
    duration_s: float,
    crf: int = 26,
    ffmpeg_exe: str | None = None,
) -> list[str]:
    """Build ffmpeg command that trims `[start_s, start_s+duration_s]` and stamps 4:3 DAR + 7:9 SAR."""
    eff_ffmpeg = ffmpeg_exe or imageio_ffmpeg.get_ffmpeg_exe()
    vf = (
        f"scale={SHARE_CLIP_WIDTH}:{SHARE_CLIP_HEIGHT}:flags=neighbor,"
        f"setsar={SHARE_CLIP_SAR},setdar=4/3"
    )
    return [
        eff_ffmpeg,
        "-y",
        "-ss",
        f"{max(0.0, float(start_s)):.4f}",
        "-i",
        str(src_mp4),
        "-t",
        f"{max(0.2, float(duration_s)):.4f}",
        "-an",
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        str(int(crf)),
        "-pix_fmt",
        "yuv420p",
        "-aspect",
        SHARE_CLIP_DAR,
        "-g",
        "30",
        "-keyint_min",
        "30",
        "-sc_threshold",
        "0",
        "-movflags",
        "+faststart",
        str(out_mp4),
    ]


def extract_share_clip_mp4(
    src_mp4: Path,
    out_mp4: Path,
    *,
    start_s: float,
    duration_s: float,
    crf: int = 26,
    max_bytes: int = MAX_SHARE_CLIP_BYTES,
    ffmpeg_exe: str | None = None,
) -> dict[str, Any]:
    """Encode trimmed share clip MP4 with 4:3 DAR/SAR, auto-adjusting CRF if needed to stay <= 4 MB."""
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    eff_ffmpeg = ffmpeg_exe or imageio_ffmpeg.get_ffmpeg_exe()
    crf_ladder = [int(crf)] + [c for c in (29, 32, 35) if c > int(crf)]

    last_size = 0
    used_crf = int(crf)
    for try_crf in crf_ladder:
        used_crf = try_crf
        cmd = build_share_clip_ffmpeg_cmd(
            src_mp4,
            out_mp4,
            start_s=start_s,
            duration_s=duration_s,
            crf=try_crf,
            ffmpeg_exe=eff_ffmpeg,
        )
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if res.returncode != 0 or not out_mp4.is_file():
            raise RuntimeError(f"ffmpeg clip trim failed (exit {res.returncode}): {res.stderr.decode(errors='replace')}")
        last_size = int(out_mp4.stat().st_size)
        if last_size <= max_bytes:
            break

    if last_size > max_bytes:
        raise ValueError(
            f"Exported clip ({last_size} bytes) exceeds hard {max_bytes}-byte limit; shorten the selected range."
        )

    return {
        "path": str(out_mp4),
        "size_bytes": last_size,
        "width": SHARE_CLIP_WIDTH,
        "height": SHARE_CLIP_HEIGHT,
        "dar": SHARE_CLIP_DAR,
        "sar": SHARE_CLIP_SAR.replace("/", ":"),
        "crf": used_crf,
    }


def render_hitbox_thumb_jpg(
    src_mp4: Path,
    out_jpg: Path,
    *,
    offset_s: float,
    boxes: list[list[int]] | None = None,
    width: int = THUMB_WIDTH,
    height: int = THUMB_HEIGHT,
    src_w: int = 384,
    src_h: int = 224,
    ffmpeg_exe: str | None = None,
) -> int:
    """Extract anchor frame at `offset_s`, burn Grouflon hitboxes onto RGB24 pixels, and encode `thumb.jpg`."""
    eff_ffmpeg = ffmpeg_exe or imageio_ffmpeg.get_ffmpeg_exe()
    out_jpg.parent.mkdir(parents=True, exist_ok=True)

    decode_cmd = [
        eff_ffmpeg,
        "-v",
        "error",
        "-ss",
        f"{max(0.0, float(offset_s)):.4f}",
        "-i",
        str(src_mp4),
        "-frames:v",
        "1",
        "-vf",
        f"scale={int(width)}:{int(height)}:flags=neighbor",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-",
    ]
    dec = subprocess.run(
        decode_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    expected_bytes = int(width) * int(height) * 3
    if dec.returncode != 0 or len(dec.stdout) < expected_bytes:
        raise RuntimeError(f"ffmpeg anchor frame decode failed: {dec.stderr.decode(errors='replace')}")

    frame = np.frombuffer(dec.stdout[:expected_bytes], dtype=np.uint8).reshape((int(height), int(width), 3))
    annotated = draw_hitboxes_on_rgb(frame, boxes, src_w=src_w, src_h=src_h)

    encode_cmd = [
        eff_ffmpeg,
        "-y",
        "-v",
        "error",
        "-f",
        "rawvideo",
        "-vcodec",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{int(width)}x{int(height)}",
        "-i",
        "-",
        "-frames:v",
        "1",
        "-q:v",
        "4",
        str(out_jpg),
    ]
    enc = subprocess.run(
        encode_cmd,
        input=annotated.tobytes(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if enc.returncode != 0 or not out_jpg.is_file():
        raise RuntimeError(f"ffmpeg JPEG encode failed: {enc.stderr.decode(errors='replace')}")
    return int(out_jpg.stat().st_size)


def prepare_share_bundle(
    quark_id: str,
    round_id: int,
    *,
    start_gf: int | None = None,
    end_gf: int | None = None,
    anchor_gf: int | None = None,
    perspective: int = 1,
    title: str = "",
    question: str = "",
    note: str = "",
    tags: list[str] | None = None,
    kind: str = "note",
    data_dir: Path = DEFAULT_DATA_DIR,
    crf: int = 26,
    ffmpeg_exe: str | None = None,
) -> dict[str, Any]:
    """Build the 3 immutable share artifacts (`clip.mp4`, `overlay.json.gz`, `thumb.jpg`) for a Moment."""
    qid = str(quark_id or "").strip()
    if not _QUARK_ID_RE.match(qid):
        raise ValueError(f"Invalid quark_id: {quark_id!r}")
    rid = int(round_id)
    if rid < 1:
        raise ValueError("round_id must be a positive integer")
    persp = normalize_side(perspective)

    data_root = data_dir.resolve()
    mf_path = mobile_manifest_path(qid, data_dir=data_root)
    if not mf_path.is_file():
        raise FileNotFoundError(f"Replay manifest not found for {qid} — process the match locally first.")

    raw_mf = json.loads(mf_path.read_text(encoding="utf-8"))
    mf = ensure_manifest_matches(raw_mf, viewer_side=persp, data_dir=data_root)
    round_doc = next((r for r in (mf.get("rounds") or []) if int(r.get("round_id", 0)) == rid), None)
    if round_doc is None:
        raise FileNotFoundError(f"Round {rid} not found in manifest for {qid}")

    rep_dir = mobile_replay_dir(qid, data_dir=data_root)
    mp4_name = str(round_doc.get("filename") or f"r{rid:02d}.mp4")
    src_mp4 = rep_dir / mp4_name
    if not src_mp4.is_file():
        raise FileNotFoundError(f"Round MP4 not found: {src_mp4}")

    r_start = int(round_doc.get("start_gf") or 0)
    r_end = int(round_doc.get("end_gf") or r_start)
    frame_count = max(1, int(round_doc.get("frame_count") or (r_end - r_start + 1)))
    fps = float(round_doc.get("fps") or 60.0)
    if fps <= 0:
        fps = 60.0
    max_span_frames = int(round(MAX_SHARE_DURATION_S * fps))
    min_span_frames = int(round(MIN_SHARE_DURATION_S * fps))

    eff_anchor = int(anchor_gf) if anchor_gf is not None else (int(start_gf) if start_gf is not None else (r_start + frame_count // 2))
    eff_anchor = max(r_start, min(r_end, eff_anchor))
    eff_start = int(start_gf) if start_gf is not None else max(r_start, eff_anchor - int(round(3.0 * fps)))
    eff_end = int(end_gf) if end_gf is not None else min(r_end, eff_anchor + int(round(2.5 * fps)))

    eff_start = max(r_start, min(r_end, eff_start))
    eff_end = max(eff_start + 1, min(r_end, eff_end))

    if (eff_end - eff_start) > max_span_frames:
        eff_end = eff_start + max_span_frames
    if (eff_end - eff_start) < min_span_frames and frame_count > min_span_frames:
        eff_end = min(r_end, eff_start + min_span_frames)
        if (eff_end - eff_start) < min_span_frames:
            eff_start = max(r_start, eff_end - min_span_frames)
    eff_anchor = max(eff_start, min(eff_end, eff_anchor))

    start_off = max(0, eff_start - r_start)
    end_off = max(start_off + 1, eff_end - r_start)
    anchor_off = max(start_off, min(end_off, eff_anchor - r_start))
    start_s = start_off / fps
    duration_s = max(0.2, (end_off - start_off + 1) / fps)
    anchor_rel_s = max(0.0, (anchor_off - start_off) / fps)

    short_qid = qid.split("-")[-1] if "-" in qid else qid[-6:]
    bundle_id = f"sh_{short_qid}_r{rid:02d}_gf{eff_start}_{uuid.uuid4().hex[:6]}"
    bundle_dir = share_exports_root(data_root) / bundle_id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # 1. clip.mp4 with 4:3 DAR / 7:9 SAR metadata
    clip_path = bundle_dir / "clip.mp4"
    clip_info = extract_share_clip_mp4(
        src_mp4,
        clip_path,
        start_s=start_s,
        duration_s=duration_s,
        crf=crf,
        ffmpeg_exe=ffmpeg_exe,
    )

    # 2. overlay.json.gz sliced strictly to [eff_start .. eff_end]
    ov_path = rep_dir / f"r{rid:02d}_overlay.json"
    ov_doc: dict[str, Any] | None = None
    if ov_path.is_file():
        try:
            ov_doc = json.loads(ov_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            ov_doc = None
    ov_slice = slice_overlay_json(ov_doc, start_gf=eff_start, end_gf=eff_end)
    overlay_gz_path = bundle_dir / "overlay.json.gz"
    overlay_bytes = write_overlay_gzip(ov_slice, overlay_gz_path)

    # Pick anchor frame's hitboxes for hitbox-rendered thumb.jpg
    anchor_boxes: list[list[int]] = []
    if ov_slice["frames"]:
        box_idx = max(0, min(len(ov_slice["frames"]) - 1, eff_anchor - int(ov_slice["start_gf"])))
        frame_entry = ov_slice["frames"][box_idx]
        if isinstance(frame_entry, list) and len(frame_entry) >= 5 and isinstance(frame_entry[4], list):
            anchor_boxes = frame_entry[4]

    # 3. thumb.jpg (768x576 exact 4:3 with hitboxes baked onto pixels)
    thumb_path = bundle_dir / "thumb.jpg"
    thumb_bytes = render_hitbox_thumb_jpg(
        clip_path,
        thumb_path,
        offset_s=anchor_rel_s,
        boxes=anchor_boxes,
        src_w=int(ov_slice.get("w") or 384),
        src_h=int(ov_slice.get("h") or 224),
        ffmpeg_exe=ffmpeg_exe,
    )

    total_bytes = int(clip_info["size_bytes"]) + int(overlay_bytes) + int(thumb_bytes)
    bundle_meta: dict[str, Any] = {
        "ok": True,
        "bundle_id": bundle_id,
        "quark_id": qid,
        "game_id": "sfiii3nr1",
        "round_id": rid,
        "perspective": persp,
        "start_gf": eff_start,
        "end_gf": eff_end,
        "anchor_gf": eff_anchor,
        "duration_s": round(duration_s, 2),
        "width": clip_info["width"],
        "height": clip_info["height"],
        "dar": clip_info["dar"],
        "sar": clip_info["sar"],
        "p1_name": str((mf.get("p1") or {}).get("name") or ""),
        "p2_name": str((mf.get("p2") or {}).get("name") or ""),
        "p1_char": str(round_doc.get("p1_char_name") or (mf.get("p1") or {}).get("char_name") or ""),
        "p2_char": str(round_doc.get("p2_char_name") or (mf.get("p2") or {}).get("char_name") or ""),
        "self_char": str(round_doc.get("self_char_name") or mf.get("self_char_name") or ""),
        "opp_char": str(round_doc.get("opp_char_name") or mf.get("opp_char_name") or ""),
        "title": str(title or "").strip()[:160],
        "question": str(question or "").strip()[:200],
        "note": str(note or "").strip()[:2000],
        "tags": list(tags or []),
        "kind": str(kind or "note"),
        "has_hitboxes": len(anchor_boxes) > 0,
        "has_overlay": len(ov_slice["frames"]) > 0,
        "total_bytes": total_bytes,
        "artifacts": {
            "clip": {
                "filename": "clip.mp4",
                "path": str(clip_path),
                "url": f"/share_exports/{bundle_id}/clip.mp4",
                "content_type": "video/mp4",
                "size_bytes": int(clip_info["size_bytes"]),
            },
            "overlay": {
                "filename": "overlay.json.gz",
                "path": str(overlay_gz_path),
                "url": f"/share_exports/{bundle_id}/overlay.json.gz",
                "content_type": "application/gzip",
                "size_bytes": int(overlay_bytes),
            },
            "thumb": {
                "filename": "thumb.jpg",
                "path": str(thumb_path),
                "url": f"/share_exports/{bundle_id}/thumb.jpg",
                "content_type": "image/jpeg",
                "size_bytes": int(thumb_bytes),
                "width": THUMB_WIDTH,
                "height": THUMB_HEIGHT,
            },
        },
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    (bundle_dir / "bundle.json").write_text(json.dumps(bundle_meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return bundle_meta
