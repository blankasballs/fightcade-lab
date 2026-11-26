"""Milestone 2 Layer 2: Unsupervised breakpoint detection (`ruptures` PELT) & 3-window slicing.

Detects high-leverage interaction inflection points `t0` within each round and slices:
  - Context Window: [t0 - 45, t0]   (0.75s prior, downsampled to 15 steps for FAISS)
  - Action Window:  [t0, t0 + 20]   (0.33s response summary from canonical inputs & animations)
  - Outcome Window: [t0, t0 + 120]  (2.0s net HP, corner, and stun swing, clamped to round end)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import ruptures as rpt

from fightcade_analysis.features import CORE_FEATURE_COLUMNS

CONTEXT_FRAMES: int = 45
CONTEXT_STEP: int = 3  # 45 / 3 = 15 timesteps per Context Window
ACTION_FRAMES: int = 20
OUTCOME_FRAMES: int = 120

# High-signal channels & weights fed into PELT change-point detection
PELT_CHANNEL_WEIGHTS: dict[str, float] = {
    "dx_norm": 1.2,
    "self_corner_prox": 1.0,
    "opp_corner_prox": 1.0,
    "self_y_norm": 1.0,
    "opp_y_norm": 1.0,
    "self_hp_norm": 3.0,
    "opp_hp_norm": 3.0,
    "self_stun_norm": 1.5,
    "opp_stun_norm": 1.5,
    "self_knockdown": 2.5,
    "opp_knockdown": 2.5,
    "self_in_combo": 2.0,
    "opp_in_combo": 2.0,
    "self_blocking": 1.2,
    "opp_blocking": 1.2,
    "self_superfreeze": 2.5,
    "opp_superfreeze": 2.5,
    "self_has_proj": 1.2,
    "opp_has_proj": 1.2,
}


@dataclass(frozen=True)
class SegmentationConfig:
    penalty: float = 18.0
    min_size: int = 45  # 0.75s minimum spacing between breakpoints
    jump: int = 5  # 12 Hz PELT candidate grid (fast + clean)
    lead_frames: int = 0  # optional shift earlier (t0 - lead_frames)
    context_frames: int = CONTEXT_FRAMES
    context_step: int = CONTEXT_STEP
    action_frames: int = ACTION_FRAMES
    outcome_frames: int = OUTCOME_FRAMES


def build_pelt_signal(round_df: pd.DataFrame) -> np.ndarray:
    """Assemble weighted multivariate signal matrix (T, D) for PELT."""
    cols = list(PELT_CHANNEL_WEIGHTS.keys())
    weights = np.array([PELT_CHANNEL_WEIGHTS[c] for c in cols], dtype=np.float32)
    mat = round_df[cols].to_numpy(dtype=np.float32)
    return mat * weights[None, :]


def detect_round_breakpoints(
    round_df: pd.DataFrame,
    config: SegmentationConfig | None = None,
) -> list[int]:
    """Run PELT on a single round's canonical feature DataFrame and return local row indices `t0`."""
    cfg = config or SegmentationConfig()
    # Restrict PELT signal to active round frames (in_match == 1) so post-KO tail isn't a false breakpoint
    active_mask = round_df["in_match"].to_numpy() == 1
    active_len = int(active_mask.sum())
    if active_len < max(cfg.min_size * 2, cfg.context_frames + cfg.action_frames + 1):
        return []

    signal = build_pelt_signal(round_df.iloc[:active_len])
    algo = rpt.Pelt(model="l2", min_size=cfg.min_size, jump=cfg.jump).fit(signal)
    raw_bkps = algo.predict(pen=cfg.penalty)

    # ruptures always appends `len(signal)` as the final entry; drop it
    bkps: list[int] = []
    for idx in raw_bkps[:-1]:
        t0 = int(idx) - cfg.lead_frames
        if t0 >= cfg.context_frames and (t0 + cfg.action_frames) < len(round_df):
            bkps.append(t0)
    return bkps


