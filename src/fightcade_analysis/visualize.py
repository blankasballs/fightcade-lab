"""Milestone 2 Layer 4: 2D tactical schematic sheets for Query vs Top-K FAISS neighbors."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from fightcade_analysis.eval_layer2 import CHAR_NAMES
from fightcade_analysis.features import STAGE_MAX_X, STAGE_MIN_X
from fightcade_analysis.index import HierarchicalContextIndex, build_corpus_from_parquets
from fightcade_analysis.segmentation import SegmentationConfig


def _draw_tactical_card(ax: plt.Axes, row: pd.Series, title: str, is_query: bool = False) -> None:
    """Draw a compact canonical 2D tactical snapshot (side-normalized: self faces right)."""
    ax.set_xlim(-20, 420)
    ax.set_ylim(-15, 190)
    ax.set_facecolor("#181c24" if not is_query else "#1f2636")

    # Back wall at x=0 (self corner) and far wall at x=400 (approx visual reference)
    self_x = (1.0 - float(row["t0_self_corner"])) * 250.0
    opp_x = self_x + float(row["t0_dx_px"])
    self_y = 60.0 if int(row["t0_self_air"]) else 0.0
    opp_y = 60.0 if int(row["t0_opp_air"]) else 0.0

    # Ground + back wall lines
    ax.axhline(0, color="#667085", linewidth=1.2)
    ax.axvline(0, color="#ef4444", linestyle="--", linewidth=1.2, alpha=0.8)

    # Player markers
    s_color = "#38bdf8" if int(row["t0_self_kd"]) == 0 else "#f59e0b"
    o_color = "#f43f5e" if int(row["t0_opp_kd"]) == 0 else "#a855f7"
    ax.scatter([self_x], [self_y + 18], s=140, color=s_color, edgecolors="white", zorder=4)
    ax.scatter([opp_x], [opp_y + 18], s=140, color=o_color, edgecolors="white", zorder=4)

    sc = CHAR_NAMES.get(int(row["self_char"]), str(int(row["self_char"])))
    oc = CHAR_NAMES.get(int(row["opp_char"]), str(int(row["opp_char"])))
    s_tag = f"SELF:{sc}" + (" [KD]" if int(row["t0_self_kd"]) else "")
    o_tag = f"OPP:{oc}" + (" [KD]" if int(row["t0_opp_kd"]) else "")
    ax.text(self_x, self_y + 38, s_tag, color=s_color, fontsize=7.5, ha="center", fontweight="bold")
    ax.text(opp_x, opp_y + 38, o_tag, color=o_color, fontsize=7.5, ha="center", fontweight="bold")

    # HP bars at top
    ax.barh([168], [160 * float(row["t0_self_hp"])], left=[10], height=10, color="#22c55e")
    ax.barh([168], [160 * float(row["t0_opp_hp"])], left=[230], height=10, color="#ef4444")

    # Compact footer stats
    info = (
        f"dx={row['t0_dx_px']:.0f}px | cnr={row['t0_self_corner']:.2f} | "
        f"act={row['action_coarse']} | out={row['outcome_value']:+.2f}\n"
        f"{row['quark_id']} @ f{int(row['emu_frame'])} (R{int(row['round_id'])})"
    )
    ax.set_title(title, fontsize=8.5, color="#f8fafc", pad=4)
    ax.text(200, 118, info, color="#cbd5e1", fontsize=7.0, ha="center", va="center")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_edgecolor("#38bdf8" if is_query else "#334155")
        spine.set_linewidth(1.8 if is_query else 0.8)


def render_spotcheck_sheet(
    parquet_dir: Path,
    out_png: Path,
    *,
    num_queries: int = 5,
    k: int = 4,
    seed: int = 7,
) -> Path:
    files = sorted(parquet_dir.glob("*.parquet"))
    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    corpus = build_corpus_from_parquets(files, config=cfg, both_perspectives=True)
    index = HierarchicalContextIndex(corpus.meta, corpus.vectors)

    rng = np.random.default_rng(seed)
    # Pick diverse queries: mix cornered, knockdown, and neutral states from the first replay
    q_qid = str(corpus.meta["quark_id"].iloc[0])
    q_pool = corpus.meta[corpus.meta["quark_id"] == q_qid]

    chosen: list[int] = []
    for mask in [
        q_pool["t0_self_kd"] == 1,
        q_pool["t0_self_corner"] >= 0.7,
        q_pool["t0_opp_corner"] >= 0.7,
        q_pool["t0_self_air"] == 1,
    ]:
        sub = q_pool[mask]
        if len(sub) > 0:
            chosen.append(int(sub.index[int(rng.integers(0, len(sub)))]))
    while len(chosen) < num_queries and len(q_pool) > len(chosen):
        cand = int(q_pool.index[int(rng.integers(0, len(q_pool)))])
        if cand not in chosen:
            chosen.append(cand)
    chosen = chosen[:num_queries]

    fig, axes = plt.subplots(len(chosen), k + 1, figsize=(3.8 * (k + 1), 2.5 * len(chosen)), facecolor="#0f172a")
    if len(chosen) == 1:
        axes = np.expand_dims(axes, axis=0)

    for r_i, q_idx in enumerate(chosen):
        q_row = corpus.meta.iloc[q_idx]
        nbrs, tier = index.query(
            corpus.vectors[q_idx],
            self_char=int(q_row["self_char"]),
            opp_char=int(q_row["opp_char"]),
            k=k,
            exclude_quark_id=q_qid,
            tier_override="universal",
        )
        _draw_tactical_card(axes[r_i, 0], q_row, f"QUERY #{r_i + 1} ({tier})", is_query=True)
        for c_i in range(k):
            if c_i < len(nbrs):
                nr = nbrs.iloc[c_i]
                _draw_tactical_card(axes[r_i, c_i + 1], nr, f"Top-{c_i + 1} (d={nr['l2_dist']:.1f})")
            else:
                axes[r_i, c_i + 1].axis("off")

    fig.tight_layout(pad=0.8)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=140, facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_png


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Layer 4 schematic spot-check PNG for FAISS retrieval.")
    parser.add_argument("--parquet-dir", type=Path, default=Path("fightcade-analysis/data/parquet"))
    parser.add_argument("--out", type=Path, default=Path("fightcade-analysis/data/eval/retrieval_spotcheck.png"))
    parser.add_argument("--queries", type=int, default=5)
    parser.add_argument("--k", type=int, default=4)
    args = parser.parse_args(argv)

    out = render_spotcheck_sheet(args.parquet_dir, args.out, num_queries=args.queries, k=args.k)
    print(f"Saved visual spot-check sheet to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
