"""Layer 1 deterministic validation tests for Milestone 2 feature extraction."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from fightcade_analysis.features import (
    BIT_LEFT,
    BIT_LP,
    BIT_RIGHT,
    CORE_FEATURE_COLUMNS,
    STAGE_MAX_X,
    STAGE_MIN_X,
    extract_player_features,
)

FT5_PARQUET = Path(__file__).resolve().parents[1] / "data" / "parquet" / "1728857242828-5771.parquet"


def _swap_left_right_mask(mask: np.ndarray) -> np.ndarray:
    left = (mask & BIT_LEFT) != 0
    right = (mask & BIT_RIGHT) != 0
    cleared = mask & np.uint16(0xFFFF ^ (BIT_LEFT | BIT_RIGHT))
    return cleared | np.where(left, BIT_RIGHT, 0) | np.where(right, BIT_LEFT, 0)


def _mirror_replay_df(df: pd.DataFrame) -> pd.DataFrame:
    """Horizontally mirror the stage and swap P1 <-> P2 roles."""
    mirrored = df.copy()
    x_sum = int(STAGE_MIN_X + STAGE_MAX_X)  # 1028

    p1_cols = [c for c in df.columns if c.startswith("p1_")]
    for p1c in p1_cols:
        p2c = "p2_" + p1c[3:]
        mirrored[p1c] = df[p2c]
        mirrored[p2c] = df[p1c]

    mirrored["p1_x"] = (x_sum - mirrored["p1_x"]).astype(df["p1_x"].dtype)
    mirrored["p2_x"] = (x_sum - mirrored["p2_x"]).astype(df["p2_x"].dtype)
    mirrored["screen_x"] = (x_sum - mirrored["screen_x"]).astype(df["screen_x"].dtype)

    mirrored["p1_facing"] = (1 - mirrored["p1_facing"]).astype(df["p1_facing"].dtype)
    mirrored["p2_facing"] = (1 - mirrored["p2_facing"]).astype(df["p2_facing"].dtype)

    mirrored["p1_input_mask"] = _swap_left_right_mask(mirrored["p1_input_mask"].to_numpy()).astype(np.uint16)
    mirrored["p2_input_mask"] = _swap_left_right_mask(mirrored["p2_input_mask"].to_numpy()).astype(np.uint16)
    return mirrored


def _make_synthetic_round() -> pd.DataFrame:
    """Small synthetic 10-frame round with walking, crouching, corner pressure, and inputs."""
    n = 10
    return pd.DataFrame(
        {
            "quark_id": ["syn-1"] * n,
            "emu_frame": np.arange(100, 100 + n, dtype=np.int32),
            "game_frame": np.arange(1000, 1000 + n, dtype=np.uint32),
            "round_id": np.ones(n, dtype=np.uint8),
            "in_match": np.ones(n, dtype=np.uint8),
            "match_state": np.full(n, 2, dtype=np.uint8),
            "timer": np.full(n, 99, dtype=np.uint8),
            "screen_x": np.full(n, 300, dtype=np.int16),
            "p1_char": np.full(n, 7, dtype=np.uint16),
            "p1_sa": np.full(n, 1, dtype=np.uint8),
            "p1_x": np.array([140, 137, 134, 130, 128, 125, 125, 130, 136, 142], dtype=np.int16),
            "p1_y": np.array([0, 0, 0, 10, 22, 30, 22, 10, 0, 0], dtype=np.int16),
            "p1_facing": np.ones(n, dtype=np.int8),
            "p1_hp": np.array([160, 160, 160, 150, 150, 150, 120, 120, 120, 120], dtype=np.uint8),
            "p1_meter_gauge": np.full(n, 44, dtype=np.uint8),
            "p1_meter_max_gauge": np.full(n, 88, dtype=np.uint8),
            "p1_meter_stock": np.ones(n, dtype=np.uint8),
            "p1_meter_max_stock": np.full(n, 3, dtype=np.uint8),
            "p1_stun": np.full(n, 16, dtype=np.uint8),
            "p1_stun_max": np.full(n, 64, dtype=np.uint8),
            "p1_action": np.zeros(n, dtype=np.uint32),
            "p1_anim": np.full(n, 100, dtype=np.uint16),
            "p1_posture": np.array([0, 8, 8, 22, 22, 22, 38, 38, 32, 0], dtype=np.uint8),
            "p1_attacking": np.array([0, 0, 0, 0, 1, 1, 0, 0, 0, 0], dtype=np.uint8),
            "p1_blocking": np.zeros(n, dtype=np.uint8),
            "p1_combo": np.zeros(n, dtype=np.uint8),
            "p1_freeze": np.zeros(n, dtype=np.uint8),
            "p1_input_mask": np.array([BIT_LEFT, BIT_LEFT, BIT_RIGHT | BIT_LP, 0, 0, 0, 0, 0, BIT_RIGHT, BIT_RIGHT], dtype=np.uint16),
            "p2_char": np.full(n, 13, dtype=np.uint16),
            "p2_sa": np.full(n, 3, dtype=np.uint8),
            "p2_x": np.array([260, 255, 250, 245, 240, 235, 235, 240, 248, 256], dtype=np.int16),
            "p2_y": np.zeros(n, dtype=np.int16),
            "p2_facing": np.zeros(n, dtype=np.int8),
            "p2_hp": np.full(n, 160, dtype=np.uint8),
            "p2_meter_gauge": np.full(n, 22, dtype=np.uint8),
            "p2_meter_max_gauge": np.full(n, 88, dtype=np.uint8),
            "p2_meter_stock": np.zeros(n, dtype=np.uint8),
            "p2_meter_max_stock": np.full(n, 2, dtype=np.uint8),
            "p2_stun": np.full(n, 8, dtype=np.uint8),
            "p2_stun_max": np.full(n, 64, dtype=np.uint8),
            "p2_action": np.zeros(n, dtype=np.uint32),
            "p2_anim": np.full(n, 200, dtype=np.uint16),
            "p2_posture": np.full(n, 6, dtype=np.uint8),
            "p2_attacking": np.zeros(n, dtype=np.uint8),
            "p2_blocking": np.array([0, 0, 0, 0, 1, 1, 0, 0, 0, 0], dtype=np.uint8),
            "p2_combo": np.array([0, 0, 0, 0, 0, 0, 2, 2, 0, 0], dtype=np.uint8),
            "p2_freeze": np.zeros(n, dtype=np.uint8),
            "p2_input_mask": np.full(n, BIT_LEFT, dtype=np.uint16),
        }
    )


def test_exact_left_right_mirror_invariance_synthetic() -> None:
    df = _make_synthetic_round()
    mirrored = _mirror_replay_df(df)

    f_p1 = extract_player_features(df, perspective=1)
    f_p2_mirrored = extract_player_features(mirrored, perspective=2)

    for col in CORE_FEATURE_COLUMNS:
        np.testing.assert_allclose(
            f_p1[col].to_numpy(),
            f_p2_mirrored[col].to_numpy(),
            rtol=1e-6,
            atol=1e-6,
            err_msg=f"Mirror invariance violated on column '{col}'",
        )

    for in_col in ("in_fwd", "in_back", "in_up", "in_down", "in_btn_mask"):
        np.testing.assert_array_equal(
            f_p1[in_col].to_numpy(),
            f_p2_mirrored[in_col].to_numpy(),
            err_msg=f"Canonical input mirror invariance violated on '{in_col}'",
        )


def test_velocity_resets_cleanly_at_round_boundary() -> None:
    r1 = _make_synthetic_round()
    r2 = _make_synthetic_round()
    r2["round_id"] = np.full(len(r2), 2, dtype=np.uint8)
    r2["p1_x"] = np.full(len(r2), 500, dtype=np.int16)  # huge jump from r1's last x=142
    both = pd.concat([r1, r2], ignore_index=True)

    feats = extract_player_features(both, perspective=1)
    r2_first_idx = len(r1)
    assert feats.loc[r2_first_idx, "self_vx_norm"] == 0.0
    assert feats.loc[r2_first_idx, "self_vy_norm"] == 0.0


@pytest.mark.skipif(not FT5_PARQUET.exists(), reason="Verified FT5 Parquet not present locally")
def test_real_ft5_parquet_bounds_mirror_and_known_moments() -> None:
    df = pd.read_parquet(FT5_PARQUET)
    f1 = extract_player_features(df, perspective=1)
    f2 = extract_player_features(df, perspective=2)

    # 1. Zero NaNs / Infs and strict normalized bounds
    mat1 = f1[list(CORE_FEATURE_COLUMNS)].to_numpy()
    mat2 = f2[list(CORE_FEATURE_COLUMNS)].to_numpy()
    assert np.isfinite(mat1).all()
    assert np.isfinite(mat2).all()

    for col in ("self_y_norm", "opp_y_norm", "self_corner_prox", "opp_corner_prox",
                "self_hp_norm", "opp_hp_norm", "self_meter_norm", "opp_meter_norm",
                "self_stun_norm", "opp_stun_norm"):
        assert f1[col].between(0.0, 1.0).all(), f"{col} out of [0, 1]"
        assert f2[col].between(0.0, 1.0).all(), f"{col} out of [0, 1]"

    # 2. Exact mirror invariance on real 30k-row FT5
    mirrored = _mirror_replay_df(df)
    f2_mirrored = extract_player_features(mirrored, perspective=2)
    for col in CORE_FEATURE_COLUMNS:
        np.testing.assert_allclose(f1[col].to_numpy(), f2_mirrored[col].to_numpy(), rtol=1e-5, atol=1e-5)

    # 3. Known moments in 1728857242828-5771
    # Round 1 start: Ibuki (x=424) vs Urien (x=600) -> dx = +176 px, mid-screen (corner_prox = 0)
    assert f1.loc[0, "dx_px"] == pytest.approx(176.0)
    assert f1.loc[0, "self_corner_prox"] == pytest.approx(0.0)
    assert f1.loc[0, "opp_corner_prox"] == pytest.approx(0.0)

    # Knockdowns occur across the 13 rounds
    assert f1["self_knockdown"].sum() > 100
    assert f1["opp_knockdown"].sum() > 100

    # Round 10 Double KO: both players reach hp_norm == 0
    r10 = f1[f1["round_id"] == 10]
    assert r10["self_hp_norm"].min() == 0.0
    assert r10["opp_hp_norm"].min() == 0.0
