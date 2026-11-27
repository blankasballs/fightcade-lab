"""Unit & HTTP integration tests for Milestone 6 (Match History Catalog, Mobile Replay Companion & Annotations)."""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import pandas as pd
import pytest
import requests

from fightcade_analysis.annotations import (
    create_annotation,
    delete_annotation,
    get_set_note,
    list_annotations,
    update_annotation,
    upsert_set_note,
)
from fightcade_analysis.features import POSTURE_KNOCKDOWN
from fightcade_analysis.match_catalog import (
    ProcessingQueue,
    list_matches,
    parse_quark_id_or_url,
    refresh_match_history,
)
from fightcade_analysis.round_renderer import (
    bake_drill_savestate,
    ensure_manifest_matches,
    extract_round_timeline_markers,
    group_rounds_into_matches,
    parse_overlay_tsv_to_json,
    prune_old_mobile_replays,
)
from fightcade_analysis.select_queue import load_queue
from fightcade_analysis.server import create_server, resolve_safe_data_path


def _make_sample_round_df(start_gf: int = 1000, n_frames: int = 240) -> pd.DataFrame:
    gfs = list(range(start_gf, start_gf + n_frames))
    p1_hp = [160] * n_frames
    p2_hp = [160] * n_frames
    p1_sf = [0] * n_frames
    p2_sf = [0] * n_frames
    p1_pos = [0] * n_frames
    p2_pos = [0] * n_frames
    for i in range(min(60, n_frames), n_frames):
        p1_hp[i] = 120  # P1 takes 40 dmg at start_gf + 60
    for i in range(min(120, n_frames), n_frames):
        p2_hp[i] = 110  # P2 takes 50 dmg at start_gf + 120
    for i in range(min(150, n_frames), min(165, n_frames)):
        p1_sf[i] = 1  # P1 superfreeze
    for i in range(min(180, n_frames), min(200, n_frames)):
        p2_pos[i] = POSTURE_KNOCKDOWN  # P2 knockdown
    return pd.DataFrame(
        {
            "game_frame": gfs,
            "round_id": [1] * n_frames,
            "p1_char": [14] * n_frames,  # Akuma
            "p2_char": [4] * n_frames,   # Ken
            "p1_hp": p1_hp,
            "p2_hp": p2_hp,
            "p1_superfreeze": p1_sf,
            "p2_superfreeze": p2_sf,
            "p1_posture": p1_pos,
            "p2_posture": p2_pos,
            "p1_input_mask": [0] * n_frames,
            "p2_input_mask": [0] * n_frames,
        }
    )


def test_extract_round_timeline_markers_and_hp_curve() -> None:
    df = _make_sample_round_df(start_gf=2000, n_frames=240)
    res = extract_round_timeline_markers(
        df,
        perspective=1,
        select_marks=[{"press_gf": 2090, "note": "Missed AA"}],
    )
    types = {m["type"] for m in res["markers"]}
    assert "dmg_self" in types
    assert "dmg_opp" in types
    assert "super" in types
    assert "kd_opp" in types
    assert "select_mark" not in types
    assert len(res["hp_curve"]) >= 10
    assert res["hp_curve"][0][0] == 0
    assert res["hp_curve"][0][1] == 1.0

    # Exact frame alignment: P1 takes 40 dmg at offset 60 (gf 2060), HP curve must hold 1.0 through 59 and drop at 60
    dmg_self = [m for m in res["markers"] if m["type"] == "dmg_self"]
    assert len(dmg_self) == 1
    assert dmg_self[0]["gf"] == 2060
    assert dmg_self[0]["offset_f"] == 60
    curve_by_off = {pt[0]: (pt[1], pt[2]) for pt in res["hp_curve"]}
    assert curve_by_off[59][0] == 1.0
    assert curve_by_off[60][0] == 0.75


def test_hp_timeline_does_not_anchor_damage_on_leading_chip_and_rebases_with_start_override() -> None:
    """Regression: isolated 1-2 HP blocked chip before a combo must not pull the dmg tick early,
    and `start_gf_override` must anchor BOTH markers and hp_curve to the same video start frame.
    """
    n_frames = 300
    start_gf = 5000
    p1_hp = [160] * n_frames
    p2_hp = [160] * n_frames
    # P2 blocks 1 HP chip at +50 and +80, then gets hit for a 30 HP combo at +113 and +125
    for i in range(50, n_frames):
        p2_hp[i] -= 1
    for i in range(80, n_frames):
        p2_hp[i] -= 1
    for i in range(113, n_frames):
        p2_hp[i] -= 14
    for i in range(125, n_frames):
        p2_hp[i] -= 16

    df = pd.DataFrame(
        {
            "game_frame": list(range(start_gf, start_gf + n_frames)),
            "round_id": [1] * n_frames,
            "p1_char": [14] * n_frames,
            "p2_char": [4] * n_frames,
            "p1_hp": p1_hp,
            "p2_hp": p2_hp,
            "p1_superfreeze": [0] * n_frames,
            "p2_superfreeze": [0] * n_frames,
            "p1_posture": [0] * n_frames,
            "p2_posture": [0] * n_frames,
            "p1_input_mask": [0] * n_frames,
            "p2_input_mask": [0] * n_frames,
        }
    )

    res = extract_round_timeline_markers(df, perspective=1, start_gf_override=5010)
    dmg_opp = [m for m in res["markers"] if m["type"] == "dmg_opp"]
    assert len(dmg_opp) == 1
    # Anchored at the real hit (gf 5113 -> offset 103 relative to 5010), NOT the blocked chip at 5050!
    assert dmg_opp[0]["gf"] == 5113
    assert dmg_opp[0]["offset_f"] == 103
    assert dmg_opp[0]["value"] == 30

    # hp_curve is rebased identically to start_gf_override=5010 and has exact pre/post vertices at 102 -> 103
    curve_by_off = {pt[0]: pt[2] for pt in res["hp_curve"]}
    assert res["hp_curve"][0][0] == 0
    assert curve_by_off[102] == round(158 / 160, 3)
    assert curve_by_off[103] == round(144 / 160, 3)


