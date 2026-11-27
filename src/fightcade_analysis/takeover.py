"""Direct Replay Takeover launcher & shared browser Launch-button UI helpers.

Allows launching instant `<1s` `.fs` savestate Replay Takeover (or stream-seek fallback)
directly from the browser Reports Hub via `POST /api/takeover`, without copying PowerShell commands.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
from typing import Any, Callable

from fightcade_analysis.clip_renderer import (
    DEFAULT_DATA_DIR,
    DEFAULT_FBNEO_EXE,
    REPO_ROOT,
    export_takeover_inputs,
    lookup_cached_stream_target,
    resolve_grouflon_dir,
    resolve_takeover_fbneo_exe,
)
from fightcade_analysis.companion_bridge import lookup_remote_fightcade_meta
from fightcade_analysis.env_config import (
    build_fbneo_cmdline,
    ensure_fbneo_config_inheritance,
    validate_stream_target,
)
from fightcade_analysis.fetch_replays import resolve_stream_target
from fightcade_analysis.ota_lua import resolve_lua_script

DEFAULT_REPLAY_TAKEOVER_LUA = REPO_ROOT / "lua" / "replay_takeover.lua"
DEFAULT_SEEK_LUA = REPO_ROOT / "lua" / "seek_and_takeover.lua"

_QUARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_SELECTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,128}$")
_GAME_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")


def get_windows_desktop_name() -> tuple[str, bool]:
    """Return `(desktop_name, is_interactive_default)` for the current thread on Windows."""
    if sys.platform != "win32":
        return ("non-windows", True)
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hdesk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        if not hdesk:
            return ("unknown", True)
        buf = ctypes.create_unicode_buffer(128)
        needed = wintypes.DWORD(0)
        uoi_name = 2
        ok = user32.GetUserObjectInformationW(hdesk, uoi_name, buf, ctypes.sizeof(buf), ctypes.byref(needed))
        if not ok:
            return ("unknown", True)
        name = str(buf.value or "unknown")
        return (name, name.lower() == "default")
    except Exception:  # noqa: BLE001
        return ("unknown", True)


def sanitize_label(label: Any) -> str:
    text = str(label or "").replace('"', "'").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = " ".join(text.split())
    return text[:120]


_NOTE_UNICODE_TRANSLATIONS = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2014": "-",
        "\u2013": "-",
        "\u2212": "-",
        "\u2192": "->",
        "\u2190": "<-",
        "\u2194": "<->",
        "\u2026": "...",
        "\u00a0": " ",
        "\u2022": "-",
        "\u00b7": "-",
    }
)

_GENERIC_NOTE_LABELS = frozenset(
    {
        "mistake",
        "question",
        "good play",
        "opp habit",
        "note",
        "moment",
        "select mark",
        "set takeaway",
        "pro clip",
        "top-rank clip",
        "query sequence",
        "replay takeover",
    }
)
_GENERIC_NOTE_PATTERNS = (
    re.compile(r"^manual selection gf\d+", re.IGNORECASE),
    re.compile(r"^in-game select mark #\d+\b", re.IGNORECASE),
    re.compile(r"^marked live during match\b", re.IGNORECASE),
)


def sanitize_note(note: Any, max_len: int = 600) -> str:
    """Normalize coaching notes for FBNeo's ASCII-only `gui.text`, preserving line breaks."""
    if note is None:
        return ""
    text = str(note).replace("\r\n", "\n").replace("\r", "\n")
    text = text.translate(_NOTE_UNICODE_TRANSLATIONS)
    text = text.replace("\t", " ")
    # Keep only printable ASCII (0x20..0x7E) and newlines
    text = "".join(ch for ch in text if ch == "\n" or 32 <= ord(ch) <= 126)

    cleaned_lines: list[str] = []
    blank_run = 0
    for raw_line in text.split("\n"):
        line = " ".join(raw_line.split())
        if not line:
            blank_run += 1
            if blank_run <= 1 and cleaned_lines:
                cleaned_lines.append("")
        else:
            blank_run = 0
            cleaned_lines.append(line)

    while cleaned_lines and not cleaned_lines[-1]:
        cleaned_lines.pop()

    result = "\n".join(cleaned_lines).strip()
    if len(result) > max_len:
        result = result[:max_len].rstrip()
    return result


