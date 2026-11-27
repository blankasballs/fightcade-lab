"""Unit & HTTP integration tests for Unified Focus Goals / Playlists ("Option 3"), Migration & Home Workbench."""

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
    get_playlist,
    legacy_focus_goals_jsonl_path,
    list_playlists,
    mutate_playlist_items,
    playlists_jsonl_path,
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


def test_unified_focus_goals_zero_clip_mental_cues_max_three_cap_graduation_and_reactivation(tmp_path: Path) -> None:
    # 0-clip mental cue Focus Goals live directly in playlists.jsonl
    g1 = create_focus_goal({"owner": "player1", "title": "Anti-air jump-ins", "tags": ["Anti-Air"]}, data_dir=tmp_path)
    g2 = create_focus_goal({"owner": "player1", "title": "Block Chun cr.MK", "matchup_char": "Chun-Li"}, data_dir=tmp_path)
    g3 = create_focus_goal({"owner": "player1", "title": "Tech corner throws"}, data_dir=tmp_path)

    assert g1["is_focus"] is True
    assert g1["focus_status"] == "active"
    assert g1["items"] == []
    assert g1["item_count"] == 0
    assert g1["ready_drills_count"] == 0
    assert playlists_jsonl_path(tmp_path).is_file()
    assert not legacy_focus_goals_jsonl_path(tmp_path).exists()

    assert len(list_focus_goals(tmp_path, status="active", owner="player1")) == MAX_ACTIVE_GOALS

    # 4th active focus must fail cleanly
    with pytest.raises(ValueError, match="Maximum of 3"):
        create_focus_goal({"owner": "player1", "title": "Fourth focus"}, data_dir=tmp_path)

    # Another owner has their own independent cap
    other = create_focus_goal({"owner": "otherplayer", "title": "Other player focus"}, data_dir=tmp_path)
    assert other["owner"] == "otherplayer"

    # Graduate g1 -> sets graduated_at and frees a slot
    grad = update_focus_goal(g1["id"], {"status": "graduated"}, data_dir=tmp_path)
    assert grad is not None
    assert grad["status"] == "graduated"
    assert grad["focus_status"] == "graduated"
    assert grad["graduated_at"] is not None
    assert len(list_focus_goals(tmp_path, status="active", owner="player1")) == 2
    assert len(list_focus_goals(tmp_path, status="graduated", owner="player1")) == 1

    # Can now pin a new 3rd active focus
    g4 = create_focus_goal({"owner": "player1", "title": "Whiff punish st.HP"}, data_dir=tmp_path)
    assert g4["status"] == "active"

    # Reactivating g1 while 3 active focuses exist must fail cleanly
    with pytest.raises(ValueError, match="Maximum of 3"):
        update_focus_goal(g1["id"], {"status": "active"}, data_dir=tmp_path)

    # Deleting g4 frees a slot -> reactivating g1 succeeds and clears graduated_at
    assert delete_focus_goal(g4["id"], data_dir=tmp_path) is True
    react = update_focus_goal(g1["id"], {"status": "active"}, data_dir=tmp_path)
    assert react is not None
    assert react["status"] == "active"
    assert react["focus_status"] == "active"
    assert react["graduated_at"] is None


def test_playlist_focus_promotion_demotion_and_item_preservation(tmp_path: Path) -> None:
    qid = "1795572328477-6666"
    _seed_sample_replay(tmp_path, qid)

    # Create a Reference Playlist with 1 clip
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
    assert pl["is_focus"] is False
    assert pl["focus_status"] is None
    assert pl["item_count"] == 1

    # Promote Reference Playlist -> Active Focus (preserving items[])
    promoted = update_playlist(pl["id"], {"is_focus": True, "focus_status": "active"}, data_dir=tmp_path)
    assert promoted is not None
    assert promoted["is_focus"] is True
    assert promoted["focus_status"] == "active"
    assert promoted["item_count"] == 1
    assert len(list_playlists(tmp_path, owner="player1", is_focus=False)) == 0
    assert len(list_focus_goals(tmp_path, status="active", owner="player1")) == 1

    # Fill the remaining 2 slots so max-3 cap applies to promotion too
    create_focus_goal({"owner": "player1", "title": "Focus 2"}, data_dir=tmp_path)
    create_focus_goal({"owner": "player1", "title": "Focus 3"}, data_dir=tmp_path)
    ref2 = create_playlist({"owner": "player1", "name": "Corner Escape"}, data_dir=tmp_path)
    with pytest.raises(ValueError, match="Maximum of 3"):
        update_playlist(ref2["id"], {"is_focus": True, "focus_status": "active"}, data_dir=tmp_path)

    # Demote / unpin back to Reference Playlist -> keeps items[], frees slot
    demoted = update_playlist(pl["id"], {"is_focus": False, "focus_status": None}, data_dir=tmp_path)
    assert demoted is not None
    assert demoted["is_focus"] is False
    assert demoted["focus_status"] is None
    assert demoted["graduated_at"] is None
    assert demoted["item_count"] == 1
    assert demoted["items"][0]["item_id"] == "it_1"

    # Now promoting ref2 succeeds!
    ref2_promoted = update_playlist(ref2["id"], {"is_focus": True, "focus_status": "active"}, data_dir=tmp_path)
    assert ref2_promoted is not None and ref2_promoted["is_focus"] is True


