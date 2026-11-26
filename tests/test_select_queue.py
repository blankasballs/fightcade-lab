"""Tests for In-Game Select (Coin) Mark Queue, LiveMatchWatcher, and Queue HTTP API."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import pandas as pd
import requests

from fightcade_analysis.clip_renderer import (
    DEFAULT_FAST_FBNEO_EXE,
    DEFAULT_FBNEO_EXE,
    find_cached_round_savestate,
    resolve_capture_fbneo_exe,
)
from fightcade_analysis.select_queue import (
    LiveMatchWatcher,
    clear_done_items,
    load_queue,
    parse_served_match_cmdline,
    update_queue_item_status,
    upsert_queue_items,
)
from fightcade_analysis.server import create_server
from fightcade_analysis.takeover import TakeoverManager


def test_parse_served_match_cmdline():
    # Fightcade uses 0-based player slots in quark:served (.0 = P1, .1 = P2)
    cmd_p1 = r'C:\Fightcade\emulator\fbneo/fcadefbneo.exe quark:served,sfiii3nr1,1795532676436-8992.0,7000,3,null'
    assert parse_served_match_cmdline(cmd_p1) == {"game_id": "sfiii3nr1", "quark_id": "1795532676436-8992", "player": 1}

    cmd_p2 = r'C:\Fightcade\emulator\fbneo/fcadefbneo.exe quark:served,sfiii3nr1,1795532676436-8992.1,7000,3,null'
    assert parse_served_match_cmdline(cmd_p2) == {"game_id": "sfiii3nr1", "quark_id": "1795532676436-8992", "player": 2}

    assert parse_served_match_cmdline("fcadefbneo.exe quark:stream,sfiii3nr1,1795-1.7,7100") is None


def test_queue_persistence_enrichment_and_takeover_launch(tmp_path: Path):
    # Seed manifest for opponent name lookup
    (tmp_path / "player1_replays.json").write_text(
        json.dumps({"summaries": [{"quarkid": "1795531097416-1969", "p1_name": "player1", "p2_name": "group2x3m"}]}),
        encoding="utf-8",
    )
    sel_dir = tmp_path / "selections"
    sel_dir.mkdir()
    fs_path = sel_dir / "1795531097416-1969_p1_sel1_gf2020_2380.fs"
    inp_path = sel_dir / "1795531097416-1969_p1_sel1_gf2020_2380.inputs"
    fs_path.write_bytes(b"X" * 8192)
    inp_path.write_text("game_frame,p1_mask,p2_mask\n2020,0,0\n", encoding="utf-8")

    mark = {
        "id": "1795531097416-1969_p1_sel1_gf2020_2380",
        "source": "in_game_select",
        "quark_id": "1795531097416-1969",
        "perspective": 1,
        "round_id": 2,
        "start_gf": 2020,
        "press_gf": 2380,
        "end_gf": 2380,
        "takeover_gf": 2065,
        "lookback_s": 6.0,
        "self_char": 14,
        "opp_char": 2,
        "savestate": str(fs_path),
        "inputs": str(inp_path),
        "note": "In-game Select mark #1 (R2)",
    }

    added = upsert_queue_items([mark], data_dir=tmp_path)
    assert len(added) == 1
    # Duplicate upsert should be ignored
    assert upsert_queue_items([mark], data_dir=tmp_path) == []

    items = load_queue(tmp_path)
    assert len(items) == 1
    assert items[0]["self_char_name"] == "Akuma"
    assert items[0]["opp_char_name"] == "Ryu"
    assert items[0]["opp_player_name"] == "group2x3m"
    assert items[0]["savestate_ready"] is True

    # Verify TakeoverManager launches queued mark using its exact takeover_gf (2065)
    fake_exe = tmp_path / "fcadefbneo.exe"
    fake_exe.write_bytes(b"exe")
    fake_lua = tmp_path / "replay_takeover.lua"
    fake_lua.write_text("-- lua", encoding="utf-8")
    launched: list[tuple[str, dict]] = []

    class FakeProc:
        pid = 777
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    mgr = TakeoverManager(
        data_dir=tmp_path,
        fbneo_exe=fake_exe,
        replay_takeover_lua=fake_lua,
        seek_lua=fake_lua,
        popen_fn=lambda cmd, **kw: (launched.append((cmd, kw)) or FakeProc()),
    )
    res = mgr.launch({"selection_id": mark["id"], "mode": "takeover"})
    assert res["ok"] is True
    assert res["method"] == "instant_savestate"
    assert launched[0][1]["env"]["FC_TAKEOVER_GF"] == "2065"
    assert launched[0][1]["env"]["FC_SAVESTATE_PATH"] == str(fs_path.resolve())

    # Status transitions + clear_done
    updated = update_queue_item_status(mark["id"], "done", data_dir=tmp_path)
    assert updated and updated["status"] == "done"
    assert clear_done_items(tmp_path) == 1
    assert load_queue(tmp_path) == []


def test_live_match_watcher_auto_scans_on_match_exit(tmp_path: Path):
    scans: list[tuple[str, int]] = []
    phase_seq = [
        [{"pid": 999, "game_id": "sfiii3nr1", "quark_id": "1795532676436-8992", "player": 2}],
        [],  # match exited!
    ]
    idx = {"i": 0}

    def fake_list():
        cur = phase_seq[min(idx["i"], len(phase_seq) - 1)]
        idx["i"] += 1
        return cur

    def fake_scan(qid: str, *, perspective: int = 1, **kwargs):
        scans.append((qid, perspective))
        return {"ok": True, "quark_id": qid, "perspective": perspective, "marks_found": 2, "marks_added": 2, "items": [], "elapsed_s": 1.2}

    w = LiveMatchWatcher(
        data_dir=tmp_path,
        poll_interval=0.05,
        post_exit_delay=0.0,
        min_match_seconds=0.0,
        list_matches_fn=fake_list,
        scan_fn=fake_scan,
    )
    finished1 = w._poll_once()
    assert finished1 == []
    assert w.status()["phase"] == "in_match"
    assert w.status()["active_player"] == 2

    finished2 = w._poll_once()
    assert len(finished2) == 1
    w._scan_finished_match(finished2[0])
    assert scans == [("1795532676436-8992", 2)]
    assert w.status()["phase"] == "idle"
    assert "queued 2 Select mark(s)" in w.status()["message"]


def test_server_queue_api_endpoints(tmp_path: Path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "clips").mkdir()

    upsert_queue_items(
        [
            {
                "id": "m1",
                "quark_id": "1795-1",
                "perspective": 1,
                "round_id": 1,
                "start_gf": 1000,
                "press_gf": 1360,
                "end_gf": 1360,
                "takeover_gf": 1045,
                "lookback_s": 6.0,
                "self_char": 14,
                "opp_char": 2,
                "note": "Test mark",
            }
        ],
        data_dir=tmp_path,
    )

    scanned: list[str] = []
    watcher = LiveMatchWatcher(
        data_dir=tmp_path,
        list_matches_fn=lambda: [],
        scan_fn=lambda qid, **kw: (scanned.append(qid) or {"ok": True, "quark_id": qid, "marks_found": 0, "marks_added": 0, "items": [], "elapsed_s": 0.1}),
    )

    server = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=True, match_watcher=watcher)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # Deprecated /reports/index.html redirects cleanly to /#insights
        r_hub = requests.get(f"{base}/reports/index.html", allow_redirects=False, timeout=5)
        assert r_hub.status_code == 302
        assert r_hub.headers.get("Location") == "/#insights"

        # GET /api/queue returns item
        r_q = requests.get(f"{base}/api/queue", timeout=5)
        assert r_q.status_code == 200
        assert len(r_q.json()["items"]) == 1

        # POST /api/queue/status marks item done
        r_st = requests.post(f"{base}/api/queue/status", json={"id": "m1", "status": "done"}, headers={"Origin": base}, timeout=5)
        assert r_st.status_code == 200
        assert r_st.json()["item"]["status"] == "done"

        # POST /api/queue/scan triggers async scan
        r_sc = requests.post(f"{base}/api/queue/scan", json={"quark_id": "1795-1", "player": 1}, headers={"Origin": base}, timeout=5)
        assert r_sc.status_code == 200
        for _ in range(20):
            if scanned:
                break
            time.sleep(0.05)
        assert scanned == ["1795-1"]

        # Cross-origin POST blocked (403)
        r_csrf = requests.post(f"{base}/api/queue/status", json={"action": "clear_done"}, headers={"Origin": "https://evil.example"}, timeout=5)
        assert r_csrf.status_code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_find_cached_round_savestate(tmp_path: Path):
    qid = "1795531097416-1969"
    p_dir = tmp_path / "personal"
    p_dir.mkdir(parents=True)
    pq_path = p_dir / f"{qid}.parquet"
    pd.DataFrame(
        {
            "game_frame": [1607, 2000, 2382, 4100, 4500],
            "round_id": [1, 1, 1, 2, 2],
        }
    ).to_parquet(pq_path, index=False)

    rc_dir = tmp_path / "replay_cache" / qid
    rc_dir.mkdir(parents=True)
    r1_fs = rc_dir / "r01_gf1607.fs"
    r2_fs = rc_dir / "r02_gf4100.fs"
    r3_small = rc_dir / "r03_gf7000.fs"
    r1_fs.write_bytes(b"A" * 150_000)
    r2_fs.write_bytes(b"B" * 150_000)
    r3_small.write_bytes(b"C" * 50_000)  # corrupt/incomplete -> ignored

    hit1 = find_cached_round_savestate(qid, 2382, data_dir=tmp_path)
    assert hit1 is not None
    assert hit1[0] == r1_fs and hit1[1] == 1607 and hit1[2] == pq_path

    hit2 = find_cached_round_savestate(qid, 4500, data_dir=tmp_path)
    assert hit2 is not None
    assert hit2[0] == r2_fs and hit2[1] == 4100

    # Target frame before cached round snapshot -> None
    assert find_cached_round_savestate(qid, 1500, data_dir=tmp_path) is None
    # Unknown quark -> None
    assert find_cached_round_savestate("9999-0", 2000, data_dir=tmp_path) is None


def test_resolve_capture_fbneo_exe(tmp_path: Path):
    custom_stock = tmp_path / "fcadefbneo.exe"
    custom_stock.write_bytes(b"exe")
    exe, maxspeed = resolve_capture_fbneo_exe(custom_stock)
    assert exe == custom_stock and maxspeed is False

    custom_fast = tmp_path / "fcadefbneo_fast.exe"
    custom_fast.write_bytes(b"exe")
    exe2, maxspeed2 = resolve_capture_fbneo_exe(custom_fast)
    assert exe2 == custom_fast and maxspeed2 is True

    # Default path prefers fcadefbneo_fast.exe iff installed
    exe3, maxspeed3 = resolve_capture_fbneo_exe(DEFAULT_FBNEO_EXE)
    if DEFAULT_FAST_FBNEO_EXE.exists():
        assert exe3 == DEFAULT_FAST_FBNEO_EXE and maxspeed3 is True
    else:
        assert exe3 == DEFAULT_FBNEO_EXE and maxspeed3 is False
