"""Track 3 tests: Companion PNA/CORS pairing, fightcadelab:// URI scheme, OTA Ed25519 Lua packs,
consent-based SHA-256 asset installer, packaged Companion Pro-Corpus lock, tray pause & privacy audit.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import threading
from typing import Any
import zipfile

import pytest
import requests

from fightcade_analysis.asset_installer import (
    download_verified_asset,
    get_onboarding_status,
    install_fast_fbneo,
    install_ffmpeg_from_zip,
    parse_sha256sums,
)
from fightcade_analysis.companion_bridge import (
    fetch_remote_companion_config,
    is_allowed_companion_origin,
    is_packaged_companion,
)
from fightcade_analysis.env_config import is_pro_corpus_opt_in
from fightcade_analysis.ota_lua import (
    build_signed_lua_pack_manifest,
    ed25519_generate_keypair,
    ed25519_public_key,
    ed25519_sign,
    ed25519_verify,
    resolve_lua_script,
    verify_and_install_lua_pack,
)
from fightcade_analysis.select_queue import LiveMatchWatcher
from fightcade_analysis.server import create_server
from fightcade_analysis.takeover import TakeoverManager
from fightcade_analysis.tray_app import CMD_QUIT, CMD_TOGGLE_WATCHER, CompanionTrayController
from fightcade_analysis.uri_scheme import (
    build_takeover_uri,
    dispatch_companion_uri,
    parse_companion_uri,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 1. Browser <-> Companion Bridge: PNA, Strict Origin, 1-Click Pairing & Revoke
# ---------------------------------------------------------------------------

def test_companion_pna_cors_and_pairing_lifecycle(tmp_path: Path) -> None:
    launched: list[dict[str, Any]] = []

    def fake_launcher(payload: dict[str, Any]) -> dict[str, Any]:
        launched.append(payload)
        return {"ok": True, "pid": 4242, "method": "stream_seek", "player": payload.get("player", 1)}

    watcher = LiveMatchWatcher(data_dir=tmp_path)
    srv = create_server(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        auto_refresh_index=False,
        takeover_launcher=fake_launcher,
        match_watcher=watcher,
        pro_corpus_enabled=False,
    )
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        hosted_origin = "https://fightcadelab.com"

        # 1a. PNA preflight from allowlisted hosted origin succeeds with PNA header
        r_opt = requests.options(
            f"{base}/api/takeover",
            headers={
                "Origin": hosted_origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Private-Network": "true",
            },
            timeout=5,
        )
        assert r_opt.status_code == 204
        assert r_opt.headers.get("Access-Control-Allow-Origin") == hosted_origin
        assert r_opt.headers.get("Access-Control-Allow-Private-Network") == "true"

        # 1b. Unknown origin blocked on OPTIONS and POST
        r_bad_opt = requests.options(f"{base}/api/takeover", headers={"Origin": "https://evil.example"}, timeout=5)
        assert r_bad_opt.status_code == 403
        r_bad_post = requests.post(
            f"{base}/api/takeover",
            json={"quark_id": "1728857242828-5771", "frame": 19501, "player": 1},
            headers={"Origin": "https://evil.example"},
            timeout=5,
        )
        assert r_bad_post.status_code == 403

        # 1c. Unpaired hosted origin blocked on POST /api/takeover
        r_unpaired = requests.post(
            f"{base}/api/takeover",
            json={"quark_id": "1728857242828-5771", "frame": 19501, "player": 1},
            headers={"Origin": hosted_origin},
            timeout=5,
        )
        assert r_unpaired.status_code == 403

        # 1d. Issue 1-time pairing code locally -> exchange from hosted origin -> unlock takeover
        r_code = requests.post(f"{base}/api/companion/pair-code", json={}, headers={"Origin": base}, timeout=5)
        assert r_code.status_code == 200
        code = r_code.json()["code"]
        assert "fightcadelab.com/pair?code=" in r_code.json()["pair_url"]

        r_pair = requests.post(
            f"{base}/api/companion/pair",
            json={"code": code},
            headers={"Origin": hosted_origin},
            timeout=5,
        )
        assert r_pair.status_code == 200
        tok = r_pair.json()["token"]
        assert tok.startswith("fcl_")

        # Code is single-use
        r_replay = requests.post(
            f"{base}/api/companion/pair",
            json={"code": code},
            headers={"Origin": hosted_origin},
            timeout=5,
        )
        assert r_replay.status_code == 403

        # Ping reports valid token
        r_ping = requests.get(
            f"{base}/api/companion/ping",
            headers={"Origin": hosted_origin, "X-Fightcade-Lab-Token": tok},
            timeout=5,
        )
        assert r_ping.status_code == 200
        assert r_ping.json()["token_valid"] is True

        # Paired hosted origin CAN now launch cross-user Takeover!
        r_tk = requests.post(
            f"{base}/api/takeover",
            json={"quark_id": "1728857242828-5771", "frame": 19501, "player": 2, "bake_if_missing": True},
            headers={"Origin": hosted_origin, "X-Fightcade-Lab-Token": tok},
            timeout=5,
        )
        assert r_tk.status_code == 200
        assert len(launched) == 1
        assert launched[0]["quark_id"] == "1728857242828-5771"

        # Unpair rotates token and immediately blocks old token
        r_unpair = requests.post(f"{base}/api/companion/unpair", json={}, headers={"Origin": base}, timeout=5)
        assert r_unpair.status_code == 200
        r_revoked = requests.post(
            f"{base}/api/takeover",
            json={"quark_id": "1728857242828-5771", "frame": 19501, "player": 1},
            headers={"Origin": hosted_origin, "X-Fightcade-Lab-Token": tok},
            timeout=5,
        )
        assert r_revoked.status_code == 403

        # Watcher pause & onboarding endpoints work locally
        r_pause = requests.post(f"{base}/api/watcher/pause", json={"paused": True}, headers={"Origin": base}, timeout=5)
        assert r_pause.status_code == 200
        assert r_pause.json()["watcher"]["paused"] is True
        assert watcher.is_paused() is True

        r_onb = requests.get(f"{base}/api/onboarding", timeout=5)
        assert r_onb.status_code == 200
        assert "checks" in r_onb.json()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)


# ---------------------------------------------------------------------------
# 2. fightcadelab:// Custom URI Scheme Parsing & Dispatch
# ---------------------------------------------------------------------------

def test_uri_scheme_parse_validation_and_dispatch(tmp_path: Path) -> None:
    uri = build_takeover_uri(
        quark_id="1728857242828-5771",
        round_id=4,
        start_gf=19380,
        end_gf=19740,
        frame=19501,
        player=2,
        speed=0.75,
        label="Akuma vs Chun cr.MK parry",
    )
    assert uri.startswith("fightcadelab://takeover?")
    parsed = parse_companion_uri(uri)
    assert parsed["action"] == "takeover"
    assert parsed["payload"]["quark_id"] == "1728857242828-5771"
    assert parsed["payload"]["frame"] == 19501
    assert parsed["payload"]["player"] == 2
    assert parsed["payload"]["speed"] == 0.75
    assert parsed["payload"]["round_id"] == 4

    # Reject bad schemes, injection chars, unknown actions
    with pytest.raises(ValueError):
        parse_companion_uri("https://fightcadelab.com/takeover?quark_id=123")
    with pytest.raises(ValueError):
        parse_companion_uri("fightcadelab://takeover?quark_id=bad;cmd&frame=100")
    with pytest.raises(ValueError):
        parse_companion_uri("fightcadelab://exec?cmd=calc.exe")

    # Dispatch via running Companion mock
    posted: list[dict[str, Any]] = []

    class FakeResp:
        status_code = 200

        def json(self) -> dict[str, Any]:
            return {"ok": True, "method": "stream_seek"}

    def fake_post(url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float) -> FakeResp:
        posted.append({"url": url, "json": json, "headers": headers})
        return FakeResp()

    res = dispatch_companion_uri(uri, data_dir=tmp_path, post_fn=fake_post)
    assert res["dispatched_via"] == "running_companion"
    assert posted[0]["headers"]["X-Fightcade-Lab-Token"].startswith("fcl_")


# ---------------------------------------------------------------------------
# 3. Signed OTA Lua Script Packs (RFC 8032 Ed25519 + SHA-256 + Path Safety)
# ---------------------------------------------------------------------------

def test_ed25519_rfc8032_vector_and_signed_ota_lua_pack(tmp_path: Path) -> None:
    # RFC 8032 Section 7.1 TEST 1 (empty message)
    rfc_seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    rfc_pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    rfc_sig = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
    )
    assert ed25519_public_key(rfc_seed) == rfc_pub
    assert ed25519_sign(rfc_seed, b"") == rfc_sig
    assert ed25519_verify(rfc_pub, b"", rfc_sig) is True
    assert ed25519_verify(rfc_pub, b"tampered", rfc_sig) is False

    # Sign & install a valid Lua pack
    seed, pub = ed25519_generate_keypair()
    pub_b64 = base64.b64encode(pub).decode("ascii")
    files = {
        "replay_takeover.lua": b"-- verified ota replay takeover v1.0.1\nreturn true\n",
        "sfiii3nr1_map.lua": b"-- verified ota map v1.0.1\nreturn {}\n",
    }
    manifest = build_signed_lua_pack_manifest(
        game_id="sfiii3nr1",
        version="1.0.1",
        files=files,
        private_seed=seed,
    )

    inst = verify_and_install_lua_pack(
        manifest,
        fetch_file_fn=lambda rel: files[rel],
        public_key_b64=pub_b64,
        data_dir=tmp_path,
    )
    assert inst["ok"] is True
    resolved = resolve_lua_script("replay_takeover.lua", game_id="sfiii3nr1", data_dir=tmp_path)
    assert resolved.read_bytes() == files["replay_takeover.lua"]

    # Tampered signature rejected
    bad_sig_manifest = dict(manifest)
    bad_sig_manifest["version"] = "1.0.2"
    with pytest.raises(ValueError, match="signature"):
        verify_and_install_lua_pack(bad_sig_manifest, fetch_file_fn=lambda rel: files[rel], public_key_b64=pub_b64, data_dir=tmp_path)

    # Tampered file bytes (SHA-256 mismatch) rejected without overwriting active pack!
    manifest_v2 = build_signed_lua_pack_manifest(
        game_id="sfiii3nr1",
        version="1.0.2",
        files={"replay_takeover.lua": b"-- legitimate v1.0.2\n"},
        private_seed=seed,
    )
    with pytest.raises(ValueError, match="SHA-256"):
        verify_and_install_lua_pack(
            manifest_v2,
            fetch_file_fn=lambda _rel: b"-- EVIL INJECTED LUA\n",
            public_key_b64=pub_b64,
            data_dir=tmp_path,
        )
    # Active pack MUST still be v1.0.1
    still_v1 = resolve_lua_script("replay_takeover.lua", game_id="sfiii3nr1", data_dir=tmp_path)
    assert still_v1.read_bytes() == files["replay_takeover.lua"]

    # Path traversal & non-.lua extension rejected
    with pytest.raises(ValueError, match="Unsafe"):
        build_signed_lua_pack_manifest(game_id="sfiii3nr1", version="1.0.3", files={"../evil.lua": b"x"}, private_seed=seed)
    with pytest.raises(ValueError, match="Unsafe"):
        build_signed_lua_pack_manifest(game_id="sfiii3nr1", version="1.0.3", files={"payload.exe": b"x"}, private_seed=seed)


# ---------------------------------------------------------------------------
# 4. Consent-Based SHA-256 External Asset Installer & Remote Config
# ---------------------------------------------------------------------------

def test_consent_based_asset_installer_and_remote_stewardship_config(tmp_path: Path) -> None:
    fb_dir = tmp_path / "fbneo"
    fb_dir.mkdir()

    # Consent strictly required
    with pytest.raises(PermissionError):
        install_fast_fbneo(fb_dir, consent=False)
    with pytest.raises(PermissionError):
        install_ffmpeg_from_zip(tmp_path, consent=False, expected_sha256="0" * 64)

    # Prepare synthetic signed assets
    fake_exe = b"MZ" + b"FAST_FBNEO_BINARY" * 100
    fake_lic = b"FinalBurn Neo License Text\n"
    exe_sha = hashlib.sha256(fake_exe).hexdigest()
    lic_sha = hashlib.sha256(fake_lic).hexdigest()
    sums_txt = f"{exe_sha}  fcadefbneo_fast.exe\n{lic_sha}  license.txt\n"
    assert parse_sha256sums(sums_txt)["fcadefbneo_fast.exe"] == exe_sha

    # Prepare synthetic ffmpeg zip
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("ffmpeg-7.0/bin/ffmpeg.exe", b"MZ_FAKE_FFMPEG")
    zip_bytes = zbuf.getvalue()
    zip_sha = hashlib.sha256(zip_bytes).hexdigest()

    class FakeResp:
        def __init__(self, status: int, content: bytes = b"", json_obj: Any = None) -> None:
            self.status_code = status
            self._content = content
            self._json = json_obj
            self.headers = {"Content-Length": str(len(content))}
            self.text = content.decode("utf-8", errors="ignore")

        def json(self) -> Any:
            return self._json

        def iter_content(self, chunk_size: int = 65536):
            yield self._content

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class FakeSession:
        def get(self, url: str, **kwargs: Any) -> FakeResp:
            if url.endswith("/releases/latest"):
                return FakeResp(
                    200,
                    json_obj={
                        "tag_name": "v0.1.0",
                        "assets": [
                            {"name": "SHA256SUMS.txt", "browser_download_url": "https://example.test/SHA256SUMS.txt"},
                            {"name": "fcadefbneo_fast.exe", "browser_download_url": "https://example.test/fcadefbneo_fast.exe"},
                            {"name": "license.txt", "browser_download_url": "https://example.test/license.txt"},
                        ],
                    },
                )
            if url.endswith("SHA256SUMS.txt"):
                return FakeResp(200, sums_txt.encode("utf-8"))
            if url.endswith("fcadefbneo_fast.exe"):
                return FakeResp(200, fake_exe)
            if url.endswith("license.txt"):
                return FakeResp(200, fake_lic)
            if url.endswith("ffmpeg.zip"):
                return FakeResp(200, zip_bytes)
            if url.endswith("/companion/config.json"):
                return FakeResp(
                    200,
                    json_obj={
                        "fightcade_api_enabled": False,
                        "min_refresh_cooldown_s": 120,
                        "max_backfill_per_hour": 4,
                        "pro_corpus_enabled": True,  # MUST be ignored & forced False!
                    },
                )
            return FakeResp(404)

    sess = FakeSession()
    res = install_fast_fbneo(fb_dir, consent=True, session=sess)  # type: ignore[arg-type]
    assert res["ok"] is True
    assert res["license_installed"] is True
    assert (fb_dir / "fcadefbneo_fast.exe").read_bytes() == fake_exe
    assert (fb_dir / "fcadefbneo_fast_license.txt").read_bytes() == fake_lic

    # Bad checksum MUST fail and leave no corrupt file behind
    with pytest.raises(ValueError, match="SHA-256"):
        download_verified_asset(
            "https://example.test/fcadefbneo_fast.exe",
            tmp_path / "corrupt.exe",
            expected_sha256="f" * 64,
            session=sess,  # type: ignore[arg-type]
        )
    assert not (tmp_path / "corrupt.exe").exists()

    # Verified FFmpeg zip extraction
    ff_res = install_ffmpeg_from_zip(
        tmp_path,
        consent=True,
        zip_url="https://example.test/ffmpeg.zip",
        expected_sha256=zip_sha,
        session=sess,  # type: ignore[arg-type]
    )
    assert Path(ff_res["path"]).read_bytes() == b"MZ_FAKE_FFMPEG"

    # Remote stewardship config fetch & cache fallback
    cfg = fetch_remote_companion_config(tmp_path, base_url="https://fightcadelab.com", session=sess)  # type: ignore[arg-type]
    assert cfg["source"] == "remote"
    assert cfg["fightcade_api_enabled"] is False
    assert cfg["max_backfill_per_hour"] == 4
    assert cfg["pro_corpus_enabled"] is False  # Never forced True remotely!


# ---------------------------------------------------------------------------
# 5. Packaged Companion Guard, Licensing Files, Staging Exclusions & Tray Menu
# ---------------------------------------------------------------------------

def test_packaged_companion_locks_pro_corpus_and_staging_excludes_forbidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    sys.path.insert(0, str(REPO_ROOT / "packaging"))
    try:
        import build_companion  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    # Legal files exist at repo root
    lic_path = REPO_ROOT / "LICENSE"
    notices_path = REPO_ROOT / "THIRD_PARTY_NOTICES.md"
    assert lic_path.is_file() and "GNU AFFERO GENERAL PUBLIC LICENSE" in lic_path.read_text(encoding="utf-8")
    assert notices_path.is_file() and "AGPL-3.0-or-later" in notices_path.read_text(encoding="utf-8")

    # FC_PACKAGED_COMPANION=1 strictly seals Pro-Corpus even if FC_ENABLE_PRO_CORPUS=1
    monkeypatch.setenv("FC_PACKAGED_COMPANION", "1")
    monkeypatch.setenv("FC_ENABLE_PRO_CORPUS", "1")
    assert is_packaged_companion() is True
    assert is_pro_corpus_opt_in(tmp_path) is False

    # Staging copies allowlisted assets and blocks forbidden binaries/scripts
    stage_dir = tmp_path / "staged"
    build_companion.stage_companion_assets(stage_dir)
    assert (stage_dir / "LICENSE").is_file()
    assert (stage_dir / "THIRD_PARTY_NOTICES.md").is_file()
    assert (stage_dir / "web" / "index.html").is_file()
    assert (stage_dir / "lua" / "replay_takeover.lua").is_file()

    (stage_dir / "fcadefbneo_fast.exe").write_bytes(b"NO")
    with pytest.raises(RuntimeError, match="Forbidden file"):
        build_companion.verify_clean_companion_dir(stage_dir)

    # Tray controller menu command dispatch (headless-safe)
    ctrl = CompanionTrayController(host="127.0.0.1", port=0, data_dir=tmp_path, watch_matches=False)
    ctrl.start_backend()
    try:
        assert ctrl.is_watcher_paused() is False
        assert ctrl.handle_menu_command(CMD_TOGGLE_WATCHER) is True
        assert ctrl.is_watcher_paused() is True
        assert ctrl.handle_menu_command(CMD_QUIT) is False
    finally:
        ctrl.stop_backend()


def test_cross_user_takeover_uses_remote_meta_cache_before_fightcade_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_exe = tmp_path / "fcadefbneo_fast.exe"
    fake_exe.write_bytes(b"MZ")
    launched_cmds: list[str] = []

    class FakeProc:
        pid = 9999

        def poll(self) -> None:
            return None

    monkeypatch.setattr(
        "fightcade_analysis.takeover.lookup_remote_fightcade_meta",
        lambda qid: {"quark_id": qid, "stream_target": f"quark:stream,sfiii3nr1,{qid}.7,7100"},
    )
    monkeypatch.setattr(
        "fightcade_analysis.takeover.resolve_stream_target",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("Should not hit Fightcade HEAD when meta cached!")),
    )

    mgr = TakeoverManager(
        data_dir=tmp_path,
        fbneo_exe=fake_exe,
        popen_fn=lambda cmd, **_kw: (launched_cmds.append(str(cmd)), FakeProc())[1],
    )
    res = mgr.launch({"quark_id": "1728857242828-5771", "frame": 19501, "player": 1})
    assert res["ok"] is True
    assert res["method"] == "stream_seek"
    assert "quark:stream,sfiii3nr1,1728857242828-5771.7,7100" in launched_cmds[0]
