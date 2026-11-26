"""Tests for the LAN/VPN Range-capable Reports HTTP server."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import requests

from fightcade_analysis.server import (
    ReloadTriggerWatcher,
    create_server,
    resolve_safe_asset_path,
    resolve_safe_data_path,
)
from fightcade_analysis.takeover import TakeoverManager, validate_takeover_payload


def test_resolve_safe_asset_path_blocks_traversal_and_non_assets(tmp_path: Path) -> None:
    assert resolve_safe_asset_path("/assets/styles.css") is not None
    assert resolve_safe_asset_path("/assets/js/app.js") is not None
    assert resolve_safe_asset_path("/assets/../server.py") is None
    assert resolve_safe_asset_path("/assets/js/../../server.py") is None
    assert resolve_safe_asset_path("/assets/index.html") is None  # .html not allowlisted under /assets/
    assert resolve_safe_asset_path("/not-assets/styles.css") is None


def test_resolve_safe_data_path_blocks_traversal_and_private_dirs(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "clips").mkdir()
    (tmp_path / "parquet").mkdir()
    (tmp_path / "personal").mkdir()

    assert resolve_safe_data_path("/reports/index.html", tmp_path) == (tmp_path / "reports" / "index.html").resolve()
    assert resolve_safe_data_path("/clips/test.mp4", tmp_path) == (tmp_path / "clips" / "test.mp4").resolve()
    assert resolve_safe_data_path("/parquet/secret.parquet", tmp_path) is None
    assert resolve_safe_data_path("/personal/my.parquet", tmp_path) is None
    assert resolve_safe_data_path("/reports/../parquet/secret.parquet", tmp_path) is None
    assert resolve_safe_data_path("/reports/gemini_summary_cache.json", tmp_path) is None


def test_reports_http_server_serves_hub_reports_and_mp4_byte_ranges(tmp_path: Path) -> None:
    rep_dir = tmp_path / "reports"
    clips_dir = tmp_path / "clips"
    rep_dir.mkdir()
    clips_dir.mkdir()

    # Seed one minimal report JSON + HTML and one fake MP4 payload
    stem = "2026-11-23_player1_akuma_vs_inertial97_alex_2584"
    (rep_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "report_type": "full_set",
                "target_quark_id": "1795443936010-2584",
                "perspective": 1,
                "match_date": "2026-11-23",
                "self_player_name": "player1",
                "opp_player_name": "inertial97",
                "self_char_name": "Akuma",
                "opp_char_name": "Alex",
                "total_decision_points": 10,
                "moments": [{"regret": 0.42}],
            }
        ),
        encoding="utf-8",
    )
    (rep_dir / f"{stem}.html").write_text("<html><body><h1>Test Report</h1></body></html>", encoding="utf-8")
    mp4_bytes = bytes(range(256)) * 4  # 1024 bytes
    (clips_dir / "sample.mp4").write_bytes(mp4_bytes)

    server = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=True, pro_corpus_enabled=True)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # 1. Root serves unified Fightcade Lab web app directly (200 OK)
        r_root = requests.get(f"{base}/", allow_redirects=False, timeout=5)
        assert r_root.status_code == 200
        assert "Fightcade Lab" in r_root.text
        assert "/assets/styles.css" in r_root.text
        assert "/assets/js/app.js" in r_root.text

        # 1b. Static frontend assets serve cleanly; traversal is blocked (403)
        r_css = requests.get(f"{base}/assets/styles.css", timeout=5)
        assert r_css.status_code == 200
        assert 'html[data-theme="light"]' in r_css.text
        r_trav = requests.get(f"{base}/assets/..%2Fserver.py", timeout=5)
        assert r_trav.status_code == 403

        # 1c. Capabilities endpoint reports structured flags
        r_caps = requests.get(f"{base}/api/capabilities", timeout=5)
        assert r_caps.status_code == 200
        caps = r_caps.json()
        assert caps["ok"] is True
        assert caps["mode"] == "local_companion"
        assert isinstance(caps["can_takeover"], bool)
        assert isinstance(caps["has_pro_corpus"], bool)

        # 1d. Deprecated Reports Hub redirects cleanly to Fightcade Lab Insights
        r_legacy = requests.get(f"{base}/reports/index.html", allow_redirects=False, timeout=5)
        assert r_legacy.status_code == 302
        assert r_legacy.headers["Location"] == "/#insights"

        # 2. Individual report URLs onboard into Fightcade Lab native Report Viewer (?raw=1 serves raw HTML)
        r_rep_redir = requests.get(f"{base}/reports/{stem}.html", allow_redirects=False, timeout=5)
        assert r_rep_redir.status_code == 302
        assert r_rep_redir.headers["Location"] == f"/#report/{stem}"

        r_rep_raw = requests.get(f"{base}/reports/{stem}.html?raw=1", timeout=5)
        assert r_rep_raw.status_code == 200
        assert "Test Report" in r_rep_raw.text

        # 2b. Native Report JSON API serves structured report payload
        r_rep_api = requests.get(f"{base}/api/insights/{stem}", timeout=5)
        assert r_rep_api.status_code == 200
        assert r_rep_api.json()["report"]["id"] == stem
        assert r_rep_api.json()["report"]["report_type"] == "full_set"

        # 3. MP4 supports HTTP 206 Partial Content byte ranges (required for mobile/Safari video)
        r_range = requests.get(f"{base}/clips/sample.mp4", headers={"Range": "bytes=100-199"}, timeout=5)
        assert r_range.status_code == 206
        assert r_range.headers.get("Accept-Ranges") == "bytes"
        assert r_range.headers.get("Content-Range") == "bytes 100-199/1024"
        assert r_range.content == mp4_bytes[100:200]

        # 4. Private paths are strictly forbidden (403)
        r_forbid = requests.get(f"{base}/parquet/anything.parquet", timeout=5)
        assert r_forbid.status_code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_takeover_api_endpoint_and_security_guards(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "clips").mkdir()
    captured: list[dict] = []

    def fake_launcher(payload: dict) -> dict:
        req = validate_takeover_payload(payload)
        captured.append(req)
        return {
            "ok": True,
            "pid": 4242,
            "method": "instant_savestate",
            "quark_id": req["quark_id"],
            "frame": req["frame"],
            "player": req["player"],
            "mode": req["mode"],
            "interactive_desktop": True,
            "warning": None,
        }

    server = create_server(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        auto_refresh_index=False,
        takeover_launcher=fake_launcher,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # 1. GET /api/status works
        r_st = requests.get(f"{base}/api/status", timeout=5)
        assert r_st.status_code == 200
        st_body = r_st.json()
        assert st_body["takeover_enabled"] is True
        assert isinstance(st_body["pid"], int)
        assert bool(st_body["started_at"])

        # 2. Valid same-origin POST /api/takeover succeeds
        r_ok = requests.post(
            f"{base}/api/takeover",
            json={"quark_id": "1795499797641-7540", "frame": 14174, "player": 2, "label": "Missed Anti-Air"},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_ok.status_code == 200
        body = r_ok.json()
        assert body["ok"] is True
        assert body["pid"] == 4242
        assert captured[-1]["quark_id"] == "1795499797641-7540"
        assert captured[-1]["frame"] == 14174
        assert captured[-1]["player"] == 2

        # 3. Cross-origin CSRF attempt is blocked (403)
        r_csrf = requests.post(
            f"{base}/api/takeover",
            json={"quark_id": "1795499797641-7540", "frame": 14174, "player": 1},
            headers={"Origin": "https://evil.example"},
            timeout=5,
        )
        assert r_csrf.status_code == 403

        # 4. Non-JSON Content-Type is rejected (415)
        r_ct = requests.post(
            f"{base}/api/takeover",
            data="quark_id=1795499797641-7540",
            headers={"Content-Type": "text/plain"},
            timeout=5,
        )
        assert r_ct.status_code == 415

        # 5. Invalid quark_id / command injection attempt is rejected (400)
        r_bad = requests.post(
            f"{base}/api/takeover",
            json={"quark_id": "123; calc.exe", "frame": 100, "player": 1},
            timeout=5,
        )
        assert r_bad.status_code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_takeover_manager_instant_savestate_and_replace_previous(tmp_path: Path) -> None:
    # Use a whitespace-free directory inside tmp_path (pytest tmp_path on Windows has no spaces)
    sav_dir = tmp_path / "savestates"
    sav_dir.mkdir()
    fs_path = sav_dir / "1795499797641-7540_gf14174.fs"
    inp_path = sav_dir / "1795499797641-7540_gf14174.inputs"
    fs_path.write_bytes(b"fakefs")
    inp_path.write_text("game_frame,p1_mask,p2_mask\n14100,0,0\n", encoding="utf-8")

    fake_exe = tmp_path / "fcadefbneo.exe"
    fake_exe.write_bytes(b"exe")
    fake_lua = tmp_path / "replay_takeover.lua"
    fake_lua.write_text("-- lua", encoding="utf-8")

    spawned: list[tuple[str, dict]] = []

    class FakeProc:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            self.terminated = True

    procs: list[FakeProc] = []

    def fake_popen(cmd: str, **kwargs):
        spawned.append((cmd, kwargs))
        p = FakeProc(1000 + len(procs))
        procs.append(p)
        return p

    mgr = TakeoverManager(
        data_dir=tmp_path,
        fbneo_exe=fake_exe,
        replay_takeover_lua=fake_lua,
        seek_lua=fake_lua,
        popen_fn=fake_popen,
    )

    r1 = mgr.launch({"quark_id": "1795499797641-7540", "frame": 14174, "player": 2, "label": "Missed Anti-Air", "speed": 0.5, "speed_scope": "takeover_only"})
    assert r1["ok"] is True
    assert r1["method"] == "instant_savestate"
    assert r1["speed"] == 0.5
    assert r1["replaced_previous"] is False
    cmd1, kw1 = spawned[0]
    # Savestate and lua path MUST NOT be double-quoted (FBNeo main.cpp:892 bug)
    assert f"-w {fs_path.resolve()} {fake_lua.resolve()}" in cmd1
    assert kw1["env"]["FC_USER_PLAYER"] == "2"
    assert kw1["env"]["FC_TARGET_FRAME"] == "14174"
    assert kw1["env"]["FC_INPUTS_PATH"] == str(inp_path.resolve())
    assert kw1["env"]["FC_SPEED"] == "0.50"
    assert kw1["env"]["FC_SPEED_SCOPE"] == "takeover_only"

    # Percent speed input normalizes cleanly; invalid speed raises ValueError
    req_pct = validate_takeover_payload({"quark_id": "1795499797641-7540", "frame": 14174, "speed": 75})
    assert req_pct["speed"] == 0.75
    assert req_pct["loop_delay"] is None
    assert req_pct["start_paused"] is None
    assert "FC_LOOP_DELAY" not in kw1["env"]
    assert "FC_START_PAUSED" not in kw1["env"]

    req_ux = validate_takeover_payload({
        "quark_id": "1795499797641-7540",
        "frame": 14174,
        "loop_delay": 3,
        "start_paused": False,
    })
    assert req_ux["loop_delay"] == 3
    assert req_ux["start_paused"] is False

    for bad_payload in (
        {"quark_id": "1795499797641-7540", "frame": 14174, "speed": 0.01},
        {"quark_id": "1795499797641-7540", "frame": 14174, "loop_delay": -1},
        {"quark_id": "1795499797641-7540", "frame": 14174, "loop_delay": 11},
        {"quark_id": "1795499797641-7540", "frame": 14174, "start_paused": "maybe"},
    ):
        try:
            validate_takeover_payload(bad_payload)
            assert False, f"expected ValueError for {bad_payload}"
        except ValueError:
            pass

    # Second launch cleanly terminates first browser-launched takeover & forwards explicit loop_delay/start_paused
    r2 = mgr.launch({"quark_id": "1795499797641-7540", "frame": 14174, "player": 1, "loop_delay": 4, "start_paused": False})
    assert r2["replaced_previous"] is True
    assert procs[0].terminated is True
    assert mgr.active_pid() == procs[1].pid
    _, kw2 = spawned[1]
    assert kw2["env"]["FC_LOOP_DELAY"] == "4"
    assert kw2["env"]["FC_START_PAUSED"] == "0"


def test_reload_trigger_watcher_waits_for_busy_then_shuts_down(tmp_path: Path) -> None:
    import time

    # Pre-existing stale trigger file is cleared on startup without shutting down
    trig = tmp_path / ".reload"
    trig.write_text("stale", encoding="utf-8")

    busy = {"flag": True}
    shutdown_called = threading.Event()
    watcher = ReloadTriggerWatcher(
        data_dir=tmp_path,
        shutdown_fn=shutdown_called.set,
        is_busy_fn=lambda: busy["flag"],
        poll_interval=0.05,
    )
    watcher.start()
    try:
        assert not trig.exists()
        assert not shutdown_called.is_set()

        # Touch trigger while busy -> should defer shutdown
        trig.write_text("reload-now", encoding="utf-8")
        time.sleep(0.15)
        assert not shutdown_called.is_set()
        assert trig.exists()

        # Once idle -> deletes trigger, sets reload_requested, calls shutdown
        busy["flag"] = False
        assert shutdown_called.wait(timeout=2.0)
        assert watcher.reload_requested is True
        assert not trig.exists()
    finally:
        watcher.stop()
