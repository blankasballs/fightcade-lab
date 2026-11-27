"""Offline renderer & background watcher for Local Takeover Response Moments.

When a user nails an optimal counterplay / punish inside FBNeo Replay Takeover and selects
`Save Current Rep` or `Save Last Rep` in the `Start` menu, `lua/replay_takeover.lua` atomically
writes `<export_id>.inputs` and `<export_id>.json` into `data/selections/takeover_exports/`.

`TakeoverExportWatcher` picks up each export sequentially, renders a self-contained 60fps
`768x448` (4:3 DAR) MP4 clip with lockstep arcade audio + toggleable `overlay.json`
(Hitboxes/Inputs/Sticks/Gauges) into `data/responses/<resp_id>/`, and registers it as a
first-class Moment (`kind="response"`, `source="takeover_response"`, `parent_moment_id`)
in `annotations.jsonl`.
"""

from __future__ import annotations

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

from fightcade_analysis.annotations import create_annotation, get_annotation
from fightcade_analysis.audio_mute import start_mute_watcher
from fightcade_analysis.clip_renderer import (
    DEFAULT_CAPTURE_LUA,
    DEFAULT_DATA_DIR,
    DEFAULT_FBNEO_EXE,
    encode_gdframes_to_mp4,
    resolve_capture_fbneo_exe,
    resolve_grouflon_dir,
)
from fightcade_analysis.env_config import build_fbneo_cmdline, ensure_fbneo_config_inheritance
from fightcade_analysis.match_catalog import detect_default_username
from fightcade_analysis.round_renderer import (
    DEFAULT_MOBILE_CRF,
    DEFAULT_MOBILE_GOP,
    DEFAULT_MOBILE_SCALE,
    _parse_gf_sidecar,
    mux_video_and_pcm_to_mp4,
    parse_overlay_tsv_to_json,
    resolve_video_encoder,
)

RESPONSES_SUBDIR = "responses"
EXPORTS_REL_DIR = Path("selections") / "takeover_exports"
MIN_RESPONSE_FRAMES = 15
MAX_RESPONSE_FRAMES = 1200

_EXPORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_QUARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")


