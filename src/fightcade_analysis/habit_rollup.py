"""Multi-Set Personal Habit & Recurring Leak Rollup.

Diagnoses all personal Parquet sets for a Fightcade player against the pre-warmed pro corpus,
aggregates recurring high-regret situations across sets to separate true habits from one-off
mistakes, generates a mobile-friendly HTML dashboard on the Reports Hub, and optionally
synthesizes a prioritized training plan via Gemini.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path
import re
import time
from typing import Any

import pandas as pd
import requests

from fightcade_analysis.clip_renderer import (
    DEFAULT_DATA_DIR,
    DEFAULT_POST_FRAMES,
    DEFAULT_PRE_FRAMES,
    DEFAULT_SAVESTATE_LEAD,
    ClipRequest,
    render_clips_batch,
)
from fightcade_analysis.coaching import diagnose_replay, pretty_char
from fightcade_analysis.gemini_summary import DEFAULT_MODEL_CHAIN, load_gemini_api_key
from fightcade_analysis.index import HierarchicalContextIndex, load_or_build_corpus
from fightcade_analysis.naming import (
    build_coaching_report_stem,
    build_habit_rollup_stem,
    build_reports_index_html,
    extract_match_date,
    lookup_replay_metadata,
)
from fightcade_analysis.segmentation import SegmentationConfig
from fightcade_analysis.takeover import (
    TAKEOVER_UI_CSS,
    TAKEOVER_UI_JS,
    render_takeover_buttons_html,
)

_PX_SUFFIX_RE = re.compile(r"\s*\(\d+\s*px\)\s*$", re.IGNORECASE)


def normalize_interaction_bucket(interaction: str) -> str:
    """Collapse per-frame pixel suffixes like 'Neutral Footsies (142px)' -> 'Neutral Footsies'."""
    cleaned = _PX_SUFFIX_RE.sub("", str(interaction or "").strip()).strip()
    return cleaned or "Other Interaction"


def discover_player_sets(
    username: str | None,
    *,
    personal_dir: Path,
    data_dir: Path = DEFAULT_DATA_DIR,
    default_perspective: int = 1,
) -> list[dict[str, Any]]:
    """Find personal Parquet files and resolve each set's player perspective & opponent metadata."""
    if not personal_dir.exists():
        return []
    uname_low = (username or "").strip().lower()
    out: list[dict[str, Any]] = []

    for pq in sorted(personal_dir.glob("*.parquet")):
        qid = pq.stem
        meta = lookup_replay_metadata(qid, data_dir=data_dir)
        p1_name = str(meta.get("p1_name") or "")
        p2_name = str(meta.get("p2_name") or "")

        if uname_low:
            if p1_name.lower() == uname_low:
                persp = 1
                self_name = p1_name
                opp_name = p2_name or "P2"
            elif p2_name.lower() == uname_low:
                persp = 2
                self_name = p2_name
                opp_name = p1_name or "P1"
            elif meta:
                # Manifest exists for this replay, but belongs to a different player
                continue
            else:
                persp = default_perspective
                self_name = username or f"P{persp}"
                opp_name = "P2" if persp == 1 else "P1"
        else:
            persp = default_perspective
            self_name = (p1_name if persp == 1 else p2_name) or f"P{persp}"
            opp_name = (p2_name if persp == 1 else p1_name) or ("P2" if persp == 1 else "P1")

        self_score = meta.get("p1_score" if persp == 1 else "p2_score")
        opp_score = meta.get("p2_score" if persp == 1 else "p1_score")
        out.append(
            {
                "parquet_path": pq,
                "quark_id": qid,
                "perspective": persp,
                "self_player_name": self_name,
                "opp_player_name": opp_name,
                "match_date": extract_match_date(qid, meta),
                "ranked": meta.get("ranked"),
                "duration": meta.get("duration"),
                "self_score": self_score,
                "opp_score": opp_score,
            }
        )

    out.sort(key=lambda x: (x["match_date"], x["quark_id"]), reverse=True)
    return out


