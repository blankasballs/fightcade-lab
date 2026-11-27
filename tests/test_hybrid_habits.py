"""Unit tests for Milestone 5 (ADR-001): Hybrid VLM + RAM Verified Habit Pipeline."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from fightcade_analysis.hybrid_habits import (
    HABIT_TAXONOMY,
    aggregate_verified_habits,
    detect_health_loss_episodes,
    render_verified_habits_html,
    select_seed_episodes,
)
from fightcade_analysis.naming import build_reports_index_html, build_verified_habits_stem


def test_detect_health_loss_episodes_anchors_before_superfreeze_and_hit1():
    # Simulate a grounded wakeup overhead eating an opponent Super Art:
    # frames 0..19: normal (hp=160, opp_sf=0, opp_combo=0)
    # frames 20..29: opponent Superfreeze flash (opp_sf=10..1, hp=160)
    # frames 30..45: 3-hit Super Art combo (hp drops 160 -> 140 -> 120 -> 95, opp_combo=1..3)
    # frames 46..85: neutral recovery (hp=95, opp_combo=0)
    # frames 86..90: tiny chip (-4 HP) -> should be filtered out by min_hp_loss=14
    rows = []
    for i in range(95):
        hp = 160
        sf = 0
        oc = 0
        if 20 <= i <= 29:
            sf = 30 - i
        elif 30 <= i <= 34:
            hp, oc = 140, 1
        elif 35 <= i <= 39:
            hp, oc = 120, 2
        elif 40 <= i <= 45:
            hp, oc = 95, 3
        elif 46 <= i <= 85:
            hp, oc = 95, 0
        else:
            hp, oc = 91, 0
        rows.append(
            {
                "quark_id": "test-super",
                "round_id": 1,
                "in_match": 1,
                "game_frame": 1000 + i,
                "p1_char": 14,
                "p2_char": 2,
                "p1_sa": 1,
                "p2_sa": 1,
                "p1_hp": hp,
                "p2_hp": 160,
                "p1_combo": 0,
                "p2_combo": oc,
                "p1_superfreeze": 0,
                "p2_superfreeze": sf,
            }
        )
    df = pd.DataFrame(rows)
    eps = detect_health_loss_episodes(df, perspective=1, min_hp_loss=14)
    assert len(eps) == 1
    ep = eps[0]
    assert ep["hp_lost"] == 65  # 160 -> 95 collapsed into ONE episode (not 3!)
    assert ep["combo_hits"] == 3
    assert ep["had_superfreeze"] is True
    # MUST backtrack strictly before Superfreeze (gf 1020) -> gf 1018, NOT mid-juggle gf 1038!
    assert ep["t_cause_gf"] == 1018
    assert ep["t_hit1_gf"] == 1030


def test_select_seed_episodes_enforces_set_diversity():
    eps = [
        {"episode_id": f"s1_{i}", "quark_id": "s1", "round_id": i, "t_cause_gf": 1000 + i * 500, "hp_lost": 80 - i, "hp_lost_pct": 50.0 - i, "was_ko": False}
        for i in range(5)
    ] + [
        {"episode_id": f"s2_{i}", "quark_id": "s2", "round_id": i, "t_cause_gf": 1000 + i * 500, "hp_lost": 60 - i, "hp_lost_pct": 37.5 - i, "was_ko": False}
        for i in range(3)
    ]
    picked = select_seed_episodes(eps, seed_count=4, max_per_set=2)
    assert len(picked) == 4
    qids = [p["quark_id"] for p in picked]
    assert qids.count("s1") == 2
    assert qids.count("s2") == 2


def test_aggregate_verified_habits_html_and_hub_badge(tmp_path: Path):
    sets_meta = [
        {"quark_id": "s1", "match_date": "2026-11-23", "opp_player_name": "Bocku!", "rounds": 6, "self_score": 1, "opp_score": 3},
        {"quark_id": "s2", "match_date": "2026-11-23", "opp_player_name": "inertial97", "rounds": 7, "self_score": 0, "opp_score": 3},
    ]
    ep1 = {
        "episode_id": "s1_p1_gf1018",
        "quark_id": "s1",
        "perspective": 1,
        "round_id": 1,
        "t_cause_gf": 1018,
        "hp_lost": 65,
        "hp_lost_pct": 40.6,
        "combo_hits": 3,
        "was_ko": False,
        "self_char_name": "Akuma",
        "opp_char_name": "Ryu",
        "match_date": "2026-11-23",
        "opp_player_name": "Bocku!",
        "clip_rel_path": "../clips/s1_gf1018.mp4",
        "visual_tag": {
            "category": "wakeup_reversal_or_super_punished",
            "category_label": HABIT_TAXONOMY["wakeup_reversal_or_super_punished"],
            "what_happened": "Akuma went for a meaty overhead on Ryu's wakeup and ate SA1.",
            "self_mistake": "Committed to slow overhead against stocked Super meter.",
            "opp_move": "Wakeup SA1",
            "confidence": "high",
        },
    }
    ep2 = {
        "episode_id": "s2_p1_gf2200",
        "quark_id": "s2",
        "perspective": 1,
        "round_id": 2,
        "t_cause_gf": 2200,
        "hp_lost": 48,
        "hp_lost_pct": 30.0,
        "combo_hits": 2,
        "was_ko": False,
        "self_char_name": "Akuma",
        "opp_char_name": "Alex",
        "match_date": "2026-11-23",
        "opp_player_name": "inertial97",
        "clip_rel_path": "../clips/s2_gf2200.mp4",
        "visual_tag": {
            "category": "missed_anti_air",
            "category_label": HABIT_TAXONOMY["missed_anti_air"],
            "what_happened": "Alex jumped in with j.HP and Akuma walked back instead of anti-airing.",
            "self_mistake": "Did not DP or s.HK the jump-in.",
            "opp_move": "Jump-in HP",
            "confidence": "high",
        },
    }

    rollup = aggregate_verified_habits("player1", sets_meta, [ep1, ep2], [ep1, ep2], corpus_size=70281)
    assert rollup["visually_verified_clips"] == 2
    assert rollup["habits"][0]["category"] == "wakeup_reversal_or_super_punished"
    assert rollup["habits"][0]["total_hp_lost"] == 65

    # Attach one visually verified pro solution and one rejected pro candidate
    rollup["habits"][0]["verified_pro_solutions"] = [
        {
            "quark_id": "pro-99",
            "perspective": 1,
            "round_id": 3,
            "game_frame": 4100,
            "self_char_name": "Akuma",
            "opp_char_name": "Ryu",
            "outcome_value": 0.25,
            "similarity_pct": 82,
            "clip_rel_path": "../clips/pro-99_gf4100.mp4",
            "verification": {
                "is_relevant": True,
                "relevance_reason": "Both show Akuma pressuring Ryu's wakeup with SA1 stocked.",
                "pro_solution": "Pro Akuma walked up and blocked to bait and punish the wakeup Super.",
            },
        }
    ]
    rollup["habits"][0]["rejected_pro_count"] = 1
    rollup["ai_synthesis"] = {
        "headline": "Respect wakeup Super meter on oki.",
        "top_priorities": [
            {"habit_rank": 1, "category": "wakeup_reversal_or_super_punished", "title": "Bait Wakeup Supers", "diagnosis": "Ate SA1 on meaty overhead.", "drill": "Shimmy/block on wakeup when opponent has Super."}
        ],
        "next_training_partner_focus": "Drill Alex jump-ins.",
        "model": "gemini-3-flash-preview",
    }

    stem = build_verified_habits_stem(rollup)
    assert stem == "2026-11-23_player1_akuma_verified_habits_2sets"

    html_doc = render_verified_habits_html(rollup)
    assert "Hit by Wakeup Reversal / Super" in html_doc
    assert "VISUALLY VERIFIED TOP-RANK SOLUTION" in html_doc
    assert "1 false match(es) filtered out" in html_doc
    assert 'href="#habit-1"' in html_doc

    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / f"{stem}.json").write_text(json.dumps(rollup), encoding="utf-8")
    (reports_dir / f"{stem}.html").write_text(html_doc, encoding="utf-8")
    hub = build_reports_index_html(reports_dir).read_text(encoding="utf-8")
    assert "Verified Habits" in hub
    assert f"{stem}.html" in hub


def test_single_match_hybrid_html_rendering(tmp_path: Path):
    from fightcade_analysis.report_ui import build_report_html

    clips_dir = tmp_path / "clips"
    sav_dir = tmp_path / "savestates"
    clips_dir.mkdir()
    sav_dir.mkdir()
    (clips_dir / "q1_gf1018.mp4").write_bytes(b"mp4")
    (sav_dir / "q1_gf1018.fs").write_bytes(b"fs")

    report = {
        "pipeline": "hybrid_v2",
        "report_type": "full_set",
        "target_quark_id": "q1",
        "perspective": 2,
        "self_player_name": "player1",
        "opp_player_name": "DraculaFlow",
        "self_char_name": "Akuma",
        "opp_char_name": "Hugo",
        "match_date": "2026-11-23",
        "total_damage_episodes": 12,
        "corpus_size": 70281,
        "moments": [
            {
                "pipeline": "hybrid_v2",
                "quark_id": "q1",
                "perspective": 2,
                "round_id": 3,
                "game_frame": 1018,
                "self_char_name": "Akuma",
                "opp_char_name": "Hugo",
                "label": "Missed Anti-Air — Akuma vs Hugo (Jump-in HP)",
                "category": "missed_anti_air",
                "category_label": "Missed Anti-Air",
                "hp_lost": 48,
                "hp_lost_pct": 30.0,
                "combo_hits": 2,
                "was_ko": False,
                "had_superfreeze": False,
                "retrieval_tier": "exact_matchup",
                "visual_tag": {
                    "what_happened": "Hugo jumped in with j.HP and hit grounded Akuma.",
                    "self_mistake": "Walked back instead of anti-airing with DP.",
                    "model": "gemini-3-flash-preview",
                },
                "verified_pro_solutions": [],
                "rejected_pro_count": 2,
                "regret": 0.30,
                "options": [],
            }
        ],
    }
    out_html = tmp_path / "reports" / "test_hybrid.html"
    build_report_html(report, out_html, clips_dir=clips_dir, savestates_dir=sav_dir)
    text = out_html.read_text(encoding="utf-8")
    assert "Pass-1 Visual Breakdown" in text
    assert "Missed Anti-Air" in text
    assert "filtered out 2 non-matching RAM top-rank candidate(s)" in text
    assert "Instant &lt;1s .fs Ready" in text
