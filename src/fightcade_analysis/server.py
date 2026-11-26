"""Lightweight LAN/VPN HTTP server for Fightcade Lab, 60fps MP4 clips & Replay Review Studio.

Features:
  - Serves the unified Fightcade Lab web app directly at `/` (with `/review` and `/mobile` aliases)
    plus strictly allowlisted static frontend assets at `/assets/<file>` from `web/`.
  - Full HTTP `206 Partial Content` / `Accept-Ranges: bytes` support so MP4 clips & per-round
    replays stream & scrub cleanly on iOS Safari, Android Chrome, and desktop browsers over VPN/LAN.
  - Strictly scoped static file access to `data/reports/`, `data/clips/`, and `data/replays/`
    (blocks `/parquet/`, `/personal/`, `/savestates/`, `/selections/`, `/index/`, manifests, and traversal).
  - Exposes Capabilities, Home Workbench aggregate, Match History Catalog, Netplay-Safe ProcessingQueue,
    Moment Annotations, Centralized Notes Hub, Focus Goals, Playlists, Insights, Pro Search & Takeover APIs.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import errno
import gzip
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from fightcade_analysis.annotations import (
    create_annotation,
    delete_annotation,
    get_set_note,
    list_annotations,
    update_annotation,
    upsert_set_note,
)
from fightcade_analysis.clip_renderer import (
    DEFAULT_DATA_DIR,
    DEFAULT_FBNEO_EXE,
    resolve_capture_fbneo_exe,
    resolve_takeover_fbneo_exe,
)
from fightcade_analysis.env_config import (
    get_disk_cache_cap_gb,
    inspect_environment,
    is_pro_corpus_opt_in,
    save_app_config,
)
from fightcade_analysis.fetch_replays import FightcadeRateLimitError
from fightcade_analysis.focus_goals import (
    MAX_ACTIVE_GOALS,
    create_focus_goal,
    delete_focus_goal,
    list_focus_goals,
    update_focus_goal,
)
from fightcade_analysis.gemini_summary import load_gemini_api_key
from fightcade_analysis.insights import InsightJobRunner, ProSearchService, get_insight_report, list_insights
from fightcade_analysis.match_catalog import (
    HourlyBackfillCapError,
    ProcessingQueue,
    RefreshCooldownError,
    detect_default_username,
    list_matches,
    refresh_match_history,
)
from fightcade_analysis.mobile_ui import (
    WEB_ASSETS_DIR,
    build_mobile_app_html,
    build_pwa_manifest_json,
)
from fightcade_analysis.notes_hub import _DEFAULT_SELECT_NOTE_RE, list_collated_notes
from fightcade_analysis.playlists import (
    create_playlist,
    delete_playlist,
    ensure_playlist_drills,
    list_playlists,
    mutate_playlist_items,
    update_playlist,
)
from fightcade_analysis.round_renderer import (
    ensure_manifest_matches,
    generate_replay_overlays,
    get_mobile_replays_disk_usage,
    mobile_manifest_path,
    prune_old_mobile_replays,
)
from fightcade_analysis.select_queue import (
    DEFAULT_LOOKBACK_FRAMES,
    LiveMatchWatcher,
    clear_done_items,
    load_queue,
    update_queue_item,
    update_queue_item_status,
)
from fightcade_analysis.asset_installer import get_onboarding_status, install_fast_fbneo, update_onboarding_state
from fightcade_analysis.companion_bridge import (
    build_pna_cors_headers,
    complete_pairing,
    extract_bearer_or_header_token,
    get_pairing_state,
    is_allowed_companion_origin,
    is_cross_origin_allowed_web,
    issue_pairing_code,
    revoke_pairing,
    verify_pairing_token,
)
from fightcade_analysis.share_export import prepare_share_bundle
from fightcade_analysis.takeover import TakeoverManager, get_windows_desktop_name
from fightcade_analysis.uri_scheme import is_uri_scheme_registered, register_uri_scheme

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_ALLOWED_ROOTS = frozenset({"reports", "clips", "replays", "share_exports"})
_ALLOWED_ASSET_EXTS = frozenset({".css", ".js", ".svg", ".png", ".ico", ".json", ".webmanifest", ".map"})
_BLOCKED_FILENAMES = frozenset({"gemini_summary_cache.json"})
_MAX_API_BODY_BYTES = 65536
_REPLAY_MANIFEST_API_RE = re.compile(r"^/api/replays/([A-Za-z0-9_-]{3,64})/manifest$")
_REPLAY_OVERLAYS_API_RE = re.compile(r"^/api/replays/([A-Za-z0-9_-]{3,64})/overlays$")
_ANN_ITEM_API_RE = re.compile(r"^/api/annotations/([A-Za-z0-9_-]{3,128})$")
_QUEUE_ITEM_API_RE = re.compile(r"^/api/queue/([A-Za-z0-9_-]{3,128})$")
_GOAL_ITEM_API_RE = re.compile(r"^/api/focus-goals/([A-Za-z0-9_-]{3,128})$")
_PLAYLIST_ITEM_API_RE = re.compile(r"^/api/playlists/([A-Za-z0-9_-]{3,128})$")
_PLAYLIST_ITEMS_API_RE = re.compile(r"^/api/playlists/([A-Za-z0-9_-]{3,128})/items$")
_PLAYLIST_DRILLS_API_RE = re.compile(r"^/api/playlists/([A-Za-z0-9_-]{3,128})/ensure-drills$")
_INSIGHT_ITEM_API_RE = re.compile(r"^/api/insights/([A-Za-z0-9_.-]{3,160})$")
_REPORT_HTML_RE = re.compile(r"^/reports/([A-Za-z0-9_.-]{3,160})\.html$")
_SHARED_MOMENT_PAGE_RE = re.compile(r"^/m/([A-Za-z0-9_-]{3,128})/?$")
_SHARED_PLAYLIST_PAGE_RE = re.compile(r"^/p/([A-Za-z0-9_-]{3,128})/?$")

RELOAD_EXIT_CODE = 75
RELOAD_TRIGGER_FILENAME = ".reload"


class ReloadTriggerWatcher:
    """Watches `data/.reload` so external tooling can request a clean restart inside `serve_reports.ps1`.

    Defers shutdown while any replay scan/render/insight job is actively running, deletes the trigger file,
    and shuts down the HTTP server so the parent interactive PowerShell supervisor respawns it
    with `WinSta0\\Default` intact.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        shutdown_fn: Callable[[], None],
        is_busy_fn: Callable[[], bool] | None = None,
        poll_interval: float = 0.5,
    ) -> None:
        self.trigger_path = data_dir.resolve() / RELOAD_TRIGGER_FILENAME
        self._shutdown_fn = shutdown_fn
        self._is_busy_fn = is_busy_fn or (lambda: False)
        self._poll_interval = poll_interval
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self.reload_requested = False

    def start(self) -> None:
        try:
            self.trigger_path.unlink(missing_ok=True)
        except OSError:
            pass
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, name="fc-reload-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        logged_busy = False
        while not self._stop_evt.is_set():
            if self.trigger_path.exists():
                if self._is_busy_fn():
                    if not logged_busy:
                        print("[server] Reload requested via data/.reload — waiting for active job to finish...")
                        logged_busy = True
                else:
                    try:
                        self.trigger_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                    self.reload_requested = True
                    print("[server] Reload trigger detected (data/.reload) — restarting cleanly...")
                    self._shutdown_fn()
                    return
            self._stop_evt.wait(self._poll_interval)