def test_group_rounds_into_matches_ft2_gap_and_char_switch() -> None:
    rounds = [
        # Match 1 (2-0 P1)
        {"round_id": 1, "start_gf": 1000, "end_gf": 2500, "p1_char": 18, "p2_char": 14, "winner": 1, "duration_s": 25.0},
        {"round_id": 2, "start_gf": 3000, "end_gf": 4500, "p1_char": 18, "p2_char": 14, "winner": 1, "duration_s": 25.0},
        # Match 2 (2-1 P2, starts after rematch gap)
        {"round_id": 3, "start_gf": 6000, "end_gf": 7500, "p1_char": 18, "p2_char": 14, "winner": 2, "duration_s": 25.0},
        {"round_id": 4, "start_gf": 8000, "end_gf": 9500, "p1_char": 18, "p2_char": 14, "winner": 1, "duration_s": 25.0},
        {"round_id": 5, "start_gf": 10000, "end_gf": 11500, "p1_char": 18, "p2_char": 14, "winner": 2, "duration_s": 25.0},
        # Match 3 (character switch from Q->Ken after incomplete/gap)
        {"round_id": 6, "start_gf": 12000, "end_gf": 13500, "p1_char": 4, "p2_char": 14, "winner": 2, "duration_s": 25.0},
        {"round_id": 7, "start_gf": 14000, "end_gf": 15500, "p1_char": 4, "p2_char": 14, "winner": 2, "duration_s": 25.0},
    ]
    annotated, matches = group_rounds_into_matches(rounds, perspective=2)
    assert len(matches) == 3
    assert [m["round_ids"] for m in matches] == [[1, 2], [3, 4, 5], [6, 7]]
    assert [(m["self_rounds_won"], m["opp_rounds_won"], m["self_won"]) for m in matches] == [
        (0, 2, False),
        (2, 1, True),
        (2, 0, True),
    ]
    assert [(m["running_self_score"], m["running_opp_score"]) for m in matches] == [(0, 1), (1, 1), (2, 1)]
    assert [(r["match_id"], r["match_round"]) for r in annotated] == [
        (1, 1), (1, 2), (2, 1), (2, 2), (2, 3), (3, 1), (3, 2)
    ]

    # Legacy manifest auto-enrichment
    mf = ensure_manifest_matches({"perspective": 2, "rounds": rounds})
    assert mf["match_count"] == 3
    assert mf["set_self_wins"] == 2
    assert mf["set_opp_wins"] == 1