def encode_env_note(note: str) -> str:
    """Encode a sanitized multiline note for single-line Windows env var transport (`FC_NOTE`)."""
    if not note:
        return ""
    return note.replace("\\", "\\\\").replace("\n", "\\n")


def _is_generic_fallback_label(text: str) -> bool:
    s = " ".join(str(text or "").split()).strip()
    if not s:
        return True
    low = s.lower()
    if low in _GENERIC_NOTE_LABELS:
        return True
    return any(pat.match(s) for pat in _GENERIC_NOTE_PATTERNS)


def validate_takeover_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate & normalize a JSON takeover launch payload. Raises ValueError on bad input."""
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")

    selection_id = str(payload.get("selection_id") or "").strip()
    if selection_id and not _SELECTION_ID_RE.match(selection_id):
        raise ValueError("Invalid selection_id format")

    quark_id = str(payload.get("quark_id") or "").strip()
    if quark_id and not _QUARK_ID_RE.match(quark_id):
        raise ValueError("Invalid quark_id format")

    frame_raw = payload.get("frame", 0)
    start_gf_raw = payload.get("start_gf", 0)
    end_gf_raw = payload.get("end_gf", 0)
    try:
        frame = int(frame_raw) if frame_raw not in (None, "") else 0
        start_gf = int(start_gf_raw) if start_gf_raw not in (None, "") else 0
        end_gf = int(end_gf_raw) if end_gf_raw not in (None, "") else 0
    except (TypeError, ValueError) as exc:
        raise ValueError("frame, start_gf, and end_gf must be integers") from exc

    if frame <= 0 and start_gf > 0:
        frame = start_gf

    if not selection_id:
        if not quark_id:
            raise ValueError("Provide quark_id + frame (or selection_id)")
        if frame < 1 or frame > 10_000_000:
            raise ValueError("frame must be between 1 and 10000000")

    bake_if_missing = bool(payload.get("bake_if_missing", False))

    try:
        player = int(payload.get("player", 1))
    except (TypeError, ValueError) as exc:
        raise ValueError("player must be 1 or 2") from exc
    if player not in (1, 2):
        raise ValueError("player must be 1 or 2")

    mode = str(payload.get("mode") or "takeover").strip().lower()
    if mode not in ("takeover", "watch"):
        raise ValueError("mode must be 'takeover' or 'watch'")

    game_id = str(payload.get("game_id") or "sfiii3nr1").strip()
    if not _GAME_ID_RE.match(game_id):
        raise ValueError("Invalid game_id format")

    try:
        lead_frames = int(payload.get("lead_frames", 120))
        takeover_lead = int(payload.get("takeover_lead", 30))
    except (TypeError, ValueError) as exc:
        raise ValueError("lead_frames and takeover_lead must be integers") from exc
    if not (0 <= lead_frames <= 600) or not (0 <= takeover_lead <= 300):
        raise ValueError("lead_frames/takeover_lead out of bounds")

    auto_loop = bool(payload.get("auto_loop", True))
    label = sanitize_label(payload.get("label", ""))

    if "note" in payload:
        note = sanitize_note(payload.get("note"))
    elif label and not _is_generic_fallback_label(label):
        note = sanitize_note(label)
    else:
        note = ""

    speed_raw = payload.get("speed", 1.0)
    try:
        speed = float(speed_raw) if speed_raw not in (None, "") else 1.0
    except (TypeError, ValueError) as exc:
        raise ValueError("speed must be a number") from exc
    if speed > 2.0:
        speed = speed / 100.0
    if not (0.10 <= speed <= 2.00):
        raise ValueError("speed must be between 0.10 and 2.00 (or 10..200%)")

    speed_scope = str(payload.get("speed_scope") or "").strip().lower()
    if speed_scope not in ("", "all", "takeover_only"):
        raise ValueError("speed_scope must be 'all' or 'takeover_only'")

    loop_delay_raw = payload.get("loop_delay", None)
    loop_delay: int | None = None
    if loop_delay_raw not in (None, ""):
        try:
            loop_delay = int(loop_delay_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("loop_delay must be an integer between 0 and 10") from exc
        if not (0 <= loop_delay <= 10):
            raise ValueError("loop_delay must be between 0 and 10 seconds")

    start_paused_raw = payload.get("start_paused", None)
    start_paused: bool | None = None
    if start_paused_raw not in (None, ""):
        if isinstance(start_paused_raw, bool):
            start_paused = start_paused_raw
        elif isinstance(start_paused_raw, (int, float)):
            start_paused = bool(start_paused_raw)
        elif isinstance(start_paused_raw, str) and start_paused_raw.strip().lower() in ("1", "true", "yes", "0", "false", "no"):
            start_paused = start_paused_raw.strip().lower() in ("1", "true", "yes")
        else:
            raise ValueError("start_paused must be a boolean")

    parent_moment_id = str(payload.get("parent_moment_id") or "").strip()
    if parent_moment_id and not _SELECTION_ID_RE.match(parent_moment_id):
        raise ValueError("Invalid parent_moment_id format")

    round_id_raw = payload.get("round_id", 0)
    try:
        round_id = int(round_id_raw) if round_id_raw not in (None, "") else 0
    except (TypeError, ValueError) as exc:
        raise ValueError("round_id must be an integer") from exc
    if round_id < 0 or round_id > 999:
        raise ValueError("round_id must be between 1 and 999")

    return {
        "quark_id": quark_id,
        "frame": frame,
        "start_gf": start_gf,
        "end_gf": end_gf,
        "bake_if_missing": bake_if_missing,
        "player": player,
        "mode": mode,
        "selection_id": selection_id,
        "parent_moment_id": parent_moment_id,
        "round_id": round_id,
        "game_id": game_id,
        "lead_frames": lead_frames,
        "takeover_lead": takeover_lead,
        "auto_loop": auto_loop,
        "label": label,
        "note": note,
        "speed": round(speed, 2),
        "speed_scope": speed_scope,
        "loop_delay": loop_delay,
        "start_paused": start_paused,
    }


def _ensure_within_data_dir(path: Path, data_dir: Path, label: str) -> Path:
    """Ensure a file path loaded from local JSONL metadata stays strictly inside `data_dir`."""
    resolved = path.resolve()
    root = data_dir.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} path escapes data_dir: {path}") from exc
    return resolved


def _load_bookmark(selection_id: str, data_dir: Path) -> dict[str, Any]:
    sel_dir = data_dir / "selections"
    candidates = [sel_dir / "queue.jsonl", sel_dir / "bookmarks.jsonl", sel_dir / "annotations.jsonl"]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        raise FileNotFoundError(f"No selections found in: {sel_dir}")
    match: dict[str, Any] | None = None
    for bm_path in existing:
        for line in bm_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(obj.get("id", "")) == selection_id:
                match = obj
        if match is not None:
            return match
    raise FileNotFoundError(f"SelectionId '{selection_id}' not found in {sel_dir}")


class TakeoverManager:
    """Manages launching interactive `fcadefbneo.exe` Replay Takeover sessions from the browser."""

    def __init__(
        self,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        fbneo_exe: Path = DEFAULT_FBNEO_EXE,
        replay_takeover_lua: Path = DEFAULT_REPLAY_TAKEOVER_LUA,
        seek_lua: Path = DEFAULT_SEEK_LUA,
        popen_fn: Callable[..., Any] = subprocess.Popen,
        bake_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self.fbneo_exe = fbneo_exe
        self.replay_takeover_lua = replay_takeover_lua.resolve()
        self.seek_lua = seek_lua.resolve()
        self._popen_fn = popen_fn
        self._bake_fn = bake_fn
        self._lock = threading.Lock()
        self._active_proc: Any | None = None

    def active_pid(self) -> int | None:
        with self._lock:
            if self._active_proc is not None and self._active_proc.poll() is None:
                return int(self._active_proc.pid)
            return None

    def _stop_previous_locked(self) -> bool:
        proc = self._active_proc
        if proc is None or proc.poll() is not None:
            self._active_proc = None
            return False
        try:
            proc.terminate()
            proc.wait(timeout=1.5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
                proc.wait(timeout=1.5)
            except Exception:  # noqa: BLE001
                pass
        self._active_proc = None
        return True

    def launch(self, raw_payload: dict[str, Any]) -> dict[str, Any]:
        req = validate_takeover_payload(raw_payload)

        launch_exe = resolve_takeover_fbneo_exe(self.fbneo_exe)
        if not launch_exe.exists():
            raise FileNotFoundError(f"fcadefbneo executable not found at: {launch_exe}")

        quark_id = req["quark_id"]
        frame = req["frame"]
        player = req["player"]
        mode = req["mode"]
        label = req["label"]
        note = req["note"]
        parent_moment_id = req["parent_moment_id"]
        round_id = req["round_id"]
        explicit_note_provided = "note" in raw_payload
        explicit_takeover_gf = 0
        savestate_override: Path | None = None
        inputs_override: Path | None = None

        if req["selection_id"]:
            bm = _load_bookmark(req["selection_id"], self.data_dir)
            quark_id = str(bm["quark_id"])
            frame = int(bm["start_gf"])
            player = int(bm.get("perspective", player))
            if not round_id and bm.get("round_id"):
                try:
                    round_id = int(bm["round_id"])
                except (TypeError, ValueError):
                    round_id = 0
            if not parent_moment_id:
                if str(bm.get("source") or "") == "takeover_response":
                    parent_moment_id = str(bm.get("parent_moment_id") or "").strip()
                else:
                    parent_moment_id = req["selection_id"]
            if bm.get("savestate"):
                savestate_override = _ensure_within_data_dir(
                    Path(str(bm["savestate"])), self.data_dir, "savestate"
                )
            if bm.get("inputs"):
                inputs_override = _ensure_within_data_dir(
                    Path(str(bm["inputs"])), self.data_dir, "inputs"
                )
            bm_note_raw = str(bm.get("note") or "").strip()
            if not explicit_note_provided and not note:
                bm_note_clean = sanitize_note(bm_note_raw)
                if bm_note_clean and not _is_generic_fallback_label(bm_note_clean):
                    note = bm_note_clean
            if not label:
                suffix = f" - {bm_note_raw}" if bm_note_raw else ""
                label = sanitize_label(f"Manual Selection gf{bm['start_gf']}..gf{bm['end_gf']}{suffix}")
            explicit_takeover_gf = int(bm.get("takeover_gf") or (frame + 20))

        savestates_dir = self.data_dir / "savestates"
        cached_fs = savestate_override or (savestates_dir / f"{quark_id}_gf{frame}.fs")
        inputs_file = inputs_override or (savestates_dir / f"{quark_id}_gf{frame}.inputs")

        # Ad-hoc offline `<2s` `.fs` bake for lazy-bake "▶ Practice" when replay_cache exists.
        # Keyed by BOTH start_gf and end_gf so editing a Moment's range never reuses a stale `.fs`/`.inputs`.
        baked_ad_hoc = False
        has_explicit_range = (
            not req["selection_id"]
            and int(req.get("start_gf") or 0) > 0
            and int(req.get("end_gf") or 0) > int(req.get("start_gf") or 0)
        )
        if req.get("bake_if_missing") and quark_id and (has_explicit_range or not cached_fs.exists()):
            s_gf = int(req.get("start_gf") or 0) or max(1, frame - int(req.get("lead_frames") or 120))
            e_gf = int(req.get("end_gf") or 0) or (frame + 90)
            if e_gf <= s_gf:
                e_gf = s_gf + 150
            adhoc_id = f"adhoc_{quark_id.split('-')[-1]}_gf{s_gf}_{e_gf}"
            adhoc_fs = self.data_dir / "selections" / f"{adhoc_id}.fs"
            adhoc_inp = self.data_dir / "selections" / f"{adhoc_id}.inputs"
            if adhoc_fs.exists() and adhoc_fs.stat().st_size > 4096:
                cached_fs = adhoc_fs
                inputs_file = adhoc_inp
                explicit_takeover_gf = max(s_gf + 20, frame - int(req.get("takeover_lead") or 30)) if frame > s_gf + 30 else (s_gf + 20)
            else:
                from fightcade_analysis.round_renderer import bake_drill_savestate, list_cached_round_savestates

                if list_cached_round_savestates(quark_id, data_dir=self.data_dir):
                    baker = self._bake_fn or bake_drill_savestate
                    try:
                        baked = baker(
                            quark_id,
                            s_gf,
                            e_gf,
                            selection_id=adhoc_id,
                            data_dir=self.data_dir,
                        )
                        b_fs = Path(str(baked.get("savestate") or adhoc_fs))
                        b_inp = Path(str(baked.get("inputs") or adhoc_inp))
                        if b_fs.exists():
                            cached_fs = b_fs
                            inputs_file = b_inp
                            explicit_takeover_gf = (
                                max(s_gf + 20, frame - int(req.get("takeover_lead") or 30))
                                if frame > s_gf + 30
                                else (s_gf + 20)
                            )
                            baked_ad_hoc = True
                    except Exception:  # noqa: BLE001
                        pass

        ensure_fbneo_config_inheritance(launch_exe, game_id=req["game_id"], data_dir=self.data_dir)
        env = os.environ.copy()
        if cached_fs.exists():
            lua_path = (
                resolve_lua_script("replay_takeover.lua", game_id=req["game_id"], data_dir=self.data_dir)
                if self.replay_takeover_lua == DEFAULT_REPLAY_TAKEOVER_LUA.resolve()
                else self.replay_takeover_lua
            )
            if not lua_path.exists():
                raise FileNotFoundError(f"Lua script not found: {lua_path}")
            fs_str = str(cached_fs.resolve())

            if not inputs_file.exists():
                export_takeover_inputs(
                    quark_id,
                    frame,
                    inputs_file,
                    data_dir=self.data_dir,
                )

            export_dir = (self.data_dir / "selections" / "takeover_exports").resolve()
            export_dir.mkdir(parents=True, exist_ok=True)
            env["FC_INPUTS_PATH"] = str(inputs_file.resolve()) if inputs_file.exists() else ""
            env["FC_SAVESTATE_PATH"] = fs_str
            env["FC_TARGET_FRAME"] = str(frame)
            env["FC_TAKEOVER_GF"] = str(explicit_takeover_gf)
            env["FC_USER_PLAYER"] = str(player)
            env["FC_TAKEOVER_LEAD"] = str(req["takeover_lead"])
            env["FC_AUTO_LOOP"] = "1" if req["auto_loop"] else "0"
            env["FC_WATCH_ONLY"] = "1" if mode == "watch" else "0"
            env["FC_LABEL"] = label
            env["FC_NOTE"] = encode_env_note(note)
            env["FC_PARENT_MOMENT_ID"] = parent_moment_id
            env["FC_QUARK_ID"] = quark_id
            env["FC_ROUND_ID"] = str(round_id) if round_id > 0 else ""
            env["FC_EXPORT_DIR"] = str(export_dir)
            env["FC_SPEED"] = f"{req['speed']:.2f}"
            if req["speed_scope"]:
                env["FC_SPEED_SCOPE"] = req["speed_scope"]
            if req["loop_delay"] is not None:
                env["FC_LOOP_DELAY"] = str(req["loop_delay"])
            if req["start_paused"] is not None:
                env["FC_START_PAUSED"] = "1" if req["start_paused"] else "0"
            g_dir = resolve_grouflon_dir(launch_exe)
            if g_dir is not None:
                env["FC_GROUFLON_TRAINING"] = "1"
                env["FC_GROUFLON_DIR"] = str(g_dir)
                sel_dir = self.data_dir / "selections"
                sel_dir.mkdir(parents=True, exist_ok=True)
                env["FC_TRAINING_SETTINGS"] = str((sel_dir / "takeover_training.cfg").resolve())
            else:
                env["FC_GROUFLON_TRAINING"] = "0"

            cmd_str = build_fbneo_cmdline(
                launch_exe,
                ["-w"],
                cached_fs.resolve(),
                lua_path,
                target_is_path=True,
            )
            method = "instant_savestate"
        else:
            lua_path = (
                resolve_lua_script("seek_and_takeover.lua", game_id=req["game_id"], data_dir=self.data_dir)
                if self.seek_lua == DEFAULT_SEEK_LUA.resolve()
                else self.seek_lua
            )
            if not lua_path.exists():
                raise FileNotFoundError(f"Lua script not found: {lua_path}")
            stream_target = lookup_cached_stream_target(quark_id, data_dir=self.data_dir)
            if not stream_target:
                remote_meta = lookup_remote_fightcade_meta(quark_id)
                if remote_meta and remote_meta.get("stream_target"):
                    stream_target = str(remote_meta["stream_target"])
            if not stream_target:
                stream_target = resolve_stream_target(req["game_id"], quark_id)
            stream_target = validate_stream_target(str(stream_target or ""))

            savestates_dir.mkdir(parents=True, exist_ok=True)
            env["FC_TARGET_FRAME"] = str(frame)
            env["FC_LEAD_FRAMES"] = str(req["lead_frames"])
            env["FC_TAKEOVER_MODE"] = mode
            env["FC_LABEL"] = label
            env["FC_NOTE"] = encode_env_note(note)
            env["FC_SAVESTATE_PATH"] = str(cached_fs)

            cmd_str = build_fbneo_cmdline(
                launch_exe,
                ["-w"],
                stream_target,
                lua_path,
                target_is_path=False,
            )
            method = "stream_seek"

        popen_kwargs: dict[str, Any] = {
            "cwd": str(launch_exe.parent),
            "env": env,
        }
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 1  # SW_SHOWNORMAL
            popen_kwargs["startupinfo"] = si

        with self._lock:
            replaced = self._stop_previous_locked()
            proc = self._popen_fn(cmd_str, **popen_kwargs)
            self._active_proc = proc

        desktop_name, is_interactive = get_windows_desktop_name()
        warning = None
        if not is_interactive:
            warning = (
                f"Server is running on hidden Windows desktop '{desktop_name}', so FBNeo's window will not be visible. "
                "Stop this server and start .\\scripts\\serve_reports.ps1 from your own PowerShell terminal."
            )

        return {
            "ok": True,
            "pid": int(proc.pid),
            "method": method,
            "baked_ad_hoc": baked_ad_hoc,
            "exe": launch_exe.name,
            "quark_id": quark_id,
            "frame": frame,
            "player": player,
            "mode": mode,
            "speed": req["speed"],
            "label": label,
            "note": note,
            "replaced_previous": replaced,
            "desktop": desktop_name,
            "interactive_desktop": is_interactive,
            "warning": warning,
        }


# ---------------------------------------------------------------------------
# Shared HTML / CSS / JS helpers for all report templates
# ---------------------------------------------------------------------------

def render_takeover_buttons_html(
    quark_id: str,
    frame: int,
    player: int,
    label: str,
    copy_cmd: str,
    *,
    is_pro: bool = False,
    selection_id: str | None = None,
) -> str:
    """Render primary `Launch Takeover` button + secondary `Copy` fallback button."""
    btn_text = "&#9654; Launch Top-Rank Takeover" if is_pro else "&#9654; Launch Takeover"
    sel_attr = f' data-selection-id="{html.escape(selection_id, quote=True)}"' if selection_id else ""
    safe_label = sanitize_label(label)
    return (
        f'<span class="takeover-btns">'
        f'<button type="button" class="launch-btn" '
        f'data-qid="{html.escape(str(quark_id), quote=True)}" '
        f'data-frame="{int(frame)}" '
        f'data-player="{int(player)}" '
        f'data-label="{html.escape(safe_label, quote=True)}"'
        f'{sel_attr} '
        f'data-copy="{html.escape(copy_cmd, quote=True)}" '
        f'onclick="launchTakeover(this)">{btn_text}</button>'
        f'<button type="button" class="copy-btn" title="Copy Takeover command" '
        f'data-copy="{html.escape(copy_cmd, quote=True)}" '
        f'onclick="copyFromData(this)">Copy</button>'
        f'</span>'
    )


TAKEOVER_UI_CSS = """
  .takeover-btns { display: inline-flex; align-items: center; gap: 5px; flex-wrap: nowrap; }
  .launch-btn {
    background: linear-gradient(135deg, #0ea5e9, #10b981);
    color: #04131c; border: 1px solid rgba(56, 189, 248, 0.55); border-radius: 5px;
    padding: 4px 10px; cursor: pointer; font-size: 0.76rem; font-weight: 700; white-space: nowrap;
    transition: filter 0.12s ease, opacity 0.12s ease;
  }
  .launch-btn:hover { filter: brightness(1.12); }
  .launch-btn:disabled { opacity: 0.7; cursor: wait; }
  .launch-btn.launched { background: #16a34a; color: #fff; border-color: #4ade80; }
  .launch-btn.err { background: #b91c1c; color: #fff; border-color: #f87171; }
  .copy-btn { background: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 5px; padding: 4px 8px; cursor: pointer; font-size: 0.74rem; white-space: nowrap; }
  .copy-btn:hover { border-color: #38bdf8; color: #fff; }
  #fc-toast {
    position: fixed; right: 18px; bottom: 18px; max-width: 460px; z-index: 9999;
    background: #0f172a; color: #e2e8f0; border: 1px solid #38bdf8; border-radius: 8px;
    padding: 11px 14px; font-size: 0.82rem; line-height: 1.4;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.45); display: none;
  }
  #fc-toast.ok { border-color: #4ade80; }
  #fc-toast.warn { border-color: #f59e0b; color: #fef3c7; }
  #fc-toast.err { border-color: #f87171; color: #fee2e2; }
"""


TAKEOVER_UI_JS = """
let _fcToastTimer = null;
function showTakeoverToast(msg, kind) {
  let el = document.getElementById('fc-toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'fc-toast';
    document.body.appendChild(el);
  }
  el.className = kind || 'ok';
  el.textContent = msg;
  el.style.display = 'block';
  if (_fcToastTimer) clearTimeout(_fcToastTimer);
  _fcToastTimer = setTimeout(() => { el.style.display = 'none'; }, kind === 'ok' ? 4200 : 7500);
}
function _copyTextSilent(text) {
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => {});
  } else {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
  }
}
async function launchTakeover(btn) {
  const origHtml = btn.dataset.origHtml || btn.innerHTML;
  btn.dataset.origHtml = origHtml;
  const copyCmd = btn.getAttribute('data-copy') || '';

  if (window.location.protocol === 'file:') {
    _copyTextSilent(copyCmd);
    showTakeoverToast('Opened via file:// — run .\\\\scripts\\\\serve_reports.ps1 and open http://127.0.0.1:8765/ for 1-click launch. Command copied to clipboard!', 'warn');
    return;
  }

  const payload = {
    quark_id: btn.getAttribute('data-qid') || '',
    frame: parseInt(btn.getAttribute('data-frame') || '0', 10),
    player: parseInt(btn.getAttribute('data-player') || '1', 10),
    label: btn.getAttribute('data-label') || '',
    selection_id: btn.getAttribute('data-selection-id') || '',
    mode: 'takeover'
  };

  btn.disabled = true;
  btn.classList.remove('launched', 'err');
  btn.textContent = 'Launching...';
  try {
    const resp = await fetch('/api/takeover', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.ok) {
      throw new Error(data.error || ('HTTP ' + resp.status));
    }
    btn.classList.add('launched');
    btn.textContent = data.warning ? 'Launched (check server)' : 'Launched!';
    if (data.warning) {
      showTakeoverToast(data.warning, 'warn');
    } else {
      const via = data.method === 'instant_savestate' ? 'Instant <1s savestate' : 'Stream seek';
      showTakeoverToast(via + ' Replay Takeover launched (P' + data.player + ') — Select Retry · Alt+6/7/8 Speed · Alt+5 Sandbox · Start Menu', 'ok');
    }
    setTimeout(() => {
      btn.classList.remove('launched');
      btn.innerHTML = origHtml;
      btn.disabled = false;
    }, 2400);
  } catch (err) {
    btn.classList.add('err');
    btn.textContent = 'Launch failed';
    _copyTextSilent(copyCmd);
    showTakeoverToast('Could not launch takeover (' + (err && err.message ? err.message : err) + '). Command copied to clipboard.', 'err');
    setTimeout(() => {
      btn.classList.remove('err');
      btn.innerHTML = origHtml;
      btn.disabled = false;
    }, 2600);
  }
}
"""
