"""Perspective- and translation-invariant feature extraction for sfiii3nr1 telemetry.

Converts raw 60 Hz RAM Parquet DataFrames into canonical per-player state features
where left/right side orientation is fully normalized:
  - Positive `dx` always means opponent is in front of `self`.
  - Positive `self_vx` always means `self` is moving forward (toward facing direction).
  - `self_corner_prox` in [0, 1] is 1.0 when pinned against `self`'s back corner and 0.0 when >= 250px away.
  - `opp_corner_prox` in [0, 1] is 1.0 when opponent is pinned against their back corner.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Verified sfiii3nr1 world coordinate bounds (from 13-round FT5 telemetry)
STAGE_MIN_X: float = 100.0
STAGE_MAX_X: float = 928.0
STAGE_CENTER_X: float = (STAGE_MIN_X + STAGE_MAX_X) / 2.0  # 514.0
MAX_SCREEN_DX: float = 336.0  # 2 * 168 px max horizontal separation
CORNER_ZONE_PX: float = 250.0
MAX_Y_NORM: float = 160.0
MAX_VEL_NORM: float = 12.0  # px/frame normalization scale
VEL_WINDOW_FRAMES: int = 4  # short finite difference window within same round

# sfiii3nr1 posture constants (from 3rd_training_lua gamestate.lua)
POSTURE_CROUCH: int = 0x20  # 32
POSTURE_KNOCKDOWN: int = 0x26  # 38

# Input bitmask bits (from lua/sfiii3nr1_map.lua)
BIT_UP: int = 0x001
BIT_DOWN: int = 0x002
BIT_LEFT: int = 0x004
BIT_RIGHT: int = 0x008
BIT_LP: int = 0x010
BIT_MP: int = 0x020
BIT_HP: int = 0x040
BIT_LK: int = 0x080
BIT_MK: int = 0x100
BIT_HK: int = 0x200

# Canonical continuous + binary feature columns used by Milestone 2 segmentation & FAISS
CORE_FEATURE_COLUMNS: tuple[str, ...] = (
    "dx_norm",
    "dy_norm",
    "self_y_norm",
    "opp_y_norm",
    "self_corner_prox",
    "opp_corner_prox",
    "self_vx_norm",
    "self_vy_norm",
    "opp_vx_norm",
    "opp_vy_norm",
    "self_hp_norm",
    "opp_hp_norm",
    "self_meter_norm",
    "opp_meter_norm",
    "self_stun_norm",
    "opp_stun_norm",
    "self_crouching",
    "opp_crouching",
    "self_knockdown",
    "opp_knockdown",
    "self_airborne",
    "opp_airborne",
    "self_attacking",
    "opp_attacking",
    "self_blocking",
    "opp_blocking",
    "self_in_combo",
    "opp_in_combo",
    "in_freeze",
    "self_superfreeze",
    "opp_superfreeze",
    "self_has_proj",
    "opp_has_proj",
)


def _round_velocity(series: pd.Series, round_ids: pd.Series, window: int = VEL_WINDOW_FRAMES) -> np.ndarray:
    """Compute per-round finite-difference velocity (px/frame) with zero leakage across rounds."""
    vel = np.zeros(len(series), dtype=np.float32)
    vals = series.to_numpy(dtype=np.float32)
    rids = round_ids.to_numpy()

    for rid in np.unique(rids):
        idx = np.flatnonzero(rids == rid)
        if len(idx) <= 1:
            continue
        rvals = vals[idx]
        rvel = np.zeros(len(idx), dtype=np.float32)
        for k in range(1, min(window, len(idx))):
            rvel[k] = (rvals[k] - rvals[0]) / float(k)
        if len(idx) > window:
            rvel[window:] = (rvals[window:] - rvals[:-window]) / float(window)
        vel[idx] = rvel
    return vel


def _safe_ratio(num: np.ndarray, den: np.ndarray, fallback_den: float) -> np.ndarray:
    denom = np.where(den > 0, den, fallback_den).astype(np.float32)
    return np.clip(num.astype(np.float32) / denom, 0.0, 1.0)


def _normalized_meter(gauge: np.ndarray, max_gauge: np.ndarray, stock: np.ndarray, max_stock: np.ndarray) -> np.ndarray:
    """Total meter progress in [0, 1] across stocks + partial bar."""
    g_ratio = _safe_ratio(gauge, max_gauge, fallback_den=88.0)
    m_stock = np.where(max_stock > 0, max_stock, 1.0).astype(np.float32)
    total = (stock.astype(np.float32) + g_ratio) / m_stock
    return np.clip(total, 0.0, 1.0)


def extract_player_features(df: pd.DataFrame, perspective: int = 1) -> pd.DataFrame:
    """Extract canonical side-invariant features from either P1 (1) or P2 (2) perspective.

    Args:
        df: Telemetry DataFrame loaded from a replay Parquet file.
        perspective: 1 for P1 as `self` (P2 as `opp`), or 2 for P2 as `self` (P1 as `opp`).

    Returns:
        DataFrame aligned row-for-row with `df` containing metadata keys, canonical features,
        and canonicalized input flags (`in_fwd`, `in_back`, `in_up`, `in_down`, `in_btn_mask`).
    """
    if perspective not in (1, 2):
        raise ValueError(f"perspective must be 1 or 2, got {perspective}")

    sp = "p1" if perspective == 1 else "p2"
    op = "p2" if perspective == 1 else "p1"

    round_id = df["round_id"]

    self_x = df[f"{sp}_x"].to_numpy(dtype=np.float32)
    self_y = df[f"{sp}_y"].to_numpy(dtype=np.float32)
    opp_x = df[f"{op}_x"].to_numpy(dtype=np.float32)
    opp_y = df[f"{op}_y"].to_numpy(dtype=np.float32)

    # In sfiii3nr1, facing == 1 means facing right (+1), facing == 0 means facing left (-1)
    self_facing = df[f"{sp}_facing"].to_numpy(dtype=np.int8)
    sign = np.where(self_facing == 1, 1.0, -1.0).astype(np.float32)

    # Relative geometry (side-invariant)
    dx_signed = (opp_x - self_x) * sign
    dy = opp_y - self_y

    dx_norm = np.clip(dx_signed / MAX_SCREEN_DX, -1.0, 1.0)
    dy_norm = np.clip(dy / MAX_Y_NORM, -1.0, 1.0)
    self_y_norm = np.clip(self_y / MAX_Y_NORM, 0.0, 1.0)
    opp_y_norm = np.clip(opp_y / MAX_Y_NORM, 0.0, 1.0)

    # Distance to each player's back wall (canonicalized by `self`'s facing orientation)
    self_back_wall_dist = np.where(sign > 0, self_x - STAGE_MIN_X, STAGE_MAX_X - self_x)
    opp_back_wall_dist = np.where(sign > 0, STAGE_MAX_X - opp_x, opp_x - STAGE_MIN_X)

    self_corner_prox = np.clip(1.0 - (np.clip(self_back_wall_dist, 0.0, CORNER_ZONE_PX) / CORNER_ZONE_PX), 0.0, 1.0)
    opp_corner_prox = np.clip(1.0 - (np.clip(opp_back_wall_dist, 0.0, CORNER_ZONE_PX) / CORNER_ZONE_PX), 0.0, 1.0)

    # Hitstop-safe per-round velocities
    raw_self_vx = _round_velocity(df[f"{sp}_x"], round_id)
    raw_self_vy = _round_velocity(df[f"{sp}_y"], round_id)
    raw_opp_vx = _round_velocity(df[f"{op}_x"], round_id)
    raw_opp_vy = _round_velocity(df[f"{op}_y"], round_id)

    self_vx_norm = np.clip((raw_self_vx * sign) / MAX_VEL_NORM, -1.0, 1.0)
    self_vy_norm = np.clip(raw_self_vy / MAX_VEL_NORM, -1.0, 1.0)
    opp_vx_norm = np.clip((raw_opp_vx * sign) / MAX_VEL_NORM, -1.0, 1.0)
    opp_vy_norm = np.clip(raw_opp_vy / MAX_VEL_NORM, -1.0, 1.0)

    # Normalized resources: infer per-round max HP so mid-set character switches (e.g. Q -> Akuma) stay exact
    self_hp = df[f"{sp}_hp"].to_numpy(dtype=np.float32)
    opp_hp = df[f"{op}_hp"].to_numpy(dtype=np.float32)
    self_max_hp_arr = df.groupby("round_id")[f"{sp}_hp"].transform("max").to_numpy(dtype=np.float32)
    opp_max_hp_arr = df.groupby("round_id")[f"{op}_hp"].transform("max").to_numpy(dtype=np.float32)
    self_max_hp_arr = np.maximum(self_max_hp_arr, 130.0)
    opp_max_hp_arr = np.maximum(opp_max_hp_arr, 130.0)

    self_hp_norm = np.clip(self_hp / self_max_hp_arr, 0.0, 1.0)
    opp_hp_norm = np.clip(opp_hp / opp_max_hp_arr, 0.0, 1.0)

    self_meter_norm = _normalized_meter(
        df[f"{sp}_meter_gauge"].to_numpy(),
        df[f"{sp}_meter_max_gauge"].to_numpy(),
        df[f"{sp}_meter_stock"].to_numpy(),
        df[f"{sp}_meter_max_stock"].to_numpy(),
    )
    opp_meter_norm = _normalized_meter(
        df[f"{op}_meter_gauge"].to_numpy(),
        df[f"{op}_meter_max_gauge"].to_numpy(),
        df[f"{op}_meter_stock"].to_numpy(),
        df[f"{op}_meter_max_stock"].to_numpy(),
    )

    self_stun_norm = _safe_ratio(df[f"{sp}_stun"].to_numpy(), df[f"{sp}_stun_max"].to_numpy(), fallback_den=64.0)
    opp_stun_norm = _safe_ratio(df[f"{op}_stun"].to_numpy(), df[f"{op}_stun_max"].to_numpy(), fallback_den=64.0)

    # Binary tactical state flags
    self_posture = df[f"{sp}_posture"].to_numpy()
    opp_posture = df[f"{op}_posture"].to_numpy()

    self_crouching = (self_posture == POSTURE_CROUCH).astype(np.float32)
    opp_crouching = (opp_posture == POSTURE_CROUCH).astype(np.float32)
    self_knockdown = (self_posture == POSTURE_KNOCKDOWN).astype(np.float32)
    opp_knockdown = (opp_posture == POSTURE_KNOCKDOWN).astype(np.float32)
    self_airborne = (self_y > 0).astype(np.float32)
    opp_airborne = (opp_y > 0).astype(np.float32)

    self_attacking = (df[f"{sp}_attacking"].to_numpy() > 0).astype(np.float32)
    opp_attacking = (df[f"{op}_attacking"].to_numpy() > 0).astype(np.float32)
    self_blocking = (df[f"{sp}_blocking"].to_numpy() > 0).astype(np.float32)
    opp_blocking = (df[f"{op}_blocking"].to_numpy() > 0).astype(np.float32)

    # In gamestate.lua, p1_combo tracks hits landed BY P1 (meaning P2 is in combo/hitstun)
    self_in_combo = (df[f"{op}_combo"].to_numpy() > 0).astype(np.float32)
    opp_in_combo = (df[f"{sp}_combo"].to_numpy() > 0).astype(np.float32)

    in_freeze = ((df[f"{sp}_freeze"].to_numpy() > 0) | (df[f"{op}_freeze"].to_numpy() > 0)).astype(np.float32)

    # Superfreeze & active projectile presence (backward-compatible with older 48-col Parquets)
    n_rows = len(df)
    self_sf_col = f"{sp}_superfreeze"
    opp_sf_col = f"{op}_superfreeze"
    self_proj_col = f"{sp}_proj_count"
    opp_proj_col = f"{op}_proj_count"

    self_superfreeze = (df[self_sf_col].to_numpy() > 0).astype(np.float32) if self_sf_col in df.columns else np.zeros(n_rows, dtype=np.float32)
    opp_superfreeze = (df[opp_sf_col].to_numpy() > 0).astype(np.float32) if opp_sf_col in df.columns else np.zeros(n_rows, dtype=np.float32)
    self_has_proj = (df[self_proj_col].to_numpy() > 0).astype(np.float32) if self_proj_col in df.columns else np.zeros(n_rows, dtype=np.float32)
    opp_has_proj = (df[opp_proj_col].to_numpy() > 0).astype(np.float32) if opp_proj_col in df.columns else np.zeros(n_rows, dtype=np.float32)

    # Side-canonicalized inputs for Action Window summarization
    in_mask = df[f"{sp}_input_mask"].to_numpy(dtype=np.uint16)
    left_held = (in_mask & BIT_LEFT) != 0
    right_held = (in_mask & BIT_RIGHT) != 0
    in_fwd = np.where(sign > 0, right_held, left_held).astype(np.uint8)
    in_back = np.where(sign > 0, left_held, right_held).astype(np.uint8)
    in_up = ((in_mask & BIT_UP) != 0).astype(np.uint8)
    in_down = ((in_mask & BIT_DOWN) != 0).astype(np.uint8)
    in_btn_mask = ((in_mask >> 4) & 0x3F).astype(np.uint8)

    out = pd.DataFrame(
        {
            "quark_id": df["quark_id"] if "quark_id" in df.columns else "",
            "perspective": np.uint8(perspective),
            "emu_frame": df["emu_frame"].to_numpy(dtype=np.int32),
            "game_frame": df["game_frame"].to_numpy(dtype=np.uint32),
            "round_id": round_id.to_numpy(dtype=np.uint8),
            "in_match": df["in_match"].to_numpy(dtype=np.uint8),
            "self_char": df[f"{sp}_char"].to_numpy(dtype=np.uint16),
            "opp_char": df[f"{op}_char"].to_numpy(dtype=np.uint16),
            "self_sa": df[f"{sp}_sa"].to_numpy(dtype=np.uint8),
            "opp_sa": df[f"{op}_sa"].to_numpy(dtype=np.uint8),
            "self_anim": df[f"{sp}_anim"].to_numpy(dtype=np.uint16),
            "opp_anim": df[f"{op}_anim"].to_numpy(dtype=np.uint16),
            "self_action": df[f"{sp}_action"].to_numpy(dtype=np.uint32),
            "opp_action": df[f"{op}_action"].to_numpy(dtype=np.uint32),
            "dx_px": dx_signed.astype(np.float32),
            "dx_norm": dx_norm,
            "dy_norm": dy_norm,
            "self_y_norm": self_y_norm,
            "opp_y_norm": opp_y_norm,
            "self_corner_prox": self_corner_prox,
            "opp_corner_prox": opp_corner_prox,
            "self_vx_norm": self_vx_norm,
            "self_vy_norm": self_vy_norm,
            "opp_vx_norm": opp_vx_norm,
            "opp_vy_norm": opp_vy_norm,
            "self_hp_norm": self_hp_norm,
            "opp_hp_norm": opp_hp_norm,
            "self_meter_norm": self_meter_norm,
            "opp_meter_norm": opp_meter_norm,
            "self_stun_norm": self_stun_norm,
            "opp_stun_norm": opp_stun_norm,
            "self_crouching": self_crouching,
            "opp_crouching": opp_crouching,
            "self_knockdown": self_knockdown,
            "opp_knockdown": opp_knockdown,
            "self_airborne": self_airborne,
            "opp_airborne": opp_airborne,
            "self_attacking": self_attacking,
            "opp_attacking": opp_attacking,
            "self_blocking": self_blocking,
            "opp_blocking": opp_blocking,
            "self_in_combo": self_in_combo,
            "opp_in_combo": opp_in_combo,
            "in_freeze": in_freeze,
            "self_superfreeze": self_superfreeze,
            "opp_superfreeze": opp_superfreeze,
            "self_has_proj": self_has_proj,
            "opp_has_proj": opp_has_proj,
            "in_fwd": in_fwd,
            "in_back": in_back,
            "in_up": in_up,
            "in_down": in_down,
            "in_btn_mask": in_btn_mask,
        }
    )
    return out
