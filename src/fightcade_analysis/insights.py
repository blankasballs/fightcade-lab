"""Insights catalog, background Hybrid VLM+RAM analysis runner, and in-studio Pro Sequence Search.

Powers:
  - `GET /api/insights` (replaces static Reports Hub table inside Fightcade Lab's Insights tab)
  - `POST /api/insights/run` (netplay-safe background runner for Match Breakdown & Verified Habits)
  - `POST /api/pro-search` & `POST /api/pro-search/render-clip` (in-studio `<1s` pro trajectory search)
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable
import uuid

from fightcade_analysis.clip_renderer import (
    DEFAULT_DATA_DIR,
    DEFAULT_POST_FRAMES,
    DEFAULT_PRE_FRAMES,
    DEFAULT_SAVESTATE_LEAD,
    ClipRequest,
    find_parquet_for_quark,
    render_clips_batch,
)
from fightcade_analysis.gemini_summary import DEFAULT_MODEL_CHAIN, load_gemini_api_key
from fightcade_analysis.naming import (
    build_coaching_report_stem,
    build_reports_index_html,
    enrich_coaching_metadata,
    extract_match_date,
    resolve_replay_players_and_ranks,
)


def _clip_info(
    quark_id: str,
    game_frame: int,
    data_root: Path,
    *,
    clip_rel_path: str | None = None,
) -> dict[str, Any]:
    """Check whether 6s MP4 clip, `.fs` savestate, and Studio full replay exist locally."""
    qid = str(quark_id or "").strip()
    gf = int(game_frame or 0)
    clip_url: str | None = None
    has_clip = False

    if clip_rel_path:
        fname = Path(str(clip_rel_path)).name
        p = data_root / "clips" / fname
        if p.is_file() and p.stat().st_size > 1024:
            has_clip = True
            clip_url = f"/clips/{fname}"
    if not has_clip and qid and gf > 0:
        fname = f"{qid}_gf{gf}.mp4"
        p = data_root / "clips" / fname
        if p.is_file() and p.stat().st_size > 1024:
            has_clip = True
            clip_url = f"/clips/{fname}"

    sav_p = (data_root / "savestates" / f"{qid}_gf{gf}.fs") if (qid and gf > 0) else None
    has_sav = bool(sav_p and sav_p.is_file() and sav_p.stat().st_size > 4096)
    has_studio = bool(qid and (data_root / "replays" / qid / "manifest.json").is_file())
    return {
        "has_clip": has_clip,
        "clip_url": clip_url,
        "has_savestate": has_sav,
        "has_studio_replay": has_studio,
    }


def _habit_preview_from_doc(doc: dict[str, Any], report_href: str, report_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h in (doc.get("habits") or [])[:6]:
        if not isinstance(h, dict):
            continue
        raw_eps = h.get("examples") or h.get("episodes") or []
        seed_moments: list[dict[str, Any]] = []
        for ep in raw_eps[:5]:
            if not isinstance(ep, dict) or not ep.get("quark_id"):
                continue
            t_cause = int(ep.get("t_cause_gf") or ep.get("game_frame") or 0)
            if t_cause <= 0:
                continue
            vt = ep.get("visual_tag") or {}
            summary_txt = str(
                vt.get("what_happened")
                or vt.get("one_line_summary")
                or h.get("label")
                or "Verified habit clip"
            ).strip()
            tip_txt = str(vt.get("self_mistake") or vt.get("coaching_tip") or summary_txt).strip()
            seed_moments.append(
                {
                    "source": "moment",
                    "quark_id": str(ep["quark_id"]),
                    "round_id": int(ep.get("round_id") or 0),
                    "perspective": int(ep.get("perspective") or 1),
                    "start_gf": max(1, t_cause - 180),
                    "end_gf": t_cause + 90,
                    "anchor_gf": t_cause,
                    "label": summary_txt[:140],
                    "note": tip_txt[:400],
                    "tags": [str(h.get("label") or "").strip()] if h.get("label") else [],
                }
            )
        for ps in (h.get("verified_pro_solutions") or [])[:2]:
            if not isinstance(ps, dict) or not ps.get("quark_id"):
                continue
            gf = int(ps.get("game_frame") or ps.get("mid_gf") or 0)
            if gf <= 0:
                continue
            ver = ps.get("verification") or ps.get("pass2_verdict") or {}
            seed_moments.append(
                {
                    "source": "pro_clip",
                    "quark_id": str(ps["quark_id"]),
                    "round_id": int(ps.get("round_id") or 0),
                    "perspective": int(ps.get("perspective") or 1),
                    "start_gf": max(1, gf - 120),
                    "end_gf": gf + 150,
                    "anchor_gf": gf,
                    "label": f"Top-Rank Answer: {ps.get('self_char_name', '')} vs {ps.get('opp_char_name', '')}".strip(),
                    "note": str(ver.get("pro_solution") or ver.get("pro_action_summary") or ps.get("action_coarse") or "")[:400],
                    "tags": [str(h.get("label") or "").strip()] if h.get("label") else [],
                }
            )

        top_summary = str(h.get("coaching_summary") or h.get("top_summary") or "").strip()
        if not top_summary and raw_eps:
            first_vt = (raw_eps[0].get("visual_tag") or {}) if isinstance(raw_eps[0], dict) else {}
            top_summary = str(first_vt.get("self_mistake") or first_vt.get("what_happened") or "").strip()

        out.append(
            {
                "category": str(h.get("category") or "other"),
                "label": str(h.get("label") or h.get("category") or "Habit"),
                "total_hp_lost": int(h.get("total_hp_lost") or 0),
                "avg_hp_lost_pct": float(h.get("avg_hp_lost_pct") or 0.0),
                "verified_clips": int(h.get("verified_clips") or len(raw_eps)),
                "sets_affected": int(h.get("sets_affected") or 0),
                "total_sets": int(h.get("total_sets") or doc.get("num_sets") or 0),
                "verified_pro_count": len(h.get("verified_pro_solutions") or []),
                "summary": top_summary,
                "report_id": report_id,
                "report_href": report_href,
                "source_insight_id": f"{report_id}:{h.get('category', 'habit')}",
                "seed_moments": seed_moments,
            }
        )
    return out


def list_insights(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    include_legacy: bool = False,
) -> dict[str, Any]:
    """Parse `data/reports/*.json` paired with `.html` into structured insight cards + archive."""
    data_root = data_dir.resolve()
    reports_dir = data_root / "reports"
    entries: list[dict[str, Any]] = []
    latest_verified: dict[str, Any] | None = None

    if reports_dir.is_dir():
        for jp in sorted(reports_dir.glob("*.json")):
            if jp.name.startswith("gemini_") and jp.name.endswith("_cache.json"):
                continue
            hp = jp.with_suffix(".html")
            try:
                doc = json.loads(jp.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(doc, dict):
                continue

            is_hybrid = doc.get("report_type") == "hybrid_habit_rollup"
            is_rollup = (not is_hybrid) and (
                (doc.get("report_type") == "habit_rollup") or ("habits" in doc and "sets" in doc)
            )
            is_seq = (not is_hybrid) and (not is_rollup) and (
                (doc.get("report_type") == "sequence_search") or ("query" in doc and "matches" in doc)
            )
            href = f"/#report/{jp.stem}"
            mtime = hp.stat().st_mtime if hp.exists() else jp.stat().st_mtime

            if is_hybrid:
                date_s = str(doc.get("match_date") or datetime.now().strftime("%Y-%m-%d"))
                uname = str(doc.get("username") or doc.get("self_player_name") or "Player")
                pchar = str(doc.get("primary_char_name") or "")
                n_sets = int(doc.get("num_sets", 0))
                n_rnds = int(doc.get("total_rounds", 0))
                v_clips = int(doc.get("visually_verified_clips", 0))
                habits_prev = _habit_preview_from_doc(doc, href, jp.stem)
                top_h = (
                    f"#1 {habits_prev[0]['label']} (-{habits_prev[0]['total_hp_lost']} HP)"
                    if habits_prev
                    else "No verified habits"
                )
                entry = {
                    "id": jp.stem,
                    "type": "hybrid_habit_rollup",
                    "type_label": "Verified Habits",
                    "date": date_s,
                    "filename": hp.name,
                    "href": href,
                    "matchup": f"{uname} ({pchar}) — {n_sets}-Set Verified Habits" if pchar else f"{uname} — {n_sets}-Set Verified Habits",
                    "summary": f"{n_sets} sets · {n_rnds} rounds · {v_clips} visually verified clips",
                    "metric": top_h,
                    "quark_id": f"verified-{uname}-{n_sets}",
                    "num_sets": n_sets,
                    "total_rounds": n_rnds,
                    "visually_verified_clips": v_clips,
                    "habits": habits_prev,
                    "ai_synthesis": doc.get("ai_synthesis"),
                    "mtime": mtime,
                }
                entries.append(entry)
            elif is_rollup:
                date_s = str(doc.get("match_date") or datetime.now().strftime("%Y-%m-%d"))
                uname = str(doc.get("username") or doc.get("self_player_name") or "Player")
                pchar = str(doc.get("primary_char_name") or "")
                n_sets = int(doc.get("num_sets", 0))
                n_rnds = int(doc.get("total_rounds", 0))
                habits = doc.get("habits") or []
                top_h = (
                    f"Top leak: {habits[0].get('interaction', '')} ({float(habits[0].get('total_regret', 0.0)):.2f})"
                    if habits
                    else "No recurring leaks"
                )
                entries.append(
                    {
                        "id": jp.stem,
                        "type": "habit_rollup",
                        "type_label": "Legacy v1 Rollup",
                        "date": date_s,
                        "filename": hp.name,
                        "href": href,
                        "matchup": f"{uname} ({pchar}) — {n_sets}-Set Habit Rollup" if pchar else f"{uname} — {n_sets}-Set Habit Rollup",
                        "summary": f"{n_sets} sets · {n_rnds} rounds · {doc.get('total_leaks', 0)} leaks",
                        "metric": top_h,
                        "quark_id": f"rollup-{uname}-{n_sets}",
                        "mtime": mtime,
                    }
                )
            elif is_seq:
                q = doc.get("query") or {}
                qid = str(q.get("quark_id") or "")
                date_s = str(doc.get("match_date") or extract_match_date(qid))
                s_user = str(doc.get("self_player_name") or q.get("self_player_name") or f"P{q.get('perspective', 1)}")
                o_user = str(doc.get("opp_player_name") or q.get("opp_player_name") or ("P2" if q.get("perspective", 1) == 1 else "P1"))
                s_char = str(q.get("self_char_name") or "")
                o_char = str(q.get("opp_char_name") or "")
                note = str(doc.get("note") or "")
                summary = f"R{q.get('round_id', '?')} · gf{q.get('start_gf', '?')}..{q.get('end_gf', '?')} ({q.get('duration_s', '?')}s)"
                if note:
                    summary = f"{note} — {summary}"
                metric = f"{len(doc.get('matches') or [])} matches ({doc.get('retrieval_tier', '')})"
                entries.append(
                    {
                        "id": jp.stem,
                        "type": "sequence_search",
                        "type_label": "Sequence Search",
                        "date": date_s,
                        "filename": hp.name,
                        "href": href,
                        "matchup": f"{s_user} ({s_char}) vs {o_user} ({o_char})",
                        "summary": summary,
                        "metric": metric,
                        "quark_id": qid,
                        "mtime": mtime,
                    }
                )
            elif "moments" in doc:
                qid = str(doc.get("target_quark_id") or "")
                persp = int(doc.get("perspective", 1))
                date_s = str(doc.get("match_date") or extract_match_date(qid))
                s_user = str(doc.get("self_player_name") or f"P{persp}")
                o_user = str(doc.get("opp_player_name") or ("P2" if persp == 1 else "P1"))
                s_char = str(doc.get("self_char_name") or "")
                o_char = str(doc.get("opp_char_name") or "")
                moments = doc.get("moments") or []
                is_v2 = doc.get("pipeline") == "hybrid_v2"
                if is_v2:
                    top_reg = (
                        f"Top -{moments[0].get('hp_lost', 0)} HP ({moments[0].get('category_label', '')})"
                        if moments
                        else "No damage episodes"
                    )
                    summary_txt = f"{len(moments)} visually verified moments · {doc.get('total_damage_episodes', 0)} Hit-#1 episodes"
                else:
                    top_reg = f"Top regret {float(moments[0].get('regret', 0.0)):+.2f}" if moments else "No positive regret"
                    summary_txt = f"{len(moments)} high-regret cards · {doc.get('total_decision_points', 0)} decision points"
                moments_prev = [
                    {
                        "category": m.get("category") or "",
                        "category_label": m.get("category_label") or m.get("label") or "",
                        "hp_lost": int(m.get("hp_lost") or 0),
                        "round_id": int(m.get("round_id") or 0),
                        "game_frame": int(m.get("game_frame") or 0),
                    }
                    for m in moments[:5]
                    if isinstance(m, dict)
                ]
                entries.append(
                    {
                        "id": jp.stem,
                        "type": "full_set",
                        "type_label": "Match Breakdown",
                        "pipeline": doc.get("pipeline") or "legacy",
                        "date": date_s,
                        "filename": hp.name,
                        "href": href,
                        "matchup": f"{s_user} ({s_char}) vs {o_user} ({o_char})",
                        "summary": summary_txt,
                        "metric": top_reg,
                        "quark_id": qid,
                        "moments": moments_prev,
                        "mtime": mtime,
                    }
                )

    type_prio = {"hybrid_habit_rollup": 3, "full_set": 2, "sequence_search": 1, "habit_rollup": 0}
    entries.sort(key=lambda e: (e["date"], type_prio.get(e["type"], 0), e["mtime"]), reverse=True)

    verified_list = [e for e in entries if e["type"] == "hybrid_habit_rollup"]
    if verified_list:
        latest_verified = verified_list[0]

    counts = {
        "total": len(entries),
        "verified_habits": len(verified_list),
        "full_set": sum(1 for e in entries if e["type"] == "full_set"),
        "sequence_search": sum(1 for e in entries if e["type"] == "sequence_search"),
        "legacy_rollups": sum(1 for e in entries if e["type"] == "habit_rollup"),
    }

    visible = entries if include_legacy else [e for e in entries if e["type"] != "habit_rollup"]
    return {
        "ok": True,
        "latest_verified_habits": latest_verified,
        "reports": visible,
        "counts": counts,
    }


def _enrich_pro_entry(entry: dict[str, Any], data_root: Path) -> dict[str, Any]:
    out = dict(entry)
    qid = str(out.get("quark_id") or "")
    persp = int(out.get("perspective") or 1)
    gf = int(out.get("game_frame") or out.get("mid_gf") or out.get("start_gf") or 0)
    if not out.get("self_player_name") or not out.get("opp_player_name"):
        pinfo = resolve_replay_players_and_ranks(qid, persp, data_dir=data_root)
        out.setdefault("self_player_name", pinfo.get("self_player_name") or f"P{persp}")
        out.setdefault("opp_player_name", pinfo.get("opp_player_name") or ("P2" if persp == 1 else "P1"))
        out.setdefault("self_rank_letter", pinfo.get("self_rank_letter") or "")
        out.setdefault("opp_rank_letter", pinfo.get("opp_rank_letter") or "")
    out.update(_clip_info(qid, gf, data_root, clip_rel_path=out.get("clip_rel_path")))
    out["game_frame"] = gf
    return out


def get_insight_report(report_id: str, *, data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Load & enrich a single persisted report JSON for the native Fightcade Lab Report Viewer."""
    rid = str(report_id or "").strip()
    if rid.endswith(".html"):
        rid = rid[:-5]
    elif rid.endswith(".json"):
        rid = rid[:-5]
    if not rid or "/" in rid or "\\" in rid or ".." in rid or (rid.startswith("gemini_") and rid.endswith("_cache")):
        raise ValueError(f"Invalid report id: {report_id!r}")

    data_root = data_dir.resolve()
    reports_dir = (data_root / "reports").resolve()
    jp = (reports_dir / f"{rid}.json").resolve()
    try:
        jp.relative_to(reports_dir)
    except ValueError as exc:
        raise ValueError("Report path traversal blocked") from exc
    if not jp.is_file():
        raise FileNotFoundError(f"Report '{rid}' not found")

    doc = json.loads(jp.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"Report '{rid}' is malformed")

    is_hybrid = doc.get("report_type") == "hybrid_habit_rollup"
    is_rollup = (not is_hybrid) and (
        (doc.get("report_type") == "habit_rollup") or ("habits" in doc and "sets" in doc)
    )
    is_seq = (not is_hybrid) and (not is_rollup) and (
        (doc.get("report_type") == "sequence_search") or ("query" in doc and "matches" in doc)
    )

    out = dict(doc)
    out["id"] = rid
    out["href"] = f"/#report/{rid}"

    if is_hybrid:
        out["report_type"] = "hybrid_habit_rollup"
        out["type_label"] = "Verified Habits"
        out["title"] = f"{doc.get('username') or doc.get('self_player_name') or 'Player'} ({doc.get('primary_char_name') or ''}) — {doc.get('num_sets', 0)}-Set Verified Habits"
        previews = _habit_preview_from_doc(doc, f"/#report/{rid}", rid)
        prev_by_cat = {p["category"]: p for p in previews}
        enriched_habits: list[dict[str, Any]] = []
        for h in doc.get("habits") or []:
            hc = dict(h)
            raw_eps = hc.get("examples") or hc.get("episodes") or []
            enr_eps = []
            for ep in raw_eps:
                epc = dict(ep)
                gf = int(epc.get("t_cause_gf") or epc.get("game_frame") or 0)
                epc["game_frame"] = gf
                epc.update(_clip_info(str(epc.get("quark_id") or ""), gf, data_root, clip_rel_path=epc.get("clip_rel_path")))
                enr_eps.append(epc)
            hc["examples"] = enr_eps
            hc["verified_pro_solutions"] = [_enrich_pro_entry(ps, data_root) for ps in (hc.get("verified_pro_solutions") or [])]
            p_match = prev_by_cat.get(str(hc.get("category") or ""))
            hc["seed_moments"] = p_match["seed_moments"] if p_match else []
            hc["source_insight_id"] = f"{rid}:{hc.get('category', 'habit')}"
            enriched_habits.append(hc)
        out["habits"] = enriched_habits
        enr_sets = []
        for s in doc.get("sets") or []:
            sc = dict(s)
            sq = str(sc.get("quark_id") or "")
            sc["has_studio_replay"] = bool(sq and (data_root / "replays" / sq / "manifest.json").is_file())
            enr_sets.append(sc)
        out["sets"] = enr_sets
    elif is_rollup:
        out["report_type"] = "habit_rollup"
        out["type_label"] = "Legacy v1 Habit Rollup"
        out["title"] = f"{doc.get('username') or doc.get('self_player_name') or 'Player'} ({doc.get('primary_char_name') or ''}) — {doc.get('num_sets', 0)}-Set Habit Rollup"
        enriched_habits = []
        for h in doc.get("habits") or []:
            hc = dict(h)
            enr_eps = []
            for ep in (hc.get("examples") or [])[:6]:
                epc = dict(ep)
                gf = int(epc.get("game_frame") or 0)
                epc.update(_clip_info(str(epc.get("quark_id") or ""), gf, data_root, clip_rel_path=epc.get("clip_rel_path")))
                if isinstance(epc.get("best_proof"), dict):
                    epc["best_proof"] = _enrich_pro_entry(epc["best_proof"], data_root)
                enr_eps.append(epc)
            hc["examples"] = enr_eps
            enriched_habits.append(hc)
        out["habits"] = enriched_habits
    elif is_seq:
        out["report_type"] = "sequence_search"
        out["type_label"] = "Sequence Search"
        q = dict(doc.get("query") or {})
        q_qid = str(q.get("quark_id") or "")
        q_gf = int(q.get("mid_gf") or q.get("start_gf") or 0)
        q.update(_clip_info(q_qid, q_gf, data_root, clip_rel_path=q.get("clip_rel_path")))
        out["query"] = q
        s_user = str(doc.get("self_player_name") or q.get("self_player_name") or f"P{q.get('perspective', 1)}")
        o_user = str(doc.get("opp_player_name") or q.get("opp_player_name") or ("P2" if q.get("perspective", 1) == 1 else "P1"))
        out["title"] = f"{s_user} ({q.get('self_char_name', '')}) vs {o_user} ({q.get('opp_char_name', '')})"
        out["matches"] = [_enrich_pro_entry(m, data_root) for m in (doc.get("matches") or [])]
    else:
        out["report_type"] = "full_set"
        out["type_label"] = "Match Breakdown"
        qid = str(doc.get("target_quark_id") or "")
        persp = int(doc.get("perspective") or 1)
        s_user = str(doc.get("self_player_name") or f"P{persp}")
        o_user = str(doc.get("opp_player_name") or ("P2" if persp == 1 else "P1"))
        out["title"] = f"{s_user} ({doc.get('self_char_name', '')}) vs {o_user} ({doc.get('opp_char_name', '')})"
        out["has_studio_replay"] = bool(qid and (data_root / "replays" / qid / "manifest.json").is_file())
        enr_moments = []
        for m in doc.get("moments") or []:
            mc = dict(m)
            m_qid = str(mc.get("quark_id") or qid)
            m_gf = int(mc.get("game_frame") or 0)
            mc.update(_clip_info(m_qid, m_gf, data_root, clip_rel_path=mc.get("clip_rel_path")))
            mc["verified_pro_solutions"] = [_enrich_pro_entry(ps, data_root) for ps in (mc.get("verified_pro_solutions") or [])]
            enr_opts = []
            for opt in mc.get("options") or []:
                oc = dict(opt)
                oc["proofs"] = [_enrich_pro_entry(pr, data_root) for pr in (oc.get("proofs") or [])]
                enr_opts.append(oc)
            mc["options"] = enr_opts
            enr_moments.append(mc)
        out["moments"] = enr_moments

    return {"ok": True, "report": out}


