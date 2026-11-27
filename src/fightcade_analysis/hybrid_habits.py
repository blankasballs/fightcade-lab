"""Milestone 5 (ADR-001): Hybrid VLM-Grounded Personal Habit Discovery + RAM Trajectory Search.

Separates responsibilities by scale & reliability:
  Stage 1 (RAM Engine Truth):   Detect indisputable Hit-#1 / Throw / Superfreeze health-loss episodes
                                (`t_cause` strictly before impact, zero brittle semantic `if/elif` rules).
  Stage 2 (Pass-1 Vision):      Gemini Flash watches ONLY the top ~20-25 personal health-loss 6s clips
                                and classifies each into an established 9-category 3rd Strike taxonomy
                                with factual on-screen move descriptions.
  Stage 3 (RAM Corpus Search):  Keep the 121-set+ pro corpus 100% in RAM-space. Use `sequence_search`
                                on the pre-mistake setup window `[t_cause - 60 .. t_cause - 10]` to
                                retrieve pro solutions with positive aftermath, plus personal RAM
                                recurrence counts across all personal sets.
  Stage 4 (Pass-2 Vision Gate): Gemini Flash watches (Your Clip + Pro Candidate Clip) together and
                                strictly filters out RAM false positives (`is_relevant == True` only).
  Stage 5 (Verified Rollup):    Render phone-friendly HTML report onto the `:8765` Reports Hub with
                                1-to-1 aligned Coach Synthesis, strictly user-footage evidence cards,
                                separated Visually Verified Pro Solutions, and Takeover/Partner export.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import threading
import time
from typing import Any

import numpy as np
import pandas as pd

from fightcade_analysis.clip_renderer import (
    DEFAULT_DATA_DIR,
    DEFAULT_POST_FRAMES,
    DEFAULT_PRE_FRAMES,
    DEFAULT_SAVESTATE_LEAD,
    ClipRequest,
    render_clips_batch,
)
from fightcade_analysis.coaching import pretty_char
from fightcade_analysis.features import CORE_FEATURE_COLUMNS, extract_player_features
from fightcade_analysis.gemini_summary import (
    DEFAULT_MODEL_CHAIN,
    _call_gemini_json,
    _encode_mp4_part,
    _load_cache,
    _save_cache,
    load_gemini_api_key,
)
from fightcade_analysis.habit_rollup import discover_player_sets
from fightcade_analysis.index import HierarchicalContextIndex, load_or_build_corpus
from fightcade_analysis.naming import (
    build_reports_index_html,
    build_verified_habits_stem,
    format_matchup_players_html,
    resolve_replay_players_and_ranks,
)
from fightcade_analysis.segmentation import SegmentationConfig
from fightcade_analysis.sequence_search import (
    TRAJ_STEPS,
    _dominant_regime,
    _score_candidate_round,
    _sequence_weight_vector,
    resample_trajectory,
    search_similar_sequences,
)
from fightcade_analysis.takeover import (
    TAKEOVER_UI_CSS,
    TAKEOVER_UI_JS,
    render_takeover_buttons_html,
)

HABIT_TAXONOMY: dict[str, str] = {
    "missed_anti_air": "Missed Anti-Air",
    "got_anti_aired": "Got Anti-Aired",
    "wakeup_reversal_or_super_punished": "Hit by Wakeup Reversal / Super",
    "wakeup_defense_loss": "Wakeup Defense Loss",
    "opened_up_in_blockstring": "Opened Up in Blockstring",
    "got_thrown": "Got Thrown",
    "whiff_punished_or_counter_hit": "Whiff-Punished / Counter-Hit",
    "unsafe_move_punished": "Unsafe Move / Dropped Combo Punished",
    "other": "Other / Miscellaneous",
}

PASS1_VERSION = "hybrid_p1_v1"
PASS2_VERSION = "hybrid_p2_v1"
SYNTH_VERSION = "hybrid_synth_v1"


# ---------------------------------------------------------------------------
# Stage 1: Objective Engine Health-Loss Episode Detection (Zero Heuristics)
# ---------------------------------------------------------------------------

def detect_health_loss_episodes(
    df: pd.DataFrame,
    *,
    perspective: int = 1,
    quark_id: str = "",
    min_hp_loss: int = 14,
    combo_gap_frames: int = 25,
    max_superfreeze_backtrack: int = 75,
) -> list[dict[str, Any]]:
    """Find indisputable damage-taken episodes anchored strictly BEFORE Hit #1 / Superfreeze."""
    if df.empty:
        return []

    s_pre = "p1" if perspective == 1 else "p2"
    o_pre = "p2" if perspective == 1 else "p1"
    qid = quark_id or (str(df["quark_id"].iloc[0]) if "quark_id" in df.columns else "unknown")

    has_sf = f"{o_pre}_superfreeze" in df.columns
    episodes: list[dict[str, Any]] = []

    for round_id, r_raw in df.groupby("round_id", sort=True):
        r_df = r_raw[r_raw["in_match"] == 1].reset_index(drop=True)
        n = len(r_df)
        if n < 10:
            continue

        gf = r_df["game_frame"].to_numpy(dtype=np.int64)
        hp = r_df[f"{s_pre}_hp"].to_numpy(dtype=np.int32)
        opp_combo = r_df[f"{o_pre}_combo"].to_numpy(dtype=np.int32)
        opp_sf = r_df[f"{o_pre}_superfreeze"].to_numpy(dtype=np.int32) if has_sf else np.zeros(n, dtype=np.int32)
        self_char = int(r_df[f"{s_pre}_char"].iloc[0])
        opp_char = int(r_df[f"{o_pre}_char"].iloc[0])
        self_sa = int(r_df[f"{s_pre}_sa"].iloc[0]) if f"{s_pre}_sa" in r_df.columns else 0
        opp_sa = int(r_df[f"{o_pre}_sa"].iloc[0]) if f"{o_pre}_sa" in r_df.columns else 0
        max_hp = max(120, int(hp.max())) if len(hp) > 0 else 160

        i = 1
        while i < n:
            if hp[i] < hp[i - 1]:
                hit1_idx = i
                hp_before = int(hp[i - 1])
                min_hp = int(hp[i])
                last_drop_idx = i
                max_combo = int(opp_combo[i])

                j = i + 1
                while j < n:
                    if hp[j] < hp[j - 1]:
                        if hp[j] < min_hp:
                            min_hp = int(hp[j])
                        last_drop_idx = j
                    if opp_combo[j] > max_combo:
                        max_combo = int(opp_combo[j])
                    # Episode closes once combo resets AND no new HP drop arrives within gap
                    if opp_combo[j] == 0 and (j - last_drop_idx) > combo_gap_frames:
                        break
                    j += 1

                hp_lost = hp_before - min_hp
                if hp_lost >= min_hp_loss:
                    # Backtrack strictly before any preceding opponent Superfreeze flash
                    cause_idx = hit1_idx - 1
                    had_superfreeze = False
                    # Check if superfreeze was active on hit1 or within 12 frames prior
                    sf_scan_lo = max(0, hit1_idx - 12)
                    sf_hits = np.flatnonzero(opp_sf[sf_scan_lo : hit1_idx + 1] > 0)
                    if len(sf_hits) > 0:
                        had_superfreeze = True
                        k = sf_scan_lo + int(sf_hits[0])
                        steps = 0
                        while k > 0 and opp_sf[k - 1] > 0 and steps < max_superfreeze_backtrack:
                            k -= 1
                            steps += 1
                        cause_idx = max(0, k - 2)
                    else:
                        cause_idx = max(0, hit1_idx - 2)

                    episodes.append(
                        {
                            "episode_id": f"{qid}_p{perspective}_gf{int(gf[cause_idx])}",
                            "quark_id": qid,
                            "perspective": int(perspective),
                            "round_id": int(round_id),
                            "t_cause_gf": int(gf[cause_idx]),
                            "t_hit1_gf": int(gf[hit1_idx]),
                            "t_end_gf": int(gf[min(last_drop_idx, n - 1)]),
                            "hp_before": hp_before,
                            "hp_after": min_hp,
                            "hp_lost": int(hp_lost),
                            "hp_lost_pct": round(100.0 * hp_lost / max(max_hp, 1), 1),
                            "combo_hits": int(max(1, max_combo)),
                            "was_ko": bool(min_hp == 0),
                            "had_superfreeze": bool(had_superfreeze),
                            "self_char": self_char,
                            "opp_char": opp_char,
                            "self_sa": self_sa,
                            "opp_sa": opp_sa,
                            "self_char_name": pretty_char(self_char),
                            "opp_char_name": pretty_char(opp_char),
                        }
                    )
                i = max(j, last_drop_idx + 1)
            else:
                i += 1

    return episodes


