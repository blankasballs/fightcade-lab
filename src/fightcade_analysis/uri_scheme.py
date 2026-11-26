"""Custom `fightcadelab://` Windows URI scheme registration, strict parsing & dispatch.

Universal browser fallback when Chrome Private Network Access or Safari blocks `https://` → `http://127.0.0.1:8765`.
Registers strictly under `HKCU\\Software\\Classes\\fightcadelab` (zero UAC / admin privileges required).
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import sys
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from fightcade_analysis.companion_bridge import get_pairing_state
from fightcade_analysis.env_config import DEFAULT_DATA_DIR
from fightcade_analysis.takeover import TakeoverManager, validate_takeover_payload

URI_SCHEME = "fightcadelab"
_QUARK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
_HASH_RE = re.compile(r"^[A-Za-z0-9_/?=&.%-]{0,200}$")


def build_takeover_uri(
    *,
    quark_id: str,
    frame: int = 0,
    round_id: int = 0,
    start_gf: int = 0,
    end_gf: int = 0,
    player: int = 1,
    speed: float = 1.0,
    game_id: str = "sfiii3nr1",
    label: str = "",
) -> str:
    """Build a canonical `fightcadelab://takeover?...` deep link."""
    params: dict[str, Any] = {
        "quark_id": str(quark_id).strip(),
        "player": int(player),
        "game_id": str(game_id or "sfiii3nr1").strip(),
    }
    if frame > 0:
        params["frame"] = int(frame)
    if round_id > 0:
        params["round_id"] = int(round_id)
    if start_gf > 0:
        params["start_gf"] = int(start_gf)
    if end_gf > 0:
        params["end_gf"] = int(end_gf)
    if abs(float(speed) - 1.0) > 1e-6:
        params["speed"] = f"{float(speed):.2f}"
    if label:
        params["label"] = " ".join(str(label).split())[:100]
    return f"{URI_SCHEME}://takeover?{urlencode(params)}"


def parse_companion_uri(raw_uri: str) -> dict[str, Any]:
    """Strictly parse and sanitize a `fightcadelab://` URI. Raises ValueError on bad input."""
    text = str(raw_uri or "").strip()
    if len(text) > 2048:
        raise ValueError("URI exceeds maximum length")
    parsed = urlsplit(text)
    if parsed.scheme.lower() != URI_SCHEME:
        raise ValueError(f"Unsupported URI scheme '{parsed.scheme}'; expected '{URI_SCHEME}://'")

    action = (parsed.netloc or parsed.path.lstrip("/")).strip().lower()
    qs = parse_qs(parsed.query, keep_blank_values=False)

    def _first(key: str, default: str = "") -> str:
        vals = qs.get(key)
        return vals[0].strip() if vals else default

    if action == "takeover":
        raw_payload: dict[str, Any] = {
            "quark_id": _first("quark_id") or _first("qid"),
            "frame": _first("frame") or _first("anchor_gf") or _first("start_gf") or "0",
            "start_gf": _first("start_gf") or "0",
            "end_gf": _first("end_gf") or "0",
            "player": _first("player") or _first("perspective") or "1",
            "game_id": _first("game_id") or "sfiii3nr1",
            "mode": _first("mode") or "takeover",
            "speed": _first("speed") or "1.0",
            "label": _first("label"),
            "bake_if_missing": True,
        }
        validated = validate_takeover_payload(raw_payload)
        round_raw = _first("round_id") or _first("r")
        if round_raw:
            try:
                rid = int(round_raw)
                if rid < 1 or rid > 999:
                    raise ValueError("round_id out of range")
                validated["round_id"] = rid
            except ValueError as exc:
                raise ValueError("round_id must be a positive integer") from exc
        return {"action": "takeover", "payload": validated}

    if action == "pair":
        code = _first("code")
        if not _CODE_RE.match(code):
            raise ValueError("Invalid pairing code in URI")
        origin = _first("origin")
        return {"action": "pair", "code": code, "origin": origin or None}

    if action == "open":
        target_hash = _first("hash").lstrip("#")
        if not _HASH_RE.match(target_hash):
            raise ValueError("Invalid hash target in URI")
        return {"action": "open", "hash": target_hash}

    raise ValueError(f"Unsupported fightcadelab:// action '{action}'")


def build_default_uri_command() -> str:
    """Build the Windows HKCU command template that handles `fightcadelab://` links."""
    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        return f'"{exe}" --uri "%1"'
    py_exe = str(Path(sys.executable).resolve())
    return f'"{py_exe}" -m fightcade_analysis.uri_scheme "%1"'


def register_uri_scheme(command: str | None = None) -> dict[str, Any]:
    """Register `fightcadelab://` under `HKCU\\Software\\Classes\\fightcadelab` (zero UAC)."""
    if sys.platform != "win32":
        return {"ok": False, "error": "Windows registry URI registration requires win32"}
    import winreg

    cmd = command or build_default_uri_command()
    base_key = rf"Software\Classes\{URI_SCHEME}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base_key) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Fightcade Lab Protocol")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{base_key}\shell\open\command") as ck:
        winreg.SetValueEx(ck, "", 0, winreg.REG_SZ, cmd)
    return {"ok": True, "scheme": URI_SCHEME, "command": cmd}


def is_uri_scheme_registered() -> bool:
    """Return True if `HKCU\\Software\\Classes\\fightcadelab\\shell\\open\\command` exists."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{URI_SCHEME}\shell\open\command") as ck:
            val, _ = winreg.QueryValueEx(ck, "")
            return bool(str(val or "").strip())
    except OSError:
        return False


