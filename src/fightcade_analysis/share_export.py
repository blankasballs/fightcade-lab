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
import shutil
import subprocess
from typing import Any
import uuid

import imageio_ffmpeg
import numpy as np
import requests

from fightcade_analysis.annotations import get_annotation, update_annotation
from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR
from fightcade_analysis.companion_bridge import get_cloud_base_url, get_cloud_token
from fightcade_analysis.fetch_replays import DEFAULT_UA
from fightcade_analysis.naming import lookup_replay_metadata
from fightcade_analysis.perspective import normalize_side
from fightcade_analysis.round_renderer import ensure_manifest_matches, mobile_manifest_path, mobile_replay_dir
from fightcade_analysis.select_queue import load_queue, update_queue_item

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
        "-c:a",
        "aac",
        "-b:a",
        "96k",
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


# ---------------------------------------------------------------------------
# Cloud Publish, Update, Rotate & Revoke (Server-to-Server with Bearer Token)
# ---------------------------------------------------------------------------

_CLOUD_VALID_KINDS: frozenset[str] = frozenset({"mistake", "question", "good_play", "opp_habit", "note"})
_MOMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")


class CloudNotConnectedError(RuntimeError):
    """Raised when the local Companion is not paired with a cloud Bearer token."""

    code = "cloud_not_connected"


