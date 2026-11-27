"""Fightcade environment auto-discovery, INI/ROM/controller inheritance, space-safe CLI helpers & local settings.

Centralizes Tier 1–3 Windows portability guardrails:
  - Auto-discovers Fightcade across standard & OneDrive Windows install paths.
  - Parses `config/fcadefbneo.ini` `szAppRomPaths[N]` for actual ROM search directories.
  - Ensures `fcadefbneo_fast.exe` inherits `config/fcadefbneo.ini` and `config/games/sfiii3nr1.ini`.
  - Formats CLI paths space-safely: double-quoted for patched `fcadefbneo_fast.exe`,
    Win32 `GetShortPathNameW` (8.3) fallback for stock `fcadefbneo.exe`.
  - Manages `FC_ENABLE_PRO_CORPUS` opt-in gate and rolling local disk cache cap (`5 GB` default).
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any

def is_frozen_runtime() -> bool:
    """Return True when running inside a frozen/Nuitka standalone Windows executable."""
    return bool(getattr(sys, "frozen", False) or ("__compiled__" in globals()))


def resolve_app_root() -> Path:
    """Resolve the application root directory (containing `lua/`, `web/`, `LICENSE`, etc.).

    In a frozen Nuitka standalone install (`%LOCALAPPDATA%\\FightcadeLab\\FightcadeLabCompanion.exe`),
    returns the directory containing `sys.executable`. In a source checkout, returns the repo root.
    """
    if is_frozen_runtime():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resolve_default_data_dir() -> Path:
    """Resolve the default per-user writable `data/` directory."""
    return resolve_app_root() / "data"


REPO_ROOT = resolve_app_root()
DEFAULT_DATA_DIR = resolve_default_data_dir()
DEFAULT_DISK_CACHE_CAP_GB: float = 5.0


def resolve_local_ffmpeg(data_dir: Path = DEFAULT_DATA_DIR) -> Path | None:
    """Locate `ffmpeg.exe` in local Companion tools dir, `PATH`, or optional `imageio-ffmpeg`."""
    local_bin = data_dir.resolve() / "tools" / "ffmpeg.exe"
    if local_bin.is_file():
        return local_bin
    on_path = shutil.which("ffmpeg")
    if on_path:
        return Path(on_path).resolve()
    try:
        import imageio_ffmpeg

        p = Path(imageio_ffmpeg.get_ffmpeg_exe())
        if p.is_file():
            return p.resolve()
    except Exception:  # noqa: BLE001
        pass
    return None


def require_local_ffmpeg(data_dir: Path = DEFAULT_DATA_DIR) -> str:
    """Return the path string to a usable `ffmpeg` binary or raise an actionable `FileNotFoundError`."""
    found = resolve_local_ffmpeg(data_dir)
    if found is not None:
        return str(found)
    raise FileNotFoundError("ffmpeg.exe not found. Download it from the Fightcade Lab Setup Wizard.")

_ROM_PATH_RE = re.compile(r"^\s*szAppRomPaths\[(\d+)\]\s*(.*)$")
_STREAM_TARGET_RE = re.compile(r"^quark:stream,[a-z0-9_-]{2,32},[A-Za-z0-9_-]{3,64}\.[0-9]{1,4},[0-9]{2,5}$")
_ROM_ID_RE = re.compile(r"^[a-z0-9_-]{2,32}$")


def validate_stream_target(raw: str) -> str:
    """Strictly validate a Fightcade `quark:stream,...` CLI target to block Lua/arg injection."""
    if not isinstance(raw, str):
        raise ValueError("stream_target must be a string")
    if (
        not raw
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw)
        or '"' in raw
        or "'" in raw
        or "\\" in raw
        or "/" in raw
        or ".." in raw
        or ".lua" in raw.lower()
    ):
        raise ValueError(f"Unsafe or malformed Fightcade stream_target: {raw!r}")
    if not _STREAM_TARGET_RE.match(raw):
        raise ValueError(f"Invalid Fightcade stream_target format: {raw!r}")
    return raw


def _read_dotenv_value(key: str, *, env_path: Path | None = None) -> str | None:
    """Read a single key from `.env` (lightweight stdlib parser, never logs secrets)."""
    path = env_path or (REPO_ROOT / ".env")
    if not path.is_file():
        return None
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                val = v.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                return val.strip()
    except OSError:
        return None
    return None


def _parse_bool_flag(val: Any) -> bool | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def load_app_config(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Load `data/config.json` safely, returning `{}` if missing or malformed."""
    cfg_path = data_dir.resolve() / "config.json"
    if not cfg_path.is_file():
        return {}
    try:
        doc = json.loads(cfg_path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_app_config(updates: dict[str, Any], data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Atomically merge `updates` into `data/config.json` and return the saved config dict."""
    cfg_path = data_dir.resolve() / "config.json"
    doc = load_app_config(data_dir)
    for k, v in updates.items():
        if v is None:
            doc.pop(k, None)
        else:
            doc[k] = v
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_suffix(f".tmp_{os.getpid()}")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(cfg_path)
    return doc


def get_disk_cache_cap_gb(data_dir: Path = DEFAULT_DATA_DIR) -> float:
    """Return rolling local full-match MP4 disk cache cap in GB (`5.0` default, `0` = unlimited)."""
    env_val = os.environ.get("FC_DISK_CACHE_CAP_GB")
    if env_val is not None and env_val.strip() != "":
        try:
            v = float(env_val.strip())
            return 0.0 if v <= 0 else max(0.5, min(500.0, round(v, 2)))
        except ValueError:
            pass
    cfg = load_app_config(data_dir)
    if "disk_cache_cap_gb" in cfg:
        try:
            v = float(cfg["disk_cache_cap_gb"])
            return 0.0 if v <= 0 else max(0.5, min(500.0, round(v, 2)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_DISK_CACHE_CAP_GB


def get_disk_cache_cap_bytes(data_dir: Path = DEFAULT_DATA_DIR) -> int:
    gb = get_disk_cache_cap_gb(data_dir)
    if gb <= 0:
        return 0
    return int(round(gb * (1024 ** 3)))


def is_pro_corpus_opt_in(data_dir: Path = DEFAULT_DATA_DIR, *, env_path: Path | None = None) -> bool:
    """Return True ONLY if explicitly opted in via `FC_ENABLE_PRO_CORPUS=1` (env/`.env`) or `config.json`.

    Packaged/frozen public Companion builds (`FC_PACKAGED_COMPANION=1`) ALWAYS return False.
    """
    if is_frozen_runtime() or _parse_bool_flag(os.environ.get("FC_PACKAGED_COMPANION")) is True:
        return False

    env_flag = _parse_bool_flag(os.environ.get("FC_ENABLE_PRO_CORPUS"))
    if env_flag is not None:
        return env_flag

    cfg = load_app_config(data_dir)
    if "enable_pro_corpus" in cfg:
        cfg_flag = _parse_bool_flag(cfg.get("enable_pro_corpus"))
        if cfg_flag is not None:
            return cfg_flag

    dotenv_flag = _parse_bool_flag(_read_dotenv_value("FC_ENABLE_PRO_CORPUS", env_path=env_path))
    if dotenv_flag is not None:
        return dotenv_flag

    return False


def is_pro_corpus_enabled(data_dir: Path = DEFAULT_DATA_DIR, *, env_path: Path | None = None) -> bool:
    """Return True only when explicitly opted in AND local `data/parquet/*.parquet` exists."""
    if not is_pro_corpus_opt_in(data_dir, env_path=env_path):
        return False
    pq_dir = data_dir.resolve() / "parquet"
    return pq_dir.is_dir() and any(pq_dir.glob("*.parquet"))


# ---------------------------------------------------------------------------
# Fightcade & FBNeo Auto-Discovery
# ---------------------------------------------------------------------------

def get_fightcade_search_candidates(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    extra_candidates: list[Path] | None = None,
) -> list[Path]:
    """Return ordered candidate Fightcade root directories across standard & OneDrive paths."""
    out: list[Path] = []
    seen: set[str] = set()

    def _add(p: Path | str | None) -> None:
        if not p:
            return
        cand = Path(p)
        key = str(cand).lower().rstrip("\\/")
        if key and key not in seen:
            seen.add(key)
            out.append(cand)

    if extra_candidates:
        for c in extra_candidates:
            _add(c)

    _add(os.environ.get("FC_FIGHTCADE_DIR"))
    cfg = load_app_config(data_dir)
    _add(cfg.get("fightcade_dir"))

    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    localappdata = os.environ.get("LOCALAPPDATA") or ""
    onedrive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer") or ""

    if userprofile:
        up = Path(userprofile)
        _add(up / "Documents" / "Fightcade")
        _add(up / "OneDrive" / "Documents" / "Fightcade")
    if onedrive:
        _add(Path(onedrive) / "Documents" / "Fightcade")
    if localappdata:
        _add(Path(localappdata) / "Fightcade")
    _add(Path(r"C:\Fightcade"))
    return out


def _fbneo_dir_from_root(root: Path) -> Path | None:
    """Check if `root` is a Fightcade root (`root/emulator/fbneo`) or directly an FBNeo dir."""
    for sub in (root / "emulator" / "fbneo", root):
        if (sub / "fcadefbneo.exe").is_file() or (sub / "fcadefbneo_fast.exe").is_file():
            return sub
    return None


def discover_fightcade_dir(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    extra_candidates: list[Path] | None = None,
) -> Path | None:
    """Locate the user's Fightcade installation root directory."""
    for cand in get_fightcade_search_candidates(data_dir=data_dir, extra_candidates=extra_candidates):
        if _fbneo_dir_from_root(cand) is not None:
            return cand
        if (cand / "emulator").is_dir():
            return cand
    return None


def discover_fbneo_dir(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    extra_candidates: list[Path] | None = None,
) -> Path | None:
    """Locate the Fightcade FBNeo directory containing `fcadefbneo.exe` (or `fcadefbneo_fast.exe`)."""
    # Explicit env / config overrides first
    for env_key in ("FC_FBNEO_DIR",):
        v = (os.environ.get(env_key) or "").strip()
        if v:
            p = Path(v)
            if p.is_dir():
                return p
    for env_key in ("FC_FBNEO_EXE", "FC_FAST_FBNEO_EXE"):
        v = (os.environ.get(env_key) or "").strip()
        if v:
            p = Path(v)
            if p.is_file():
                return p.parent

    cfg = load_app_config(data_dir)
    if cfg.get("fbneo_dir"):
        p = Path(str(cfg["fbneo_dir"]))
        if p.is_dir():
            return p

    for root in get_fightcade_search_candidates(data_dir=data_dir, extra_candidates=extra_candidates):
        found = _fbneo_dir_from_root(root)
        if found is not None:
            return found
    return None


def resolve_default_fbneo_exe(*, data_dir: Path = DEFAULT_DATA_DIR) -> Path:
    """Return discovered stock `fcadefbneo.exe` path (falling back to standard Documents path)."""
    env_exe = (os.environ.get("FC_FBNEO_EXE") or "").strip()
    if env_exe:
        return Path(env_exe)
    fb_dir = discover_fbneo_dir(data_dir=data_dir)
    if fb_dir is not None:
        return fb_dir / "fcadefbneo.exe"
    userprofile = os.environ.get("USERPROFILE") or str(Path.home())
    return Path(userprofile) / "Documents" / "Fightcade" / "emulator" / "fbneo" / "fcadefbneo.exe"


def resolve_default_fast_fbneo_exe(
    *,
    stock_exe: Path | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Path:
    """Return discovered `fcadefbneo_fast.exe` path."""
    env_fast = (os.environ.get("FC_FAST_FBNEO_EXE") or "").strip()
    if env_fast:
        return Path(env_fast)
    if stock_exe is not None:
        cand = stock_exe.parent / "fcadefbneo_fast.exe"
        if cand.is_file():
            return cand
    fb_dir = discover_fbneo_dir(data_dir=data_dir)
    if fb_dir is not None:
        return fb_dir / "fcadefbneo_fast.exe"
    base = stock_exe.parent if stock_exe is not None else resolve_default_fbneo_exe(data_dir=data_dir).parent
    return base / "fcadefbneo_fast.exe"


def discover_grouflon_dir(
    fbneo_exe: Path | None = None,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> Path | None:
    """Locate Grouflon's `3rd_training_lua-master` (or `3rd_training_lua`) cleanly without vendoring."""
    candidates: list[Path] = []
    env_g = (os.environ.get("FC_GROUFLON_DIR") or "").strip()
    if env_g:
        candidates.append(Path(env_g))

    search_parents: list[Path] = []
    if fbneo_exe is not None:
        search_parents.append(fbneo_exe.parent)
    fb_dir = discover_fbneo_dir(data_dir=data_dir)
    if fb_dir is not None and fb_dir not in search_parents:
        search_parents.append(fb_dir)

    for parent in search_parents:
        candidates.append(parent / "3rd_training_lua-master")
        candidates.append(parent / "3rd_training_lua")

    seen: set[str] = set()
    for cand in candidates:
        key = str(cand).lower()
        if key in seen:
            continue
        seen.add(key)
        if (cand / "src" / "startup.lua").is_file():
            return cand.resolve()
    return None


# ---------------------------------------------------------------------------
# `config/fcadefbneo.ini` ROM Path Parsing & Config/Controller Inheritance
# ---------------------------------------------------------------------------

def parse_fbneo_rom_paths(fbneo_dir: Path, *, ini_path: Path | None = None) -> list[Path]:
    """Parse `szAppRomPaths[N]` directories from `config/fcadefbneo.ini` (resolving relative paths)."""
    fb_root = fbneo_dir.resolve()
    cfg_file = ini_path or (fb_root / "config" / "fcadefbneo.ini")
    parsed: list[tuple[int, Path]] = []

    if cfg_file.is_file():
        try:
            text = cfg_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            m = _ROM_PATH_RE.match(raw_line)
            if not m:
                continue
            idx = int(m.group(1))
            val = m.group(2).strip()
            if not val:
                continue
            p = Path(val)
            if not p.is_absolute():
                p = (fb_root / val).resolve()
            parsed.append((idx, p))

    parsed.sort(key=lambda pair: pair[0])
    out: list[Path] = []
    seen: set[str] = set()
    for _, p in parsed:
        k = str(p).lower().rstrip("\\/")
        if k not in seen:
            seen.add(k)
            out.append(p)

    # Fallback standard Fightcade ROM folders if INI is missing or empty
    for fallback in (fb_root / "ROMs", fb_root / "roms"):
        k = str(fallback).lower().rstrip("\\/")
        if k not in seen:
            seen.add(k)
            out.append(fallback)
    return out


def find_rom_file(
    game_id: str = "sfiii3nr1",
    *,
    fbneo_dir: Path | None = None,
    ini_path: Path | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> tuple[Path | None, list[Path]]:
    """Search parsed FBNeo ROM paths for `<game_id>.zip` or `<game_id>.7z` (case-insensitive)."""
    fb_root = fbneo_dir or discover_fbneo_dir(data_dir=data_dir)
    if fb_root is None:
        return None, []
    rom_dirs = parse_fbneo_rom_paths(fb_root, ini_path=ini_path)
    targets_lower = {f"{game_id.lower()}.zip", f"{game_id.lower()}.7z"}
    for rdir in rom_dirs:
        if not rdir.is_dir():
            continue
        for ext in (".zip", ".7z"):
            direct = rdir / f"{game_id}{ext}"
            if direct.is_file():
                return direct, rom_dirs
        try:
            for entry in rdir.iterdir():
                if entry.is_file() and entry.name.lower() in targets_lower:
                    return entry, rom_dirs
        except OSError:
            continue
    return None, rom_dirs


def _rewrite_relative_rom_paths_in_ini(ini_text: str, stock_fbneo_dir: Path) -> str:
    """Rewrite relative `szAppRomPaths[N]` entries to absolute paths under `stock_fbneo_dir`."""
    stock_root = stock_fbneo_dir.resolve()
    out_lines: list[str] = []
    for line in ini_text.splitlines(keepends=True):
        m = _ROM_PATH_RE.match(line.rstrip("\r\n"))
        if not m:
            out_lines.append(line)
            continue
        idx_s, val = m.group(1), m.group(2).strip()
        if not val:
            out_lines.append(line)
            continue
        p = Path(val)
        if not p.is_absolute():
            abs_p = str((stock_root / val).resolve()).rstrip("\\/") + "\\"
            eol = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            out_lines.append(f"szAppRomPaths[{idx_s}] {abs_p}{eol}")
        else:
            out_lines.append(line)
    return "".join(out_lines)


def ensure_fbneo_config_inheritance(
    exe_path: Path,
    *,
    stock_fbneo_dir: Path | None = None,
    game_id: str = "sfiii3nr1",
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Ensure `fcadefbneo_fast.exe` inherits `config/fcadefbneo.ini` + `config/games/<game_id>.ini`.

    FBNeo's `WinMain()` calls `AppDirectory()` (chdir to the exe's folder) and `ConfigAppLoad()`
    reads `config/<exe_stem>.ini` + `config/games/<game_id>.ini` relative to that folder.
    Syncing from the user's Fightcade FBNeo folder guarantees custom ROM paths and fightstick
    button bindings work immediately, even if `fcadefbneo_fast.exe` lives outside Fightcade's folder.
    """
    exe_resolved = exe_path.resolve()
    dst_dir = exe_resolved.parent
    src_dir = (stock_fbneo_dir.resolve() if stock_fbneo_dir else None) or discover_fbneo_dir(data_dir=data_dir)
    result: dict[str, Any] = {
        "inherited_app_ini": False,
        "inherited_game_ini": False,
        "same_dir": False,
    }
    if src_dir is None or not src_dir.is_dir():
        return result

    same_dir = dst_dir.resolve() == src_dir.resolve()
    result["same_dir"] = same_dir

    src_app_ini = src_dir / "config" / "fcadefbneo.ini"
    dst_app_ini = dst_dir / "config" / f"{exe_resolved.stem}.ini"
    if src_app_ini.is_file() and src_app_ini.resolve() != dst_app_ini.resolve():
        need_sync = (not dst_app_ini.is_file()) or (src_app_ini.stat().st_mtime > dst_app_ini.stat().st_mtime)
        if need_sync:
            try:
                dst_app_ini.parent.mkdir(parents=True, exist_ok=True)
                raw = src_app_ini.read_text(encoding="utf-8", errors="ignore")
                content = raw if same_dir else _rewrite_relative_rom_paths_in_ini(raw, src_dir)
                dst_app_ini.write_text(content, encoding="utf-8")
                result["inherited_app_ini"] = True
            except OSError:
                pass

    if not same_dir:
        src_game_ini = src_dir / "config" / "games" / f"{game_id}.ini"
        dst_game_ini = dst_dir / "config" / "games" / f"{game_id}.ini"
        if src_game_ini.is_file():
            need_game = (not dst_game_ini.is_file()) or (src_game_ini.stat().st_mtime > dst_game_ini.stat().st_mtime)
            if need_game:
                try:
                    dst_game_ini.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_game_ini, dst_game_ini)
                    result["inherited_game_ini"] = True
                except OSError:
                    pass
        src_presets = src_dir / "config" / "presets"
        dst_presets = dst_dir / "config" / "presets"
        if src_presets.is_dir() and not dst_presets.exists():
            try:
                shutil.copytree(src_presets, dst_presets, dirs_exist_ok=True)
            except OSError:
                pass

    return result


# ---------------------------------------------------------------------------
# Space-Safe CLI Path Formatting for FBNeo
# ---------------------------------------------------------------------------

def is_fast_fbneo_exe(exe_path: Path) -> bool:
    """Return True if `exe_path` is our patched `fcadefbneo_fast.exe` (quote bug fixed)."""
    return exe_path.name.lower() == "fcadefbneo_fast.exe"


def to_short_path_win32(path: str | Path) -> str:
    """Convert an existing Windows path to its space-free 8.3 short path via `GetShortPathNameW`."""
    raw = str(path)
    if sys.platform != "win32" or " " not in raw:
        return raw
    try:
        kernel32 = ctypes.windll.kernel32
        get_short = kernel32.GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_short.restype = ctypes.c_uint32

        p = Path(raw)
        if p.exists():
            buf_len = get_short(raw, None, 0)
            if buf_len > 0:
                buf = ctypes.create_unicode_buffer(buf_len + 1)
                if get_short(raw, buf, buf_len + 1) > 0 and buf.value:
                    return str(buf.value)
        elif p.parent.exists():
            parent_str = str(p.parent)
            buf_len = get_short(parent_str, None, 0)
            if buf_len > 0:
                buf = ctypes.create_unicode_buffer(buf_len + 1)
                if get_short(parent_str, buf, buf_len + 1) > 0 and buf.value:
                    return str(Path(buf.value) / p.name)
    except Exception:  # noqa: BLE001
        pass
    return raw


def format_fbneo_cli_arg(
    arg: str | Path,
    *,
    exe_path: Path,
    is_path: bool = True,
    short_path_fn: Any | None = None,
) -> str:
    """Format a single CLI argument safely for `fcadefbneo_fast.exe` or stock `fcadefbneo.exe`.

    - Arguments without whitespace are returned unquoted (100% compatible with both binaries).
    - For `fcadefbneo_fast.exe` (which fixes `main.cpp:892`), paths with spaces are safely double-quoted.
    - For stock `fcadefbneo.exe` (unpatched `main.cpp:892`), resolves a space-free 8.3 short path via
      `GetShortPathNameW`; if the volume has 8.3 names disabled, raises a clear actionable `ValueError`.
    """
    s = str(arg)
    if not any(ch.isspace() for ch in s):
        return s

    if is_fast_fbneo_exe(exe_path):
        escaped = s.replace('"', "")
        return f'"{escaped}"'

    if is_path:
        resolver = short_path_fn or to_short_path_win32
        short = str(resolver(s))
        if short and not any(ch.isspace() for ch in short):
            return short

    raise ValueError(
        f"Path contains spaces ('{s}') and stock fcadefbneo.exe truncates quoted CLI arguments "
        "(main.cpp:892) while 8.3 short paths are unavailable on this volume. "
        "Install fcadefbneo_fast.exe alongside fcadefbneo.exe for full space-safe path support."
    )


def build_fbneo_cmdline(
    exe_path: Path,
    flags: list[str],
    target_arg: str | Path,
    lua_path: Path,
    *,
    target_is_path: bool = False,
    short_path_fn: Any | None = None,
) -> str:
    """Build a space-safe Windows command string for launching FBNeo with `<target>` before `<lua>`."""
    if not target_is_path:
        target_s = str(target_arg)
        if (
            not target_s
            or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in target_s)
            or '"' in target_s
            or "'" in target_s
            or "\\" in target_s
            or "/" in target_s
            or ".." in target_s
            or ".lua" in target_s.lower()
        ):
            raise ValueError(f"Unsafe non-path FBNeo target argument: {target_s!r}")
        if not _ROM_ID_RE.match(target_s):
            validate_stream_target(target_s)

    parts = [f'"{exe_path}"']
    for fl in flags:
        fl_s = str(fl or "").strip()
        if fl_s:
            if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in fl_s) or '"' in fl_s or "'" in fl_s:
                raise ValueError(f"Unsafe FBNeo CLI flag: {fl!r}")
            parts.append(fl_s)
    parts.append(
        format_fbneo_cli_arg(
            target_arg,
            exe_path=exe_path,
            is_path=target_is_path,
            short_path_fn=short_path_fn,
        )
    )
    parts.append(
        format_fbneo_cli_arg(
            lua_path,
            exe_path=exe_path,
            is_path=True,
            short_path_fn=short_path_fn,
        )
    )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Environment Health Summary (for `/api/capabilities` & Status Popover)
# ---------------------------------------------------------------------------

def inspect_environment(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    fbneo_exe: Path | None = None,
    game_id: str = "sfiii3nr1",
) -> dict[str, Any]:
    """Return structured health diagnostics for Fightcade, FBNeo, ROMs, controller config & Grouflon."""
    stock_exe = fbneo_exe or resolve_default_fbneo_exe(data_dir=data_dir)
    fast_exe = resolve_default_fast_fbneo_exe(stock_exe=stock_exe, data_dir=data_dir)
    fb_dir = stock_exe.parent if stock_exe.parent.is_dir() else discover_fbneo_dir(data_dir=data_dir)
    fc_dir = discover_fightcade_dir(data_dir=data_dir)

    rom_file, rom_dirs = find_rom_file(game_id, fbneo_dir=fb_dir, data_dir=data_dir)
    ctrl_ini = (fb_dir / "config" / "games" / f"{game_id}.ini") if fb_dir is not None else None
    g_dir = discover_grouflon_dir(stock_exe, data_dir=data_dir)
    ffmpeg_path = resolve_local_ffmpeg(data_dir=data_dir)

    return {
        "fightcade_dir": str(fc_dir) if fc_dir else None,
        "fbneo_dir": str(fb_dir) if fb_dir else None,
        "fbneo_found": bool(stock_exe.is_file() or fast_exe.is_file()),
        "stock_fbneo_found": bool(stock_exe.is_file()),
        "fast_fbneo_found": bool(fast_exe.is_file()),
        "ffmpeg_found": ffmpeg_path is not None,
        "ffmpeg_path": str(ffmpeg_path) if ffmpeg_path else None,
        "rom_found": rom_file is not None,
        "rom_path": str(rom_file) if rom_file else None,
        "rom_search_paths": [str(p) for p in rom_dirs[:6]],
        "controller_config_found": bool(ctrl_ini and ctrl_ini.is_file()),
        "has_grouflon": g_dir is not None,
        "grouflon_dir": str(g_dir) if g_dir else None,
    }
