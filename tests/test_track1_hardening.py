"""Comprehensive unit & HTTP tests for Track 1: Environment Hardening & Engine Portability.

Covers:
  - Fightcade auto-discovery across Documents, OneDrive, LocalAppData, C:\\Fightcade
  - `config/fcadefbneo.ini` ROM search path parsing & controller config inheritance
  - Space-safe CLI path quoting (`fcadefbneo_fast.exe`) & 8.3 short-path fallback (stock `fcadefbneo.exe`)
  - Missing Grouflon `3rd_training_lua` graceful degradation
  - `FC_ENABLE_PRO_CORPUS` opt-in feature gate (`GET /api/capabilities`, `403` seal-off)
  - Fightcade stewardship safeguards (User-Agent, 30s->2m->10m->pause backoff + Retry-After,
    incremental stop-at-known-quark_id pagination, 60s Refresh cooldown, concurrency=1 +
    10-15s jittered stream cooldown, hourly backfill cap)
  - Sane local defaults (`127.0.0.1:8765`, `--lan`, port-in-use exit code 2, 5 GB rolling disk cap
    protecting Saved Moments & Playlists)
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any

import pandas as pd
import pytest
import requests

from fightcade_analysis.env_config import (
    DEFAULT_DISK_CACHE_CAP_GB,
    build_fbneo_cmdline,
    discover_fbneo_dir,
    discover_fightcade_dir,
    discover_grouflon_dir,
    ensure_fbneo_config_inheritance,
    find_rom_file,
    format_fbneo_cli_arg,
    get_disk_cache_cap_gb,
    is_pro_corpus_enabled,
    parse_fbneo_rom_paths,
)
from fightcade_analysis.fetch_replays import (
    DEFAULT_BACKOFF_SCHEDULE,
    DEFAULT_UA,
    FightcadeRateLimitError,
    _request_with_backoff,
    fetch_quarks,
    parse_retry_after,
)
from fightcade_analysis.match_catalog import (
    HourlyBackfillCapError,
    ProcessingQueue,
    RefreshCooldownError,
    refresh_match_history,
    reset_refresh_cooldowns,
)
from fightcade_analysis.round_renderer import (
    generate_replay_overlays,
    get_mobile_replays_disk_usage,
    get_protected_replay_quark_ids,
    prune_old_mobile_replays,
)
from fightcade_analysis.server import compute_capabilities, create_server, main as server_main
from fightcade_analysis.takeover import TakeoverManager


# ---------------------------------------------------------------------------
# 1. Fightcade Auto-Discovery, INI ROM Paths & Controller Config Inheritance
# ---------------------------------------------------------------------------

def test_discover_fightcade_onedrive_and_standard_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_user = tmp_path / "John Doe"
    onedrive_fbneo = fake_user / "OneDrive" / "Documents" / "Fightcade" / "emulator" / "fbneo"
    onedrive_fbneo.mkdir(parents=True)
    (onedrive_fbneo / "fcadefbneo.exe").write_bytes(b"MZ")

    monkeypatch.delenv("FC_FIGHTCADE_DIR", raising=False)
    monkeypatch.delenv("FC_FBNEO_DIR", raising=False)
    monkeypatch.delenv("FC_FBNEO_EXE", raising=False)
    monkeypatch.delenv("FC_FAST_FBNEO_EXE", raising=False)
    monkeypatch.setenv("USERPROFILE", str(fake_user))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.delenv("OneDrive", raising=False)
    monkeypatch.delenv("OneDriveConsumer", raising=False)

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    fc_dir = discover_fightcade_dir(data_dir=data_dir)
    fb_dir = discover_fbneo_dir(data_dir=data_dir)
    assert fc_dir == fake_user / "OneDrive" / "Documents" / "Fightcade"
    assert fb_dir == onedrive_fbneo


def test_parse_fbneo_ini_rom_paths_and_find_rom_in_custom_dir(tmp_path: Path) -> None:
    fbneo_dir = tmp_path / "Fightcade" / "emulator" / "fbneo"
    custom_roms = tmp_path / "My Custom Arcade ROMs"
    (fbneo_dir / "config").mkdir(parents=True)
    custom_roms.mkdir(parents=True)
    rom_zip = custom_roms / "sfiii3nr1.zip"
    rom_zip.write_bytes(b"PK\x03\x04dummy")

    (fbneo_dir / "config" / "fcadefbneo.ini").write_text(
        "// Fightcade FBNeo Main Config\n"
        f"szAppRomPaths[0] {custom_roms}\\\n"
        "szAppRomPaths[1] \n"
        "szAppRomPaths[2] roms/cps3/\n",
        encoding="utf-8",
    )

    dirs = parse_fbneo_rom_paths(fbneo_dir)
    assert dirs[0] == custom_roms
    assert (fbneo_dir / "roms" / "cps3").resolve() in dirs

    found, searched = find_rom_file("sfiii3nr1", fbneo_dir=fbneo_dir)
    assert found == rom_zip
    assert custom_roms in searched


def test_config_and_controller_inheritance_across_dirs(tmp_path: Path) -> None:
    stock_dir = tmp_path / "Fightcade" / "emulator" / "fbneo"
    (stock_dir / "config" / "games").mkdir(parents=True)
    (stock_dir / "config" / "presets").mkdir(parents=True)
    (stock_dir / "config" / "fcadefbneo.ini").write_text(
        "szAppRomPaths[0] roms/\n"
        "nEnableSOCD 2\n",
        encoding="utf-8",
    )
    (stock_dir / "config" / "games" / "sfiii3nr1.ini").write_text(
        'input "P1 Weak Punch" switch 0x4000\n'
        'input "P1 Coin" switch 0x4008\n',
        encoding="utf-8",
    )

    # Case A: Isolated fcadefbneo_fast.exe in separate companion bin dir
    bin_dir = tmp_path / "FightcadeLab" / "bin"
    bin_dir.mkdir(parents=True)
    fast_exe = bin_dir / "fcadefbneo_fast.exe"
    fast_exe.write_bytes(b"MZ")

    res = ensure_fbneo_config_inheritance(fast_exe, stock_fbneo_dir=stock_dir, game_id="sfiii3nr1")
    assert res["inherited_app_ini"] is True
    assert res["inherited_game_ini"] is True
    assert res["same_dir"] is False

    inherited_app = (bin_dir / "config" / "fcadefbneo_fast.ini").read_text(encoding="utf-8")
    expected_abs = str((stock_dir / "roms").resolve()) + "\\"
    assert f"szAppRomPaths[0] {expected_abs}" in inherited_app
    assert "nEnableSOCD 2" in inherited_app

    inherited_ctrl = (bin_dir / "config" / "games" / "sfiii3nr1.ini").read_text(encoding="utf-8")
    assert 'input "P1 Coin" switch 0x4008' in inherited_ctrl


# ---------------------------------------------------------------------------
# 2. Space-Safe CLI Arguments & Takeover Launch With Spaces
# ---------------------------------------------------------------------------

def test_space_safe_cli_quoting_fast_vs_stock_short_path(tmp_path: Path) -> None:
    fast_exe = tmp_path / "fcadefbneo_fast.exe"
    stock_exe = tmp_path / "fcadefbneo.exe"
    spaced_dir = tmp_path / "John Doe" / "OneDrive Folder"
    spaced_dir.mkdir(parents=True)
    fs_file = spaced_dir / "match state.fs"
    lua_file = spaced_dir / "replay takeover.lua"
    fs_file.write_bytes(b"FS")
    lua_file.write_text("-- lua", encoding="utf-8")

    # Fast exe quotes arguments containing spaces safely (main.cpp:892 fixed)
    cmd_fast = build_fbneo_cmdline(fast_exe, ["-w", "-headless"], fs_file, lua_file, target_is_path=True)
    assert f'"{fs_file}"' in cmd_fast
    assert f'"{lua_file}"' in cmd_fast

    # Stock exe uses 8.3 short-path resolver (unquoted, zero whitespace)
    fake_short = lambda p: str(p).replace("John Doe", "JOHNDO~1").replace("OneDrive Folder", "ONEDRI~1").replace(" ", "_")
    arg_stock = format_fbneo_cli_arg(fs_file, exe_path=stock_exe, is_path=True, short_path_fn=fake_short)
    assert " " not in arg_stock
    assert '"' not in arg_stock

    # If 8.3 names are disabled on stock exe, raises clear actionable error mentioning fcadefbneo_fast.exe
    with pytest.raises(ValueError, match="fcadefbneo_fast.exe"):
        format_fbneo_cli_arg(fs_file, exe_path=stock_exe, is_path=True, short_path_fn=lambda p: str(p))


def test_takeover_launches_cleanly_with_spaces_and_missing_grouflon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spaced_root = tmp_path / "Users With Spaces" / "My Fightcade"
    fb_dir = spaced_root / "emulator" / "fbneo"
    fb_dir.mkdir(parents=True)
    fast_exe = fb_dir / "fcadefbneo_fast.exe"
    fast_exe.write_bytes(b"MZ")
    lua_file = spaced_root / "lua dir" / "replay_takeover.lua"
    lua_file.parent.mkdir(parents=True)
    lua_file.write_text("-- lua", encoding="utf-8")

    data_dir = spaced_root / "data dir"
    sav_dir = data_dir / "savestates"
    sav_dir.mkdir(parents=True)
    fs_file = sav_dir / "1795443936010-2584_gf16938.fs"
    fs_file.write_bytes(b"FS" * 4096)

    # Ensure Grouflon is absent (neither env nor fb_dir)
    monkeypatch.delenv("FC_GROUFLON_DIR", raising=False)
    monkeypatch.setenv("FC_FBNEO_DIR", str(fb_dir))

    launched: list[tuple[str, dict[str, Any]]] = []

    class FakeProc:
        pid = 1337
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    mgr = TakeoverManager(
        data_dir=data_dir,
        fbneo_exe=fast_exe,
        replay_takeover_lua=lua_file,
        seek_lua=lua_file,
        popen_fn=lambda cmd, **kw: (launched.append((cmd, kw)) or FakeProc()),
    )

    res = mgr.launch({"quark_id": "1795443936010-2584", "frame": 16938, "player": 1})
    assert res["ok"] is True
    assert res["method"] == "instant_savestate"
    assert len(launched) == 1
    cmd_str, kw = launched[0]
    assert f'"{fs_file.resolve()}"' in cmd_str
    assert f'"{lua_file.resolve()}"' in cmd_str
    assert kw["env"]["FC_GROUFLON_TRAINING"] == "0"
    assert discover_grouflon_dir(fast_exe, data_dir=data_dir) is None


# ---------------------------------------------------------------------------
# 3. Missing Grouflon Graceful Degradation & Pro-Corpus Feature Gate
# ---------------------------------------------------------------------------

def test_missing_grouflon_capabilities_and_overlay_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fb_dir = tmp_path / "fbneo_no_grouflon"
    fb_dir.mkdir()
    fast_exe = fb_dir / "fcadefbneo_fast.exe"
    fast_exe.write_bytes(b"MZ")

    monkeypatch.delenv("FC_GROUFLON_DIR", raising=False)
    monkeypatch.setenv("FC_FBNEO_DIR", str(fb_dir))

    caps = compute_capabilities(tmp_path, fbneo_exe=fast_exe)
    assert caps["has_grouflon"] is False
    assert caps["can_generate_overlays"] is False
    assert caps["can_takeover"] is True
    assert caps["can_render"] is True

    with pytest.raises(FileNotFoundError, match="3rd_training_lua-master"):
        generate_replay_overlays("1795443936010-2584", data_dir=tmp_path, fbneo_exe=fast_exe)


def test_pro_corpus_feature_gate_default_off_and_opt_in_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Seed a local pro parquet file so corpus exists on disk
    pq_dir = tmp_path / "parquet"
    pq_dir.mkdir(parents=True)
    pd.DataFrame({"game_frame": [100, 101]}).to_parquet(pq_dir / "pro_1.parquet", index=False)

    monkeypatch.delenv("FC_ENABLE_PRO_CORPUS", raising=False)
    # Without explicit opt-in, pro_corpus_enabled MUST default to False
    assert is_pro_corpus_enabled(tmp_path, env_path=tmp_path / ".env_missing") is False

    # HTTP server with default gate seals off POST /api/pro-search & /api/pro-search/render-clip
    srv_off = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False, pro_corpus_enabled=False)
    port_off = srv_off.server_address[1]
    t_off = threading.Thread(target=srv_off.serve_forever, daemon=True)
    t_off.start()
    try:
        base = f"http://127.0.0.1:{port_off}"
        caps = requests.get(f"{base}/api/capabilities", timeout=5).json()
        assert caps["pro_corpus_enabled"] is False

        r1 = requests.post(
            f"{base}/api/pro-search",
            json={"quark_id": "1795443936010-2584", "perspective": 1, "start_gf": 100, "end_gf": 160},
            headers={"Origin": base},
            timeout=5,
        )
        assert r1.status_code == 403
        assert "roadmap" in r1.json()["error"].lower()

        r2 = requests.post(
            f"{base}/api/pro-search/render-clip",
            json={"quark_id": "1728857242828-5771", "frame": 19501},
            headers={"Origin": base},
            timeout=5,
        )
        assert r2.status_code == 403

        # Insights endpoints are also sealed off by default (403)
        r_ins = requests.get(f"{base}/api/insights", timeout=5)
        assert r_ins.status_code == 403
        assert "roadmap" in r_ins.json()["error"].lower()

        r_ins_item = requests.get(f"{base}/api/insights/2026-11-23_sample_report", timeout=5)
        assert r_ins_item.status_code == 403

        r_ins_run = requests.post(
            f"{base}/api/insights/run",
            json={"kind": "verified_habits"},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_ins_run.status_code == 403

        # Home pulse omits latest_insight while gated
        r_home = requests.get(f"{base}/api/home", timeout=5)
        assert r_home.status_code == 200
        assert r_home.json()["pulse"]["latest_insight"] is None
    finally:
        srv_off.shutdown()
        srv_off.server_close()
        t_off.join(timeout=3)

    # Explicit opt-in via FC_ENABLE_PRO_CORPUS=1 unlocks it
    monkeypatch.setenv("FC_ENABLE_PRO_CORPUS", "1")
    assert is_pro_corpus_enabled(tmp_path, env_path=tmp_path / ".env_missing") is True
    caps_on = compute_capabilities(tmp_path)
    assert caps_on["pro_corpus_enabled"] is True


# ---------------------------------------------------------------------------
# 4. Fightcade Stewardship & Rate-Limiting Safeguards
# ---------------------------------------------------------------------------

def test_user_agent_and_backoff_ladder_with_retry_after() -> None:
    assert DEFAULT_UA.startswith("FightcadeLab-Companion/")
    assert "fightcadelab.com" in DEFAULT_UA
    assert DEFAULT_BACKOFF_SCHEDULE == (30.0, 120.0, 600.0)
    assert parse_retry_after("45") == 45.0

    class FakeResp:
        def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
            self.status_code = status
            self.headers = headers or {}

    class FakeSession:
        def __init__(self, responses: list[FakeResp]) -> None:
            self._responses = list(responses)
            self.calls = 0

        def request(self, method: str, url: str, **kwargs: Any) -> FakeResp:
            self.calls += 1
            return self._responses.pop(0)

    # 429 (Retry-After: 90) -> 403 -> 503 -> 429 (pause!)
    sleeps: list[float] = []
    sess = FakeSession([
        FakeResp(429, {"Retry-After": "90"}),
        FakeResp(403),
        FakeResp(503),
        FakeResp(429),
    ])
    with pytest.raises(FightcadeRateLimitError) as exc_info:
        _request_with_backoff(
            sess,  # type: ignore[arg-type]
            "POST",
            "https://web.fightcade.com/api/",
            backoff_schedule=(30.0, 120.0, 600.0),
            sleep_fn=sleeps.append,
            jitter_fn=lambda a, b: 0.0,
        )
    assert exc_info.value.status_code == 429
    # Step 1 honored Retry-After=90 (> 30s), Step 2 waited 120s, Step 3 waited 600s, Step 4 paused!
    assert sleeps == [90.0, 120.0, 600.0]
    assert sess.calls == 4


def test_fetch_quarks_incremental_stop_at_known_quark_id() -> None:
    pages_requested: list[int] = []

    class FakeResp:
        status_code = 200
        headers: dict[str, str] = {}

        def __init__(self, items: list[dict[str, Any]]) -> None:
            self._items = items

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"results": {"results": self._items}}

    class FakeSession:
        headers: dict[str, str] = {}

        def request(self, method: str, url: str, **kwargs: Any) -> FakeResp:
            offset = int(kwargs["json"]["offset"])
            pages_requested.append(offset)
            # Return 50 items so normally fetch_quarks would fetch page 2
            batch = [{"quarkid": f"new_{i}", "gameid": "sfiii3nr1", "duration": 200, "players": []} for i in range(3)]
            batch.append({"quarkid": "known_match_1", "gameid": "sfiii3nr1", "duration": 200, "players": []})
            batch.extend([{"quarkid": f"older_{i}", "gameid": "sfiii3nr1", "duration": 200, "players": []} for i in range(46)])
            return FakeResp(batch)

    items = fetch_quarks(
        username="player1",
        best=False,
        limit=100,
        max_pages=4,
        session=FakeSession(),  # type: ignore[arg-type]
        stop_at_known_ids={"known_match_1"},
        sleep_fn=lambda _: None,
    )
    assert [x["quarkid"] for x in items] == ["new_0", "new_1", "new_2"]
    assert pages_requested == [0]  # Halted immediately on page 1 without fetching page 2!


def test_refresh_60s_cooldown_and_incremental_pass_through(tmp_path: Path) -> None:
    reset_refresh_cooldowns()
    now = [1000.0]
    seen_known_args: list[set[str] | None] = []

    def fake_fetch(**kw: Any) -> list[dict[str, Any]]:
        seen_known_args.append(kw.get("stop_at_known_ids"))
        return [
            {
                "quarkid": f"qid_{int(now[0])}",
                "gameid": "sfiii3nr1",
                "date": int(now[0] * 1000),
                "duration": 180,
                "players": [{"name": "player1", "rank": 5}, {"name": "opp", "rank": 5}],
            }
        ]

    res1 = refresh_match_history(
        "player1",
        data_dir=tmp_path,
        fetch_fn=fake_fetch,
        cooldown_seconds=60.0,
        clock_fn=lambda: now[0],
    )
    assert res1["ok"] is True

    # Calling again 15s later MUST raise RefreshCooldownError (~45s remaining)
    now[0] = 1015.0
    with pytest.raises(RefreshCooldownError) as exc_info:
        refresh_match_history(
            "player1",
            data_dir=tmp_path,
            fetch_fn=fake_fetch,
            cooldown_seconds=60.0,
            clock_fn=lambda: now[0],
        )
    assert 40.0 <= exc_info.value.retry_after_s <= 50.0

    # After 60s elapsed, succeeds AND passes stop_at_known_ids containing the cached match
    now[0] = 1061.0
    res2 = refresh_match_history(
        "player1",
        data_dir=tmp_path,
        fetch_fn=fake_fetch,
        cooldown_seconds=60.0,
        clock_fn=lambda: now[0],
    )
    assert res2["ok"] is True
    assert seen_known_args[-1] == {"qid_1000"}


def test_processing_queue_jittered_cooldown_and_hourly_backfill_cap(tmp_path: Path) -> None:
    now = [5000.0]
    jitter_calls: list[tuple[float, float]] = []

    def fake_jitter(lo: float, hi: float) -> float:
        jitter_calls.append((lo, hi))
        return 0.01  # keep unit test instant while verifying 10..15s range requested

    def fake_scan(qid: str, **kw: Any) -> dict[str, Any]:
        return {"ok": True, "quark_id": qid, "marks_found": 0}

    def fake_render(qid: str, **kw: Any) -> dict[str, Any]:
        return {"round_count": 2}

    q = ProcessingQueue(
        data_dir=tmp_path,
        scan_fn=fake_scan,
        render_fn=fake_render,
        stream_cooldown_range=(10.0, 15.0),
        max_streams_per_hour=2,
        clock_fn=lambda: now[0],
        jitter_fn=fake_jitter,
    )
    try:
        q.enqueue("1795500000001-1001")
        q.enqueue("1795500000002-1002")

        # 3rd online stream within the same hour MUST hit the hourly backfill cap!
        with pytest.raises(HourlyBackfillCapError) as exc_info:
            q.enqueue("1795500000003-1003")
        assert exc_info.value.cap == 2

        # Offline-cached match (Parquet + replay_cache present) MUST NOT be blocked by hourly stream cap!
        cached_qid = "1795500000004-1004"
        (tmp_path / "personal").mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"game_frame": [1, 2], "round_id": [1, 1]}).to_parquet(
            tmp_path / "personal" / f"{cached_qid}.parquet", index=False
        )
        rc_dir = tmp_path / "replay_cache" / cached_qid
        rc_dir.mkdir(parents=True, exist_ok=True)
        (rc_dir / "r01_gf100.fs").write_bytes(b"F" * 120_000)

        offline_job = q.enqueue(cached_qid)
        assert offline_job["quark_id"] == cached_qid

        # Wait briefly for worker to finish
        deadline = time.time() + 5.0
        while time.time() < deadline:
            jobs = q.jobs_by_quark()
            if all(jobs[k]["state"] == "done" for k in ("1795500000001-1001", "1795500000002-1002", cached_qid)):
                break
            time.sleep(0.05)

        assert jitter_calls == [(10.0, 15.0), (10.0, 15.0)]
    finally:
        q.stop()


# ---------------------------------------------------------------------------
# 5. Sane Local Defaults: 127.0.0.1 Bind, Port Conflict & 5 GB Disk Cache Cap
# ---------------------------------------------------------------------------

def test_server_defaults_to_localhost_and_handles_port_collision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Bind an ephemeral socket on 127.0.0.1 to trigger a real port collision
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy_port = blocker.getsockname()[1]
    try:
        rc = server_main(["--port", str(busy_port), "--data-dir", str(tmp_path), "--no-watch-matches"])
        assert rc == 2  # Distinct non-75 exit code so supervisor does not spin-loop
        err = capsys.readouterr().err
        assert "already in use" in err.lower()
        assert str(busy_port) in err
    finally:
        blocker.close()


def test_rolling_disk_cache_cap_protects_saved_moments_and_playlists(tmp_path: Path) -> None:
    assert get_disk_cache_cap_gb(tmp_path) == DEFAULT_DISK_CACHE_CAP_GB == 5.0

    # Create 4 replay directories with staggered timestamps (oldest -> newest)
    replays_root = tmp_path / "replays"
    qids = ["q_oldest_protected_moment", "q_old_unprotected_1", "q_old_protected_playlist", "q_newest_unprotected"]
    base_t = time.time() - 400
    for idx, qid in enumerate(qids):
        d = replays_root / qid
        d.mkdir(parents=True)
        (d / "r01.mp4").write_bytes(b"X" * 10_000)
        mf = d / "manifest.json"
        mf.write_text("{}", encoding="utf-8")
        os.utime(mf, (base_t + idx * 60, base_t + idx * 60))

    # Protect #0 via Saved Moment (`annotations.jsonl`) and #2 via Playlist (`playlists.jsonl`)
    sel_dir = tmp_path / "selections"
    sel_dir.mkdir(parents=True)
    (sel_dir / "annotations.jsonl").write_text(
        json.dumps({"id": "a1", "quark_id": "q_oldest_protected_moment"}) + "\n",
        encoding="utf-8",
    )
    (sel_dir / "playlists.jsonl").write_text(
        json.dumps({"id": "pl1", "name": "AA Lab", "items": [{"quark_id": "q_old_protected_playlist"}]}) + "\n",
        encoding="utf-8",
    )

    protected = get_protected_replay_quark_ids(tmp_path)
    assert protected == {"q_oldest_protected_moment", "q_old_protected_playlist"}

    # Prune down to 25,000 bytes (enough for 2.5 folders -> must evict unprotected oldest, keeping both protected!)
    removed = prune_old_mobile_replays(tmp_path, max_sets=0, max_cache_bytes=25_000)
    assert removed == ["q_old_unprotected_1", "q_newest_unprotected"]
    assert (replays_root / "q_oldest_protected_moment").is_dir()
    assert (replays_root / "q_old_protected_playlist").is_dir()
    assert not (replays_root / "q_old_unprotected_1").exists()

    usage = get_mobile_replays_disk_usage(tmp_path)
    assert usage["match_count"] == 2
    assert usage["protected_count"] == 2
    assert usage["cap_gb"] == 5.0


def test_http_settings_endpoint_and_ui_hardening_markers(tmp_path: Path) -> None:
    srv = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False, pro_corpus_enabled=False)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        r_get = requests.get(f"{base}/api/settings", timeout=5)
        assert r_get.status_code == 200
        assert r_get.json()["disk_cache_cap_gb"] == 5.0

        r_patch = requests.patch(
            f"{base}/api/settings",
            json={"disk_cache_cap_gb": 8.5},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_patch.status_code == 200
        assert r_patch.json()["disk_cache_cap_gb"] == 8.5

        # Verify web UI assets expose Grouflon banner, lazy-bake loop summary, and Top-Rank roadmap gate
        html_txt = requests.get(f"{base}/", timeout=5).text
        assert "grouflonBanner" in html_txt
        assert "composerAutoLoopHint" not in html_txt
        assert "Create Takeover" not in html_txt
        assert "drillToggle" not in html_txt
        assert "studioMatchupHeader" in html_txt
        assert "btnPracticeTakeover" not in html_txt
        assert "studioTakeoverTip" not in html_txt
        assert "btnFindProAnswers" not in html_txt
        assert "Top-Rank Search" in html_txt
        assert "Find Pro Answers" not in html_txt
        assert "Pro Study Reel" not in html_txt

        state_js = requests.get(f"{base}/assets/js/state.js", timeout=5).text
        assert "On the roadmap" in state_js
        assert "isMobileMode" in state_js
        assert 'pro_clip: "Top-Rank Clip"' in state_js
        assert "studioEditId" in state_js
        studio_js = requests.get(f"{base}/assets/js/studio.js", timeout=5).text
        assert "startStudioInlineEdit" in studio_js
        assert "saveStudioInlineEdit" in studio_js
        assert "inline-note-textarea" in studio_js
        assert "Top-Rank Clips" not in studio_js
        assert "Pro Search is on the roadmap." not in studio_js
        pop_js = requests.get(f"{base}/assets/js/status_popover.js", timeout=5).text
        assert "Replay Storage" in pop_js
        assert "Cloud Sharing" in pop_js
        assert "Setup &amp; Diagnostics" in pop_js
        assert "P1 Coin" not in pop_js
        assert "positionStatusPopover" in pop_js
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)