def select_seed_episodes(
    all_episodes: list[dict[str, Any]],
    *,
    seed_count: int = 24,
    max_per_set: int = 3,
    min_gf_sep: int = 180,
) -> list[dict[str, Any]]:
    """Select top highest health-loss episodes with set diversity and round deduplication."""
    ranked = sorted(
        all_episodes,
        key=lambda e: (e["hp_lost_pct"], e["hp_lost"], 1 if e["was_ko"] else 0),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    per_set: Counter[str] = Counter()

    # Pass 1: enforce per-set diversity cap so all personal sets get represented
    for ep in ranked:
        if len(selected) >= seed_count:
            break
        qid = ep["quark_id"]
        if per_set[qid] >= max_per_set:
            continue
        too_close = any(
            s["quark_id"] == qid
            and s["round_id"] == ep["round_id"]
            and abs(s["t_cause_gf"] - ep["t_cause_gf"]) < min_gf_sep
            for s in selected
        )
        if too_close:
            continue
        selected.append(ep)
        per_set[qid] += 1

    # Pass 2: if fewer than seed_count (e.g. only 2-3 sets), fill remaining slots ignoring max_per_set
    if len(selected) < seed_count:
        chosen_ids = {s["episode_id"] for s in selected}
        for ep in ranked:
            if len(selected) >= seed_count:
                break
            if ep["episode_id"] in chosen_ids:
                continue
            too_close = any(
                s["quark_id"] == ep["quark_id"]
                and s["round_id"] == ep["round_id"]
                and abs(s["t_cause_gf"] - ep["t_cause_gf"]) < min_gf_sep
                for s in selected
            )
            if too_close:
                continue
            selected.append(ep)
            chosen_ids.add(ep["episode_id"])

    return selected


# ---------------------------------------------------------------------------
# Stage 2: Pass-1 Visual Grounding (Gemini Flash on Personal Clips Only)
# ---------------------------------------------------------------------------

def _pass1_cache_key(ep: dict[str, Any], clip_path: Path) -> str:
    payload = {
        "v": PASS1_VERSION,
        "qid": ep["quark_id"],
        "p": ep["perspective"],
        "gf": ep["t_cause_gf"],
        "clip": clip_path.name,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def classify_personal_clip_with_gemini(
    ep: dict[str, Any],
    clip_path: Path,
    *,
    api_key: str,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
) -> dict[str, Any]:
    """Have Gemini Flash watch one personal 6s clip and tag it into the established taxonomy."""
    p_side = f"P{ep['perspective']}"
    opp_side = "P2" if int(ep["perspective"]) == 1 else "P1"
    taxonomy_lines = "\n".join(f"  - {k}: {v}" for k, v in HABIT_TAXONOMY.items())

    prompt = (
        "You are an expert Street Fighter III: 3rd Strike coach watching a 6-second replay clip.\n"
        "The yellow HUD banner '>>> DECISION POINT (t0) <<<' appears at ~3.5s, strictly BEFORE the hit/Super Art where the coached player takes damage.\n"
        "TRUST ONLY WHAT YOU LITERALLY SEE IN THE VIDEO. Do NOT guess or invent moves.\n\n"
        f"Coached player: {p_side} {ep['self_char_name']} (SA{ep.get('self_sa', '?')})\n"
        f"Opponent: {opp_side} {ep['opp_char_name']} (SA{ep.get('opp_sa', '?')})\n"
        f"Damage taken starting at t0: -{ep['hp_lost']} HP ({ep['hp_lost_pct']}% life, {ep['combo_hits']}-hit combo"
        f"{', KO' if ep.get('was_ko') else ''}{', preceded by opponent Superfreeze' if ep.get('had_superfreeze') else ''}).\n\n"
        "Choose EXACTLY ONE category key from this established taxonomy based on what caused the coached player to get hit:\n"
        f"{taxonomy_lines}\n\n"
        "Category discipline:\n"
        "- Choose `missed_anti_air` ONLY if the opponent jumped in and hit the grounded coached player with an aerial attack.\n"
        "- Choose `got_anti_aired` ONLY if the coached player voluntarily jumped in and got hit out of the air (NEVER if they were launched into the air by a ground move/Super!).\n"
        "- Choose `wakeup_reversal_or_super_punished` if the opponent was waking up from knockdown and beat the coached player's pressure/meaty with a reversal or Super Art.\n"
        "- Choose `opened_up_in_blockstring` ONLY if the coached player was visibly blocking pressure immediately before getting hit.\n\n"
        "Return strict JSON with keys:\n"
        '  "category": one exact key from the taxonomy above,\n'
        '  "what_happened": 1 concise factual sentence naming the real moves/actions seen on screen,\n'
        '  "self_mistake": 1 concise sentence describing what decision/commitment caused the coached player to eat this damage,\n'
        '  "opp_move": short name of the move that opened up the coached player (e.g. "Jump-in HK", "Wakeup SA1", "c.MK -> Super"),\n'
        '  "confidence": "high" | "medium" | "low"\n'
    )

    parts = [
        {"text": prompt},
        {"text": "\n[CLIP: 6-second personal match footage]\n"},
        _encode_mp4_part(clip_path),
    ]
    parsed, model_used = _call_gemini_json(parts, api_key=api_key, models=models, timeout_seconds=90.0)
    cat = str(parsed.get("category", "other")).strip().lower()
    if cat not in HABIT_TAXONOMY:
        cat = "other"
    return {
        "category": cat,
        "category_label": HABIT_TAXONOMY[cat],
        "what_happened": str(parsed.get("what_happened", "")).strip(),
        "self_mistake": str(parsed.get("self_mistake", "")).strip(),
        "opp_move": str(parsed.get("opp_move", "")).strip(),
        "confidence": str(parsed.get("confidence", "medium")).strip().lower(),
        "model": model_used,
    }


def run_pass1_visual_tagging(
    episodes: list[dict[str, Any]],
    *,
    clips_dir: Path,
    cache_path: Path,
    max_workers: int = 2,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
) -> dict[str, int]:
    """Attach visually verified Pass-1 tags to every episode that has a rendered 6s clip."""
    api_key = load_gemini_api_key()
    stats = {"total": len(episodes), "cached": 0, "called": 0, "failed": 0, "skipped_no_clip": 0}
    if not api_key:
        print("WARNING: GEMINI_API_KEY not found; skipping Pass-1 visual tagging.")
        return stats

    cache = _load_cache(cache_path)
    lock = threading.Lock()
    pending: list[tuple[dict[str, Any], Path, str]] = []

    for ep in episodes:
        clip_name = f"{ep['quark_id']}_gf{ep['t_cause_gf']}.mp4"
        clip_path = clips_dir / clip_name
        if not clip_path.exists():
            stats["skipped_no_clip"] += 1
            continue
        ck = _pass1_cache_key(ep, clip_path)
        if ck in cache:
            ep["visual_tag"] = dict(cache[ck])
            stats["cached"] += 1
        else:
            pending.append((ep, clip_path, ck))

    def _worker(item: tuple[dict[str, Any], Path, str]) -> tuple[dict[str, Any], str, dict[str, Any] | None, str | None]:
        ep, cpath, ck = item
        try:
            res = classify_personal_clip_with_gemini(ep, cpath, api_key=api_key, models=models)
            return ep, ck, res, None
        except Exception as exc:  # noqa: BLE001
            return ep, ck, None, str(exc)

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
            futs = [pool.submit(_worker, item) for item in pending]
            for fut in as_completed(futs):
                ep, ck, res, err = fut.result()
                if res is not None:
                    ep["visual_tag"] = res
                    with lock:
                        cache[ck] = res
                        _save_cache(cache_path, cache)
                        stats["called"] += 1
                else:
                    with lock:
                        stats["failed"] += 1
                    print(f"WARNING: Pass-1 visual tagging failed for {ep['episode_id']}: {err}")

    return stats


# ---------------------------------------------------------------------------
# Stage 3: RAM-Space Trajectory Search (Zero VLM on the 121-set Pro Corpus)
# ---------------------------------------------------------------------------

def count_personal_ram_recurrence(
    seed_ep: dict[str, Any],
    personal_feats_by_qid: dict[str, tuple[int, pd.DataFrame]],
    *,
    max_norm_dist: float = 0.30,
) -> dict[str, Any]:
    """Scan all personal sets in RAM-space to estimate how often a verified mistake trajectory recurs."""
    qid = seed_ep["quark_id"]
    entry = personal_feats_by_qid.get(qid)
    if entry is None:
        return {"ram_matches": 0, "sets_matched": 0}
    _, seed_feats = entry
    r_df = seed_feats[seed_feats["round_id"] == seed_ep["round_id"]].reset_index(drop=True)
    if r_df.empty:
        return {"ram_matches": 0, "sets_matched": 0}

    s_gf = seed_ep["t_cause_gf"] - 45
    e_gf = seed_ep["t_cause_gf"] + 10
    mask = (r_df["game_frame"] >= s_gf) & (r_df["game_frame"] <= e_gf)
    idxs = np.flatnonzero(mask.to_numpy())
    if len(idxs) < 10:
        return {"ram_matches": 0, "sets_matched": 0}

    s_i, e_i = int(idxs[0]), int(idxs[-1])
    q_len = max(15, e_i - s_i + 1)
    cols = list(CORE_FEATURE_COLUMNS)
    q_mat = r_df.iloc[s_i : e_i + 1][cols].to_numpy(dtype=np.float32)
    w_vec = _sequence_weight_vector(TRAJ_STEPS)
    w_sq = float(np.sum(w_vec.astype(np.float64) ** 2))
    q_traj = resample_trajectory(q_mat, TRAJ_STEPS) * w_vec
    q_dx = float(r_df.iloc[s_i : e_i + 1]["dx_px"].mean())
    q_sc = float(r_df.iloc[s_i : e_i + 1]["self_corner_prox"].mean())
    q_oc = float(r_df.iloc[s_i : e_i + 1]["opp_corner_prox"].mean())
    q_reg = _dominant_regime(r_df.iloc[s_i : e_i + 1])

    total_hits = 0
    sets_hit: set[str] = set()
    for p_qid, (_, p_feats) in personal_feats_by_qid.items():
        for p_rid, cand_round in p_feats.groupby("round_id", sort=False):
            if len(cand_round) < 15:
                continue
            hits = _score_candidate_round(
                cand_round,
                q_traj,
                w_vec,
                w_sq,
                q_len=q_len,
                q_mean_dx=q_dx,
                q_mean_self_cnr=q_sc,
                q_mean_opp_cnr=q_oc,
                q_dom_regime=q_reg,
                scales=(0.85, 1.00, 1.15),
                max_norm_dist=max_norm_dist,
            )
            for h in hits:
                # Exclude the seed episode itself
                if p_qid == qid and int(p_rid) == int(seed_ep["round_id"]) and abs(int(h["end_gf"]) - int(e_gf)) < 90:
                    continue
                total_hits += 1
                sets_hit.add(p_qid)

    return {"ram_matches": total_hits, "sets_matched": len(sets_hit)}


def search_pro_setup_solutions(
    seed_ep: dict[str, Any],
    *,
    target_parquet: Path,
    parquet_dir: Path,
    corpus_index: HierarchicalContextIndex,
    cfg: SegmentationConfig,
    max_candidates: int = 2,
) -> dict[str, Any]:
    """Query the 121-set pro corpus in RAM-space using ONLY the pre-mistake setup window."""
    start_gf = max(1, int(seed_ep["t_cause_gf"]) - 60)
    end_gf = max(start_gf + 15, int(seed_ep["t_cause_gf"]) - 10)
    try:
        res = search_similar_sequences(
            target_parquet=target_parquet,
            perspective=int(seed_ep["perspective"]),
            start_gf=start_gf,
            end_gf=end_gf,
            parquet_dir=parquet_dir,
            corpus_index=corpus_index,
            config=cfg,
            top_k=14,
            exclude_self=True,
            max_norm_dist=0.38,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Setup-window pro search failed for {seed_ep['episode_id']}: {exc}")
        return {"retrieval_tier": "none", "candidates": []}

    raw_matches = res.get("matches", [])
    # Prefer pros who solved the setup (positive aftermath outcome_value > 0.02)
    positive = [m for m in raw_matches if float(m.get("outcome_value", 0.0)) > 0.02]
    pool = positive if positive else [m for m in raw_matches if float(m.get("outcome_value", 0.0)) >= 0.0]
    # Keep closest-first (norm_dist ASC) with distinct quark_ids
    chosen: list[dict[str, Any]] = []
    seen_qids: set[str] = set()
    for m in sorted(pool, key=lambda x: float(x.get("norm_dist", 1.0))):
        mq = str(m.get("quark_id", ""))
        if mq in seen_qids:
            continue
        seen_qids.add(mq)
        p_persp = int(m.get("perspective", 1))
        p_info = resolve_replay_players_and_ranks(mq, p_persp, data_dir=parquet_dir.resolve().parent)
        chosen.append(
            {
                "quark_id": mq,
                "perspective": p_persp,
                "round_id": int(m.get("round_id", 0)),
                "game_frame": int(m.get("end_gf", m.get("mid_gf", 0))),
                "start_gf": int(m.get("start_gf", 0)),
                "end_gf": int(m.get("end_gf", 0)),
                "self_char_name": str(m.get("self_char_name", "")),
                "opp_char_name": str(m.get("opp_char_name", "")),
                "self_player_name": p_info["self_player_name"],
                "self_rank": p_info["self_rank"],
                "self_rank_letter": p_info["self_rank_letter"],
                "opp_player_name": p_info["opp_player_name"],
                "opp_rank": p_info["opp_rank"],
                "opp_rank_letter": p_info["opp_rank_letter"],
                "action_coarse": str(m.get("action_coarse", "")),
                "outcome_value": float(m.get("outcome_value", 0.0)),
                "net_hp_swing": float(m.get("net_hp_swing", 0.0)),
                "similarity_pct": int(m.get("similarity_pct", 0)),
                "norm_dist": float(m.get("norm_dist", 0.0)),
                "retrieval_tier": str(res.get("retrieval_tier", "")),
                "clip_rel_path": None,
                "verification": None,
            }
        )
        if len(chosen) >= max_candidates:
            break

    return {
        "retrieval_tier": str(res.get("retrieval_tier", "")),
        "candidates": chosen,
    }


# ---------------------------------------------------------------------------
# Stage 4: Pass-2 Visual Relevance Gate (Gemini Flash on Top Pro Candidates)
# ---------------------------------------------------------------------------

def _pass2_cache_key(seed_ep: dict[str, Any], user_clip: Path, cand: dict[str, Any], pro_clip: Path) -> str:
    payload = {
        "v": PASS2_VERSION,
        "u_qid": seed_ep["quark_id"],
        "u_gf": seed_ep["t_cause_gf"],
        "u_clip": user_clip.name,
        "p_qid": cand["quark_id"],
        "p_gf": cand["game_frame"],
        "p_clip": pro_clip.name,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def verify_pro_candidate_with_gemini(
    seed_ep: dict[str, Any],
    user_clip: Path,
    cand: dict[str, Any],
    pro_clip: Path,
    *,
    api_key: str,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
) -> dict[str, Any]:
    """Watch Your Clip + Pro Candidate Clip side-by-side and strictly gate relevance."""
    vtag = seed_ep.get("visual_tag") or {}
    u_side = f"P{seed_ep['perspective']}"
    p_side = f"P{cand['perspective']}"

    prompt = (
        "You are an expert Street Fighter III: 3rd Strike coach acting as a STRICT RELEVANCE GATE.\n"
        "You are watching two 6-second replay clips:\n"
        f"  1) USER CLIP: {u_side} {seed_ep['self_char_name']} vs {seed_ep['opp_char_name']} "
        f"(Category: {vtag.get('category_label', 'Unknown')} — {vtag.get('what_happened', '')})\n"
        f"  2) PRO CANDIDATE CLIP: {p_side} {cand['self_char_name']} vs {cand['opp_char_name']} "
        f"(RAM trajectory similarity {cand.get('similarity_pct', 0)}%)\n\n"
        "Judge strictly from the VIDEO FRAMES whether the Pro Candidate clip shows a GENUINELY COMPARABLE "
        "tactical setup/situation around the yellow '>>> DECISION POINT (t0) <<<' banner (~3.5s) where the pro "
        "actually demonstrates a relevant answer.\n"
        "Be STRICT: if the user clip is an anti-air jump-in and the pro clip is midscreen ground footsies or a totally "
        "different situation, set `is_relevant` to false.\n\n"
        "Return strict JSON with keys:\n"
        '  "is_relevant": boolean,\n'
        '  "relevance_reason": 1 concise sentence explaining why the setups match or do not match,\n'
        '  "pro_solution": if is_relevant is true, 1-2 specific sentences naming what the pro actually did on screen to solve the setup (else empty string)\n'
    )

    parts = [
        {"text": prompt},
        {"text": "\n[CLIP 1: USER CLIP]\n"},
        _encode_mp4_part(user_clip),
        {"text": "\n[CLIP 2: PRO CANDIDATE CLIP]\n"},
        _encode_mp4_part(pro_clip),
    ]
    parsed, model_used = _call_gemini_json(parts, api_key=api_key, models=models, timeout_seconds=90.0)
    return {
        "is_relevant": bool(parsed.get("is_relevant", False)),
        "relevance_reason": str(parsed.get("relevance_reason", "")).strip(),
        "pro_solution": str(parsed.get("pro_solution", "")).strip(),
        "model": model_used,
    }


def run_pass2_pro_verification(
    habits: list[dict[str, Any]],
    *,
    clips_dir: Path,
    cache_path: Path,
    max_workers: int = 2,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
) -> dict[str, int]:
    """Run Pass-2 visual relevance verification on all rendered pro candidates across habits."""
    api_key = load_gemini_api_key()
    stats = {"checked": 0, "cached": 0, "passed": 0, "rejected": 0, "failed": 0}
    if not api_key:
        return stats

    cache = _load_cache(cache_path)
    lock = threading.Lock()
    pending: list[tuple[dict[str, Any], Path, dict[str, Any], Path, str]] = []

    for h in habits:
        seed = h.get("pro_seed_episode")
        if not seed:
            continue
        u_clip = clips_dir / f"{seed['quark_id']}_gf{seed['t_cause_gf']}.mp4"
        if not u_clip.exists():
            continue
        for cand in h.get("pro_candidates", []):
            p_clip = clips_dir / f"{cand['quark_id']}_gf{cand['game_frame']}.mp4"
            if not p_clip.exists():
                continue
            ck = _pass2_cache_key(seed, u_clip, cand, p_clip)
            if ck in cache:
                cand["verification"] = dict(cache[ck])
                stats["cached"] += 1
                stats["checked"] += 1
                if cand["verification"].get("is_relevant"):
                    stats["passed"] += 1
                else:
                    stats["rejected"] += 1
            else:
                pending.append((seed, u_clip, cand, p_clip, ck))

    def _worker(item: tuple[dict[str, Any], Path, dict[str, Any], Path, str]):
        seed, u_clip, cand, p_clip, ck = item
        try:
            res = verify_pro_candidate_with_gemini(seed, u_clip, cand, p_clip, api_key=api_key, models=models)
            return cand, ck, res, None
        except Exception as exc:  # noqa: BLE001
            return cand, ck, None, str(exc)

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
            futs = [pool.submit(_worker, item) for item in pending]
            for fut in as_completed(futs):
                cand, ck, res, err = fut.result()
                with lock:
                    if res is not None:
                        cand["verification"] = res
                        cache[ck] = res
                        _save_cache(cache_path, cache)
                        stats["checked"] += 1
                        if res.get("is_relevant"):
                            stats["passed"] += 1
                        else:
                            stats["rejected"] += 1
                    else:
                        stats["failed"] += 1
                        print(f"WARNING: Pass-2 verification failed for {cand['quark_id']}_gf{cand['game_frame']}: {err}")

    # Filter each habit's verified_pro_solutions cleanly
    for h in habits:
        h["verified_pro_solutions"] = [
            c for c in h.get("pro_candidates", [])
            if isinstance(c.get("verification"), dict) and c["verification"].get("is_relevant")
        ]
        h["rejected_pro_count"] = sum(
            1 for c in h.get("pro_candidates", [])
            if isinstance(c.get("verification"), dict) and not c["verification"].get("is_relevant")
        )

    return stats


# ---------------------------------------------------------------------------
# Aggregation & Visually Grounded Coach Synthesis
# ---------------------------------------------------------------------------

def aggregate_verified_habits(
    username: str,
    sets_meta: list[dict[str, Any]],
    all_episodes: list[dict[str, Any]],
    tagged_episodes: list[dict[str, Any]],
    *,
    corpus_size: int = 0,
) -> dict[str, Any]:
    """Aggregate Pass-1 visually verified personal episodes into ranked habit buckets."""
    by_cat: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ep in tagged_episodes:
        vt = ep.get("visual_tag")
        if not isinstance(vt, dict):
            continue
        cat = str(vt.get("category", "other"))
        by_cat[cat].append(ep)

    num_sets = len(sets_meta)
    habits: list[dict[str, Any]] = []
    for cat, items in by_cat.items():
        items_sorted = sorted(items, key=lambda x: (x["hp_lost_pct"], x["hp_lost"]), reverse=True)
        unique_sets = sorted({x["quark_id"] for x in items_sorted})
        tot_hp = sum(int(x["hp_lost"]) for x in items_sorted)
        avg_hp_pct = sum(float(x["hp_lost_pct"]) for x in items_sorted) / len(items_sorted)
        opp_counts = Counter(x["opp_char_name"] for x in items_sorted)
        move_counts = Counter(
            str(x["visual_tag"].get("opp_move", "")).strip()
            for x in items_sorted
            if str(x["visual_tag"].get("opp_move", "")).strip()
        )
        habits.append(
            {
                "category": cat,
                "label": HABIT_TAXONOMY.get(cat, cat),
                "verified_clips": len(items_sorted),
                "sets_affected": len(unique_sets),
                "total_sets": num_sets,
                "set_spread_pct": int(round(100.0 * len(unique_sets) / max(num_sets, 1))),
                "is_recurring": len(unique_sets) >= 2 or len(items_sorted) >= 3,
                "total_hp_lost": int(tot_hp),
                "avg_hp_lost_pct": round(avg_hp_pct, 1),
                "top_opponents": [{"char": ch, "count": c} for ch, c in opp_counts.most_common(4)],
                "common_opp_moves": [{"move": m, "count": c} for m, c in move_counts.most_common(4)],
                "ram_recurrence_extra": 0,
                "examples": items_sorted,
                "pro_seed_episode": items_sorted[0] if items_sorted else None,
                "pro_candidates": [],
                "verified_pro_solutions": [],
                "rejected_pro_count": 0,
            }
        )

    habits.sort(key=lambda h: (h["total_hp_lost"], h["verified_clips"], h["sets_affected"]), reverse=True)

    # Matchup & set health-loss summary across ALL detected Hit-#1 episodes
    mu_map: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"opp_char_name": "", "sets": set(), "rounds": 0, "episodes": 0, "total_hp_lost": 0}
    )
    set_ep_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ep in all_episodes:
        set_ep_map[ep["quark_id"]].append(ep)
        m = mu_map[ep["opp_char_name"]]
        m["opp_char_name"] = ep["opp_char_name"]
        m["sets"].add(ep["quark_id"])
        m["episodes"] += 1
        m["total_hp_lost"] += int(ep["hp_lost"])

    char_counter = Counter(ep["self_char_name"] for ep in all_episodes)
    primary_char = char_counter.most_common(1)[0][0] if char_counter else "Unknown"
    latest_date = max((s["match_date"] for s in sets_meta if s.get("match_date")), default=datetime.now().strftime("%Y-%m-%d"))

    set_rows = []
    total_rounds = 0
    for s in sets_meta:
        qid = s["quark_id"]
        eps = set_ep_map.get(qid, [])
        rnds = int(s.get("rounds", 0))
        total_rounds += rnds
        opp_c = Counter(e["opp_char_name"] for e in eps).most_common(1)
        opp_char_name = opp_c[0][0] if opp_c else "?"
        if opp_char_name in mu_map:
            mu_map[opp_char_name]["rounds"] += rnds
        set_rows.append(
            {
                "quark_id": qid,
                "match_date": s["match_date"],
                "opp_player_name": s["opp_player_name"],
                "opp_char_name": opp_char_name,
                "self_score": s.get("self_score"),
                "opp_score": s.get("opp_score"),
                "rounds": rnds,
                "episodes": len(eps),
                "total_hp_lost": sum(int(e["hp_lost"]) for e in eps),
            }
        )

    matchups = [
        {
            "opp_char_name": v["opp_char_name"],
            "sets": len(v["sets"]),
            "rounds": v["rounds"],
            "episodes": v["episodes"],
            "total_hp_lost": v["total_hp_lost"],
        }
        for v in mu_map.values()
    ]
    matchups.sort(key=lambda x: (x["total_hp_lost"], x["episodes"]), reverse=True)

    return {
        "report_type": "hybrid_habit_rollup",
        "username": username,
        "self_player_name": username,
        "primary_char_name": primary_char,
        "match_date": latest_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "num_sets": num_sets,
        "total_rounds": total_rounds,
        "total_damage_episodes": len(all_episodes),
        "visually_verified_clips": sum(h["verified_clips"] for h in habits),
        "corpus_size": corpus_size,
        "habits": habits,
        "matchups": matchups,
        "sets": set_rows,
        "ai_synthesis": None,
    }


def synthesize_verified_habits_with_gemini(
    rollup: dict[str, Any],
    *,
    cache_path: Path | None = None,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
) -> dict[str, Any] | None:
    """Generate 1-to-1 aligned Top-3 Priorities strictly from Pass-1 + Pass-2 visually verified clips."""
    api_key = load_gemini_api_key()
    if not api_key:
        return None

    top3 = rollup.get("habits", [])[:3]
    if not top3:
        return None

    payload = {
        "v": SYNTH_VERSION,
        "username": rollup["username"],
        "primary_char": rollup["primary_char_name"],
        "num_sets": rollup["num_sets"],
        "total_rounds": rollup["total_rounds"],
        "visually_verified_clips": rollup["visually_verified_clips"],
        "top3_verified_habits": [
            {
                "habit_rank": i,
                "category": h["category"],
                "label": h["label"],
                "verified_clips": h["verified_clips"],
                "sets_affected": f"{h['sets_affected']}/{h['total_sets']}",
                "total_hp_lost": h["total_hp_lost"],
                "avg_hp_lost_pct": h["avg_hp_lost_pct"],
                "ram_recurrence_extra": h.get("ram_recurrence_extra", 0),
                "observed_clips": [
                    {
                        "vs": f"{ex['opp_player_name']} ({ex['opp_char_name']})",
                        "hp_lost_pct": ex["hp_lost_pct"],
                        "what_happened": ex["visual_tag"]["what_happened"],
                        "self_mistake": ex["visual_tag"]["self_mistake"],
                    }
                    for ex in h["examples"][:4]
                ],
                "verified_pro_solutions": [
                    c["verification"]["pro_solution"]
                    for c in h.get("verified_pro_solutions", [])
                    if isinstance(c.get("verification"), dict) and c["verification"].get("pro_solution")
                ],
            }
            for i, h in enumerate(top3, 1)
        ],
    }

    ck = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    cache = _load_cache(cache_path) if cache_path else {}
    if ck in cache:
        return dict(cache[ck])

    prompt = (
        "You are an expert Street Fighter III: 3rd Strike coach summarizing VISUALLY VERIFIED personal clips.\n"
        "Every single clip below was already watched frame-by-frame. Base your coaching STRICTLY on those observed clips.\n"
        "ALIGNMENT RULE: `top_priorities` MUST map 1-to-1 in EXACT ORDER to `top3_verified_habits` (habit_rank 1, 2, 3).\n\n"
        "Return strict JSON with keys:\n"
        '  "headline": 1-sentence executive summary of habit_rank 1 grounded in the observed footage,\n'
        '  "top_priorities": array of objects matching each item in `top3_verified_habits` in order, each with '
        '{"habit_rank": int, "category": exact category, "title": short crisp title, '
        '"diagnosis": 1-2 sentences citing specific moves/patterns observed in the clips, '
        '"drill": 1 concrete actionable training drill or in-match rule (referencing verified_pro_solutions if present)},\n'
        '  "next_training_partner_focus": 1 sentence recommending which verified clip/habit to import into the live AI Training Partner first.\n\n'
        f"DATA:\n{json.dumps(payload, indent=2)}"
    )
    try:
        parsed, model_used = _call_gemini_json([{"text": prompt}], api_key=api_key, models=models, timeout_seconds=45.0)
        parsed["model"] = model_used
        if cache_path:
            cache[ck] = parsed
            _save_cache(cache_path, cache)
        return parsed
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: Verified habit synthesis failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Stage 5: Responsive Mobile-Friendly HTML Renderer
# ---------------------------------------------------------------------------

def render_verified_habits_html(rollup: dict[str, Any], *, data_dir: Path = DEFAULT_DATA_DIR) -> str:
    """Render the Milestone-5 Visually Verified Habit Rollup HTML dashboard."""
    uname = html.escape(str(rollup["username"]))
    pchar = html.escape(str(rollup["primary_char_name"]))
    num_sets = int(rollup["num_sets"])
    tot_rounds = int(rollup["total_rounds"])
    tot_eps = int(rollup["total_damage_episodes"])
    ver_clips = int(rollup["visually_verified_clips"])
    corpus_sz = int(rollup.get("corpus_size", 0))

    # Aligned Coach Synthesis
    ai_html = ""
    ai = rollup.get("ai_synthesis")
    habits_list = rollup.get("habits", [])
    if isinstance(ai, dict) and ai.get("headline"):
        prio_cards = []
        for idx, p in enumerate(ai.get("top_priorities", [])[:3], 1):
            rank = int(p.get("habit_rank") or idx)
            if rank < 1 or rank > len(habits_list):
                rank = idx
            h_lbl = habits_list[rank - 1]["label"] if rank <= len(habits_list) else str(p.get("category", ""))
            prio_cards.append(
                f"""
                <div class="prio-card">
                  <div class="prio-title">#{idx} {html.escape(str(p.get('title', '')))}</div>
                  <div class="prio-map">
                    <a class="jump-link" href="#habit-{rank}" onclick="openHabit({rank})">Verified Evidence: Habit #{rank} — {html.escape(h_lbl)} &darr;</a>
                  </div>
                  <div class="prio-diag">{html.escape(str(p.get('diagnosis', '')))}</div>
                  <div class="prio-drill"><strong>Drill / Rule:</strong> {html.escape(str(p.get('drill', '')))}</div>
                </div>
                """
            )
        tp_note = html.escape(str(ai.get("next_training_partner_focus", "")))
        ai_html = f"""
        <section class="ai-box">
          <div class="ai-head">Visually Verified Coach Synthesis <span class="model-tag">{html.escape(str(ai.get('model', 'Gemini')))}</span></div>
          <p class="ai-headline">{html.escape(str(ai['headline']))}</p>
          <div class="prio-grid">{''.join(prio_cards)}</div>
          {f'<p class="ai-mu"><strong>AI Training Partner Focus:</strong> {tp_note}</p>' if tp_note else ''}
        </section>
        """

    # Habit Cards
    max_hp_sum = max((int(h["total_hp_lost"]) for h in habits_list), default=1) or 1
    habit_cards: list[str] = []
    for idx, h in enumerate(habits_list, 1):
        bar_w = max(8, min(100, int(round(100.0 * int(h["total_hp_lost"]) / max_hp_sum))))
        rec_badge = (
            '<span class="badge badge-hot">Recurring Habit</span>'
            if h.get("is_recurring")
            else '<span class="badge badge-one">Isolated</span>'
        )
        opps_str = ", ".join(f"{html.escape(o['char'])} ({o['count']})" for o in h.get("top_opponents", [])) or "—"
        moves_str = ", ".join(f"<code>{html.escape(m['move'])}</code> ({m['count']})" for m in h.get("common_opp_moves", [])) or "—"
        ram_extra = int(h.get("ram_recurrence_extra", 0))
        ram_badge = (
            f"<span>+ <strong>{ram_extra}</strong> similar RAM trajectory match(es) across personal sets</span>"
            if ram_extra > 0
            else ""
        )

        # Strictly YOUR footage cards
        user_cards: list[str] = []
        for ex_idx, ex in enumerate(h.get("examples", [])):
            vt = ex.get("visual_tag") or {}
            clip = ex.get("clip_rel_path")
            vid_id = f"vid_h{idx}_u{ex_idx}"
            takeover_cmd = (
                f".\\scripts\\takeover.ps1 -QuarkId {ex['quark_id']} "
                f"-Frame {ex['t_cause_gf']} -Player {ex['perspective']} "
                f"-Label \"{h['label']}\""
            )
            vid_html = (
                f"""
                <div class="vid-wrap">
                  <div class="vid-bar">
                    <span class="pill-you">YOUR FOOTAGE</span>
                    <span class="muted">t0 = pre-hit gf{ex['t_cause_gf']} · -{ex['hp_lost']} HP ({ex['hp_lost_pct']}%)</span>
                    <button class="play-btn" onclick="togglePlay('{vid_id}', this)">Play</button>
                  </div>
                  <video id="{vid_id}" controls playsinline preload="metadata" src="{html.escape(str(clip))}"></video>
                </div>
                """
                if clip
                else ""
            )
            user_cards.append(
                f"""
                <div class="ex-card">
                  <div class="ex-head">
                    <div>
                      <span class="mono">{html.escape(str(ex.get('match_date', '')))}</span> ·
                      <strong>vs {html.escape(str(ex.get('opp_player_name', 'Opp')))} ({html.escape(str(ex['opp_char_name']))})</strong>
                      <span class="muted">R{ex['round_id']} · {ex['combo_hits']}-hit{' · KO' if ex.get('was_ko') else ''}</span>
                    </div>
                    <div class="ex-btns">
                      <span class="neg">-{ex['hp_lost']} HP ({ex['hp_lost_pct']}%)</span>
                      {render_takeover_buttons_html(str(ex['quark_id']), int(ex['t_cause_gf']), int(ex['perspective']), str(h['label']), takeover_cmd, is_pro=False)}
                    </div>
                  </div>
                  <div class="ex-desc">
                    <div><strong>What happened:</strong> {html.escape(str(vt.get('what_happened', '')))}</div>
                    <div class="muted" style="margin-top:3px"><strong>Mistake:</strong> {html.escape(str(vt.get('self_mistake', '')))}</div>
                  </div>
                  {vid_html}
                </div>
                """
            )

        # Clearly separated Visually Verified Pro Solutions (Pass-2 gated!)
        pro_html = ""
        v_pros = h.get("verified_pro_solutions", [])
        rej_cnt = int(h.get("rejected_pro_count", 0))
        if v_pros:
            pro_items: list[str] = []
            for p_idx, pc in enumerate(v_pros):
                ver = pc.get("verification") or {}
                pvid_id = f"vid_h{idx}_p{p_idx}"
                pro_cmd = (
                    f".\\scripts\\takeover.ps1 -QuarkId {pc['quark_id']} "
                    f"-Frame {pc['game_frame']} -Player {pc['perspective']} "
                    f"-Label \"Top-Rank Solution {h['label']}\""
                )
                pclip = pc.get("clip_rel_path")
                pro_players_html = format_matchup_players_html(
                    str(pc["quark_id"]),
                    int(pc["perspective"]),
                    str(pc.get("self_char_name", "")),
                    str(pc.get("opp_char_name", "")),
                    data_dir=data_dir,
                    precomputed=pc,
                )
                pro_items.append(
                    f"""
                    <div class="pro-card">
                      <div class="ex-head">
                        <div>
                          <span class="pill-pro">VISUALLY VERIFIED TOP-RANK SOLUTION ({pc['similarity_pct']}% setup match)</span>
                          <strong>{pro_players_html}</strong>
                          <span class="muted">R{pc['round_id']} gf{pc['game_frame']} · EV {pc['outcome_value']:+.2f}</span>
                        </div>
                        {render_takeover_buttons_html(str(pc['quark_id']), int(pc['game_frame']), int(pc['perspective']), f"Top-Rank Solution {h['label']}", pro_cmd, is_pro=True)}
                      </div>
                      <div class="ex-desc">
                        <div><strong>How the top-rank player solved it:</strong> {html.escape(str(ver.get('pro_solution', '')))}</div>
                        <div class="muted" style="margin-top:3px"><strong>Relevance check:</strong> {html.escape(str(ver.get('relevance_reason', '')))}</div>
                      </div>
                      {f'<div class="vid-wrap"><div class="vid-bar"><span class="pill-pro">TOP-RANK FOOTAGE</span><button class="play-btn" onclick="togglePlay(\'{pvid_id}\', this)">Play</button></div><video id="{pvid_id}" controls playsinline preload="metadata" src="{html.escape(str(pclip))}"></video></div>' if pclip else ''}
                    </div>
                    """
                )
            pro_html = f"""
            <div class="pro-section">
              <div class="pro-section-title">How Top-Rank Players Solved This Setup (Pass-2 Visually Verified{f' · {rej_cnt} false match(es) filtered out' if rej_cnt else ''})</div>
              {''.join(pro_items)}
            </div>
            """
        elif rej_cnt > 0:
            pro_html = f'<div class="pro-filtered-note">Pass-2 visual gate filtered out {rej_cnt} RAM top-rank candidate(s) that did not visually match this setup.</div>'

        open_attr = " open" if idx == 1 else ""
        habit_cards.append(
            f"""
            <article class="habit-card" id="habit-{idx}">
              <div class="habit-top">
                <div>
                  <span class="rank">#{idx}</span>
                  <span class="habit-name">{html.escape(h['label'])}</span>
                  <span class="badge badge-ver">Visually Verified</span>
                  {rec_badge}
                </div>
                <div class="reg-score">Total Damage Cost <strong>-{h['total_hp_lost']} HP</strong></div>
              </div>
              <div class="bar-track"><div class="bar-fill" style="width:{bar_w}%"></div></div>
              <div class="habit-stats">
                <span><strong>{h['verified_clips']}</strong> verified clip(s) across <strong>{h['sets_affected']}/{h['total_sets']}</strong> sets ({h['set_spread_pct']}%)</span>
                <span>Avg Loss: <strong class="neg">-{h['avg_hp_lost_pct']}% life</strong></span>
                {ram_badge}
              </div>
              <div class="habit-actions">
                <div><strong>Moves that hit you:</strong> {moves_str}</div>
                <div><strong>Opponents:</strong> {opps_str}</div>
              </div>
              <details id="habit-details-{idx}"{open_attr}>
                <summary>View {len(user_cards)} verified clip(s) of your footage{f' + {len(v_pros)} verified top-rank solution(s)' if v_pros else ''}</summary>
                <div class="cards-grid">{''.join(user_cards)}</div>
                {pro_html}
              </details>
            </article>
            """
        )

    mu_rows = [
        f"<tr><td><strong>{html.escape(m['opp_char_name'])}</strong></td><td>{m['sets']}</td><td>{m['rounds']}</td><td>{m['episodes']}</td><td class='neg'>-{m['total_hp_lost']} HP</td></tr>"
        for m in rollup.get("matchups", [])
    ]
    set_rows = []
    for s in rollup.get("sets", []):
        score_txt = (
            f"{s['self_score']}-{s['opp_score']}"
            if s.get("self_score") is not None and s.get("opp_score") is not None
            else "casual"
        )
        set_rows.append(
            f"<tr><td class='mono'>{html.escape(str(s['match_date']))}</td>"
            f"<td>{html.escape(str(s['opp_player_name']))} ({html.escape(str(s['opp_char_name']))})</td>"
            f"<td class='mono'>{html.escape(score_txt)}</td>"
            f"<td>{s['rounds']}</td><td>{s['episodes']}</td><td class='neg'>-{s['total_hp_lost']} HP</td></tr>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{uname} ({pchar}) — Visually Verified Habit Rollup ({num_sets} sets)</title>
<style>
  :root {{
    --bg: #0b1120; --surface: #111827; --card: #172033; --border: #263248;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; --ok: #4ade80; --warn: #f59e0b; --bad: #f87171;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); padding: 20px; max-width: 1180px; margin-inline: auto; }}
  .back-link {{ display: inline-block; color: var(--accent); text-decoration: none; font-size: 0.86rem; margin-bottom: 10px; }}
  .back-link:hover {{ text-decoration: underline; }}
  header {{ border-bottom: 1px solid var(--border); padding-bottom: 14px; margin-bottom: 20px; }}
  h1 {{ margin: 0 0 6px 0; font-size: 1.45rem; }}
  h2 {{ font-size: 1.12rem; margin: 26px 0 12px 0; color: #f8fafc; }}
  .sub {{ color: var(--muted); font-size: 0.88rem; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-top: 14px; }}
  .kpi {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 11px 14px; }}
  .kpi-val {{ font-size: 1.3rem; font-weight: 700; color: #fff; }}
  .kpi-lbl {{ font-size: 0.76rem; color: var(--muted); margin-top: 2px; }}
  .ai-box {{ background: linear-gradient(135deg, rgba(16,185,129,0.12), rgba(56,189,248,0.08)); border: 1px solid rgba(52,211,153,0.4); border-radius: 10px; padding: 16px; margin-bottom: 22px; }}
  .ai-head {{ font-weight: 700; color: #6ee7b7; font-size: 0.92rem; display: flex; justify-content: space-between; align-items: center; }}
  .model-tag {{ font-size: 0.72rem; background: rgba(15,23,42,0.7); color: var(--muted); padding: 2px 8px; border-radius: 999px; }}
  .ai-headline {{ font-size: 1.02rem; font-weight: 600; margin: 10px 0 14px 0; color: #fff; }}
  .prio-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; }}
  .prio-card {{ background: rgba(15,23,42,0.78); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 0.85rem; }}
  .prio-title {{ font-weight: 700; color: #fbbf24; margin-bottom: 4px; }}
  .prio-map {{ margin-bottom: 7px; font-size: 0.78rem; }}
  .jump-link {{ color: #7dd3fc; text-decoration: none; font-weight: 600; }}
  .jump-link:hover {{ text-decoration: underline; }}
  .prio-diag {{ color: var(--text); margin-bottom: 8px; line-height: 1.4; }}
  .prio-drill {{ color: #86efac; line-height: 1.4; }}
  .ai-mu {{ margin: 12px 0 0 0; font-size: 0.86rem; color: #cbd5e1; }}
  .habit-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 15px 16px; margin-bottom: 14px; }}
  .habit-top {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
  .rank {{ color: var(--accent); font-weight: 700; margin-right: 6px; }}
  .habit-name {{ font-size: 1.05rem; font-weight: 700; color: #fff; margin-right: 6px; }}
  .badge {{ font-size: 0.72rem; padding: 2px 8px; border-radius: 999px; font-weight: 600; margin-right: 5px; }}
  .badge-ver {{ background: rgba(16, 185, 129, 0.22); color: #6ee7b7; }}
  .badge-hot {{ background: rgba(245, 158, 11, 0.22); color: #fbbf24; }}
  .badge-one {{ background: rgba(148, 163, 184, 0.16); color: var(--muted); }}
  .reg-score {{ font-size: 0.88rem; color: var(--bad); }}
  .bar-track {{ height: 6px; background: #0f172a; border-radius: 4px; margin: 10px 0; overflow: hidden; }}
  .bar-fill {{ height: 100%; background: linear-gradient(90deg, #10b981, #f59e0b, #ef4444); border-radius: 4px; }}
  .habit-stats {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 0.84rem; color: #cbd5e1; margin-bottom: 8px; }}
  .habit-actions {{ font-size: 0.84rem; color: #cbd5e1; display: grid; gap: 4px; margin-bottom: 10px; }}
  code {{ background: #0f172a; padding: 1px 5px; border-radius: 4px; font-size: 0.82rem; color: #7dd3fc; }}
  details {{ border-top: 1px solid var(--border); padding-top: 8px; }}
  summary {{ cursor: pointer; font-size: 0.84rem; color: var(--accent); font-weight: 600; }}
  .cards-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 12px; margin-top: 10px; }}
  .ex-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 11px 12px; }}
  .ex-head {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; font-size: 0.83rem; }}
  .ex-btns {{ display: flex; align-items: center; gap: 8px; }}
  .ex-desc {{ font-size: 0.83rem; line-height: 1.4; margin-bottom: 8px; }}
  .vid-wrap {{ background: #0f172a; border: 1px solid #1e293b; border-radius: 6px; padding: 7px; }}
  .vid-bar {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; font-size: 0.76rem; }}
  video {{ width: 100%; border-radius: 5px; background: #000; display: block; }}
  .pill-you {{ background: rgba(248, 113, 113, 0.2); color: #fca5a5; padding: 1px 6px; border-radius: 4px; font-weight: 700; font-size: 0.7rem; }}
  .pill-pro {{ background: rgba(74, 222, 128, 0.2); color: #86efac; padding: 1px 6px; border-radius: 4px; font-weight: 700; font-size: 0.7rem; }}
  .play-btn {{ margin-left: auto; background: #1e293b; color: #fff; border: 1px solid #334155; border-radius: 4px; padding: 2px 8px; cursor: pointer; font-size: 0.72rem; }}
  .play-btn:hover, .copy-btn:hover {{ border-color: var(--accent); }}
  .copy-btn {{ background: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 5px; padding: 3px 8px; cursor: pointer; font-size: 0.74rem; white-space: nowrap; }}
  .pro-section {{ margin-top: 14px; border-top: 1px dashed rgba(74, 222, 128, 0.35); padding-top: 10px; }}
  .pro-section-title {{ font-size: 0.85rem; font-weight: 700; color: #86efac; margin-bottom: 8px; }}
  .pro-card {{ background: rgba(16, 185, 129, 0.06); border: 1px solid rgba(74, 222, 128, 0.3); border-radius: 8px; padding: 11px 12px; margin-bottom: 8px; }}
  .pro-filtered-note {{ margin-top: 10px; font-size: 0.78rem; color: var(--muted); font-style: italic; }}
  .table-wrap {{ overflow-x: auto; margin-top: 8px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; font-size: 0.84rem; }}
  th, td {{ padding: 9px 11px; border-bottom: 1px solid var(--border); text-align: left; }}
  th {{ background: var(--surface); color: var(--muted); font-weight: 600; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; white-space: nowrap; }}
  .muted {{ color: var(--muted); font-size: 0.78rem; }}
  .neg {{ color: var(--bad); font-weight: 600; }}
{TAKEOVER_UI_CSS}
</style>
</head>
<body>
  <a class="back-link" href="/#insights">&larr; Fightcade Lab Insights</a>
  <header>
    <h1>{uname} ({pchar}) — Visually Verified Habit Rollup</h1>
    <div class="sub">Hybrid VLM + RAM Pipeline (ADR-001): Hit-#1 health-loss anchoring · Pass-1 visual tagging on your top {ver_clips} clips · RAM setup search across {corpus_sz:,} top-rank points · Pass-2 visual relevance gate</div>
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-val">{num_sets}</div><div class="kpi-lbl">Personal Sets</div></div>
      <div class="kpi"><div class="kpi-val">{tot_rounds}</div><div class="kpi-lbl">Rounds Scanned</div></div>
      <div class="kpi"><div class="kpi-val">{tot_eps}</div><div class="kpi-lbl">Hit-#1 Loss Episodes</div></div>
      <div class="kpi"><div class="kpi-val" style="color:#6ee7b7">{ver_clips}</div><div class="kpi-lbl">Visually Verified Clips</div></div>
    </div>
  </header>

  {ai_html}

  <h2>Verified Personal Habits (Ranked by Total Health Lost)</h2>
  {''.join(habit_cards) if habit_cards else '<p class="sub">No visually verified habits found.</p>'}

  <h2>Health-Loss Breakdown by Opponent Character</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Opponent</th><th>Sets</th><th>Rounds</th><th>Damage Episodes</th><th>Total HP Lost</th></tr></thead>
      <tbody>{''.join(mu_rows)}</tbody>
    </table>
  </div>

  <h2>Included Personal Sets</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Date</th><th>Opponent</th><th>Score</th><th>Rounds</th><th>Episodes</th><th>Total HP Lost</th></tr></thead>
      <tbody>{''.join(set_rows)}</tbody>
    </table>
  </div>

<script>
function openHabit(rank) {{
  const det = document.getElementById('habit-details-' + rank);
  if (det) det.open = true;
}}
function togglePlay(id, btn) {{
  const v = document.getElementById(id);
  if (!v) return;
  if (v.paused) {{ v.play().catch(() => {{}}); btn.textContent = 'Pause'; }}
  else {{ v.pause(); btn.textContent = 'Play'; }}
}}
document.addEventListener('DOMContentLoaded', () => {{
  document.querySelectorAll('video').forEach(v => {{
    v.addEventListener('ended', () => {{
      const btn = document.querySelector(`button[onclick*="'${{v.id}}'"]`);
      if (btn) btn.textContent = 'Play';
    }});
  }});
}});
function copyFromData(btn) {{
  const text = btn.getAttribute('data-copy') || '';
  const done = () => {{ const old = btn.textContent; btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = old, 1200); }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  }} else {{
    fallbackCopy(text, done);
  }}
}}
function fallbackCopy(text, cb) {{
  const ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try {{ document.execCommand('copy'); }} catch (e) {{}}
  document.body.removeChild(ta);
  cb();
}}
{TAKEOVER_UI_JS}
</script>
</body>
</html>
"""


def run_single_match_hybrid(
    target_parquet: Path,
    *,
    perspective: int,
    parquet_dir: Path,
    corpus_index: HierarchicalContextIndex,
    cfg: SegmentationConfig,
    data_dir: Path = DEFAULT_DATA_DIR,
    out_dir: Path = Path("data/reports"),
    top_n: int = 5,
    min_hp_loss: int = 14,
    pro_candidates_per_moment: int = 2,
    render_clips: bool = True,
    summarize: bool = True,
    clip_workers: int = 3,
    gemini_workers: int = 2,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run the ADR-001 Hybrid VLM + RAM pipeline on a single match Parquet.

    Returns `(report_dict, stage_stats)`.
    """
    df = pd.read_parquet(target_parquet)
    qid = str(df["quark_id"].iloc[0]) if "quark_id" in df.columns and len(df) > 0 else target_parquet.stem
    rounds = int(df["round_id"].nunique()) if len(df) > 0 else 0

    s_pre = "p1" if perspective == 1 else "p2"
    o_pre = "p2" if perspective == 1 else "p1"
    self_char = int(df[f"{s_pre}_char"].iloc[0]) if len(df) > 0 else 0
    opp_char = int(df[f"{o_pre}_char"].iloc[0]) if len(df) > 0 else 0

    all_eps = detect_health_loss_episodes(
        df,
        perspective=perspective,
        quark_id=qid,
        min_hp_loss=min_hp_loss,
    )
    selected = select_seed_episodes(
        all_eps,
        seed_count=top_n,
        max_per_set=top_n,
        min_gf_sep=180,
    )

    clips_dir = data_dir / "clips"
    savestates_dir = data_dir / "savestates"
    stage_stats: dict[str, Any] = {
        "episodes_found": len(all_eps),
        "selected": len(selected),
        "user_clips": None,
        "pass1": None,
        "pro_clips": None,
        "pass2": None,
    }

    # Stage 2a: Render personal 6s clips (all in 1 silent FBNeo pass!)
    if render_clips and selected:
        u_reqs = [
            ClipRequest(
                quark_id=ep["quark_id"],
                target_frame=ep["t_cause_gf"],
                pre_frames=DEFAULT_PRE_FRAMES,
                post_frames=DEFAULT_POST_FRAMES,
                savestate_lead=DEFAULT_SAVESTATE_LEAD,
            )
            for ep in selected
        ]
        u_res, u_stats = render_clips_batch(
            u_reqs,
            clips_dir=clips_dir,
            savestates_dir=savestates_dir,
            max_workers=clip_workers,
            timeout_seconds=360,
        )
        stage_stats["user_clips"] = u_stats
        for ep in selected:
            key = f"{ep['quark_id']}_gf{ep['t_cause_gf']}"
            cr = u_res.get(key)
            ep["clip_rel_path"] = f"../clips/{cr.mp4_path.name}" if (cr and cr.mp4_path.exists()) else None

    # Stage 2b: Pass-1 Visual Tagging on personal clips
    if render_clips and summarize and selected:
        stage_stats["pass1"] = run_pass1_visual_tagging(
            selected,
            clips_dir=clips_dir,
            cache_path=out_dir / "gemini_hybrid_pass1_cache.json",
            max_workers=gemini_workers,
            models=models,
        )

    # Stage 3: RAM-Space Setup-Window Pro Solution Search
    pro_reqs: list[ClipRequest] = []
    for ep in selected:
        p_search = search_pro_setup_solutions(
            ep,
            target_parquet=target_parquet,
            parquet_dir=parquet_dir,
            corpus_index=corpus_index,
            cfg=cfg,
            max_candidates=pro_candidates_per_moment,
        )
        ep["retrieval_tier"] = p_search["retrieval_tier"]
        ep["pro_candidates"] = p_search["candidates"]
        ep["verified_pro_solutions"] = []
        ep["rejected_pro_count"] = 0
        for cand in p_search["candidates"]:
            pro_reqs.append(
                ClipRequest(
                    quark_id=cand["quark_id"],
                    target_frame=cand["game_frame"],
                    pre_frames=DEFAULT_PRE_FRAMES,
                    post_frames=DEFAULT_POST_FRAMES,
                    savestate_lead=DEFAULT_SAVESTATE_LEAD,
                )
            )

    # Stage 4: Render Pro Candidates + Pass-2 Strict Visual Relevance Gate
    if render_clips and pro_reqs:
        p_res, p_stats = render_clips_batch(
            pro_reqs,
            clips_dir=clips_dir,
            savestates_dir=savestates_dir,
            max_workers=clip_workers,
            timeout_seconds=360,
        )
        stage_stats["pro_clips"] = p_stats
        for ep in selected:
            for cand in ep.get("pro_candidates", []):
                key = f"{cand['quark_id']}_gf{cand['game_frame']}"
                cr = p_res.get(key)
                cand["clip_rel_path"] = f"../clips/{cr.mp4_path.name}" if (cr and cr.mp4_path.exists()) else None

        if summarize:
            # Reuse run_pass2_pro_verification by wrapping each episode as a 1-item habit container
            wrappers = [{"pro_seed_episode": ep, "pro_candidates": ep.get("pro_candidates", [])} for ep in selected]
            stage_stats["pass2"] = run_pass2_pro_verification(
                wrappers,
                clips_dir=clips_dir,
                cache_path=out_dir / "gemini_hybrid_pass2_cache.json",
                max_workers=gemini_workers,
                models=models,
            )
            for ep, wrap in zip(selected, wrappers):
                ep["verified_pro_solutions"] = wrap.get("verified_pro_solutions", [])
                ep["rejected_pro_count"] = int(wrap.get("rejected_pro_count", 0))
        else:
            for ep in selected:
                ep["verified_pro_solutions"] = list(ep.get("pro_candidates", []))

    # Normalize into report_ui's `moments` schema (with `pipeline="hybrid_v2"`)
    moments: list[dict[str, Any]] = []
    for ep in selected:
        vt = ep.get("visual_tag") or {}
        cat_label = str(vt.get("category_label") or "Health-Loss Episode")
        opp_move = str(vt.get("opp_move") or "").strip()
        label = (
            f"{cat_label} — {ep['self_char_name']} vs {ep['opp_char_name']}"
            + (f" ({opp_move})" if opp_move else "")
        )
        moments.append(
            {
                "pipeline": "hybrid_v2",
                "quark_id": ep["quark_id"],
                "perspective": int(ep["perspective"]),
                "round_id": int(ep["round_id"]),
                "game_frame": int(ep["t_cause_gf"]),
                "t_hit1_gf": int(ep["t_hit1_gf"]),
                "self_char": int(ep["self_char"]),
                "opp_char": int(ep["opp_char"]),
                "self_char_name": ep["self_char_name"],
                "opp_char_name": ep["opp_char_name"],
                "label": label,
                "category": str(vt.get("category", "other")),
                "category_label": cat_label,
                "hp_lost": int(ep["hp_lost"]),
                "hp_lost_pct": float(ep["hp_lost_pct"]),
                "combo_hits": int(ep["combo_hits"]),
                "was_ko": bool(ep["was_ko"]),
                "had_superfreeze": bool(ep["had_superfreeze"]),
                "retrieval_tier": str(ep.get("retrieval_tier", "none")),
                "visual_tag": vt,
                "verified_pro_solutions": ep.get("verified_pro_solutions", []),
                "rejected_pro_count": int(ep.get("rejected_pro_count", 0)),
                "regret": round(float(ep["hp_lost_pct"]) / 100.0, 4),
                "options": [],
            }
        )

    report = {
        "pipeline": "hybrid_v2",
        "target_quark_id": qid,
        "perspective": int(perspective),
        "self_char": self_char,
        "self_char_name": pretty_char(self_char),
        "opp_char": opp_char,
        "opp_char_name": pretty_char(opp_char),
        "total_rounds": rounds,
        "total_decision_points": len(all_eps),
        "total_damage_episodes": len(all_eps),
        "corpus_size": int(len(corpus_index.meta)),
        "moments": moments,
    }
    return report, stage_stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hybrid VLM + RAM Verified Habit Rollup (ADR-001).")
    parser.add_argument("--username", required=True, help="Fightcade player handle (e.g., player1)")
    parser.add_argument("--personal-dir", type=Path, default=Path("data/personal"))
    parser.add_argument("--parquet-dir", type=Path, default=Path("data/parquet"))
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=Path("data/reports"))
    parser.add_argument("--seed-count", type=int, default=20, help="Top personal health-loss clips to visually tag (default: 20)")
    parser.add_argument("--max-per-set", type=int, default=2, help="Max seed clips per personal set before backfill (default: 2)")
    parser.add_argument("--min-hp-loss", type=int, default=16, help="Minimum raw HP loss per episode (default: 16)")
    parser.add_argument("--max-pro-habits", type=int, default=4, help="Top verified habits to search pro setup solutions for (default: 4)")
    parser.add_argument("--pro-candidates-per-habit", type=int, default=2, help="Pro candidates per habit to Pass-2 visually verify (default: 2)")
    parser.add_argument("--clip-workers", type=int, default=3)
    parser.add_argument("--gemini-workers", type=int, default=2)
    parser.add_argument("--skip-pro-search", action="store_true", help="Skip Stage 3+4 pro setup search & Pass-2 verification")
    args = parser.parse_args(argv)

    t0 = time.perf_counter()
    sets = discover_player_sets(args.username, personal_dir=args.personal_dir, data_dir=args.data_dir)
    if not sets:
        print(f"No personal Parquet sets found for '{args.username}' in {args.personal_dir}")
        return 1

    # Stage 1: Objective Hit-#1 health-loss episode detection across all personal sets
    all_episodes: list[dict[str, Any]] = []
    personal_feats: dict[str, tuple[int, pd.DataFrame]] = {}
    pq_by_qid: dict[str, Path] = {}
    for s in sets:
        pq: Path = s["parquet_path"]
        pq_by_qid[s["quark_id"]] = pq
        df = pd.read_parquet(pq)
        s["rounds"] = int(df["round_id"].nunique()) if len(df) else 0
        personal_feats[s["quark_id"]] = (s["perspective"], extract_player_features(df, perspective=s["perspective"]))
        eps = detect_health_loss_episodes(
            df,
            perspective=s["perspective"],
            quark_id=s["quark_id"],
            min_hp_loss=args.min_hp_loss,
        )
        for ep in eps:
            ep["match_date"] = s["match_date"]
            ep["opp_player_name"] = s["opp_player_name"]
        all_episodes.extend(eps)

    seeds = select_seed_episodes(all_episodes, seed_count=args.seed_count, max_per_set=args.max_per_set)
    print(
        f"[Stage 1] Found {len(all_episodes)} Hit-#1 damage episodes across {len(sets)} sets "
        f"-> selected top {len(seeds)} health-loss seeds ({time.perf_counter()-t0:.1f}s)"
    )

    # Render personal seed 6s clips
    t_clip1 = time.perf_counter()
    clips_dir = args.data_dir / "clips"
    savestates_dir = args.data_dir / "savestates"
    seed_reqs = [
        ClipRequest(
            quark_id=ep["quark_id"],
            target_frame=ep["t_cause_gf"],
            pre_frames=DEFAULT_PRE_FRAMES,
            post_frames=DEFAULT_POST_FRAMES,
            savestate_lead=DEFAULT_SAVESTATE_LEAD,
        )
        for ep in seeds
    ]
    clip_res, cstats1 = render_clips_batch(
        seed_reqs,
        clips_dir=clips_dir,
        savestates_dir=savestates_dir,
        max_workers=args.clip_workers,
        timeout_seconds=420,
    )
    for ep in seeds:
        key = f"{ep['quark_id']}_gf{ep['t_cause_gf']}"
        res = clip_res.get(key)
        ep["clip_rel_path"] = f"../clips/{res.mp4_path.name}" if (res and res.mp4_path.exists()) else None
    print(
        f"[Stage 2a] Personal seed clips in {time.perf_counter()-t_clip1:.1f}s "
        f"(req={cstats1['requested']}, cached={cstats1['cached']}, rendered={cstats1['rendered']}, failed={cstats1['failed']})"
    )

    # Stage 2b: Pass-1 Visual Tagging on personal clips ONLY
    t_p1 = time.perf_counter()
    p1_stats = run_pass1_visual_tagging(
        seeds,
        clips_dir=clips_dir,
        cache_path=args.out_dir / "gemini_hybrid_pass1_cache.json",
        max_workers=args.gemini_workers,
    )
    print(
        f"[Stage 2b] Pass-1 visual tagging in {time.perf_counter()-t_p1:.1f}s "
        f"(cached={p1_stats['cached']}, called={p1_stats['called']}, failed={p1_stats['failed']})"
    )

    # Load pre-warmed pro corpus (RAM-space only!)
    pro_files = sorted(args.parquet_dir.glob("*.parquet"))
    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    corpus, hit = load_or_build_corpus(
        pro_files,
        config=cfg,
        both_perspectives=True,
        cache_dir=args.data_dir / "index",
    )
    pro_index = HierarchicalContextIndex(corpus.meta, corpus.vectors)

    rollup = aggregate_verified_habits(
        args.username,
        sets,
        all_episodes,
        seeds,
        corpus_size=len(corpus.meta),
    )

    # Stage 3: RAM-Space Personal Recurrence + Pro Setup Solution Search
    t_s3 = time.perf_counter()
    pro_reqs: list[ClipRequest] = []
    for h in rollup["habits"][: args.max_pro_habits]:
        seed_ep = h.get("pro_seed_episode")
        if not seed_ep:
            continue
        rec = count_personal_ram_recurrence(seed_ep, personal_feats)
        h["ram_recurrence_extra"] = int(rec["ram_matches"])
        if not args.skip_pro_search:
            tpq = pq_by_qid.get(seed_ep["quark_id"])
            if tpq and tpq.exists():
                p_search = search_pro_setup_solutions(
                    seed_ep,
                    target_parquet=tpq,
                    parquet_dir=args.parquet_dir,
                    corpus_index=pro_index,
                    cfg=cfg,
                    max_candidates=args.pro_candidates_per_habit,
                )
                h["pro_candidates"] = p_search["candidates"]
                for cand in p_search["candidates"]:
                    pro_reqs.append(
                        ClipRequest(
                            quark_id=cand["quark_id"],
                            target_frame=cand["game_frame"],
                            pre_frames=DEFAULT_PRE_FRAMES,
                            post_frames=DEFAULT_POST_FRAMES,
                            savestate_lead=DEFAULT_SAVESTATE_LEAD,
                        )
                    )
    print(f"[Stage 3] RAM setup search + personal recurrence in {time.perf_counter()-t_s3:.1f}s ({len(pro_reqs)} pro candidates)")

    # Stage 4: Render pro candidates + Pass-2 Visual Relevance Gate
    if pro_reqs and not args.skip_pro_search:
        t_c2 = time.perf_counter()
        pro_clip_res, cstats2 = render_clips_batch(
            pro_reqs,
            clips_dir=clips_dir,
            savestates_dir=savestates_dir,
            max_workers=args.clip_workers,
            timeout_seconds=360,
        )
        for h in rollup["habits"][: args.max_pro_habits]:
            for cand in h.get("pro_candidates", []):
                key = f"{cand['quark_id']}_gf{cand['game_frame']}"
                res = pro_clip_res.get(key)
                cand["clip_rel_path"] = f"../clips/{res.mp4_path.name}" if (res and res.mp4_path.exists()) else None
        print(
            f"[Stage 4a] Pro candidate clips in {time.perf_counter()-t_c2:.1f}s "
            f"(req={cstats2['requested']}, cached={cstats2['cached']}, rendered={cstats2['rendered']}, failed={cstats2['failed']})"
        )

        t_p2 = time.perf_counter()
        p2_stats = run_pass2_pro_verification(
            rollup["habits"][: args.max_pro_habits],
            clips_dir=clips_dir,
            cache_path=args.out_dir / "gemini_hybrid_pass2_cache.json",
            max_workers=args.gemini_workers,
        )
        print(
            f"[Stage 4b] Pass-2 visual relevance gate in {time.perf_counter()-t_p2:.1f}s "
            f"(checked={p2_stats['checked']}, passed={p2_stats['passed']}, rejected={p2_stats['rejected']}, cached={p2_stats['cached']})"
        )

    # Stage 5: Grounded Executive Synthesis & HTML Rollup
    rollup["ai_synthesis"] = synthesize_verified_habits_with_gemini(
        rollup,
        cache_path=args.out_dir / "gemini_hybrid_synth_cache.json",
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = build_verified_habits_stem(rollup)
    json_path = args.out_dir / f"{stem}.json"
    html_path = args.out_dir / f"{stem}.html"
    json_path.write_text(json.dumps(rollup, indent=2), encoding="utf-8")
    html_path.write_text(render_verified_habits_html(rollup, data_dir=args.data_dir), encoding="utf-8")
    hub_path = build_reports_index_html(args.out_dir)

    print(f"\nWrote verified habits JSON: {json_path}")
    print(f"Wrote verified habits HTML: {html_path}")
    print(f"Updated Reports Hub       : {hub_path}")
    print(f"Total elapsed             : {time.perf_counter()-t0:.1f}s")
    print("\n=== Visually Verified Personal Habits ===")
    for i, h in enumerate(rollup["habits"], 1):
        vp = len(h.get("verified_pro_solutions", []))
        rj = int(h.get("rejected_pro_count", 0))
        print(
            f"  {i}. {h['label']}: {h['verified_clips']} clips across {h['sets_affected']}/{h['total_sets']} sets "
            f"| -{h['total_hp_lost']} HP (avg -{h['avg_hp_lost_pct']}%) | pro_verified={vp} (filtered={rj})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
