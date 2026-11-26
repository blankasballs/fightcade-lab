"""Tests for manual interaction selection, bookmark loading, full-sequence semantic search, and HTML output."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from fightcade_analysis.index import HierarchicalContextIndex, build_corpus_from_parquets
from fightcade_analysis.segmentation import SegmentationConfig
from fightcade_analysis.sequence_search import (
    build_sequence_search_html,
    load_bookmarks,
    resolve_bookmark,
    search_similar_sequences,
)

PARQUET_DIR = Path(__file__).resolve().parents[1] / "data" / "parquet"


def test_bookmark_jsonl_loading_and_resolution(tmp_path: Path) -> None:
    bm_path = tmp_path / "bookmarks.jsonl"
    entries = [
        {"id": "qid1_p1_gf100_220", "quark_id": "qid1", "perspective": 1, "start_gf": 100, "end_gf": 220, "duration_s": 2.0},
        {"id": "qid2_p2_gf500_680", "quark_id": "qid2", "perspective": 2, "start_gf": 500, "end_gf": 680, "duration_s": 3.0},
    ]
    bm_path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")

    loaded = load_bookmarks(bm_path)
    assert len(loaded) == 2
    assert resolve_bookmark(latest=True, bookmarks_path=bm_path)["id"] == "qid2_p2_gf500_680"
    assert resolve_bookmark(selection_id="qid1_p1_gf100_220", bookmarks_path=bm_path)["start_gf"] == 100


def test_sequence_search_self_match_and_cross_replay_retrieval(tmp_path: Path) -> None:
    files = sorted(PARQUET_DIR.glob("*.parquet"))[:6]
    if len(files) < 3:
        pytest.skip("Need at least 3 Parquet replays for sequence search test")

    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    corpus = build_corpus_from_parquets(files, config=cfg, both_perspectives=True)
    index = HierarchicalContextIndex(corpus.meta, corpus.vectors)

    # Pick a clean 120-frame (2.0s) window from the first replay's first active round
    target_pq = files[0]
    df = pd.read_parquet(target_pq)
    r1 = df[(df["round_id"] == 1) & (df["in_match"] == 1)].reset_index(drop=True)
    assert len(r1) > 250
    start_gf = int(r1.iloc[100]["game_frame"])
    end_gf = int(r1.iloc[220]["game_frame"])

    # 1. With exclude_self=False, the exact same sequence must appear as ~100% match!
    self_res = search_similar_sequences(
        target_pq,
        perspective=1,
        start_gf=start_gf,
        end_gf=end_gf,
        parquet_dir=PARQUET_DIR,
        corpus_index=index,
        config=cfg,
        top_k=5,
        tier_override="universal",
        max_norm_dist=0.45,
        exclude_self=False,
    )
    assert len(self_res["matches"]) >= 1
    top_self = self_res["matches"][0]
    assert top_self["quark_id"] == target_pq.stem
    assert top_self["similarity_pct"] >= 95
    assert top_self["norm_dist"] <= 0.03

    # 2. With exclude_self=True, all matches must come from OTHER replays and stay sorted by distance
    cross_res = search_similar_sequences(
        target_pq,
        perspective=1,
        start_gf=start_gf,
        end_gf=end_gf,
        parquet_dir=PARQUET_DIR,
        corpus_index=index,
        config=cfg,
        top_k=5,
        tier_override="universal",
        max_norm_dist=0.45,
        exclude_self=True,
        note="Unit test neutral sequence",
    )
    assert len(cross_res["matches"]) >= 1
    dists = [m["norm_dist"] for m in cross_res["matches"]]
    assert dists == sorted(dists)
    for m in cross_res["matches"]:
        assert m["quark_id"] != target_pq.stem

    # 3. Verify HTML report renders cleanly, paused by default, with escaped data-copy buttons
    out_html = tmp_path / "search_test.html"
    build_sequence_search_html(cross_res, out_html, clips_dir=tmp_path, savestates_dir=tmp_path)
    html_text = out_html.read_text(encoding="utf-8")
    assert "Most Similar Corpus Interactions" in html_text
    assert "Unit test neutral sequence" in html_text
    assert 'href="/#insights"' in html_text
    assert 'onclick="copyFromData(this)"' in html_text
    assert "autoplay" not in html_text