def _parse_byte_range(range_header: str, file_size: int) -> tuple[int, int] | None:
    """Parse a single HTTP `Range: bytes=start-end` header into inclusive `(start, end)`."""
    if file_size <= 0:
        return None
    m = _RANGE_RE.match(range_header.strip())
    if not m:
        return None
    start_s, end_s = m.group(1), m.group(2)
    if not start_s and not end_s:
        return None
    if not start_s:
        suffix = int(end_s)
        if suffix <= 0:
            return None
        start = max(0, file_size - suffix)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else (file_size - 1)
    if start >= file_size or start > end:
        return None
    end = min(end, file_size - 1)
    return start, end


def resolve_safe_data_path(url_path: str, data_dir: Path) -> Path | None:
    """Resolve URL path strictly inside `data_dir/{reports,clips,replays}` or return None if forbidden."""
    clean = unquote(urlsplit(url_path).path).lstrip("/")
    if not clean:
        return None
    parts = Path(clean).parts
    if not parts or parts[0] not in _ALLOWED_ROOTS:
        return None
    if any(p in ("..", "") for p in parts):
        return None
    if parts[-1] in _BLOCKED_FILENAMES:
        return None

    data_root = data_dir.resolve()
    candidate = (data_root / Path(*parts)).resolve()
    try:
        candidate.relative_to(data_root / parts[0])
    except ValueError:
        return None
    return candidate