def test_automatic_idempotent_legacy_focus_goals_migration_and_deduplication(tmp_path: Path) -> None:
    qid = "1795572328477-6666"
    _seed_sample_replay(tmp_path, qid)

    sel_dir = tmp_path / "selections"
    sel_dir.mkdir(parents=True, exist_ok=True)

    # Seed legacy playlists.jsonl with 1 linked playlist and 1 unrelated reference playlist
    legacy_playlists = [
        {
            "id": "pl_linked01",
            "owner": "player1",
            "name": "Anti-Air",
            "description": "short",
            "kind": "drill",
            "shareable": False,
            "items": [
                {
                    "item_id": "it_aa1",
                    "source": "annotation",
                    "quark_id": qid,
                    "round_id": 1,
                    "perspective": 2,
                    "start_gf": 1100,
                    "end_gf": 1220,
                    "anchor_gf": 1160,
                    "label": "b+HP timing",
                }
            ],
            "created_at": "2026-11-24T10:00:00",
            "updated_at": "2026-11-24T10:05:00",
        },
        {
            "id": "pl_ref02",
            "owner": "player1",
            "name": "Ken Matchup Study",
            "description": "Neutral clips",
            "kind": "matchup",
            "shareable": True,
            "items": [],
            "created_at": "2026-11-24T11:00:00",
            "updated_at": "2026-11-24T11:00:00",
        },
    ]
    playlists_jsonl_path(tmp_path).write_text(
        "\n".join(json.dumps(r) for r in legacy_playlists) + "\n",
        encoding="utf-8",
    )

    # Seed legacy focus_goals.jsonl with:
    #  1) Linked active goal (richer title + description -> merges onto pl_linked01, zero duplication)
    #  2) Unlinked 0-clip active mental cue goal -> creates unified is_focus=True record keeping id/title/created_at
    #  3) Unlinked graduated goal -> keeps status=graduated + graduated_at
    legacy_goals = [
        {
            "id": "goal_linked_1",
            "owner": "player1",
            "title": "Anti-Air Jump-Ins with b+HP",
            "description": "Stay grounded and react early to jump-in arcs.",
            "status": "active",
            "matchup_char": "Ken",
            "tags": ["Anti-Air", "Defense"],
            "playlist_id": "pl_linked01",
            "source_insight_id": "ins_01:anti_air",
            "trend_hint": "From AI Checkup",
            "created_at": "2026-11-24T10:01:00",
            "updated_at": "2026-11-24T10:01:00",
            "graduated_at": None,
        },
        {
            "id": "goal_cue_2",
            "owner": "player1",
            "title": "Parry Opponents Oki",
            "description": "Sweep and UOH are common Oki options.",
            "status": "active",
            "matchup_char": "",
            "tags": ["Oki"],
            "playlist_id": None,
            "source_insight_id": None,
            "trend_hint": "",
            "created_at": "2026-11-25T22:15:39",
            "updated_at": "2026-11-25T22:15:55",
            "graduated_at": None,
        },
        {
            "id": "goal_grad_3",
            "owner": "player1",
            "title": "Stop mashing EX DP on wakeup",
            "description": "Block first on knockdown.",
            "status": "graduated",
            "matchup_char": "",
            "tags": ["Wakeup / Oki"],
            "playlist_id": None,
            "source_insight_id": None,
            "trend_hint": "",
            "created_at": "2026-11-20T09:00:00",
            "updated_at": "2026-11-22T09:00:00",
            "graduated_at": "2026-11-22T09:00:00",
        },
    ]
    legacy_path = legacy_focus_goals_jsonl_path(tmp_path)
    legacy_path.write_text("\n".join(json.dumps(g) for g in legacy_goals) + "\n", encoding="utf-8")

    # First read triggers automatic one-time migration
    all_pls = list_playlists(tmp_path, owner="player1", enrich=True)

    # Legacy file archived atomically to focus_goals.jsonl.migrated
    assert not legacy_path.exists()
    assert (sel_dir / "focus_goals.jsonl.migrated").is_file()

    # Exact total = 4 (pl_linked01 merged, pl_ref02 untouched, goal_cue_2, goal_grad_3) -> ZERO duplication!
    by_id = {p["id"]: p for p in all_pls}
    assert set(by_id.keys()) == {"pl_linked01", "pl_ref02", "goal_cue_2", "goal_grad_3"}

    merged = by_id["pl_linked01"]
    assert merged["is_focus"] is True
    assert merged["focus_status"] == "active"
    assert merged["name"] == "Anti-Air Jump-Ins with b+HP"  # richer goal title preserved
    assert merged["description"] == "Stay grounded and react early to jump-in arcs."
    assert merged["matchup_char"] == "Ken"
    assert merged["tags"] == ["Anti-Air", "Defense"]
    assert merged["source_insight_id"] == "ins_01:anti_air"
    assert merged["item_count"] == 1
    assert merged["items"][0]["item_id"] == "it_aa1"

    cue = by_id["goal_cue_2"]
    assert cue["is_focus"] is True
    assert cue["focus_status"] == "active"
    assert cue["name"] == "Parry Opponents Oki"
    assert cue["items"] == []
    assert cue["created_at"] == "2026-11-25T22:15:39"

    grad = by_id["goal_grad_3"]
    assert grad["is_focus"] is True
    assert grad["focus_status"] == "graduated"
    assert grad["graduated_at"] == "2026-11-22T09:00:00"

    ref = by_id["pl_ref02"]
    assert ref["is_focus"] is False
    assert ref["focus_status"] is None

    # Subsequent reads are 100% idempotent
    all_pls_again = list_playlists(tmp_path, owner="player1", enrich=False)
    assert len(all_pls_again) == 4


