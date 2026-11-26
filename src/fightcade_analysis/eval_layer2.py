"""Run Layer 2 PELT breakpoint & leakage evaluation across a directory of Parquet replays."""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import pandas as pd

from fightcade_analysis.features import extract_player_features
from fightcade_analysis.segmentation import SegmentationConfig, evaluate_segmentation

CHAR_NAMES: dict[int, str] = {
    0: "gill", 1: "alex", 2: "ryu", 3: "yun", 4: "dudley", 5: "necro",
    6: "hugo", 7: "ibuki", 8: "elena", 9: "oro", 10: "yang", 11: "ken",
    12: "sean", 13: "urien", 14: "gouki", 15: "gill2", 16: "chunli",
    17: "makoto", 18: "q", 19: "twelve", 20: "remy",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect corpus character breakdown & evaluate Layer 2 PELT.")
    parser.add_argument(
        "--parquet-dir",
        type=Path,
        default=Path("fightcade-analysis/data/parquet"),
        help="Directory containing replay Parquet files",
    )
    args = parser.parse_args(argv)

    files = sorted(args.parquet_dir.glob("*.parquet"))
    if not files:
        print(f"No Parquet files found in {args.parquet_dir}")
        return 1

    char_rounds: Counter[str] = Counter()
    matchup_rounds: Counter[str] = Counter()
    total_frames = 0
    total_rounds = 0
    all_p1_feats: list[pd.DataFrame] = []

    for f in files:
        df = pd.read_parquet(f)
        total_frames += len(df)
        for rid, rdf in df.groupby("round_id"):
            total_rounds += 1
            c1 = CHAR_NAMES.get(int(rdf["p1_char"].iloc[0]), f"id{int(rdf['p1_char'].iloc[0])}")
            c2 = CHAR_NAMES.get(int(rdf["p2_char"].iloc[0]), f"id{int(rdf['p2_char'].iloc[0])}")
            char_rounds[c1] += 1
            char_rounds[c2] += 1
            pair = " vs ".join(sorted([c1, c2]))
            matchup_rounds[pair] += 1

        feats = extract_player_features(df, perspective=1)
        # Ensure unique round_id across files for aggregate evaluation
        feats["round_id"] = feats["round_id"].astype(str) + "_" + f.stem
        all_p1_feats.append(feats)

    print(f"=== Corpus Summary: {len(files)} Parquet sets | {total_rounds} rounds | {total_frames:,} frames ===")
    print("Character rounds:", dict(char_rounds.most_common()))
    print("Matchup rounds:", dict(matchup_rounds.most_common()))
    print()

    combined = pd.concat(all_p1_feats, ignore_index=True)
    print("=== Layer 2 PELT Sweep (all rounds, P1 perspective) ===")
    print(f"{'pen':>5} {'lead':>4} | {'bkps/rnd':>8} {'total':>5} | {'dmg':>5} {'kd':>5} {'wake':>5} {'cb':>5} | {'pre_dmg':>7} {'leak':>6}")
    for pen in (12.0, 18.0, 28.0):
        for lead in (0, 8, 15):
            m = evaluate_segmentation(combined, SegmentationConfig(penalty=pen, lead_frames=lead))
            print(
                f"{pen:5.1f} {lead:4d} | {m['mean_bkps_per_round']:8.1f} {m['total_breakpoints']:5d} | "
                f"{m['oracle_recall']['damage']:5.2f} {m['oracle_recall']['knockdown']:5.2f} "
                f"{m['oracle_recall']['wakeup']:5.2f} {m['oracle_recall']['combo_start']:5.2f} | "
                f"{m['oracle_pre_recall']['damage']:7.2f} {m['context_hp_leak_rate']:6.2f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
