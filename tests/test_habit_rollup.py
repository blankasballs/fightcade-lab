"""Unit tests for Multi-Set Personal Habit & Leak Rollup and Reports Hub integration."""

from __future__ import annotations

import json
from pathlib import Path

from fightcade_analysis.habit_rollup import (
    aggregate_habit_rollup,
    discover_player_sets,
    normalize_interaction_bucket,
    render_habit_rollup_html,
)
from fightcade_analysis.naming import build_habit_rollup_stem, build_reports_index_html


def test_normalize_interaction_bucket():
    assert normalize_interaction_bucket("Neutral Footsies (142px)") == "Neutral Footsies"
    assert normalize_interaction_bucket("Corner Blockstring Defense") == "Corner Blockstring Defense"
    assert normalize_interaction_bucket("  Anti-Air Decision (85 px) ") == "Anti-Air Decision"


def test_discover_player_sets_detects_side(tmp_path: Path):
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "1795443936010-2584.parquet").write_bytes(b"stub")
    (personal / "1795444743213-3167.parquet").write_bytes(b"stub")
    (personal / "1795999999999-1111.parquet").write_bytes(b"stub")

    manifest = {
        "summaries": [
            {"quarkid": "1795443936010-2584", "date": 1795443936010, "p1_name": "player1", "p2_name": "inertial97", "p1_score": 0, "p2_score": 3},
            {"quarkid": "1795444743213-3167", "date": 1795444743213, "p1_name": "MambaMentality", "p2_name": "player1", "p1_score": 3, "p2_score": 1},
            {"quarkid": "1795999999999-1111", "date": 1795999999999, "p1_name": "TenRUN", "p2_name": "Cruise"},
        ]
    }
    (tmp_path / "player1_replays.json").write_text(json.dumps(manifest), encoding="utf-8")

    found = discover_player_sets("player1", personal_dir=personal, data_dir=tmp_path)
    assert len(found) == 2
    by_id = {f["quark_id"]: f for f in found}
    assert by_id["1795443936010-2584"]["perspective"] == 1
    assert by_id["1795443936010-2584"]["opp_player_name"] == "inertial97"
    assert by_id["1795444743213-3167"]["perspective"] == 2
    assert by_id["1795444743213-3167"]["opp_player_name"] == "MambaMentality"
    assert by_id["1795444743213-3167"]["self_score"] == 1


