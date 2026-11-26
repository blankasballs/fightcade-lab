"""Offline unit tests for Fightcade replay metadata filtering, stream parsing, and manifest merging."""

from __future__ import annotations

import pytest

from fightcade_analysis.fetch_replays import (
    filter_quarks,
    merge_manifest_records,
    parse_fcade_stream_location,
)


def _q(qid: str, p1: str, r1: int, p2: str, r2: int, dur: float = 300.0, ranked: int = 3) -> dict:
    return {
        "quarkid": qid,
        "gameid": "sfiii3nr1",
        "duration": dur,
        "ranked": ranked,
        "players": [
            {"name": p1, "rank": r1, "score": 3},
            {"name": p2, "rank": r2, "score": 1},
        ],
    }


def test_parse_fcade_stream_location() -> None:
    assert (
        parse_fcade_stream_location("fcade://stream/fbneo/sfiii3nr1/1728857242828-5771.7,7100")
        == "quark:stream,sfiii3nr1,1728857242828-5771.7,7100"
    )
    with pytest.raises(ValueError):
        parse_fcade_stream_location("https://example.com/not-a-stream")


def test_filter_quarks_rank_duration_and_pair_diversity() -> None:
    items = [
        _q("q1", "Alice", 6, "Bob", 6, dur=500.0, ranked=5),
        _q("q2", "Bob", 6, "Alice", 6, dur=450.0, ranked=5),  # same pair, reversed order
        _q("q3", "Alice", 6, "Bob", 6, dur=400.0, ranked=5),  # 3rd of same pair -> should drop at max_per_pair=2
        _q("q4", "Charlie", 5, "Dave", 2, dur=300.0, ranked=3),  # Dave below min_rank=4
        _q("q5", "Eve", 5, "Frank", 5, dur=25.0, ranked=3),  # too short
        _q("q6", "Grace", 5, "Heidi", 5, dur=1800.0, ranked=10),  # too long if max_duration=900
        _q("q7", "Ivan", 5, "Judy", 4, dur=350.0, ranked=0),  # unranked
    ]

    kept = filter_quarks(
        items,
        ranked_only=True,
        min_rank=4,
        min_duration=60.0,
        max_duration=900.0,
        max_per_pair=2,
    )
    assert [x["quarkid"] for x in kept] == ["q1", "q2"]

    kept_any = filter_quarks(items, min_any_rank=5, min_duration=60.0, max_duration=900.0, max_per_pair=1)
    assert [x["quarkid"] for x in kept_any] == ["q1", "q4", "q7"]


def test_merge_manifest_preserves_cached_stream_targets() -> None:
    existing = {
        "raw": [_q("q1", "Alice", 6, "Bob", 6)],
        "summaries": [{"quarkid": "q1", "stream_target": "quark:stream,sfiii3nr1,q1.7,7100"}],
    }
    merged = merge_manifest_records(
        existing,
        new_raw=[_q("q1", "Alice", 6, "Bob", 6), _q("q2", "Eve", 5, "Frank", 5)],
        new_summaries=[
            {"quarkid": "q1", "stream_target": None},  # shouldn't clobber cached target
            {"quarkid": "q2", "stream_target": "quark:stream,sfiii3nr1,q2.7,7100"},
        ],
    )
    by_id = {s["quarkid"]: s for s in merged["summaries"]}
    assert by_id["q1"]["stream_target"] == "quark:stream,sfiii3nr1,q1.7,7100"
    assert by_id["q2"]["stream_target"] == "quark:stream,sfiii3nr1,q2.7,7100"
    assert len(merged["raw"]) == 2
