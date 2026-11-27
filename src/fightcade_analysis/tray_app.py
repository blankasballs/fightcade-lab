"""Windows System Tray Companion wrapper (pure stdlib `ctypes` Win32 `Shell_NotifyIconW`).

Zero-terminal desktop experience for non-technical Fightcade players:
  - Runs the local HTTP Companion (`127.0.0.1:8765`) + `LiveMatchWatcher` + `ProcessingQueue`
    quietly in background threads without opening a console window.
  - Exposes a clean Windows tray icon & context menu:
      * Open Fightcade Lab
      * Pause / Resume Auto-Capture
      * Activity Log (`data/logs/companion.log`)
      * Start with Windows (`HKCU\\...\\Run\\FightcadeLab`, zero UAC)
      * Register `fightcadelab://` Takeover Links
      * Quit
  - Handles `--uri "fightcadelab://..."` CLI invocations cleanly.
  - Fetches remote stewardship config (`/companion/config.json`) on startup.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
import webbrowser
from typing import Any

from fightcade_analysis.companion_bridge import fetch_remote_companion_config
from fightcade_analysis.env_config import DEFAULT_DATA_DIR, is_frozen_runtime
from fightcade_analysis.mobile_ui import WEB_ASSETS_DIR
from fightcade_analysis.server import DEFAULT_HOST, DEFAULT_PORT, create_server
from fightcade_analysis.uri_scheme import (
    dispatch_companion_uri,
    is_uri_scheme_registered,
    register_uri_scheme,
)

STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_VALUE = "FightcadeLabCompanion"


def companion_log_path(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    return data_dir.resolve() / "logs" / "companion.log"


def configure_companion_logging(data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    """Attach a rolling UTF-8 file logger (`1 MB x 3`) under `data/logs/companion.log`."""
    log_path = companion_log_path(data_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("fightcade_lab")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(str(log_path), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(handler)
    return log_path


def build_startup_command() -> str:
    if is_frozen_runtime():
        return f'"{Path(sys.executable).resolve()}"'
    py_exe = Path(sys.executable).resolve()
    pyw = py_exe.with_name("pythonw.exe")
    runner = pyw if pyw.is_file() else py_exe
    return f'"{runner}" -m fightcade_analysis.tray_app'


def is_start_with_windows_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH) as key:
            val, _ = winreg.QueryValueEx(key, STARTUP_REG_VALUE)
            return bool(str(val or "").strip())
    except OSError:
        return False


def set_start_with_windows(enabled: bool, command: str | None = None) -> dict[str, Any]:
    """Toggle `HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\FightcadeLabCompanion`."""
    if sys.platform != "win32":
        return {"ok": False, "enabled": False, "error": "Windows startup toggle requires win32"}
    import winreg

    if enabled:
        cmd = command or build_startup_command()
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH) as key:
            winreg.SetValueEx(key, STARTUP_REG_VALUE, 0, winreg.REG_SZ, cmd)
        return {"ok": True, "enabled": True, "command": cmd}

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, STARTUP_REG_VALUE)
    except OSError:
        pass
    return {"ok": True, "enabled": False}


# ---------------------------------------------------------------------------
# Pure-Win32 Tray Icon Controller (`Shell_NotifyIconW`)
# ---------------------------------------------------------------------------

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_USER = 0x0400
WM_TRAYICON = WM_USER + 20
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_CHECKED = 0x00000008
MF_SEPARATOR = 0x00000800
TPM_LEFTALIGN = 0x0000
TPM_BOTTOMALIGN = 0x0020
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010
LR_DEFAULTSIZE = 0x00000040


def _load_tray_icon(user32: Any, icon_path: Path | None = None) -> tuple[Any, bool]:
    """Load `favicon.ico` via Win32 `LoadImageW`, falling back cleanly to `IDI_APPLICATION`."""
    ico = icon_path or (WEB_ASSETS_DIR / "favicon.ico")
    try:
        if ico.is_file():
            user32.LoadImageW.argtypes = [
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.LoadImageW.restype = wintypes.HANDLE
            hicon = user32.LoadImageW(None, str(ico), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
            if hicon:
                return hicon, True
    except Exception:  # noqa: BLE001
        pass
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadIconW.restype = wintypes.HICON
    return user32.LoadIconW(None, ctypes.cast(IDI_APPLICATION, wintypes.LPCWSTR)), False

CMD_OPEN = 1001
CMD_TOGGLE_WATCHER = 1002
CMD_ACTIVITY_LOG = 1003
CMD_TOGGLE_STARTUP = 1004
CMD_REGISTER_URI = 1005
CMD_QUIT = 1099


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
    ]


class CompanionTrayController:
    """High-level controller bridging tray actions, local HTTP server & `LiveMatchWatcher`."""

    def __init__(
        self,
        *,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        data_dir: Path = DEFAULT_DATA_DIR,
        watch_matches: bool = True,
        open_browser_on_start: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.data_dir = data_dir.resolve()
        self.watch_matches = watch_matches
        self.open_browser_on_start = open_browser_on_start
        self.log_path = configure_companion_logging(self.data_dir)
        self.logger = logging.getLogger("fightcade_lab")
        self.server: Any | None = None
        self._server_thread: threading.Thread | None = None
        self._remote_cfg: dict[str, Any] = {}

    @property
    def url(self) -> str:
        display_host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        return f"http://{display_host}:{self.port}/"

    def start_backend(self) -> None:
        self.logger.info("Starting Fightcade Lab Companion on %s (data=%s)", self.url, self.data_dir)
        self._remote_cfg = fetch_remote_companion_config(self.data_dir)
        self.logger.info(
            "Remote stewardship config loaded (source=%s, api_enabled=%s, max_backfill_hr=%s)",
            self._remote_cfg.get("source"),
            self._remote_cfg.get("fightcade_api_enabled"),
            self._remote_cfg.get("max_backfill_per_hour"),
        )
        self.server = create_server(
            host=self.host,
            port=self.port,
            data_dir=self.data_dir,
            auto_refresh_index=False,
            watch_matches=self.watch_matches,
        )
        self._server_thread = threading.Thread(
            target=self.server.serve_forever,
            name="fc-companion-http",
            daemon=True,
        )
        self._server_thread.start()
        if self.open_browser_on_start:
            self.open_web_ui()

    def stop_backend(self) -> None:
        if self.server is not None:
            self.logger.info("Stopping Fightcade Lab Companion...")
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Error shutting down server: %s", exc)
            self.server = None

    def is_watcher_paused(self) -> bool:
        watcher = getattr(self.server, "fc_watcher", None) if self.server is not None else None
        if watcher is None:
            return True
        return bool(watcher.is_paused())

    def toggle_watcher_pause(self) -> bool:
        watcher = getattr(self.server, "fc_watcher", None) if self.server is not None else None
        if watcher is None:
            return True
        new_paused = not watcher.is_paused()
        watcher.set_paused(new_paused)
        self.logger.info("Auto-capture %s via tray menu", "paused" if new_paused else "resumed")
        return new_paused

    def open_web_ui(self) -> None:
        webbrowser.open(self.url)

    def open_activity_log(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.write_text("Fightcade Lab Companion log initialized.\n", encoding="utf-8")
        if sys.platform == "win32":
            os.startfile(str(self.log_path))  # type: ignore[attr-defined]
        else:
            webbrowser.open(self.log_path.as_uri())

    def handle_menu_command(self, cmd_id: int) -> bool:
        """Execute a tray menu command. Returns False when Quit was requested."""
        if cmd_id == CMD_OPEN:
            self.open_web_ui()
        elif cmd_id == CMD_TOGGLE_WATCHER:
            self.toggle_watcher_pause()
        elif cmd_id == CMD_ACTIVITY_LOG:
            self.open_activity_log()
        elif cmd_id == CMD_TOGGLE_STARTUP:
            set_start_with_windows(not is_start_with_windows_enabled())
        elif cmd_id == CMD_REGISTER_URI:
            register_uri_scheme()
        elif cmd_id == CMD_QUIT:
            return False
        return True

    def run_tray_loop(self) -> int:
        """Run the native Win32 message loop with tray icon until Quit."""
        if sys.platform != "win32":
            self.start_backend()
            try:
                while self._server_thread and self._server_thread.is_alive():
                    self._server_thread.join(timeout=0.5)
            except KeyboardInterrupt:
                pass
            finally:
                self.stop_backend()
            return 0

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32

        WNDPROC = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HICON),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        self.start_backend()
        hwnd_ref: list[int] = [0]

        def _show_menu(hwnd: int) -> None:
            hmenu = user32.CreatePopupMenu()
            user32.AppendMenuW(hmenu, MF_STRING, CMD_OPEN, "Open Fightcade Lab")
            pause_label = "Resume Auto-Capture" if self.is_watcher_paused() else "Pause Auto-Capture"
            user32.AppendMenuW(hmenu, MF_STRING, CMD_TOGGLE_WATCHER, pause_label)
            user32.AppendMenuW(hmenu, MF_STRING, CMD_ACTIVITY_LOG, "Activity Log")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            startup_flags = MF_STRING | (MF_CHECKED if is_start_with_windows_enabled() else 0)
            user32.AppendMenuW(hmenu, startup_flags, CMD_TOGGLE_STARTUP, "Start with Windows")
            uri_flags = MF_STRING | (MF_CHECKED if is_uri_scheme_registered() else 0)
            user32.AppendMenuW(hmenu, uri_flags, CMD_REGISTER_URI, "Register fightcadelab:// Links")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, CMD_QUIT, "Quit Fightcade Lab")

            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            user32.SetForegroundWindow(hwnd)
            user32.TrackPopupMenu(hmenu, TPM_LEFTALIGN | TPM_BOTTOMALIGN, pt.x, pt.y, 0, hwnd, None)
            user32.DestroyMenu(hmenu)

        def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_TRAYICON:
                if lparam == WM_LBUTTONDBLCLK:
                    self.open_web_ui()
                elif lparam == WM_RBUTTONUP:
                    _show_menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                cmd_id = wparam & 0xFFFF
                if not self.handle_menu_command(cmd_id):
                    user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wnd_proc_cb = WNDPROC(_wnd_proc)
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = f"FightcadeLabTray_{os.getpid()}"
        wc = WNDCLASSW()
        wc.lpfnWndProc = wnd_proc_cb
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        user32.RegisterClassW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(0, cls_name, "Fightcade Lab Companion", 0, 0, 0, 0, 0, 0, 0, hinst, None)
        hwnd_ref[0] = hwnd

        hicon, owns_icon = _load_tray_icon(user32)
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = hicon
        nid.szTip = "Fightcade Lab Companion (127.0.0.1)"
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        msg_struct = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg_struct), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg_struct))
                user32.DispatchMessageW(ctypes.byref(msg_struct))
        finally:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            if owns_icon and hicon:
                try:
                    user32.DestroyIcon.argtypes = [wintypes.HICON]
                    user32.DestroyIcon(hicon)
                except Exception:  # noqa: BLE001
                    pass
            self.stop_backend()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fightcade Lab Windows System Tray Companion.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port (default: 8765)")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--no-watch-matches", action="store_true")
    parser.add_argument("--open", action="store_true", help="Open Fightcade Lab in default browser on startup")
    parser.add_argument("--uri", default=None, help="Dispatch a fightcadelab:// URI and exit")
    args = parser.parse_args(argv)

    if args.uri:
        try:
            res = dispatch_companion_uri(
                args.uri,
                companion_base=f"http://127.0.0.1:{args.port}",
                data_dir=args.data_dir,
            )
            print(res)
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[tray] URI dispatch failed: {exc}", file=sys.stderr)
            return 1

    ctrl = CompanionTrayController(
        host=args.host,
        port=args.port,
        data_dir=args.data_dir,
        watch_matches=not args.no_watch_matches,
        open_browser_on_start=args.open,
    )
    return ctrl.run_tray_loop()


if __name__ == "__main__":
    raise SystemExit(main())
