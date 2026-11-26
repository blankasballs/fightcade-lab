"""Unit & HTTP integration tests for Focus Goals, Playlists, Perspective Enrichment & Home Workbench."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import pytest
import requests

from fightcade_analysis.focus_goals import (
    MAX_ACTIVE_GOALS,
    create_focus_goal,
    delete_focus_goal,
    list_focus_goals,
    update_focus_goal,
)
from fightcade_analysis.playlists import (
    create_playlist,
    delete_playlist,
    ensure_playlist_drills,
    list_playlists,
    mutate_playlist_items,
    update_playlist,
)
from fightcade_analysis.select_queue import upsert_queue_items
from fightcade_analysis.server import create_server


def _seed_sample_replay(tmp_path: Path, qid: str = "1795572328477-6666") -> None:
    rep_dir = tmp_path / "replays" / qid
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / "r01.mp4").write_bytes(b"MP4" * 200)
    (rep_dir / "r01_overlay.json").write_text(json.dumps({"start_gf": 1000, "frames": []}), encoding="utf-8")
    (rep_dir / "manifest.json").write_text(
        json.dumps(
            {
                "version": 3,
                "quark_id": qid,
                "perspective": 2,
                "p1": {"side": 1, "name": "Chuxster22", "rank_letter": "B", "char_id": 11, "char_name": "Ken", "chars": ["Ken"]},
                "p2": {"side": 2, "name": "player1", "rank_letter": "D", "char_id": 14, "char_name": "Akuma", "chars": ["Akuma"]},
                "round_count": 1,
                "rounds": [
                    {
                        "round_id": 1,
                        "filename": "r01.mp4",
                        "video_url": f"/replays/{qid}/r01.mp4",
                        "overlay_url": f"/replays/{qid}/r01_overlay.json",
                        "start_gf": 1000,
                        "end_gf": 2200,
                        "frame_count": 1201,
                        "fps": 60,
                        "p1_char": 11,
                        "p2_char": 14,
                        "p1_char_name": "Ken",
                        "p2_char_name": "Akuma",
                        "winner": 2,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    rc_dir = tmp_path / "replay_cache" / qid
    rc_dir.mkdir(parents=True, exist_ok=True)
    (rc_dir / "r01_gf1000.fs").write_bytes(b"F" * 150_000)


def test_focus_goals_max_three_cap_graduation_and_reactivation(tmp_path: Path) -> None:
    g1 = create_focus_goal({"owner": "player1", "title": "Anti-air jump-ins", "tags": ["Anti-Air"]}, data_dir=tmp_path)
    g2 = create_focus_goal({"owner": "player1", "title": "Block Chun cr.MK", "matchup_char": "Chun-Li"}, data_dir=tmp_path)
    g3 = create_focus_goal({"owner": "player1", "title": "Tech corner throws"}, data_dir=tmp_path)

    assert len(list_focus_goals(tmp_path, status="active", owner="player1")) == MAX_ACTIVE_GOALS

    # 4th active goal must fail cleanly
    with pytest.raises(ValueError, match="Maximum of 3"):
        create_focus_goal({"owner": "player1", "title": "Fourth goal"}, data_dir=tmp_path)

    # Another owner has their own independent cap
    other = create_focus_goal({"owner": "otherplayer", "title": "Other player goal"}, data_dir=tmp_path)
    assert other["owner"] == "otherplayer"

    # Graduate g1 -> sets graduated_at and frees a slot
    grad = update_focus_goal(g1["id"], {"status": "graduated"}, data_dir=tmp_path)
    assert grad is not None
    assert grad["status"] == "graduated"
    assert grad["graduated_at"] is not None
    assert len(list_focus_goals(tmp_path, status="active", owner="player1")) == 2
    assert len(list_focus_goals(tmp_path, status="graduated", owner="player1")) == 1

    # Can now pin a new 3rd active goal
    g4 = create_focus_goal({"owner": "player1", "title": "Whiff punish st.HP"}, data_dir=tmp_path)
    assert g4["status"] == "active"

    # Reactivating g1 while 3 active goals exist must fail cleanly
    with pytest.raises(ValueError, match="Maximum of 3"):
        update_focus_goal(g1["id"], {"status": "active"}, data_dir=tmp_path)

    # Deleting g4 frees a slot -> reactivating g1 succeeds and clears graduated_at
    assert delete_focus_goal(g4["id"], data_dir=tmp_path) is True
    react = update_focus_goal(g1["id"], {"status": "active"}, data_dir=tmp_path)
    assert react is not None
    assert react["status"] == "active"
    assert react["graduated_at"] is None


def test_playlists_crud_reorder_perspective_enrichment_and_goal_cleanup(tmp_path: Path) -> None:
    qid = "1795572328477-6666"
    _seed_sample_replay(tmp_path, qid)

    # Create a standalone pro clip too
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    (clips_dir / "1728857242828-5771_gf19501.mp4").write_bytes(b"MP4" * 500)
    sav_dir = tmp_path / "savestates"
    sav_dir.mkdir(parents=True, exist_ok=True)
    (sav_dir / "1728857242828-5771_gf19501.fs").write_bytes(b"S" * 8192)

    pl = create_playlist(
        {
            "owner": "player1",
            "name": "Anti-Air Lab",
            "description": "Jump-in spacing drills",
            "kind": "drill",
            "items": [
                {
                    "item_id": "it_1",
                    "source": "annotation",
                    "quark_id": qid,
                    "round_id": 1,
                    "perspective": 2,
                    "start_gf": 1120,
                    "end_gf": 1240,
                    "anchor_gf": 1180,
                    "label": "Delayed b+HP",
                }
            ],
        },
        data_dir=tmp_path,
    )
    assert pl["item_count"] == 1
    it0 = pl["items"][0]
    # Perspective 2 -> self is Akuma (player1), opp is Ken (Chuxster22)
    assert it0["self_char_name"] == "Akuma"
    assert it0["opp_char_name"] == "Ken"
    assert it0["opp_player_name"] == "Chuxster22"
    assert it0["has_video"] is True
    assert it0["overlay_url"] == f"/replays/{qid}/r01_overlay.json"
    assert it0["can_bake_drill"] is True

    # Link to a focus goal
    goal = create_focus_goal(
        {"owner": "player1", "title": "Anti-Air", "playlist_id": pl["id"]},
        data_dir=tmp_path,
    )

    # Add a pro clip item and reorder
    pl2 = mutate_playlist_items(
        pl["id"],
        {
            "action": "add",
            "item": {
                "item_id": "pro_5771_gf19501",
                "source": "pro_clip",
                "quark_id": "1728857242828-5771",
                "round_id": 4,
                "perspective": 1,
                "start_gf": 19381,
                "end_gf": 19651,
                "anchor_gf": 19501,
                "label": "Kuroda air throw",
            },
        },
        data_dir=tmp_path,
    )
    assert pl2 is not None
    assert [x["item_id"] for x in pl2["items"]] == ["it_1", "pro_5771_gf19501"]
    assert pl2["items"][1]["is_standalone_clip"] is True
    assert pl2["items"][1]["savestate_ready"] is True
    assert pl2["linked_goals"] == [{"id": goal["id"], "title": "Anti-Air"}]

    pl3 = mutate_playlist_items(
        pl["id"],
        {"action": "reorder", "order": ["pro_5771_gf19501", "it_1"]},
        data_dir=tmp_path,
    )
    assert pl3 is not None
    assert [x["item_id"] for x in pl3["items"]] == ["pro_5771_gf19501", "it_1"]

    # Pre-bake missing drills synchronously via mocked baker
    baked_ids: list[str] = []

    def fake_bake(quark_id: str, start_gf: int, end_gf: int, *, selection_id: str, data_dir: Path):
        baked_ids.append(selection_id)
        p = data_dir / "selections" / f"{selection_id}.fs"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"X" * 8192)
        return {"savestate": str(p), "inputs": ""}

    bres = ensure_playlist_drills(pl["id"], data_dir=tmp_path, bake_fn=fake_bake, async_bake=False)
    assert bres["ok"] is True
    assert baked_ids == ["it_1"]  # pro_5771 already had .fs ready

    # Update playlist metadata & remove item
    pl4 = update_playlist(pl["id"], {"name": "Anti-Air Lab v2", "shareable": True}, data_dir=tmp_path)
    assert pl4 is not None and pl4["name"] == "Anti-Air Lab v2" and pl4["shareable"] is True

    pl5 = mutate_playlist_items(pl["id"], {"action": "remove", "item_id": "it_1"}, data_dir=tmp_path)
    assert pl5 is not None and len(pl5["items"]) == 1

    # Deleting playlist clears dangling playlist_id on the linked Focus Goal
    assert delete_playlist(pl["id"], data_dir=tmp_path) is True
    assert list_playlists(tmp_path, owner="player1") == []
    updated_goal = list_focus_goals(tmp_path, status="active", owner="player1")[0]
    assert updated_goal["playlist_id"] is None


def test_http_focus_goals_playlists_and_home_workbench_aggregate(tmp_path: Path) -> None:
    qid = "1795572328477-6666"
    _seed_sample_replay(tmp_path, qid)
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "clips").mkdir(exist_ok=True)

    # Seed one untriaged in-game Select mark for this match
    upsert_queue_items(
        [
            {
                "id": f"{qid}_p2_sel1_gf1200_1560",
                "source": "in_game_select",
                "quark_id": qid,
                "perspective": 2,
                "round_id": 1,
                "start_gf": 1200,
                "press_gf": 1560,
                "end_gf": 1560,
                "self_char": 14,
                "opp_char": 11,
                "note": "In-game Select mark #1 (R1)",
            }
        ],
        data_dir=tmp_path,
    )

    server = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False, async_bake_drills=False)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        hdrs = {"Origin": base}

        # Create playlist via HTTP
        r_pl = requests.post(
            f"{base}/api/playlists",
            json={"name": "Anti-Air Drills", "kind": "drill", "owner": "player1"},
            headers=hdrs,
            timeout=5,
        )
        assert r_pl.status_code == 200
        pl_id = r_pl.json()["playlist"]["id"]

        # Create Focus Goal linked to playlist via HTTP
        r_g = requests.post(
            f"{base}/api/focus-goals",
            json={"title": "Anti-air early", "owner": "player1", "playlist_id": pl_id},
            headers=hdrs,
            timeout=5,
        )
        assert r_g.status_code == 200
        g_id = r_g.json()["goal"]["id"]

        # GET /api/home returns latest match + select_marks_count + focus_goals + playlists + pulse
        r_home = requests.get(f"{base}/api/home?username=player1", timeout=5)
        assert r_home.status_code == 200
        home = r_home.json()
        assert home["ok"] is True
        assert home["latest_match"]["quark_id"] == qid
        assert home["latest_match"]["select_marks_count"] == 1
        assert home["latest_match"]["select_mark_rounds"] == [1]
        assert home["latest_match"]["has_overlay_data"] is True
        assert home["latest_match"]["preview_video_url"] == f"/replays/{qid}/r01.mp4"
        assert len(home["focus_goals"]["active"]) == 1
        assert home["focus_goals"]["active"][0]["id"] == g_id
        assert len(home["playlists"]) == 1
        assert home["pulse"]["untriaged_marks"] == 1

        # Cross-origin mutation blocked (403)
        r_csrf = requests.post(
            f"{base}/api/focus-goals",
            json={"title": "CSRF Goal"},
            headers={"Origin": "https://evil.example"},
            timeout=5,
        )
        assert r_csrf.status_code == 403

        # Graduate & DELETE via HTTP
        r_patch = requests.patch(
            f"{base}/api/focus-goals/{g_id}",
            json={"status": "graduated"},
            headers=hdrs,
            timeout=5,
        )
        assert r_patch.status_code == 200
        assert r_patch.json()["goal"]["status"] == "graduated"

        r_del = requests.delete(f"{base}/api/playlists/{pl_id}", headers=hdrs, timeout=5)
        assert r_del.status_code == 200
        assert r_del.json()["deleted"] == pl_id
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_empty_data_and_degraded_capabilities_return_clean_payloads(tmp_path: Path) -> None:
    """Verify empty data/ and study-only capabilities (can_takeover=False, has_gemini=False) work with zero crashes."""
    degraded_caps = {
        "ok": True,
        "mode": "study_only",
        "owner": "player1",
        "can_takeover": False,
        "interactive_desktop": False,
        "can_render": False,
        "can_generate_overlays": False,
        "watcher_active": False,
        "has_gemini": False,
        "has_pro_corpus": False,
        "pro_corpus_sets": 0,
    }
    server = create_server(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        auto_refresh_index=False,
        capabilities_fn=lambda: degraded_caps,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        r_caps = requests.get(f"{base}/api/capabilities", timeout=5)
        assert r_caps.status_code == 200
        assert r_caps.json()["can_takeover"] is False

        r_home = requests.get(f"{base}/api/home", timeout=5)
        assert r_home.status_code == 200
        home = r_home.json()
        assert home["latest_match"] is None
        assert home["focus_goals"]["active"] == []
        assert home["playlists"] == []
        assert home["pulse"]["notes_total"] == 0
        assert home["pulse"]["latest_insight"] is None

        r_ins = requests.get(f"{base}/api/insights", timeout=5)
        assert r_ins.status_code == 403
        assert "roadmap" in r_ins.json()["error"].lower()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
