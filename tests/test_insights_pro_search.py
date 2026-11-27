"""Unit & HTTP tests for Insights catalog, background analysis runner, Pro Search & ad-hoc Takeover bake."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import pandas as pd
import requests

from fightcade_analysis.insights import InsightJobRunner, ProSearchService, list_insights
from fightcade_analysis.server import create_server
from fightcade_analysis.takeover import TakeoverManager


def _seed_reports(tmp_path: Path) -> None:
    rep_dir = tmp_path / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)

    # 1. Verified Habits (hybrid_habit_rollup)
    vh_stem = "2026-11-23_player1_akuma_verified_habits_14sets"
    (rep_dir / f"{vh_stem}.html").write_text("<html><body>Verified Habits</body></html>", encoding="utf-8")
    (rep_dir / f"{vh_stem}.json").write_text(
        json.dumps(
            {
                "report_type": "hybrid_habit_rollup",
                "match_date": "2026-11-23",
                "username": "player1",
                "primary_char_name": "Akuma",
                "num_sets": 14,
                "total_rounds": 199,
                "visually_verified_clips": 19,
                "habits": [
                    {
                        "category": "missed_anti_air",
                        "label": "Missed Anti-Air",
                        "total_hp_lost": 327,
                        "avg_hp_lost_pct": 34.5,
                        "verified_clips": 5,
                        "sets_affected": 5,
                        "total_sets": 14,
                        "episodes": [
                            {
                                "quark_id": "1795443936010-2584",
                                "round_id": 2,
                                "perspective": 1,
                                "t_cause_gf": 17660,
                                "visual_tag": {"one_line_summary": "Late b+HP vs jump-in"},
                            }
                        ],
                        "verified_pro_solutions": [
                            {
                                "quark_id": "1728857242828-5771",
                                "round_id": 4,
                                "perspective": 1,
                                "game_frame": 19501,
                                "self_char_name": "Akuma",
                                "opp_char_name": "Alex",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # 2. Legacy v1 habit rollup (hidden by default unless include_legacy=1)
    leg_stem = "2026-11-22_player1_akuma_habit_rollup_14sets"
    (rep_dir / f"{leg_stem}.html").write_text("<html><body>Legacy</body></html>", encoding="utf-8")
    (rep_dir / f"{leg_stem}.json").write_text(
        json.dumps(
            {
                "report_type": "habit_rollup",
                "match_date": "2026-11-22",
                "username": "player1",
                "primary_char_name": "Akuma",
                "num_sets": 14,
                "total_rounds": 199,
                "total_leaks": 50,
                "habits": [{"interaction": "Anti-Air", "total_regret": 18.75}],
                "sets": [],
            }
        ),
        encoding="utf-8",
    )


def test_list_insights_filters_legacy_and_extracts_seed_moments(tmp_path: Path) -> None:
    _seed_reports(tmp_path)

    res = list_insights(tmp_path, include_legacy=False)
    assert res["ok"] is True
    assert res["counts"]["verified_habits"] == 1
    assert res["counts"]["legacy_rollups"] == 1
    assert len(res["reports"]) == 1  # legacy hidden by default
    lv = res["latest_verified_habits"]
    assert lv is not None
    assert lv["habits"][0]["label"] == "Missed Anti-Air"
    assert len(lv["habits"][0]["seed_moments"]) == 2  # 1 personal episode + 1 pro solution

    res_all = list_insights(tmp_path, include_legacy=True)
    assert len(res_all["reports"]) == 2


def test_http_insights_pro_search_and_insight_runner(tmp_path: Path) -> None:
    _seed_reports(tmp_path)
    (tmp_path / "clips").mkdir(exist_ok=True)
    qid = "1795443936010-2584"
    (tmp_path / "personal").mkdir(exist_ok=True)
    pd.DataFrame({"game_frame": [1000, 1001], "round_id": [1, 1]}).to_parquet(
        tmp_path / "personal" / f"{qid}.parquet", index=False
    )

    # Seed manifest metadata for pro player handle/rank enrichment
    (tmp_path / "pro_replays.json").write_text(
        json.dumps(
            {
                "summaries": [
                    {
                        "quarkid": "1728857242828-5771",
                        "p1_name": "Kuroda",
                        "p1_rank": 6,
                        "p2_name": "MOV",
                        "p2_rank": 6,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    def fake_search(*args, **kwargs):
        return {
            "retrieval_tier": "exact_matchup",
            "rounds_scanned": 24,
            "query": {"quark_id": qid, "perspective": 1, "start_gf": 17620, "end_gf": 17740},
            "matches": [
                {
                    "quark_id": "1728857242828-5771",
                    "perspective": 1,
                    "round_id": 4,
                    "start_gf": 19381,
                    "end_gf": 19651,
                    "mid_gf": 19501,
                    "norm_dist": 0.14,
                    "self_char_name": "Akuma",
                    "opp_char_name": "Alex",
                    "action_coarse": "Anti-Air",
                    "outcome_value": 0.22,
                }
            ],
        }

    def fake_render_clips(reqs, *, clips_dir, savestates_dir, **kw):
        clips_dir.mkdir(parents=True, exist_ok=True)
        savestates_dir.mkdir(parents=True, exist_ok=True)
        req = reqs[0]
        key = f"{req.quark_id}_gf{req.target_frame}"
        mp4 = clips_dir / f"{key}.mp4"
        fs = savestates_dir / f"{key}.fs"
        mp4.write_bytes(b"MP4" * 1000)
        fs.write_bytes(b"FS" * 4000)

        class _Res:
            mp4_path = mp4
            savestate_path = fs

        return ({key: _Res()}, {"rendered": 1})

    pro_svc = ProSearchService(
        data_dir=tmp_path,
        corpus_index=object(),  # type: ignore[arg-type]
        search_fn=fake_search,
        render_clips_fn=fake_render_clips,
    )

    ran_jobs: list[str] = []
    ins_runner = InsightJobRunner(
        data_dir=tmp_path,
        run_match_fn=lambda job: (ran_jobs.append(job["quark_id"]) or {"report_url": "/reports/fake.html"}),
        run_habits_fn=lambda job: (ran_jobs.append(job["username"]) or {"report_url": "/reports/vh.html"}),
    )

    server = create_server(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        auto_refresh_index=False,
        insight_runner=ins_runner,
        pro_search_service=pro_svc,
        pro_corpus_enabled=True,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        hdrs = {"Origin": base}

        # 1. GET /api/insights & GET /api/insights/<id>
        r_ins = requests.get(f"{base}/api/insights", timeout=5)
        assert r_ins.status_code == 200
        lv = r_ins.json()["latest_verified_habits"]
        assert lv["habits"][0]["label"] == "Missed Anti-Air"
        assert lv["href"] == f"/#report/{lv['id']}"

        r_rep = requests.get(f"{base}/api/insights/{lv['id']}", timeout=5)
        assert r_rep.status_code == 200
        rep_body = r_rep.json()["report"]
        assert rep_body["report_type"] == "hybrid_habit_rollup"
        assert rep_body["habits"][0]["label"] == "Missed Anti-Air"
        assert len(rep_body["habits"][0]["examples"]) == 1
        assert len(rep_body["habits"][0]["verified_pro_solutions"]) == 1

        # 2. POST /api/pro-search
        r_ps = requests.post(
            f"{base}/api/pro-search",
            json={"quark_id": qid, "perspective": 1, "start_gf": 17620, "end_gf": 17740},
            headers=hdrs,
            timeout=5,
        )
        assert r_ps.status_code == 200
        ps = r_ps.json()
        assert ps["retrieval_tier"] == "exact_matchup"
        assert ps["matches"][0]["self_player_name"] == "Kuroda"
        assert ps["matches"][0]["self_rank_letter"] == "S"
        assert ps["matches"][0]["has_clip"] is False

        # 3. POST /api/pro-search/render-clip
        r_rc = requests.post(
            f"{base}/api/pro-search/render-clip",
            json={"quark_id": "1728857242828-5771", "frame": 19501},
            headers=hdrs,
            timeout=5,
        )
        assert r_rc.status_code == 200
        assert r_rc.json()["has_clip"] is True
        assert r_rc.json()["clip_url"] == "/clips/1728857242828-5771_gf19501.mp4"

        # 4. POST /api/insights/run validation & execution
        r_bad = requests.post(f"{base}/api/insights/run", json={"kind": "bogus"}, headers=hdrs, timeout=5)
        assert r_bad.status_code == 400

        r_run = requests.post(
            f"{base}/api/insights/run",
            json={"kind": "match_breakdown", "quark_id": qid, "perspective": 1},
            headers=hdrs,
            timeout=5,
        )
        assert r_run.status_code == 200
        deadline = time.time() + 5.0
        while time.time() < deadline and qid not in ran_jobs:
            time.sleep(0.05)
        assert qid in ran_jobs
    finally:
        ins_runner.stop()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_takeover_bake_if_missing_uses_replay_cache_adhoc_bake(tmp_path: Path) -> None:
    qid = "1795443936010-2584"
    rc_dir = tmp_path / "replay_cache" / qid
    rc_dir.mkdir(parents=True)
    (rc_dir / "r02_gf17000.fs").write_bytes(b"R" * 150_000)

    fake_exe = tmp_path / "fcadefbneo.exe"
    fake_exe.write_bytes(b"exe")
    fake_lua = tmp_path / "replay_takeover.lua"
    fake_lua.write_text("-- lua", encoding="utf-8")

    baked_calls: list[tuple[str, int, int, str]] = []

    def fake_bake(quark_id: str, start_gf: int, end_gf: int, *, selection_id: str, data_dir: Path):
        baked_calls.append((quark_id, start_gf, end_gf, selection_id))
        fs_p = data_dir / "selections" / f"{selection_id}.fs"
        inp_p = data_dir / "selections" / f"{selection_id}.inputs"
        fs_p.parent.mkdir(parents=True, exist_ok=True)
        fs_p.write_bytes(b"B" * 8192)
        inp_p.write_text("game_frame,p1_mask,p2_mask\n17620,0,0\n", encoding="utf-8")
        return {"savestate": str(fs_p), "inputs": str(inp_p), "method": "fake"}

    class FakeProc:
        pid = 9999
        def poll(self): return None
        def terminate(self): pass
        def wait(self, timeout=None): return 0

    mgr = TakeoverManager(
        data_dir=tmp_path,
        fbneo_exe=fake_exe,
        replay_takeover_lua=fake_lua,
        seek_lua=fake_lua,
        popen_fn=lambda cmd, **kw: FakeProc(),
        bake_fn=fake_bake,
    )

    res = mgr.launch(
        {
            "quark_id": qid,
            "frame": 17660,
            "start_gf": 17620,
            "end_gf": 17740,
            "player": 1,
            "bake_if_missing": True,
            "label": "Ad-hoc Studio practice",
        }
    )
    assert res["ok"] is True
    assert res["method"] == "instant_savestate"
    assert res["baked_ad_hoc"] is True
    assert len(baked_calls) == 1
    assert baked_calls[0][1] == 17620
    assert baked_calls[0][2] == 17740

    # 2nd launch with identical (start_gf, end_gf) reuses cached ad-hoc `.fs` without re-baking
    res2 = mgr.launch(
        {
            "quark_id": qid,
            "frame": 17660,
            "start_gf": 17620,
            "end_gf": 17740,
            "player": 1,
            "bake_if_missing": True,
            "label": "Ad-hoc Studio practice",
        }
    )
    assert res2["ok"] is True
    assert res2["method"] == "instant_savestate"
    assert res2["baked_ad_hoc"] is False
    assert len(baked_calls) == 1

    # Changing end_gf (even with same start_gf & pre-existing savestates/<qid>_gf<frame>.fs) triggers fresh range-keyed bake
    sav_dir = tmp_path / "savestates"
    sav_dir.mkdir(parents=True, exist_ok=True)
    (sav_dir / f"{qid}_gf17660.fs").write_bytes(b"OLD" * 4096)

    res3 = mgr.launch(
        {
            "quark_id": qid,
            "frame": 17660,
            "start_gf": 17620,
            "end_gf": 17810,
            "player": 1,
            "bake_if_missing": True,
            "label": "Edited end_gf",
        }
    )
    assert res3["ok"] is True
    assert res3["method"] == "instant_savestate"
    assert res3["baked_ad_hoc"] is True
    assert len(baked_calls) == 2
    assert baked_calls[1][1:3] == (17620, 17810)
