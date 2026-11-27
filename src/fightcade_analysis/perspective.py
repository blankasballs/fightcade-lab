"""Single Perspective Boundary: Raw Engine Space (`P1` / `P2`) -> Viewer Space (`self` / `opp`).

Architectural invariant:
  1. Disk artifacts (`Parquet`, `manifest.json`, `hp_curve`, `markers`, `rNN_overlay.json`) store
     objective raw `P1` / `P2` facts (legacy manifests with baked `dmg_self`/`dmg_opp` are
     transparently normalized into raw `side: 1|2` on read).
  2. ALL `P1`/`P2` <-> `self`/`opp` projection happens strictly inside this module at read/API time.
  3. Downstream UI (`mobile_ui.py`) and reports consume pre-projected `self` / `opp` structures
     with zero ad-hoc `if perspective == 1` branching.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from fightcade_analysis.characters import pretty_char
from fightcade_analysis.naming import format_rank_letter, lookup_replay_metadata

Side = Literal[1, 2]
T = TypeVar("T")

_MATCH_GAP_FRAMES: int = 800  # ~13.3s between distinct FT2 matches (intra-match round gaps are ~7-9s)


def normalize_side(value: Any, *, default: Side = 1) -> Side:
    """Validate and return canonical side `1` or `2`."""
    try:
        v = int(value)
        if v == 1:
            return 1
        if v == 2:
            return 2
    except (TypeError, ValueError):
        pass
    return default


def other_side(side: Side) -> Side:
    return 2 if side == 1 else 1


def is_placeholder_name(name: Any) -> bool:
    s = str(name or "").strip()
    return not s or s.upper() in ("P1", "P2")


@dataclass(frozen=True)
class SidePair(Generic[T]):
    """An objective `(p1, p2)` pair with canonical viewer-perspective projection."""

    p1: T
    p2: T

    def for_side(self, side: Side) -> T:
        return self.p1 if side == 1 else self.p2

    def self_val(self, viewer: Side) -> T:
        return self.p1 if viewer == 1 else self.p2

    def opp_val(self, viewer: Side) -> T:
        return self.p2 if viewer == 1 else self.p1

    def split(self, viewer: Side) -> tuple[T, T]:
        """Return `(self_val, opp_val)` for `viewer`."""
        return (self.p1, self.p2) if viewer == 1 else (self.p2, self.p1)

    def swap(self) -> SidePair[T]:
        return SidePair(self.p2, self.p1)


def resolve_viewer_side(
    *,
    username: str | None = None,
    p1_name: Any = None,
    p2_name: Any = None,
    explicit_side: Any = None,
    fallback_side: Any = None,
) -> Side:
    """Authoritative viewer-side resolution (single source of truth).

    Precedence:
      1. `explicit_side` (1 or 2) when explicitly requested (e.g. Flip Perspective button)
      2. Exact case-insensitive match of `username` against `p1_name` / `p2_name`
      3. `fallback_side` (1 or 2, e.g. from `quark:served` or stored manifest)
      4. Default `1`
    """
    if explicit_side in (1, 2, "1", "2"):
        return normalize_side(explicit_side)

    u = str(username or "").strip().lower()
    if u:
        p1 = str(p1_name or "").strip().lower()
        p2 = str(p2_name or "").strip().lower()
        if p2 == u and p1 != u:
            return 2
        if p1 == u and p2 != u:
            return 1

    if fallback_side in (1, 2, "1", "2"):
        return normalize_side(fallback_side)

    return 1


def normalize_raw_marker(marker: dict[str, Any], *, legacy_perspective: Side = 1) -> dict[str, Any]:
    """Normalize either a raw (`kind`+`side`) or legacy (`dmg_self`/`dmg_opp`) marker to canonical raw form.

    Canonical raw fields:
      - `gf`: deterministic `game_frame`
      - `offset_f`: frame offset from round `start_gf`
      - `kind`: `"dmg" | "kd" | "super" | "select_mark"`
      - `side`: `1 | 2` (for `"dmg"`, the side that TOOK the damage; for `"kd"`, the side knocked down;
        for `"super"` / `"select_mark"`, the side that triggered it)
      - `value`: numeric magnitude / side
      - `note`: optional text (for `select_mark`)
    """
    gf = int(marker.get("gf", 0))
    offset_f = int(marker.get("offset_f", 0))
    value = marker.get("value", 0)

    # Already canonical raw format
    if marker.get("kind") in ("dmg", "kd", "super", "select_mark") and marker.get("side") in (1, 2):
        out: dict[str, Any] = {
            "gf": gf,
            "offset_f": offset_f,
            "kind": str(marker["kind"]),
            "side": normalize_side(marker["side"]),
            "value": value,
        }
        if marker.get("note"):
            out["note"] = str(marker["note"])
        return out

    # Transparent upgrade from legacy perspective-baked markers
    mtype = str(marker.get("type") or "")
    lp = normalize_side(legacy_perspective)
    opp = other_side(lp)

    if mtype == "dmg_self":
        return {"gf": gf, "offset_f": offset_f, "kind": "dmg", "side": lp, "value": value}
    if mtype == "dmg_opp":
        return {"gf": gf, "offset_f": offset_f, "kind": "dmg", "side": opp, "value": value}
    if mtype == "kd_self":
        return {"gf": gf, "offset_f": offset_f, "kind": "kd", "side": lp, "value": lp}
    if mtype == "kd_opp":
        return {"gf": gf, "offset_f": offset_f, "kind": "kd", "side": opp, "value": opp}
    if mtype == "super":
        s = normalize_side(value, default=lp)
        return {"gf": gf, "offset_f": offset_f, "kind": "super", "side": s, "value": s}
    if mtype == "select_mark":
        s = normalize_side(marker.get("side"), default=lp)
        return {
            "gf": gf,
            "offset_f": offset_f,
            "kind": "select_mark",
            "side": s,
            "value": 1,
            "note": str(marker.get("label") or marker.get("note") or "In-game Select Mark"),
        }

    return {
        "gf": gf,
        "offset_f": offset_f,
        "kind": str(marker.get("kind") or mtype or "note"),
        "side": normalize_side(marker.get("side"), default=lp),
        "value": value,
        "note": str(marker.get("label") or marker.get("note") or ""),
    }


def project_marker(raw_marker: dict[str, Any], viewer_side: Side) -> dict[str, Any]:
    """Project a canonical raw marker (`kind` + `side`) into viewer space (`self` / `opp`)."""
    vs = normalize_side(viewer_side)
    kind = str(raw_marker.get("kind") or "")
    side = normalize_side(raw_marker.get("side"), default=vs)
    is_self = (side == vs)
    actor = "self" if is_self else "opp"
    val = raw_marker.get("value", 0)

    if kind == "dmg":
        mtype = "dmg_self" if is_self else "dmg_opp"
        label = f"{'Took' if is_self else 'Dealt'} -{int(val)} HP"
    elif kind == "kd":
        mtype = "kd_self" if is_self else "kd_opp"
        label = "You KD" if is_self else "Opp KD"
    elif kind == "super":
        mtype = "super"
        label = "Your Super" if is_self else "Opp Super"
    elif kind == "select_mark":
        mtype = "select_mark"
        label = str(raw_marker.get("note") or "In-game Select Mark")
    else:
        mtype = kind or "note"
        label = str(raw_marker.get("note") or mtype)

    return {
        "gf": int(raw_marker.get("gf", 0)),
        "offset_f": int(raw_marker.get("offset_f", 0)),
        "kind": kind,
        "side": side,
        "actor": actor,
        "type": mtype,
        "label": label,
        "value": val,
    }


def project_hp_curve(raw_hp_curve: list[list[Any]], viewer_side: Side) -> list[list[Any]]:
    """Project raw `[offset_f, p1_hp_pct, p2_hp_pct]` points into viewer `[offset_f, self_hp_pct, opp_hp_pct]`."""
    vs = normalize_side(viewer_side)
    if vs == 1:
        return [[pt[0], pt[1], pt[2]] for pt in raw_hp_curve if len(pt) >= 3]
    return [[pt[0], pt[2], pt[1]] for pt in raw_hp_curve if len(pt) >= 3]


def ordered_distinct_chars(rounds: list[dict[str, Any]], side: Side) -> list[str]:
    """Return ordered distinct character names played by `side` (`1` or `2`) across `rounds`."""
    cid_key = "p1_char" if side == 1 else "p2_char"
    name_key = "p1_char_name" if side == 1 else "p2_char_name"
    seen: set[str] = set()
    out: list[str] = []
    for r in rounds:
        name = str(r.get(name_key) or "").strip()
        if not name and r.get(cid_key) is not None:
            name = pretty_char(int(r[cid_key]))
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def extract_p1_p2_from_manifest(manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract raw `(p1_info, p2_info)` from a raw or legacy manifest dict without losing P2 data."""
    p1_obj = manifest.get("p1") if isinstance(manifest.get("p1"), dict) else {}
    p2_obj = manifest.get("p2") if isinstance(manifest.get("p2"), dict) else {}

    legacy_persp = normalize_side(manifest.get("perspective"), default=1)
    legacy_self_name = str(manifest.get("self_player_name") or "").strip()
    legacy_opp_name = str(manifest.get("opp_player_name") or "").strip()
    legacy_self_rk = str(manifest.get("self_rank_letter") or "").strip()
    legacy_opp_rk = str(manifest.get("opp_rank_letter") or "").strip()

    if legacy_persp == 1:
        fb_p1_name, fb_p2_name = legacy_self_name, legacy_opp_name
        fb_p1_rk, fb_p2_rk = legacy_self_rk, legacy_opp_rk
    else:
        fb_p2_name, fb_p1_name = legacy_self_name, legacy_opp_name
        fb_p2_rk, fb_p1_rk = legacy_self_rk, legacy_opp_rk

    p1_name = str(p1_obj.get("name") or manifest.get("p1_player_name") or "").strip()
    if is_placeholder_name(p1_name) and not is_placeholder_name(fb_p1_name):
        p1_name = fb_p1_name
    p2_name = str(p2_obj.get("name") or manifest.get("p2_player_name") or "").strip()
    if is_placeholder_name(p2_name) and not is_placeholder_name(fb_p2_name):
        p2_name = fb_p2_name

    p1_rk = str(p1_obj.get("rank_letter") or manifest.get("p1_rank_letter") or fb_p1_rk or "").strip()
    p2_rk = str(p2_obj.get("rank_letter") or manifest.get("p2_rank_letter") or fb_p2_rk or "").strip()

    rounds = manifest.get("rounds") or []
    p1_chars = list(p1_obj.get("chars") or manifest.get("p1_chars") or ordered_distinct_chars(rounds, 1))
    p2_chars = list(p2_obj.get("chars") or manifest.get("p2_chars") or ordered_distinct_chars(rounds, 2))
    p1_char_name = str(p1_obj.get("char_name") or manifest.get("p1_char_name") or (p1_chars[0] if p1_chars else ""))
    p2_char_name = str(p2_obj.get("char_name") or manifest.get("p2_char_name") or (p2_chars[0] if p2_chars else ""))
    p1_cid = int(p1_obj.get("char_id", manifest.get("p1_char", 0)) or 0)
    p2_cid = int(p2_obj.get("char_id", manifest.get("p2_char", 0)) or 0)

    return (
        {"side": 1, "name": p1_name, "rank_letter": p1_rk, "char_id": p1_cid, "char_name": p1_char_name, "chars": p1_chars},
        {"side": 2, "name": p2_name, "rank_letter": p2_rk, "char_id": p2_cid, "char_name": p2_char_name, "chars": p2_chars},
    )