def takeover_exports_dir(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / EXPORTS_REL_DIR


def responses_root_dir(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / RESPONSES_SUBDIR


def _ensure_within_data_dir(path: Path, data_root: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(data_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes data_dir: {path}") from exc
    return resolved


def validate_takeover_export(raw: dict[str, Any], *, data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Strictly validate an exported rep JSON descriptor from `lua/replay_takeover.lua`."""
    if not isinstance(raw, dict):
        raise ValueError("Takeover export descriptor must be a JSON object")

    data_root = data_dir.resolve()
    export_id = str(raw.get("export_id") or "").strip()
    if not _EXPORT_ID_RE.match(export_id):
        raise ValueError(f"Invalid export_id: {export_id!r}")

    quark_id = str(raw.get("quark_id") or "").strip()
    if not _QUARK_ID_RE.match(quark_id):
        raise ValueError(f"Invalid quark_id: {quark_id!r}")

    parent_moment_id = str(raw.get("parent_moment_id") or "").strip()
    if parent_moment_id and not _EXPORT_ID_RE.match(parent_moment_id):
        raise ValueError(f"Invalid parent_moment_id: {parent_moment_id!r}")

    try:
        round_id = int(raw.get("round_id") or 0)
        perspective = int(raw.get("perspective") or 1)
        start_gf = int(raw.get("start_gf") or 0)
        end_gf = int(raw.get("end_gf") or 0)
        frame_count = int(raw.get("frame_count") or 0)
        start_p1_hp = int(raw.get("start_p1_hp") or 0)
        start_p2_hp = int(raw.get("start_p2_hp") or 0)
        end_p1_hp = int(raw.get("end_p1_hp") or 0)
        end_p2_hp = int(raw.get("end_p2_hp") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("Numeric export fields must be integers") from exc

    if perspective not in (1, 2):
        raise ValueError("perspective must be 1 or 2")
    if frame_count < MIN_RESPONSE_FRAMES or frame_count > MAX_RESPONSE_FRAMES:
        raise ValueError(f"frame_count must be between {MIN_RESPONSE_FRAMES} and {MAX_RESPONSE_FRAMES}")
    if start_gf < 1 or end_gf < start_gf:
        raise ValueError("Invalid start_gf / end_gf bounds")

    raw_fs = str(raw.get("savestate_path") or "").strip()
    raw_inp = str(raw.get("inputs_path") or "").strip()
    if not raw_fs or not raw_inp:
        raise ValueError("savestate_path and inputs_path are required")

    savestate_path = _ensure_within_data_dir(Path(raw_fs), data_root, "savestate_path")
    inputs_path = _ensure_within_data_dir(Path(raw_inp), data_root, "inputs_path")
    if not savestate_path.is_file():
        raise FileNotFoundError(f"savestate_path not found: {savestate_path}")
    if not inputs_path.is_file():
        raise FileNotFoundError(f"inputs_path not found: {inputs_path}")

    return {
        "export_id": export_id,
        "parent_moment_id": parent_moment_id or None,
        "quark_id": quark_id,
        "round_id": max(1, round_id) if round_id > 0 else 1,
        "perspective": perspective,
        "savestate_path": savestate_path,
        "inputs_path": inputs_path,
        "start_gf": start_gf,
        "end_gf": end_gf,
        "frame_count": frame_count,
        "start_p1_hp": max(0, min(200, start_p1_hp)),
        "start_p2_hp": max(0, min(200, start_p2_hp)),
        "end_p1_hp": max(0, min(200, end_p1_hp)),
        "end_p2_hp": max(0, min(200, end_p2_hp)),
    }


def compute_perspective_hp_deltas(export_doc: dict[str, Any]) -> tuple[int, int]:
    """Return `(hp_delta_self, hp_delta_opp)` from raw P1/P2 start/end HP values.

    Negative values represent HP lost (e.g. `hp_delta_opp = -72` means you dealt 72 damage;
    `hp_delta_self = 0` means you took 0 damage).
    """
    p1_delta = int(export_doc.get("end_p1_hp", 0)) - int(export_doc.get("start_p1_hp", 0))
    p2_delta = int(export_doc.get("end_p2_hp", 0)) - int(export_doc.get("start_p2_hp", 0))
    if int(export_doc.get("perspective", 1)) == 2:
        return (p2_delta, p1_delta)
    return (p1_delta, p2_delta)


def _render_response_media(
    *,
    resp_id: str,
    resp_dir: Path,
    resp_fs: Path,
    resp_inputs: Path,
    start_gf: int,
    end_gf: int,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    capture_lua: Path = DEFAULT_CAPTURE_LUA,
    scale: int = DEFAULT_MOBILE_SCALE,
    crf: int = DEFAULT_MOBILE_CRF,
    gop_size: int = DEFAULT_MOBILE_GOP,
    capture_audio: bool = True,
    timeout_seconds: int = 90,
) -> dict[str, Any]:
    """Run invisible headless FBNeo offline capture to produce `clip.mp4` + `overlay.json`."""
    eff_exe, use_maxspeed = resolve_capture_fbneo_exe(fbneo_exe)
    if not eff_exe.exists():
        raise FileNotFoundError(f"fcadefbneo executable not found at: {eff_exe}")
    ensure_fbneo_config_inheritance(eff_exe)

    lua_path = capture_lua.resolve()
    flags = ["-w", "-headless"] if use_maxspeed else ["-w"]
    cmd_str = build_fbneo_cmdline(eff_exe, flags, resp_fs.resolve(), lua_path, target_is_path=True)

    mp4_path = resp_dir / "clip.mp4"
    vid_tmp_path = resp_dir / "clip.mp4.vid.mp4"
    pcm_tmp_path = resp_dir / "clip.mp4.pcm"
    overlay_json_path = resp_dir / "overlay.json"

    tmp_tag = f"_resp_{resp_id[:20]}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    use_direct_pipe = bool(use_maxspeed)
    eff_encoder = resolve_video_encoder() if use_direct_pipe else "libx264:fast"
    raw_path = resp_dir / f"{tmp_tag}.gdframes"
    target_out_path = mp4_path if use_direct_pipe else raw_path
    overlay_tsv_path = resp_dir / f"{tmp_tag}.overlay.tsv"
    gf_range_path = Path(f"{target_out_path}.gf")
    jobs_path = resp_dir / f"{tmp_tag}.tsv"

    post_frames = max(1, int(end_gf) - int(start_gf))
    jobs_path.write_text(
        f"{int(start_gf)}\t0\t{post_frames}\t0\t\t{target_out_path}\t\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["FC_JOBS_FILE"] = str(jobs_path)
    env["FC_OFFLINE_INPUTS"] = str(resp_inputs.resolve())
    env["FC_HIDE_T0_BANNER"] = "1"
    env["FC_WRITE_GF_RANGE"] = "1"
    env["FC_GROUFLON_OVERLAY"] = "0"
    env["FC_EXPORT_OVERLAY"] = "1"
    env["FC_OVERLAY_DUMP_OUT"] = str(overlay_tsv_path)
    g_dir = resolve_grouflon_dir(eff_exe)
    if g_dir is not None:
        env["FC_GROUFLON_DIR"] = str(g_dir)
    if use_direct_pipe:
        env["FC_FFMPEG_EXE"] = imageio_ffmpeg.get_ffmpeg_exe()
        env["FC_VIDEO_SCALE"] = str(max(1, int(scale)))
        env["FC_VIDEO_CRF"] = str(int(crf))
        env["FC_VIDEO_GOP"] = str(int(gop_size) if gop_size else 0)
        env["FC_VIDEO_ENCODER"] = eff_encoder
        env["FC_CAPTURE_AUDIO"] = "1" if capture_audio else "0"
        env["FC_PIPE_TAG"] = f"resp_{os.getpid()}_{uuid.uuid4().hex[:6]}"

    popen_kwargs: dict[str, Any] = {"cwd": str(eff_exe.parent), "env": env}
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        popen_kwargs["startupinfo"] = si

    t0 = time.perf_counter()
    has_audio = False
    proc: subprocess.Popen[bytes] | None = None
    try:
        for _ in range(2):
            target_out_path.unlink(missing_ok=True)
            vid_tmp_path.unlink(missing_ok=True)
            pcm_tmp_path.unlink(missing_ok=True)
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
            has_vid_out = (
                (vid_tmp_path.exists() and vid_tmp_path.stat().st_size > min_bytes)
                or (target_out_path.exists() and target_out_path.stat().st_size > min_bytes)
            )
            if proc.returncode == 0 and has_vid_out:
                break

        err_file = Path(f"{jobs_path}.err")
        err_detail = ""
        if err_file.exists():
            err_detail = err_file.read_text(encoding="utf-8", errors="ignore").strip()
            err_file.unlink(missing_ok=True)

        min_bytes = 1024 if use_direct_pipe else 12
        has_vid_out = (
            (vid_tmp_path.exists() and vid_tmp_path.stat().st_size > min_bytes)
            or (target_out_path.exists() and target_out_path.stat().st_size > min_bytes)
        )
        if proc is None or proc.returncode != 0 or not has_vid_out:
            rc = proc.returncode if proc is not None else -1
            raise RuntimeError(f"Offline response capture failed for {resp_id} (exit={rc}): {err_detail}")

        actual_first_gf, actual_last_gf, pipe_frames, _, aud_spf, muxed_inline = _parse_gf_sidecar(
            gf_range_path, start_gf, end_gf
        )
        if use_direct_pipe:
            n_frames = pipe_frames if pipe_frames > 0 else max(1, actual_last_gf - actual_first_gf + 1)
            if muxed_inline and mp4_path.exists() and mp4_path.stat().st_size > 1024:
                has_audio = True
            elif vid_tmp_path.exists() and vid_tmp_path.stat().st_size > 1024:
                if capture_audio and pcm_tmp_path.exists() and pcm_tmp_path.stat().st_size > 1024:
                    mux_video_and_pcm_to_mp4(
                        vid_tmp_path,
                        pcm_tmp_path,
                        mp4_path,
                        samples_per_frame=aud_spf if aud_spf > 0 else 740,
                        fps=60,
                    )
                    has_audio = True
                else:
                    vid_tmp_path.replace(mp4_path)
        else:
            n_frames, _, _ = encode_gdframes_to_mp4(
                raw_path,
                mp4_path,
                fps=60,
                scale=scale,
                crf=crf,
                gop_size=gop_size,
            )

        has_overlay = False
        if overlay_tsv_path.exists() and overlay_tsv_path.stat().st_size > 16:
            parse_overlay_tsv_to_json(overlay_tsv_path, overlay_json_path, start_gf=actual_first_gf)
            has_overlay = overlay_json_path.is_file()
    finally:
        raw_path.unlink(missing_ok=True)
        vid_tmp_path.unlink(missing_ok=True)
        pcm_tmp_path.unlink(missing_ok=True)
        overlay_tsv_path.unlink(missing_ok=True)
        gf_range_path.unlink(missing_ok=True)
        jobs_path.unlink(missing_ok=True)

    return {
        "clip_path": mp4_path,
        "overlay_path": overlay_json_path if has_overlay else None,
        "first_gf": int(actual_first_gf),
        "last_gf": int(actual_last_gf),
        "frame_count": int(n_frames),
        "has_audio": has_audio,
        "render_s": round(time.perf_counter() - t0, 2),
    }


def _archive_export_files(
    export_json_path: Path,
    inputs_path: Path | None,
    subdir_name: str,
    *,
    error_text: str | None = None,
) -> None:
    dest_dir = export_json_path.parent / subdir_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    for src in (export_json_path, inputs_path):
        if src is None or not src.exists():
            continue
        # Only move inputs if it lives in the exports queue directory (never move source replay files)
        if src != export_json_path and src.parent.resolve() != export_json_path.parent.resolve():
            continue
        dst = dest_dir / src.name
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            try:
                src.unlink(missing_ok=True)
            except OSError:
                pass
    if error_text:
        err_path = dest_dir / f"{export_json_path.stem}.err"
        try:
            err_path.write_text(error_text + "\n", encoding="utf-8")
        except OSError:
            pass


def render_takeover_response(
    export_json_path: Path,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    capture_lua: Path = DEFAULT_CAPTURE_LUA,
    render_media_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Render an exported Takeover rep into `data/responses/<resp_id>/` and create a first-class Moment."""
    data_root = data_dir.resolve()
    raw_doc = json.loads(export_json_path.read_text(encoding="utf-8"))
    exp = validate_takeover_export(raw_doc, data_dir=data_root)

    resp_id = exp["export_id"]
    resp_dir = responses_root_dir(data_root) / resp_id
    resp_dir.mkdir(parents=True, exist_ok=True)

    resp_fs = resp_dir / "start.fs"
    resp_inputs = resp_dir / "rep.inputs"
    shutil.copy2(exp["savestate_path"], resp_fs)
    shutil.copy2(exp["inputs_path"], resp_inputs)

    renderer = render_media_fn or _render_response_media
    media = renderer(
        resp_id=resp_id,
        resp_dir=resp_dir,
        resp_fs=resp_fs,
        resp_inputs=resp_inputs,
        start_gf=exp["start_gf"],
        end_gf=exp["end_gf"],
        fbneo_exe=fbneo_exe,
        capture_lua=capture_lua,
    )

    hp_delta_self, hp_delta_opp = compute_perspective_hp_deltas(exp)

    # Inherit round_id / owner from parent annotation if available and missing
    parent_id = exp["parent_moment_id"]
    parent_ann = get_annotation(parent_id, data_dir=data_root) if parent_id else None
    round_id = exp["round_id"]
    if round_id <= 1 and parent_ann and int(parent_ann.get("round_id") or 0) > 0:
        round_id = int(parent_ann["round_id"])
    owner = (
        str(parent_ann.get("owner") or "").strip()
        if parent_ann
        else ""
    ) or detect_default_username(data_root)

    first_gf = int(media.get("first_gf") or exp["start_gf"])
    last_gf = int(media.get("last_gf") or exp["end_gf"])
    n_frames = int(media.get("frame_count") or exp["frame_count"])
    has_overlay = bool(media.get("overlay_path")) or (resp_dir / "overlay.json").is_file()

    ann = create_annotation(
        {
            "id": resp_id,
            "owner": owner,
            "quark_id": exp["quark_id"],
            "round_id": round_id,
            "perspective": exp["perspective"],
            "anchor_gf": first_gf,
            "start_gf": first_gf,
            "end_gf": max(first_gf + 1, last_gf),
            "is_range": True,
            "kind": "response",
            "source": "takeover_response",
            "parent_moment_id": parent_id,
            "response_clip_url": f"/responses/{resp_id}/clip.mp4",
            "response_overlay_url": f"/responses/{resp_id}/overlay.json" if has_overlay else None,
            "savestate": str(resp_fs.resolve()),
            "inputs": str(resp_inputs.resolve()),
            "hp_delta_self": hp_delta_self,
            "hp_delta_opp": hp_delta_opp,
            "frame_count": n_frames,
            "note": "",
            "tags": [],
            "queued_drill": False,
        },
        data_dir=data_root,
        async_bake=False,
    )

    _archive_export_files(export_json_path, exp["inputs_path"], "processed")

    return {
        "ok": True,
        "response_id": resp_id,
        "annotation": ann,
        "clip_path": str(resp_dir / "clip.mp4"),
        "overlay_path": str(resp_dir / "overlay.json") if has_overlay else None,
        "render_s": media.get("render_s", 0.0),
    }


class TakeoverExportWatcher:
    """Lightweight background daemon polling `data/selections/takeover_exports/*.json`."""

    def __init__(
        self,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        fbneo_exe: Path = DEFAULT_FBNEO_EXE,
        poll_interval: float = 1.0,
        render_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.fbneo_exe = fbneo_exe
        self.exports_dir = takeover_exports_dir(self.data_dir)
        self._poll_interval = max(0.1, float(poll_interval))
        self._render_fn = render_fn or render_takeover_response
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._processing = False
        self._pending_count = 0
        self._last_response_at: str | None = None
        self._last_response_id: str | None = None
        self._last_error: str | None = None

    def start(self) -> None:
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, name="fc-takeover-export-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)

    def is_busy(self) -> bool:
        with self._lock:
            return self._processing or self._pending_count > 0

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "processing": self._processing,
                "pending_responses": self._pending_count,
                "last_response_at": self._last_response_at,
                "last_response_id": self._last_response_id,
                "last_error": self._last_error,
            }

    def process_once(self) -> int:
        """Scan and process any pending `*.json` exports once. Returns number processed."""
        if not self.exports_dir.is_dir():
            return 0
        pending = sorted(
            p for p in self.exports_dir.glob("*.json")
            if p.is_file() and not p.name.endswith(".tmp")
        )
        with self._lock:
            self._pending_count = len(pending)
        if not pending:
            return 0

        processed = 0
        for idx, json_path in enumerate(pending):
            if self._stop_evt.is_set():
                break
            with self._lock:
                self._processing = True
                self._pending_count = len(pending) - idx
            try:
                res = self._render_fn(json_path, data_dir=self.data_dir, fbneo_exe=self.fbneo_exe)
                now_iso = datetime.now().isoformat(timespec="seconds")
                resp_id = str(res.get("response_id") or json_path.stem)
                with self._lock:
                    self._last_response_at = now_iso
                    self._last_response_id = resp_id
                    self._last_error = None
                processed += 1
                print(f"[responses] Rendered Takeover Response '{resp_id}' in {res.get('render_s', 0)}s")
            except Exception as exc:  # noqa: BLE001
                err_msg = f"Failed to render Takeover Response '{json_path.name}': {exc}"
                print(f"[responses] ERROR: {err_msg}")
                with self._lock:
                    self._last_error = str(exc)
                inp_cand = json_path.with_suffix(".inputs")
                _archive_export_files(json_path, inp_cand if inp_cand.exists() else None, "failed", error_text=err_msg)
            finally:
                with self._lock:
                    self._processing = False
                    self._pending_count = max(0, len(pending) - idx - 1)
        return processed

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.process_once()
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    self._last_error = str(exc)
            self._stop_evt.wait(self._poll_interval)
