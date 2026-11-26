"""Convert raw 60 Hz CSV telemetry from dump_telemetry.lua into validated Parquet."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA_DTYPES: dict[str, str] = {
    "emu_frame": "int32",
    "game_frame": "uint32",
    "round_id": "uint8",
    "in_match": "uint8",
    "match_state": "uint8",
    "timer": "uint8",
    "screen_x": "int16",
    "p1_char": "uint16",
    "p1_sa": "uint8",
    "p1_x": "int16",
    "p1_y": "int16",
    "p1_facing": "int8",
    "p1_hp": "uint8",
    "p1_meter_gauge": "uint8",
    "p1_meter_max_gauge": "uint8",
    "p1_meter_stock": "uint8",
    "p1_meter_max_stock": "uint8",
    "p1_stun": "uint8",
    "p1_stun_max": "uint8",
    "p1_action": "uint32",
    "p1_anim": "uint16",
    "p1_posture": "uint8",
    "p1_attacking": "uint8",
    "p1_blocking": "uint8",
    "p1_combo": "uint8",
    "p1_freeze": "uint8",
    "p1_superfreeze": "uint8",
    "p1_proj_count": "uint8",
    "p1_input_mask": "uint16",
    "p2_char": "uint16",
    "p2_sa": "uint8",
    "p2_x": "int16",
    "p2_y": "int16",
    "p2_facing": "int8",
    "p2_hp": "uint8",
    "p2_meter_gauge": "uint8",
    "p2_meter_max_gauge": "uint8",
    "p2_meter_stock": "uint8",
    "p2_meter_max_stock": "uint8",
    "p2_stun": "uint8",
    "p2_stun_max": "uint8",
    "p2_action": "uint32",
    "p2_anim": "uint16",
    "p2_posture": "uint8",
    "p2_attacking": "uint8",
    "p2_blocking": "uint8",
    "p2_combo": "uint8",
    "p2_freeze": "uint8",
    "p2_superfreeze": "uint8",
    "p2_proj_count": "uint8",
    "p2_input_mask": "uint16",
}


def validate_dataframe(df: pd.DataFrame) -> list[str]:
    warnings: list[str] = []
    if df.empty:
        warnings.append("Telemetry DataFrame is empty (0 rows).")
        return warnings

    if not df["emu_frame"].is_monotonic_increasing:
        warnings.append("emu_frame is not monotonically increasing.")

    rounds = int(df["round_id"].max())
    if rounds < 1:
        warnings.append("No active rounds detected (round_id max < 1).")

    if df["p1_char"].nunique() > 1 or df["p2_char"].nunique() > 1:
        warnings.append(
            f"Multiple character IDs seen in single replay: "
            f"p1={df['p1_char'].unique().tolist()}, p2={df['p2_char'].unique().tolist()}"
        )

    return warnings


def convert_csv_to_parquet(
    csv_path: Path,
    parquet_path: Path,
    quark_id: str | None = None,
    delete_csv: bool = False,
) -> dict[str, object]:
    raw_df = pd.read_csv(csv_path)
    dropped_rows = 0
    if raw_df.isna().any().any():
        before = len(raw_df)
        raw_df = raw_df.dropna()
        dropped_rows = before - len(raw_df)

    # Backward compatibility: fill newly added optional RAM columns with 0 if reading an older CSV
    for col, dtype in SCHEMA_DTYPES.items():
        if col not in raw_df.columns:
            raw_df[col] = 0

    df = raw_df[list(SCHEMA_DTYPES.keys())].astype(SCHEMA_DTYPES)
    warnings = validate_dataframe(df)
    if dropped_rows > 0:
        warnings.append(f"Dropped {dropped_rows} incomplete/NaN trailing row(s) from CSV.")

    if quark_id:
        df.insert(0, "quark_id", quark_id)

    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, parquet_path, compression="zstd")

    if delete_csv and csv_path.exists():
        csv_path.unlink()

    return {
        "rows": len(df),
        "rounds": int(df["round_id"].max()) if not df.empty else 0,
        "p1_char": int(df["p1_char"].iloc[0]) if not df.empty else -1,
        "p2_char": int(df["p2_char"].iloc[0]) if not df.empty else -1,
        "warnings": warnings,
        "parquet_bytes": parquet_path.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert raw Lua telemetry CSV to Parquet.")
    parser.add_argument("csv_path", type=Path, help="Input CSV path")
    parser.add_argument("parquet_path", type=Path, help="Output Parquet path")
    parser.add_argument("--quark-id", type=str, default=None, help="Optional Fightcade quarkid metadata")
    parser.add_argument("--delete-csv", action="store_true", help="Remove raw CSV after successful conversion")
    args = parser.parse_args(argv)

    summary = convert_csv_to_parquet(
        csv_path=args.csv_path,
        parquet_path=args.parquet_path,
        quark_id=args.quark_id,
        delete_csv=args.delete_csv,
    )
    print(
        f"Wrote {args.parquet_path} ({summary['rows']} rows, {summary['rounds']} rounds, "
        f"p1_char={summary['p1_char']}, p2_char={summary['p2_char']}, {summary['parquet_bytes']} bytes)"
    )
    for w in summary["warnings"]:
        print(f"WARNING: {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