def group_rounds_into_matches(
    rounds: list[dict[str, Any]],
    *,
    viewer_side: Side = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group chronological raw rounds into FT2 matches and project W/L + scores for `viewer_side`."""
    if not rounds:
        return [], []

    vs = normalize_side(viewer_side)
    annotated: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []

    match_id = 0
    match_round = 0
    p1_rw = 0
    p2_rw = 0
    cur_match_rounds: list[dict[str, Any]] = []
    p1_set_wins = 0
    p2_set_wins = 0

    def _finalize() -> None:
        nonlocal p1_set_wins, p2_set_wins
        if not cur_match_rounds:
            return
        first = cur_match_rounds[0]
        p1_cid = int(first.get("p1_char", 0))
        p2_cid = int(first.get("p2_char", 0))
        cnames = SidePair(
            str(first.get("p1_char_name") or pretty_char(p1_cid)),
            str(first.get("p2_char_name") or pretty_char(p2_cid)),
        )
        rw_pair = SidePair(p1_rw, p2_rw)

        winner: int = 1 if p1_rw > p2_rw else (2 if p2_rw > p1_rw else 0)
        if winner == 1:
            p1_set_wins += 1
        elif winner == 2:
            p2_set_wins += 1

        set_pair = SidePair(p1_set_wins, p2_set_wins)
        self_cname, opp_cname = cnames.split(vs)
        self_rw, opp_rw = rw_pair.split(vs)
        run_self, run_opp = set_pair.split(vs)

        matches.append(
            {
                "match_id": match_id,
                "p1_char": p1_cid,
                "p2_char": p2_cid,
                "p1_char_name": cnames.p1,
                "p2_char_name": cnames.p2,
                "self_char_name": self_cname,
                "opp_char_name": opp_cname,
                "p1_rounds_won": p1_rw,
                "p2_rounds_won": p2_rw,
                "self_rounds_won": self_rw,
                "opp_rounds_won": opp_rw,
                "winner": winner,
                "self_won": (winner == vs) if winner in (1, 2) else None,
                "running_p1_score": p1_set_wins,
                "running_p2_score": p2_set_wins,
                "running_self_score": run_self,
                "running_opp_score": run_opp,
                "round_ids": [int(x["round_id"]) for x in cur_match_rounds],
                "round_count": len(cur_match_rounds),
                "duration_s": round(sum(float(x.get("duration_s", 0.0)) for x in cur_match_rounds), 2),
            }
        )

    prev: dict[str, Any] | None = None
    for raw in rounds:
        r = dict(raw)
        start_new = False
        if prev is None:
            start_new = True
        else:
            gap = int(r.get("start_gf", 0)) - int(prev.get("end_gf", 0))
            char_changed = (
                int(r.get("p1_char", 0)) != int(prev.get("p1_char", 0))
                or int(r.get("p2_char", 0)) != int(prev.get("p2_char", 0))
            )
            if p1_rw >= 2 or p2_rw >= 2 or gap >= _MATCH_GAP_FRAMES or char_changed:
                start_new = True

        if start_new:
            _finalize()
            match_id += 1
            match_round = 0
            p1_rw = 0
            p2_rw = 0
            cur_match_rounds = []

        match_round += 1
        r["match_id"] = match_id
        r["match_round"] = match_round
        w = int(r.get("winner", 0))
        r["self_won"] = (w == vs) if w in (1, 2) else None
        if w == 1:
            p1_rw += 1
        elif w == 2:
            p2_rw += 1

        cur_match_rounds.append(r)
        annotated.append(r)
        prev = r

    _finalize()
    return annotated, matches


def project_manifest(
    raw_manifest: dict[str, Any],
    *,
    viewer_side: Any = None,
    username: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    """Project any raw (or legacy) replay manifest into 100% consistent viewer space (`self` / `opp`).

    Guarantees:
      - `hp_curve` is ALWAYS `[offset_f, self_hp_pct, opp_hp_pct]` (viewer-projected!)
      - `markers` are ALWAYS projected to `actor="self"|"opp"`, `type`, and `label` for `viewer_side`
      - `self` and `opp` objects + flat aliases (`self_player_name`, `self_chars`, `set_self_wins`, ...)
        are strictly derived through `SidePair` with zero P1 assumptions.
    """
    out = dict(raw_manifest)
    qid = str(out.get("quark_id") or "")
    legacy_persp = normalize_side(out.get("perspective"), default=1)

    p1_info, p2_info = extract_p1_p2_from_manifest(out)

    # Refresh placeholder P1/P2 names & ranks from cached Fightcade metadata if available
    if data_dir is not None and qid:
        meta = lookup_replay_metadata(qid, data_dir=data_dir)
        if meta:
            if is_placeholder_name(p1_info["name"]) and not is_placeholder_name(meta.get("p1_name")):
                p1_info["name"] = str(meta["p1_name"]).strip()
            if is_placeholder_name(p2_info["name"]) and not is_placeholder_name(meta.get("p2_name")):
                p2_info["name"] = str(meta["p2_name"]).strip()
            if not p1_info["rank_letter"] and meta.get("p1_rank") is not None:
                p1_info["rank_letter"] = format_rank_letter(meta.get("p1_rank"))
            if not p2_info["rank_letter"] and meta.get("p2_rank") is not None:
                p2_info["rank_letter"] = format_rank_letter(meta.get("p2_rank"))

    eff_side = resolve_viewer_side(
        username=username,
        p1_name=p1_info["name"],
        p2_name=p2_info["name"],
        explicit_side=viewer_side,
        fallback_side=legacy_persp,
    )

    p1_info["name"] = p1_info["name"] or "P1"
    p2_info["name"] = p2_info["name"] or "P2"
    players = SidePair(p1_info, p2_info)
    self_p, opp_p = players.split(eff_side)

    raw_rounds = out.get("rounds") or []
    grouped_rounds, match_groups = group_rounds_into_matches(raw_rounds, viewer_side=eff_side)

    projected_rounds: list[dict[str, Any]] = []
    rep_dir = (data_dir.resolve() / "replays" / qid) if (data_dir is not None and qid) else None
    for r in grouped_rounds:
        pr = dict(r)
        # Always normalize markers to raw (handling legacy manifests cleanly) then project for eff_side
        raw_markers = [normalize_raw_marker(m, legacy_perspective=legacy_persp) for m in (r.get("markers") or [])]
        pr["markers"] = [project_marker(rm, eff_side) for rm in raw_markers]

        # Project hp_curve strictly from raw `[offset_f, p1_hp, p2_hp]` -> `[offset_f, self_hp, opp_hp]`.
        # Always preserve `hp_curve_raw` so re-projecting an already-projected manifest is 100% idempotent!
        raw_curve = r.get("hp_curve_raw") if isinstance(r.get("hp_curve_raw"), list) else (r.get("hp_curve") or [])
        pr["hp_curve_raw"] = raw_curve
        pr["hp_curve"] = project_hp_curve(raw_curve, eff_side)
        pr["hp_curve_space"] = "viewer"

        c_pair = SidePair(
            str(r.get("p1_char_name") or pretty_char(int(r.get("p1_char", 0) or 0))),
            str(r.get("p2_char_name") or pretty_char(int(r.get("p2_char", 0) or 0))),
        )
        pr["self_char_name"], pr["opp_char_name"] = c_pair.split(eff_side)

        if rep_dir is not None:
            rid = int(r.get("round_id", 0))
            ov_name = f"r{rid:02d}_overlay.json"
            if (rep_dir / ov_name).is_file():
                pr["overlay_url"] = f"/replays/{qid}/{ov_name}"

        projected_rounds.append(pr)

    set_self_wins = int(match_groups[-1]["running_self_score"]) if match_groups else 0
    set_opp_wins = int(match_groups[-1]["running_opp_score"]) if match_groups else 0

    out["perspective"] = eff_side
    out["viewer_side"] = eff_side
    out["p1"] = p1_info
    out["p2"] = p2_info
    out["self"] = {**self_p, "wins": set_self_wins}
    out["opp"] = {**opp_p, "wins": set_opp_wins}

    # Backward-compatible flat fields derived strictly from SidePair
    out["p1_char"] = p1_info["char_id"]
    out["p2_char"] = p2_info["char_id"]
    out["p1_char_name"] = p1_info["char_name"]
    out["p2_char_name"] = p2_info["char_name"]
    out["p1_chars"] = p1_info["chars"]
    out["p2_chars"] = p2_info["chars"]
    out["self_char"] = self_p["char_id"]
    out["opp_char"] = opp_p["char_id"]
    out["self_char_name"] = self_p["char_name"]
    out["opp_char_name"] = opp_p["char_name"]
    out["self_chars"] = self_p["chars"]
    out["opp_chars"] = opp_p["chars"]
    out["self_player_name"] = self_p["name"]
    out["opp_player_name"] = opp_p["name"]
    out["self_rank_letter"] = self_p["rank_letter"]
    out["opp_rank_letter"] = opp_p["rank_letter"]
    out["match_count"] = len(match_groups)
    out["set_self_wins"] = set_self_wins
    out["set_opp_wins"] = set_opp_wins
    out["matches"] = match_groups
    out["rounds"] = projected_rounds

    if data_dir is not None and qid:
        rc_dir = data_dir.resolve() / "replay_cache" / qid
        has_rc = rc_dir.is_dir() and any(p.stat().st_size > 100_000 for p in rc_dir.glob("r*_gf*.fs"))
        out["has_overlay_data"] = any(bool(r.get("overlay_url")) for r in projected_rounds)
        out["can_generate_overlays"] = has_rc

    return out


def swap_raw_manifest_sides(raw_manifest: dict[str, Any]) -> dict[str, Any]:
    """Swap P1 <-> P2 across an entire raw manifest (used by property-based symmetry tests)."""
    p1_info, p2_info = extract_p1_p2_from_manifest(raw_manifest)
    swapped_p1 = {**p2_info, "side": 1}
    swapped_p2 = {**p1_info, "side": 2}
    legacy_persp = normalize_side(raw_manifest.get("perspective"), default=1)

    swapped_rounds: list[dict[str, Any]] = []
    for r in raw_manifest.get("rounds") or []:
        sr = dict(r)
        sr["p1_char"], sr["p2_char"] = r.get("p2_char", 0), r.get("p1_char", 0)
        sr["p1_char_name"], sr["p2_char_name"] = r.get("p2_char_name", ""), r.get("p1_char_name", "")
        w = int(r.get("winner", 0))
        sr["winner"] = 2 if w == 1 else (1 if w == 2 else 0)
        sr["hp_curve"] = [[pt[0], pt[2], pt[1]] for pt in (r.get("hp_curve") or []) if len(pt) >= 3]
        raw_ms = [normalize_raw_marker(m, legacy_perspective=legacy_persp) for m in (r.get("markers") or [])]
        swapped_ms: list[dict[str, Any]] = []
        for m in raw_ms:
            sm = dict(m)
            sm["side"] = other_side(normalize_side(m["side"]))
            if sm["kind"] in ("kd", "super"):
                sm["value"] = sm["side"]
            swapped_ms.append(sm)
        sr["markers"] = swapped_ms
        swapped_rounds.append(sr)

    return {
        "version": raw_manifest.get("version", 2),
        "quark_id": raw_manifest.get("quark_id", ""),
        "perspective": other_side(legacy_persp),
        "p1": swapped_p1,
        "p2": swapped_p2,
        "p1_char": swapped_p1["char_id"],
        "p2_char": swapped_p2["char_id"],
        "p1_char_name": swapped_p1["char_name"],
        "p2_char_name": swapped_p2["char_name"],
        "p1_chars": swapped_p1["chars"],
        "p2_chars": swapped_p2["chars"],
        "round_count": len(swapped_rounds),
        "rounds": swapped_rounds,
    }
