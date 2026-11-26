"""Stage 3 & 4 tests: structured role labeling, regime-gated Outcome Regret diagnosis, HTML UI, and Gemini summary wiring."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from fightcade_analysis.coaching import (
    _select_closest_proofs,
    describe_decision_point,
    diagnose_replay,
)
from fightcade_analysis.gemini_summary import load_gemini_api_key
from fightcade_analysis.index import (
    HierarchicalContextIndex,
    build_corpus_from_parquets,
    classify_tactical_regime,
)
from fightcade_analysis.report_ui import build_report_html
from fightcade_analysis.segmentation import SegmentationConfig

PARQUET_DIR = Path(__file__).resolve().parents[1] / "data" / "parquet"


def test_structured_role_labels_distinguish_air_super_from_anti_air() -> None:
    air_super = describe_decision_point(
        {
            "self_char": 13,  # Urien
            "opp_char": 7,    # Ibuki
            "self_sa": 3,
            "opp_sa": 1,
            "t0_self_corner": 0.95,
            "t0_opp_air": 1,
            "t0_opp_sf": 1,
        }
    )
    assert "Urien (Defender, Cornered)" in air_super["label"]
    assert "Ibuki (Attacker, Air SA1)" in air_super["label"]
    assert "Super Art Defense" in air_super["label"]

    anti_air = describe_decision_point(
        {
            "self_char": 14,  # Akuma
            "opp_char": 11,   # Ken
            "self_sa": 2,
            "opp_sa": 3,
            "t0_self_corner": 0.1,
            "t0_opp_air": 1,
            "t0_opp_sf": 0,
        }
    )
    assert anti_air["label"] == "Akuma (Defender) vs Ken (Attacker, Jump-in) — Anti-Air Decision"


def test_tactical_regime_separates_defense_from_jumpin_and_oki() -> None:
    blk_def = {"t0_self_blk": 1, "t0_opp_atk": 1}
    jump_in = {"t0_self_air": 1, "t0_opp_air": 0}
    oki_off = {"t0_opp_kd": 1}
    wakeup_def = {"t0_self_kd": 1}
    assert classify_tactical_regime(blk_def) == "defense_pressure"
    assert classify_tactical_regime(jump_in) == "jumpin_off"
    assert classify_tactical_regime(oki_off) == "oki_off"
    assert classify_tactical_regime(wakeup_def) == "wakeup_def"


def test_proof_picker_prefers_closest_match_over_distant_lucky_outlier() -> None:
    grp = pd.DataFrame(
        [
            {"quark_id": "far_lucky", "norm_dist": 0.31, "outcome_value": 0.65},
            {"quark_id": "close_good", "norm_dist": 0.11, "outcome_value": 0.35},
            {"quark_id": "mid_good", "norm_dist": 0.21, "outcome_value": 0.50},
        ]
    )
    picked = _select_closest_proofs(grp, actual_val=-0.20, proofs_per_option=2)
    assert list(picked["quark_id"]) == ["close_good", "mid_good"]


def test_load_gemini_api_key_from_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nGEMINI_API_KEY="test-key-123"\n', encoding="utf-8")
    assert load_gemini_api_key(env_file) == "test-key-123"


def test_stage3_regret_diagnosis_and_stage4_html_report(tmp_path: Path) -> None:
    files = sorted(PARQUET_DIR.glob("*.parquet"))
    if len(files) < 3:
        pytest.skip("Need at least 3 Parquet replays for Stage 3 diagnosis test")

    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    # Use a fast subset of up to 8 replays for deterministic test runtime
    sample_files = files[:8]
    corpus = build_corpus_from_parquets(sample_files, config=cfg, both_perspectives=True)
    index = HierarchicalContextIndex(corpus.meta, corpus.vectors)

    report = diagnose_replay(sample_files[0], index, perspective=1, config=cfg, k_neighbors=20, top_n=5)
    assert report["total_decision_points"] > 10
    assert len(report["moments"]) >= 1

    # Ensure no self-leakage and every proof matches the query's tactical regime
    target_qid = report["target_quark_id"]
    for m in report["moments"]:
        assert m["regret"] > 0
        assert "vs" in m["label"] and "—" in m["label"]
        assert "match_similarity_pct" in m
        for opt in m["options"]:
            for pr in opt["proofs"]:
                assert pr["quark_id"] != target_qid
                assert pr["regime"] == m["regime"]
                assert pr["norm_dist"] <= 0.34

    # Attach mock AI summary + fake mp4 to verify HTML rendering and paused-by-default videos
    first = report["moments"][0]
    first["ai_summary"] = {
        "model": "gemini-3-flash-preview",
        "situation_summary": "Akuma jumped late and got anti-aired.",
        "alternative_takeaway": "Pros stayed grounded and whiff-punished.",
        "clip_captions": {"user": "User clip caption", "alt_1": "Alt 1 clip caption"},
    }
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    (clips_dir / f"{first['quark_id']}_gf{int(first['game_frame'])}.mp4").write_bytes(b"fake")

    out_html = tmp_path / "test_report.html"
    build_report_html(report, out_html, clips_dir=clips_dir, savestates_dir=tmp_path / "savestates")
    html_text = out_html.read_text(encoding="utf-8")
    assert "Export Annotations JSON" in html_text
    assert "Launch Takeover" in html_text
    assert "Copy Takeover command" in html_text
    assert 'href="/#insights"' in html_text
    assert 'onclick="copyFromData(this)"' in html_text
    assert 'data-copy="C:\\Users\\621\\source\\repos\\fightcade-analysis\\scripts\\takeover.ps1' in html_text
    assert 'onclick="copyText(' not in html_text
    assert "Pause All Videos" in html_text
    assert "Play Card Clips" in html_text
    assert "AI Interaction Breakdown" in html_text
    assert "Akuma jumped late and got anti-aired." in html_text
    assert "autoplay" not in html_text
    assert report["moments"][0]["label"] in html_text