def test_p2_local_manifest_names_and_placeholder_backfill(tmp_path: Path) -> None:
    """Regression test: P2 matches not yet in Fightcade history must resolve opponent name from manifest (not 'P1')."""
    qid = "1795558989575-1518"
    rep_dir = tmp_path / "replays" / qid
    rep_dir.mkdir(parents=True)
    (rep_dir / "manifest.json").write_text(
        json.dumps(
            {
                "quark_id": qid,
                "perspective": 2,
                "self_player_name": "player1",
                "opp_player_name": "IbukiMaster",
                "self_rank_letter": "D",
                "opp_rank_letter": "B",
                "p1_char_name": "Ibuki",
                "p2_char_name": "Akuma",
                "round_count": 2,
                "rounds": [
                    {"round_id": 1, "start_gf": 1000, "end_gf": 2000, "p1_char": 17, "p2_char": 14, "winner": 2},
                    {"round_id": 2, "start_gf": 2500, "end_gf": 3500, "p1_char": 17, "p2_char": 14, "winner": 2},
                ],
            }
        ),
        encoding="utf-8",
    )

    cat = list_matches(username="player1", data_dir=tmp_path)
    assert len(cat["matches"]) == 1
    m = cat["matches"][0]
    assert m["perspective"] == 2
    assert m["self_player_name"] == "player1"
    assert m["opp_player_name"] == "IbukiMaster"
    assert m["opp_rank_letter"] == "B"
    assert m["self_char_name"] == "Akuma"
    assert m["opp_char_name"] == "Ibuki"

    # Also test manifest with stale 'P1'/'P2' placeholders gets backfilled once history cache arrives
    (tmp_path / "match_history_player1.json").write_text(
        json.dumps(
            {
                "summaries": [
                    {
                        "quarkid": qid,
                        "date": 1795558989575,
                        "p1_name": "RealIbuki",
                        "p1_rank": 4,
                        "p2_name": "player1",
                        "p2_rank": 2,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    stale_mf = {
        "quark_id": qid,
        "perspective": 2,
        "self_player_name": "P2",
        "opp_player_name": "P1",
        "rounds": [{"round_id": 1, "start_gf": 1000, "end_gf": 2000, "p1_char": 17, "p2_char": 14, "winner": 2}],
    }
    enriched = ensure_manifest_matches(stale_mf, data_dir=tmp_path)
    assert enriched["self_player_name"] == "player1"
    assert enriched["opp_player_name"] == "RealIbuki"
    assert enriched["opp_rank_letter"] == "B"


def test_multi_char_tags_and_matchup_filtering(tmp_path: Path) -> None:
    qid = "1795558989575-1518"
    rep_dir = tmp_path / "replays" / qid
    rep_dir.mkdir(parents=True)
    (rep_dir / "manifest.json").write_text(
        json.dumps(
            {
                "quark_id": qid,
                "perspective": 2,
                "self_player_name": "player1",
                "opp_player_name": "Matito.",
                "p1_char_name": "Ibuki",
                "p2_char_name": "Akuma",
                "round_count": 4,
                "rounds": [
                    {"round_id": 1, "start_gf": 1000, "end_gf": 2000, "p1_char": 19, "p2_char": 14, "p1_char_name": "Makoto", "p2_char_name": "Akuma", "winner": 2},
                    {"round_id": 2, "start_gf": 2500, "end_gf": 3500, "p1_char": 19, "p2_char": 14, "p1_char_name": "Makoto", "p2_char_name": "Akuma", "winner": 2},
                    {"round_id": 3, "start_gf": 5000, "end_gf": 6000, "p1_char": 17, "p2_char": 14, "p1_char_name": "Ibuki", "p2_char_name": "Akuma", "winner": 1},
                    {"round_id": 4, "start_gf": 6500, "end_gf": 7500, "p1_char": 17, "p2_char_name": "Akuma", "p1_char_name": "Ibuki", "p2_char": 14, "winner": 1},
                ],
            }
        ),
        encoding="utf-8",
    )

    cat = list_matches(username="player1", data_dir=tmp_path)
    m = cat["matches"][0]
    assert m["self_chars"] == ["Akuma"]
    assert m["opp_chars"] == ["Makoto", "Ibuki"]
    assert cat["available_chars"] == {"self": ["Akuma"], "opp": ["Ibuki", "Makoto"]}

    # Filtering by EITHER opponent character finds this multi-character set
    assert len(list_matches(username="player1", data_dir=tmp_path, opp_char="Makoto")["matches"]) == 1
    assert len(list_matches(username="player1", data_dir=tmp_path, opp_char="Ibuki")["matches"]) == 1
    assert len(list_matches(username="player1", data_dir=tmp_path, opp_char="Ken")["matches"]) == 0


def test_parse_overlay_tsv_to_json_and_manifest_detection(tmp_path: Path) -> None:
    qid = "1795547282177-8026"
    rep_dir = tmp_path / "replays" / qid
    rep_dir.mkdir(parents=True)
    tsv = tmp_path / "r01.overlay.tsv"
    tsv.write_text(
        "15214\t16\t4\t160,12,96,0,2,0,72\t160,8,112,0,2,0,56\t0,100,120,30,60;1,130,130,40,20\n"
        "15215\t0\t0\t160,12,96,0,2,0,72\t145,10,112,0,2,10,56\t0,102,120,30,60\n",
        encoding="utf-8",
    )
    out_json = rep_dir / "r01_overlay.json"
    info = parse_overlay_tsv_to_json(tsv, out_json, start_gf=15214)
    assert info["frame_count"] == 2
    parsed = json.loads(out_json.read_text(encoding="utf-8"))
    assert parsed["start_gf"] == 15214
    assert parsed["frames"][0][0] == 16
    assert len(parsed["frames"][0][4]) == 2
    assert parsed["frames"][0][4][1] == [1, 130, 130, 40, 20]

    mf = ensure_manifest_matches(
        {"quark_id": qid, "perspective": 2, "rounds": [{"round_id": 1, "start_gf": 15214, "end_gf": 15215}]},
        data_dir=tmp_path,
    )
    assert mf["rounds"][0]["overlay_url"] == f"/replays/{qid}/r01_overlay.json"
    assert mf["has_overlay_data"] is True


def test_bake_drill_savestate_fast_path_round_cache_copy(tmp_path: Path) -> None:
    qid = "1795547282177-8026"
    pq_dir = tmp_path / "personal"
    pq_dir.mkdir(parents=True)
    df = _make_sample_round_df(start_gf=15213, n_frames=300)
    df.to_parquet(pq_dir / f"{qid}.parquet", index=False)

    rc_dir = tmp_path / "replay_cache" / qid
    rc_dir.mkdir(parents=True)
    fs_bytes = b"FSSTATE" * 20000  # > 100 KB
    (rc_dir / "r01_gf15213.fs").write_bytes(fs_bytes)

    res = bake_drill_savestate(
        qid,
        start_gf=15213,
        end_gf=15390,
        selection_id="mob_8026_r01_gf15213_test",
        data_dir=tmp_path,
    )
    assert res["method"] == "round_cache_copy"
    assert Path(res["savestate"]).read_bytes() == fs_bytes
    assert Path(res["inputs"]).exists()
    assert "15213,0,0" in Path(res["inputs"]).read_text(encoding="utf-8")


def test_prune_old_mobile_replays_keeps_newest(tmp_path: Path) -> None:
    rep_root = tmp_path / "replays"
    for i in range(4):
        d = rep_root / f"179500000000{i}-100{i}"
        d.mkdir(parents=True)
        mf = d / "manifest.json"
        mf.write_text(json.dumps({"quark_id": d.name}), encoding="utf-8")
        # Stagger mtimes deterministically
        ts = 1_700_000_000 + i * 100
        import os

        os.utime(mf, (ts, ts))
    removed = prune_old_mobile_replays(tmp_path, max_sets=2)
    assert len(removed) == 2
    remaining = sorted(p.name for p in rep_root.iterdir() if p.is_dir())
    assert remaining == ["1795000000002-1002", "1795000000003-1003"]


def test_annotations_crud_and_drill_queue_sync(tmp_path: Path) -> None:
    qid = "1795547282177-8026"
    mf_dir = tmp_path / "replays" / qid
    mf_dir.mkdir(parents=True)
    (mf_dir / "manifest.json").write_text(
        json.dumps(
            {
                "quark_id": qid,
                "perspective": 1,
                "p1_char": 14,
                "p2_char": 4,
                "rounds": [{"round_id": 1, "start_gf": 15213, "end_gf": 16778, "p1_char": 14, "p2_char": 4}],
            }
        ),
        encoding="utf-8",
    )

    baked_calls: list[tuple[str, int, int, str]] = []

    def fake_bake(quark_id: str, start_gf: int, end_gf: int, *, selection_id: str, data_dir: Path):
        baked_calls.append((quark_id, start_gf, end_gf, selection_id))
        fs_p = data_dir / "selections" / f"{selection_id}.fs"
        inp_p = data_dir / "selections" / f"{selection_id}.inputs"
        fs_p.parent.mkdir(parents=True, exist_ok=True)
        fs_p.write_bytes(b"X" * 8192)
        inp_p.write_text("game_frame,p1_mask,p2_mask\n15300,0,0\n", encoding="utf-8")
        return {"savestate": str(fs_p), "inputs": str(inp_p), "method": "fake"}

    ann = create_annotation(
        {
            "quark_id": qid,
            "round_id": 1,
            "anchor_gf": 15500,
            "kind": "mistake",
            "tags": ["Anti-Air", "Corner"],
            "note": "Hesitated on cr.HP anti-air",
            "queued_drill": True,
        },
        data_dir=tmp_path,
        bake_fn=fake_bake,
        async_bake=False,
    )
    assert ann["drill_status"] == "ready"
    assert ann["start_gf"] == 15320  # 15500 - 180
    assert ann["end_gf"] == 15590    # 15500 + 90
    assert len(baked_calls) == 1

    # Verify it landed in desktop Takeover Queue with mobile tags & note
    q = load_queue(tmp_path)
    assert len(q) == 1
    assert q[0]["id"] == ann["id"]
    assert q[0]["source"] == "mobile_note"
    assert q[0]["savestate_ready"] is True
    assert "Anti-Air" in q[0]["note"]

    # Updating start_gf / end_gf on a queued_drill annotation re-bakes and syncs queue.jsonl bounds
    baked_before = len(baked_calls)
    ranged = update_annotation(
        ann["id"],
        {"start_gf": 15380, "end_gf": 15560},
        data_dir=tmp_path,
        bake_fn=fake_bake,
        async_bake=False,
    )
    assert ranged is not None
    assert ranged["start_gf"] == 15380
    assert ranged["end_gf"] == 15560
    assert ranged["is_range"] is True
    assert ranged["drill_status"] == "ready"
    assert len(baked_calls) == baked_before + 1
    assert baked_calls[-1][1:3] == (15380, 15560)
    q_after_range = load_queue(tmp_path)
    assert len(q_after_range) == 1
    assert q_after_range[0]["start_gf"] == 15380
    assert q_after_range[0]["end_gf"] == 15560

    # Validation: end_gf must be strictly greater than start_gf, positive ints only
    with pytest.raises(ValueError, match="end_gf must be greater than start_gf"):
        update_annotation(ann["id"], {"start_gf": 15600, "end_gf": 15500}, data_dir=tmp_path, bake_fn=fake_bake, async_bake=False)
    with pytest.raises(ValueError, match="end_gf must be greater than start_gf"):
        update_annotation(ann["id"], {"start_gf": 15560, "end_gf": 15560}, data_dir=tmp_path, bake_fn=fake_bake, async_bake=False)
    with pytest.raises(ValueError, match="end_gf must be greater than start_gf"):
        update_annotation(ann["id"], {"start_gf": 15600}, data_dir=tmp_path, bake_fn=fake_bake, async_bake=False)
    with pytest.raises(ValueError, match="positive integers"):
        update_annotation(ann["id"], {"start_gf": 0, "end_gf": 15560}, data_dir=tmp_path, bake_fn=fake_bake, async_bake=False)
    with pytest.raises(ValueError, match="integers"):
        update_annotation(ann["id"], {"start_gf": "bad", "end_gf": 15560}, data_dir=tmp_path, bake_fn=fake_bake, async_bake=False)

    # Unqueue drill via update_annotation -> dismissed from queue
    patched = update_annotation(
        ann["id"],
        {"note": "Updated note", "queued_drill": False},
        data_dir=tmp_path,
        bake_fn=fake_bake,
        async_bake=False,
    )
    assert patched is not None
    assert patched["note"] == "Updated note"
    assert patched["queued_drill"] is False
    assert load_queue(tmp_path) == []

    # Updating range when queued_drill is False invalidates any stale `.fs`/`.inputs` without re-baking
    stale_fs = tmp_path / "selections" / f"{ann['id']}.fs"
    stale_inp = tmp_path / "selections" / f"{ann['id']}.inputs"
    assert stale_fs.exists() and stale_inp.exists()
    baked_count_non_drill = len(baked_calls)
    ranged_non_drill = update_annotation(
        ann["id"],
        {"start_gf": 15350, "end_gf": 15520},
        data_dir=tmp_path,
        bake_fn=fake_bake,
        async_bake=False,
    )
    assert ranged_non_drill is not None
    assert ranged_non_drill["start_gf"] == 15350
    assert ranged_non_drill["end_gf"] == 15520
    assert ranged_non_drill["savestate"] is None
    assert ranged_non_drill["inputs"] is None
    assert ranged_non_drill["drill_status"] == "none"
    assert not stale_fs.exists()
    assert not stale_inp.exists()
    assert len(baked_calls) == baked_count_non_drill

    # Default lazy-bake behavior: creating a Moment without queued_drill does not bake upfront
    lazy_ann = create_annotation(
        {
            "quark_id": qid,
            "round_id": 1,
            "anchor_gf": 15600,
            "start_gf": 15480,
            "end_gf": 15690,
            "kind": "note",
            "note": "Lazy-bake default check",
        },
        data_dir=tmp_path,
        bake_fn=fake_bake,
        async_bake=False,
    )
    assert lazy_ann["queued_drill"] is False
    assert lazy_ann["drill_status"] == "none"
    assert len(baked_calls) == baked_count_non_drill
    assert load_queue(tmp_path) == []
    assert delete_annotation(lazy_ann["id"], data_dir=tmp_path) is True

    # Delete annotation
    assert delete_annotation(ann["id"], data_dir=tmp_path) is True
    assert list_annotations(tmp_path, quark_id=qid) == []

    # Set takeaway note
    sn = upsert_set_note(qid, note="Stop jumping vs SA3", tags=["Neutral"], data_dir=tmp_path)
    assert sn["note"] == "Stop jumping vs SA3"
    assert get_set_note(qid, data_dir=tmp_path)["note"] == "Stop jumping vs SA3"


def test_parse_quark_id_or_url_and_match_catalog_and_queue(tmp_path: Path) -> None:
    assert parse_quark_id_or_url("1795547282177-8026") == "1795547282177-8026"
    assert parse_quark_id_or_url("https://replay.fightcade.com/fbneo/sfiii3nr1/1795547282177-8026") == "1795547282177-8026"
    assert parse_quark_id_or_url("1795547282177-8026.7,7100") == "1795547282177-8026"
    with pytest.raises(ValueError):
        parse_quark_id_or_url("bad; rm -rf /")

    # Seed Fightcade refresh via fake fetcher
    def fake_fetch(**kwargs):
        return [
            {
                "quarkid": "1795547282177-8026",
                "date": 1795547282177,
                "duration": 310.0,
                "ranked": 1,
                "players": [
                    {"name": "player1", "rank": 3, "score": 3},
                    {"name": "kenmaster", "rank": 4, "score": 2},
                ],
            },
            {
                "quarkid": "1795500000000-1111",
                "date": 1795500000000,
                "duration": 220.0,
                "ranked": 0,
                "players": [
                    {"name": "chunpro", "rank": 5, "score": 5},
                    {"name": "player1", "rank": 3, "score": 1},
                ],
            },
        ]

    ref = refresh_match_history("player1", data_dir=tmp_path, fetch_fn=fake_fetch)
    assert ref["added"] == 2

    # Make first match already have Parquet + replay_cache (cached_no_video)
    qid1 = "1795547282177-8026"
    (tmp_path / "personal").mkdir(parents=True)
    _make_sample_round_df(15213, 120).to_parquet(tmp_path / "personal" / f"{qid1}.parquet", index=False)
    rc1 = tmp_path / "replay_cache" / qid1
    rc1.mkdir(parents=True)
    (rc1 / "r01_gf15213.fs").write_bytes(b"F" * 150_000)

    cat = list_matches(username="player1", data_dir=tmp_path)
    by_q = {m["quark_id"]: m for m in cat["matches"]}
    assert by_q[qid1]["status"] == "cached_no_video"
    assert by_q[qid1]["perspective"] == 1
    assert by_q[qid1]["self_char_name"] == "Akuma"
    assert by_q["1795500000000-1111"]["status"] == "unprocessed"
    assert by_q["1795500000000-1111"]["perspective"] == 2

    # ProcessingQueue should SKIP streaming for qid1 (fast-path!) and yield while live match is active
    in_match = {"flag": True}
    calls: list[str] = []

    def fake_scan(qid: str, **kw):
        calls.append(f"scan:{qid}")
        return {"ok": True}

    def fake_render(qid: str, **kw):
        calls.append(f"render:{qid}")
        mf_p = tmp_path / "replays" / qid / "manifest.json"
        mf_p.parent.mkdir(parents=True, exist_ok=True)
        mf_doc = {"quark_id": qid, "round_count": 1, "rounds": [{"round_id": 1, "start_gf": 15213, "end_gf": 15332}]}
        mf_p.write_text(json.dumps(mf_doc), encoding="utf-8")
        return mf_doc

    pq = ProcessingQueue(
        data_dir=tmp_path,
        is_live_match_fn=lambda: in_match["flag"],
        scan_fn=fake_scan,
        render_fn=fake_render,
        stream_cooldown_s=0.0,
    )
    try:
        pq.enqueue(qid1, perspective=1)
        time.sleep(0.15)
        # Should be paused while live match is active!
        assert calls == []
        in_match["flag"] = False
        deadline = time.time() + 5.0
        while time.time() < deadline:
            j = pq.jobs_by_quark()[qid1]
            if j["state"] == "done":
                break
            time.sleep(0.05)
        assert pq.jobs_by_quark()[qid1]["state"] == "done"
        assert calls == [f"render:{qid1}"]  # zero streaming because replay_cache + parquet existed!
    finally:
        pq.stop()


def test_mobile_http_endpoints_and_security(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "clips").mkdir()
    qid = "1795547282177-8026"
    rep_dir = tmp_path / "replays" / qid
    rep_dir.mkdir(parents=True)
    mp4_bytes = bytes(range(256)) * 8
    (rep_dir / "r01.mp4").write_bytes(mp4_bytes)
    (rep_dir / "manifest.json").write_text(
        json.dumps(
            {
                "quark_id": qid,
                "perspective": 1,
                "self_player_name": "player1",
                "opp_player_name": "kenmaster",
                "self_char_name": "Akuma",
                "opp_char_name": "Ken",
                "round_count": 1,
                "rounds": [
                    {
                        "round_id": 1,
                        "filename": "r01.mp4",
                        "video_url": f"/replays/{qid}/r01.mp4",
                        "start_gf": 15213,
                        "end_gf": 15512,
                        "frame_count": 300,
                        "fps": 60,
                        "markers": [],
                        "hp_curve": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    # Path guard allows /replays/ but still blocks /selections/ and /personal/
    assert resolve_safe_data_path(f"/replays/{qid}/r01.mp4", tmp_path) == (rep_dir / "r01.mp4").resolve()
    assert resolve_safe_data_path("/selections/annotations.jsonl", tmp_path) is None

    def fake_bake(quark_id: str, start_gf: int, end_gf: int, *, selection_id: str, data_dir: Path):
        return {"savestate": str(data_dir / "selections" / f"{selection_id}.fs"), "inputs": ""}

    server = create_server(
        host="127.0.0.1",
        port=0,
        data_dir=tmp_path,
        auto_refresh_index=False,
        bake_drill_fn=fake_bake,
        async_bake_drills=False,
    )
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # 1. Replay Review HTML + modular static assets + PWA manifest (/, /review, /mobile aliases)
        r_rev = requests.get(f"{base}/review", timeout=5)
        assert r_rev.status_code == 200
        assert "<title>Fightcade Lab</title>" in r_rev.text
        assert "/assets/styles.css" in r_rev.text
        assert "toggleTheme()" in r_rev.text
        assert 'id="btnToggleLoop"' not in r_rev.text
        assert 'id="btnExportShare"' not in r_rev.text
        assert 'id="btnNewMomentTransport"' in r_rev.text
        assert 'onclick="openComposer()"' in r_rev.text
        assert 'id="btnPreviewLoop"' in r_rev.text
        assert 'id="composerLoopBadge"' not in r_rev.text
        assert 'id="composerAutoLoopHint"' not in r_rev.text
        assert 'id="drillToggle"' not in r_rev.text
        assert 'id="btnNotesDrillOnly"' not in r_rev.text

        r_studio_js = requests.get(f"{base}/assets/js/studio.js", timeout=5)
        assert r_studio_js.status_code == 200
        assert "shareStudioMoment(" in r_studio_js.text
        assert "toggleLoopPreview(" in r_studio_js.text

        r_state_js = requests.get(f"{base}/assets/js/state.js", timeout=5)
        assert r_state_js.status_code == 200
        assert "launchDrill(" in r_state_js.text
        assert "runPracticeButton(" in r_state_js.text

        r_notes_js = requests.get(f"{base}/assets/js/notes.js", timeout=5)
        assert r_notes_js.status_code == 200
        assert "toggleNotePreview(" in r_notes_js.text
        assert "practiceHubNote(" in r_notes_js.text

        r_app_js = requests.get(f"{base}/assets/js/app.js", timeout=5)
        assert r_app_js.status_code == 200
        assert "showNotesView(" in r_app_js.text

        r_css = requests.get(f"{base}/assets/styles.css", timeout=5)
        assert r_css.status_code == 200
        assert 'html[data-theme="light"]' in r_css.text

        r_mob = requests.get(f"{base}/mobile", timeout=5)
        assert r_mob.status_code == 200
        assert "<title>Fightcade Lab</title>" in r_mob.text

        r_pwa = requests.get(f"{base}/manifest.webmanifest", timeout=5)
        assert r_pwa.status_code == 200
        pwa_json = r_pwa.json()
        assert pwa_json["display"] == "standalone"
        assert pwa_json["start_url"] == "/"
        pwa_icons = pwa_json.get("icons") or []
        assert any(
            ic.get("src") == "/assets/img/icon-192.png" and ic.get("sizes") == "192x192"
            for ic in pwa_icons
        )
        assert any(
            ic.get("src") == "/assets/img/icon-512.png" and ic.get("sizes") == "512x512"
            for ic in pwa_icons
        )

        # Brand assets (favicons, touch icon, app icons, header banner)
        assert 'href="/favicon.ico"' in r_rev.text
        assert 'href="/apple-touch-icon.png"' in r_rev.text
        assert 'class="brand-icon"' in r_rev.text
        brand_asset_expectations = [
            ("/favicon.ico", "image/x-icon"),
            ("/apple-touch-icon.png", "image/png"),
            ("/assets/img/favicon-32.png", "image/png"),
            ("/assets/img/icon-192.png", "image/png"),
            ("/assets/img/icon-512.png", "image/png"),
            ("/assets/img/header-astro-lab.jpg", "image/jpeg"),
            ("/assets/img/about-evo-moment37.jpg", "image/jpeg"),
        ]
        for asset_path, expected_ct in brand_asset_expectations:
            r_img = requests.get(f"{base}{asset_path}", timeout=5)
            assert r_img.status_code == 200, f"{asset_path} returned {r_img.status_code}"
            assert expected_ct in (r_img.headers.get("Content-Type") or "").lower(), (
                f"{asset_path} Content-Type={r_img.headers.get('Content-Type')}"
            )
            assert len(r_img.content) > 100

        r_pwa_alias = requests.get(f"{base}/review/manifest.webmanifest", timeout=5)
        assert r_pwa_alias.status_code == 200
        assert r_pwa_alias.json()["display"] == "standalone"

        # 2. Per-round MP4 supports HTTP 206 byte range streaming
        r_rng = requests.get(f"{base}/replays/{qid}/r01.mp4", headers={"Range": "bytes=0-255"}, timeout=5)
        assert r_rng.status_code == 206
        assert r_rng.content == mp4_bytes[:256]

        # 3. Match list & manifest API
        r_m = requests.get(f"{base}/api/matches?username=player1", timeout=5)
        assert r_m.status_code == 200
        assert r_m.json()["counts"]["ready"] == 1

        r_mf = requests.get(f"{base}/api/replays/{qid}/manifest", timeout=5)
        assert r_mf.status_code == 200
        assert r_mf.json()["manifest"]["round_count"] == 1

        # 4. Annotations full CRUD (POST -> GET -> PATCH -> DELETE)
        r_post = requests.post(
            f"{base}/api/annotations",
            json={
                "quark_id": qid,
                "round_id": 1,
                "anchor_gf": 15400,
                "kind": "question",
                "tags": ["Throw / Tech"],
                "note": "Can I late-tech here?",
                "queued_drill": True,
            },
            headers={"Origin": base},
            timeout=5,
        )
        assert r_post.status_code == 200
        ann_id = r_post.json()["annotation"]["id"]
        assert r_post.json()["annotation"]["drill_status"] == "ready"

        # PATCH range bounds on queued_drill annotation -> 200 OK + updated bounds + re-baked drill
        r_patch_range = requests.patch(
            f"{base}/api/annotations/{ann_id}",
            json={"start_gf": 15310, "end_gf": 15480},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_patch_range.status_code == 200
        assert r_patch_range.json()["annotation"]["start_gf"] == 15310
        assert r_patch_range.json()["annotation"]["end_gf"] == 15480
        assert r_patch_range.json()["annotation"]["drill_status"] == "ready"

        # PATCH invalid range bounds (end_gf <= start_gf) -> 400 Bad Request
        r_patch_bad = requests.patch(
            f"{base}/api/annotations/{ann_id}",
            json={"start_gf": 15480, "end_gf": 15310},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_patch_bad.status_code == 400
        assert r_patch_bad.json()["ok"] is False

        r_patch = requests.patch(
            f"{base}/api/annotations/{ann_id}",
            json={"note": "Yes, delay tech works!", "queued_drill": False},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_patch.status_code == 200
        assert r_patch.json()["annotation"]["note"] == "Yes, delay tech works!"

        r_del = requests.delete(f"{base}/api/annotations/{ann_id}", headers={"Origin": base}, timeout=5)
        assert r_del.status_code == 200
        assert r_del.json()["deleted"] == ann_id

        # 5. Cross-origin DELETE/POST blocked
        r_csrf = requests.post(
            f"{base}/api/set-notes",
            json={"quark_id": qid, "note": "evil"},
            headers={"Origin": "https://evil.example"},
            timeout=5,
        )
        assert r_csrf.status_code == 403

        # 6. Set note save & retrieve
        r_sn = requests.post(
            f"{base}/api/set-notes",
            json={"quark_id": qid, "note": "Watch Ken cr.MK range"},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_sn.status_code == 200
        r_sn_get = requests.get(f"{base}/api/set-notes?quark_id={qid}", timeout=5)
        assert r_sn_get.json()["set_note"]["note"] == "Watch Ken cr.MK range"

        # 7. Centralized Notes Hub endpoints (/notes redirect, GET /api/notes, PATCH /api/queue/<id>)
        r_notes_redir = requests.get(f"{base}/notes", allow_redirects=False, timeout=5)
        assert r_notes_redir.status_code == 302
        assert r_notes_redir.headers["Location"] == "/#notes"

        r_all_notes = requests.get(f"{base}/api/notes?username=player1", timeout=5)
        assert r_all_notes.status_code == 200
        notes_body = r_all_notes.json()
        assert notes_body["ok"] is True
        assert notes_body["counts"]["set_notes"] == 1
        assert notes_body["notes"][0]["note"] == "Watch Ken cr.MK range"

        # 8. PATCH /api/queue/<id> updates in-game select mark notes/tags
        from fightcade_analysis.select_queue import upsert_queue_items

        upsert_queue_items(
            [
                {
                    "id": f"{qid}_p1_sel1_gf15300_15450",
                    "source": "in_game_select",
                    "quark_id": qid,
                    "perspective": 1,
                    "round_id": 1,
                    "start_gf": 15300,
                    "press_gf": 15450,
                    "end_gf": 15450,
                    "self_char": 14,
                    "opp_char": 4,
                    "note": "In-game Select mark #1 (R1)",
                }
            ],
            data_dir=tmp_path,
        )
        r_qpatch = requests.patch(
            f"{base}/api/queue/{qid}_p1_sel1_gf15300_15450",
            json={"note": "Punish unsafe tatsu", "tags": ["Whiff Punish"], "kind": "mistake"},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_qpatch.status_code == 200
        assert r_qpatch.json()["item"]["note"] == "Punish unsafe tatsu"
        assert r_qpatch.json()["item"]["tags"] == ["Whiff Punish"]

        # 9. Replay manifest endpoint omits retired automatic select_marks
        r_mf2 = requests.get(f"{base}/api/replays/{qid}/manifest", timeout=5)
        assert r_mf2.status_code == 200
        mf2_body = r_mf2.json()
        assert mf2_body["select_marks"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_collated_notes_hub_merges_annotations_set_notes_select_marks_and_bookmarks(tmp_path: Path) -> None:
    """Verify centralized Notes Hub collates all 4 sources, deduplicates mobile_note copies, and computes clip previews."""
    from fightcade_analysis.notes_hub import list_collated_notes
    from fightcade_analysis.select_queue import update_queue_item, upsert_queue_items

    qid = "1795572328477-6666"
    rep_dir = tmp_path / "replays" / qid
    rep_dir.mkdir(parents=True)
    (rep_dir / "r02.mp4").write_bytes(b"MP4" * 200)
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
                        "round_id": 2,
                        "filename": "r02.mp4",
                        "video_url": f"/replays/{qid}/r02.mp4",
                        "start_gf": 18000,
                        "end_gf": 19800,
                        "frame_count": 1801,
                        "fps": 60,
                        "p1_char": 11,
                        "p2_char": 14,
                        "p1_char_name": "Ken",
                        "p2_char_name": "Akuma",
                        "winner": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def fake_bake(quark_id: str, start_gf: int, end_gf: int, *, selection_id: str, data_dir: Path):
        fs_p = data_dir / "selections" / f"{selection_id}.fs"
        inp_p = data_dir / "selections" / f"{selection_id}.inputs"
        fs_p.parent.mkdir(parents=True, exist_ok=True)
        fs_p.write_bytes(b"S" * 8192)
        inp_p.write_text("game_frame,p1_mask,p2_mask\n18386,0,0\n", encoding="utf-8")
        return {"savestate": str(fs_p), "inputs": str(inp_p)}

    # 1. Moment annotation (also writes a mobile_note copy to queue.jsonl — must NOT duplicate!)
    ann = create_annotation(
        {
            "quark_id": qid,
            "round_id": 2,
            "perspective": 2,
            "anchor_gf": 18503,
            "start_gf": 18386,
            "end_gf": 18503,
            "kind": "mistake",
            "tags": ["Anti-Air"],
            "note": "Should parry the air-to-air here",
            "queued_drill": True,
        },
        data_dir=tmp_path,
        bake_fn=fake_bake,
        async_bake=False,
    )

    # 2. Set takeaway note
    upsert_set_note(qid, note="Respect Ken EX tatsu on wakeup", tags=["Wakeup / Oki"], data_dir=tmp_path)

    # 3. In-game Select mark with placeholder player names (should backfill from manifest!)
    sel_fs = tmp_path / "selections" / f"{qid}_p2_sel1_gf18600_18960.fs"
    sel_fs.parent.mkdir(parents=True, exist_ok=True)
    sel_fs.write_bytes(b"F" * 8192)
    upsert_queue_items(
        [
            {
                "id": f"{qid}_p2_sel1_gf18600_18960",
                "source": "in_game_select",
                "quark_id": qid,
                "perspective": 2,
                "round_id": 2,
                "start_gf": 18600,
                "press_gf": 18960,
                "end_gf": 18960,
                "self_char": 14,
                "opp_char": 11,
                "self_player_name": "P2",
                "opp_player_name": "P1",
                "note": "In-game Select mark #1 (R2)",
                "savestate": str(sel_fs),
            }
        ],
        data_dir=tmp_path,
    )

    # 4. Manual Lua bookmark
    bm_path = tmp_path / "selections" / "bookmarks.jsonl"
    with bm_path.open("a", encoding="utf-8") as bf:
        bf.write(
            json.dumps(
                {
                    "id": "manual_bm_1",
                    "quark_id": qid,
                    "perspective": 2,
                    "round_id": 2,
                    "start_gf": 19000,
                    "end_gf": 19180,
                    "p1_char": 11,
                    "p2_char": 14,
                    "note": "Corner escape test",
                }
            )
            + "\n"
        )

    # Default collation omits retired automatic in-game Select marks
    res_default = list_collated_notes(username="player1", data_dir=tmp_path)
    assert res_default["ok"] is True
    assert res_default["counts"]["total"] == 3
    assert res_default["counts"]["select_marks"] == 0

    # Explicit opt-in include_select_marks=True preserves backward-compatible collation
    res = list_collated_notes(username="player1", data_dir=tmp_path, include_select_marks=True)
    assert res["ok"] is True
    # Exactly 4 items (mobile_note queue copy was deduplicated!)
    assert res["counts"]["total"] == 4
    assert res["counts"]["moments"] == 1
    assert res["counts"]["set_notes"] == 1
    assert res["counts"]["select_marks"] == 1
    assert res["counts"]["bookmarks"] == 1
    assert res["counts"]["needs_note"] == 1  # the default-text select mark

    by_id = {n["id"]: n for n in res["notes"]}

    # Moment annotation has inline video preview timings & perspective-safe characters
    a_item = by_id[ann["id"]]
    assert a_item["self_char_name"] == "Akuma"
    assert a_item["opp_char_name"] == "Ken"
    assert a_item["opp_player_name"] == "Chuxster22"
    assert a_item["opp_rank_letter"] == "B"
    assert a_item["has_video"] is True
    assert a_item["video_url"] == f"/replays/{qid}/r02.mp4"
    assert a_item["clip_start_s"] == pytest.approx((18386 - 18000) / 60.0, abs=0.01)
    assert a_item["clip_end_s"] == pytest.approx((18503 - 18000) / 60.0, abs=0.01)
    assert a_item["can_launch_drill"] is True

    # In-game Select mark backfilled opponent handle & can be annotated inline
    sel_id = f"{qid}_p2_sel1_gf18600_18960"
    s_item = by_id[sel_id]
    assert s_item["opp_player_name"] == "Chuxster22"
    assert s_item["self_char_name"] == "Akuma"
    assert s_item["opp_char_name"] == "Ken"
    assert s_item["needs_note"] is True
    assert s_item["has_video"] is True
    assert s_item["can_launch_drill"] is True

    # Updating the Select mark via update_queue_item clears needs_note & adds tags
    updated_sel = update_queue_item(sel_id, {"note": "Delay tech worked here", "tags": ["Throw / Tech"], "kind": "question"}, data_dir=tmp_path)
    assert updated_sel is not None
    assert updated_sel["note"] == "Delay tech worked here"
    assert updated_sel["tags"] == ["Throw / Tech"]

    res2 = list_collated_notes(username="player1", data_dir=tmp_path, include_select_marks=True)
    s_item2 = next(n for n in res2["notes"] if n["id"] == sel_id)
    assert s_item2["needs_note"] is False
    assert s_item2["kind"] == "question"
    assert s_item2["tags"] == ["Throw / Tech"]
    assert res2["counts"]["needs_note"] == 0


def test_other_username_search_isolation_and_punctuation_handles(tmp_path: Path) -> None:
    """Searching another Fightcade handle (including punctuation like `!!M_DAVE!!`) must not
    leak the default user's local matches or overwrite `data/config.json`."""
    from fightcade_analysis.match_catalog import detect_default_username, save_default_username

    save_default_username("player1", data_dir=tmp_path)

    # Seed local personal parquet + rendered mobile replay belonging ONLY to player1 vs kenmaster
    my_qid = "1795547282177-8026"
    (tmp_path / "personal").mkdir(parents=True)
    _make_sample_round_df(15213, 120).to_parquet(tmp_path / "personal" / f"{my_qid}.parquet", index=False)
    rep_dir = tmp_path / "replays" / my_qid
    rep_dir.mkdir(parents=True)
    (rep_dir / "manifest.json").write_text(
        json.dumps(
            {
                "quark_id": my_qid,
                "perspective": 1,
                "self_player_name": "player1",
                "opp_player_name": "kenmaster",
                "p1_char_name": "Akuma",
                "p2_char_name": "Ken",
                "round_count": 1,
                "rounds": [{"round_id": 1, "start_gf": 15213, "end_gf": 15332}],
            }
        ),
        encoding="utf-8",
    )

    # Also seed one unsynced local replay with NO player names (should show ONLY for default user)
    anon_qid = "1795547299999-9999"
    anon_dir = tmp_path / "replays" / anon_qid
    anon_dir.mkdir(parents=True)
    (anon_dir / "manifest.json").write_text(
        json.dumps(
            {
                "quark_id": anon_qid,
                "perspective": 1,
                "round_count": 1,
                "rounds": [{"round_id": 1, "start_gf": 1000, "end_gf": 1200}],
            }
        ),
        encoding="utf-8",
    )

    # Seed one shared local replay between player1 and !!M_DAVE!!
    shared_qid = "1795620894986-7715"
    shared_dir = tmp_path / "replays" / shared_qid
    shared_dir.mkdir(parents=True)
    (shared_dir / "manifest.json").write_text(
        json.dumps(
            {
                "quark_id": shared_qid,
                "perspective": 1,
                "self_player_name": "player1",
                "opp_player_name": "!!M_DAVE!!",
                "p1_char_name": "Akuma",
                "p2_char_name": "Dudley",
                "round_count": 1,
                "rounds": [{"round_id": 1, "start_gf": 2000, "end_gf": 2200}],
            }
        ),
        encoding="utf-8",
    )

    # Before refreshing !!M_DAVE!!, searching them must show ONLY the shared match (zero stale player1 matches!)
    pre = list_matches(username="!!M_DAVE!!", data_dir=tmp_path)
    assert [m["quark_id"] for m in pre["matches"]] == [shared_qid]
    assert pre["matches"][0]["perspective"] == 2
    assert pre["matches"][0]["self_player_name"] == "!!M_DAVE!!"
    assert pre["matches"][0]["opp_player_name"] == "player1"

    # Refreshing !!M_DAVE!! succeeds with punctuation handle and does NOT clobber config.json
    def fake_fetch_dave(**kwargs):
        assert kwargs["username"] == "!!M_DAVE!!"
        return [
            {
                "quarkid": "1795619820862-9324",
                "date": 1795619820862,
                "duration": 260.0,
                "ranked": 1,
                "players": [
                    {"name": "Aokoharu", "rank": 4, "score": 2},
                    {"name": "!!M_DAVE!!", "rank": 3, "score": 3},
                ],
            }
        ]

    ref = refresh_match_history("!!M_DAVE!!", data_dir=tmp_path, fetch_fn=fake_fetch_dave)
    assert ref["ok"] is True
    assert ref["username"] == "!!M_DAVE!!"
    assert ref["added"] == 1
    assert detect_default_username(tmp_path) == "player1"

    # Cache file for !!M_DAVE!! does not collide with M_DAVE
    post_dave = list_matches(username="!!M_DAVE!!", data_dir=tmp_path)
    assert {m["quark_id"] for m in post_dave["matches"]} == {shared_qid, "1795619820862-9324"}
    post_plain = list_matches(username="M_DAVE", data_dir=tmp_path)
    assert post_plain["matches"] == []

    # Path traversal / control characters remain rejected
    with pytest.raises(ValueError):
        refresh_match_history("../evil", data_dir=tmp_path, fetch_fn=fake_fetch_dave)
    with pytest.raises(ValueError):
        list_matches(username="bad/name", data_dir=tmp_path)


def test_mobile_topbar_hamburger_drawer_and_brand_icon():
    web_dir = Path(__file__).resolve().parents[1] / "src" / "fightcade_analysis" / "web"
    html = (web_dir / "index.html").read_text(encoding="utf-8")
    css = (web_dir / "styles.css").read_text(encoding="utf-8")
    app_js = (web_dir / "js" / "app.js").read_text(encoding="utf-8")
    pop_js = (web_dir / "js" / "status_popover.js").read_text(encoding="utf-8")

    # Mobile hamburger toggle, single-source-of-truth drawer, and backdrop exist
    assert 'id="btnMobileMenu"' in html
    assert 'aria-controls="topbarMenuDrawer"' in html
    assert 'id="topbarMenuDrawer"' in html
    assert 'id="mobileMenuBackdrop"' in html

    # Single DOM source of truth: no duplicated nav/auth/status/theme IDs
    for single_id in (
        'id="navTabs"',
        'id="navHome"',
        'id="navMatches"',
        'id="navNotes"',
        'id="navInsights"',
        'id="authControls"',
        'id="btnStatusPill"',
        'id="btnThemeToggle"',
        'id="studioBreadcrumb"',
    ):
        assert html.count(single_id) == 1, f"Expected single instance of {single_id}"

    # #navTabs and .topbar-right live inside #topbarMenuDrawer; #studioBreadcrumb stays in .topbar-left
    drawer_start = html.index('id="topbarMenuDrawer"')
    btn_menu_pos = html.index('id="btnMobileMenu"')
    assert html.index('id="studioBreadcrumb"') < drawer_start
    assert drawer_start < html.index('id="navTabs"') < btn_menu_pos
    assert drawer_start < html.index('id="btnStatusPill"') < btn_menu_pos
    assert drawer_start < html.index('id="btnThemeToggle"') < btn_menu_pos

    # Mobile CSS rules (<= 899px): single-row nowrap topbar, 36x36 brand icon, drawer open rules
    mobile_section = css[css.index("@media (max-width: 899px) {"):]
    assert "flex-wrap: nowrap;" in mobile_section
    assert ".brand-icon { width: 36px; height: 36px; }" in mobile_section
    assert ".mobile-menu-btn {" in mobile_section
    assert ".topbar-menu-drawer.open {" in mobile_section
    assert ".mobile-menu-backdrop {" in mobile_section

    # JS interaction wiring
    for fn_name in ("function toggleMobileNavMenu()", "function closeMobileNavMenu(", "function isMobileNavMenuOpen()"):
        assert fn_name in app_js
    assert "closeMobileNavMenu()" in pop_js
