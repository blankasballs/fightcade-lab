"""Property-based P1 <-> P2 symmetry & perspective boundary invariant tests.

Proves that `project_manifest(swap_raw_manifest_sides(raw), viewer_side=2)` is IDENTICAL
in every viewer-facing field (`self`, `opp`, `matches`, `rounds`, `hp_curve`, `markers`,
W/L, set scores, character lists) to `project_manifest(raw, viewer_side=1)`.
Any accidental P1 hardcoding anywhere in the perspective boundary fails these tests immediately.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import requests

from fightcade_analysis.match_catalog import list_matches
from fightcade_analysis.perspective import (
    SidePair,
    normalize_raw_marker,
    project_hp_curve,
    project_manifest,
    project_marker,
    resolve_viewer_side,
    swap_raw_manifest_sides,
)
from fightcade_analysis.server import create_server


def _sample_raw_manifest() -> dict:
    return {
        "version": 3,
        "quark_id": "1795558989575-1518",
        "perspective": 1,
        "p1": {
            "side": 1,
            "name": "player1",
            "rank_letter": "D",
            "char_id": 14,
            "char_name": "Akuma",
            "chars": ["Akuma"],
        },
        "p2": {
            "side": 2,
            "name": "Matito.",
            "rank_letter": "C",
            "char_id": 17,
            "char_name": "Ibuki",
            "chars": ["Makoto", "Ibuki"],
        },
        "round_count": 4,
        "rounds": [
            {
                "round_id": 1,
                "start_gf": 1000,
                "end_gf": 2400,
                "p1_char": 14,
                "p2_char": 19,
                "p1_char_name": "Akuma",
                "p2_char_name": "Makoto",
                "winner": 1,
                "duration_s": 23.3,
                "hp_curve": [[0, 1.0, 1.0], [300, 0.75, 0.50], [600, 0.75, 0.0]],
                "markers": [
                    {"gf": 1200, "offset_f": 200, "kind": "dmg", "side": 1, "value": 40},
                    {"gf": 1350, "offset_f": 350, "kind": "dmg", "side": 2, "value": 65},
                    {"gf": 1400, "offset_f": 400, "kind": "kd", "side": 2, "value": 2},
                    {"gf": 1500, "offset_f": 500, "kind": "super", "side": 1, "value": 1},
                ],
            },
            {
                "round_id": 2,
                "start_gf": 2850,
                "end_gf": 4200,
                "p1_char": 14,
                "p2_char": 19,
                "p1_char_name": "Akuma",
                "p2_char_name": "Makoto",
                "winner": 1,
                "duration_s": 22.5,
                "hp_curve": [[0, 1.0, 1.0], [400, 0.90, 0.0]],
                "markers": [
                    {"gf": 3100, "offset_f": 250, "kind": "dmg", "side": 2, "value": 90},
                ],
            },
            {
                "round_id": 3,
                "start_gf": 5600,
                "end_gf": 7000,
                "p1_char": 14,
                "p2_char": 17,
                "p1_char_name": "Akuma",
                "p2_char_name": "Ibuki",
                "winner": 2,
                "duration_s": 23.3,
                "hp_curve": [[0, 1.0, 1.0], [400, 0.0, 0.60]],
                "markers": [
                    {"gf": 5900, "offset_f": 300, "kind": "dmg", "side": 1, "value": 80},
                    {"gf": 5950, "offset_f": 350, "kind": "kd", "side": 1, "value": 1},
                    {"gf": 6000, "offset_f": 400, "kind": "super", "side": 2, "value": 2},
                ],
            },
            {
                "round_id": 4,
                "start_gf": 7450,
                "end_gf": 8800,
                "p1_char": 14,
                "p2_char": 17,
                "p1_char_name": "Akuma",
                "p2_char_name": "Ibuki",
                "winner": 2,
                "duration_s": 22.5,
                "hp_curve": [[0, 1.0, 1.0], [500, 0.0, 0.45]],
                "markers": [],
            },
        ],
    }


def test_side_pair_and_resolve_viewer_side() -> None:
    pair = SidePair("Alpha", "Beta")
    assert pair.split(1) == ("Alpha", "Beta")
    assert pair.split(2) == ("Beta", "Alpha")
    assert pair.swap().split(2) == pair.split(1)

    assert resolve_viewer_side(username="player1", p1_name="Oro_Rookie", p2_name="PLAYER1") == 2
    assert resolve_viewer_side(username="player1", p1_name="player1", p2_name="Oro_Rookie") == 1
    assert resolve_viewer_side(username="player1", p1_name="player1", p2_name="Oro_Rookie", explicit_side=2) == 2
    assert resolve_viewer_side(username="other", p1_name="A", p2_name="B", fallback_side=2) == 2


def test_manifest_exact_p1_p2_mirror_symmetry() -> None:
    """Core invariant: swapping P1<->P2 and viewing as P2 MUST produce identical viewer-space output as viewing as P1."""
    raw = _sample_raw_manifest()
    swapped = swap_raw_manifest_sides(raw)

    proj_as_p1 = project_manifest(raw, viewer_side=1)
    proj_as_p2 = project_manifest(swapped, viewer_side=2)

    # Top-level self/opp identity, chars, scores
    for k in (
        "self_player_name",
        "opp_player_name",
        "self_rank_letter",
        "opp_rank_letter",
        "self_char_name",
        "opp_char_name",
        "self_chars",
        "opp_chars",
        "set_self_wins",
        "set_opp_wins",
        "match_count",
    ):
        assert proj_as_p1[k] == proj_as_p2[k], f"Mismatch on top-level field {k}"

    assert proj_as_p1["self"]["name"] == proj_as_p2["self"]["name"] == "player1"
    assert proj_as_p1["opp"]["name"] == proj_as_p2["opp"]["name"] == "Matito."
    assert proj_as_p1["self"]["side"] == 1
    assert proj_as_p2["self"]["side"] == 2

    # Match breakdown symmetry
    assert len(proj_as_p1["matches"]) == len(proj_as_p2["matches"]) == 2
    for m1, m2 in zip(proj_as_p1["matches"], proj_as_p2["matches"]):
        for k in ("self_char_name", "opp_char_name", "self_rounds_won", "opp_rounds_won", "self_won", "running_self_score", "running_opp_score"):
            assert m1[k] == m2[k], f"Mismatch on match {m1['match_id']} field {k}"

    # Per-round HP curve, winner, chars, and EVERY timeline marker symmetry
    for r1, r2 in zip(proj_as_p1["rounds"], proj_as_p2["rounds"]):
        assert r1["self_won"] == r2["self_won"]
        assert r1["self_char_name"] == r2["self_char_name"]
        assert r1["opp_char_name"] == r2["opp_char_name"]
        assert r1["hp_curve"] == r2["hp_curve"]
        assert len(r1["markers"]) == len(r2["markers"])
        for mk1, mk2 in zip(r1["markers"], r2["markers"]):
            for k in ("gf", "offset_f", "kind", "actor", "type", "label"):
                assert mk1[k] == mk2[k], f"Mismatch on marker field {k}: {mk1} vs {mk2}"


def test_legacy_baked_manifest_reprojects_cleanly_when_flipped() -> None:
    """Existing legacy manifests on disk (with baked dmg_self/dmg_opp) must flip cleanly between P1 and P2."""
    legacy_mf = {
        "version": 1,
        "quark_id": "1795547282177-8026",
        "perspective": 2,
        "self_player_name": "player1",
        "opp_player_name": "Oro_Rookie",
        "p1_char": 18,
        "p2_char": 14,
        "p1_char_name": "Q",
        "p2_char_name": "Akuma",
        "rounds": [
            {
                "round_id": 1,
                "start_gf": 15214,
                "end_gf": 16778,
                "p1_char": 18,
                "p2_char": 14,
                "p1_char_name": "Q",
                "p2_char_name": "Akuma",
                "winner": 1,
                "hp_curve": [[0, 1.0, 1.0], [100, 0.80, 0.40]],
                "markers": [
                    {"gf": 15300, "offset_f": 86, "type": "dmg_self", "label": "Took -30 HP", "value": 30},
                    {"gf": 15400, "offset_f": 186, "type": "dmg_opp", "label": "Dealt -20 HP", "value": 20},
                ],
            }
        ],
    }

    # As P2 (player1): self=Akuma (0.40 HP), opp=Q (0.80 HP), lost R1
    as_p2 = project_manifest(legacy_mf, viewer_side=2)
    assert as_p2["self_player_name"] == "player1"
    assert as_p2["opp_player_name"] == "Oro_Rookie"
    assert as_p2["rounds"][0]["self_won"] is False
    assert as_p2["rounds"][0]["hp_curve"][1] == [100, 0.40, 0.80]
    assert as_p2["rounds"][0]["markers"][0]["type"] == "dmg_self"
    assert as_p2["rounds"][0]["markers"][1]["type"] == "dmg_opp"

    # Flip to P1 (Oro_Rookie): self=Q (0.80 HP), opp=Akuma (0.40 HP), won R1, dmg markers inverted!
    as_p1 = project_manifest(legacy_mf, viewer_side=1)
    assert as_p1["self_player_name"] == "Oro_Rookie"
    assert as_p1["opp_player_name"] == "player1"
    assert as_p1["rounds"][0]["self_won"] is True
    assert as_p1["rounds"][0]["hp_curve"][1] == [100, 0.80, 0.40]
    assert as_p1["rounds"][0]["markers"][0]["type"] == "dmg_opp"
    assert as_p1["rounds"][0]["markers"][0]["label"] == "Dealt -30 HP"
    assert as_p1["rounds"][0]["markers"][1]["type"] == "dmg_self"
    assert as_p1["rounds"][0]["markers"][1]["label"] == "Took -20 HP"

    # Idempotence: re-projecting an already-projected P2 manifest MUST NOT double-flip hp_curve!
    as_p2_again = project_manifest(as_p2, viewer_side=2)
    assert as_p2_again["rounds"][0]["hp_curve"][1] == [100, 0.40, 0.80]
    as_p1_from_p2 = project_manifest(as_p2, viewer_side=1)
    assert as_p1_from_p2["rounds"][0]["hp_curve"][1] == [100, 0.80, 0.40]


def test_http_manifest_endpoint_supports_live_perspective_flip(tmp_path: Path) -> None:
    qid = "1795558989575-1518"
    rep_dir = tmp_path / "replays" / qid
    rep_dir.mkdir(parents=True)
    (tmp_path / "reports").mkdir()
    (tmp_path / "clips").mkdir()
    (rep_dir / "manifest.json").write_text(json.dumps(_sample_raw_manifest()), encoding="utf-8")

    server = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        r1 = requests.get(f"{base}/api/replays/{qid}/manifest?perspective=1", timeout=5).json()["manifest"]
        r2 = requests.get(f"{base}/api/replays/{qid}/manifest?perspective=2", timeout=5).json()["manifest"]
        assert r1["self_player_name"] == "player1"
        assert r2["self_player_name"] == "Matito."
        assert r1["rounds"][0]["hp_curve"][1] == [300, 0.75, 0.50]
        assert r2["rounds"][0]["hp_curve"][1] == [300, 0.50, 0.75]
        assert r1["rounds"][0]["self_won"] is True
        assert r2["rounds"][0]["self_won"] is False
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=3)
