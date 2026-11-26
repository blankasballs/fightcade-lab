"""Layer 2 validation tests for PELT breakpoint detection, 3-window slicing, and leakage checks."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fightcade_analysis.features import CORE_FEATURE_COLUMNS, extract_player_features
from fightcade_analysis.segmentation import (
    CONTEXT_FRAMES,
    CONTEXT_STEP,
    SegmentationConfig,
    evaluate_segmentation,
    extract_decision_points,
)

FT5_PARQUET = Path(__file__).resolve().parents[1] / "data" / "parquet" / "1728857242828-5771.parquet"


@pytest.mark.skipif(not FT5_PARQUET.exists(), reason="Verified FT5 Parquet not present locally")
def test_layer2_segmentation_density_vectors_and_recall() -> None:
    df = pd.read_parquet(FT5_PARQUET)
    feats = extract_player_features(df, perspective=1)
    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)

    meta_df, ctx_vecs = extract_decision_points(feats, config=cfg)
    metrics = evaluate_segmentation(feats, config=cfg)

    expected_dim = (CONTEXT_FRAMES // CONTEXT_STEP) * len(CORE_FEATURE_COLUMNS)
    assert len(meta_df) == ctx_vecs.shape[0]
    assert ctx_vecs.shape[1] == expected_dim
    assert np.isfinite(ctx_vecs).all()

    # Density sanity: ~8 to 40 breakpoints per round on a 30-50s 3rd Strike round
    assert 6.0 <= metrics["mean_bkps_per_round"] <= 45.0, f"Unexpected density: {metrics}"

    # Recall sanity on held-out RAM oracle events
    assert metrics["oracle_recall"]["damage"] >= 0.75, f"Low damage recall: {metrics}"
    assert metrics["oracle_recall"]["knockdown"] >= 0.70, f"Low knockdown recall: {metrics}"

    # Outcome & Action columns sanity
    assert meta_df["outcome_value"].notna().all()
    assert meta_df["net_hp_swing"].between(-1.0, 1.0).all()
    assert meta_df["action_coarse"].nunique() >= 3

    # Verify lead_frames keeps context HP-drop leakage low (< 25%)
    assert metrics["context_hp_leak_rate"] < 0.25, f"High context leakage: {metrics}"
