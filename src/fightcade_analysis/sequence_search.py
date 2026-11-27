"""Manual Interaction Selection — Full-Sequence Semantic Search & HTML Comparison Report.

Given a user-annotated sequence `[start_gf .. end_gf]` (either from `data/selections/bookmarks.jsonl`
marked live inside `fcadefbneo.exe` via `scripts/annotate.ps1`, or via direct CLI frame arguments),
searches the 1M+ frame Fightcade corpus for the closest matching interaction trajectories using:
  Stage 1: Fast multi-anchor candidate round retrieval via cached `HierarchicalContextIndex`
  Stage 2: Exact multi-scale (0.80x, 1.00x, 1.25x duration) vectorized trajectory re-ranking
           with matchup-first quality fallback (`exact_matchup -> same_self_char -> universal`)
  Stage 3: Single-pass 60fps MP4 clip + `.fs`/`.inputs` rendering, optional Gemini breakdown,
           and paused-by-default HTML report with one-click Replay Takeover on every match!
"""

from __future__ import annotations

import argparse
from collections import Counter
import html
import json
import os
from pathlib import Path
import time
from typing import Any

import numpy as np
import pandas as pd

from fightcade_analysis.clip_renderer import (
    DEFAULT_DATA_DIR,
    ClipRequest,
    find_parquet_for_quark,
    render_clips_batch,
)
from fightcade_analysis.coaching import _similarity_pct, describe_decision_point
from fightcade_analysis.features import CORE_FEATURE_COLUMNS, extract_player_features
from fightcade_analysis.gemini_summary import (
    DEFAULT_MODEL_CHAIN,
    _call_gemini_json,
    _encode_mp4_part,
    _load_cache,
    _save_cache,
    load_gemini_api_key,
)
from fightcade_analysis.index import (
    DEFAULT_FEATURE_WEIGHTS,
    HierarchicalContextIndex,
    classify_tactical_regime,
    load_or_build_corpus,
)
from fightcade_analysis.naming import (
    build_reports_index_html,
    build_sequence_search_stem,
    enrich_sequence_search_metadata,
    format_matchup_players_html,
)
from fightcade_analysis.segmentation import (
    SegmentationConfig,
    compute_outcome_window,
    summarize_action_window,
)
from fightcade_analysis.takeover import (
    TAKEOVER_UI_CSS,
    TAKEOVER_UI_JS,
    render_takeover_buttons_html,
)

TRAJ_STEPS: int = 16
DURATION_SCALES: tuple[float, ...] = (0.80, 1.00, 1.25)


def load_bookmarks(bookmarks_path: Path | None = None) -> list[dict[str, Any]]:
    path = bookmarks_path or (DEFAULT_DATA_DIR / "selections" / "bookmarks.jsonl")
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def resolve_bookmark(
    *,
    selection_id: str | None = None,
    latest: bool = False,
    bookmarks_path: Path | None = None,
) -> dict[str, Any]:
    bms = load_bookmarks(bookmarks_path)
    if not bms:
        raise FileNotFoundError(f"No bookmarks found in {bookmarks_path or (DEFAULT_DATA_DIR / 'selections' / 'bookmarks.jsonl')}")
    if latest:
        return bms[-1]
    if selection_id:
        for bm in reversed(bms):
            if bm.get("id") == selection_id:
                return bm
        raise KeyError(f"SelectionId '{selection_id}' not found in bookmarks")
    raise ValueError("Provide either selection_id or latest=True")


def _sequence_weight_vector(n_steps: int = TRAJ_STEPS) -> np.ndarray:
    per_feat = np.array([DEFAULT_FEATURE_WEIGHTS.get(c, 1.0) for c in CORE_FEATURE_COLUMNS], dtype=np.float32)
    return np.tile(per_feat, n_steps).astype(np.float32)


def resample_trajectory(feat_mat: np.ndarray, n_steps: int = TRAJ_STEPS) -> np.ndarray:
    """Uniformly sample a (L, F) canonical feature trajectory to (n_steps * F,) float32."""
    length = feat_mat.shape[0]
    if length == 0:
        return np.zeros(n_steps * feat_mat.shape[1], dtype=np.float32)
    if length == 1:
        return np.tile(feat_mat[0], n_steps).astype(np.float32)
    idx = np.round(np.linspace(0, length - 1, n_steps)).astype(np.int64)
    return feat_mat[idx].reshape(-1).astype(np.float32)


def _snapshot_for_row(row: pd.Series) -> dict[str, Any]:
    return {
        "self_char": int(row["self_char"]),
        "opp_char": int(row["opp_char"]),
        "self_sa": int(row["self_sa"]),
        "opp_sa": int(row["opp_sa"]),
        "t0_dx_px": float(row["dx_px"]),
        "t0_self_corner": float(row["self_corner_prox"]),
        "t0_opp_corner": float(row["opp_corner_prox"]),
        "t0_self_kd": int(row["self_knockdown"]),
        "t0_opp_kd": int(row["opp_knockdown"]),
        "t0_self_air": int(row["self_airborne"]),
        "t0_opp_air": int(row["opp_airborne"]),
        "t0_self_atk": int(row["self_attacking"]),
        "t0_opp_atk": int(row["opp_attacking"]),
        "t0_self_blk": int(row["self_blocking"]),
        "t0_opp_blk": int(row["opp_blocking"]),
        "t0_self_sf": int(row["self_superfreeze"]),
        "t0_opp_sf": int(row["opp_superfreeze"]),
        "t0_self_proj": int(row["self_has_proj"]),
        "t0_opp_proj": int(row["opp_has_proj"]),
    }