def resolve_safe_asset_path(url_path: str, web_dir: Path = WEB_ASSETS_DIR) -> Path | None:
    """Resolve `/assets/<subpath>` strictly inside `web_dir` with allowlisted static extensions."""
    raw = unquote(urlsplit(url_path).path)
    if not raw.startswith("/assets/"):
        return None
    rel = raw[len("/assets/"):].lstrip("/")
    if not rel or "\\" in rel:
        return None
    parts = Path(rel).parts
    if not parts or any(p in ("..", ".", "") for p in parts):
        return None
    root = web_dir.resolve()
    candidate = (root / Path(*parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate.suffix.lower() not in _ALLOWED_ASSET_EXTS:
        return None
    return candidate


def compute_capabilities(
    data_dir: Path,
    *,
    watcher: LiveMatchWatcher | None = None,
    fbneo_exe: Path = DEFAULT_FBNEO_EXE,
    pro_corpus_enabled_override: bool | None = None,
) -> dict[str, Any]:
    """Build `/api/capabilities` payload gating local-companion vs study-only & public features."""
    data_root = data_dir.resolve()
    launch_exe = resolve_takeover_fbneo_exe(fbneo_exe)
    can_takeover = bool(sys.platform == "win32" and launch_exe.exists())
    _, is_interactive = get_windows_desktop_name()
    cap_exe, _ = resolve_capture_fbneo_exe(fbneo_exe)
    can_render = bool(sys.platform == "win32" and (cap_exe.exists() or fbneo_exe.exists()))
    pq_dir = data_root / "parquet"
    pro_sets = len(list(pq_dir.glob("*.parquet"))) if pq_dir.is_dir() else 0
    opt_in = is_pro_corpus_opt_in(data_root)
    if pro_corpus_enabled_override is not None:
        pro_corpus_enabled = bool(pro_corpus_enabled_override)
    else:
        pro_corpus_enabled = bool(opt_in and pro_sets > 0)

    env_info = inspect_environment(data_root, fbneo_exe=fbneo_exe)
    has_grouflon = bool(env_info.get("has_grouflon", False))
    disk_cache = get_mobile_replays_disk_usage(data_root)
    w_status = watcher.status() if watcher is not None else {}
    return {
        "ok": True,
        "mode": "local_companion",
        "owner": detect_default_username(data_root),
        "can_takeover": can_takeover,
        "interactive_desktop": bool(is_interactive),
        "can_render": can_render,
        "can_generate_overlays": bool(can_render and has_grouflon),
        "has_grouflon": has_grouflon,
        "grouflon_dir": env_info.get("grouflon_dir"),
        "watcher_active": bool(w_status.get("enabled", w_status.get("running", False))),
        "has_gemini": bool(load_gemini_api_key()),
        "has_pro_corpus": pro_sets > 0,
        "pro_corpus_sets": pro_sets,
        "pro_corpus_opt_in": opt_in,
        "pro_corpus_enabled": pro_corpus_enabled,
        "disk_cache": disk_cache,
        "environment": env_info,
    }


def _select_marks_for_replay(qid: str, data_root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for qi in load_queue(data_root, include_dismissed=False):
        if str(qi.get("quark_id")) != str(qid):
            continue
        if qi.get("source") == "mobile_note":
            continue
        raw_note = str(qi.get("note") or "").strip()
        is_default = (not raw_note) or bool(_DEFAULT_SELECT_NOTE_RE.match(raw_note))
        anchor_gf = int(qi.get("press_gf") or qi.get("end_gf") or qi.get("start_gf") or 0)
        item = dict(qi)
        item["anchor_gf"] = anchor_gf
        item["needs_note"] = is_default
        item["is_default_select_note"] = is_default
        out.append(item)
    out.sort(key=lambda x: (int(x.get("round_id") or 0), int(x.get("anchor_gf") or 0)))
    return out


def make_handler(
    data_dir: Path,
    *,
    auto_refresh_index: bool = True,
    takeover_launcher: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    takeover_manager: TakeoverManager | None = None,
    match_watcher: LiveMatchWatcher | None = None,
    processing_queue: ProcessingQueue | None = None,
    insight_runner: InsightJobRunner | None = None,
    pro_search_service: ProSearchService | None = None,
    capabilities_fn: Callable[[], dict[str, Any]] | None = None,
    refresh_history_fn: Callable[..., dict[str, Any]] = refresh_match_history,
    bake_drill_fn: Callable[..., dict[str, Any]] | None = None,
    async_bake_drills: bool = True,
    pro_corpus_enabled: bool | None = None,
) -> type[BaseHTTPRequestHandler]:
    data_root = data_dir.resolve()
    reports_dir = data_root / "reports"
    manager = takeover_manager or TakeoverManager(data_dir=data_root, bake_fn=bake_drill_fn)
    launcher = takeover_launcher or manager.launch
    watcher = match_watcher or LiveMatchWatcher(data_dir=data_root)
    proc_queue = processing_queue or ProcessingQueue(
        data_dir=data_root,
        is_live_match_fn=watcher.is_in_match,
    )
    ins_runner = insight_runner or InsightJobRunner(
        data_dir=data_root,
        is_live_match_fn=watcher.is_in_match,
    )
    pro_svc = pro_search_service or ProSearchService(
        data_dir=data_root,
        is_live_match_fn=watcher.is_in_match,
    )
    get_caps = capabilities_fn or (
        lambda: compute_capabilities(
            data_root,
            watcher=watcher,
            pro_corpus_enabled_override=pro_corpus_enabled,
        )
    )

    def _is_pro_corpus_allowed() -> bool:
        if pro_corpus_enabled is not None:
            return bool(pro_corpus_enabled)
        return bool(get_caps().get("pro_corpus_enabled", False))
    server_started_at = datetime.now().isoformat(timespec="seconds")
    server_pid = os.getpid()

    def _combined_jobs() -> list[dict[str, Any]]:
        jobs = list(proc_queue.list_jobs()) + list(ins_runner.list_jobs())
        jobs.sort(key=lambda j: str(j.get("updated_at") or ""), reverse=True)
        return jobs[:50]

    class ReportsHTTPHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            print(f"[http] {self.address_string()} - {format % args}")

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_error_text(self, status: HTTPStatus, msg: str) -> None:
            body = f"{status.value} {status.phrase}: {msg}\n".encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_bytes(
            self,
            status: HTTPStatus,
            body: bytes,
            content_type: str,
            *,
            cache_control: str = "no-cache",
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", cache_control)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_pna_cors_headers(self) -> None:
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            req_pna = str(self.headers.get("Access-Control-Request-Private-Network") or "").lower() == "true"
            for k, v in build_pna_cors_headers(origin, host_header=host, request_private_network=req_pna).items():
                self.send_header(k, v)

        def _send_json(self, status: HTTPStatus, payload: dict[str, Any], *, close_conn: bool = False) -> None:
            body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self._send_pna_cors_headers()
            self.send_header("Content-Length", str(len(body)))
            if close_conn:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _is_same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = (self.headers.get("Host") or "").strip().lower()
            origin_netloc = urlsplit(origin.strip()).netloc.lower()
            return bool(host) and origin_netloc == host

        def _is_authorized_cross_origin(self, req_path: str) -> bool:
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            if not is_cross_origin_allowed_web(origin, host):
                return False
            # `POST /api/companion/pair` authenticates via one-time pairing code inside JSON body
            if req_path == "/api/companion/pair":
                return True
            tok = extract_bearer_or_header_token(self.headers)
            return verify_pairing_token(tok, data_dir=data_root)

        def _drain_small_body(self) -> None:
            try:
                clen = int(self.headers.get("Content-Length") or "0")
                if 0 < clen <= _MAX_API_BODY_BYTES:
                    self.rfile.read(clen)
            except Exception:  # noqa: BLE001
                pass

        def _read_json_body(self, *, req_path: str = "") -> dict[str, Any] | None:
            if not self._is_same_origin() and not self._is_authorized_cross_origin(req_path):
                self._drain_small_body()
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Cross-origin API request blocked (pair Companion first)"}, close_conn=True)
                return None

            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                self._drain_small_body()
                self._send_json(
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    {"ok": False, "error": "Content-Type must be application/json"},
                    close_conn=True,
                )
                return None

            try:
                content_len = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid Content-Length"}, close_conn=True)
                return None
            if content_len <= 0 or content_len > _MAX_API_BODY_BYTES:
                self._send_json(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    {"ok": False, "error": "Invalid or oversized JSON body"},
                    close_conn=True,
                )
                return None

            raw_body = self.rfile.read(content_len)
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except Exception:  # noqa: BLE001
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Malformed JSON payload"})
                return None
            if not isinstance(payload, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "JSON payload must be an object"})
                return None
            return payload

        def _serve_file(self, path: Path, *, send_body: bool, cache_control_override: str | None = None) -> None:
            if not path.is_file():
                self._send_error_text(HTTPStatus.NOT_FOUND, "File not found")
                return

            file_size = path.stat().st_size
            ctype, _ = mimetypes.guess_type(str(path))
            if path.suffix.lower() == ".js":
                ctype = "application/javascript; charset=utf-8"
            elif path.suffix.lower() == ".css":
                ctype = "text/css; charset=utf-8"
            elif not ctype:
                ctype = "application/octet-stream"
            elif ctype.startswith("text/"):
                ctype = f"{ctype}; charset=utf-8"

            default_cc = (
                cache_control_override
                if cache_control_override is not None
                else ("no-cache" if path.suffix in (".html", ".json", ".js", ".css") else "public, max-age=3600")
            )

            range_hdr = self.headers.get("Range")
            if range_hdr:
                br = _parse_byte_range(range_hdr, file_size)
                if br is None:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = br
                length = end - start + 1
                self.send_response(HTTPStatus.PARTIAL_CONTENT)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", default_cc)
                self.end_headers()
                if send_body:
                    self._stream_slice(path, start, length)
                return

            accept_enc = (self.headers.get("Accept-Encoding") or "").lower()
            if not range_hdr and path.suffix == ".json" and "gzip" in accept_enc:
                raw_bytes = path.read_bytes()
                gz_bytes = gzip.compress(raw_bytes, compresslevel=6)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
                self.send_header("Content-Length", str(len(gz_bytes)))
                self.send_header("Cache-Control", "public, max-age=3600" if path.name.endswith("_overlay.json") else "no-cache")
                self.end_headers()
                if send_body:
                    self.wfile.write(gz_bytes)
                return

            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(file_size))
            self.send_header("Cache-Control", default_cc)
            self.end_headers()
            if send_body:
                self._stream_slice(path, 0, file_size)

        def _stream_slice(self, path: Path, start: int, length: int) -> None:
            chunk_size = 64 * 1024
            remaining = length
            with path.open("rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        break
                    remaining -= len(chunk)

        def _handle(self, *, send_body: bool) -> None:
            parsed = urlsplit(self.path)
            req_path = unquote(parsed.path)
            qs = parse_qs(parsed.query)

            # Unified Fightcade Lab App Shell at `/` (with `/review` & `/mobile` seamless aliases)
            if req_path in ("", "/", "/review", "/review/", "/mobile", "/mobile/"):
                html_str = build_mobile_app_html(default_username=detect_default_username(data_root))
                self._send_bytes(HTTPStatus.OK, html_str.encode("utf-8"), "text/html; charset=utf-8")
                return
            if req_path in ("/notes", "/notes/"):
                self._redirect("/#notes")
                return
            m_shared_m = _SHARED_MOMENT_PAGE_RE.match(req_path)
            if m_shared_m:
                html_str = build_mobile_app_html(
                    default_username=detect_default_username(data_root),
                    initial_route=f"m/{m_shared_m.group(1)}",
                )
                self._send_bytes(HTTPStatus.OK, html_str.encode("utf-8"), "text/html; charset=utf-8")
                return
            m_shared_p = _SHARED_PLAYLIST_PAGE_RE.match(req_path)
            if m_shared_p:
                html_str = build_mobile_app_html(
                    default_username=detect_default_username(data_root),
                    initial_route=f"p/{m_shared_p.group(1)}",
                )
                self._send_bytes(HTTPStatus.OK, html_str.encode("utf-8"), "text/html; charset=utf-8")
                return
            if req_path in ("/manifest.webmanifest", "/review/manifest.webmanifest", "/mobile/manifest.webmanifest"):
                self._send_bytes(
                    HTTPStatus.OK,
                    build_pwa_manifest_json().encode("utf-8"),
                    "application/manifest+json; charset=utf-8",
                )
                return
            if req_path.startswith("/assets/"):
                asset_path = resolve_safe_asset_path(req_path)
                if asset_path is None:
                    self._send_error_text(HTTPStatus.FORBIDDEN, "Forbidden asset path")
                    return
                self._serve_file(asset_path, send_body=send_body, cache_control_override="no-cache")
                return

            # JSON APIs
            if req_path == "/api/companion/ping":
                pairing = get_pairing_state(data_root)
                tok = extract_bearer_or_header_token(self.headers)
                tok_ok = verify_pairing_token(tok, data_dir=data_root) if tok else False
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "mode": "local_companion",
                        "companion_version": "0.1.0",
                        "install_id": pairing["install_id"],
                        "paired": pairing["paired"],
                        "token_valid": tok_ok,
                        "can_takeover": bool(get_caps().get("can_takeover")),
                        "uri_scheme_registered": is_uri_scheme_registered(),
                    },
                )
                return

            if req_path == "/api/onboarding":
                self._send_json(HTTPStatus.OK, get_onboarding_status(data_root))
                return

            if req_path == "/api/capabilities":
                self._send_json(HTTPStatus.OK, get_caps())
                return

            if req_path == "/api/settings":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "disk_cache_cap_gb": get_disk_cache_cap_gb(data_root),
                        "disk_cache": get_mobile_replays_disk_usage(data_root),
                        "pro_corpus_enabled": _is_pro_corpus_allowed(),
                        "stream_quota": proc_queue.stream_quota_status(),
                    },
                )
                return

            if req_path == "/api/home":
                uname = (qs.get("username") or [""])[0].strip() or detect_default_username(data_root)
                try:
                    cat = list_matches(username=uname, data_dir=data_root, job_queue=proc_queue)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                matches = cat.get("matches") or []
                latest_match = matches[0] if matches else None
                recent_matches = matches[1:4]
                active_goals = list_focus_goals(data_root, status="active", owner=uname)
                graduated_goals = list_focus_goals(data_root, status="graduated", owner=uname)
                all_playlists = list_playlists(data_root, owner=uname, username=uname, enrich=True)
                notes_res = list_collated_notes(username=uname, data_dir=data_root, include_select_marks=True)
                nc = notes_res.get("counts") or {}
                latest_insight = None
                if _is_pro_corpus_allowed():
                    ins_res = list_insights(data_root, include_legacy=False)
                    lv = ins_res.get("latest_verified_habits")
                    if lv:
                        latest_insight = {
                            "id": lv.get("id"),
                            "title": lv.get("matchup"),
                            "summary": lv.get("summary"),
                            "top_habit": lv.get("metric"),
                            "href": lv.get("href"),
                            "date": lv.get("date"),
                            "num_sets": lv.get("num_sets"),
                        }
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "username": uname,
                        "capabilities": get_caps(),
                        "latest_match": latest_match,
                        "recent_matches": recent_matches,
                        "focus_goals": {
                            "active": active_goals,
                            "graduated_count": len(graduated_goals),
                            "max_active": MAX_ACTIVE_GOALS,
                        },
                        "playlists": all_playlists[:4],
                        "playlists_total": len(all_playlists),
                        "pulse": {
                            "notes_total": int(nc.get("total") or 0),
                            "matchups_count": int(nc.get("matchups_count") or 0),
                            "untriaged_marks": int(nc.get("needs_note") or 0),
                            "latest_insight": latest_insight,
                        },
                    },
                )
                return

            if req_path == "/api/status":
                desktop_name, is_interactive = get_windows_desktop_name()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "pid": server_pid,
                        "started_at": server_started_at,
                        "takeover_enabled": True,
                        "desktop": desktop_name,
                        "interactive_desktop": is_interactive,
                        "active_takeover_pid": manager.active_pid(),
                        "watcher": watcher.status(),
                        "jobs": _combined_jobs()[:10],
                    },
                )
                return

            if req_path == "/api/queue":
                items = load_queue(data_root)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "watcher": watcher.status(),
                        "items": items,
                    },
                )
                return

            if req_path == "/api/matches":
                uname = (qs.get("username") or [""])[0].strip() or None
                status_filter = (qs.get("filter") or ["all"])[0].strip() or "all"
                self_char = (qs.get("self_char") or [""])[0].strip() or None
                opp_char = (qs.get("opp_char") or [""])[0].strip() or None
                try:
                    res = list_matches(
                        username=uname,
                        data_dir=data_root,
                        job_queue=proc_queue,
                        status_filter=status_filter,
                        self_char=self_char,
                        opp_char=opp_char,
                    )
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            if req_path == "/api/jobs":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "watcher": watcher.status(),
                        "jobs": _combined_jobs(),
                    },
                )
                return

            if req_path == "/api/focus-goals":
                st = (qs.get("status") or ["all"])[0].strip() or "all"
                owner = (qs.get("owner") or [""])[0].strip() or None
                try:
                    goals = list_focus_goals(data_root, status=st, owner=owner)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "goals": goals,
                        "max_active": MAX_ACTIVE_GOALS,
                    },
                )
                return

            if req_path == "/api/playlists":
                owner = (qs.get("owner") or [""])[0].strip() or None
                uname = (qs.get("username") or [""])[0].strip() or owner
                pls = list_playlists(data_root, owner=owner, username=uname, enrich=True)
                self._send_json(HTTPStatus.OK, {"ok": True, "playlists": pls})
                return

            if req_path == "/api/insights":
                if not _is_pro_corpus_allowed():
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "ok": False,
                            "error": (
                                "Insights & Pro-Corpus checkups are disabled by default (on the roadmap). "
                                "Set FC_ENABLE_PRO_CORPUS=1 or enable_pro_corpus=true in config.json to opt in locally."
                            ),
                        },
                    )
                    return
                inc_raw = (qs.get("include_legacy") or ["0"])[0].strip().lower()
                inc_legacy = inc_raw in ("1", "true", "yes")
                res = list_insights(data_root, include_legacy=inc_legacy)
                self._send_json(HTTPStatus.OK, res)
                return

            m_ins = _INSIGHT_ITEM_API_RE.match(req_path)
            if m_ins:
                if not _is_pro_corpus_allowed():
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "ok": False,
                            "error": (
                                "Insights & Pro-Corpus reports are disabled by default (on the roadmap). "
                                "Set FC_ENABLE_PRO_CORPUS=1 to opt in locally."
                            ),
                        },
                    )
                    return
                rid = m_ins.group(1)
                try:
                    res = get_insight_report(rid, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except FileNotFoundError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            m_mf = _REPLAY_MANIFEST_API_RE.match(req_path)
            if m_mf:
                qid = m_mf.group(1)
                mf_path = mobile_manifest_path(qid, data_dir=data_root)
                if not mf_path.exists():
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Mobile replay manifest not found for {qid}"})
                    return
                req_persp_raw = (qs.get("perspective") or [""])[0].strip()
                req_persp = int(req_persp_raw) if req_persp_raw in ("1", "2") else None
                req_user = (qs.get("username") or [""])[0].strip() or detect_default_username(data_root)
                try:
                    mf_doc = ensure_manifest_matches(
                        json.loads(mf_path.read_text(encoding="utf-8")),
                        data_dir=data_root,
                        viewer_side=req_persp,
                        username=req_user,
                    )
                except Exception as exc:  # noqa: BLE001
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "manifest": mf_doc,
                        "annotations": list_annotations(data_root, quark_id=qid),
                        "select_marks": _select_marks_for_replay(qid, data_root),
                        "set_note": get_set_note(qid, data_dir=data_root),
                    },
                )
                return

            if req_path == "/api/notes":
                uname = (qs.get("username") or [""])[0].strip() or None
                inc_sel_raw = (qs.get("include_select_marks") or ["1"])[0].strip().lower()
                inc_sel = inc_sel_raw not in ("0", "false", "no")
                res = list_collated_notes(
                    username=uname,
                    data_dir=data_root,
                    include_select_marks=inc_sel,
                )
                self._send_json(HTTPStatus.OK, res)
                return

            if req_path == "/api/annotations":
                qid = (qs.get("quark_id") or [""])[0].strip() or None
                rid_raw = (qs.get("round_id") or [""])[0].strip()
                rid = int(rid_raw) if rid_raw.isdigit() else None
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "annotations": list_annotations(data_root, quark_id=qid, round_id=rid),
                    },
                )
                return

            if req_path == "/api/set-notes":
                qid = (qs.get("quark_id") or [""])[0].strip()
                if not qid:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Provide quark_id query parameter"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "set_note": get_set_note(qid, data_dir=data_root)})
                return

            # Legacy Reports Hub deprecated -> redirect cleanly to Fightcade Lab Insights
            if req_path in ("/reports", "/reports/", "/reports/index.html"):
                self._redirect("/#insights")
                return

            # Onboard individual /reports/<stem>.html links into the native Fightcade Lab Report Viewer
            # (pass ?raw=1 to inspect the static offline HTML file directly if needed)
            m_rep_html = _REPORT_HTML_RE.match(req_path)
            if m_rep_html and (qs.get("raw") or ["0"])[0] not in ("1", "true"):
                stem = m_rep_html.group(1)
                if (reports_dir / f"{stem}.json").is_file():
                    self._redirect(f"/#report/{stem}")
                    return

            resolved = resolve_safe_data_path(req_path, data_root)
            if resolved is None:
                self._send_error_text(HTTPStatus.FORBIDDEN, "Path outside /reports, /clips, or /replays")
                return
            self._serve_file(resolved, send_body=send_body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            if not is_allowed_companion_origin(origin, host):
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Origin not allowed"}, close_conn=True)
                return
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_pna_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            self._handle(send_body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._handle(send_body=False)

        def do_POST(self) -> None:  # noqa: N802
            req_path = unquote(urlsplit(self.path).path)
            m_ov = _REPLAY_OVERLAYS_API_RE.match(req_path)
            m_pl_items = _PLAYLIST_ITEMS_API_RE.match(req_path)
            m_pl_drills = _PLAYLIST_DRILLS_API_RE.match(req_path)
            allowed_post = {
                "/api/takeover",
                "/api/queue/scan",
                "/api/queue/status",
                "/api/matches/refresh",
                "/api/matches/process",
                "/api/annotations",
                "/api/set-notes",
                "/api/focus-goals",
                "/api/playlists",
                "/api/insights/run",
                "/api/pro-search",
                "/api/pro-search/render-clip",
                "/api/share/prepare",
                "/api/companion/pair-code",
                "/api/companion/pair",
                "/api/companion/unpair",
                "/api/watcher/pause",
                "/api/onboarding",
            }
            if req_path not in allowed_post and not m_ov and not m_pl_items and not m_pl_drills:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Unknown API endpoint"}, close_conn=True)
                return

            payload = self._read_json_body(req_path=req_path)
            if payload is None:
                return

            if req_path == "/api/companion/pair-code":
                if not self._is_same_origin():
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Pairing codes can only be issued locally"})
                    return
                self._send_json(HTTPStatus.OK, issue_pairing_code(data_root))
                return

            if req_path == "/api/companion/pair":
                code = str(payload.get("code") or "").strip()
                origin = self.headers.get("Origin") or str(payload.get("origin") or "").strip() or None
                cloud_tok = str(payload.get("cloud_token") or "").strip() or None
                try:
                    res = complete_pairing(code, origin=origin, cloud_token=cloud_tok, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            if req_path == "/api/companion/unpair":
                if not self._is_same_origin():
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Unpair must be invoked locally"})
                    return
                self._send_json(HTTPStatus.OK, revoke_pairing(data_root))
                return

            if req_path == "/api/watcher/pause":
                if not self._is_same_origin():
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Watcher pause must be invoked locally"})
                    return
                paused = bool(payload.get("paused", not watcher.is_paused()))
                st = watcher.set_paused(paused)
                self._send_json(HTTPStatus.OK, {"ok": True, "watcher": st})
                return

            if req_path == "/api/onboarding":
                if not self._is_same_origin():
                    self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Onboarding must be invoked locally"})
                    return
                action = str(payload.get("action") or "save").strip().lower()
                if action == "register_uri":
                    res = register_uri_scheme()
                    self._send_json(HTTPStatus.OK, {"ok": bool(res.get("ok")), "uri": res, "onboarding": get_onboarding_status(data_root)})
                    return
                if action == "install_fast_fbneo":
                    consent = bool(payload.get("consent", False))
                    env_info = inspect_environment(data_dir=data_root)
                    fb_dir = env_info.get("fbneo_dir")
                    if not fb_dir:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Fightcade FBNeo folder not detected"})
                        return
                    try:
                        res = install_fast_fbneo(Path(fb_dir), consent=consent)
                    except PermissionError as exc:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                        return
                    except Exception as exc:  # noqa: BLE001
                        self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
                        return
                    self._send_json(HTTPStatus.OK, {"ok": True, "install": res, "onboarding": get_onboarding_status(data_root)})
                    return
                self._send_json(HTTPStatus.OK, update_onboarding_state(payload, data_root))
                return

            if m_ov:
                qid = m_ov.group(1)
                force = bool(payload.get("force", False))
                req_persp: int | None = None
                if payload.get("perspective") in (1, 2, "1", "2"):
                    req_persp = int(payload["perspective"])
                req_user = str(payload.get("username") or "").strip() or None
                try:
                    res = generate_replay_overlays(qid, data_dir=data_root, force=force)
                    if isinstance(res, dict) and isinstance(res.get("manifest"), dict):
                        res["manifest"] = ensure_manifest_matches(
                            res["manifest"],
                            viewer_side=req_persp,
                            username=req_user,
                        )
                except FileNotFoundError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            if m_pl_items:
                pl_id = m_pl_items.group(1)
                try:
                    updated_pl = mutate_playlist_items(pl_id, payload, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                if updated_pl is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Playlist '{pl_id}' not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "playlist": updated_pl})
                return

            if m_pl_drills:
                pl_id = m_pl_drills.group(1)
                try:
                    res = ensure_playlist_drills(
                        pl_id,
                        data_dir=data_root,
                        bake_fn=bake_drill_fn,
                        async_bake=async_bake_drills,
                    )
                except FileNotFoundError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            if req_path == "/api/focus-goals":
                if not payload.get("owner"):
                    payload = {**payload, "owner": detect_default_username(data_root)}
                try:
                    goal = create_focus_goal(payload, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "goal": goal})
                return

            if req_path == "/api/playlists":
                if not payload.get("owner"):
                    payload = {**payload, "owner": detect_default_username(data_root)}
                try:
                    pl = create_playlist(payload, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "playlist": pl})
                return

            if req_path == "/api/insights/run":
                if not _is_pro_corpus_allowed():
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "ok": False,
                            "error": (
                                "Insights & Pro-Corpus checkups are disabled by default (on the roadmap). "
                                "Set FC_ENABLE_PRO_CORPUS=1 to opt in locally."
                            ),
                        },
                    )
                    return
                kind = str(payload.get("kind") or "").strip().lower()
                qid = str(payload.get("quark_id") or "").strip() or None
                uname = str(payload.get("username") or "").strip() or detect_default_username(data_root)
                try:
                    persp = int(payload.get("perspective", 1))
                except (TypeError, ValueError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "perspective must be 1 or 2"})
                    return
                try:
                    job = ins_runner.enqueue(kind=kind, quark_id=qid, perspective=persp, username=uname)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except FileNotFoundError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "job": job})
                return

            if req_path == "/api/share/prepare":
                try:
                    qid = str(payload.get("quark_id") or "").strip()
                    round_id = int(payload.get("round_id", 0))
                    start_gf = int(payload["start_gf"]) if payload.get("start_gf") not in (None, "") else None
                    end_gf = int(payload["end_gf"]) if payload.get("end_gf") not in (None, "") else None
                    anchor_gf = int(payload["anchor_gf"]) if payload.get("anchor_gf") not in (None, "") else None
                    persp = int(payload.get("perspective", 1))
                    crf = int(payload.get("crf", 26))
                    res = prepare_share_bundle(
                        qid,
                        round_id,
                        start_gf=start_gf,
                        end_gf=end_gf,
                        anchor_gf=anchor_gf,
                        perspective=persp,
                        title=str(payload.get("title") or ""),
                        question=str(payload.get("question") or ""),
                        note=str(payload.get("note") or ""),
                        tags=payload.get("tags") if isinstance(payload.get("tags"), list) else None,
                        kind=str(payload.get("kind") or "note"),
                        data_dir=data_root,
                        crf=crf,
                    )
                except (ValueError, TypeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except FileNotFoundError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            if req_path == "/api/pro-search":
                if not _is_pro_corpus_allowed():
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "ok": False,
                            "pro_corpus_enabled": False,
                            "error": "Pro Corpus Search is on the roadmap (disabled by default). Set FC_ENABLE_PRO_CORPUS=1 locally to enable.",
                        },
                    )
                    return
                try:
                    res = pro_svc.search(
                        quark_id=str(payload.get("quark_id") or ""),
                        perspective=int(payload.get("perspective", 1)),
                        start_gf=int(payload.get("start_gf", 0)),
                        end_gf=int(payload.get("end_gf", 0)),
                        round_id=int(payload["round_id"]) if payload.get("round_id") not in (None, "") else None,
                        top_k=int(payload.get("top_k", 6)),
                        tier_override=str(payload.get("tier_override") or "").strip() or None,
                    )
                except (ValueError, TypeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except FileNotFoundError as exc:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            if req_path == "/api/pro-search/render-clip":
                if not _is_pro_corpus_allowed():
                    self._send_json(
                        HTTPStatus.FORBIDDEN,
                        {
                            "ok": False,
                            "pro_corpus_enabled": False,
                            "error": "Pro Corpus clip rendering is on the roadmap (disabled by default). Set FC_ENABLE_PRO_CORPUS=1 locally to enable.",
                        },
                    )
                    return
                try:
                    res = pro_svc.render_clip(
                        quark_id=str(payload.get("quark_id") or ""),
                        frame=int(payload.get("frame", 0)),
                        label=str(payload.get("label") or ""),
                    )
                except (ValueError, TypeError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            if req_path == "/api/matches/refresh":
                uname = str(payload.get("username") or "").strip() or detect_default_username(data_root)
                load_older = bool(payload.get("load_older", False))
                try:
                    res = refresh_history_fn(uname, load_older=load_older, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except RefreshCooldownError as exc:
                    self._send_json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"ok": False, "error": str(exc), "retry_after_s": round(exc.retry_after_s, 1)},
                    )
                    return
                except FightcadeRateLimitError as exc:
                    self._send_json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {
                            "ok": False,
                            "error": str(exc),
                            "retry_after_s": exc.retry_after,
                        },
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": f"Fightcade refresh failed: {exc}"})
                    return
                self._send_json(HTTPStatus.OK, res)
                return

            if req_path == "/api/matches/process":
                raw_ids = payload.get("quark_ids")
                single_id = payload.get("quark_id")
                force = bool(payload.get("force", False))
                try:
                    perspective = int(payload.get("perspective", 1))
                except (TypeError, ValueError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "perspective must be 1 or 2"})
                    return
                if isinstance(raw_ids, list) and raw_ids:
                    jobs = []
                    try:
                        for q in raw_ids[:20]:
                            jobs.append(proc_queue.enqueue(str(q), perspective=perspective, force=force))
                    except ValueError as exc:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                        return
                    except HourlyBackfillCapError as exc:
                        self._send_json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {
                                "ok": False,
                                "error": str(exc),
                                "retry_after_s": round(exc.retry_after_s, 1),
                                "cap": exc.cap,
                                "used": exc.used,
                            },
                        )
                        return
                    self._send_json(HTTPStatus.OK, {"ok": True, "jobs": jobs})
                    return
                if not single_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Provide quark_id or quark_ids"})
                    return
                try:
                    job = proc_queue.enqueue(str(single_id), perspective=perspective, force=force)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                except HourlyBackfillCapError as exc:
                    self._send_json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {
                            "ok": False,
                            "error": str(exc),
                            "retry_after_s": round(exc.retry_after_s, 1),
                            "cap": exc.cap,
                            "used": exc.used,
                        },
                    )
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "job": job})
                return

            if req_path == "/api/annotations":
                if not payload.get("owner"):
                    payload = {**payload, "owner": detect_default_username(data_root)}
                try:
                    ann = create_annotation(
                        payload,
                        data_dir=data_root,
                        bake_fn=bake_drill_fn,
                        async_bake=async_bake_drills,
                    )
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "annotation": ann})
                return

            if req_path == "/api/set-notes":
                qid = str(payload.get("quark_id") or "").strip()
                owner = str(payload.get("owner") or "").strip() or detect_default_username(data_root)
                try:
                    rec = upsert_set_note(
                        qid,
                        note=str(payload.get("note") or ""),
                        tags=payload.get("tags"),
                        owner=owner,
                        data_dir=data_root,
                    )
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "set_note": rec})
                return

            if req_path == "/api/queue/scan":
                username = str(payload.get("username") or "").strip() or None
                quark_id = str(payload.get("quark_id") or "").strip() or None
                if not username and not quark_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Provide username or quark_id"})
                    return
                try:
                    player = int(payload.get("player", 1))
                    lookback_s = float(payload.get("lookback_seconds", DEFAULT_LOOKBACK_FRAMES / 60.0))
                    lookback_f = max(60, min(900, int(round(lookback_s * 60.0))))
                except (TypeError, ValueError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Invalid player or lookback_seconds"})
                    return
                res = watcher.trigger_manual_scan(
                    username=username,
                    quark_id=quark_id,
                    player=player,
                    lookback_frames=lookback_f,
                )
                self._send_json(HTTPStatus.OK, res)
                return

            if req_path == "/api/queue/status":
                action = str(payload.get("action") or "").strip().lower()
                if action == "clear_done":
                    removed = clear_done_items(data_root)
                    self._send_json(HTTPStatus.OK, {"ok": True, "removed": removed})
                    return
                item_id = str(payload.get("id") or "").strip()
                status = str(payload.get("status") or "").strip().lower()
                if not item_id or not status:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Provide id + status (or action='clear_done')"})
                    return
                try:
                    updated = update_queue_item_status(item_id, status, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                if updated is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Queue item '{item_id}' not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "item": updated})
                return

            # POST /api/takeover
            try:
                result = launcher(payload)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            except FileNotFoundError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"Failed to launch takeover: {exc}"})
                return

            self._send_json(HTTPStatus.OK, result)

        def do_PATCH(self) -> None:  # noqa: N802
            req_path = unquote(urlsplit(self.path).path)
            if req_path == "/api/settings":
                payload = self._read_json_body()
                if payload is None:
                    return
                updates: dict[str, Any] = {}
                pruned: list[str] = []
                if "disk_cache_cap_gb" in payload:
                    try:
                        raw_gb = float(payload["disk_cache_cap_gb"])
                    except (TypeError, ValueError):
                        self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "disk_cache_cap_gb must be a number"})
                        return
                    if raw_gb < 0 or raw_gb > 500:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"ok": False, "error": "disk_cache_cap_gb must be 0 (unlimited) or between 0.5 and 500 GB"},
                        )
                        return
                    clean_gb = 0.0 if raw_gb == 0 else max(0.5, round(raw_gb, 2))
                    updates["disk_cache_cap_gb"] = clean_gb
                if updates:
                    save_app_config(updates, data_dir=data_root)
                if payload.get("prune_now") and "disk_cache_cap_gb" in updates:
                    pruned = prune_old_mobile_replays(data_root, max_sets=0)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "disk_cache_cap_gb": get_disk_cache_cap_gb(data_root),
                        "disk_cache": get_mobile_replays_disk_usage(data_root),
                        "pruned": pruned,
                    },
                )
                return

            m_ann = _ANN_ITEM_API_RE.match(req_path)
            m_q = _QUEUE_ITEM_API_RE.match(req_path)
            m_goal = _GOAL_ITEM_API_RE.match(req_path)
            m_pl = _PLAYLIST_ITEM_API_RE.match(req_path)
            if not m_ann and not m_q and not m_goal and not m_pl:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Unknown PATCH endpoint"}, close_conn=True)
                return
            payload = self._read_json_body()
            if payload is None:
                return

            if m_goal:
                goal_id = m_goal.group(1)
                try:
                    updated_g = update_focus_goal(goal_id, payload, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                if updated_g is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Focus goal '{goal_id}' not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "goal": updated_g})
                return

            if m_pl:
                pl_id = m_pl.group(1)
                try:
                    updated_pl = update_playlist(pl_id, payload, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                if updated_pl is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Playlist '{pl_id}' not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "playlist": updated_pl})
                return

            if m_q:
                item_id = m_q.group(1)
                try:
                    updated_q = update_queue_item(item_id, payload, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                if updated_q is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Queue item '{item_id}' not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "item": updated_q})
                return

            ann_id = m_ann.group(1)
            try:
                updated = update_annotation(
                    ann_id,
                    payload,
                    data_dir=data_root,
                    bake_fn=bake_drill_fn,
                    async_bake=async_bake_drills,
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            if updated is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Annotation '{ann_id}' not found"})
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "annotation": updated})

        def do_DELETE(self) -> None:  # noqa: N802
            req_path = unquote(urlsplit(self.path).path)
            m_ann = _ANN_ITEM_API_RE.match(req_path)
            m_goal = _GOAL_ITEM_API_RE.match(req_path)
            m_pl = _PLAYLIST_ITEM_API_RE.match(req_path)
            if not m_ann and not m_goal and not m_pl:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Unknown DELETE endpoint"}, close_conn=True)
                return
            if not self._is_same_origin():
                self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "Cross-origin API request blocked"}, close_conn=True)
                return

            if m_goal:
                goal_id = m_goal.group(1)
                try:
                    removed = delete_focus_goal(goal_id, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                if not removed:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Focus goal '{goal_id}' not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "deleted": goal_id})
                return

            if m_pl:
                pl_id = m_pl.group(1)
                try:
                    removed = delete_playlist(pl_id, data_dir=data_root)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return
                if not removed:
                    self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Playlist '{pl_id}' not found"})
                    return
                self._send_json(HTTPStatus.OK, {"ok": True, "deleted": pl_id})
                return

            ann_id = m_ann.group(1)
            try:
                removed = delete_annotation(ann_id, data_dir=data_root)
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return
            if not removed:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": f"Annotation '{ann_id}' not found"})
                return
            self._send_json(HTTPStatus.OK, {"ok": True, "deleted": ann_id})

    return ReportsHTTPHandler


