"""Security hardening regression tests (Pass 1):
- stream_target CLI/Lua RCE prevention & savestate/inputs path containment
- fightcadelab:// URI scheme & pairing origin validation
- asset_installer HTTPS + GitHub release prefix + pinned SHA-256 / Ed25519 verification
- ota_lua monotonic semver anti-rollback enforcement
- server.py DNS-rebinding Host validation, tightened CORS, cross-origin GET token guard & security headers
- Frontend JS static DOM XSS sweep (zero `'${esc(...)}'` inside inline event handlers + safeHref verification)
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any

import pytest
import requests

from fightcade_analysis.asset_installer import (
    FAST_FBNEO_DOWNLOAD_PREFIX,
    download_verified_asset,
    install_fast_fbneo,
)
from fightcade_analysis.companion_bridge import (
    complete_pairing,
    is_allowed_companion_origin,
    issue_pairing_code,
    lookup_remote_fightcade_meta,
)
from fightcade_analysis.env_config import build_fbneo_cmdline, validate_stream_target
from fightcade_analysis.ota_lua import (
    build_signed_lua_pack_manifest,
    ed25519_generate_keypair,
    ed25519_sign,
    resolve_lua_script,
    verify_and_install_lua_pack,
)
from fightcade_analysis.server import create_server
from fightcade_analysis.takeover import TakeoverManager
from fightcade_analysis.uri_scheme import parse_companion_uri

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_JS_DIR = REPO_ROOT / "src" / "fightcade_analysis" / "web" / "js"


# ---------------------------------------------------------------------------
# 1. stream_target RCE Prevention & Savestate Path Containment
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_target",
    [
        r"\\evil\share\evil.lua",
        "sfiii3nr1 evil.lua",
        "quark:stream,sfiii3nr1,1728857242828-5771.7,7100 evil.lua",
        'quark:stream,sfiii3nr1,1728857242828-5771.7,7100" -w evil.lua',
        "quark:stream,sfiii3nr1,../evil.7,7100",
        "quark:stream,sfiii3nr1,1728857242828-5771.7,7100\nmalicious",
        "quark:stream,sfiii3nr1,evil.lua.7,7100",
        "C:\\Users\\attacker\\payload.lua",
        "",
    ],
)
def test_stream_target_and_cmdline_reject_rce_payloads(tmp_path: Path, bad_target: str) -> None:
    with pytest.raises(ValueError):
        validate_stream_target(bad_target)

    exe = tmp_path / "fcadefbneo_fast.exe"
    lua = tmp_path / "seek_and_takeover.lua"
    with pytest.raises(ValueError):
        build_fbneo_cmdline(exe, ["-w"], bad_target, lua, target_is_path=False)


def test_stream_target_and_cmdline_accept_valid_targets(tmp_path: Path) -> None:
    valid_stream = "quark:stream,sfiii3nr1,1728857242828-5771.7,7100"
    assert validate_stream_target(valid_stream) == valid_stream

    exe = tmp_path / "fcadefbneo_fast.exe"
    lua = tmp_path / "seek_and_takeover.lua"
    cmd_stream = build_fbneo_cmdline(exe, ["-w"], valid_stream, lua, target_is_path=False)
    assert valid_stream in cmd_stream

    cmd_rom = build_fbneo_cmdline(exe, ["-w"], "sfiii3nr1", lua, target_is_path=False)
    assert " sfiii3nr1 " in cmd_rom


def test_remote_meta_drops_poisoned_stream_target_and_takeover_rejects_rce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {
                "ok": True,
                "meta": {
                    "quark_id": "1728857242828-5771",
                    "stream_target": r"\\evil\share\evil.lua",
                },
            }

    class FakeSess:
        def get(self, *_a: Any, **_kw: Any) -> FakeResp:
            return FakeResp()

    meta = lookup_remote_fightcade_meta(
        "1728857242828-5771",
        base_url="https://fightcadelab.com",
        session=FakeSess(),  # type: ignore[arg-type]
    )
    assert meta is not None
    assert "stream_target" not in meta

    # Even if a mocked meta/cache/resolver returns a poisoned stream_target, TakeoverManager.launch MUST reject it!
    fake_exe = tmp_path / "fcadefbneo_fast.exe"
    fake_exe.write_bytes(b"MZ")
    fake_lua = tmp_path / "seek_and_takeover.lua"
    fake_lua.write_text("-- lua", encoding="utf-8")

    spawned: list[str] = []
    mgr = TakeoverManager(
        data_dir=tmp_path,
        fbneo_exe=fake_exe,
        replay_takeover_lua=fake_lua,
        seek_lua=fake_lua,
        popen_fn=lambda cmd, **_kw: spawned.append(str(cmd)),
    )

    monkeypatch.setattr(
        "fightcade_analysis.takeover.lookup_cached_stream_target",
        lambda *_a, **_kw: "sfiii3nr1 evil.lua",
    )
    with pytest.raises(ValueError):
        mgr.launch({"quark_id": "1728857242828-5771", "frame": 19501, "player": 1})
    assert spawned == []

    monkeypatch.setattr(
        "fightcade_analysis.takeover.lookup_cached_stream_target",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "fightcade_analysis.takeover.lookup_remote_fightcade_meta",
        lambda *_a, **_kw: {"stream_target": r"\\evil\share\evil.lua"},
    )
    with pytest.raises(ValueError):
        mgr.launch({"quark_id": "1728857242828-5771", "frame": 19501, "player": 1})
    assert spawned == []


def test_takeover_rejects_bookmark_savestate_or_inputs_escaping_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    sel_dir = data_dir / "selections"
    sel_dir.mkdir(parents=True)

    outside_fs = tmp_path / "outside_evil.fs"
    outside_fs.write_bytes(b"EVIL")

    (sel_dir / "queue.jsonl").write_text(
        json.dumps(
            {
                "id": "sel_escape_1",
                "quark_id": "1728857242828-5771",
                "start_gf": 1000,
                "end_gf": 1200,
                "perspective": 1,
                "savestate": str(outside_fs),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    fake_exe = tmp_path / "fcadefbneo_fast.exe"
    fake_exe.write_bytes(b"MZ")
    fake_lua = tmp_path / "replay_takeover.lua"
    fake_lua.write_text("-- lua", encoding="utf-8")

    mgr = TakeoverManager(
        data_dir=data_dir,
        fbneo_exe=fake_exe,
        replay_takeover_lua=fake_lua,
        seek_lua=fake_lua,
        popen_fn=lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("Should never spawn")),
    )
    with pytest.raises(ValueError, match="escapes data_dir"):
        mgr.launch({"selection_id": "sel_escape_1"})


# ---------------------------------------------------------------------------
# 2. URI Scheme Hardening & Untrusted Pairing Origin Rejection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_uri",
    [
        'fightcadelab://takeover?quark_id=1728857242828-5771&frame=100&label="evil"',
        "fightcadelab://takeover?quark_id=1728857242828-5771&frame=100&label=`calc`",
        "fightcadelab://takeover?quark_id=1728857242828-5771&frame=100\r\nInjected",
        "fightcadelab://takeover?quark_id=1728857242828-5771&frame=100\x00null",
        "fightcadelab://pair?code=validcode1234&origin=https://evil.com",
    ],
)
def test_uri_scheme_rejects_control_chars_quotes_backticks_and_evil_origin(bad_uri: str) -> None:
    with pytest.raises(ValueError):
        parse_companion_uri(bad_uri)


def test_pairing_rejects_untrusted_body_origin_in_api_and_bridge(tmp_path: Path) -> None:
    issued = issue_pairing_code(tmp_path)
    with pytest.raises(ValueError, match="Untrusted pairing origin"):
        complete_pairing(issued["code"], origin="https://evil.com", data_dir=tmp_path)

    srv = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        r_code = requests.post(f"{base}/api/companion/pair-code", json={}, headers={"Origin": base}, timeout=5)
        assert r_code.status_code == 200
        code = r_code.json()["code"]

        # Local caller or URI dispatcher passing untrusted body.origin MUST be rejected (403)
        r_bad = requests.post(
            f"{base}/api/companion/pair",
            json={"code": code, "origin": "https://evil.com"},
            timeout=5,
        )
        assert r_bad.status_code == 403

        # Trusted origin with same unused code still succeeds!
        r_ok = requests.post(
            f"{base}/api/companion/pair",
            json={"code": code, "origin": "https://fightcadelab.com"},
            timeout=5,
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["token"].startswith("fcl_")
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)


# ---------------------------------------------------------------------------
# 3. Asset Installer HTTPS / GitHub Prefix / Ed25519 + OTA Lua Anti-Rollback
# ---------------------------------------------------------------------------

def test_asset_installer_rejects_http_and_untrusted_urls_and_verifies_ed25519(tmp_path: Path) -> None:
    fb_dir = tmp_path / "fbneo"
    fb_dir.mkdir()

    # Non-HTTPS URL rejected
    with pytest.raises(ValueError, match="https://"):
        download_verified_asset(
            "http://github.com/blankasballs/fightcade-fbneo-fast/releases/download/v0.1.0/fcadefbneo_fast.exe",
            tmp_path / "out.exe",
            expected_sha256="a" * 64,
        )

    fake_exe = b"MZ_FAST_FBNEO_SIGNED"
    exe_sha = hashlib.sha256(fake_exe).hexdigest()
    sums_bytes = f"{exe_sha}  fcadefbneo_fast.exe\n".encode("utf-8")

    seed, pub = ed25519_generate_keypair()
    pub_b64 = base64.b64encode(pub).decode("ascii")
    sig_b64 = base64.b64encode(ed25519_sign(seed, sums_bytes)).decode("ascii")

    class FakeResp:
        def __init__(self, status: int, content: bytes = b"", json_obj: Any = None) -> None:
            self.status_code = status
            self.content = content
            self._json = json_obj
            self.headers = {"Content-Length": str(len(content))}
            self.text = content.decode("utf-8", errors="ignore")

        def json(self) -> Any:
            return self._json

        def iter_content(self, chunk_size: int = 65536):
            yield self.content

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    # Untrusted exe_url host MUST be rejected
    class EvilAssetSession:
        def get(self, url: str, **_kw: Any) -> FakeResp:
            return FakeResp(
                200,
                json_obj={
                    "tag_name": "v0.1.0",
                    "assets": [
                        {
                            "name": "SHA256SUMS.txt",
                            "browser_download_url": f"{FAST_FBNEO_DOWNLOAD_PREFIX}v0.1.0/SHA256SUMS.txt",
                        },
                        {
                            "name": "fcadefbneo_fast.exe",
                            "browser_download_url": "https://evil.example/fcadefbneo_fast.exe",
                        },
                    ],
                },
            )

    with pytest.raises(ValueError, match="Untrusted"):
        install_fast_fbneo(fb_dir, consent=True, session=EvilAssetSession())  # type: ignore[arg-type]

    # Signed SHA256SUMS.txt + pinned SHA-256 passes, while bad signature or wrong pinned hash fails
    class GoodSignedSession:
        def get(self, url: str, **_kw: Any) -> FakeResp:
            if url.endswith("/releases/latest"):
                return FakeResp(
                    200,
                    json_obj={
                        "tag_name": "v0.1.0",
                        "assets": [
                            {
                                "name": "SHA256SUMS.txt",
                                "browser_download_url": f"{FAST_FBNEO_DOWNLOAD_PREFIX}v0.1.0/SHA256SUMS.txt",
                            },
                            {
                                "name": "fcadefbneo_fast.exe",
                                "browser_download_url": f"{FAST_FBNEO_DOWNLOAD_PREFIX}v0.1.0/fcadefbneo_fast.exe",
                            },
                        ],
                    },
                )
            if url.endswith("SHA256SUMS.txt"):
                return FakeResp(200, sums_bytes)
            if url.endswith("fcadefbneo_fast.exe"):
                return FakeResp(200, fake_exe)
            return FakeResp(404)

    # Wrong signature rejected
    bad_sig = base64.b64encode(b"\x00" * 64).decode("ascii")
    with pytest.raises(ValueError, match="Ed25519"):
        install_fast_fbneo(
            fb_dir,
            consent=True,
            sums_signature_b64=bad_sig,
            public_key_b64=pub_b64,
            session=GoodSignedSession(),  # type: ignore[arg-type]
        )

    # Wrong pinned hash rejected
    with pytest.raises(ValueError, match="Pinned SHA-256"):
        install_fast_fbneo(
            fb_dir,
            consent=True,
            expected_sha256="b" * 64,
            session=GoodSignedSession(),  # type: ignore[arg-type]
        )

    # Valid Ed25519 + pinned SHA-256 succeeds
    res = install_fast_fbneo(
        fb_dir,
        consent=True,
        expected_sha256=exe_sha,
        sums_signature_b64=sig_b64,
        public_key_b64=pub_b64,
        session=GoodSignedSession(),  # type: ignore[arg-type]
    )
    assert res["ok"] is True
    assert (fb_dir / "fcadefbneo_fast.exe").read_bytes() == fake_exe


def test_ota_lua_anti_rollback_blocks_downgrade_unless_explicit(tmp_path: Path) -> None:
    seed, pub = ed25519_generate_keypair()
    pub_b64 = base64.b64encode(pub).decode("ascii")

    f_v2 = {"replay_takeover.lua": b"-- v1.10.0\n"}
    m_v2 = build_signed_lua_pack_manifest(game_id="sfiii3nr1", version="1.10.0", files=f_v2, private_seed=seed)
    verify_and_install_lua_pack(m_v2, fetch_file_fn=lambda rel: f_v2[rel], public_key_b64=pub_b64, data_dir=tmp_path)

    # Semver numeric downgrade 1.10.0 -> 1.2.0 MUST be rejected!
    f_old = {"replay_takeover.lua": b"-- v1.2.0\n"}
    m_old = build_signed_lua_pack_manifest(game_id="sfiii3nr1", version="1.2.0", files=f_old, private_seed=seed)
    with pytest.raises(ValueError, match="downgrade"):
        verify_and_install_lua_pack(m_old, fetch_file_fn=lambda rel: f_old[rel], public_key_b64=pub_b64, data_dir=tmp_path)
    assert resolve_lua_script("replay_takeover.lua", game_id="sfiii3nr1", data_dir=tmp_path).read_bytes() == f_v2["replay_takeover.lua"]

    # Explicit allow_downgrade=True succeeds
    verify_and_install_lua_pack(
        m_old,
        fetch_file_fn=lambda rel: f_old[rel],
        public_key_b64=pub_b64,
        data_dir=tmp_path,
        allow_downgrade=True,
    )
    assert resolve_lua_script("replay_takeover.lua", game_id="sfiii3nr1", data_dir=tmp_path).read_bytes() == f_old["replay_takeover.lua"]


# ---------------------------------------------------------------------------
# 4. Server DNS Rebinding, Tightened CORS, Cross-Origin GET Guard & Security Headers
# ---------------------------------------------------------------------------

def test_server_dns_rebinding_cors_get_auth_and_security_headers(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True)
    (clips_dir / "sample.mp4").write_bytes(b"FAKE_MP4_DATA")

    # Unit check on tightened CORS: cross-port localhost is blocked!
    assert is_allowed_companion_origin("http://localhost:3000", "127.0.0.1:8765") is False
    assert is_allowed_companion_origin("http://127.0.0.1:3000", "127.0.0.1:8765") is False
    assert is_allowed_companion_origin("http://127.0.0.1:8765", "127.0.0.1:8765") is True
    assert is_allowed_companion_origin("https://fightcadelab.com", "127.0.0.1:8765") is True

    srv = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        hosted_origin = "https://fightcadelab.com"

        # 4a. DNS Rebinding: Host: evil.com:8765 -> 403 on GET, POST, OPTIONS
        r_rebind_get = requests.get(f"{base}/api/status", headers={"Host": "evil.com:8765"}, timeout=5)
        assert r_rebind_get.status_code == 403

        r_rebind_post = requests.post(
            f"{base}/api/takeover",
            json={"quark_id": "1728857242828-5771", "frame": 100},
            headers={"Host": "evil.com:8765"},
            timeout=5,
        )
        assert r_rebind_post.status_code == 403

        r_rebind_opt = requests.options(
            f"{base}/api/takeover",
            headers={"Host": "evil.com:8765", "Origin": hosted_origin},
            timeout=5,
        )
        assert r_rebind_opt.status_code == 403

        # Valid Host: localhost:<port> allowed
        r_localhost = requests.get(f"{base}/api/status", headers={"Host": f"localhost:{port}"}, timeout=5)
        assert r_localhost.status_code == 200

        # 4b. Cross-port localhost CORS blocked
        r_xport_opt = requests.options(
            f"{base}/api/takeover",
            headers={"Origin": "http://localhost:3000"},
            timeout=5,
        )
        assert r_xport_opt.status_code == 403
        r_xport_status = requests.get(
            f"{base}/api/status",
            headers={"Origin": "http://localhost:3000"},
            timeout=5,
        )
        assert r_xport_status.headers.get("Access-Control-Allow-Origin") is None

        # 4c. Cross-origin GET guard:
        # Discovery endpoints exempt for allowlisted web origin (without token)
        for pub_path in ("/api/status", "/api/capabilities", "/api/companion/ping"):
            r_pub = requests.get(f"{base}{pub_path}", headers={"Origin": hosted_origin}, timeout=5)
            assert r_pub.status_code == 200, f"{pub_path} should allow cross-origin discovery"
            assert r_pub.headers.get("Access-Control-Allow-Origin") == hosted_origin

        # Private /api/* and /clips/* MUST return 403 cross-origin without pairing token
        for priv_path in ("/api/notes", "/api/matches", "/api/home", "/api/playlists", "/clips/sample.mp4"):
            r_priv = requests.get(f"{base}{priv_path}", headers={"Origin": hosted_origin}, timeout=5)
            assert r_priv.status_code == 403, f"{priv_path} must block unauthenticated cross-origin GET"

        # Pair and verify cross-origin GET with valid token succeeds
        r_code = requests.post(f"{base}/api/companion/pair-code", json={}, headers={"Origin": base}, timeout=5)
        code = r_code.json()["code"]
        r_pair = requests.post(
            f"{base}/api/companion/pair",
            json={"code": code},
            headers={"Origin": hosted_origin},
            timeout=5,
        )
        tok = r_pair.json()["token"]

        r_notes_ok = requests.get(
            f"{base}/api/notes",
            headers={"Origin": hosted_origin, "X-Fightcade-Lab-Token": tok},
            timeout=5,
        )
        assert r_notes_ok.status_code == 200
        assert r_notes_ok.headers.get("Access-Control-Allow-Origin") == hosted_origin

        r_clip_ok = requests.get(
            f"{base}/clips/sample.mp4",
            headers={"Origin": hosted_origin, "Authorization": f"Bearer {tok}"},
            timeout=5,
        )
        assert r_clip_ok.status_code == 200
        assert r_clip_ok.content == b"FAKE_MP4_DATA"

        # 4d. Security headers on HTML, JSON, and static JS assets
        r_html = requests.get(f"{base}/", timeout=5)
        assert r_html.status_code == 200
        assert r_html.headers.get("X-Content-Type-Options") == "nosniff"
        assert r_html.headers.get("X-Frame-Options") == "DENY"
        assert r_html.headers.get("Referrer-Policy") == "no-referrer"
        csp = r_html.headers.get("Content-Security-Policy") or ""
        assert "default-src 'self'" in csp
        assert "object-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

        r_json = requests.get(f"{base}/api/status", timeout=5)
        assert r_json.headers.get("X-Content-Type-Options") == "nosniff"
        assert r_json.headers.get("X-Frame-Options") == "DENY"
        assert r_json.headers.get("Referrer-Policy") == "no-referrer"

        r_js = requests.get(f"{base}/assets/js/state.js", timeout=5)
        assert r_js.status_code == 200
        assert r_js.headers.get("X-Content-Type-Options") == "nosniff"
        assert r_js.headers.get("X-Frame-Options") == "DENY"
        assert r_js.headers.get("Referrer-Policy") == "no-referrer"
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)


# ---------------------------------------------------------------------------
# 5. Frontend JS Static DOM XSS Sweep
# ---------------------------------------------------------------------------

def test_frontend_js_has_zero_html_escaped_js_string_in_event_handlers() -> None:
    # Catches onclick="fn('${esc(val)}')" or onchange="fn('${val}')" inside HTML event attributes
    unsafe_esc_in_single_quotes = re.compile(r"""on[a-z]+\s*=\s*"[^"]*'\$\{esc\([^}]+\)\}'""")
    unsafe_raw_in_single_quotes = re.compile(r"""on[a-z]+\s*=\s*"[^"]*'\$\{[^}]+\}'""")

    js_files = sorted(WEB_JS_DIR.glob("*.js"))
    assert js_files, f"No JS files found in {WEB_JS_DIR}"

    violations: list[str] = []
    for js_path in js_files:
        text = js_path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if unsafe_esc_in_single_quotes.search(line) or unsafe_raw_in_single_quotes.search(line):
                violations.append(f"{js_path.name}:{line_no}: {line.strip()}")

    assert not violations, "Found unsafe inline event handler JS string interpolations:\n" + "\n".join(violations)

    # Also confirm safeHref exists in state.js
    state_js = (WEB_JS_DIR / "state.js").read_text(encoding="utf-8")
    assert "function safeHref(" in state_js
