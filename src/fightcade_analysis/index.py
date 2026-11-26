"""Milestone 2 Layer 3: Hierarchical FAISS Context Window indexing & LORO coherence evaluation.

Includes tactical-regime compatibility gating and scale-free normalized RMS distance (`norm_dist`)
so Stage 3 coaching retrieval stays tightly aligned on spacing, corner pressure, and attacker/defender roles.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import faiss
import numpy as np
import pandas as pd

from fightcade_analysis.features import CORE_FEATURE_COLUMNS, extract_player_features
from fightcade_analysis.segmentation import SegmentationConfig, extract_decision_points

# Feature-group weights applied to each timestep of the Context Window before FAISS L2 search.
# Emphasizes geometry, corner pressure, knockdown/airborne/superfreeze, and attacker/defender polarity
# while dialing down HP/stun so identical tactical setups at different health totals stay tightly grouped.
DEFAULT_FEATURE_WEIGHTS: dict[str, float] = {
    "dx_norm": 3.2,
    "dy_norm": 1.8,
    "self_y_norm": 2.0,
    "opp_y_norm": 2.0,
    "self_corner_prox": 3.0,
    "opp_corner_prox": 3.0,
    "self_vx_norm": 1.1,
    "self_vy_norm": 1.1,
    "opp_vx_norm": 1.1,
    "opp_vy_norm": 1.1,
    "self_hp_norm": 0.2,
    "opp_hp_norm": 0.2,
    "self_meter_norm": 0.35,
    "opp_meter_norm": 0.35,
    "self_stun_norm": 0.25,
    "opp_stun_norm": 0.25,
    "self_crouching": 0.7,
    "opp_crouching": 0.7,
    "self_knockdown": 4.0,
    "opp_knockdown": 4.0,
    "self_airborne": 2.8,
    "opp_airborne": 2.8,
    "self_attacking": 2.0,
    "opp_attacking": 2.0,
    "self_blocking": 2.4,
    "opp_blocking": 2.4,
    "self_in_combo": 2.2,
    "opp_in_combo": 2.2,
    "in_freeze": 0.3,
    "self_superfreeze": 4.0,
    "opp_superfreeze": 4.0,
    "self_has_proj": 2.0,
    "opp_has_proj": 2.0,
}


def classify_tactical_regime(row: pd.Series | dict[str, Any]) -> str:
    """Assign a coarse tactical regime bucket at `t0` to prevent role/state flips during retrieval."""
    if int(row.get("t0_opp_sf", 0)) == 1:
        return "superfreeze_def"
    if int(row.get("t0_self_sf", 0)) == 1:
        return "superfreeze_off"
    if int(row.get("t0_self_kd", 0)) == 1:
        return "wakeup_def"
    if int(row.get("t0_opp_kd", 0)) == 1:
        return "oki_off"
    s_air = int(row.get("t0_self_air", 0)) == 1
    o_air = int(row.get("t0_opp_air", 0)) == 1
    if o_air and not s_air:
        return "antiair_def"
    if s_air and not o_air:
        return "jumpin_off"
    if s_air and o_air:
        return "air_to_air"
    s_blk = int(row.get("t0_self_blk", 0)) == 1
    o_blk = int(row.get("t0_opp_blk", 0)) == 1
    s_atk = int(row.get("t0_self_atk", 0)) == 1
    o_atk = int(row.get("t0_opp_atk", 0)) == 1
    if s_blk or (o_atk and not s_atk):
        return "defense_pressure"
    if o_blk or (s_atk and not o_atk):
        return "offense_pressure"
    return "neutral"


def build_context_weight_vector(
    n_steps: int = 15,
    feature_weights: dict[str, float] | None = None,
    recency_power: float = 1.5,
) -> np.ndarray:
    """Build per-element weight vector of shape (n_steps * F,) emphasizing recent frames near t0."""
    fw = feature_weights or DEFAULT_FEATURE_WEIGHTS
    per_feat = np.array([fw.get(c, 1.0) for c in CORE_FEATURE_COLUMNS], dtype=np.float32)
    # Ramp temporal weight strongly toward t0, normalized to unit mean for stable distance scale
    raw_tw = np.linspace(0.4, 2.0, n_steps, dtype=np.float32) ** recency_power
    time_weights = raw_tw / float(np.mean(raw_tw))
    return (time_weights[:, None] * per_feat[None, :]).reshape(-1).astype(np.float32)


def apply_weights(vectors: np.ndarray, weight_vec: np.ndarray) -> np.ndarray:
    if len(vectors) == 0:
        return vectors.astype(np.float32)
    return np.ascontiguousarray(vectors * weight_vec[None, :], dtype=np.float32)


@dataclass
class CorpusDataset:
    meta: pd.DataFrame
    vectors: np.ndarray  # raw unweighted (N, D) float32


def build_corpus_from_parquets(
    parquet_paths: list[Path],
    config: SegmentationConfig | None = None,
    both_perspectives: bool = True,
) -> CorpusDataset:
    """Load Parquet replays, extract canonical features + decision points, and stack into one corpus."""
    cfg = config or SegmentationConfig()
    perspectives = (1, 2) if both_perspectives else (1,)
    metas: list[pd.DataFrame] = []
    vecs: list[np.ndarray] = []

    for path in parquet_paths:
        df = pd.read_parquet(path)
        if "quark_id" not in df.columns:
            df["quark_id"] = path.stem
        for p in perspectives:
            feats = extract_player_features(df, perspective=p)
            m_df, c_vecs = extract_decision_points(feats, config=cfg)
            if len(m_df) > 0:
                metas.append(m_df)
                vecs.append(c_vecs)

    if not metas:
        n_steps = cfg.context_frames // cfg.context_step
        return CorpusDataset(pd.DataFrame(), np.empty((0, n_steps * len(CORE_FEATURE_COLUMNS)), dtype=np.float32))

    return CorpusDataset(
        meta=pd.concat(metas, ignore_index=True),
        vectors=np.vstack(vecs).astype(np.float32),
    )


def _classify_regimes_vectorized(meta: pd.DataFrame) -> np.ndarray:
    """Vectorized equivalent of `classify_tactical_regime` across all corpus rows."""
    n = len(meta)
    if n == 0:
        return np.array([], dtype=object)

    def _col(name: str) -> np.ndarray:
        if name in meta.columns:
            return meta[name].to_numpy() == 1
        return np.zeros(n, dtype=bool)

    o_sf = _col("t0_opp_sf")
    s_sf = _col("t0_self_sf")
    s_kd = _col("t0_self_kd")
    o_kd = _col("t0_opp_kd")
    s_air = _col("t0_self_air")
    o_air = _col("t0_opp_air")
    s_blk = _col("t0_self_blk")
    o_blk = _col("t0_opp_blk")
    s_atk = _col("t0_self_atk")
    o_atk = _col("t0_opp_atk")

    conditions = [
        o_sf,
        s_sf,
        s_kd,
        o_kd,
        o_air & (~s_air),
        s_air & (~o_air),
        s_air & o_air,
        s_blk | (o_atk & (~s_atk)),
        o_blk | (s_atk & (~o_atk)),
    ]
    choices = [
        "superfreeze_def",
        "superfreeze_off",
        "wakeup_def",
        "oki_off",
        "antiair_def",
        "jumpin_off",
        "air_to_air",
        "defense_pressure",
        "offense_pressure",
    ]
    return np.select(conditions, choices, default="neutral").astype(object)


CORPUS_CACHE_VERSION = "v2"


def _corpus_cache_key(
    parquet_paths: list[Path],
    config: SegmentationConfig,
    both_perspectives: bool,
) -> str:
    file_sig = []
    for p in sorted(parquet_paths, key=lambda x: x.name):
        st = p.stat()
        file_sig.append([p.name, st.st_size, int(st.st_mtime)])
    payload = {
        "v": CORPUS_CACHE_VERSION,
        "both_perspectives": bool(both_perspectives),
        "config": asdict(config),
        "features": list(CORE_FEATURE_COLUMNS),
        "files": file_sig,
    }
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def load_or_build_corpus(
    parquet_paths: list[Path],
    *,
    config: SegmentationConfig | None = None,
    both_perspectives: bool = True,
    cache_dir: Path | None = None,
    force_rebuild: bool = False,
) -> tuple[CorpusDataset, bool]:
    """Load precomputed PELT corpus from disk cache or build & persist it.

    Returns:
        (CorpusDataset, from_cache_bool)
    """
    cfg = config or SegmentationConfig()
    if cache_dir is None:
        if parquet_paths:
            cache_dir = parquet_paths[0].resolve().parent.parent / "index"
        else:
            cache_dir = Path("data/index")

    key = _corpus_cache_key(parquet_paths, cfg, both_perspectives)
    meta_path = cache_dir / f"corpus_{key}.parquet"
    vecs_path = cache_dir / f"corpus_{key}.npy"

    if not force_rebuild and meta_path.exists() and vecs_path.exists():
        meta_df = pd.read_parquet(meta_path)
        vectors = np.load(vecs_path).astype(np.float32, copy=False)
        if len(meta_df) == len(vectors):
            return CorpusDataset(meta=meta_df, vectors=vectors), True

    corpus = build_corpus_from_parquets(parquet_paths, config=cfg, both_perspectives=both_perspectives)
    if len(corpus.meta) > 0:
        cache_dir.mkdir(parents=True, exist_ok=True)
        corpus.meta.to_parquet(meta_path, index=False, compression="zstd")
        np.save(vecs_path, corpus.vectors.astype(np.float32))
    return corpus, False


class HierarchicalContextIndex:
    """FAISS index with 3-tier matchup fallback: exact matchup -> same self_char -> universal."""

    def __init__(
        self,
        meta: pd.DataFrame,
        raw_vectors: np.ndarray,
        weight_vec: np.ndarray | None = None,
    ) -> None:
        self.meta = meta.reset_index(drop=True)
        self.raw_vectors = raw_vectors.astype(np.float32)
        n_feats = len(CORE_FEATURE_COLUMNS)
        n_steps = raw_vectors.shape[1] // n_feats if raw_vectors.ndim == 2 and raw_vectors.shape[1] > 0 else 15
        self.weight_vec = weight_vec if weight_vec is not None else build_context_weight_vector(n_steps=n_steps)
        self.weight_sq_sum = float(np.sum(self.weight_vec.astype(np.float64) ** 2))
        self.weighted_vectors = apply_weights(self.raw_vectors, self.weight_vec)

        if len(self.meta) > 0:
            self._regimes = _classify_regimes_vectorized(self.meta)
            self._dx_arr = self.meta["t0_dx_px"].to_numpy(dtype=np.float32)
            self._self_cnr_arr = self.meta["t0_self_corner"].to_numpy(dtype=np.float32)
            self._opp_cnr_arr = self.meta["t0_opp_corner"].to_numpy(dtype=np.float32)
            self._qid_arr = self.meta["quark_id"].to_numpy(dtype=object)
        else:
            self._regimes = np.array([], dtype=object)
            self._dx_arr = np.array([], dtype=np.float32)
            self._self_cnr_arr = np.array([], dtype=np.float32)
            self._opp_cnr_arr = np.array([], dtype=np.float32)
            self._qid_arr = np.array([], dtype=object)

        self.dim = self.weighted_vectors.shape[1]
        self._universal_index = self._make_index(self.weighted_vectors)
        self._self_char_indices: dict[int, tuple[faiss.IndexFlatL2, np.ndarray]] = {}
        self._matchup_indices: dict[tuple[int, int], tuple[faiss.IndexFlatL2, np.ndarray]] = {}

        if len(self.meta) > 0:
            for sc, grp in self.meta.groupby("self_char"):
                row_ids = grp.index.to_numpy(dtype=np.int64)
                self._self_char_indices[int(sc)] = (self._make_index(self.weighted_vectors[row_ids]), row_ids)

            for (sc, oc), grp in self.meta.groupby(["self_char", "opp_char"]):
                row_ids = grp.index.to_numpy(dtype=np.int64)
                self._matchup_indices[(int(sc), int(oc))] = (
                    self._make_index(self.weighted_vectors[row_ids]),
                    row_ids,
                )

    def _make_index(self, mat: np.ndarray) -> faiss.IndexFlatL2:
        idx = faiss.IndexFlatL2(self.dim)
        if len(mat) > 0:
            idx.add(np.ascontiguousarray(mat, dtype=np.float32))
        return idx

    def _search_tier(
        self,
        q: np.ndarray,
        index: faiss.IndexFlatL2,
        row_map: np.ndarray,
        *,
        k: int,
        exclude_quark_id: str | None,
        q_regime: str | None,
        q_dx: float | None,
        q_self_cnr: float | None,
        q_opp_cnr: float | None,
        max_norm_dist: float | None,
        max_dx_diff_px: float | None,
        max_corner_diff: float | None,
    ) -> pd.DataFrame:
        if index.ntotal == 0:
            return pd.DataFrame()

        has_filters = any(
            x is not None for x in (exclude_quark_id, q_regime, max_norm_dist, max_dx_diff_px, max_corner_diff)
        )
        fetch_k = min(index.ntotal, max(k * 16, 400) if has_filters else k)
        dists, local_ids = index.search(q, fetch_k)
        global_ids = row_map[local_ids[0]]

        denom = max(self.weight_sq_sum, 1e-6)
        out_rows: list[int] = []
        out_l2: list[float] = []
        out_norm: list[float] = []

        for gid_raw, dist_raw in zip(global_ids, dists[0]):
            gid = int(gid_raw)
            if gid < 0:
                continue
            if exclude_quark_id and self._qid_arr[gid] == exclude_quark_id:
                continue

            l2_val = max(0.0, float(dist_raw))
            norm_val = float(np.sqrt(l2_val / denom))
            if max_norm_dist is not None and norm_val > max_norm_dist:
                # Results from FAISS IndexFlatL2 are sorted ascending by distance
                break

            if q_regime is not None and self._regimes[gid] != q_regime:
                continue
            if max_dx_diff_px is not None and q_dx is not None:
                if abs(float(self._dx_arr[gid]) - q_dx) > max_dx_diff_px:
                    continue
            if max_corner_diff is not None and q_self_cnr is not None and q_opp_cnr is not None:
                if (
                    abs(float(self._self_cnr_arr[gid]) - q_self_cnr) > max_corner_diff
                    or abs(float(self._opp_cnr_arr[gid]) - q_opp_cnr) > max_corner_diff
                ):
                    continue

            out_rows.append(gid)
            out_l2.append(l2_val)
            out_norm.append(norm_val)
            if len(out_rows) >= k:
                break

        if not out_rows:
            return pd.DataFrame()

        res = self.meta.iloc[out_rows].copy().reset_index(drop=True)
        res.insert(0, "norm_dist", out_norm)
        res.insert(0, "l2_dist", out_l2)
        return res

    def query(
        self,
        raw_query_vec: np.ndarray,
        *,
        self_char: int | None = None,
        opp_char: int | None = None,
        k: int = 10,
        min_tier_samples: int = 25,
        exclude_quark_id: str | None = None,
        tier_override: str | None = None,
        query_meta: pd.Series | dict[str, Any] | None = None,
        require_same_regime: bool = False,
        max_norm_dist: float | None = None,
        max_dx_diff_px: float | None = None,
        max_corner_diff: float | None = None,
        min_quality_neighbors: int | None = None,
    ) -> tuple[pd.DataFrame, str]:
        """Search for top-K nearest Context Windows with automatic quality-aware hierarchical fallback.

        Returns:
            (neighbors_df with `l2_dist` and `norm_dist` columns, tier_used string)
        """
        q = np.ascontiguousarray((raw_query_vec.reshape(1, -1) * self.weight_vec[None, :]), dtype=np.float32)

        q_regime = classify_tactical_regime(query_meta) if (require_same_regime and query_meta is not None) else None
        q_dx = float(query_meta["t0_dx_px"]) if query_meta is not None and "t0_dx_px" in query_meta else None
        q_self_cnr = (
            float(query_meta["t0_self_corner"]) if query_meta is not None and "t0_self_corner" in query_meta else None
        )
        q_opp_cnr = (
            float(query_meta["t0_opp_corner"]) if query_meta is not None and "t0_opp_corner" in query_meta else None
        )

        def _eligible_count(row_ids: np.ndarray) -> int:
            if not exclude_quark_id:
                return len(row_ids)
            return int((self._qid_arr[row_ids] != exclude_quark_id).sum())

        # Build ordered candidate tier list
        tiers: list[tuple[str, faiss.IndexFlatL2, np.ndarray]] = []
        if tier_override == "universal":
            tiers.append(("universal", self._universal_index, np.arange(len(self.meta), dtype=np.int64)))
        else:
            if (
                tier_override in (None, "exact_matchup")
                and self_char is not None
                and opp_char is not None
                and (self_char, opp_char) in self._matchup_indices
                and _eligible_count(self._matchup_indices[(self_char, opp_char)][1]) >= min_tier_samples
            ):
                idx, rmap = self._matchup_indices[(self_char, opp_char)]
                tiers.append(("exact_matchup", idx, rmap))

            if (
                tier_override in (None, "same_self_char")
                and self_char is not None
                and self_char in self._self_char_indices
                and _eligible_count(self._self_char_indices[self_char][1]) >= min_tier_samples
            ):
                idx, rmap = self._self_char_indices[self_char]
                tiers.append(("same_self_char", idx, rmap))

            if tier_override is None or not tiers:
                tiers.append(("universal", self._universal_index, np.arange(len(self.meta), dtype=np.int64)))

        best_res = pd.DataFrame()
        best_tier = tiers[0][0] if tiers else "universal"
        min_needed = min_quality_neighbors if min_quality_neighbors is not None else 1

        for tier_name, idx, rmap in tiers:
            res = self._search_tier(
                q,
                idx,
                rmap,
                k=k,
                exclude_quark_id=exclude_quark_id,
                q_regime=q_regime,
                q_dx=q_dx,
                q_self_cnr=q_self_cnr,
                q_opp_cnr=q_opp_cnr,
                max_norm_dist=max_norm_dist,
                max_dx_diff_px=max_dx_diff_px,
                max_corner_diff=max_corner_diff,
            )
            if len(res) >= min_needed:
                return res, tier_name
            if len(res) > len(best_res):
                best_res = res
                best_tier = tier_name

        return best_res, best_tier


def evaluate_loro_coherence(
    corpus: CorpusDataset,
    *,
    k: int = 5,
    max_queries_per_replay: int = 60,
    tier_override: str | None = "universal",
    seed: int = 42,
) -> dict[str, Any]:
    """Leave-One-Replay-Out retrieval coherence evaluation (Layer 3)."""
    if len(corpus.meta) == 0:
        raise ValueError("Empty corpus")

    rng = np.random.default_rng(seed)
    index = HierarchicalContextIndex(corpus.meta, corpus.vectors)

    dx_errors: list[float] = []
    corner_errors: list[float] = []
    kd_agrees: list[float] = []
    air_agrees: list[float] = []
    outcome_stds: list[float] = []
    rand_outcome_stds: list[float] = []
    tier_counts: dict[str, int] = {}

    all_outcomes = corpus.meta["outcome_value"].to_numpy(dtype=np.float32)

    for qid, grp in corpus.meta.groupby("quark_id"):
        row_ids = grp.index.to_numpy()
        if len(row_ids) > max_queries_per_replay:
            row_ids = rng.choice(row_ids, size=max_queries_per_replay, replace=False)

        for rid in row_ids:
            q_row = corpus.meta.iloc[int(rid)]
            nbrs, tier = index.query(
                corpus.vectors[int(rid)],
                self_char=int(q_row["self_char"]),
                opp_char=int(q_row["opp_char"]),
                k=k,
                exclude_quark_id=str(qid),
                tier_override=tier_override,
            )
            if len(nbrs) < k:
                continue
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

            dx_errors.extend(np.abs(nbrs["t0_dx_px"].to_numpy() - float(q_row["t0_dx_px"])).tolist())
            corner_errors.extend(
                np.abs(nbrs["t0_self_corner"].to_numpy() - float(q_row["t0_self_corner"])).tolist()
            )
            kd_agrees.append(float((nbrs["t0_self_kd"].to_numpy() == int(q_row["t0_self_kd"])).mean()))
            air_agrees.append(float((nbrs["t0_self_air"].to_numpy() == int(q_row["t0_self_air"])).mean()))
            outcome_stds.append(float(nbrs["outcome_value"].std(ddof=0)))

            rand_idx = rng.integers(0, len(all_outcomes), size=k)
            rand_outcome_stds.append(float(all_outcomes[rand_idx].std(ddof=0)))

    return {
        "queries_evaluated": len(kd_agrees),
        "tier_counts": tier_counts,
        "median_dx_err_px": float(np.median(dx_errors)) if dx_errors else float("nan"),
        "mean_dx_err_px": float(np.mean(dx_errors)) if dx_errors else float("nan"),
        "mean_corner_err": float(np.mean(corner_errors)) if corner_errors else float("nan"),
        "self_kd_agreement": float(np.mean(kd_agrees)) if kd_agrees else float("nan"),
        "self_air_agreement": float(np.mean(air_agrees)) if air_agrees else float("nan"),
        "neighbor_outcome_std": float(np.mean(outcome_stds)) if outcome_stds else float("nan"),
        "random_outcome_std": float(np.mean(rand_outcome_stds)) if rand_outcome_stds else float("nan"),
    }