def discover_local_ips() -> list[str]:
    """Return sorted non-loopback IPv4 addresses on this machine (LAN + VPN/Tailscale)."""
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(info[4][0])
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


class _FightcadeHTTPServer(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR allows two servers to silently double-bind the same port;
    # disabling it on win32 ensures clean immediate WSAEADDRINUSE (10048) detection.
    allow_reuse_address = sys.platform != "win32"


def create_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    data_dir: Path = DEFAULT_DATA_DIR,
    auto_refresh_index: bool = True,
    takeover_launcher: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    takeover_manager: TakeoverManager | None = None,
    match_watcher: LiveMatchWatcher | None = None,
    processing_queue: ProcessingQueue | None = None,
    insight_runner: InsightJobRunner | None = None,
    pro_search_service: ProSearchService | None = None,
    capabilities_fn: Callable[[], dict[str, Any]] | None = None,
    refresh_history_fn: Callable[..., dict[str, Any]] = refresh_match_history,
    bake_drill_fn: Callable[..., dict[str, Any]] | None = None,
    async_bake_drills: bool = True,
    pro_corpus_enabled: bool | None = None,
    watch_matches: bool = False,
) -> ThreadingHTTPServer:
    watcher = match_watcher or LiveMatchWatcher(data_dir=data_dir)
    if watch_matches and match_watcher is None:
        watcher.start()
    handler_cls = make_handler(
        data_dir=data_dir,
        auto_refresh_index=auto_refresh_index,
        takeover_launcher=takeover_launcher,
        takeover_manager=takeover_manager,
        match_watcher=watcher,
        processing_queue=processing_queue,
        insight_runner=insight_runner,
        pro_search_service=pro_search_service,
        capabilities_fn=capabilities_fn,
        refresh_history_fn=refresh_history_fn,
        bake_drill_fn=bake_drill_fn,
        async_bake_drills=async_bake_drills,
        pro_corpus_enabled=pro_corpus_enabled,
    )
    srv = _FightcadeHTTPServer((host, port), handler_cls)
    srv.fc_watcher = watcher  # type: ignore[attr-defined]
    return srv