def _dominant_regime(slice_df: pd.DataFrame) -> str:
    """Return the dominant non-neutral tactical regime across a sequence (or 'neutral')."""
    if len(slice_df) == 0:
        return "neutral"
    sample_idx = np.unique(np.linspace(0, len(slice_df) - 1, min(9, len(slice_df))).astype(int))
    regs = [classify_tactical_regime(_snapshot_for_row(slice_df.iloc[int(i)])) for i in sample_idx]
    non_neutral = [r for r in regs if r != "neutral"]
    if non_neutral:
        return Counter(non_neutral).most_common(1)[0][0]
    return "neutral"


def _build_query_context_anchors(
    round_df: pd.DataFrame,
    start_idx: int,
    end_idx: int,
    cfg: SegmentationConfig,
) -> list[np.ndarray]:
    """Extract up to 3 Context Window vectors (start, mid, end) for Stage-1 FAISS candidate recall."""
    cols = list(CORE_FEATURE_COLUMNS)
    feat_mat = round_df[cols].to_numpy(dtype=np.float32)
    n_steps = cfg.context_frames // cfg.context_step
    anchors: list[np.ndarray] = []

    raw_positions = [
        max(cfg.context_frames, start_idx + cfg.context_frames),
        max(cfg.context_frames, (start_idx + end_idx) // 2),
        max(cfg.context_frames, end_idx),
    ]
    seen_pos: set[int] = set()
    for pos in raw_positions:
        p = min(pos, len(round_df) - 1)
        if p in seen_pos or p < cfg.context_frames:
            continue
        seen_pos.add(p)
        sl = feat_mat[p - cfg.context_frames : p : cfg.context_step]
        if sl.shape[0] == n_steps:
            anchors.append(sl.reshape(-1).astype(np.float32))
    return anchors


def _score_candidate_round(
    cand_round_df: pd.DataFrame,
    q_traj_weighted: np.ndarray,
    weight_vec: np.ndarray,
    weight_sq_sum: float,
    *,
    q_len: int,
    q_mean_dx: float,
    q_mean_self_cnr: float,
    q_mean_opp_cnr: float,
    q_dom_regime: str,
    n_steps: int = TRAJ_STEPS,
    stride: int = 3,
    scales: tuple[float, ...] = DURATION_SCALES,
    max_norm_dist: float = 0.36,
    max_dx_diff: float = 110.0,
    max_corner_diff: float = 0.45,
) -> list[dict[str, Any]]:
    """Vectorized multi-scale sliding-window trajectory scan across one candidate round."""
    active_df = cand_round_df[cand_round_df["in_match"] == 1].reset_index(drop=True)
    T = len(active_df)
    if T < 15:
        return []

    cols = list(CORE_FEATURE_COLUMNS)
    feat_mat = active_df[cols].to_numpy(dtype=np.float32)
    dx_arr = active_df["dx_px"].to_numpy(dtype=np.float32)
    s_cnr_arr = active_df["self_corner_prox"].to_numpy(dtype=np.float32)
    o_cnr_arr = active_df["opp_corner_prox"].to_numpy(dtype=np.float32)

    raw_hits: list[tuple[float, int, int]] = []  # (norm_dist, s_idx, e_idx)
    denom = max(weight_sq_sum, 1e-6)

    for sc in scales:
        win_len = max(15, int(round(q_len * sc)))
        if win_len >= T:
            continue
        starts = np.arange(0, T - win_len, stride, dtype=np.int64)
        if len(starts) == 0:
            continue

        # Fast vectorized pre-filter on mean spacing and corner proximity
        mid_idx = starts + (win_len // 2)
        end_idx = starts + win_len - 1
        mean_dx = (dx_arr[starts] + dx_arr[mid_idx] + dx_arr[end_idx]) / 3.0
        mean_sc = (s_cnr_arr[starts] + s_cnr_arr[mid_idx] + s_cnr_arr[end_idx]) / 3.0
        mean_oc = (o_cnr_arr[starts] + o_cnr_arr[mid_idx] + o_cnr_arr[end_idx]) / 3.0

        geom_ok = (
            (np.abs(mean_dx - q_mean_dx) <= max_dx_diff)
            & (np.abs(mean_sc - q_mean_self_cnr) <= max_corner_diff)
            & (np.abs(mean_oc - q_mean_opp_cnr) <= max_corner_diff)
        )
        starts = starts[geom_ok]
        if len(starts) == 0:
            continue

        # Gather n_steps evenly spaced indices per window -> shape (N_win, n_steps)
        rel_steps = np.round(np.linspace(0, win_len - 1, n_steps)).astype(np.int64)
        gather_idx = starts[:, None] + rel_steps[None, :]  # (N_win, n_steps)
        # Flatten features: (N_win, n_steps * F)
        win_vecs = feat_mat[gather_idx].reshape(len(starts), -1)
        weighted_diff = (win_vecs * weight_vec[None, :]) - q_traj_weighted[None, :]
        l2_sq = np.sum(weighted_diff * weighted_diff, axis=1, dtype=np.float64)
        norm_dists = np.sqrt(np.maximum(0.0, l2_sq) / denom)

        passing = np.flatnonzero(norm_dists <= max_norm_dist)
        for p in passing:
            s_i = int(starts[int(p)])
            raw_hits.append((float(norm_dists[int(p)]), s_i, s_i + win_len - 1))

    if not raw_hits:
        return []

    # Non-maximum suppression within this round so overlapping scales/windows collapse to the best alignment
    raw_hits.sort(key=lambda x: x[0])
    min_sep = max(60, q_len // 2)
    kept: list[dict[str, Any]] = []
    kept_centers: list[int] = []

    for nd, s_i, e_i in raw_hits:
        # 1-frame local refinement around coarse stride hit for crisp transition alignment
        win_len = e_i - s_i + 1
        best_nd, best_s, best_e = nd, s_i, e_i
        rel_steps = np.round(np.linspace(0, win_len - 1, n_steps)).astype(np.int64)
        for delta in (-2, -1, 1, 2):
            ns = s_i + delta
            ne = ns + win_len - 1
            if ns < 0 or ne >= T:
                continue
            vec = feat_mat[ns + rel_steps].reshape(-1) * weight_vec
            diff = vec - q_traj_weighted
            cand_nd = float(np.sqrt(max(0.0, float(np.dot(diff, diff))) / denom))
            if cand_nd < best_nd:
                best_nd, best_s, best_e = cand_nd, ns, ne
        nd, s_i, e_i = best_nd, best_s, best_e

        center = (s_i + e_i) // 2
        if any(abs(center - kc) < min_sep for kc in kept_centers):
            continue

        slice_df = active_df.iloc[s_i : e_i + 1]
        cand_regime = _dominant_regime(slice_df)
        # Penalize/reject severe role flips (e.g. wakeup defense vs oki offense)
        if q_dom_regime != "neutral" and cand_regime != q_dom_regime:
            # Allow close pressure/neutral overlap only if distance is very tight; reject strict polarity flips
            strict_regimes = {"wakeup_def", "oki_off", "superfreeze_def", "superfreeze_off", "antiair_def", "jumpin_off"}
            if q_dom_regime in strict_regimes or cand_regime in strict_regimes:
                continue

        kept_centers.append(center)
        mid_row = active_df.iloc[center]
        desc = describe_decision_point(_snapshot_for_row(mid_row))
        act_slice = active_df.iloc[s_i : min(s_i + 30, e_i + 1)]
        act_info = summarize_action_window(act_slice)
        out_info = compute_outcome_window(active_df, s_i, outcome_frames=max(e_i - s_i, 90))

        kept.append(
            {
                "quark_id": str(mid_row["quark_id"]),
                "perspective": int(mid_row["perspective"]),
                "round_id": int(mid_row["round_id"]),
                "start_gf": int(active_df.iloc[s_i]["game_frame"]),
                "end_gf": int(active_df.iloc[e_i]["game_frame"]),
                "mid_gf": int(mid_row["game_frame"]),
                "duration_s": round((e_i - s_i + 1) / 60.0, 2),
                "self_char": int(mid_row["self_char"]),
                "opp_char": int(mid_row["opp_char"]),
                "self_char_name": desc["self_char_name"],
                "opp_char_name": desc["opp_char_name"],
                "label": desc["label"],
                "regime": cand_regime,
                "action_coarse": str(act_info["action_coarse"]),
                "outcome_value": round(float(out_info["outcome_value"]), 4),
                "net_hp_swing": round(float(out_info["net_hp_swing"]), 4),
                "norm_dist": round(nd, 4),
                "similarity_pct": _similarity_pct(nd),
                "mean_dx_px": round(float(slice_df["dx_px"].mean()), 1),
                "mean_self_corner": round(float(slice_df["self_corner_prox"].mean()), 2),
            }
        )
        if len(kept) >= 4:
            break

    return kept


def search_similar_sequences(
    target_parquet: Path,
    *,
    perspective: int,
    start_gf: int,
    end_gf: int,
    parquet_dir: Path,
    corpus_index: HierarchicalContextIndex | None = None,
    config: SegmentationConfig | None = None,
    top_k: int = 6,
    tier_override: str | None = None,
    max_norm_dist: float = 0.36,
    min_tier_matches: int = 4,
    exclude_self: bool = True,
    selection_id: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Semantically search the corpus for interactions matching `[start_gf .. end_gf]`."""
    cfg = config or SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    parquet_files = sorted(parquet_dir.glob("*.parquet"))
    pq_by_qid = {p.stem: p for p in parquet_files}
    if target_parquet.stem not in pq_by_qid:
        pq_by_qid[target_parquet.stem] = target_parquet

    if corpus_index is None:
        corpus, _ = load_or_build_corpus(
            parquet_files,
            config=cfg,
            both_perspectives=True,
            cache_dir=parquet_dir.resolve().parent / "index",
        )
        corpus_index = HierarchicalContextIndex(corpus.meta, corpus.vectors)

    # Extract target sequence features
    t_df = pd.read_parquet(target_parquet)
    target_qid = str(t_df["quark_id"].iloc[0]) if "quark_id" in t_df.columns and len(t_df) > 0 else target_parquet.stem
    t_feats = extract_player_features(t_df, perspective=perspective)

    seq_mask = (t_feats["game_frame"] >= start_gf) & (t_feats["game_frame"] <= end_gf)
    seq_df = t_feats[seq_mask].reset_index(drop=True)
    if len(seq_df) < 5:
        raise ValueError(
            f"Selected range gf{start_gf}..gf{end_gf} has only {len(seq_df)} frames in {target_parquet} "
            f"(available game_frame range: {int(t_feats['game_frame'].min())}..{int(t_feats['game_frame'].max())})"
        )

    round_id = int(seq_df["round_id"].mode().iloc[0])
    round_df = t_feats[t_feats["round_id"] == round_id].reset_index(drop=True)
    r_mask = (round_df["game_frame"] >= start_gf) & (round_df["game_frame"] <= end_gf)
    r_idxs = np.flatnonzero(r_mask.to_numpy())
    start_idx = int(r_idxs[0])
    end_idx = int(r_idxs[-1])
    q_len = max(15, end_idx - start_idx + 1)

    cols = list(CORE_FEATURE_COLUMNS)
    q_feat_mat = round_df.iloc[start_idx : end_idx + 1][cols].to_numpy(dtype=np.float32)
    weight_vec = _sequence_weight_vector(TRAJ_STEPS)
    weight_sq_sum = float(np.sum(weight_vec.astype(np.float64) ** 2))
    q_traj_weighted = resample_trajectory(q_feat_mat, TRAJ_STEPS) * weight_vec

    mid_row = round_df.iloc[(start_idx + end_idx) // 2]
    q_desc = describe_decision_point(_snapshot_for_row(mid_row))
    q_dom_regime = _dominant_regime(round_df.iloc[start_idx : end_idx + 1])
    q_act = summarize_action_window(round_df.iloc[start_idx : min(start_idx + 30, end_idx + 1)])
    q_out = compute_outcome_window(round_df, start_idx, outcome_frames=max(q_len, 90))

    q_self_char = int(mid_row["self_char"])
    q_opp_char = int(mid_row["opp_char"])
    q_mean_dx = float(round_df.iloc[start_idx : end_idx + 1]["dx_px"].mean())
    q_mean_self_cnr = float(round_df.iloc[start_idx : end_idx + 1]["self_corner_prox"].mean())
    q_mean_opp_cnr = float(round_df.iloc[start_idx : end_idx + 1]["opp_corner_prox"].mean())

    anchors = _build_query_context_anchors(round_df, start_idx, end_idx, cfg)
    feat_cache: dict[tuple[str, int], pd.DataFrame] = {}

    def _get_player_feats(qid: str, persp: int) -> pd.DataFrame | None:
        key = (qid, persp)
        if key not in feat_cache:
            p = pq_by_qid.get(qid)
            if p is None or not p.exists():
                return None
            feat_cache[key] = extract_player_features(pd.read_parquet(p), perspective=persp)
        return feat_cache[key]

    tiers_to_try = [tier_override] if tier_override else ["exact_matchup", "same_self_char", "universal"]
    best_matches: list[dict[str, Any]] = []
    best_tier = tiers_to_try[0]
    rounds_scanned = 0

    for tier_name in tiers_to_try:
        # Stage 1: Gather candidate (quark_id, perspective, round_id) slices
        cand_rounds: list[tuple[str, int, int]] = []
        meta = corpus_index.meta
        if tier_name == "exact_matchup":
            sub = meta[(meta["self_char"] == q_self_char) & (meta["opp_char"] == q_opp_char)]
        elif tier_name == "same_self_char":
            sub = meta[meta["self_char"] == q_self_char]
        else:
            sub = meta

        if exclude_self:
            sub = sub[sub["quark_id"] != target_qid]

        if len(sub) == 0:
            continue

        unique_rounds = list(sub.groupby(["quark_id", "perspective", "round_id"]).groups.keys())
        if len(unique_rounds) <= 80:
            # Exhaustive scan across all rounds in this tier (<0.8s vectorized!)
            cand_rounds = [(str(q), int(p), int(r)) for q, p, r in unique_rounds]
        elif anchors:
            # Use FAISS multi-anchor Stage-1 recall ranked by Gaussian similarity (so one near-exact match
            # always outranks multiple loose matches)
            round_scores: dict[tuple[str, int, int], float] = {}
            for anc in anchors:
                nbrs, _ = corpus_index.query(
                    anc,
                    self_char=q_self_char,
                    opp_char=q_opp_char,
                    k=120,
                    exclude_quark_id=target_qid if exclude_self else None,
                    tier_override=tier_name,
                )
                for _, nr in nbrs.iterrows():
                    rk = (str(nr["quark_id"]), int(nr["perspective"]), int(nr["round_id"]))
                    nd = float(nr.get("norm_dist", 0.5))
                    sim_w = float(np.exp(-((nd / 0.15) ** 2)))
                    round_scores[rk] = round_scores.get(rk, 0.0) + sim_w
            cand_rounds = [k for k, _ in sorted(round_scores.items(), key=lambda x: x[1], reverse=True)[:90]]
            if not exclude_self and (target_qid, int(perspective), round_id) not in cand_rounds:
                cand_rounds.insert(0, (target_qid, int(perspective), round_id))
        else:
            cand_rounds = [(str(q), int(p), int(r)) for q, p, r in unique_rounds[:90]]

        # Stage 2: Exact multi-scale vectorized sliding-window scan on candidate rounds
        tier_hits: list[dict[str, Any]] = []
        for c_qid, c_p, c_rid in cand_rounds:
            pf = _get_player_feats(c_qid, c_p)
            if pf is None:
                continue
            c_round = pf[pf["round_id"] == c_rid]
            if len(c_round) < 15:
                continue
            rounds_scanned += 1
            hits = _score_candidate_round(
                c_round,
                q_traj_weighted,
                weight_vec,
                weight_sq_sum,
                q_len=q_len,
                q_mean_dx=q_mean_dx,
                q_mean_self_cnr=q_mean_self_cnr,
                q_mean_opp_cnr=q_mean_opp_cnr,
                q_dom_regime=q_dom_regime,
                max_norm_dist=max_norm_dist,
            )
            tier_hits.extend(hits)

        tier_hits.sort(key=lambda h: h["norm_dist"])
        if len(tier_hits) >= min_tier_matches or tier_override is not None:
            best_matches = tier_hits[:top_k]
            best_tier = tier_name
            break
        if len(tier_hits) > len(best_matches):
            best_matches = tier_hits[:top_k]
            best_tier = tier_name

    return {
        "selection_id": selection_id or f"{target_qid}_p{perspective}_gf{start_gf}_{end_gf}",
        "note": note,
        "query": {
            "quark_id": target_qid,
            "perspective": int(perspective),
            "round_id": round_id,
            "start_gf": int(start_gf),
            "end_gf": int(end_gf),
            "mid_gf": int(mid_row["game_frame"]),
            "duration_s": round(q_len / 60.0, 2),
            "self_char": q_self_char,
            "opp_char": q_opp_char,
            "self_sa": int(mid_row["self_sa"]),
            "opp_sa": int(mid_row["opp_sa"]),
            "self_char_name": q_desc["self_char_name"],
            "opp_char_name": q_desc["opp_char_name"],
            "label": q_desc["label"],
            "regime": q_dom_regime,
            "user_action": str(q_act["action_coarse"]),
            "outcome_value": round(float(q_out["outcome_value"]), 4),
            "net_hp_swing": round(float(q_out["net_hp_swing"]), 4),
            "mean_dx_px": round(q_mean_dx, 1),
            "mean_self_corner": round(q_mean_self_cnr, 2),
        },
        "retrieval_tier": best_tier,
        "rounds_scanned": rounds_scanned,
        "corpus_size": int(len(corpus_index.meta)),
        "matches": best_matches,
    }


def summarize_sequence_search(
    result: dict[str, Any],
    *,
    clips_dir: Path,
    cache_path: Path,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
    force: bool = False,
) -> dict[str, Any] | None:
    """Optional Gemini multimodal comparison of your marked sequence vs top 2 similar corpus sequences."""
    api_key = load_gemini_api_key()
    if not api_key:
        print("WARNING: GEMINI_API_KEY not found; skipping Gemini sequence comparison.")
        return None

    q = result["query"]
    matches = result.get("matches", [])[:2]
    if not matches:
        return None

    cache = _load_cache(cache_path)
    ckey = f"seq_{result['selection_id']}_{'_'.join(m['quark_id'] + '_' + str(m['mid_gf']) for m in matches)}"
    if not force and ckey in cache:
        result["ai_summary"] = cache[ckey]
        return cache[ckey]

    parts: list[dict[str, Any]] = [
        {
            "text": (
                "You are an expert Street Fighter III: 3rd Strike coach comparing a player's manually selected "
                "sequence against the closest matching pro sequences from a Fightcade corpus.\n"
                f"Query: P{q['perspective']} {q['self_char_name']} vs {q['opp_char_name']} | {q['label']} "
                f"(duration {q['duration_s']}s, spacing ~{q['mean_dx_px']:.0f}px, HP swing {q['net_hp_swing']:+.2f}).\n"
                + (f"Player's note: {result['note']}\n" if result.get("note") else "")
            )
        }
    ]
    q_mp4 = clips_dir / f"{q['quark_id']}_gf{int(q['mid_gf'])}.mp4"
    if q_mp4.exists():
        parts.append({"text": "\n[CLIP: query — Player's selected sequence]\n"})
        parts.append(_encode_mp4_part(q_mp4))

    for i, m in enumerate(matches, 1):
        parts.append(
            {
                "text": (
                    f"\n[CLIP: match_{i} — Corpus Match #{i} ({m['similarity_pct']}% similar)]\n"
                    f"P{m['perspective']} {m['self_char_name']} vs {m['opp_char_name']} | {m['label']} | "
                    f"action={m['action_coarse']}, HP swing={m['net_hp_swing']:+.2f}\n"
                )
            }
        )
        m_mp4 = clips_dir / f"{m['quark_id']}_gf{int(m['mid_gf'])}.mp4"
        if m_mp4.exists():
            parts.append(_encode_mp4_part(m_mp4))

    parts.append(
        {
            "text": (
                "\nReturn ONLY a JSON object:\n"
                '{"pattern_summary": "1-2 sentences explaining the shared tactical interaction across these clips.", '
                '"pro_takeaway": "1-2 sentences highlighting what the matched pros did differently and what to practice in Replay Takeover."}'
            )
        }
    )
    try:
        parsed, model_used = _call_gemini_json(parts, api_key=api_key, models=models)
        entry = {
            "model": model_used,
            "pattern_summary": str(parsed.get("pattern_summary", "")).strip(),
            "pro_takeaway": str(parsed.get("pro_takeaway", "")).strip(),
        }
        result["ai_summary"] = entry
        cache[ckey] = entry
        _save_cache(cache_path, cache)
        return entry
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Gemini sequence comparison failed: {exc}")
        return None


def _rel_url(html_path: Path, media_path: Path) -> str:
    return os.path.relpath(media_path, start=html_path.parent).replace("\\", "/")


def build_sequence_search_html(
    result: dict[str, Any],
    out_html: Path,
    *,
    clips_dir: Path | None = None,
    savestates_dir: Path | None = None,
    bookmark: dict[str, Any] | None = None,
) -> Path:
    """Render a self-contained dark-mode HTML Semantic Sequence Search & Takeover report."""
    out_html.parent.mkdir(parents=True, exist_ok=True)
    clips_dir = clips_dir or (out_html.parents[1] / "clips")
    savestates_dir = savestates_dir or (out_html.parents[1] / "savestates")
    q = result["query"]

    def _takeover_cmd(qid: str, frame: int, player: int, label: str, sel_id: str | None = None) -> str:
        if sel_id and bookmark and bookmark.get("savestate") and Path(bookmark["savestate"]).exists():
            return f'C:\\Users\\621\\source\\repos\\fightcade-analysis\\scripts\\takeover.ps1 -SelectionId {sel_id}'
        safe_label = label.replace('"', "'")
        return (
            f'C:\\Users\\621\\source\\repos\\fightcade-analysis\\scripts\\takeover.ps1 '
            f'-QuarkId {qid} -Frame {frame} -Player {int(player)} -Label "{safe_label}"'
        )

    def _clip_card(
        title_html: str,
        qid: str,
        mid_gf: int,
        start_gf: int,
        end_gf: int,
        player: int,
        label: str,
        is_query: bool,
        sel_id: str | None = None,
    ) -> str:
        stem = f"{qid}_gf{mid_gf}"
        mp4 = clips_dir / f"{stem}.mp4"
        fs_exists = (savestates_dir / f"{stem}.fs").exists() or bool(
            sel_id and bookmark and bookmark.get("savestate") and Path(bookmark["savestate"]).exists()
        )
        cmd = _takeover_cmd(qid, mid_gf, player, label, sel_id=sel_id if is_query else None)

        if mp4.exists():
            src = html.escape(_rel_url(out_html, mp4))
            media = f'<video src="{src}" loop muted playsinline preload="metadata" controls></video>'
        else:
            media = f'<div class="no-video">No clip cached yet for <code>{html.escape(stem)}</code> (run with <code>--render-clips</code>)</div>'

        badge = '<span class="badge badge-ok">Instant &lt;1s Takeover Ready</span>' if fs_exists else '<span class="badge badge-muted">Stream Seek</span>'
        cls = "clip-query" if is_query else "clip-match"
        btns_html = render_takeover_buttons_html(
            qid,
            mid_gf,
            player,
            label,
            cmd,
            is_pro=not is_query,
            selection_id=sel_id if is_query else None,
        )
        return f"""
        <div class="clip-box {cls}">
          <div class="clip-meta">{title_html} {badge}</div>
          {media}
          <div class="clip-actions">
            <code>{html.escape(qid)} @ gf{start_gf}..{end_gf}</code>
            {btns_html}
          </div>
        </div>
        """

    q_title = (
        f"<strong>YOUR SELECTION:</strong> {html.escape(q['label'])} "
        f"({q['duration_s']:.1f}s, HP {q['net_hp_swing']:+.2f})"
    )
    query_card = _clip_card(
        q_title,
        q["quark_id"],
        int(q["mid_gf"]),
        int(q["start_gf"]),
        int(q["end_gf"]),
        int(q["perspective"]),
        q["label"],
        is_query=True,
        sel_id=result.get("selection_id"),
    )

    ai = result.get("ai_summary") or {}
    ai_html = ""
    if ai.get("pattern_summary") or ai.get("pro_takeaway"):
        ai_html = f"""
        <div class="ai-box">
          <div class="ai-title">AI Sequence Comparison <span class="badge badge-ai">{html.escape(str(ai.get('model', 'gemini')))}</span></div>
          {f"<p><strong>Shared pattern:</strong> {html.escape(str(ai['pattern_summary']))}</p>" if ai.get('pattern_summary') else ""}
          {f"<p><strong>Top-Rank takeaway:</strong> {html.escape(str(ai['pro_takeaway']))}</p>" if ai.get('pro_takeaway') else ""}
        </div>
        """

    match_cards: list[str] = []
    for idx, m in enumerate(result.get("matches", []), 1):
        who_html = format_matchup_players_html(
            str(m["quark_id"]),
            int(m["perspective"]),
            str(m["self_char_name"]),
            str(m["opp_char_name"]),
            data_dir=clips_dir.parent,
            precomputed=m,
        )
        m_title = (
            f"<strong>#{idx} <span class='sim-tag'>Match {m['similarity_pct']}%</span>:</strong> "
            f"{who_html} &middot; "
            f"{html.escape(m['action_coarse'])} (HP {m['net_hp_swing']:+.2f}, Out {m['outcome_value']:+.2f})"
        )
        match_cards.append(
            _clip_card(
                m_title,
                m["quark_id"],
                int(m["mid_gf"]),
                int(m["start_gf"]),
                int(m["end_gf"]),
                int(m["perspective"]),
                m["label"],
                is_query=False,
            )
        )

    note_html = f'<div class="note-pill">Note: {html.escape(result["note"])}</div>' if result.get("note") else ""
    s_player = result.get("self_player_name") or q.get("self_player_name") or f"P{q.get('perspective', 1)}"
    o_player = result.get("opp_player_name") or q.get("opp_player_name") or ("P2" if q.get("perspective", 1) == 1 else "P1")
    topic_part = f" · {result['note']}" if result.get("note") else ""
    title = (
        f"{s_player} ({q['self_char_name']}) vs {o_player} ({q['opp_char_name']}) "
        f"· R{q.get('round_id', '?')}{topic_part}"
    )

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title)}</title>
<style>
  :root {{
    --bg: #0b1120; --surface: #111827; --card: #172033; --border: #263248;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; --ok: #22c55e; --danger: #f43f5e;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); padding: 24px; }}
  header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 14px; }}
  h1 {{ margin: 0; font-size: 1.3rem; }}
  .sub {{ color: var(--muted); font-size: 0.88rem; margin-top: 4px; }}
  .note-pill {{ display: inline-block; margin-top: 8px; padding: 4px 10px; border-radius: 999px; background: rgba(56, 189, 248, 0.14); border: 1px solid rgba(56, 189, 248, 0.35); color: #bae6fd; font-size: 0.82rem; }}
  .query-wrap {{ max-width: 560px; margin-bottom: 20px; }}
  .ai-box {{ background: rgba(56, 189, 248, 0.08); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 8px; padding: 12px 14px; margin-bottom: 20px; font-size: 0.88rem; line-height: 1.48; }}
  .ai-box p {{ margin: 5px 0 0 0; }}
  .ai-title {{ font-weight: 700; color: #bae6fd; margin-bottom: 4px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }}
  .clip-box {{ background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 12px; display: flex; flex-direction: column; }}
  .clip-query {{ border-color: rgba(244, 63, 94, 0.6); }}
  .clip-match {{ border-color: rgba(34, 197, 94, 0.45); }}
  .clip-meta {{ font-size: 0.82rem; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; gap: 6px; flex-wrap: wrap; }}
  .sim-tag {{ color: #7dd3fc; font-weight: 700; }}
  video {{ width: 100%; border-radius: 6px; background: #000; display: block; }}
  .no-video {{ height: 180px; display: flex; align-items: center; justify-content: center; background: #090d16; border-radius: 6px; color: var(--muted); font-size: 0.82rem; padding: 12px; text-align: center; }}
  .clip-actions {{ margin-top: auto; padding-top: 8px; display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 0.78rem; color: var(--muted); }}
  .btn {{ background: #1e293b; color: var(--text); border: 1px solid #334155; border-radius: 6px; padding: 6px 10px; cursor: pointer; font-size: 0.78rem; }}
  .btn:hover {{ border-color: var(--accent); color: #fff; }}
  .badge {{ font-size: 0.7rem; padding: 2px 6px; border-radius: 999px; font-weight: 600; }}
  .badge-ok {{ background: rgba(34, 197, 94, 0.18); color: #4ade80; }}
  .badge-muted {{ background: rgba(148, 163, 184, 0.15); color: var(--muted); }}
  .badge-ai {{ background: rgba(56, 189, 248, 0.2); color: #7dd3fc; }}
{TAKEOVER_UI_CSS}
</style>
</head>
<body>
  <header>
    <div>
      <div style="margin-bottom:6px;"><a href="/#insights" style="color:var(--accent);text-decoration:none;font-size:0.82rem;">&larr; Fightcade Lab Insights</a></div>
      <h1>{html.escape(title)}</h1>
      <div class="sub">
        QuarkId <code>{html.escape(str(q.get('quark_id', '')))}</code> (gf{q.get('start_gf', '?')}..{q.get('end_gf', '?')}) &middot;
        Tier: <code>{html.escape(result['retrieval_tier'])}</code> &middot;
        Scanned {result['rounds_scanned']} candidate rounds across {result['corpus_size']:,} indexed corpus states &middot;
        Found {len(result.get('matches', []))} similar sequences (videos paused by default)
      </div>
      {note_html}
    </div>
    <div>
      <button class="btn" onclick="pauseAll()">Pause All Videos</button>
    </div>
  </header>

  <div class="query-wrap">{query_card}</div>
  {ai_html}
  <h2>Most Similar Corpus Interactions</h2>
  <div class="grid">
    {''.join(match_cards) if match_cards else '<p class="sub">No similar sequences passed the similarity threshold. Try <code>--tier same_self_char</code> or <code>--max-norm-dist 0.42</code>.</p>'}
  </div>

<script>
function pauseAll() {{
  document.querySelectorAll('video').forEach(v => v.pause());
}}
function copyFromData(btn) {{
  const text = btn.getAttribute('data-copy') || '';
  const showDone = (msg) => {{
    const orig = btn.dataset.origLabel || btn.textContent;
    btn.dataset.origLabel = orig;
    btn.textContent = msg;
    setTimeout(() => {{ btn.textContent = orig; }}, 1400);
  }};
  const fallbackCopy = () => {{
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try {{ ok = document.execCommand('copy'); }} catch {{ ok = false; }}
    document.body.removeChild(ta);
    showDone(ok ? 'Copied!' : 'Copy failed');
  }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(() => showDone('Copied!')).catch(fallbackCopy);
  }} else {{
    fallbackCopy();
  }}
}}
{TAKEOVER_UI_JS}
</script>
</body>
</html>
"""
    out_html.write_text(page, encoding="utf-8")
    return out_html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Semantically search the corpus for a manually selected replay sequence.")
    parser.add_argument("--selection-id", default=None, help="Bookmark ID from data/selections/bookmarks.jsonl")
    parser.add_argument("--latest", action="store_true", help="Use the most recent bookmark from bookmarks.jsonl")
    parser.add_argument("--list-bookmarks", action="store_true", help="List saved manual bookmarks and exit")
    parser.add_argument("--target", type=Path, default=None, help="Direct target Parquet path (if not using bookmark)")
    parser.add_argument("--perspective", type=int, choices=[1, 2], default=1)
    parser.add_argument("--start-gf", type=int, default=None, help="Start game_frame (if not using bookmark)")
    parser.add_argument("--end-gf", type=int, default=None, help="End game_frame (if not using bookmark)")
    parser.add_argument("--note", default="", help="Optional note/description for the sequence")
    parser.add_argument("--parquet-dir", type=Path, default=Path("data/parquet"))
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--tier", choices=["exact_matchup", "same_self_char", "universal"], default=None)
    parser.add_argument("--max-norm-dist", type=float, default=0.36)
    parser.add_argument("--include-self", action="store_true", help="Allow matches from the same replay")
    parser.add_argument("--render-clips", action="store_true", help="Render MP4 clips + instant .fs/.inputs for query & matches")
    parser.add_argument("--clip-workers", type=int, default=3)
    parser.add_argument("--summarize", action="store_true", help="Add Gemini multimodal comparison summary")
    parser.add_argument("--out-name", default=None, help="Optional custom output filename stem (without extension)")
    parser.add_argument("--out-dir", type=Path, default=Path("data/reports"))
    args = parser.parse_args(argv)

    data_root = args.parquet_dir.resolve().parent
    bm_path = data_root / "selections" / "bookmarks.jsonl"

    if args.list_bookmarks:
        bms = load_bookmarks(bm_path)
        if not bms:
            print("No manual bookmarks saved yet. Run .\\scripts\\annotate.ps1 -QuarkId <id> to mark sequences.")
            return 0
        for i, bm in enumerate(bms, 1):
            print(f"{i:2d}. {bm['id']} | gf{bm['start_gf']}..gf{bm['end_gf']} ({bm['duration_s']}s) | note='{bm.get('note', '')}'")
        return 0

    bookmark: dict[str, Any] | None = None
    if args.selection_id or args.latest:
        bookmark = resolve_bookmark(selection_id=args.selection_id, latest=args.latest, bookmarks_path=bm_path)
        qid = str(bookmark["quark_id"])
        target_pq = args.target or find_parquet_for_quark(qid, data_dir=data_root)
        if target_pq is None:
            raise FileNotFoundError(
                f"Telemetry Parquet not found for {qid} in data/personal/ or data/parquet/. "
                f"Ingest it once via scripts/run_replay.ps1 + csv_to_parquet.py to enable semantic corpus search "
                f"(note: instant Replay Takeover via scripts/takeover.ps1 -SelectionId {bookmark['id']} works right now without Parquet!)."
            )
        persp = int(bookmark["perspective"])
        start_gf = int(bookmark["start_gf"])
        end_gf = int(bookmark["end_gf"])
        sel_id = str(bookmark["id"])
        note = args.note or str(bookmark.get("note", ""))
    else:
        if args.target is None or args.start_gf is None or args.end_gf is None:
            parser.error("Provide either (--selection-id / --latest) or (--target, --start-gf, --end-gf).")
        target_pq = args.target
        persp = args.perspective
        start_gf = args.start_gf
        end_gf = args.end_gf
        sel_id = f"{target_pq.stem}_p{persp}_gf{start_gf}_{end_gf}"
        note = args.note

    t0 = time.perf_counter()
    result = search_similar_sequences(
        target_pq,
        perspective=persp,
        start_gf=start_gf,
        end_gf=end_gf,
        parquet_dir=args.parquet_dir,
        top_k=args.top_k,
        tier_override=args.tier,
        max_norm_dist=args.max_norm_dist,
        exclude_self=not args.include_self,
        selection_id=sel_id,
        note=note,
    )
    t_search = time.perf_counter() - t0

    clips_dir = data_root / "clips"
    savestates_dir = data_root / "savestates"

    if args.render_clips:
        q = result["query"]
        half_dur = max(120, int((q["end_gf"] - q["start_gf"]) // 2) + 90)
        reqs = [
            ClipRequest(
                quark_id=q["quark_id"],
                target_frame=int(q["mid_gf"]),
                label=f"QUERY: {q['label']}",
                pre_frames=half_dur,
                post_frames=half_dur,
                savestate_lead=max(90, int(q["mid_gf"]) - int(q["start_gf"])),
            )
        ]
        for m in result["matches"]:
            m_half = max(120, int((m["end_gf"] - m["start_gf"]) // 2) + 90)
            reqs.append(
                ClipRequest(
                    quark_id=m["quark_id"],
                    target_frame=int(m["mid_gf"]),
                    label=f"MATCH {m['similarity_pct']}%: {m['label']}",
                    pre_frames=m_half,
                    post_frames=m_half,
                    savestate_lead=max(90, int(m["mid_gf"]) - int(m["start_gf"])),
                )
            )
        render_clips_batch(
            reqs,
            clips_dir=clips_dir,
            savestates_dir=savestates_dir,
            max_workers=args.clip_workers,
        )

    if args.summarize:
        summarize_sequence_search(
            result,
            clips_dir=clips_dir,
            cache_path=args.out_dir / "gemini_summary_cache.json",
        )

    enrich_sequence_search_metadata(result, data_dir=data_root)
    stem = args.out_name or build_sequence_search_stem(result, data_dir=data_root)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / f"{stem}.json"
    html_path = args.out_dir / f"{stem}.html"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    build_sequence_search_html(
        result,
        html_path,
        clips_dir=clips_dir,
        savestates_dir=savestates_dir,
        bookmark=bookmark,
    )
    index_path = build_reports_index_html(args.out_dir)

    print(
        f"Semantic search finished in {t_search:.2f}s | tier={result['retrieval_tier']} "
        f"| scanned={result['rounds_scanned']} rounds | matches={len(result['matches'])}"
    )
    for i, m in enumerate(result["matches"], 1):
        print(
            f"  {i}. [{m['similarity_pct']:2d}%] {m['quark_id']} P{m['perspective']} gf{m['start_gf']}..{m['end_gf']} "
            f"| {m['label']} | act={m['action_coarse']} HP={m['net_hp_swing']:+.2f}"
        )
    print(f"Wrote search JSON  : {json_path}")
    print(f"Wrote search HTML  : {html_path}")
    print(f"Updated reports hub: {index_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