class ProSearchService:
    """In-memory cached pro corpus index + `<1s` sequence search + on-demand pro clip rendering."""

    def __init__(
        self,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        is_live_match_fn: Callable[[], bool] | None = None,
        corpus_index: Any | None = None,
        search_fn: Callable[..., dict[str, Any]] | None = None,
        render_clips_fn: Callable[..., tuple[dict[str, Any], dict[str, int]]] | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self._is_live_match_fn = is_live_match_fn or (lambda: False)
        self._lock = threading.Lock()
        self._cached_index: Any | None = corpus_index
        self._cached_sig: tuple[int, int] | None = None
        self._search_fn = search_fn
        self._render_clips_fn = render_clips_fn or render_clips_batch

    def _get_or_load_index(self) -> Any:
        with self._lock:
            if self._cached_index is not None and self._cached_sig is None:
                # Injected index (tests)
                return self._cached_index
            from fightcade_analysis.index import HierarchicalContextIndex, load_or_build_corpus
            from fightcade_analysis.segmentation import SegmentationConfig

            pq_dir = self.data_dir / "parquet"
            files = sorted(pq_dir.glob("*.parquet")) if pq_dir.is_dir() else []
            if not files:
                raise FileNotFoundError(f"No top-rank corpus Parquet files found in {pq_dir}")
            sig = (len(files), max(int(p.stat().st_mtime_ns) for p in files))
            if self._cached_index is not None and self._cached_sig == sig:
                return self._cached_index
            cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
            corpus, _ = load_or_build_corpus(
                files,
                config=cfg,
                both_perspectives=True,
                cache_dir=self.data_dir / "index",
            )
            self._cached_index = HierarchicalContextIndex(corpus.meta, corpus.vectors)
            self._cached_sig = sig
            return self._cached_index

    def search(
        self,
        *,
        quark_id: str,
        perspective: int,
        start_gf: int,
        end_gf: int,
        round_id: int | None = None,
        top_k: int = 6,
        tier_override: str | None = None,
    ) -> dict[str, Any]:
        qid = str(quark_id or "").strip()
        if not qid:
            raise ValueError("quark_id is required")
        persp = int(perspective)
        if persp not in (1, 2):
            raise ValueError("perspective must be 1 or 2")
        s_gf = int(start_gf)
        e_gf = int(end_gf)
        if s_gf < 1 or e_gf <= s_gf:
            raise ValueError("Provide valid start_gf < end_gf")
        k = max(1, min(12, int(top_k)))
        if tier_override and tier_override not in ("exact_matchup", "same_self_char", "universal"):
            raise ValueError("tier_override must be exact_matchup, same_self_char, or universal")

        target_pq = find_parquet_for_quark(qid, data_dir=self.data_dir)
        if target_pq is None or not target_pq.exists():
            raise FileNotFoundError(f"Telemetry Parquet not found for {qid}")

        t0 = time.perf_counter()
        idx = self._get_or_load_index()
        from fightcade_analysis.coaching import _similarity_pct
        from fightcade_analysis.segmentation import SegmentationConfig

        search_fn = self._search_fn
        if search_fn is None:
            from fightcade_analysis.sequence_search import search_similar_sequences

            search_fn = search_similar_sequences
        cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
        raw = search_fn(
            target_pq,
            perspective=persp,
            start_gf=s_gf,
            end_gf=e_gf,
            parquet_dir=self.data_dir / "parquet",
            corpus_index=idx,
            config=cfg,
            top_k=k,
            tier_override=tier_override,
        )

        clips_dir = self.data_dir / "clips"
        sav_dir = self.data_dir / "savestates"
        enriched_matches: list[dict[str, Any]] = []
        for m in raw.get("matches") or []:
            mq = str(m.get("quark_id") or "")
            mp = int(m.get("perspective") or 1)
            mid_gf = int(m.get("mid_gf") or m.get("start_gf") or 0)
            pinfo = resolve_replay_players_and_ranks(mq, mp, data_dir=self.data_dir)
            clip_name = f"{mq}_gf{mid_gf}.mp4"
            clip_p = clips_dir / clip_name
            sav_p = sav_dir / f"{mq}_gf{mid_gf}.fs"
            has_clip = clip_p.is_file() and clip_p.stat().st_size > 1024
            has_sav = sav_p.is_file() and sav_p.stat().st_size > 4096
            nd = float(m.get("norm_dist", 0.35))
            sim_pct = int(round(_similarity_pct(nd))) if "similarity_pct" not in m else int(m["similarity_pct"])

            item = dict(m)
            item.update(
                {
                    "mid_gf": mid_gf,
                    "self_player_name": pinfo.get("self_player_name") or f"P{mp}",
                    "opp_player_name": pinfo.get("opp_player_name") or ("P2" if mp == 1 else "P1"),
                    "self_rank_letter": pinfo.get("self_rank_letter") or "",
                    "opp_rank_letter": pinfo.get("opp_rank_letter") or "",
                    "similarity_pct": sim_pct,
                    "has_clip": has_clip,
                    "clip_url": f"/clips/{clip_name}" if has_clip else None,
                    "has_savestate": has_sav,
                }
            )
            enriched_matches.append(item)

        return {
            "ok": True,
            "elapsed_s": round(time.perf_counter() - t0, 2),
            "retrieval_tier": raw.get("retrieval_tier") or "universal",
            "rounds_scanned": int(raw.get("rounds_scanned") or 0),
            "query": raw.get("query") or {
                "quark_id": qid,
                "perspective": persp,
                "round_id": round_id,
                "start_gf": s_gf,
                "end_gf": e_gf,
            },
            "matches": enriched_matches,
        }

    def render_clip(self, *, quark_id: str, frame: int, label: str = "") -> dict[str, Any]:
        if self._is_live_match_fn():
            raise RuntimeError("Cannot render clip while a live Fightcade match is active")
        qid = str(quark_id or "").strip()
        gf = int(frame)
        if not qid or gf < 1:
            raise ValueError("Provide valid quark_id and positive frame")
        req = ClipRequest(
            quark_id=qid,
            target_frame=gf,
            pre_frames=DEFAULT_PRE_FRAMES,
            post_frames=DEFAULT_POST_FRAMES,
            savestate_lead=DEFAULT_SAVESTATE_LEAD,
        )
        clips_dir = self.data_dir / "clips"
        sav_dir = self.data_dir / "savestates"
        res_map, _ = self._render_clips_fn(
            [req],
            clips_dir=clips_dir,
            savestates_dir=sav_dir,
            max_workers=1,
            timeout_seconds=180,
        )
        key = f"{qid}_gf{gf}"
        r = res_map.get(key)
        clip_p = r.mp4_path if (r and r.mp4_path and r.mp4_path.exists()) else (clips_dir / f"{key}.mp4")
        sav_p = r.savestate_path if (r and r.savestate_path and r.savestate_path.exists()) else (sav_dir / f"{key}.fs")
        has_clip = clip_p.is_file() and clip_p.stat().st_size > 1024
        has_sav = sav_p.is_file() and sav_p.stat().st_size > 4096
        if not has_clip:
            raise RuntimeError(f"Clip rendering produced no MP4 for {key}")
        return {
            "ok": True,
            "quark_id": qid,
            "frame": gf,
            "label": label,
            "has_clip": has_clip,
            "clip_url": f"/clips/{clip_p.name}",
            "has_savestate": has_sav,
        }


class InsightJobRunner:
    """Netplay-safe sequential background runner for AI Match Breakdowns & Verified Habits Checkups."""

    def __init__(
        self,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        is_live_match_fn: Callable[[], bool] | None = None,
        run_match_fn: Callable[..., dict[str, Any]] | None = None,
        run_habits_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.data_dir = data_dir.resolve()
        self._is_live_match_fn = is_live_match_fn or (lambda: False)
        self._run_match_fn = run_match_fn or self._default_run_match_breakdown
        self._run_habits_fn = run_habits_fn or self._default_run_verified_habits

        self._lock = threading.Lock()
        self._wake_evt = threading.Event()
        self._stop_evt = threading.Event()
        self._pending: deque[dict[str, Any]] = deque()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._loop, name="fc-insights-runner", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._wake_evt.set()
        with self._lock:
            t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)

    def has_running_job(self) -> bool:
        with self._lock:
            return any(j.get("state") == "running" for j in self._jobs.values())

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            vals = [dict(j) for j in self._jobs.values()]
        vals.sort(key=lambda j: str(j.get("updated_at") or ""), reverse=True)
        return vals[:20]

    def enqueue(
        self,
        *,
        kind: str,
        quark_id: str | None = None,
        perspective: int = 1,
        username: str | None = None,
    ) -> dict[str, Any]:
        k = str(kind or "").strip().lower()
        if k not in ("match_breakdown", "verified_habits"):
            raise ValueError("kind must be 'match_breakdown' or 'verified_habits'")
        if k == "match_breakdown":
            qid = str(quark_id or "").strip()
            if not qid:
                raise ValueError("match_breakdown requires quark_id")
            if int(perspective) not in (1, 2):
                raise ValueError("perspective must be 1 or 2")
            pq = find_parquet_for_quark(qid, data_dir=self.data_dir)
            if pq is None or not pq.exists():
                raise FileNotFoundError(f"Telemetry Parquet not found for {qid}. Process the match first.")
            job_key = f"insight_match_{qid}"
            msg = f"Queued AI Breakdown for {qid.split('-')[-1]}"
        else:
            uname = str(username or "").strip()
            if not uname:
                raise ValueError("verified_habits requires username")
            qid = f"habits-{uname}"
            job_key = f"insight_habits_{uname.lower()}"
            msg = f"Queued Verified Habits Checkup for {uname}"

        now_iso = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            existing = self._jobs.get(job_key)
            if existing and existing.get("state") in ("queued", "running", "paused"):
                return dict(existing)
            job = {
                "id": job_key,
                "kind": k,
                "quark_id": qid,
                "perspective": int(perspective),
                "username": str(username or "").strip(),
                "source": "insights",
                "state": "queued",
                "phase": "queued",
                "message": msg,
                "progress_done": 0,
                "progress_total": 100,
                "report_url": None,
                "error": None,
                "created_at": now_iso,
                "updated_at": now_iso,
            }
            self._jobs[job_key] = job
            self._pending.append(job)

        self.start()
        self._wake_evt.set()
        return dict(job)

    def _update(self, job_key: str, **fields: Any) -> None:
        with self._lock:
            j = self._jobs.get(job_key)
            if j is not None:
                j.update(fields)
                j["updated_at"] = datetime.now().isoformat(timespec="seconds")

    def _wait_if_live_match(self, job_key: str) -> bool:
        while not self._stop_evt.is_set() and self._is_live_match_fn():
            self._update(job_key, state="paused", phase="waiting_netplay", message="Paused while live Fightcade match is active")
            self._stop_evt.wait(2.0)
        return not self._stop_evt.is_set()

    def _default_run_match_breakdown(self, job: dict[str, Any]) -> dict[str, Any]:
        from fightcade_analysis.hybrid_habits import run_single_match_hybrid
        from fightcade_analysis.report_ui import build_report_html

        qid = str(job["quark_id"])
        persp = int(job["perspective"])
        pq_path = find_parquet_for_quark(qid, data_dir=self.data_dir)
        if pq_path is None or not pq_path.exists():
            raise FileNotFoundError(f"Parquet not found for {qid}")

        pq_dir = self.data_dir / "parquet"
        files = sorted(pq_dir.glob("*.parquet")) if pq_dir.is_dir() else []
        if not files:
            raise FileNotFoundError("No top-rank corpus Parquets found in data/parquet/")
        from fightcade_analysis.index import HierarchicalContextIndex, load_or_build_corpus
        from fightcade_analysis.segmentation import SegmentationConfig

        cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)
        corpus, _ = load_or_build_corpus(files, config=cfg, both_perspectives=True, cache_dir=self.data_dir / "index")
        index = HierarchicalContextIndex(corpus.meta, corpus.vectors)

        out_dir = self.data_dir / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        has_gem = bool(load_gemini_api_key())
        report, _ = run_single_match_hybrid(
            pq_path,
            perspective=persp,
            parquet_dir=pq_dir,
            corpus_index=index,
            cfg=cfg,
            data_dir=self.data_dir,
            out_dir=out_dir,
            top_n=5,
            render_clips=True,
            summarize=has_gem,
            clip_workers=2,
            gemini_workers=2,
            models=DEFAULT_MODEL_CHAIN,
        )
        enrich_coaching_metadata(report, data_dir=self.data_dir)
        stem = build_coaching_report_stem(report, data_dir=self.data_dir)
        json_path = out_dir / f"{stem}.json"
        html_path = out_dir / f"{stem}.html"
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        build_report_html(report, html_path, clips_dir=self.data_dir / "clips", savestates_dir=self.data_dir / "savestates")
        build_reports_index_html(out_dir)
        return {"report_url": f"/reports/{html_path.name}"}

    def _default_run_verified_habits(self, job: dict[str, Any]) -> dict[str, Any]:
        from fightcade_analysis import hybrid_habits

        uname = str(job["username"])
        out_dir = self.data_dir / "reports"
        rc = hybrid_habits.main(
            [
                "--username",
                uname,
                "--personal-dir",
                str(self.data_dir / "personal"),
                "--parquet-dir",
                str(self.data_dir / "parquet"),
                "--data-dir",
                str(self.data_dir),
                "--out-dir",
                str(out_dir),
            ]
        )
        if rc != 0:
            raise RuntimeError(f"Verified Habits pipeline exited with code {rc}")
        ins = list_insights(self.data_dir, include_legacy=False)
        latest = ins.get("latest_verified_habits")
        return {"report_url": latest["href"] if latest else None}

    def _run_job(self, job: dict[str, Any]) -> None:
        key = str(job["id"])
        if not self._wait_if_live_match(key):
            return
        try:
            self._update(
                key,
                state="running",
                phase="analyzing",
                message="Running Hybrid VLM + RAM analysis...",
                progress_done=25,
                progress_total=100,
            )
            if job["kind"] == "match_breakdown":
                res = self._run_match_fn(job)
            else:
                res = self._run_habits_fn(job)
            self._update(
                key,
                state="done",
                phase="ready",
                message="Analysis complete",
                progress_done=100,
                progress_total=100,
                report_url=(res or {}).get("report_url"),
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            self._update(
                key,
                state="error",
                phase="error",
                message=f"Failed: {exc}",
                error=str(exc),
            )

    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            job = None
            with self._lock:
                if self._pending:
                    job = self._pending.popleft()
            if job is None:
                self._wake_evt.wait(1.0)
                self._wake_evt.clear()
                continue
            self._run_job(job)