def test_aggregate_habit_rollup_and_hub_index(tmp_path: Path):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    # Pretend a full-set report already exists for set1
    (reports_dir / "2026-11-23_player1_akuma_vs_inertial97_alex_2584.html").write_text("<html></html>", encoding="utf-8")
    (reports_dir / "2026-11-23_player1_akuma_vs_inertial97_alex_2584.json").write_text(
        json.dumps({"report_type": "full_set", "target_quark_id": "1795443936010-2584", "moments": []}),
        encoding="utf-8",
    )

    def _mk_moment(qid: str, inter: str, regime: str, regret: float, act: str, alt: str, opp: str):
        return {
            "quark_id": qid,
            "round_id": 1,
            "game_frame": 1000,
            "self_char_name": "Akuma",
            "opp_char_name": opp,
            "interaction": inter,
            "regime": regime,
            "label": f"Akuma vs {opp} — {inter}",
            "user_action": act,
            "actual_value": -0.25,
            "actual_hp_swing": -0.22,
            "ev_corpus": 0.05,
            "regret": regret,
            "match_similarity_pct": 78,
            "options": [
                {
                    "action_coarse": alt,
                    "ev": 0.08,
                    "ev_gain_vs_user": 0.33,
                    "proofs": [
                        {
                            "quark_id": "pro-123",
                            "perspective": 1,
                            "round_id": 2,
                            "game_frame": 2500,
                            "self_char_name": "Akuma",
                            "opp_char_name": opp,
                            "action_coarse": alt,
                            "outcome_value": 0.12,
                            "similarity_pct": 81,
                        }
                    ],
                }
            ],
        }

    diags = [
        {
            "quark_id": "1795443936010-2584",
            "match_date": "2026-11-23",
            "perspective": 1,
            "self_player_name": "player1",
            "opp_player_name": "inertial97",
            "rounds": 7,
            "report": {
                "target_quark_id": "1795443936010-2584",
                "perspective": 1,
                "self_char_name": "Akuma",
                "opp_char_name": "Alex",
                "total_decision_points": 140,
                "corpus_size": 70281,
                "moments": [
                    _mk_moment("1795443936010-2584", "Corner Blockstring Defense", "corner_defense", 0.32, "jump_attack", "block_or_parry", "Alex"),
                    _mk_moment("1795443936010-2584", "Neutral Footsies (135px)", "neutral", 0.14, "whiff_heavy", "poke_low", "Alex"),
                ],
            },
        },
        {
            "quark_id": "1795444743213-3167",
            "match_date": "2026-11-23",
            "perspective": 1,
            "self_player_name": "player1",
            "opp_player_name": "MambaMentality",
            "rounds": 6,
            "report": {
                "target_quark_id": "1795444743213-3167",
                "perspective": 1,
                "self_char_name": "Akuma",
                "opp_char_name": "Ken",
                "total_decision_points": 120,
                "corpus_size": 70281,
                "moments": [
                    _mk_moment("1795444743213-3167", "Corner Blockstring Defense", "corner_defense", 0.28, "jump_attack", "block_or_parry", "Ken"),
                    _mk_moment("1795444743213-3167", "Neutral Footsies (178px)", "neutral", 0.16, "walk_back", "poke_low", "Ken"),
                ],
            },
        },
    ]

    rollup = aggregate_habit_rollup("player1", diags, reports_dir=reports_dir, data_dir=tmp_path)
    assert rollup["num_sets"] == 2
    assert rollup["total_rounds"] == 13
    assert rollup["primary_char_name"] == "Akuma"
    assert len(rollup["habits"]) == 2
    top = rollup["habits"][0]
    assert top["interaction"] == "Corner Blockstring Defense"
    assert top["sets_affected"] == 2
    assert top["is_recurring"] is True
    assert top["total_regret"] == 0.6
    assert top["examples"][0]["report_href"] == "2026-11-23_player1_akuma_vs_inertial97_alex_2584.html"

    stem = build_habit_rollup_stem(rollup)
    assert stem == "2026-11-23_player1_akuma_habit_rollup_2sets"

    # Verify video card rendering (strictly user footage by default, plus anchor jump links)
    top["examples"][0]["clip_rel_path"] = "../clips/1795443936010-2584_gf1000.mp4"
    rollup["ai_synthesis"] = {
        "headline": "Test headline",
        "top_priorities": [
            {"habit_rank": 1, "interaction": "Corner Blockstring Defense", "title": "Blockstring", "diagnosis": "Diag", "drill": "Drill"}
        ],
        "model": "gemini-3-flash-preview",
    }
    html_doc = render_habit_rollup_html(rollup)
    assert "Corner Blockstring Defense" in html_doc
    assert "Neutral Footsies" in html_doc
    assert "Copy Takeover" in html_doc
    assert "../clips/1795443936010-2584_gf1000.mp4" in html_doc
    assert 'id="habit-1"' in html_doc
    assert 'href="#habit-1"' in html_doc
    assert "PRO ANSWER" not in html_doc

    (reports_dir / f"{stem}.json").write_text(json.dumps(rollup), encoding="utf-8")
    (reports_dir / f"{stem}.html").write_text(html_doc, encoding="utf-8")
    hub = build_reports_index_html(reports_dir)
    hub_text = hub.read_text(encoding="utf-8")
    assert "Habit Rollup" in hub_text
    assert f"{stem}.html" in hub_text