class CloudPublishError(RuntimeError):
    """Raised when the cloud backend rejects a publish, rotate, or revoke request."""

    def __init__(self, message: str, *, status_code: int = 502, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.code = code


def _map_cloud_kind(raw_kind: Any) -> str:
    k = str(raw_kind or "note").strip().lower()
    return k if k in _CLOUD_VALID_KINDS else "note"


def _resolve_contained_path(rel_or_url: str, data_root: Path) -> Path | None:
    raw = str(rel_or_url or "").strip()
    if not raw:
        return None
    rel = raw.lstrip("/")
    if not rel or ".." in Path(rel).parts:
        return None
    candidate = (data_root / Path(rel)).resolve()
    try:
        candidate.relative_to(data_root.resolve())
    except ValueError:
        return None
    return candidate


def _lookup_local_moment(
    moment_id: str,
    *,
    source_hint: str = "",
    data_root: Path,
) -> tuple[dict[str, Any] | None, str]:
    mid = str(moment_id or "").strip()
    src = str(source_hint or "").strip().lower()
    if not mid:
        return None, src or "annotation"

    if src == "select_mark":
        for qi in load_queue(data_root, include_dismissed=True):
            if str(qi.get("id") or "") == mid:
                return qi, "select_mark"

    ann = get_annotation(mid, data_dir=data_root)
    if ann is not None:
        is_resp = str(ann.get("source") or "").lower() == "takeover_response" or bool(ann.get("response_clip_url"))
        return ann, ("takeover_response" if is_resp else "annotation")

    for qi in load_queue(data_root, include_dismissed=True):
        if str(qi.get("id") or "") == mid:
            return qi, "select_mark"

    return None, src or "annotation"


def _persist_local_cloud_meta(
    moment_id: str,
    resolved_source: str,
    patch: dict[str, Any],
    *,
    data_root: Path,
) -> dict[str, Any] | None:
    mid = str(moment_id or "").strip()
    if not mid or not _MOMENT_ID_RE.match(mid):
        return None
    if resolved_source == "select_mark":
        return update_queue_item(mid, patch, data_dir=data_root)
    return update_annotation(mid, patch, data_dir=data_root)


def prepare_response_share_bundle(
    response_id: str,
    *,
    record: dict[str, Any] | None = None,
    title: str = "",
    question: str = "",
    note: str = "",
    tags: list[str] | None = None,
    kind: str = "note",
    data_dir: Path = DEFAULT_DATA_DIR,
    crf: int = 26,
    ffmpeg_exe: str | None = None,
) -> dict[str, Any]:
    """Build the 3 R2 share artifacts for a Labbed Answer (`takeover_response`), reusing `data/responses/<id>/clip.mp4`."""
    rid_str = str(response_id or "").strip()
    if not _MOMENT_ID_RE.match(rid_str):
        raise ValueError(f"Invalid response_id: {response_id!r}")

    data_root = data_dir.resolve()
    ann = record or get_annotation(rid_str, data_dir=data_root) or {}

    clip_src: Path | None = None
    raw_clip_url = str(ann.get("response_clip_url") or "").strip()
    if raw_clip_url:
        cand = _resolve_contained_path(raw_clip_url, data_root)
        if cand is not None and cand.is_file():
            clip_src = cand
    if clip_src is None:
        default_clip = (data_root / "responses" / rid_str / "clip.mp4").resolve()
        if default_clip.is_file():
            clip_src = default_clip
    if clip_src is None:
        raise FileNotFoundError(f"Response clip.mp4 not found for {rid_str}")

    qid = str(ann.get("quark_id") or "").strip()
    if not _QUARK_ID_RE.match(qid):
        raise ValueError(f"Invalid quark_id on response {rid_str}: {qid!r}")
    round_id = max(1, int(ann.get("round_id") or 1))
    persp = normalize_side(ann.get("perspective") or 1)
    start_gf = max(0, int(ann.get("start_gf") or 0))
    frame_count = max(1, int(ann.get("frame_count") or 0))
    raw_end_gf = int(ann.get("end_gf") or 0)
    end_gf = raw_end_gf if raw_end_gf > start_gf else (start_gf + max(1, frame_count))
    anchor_gf = int(ann.get("anchor_gf") or start_gf)
    if frame_count <= 1:
        frame_count = max(2, end_gf - start_gf + 1)
    duration_s = round(max(0.25, min(19.5, (frame_count - 1) / 60.0)), 2)

    bundle_id = f"sh_resp_{rid_str[-10:]}_{uuid.uuid4().hex[:6]}"
    bundle_dir = share_exports_root(data_root) / bundle_id
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # 1. Reuse existing response clip.mp4 directly when within 4 MB ceiling; re-encode only if oversized
    clip_path = bundle_dir / "clip.mp4"
    src_size = int(clip_src.stat().st_size)
    if src_size <= MAX_SHARE_CLIP_BYTES:
        shutil.copy2(clip_src, clip_path)
        clip_size = int(clip_path.stat().st_size)
        clip_info = {
            "path": str(clip_path),
            "size_bytes": clip_size,
            "width": SHARE_CLIP_WIDTH,
            "height": SHARE_CLIP_HEIGHT,
            "dar": SHARE_CLIP_DAR,
            "sar": SHARE_CLIP_SAR.replace("/", ":"),
            "crf": crf,
        }
    else:
        clip_info = extract_share_clip_mp4(
            clip_src,
            clip_path,
            start_s=0.0,
            duration_s=min(MAX_SHARE_DURATION_S, duration_s),
            crf=crf,
            ffmpeg_exe=ffmpeg_exe,
        )

    # 2. Gzip-compress response overlay.json -> overlay.json.gz
    ov_src: Path | None = None
    raw_ov_url = str(ann.get("response_overlay_url") or "").strip()
    if raw_ov_url:
        cand_ov = _resolve_contained_path(raw_ov_url, data_root)
        if cand_ov is not None and cand_ov.is_file():
            ov_src = cand_ov
    if ov_src is None:
        default_ov = clip_src.parent / "overlay.json"
        if default_ov.is_file():
            ov_src = default_ov

    ov_doc: dict[str, Any] | None = None
    if ov_src is not None:
        try:
            parsed_ov = json.loads(ov_src.read_text(encoding="utf-8"))
            if isinstance(parsed_ov, dict):
                ov_doc = parsed_ov
        except Exception:  # noqa: BLE001
            ov_doc = None

    ov_frames = ov_doc.get("frames") if (ov_doc and isinstance(ov_doc.get("frames"), list)) else []
    has_overlay = len(ov_frames) > 0
    ov_payload = ov_doc if has_overlay else {
        "v": 1,
        "w": 384,
        "h": 224,
        "start_gf": start_gf,
        "frame_count": 0,
        "frames": [],
    }
    overlay_gz_path = bundle_dir / "overlay.json.gz"
    overlay_bytes = write_overlay_gzip(ov_payload, overlay_gz_path)

    # Pick a representative frame (midpoint, or first frame with hitboxes) for hitbox-rendered thumb.jpg
    anchor_boxes: list[list[int]] = []
    thumb_offset_s = max(0.0, round(duration_s * 0.4, 3))
    if has_overlay:
        mid_idx = len(ov_frames) // 2
        chosen_idx = mid_idx
        for cand_i in [mid_idx, *range(len(ov_frames))]:
            fe = ov_frames[cand_i]
            if isinstance(fe, list) and len(fe) >= 5 and isinstance(fe[4], list) and fe[4]:
                anchor_boxes = fe[4]
                chosen_idx = cand_i
                break
        thumb_offset_s = max(0.0, min(max(0.0, duration_s - 0.05), round(chosen_idx / 60.0, 3)))

    # 3. Render 768x576 hitbox thumb.jpg from clip.mp4
    thumb_path = bundle_dir / "thumb.jpg"
    try:
        thumb_bytes = render_hitbox_thumb_jpg(
            clip_path,
            thumb_path,
            offset_s=thumb_offset_s,
            boxes=anchor_boxes,
            src_w=int(ov_payload.get("w") or 384),
            src_h=int(ov_payload.get("h") or 224),
            ffmpeg_exe=ffmpeg_exe,
        )
    except Exception:
        thumb_bytes = render_hitbox_thumb_jpg(
            clip_path,
            thumb_path,
            offset_s=0.0,
            boxes=anchor_boxes,
            src_w=int(ov_payload.get("w") or 384),
            src_h=int(ov_payload.get("h") or 224),
            ffmpeg_exe=ffmpeg_exe,
        )

    # Enrich matchup & player names from local manifest / Fightcade metadata
    p1_name = ""
    p2_name = ""
    p1_char = ""
    p2_char = ""
    self_char = str(ann.get("self_char_name") or "")
    opp_char = str(ann.get("opp_char_name") or "")
    mf_path = mobile_manifest_path(qid, data_dir=data_root)
    if mf_path.is_file():
        try:
            raw_mf = json.loads(mf_path.read_text(encoding="utf-8"))
            mf = ensure_manifest_matches(raw_mf, viewer_side=persp, data_dir=data_root)
            p1_name = str((mf.get("p1") or {}).get("name") or "")
            p2_name = str((mf.get("p2") or {}).get("name") or "")
            r_doc = next((r for r in (mf.get("rounds") or []) if int(r.get("round_id", 0)) == round_id), None)
            p1_char = str((r_doc or {}).get("p1_char_name") or (mf.get("p1") or {}).get("char_name") or "")
            p2_char = str((r_doc or {}).get("p2_char_name") or (mf.get("p2") or {}).get("char_name") or "")
            self_char = self_char or str((r_doc or {}).get("self_char_name") or mf.get("self_char_name") or "")
            opp_char = opp_char or str((r_doc or {}).get("opp_char_name") or mf.get("opp_char_name") or "")
        except Exception:  # noqa: BLE001
            pass
    if not p1_name or not p2_name:
        meta = lookup_replay_metadata(qid, data_dir=data_root)
        p1_name = p1_name or str(meta.get("p1_name") or "")
        p2_name = p2_name or str(meta.get("p2_name") or "")

    total_bytes = int(clip_info["size_bytes"]) + int(overlay_bytes) + int(thumb_bytes)
    bundle_meta: dict[str, Any] = {
        "ok": True,
        "bundle_id": bundle_id,
        "is_response_clip": True,
        "response_id": rid_str,
        "quark_id": qid,
        "game_id": "sfiii3nr1",
        "round_id": round_id,
        "perspective": persp,
        "start_gf": start_gf,
        "end_gf": end_gf,
        "anchor_gf": anchor_gf,
        "duration_s": duration_s,
        "width": clip_info["width"],
        "height": clip_info["height"],
        "dar": clip_info["dar"],
        "sar": clip_info["sar"],
        "p1_name": p1_name,
        "p2_name": p2_name,
        "p1_char": p1_char,
        "p2_char": p2_char,
        "self_char": self_char or (p1_char if persp == 1 else p2_char),
        "opp_char": opp_char or (p2_char if persp == 1 else p1_char),
        "title": str(title or ann.get("note") or "").strip()[:160],
        "question": str(question or "").strip()[:200],
        "note": str(note if note != "" else (ann.get("note") or "")).strip()[:2000],
        "tags": list(tags if tags is not None else (ann.get("tags") or [])),
        "kind": str(kind or ann.get("kind") or "note"),
        "has_hitboxes": len(anchor_boxes) > 0,
        "has_overlay": has_overlay,
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


def _parse_cloud_json(resp: Any, fallback_msg: str) -> dict[str, Any]:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {"ok": False, "error": fallback_msg}


def _cloud_request_json(
    sess: Any,
    method: str,
    url: str,
    *,
    cloud_token: str,
    json_body: dict[str, Any] | None = None,
    timeout_s: float = 30.0,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Authorization": f"Bearer {cloud_token}",
        "Content-Type": "application/json",
        "User-Agent": DEFAULT_UA,
    }
    try:
        resp = sess.request(method, url, json=json_body, headers=headers, timeout=timeout_s)
    except requests.RequestException as exc:
        raise CloudPublishError(f"Could not reach Fightcade Lab cloud ({exc})", status_code=502) from exc

    status = int(getattr(resp, "status_code", 500))
    data = _parse_cloud_json(resp, f"Cloud returned HTTP {status}")
    if status == 401:
        raise CloudNotConnectedError(
            "Your Fightcade Lab cloud connection expired — reconnect your account to share."
        )
    return status, data


def _upload_artifact_to_cloud(
    sess: Any,
    *,
    base_url: str,
    cloud_token: str,
    cloud_moment_id: str,
    artifact_kind: str,
    file_path: Path,
    content_type: str,
    quark_id: str,
    round_id: int,
    perspective: int,
    start_gf: int,
    end_gf: int,
    width: int | None = None,
    height: int | None = None,
    duration_s: float | None = None,
) -> dict[str, Any]:
    raw_bytes = file_path.read_bytes()
    content_length = len(raw_bytes)
    if content_length <= 0:
        raise ValueError(f"Cannot upload empty {artifact_kind} artifact: {file_path}")

    presign_body: dict[str, Any] = {
        "artifact_kind": artifact_kind,
        "content_type": content_type,
        "content_length": content_length,
        "quark_id": quark_id,
        "game_id": "sfiii3nr1",
        "round_id": max(1, int(round_id)),
        "perspective": normalize_side(perspective),
        "start_gf": max(0, int(start_gf)),
        "end_gf": max(int(start_gf) + 1, int(end_gf)),
        "moment_id": cloud_moment_id,
    }
    if artifact_kind == "clip":
        presign_body["width"] = int(width or SHARE_CLIP_WIDTH)
        presign_body["height"] = int(height or SHARE_CLIP_HEIGHT)
        presign_body["duration_s"] = round(max(0.2, min(19.9, float(duration_s or 1.0))), 2)

    st_pre, pre_data = _cloud_request_json(
        sess,
        "POST",
        f"{base_url}/api/uploads/presign",
        cloud_token=cloud_token,
        json_body=presign_body,
    )
    if st_pre >= 400 or pre_data.get("ok") is False:
        raise CloudPublishError(
            str(pre_data.get("error") or f"Presign failed for {artifact_kind} (HTTP {st_pre})"),
            status_code=st_pre,
            code=pre_data.get("code"),
        )

    upload_id = str(pre_data.get("upload_id") or "").strip()
    upload_url = str(pre_data.get("upload_url") or "").strip()
    if not upload_id or not upload_url:
        raise CloudPublishError(f"Invalid presign response for {artifact_kind}", status_code=502)
    if upload_url.startswith("/"):
        upload_url = f"{base_url}{upload_url}"

    # Strictly send Content-Type + Content-Length only (no Authorization header so R2 SigV4 stays valid)
    put_headers = {
        "Content-Type": content_type,
        "Content-Length": str(content_length),
    }
    try:
        put_resp = sess.request("PUT", upload_url, data=raw_bytes, headers=put_headers, timeout=60.0)
    except requests.RequestException as exc:
        raise CloudPublishError(f"Upload PUT failed for {artifact_kind}: {exc}", status_code=502) from exc

    put_status = int(getattr(put_resp, "status_code", 500))
    if put_status >= 400:
        put_data = _parse_cloud_json(put_resp, f"Upload PUT failed for {artifact_kind} (HTTP {put_status})")
        raise CloudPublishError(
            str(put_data.get("error") or f"Upload PUT failed for {artifact_kind} (HTTP {put_status})"),
            status_code=put_status,
            code=put_data.get("code"),
        )

    st_cmp, cmp_data = _cloud_request_json(
        sess,
        "POST",
        f"{base_url}/api/uploads/complete",
        cloud_token=cloud_token,
        json_body={"upload_id": upload_id, "moment_id": cloud_moment_id},
    )
    if st_cmp >= 400 or cmp_data.get("ok") is False:
        raise CloudPublishError(
            str(cmp_data.get("error") or f"Upload complete failed for {artifact_kind} (HTTP {st_cmp})"),
            status_code=st_cmp,
            code=cmp_data.get("code"),
        )
    return cmp_data


def publish_moment_to_cloud(
    payload: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    cloud_base_url: str | None = None,
    session: Any | None = None,
    ffmpeg_exe: str | None = None,
) -> dict[str, Any]:
    """Prepare media artifacts (if needed), upload to Fightcade Lab Cloud, and persist share metadata locally."""
    if not isinstance(payload, dict):
        raise ValueError("Publish payload must be a JSON object")

    data_root = data_dir.resolve()
    cloud_token = get_cloud_token(data_root)
    if not cloud_token:
        raise CloudNotConnectedError("Connect your Fightcade Lab account to publish shareable links.")

    base_url = (cloud_base_url or get_cloud_base_url()).rstrip("/")
    sess = session or requests.Session()

    moment_id = str(payload.get("moment_id") or payload.get("id") or payload.get("item_id") or "").strip()
    source_hint = str(payload.get("source") or "").strip().lower()
    local_rec, resolved_source = _lookup_local_moment(moment_id, source_hint=source_hint, data_root=data_root)
    is_resp = (
        resolved_source == "takeover_response"
        or bool(payload.get("is_response_clip"))
        or bool((local_rec or {}).get("response_clip_url"))
        or bool(payload.get("response_clip_url"))
    )

    shared_caption = str(
        payload.get("shared_caption")
        if "shared_caption" in payload
        else ((local_rec or {}).get("shared_caption") or "")
    ).strip()[:1000]
    include_note_on_share = bool(
        payload.get("include_note_on_share")
        if "include_note_on_share" in payload
        else (local_rec or {}).get("include_note_on_share", False)
    )
    hide_player_names = bool(
        payload.get("hide_player_names")
        if "hide_player_names" in payload
        else (local_rec or {}).get("hide_player_names", False)
    )
    note = str(
        payload.get("note")
        if "note" in payload
        else ((local_rec or {}).get("note") or "")
    ).strip()[:2000]
    raw_tags = payload.get("tags") if "tags" in payload else ((local_rec or {}).get("tags") or [])
    tags = [str(t).strip() for t in raw_tags if str(t).strip()] if isinstance(raw_tags, list) else []
    raw_kind = payload.get("kind") or (local_rec or {}).get("kind") or "note"
    cloud_kind = _map_cloud_kind(raw_kind)
    title = str(payload.get("title") or shared_caption or note or "").strip()[:160]
    question = str(payload.get("question") or shared_caption or "").strip()[:200]
    force_new = bool(payload.get("force_new", False))

    existing_cloud_id = str(
        payload.get("cloud_moment_id") or (local_rec or {}).get("cloud_moment_id") or ""
    ).strip() or None

    # Fast-path: update existing cloud moment settings / re-enable unlisted link without re-uploading media
    if existing_cloud_id and not force_new:
        patch_body = {
            "visibility": "unlisted",
            "shared_caption": shared_caption,
            "include_note_on_share": include_note_on_share,
            "hide_player_names": hide_player_names,
            "title": title,
            "question": question,
            "note": note,
            "tags": tags,
            "kind": cloud_kind,
        }
        st_patch, patch_data = _cloud_request_json(
            sess,
            "PATCH",
            f"{base_url}/api/moments/{existing_cloud_id}",
            cloud_token=cloud_token,
            json_body=patch_body,
        )
        if st_patch == 404:
            existing_cloud_id = None
        elif st_patch >= 400 or patch_data.get("ok") is False:
            raise CloudPublishError(
                str(patch_data.get("error") or f"Failed to update shared moment (HTTP {st_patch})"),
                status_code=st_patch,
                code=patch_data.get("code"),
            )
        else:
            cloud_moment = patch_data.get("moment") or {}
            cm_id = str(cloud_moment.get("id") or existing_cloud_id)
            share_tok = cloud_moment.get("share_token")
            share_url = cloud_moment.get("share_url") or (f"{base_url}/m/{share_tok}" if share_tok else f"{base_url}/m/{cm_id}")
            vis = str(cloud_moment.get("visibility") or "unlisted")
            local_patch = {
                "cloud_moment_id": cm_id,
                "cloud_share_token": share_tok,
                "cloud_share_url": share_url,
                "cloud_visibility": vis,
                "shared_caption": shared_caption,
                "include_note_on_share": include_note_on_share,
                "hide_player_names": hide_player_names,
            }
            if moment_id:
                _persist_local_cloud_meta(moment_id, resolved_source, local_patch, data_root=data_root)
            return {
                "ok": True,
                "updated": True,
                "uploaded": False,
                "local_moment_id": moment_id or None,
                "source": resolved_source,
                "moment": cloud_moment,
                **local_patch,
            }

    # Fresh cloud publish: build local share artifacts first
    if is_resp:
        resp_id = moment_id or str(payload.get("response_id") or "").strip()
        bundle = prepare_response_share_bundle(
            resp_id,
            record=local_rec if local_rec is not None else payload,
            title=title,
            question=question,
            note=note,
            tags=tags,
            kind=str(raw_kind),
            data_dir=data_root,
            crf=int(payload.get("crf", 26)),
            ffmpeg_exe=ffmpeg_exe,
        )
    else:
        qid = str(payload.get("quark_id") or (local_rec or {}).get("quark_id") or "").strip()
        round_id = int(payload.get("round_id") or (local_rec or {}).get("round_id") or 0)
        start_gf_raw = payload.get("start_gf") if payload.get("start_gf") not in (None, "") else (local_rec or {}).get("start_gf")
        end_gf_raw = payload.get("end_gf") if payload.get("end_gf") not in (None, "") else (local_rec or {}).get("end_gf")
        anchor_gf_raw = (
            payload.get("anchor_gf")
            if payload.get("anchor_gf") not in (None, "")
            else ((local_rec or {}).get("anchor_gf") or (local_rec or {}).get("press_gf"))
        )
        persp = int(payload.get("perspective") or (local_rec or {}).get("perspective") or 1)
        bundle = prepare_share_bundle(
            qid,
            round_id,
            start_gf=int(start_gf_raw) if start_gf_raw not in (None, "") else None,
            end_gf=int(end_gf_raw) if end_gf_raw not in (None, "") else None,
            anchor_gf=int(anchor_gf_raw) if anchor_gf_raw not in (None, "") else None,
            perspective=persp,
            title=title,
            question=question,
            note=note,
            tags=tags,
            kind=str(raw_kind),
            data_dir=data_root,
            crf=int(payload.get("crf", 26)),
            ffmpeg_exe=ffmpeg_exe,
        )

    eff_title = title or bundle.get("title") or f"{bundle.get('self_char') or 'P1'} vs {bundle.get('opp_char') or 'P2'} · R{bundle['round_id']}"
    create_body = {
        "visibility": "unlisted",
        "quark_id": bundle["quark_id"],
        "game_id": bundle.get("game_id") or "sfiii3nr1",
        "round_id": max(1, int(bundle["round_id"])),
        "perspective": normalize_side(bundle["perspective"]),
        "start_gf": max(0, int(bundle["start_gf"])),
        "end_gf": max(int(bundle["start_gf"]) + 1, int(bundle["end_gf"])),
        "anchor_gf": max(0, int(bundle["anchor_gf"])),
        "kind": cloud_kind,
        "title": str(eff_title)[:160],
        "question": question,
        "shared_caption": shared_caption,
        "note": note,
        "include_note_on_share": include_note_on_share,
        "hide_player_names": hide_player_names,
        "tags": tags,
        "p1_name": str(bundle.get("p1_name") or ""),
        "p2_name": str(bundle.get("p2_name") or ""),
        "p1_char": str(bundle.get("p1_char") or ""),
        "p2_char": str(bundle.get("p2_char") or ""),
        "self_char": str(bundle.get("self_char") or ""),
        "opp_char": str(bundle.get("opp_char") or ""),
        "clip_width": int(bundle.get("width") or SHARE_CLIP_WIDTH),
        "clip_height": int(bundle.get("height") or SHARE_CLIP_HEIGHT),
        "clip_duration_s": float(bundle.get("duration_s") or 0),
    }

    st_create, create_data = _cloud_request_json(
        sess,
        "POST",
        f"{base_url}/api/moments",
        cloud_token=cloud_token,
        json_body=create_body,
    )
    if st_create >= 400 or create_data.get("ok") is False or not isinstance(create_data.get("moment"), dict):
        raise CloudPublishError(
            str(create_data.get("error") or f"Failed to create cloud moment (HTTP {st_create})"),
            status_code=st_create,
            code=create_data.get("code"),
        )

    cloud_moment = create_data["moment"]
    cm_id = str(cloud_moment.get("id") or "").strip()
    if not cm_id:
        raise CloudPublishError("Cloud returned moment without an id", status_code=502)

    arts = bundle.get("artifacts") or {}
    uploaded_artifacts: list[str] = []

    # Upload clip (required)
    clip_art = arts.get("clip") or {}
    clip_file = Path(str(clip_art.get("path") or ""))
    _upload_artifact_to_cloud(
        sess,
        base_url=base_url,
        cloud_token=cloud_token,
        cloud_moment_id=cm_id,
        artifact_kind="clip",
        file_path=clip_file,
        content_type="video/mp4",
        quark_id=bundle["quark_id"],
        round_id=int(bundle["round_id"]),
        perspective=int(bundle["perspective"]),
        start_gf=int(bundle["start_gf"]),
        end_gf=int(bundle["end_gf"]),
        width=int(bundle.get("width") or SHARE_CLIP_WIDTH),
        height=int(bundle.get("height") or SHARE_CLIP_HEIGHT),
        duration_s=float(bundle.get("duration_s") or 1.0),
    )
    uploaded_artifacts.append("clip")

    # Upload overlay.json.gz only when overlay frames are present
    ov_art = arts.get("overlay") or {}
    ov_file = Path(str(ov_art.get("path") or "")) if ov_art.get("path") else None
    if bundle.get("has_overlay") and ov_file is not None and ov_file.is_file() and ov_file.stat().st_size > 0:
        _upload_artifact_to_cloud(
            sess,
            base_url=base_url,
            cloud_token=cloud_token,
            cloud_moment_id=cm_id,
            artifact_kind="overlay",
            file_path=ov_file,
            content_type="application/gzip",
            quark_id=bundle["quark_id"],
            round_id=int(bundle["round_id"]),
            perspective=int(bundle["perspective"]),
            start_gf=int(bundle["start_gf"]),
            end_gf=int(bundle["end_gf"]),
        )
        uploaded_artifacts.append("overlay")

    # Upload hitbox-rendered thumb.jpg
    thumb_art = arts.get("thumb") or {}
    thumb_file = Path(str(thumb_art.get("path") or "")) if thumb_art.get("path") else None
    if thumb_file is not None and thumb_file.is_file() and thumb_file.stat().st_size > 0:
        _upload_artifact_to_cloud(
            sess,
            base_url=base_url,
            cloud_token=cloud_token,
            cloud_moment_id=cm_id,
            artifact_kind="thumb",
            file_path=thumb_file,
            content_type="image/jpeg",
            quark_id=bundle["quark_id"],
            round_id=int(bundle["round_id"]),
            perspective=int(bundle["perspective"]),
            start_gf=int(bundle["start_gf"]),
            end_gf=int(bundle["end_gf"]),
        )
        uploaded_artifacts.append("thumb")

    share_tok = cloud_moment.get("share_token")
    share_url = cloud_moment.get("share_url") or (f"{base_url}/m/{share_tok}" if share_tok else f"{base_url}/m/{cm_id}")
    vis = str(cloud_moment.get("visibility") or "unlisted")

    local_patch = {
        "cloud_moment_id": cm_id,
        "cloud_share_token": share_tok,
        "cloud_share_url": share_url,
        "cloud_visibility": vis,
        "shared_caption": shared_caption,
        "include_note_on_share": include_note_on_share,
        "hide_player_names": hide_player_names,
    }
    if moment_id:
        _persist_local_cloud_meta(moment_id, resolved_source, local_patch, data_root=data_root)

    return {
        "ok": True,
        "updated": False,
        "uploaded": True,
        "local_moment_id": moment_id or None,
        "source": resolved_source,
        "bundle_id": bundle.get("bundle_id"),
        "uploaded_artifacts": uploaded_artifacts,
        "moment": cloud_moment,
        **local_patch,
    }


def rotate_cloud_share_link(
    moment_id: str,
    *,
    source: str = "",
    cloud_moment_id: str | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    cloud_base_url: str | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    """Mint a fresh revocable share link (`POST /api/moments/<id>/rotate-share`) and persist locally."""
    data_root = data_dir.resolve()
    cloud_token = get_cloud_token(data_root)
    if not cloud_token:
        raise CloudNotConnectedError("Connect your Fightcade Lab account to manage share links.")

    local_rec, resolved_source = _lookup_local_moment(moment_id, source_hint=source, data_root=data_root)
    cm_id = str(cloud_moment_id or (local_rec or {}).get("cloud_moment_id") or "").strip()
    if not cm_id:
        raise ValueError("This moment does not have a published cloud share link yet.")

    base_url = (cloud_base_url or get_cloud_base_url()).rstrip("/")
    sess = session or requests.Session()
    st, data = _cloud_request_json(
        sess,
        "POST",
        f"{base_url}/api/moments/{cm_id}/rotate-share",
        cloud_token=cloud_token,
        json_body={},
    )
    if st >= 400 or data.get("ok") is False:
        raise CloudPublishError(
            str(data.get("error") or f"Failed to rotate share link (HTTP {st})"),
            status_code=st,
            code=data.get("code"),
        )

    cloud_moment = data.get("moment") or {}
    new_tok = cloud_moment.get("share_token")
    new_url = cloud_moment.get("share_url") or (f"{base_url}/m/{new_tok}" if new_tok else None)
    vis = str(cloud_moment.get("visibility") or "unlisted")
    local_patch = {
        "cloud_moment_id": str(cloud_moment.get("id") or cm_id),
        "cloud_share_token": new_tok,
        "cloud_share_url": new_url,
        "cloud_visibility": vis,
    }
    if moment_id:
        _persist_local_cloud_meta(moment_id, resolved_source, local_patch, data_root=data_root)

    return {
        "ok": True,
        "local_moment_id": moment_id or None,
        "source": resolved_source,
        "moment": cloud_moment,
        **local_patch,
    }


def revoke_cloud_share_link(
    moment_id: str,
    *,
    source: str = "",
    cloud_moment_id: str | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
    cloud_base_url: str | None = None,
    session: Any | None = None,
) -> dict[str, Any]:
    """Revoke an active unlisted share link (`PATCH /api/moments/<id>` -> `visibility: 'private'`) and clear locally."""
    data_root = data_dir.resolve()
    cloud_token = get_cloud_token(data_root)
    if not cloud_token:
        raise CloudNotConnectedError("Connect your Fightcade Lab account to manage share links.")

    local_rec, resolved_source = _lookup_local_moment(moment_id, source_hint=source, data_root=data_root)
    cm_id = str(cloud_moment_id or (local_rec or {}).get("cloud_moment_id") or "").strip()
    if not cm_id:
        raise ValueError("This moment does not have a published cloud share link yet.")

    base_url = (cloud_base_url or get_cloud_base_url()).rstrip("/")
    sess = session or requests.Session()
    st, data = _cloud_request_json(
        sess,
        "PATCH",
        f"{base_url}/api/moments/{cm_id}",
        cloud_token=cloud_token,
        json_body={"visibility": "private"},
    )
    if st >= 400 or data.get("ok") is False:
        raise CloudPublishError(
            str(data.get("error") or f"Failed to revoke share link (HTTP {st})"),
            status_code=st,
            code=data.get("code"),
        )

    cloud_moment = data.get("moment") or {}
    local_patch = {
        "cloud_moment_id": str(cloud_moment.get("id") or cm_id),
        "cloud_share_token": None,
        "cloud_share_url": None,
        "cloud_visibility": "private",
    }
    if moment_id:
        _persist_local_cloud_meta(moment_id, resolved_source, local_patch, data_root=data_root)

    return {
        "ok": True,
        "local_moment_id": moment_id or None,
        "source": resolved_source,
        "moment": cloud_moment,
        **local_patch,
    }