def test_playlists_crud_reorder_perspective_enrichment_and_prebake(tmp_path: Path) -> None:
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
            "is_focus": True,
            "focus_status": "active",
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
    assert pl["is_focus"] is True
    it0 = pl["items"][0]
    # Perspective 2 -> self is Akuma (player1), opp is Ken (Chuxster22)
    assert it0["self_char_name"] == "Akuma"
    assert it0["opp_char_name"] == "Ken"
    assert it0["opp_player_name"] == "Chuxster22"
    assert it0["has_video"] is True
    assert it0["overlay_url"] == f"/replays/{qid}/r01_overlay.json"
    assert it0["can_bake_drill"] is True

    # Add a pro clip item and reorder directly on the unified Focus
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
                "label": "Pro air throw",
            },
        },
        data_dir=tmp_path,
    )
    assert pl2 is not None
    assert [x["item_id"] for x in pl2["items"]] == ["it_1", "pro_5771_gf19501"]
    assert pl2["items"][1]["is_standalone_clip"] is True
    assert pl2["items"][1]["savestate_ready"] is True
    assert pl2["ready_drills_count"] == 1

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

    # After baking, ready_drills_count == 2
    refreshed = get_playlist(pl["id"], data_dir=tmp_path, enrich=True)
    assert refreshed is not None and refreshed["ready_drills_count"] == 2

    # Update metadata & remove item & delete
    pl4 = update_playlist(pl["id"], {"name": "Anti-Air Lab v2", "shareable": True}, data_dir=tmp_path)
    assert pl4 is not None and pl4["name"] == "Anti-Air Lab v2" and pl4["shareable"] is True

    pl5 = mutate_playlist_items(pl["id"], {"action": "remove", "item_id": "it_1"}, data_dir=tmp_path)
    assert pl5 is not None and len(pl5["items"]) == 1

    assert delete_playlist(pl["id"], data_dir=tmp_path) is True
    assert list_playlists(tmp_path, owner="player1") == []