def unregister_uri_scheme() -> dict[str, Any]:
    """Remove `HKCU\\Software\\Classes\\fightcadelab` cleanly."""
    if sys.platform != "win32":
        return {"ok": False, "error": "Windows registry URI unregistration requires win32"}
    import winreg

    for sub in (
        rf"Software\Classes\{URI_SCHEME}\shell\open\command",
        rf"Software\Classes\{URI_SCHEME}\shell\open",
        rf"Software\Classes\{URI_SCHEME}\shell",
        rf"Software\Classes\{URI_SCHEME}",
    ):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass
    return {"ok": True, "scheme": URI_SCHEME}


def dispatch_companion_uri(
    raw_uri: str,
    *,
    companion_base: str = "http://127.0.0.1:8765",
    data_dir: Path = DEFAULT_DATA_DIR,
    post_fn: Callable[..., Any] | None = None,
    takeover_manager: TakeoverManager | None = None,
) -> dict[str, Any]:
    """Parse and execute a `fightcadelab://` URI via the running Companion or direct fallback."""
    parsed = parse_companion_uri(raw_uri)
    action = parsed["action"]
    pairing = get_pairing_state(data_dir, include_token=True)
    headers = {
        "Content-Type": "application/json",
        "X-Fightcade-Lab-Token": str(pairing.get("local_token") or ""),
    }
    do_post = post_fn or requests.post

    if action == "takeover":
        payload = parsed["payload"]
        try:
            resp = do_post(
                f"{companion_base.rstrip('/')}/api/takeover",
                json=payload,
                headers=headers,
                timeout=4.0,
            )
            if getattr(resp, "status_code", 0) == 200:
                return {"ok": True, "dispatched_via": "running_companion", "result": resp.json()}
        except Exception:  # noqa: BLE001
            pass

        # Fallback: Companion HTTP server isn't running yet -> launch Takeover directly
        mgr = takeover_manager or TakeoverManager(data_dir=data_dir)
        res = mgr.launch(payload)
        return {"ok": True, "dispatched_via": "direct_launch", "result": res}

    if action == "open":
        import webbrowser

        target = f"{companion_base.rstrip('/')}/#{parsed['hash']}" if parsed["hash"] else f"{companion_base.rstrip('/')}/"
        webbrowser.open(target)
        return {"ok": True, "dispatched_via": "browser", "url": target}

    if action == "pair":
        resp = do_post(
            f"{companion_base.rstrip('/')}/api/companion/pair",
            json={"code": parsed["code"], "origin": parsed.get("origin")},
            headers=headers,
            timeout=4.0,
        )
        data = resp.json() if hasattr(resp, "json") else {}
        if getattr(resp, "status_code", 0) != 200:
            raise RuntimeError(str(data.get("error") or f"Pairing failed (HTTP {getattr(resp, 'status_code', '?')})"))
        return {"ok": True, "dispatched_via": "running_companion", "result": data}

    raise ValueError(f"Unhandled action: {action}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Usage: python -m fightcade_analysis.uri_scheme [--register | --unregister | <fightcadelab://...>]", file=sys.stderr)
        return 2
    if args[0] == "--register":
        res = register_uri_scheme()
        print(res)
        return 0 if res.get("ok") else 1
    if args[0] == "--unregister":
        res = unregister_uri_scheme()
        print(res)
        return 0 if res.get("ok") else 1
    try:
        res = dispatch_companion_uri(args[0])
        print(res)
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"[uri_scheme] Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