def aggregate_habit_rollup(
    username: str,
    set_diagnoses: list[dict[str, Any]],
    *,
    min_regret: float = 0.06,
    reports_dir: Path | None = None,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Aggregate per-set coaching diagnoses into cross-set recurring habit/leak buckets."""
    existing_reports: dict[str, str] = {}
    if reports_dir and reports_dir.exists():
        for jp in sorted(reports_dir.glob("*.json")):
            if jp.name == "gemini_summary_cache.json":
                continue
            hp = jp.with_suffix(".html")
            if not hp.exists():
                continue
            try:
                doc = json.loads(jp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(doc, dict) and doc.get("report_type", "full_set") == "full_set" and doc.get("target_quark_id"):
                existing_reports[str(doc["target_quark_id"])] = hp.name

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    matchup_sets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "opp_char_name": "",
            "sets": 0,
            "rounds": 0,
            "decision_points": 0,
            "leak_count": 0,
            "total_regret": 0.0,
            "bucket_regret": Counter(),
        }
    )

    set_summaries: list[dict[str, Any]] = []
    char_counter: Counter[str] = Counter()
    total_rounds = 0
    total_dps = 0
    total_leaks = 0
    total_regret_all = 0.0

    for entry in set_diagnoses:
        rep = entry["report"]
        qid = str(rep["target_quark_id"])
        persp = int(rep["perspective"])
        self_char = str(rep.get("self_char_name", ""))
        opp_char = str(rep.get("opp_char_name", ""))
        rounds = int(entry.get("rounds", 0))
        dps = int(rep.get("total_decision_points", 0))

        char_counter[self_char] += max(rounds, 1)
        total_rounds += rounds
        total_dps += dps

        leaks = [m for m in rep.get("moments", []) if float(m.get("regret", 0.0)) >= min_regret]
        set_regret = sum(float(m["regret"]) for m in leaks)
        total_leaks += len(leaks)
        total_regret_all += set_regret

        # Link to full-set report if already present (or expected stem)
        report_href = existing_reports.get(qid)
        if not report_href and reports_dir:
            cand_stem = build_coaching_report_stem(dict(rep), data_dir=data_dir)
            if (reports_dir / f"{cand_stem}.html").exists():
                report_href = f"{cand_stem}.html"

        ms = matchup_sets[opp_char]
        ms["opp_char_name"] = opp_char
        ms["sets"] += 1
        ms["rounds"] += rounds
        ms["decision_points"] += dps
        ms["leak_count"] += len(leaks)
        ms["total_regret"] += set_regret

        for m in leaks:
            inter_clean = normalize_interaction_bucket(str(m.get("interaction", "")))
            regime = str(m.get("regime", "neutral"))
            best_opt = m["options"][0] if m.get("options") else None
            best_proof = None
            if best_opt and best_opt.get("proofs"):
                pr0 = best_opt["proofs"][0]
                best_proof = {
                    "quark_id": str(pr0.get("quark_id", "")),
                    "perspective": int(pr0.get("perspective", 1)),
                    "round_id": int(pr0.get("round_id", 0)),
                    "game_frame": int(pr0.get("game_frame", 0)),
                    "self_char_name": str(pr0.get("self_char_name", "")),
                    "opp_char_name": str(pr0.get("opp_char_name", "")),
                    "action_coarse": str(pr0.get("action_coarse", best_opt.get("action_coarse", ""))),
                    "outcome_value": float(pr0.get("outcome_value", 0.0)),
                    "similarity_pct": int(pr0.get("similarity_pct", 0)),
                    "clip_rel_path": None,
                }
            enriched_m = {
                "quark_id": qid,
                "perspective": persp,
                "match_date": entry.get("match_date", ""),
                "opp_player_name": entry.get("opp_player_name", "P2"),
                "self_char_name": str(m.get("self_char_name", self_char)),
                "opp_char_name": str(m.get("opp_char_name", opp_char)),
                "round_id": int(m.get("round_id", 0)),
                "game_frame": int(m.get("game_frame", 0)),
                "label": str(m.get("label", "")),
                "interaction": inter_clean,
                "regime": regime,
                "user_action": str(m.get("user_action", "")),
                "actual_value": float(m.get("actual_value", 0.0)),
                "actual_hp_swing": float(m.get("actual_hp_swing", 0.0)),
                "ev_corpus": float(m.get("ev_corpus", 0.0)),
                "regret": float(m.get("regret", 0.0)),
                "match_similarity_pct": int(m.get("match_similarity_pct", 0)),
                "best_alt_action": str(best_opt["action_coarse"]) if best_opt else "",
                "best_alt_ev": float(best_opt["ev"]) if best_opt else 0.0,
                "best_alt_gain": float(best_opt["ev_gain_vs_user"]) if best_opt else 0.0,
                "best_proof": best_proof,
                "clip_rel_path": None,
                "report_href": report_href,
            }
            buckets[(inter_clean, regime)].append(enriched_m)
            ms["bucket_regret"][inter_clean] += float(m["regret"])

        top_leak = leaks[0] if leaks else None
        set_summaries.append(
            {
                "quark_id": qid,
                "match_date": entry.get("match_date", ""),
                "perspective": persp,
                "self_player_name": entry.get("self_player_name", username),
                "opp_player_name": entry.get("opp_player_name", "P2"),
                "self_char_name": self_char,
                "opp_char_name": opp_char,
                "ranked": entry.get("ranked"),
                "self_score": entry.get("self_score"),
                "opp_score": entry.get("opp_score"),
                "rounds": rounds,
                "decision_points": dps,
                "leak_count": len(leaks),
                "total_regret": round(set_regret, 3),
                "top_leak_label": normalize_interaction_bucket(top_leak["interaction"]) if top_leak else "None",
                "top_leak_regret": round(float(top_leak["regret"]), 3) if top_leak else 0.0,
                "report_href": report_href,
            }
        )

    habits: list[dict[str, Any]] = []
    num_sets = len(set_diagnoses)
    for (inter_clean, regime), items in buckets.items():
        items_sorted = sorted(items, key=lambda x: x["regret"], reverse=True)
        unique_sets = sorted({x["quark_id"] for x in items_sorted})
        n_occ = len(items_sorted)
        tot_reg = sum(x["regret"] for x in items_sorted)
        avg_reg = tot_reg / n_occ
        avg_actual = sum(x["actual_value"] for x in items_sorted) / n_occ
        avg_ev = sum(x["ev_corpus"] for x in items_sorted) / n_occ
        avg_hp = sum(x["actual_hp_swing"] for x in items_sorted) / n_occ

        user_acts = Counter(x["user_action"] for x in items_sorted if x["user_action"])
        pro_alts = Counter(x["best_alt_action"] for x in items_sorted if x["best_alt_action"])
        opps = Counter(x["opp_char_name"] for x in items_sorted if x["opp_char_name"])

        # Pick diverse representative examples (prefer distinct sets first)
        examples: list[dict[str, Any]] = []
        seen_qids: set[str] = set()
        for it in items_sorted:
            if it["quark_id"] not in seen_qids:
                examples.append(it)
                seen_qids.add(it["quark_id"])
            if len(examples) >= 4:
                break
        if len(examples) < 4:
            for it in items_sorted:
                if it not in examples:
                    examples.append(it)
                if len(examples) >= 4:
                    break

        habits.append(
            {
                "interaction": inter_clean,
                "regime": regime,
                "occurrences": n_occ,
                "sets_affected": len(unique_sets),
                "total_sets": num_sets,
                "set_spread_pct": int(round((100.0 * len(unique_sets)) / max(num_sets, 1))),
                "is_recurring": len(unique_sets) >= 2 or n_occ >= 3,
                "total_regret": round(tot_reg, 3),
                "avg_regret": round(avg_reg, 3),
                "avg_actual_value": round(avg_actual, 3),
                "avg_corpus_ev": round(avg_ev, 3),
                "avg_hp_swing": round(avg_hp, 3),
                "top_user_actions": [{"action": a, "count": c} for a, c in user_acts.most_common(3)],
                "top_pro_alternatives": [{"action": a, "count": c} for a, c in pro_alts.most_common(3)],
                "top_opponents": [{"char": ch, "count": c} for ch, c in opps.most_common(4)],
                "examples": examples,
            }
        )

    # Sort recurring multi-set leaks by total regret impact first
    habits.sort(key=lambda h: (h["sets_affected"] >= 2, h["total_regret"], h["occurrences"]), reverse=True)

    matchups: list[dict[str, Any]] = []
    for opp_char, ms in matchup_sets.items():
        top_bucket = ms["bucket_regret"].most_common(1)
        matchups.append(
            {
                "opp_char_name": opp_char,
                "sets": ms["sets"],
                "rounds": ms["rounds"],
                "decision_points": ms["decision_points"],
                "leak_count": ms["leak_count"],
                "total_regret": round(ms["total_regret"], 3),
                "avg_regret_per_leak": round(ms["total_regret"] / max(ms["leak_count"], 1), 3),
                "top_leak": top_bucket[0][0] if top_bucket else "None",
            }
        )
    matchups.sort(key=lambda x: (x["total_regret"], x["sets"]), reverse=True)

    primary_char = char_counter.most_common(1)[0][0] if char_counter else "Unknown"
    latest_date = max((s["match_date"] for s in set_summaries if s.get("match_date")), default=datetime.now().strftime("%Y-%m-%d"))

    return {
        "report_type": "habit_rollup",
        "username": username,
        "self_player_name": username,
        "primary_char_name": primary_char,
        "match_date": latest_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "num_sets": num_sets,
        "total_rounds": total_rounds,
        "total_decision_points": total_dps,
        "total_leaks": total_leaks,
        "total_regret": round(total_regret_all, 3),
        "corpus_size": int(set_diagnoses[0]["report"].get("corpus_size", 0)) if set_diagnoses else 0,
        "min_regret_threshold": min_regret,
        "habits": habits,
        "matchups": matchups,
        "sets": set_summaries,
        "ai_synthesis": None,
    }


def synthesize_rollup_with_gemini(
    rollup: dict[str, Any],
    *,
    cache_path: Path | None = None,
    models: tuple[str, ...] = DEFAULT_MODEL_CHAIN,
    timeout: float = 45.0,
) -> dict[str, Any] | None:
    """Ask Gemini for an executive Top-3 Habit & Training Drill Plan from the aggregated stats."""
    api_key = load_gemini_api_key()
    if not api_key:
        return None

    top3_habits = rollup.get("habits", [])[:3]
    compact_payload = {
        "synthesis_version": "v2_aligned_top3",
        "username": rollup["username"],
        "primary_char": rollup["primary_char_name"],
        "num_sets": rollup["num_sets"],
        "total_rounds": rollup["total_rounds"],
        "total_leaks": rollup["total_leaks"],
        "top3_habits_in_order": [
            {
                "habit_rank": i,
                "interaction": h["interaction"],
                "regime": h["regime"],
                "occurrences": h["occurrences"],
                "sets_affected": f"{h['sets_affected']}/{h['total_sets']}",
                "total_regret": h["total_regret"],
                "avg_actual_value": h["avg_actual_value"],
                "avg_corpus_ev": h["avg_corpus_ev"],
                "avg_hp_swing": h["avg_hp_swing"],
                "user_actions": h["top_user_actions"],
                "pro_alternatives": h["top_pro_alternatives"],
                "opponents": h["top_opponents"],
            }
            for i, h in enumerate(top3_habits, 1)
        ],
        "matchups": rollup.get("matchups", [])[:8],
    }

    cache_key = hashlib.sha256(json.dumps(compact_payload, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    cache: dict[str, Any] = {}
    if cache_path and cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}
    if cache_key in cache:
        return dict(cache[cache_key])

    prompt = (
        "You are an expert Street Fighter III: 3rd Strike coach reviewing a multi-set habit/leak rollup "
        f"for player '{rollup['username']}' ({rollup['primary_char_name']}) across {rollup['num_sets']} sets "
        f"({rollup['total_rounds']} rounds) compared against an S/A-rank pro corpus.\n"
        "IMPORTANT ALIGNMENT RULE: `top_priorities` MUST map 1-to-1 in EXACT ORDER to the 3 items in "
        "`top3_habits_in_order` (Priority #1 = habit_rank 1, Priority #2 = habit_rank 2, Priority #3 = habit_rank 3). "
        "Do NOT skip a habit or substitute a lower-ranked habit — if user_actions and pro_alternatives share the same "
        "coarse label (e.g. both `air_attack`), diagnose the execution/spacing/timing gap shown by `avg_hp_swing` and `avg_actual_value` vs `avg_corpus_ev`.\n"
        "Using ONLY the grounded data below, return strict JSON with keys:\n"
        '  "headline": 1-sentence executive diagnosis of habit_rank 1,\n'
        '  "top_priorities": array of 3 objects in exact order (habit_rank 1, 2, 3), each with '
        '{"habit_rank": int, "interaction": exact interaction string from data, "title": short coaching title, '
        '"diagnosis": 1-2 sentences citing exact numbers/actions, "drill": 1 concrete actionable training drill or in-match rule},\n'
        '  "matchup_note": 1-2 sentences highlighting which opponent matchups cost the most regret.\n\n'
        f"DATA:\n{json.dumps(compact_payload, indent=2)}"
    )

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.25,
            "responseMimeType": "application/json",
        },
    }

    last_err = ""
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(3):
            try:
                resp = requests.post(
                    url,
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                    json=body,
                    timeout=timeout,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    time.sleep(1.5 * (2**attempt))
                    continue
                if resp.status_code == 404:
                    break
                resp.raise_for_status()
                data = resp.json()
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    parsed["model"] = model
                    if cache_path:
                        cache[cache_key] = parsed
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_text(json.dumps(cache, indent=2), encoding="utf-8")
                    return parsed
            except Exception as exc:  # noqa: BLE001
                last_err = f"{model}: {exc}"
                break

    if last_err:
        print(f"WARNING: Gemini rollup synthesis failed ({last_err})")
    return None


def attach_rollup_clips(
    rollup: dict[str, Any],
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    max_clip_habits: int = 6,
    clips_per_habit: int = 3,
    pro_clips_per_habit: int = 0,
    clip_workers: int = 3,
    timeout_seconds: int = 420,
) -> dict[str, int]:
    """Batch-render 6s MP4 clips + instant .fs savestates for cited habit examples (and top pro proofs)."""
    clips_dir = data_dir / "clips"
    savestates_dir = data_dir / "savestates"
    reqs: list[ClipRequest] = []
    targets: list[tuple[dict[str, Any], str]] = []  # (target_dict, key)

    for h in rollup.get("habits", [])[:max_clip_habits]:
        for ex_idx, ex in enumerate(h.get("examples", [])[:clips_per_habit]):
            qid = str(ex.get("quark_id", ""))
            gf = int(ex.get("game_frame", 0))
            if qid and gf > 0:
                key = f"{qid}_gf{gf}"
                reqs.append(
                    ClipRequest(
                        quark_id=qid,
                        target_frame=gf,
                        pre_frames=DEFAULT_PRE_FRAMES,
                        post_frames=DEFAULT_POST_FRAMES,
                        savestate_lead=DEFAULT_SAVESTATE_LEAD,
                    )
                )
                targets.append((ex, key))

            if ex_idx < pro_clips_per_habit and isinstance(ex.get("best_proof"), dict):
                pr = ex["best_proof"]
                pqid = str(pr.get("quark_id", ""))
                pgf = int(pr.get("game_frame", 0))
                if pqid and pgf > 0:
                    pkey = f"{pqid}_gf{pgf}"
                    reqs.append(
                        ClipRequest(
                            quark_id=pqid,
                            target_frame=pgf,
                            pre_frames=DEFAULT_PRE_FRAMES,
                            post_frames=DEFAULT_POST_FRAMES,
                            savestate_lead=DEFAULT_SAVESTATE_LEAD,
                        )
                    )
                    targets.append((pr, pkey))

    if not reqs:
        return {"requested": 0, "cached": 0, "rendered": 0, "failed": 0, "fbneo_passes": 0}

    results, stats = render_clips_batch(
        reqs,
        clips_dir=clips_dir,
        savestates_dir=savestates_dir,
        max_workers=max(1, int(clip_workers)),
        timeout_seconds=timeout_seconds,
    )

    for target_dict, key in targets:
        res = results.get(key)
        if res and res.mp4_path.exists():
            target_dict["clip_rel_path"] = f"../clips/{res.mp4_path.name}"

    return stats


def render_habit_rollup_html(rollup: dict[str, Any]) -> str:
    """Render a responsive, phone-friendly HTML Multi-Set Habit & Leak Rollup dashboard."""
    uname = html.escape(str(rollup["username"]))
    pchar = html.escape(str(rollup["primary_char_name"]))
    num_sets = int(rollup["num_sets"])
    tot_rounds = int(rollup["total_rounds"])
    tot_dps = int(rollup["total_decision_points"])
    tot_leaks = int(rollup["total_leaks"])
    tot_reg = float(rollup["total_regret"])
    corpus_sz = int(rollup.get("corpus_size", 0))

    # Optional Gemini executive synthesis card (strictly linked to matching Habit Cards below)
    ai_html = ""
    ai = rollup.get("ai_synthesis")
    if isinstance(ai, dict) and ai.get("headline"):
        habits_list = rollup.get("habits", [])
        habit_idx_by_name = {str(h.get("interaction", "")).lower(): i for i, h in enumerate(habits_list, 1)}
        prio_cards = []
        for idx, p in enumerate(ai.get("top_priorities", [])[:3], 1):
            p_inter = str(p.get("interaction") or "").strip()
            target_rank = int(p.get("habit_rank") or habit_idx_by_name.get(p_inter.lower(), idx))
            if target_rank < 1 or target_rank > len(habits_list):
                target_rank = idx
            matched_inter = habits_list[target_rank - 1]["interaction"] if target_rank <= len(habits_list) else p_inter
            prio_cards.append(
                f"""
                <div class="prio-card">
                  <div class="prio-title">#{idx} {html.escape(str(p.get('title', '')))}</div>
                  <div class="prio-map">
                    <a class="jump-link" href="#habit-{target_rank}" onclick="openHabit({target_rank})">Evidence: Habit #{target_rank} — {html.escape(matched_inter)} &darr;</a>
                  </div>
                  <div class="prio-diag">{html.escape(str(p.get('diagnosis', '')))}</div>
                  <div class="prio-drill"><strong>Drill / Rule:</strong> {html.escape(str(p.get('drill', '')))}</div>
                </div>
                """
            )
        mu_note = html.escape(str(ai.get("matchup_note", "")))
        ai_html = f"""
        <section class="ai-box">
          <div class="ai-head">Coach Synthesis <span class="model-tag">{html.escape(str(ai.get('model', 'Gemini')))}</span></div>
          <p class="ai-headline">{html.escape(str(ai['headline']))}</p>
          <div class="prio-grid">{''.join(prio_cards)}</div>
          {f'<p class="ai-mu"><strong>Matchup Focus:</strong> {mu_note}</p>' if mu_note else ''}
        </section>
        """

    # Recurring Habit Cards
    habit_cards: list[str] = []
    max_reg = max((float(h["total_regret"]) for h in rollup.get("habits", [])), default=1.0) or 1.0
    for idx, h in enumerate(rollup.get("habits", [])[:12], 1):
        bar_w = max(8, min(100, int(round(100.0 * float(h["total_regret"]) / max_reg))))
        rec_badge = (
            '<span class="badge badge-hot">Recurring Habit</span>'
            if h.get("is_recurring")
            else '<span class="badge badge-one">Isolated</span>'
        )
        user_acts = ", ".join(f"<code>{html.escape(a['action'])}</code> ({a['count']})" for a in h["top_user_actions"]) or "—"
        pro_alts = ", ".join(f"<code>{html.escape(a['action'])}</code> ({a['count']})" for a in h["top_pro_alternatives"]) or "—"
        opps_str = ", ".join(f"{html.escape(o['char'])} ({o['count']})" for o in h["top_opponents"]) or "—"

        ex_cards: list[str] = []
        ex_rows: list[str] = []
        has_any_clip = any(
            ex.get("clip_rel_path") or (isinstance(ex.get("best_proof"), dict) and ex["best_proof"].get("clip_rel_path"))
            for ex in h.get("examples", [])
        )
        for ex_idx, ex in enumerate(h.get("examples", [])):
            cmd = (
                f".\\scripts\\takeover.ps1 -QuarkId {ex['quark_id']} "
                f"-Frame {ex['game_frame']} -Player {ex['perspective']} "
                f"-Label \"{ex['interaction']}\""
            )
            link_html = (
                f'<a class="rep-link" href="{html.escape(ex["report_href"])}">vs {html.escape(ex["opp_player_name"])} ({html.escape(ex["opp_char_name"])})</a>'
                if ex.get("report_href")
                else f"vs {html.escape(ex['opp_player_name'])} ({html.escape(ex['opp_char_name'])})"
            )
            user_clip = ex.get("clip_rel_path")
            pr = ex.get("best_proof") if isinstance(ex.get("best_proof"), dict) else None
            pro_clip = pr.get("clip_rel_path") if pr else None

            if user_clip or pro_clip:
                vid_panes: list[str] = []
                if user_clip:
                    vid_id = f"vid_h{idx}_e{ex_idx}_u"
                    vid_panes.append(
                        f"""
                        <div class="vid-pane">
                          <div class="vid-cap">
                            <span class="pill-you">YOUR CHOICE</span>
                            <code>{html.escape(ex['user_action'])}</code> (HP {ex['actual_hp_swing']*100:+.0f}%)
                            <button class="play-btn" onclick="togglePlay('{vid_id}', this)">Play</button>
                          </div>
                          <video id="{vid_id}" controls playsinline preload="metadata" src="{html.escape(str(user_clip))}"></video>
                        </div>
                        """
                    )
                if pro_clip and pr:
                    pvid_id = f"vid_h{idx}_e{ex_idx}_p"
                    pro_cmd = (
                        f".\\scripts\\takeover.ps1 -QuarkId {pr['quark_id']} "
                        f"-Frame {pr['game_frame']} -Player {pr['perspective']} "
                        f"-Label \"Top-Rank {pr['action_coarse']}\""
                    )
                    vid_panes.append(
                        f"""
                        <div class="vid-pane">
                          <div class="vid-cap">
                            <span class="pill-pro">TOP-RANK ANSWER ({pr['similarity_pct']}% match)</span>
                            <code>{html.escape(pr['action_coarse'])}</code> (EV {pr['outcome_value']:+.2f})
                            <button class="play-btn" onclick="togglePlay('{pvid_id}', this)">Play</button>
                          </div>
                          <video id="{pvid_id}" controls playsinline preload="metadata" src="{html.escape(str(pro_clip))}"></video>
                          <div class="vid-foot">
                            <span class="muted">{html.escape(pr['self_char_name'])} vs {html.escape(pr['opp_char_name'])} · R{pr['round_id']} gf{pr['game_frame']}</span>
                            {render_takeover_buttons_html(str(pr['quark_id']), int(pr['game_frame']), int(pr['perspective']), f"Top-Rank {pr['action_coarse']}", pro_cmd, is_pro=True)}
                          </div>
                        </div>
                        """
                    )
                ex_cards.append(
                    f"""
                    <div class="ex-clip-card">
                      <div class="ex-clip-head">
                        <div>
                          <span class="mono">{html.escape(ex['match_date'])}</span> · {link_html}
                          <span class="muted">R{ex['round_id']} gf{ex['game_frame']}</span>
                        </div>
                        <div class="ex-clip-actions">
                          <span class="neg">Regret +{ex['regret']:.2f}</span>
                          {render_takeover_buttons_html(str(ex['quark_id']), int(ex['game_frame']), int(ex['perspective']), str(ex['interaction']), cmd, is_pro=False)}
                        </div>
                      </div>
                      <div class="vid-grid">{''.join(vid_panes)}</div>
                    </div>
                    """
                )
            else:
                ex_rows.append(
                    f"""
                    <tr>
                      <td class="mono">{html.escape(ex['match_date'])}</td>
                      <td>{link_html} <span class="muted">R{ex['round_id']} gf{ex['game_frame']}</span></td>
                      <td><code>{html.escape(ex['user_action'])}</code> → top-rank <code>{html.escape(ex['best_alt_action'])}</code></td>
                      <td class="neg">+{ex['regret']:.2f}</td>
                      <td>{render_takeover_buttons_html(str(ex['quark_id']), int(ex['game_frame']), int(ex['perspective']), str(ex['interaction']), cmd, is_pro=False)}</td>
                    </tr>
                    """
                )

        ex_table_html = (
            f"""
            <div class="table-wrap">
              <table class="mini-tbl">
                <thead><tr><th>Date</th><th>Match &amp; Frame</th><th>You → Top-Rank</th><th>Regret</th><th>Drill</th></tr></thead>
                <tbody>{''.join(ex_rows)}</tbody>
              </table>
            </div>
            """
            if ex_rows
            else ""
        )
        open_attr = " open" if (idx == 1 and has_any_clip) else ""
        clip_count_lbl = f" ({len(ex_cards)} with 6s video)" if ex_cards else ""

        habit_cards.append(
            f"""
            <article class="habit-card" id="habit-{idx}">
              <div class="habit-top">
                <div>
                  <span class="rank">#{idx}</span>
                  <span class="habit-name">{html.escape(h['interaction'])}</span>
                  <span class="regime-pill">{html.escape(h['regime'])}</span>
                  {rec_badge}
                </div>
                <div class="reg-score">Total Regret <strong>{h['total_regret']:.2f}</strong></div>
              </div>
              <div class="bar-track"><div class="bar-fill" style="width:{bar_w}%"></div></div>
              <div class="habit-stats">
                <span><strong>{h['occurrences']}</strong> leaks across <strong>{h['sets_affected']}/{h['total_sets']}</strong> sets ({h['set_spread_pct']}%)</span>
                <span>Avg Regret: <strong>{h['avg_regret']:+.2f}</strong></span>
                <span>You: <strong>{h['avg_actual_value']:+.2f}</strong> vs Top-Rank EV: <strong>{h['avg_corpus_ev']:+.2f}</strong></span>
                <span>Avg HP Swing: <strong class="neg">{h['avg_hp_swing']*100:+.0f}%</strong></span>
              </div>
              <div class="habit-actions">
                <div><strong>Your common choice:</strong> {user_acts}</div>
                <div><strong>Top-rank answer:</strong> {pro_alts}</div>
                <div><strong>Opponents:</strong> {opps_str}</div>
              </div>
              <details id="habit-details-{idx}"{open_attr}>
                <summary>View {len(ex_cards) + len(ex_rows)} cited example(s){clip_count_lbl} &amp; Takeover commands</summary>
                {''.join(ex_cards)}
                {ex_table_html}
              </details>
            </article>
            """
        )

    # Matchup table
    mu_rows = []
    for m in rollup.get("matchups", []):
        mu_rows.append(
            f"""
            <tr>
              <td><strong>{html.escape(m['opp_char_name'])}</strong></td>
              <td>{m['sets']}</td>
              <td>{m['rounds']}</td>
              <td>{m['leak_count']}</td>
              <td class="neg">{m['total_regret']:.2f}</td>
              <td>{html.escape(m['top_leak'])}</td>
            </tr>
            """
        )

    # Per-set table
    set_rows = []
    for s in rollup.get("sets", []):
        score_str = f"{s['self_score']}-{s['opp_score']}" if s.get("self_score") is not None and s.get("opp_score") is not None else "casual"
        opp_cell = (
            f'<a class="rep-link" href="{html.escape(s["report_href"])}">{html.escape(s["opp_player_name"])} ({html.escape(s["opp_char_name"])})</a>'
            if s.get("report_href")
            else f"{html.escape(s['opp_player_name'])} ({html.escape(s['opp_char_name'])})"
        )
        set_rows.append(
            f"""
            <tr>
              <td class="mono">{html.escape(s['match_date'])}</td>
              <td>{opp_cell}</td>
              <td>{html.escape(s['self_char_name'])}</td>
              <td class="mono">{html.escape(score_str)}</td>
              <td>{s['rounds']}</td>
              <td>{s['leak_count']}</td>
              <td class="neg">{s['total_regret']:.2f}</td>
              <td>{html.escape(s['top_leak_label'])} ({s['top_leak_regret']:+.2f})</td>
            </tr>
            """
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{uname} ({pchar}) — Multi-Set Habit &amp; Leak Rollup ({num_sets} sets)</title>
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
  .ai-box {{ background: linear-gradient(135deg, rgba(56,189,248,0.10), rgba(245,158,11,0.08)); border: 1px solid rgba(56,189,248,0.35); border-radius: 10px; padding: 16px; margin-bottom: 22px; }}
  .ai-head {{ font-weight: 700; color: #7dd3fc; font-size: 0.92rem; display: flex; justify-content: space-between; align-items: center; }}
  .model-tag {{ font-size: 0.72rem; background: rgba(15,23,42,0.7); color: var(--muted); padding: 2px 8px; border-radius: 999px; }}
  .ai-headline {{ font-size: 1.02rem; font-weight: 600; margin: 10px 0 14px 0; color: #fff; }}
  .prio-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; }}
  .prio-card {{ background: rgba(15,23,42,0.75); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-size: 0.85rem; }}
  .prio-title {{ font-weight: 700; color: #fbbf24; margin-bottom: 4px; }}
  .prio-map {{ margin-bottom: 7px; font-size: 0.78rem; }}
  .jump-link {{ color: #7dd3fc; text-decoration: none; font-weight: 600; }}
  .jump-link:hover {{ text-decoration: underline; }}
  .prio-diag {{ color: var(--text); margin-bottom: 8px; line-height: 1.4; }}
  .prio-drill {{ color: #86efac; line-height: 1.4; }}
  .ai-mu {{ margin: 12px 0 0 0; font-size: 0.86rem; color: #cbd5e1; }}
  .habit-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 15px 16px; margin-bottom: 12px; }}
  .habit-top {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
  .rank {{ color: var(--accent); font-weight: 700; margin-right: 6px; }}
  .habit-name {{ font-size: 1.03rem; font-weight: 700; color: #fff; margin-right: 6px; }}
  .regime-pill {{ font-size: 0.72rem; background: #1e293b; color: #cbd5e1; padding: 2px 7px; border-radius: 5px; margin-right: 6px; }}
  .badge {{ font-size: 0.72rem; padding: 2px 8px; border-radius: 999px; font-weight: 600; }}
  .badge-hot {{ background: rgba(245, 158, 11, 0.22); color: #fbbf24; }}
  .badge-one {{ background: rgba(148, 163, 184, 0.16); color: var(--muted); }}
  .reg-score {{ font-size: 0.88rem; color: var(--bad); }}
  .bar-track {{ height: 6px; background: #0f172a; border-radius: 4px; margin: 10px 0; overflow: hidden; }}
  .bar-fill {{ height: 100%; background: linear-gradient(90deg, #f59e0b, #ef4444); border-radius: 4px; }}
  .habit-stats {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 0.84rem; color: #cbd5e1; margin-bottom: 8px; }}
  .habit-actions {{ font-size: 0.84rem; color: #cbd5e1; display: grid; gap: 4px; margin-bottom: 10px; }}
  code {{ background: #0f172a; padding: 1px 5px; border-radius: 4px; font-size: 0.82rem; color: #7dd3fc; }}
  details {{ border-top: 1px solid var(--border); padding-top: 8px; }}
  summary {{ cursor: pointer; font-size: 0.83rem; color: var(--accent); font-weight: 600; }}
  .table-wrap {{ overflow-x: auto; margin-top: 8px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; font-size: 0.84rem; }}
  .mini-tbl {{ background: var(--surface); }}
  th, td {{ padding: 9px 11px; border-bottom: 1px solid var(--border); text-align: left; }}
  th {{ background: var(--surface); color: var(--muted); font-weight: 600; }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; white-space: nowrap; }}
  .muted {{ color: var(--muted); font-size: 0.78rem; }}
  .neg {{ color: var(--bad); font-weight: 600; }}
  .rep-link {{ color: #7dd3fc; text-decoration: none; font-weight: 600; }}
  .rep-link:hover {{ text-decoration: underline; }}
  .copy-btn {{ background: #1e293b; color: #e2e8f0; border: 1px solid #334155; border-radius: 5px; padding: 4px 8px; cursor: pointer; font-size: 0.76rem; white-space: nowrap; }}
  .copy-btn:hover {{ border-color: var(--accent); }}
  .ex-clip-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 11px 12px; margin-top: 10px; }}
  .ex-clip-head {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; font-size: 0.84rem; }}
  .ex-clip-actions {{ display: flex; align-items: center; gap: 10px; }}
  .vid-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 10px; }}
  .vid-pane {{ background: #0f172a; border: 1px solid #1e293b; border-radius: 6px; padding: 8px; }}
  .vid-cap {{ display: flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 0.78rem; margin-bottom: 6px; }}
  .vid-foot {{ display: flex; justify-content: space-between; align-items: center; gap: 6px; margin-top: 6px; }}
  video {{ width: 100%; border-radius: 5px; background: #000; display: block; }}
  .pill-you {{ background: rgba(248, 113, 113, 0.2); color: #fca5a5; padding: 1px 6px; border-radius: 4px; font-weight: 700; font-size: 0.7rem; }}
  .pill-pro {{ background: rgba(74, 222, 128, 0.2); color: #86efac; padding: 1px 6px; border-radius: 4px; font-weight: 700; font-size: 0.7rem; }}
  .play-btn {{ margin-left: auto; background: #1e293b; color: #fff; border: 1px solid #334155; border-radius: 4px; padding: 2px 8px; cursor: pointer; font-size: 0.72rem; }}
  .play-btn:hover {{ border-color: var(--accent); }}
{TAKEOVER_UI_CSS}
</style>
</head>
<body>
  <a class="back-link" href="/#insights">&larr; Fightcade Lab Insights</a>
  <header>
    <h1>{uname} ({pchar}) — Multi-Set Habit &amp; Leak Rollup</h1>
    <div class="sub">Aggregated across {num_sets} personal sets ({tot_rounds} rounds) against {corpus_sz:,} top-rank reference decision points</div>
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-val">{num_sets}</div><div class="kpi-lbl">Sets Analyzed</div></div>
      <div class="kpi"><div class="kpi-val">{tot_rounds}</div><div class="kpi-lbl">Total Rounds</div></div>
      <div class="kpi"><div class="kpi-val">{tot_dps:,}</div><div class="kpi-lbl">Decision Points</div></div>
      <div class="kpi"><div class="kpi-val">{tot_leaks}</div><div class="kpi-lbl">Positive-Regret Leaks</div></div>
      <div class="kpi"><div class="kpi-val" style="color:#f87171">{tot_reg:.2f}</div><div class="kpi-lbl">Total Regret Cost</div></div>
    </div>
  </header>

  {ai_html}

  <h2>Top Recurring Habits &amp; Leaks (Ranked by Cross-Set Regret Cost)</h2>
  {''.join(habit_cards) if habit_cards else '<p class="sub">No significant recurring leaks found above threshold.</p>'}

  <h2>Matchup Breakdown</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Opponent Char</th><th>Sets</th><th>Rounds</th><th>Leaks</th><th>Total Regret</th><th>Biggest Recurring Leak</th></tr></thead>
      <tbody>{''.join(mu_rows)}</tbody>
    </table>
  </div>

  <h2>Included Personal Sets</h2>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Date</th><th>Opponent</th><th>Your Char</th><th>Score</th><th>Rounds</th><th>Leaks</th><th>Total Regret</th><th>Top Leak</th></tr></thead>
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a Multi-Set Personal Habit & Leak Rollup report.")
    parser.add_argument("--username", required=True, help="Fightcade player handle (e.g., player1)")
    parser.add_argument("--personal-dir", type=Path, default=Path("data/personal"))
    parser.add_argument("--parquet-dir", type=Path, default=Path("data/parquet"))
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=Path("data/reports"))
    parser.add_argument("--top-n-per-set", type=int, default=25, help="Max leaks extracted per set for rollup (default: 25)")
    parser.add_argument("--min-regret", type=float, default=0.06, help="Minimum regret to count as a leak (default: 0.06)")
    parser.add_argument("--render-clips", action="store_true", help="Render 6s MP4 clips + .fs savestates for cited habit examples")
    parser.add_argument("--max-clip-habits", type=int, default=6, help="Number of top habits to attach video clips to (default: 6)")
    parser.add_argument("--clips-per-habit", type=int, default=3, help="Cited user clips per habit (default: 3)")
    parser.add_argument("--pro-clips-per-habit", type=int, default=0, help="Optional pro reference clips per habit (default: 0 = strictly your footage)")
    parser.add_argument("--clip-workers", type=int, default=3, help="Parallel FBNeo clip workers (default: 3)")
    parser.add_argument("--summarize", action="store_true", help="Synthesize Top-3 Training Priorities via Gemini")
    args = parser.parse_args(argv)

    t0 = time.perf_counter()
    sets = discover_player_sets(args.username, personal_dir=args.personal_dir, data_dir=args.data_dir)
    if not sets:
        print(f"No personal Parquet sets found for '{args.username}' in {args.personal_dir}")
        return 1

    pro_files = sorted(args.parquet_dir.glob("*.parquet"))
    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
    corpus, hit = load_or_build_corpus(
        pro_files,
        config=cfg,
        both_perspectives=True,
        cache_dir=args.data_dir / "index",
    )
    index = HierarchicalContextIndex(corpus.meta, corpus.vectors)
    print(f"Loaded pro corpus ({len(corpus.meta):,} decision points, cache={'HIT' if hit else 'MISS'}) in {time.perf_counter()-t0:.2f}s")

    set_diagnoses: list[dict[str, Any]] = []
    for s in sets:
        pq: Path = s["parquet_path"]
        df_r = pd.read_parquet(pq, columns=["round_id"])
        rounds = int(df_r["round_id"].nunique()) if len(df_r) else 0
        rep = diagnose_replay(
            pq,
            index,
            perspective=s["perspective"],
            config=cfg,
            top_n=args.top_n_per_set,
        )
        rep["self_player_name"] = s["self_player_name"]
        rep["opp_player_name"] = s["opp_player_name"]
        rep["match_date"] = s["match_date"]
        set_diagnoses.append({**s, "rounds": rounds, "report": rep})
        print(
            f"  [{s['match_date']}] {s['quark_id']} vs {s['opp_player_name']} ({rep['opp_char_name']}) "
            f"| {rounds} rnds | {len(rep['moments'])} leaks"
        )

    rollup = aggregate_habit_rollup(
        args.username,
        set_diagnoses,
        min_regret=args.min_regret,
        reports_dir=args.out_dir,
        data_dir=args.data_dir,
    )

    if args.render_clips:
        tc0 = time.perf_counter()
        cstats = attach_rollup_clips(
            rollup,
            data_dir=args.data_dir,
            max_clip_habits=args.max_clip_habits,
            clips_per_habit=args.clips_per_habit,
            pro_clips_per_habit=args.pro_clips_per_habit,
            clip_workers=args.clip_workers,
        )
        print(
            f"Rendered rollup clips in {time.perf_counter()-tc0:.1f}s "
            f"(requested={cstats['requested']}, cached={cstats['cached']}, "
            f"rendered={cstats['rendered']}, failed={cstats['failed']}, passes={cstats['fbneo_passes']})"
        )

    if args.summarize:
        rollup["ai_synthesis"] = synthesize_rollup_with_gemini(
            rollup,
            cache_path=args.out_dir / "gemini_rollup_cache.json",
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = build_habit_rollup_stem(rollup)
    json_path = args.out_dir / f"{stem}.json"
    html_path = args.out_dir / f"{stem}.html"
    json_path.write_text(json.dumps(rollup, indent=2), encoding="utf-8")
    html_path.write_text(render_habit_rollup_html(rollup), encoding="utf-8")
    hub_path = build_reports_index_html(args.out_dir)

    print(f"\nWrote habit rollup JSON: {json_path}")
    print(f"Wrote habit rollup HTML: {html_path}")
    print(f"Updated Reports Hub    : {hub_path}")
    print(f"Total elapsed          : {time.perf_counter()-t0:.2f}s")
    print("\n=== Top 5 Recurring Habits ===")
    for i, h in enumerate(rollup["habits"][:5], 1):
        act = h["top_user_actions"][0]["action"] if h["top_user_actions"] else "?"
        alt = h["top_pro_alternatives"][0]["action"] if h["top_pro_alternatives"] else "?"
        print(
            f"  {i}. {h['interaction']} ({h['regime']}): {h['occurrences']} leaks across "
            f"{h['sets_affected']}/{h['total_sets']} sets | total_regret={h['total_regret']:.2f} "
            f"| you={act} -> pro={alt}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