def test_http_unified_focus_playlists_insights_prepopulate_and_non_duplicated_home(tmp_path: Path) -> None:
    qid = "1795572328477-6666"
    _seed_sample_replay(tmp_path, qid)
    (tmp_path / "reports").mkdir(exist_ok=True)
    (tmp_path / "clips").mkdir(exist_ok=True)
    sav_dir = tmp_path / "savestates"
    sav_dir.mkdir(exist_ok=True)
    (sav_dir / f"{qid}_gf1180.fs").write_bytes(b"S" * 8192)

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

        # 1. Create a Reference Playlist via HTTP
        r_pl = requests.post(
            f"{base}/api/playlists",
            json={"name": "Ken Neutral Study", "kind": "matchup", "owner": "player1"},
            headers=hdrs,
            timeout=5,
        )
        assert r_pl.status_code == 200
        ref_pl_id = r_pl.json()["playlist"]["id"]
        assert r_pl.json()["playlist"]["is_focus"] is False

        # 2. Create a pre-populated Active Focus via POST /api/focus-goals (Insights 1-call flow)
        r_g = requests.post(
            f"{base}/api/focus-goals",
            json={
                "title": "Fix: Missed Anti-Air",
                "owner": "player1",
                "source_insight_id": "rep_habits:missed_anti_air",
                "tags": ["Anti-Air"],
                "items": [
                    {
                        "item_id": "ins_clip_1",
                        "source": "moment",
                        "quark_id": qid,
                        "round_id": 1,
                        "perspective": 2,
                        "start_gf": 1100,
                        "end_gf": 1250,
                        "anchor_gf": 1180,
                        "label": "Missed anti-air vs Ken",
                    }
                ],
            },
            headers=hdrs,
            timeout=5,
        )
        assert r_g.status_code == 200
        g_doc = r_g.json()["goal"]
        g_id = g_doc["id"]
        assert g_doc["item_count"] == 1
        assert g_doc["ready_drills_count"] == 1
        assert len(g_doc["items"]) == 1

        # 3. GET /api/home returns Active Focus on left and ONLY non-focus Reference Playlists on right (zero duplication!)
        r_home = requests.get(f"{base}/api/home?username=player1", timeout=5)
        assert r_home.status_code == 200
        home = r_home.json()
        assert home["ok"] is True
        assert home["latest_match"]["quark_id"] == qid
        assert home["latest_match"]["select_marks_count"] == 0
        assert home["latest_match"]["select_mark_rounds"] == []
        assert home["latest_match"]["first_select_mark"] is None
        assert home["latest_match"]["has_overlay_data"] is True
        assert home["latest_match"]["preview_video_url"] == f"/replays/{qid}/r01.mp4"
        assert len(home["focus_goals"]["active"]) == 1
        assert home["focus_goals"]["active"][0]["id"] == g_id
        assert home["focus_goals"]["active"][0]["item_count"] == 1
        assert home["focus_goals"]["active"][0]["ready_drills_count"] == 1
        assert [p["id"] for p in home["playlists"]] == [ref_pl_id]
        assert all(p["is_focus"] is False for p in home["playlists"])
        assert home["pulse"]["untriaged_marks"] == 0

        # 4. Promote Reference Playlist to Active Focus via PATCH /api/playlists/<id>
        r_prom = requests.patch(
            f"{base}/api/playlists/{ref_pl_id}",
            json={"is_focus": True, "focus_status": "active"},
            headers=hdrs,
            timeout=5,
        )
        assert r_prom.status_code == 200
        assert r_prom.json()["playlist"]["is_focus"] is True

        # Home now has 2 Active Focuses and 0 Reference Playlists
        home2 = requests.get(f"{base}/api/home?username=player1", timeout=5).json()
        assert len(home2["focus_goals"]["active"]) == 2
        assert home2["playlists"] == []

        # 5. Unpin back to Reference Playlist via PATCH /api/playlists/<id>
        r_unpin = requests.patch(
            f"{base}/api/playlists/{ref_pl_id}",
            json={"is_focus": False, "focus_status": None},
            headers=hdrs,
            timeout=5,
        )
        assert r_unpin.status_code == 200
        assert r_unpin.json()["playlist"]["is_focus"] is False
        assert r_unpin.json()["playlist"]["focus_status"] is None

        # 6. Cross-origin mutation blocked (403)
        r_csrf = requests.post(
            f"{base}/api/focus-goals",
            json={"title": "CSRF Goal"},
            headers={"Origin": "https://evil.example"},
            timeout=5,
        )
        assert r_csrf.status_code == 403

        # 7. Graduate Focus Goal (keeps clips archived) & verify graduated_count on /api/home
        r_patch = requests.patch(
            f"{base}/api/playlists/{g_id}",
            json={"focus_status": "graduated"},
            headers=hdrs,
            timeout=5,
        )
        assert r_patch.status_code == 200
        assert r_patch.json()["playlist"]["focus_status"] == "graduated"
        assert r_patch.json()["playlist"]["graduated_at"] is not None
        assert r_patch.json()["playlist"]["item_count"] == 1

        home3 = requests.get(f"{base}/api/home?username=player1", timeout=5).json()
        assert home3["focus_goals"]["active"] == []
        assert home3["focus_goals"]["graduated_count"] == 1

        # 8. Delete via HTTP
        r_del = requests.delete(f"{base}/api/playlists/{ref_pl_id}", headers=hdrs, timeout=5)
        assert r_del.status_code == 200
        assert r_del.json()["deleted"] == ref_pl_id
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
