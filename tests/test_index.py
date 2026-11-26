"""Layer 3 validation tests for hierarchical FAISS indexing and LORO retrieval coherence."""

from __future__ import annotations

from pathlib import Path

import pytest

from fightcade_analysis.index import (
    _classify_regimes_vectorized,
    build_corpus_from_parquets,
    classify_tactical_regime,
    evaluate_loro_coherence,
    load_or_build_corpus,
)
from fightcade_analysis.segmentation import SegmentationConfig

PARQUET_DIR = Path(__file__).resolve().parents[1] / "data" / "parquet"


def test_corpus_cache_roundtrip_and_vectorized_regime_parity(tmp_path: Path) -> None:
    files = sorted(PARQUET_DIR.glob("*.parquet"))[:3]
    if len(files) < 2:
        pytest.skip("Need at least 2 Parquet replays for corpus cache test")

    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    c1, hit1 = load_or_build_corpus(files, config=cfg, both_perspectives=True, cache_dir=tmp_path)
    assert hit1 is False
    assert len(c1.meta) > 100

    c2, hit2 = load_or_build_corpus(files, config=cfg, both_perspectives=True, cache_dir=tmp_path)
    assert hit2 is True
    assert len(c2.meta) == len(c1.meta)
    assert c2.vectors.shape == c1.vectors.shape

    # Vectorized regime classifier must match row-wise classify_tactical_regime 100%
    vec_regimes = _classify_regimes_vectorized(c1.meta)
    row_regimes = [classify_tactical_regime(c1.meta.iloc[i]) for i in range(len(c1.meta))]
    assert list(vec_regimes) == row_regimes


def test_layer3_loro_retrieval_coherence_across_corpus() -> None:
    files = sorted(PARQUET_DIR.glob("*.parquet"))[:10]
    if len(files) < 2:
        pytest.skip("Need at least 2 Parquet replays for Leave-One-Replay-Out evaluation")

    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    corpus = build_corpus_from_parquets(files, config=cfg, both_perspectives=True)
    assert len(corpus.meta) > 200

    metrics = evaluate_loro_coherence(corpus, k=5, max_queries_per_replay=50, tier_override="universal")
    assert metrics["queries_evaluated"] >= 50
    # Top-5 neighbors should match physical spacing within reasonable fighting-game tolerance
    assert metrics["median_dx_err_px"] < 55.0, f"High spacing error: {metrics}"
    # Corner proximity error should be tight (< 0.20 on [0, 1] scale)
    assert metrics["mean_corner_err"] < 0.20, f"High corner error: {metrics}"
    # Knockdown and airborne state agreement should be high (> 85%)
    assert metrics["self_kd_agreement"] >= 0.85, f"Low KD agreement: {metrics}"
    assert metrics["self_air_agreement"] >= 0.85, f"Low airborne agreement: {metrics}"
