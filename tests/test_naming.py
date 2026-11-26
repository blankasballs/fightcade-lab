"""Unit tests for human-readable report naming, manifest metadata lookup, and Reports Hub HTML."""

from __future__ import annotations

import json
from pathlib import Path
import re

from fightcade_analysis.naming import (
    build_coaching_report_stem,
    build_reports_index_html,
    build_sequence_search_stem,
    char_slug,
    extract_match_date,
    format_matchup_players_html,
    format_rank_letter,
    short_quark_suffix,
    slugify,
)


def test_slugify_char_slug_and_short_suffix() -> None:
    assert slugify("#blessed") == "blessed"
    assert slugify("Alex corner pressure into jump-in HK", max_len=28, sep="-") == "alex-corner-pressure-into-ju"
    assert slugify("player1", max_len=18, sep="") == "player1"
    assert char_slug("Chun-Li") == "chunli"
    assert char_slug("Akuma") == "akuma"
    assert short_quark_suffix("1795443936010-2584") == "2584"


def test_extract_match_date_from_quark_id_and_manifest() -> None:
    d = extract_match_date("1795443936010-2584")
    assert re.match(r"^2026-11-2[23]$", d)
    d2 = extract_match_date("custom-replay", {"date": 1795443936010})
    assert d2 == d


def test_coaching_and_sequence_stems_with_and_without_manifest(tmp_path: Path) -> None:
    manifest = {
        "summaries": [
            {
                "quarkid": "1795443936010-2584",
                "date": 1795443936010,
                "p1_name": "player1",
                "p2_name": "inertial97",
                "ranked": 3,
            }
        ]
    }
    (tmp_path / "player1_replays.json").write_text(json.dumps(manifest), encoding="utf-8")

    coach_rep = {
        "target_quark_id": "1795443936010-2584",
        "perspective": 1,
        "self_char_name": "Akuma",
        "opp_char_name": "Alex",
        "moments": [],
    }
    stem = build_coaching_report_stem(coach_rep, data_dir=tmp_path)
    assert stem.endswith("_player1_akuma_vs_inertial97_alex_2584")
    assert coach_rep["self_player_name"] == "player1"
    assert coach_rep["opp_player_name"] == "inertial97"

    # Without manifest -> graceful p1 / p2 fallback
    no_meta_rep = {
        "target_quark_id": "1795443936010-9999",
        "perspective": 2,
        "self_char_name": "Chun-Li",
        "opp_char_name": "Ken",
        "moments": [],
    }
    fallback_stem = build_coaching_report_stem(no_meta_rep, data_dir=tmp_path)
    assert fallback_stem.endswith("_p2_chunli_vs_p1_ken_9999")

    # Sequence search stem groups directly under same match prefix
    seq_res = {
        "selection_id": "1795443936010-2584_p1_gf17660_17780",
        "note": "Alex corner trap",
        "query": {
            "quark_id": "1795443936010-2584",
            "perspective": 1,
            "round_id": 2,
            "start_gf": 17660,
            "end_gf": 17780,
            "self_char_name": "Akuma",
            "opp_char_name": "Alex",
            "label": "Akuma (Neutral) vs Alex (Neutral) — Neutral Footsies (180px)",
        },
        "matches": [],
    }
    seq_stem = build_sequence_search_stem(seq_res, data_dir=tmp_path)
    assert seq_stem.startswith(stem + "_")
    assert seq_stem.endswith("_r2_alex-corner-trap_gf17660-17780")


def test_build_reports_index_html_lists_both_report_types(tmp_path: Path) -> None:
    rep_dir = tmp_path / "reports"
    rep_dir.mkdir()

    # Full-set report pair
    s1 = "2026-11-23_player1_akuma_vs_inertial97_alex_2584"
    (rep_dir / f"{s1}.json").write_text(
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
                "total_decision_points": 138,
                "moments": [{"regret": 0.546}],
            }
        ),
        encoding="utf-8",
    )
    (rep_dir / f"{s1}.html").write_text("<html>full set</html>", encoding="utf-8")

    # Sequence search pair
    s2 = "2026-11-23_player1_akuma_vs_inertial97_alex_2584_r2_alex-corner-trap_gf17660-17780"
    (rep_dir / f"{s2}.json").write_text(
        json.dumps(
            {
                "report_type": "sequence_search",
                "match_date": "2026-11-23",
                "self_player_name": "player1",
                "opp_player_name": "inertial97",
                "note": "Alex corner trap",
                "retrieval_tier": "exact_matchup",
                "query": {
                    "quark_id": "1795443936010-2584",
                    "perspective": 1,
                    "round_id": 2,
                    "start_gf": 17660,
                    "end_gf": 17780,
                    "duration_s": 2.02,
                    "self_char_name": "Akuma",
                    "opp_char_name": "Alex",
                },
                "matches": [{"similarity_pct": 69}],
            }
        ),
        encoding="utf-8",
    )
    (rep_dir / f"{s2}.html").write_text("<html>seq</html>", encoding="utf-8")

    # Cache file should be ignored
    (rep_dir / "gemini_summary_cache.json").write_text("{}", encoding="utf-8")

    idx_path = build_reports_index_html(rep_dir)
    text = idx_path.read_text(encoding="utf-8")
    assert "Fightcade Analysis — Reports Hub" in text
    assert f'href="{s1}.html"' in text
    assert f'href="{s2}.html"' in text
    assert "Full Set" in text
    assert "Sequence Search" in text
    assert "gemini_summary_cache" not in text


def test_format_matchup_players_html_with_ranks(tmp_path: Path):
    assert format_rank_letter(6) == "S"
    assert format_rank_letter(5) == "A"
    assert format_rank_letter(0) == "U"
    assert format_rank_letter(None) == ""

    manifest = {
        "summaries": [
            {
                "quarkid": "1795259745266-9712",
                "p1_name": "Otana",
                "p1_rank": 6,
                "p2_name": "Slowdive",
                "p2_rank": 5,
            }
        ]
    }
    (tmp_path / "general_pro_corpus.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Perspective 2 reverses order so focused player comes first
    html_p2 = format_matchup_players_html(
        "1795259745266-9712", 2, "Akuma", "Hugo", data_dir=tmp_path
    )
    assert "Slowdive" in html_p2 and ">A<" in html_p2
    assert "Otana" in html_p2 and ">S<" in html_p2
    assert html_p2.index("Slowdive") < html_p2.index("Otana")

    # Unknown quark falls back cleanly to character names only
    html_unk = format_matchup_players_html("unknown-999", 1, "Akuma", "Hugo", data_dir=tmp_path)
    assert html_unk == "Akuma vs Hugo"
