"""Stage 4: Lightweight local HTML Coaching Report, MP4 Clip Playlist & Quick-Annotation UI.

Renders a self-contained dark-mode HTML dashboard for a Stage 3 coaching report JSON:
  - Optionally renders headless 6-second 60fps MP4 clips + instant `.fs` savestates for
    each high-regret moment and its closest pro alternative proofs.
  - Optionally generates cached multimodal Gemini tactical summaries directly from the clips + RAM state.
  - Displays side-by-side MP4 clips (paused by default, with per-card Play/Pause controls),
    similarity badges, informative character/role headers, and one-click Copy Takeover commands.
  - Includes a built-in Quick-Annotation toolbar stored in browser localStorage with JSON export.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
import time
from typing import Any

from fightcade_analysis.clip_renderer import (
    DEFAULT_POST_FRAMES,
    DEFAULT_PRE_FRAMES,
    DEFAULT_SAVESTATE_LEAD,
    ClipRequest,
    render_clips_batch,
)
from fightcade_analysis.coaching import diagnose_replay
from fightcade_analysis.gemini_summary import DEFAULT_MODEL_CHAIN, summarize_report_moments
from fightcade_analysis.hybrid_habits import run_single_match_hybrid
from fightcade_analysis.index import HierarchicalContextIndex, load_or_build_corpus
from fightcade_analysis.naming import (
    build_coaching_report_stem,
    build_reports_index_html,
    enrich_coaching_metadata,
    format_matchup_players_html,
)
from fightcade_analysis.segmentation import SegmentationConfig
from fightcade_analysis.takeover import (
    TAKEOVER_UI_CSS,
    TAKEOVER_UI_JS,
    render_takeover_buttons_html,
)

DEFAULT_TAGS: tuple[tuple[str, str], ...] = (
    ("true_aa", "True Anti-Air"),
    ("air_super", "Air Super"),
    ("safejump", "Safejump"),
    ("wakeup_oki", "Wakeup / Oki"),
    ("corner_escape", "Corner Escape"),
    ("blockstring", "Blockstring"),
    ("neutral_poke", "Neutral Poke"),
    ("false_segment", "False Segment"),
)


def _rel_media_url(html_path: Path, media_path: Path) -> str:
    rel = os.path.relpath(media_path, start=html_path.parent)
    return rel.replace("\\", "/")


def _takeover_cmd(quark_id: str, frame: int, player: int = 1, label: str = "") -> str:
    safe_label = label.replace('"', "'")
    return (
        f'C:\\Users\\621\\source\\repos\\fightcade-analysis\\scripts\\takeover.ps1 '
        f'-QuarkId {quark_id} -Frame {frame} -Player {int(player)} -Label "{safe_label}"'
    )


def render_clips_for_report(
    report: dict[str, Any],
    *,
    clips_dir: Path,
    savestates_dir: Path,
    max_moments: int = 5,
    max_options_per_moment: int = 2,
    pre_frames: int = DEFAULT_PRE_FRAMES,
    post_frames: int = DEFAULT_POST_FRAMES,
    savestate_lead: int = DEFAULT_SAVESTATE_LEAD,
    max_workers: int = 3,
    force: bool = False,
) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    """Render (or reuse matching cached) MP4 clips & `.fs` savestates using single-pass replay grouping."""
    reqs: list[ClipRequest] = []
    for m in report.get("moments", [])[:max_moments]:
        reqs.append(
            ClipRequest(
                quark_id=m["quark_id"],
                target_frame=int(m["game_frame"]),
                label=m["label"],
                pre_frames=pre_frames,
                post_frames=post_frames,
                savestate_lead=savestate_lead,
            )
        )
        for opt in m.get("options", [])[:max_options_per_moment]:
            proofs = opt.get("proofs", [])
            if proofs:
                pr = proofs[0]
                reqs.append(
                    ClipRequest(
                        quark_id=pr["quark_id"],
                        target_frame=int(pr["game_frame"]),
                        label=pr["label"],
                        pre_frames=pre_frames,
                        post_frames=post_frames,
                        savestate_lead=savestate_lead,
                    )
                )

    res_map, stats = render_clips_batch(
        reqs,
        clips_dir=clips_dir,
        savestates_dir=savestates_dir,
        max_workers=max_workers,
        force=force,
    )
    rendered = {
        key: {
            "mp4": str(cr.mp4_path),
            "savestate": str(cr.savestate_path) if cr.savestate_path else "",
        }
        for key, cr in res_map.items()
    }
    return rendered, stats


def build_report_html(
    report: dict[str, Any],
    out_html: Path,
    *,
    clips_dir: Path | None = None,
    savestates_dir: Path | None = None,
) -> Path:
    """Generate a self-contained local HTML coaching & clip-playlist report."""
    out_html.parent.mkdir(parents=True, exist_ok=True)
    clips_dir = clips_dir or (out_html.parents[1] / "clips")
    savestates_dir = savestates_dir or (out_html.parents[1] / "savestates")

    def _video_block(
        qid: str,
        game_frame: int,
        player: int,
        label: str,
        extra_meta: str,
        is_user: bool,
        caption: str = "",
    ) -> str:
        stem = f"{qid}_gf{game_frame}"
        mp4 = clips_dir / f"{stem}.mp4"
        fs = savestates_dir / f"{stem}.fs"
        cmd = _takeover_cmd(qid, game_frame, player=player, label=label)
        has_fs = fs.exists()

        if mp4.exists():
            src = html.escape(_rel_media_url(out_html, mp4))
            # Paused by default (no autoplay); preload metadata so initial frame renders cleanly
            media_html = f'<video src="{src}" loop muted playsinline preload="metadata" controls></video>'
        else:
            render_cmd = f'fightcade-analysis\\.venv\\Scripts\\python.exe -m fightcade_analysis.clip_renderer --quark-id {qid} --frame {game_frame}'
            media_html = (
                f'<div class="no-video">'
                f'<div>No clip cached yet for <code>{html.escape(stem)}</code></div>'
                f'<button class="btn-small" data-copy="{html.escape(render_cmd, quote=True)}" onclick="copyFromData(this)">Copy Render Cmd</button>'
                f'</div>'
            )

        fs_badge = '<span class="badge badge-ok">Instant &lt;1s .fs Ready</span>' if has_fs else '<span class="badge badge-muted">Stream Seek</span>'
        role_cls = "clip-user" if is_user else "clip-pro"
        cap_html = f'<div class="clip-caption">{html.escape(caption)}</div>' if caption else ""
        btns_html = render_takeover_buttons_html(qid, game_frame, player, label, cmd, is_pro=not is_user)
        return f"""
        <div class="clip-box {role_cls}">
          <div class="clip-meta">{extra_meta} {fs_badge}</div>
          {media_html}
          {cap_html}
          <div class="clip-actions">
            <code>{html.escape(qid)} @ gf{game_frame}</code>
            {btns_html}
          </div>
        </div>
        """

    is_hybrid_report = report.get("pipeline") == "hybrid_v2"
    cards_html: list[str] = []
    for idx, m in enumerate(report.get("moments", []), 1):
        qid = m["quark_id"]
        gframe = int(m["game_frame"])
        card_id = f"{qid}_p{m['perspective']}_gf{gframe}"
        tag_buttons = "".join(
            f'<button class="tag-btn" data-card="{html.escape(card_id)}" data-tag="{tag_key}" '
            f'onclick="toggleTag(this)">{html.escape(tag_label)}</button>'
            for tag_key, tag_label in DEFAULT_TAGS
        )

        if m.get("pipeline") == "hybrid_v2" or is_hybrid_report:
            vt = m.get("visual_tag") or {}
            what_h = html.escape(str(vt.get("what_happened", "")))
            mistake = html.escape(str(vt.get("self_mistake", "")))
            model_tag = html.escape(str(vt.get("model", "gemini")))
            ai_html = (
                f"""
                <div class="ai-box" style="border-color: rgba(16,185,129,0.4); background: rgba(16,185,129,0.08);">
                  <div class="ai-title" style="color:#6ee7b7;">Pass-1 Visual Breakdown <span class="badge badge-ok">{model_tag}</span></div>
                  {f'<p><strong>What happened:</strong> {what_h}</p>' if what_h else ''}
                  {f'<p><strong>Mistake:</strong> {mistake}</p>' if mistake else ''}
                </div>
                """
                if (what_h or mistake)
                else ""
            )

            hp_lost = int(m.get("hp_lost", 0))
            hp_pct = float(m.get("hp_lost_pct", 0.0))
            hits = int(m.get("combo_hits", 1))
            ko_tag = " &middot; KO" if m.get("was_ko") else ""
            sf_tag = " &middot; Pre-Superfreeze" if m.get("had_superfreeze") else ""

            user_meta = (
                f"<strong>YOUR FOOTAGE:</strong> Hit #1 pre-impact anchor "
                f"(<strong>-{hp_lost} HP / -{hp_pct:.1f}%</strong>, {hits}-hit{ko_tag})"
            )
            user_block = _video_block(
                qid,
                gframe,
                int(m["perspective"]),
                m["label"],
                user_meta,
                is_user=True,
                caption=str(vt.get("what_happened", "")),
            )

            pro_blocks: list[str] = []
            data_root_dir = clips_dir.parent
            for pc in m.get("verified_pro_solutions", []):
                ver = pc.get("verification") or {}
                sim_str = f"Setup Match {pc.get('similarity_pct', 0)}%"
                pro_who = format_matchup_players_html(
                    str(pc["quark_id"]),
                    int(pc["perspective"]),
                    str(pc.get("self_char_name", "")),
                    str(pc.get("opp_char_name", "")),
                    data_dir=data_root_dir,
                    precomputed=pc,
                )
                p_meta = (
                    f"<strong>VERIFIED TOP-RANK SOLUTION:</strong> "
                    f"{pro_who} "
                    f"(EV <strong>{float(pc.get('outcome_value', 0.0)):+.2f}</strong>, "
                    f"<span class='sim-tag'>{sim_str}</span>)"
                )
                cap_parts = []
                if ver.get("pro_solution"):
                    cap_parts.append(f"Top-Rank solution: {ver['pro_solution']}")
                if ver.get("relevance_reason"):
                    cap_parts.append(f"Relevance: {ver['relevance_reason']}")
                pro_blocks.append(
                    _video_block(
                        str(pc["quark_id"]),
                        int(pc["game_frame"]),
                        int(pc["perspective"]),
                        f"Top-Rank Solution: {m.get('category_label', '')}",
                        p_meta,
                        is_user=False,
                        caption=" ".join(cap_parts),
                    )
                )

            rej = int(m.get("rejected_pro_count", 0))
            pro_note = (
                f'<div class="sub" style="margin-bottom:10px;font-style:italic;">Pass-2 visual relevance gate filtered out {rej} non-matching RAM top-rank candidate(s).</div>'
                if rej > 0
                else ""
            )

            cards_html.append(
                f"""
                <section class="card" id="{html.escape(card_id)}">
                  <div class="card-header">
                    <div>
                      <span class="rank-pill">#{idx}</span>
                      <h2>{html.escape(m['label'])}</h2>
                      <div class="sub">
                        Round {m['round_id']} &middot; Pre-Hit GameFrame {gframe}{sf_tag} &middot;
                        top-rank search tier=<code>{html.escape(str(m.get('retrieval_tier', 'none')))}</code> &middot;
                        <span class="badge badge-ok">Visually Verified</span>
                      </div>
                    </div>
                    <div class="header-right">
                      <button class="btn play-card-btn" onclick="toggleCardVideos(this, '{html.escape(card_id)}')">&#9654; Play Card Clips</button>
                      <div class="regret-box">
                        <div class="regret-val">-{hp_lost} HP ({hp_pct:.1f}%)</div>
                        <div class="regret-sub">{hits}-hit damage episode{ko_tag}</div>
                      </div>
                    </div>
                  </div>

                  {ai_html}

                  <div class="clips-grid">
                    {user_block}
                    {''.join(pro_blocks[:2])}
                  </div>
                  {pro_note}

                  <div class="annot-bar">
                    <span class="annot-title">Quick Annotate:</span>
                    {tag_buttons}
                  </div>
                </section>
                """
            )
            continue

        ai = m.get("ai_summary") or {}
        captions = ai.get("clip_captions") if isinstance(ai.get("clip_captions"), dict) else {}

        ai_html = ""
        if ai.get("situation_summary") or ai.get("alternative_takeaway"):
            model_tag = html.escape(str(ai.get("model", "gemini")))
            sit = html.escape(str(ai.get("situation_summary", "")))
            take = html.escape(str(ai.get("alternative_takeaway", "")))
            ai_html = f"""
            <div class="ai-box">
              <div class="ai-title">AI Interaction Breakdown <span class="badge badge-ai">{model_tag}</span></div>
              {f'<p><strong>What happened:</strong> {sit}</p>' if sit else ''}
              {f'<p><strong>Top-Rank adjustment:</strong> {take}</p>' if take else ''}
            </div>
            """

        user_meta = (
            f"<strong>YOUR CHOICE:</strong> {html.escape(m['user_action'])} "
            f"(Outcome <strong>{m['actual_value']:+.2f}</strong>, HP {m['actual_hp_swing']:+.2f})"
        )
        user_block = _video_block(
            qid,
            gframe,
            int(m["perspective"]),
            m["label"],
            user_meta,
            is_user=True,
            caption=str(captions.get("user", "")),
        )

        pro_blocks: list[str] = []
        opt_rows: list[str] = []
        alt_idx = 0
        for opt in m.get("options", []):
            sim_cell = f"{opt.get('best_similarity_pct', '—')}%" if "best_similarity_pct" in opt else "—"
            opt_rows.append(
                f"<tr><td>{html.escape(opt['action_coarse'])}</td>"
                f"<td>{opt['count']}</td>"
                f"<td>{sim_cell}</td>"
                f"<td><strong>{opt['ev']:+.2f}</strong></td>"
                f"<td>{opt['mean_hp_swing']:+.2f}</td>"
                f"<td class='gain'>{opt['ev_gain_vs_user']:+.2f}</td></tr>"
            )
            for pr in opt.get("proofs", [])[:1]:
                alt_idx += 1
                sim_str = f"Match {pr['similarity_pct']}%" if "similarity_pct" in pr else f"d={pr['l2_dist']}"
                pro_who = format_matchup_players_html(
                    str(pr["quark_id"]),
                    int(pr["perspective"]),
                    str(pr["self_char_name"]),
                    str(pr["opp_char_name"]),
                    data_dir=clips_dir.parent,
                    precomputed=pr,
                )
                p_meta = (
                    f"<strong>ALT ({html.escape(opt['action_coarse'])}):</strong> "
                    f"{pro_who} "
                    f"(Outcome <strong>{pr['outcome_value']:+.2f}</strong>, "
                    f"<span class='sim-tag'>{sim_str}</span>)"
                )
                pro_blocks.append(
                    _video_block(
                        pr["quark_id"],
                        int(pr["game_frame"]),
                        int(pr["perspective"]),
                        pr["label"],
                        p_meta,
                        is_user=False,
                        caption=str(captions.get(f"alt_{alt_idx}", "")),
                    )
                )

        tag_buttons = "".join(
            f'<button class="tag-btn" data-card="{html.escape(card_id)}" data-tag="{tag_key}" '
            f'onclick="toggleTag(this)">{html.escape(tag_label)}</button>'
            for tag_key, tag_label in DEFAULT_TAGS
        )
        sim_sub = f" &middot; match={m['match_similarity_pct']}%" if "match_similarity_pct" in m else ""

        cards_html.append(
            f"""
            <section class="card" id="{html.escape(card_id)}">
              <div class="card-header">
                <div>
                  <span class="rank-pill">#{idx}</span>
                  <h2>{html.escape(m['label'])}</h2>
                  <div class="sub">
                    Round {m['round_id']} &middot; GameFrame {gframe} &middot;
                    dx={m['t0_dx_px']:.0f}px &middot; corner={m['t0_self_corner']:.2f} &middot;
                    tier=<code>{html.escape(m['retrieval_tier'])}</code> (K={m['neighbors_used']}{sim_sub})
                  </div>
                </div>
                <div class="header-right">
                  <button class="btn play-card-btn" onclick="toggleCardVideos(this, '{html.escape(card_id)}')">&#9654; Play Card Clips</button>
                  <div class="regret-box">
                    <div class="regret-val">Regret {m['regret']:+.2f}</div>
                    <div class="regret-sub">Corpus EV {m['ev_corpus']:+.2f} vs Actual {m['actual_value']:+.2f}</div>
                  </div>
                </div>
              </div>

              {ai_html}

              <div class="clips-grid">
                {user_block}
                {''.join(pro_blocks[:2])}
              </div>

              <table class="opt-table">
                <thead>
                  <tr><th>Corpus Option</th><th>Samples</th><th>Closest Match</th><th>Option EV</th><th>Mean HP Swing</th><th>Gain vs Yours</th></tr>
                </thead>
                <tbody>{''.join(opt_rows)}</tbody>
              </table>

              <div class="annot-bar">
                <span class="annot-title">Quick Annotate:</span>
                {tag_buttons}
              </div>
            </section>
            """
        )

    s_player = report.get("self_player_name") or f"P{report.get('perspective')}"
    o_player = report.get("opp_player_name") or ("P2" if report.get("perspective") == 1 else "P1")
    mdate = report.get("match_date") or ""
    date_part = f" — {mdate}" if mdate else ""
    title = (
        f"{s_player} ({report.get('self_char_name')}) vs "
        f"{o_player} ({report.get('opp_char_name')}){date_part}"
    )
    storage_key = f"fc_annotations_{report.get('target_quark_id')}_p{report.get('perspective')}"

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>{html.escape(title)}</title>
<style>
  :root {{
    --bg: #0b1120; --surface: #111827; --card: #172033; --border: #263248;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; --danger: #f43f5e; --ok: #22c55e; --warn: #f59e0b;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); padding: 24px; }}
  header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 16px; }}
  h1 {{ margin: 0; font-size: 1.35rem; }}
  .header-controls {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .summary-meta {{ color: var(--muted); font-size: 0.9rem; margin-top: 4px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 18px; margin-bottom: 20px; }}
  .card-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; margin-bottom: 14px; }}
  .card-header h2 {{ display: inline; margin: 0 0 0 8px; font-size: 1.08rem; }}
  .header-right {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .rank-pill {{ background: var(--accent); color: #082f49; font-weight: 700; padding: 2px 8px; border-radius: 999px; font-size: 0.85rem; }}
  .sub {{ color: var(--muted); font-size: 0.84rem; margin-top: 6px; }}
  .regret-box {{ text-align: right; background: rgba(244, 63, 94, 0.12); border: 1px solid rgba(244, 63, 94, 0.35); padding: 8px 12px; border-radius: 8px; }}
  .regret-val {{ color: #fb7185; font-weight: 700; font-size: 1.05rem; }}
  .regret-sub {{ color: var(--muted); font-size: 0.78rem; }}
  .ai-box {{ background: rgba(56, 189, 248, 0.08); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; font-size: 0.88rem; line-height: 1.48; }}
  .ai-box p {{ margin: 5px 0 0 0; }}
  .ai-title {{ font-weight: 700; font-size: 0.84rem; color: #bae6fd; display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }}
  .badge-ai {{ background: rgba(56, 189, 248, 0.2); color: #7dd3fc; }}
  .clips-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 12px; margin-bottom: 14px; }}
  .clip-box {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px; display: flex; flex-direction: column; }}
  .clip-user {{ border-color: rgba(244, 63, 94, 0.55); }}
  .clip-pro {{ border-color: rgba(34, 197, 94, 0.45); }}
  .clip-meta {{ font-size: 0.82rem; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; gap: 6px; flex-wrap: wrap; }}
  .sim-tag {{ color: #7dd3fc; font-weight: 600; }}
  video {{ width: 100%; border-radius: 6px; background: #000; display: block; }}
  .clip-caption {{ font-size: 0.8rem; color: #cbd5e1; margin-top: 8px; line-height: 1.38; }}
  .no-video {{ height: 180px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 10px; background: #090d16; border-radius: 6px; color: var(--muted); font-size: 0.82rem; text-align: center; padding: 12px; }}
  .clip-actions {{ margin-top: auto; padding-top: 8px; display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 0.78rem; color: var(--muted); }}
  .btn, .btn-small {{ background: #1e293b; color: var(--text); border: 1px solid #334155; border-radius: 6px; padding: 5px 10px; cursor: pointer; font-size: 0.78rem; }}
  .btn:hover, .btn-small:hover {{ border-color: var(--accent); color: #fff; }}
  .play-card-btn {{ background: rgba(56, 189, 248, 0.14); border-color: rgba(56, 189, 248, 0.4); color: #e0f2fe; font-weight: 600; padding: 7px 12px; }}
  .badge {{ font-size: 0.7rem; padding: 2px 6px; border-radius: 999px; font-weight: 600; }}
  .badge-ok {{ background: rgba(34, 197, 94, 0.18); color: #4ade80; }}
  .badge-muted {{ background: rgba(148, 163, 184, 0.15); color: var(--muted); }}
  .opt-table {{ width: 100%; border-collapse: collapse; font-size: 0.83rem; margin-bottom: 12px; }}
  .opt-table th, .opt-table td {{ padding: 6px 10px; border-bottom: 1px solid var(--border); text-align: left; }}
  .opt-table th {{ color: var(--muted); font-weight: 600; }}
  .gain {{ color: #4ade80; font-weight: 600; }}
  .annot-bar {{ display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding-top: 6px; }}
  .annot-title {{ font-size: 0.8rem; color: var(--muted); margin-right: 4px; }}
  .tag-btn {{ background: #0f172a; color: var(--muted); border: 1px solid var(--border); border-radius: 999px; padding: 4px 10px; font-size: 0.76rem; cursor: pointer; }}
  .tag-btn.active {{ background: rgba(56, 189, 248, 0.2); border-color: var(--accent); color: #e0f2fe; font-weight: 600; }}
{TAKEOVER_UI_CSS}
</style>
</head>
<body>
  <header>
    <div>
      <div style="margin-bottom:6px;"><a href="/#insights" style="color:var(--accent);text-decoration:none;font-size:0.82rem;">&larr; Fightcade Lab Insights</a></div>
      <h1>{html.escape(title)}</h1>
      <div class="summary-meta">
        QuarkId <code>{html.escape(str(report.get('target_quark_id', '')))}</code> (P{report.get('perspective')}) &middot;
        {(f"Hybrid VLM + RAM Verified Analysis ({report.get('total_damage_episodes', 0)} Hit-#1 episodes) against {report.get('corpus_size', 0):,} top-rank states" if is_hybrid_report else f"Analyzed {report.get('total_decision_points', 0)} decision points against {report.get('corpus_size', 0):,} indexed corpus states")} &middot;
        Showing top {len(report.get('moments', []))} moments (videos paused by default)
      </div>
    </div>
    <div class="header-controls">
      <button class="btn" onclick="pauseAllVideos()">Pause All Videos</button>
      <button class="btn" onclick="exportAnnotations()">Export Annotations JSON</button>
    </div>
  </header>

  {''.join(cards_html) if cards_html else '<p class="summary-meta">No positive-regret moments found.</p>'}

<script>
const STORAGE_KEY = {json.dumps(storage_key)};
const REPORT_META = {json.dumps({"target_quark_id": report.get("target_quark_id"), "perspective": report.get("perspective")})};

function loadStore() {{
  try {{ return JSON.parse(localStorage.getItem(STORAGE_KEY) || '{{}}'); }} catch {{ return {{}}; }}
}}
function saveStore(obj) {{
  localStorage.setItem(STORAGE_KEY, JSON.stringify(obj));
}}
function hydrateTags() {{
  const store = loadStore();
  document.querySelectorAll('.tag-btn').forEach(btn => {{
    const cid = btn.dataset.card;
    const tag = btn.dataset.tag;
    const tags = store[cid] || [];
    btn.classList.toggle('active', tags.includes(tag));
  }});
}}
function toggleTag(btn) {{
  const store = loadStore();
  const cid = btn.dataset.card;
  const tag = btn.dataset.tag;
  const set = new Set(store[cid] || []);
  if (set.has(tag)) set.delete(tag); else set.add(tag);
  store[cid] = Array.from(set);
  saveStore(store);
  hydrateTags();
}}
function pauseAllVideos() {{
  document.querySelectorAll('video').forEach(v => v.pause());
  document.querySelectorAll('.play-card-btn').forEach(b => {{ b.innerHTML = '&#9654; Play Card Clips'; }});
}}
function toggleCardVideos(btn, cardId) {{
  const card = document.getElementById(cardId);
  if (!card) return;
  const vids = Array.from(card.querySelectorAll('video'));
  if (!vids.length) return;
  const anyPlaying = vids.some(v => !v.paused);
  if (anyPlaying) {{
    vids.forEach(v => v.pause());
    btn.innerHTML = '&#9654; Play Card Clips';
  }} else {{
    pauseAllVideos();
    vids.forEach(v => {{ v.currentTime = 0; v.play().catch(() => {{}}); }});
    btn.innerHTML = '&#10074;&#10074; Pause Card Clips';
  }}
}}
function copyFromData(btn) {{
  const text = btn.getAttribute('data-copy') || '';
  const showDone = (msg) => {{
    const orig = btn.dataset.origLabel || btn.textContent;
    btn.dataset.origLabel = orig;
    btn.textContent = msg;
    setTimeout(() => {{ btn.textContent = orig; }}, 1400);
  }};
  const fallbackCopy = () => {{
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    let ok = false;
    try {{ ok = document.execCommand('copy'); }} catch {{ ok = false; }}
    document.body.removeChild(ta);
    showDone(ok ? 'Copied!' : 'Copy failed');
  }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(() => showDone('Copied!')).catch(fallbackCopy);
  }} else {{
    fallbackCopy();
  }}
}}
function exportAnnotations() {{
  const payload = {{ ...REPORT_META, annotations: loadStore() }};
  const blob = new Blob([JSON.stringify(payload, null, 2)], {{ type: 'application/json' }});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = STORAGE_KEY + '.json';
  a.click();
}}
hydrateTags();
{TAKEOVER_UI_JS}
</script>
</body>
</html>
"""
    out_html.write_text(page, encoding="utf-8")
    return out_html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Stage 3+4 coaching report HTML with optional MP4 clips and AI summaries.")
    parser.add_argument("--target", type=Path, required=True, help="Target replay Parquet to diagnose")
    parser.add_argument("--perspective", type=int, choices=[1, 2], default=1)
    parser.add_argument("--parquet-dir", type=Path, default=Path("data/parquet"))
    parser.add_argument("--mode", choices=["hybrid", "legacy"], default="hybrid", help="Analysis pipeline: hybrid (ADR-001 Hit-#1 + Pass-1/2 Vision, default) or legacy (PELT)")
    parser.add_argument("--rebuild-cache", action="store_true", help="Force rebuild of disk PELT corpus cache")
    parser.add_argument("--top-n", type=int, default=5)
    parser.add_argument("--max-norm-dist", type=float, default=0.34)
    parser.add_argument("--render-clips", action="store_true", help="Headlessly render 6s MP4 clips + .fs savestates")
    parser.add_argument("--force-clips", action="store_true", help="Force re-render even if cached clips match")
    parser.add_argument("--clip-workers", type=int, default=3, help="Max concurrent replay passes during clip rendering (default: 3)")
    parser.add_argument("--pre-frames", type=int, default=DEFAULT_PRE_FRAMES, help="Frames before t0 (default: 210 = 3.5s)")
    parser.add_argument("--post-frames", type=int, default=DEFAULT_POST_FRAMES, help="Frames after t0 (default: 150 = 2.5s)")
    parser.add_argument("--savestate-lead", type=int, default=DEFAULT_SAVESTATE_LEAD, help="Savestate frames before t0 (default: 120 = 2.0s)")
    parser.add_argument("--max-clip-moments", type=int, default=3, help="How many top moments to render clips/summaries for")
    parser.add_argument("--summarize", action="store_true", help="Generate multimodal Gemini interaction summaries")
    parser.add_argument("--force-summarize", action="store_true", help="Ignore cached Gemini summaries")
    parser.add_argument("--gemini-workers", type=int, default=2, help="Max concurrent Gemini summary requests (default: 2)")
    parser.add_argument("--gemini-model", default=None, help="Optional single Gemini model ID override")
    parser.add_argument("--out-name", default=None, help="Optional custom output filename stem (without extension)")
    parser.add_argument("--out-dir", type=Path, default=Path("data/reports"))
    args = parser.parse_args(argv)

    t_start = time.perf_counter()
    timings: dict[str, float] = {}

    files = sorted(args.parquet_dir.glob("*.parquet"))
    cfg = SegmentationConfig(penalty=18.0, min_size=45, jump=5, lead_frames=8)

    t0 = time.perf_counter()
    corpus, from_cache = load_or_build_corpus(
        files,
        config=cfg,
        both_perspectives=True,
        cache_dir=args.parquet_dir.resolve().parent / "index",
        force_rebuild=args.rebuild_cache,
    )
    timings["corpus_load"] = round(time.perf_counter() - t0, 3)

    t0 = time.perf_counter()
    index = HierarchicalContextIndex(corpus.meta, corpus.vectors)
    timings["index_build"] = round(time.perf_counter() - t0, 3)

    data_root = args.parquet_dir.resolve().parent
    args.out_dir.mkdir(parents=True, exist_ok=True)
    clips_dir = data_root / "clips"
    savestates_dir = data_root / "savestates"

    clip_stats: dict[str, int] | None = None
    gem_stats: dict[str, int] | None = None
    hybrid_stats: dict[str, Any] | None = None

    if args.mode == "hybrid":
        t0 = time.perf_counter()
        models = (args.gemini_model,) if args.gemini_model else DEFAULT_MODEL_CHAIN
        report, hybrid_stats = run_single_match_hybrid(
            args.target,
            perspective=args.perspective,
            parquet_dir=args.parquet_dir,
            corpus_index=index,
            cfg=cfg,
            data_dir=data_root,
            out_dir=args.out_dir,
            top_n=args.top_n,
            render_clips=args.render_clips,
            summarize=args.summarize,
            clip_workers=args.clip_workers,
            gemini_workers=args.gemini_workers,
            models=models,
        )
        timings["hybrid_pipeline"] = round(time.perf_counter() - t0, 3)
        enrich_coaching_metadata(report, data_dir=data_root)
        stem = args.out_name or build_coaching_report_stem(report, data_dir=data_root)
        json_path = args.out_dir / f"{stem}.json"
        html_path = args.out_dir / f"{stem}.html"
    else:
        t0 = time.perf_counter()
        report = diagnose_replay(
            args.target,
            index,
            perspective=args.perspective,
            config=cfg,
            top_n=args.top_n,
            max_norm_dist=args.max_norm_dist,
        )
        timings["diagnose"] = round(time.perf_counter() - t0, 3)

        enrich_coaching_metadata(report, data_dir=data_root)
        stem = args.out_name or build_coaching_report_stem(report, data_dir=data_root)
        json_path = args.out_dir / f"{stem}.json"
        html_path = args.out_dir / f"{stem}.html"

        if args.render_clips:
            t0 = time.perf_counter()
            _, clip_stats = render_clips_for_report(
                report,
                clips_dir=clips_dir,
                savestates_dir=savestates_dir,
                max_moments=args.max_clip_moments,
                max_options_per_moment=2,
                pre_frames=args.pre_frames,
                post_frames=args.post_frames,
                savestate_lead=args.savestate_lead,
                max_workers=args.clip_workers,
                force=args.force_clips,
            )
            timings["clip_render"] = round(time.perf_counter() - t0, 3)

        if args.summarize:
            t0 = time.perf_counter()
            models = (args.gemini_model,) if args.gemini_model else DEFAULT_MODEL_CHAIN
            _, gem_stats = summarize_report_moments(
                report,
                clips_dir=clips_dir,
                cache_path=args.out_dir / "gemini_summary_cache.json",
                max_moments=args.max_clip_moments,
                models=models,
                pre_seconds=args.pre_frames / 60.0,
                total_seconds=(args.pre_frames + args.post_frames) / 60.0,
                max_workers=args.gemini_workers,
                force=args.force_summarize,
            )
            timings["gemini_summary"] = round(time.perf_counter() - t0, 3)

    t0 = time.perf_counter()
    timings["total"] = round(time.perf_counter() - t_start, 3)
    report["timings_s"] = timings
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    build_report_html(report, html_path, clips_dir=clips_dir, savestates_dir=savestates_dir)
    index_path = build_reports_index_html(args.out_dir)
    timings["html_write"] = round(time.perf_counter() - t0, 3)
    timings["total"] = round(time.perf_counter() - t_start, 3)

    cache_label = "cache hit" if from_cache else f"built ({len(files)} parquets)"
    print(f"Wrote coaching report JSON: {json_path}")
    print(f"Wrote coaching report HTML: {html_path}")
    print(f"Updated reports hub       : {index_path}")
    print("=== Stage Timings ===")
    print(f"  corpus_load    : {timings['corpus_load']:6.2f}s ({cache_label}, {len(corpus.meta):,} pts)")
    print(f"  index_build    : {timings['index_build']:6.2f}s")
    if hybrid_stats is not None:
        p1s = hybrid_stats.get("pass1") or {}
        p2s = hybrid_stats.get("pass2") or {}
        print(
            f"  hybrid_pipeline: {timings['hybrid_pipeline']:6.2f}s "
            f"(episodes={hybrid_stats['episodes_found']} -> top {hybrid_stats['selected']}, "
            f"pass1_ok={p1s.get('called', 0) + p1s.get('cached', 0)}, "
            f"pass2_passed={p2s.get('passed', 0)}/{p2s.get('checked', 0)})"
        )
    if "diagnose" in timings:
        print(f"  diagnose       : {timings['diagnose']:6.2f}s")
    if clip_stats is not None:
        print(
            f"  clip_render    : {timings['clip_render']:6.2f}s "
            f"(rendered={clip_stats['rendered']}, cached={clip_stats['cached']}, "
            f"fbneo_passes={clip_stats['fbneo_passes']}, failed={clip_stats['failed']})"
        )
    if gem_stats is not None:
        print(
            f"  gemini_summary : {timings['gemini_summary']:6.2f}s "
            f"(api_calls={gem_stats['api_calls']}, cached={gem_stats['cached']}, failed={gem_stats['failed']})"
        )
    print(f"  html_write     : {timings['html_write']:6.2f}s")
    print(f"  TOTAL          : {timings['total']:6.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