def summarize_action_window(action_df: pd.DataFrame) -> dict[str, Any]:
    """Summarize player response over [t0, t0 + 20] into interpretable action descriptors."""
    n = max(len(action_df), 1)
    fwd_frac = float(action_df["in_fwd"].mean())
    back_frac = float(action_df["in_back"].mean())
    up_any = int(action_df["in_up"].max() > 0)
    down_frac = float(action_df["in_down"].mean())
    btn_union = int(np.bitwise_or.reduce(action_df["in_btn_mask"].to_numpy(dtype=np.uint16)))
    attack_any = int(action_df["self_attacking"].max() > 0)
    block_any = int(action_df["self_blocking"].max() > 0)
    air_any = int(action_df["self_airborne"].max() > 0)

    # First animation change within action window (or initial anim if unchanged)
    anims = action_df["self_anim"].to_numpy()
    start_anim = int(anims[0])
    changed = anims[anims != start_anim]
    primary_anim = int(changed[0]) if len(changed) > 0 else start_anim

    # Coarse human-readable action bucket (supplemented by primary_anim in Stage 3)
    if attack_any or btn_union > 0:
        if up_any or air_any:
            coarse = "air_attack"
        elif back_frac >= 0.4:
            coarse = "defensive_button"
        else:
            coarse = "attack"
    elif block_any or back_frac >= 0.5:
        coarse = "block_or_back"
    elif up_any or air_any:
        coarse = "jump"
    elif fwd_frac >= 0.5:
        coarse = "advance"
    elif down_frac >= 0.5:
        coarse = "crouch"
    else:
        coarse = "neutral_hold"

    return {
        "action_coarse": coarse,
        "action_primary_anim": primary_anim,
        "action_btn_union": btn_union,
        "action_fwd_frac": round(fwd_frac, 3),
        "action_back_frac": round(back_frac, 3),
        "action_attack_any": attack_any,
        "action_block_any": block_any,
        "action_air_any": air_any,
        "action_frames": n,
    }


def compute_outcome_window(round_df: pd.DataFrame, t0: int, outcome_frames: int = OUTCOME_FRAMES) -> dict[str, float]:
    """Compute net health, stun, and positional swing over [t0, min(t0 + outcome_frames, round_end)]."""
    t1 = min(t0 + outcome_frames, len(round_df) - 1)
    r0 = round_df.iloc[t0]
    r1 = round_df.iloc[t1]

    delta_self_hp = float(r1["self_hp_norm"] - r0["self_hp_norm"])
    delta_opp_hp = float(r1["opp_hp_norm"] - r0["opp_hp_norm"])
    net_hp_swing = delta_self_hp - delta_opp_hp

    delta_self_stun = float(r1["self_stun_norm"] - r0["self_stun_norm"])
    delta_opp_stun = float(r1["opp_stun_norm"] - r0["opp_stun_norm"])
    net_stun_swing = delta_opp_stun - delta_self_stun

    delta_self_corner = float(r1["self_corner_prox"] - r0["self_corner_prox"])
    delta_opp_corner = float(r1["opp_corner_prox"] - r0["opp_corner_prox"])
    net_corner_swing = delta_opp_corner - delta_self_corner

    outcome_value = net_hp_swing + 0.15 * net_corner_swing + 0.10 * net_stun_swing

    return {
        "delta_self_hp": delta_self_hp,
        "delta_opp_hp": delta_opp_hp,
        "net_hp_swing": net_hp_swing,
        "net_stun_swing": net_stun_swing,
        "net_corner_swing": net_corner_swing,
        "outcome_value": outcome_value,
    }