def _is_port_in_use_error(exc: OSError) -> bool:
    return exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in (10048, 10013)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve Fightcade Lab Workbench, Replay Studio & MP4 clips.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 for zero Windows Firewall popups; pass --lan or --host 0.0.0.0 for LAN/Tailscale)",
    )
    parser.add_argument(
        "--lan",
        action="store_true",
        help="Bind to 0.0.0.0 so phones/tablets on LAN or Tailscale can reach Fightcade Lab",
    )
    parser.add_argument("--port", type=int, default=8765, help="TCP port (default: 8765)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR, help="Path to data/ directory")
    parser.add_argument("--no-auto-refresh", action="store_true", help="Disable auto-rebuilding legacy reports/index.html")
    parser.add_argument("--no-watch-matches", action="store_true", help="Disable automatic post-match Select-mark watcher")
    parser.add_argument("--no-auto-mobile-render", action="store_true", help="Disable auto-rendering mobile round MP4s after live matches")
    args = parser.parse_args(argv)

    bind_host = "0.0.0.0" if args.lan else str(args.host)

    proc_queue_holder: list[ProcessingQueue] = []

    def _on_live_match_scanned(qid: str, persp: int) -> None:
        if not args.no_auto_mobile_render and proc_queue_holder:
            proc_queue_holder[0].enqueue(qid, perspective=persp, source="live_watcher")

    watcher = LiveMatchWatcher(data_dir=args.data_dir, on_match_scanned=_on_live_match_scanned)
    proc_queue = ProcessingQueue(data_dir=args.data_dir, is_live_match_fn=watcher.is_in_match)
    proc_queue_holder.append(proc_queue)
    ins_runner = InsightJobRunner(data_dir=args.data_dir, is_live_match_fn=watcher.is_in_match)
    pro_svc = ProSearchService(data_dir=args.data_dir, is_live_match_fn=watcher.is_in_match)

    try:
        server = create_server(
            host=bind_host,
            port=args.port,
            data_dir=args.data_dir,
            auto_refresh_index=not args.no_auto_refresh,
            match_watcher=watcher,
            processing_queue=proc_queue,
            insight_runner=ins_runner,
            pro_search_service=pro_svc,
        )
    except OSError as exc:
        if _is_port_in_use_error(exc):
            print(
                f"ERROR: Port {args.port} on {bind_host} is already in use.\n"
                f"  - Fightcade Lab may already be running at http://127.0.0.1:{args.port}/\n"
                f"  - Or pass a different port: --port {args.port + 1}",
                file=sys.stderr,
            )
            return 2
        raise

    proc_queue.start()
    if not args.no_watch_matches:
        watcher.start()

    actual_port = server.server_address[1]
    desktop_name, is_interactive = get_windows_desktop_name()
    print(f"Serving Fightcade Lab (scoped to {args.data_dir.resolve()}\\{{reports,clips,replays}})")
    print(f"  Fightcade Lab : http://127.0.0.1:{actual_port}/")
    if bind_host in ("0.0.0.0", "::"):
        for ip in discover_local_ips():
            print(f"  LAN/Tailscale : http://{ip}:{actual_port}/")
    else:
        print("  Network Scope : localhost-only (pass --lan for LAN/Tailscale phone access)")
    if is_interactive:
        print(f"  Takeover API  : POST /api/takeover READY (Windows desktop: {desktop_name})")
    else:
        print(
            f"  WARNING: Running on non-interactive Windows desktop '{desktop_name}'. "
            "Browser Launch Takeover windows will be hidden — start .\\scripts\\serve_reports.ps1 from your own PowerShell terminal."
        )
    if not args.no_watch_matches:
        auto_str = " + auto round-MP4 render" if not args.no_auto_mobile_render else ""
        print(f"  Select-Mark Watcher: ACTIVE (tap Select/Coin during live matches{auto_str})")

    def _is_busy() -> bool:
        if proc_queue.has_running_job() or ins_runner.has_running_job():
            return True
        phase = str(watcher.status().get("phase") or "")
        return phase in ("scanning", "waiting_replay")

    reload_watcher = ReloadTriggerWatcher(
        data_dir=args.data_dir,
        shutdown_fn=server.shutdown,
        is_busy_fn=_is_busy,
    )
    reload_watcher.start()

    print("  Auto-Reload Trigger: touch data/.reload to restart cleanly in-place.")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.")
    finally:
        reload_watcher.stop()
        ins_runner.stop()
        proc_queue.stop()
        watcher.stop()
        server.server_close()
    if reload_watcher.reload_requested:
        return RELOAD_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
