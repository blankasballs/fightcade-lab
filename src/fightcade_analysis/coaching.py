"""Stage 3: Personal Replay Diagnosis, Outcome Regret Scoring & Structured Role Labeling.

Analyzes a target replay against the indexed Fightcade corpus to surface high-leverage
decision points where the player's Outcome Value significantly trailed corpus EV:
    Regret = EV_corpus - ActualValue_user
Uses regime-gated, distance-bounded retrieval and closest-first proof ranking so alternative
clips closely mirror the user's exact spacing, corner, and attacker/defender situation.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fightcade_analysis.characters import CHAR_NAMES, PRETTY_CHAR_NAMES, pretty_char
from fightcade_analysis.features import extract_player_features
from fightcade_analysis.index import (
    HierarchicalContextIndex,
    build_corpus_from_parquets,
    classify_tactical_regime,
    load_or_build_corpus,
)
from fightcade_analysis.segmentation import SegmentationConfig, extract_decision_points


def describe_decision_point(row: pd.Series | dict[str, Any]) -> dict[str, str]:
    """Build a structured, unambiguous label: `{SelfChar} ({SelfRole}) vs {OppChar} ({OppRole}) — {Interaction}`."""
    sc = pretty_char(int(row["self_char"]))
    oc = pretty_char(int(row["opp_char"]))
    s_sa = int(row.get("self_sa", 0))
    o_sa = int(row.get("opp_sa", 0))

    s_cnr = float(row.get("t0_self_corner", 0.0)) >= 0.65
    o_cnr = float(row.get("t0_opp_corner", 0.0)) >= 0.65
    s_kd = int(row.get("t0_self_kd", 0)) == 1
    o_kd = int(row.get("t0_opp_kd", 0)) == 1
    s_air = int(row.get("t0_self_air", 0)) == 1
    o_air = int(row.get("t0_opp_air", 0)) == 1
    s_sf = int(row.get("t0_self_sf", 0)) == 1
    o_sf = int(row.get("t0_opp_sf", 0)) == 1
    s_proj = int(row.get("t0_self_proj", 0)) == 1
    o_proj = int(row.get("t0_opp_proj", 0)) == 1
    s_atk = int(row.get("t0_self_atk", 0)) == 1
    o_atk = int(row.get("t0_opp_atk", 0)) == 1
    s_blk = int(row.get("t0_self_blk", 0)) == 1
    o_blk = int(row.get("t0_opp_blk", 0)) == 1
    dx = float(row.get("t0_dx_px", 0.0))

    s_cnr_tag = ", Cornered" if s_cnr else ""
    o_cnr_tag = ", Cornered" if o_cnr else ""

    if o_sf:
        s_role = f"Defender{s_cnr_tag}"
        o_role = f"Attacker, Air SA{o_sa}" if o_air else f"Attacker, SA{o_sa}"
        interaction = "Super Art Defense"
    elif s_sf:
        s_role = f"Attacker, Air SA{s_sa}" if s_air else f"Attacker, SA{s_sa}"
        o_role = f"Defender{o_cnr_tag}"
        interaction = "Super Art Conversion"
    elif s_kd:
        s_role = f"Defender, Knockdown{s_cnr_tag}"
        o_role = "Attacker, Okizeme"
        interaction = "Corner Wakeup Defense" if s_cnr else "Midscreen Wakeup Defense"
    elif o_kd:
        s_role = "Attacker, Okizeme"
        o_role = f"Defender, Knockdown{o_cnr_tag}"
        interaction = "Corner Okizeme Setup" if o_cnr else "Midscreen Okizeme Setup"
    elif o_air and not s_air:
        s_role = f"Defender{s_cnr_tag}"
        o_role = "Attacker, Jump-in"
        interaction = "Corner Anti-Air Decision" if s_cnr else "Anti-Air Decision"
    elif s_air and not o_air:
        s_role = "Attacker, Jump-in"
        o_role = f"Defender{o_cnr_tag}"
        interaction = "Jump-in Approach"
    elif o_proj:
        s_role = f"Defender{s_cnr_tag}"
        o_role = "Attacker, Projectile"
        interaction = "Projectile Defense"
    elif s_proj:
        s_role = "Attacker, Projectile"
        o_role = f"Defender{o_cnr_tag}"
        interaction = "Projectile Pressure"
    elif s_blk or o_atk:
        s_role = f"Defender{s_cnr_tag}"
        o_role = "Attacker, Pressure"
        interaction = "Corner Blockstring Defense" if s_cnr else "Blockstring Defense"
    elif s_atk or o_blk:
        s_role = "Attacker, Pressure"
        o_role = f"Defender{o_cnr_tag}"
        interaction = "Corner Offense" if o_cnr else "Offense / Pressure"
    elif s_cnr:
        s_role = "Defender, Cornered"
        o_role = "Attacker, Corner Pressure"
        interaction = "Corner Escape"
    elif o_cnr:
        s_role = "Attacker, Corner Pressure"
        o_role = "Defender, Cornered"
        interaction = "Corner Lock"
    else:
        s_role = "Neutral"
        o_role = "Neutral"
        interaction = f"Neutral Footsies ({dx:.0f}px)"

    full_label = f"{sc} ({s_role}) vs {oc} ({o_role}) — {interaction}"
    return {
        "self_char_name": sc,
        "opp_char_name": oc,
        "self_role": s_role,
        "opp_role": o_role,
        "interaction": interaction,
        "label": full_label,
    }


def _weighted_ev(outcomes: np.ndarray, norm_dists: np.ndarray, sigma: float = 0.16) -> float:
    """Gaussian-kernel distance-weighted expected outcome value favoring tight tactical matches."""
    if len(outcomes) == 0:
        return 0.0
    weights = np.exp(-((norm_dists / max(sigma, 1e-4)) ** 2))
    w_sum = float(np.sum(weights))
    if w_sum <= 1e-12:
        return float(np.mean(outcomes))
    return float(np.sum(weights * outcomes) / w_sum)


def _similarity_pct(norm_dist: float) -> int:
    """Convert weighted RMS feature distance into an intuitive 0-100% similarity score."""
    return int(round(max(0.0, min(1.0, float(np.exp(-2.2 * norm_dist)))) * 100))


def _select_closest_proofs(
    grp: pd.DataFrame,
    *,
    actual_val: float,
    proofs_per_option: int,
) -> pd.DataFrame:
    """Rank proof clips primarily by closest tactical similarity (`norm_dist` ASC),
    preferring positive-outcome examples (that beat the user) over losing or distant outliers.
    """
    winning = grp[grp["outcome_value"] > max(actual_val, 0.0)]
    if len(winning) > 0:
        pool = winning
    else:
        better = grp[grp["outcome_value"] > actual_val]
        pool = better if len(better) > 0 else grp
    # Sort strictly closest-first, breaking close ties by better outcome
    ranked = pool.assign(_dist_bucket=(pool["norm_dist"] / 0.025).round()).sort_values(
        by=["_dist_bucket", "outcome_value", "norm_dist"],
        ascending=[True, False, True],
    )
    return ranked.drop(columns=["_dist_bucket"]).head(proofs_per_option)


def diagnose_replay(
    target_parquet: Path,
    corpus_index: HierarchicalContextIndex,
    *,
    perspective: int = 1,
    config: SegmentationConfig | None = None,
    k_neighbors: int = 25,
    top_n: int = 8,
    min_option_count: int = 2,
    min_frame_separation: int = 90,
    min_tier_samples: int = 20,
    tier_override: str | None = None,
    proofs_per_option: int = 2,
    require_same_regime: bool = True,
    max_norm_dist: float = 0.34,
    max_dx_diff_px: float = 95.0,
    max_corner_diff: float = 0.45,
    min_quality_neighbors: int = 8,
) -> dict[str, Any]:
    """Run Outcome Regret coaching diagnosis on one replay against a HierarchicalContextIndex."""
    cfg = config or SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    df = pd.read_parquet(target_parquet)
    quark_id = str(df["quark_id"].iloc[0]) if "quark_id" in df.columns and len(df) > 0 else target_parquet.stem

    feats = extract_player_features(df, perspective=perspective)
    q_meta, q_vecs = extract_decision_points(feats, config=cfg)

    if len(q_meta) == 0:
        return {
            "target_quark_id": quark_id,
            "perspective": perspective,
            "total_decision_points": 0,
            "moments": [],
        }

    scored: list[dict[str, Any]] = []
    for i in range(len(q_meta)):
        row = q_meta.iloc[i]
        nbrs, tier = corpus_index.query(
            q_vecs[i],
            self_char=int(row["self_char"]),
            opp_char=int(row["opp_char"]),
            k=k_neighbors,
            min_tier_samples=min_tier_samples,
            exclude_quark_id=quark_id,
            tier_override=tier_override,
            query_meta=row,
            require_same_regime=require_same_regime,
            max_norm_dist=max_norm_dist,
            max_dx_diff_px=max_dx_diff_px,
            max_corner_diff=max_corner_diff,
            min_quality_neighbors=min_quality_neighbors,
        )
        if len(nbrs) < min_option_count:
            continue

        actual_val = float(row["outcome_value"])
        ev_corpus = _weighted_ev(nbrs["outcome_value"].to_numpy(), nbrs["norm_dist"].to_numpy())
        regret = ev_corpus - actual_val
        median_norm_dist = float(np.median(nbrs["norm_dist"].to_numpy()))

        # Group corpus neighbors by Action Window coarse choice
        options: list[dict[str, Any]] = []
        for act_name, grp in nbrs.groupby("action_coarse"):
            if len(grp) < min_option_count:
                continue
            opt_ev = _weighted_ev(grp["outcome_value"].to_numpy(), grp["norm_dist"].to_numpy())
            opt_hp = float(grp["net_hp_swing"].mean())
            proof_grp = _select_closest_proofs(grp, actual_val=actual_val, proofs_per_option=proofs_per_option)
            proofs = []
            for _, pr in proof_grp.iterrows():
                pdesc = describe_decision_point(pr)
                nd = float(pr["norm_dist"])
                proofs.append(
                    {
                        "quark_id": str(pr["quark_id"]),
                        "perspective": int(pr["perspective"]),
                        "round_id": int(pr["round_id"]),
                        "emu_frame": int(pr["emu_frame"]),
                        "game_frame": int(pr["game_frame"]),
                        "self_char": int(pr["self_char"]),
                        "opp_char": int(pr["opp_char"]),
                        "self_char_name": pdesc["self_char_name"],
                        "opp_char_name": pdesc["opp_char_name"],
                        "label": pdesc["label"],
                        "regime": classify_tactical_regime(pr),
                        "action_coarse": str(pr["action_coarse"]),
                        "outcome_value": round(float(pr["outcome_value"]), 4),
                        "net_hp_swing": round(float(pr["net_hp_swing"]), 4),
                        "l2_dist": round(float(pr["l2_dist"]), 2),
                        "norm_dist": round(nd, 4),
                        "similarity_pct": _similarity_pct(nd),
                        "t0_dx_px": round(float(pr["t0_dx_px"]), 1),
                        "t0_self_corner": round(float(pr["t0_self_corner"]), 2),
                    }
                )
            options.append(
                {
                    "action_coarse": str(act_name),
                    "count": int(len(grp)),
                    "ev": round(opt_ev, 4),
                    "mean_hp_swing": round(opt_hp, 4),
                    "ev_gain_vs_user": round(opt_ev - actual_val, 4),
                    "best_similarity_pct": proofs[0]["similarity_pct"] if proofs else 0,
                    "proofs": proofs,
                }
            )

        if not options:
            continue

        options.sort(key=lambda x: x["ev"], reverse=True)
        best_opt_ev = options[0]["ev"]
        best_opt_regret = best_opt_ev - actual_val

        desc = describe_decision_point(row)
        scored.append(
            {
                "quark_id": quark_id,
                "perspective": int(perspective),
                "round_id": int(row["round_id"]),
                "emu_frame": int(row["emu_frame"]),
                "game_frame": int(row["game_frame"]),
                "self_char": int(row["self_char"]),
                "opp_char": int(row["opp_char"]),
                "self_sa": int(row["self_sa"]),
                "opp_sa": int(row["opp_sa"]),
                "label": desc["label"],
                "regime": classify_tactical_regime(row),
                "self_char_name": desc["self_char_name"],
                "opp_char_name": desc["opp_char_name"],
                "self_role": desc["self_role"],
                "opp_role": desc["opp_role"],
                "interaction": desc["interaction"],
                "retrieval_tier": tier,
                "neighbors_used": int(len(nbrs)),
                "median_norm_dist": round(median_norm_dist, 4),
                "match_similarity_pct": _similarity_pct(median_norm_dist),
                "user_action": str(row["action_coarse"]),
                "actual_value": round(actual_val, 4),
                "actual_hp_swing": round(float(row["net_hp_swing"]), 4),
                "ev_corpus": round(ev_corpus, 4),
                "regret": round(regret, 4),
                "best_option_ev": round(best_opt_ev, 4),
                "best_option_regret": round(best_opt_regret, 4),
                "t0_dx_px": round(float(row["t0_dx_px"]), 1),
                "t0_self_corner": round(float(row["t0_self_corner"]), 2),
                "t0_self_hp": round(float(row["t0_self_hp"]), 2),
                "t0_opp_hp": round(float(row["t0_opp_hp"]), 2),
                "options": options[:4],
            }
        )

    # Sort by highest Outcome Regret and deduplicate nearby frames in the same round
    scored.sort(key=lambda m: (m["regret"], m["best_option_regret"]), reverse=True)
    selected: list[dict[str, Any]] = []
    for cand in scored:
        if cand["regret"] <= 0:
            break
        too_close = any(
            (s["round_id"] == cand["round_id"]) and (abs(s["emu_frame"] - cand["emu_frame"]) < min_frame_separation)
            for s in selected
        )
        if too_close:
            continue
        selected.append(cand)
        if len(selected) >= top_n:
            break

    first_row = q_meta.iloc[0]
    return {
        "target_quark_id": quark_id,
        "perspective": int(perspective),
        "self_char": int(first_row["self_char"]),
        "self_char_name": pretty_char(int(first_row["self_char"])),
        "opp_char": int(first_row["opp_char"]),
        "opp_char_name": pretty_char(int(first_row["opp_char"])),
        "total_decision_points": int(len(q_meta)),
        "corpus_size": int(len(corpus_index.meta)),
        "moments": selected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnose a replay for Outcome Regret against the Parquet corpus.")
    parser.add_argument("--target", type=Path, required=True, help="Target replay Parquet file to diagnose")
    parser.add_argument("--perspective", type=int, choices=[1, 2], default=1, help="Player perspective (1 or 2)")
    parser.add_argument("--parquet-dir", type=Path, default=Path("data/parquet"))
    parser.add_argument("--top-n", type=int, default=6)
    parser.add_argument("--k", type=int, default=25)
    parser.add_argument("--max-norm-dist", type=float, default=0.34)
    parser.add_argument("--out-json", type=Path, default=None)
    args = parser.parse_args(argv)

    files = sorted(args.parquet_dir.glob("*.parquet"))
    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    corpus, _ = load_or_build_corpus(
        files,
        config=cfg,
        both_perspectives=True,
        cache_dir=args.parquet_dir.resolve().parent / "index",
    )
    index = HierarchicalContextIndex(corpus.meta, corpus.vectors)

    report = diagnose_replay(
        args.target,
        index,
        perspective=args.perspective,
        config=cfg,
        k_neighbors=args.k,
        top_n=args.top_n,
        max_norm_dist=args.max_norm_dist,
    )

    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Wrote coaching report JSON to {args.out_json}")

    print(
        f"=== Coaching Diagnosis: {report['target_quark_id']} "
        f"(P{report['perspective']} {report['self_char_name']} vs {report['opp_char_name']}) "
        f"| corpus={report['corpus_size']} decision points ==="
    )
    for idx, m in enumerate(report["moments"], 1):
        best = m["options"][0] if m["options"] else None
        best_str = (
            f"best={best['action_coarse']} (EV={best['ev']:+.2f}, n={best['count']}, sim={best['best_similarity_pct']}%)"
            if best
            else "n/a"
        )
        print(
            f"{idx}. [R{m['round_id']} gf{m['game_frame']}] {m['label']}\n"
            f"   Regret={m['regret']:+.2f} | You: {m['user_action']} ({m['actual_value']:+.2f}) "
            f"| Corpus EV={m['ev_corpus']:+.2f} ({m['retrieval_tier']}, sim={m['match_similarity_pct']}%) | {best_str}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