def extract_decision_points(
    feat_df: pd.DataFrame,
    config: SegmentationConfig | None = None,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Detect breakpoints across all rounds of a player-perspective feature DataFrame.

    Returns:
        meta_df: One row per decision point with deep-link keys, t0 state snapshot,
                 Action Window summary, and Outcome Window metrics.
        context_vectors: float32 array of shape (N, n_steps * n_features) ready for FAISS.
    """
    cfg = config or SegmentationConfig()
    rows: list[dict[str, Any]] = []
    vectors: list[np.ndarray] = []

    feature_cols = list(CORE_FEATURE_COLUMNS)
    n_steps = cfg.context_frames // cfg.context_step
    expected_dim = n_steps * len(feature_cols)

    for rid, r_df in feat_df.groupby("round_id", sort=True):
        r_df = r_df.reset_index(drop=True)
        bkps = detect_round_breakpoints(r_df, config=cfg)
        if not bkps:
            continue

        feat_mat = r_df[feature_cols].to_numpy(dtype=np.float32)

        for t0 in bkps:
            # Context window [t0 - context_frames, t0) downsampled by context_step -> (n_steps, F)
            ctx_slice = feat_mat[t0 - cfg.context_frames : t0 : cfg.context_step]
            if ctx_slice.shape[0] != n_steps:
                continue
            vectors.append(ctx_slice.reshape(-1))

            act_df = r_df.iloc[t0 : t0 + cfg.action_frames]
            action_info = summarize_action_window(act_df)
            outcome_info = compute_outcome_window(r_df, t0, cfg.outcome_frames)

            snap = r_df.iloc[t0]
            row: dict[str, Any] = {
                "quark_id": snap["quark_id"],
                "perspective": int(snap["perspective"]),
                "round_id": int(rid),
                "round_local_idx": int(t0),
                "emu_frame": int(snap["emu_frame"]),
                "game_frame": int(snap["game_frame"]),
                "self_char": int(snap["self_char"]),
                "opp_char": int(snap["opp_char"]),
                "self_sa": int(snap["self_sa"]),
                "opp_sa": int(snap["opp_sa"]),
                "t0_dx_px": float(snap["dx_px"]),
                "t0_self_corner": float(snap["self_corner_prox"]),
                "t0_opp_corner": float(snap["opp_corner_prox"]),
                "t0_self_kd": int(snap["self_knockdown"]),
                "t0_opp_kd": int(snap["opp_knockdown"]),
                "t0_self_air": int(snap["self_airborne"]),
                "t0_opp_air": int(snap["opp_airborne"]),
                "t0_self_atk": int(snap["self_attacking"]),
                "t0_opp_atk": int(snap["opp_attacking"]),
                "t0_self_blk": int(snap["self_blocking"]),
                "t0_opp_blk": int(snap["opp_blocking"]),
                "t0_self_sf": int(snap["self_superfreeze"]),
                "t0_opp_sf": int(snap["opp_superfreeze"]),
                "t0_self_proj": int(snap["self_has_proj"]),
                "t0_opp_proj": int(snap["opp_has_proj"]),
                "t0_self_hp": float(snap["self_hp_norm"]),
                "t0_opp_hp": float(snap["opp_hp_norm"]),
            }
            row.update(action_info)
            row.update(outcome_info)
            rows.append(row)

    if not rows:
        return pd.DataFrame(), np.empty((0, expected_dim), dtype=np.float32)

    return pd.DataFrame(rows), np.vstack(vectors).astype(np.float32)


def extract_oracle_events(round_df: pd.DataFrame, cooldown: int = 45) -> dict[str, list[int]]:
    """Extract held-out ground-truth RAM events for Layer 2 breakpoint recall & timing evaluation."""
    active_len = int((round_df["in_match"].to_numpy() == 1).sum())
    if active_len < 30:
        return {"damage": [], "knockdown": [], "wakeup": [], "combo_start": []}

    sub = round_df.iloc[:active_len]
    events: dict[str, list[int]] = {"damage": [], "knockdown": [], "wakeup": [], "combo_start": []}

    def _dedup(indices: np.ndarray) -> list[int]:
        out: list[int] = []
        for idx in indices:
            i = int(idx)
            if not out or (i - out[-1]) >= cooldown:
                out.append(i)
        return out

    # 1. Significant HP drop (>= 6% of max HP over 10 frames)
    s_hp = sub["self_hp_norm"].to_numpy()
    o_hp = sub["opp_hp_norm"].to_numpy()
    dmg_mask = np.zeros(active_len, dtype=bool)
    if active_len > 10:
        dmg_mask[10:] = ((s_hp[:-10] - s_hp[10:]) >= 0.06) | ((o_hp[:-10] - o_hp[10:]) >= 0.06)
    events["damage"] = _dedup(np.flatnonzero(dmg_mask))

    # 2. Knockdown start (0 -> 1) and Wakeup (1 -> 0) for either player
    s_kd = sub["self_knockdown"].to_numpy()
    o_kd = sub["opp_knockdown"].to_numpy()
    kd_start = np.flatnonzero(((s_kd[1:] == 1) & (s_kd[:-1] == 0)) | ((o_kd[1:] == 1) & (o_kd[:-1] == 0))) + 1
    kd_end = np.flatnonzero(((s_kd[1:] == 0) & (s_kd[:-1] == 1)) | ((o_kd[1:] == 0) & (o_kd[:-1] == 1))) + 1
    events["knockdown"] = _dedup(kd_start)
    events["wakeup"] = _dedup(kd_end)

    # 3. Combo start (0 -> 1) for either player
    s_cb = sub["self_in_combo"].to_numpy()
    o_cb = sub["opp_in_combo"].to_numpy()
    cb_start = np.flatnonzero(((s_cb[1:] == 1) & (s_cb[:-1] == 0)) | ((o_cb[1:] == 1) & (o_cb[:-1] == 0))) + 1
    events["combo_start"] = _dedup(cb_start)

    return events


def evaluate_segmentation(
    feat_df: pd.DataFrame,
    config: SegmentationConfig | None = None,
    pre_window: int = 45,
    post_window: int = 20,
) -> dict[str, Any]:
    """Compute Layer 2 quality metrics: density, oracle event recall, pre- vs post-timing, and leakage."""
    cfg = config or SegmentationConfig()
    bkps_per_round: list[int] = []
    oracle_totals: dict[str, int] = {"damage": 0, "knockdown": 0, "wakeup": 0, "combo_start": 0}
    oracle_hits: dict[str, int] = {"damage": 0, "knockdown": 0, "wakeup": 0, "combo_start": 0}
    oracle_pre_hits: dict[str, int] = {"damage": 0, "knockdown": 0, "wakeup": 0, "combo_start": 0}
    context_hp_drop_leaks = 0
    total_bkps = 0

    for _, r_df in feat_df.groupby("round_id", sort=True):
        r_df = r_df.reset_index(drop=True)
        bkps = detect_round_breakpoints(r_df, config=cfg)
        bkps_per_round.append(len(bkps))
        total_bkps += len(bkps)

        bkp_arr = np.array(bkps, dtype=np.int32)
        oracles = extract_oracle_events(r_df)
        for kind, ev_list in oracles.items():
            oracle_totals[kind] += len(ev_list)
            for ev in ev_list:
                if len(bkp_arr) == 0:
                    continue
                diffs = bkp_arr - ev  # negative => breakpoint BEFORE event (pre-decision)
                if np.any((diffs >= -pre_window) & (diffs <= post_window)):
                    oracle_hits[kind] += 1
                if np.any((diffs >= -pre_window) & (diffs <= 0)):
                    oracle_pre_hits[kind] += 1

        # Leakage check: did an HP drop already happen inside the final 5 frames of Context [t0-5, t0]?
        s_hp = r_df["self_hp_norm"].to_numpy()
        o_hp = r_df["opp_hp_norm"].to_numpy()
        for t0 in bkps:
            if t0 >= 5:
                s_drop = s_hp[t0 - 5] - s_hp[t0]
                o_drop = o_hp[t0 - 5] - o_hp[t0]
                if s_drop >= 0.05 or o_drop >= 0.05:
                    context_hp_drop_leaks += 1

    recall = {
        k: (oracle_hits[k] / oracle_totals[k] if oracle_totals[k] > 0 else 1.0)
        for k in oracle_totals
    }
    pre_recall = {
        k: (oracle_pre_hits[k] / oracle_totals[k] if oracle_totals[k] > 0 else 1.0)
        for k in oracle_totals
    }

    return {
        "rounds": len(bkps_per_round),
        "total_breakpoints": total_bkps,
        "mean_bkps_per_round": float(np.mean(bkps_per_round)) if bkps_per_round else 0.0,
        "min_bkps_per_round": int(min(bkps_per_round)) if bkps_per_round else 0,
        "max_bkps_per_round": int(max(bkps_per_round)) if bkps_per_round else 0,
        "oracle_totals": oracle_totals,
        "oracle_recall": recall,
        "oracle_pre_recall": pre_recall,
        "context_hp_leak_rate": (context_hp_drop_leaks / total_bkps) if total_bkps > 0 else 0.0,
    }
