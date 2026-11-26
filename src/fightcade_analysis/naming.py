"""Human-readable report naming, Fightcade manifest metadata lookup, and Reports Hub generator.

Naming conventions:
  - Full-set analysis:
      {YYYY-MM-DD}_{self_user}_{self_char}_vs_{opp_user}_{opp_char}_{short_qid}
  - Manual sequence search:
      {YYYY-MM-DD}_{self_user}_{self_char}_vs_{opp_user}_{opp_char}_{short_qid}_r{round}_{topic}_gf{start}-{end}

Keeps full-set reports and sequence searches from the same match grouped together chronologically
in Windows Explorer while also maintaining a browsable `data/reports/index.html` hub.
"""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re
from typing import Any

from fightcade_analysis.clip_renderer import DEFAULT_DATA_DIR

RANK_LETTERS: dict[int, str] = {
    6: "S",
    5: "A",
    4: "B",
    3: "C",
    2: "D",
    1: "E",
    0: "U",
}


def format_rank_letter(rank: Any) -> str:
    """Map Fightcade integer rank (1..6, 0=unranked) to letter ('S', 'A', 'B', 'C', 'D', 'E', 'U')."""
    if rank is None or rank == "":
        return ""
    try:
        return RANK_LETTERS.get(int(rank), "")
    except (TypeError, ValueError):
        return ""


def resolve_replay_players_and_ranks(
    quark_id: str,
    perspective: int,
    data_dir: Path = DEFAULT_DATA_DIR,
) -> dict[str, Any]:
    """Resolve focused & opponent player handles and Fightcade ranks from cached `data/*.json` manifests."""
    meta = lookup_replay_metadata(quark_id, data_dir=data_dir)
    if int(perspective) == 1:
        s_name, s_rank = meta.get("p1_name"), meta.get("p1_rank")
        o_name, o_rank = meta.get("p2_name"), meta.get("p2_rank")
    else:
        s_name, s_rank = meta.get("p2_name"), meta.get("p2_rank")
        o_name, o_rank = meta.get("p1_name"), meta.get("p1_rank")
    return {
        "self_player_name": str(s_name) if s_name else "",
        "self_rank": int(s_rank) if s_rank is not None else None,
        "self_rank_letter": format_rank_letter(s_rank),
        "opp_player_name": str(o_name) if o_name else "",
        "opp_rank": int(o_rank) if o_rank is not None else None,
        "opp_rank_letter": format_rank_letter(o_rank),
    }


def _rank_pill_html(letter: str) -> str:
    if not letter:
        return ""
    colors = {
        "S": ("rgba(250, 204, 21, 0.22)", "#fde047", "rgba(250, 204, 21, 0.45)"),
        "A": ("rgba(192, 132, 252, 0.22)", "#d8b4fe", "rgba(192, 132, 252, 0.45)"),
        "B": ("rgba(56, 189, 248, 0.20)", "#7dd3fc", "rgba(56, 189, 248, 0.4)"),
        "C": ("rgba(52, 211, 153, 0.18)", "#6ee7b7", "rgba(52, 211, 153, 0.35)"),
    }
    bg, fg, bd = colors.get(letter, ("rgba(148, 163, 184, 0.18)", "#cbd5e1", "rgba(148, 163, 184, 0.35)"))
    return (
        f'<span style="display:inline-block;padding:0 5px;margin-left:3px;border-radius:4px;'
        f'font-size:0.68rem;font-weight:700;background:{bg};color:{fg};border:1px solid {bd};">'
        f'{html.escape(letter)}</span>'
    )


def format_matchup_players_html(
    quark_id: str,
    perspective: int,
    self_char_name: str,
    opp_char_name: str,
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    precomputed: dict[str, Any] | None = None,
) -> str:
    """Format `PlayerA [S] (Akuma) vs PlayerB [A] (Hugo)` HTML (falls back gracefully to characters only)."""
    info = precomputed if (precomputed and precomputed.get("self_player_name")) else resolve_replay_players_and_ranks(
        quark_id, perspective, data_dir=data_dir
    )
    s_name = str(info.get("self_player_name") or "").strip()
    o_name = str(info.get("opp_player_name") or "").strip()
    s_rk = str(info.get("self_rank_letter") or format_rank_letter(info.get("self_rank")))
    o_rk = str(info.get("opp_rank_letter") or format_rank_letter(info.get("opp_rank")))

    sc = html.escape(str(self_char_name or ""))
    oc = html.escape(str(opp_char_name or ""))

    if s_name or o_name:
        left = f"{html.escape(s_name or 'P1')}{_rank_pill_html(s_rk)} ({sc})"
        right = f"{html.escape(o_name or 'P2')}{_rank_pill_html(o_rk)} ({oc})"
        return f"{left} vs {right}"
    return f"{sc} vs {oc}"


def slugify(text: str, *, max_len: int = 24, sep: str = "-") -> str:
    """Convert arbitrary text (player handle, note, interaction) to a Windows-safe lowercase ASCII slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9]+", sep, str(text or "").strip().lower())
    if sep:
        cleaned = re.sub(re.escape(sep) + r"{2,}", sep, cleaned).strip(sep)
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(sep)
    return cleaned or "unknown"


def char_slug(char_name: str) -> str:
    """Compact lowercase alphanumeric token for a character name (e.g. 'Chun-Li' -> 'chunli')."""
    token = re.sub(r"[^a-zA-Z0-9]+", "", str(char_name or "").lower())
    return token or "char"


def short_quark_suffix(quark_id: str) -> str:
    """Extract compact collision-avoidance suffix from a Fightcade quarkid (e.g. '1795443936010-2584' -> '2584')."""
    qid = str(quark_id or "").strip()
    if "-" in qid:
        tail = re.sub(r"[^a-zA-Z0-9]", "", qid.rsplit("-", 1)[-1])
        if tail:
            return tail[:8]
    alnum = re.sub(r"[^a-zA-Z0-9]", "", qid)
    return alnum[-6:] if alnum else "0000"


def lookup_replay_metadata(quark_id: str, data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Look up cached Fightcade replay metadata (`p1_name`, `p2_name`, `date`, `ranked`, scores) from `data/*.json`."""
    if not data_dir or not data_dir.exists():
        return {}
    qid = str(quark_id)
    for jf in sorted(data_dir.glob("*.json")):
        try:
            doc = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(doc, dict):
            continue
        for s in doc.get("summaries", []):
            if isinstance(s, dict) and str(s.get("quarkid", "")) == qid:
                return dict(s)
        for r in doc.get("raw", []):
            if isinstance(r, dict) and str(r.get("quarkid", "")) == qid:
                players = r.get("players", [])
                p1 = players[0] if len(players) > 0 else {}
                p2 = players[1] if len(players) > 1 else {}
                return {
                    "quarkid": qid,
                    "date": r.get("date"),
                    "duration": r.get("duration"),
                    "ranked": r.get("ranked"),
                    "p1_name": p1.get("name"),
                    "p1_rank": p1.get("rank"),
                    "p1_score": p1.get("score"),
                    "p2_name": p2.get("name"),
                    "p2_rank": p2.get("rank"),
                    "p2_score": p2.get("score"),
                }
    return {}


def extract_match_date(quark_id: str, meta: dict[str, Any] | None = None) -> str:
    """Derive `YYYY-MM-DD` from manifest `date` (epoch ms) or the leading timestamp in `quark_id`."""
    epoch_ms: int | None = None
    if meta and meta.get("date"):
        try:
            epoch_ms = int(meta["date"])
        except (TypeError, ValueError):
            epoch_ms = None
    if epoch_ms is None:
        m = re.match(r"^(\d{12,14})(?:-|$)", str(quark_id or ""))
        if m:
            epoch_ms = int(m.group(1))
    if epoch_ms is not None and epoch_ms > 1_000_000_000_000:
        dt = datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def _resolve_player_names(
    quark_id: str,
    perspective: int,
    data_dir: Path,
    existing_self: str | None = None,
    existing_opp: str | None = None,
) -> tuple[str, str, str, dict[str, Any]]:
    meta = lookup_replay_metadata(quark_id, data_dir=data_dir)
    match_date = extract_match_date(quark_id, meta)
    if perspective == 1:
        s_name = existing_self or meta.get("p1_name") or "P1"
        o_name = existing_opp or meta.get("p2_name") or "P2"
    else:
        s_name = existing_self or meta.get("p2_name") or "P2"
        o_name = existing_opp or meta.get("p1_name") or "P1"
    return str(s_name), str(o_name), match_date, meta


def enrich_coaching_metadata(report: dict[str, Any], data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Attach `report_type`, `self_player_name`, `opp_player_name`, and `match_date` in place."""
    qid = str(report.get("target_quark_id", ""))
    persp = int(report.get("perspective", 1))
    s_name, o_name, mdate, meta = _resolve_player_names(
        qid,
        persp,
        data_dir,
        existing_self=report.get("self_player_name"),
        existing_opp=report.get("opp_player_name"),
    )
    report["report_type"] = "full_set"
    report["self_player_name"] = s_name
    report["opp_player_name"] = o_name
    report["match_date"] = report.get("match_date") or mdate
    if meta.get("ranked") is not None:
        report["ranked"] = meta.get("ranked")
    return report


def enrich_sequence_search_metadata(result: dict[str, Any], data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, Any]:
    """Attach `report_type`, `self_player_name`, `opp_player_name`, and `match_date` in place."""
    q = result.get("query", {})
    qid = str(q.get("quark_id", ""))
    persp = int(q.get("perspective", 1))
    s_name, o_name, mdate, _ = _resolve_player_names(
        qid,
        persp,
        data_dir,
        existing_self=q.get("self_player_name") or result.get("self_player_name"),
        existing_opp=q.get("opp_player_name") or result.get("opp_player_name"),
    )
    result["report_type"] = "sequence_search"
    result["self_player_name"] = s_name
    result["opp_player_name"] = o_name
    result["match_date"] = result.get("match_date") or mdate
    if isinstance(q, dict):
        q["self_player_name"] = s_name
        q["opp_player_name"] = o_name
        q["match_date"] = result["match_date"]
    return result


def build_coaching_report_stem(report: dict[str, Any], data_dir: Path = DEFAULT_DATA_DIR) -> str:
    """Build `{YYYY-MM-DD}_{self_user}_{self_char}_vs_{opp_user}_{opp_char}_{short_qid}`."""
    enrich_coaching_metadata(report, data_dir=data_dir)
    qid = str(report.get("target_quark_id", ""))
    date_s = str(report["match_date"])
    s_user = slugify(report["self_player_name"], max_len=18, sep="")
    o_user = slugify(report["opp_player_name"], max_len=18, sep="")
    s_char = char_slug(report.get("self_char_name", "p1"))
    o_char = char_slug(report.get("opp_char_name", "p2"))
    short_id = short_quark_suffix(qid)
    return f"{date_s}_{s_user}_{s_char}_vs_{o_user}_{o_char}_{short_id}"


def build_sequence_search_stem(result: dict[str, Any], data_dir: Path = DEFAULT_DATA_DIR) -> str:
    """Build `{YYYY-MM-DD}_{self_user}_{self_char}_vs_{opp_user}_{opp_char}_{short_qid}_r{round}_{topic}_gf{start}-{end}`."""
    enrich_sequence_search_metadata(result, data_dir=data_dir)
    q = result["query"]
    qid = str(q.get("quark_id", ""))
    date_s = str(result["match_date"])
    s_user = slugify(result["self_player_name"], max_len=18, sep="")
    o_user = slugify(result["opp_player_name"], max_len=18, sep="")
    s_char = char_slug(q.get("self_char_name", "p1"))
    o_char = char_slug(q.get("opp_char_name", "p2"))
    short_id = short_quark_suffix(qid)
    rnd = int(q.get("round_id", 0))

    raw_note = str(result.get("note") or "").strip()
    if raw_note:
        topic = slugify(raw_note, max_len=28, sep="-")
    else:
        label = str(q.get("label", ""))
        interaction = label.split("—", 1)[-1].strip() if "—" in label else str(q.get("regime", "sequence"))
        interaction = re.sub(r"\(\d+px\)", "", interaction).strip()
        topic = slugify(interaction, max_len=28, sep="-")

    start_gf = int(q.get("start_gf", 0))
    end_gf = int(q.get("end_gf", 0))
    return f"{date_s}_{s_user}_{s_char}_vs_{o_user}_{o_char}_{short_id}_r{rnd}_{topic}_gf{start_gf}-{end_gf}"


def build_habit_rollup_stem(rollup: dict[str, Any]) -> str:
    """Build `{YYYY-MM-DD}_{username}_{primary_char}_habit_rollup_{N}sets`."""
    date_s = str(rollup.get("match_date") or datetime.now().strftime("%Y-%m-%d"))
    uname = slugify(str(rollup.get("username") or rollup.get("self_player_name") or "player"), max_len=18, sep="")
    pchar = char_slug(str(rollup.get("primary_char_name") or "all"))
    n_sets = int(rollup.get("num_sets", 0))
    return f"{date_s}_{uname}_{pchar}_habit_rollup_{n_sets}sets"


def build_verified_habits_stem(rollup: dict[str, Any]) -> str:
    """Build `{YYYY-MM-DD}_{username}_{primary_char}_verified_habits_{N}sets`."""
    date_s = str(rollup.get("match_date") or datetime.now().strftime("%Y-%m-%d"))
    uname = slugify(str(rollup.get("username") or rollup.get("self_player_name") or "player"), max_len=18, sep="")
    pchar = char_slug(str(rollup.get("primary_char_name") or "all"))
    n_sets = int(rollup.get("num_sets", 0))
    return f"{date_s}_{uname}_{pchar}_verified_habits_{n_sets}sets"


def build_reports_index_html(reports_dir: Path) -> Path:
    """Generate/update `reports_dir/index.html` listing all Full-Set and Sequence-Search reports."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []

    for jp in sorted(reports_dir.glob("*.json")):
        if jp.name.startswith("gemini_") and jp.name.endswith("_cache.json"):
            continue
        hp = jp.with_suffix(".html")
        if not hp.exists():
            continue
        try:
            doc = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(doc, dict):
            continue

        is_hybrid = doc.get("report_type") == "hybrid_habit_rollup"
        is_rollup = (doc.get("report_type") == "habit_rollup") or ("habits" in doc and "sets" in doc)
        is_seq = (doc.get("report_type") == "sequence_search") or ("query" in doc and "matches" in doc)
        if is_hybrid:
            date_s = str(doc.get("match_date") or datetime.now().strftime("%Y-%m-%d"))
            uname = str(doc.get("username") or doc.get("self_player_name") or "Player")
            pchar = str(doc.get("primary_char_name") or "")
            n_sets = int(doc.get("num_sets", 0))
            n_rnds = int(doc.get("total_rounds", 0))
            v_clips = int(doc.get("visually_verified_clips", 0))
            habits = doc.get("habits", [])
            top_h = f"#1: {habits[0]['label']} (-{habits[0]['total_hp_lost']} HP)" if habits else "No verified habits"
            entries.append(
                {
                    "type": "hybrid_habit_rollup",
                    "type_label": "Verified Habits",
                    "date": date_s,
                    "href": hp.name,
                    "matchup": f"{uname} ({pchar}) — {n_sets}-Set Verified Habits",
                    "summary": f"{n_sets} sets · {n_rnds} rounds · {v_clips} visually verified clips",
                    "metric": top_h,
                    "quark_id": f"verified-{uname}-{n_sets}",
                    "mtime": hp.stat().st_mtime,
                }
            )
        elif is_rollup:
            date_s = str(doc.get("match_date") or datetime.now().strftime("%Y-%m-%d"))
            uname = str(doc.get("username") or doc.get("self_player_name") or "Player")
            pchar = str(doc.get("primary_char_name") or "")
            n_sets = int(doc.get("num_sets", 0))
            n_rnds = int(doc.get("total_rounds", 0))
            habits = doc.get("habits", [])
            top_h = f"Top leak: {habits[0]['interaction']} ({habits[0]['total_regret']:.2f})" if habits else "No recurring leaks"
            entries.append(
                {
                    "type": "habit_rollup",
                    "type_label": "Habit Rollup",
                    "date": date_s,
                    "href": hp.name,
                    "matchup": f"{uname} ({pchar}) — {n_sets}-Set Habit Rollup",
                    "summary": f"{n_sets} sets · {n_rnds} rounds · {doc.get('total_leaks', 0)} leaks",
                    "metric": top_h,
                    "quark_id": f"rollup-{uname}-{n_sets}",
                    "mtime": hp.stat().st_mtime,
                }
            )
        elif is_seq:
            q = doc.get("query", {})
            qid = str(q.get("quark_id", ""))
            date_s = str(doc.get("match_date") or extract_match_date(qid))
            s_user = str(doc.get("self_player_name") or q.get("self_player_name") or f"P{q.get('perspective', 1)}")
            o_user = str(doc.get("opp_player_name") or q.get("opp_player_name") or ("P2" if q.get("perspective", 1) == 1 else "P1"))
            s_char = str(q.get("self_char_name", ""))
            o_char = str(q.get("opp_char_name", ""))
            note = str(doc.get("note") or "")
            summary = f"R{q.get('round_id', '?')} · gf{q.get('start_gf', '?')}..{q.get('end_gf', '?')} ({q.get('duration_s', '?')}s)"
            if note:
                summary = f"{note} — {summary}"
            metric = f"{len(doc.get('matches', []))} matches ({doc.get('retrieval_tier', '')})"
            entries.append(
                {
                    "type": "sequence_search",
                    "type_label": "Sequence Search",
                    "date": date_s,
                    "href": hp.name,
                    "matchup": f"{s_user} ({s_char}) vs {o_user} ({o_char})",
                    "summary": summary,
                    "metric": metric,
                    "quark_id": qid,
                    "mtime": hp.stat().st_mtime,
                }
            )
        elif "moments" in doc:
            qid = str(doc.get("target_quark_id", ""))
            persp = int(doc.get("perspective", 1))
            date_s = str(doc.get("match_date") or extract_match_date(qid))
            s_user = str(doc.get("self_player_name") or f"P{persp}")
            o_user = str(doc.get("opp_player_name") or ("P2" if persp == 1 else "P1"))
            s_char = str(doc.get("self_char_name", ""))
            o_char = str(doc.get("opp_char_name", ""))
            moments = doc.get("moments", [])
            if doc.get("pipeline") == "hybrid_v2":
                top_reg = (
                    f"Verified · Top -{moments[0]['hp_lost']} HP ({moments[0].get('category_label', '')})"
                    if moments
                    else "No damage episodes"
                )
                summary_txt = f"{len(moments)} visually verified moments · {doc.get('total_damage_episodes', 0)} Hit-#1 episodes"
            else:
                top_reg = f"Top regret {moments[0]['regret']:+.2f}" if moments else "No positive regret"
                summary_txt = f"{len(moments)} high-regret cards · {doc.get('total_decision_points', 0)} decision points"
            entries.append(
                {
                    "type": "full_set",
                    "type_label": "Full Set",
                    "date": date_s,
                    "href": hp.name,
                    "matchup": f"{s_user} ({s_char}) vs {o_user} ({o_char})",
                    "summary": summary_txt,
                    "metric": top_reg,
                    "quark_id": qid,
                    "mtime": hp.stat().st_mtime,
                }
            )

    # Sort newest date first, then hybrid_habit_rollup > habit_rollup > full_set > sequence_search
    type_prio = {"hybrid_habit_rollup": 3, "habit_rollup": 2, "full_set": 1, "sequence_search": 0}
    entries.sort(key=lambda e: (e["date"], type_prio.get(e["type"], 0), e["mtime"]), reverse=True)

    rows_html: list[str] = []
    for e in entries:
        if e["type"] == "hybrid_habit_rollup":
            badge_cls = "badge-verified"
        elif e["type"] == "habit_rollup":
            badge_cls = "badge-rollup"
        elif e["type"] == "full_set":
            badge_cls = "badge-set"
        else:
            badge_cls = "badge-seq"
        search_blob = f"{e['date']} {e['type_label']} {e['matchup']} {e['summary']} {e['quark_id']}".lower()
        rows_html.append(
            f"""
            <tr data-type="{html.escape(e['type'])}" data-search="{html.escape(search_blob)}">
              <td class="mono">{html.escape(e['date'])}</td>
              <td><span class="badge {badge_cls}">{html.escape(e['type_label'])}</span></td>
              <td><a class="rep-link" href="{html.escape(e['href'])}">{html.escape(e['matchup'])}</a><div class="qid">{html.escape(e['href'])}</div></td>
              <td>{html.escape(e['summary'])}</td>
              <td class="metric">{html.escape(e['metric'])}</td>
            </tr>
            """
        )

    out_index = reports_dir / "index.html"
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Fightcade Analysis — Reports Hub</title>
<script>
  (function() {{
    try {{
      var t = localStorage.getItem("fc_theme");
      if (!t && window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) t = "light";
      if (t === "light") document.documentElement.setAttribute("data-theme", "light");
    }} catch (e) {{}}
  }})();
</script>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0b1120; --surface: #111827; --card: #172033; --border: #263248;
    --text: #e2e8f0; --muted: #94a3b8; --accent: #38bdf8; --ok: #22c55e;
    --btn-bg: #1e293b; --btn-border: #334155; --btn-hover-fg: #ffffff;
    --mono-fg: #cbd5e1; --link-fg: #7dd3fc; --link-hover: #bae6fd;
    --metric-fg: #4ade80; --q-title: #f8fafc; --q-item-bg: rgba(15,23,42,0.82);
    --badge-set-bg: rgba(56, 189, 248, 0.18); --badge-set-fg: #7dd3fc;
    --badge-seq-bg: rgba(168, 85, 247, 0.22); --badge-seq-fg: #d8b4fe;
    --badge-rollup-bg: rgba(245, 158, 11, 0.22); --badge-rollup-fg: #fbbf24;
    --badge-verified-bg: rgba(16, 185, 129, 0.24); --badge-verified-fg: #6ee7b7;
  }}
  html[data-theme="light"] {{
    color-scheme: light;
    --bg: #f1f5f9; --surface: #f8fafc; --card: #ffffff; --border: #cbd5e1;
    --text: #0f172a; --muted: #475569; --accent: #0284c7; --ok: #059669;
    --btn-bg: #ffffff; --btn-border: #cbd5e1; --btn-hover-fg: #0c4a6e;
    --mono-fg: #334155; --link-fg: #0369a1; --link-hover: #0284c7;
    --metric-fg: #047857; --q-title: #0f172a; --q-item-bg: #ffffff;
    --badge-set-bg: rgba(14, 165, 233, 0.14); --badge-set-fg: #0369a1;
    --badge-seq-bg: rgba(168, 85, 247, 0.14); --badge-seq-fg: #7e22ce;
    --badge-rollup-bg: rgba(245, 158, 11, 0.16); --badge-rollup-fg: #b45309;
    --badge-verified-bg: rgba(16, 185, 129, 0.16); --badge-verified-fg: #047857;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); padding: 28px; }}
  header {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 14px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 14px; }}
  h1 {{ margin: 0; font-size: 1.4rem; }}
  .sub {{ color: var(--muted); font-size: 0.88rem; margin-top: 4px; }}
  .controls {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 16px; }}
  .btn {{ background: var(--btn-bg); color: var(--text); border: 1px solid var(--btn-border); border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 0.82rem; }}
  .btn.active, .btn:hover {{ border-color: var(--accent); color: var(--btn-hover-fg); background: rgba(14, 165, 233, 0.14); }}
  input.filter-box {{ background: var(--card); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 7px 12px; font-size: 0.84rem; min-width: 260px; }}
  table {{ width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; font-size: 0.86rem; }}
  th, td {{ padding: 11px 14px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }}
  th {{ color: var(--muted); font-weight: 600; background: var(--surface); }}
  tr:hover td {{ background: rgba(14, 165, 233, 0.06); }}
  .mono {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; white-space: nowrap; color: var(--mono-fg); }}
  .rep-link {{ color: var(--link-fg); font-weight: 600; text-decoration: none; font-size: 0.94rem; }}
  .rep-link:hover {{ text-decoration: underline; color: var(--link-hover); }}
  .qid {{ font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 0.72rem; color: var(--muted); margin-top: 2px; }}
  .metric {{ color: var(--metric-fg); font-weight: 600; white-space: nowrap; }}
  .badge {{ font-size: 0.72rem; padding: 3px 8px; border-radius: 999px; font-weight: 600; white-space: nowrap; }}
  .badge-set {{ background: var(--badge-set-bg); color: var(--badge-set-fg); }}
  .badge-seq {{ background: var(--badge-seq-bg); color: var(--badge-seq-fg); }}
  .badge-rollup {{ background: var(--badge-rollup-bg); color: var(--badge-rollup-fg); }}
  .badge-verified {{ background: var(--badge-verified-bg); color: var(--badge-verified-fg); }}
  .queue-panel {{ background: linear-gradient(135deg, rgba(14,165,233,0.09), rgba(16,185,129,0.08)); border: 1px solid rgba(14,165,233,0.35); border-radius: 12px; padding: 16px 18px; margin-bottom: 22px; }}
  .queue-head {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; }}
  .queue-title {{ font-size: 1.02rem; font-weight: 700; color: var(--q-title); display: flex; align-items: center; gap: 8px; }}
  .q-dot {{ width: 9px; height: 9px; border-radius: 50%; background: #4ade80; display: inline-block; }}
  .q-dot.busy {{ background: #fbbf24; }}
  .q-dot.err {{ background: #f87171; }}
  .q-status {{ font-size: 0.8rem; color: var(--muted); margin-bottom: 10px; }}
  .q-actions {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
  .q-input {{ background: var(--card); color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 5px 9px; font-size: 0.8rem; width: 130px; }}
  .btn-primary {{ background: linear-gradient(135deg, #0ea5e9, #10b981); color: #04131c; border: 1px solid rgba(56,189,248,0.6); border-radius: 6px; padding: 6px 12px; cursor: pointer; font-size: 0.8rem; font-weight: 700; }}
  .btn-primary:hover {{ filter: brightness(1.06); }}
  .btn-primary:disabled {{ opacity: 0.55; cursor: not-allowed; }}
  .q-list {{ display: grid; gap: 8px; }}
  .q-item {{ background: var(--q-item-bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; }}
  .q-item.done {{ opacity: 0.55; border-style: dashed; }}
  .q-meta {{ font-size: 0.84rem; }}
  .q-sub {{ font-size: 0.76rem; color: var(--muted); margin-top: 2px; }}
  .q-btns {{ display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }}
  .q-empty {{ font-size: 0.84rem; color: var(--muted); padding: 6px 2px; }}
  #hub-toast {{ position: fixed; right: 18px; bottom: 18px; max-width: 440px; z-index: 9999; background: #0f172a; color: #e2e8f0; border: 1px solid #4ade80; border-radius: 8px; padding: 10px 14px; font-size: 0.82rem; box-shadow: 0 10px 28px rgba(0,0,0,0.45); display: none; }}
</style>
</head>
<body>
  <header>
    <div>
      <h1>Fightcade Analysis — Reports Hub</h1>
      <div class="sub">{len(entries)} generated report(s) in <code>data/reports/</code></div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
      <button class="btn" id="btn-hub-theme" onclick="toggleHubTheme()" title="Switch light / dark theme">&#9728;&#65039;</button>
      <a class="btn" href="/review#notes" style="display:inline-block;text-decoration:none;font-weight:600;">&#128221; All Notes</a>
      <a class="btn-primary" href="/review" style="display:inline-block;text-decoration:none;">&#9654; Replay Review</a>
    </div>
  </header>

  <section class="queue-panel" id="queue-panel">
    <div class="queue-head">
      <div class="queue-title"><span class="q-dot" id="q-dot"></span> In-Game Select Takeover Queue <span class="badge badge-verified" id="q-count">0 pending</span></div>
      <div class="q-actions">
        <button class="btn-primary" id="btn-drill-next" onclick="drillNextQueued()" disabled>&#9654; Drill Next</button>
        <input class="q-input" id="q-username" type="text" placeholder="Fightcade username" value="" />
        <button class="btn" id="btn-scan-latest" onclick="scanLatestMatch()">Scan Latest Match</button>
        <button class="btn" onclick="clearDoneQueue()">Clear Done</button>
      </div>
    </div>
    <div class="q-status" id="q-status">Tap <strong>Select (Coin)</strong> during a live Fightcade match to mark tricky sequences — they auto-queue here (-6s window) right after the match ends.</div>
    <div class="q-list" id="q-list"><div class="q-empty">Loading queue...</div></div>
  </section>

  <div class="controls">
    <button class="btn active" data-filter="all" onclick="setType('all', this)">All ({len(entries)})</button>
    <button class="btn" data-filter="hybrid_habit_rollup" onclick="setType('hybrid_habit_rollup', this)">Verified Habits ({sum(1 for e in entries if e['type'] == 'hybrid_habit_rollup')})</button>
    <button class="btn" data-filter="habit_rollup" onclick="setType('habit_rollup', this)">Habit Rollup ({sum(1 for e in entries if e['type'] == 'habit_rollup')})</button>
    <button class="btn" data-filter="full_set" onclick="setType('full_set', this)">Full Set ({sum(1 for e in entries if e['type'] == 'full_set')})</button>
    <button class="btn" data-filter="sequence_search" onclick="setType('sequence_search', this)">Sequence Search ({sum(1 for e in entries if e['type'] == 'sequence_search')})</button>
    <input class="filter-box" type="search" id="q" placeholder="Filter by player, character, note, date..." oninput="applyFilter()" />
  </div>

  <table>
    <thead>
      <tr><th>Date</th><th>Type</th><th>Matchup &amp; Report</th><th>Details</th><th>Highlight</th></tr>
    </thead>
    <tbody id="rows">
      {''.join(rows_html) if rows_html else '<tr><td colspan="5" class="sub">No reports generated yet.</td></tr>'}
    </tbody>
  </table>

<script>
let activeType = 'all';
function setType(t, btn) {{
  activeType = t;
  document.querySelectorAll('.btn[data-filter]').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  applyFilter();
}}
function applyFilter() {{
  const q = (document.getElementById('q').value || '').trim().toLowerCase();
  document.querySelectorAll('#rows tr[data-type]').forEach(tr => {{
    const okType = activeType === 'all' || tr.dataset.type === activeType;
    const okText = !q || (tr.dataset.search || '').includes(q);
    tr.style.display = (okType && okText) ? '' : 'none';
  }});
}}

let _queueItems = [];
let _hubToastTimer = null;
function showHubToast(msg, isErr) {{
  let el = document.getElementById('hub-toast');
  if (!el) {{
    el = document.createElement('div');
    el.id = 'hub-toast';
    document.body.appendChild(el);
  }}
  el.style.borderColor = isErr ? '#f87171' : '#4ade80';
  el.textContent = msg;
  el.style.display = 'block';
  if (_hubToastTimer) clearTimeout(_hubToastTimer);
  _hubToastTimer = setTimeout(() => {{ el.style.display = 'none'; }}, 4200);
}}
function _esc(s) {{
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}
function renderQueue(data) {{
  const items = Array.isArray(data.items) ? data.items : [];
  _queueItems = items;
  const pending = items.filter(it => it.status === 'pending');
  const dot = document.getElementById('q-dot');
  const st = document.getElementById('q-status');
  const cnt = document.getElementById('q-count');
  const btnNext = document.getElementById('btn-drill-next');
  const list = document.getElementById('q-list');
  const w = data.watcher || {{}};

  cnt.textContent = pending.length + ' pending';
  btnNext.disabled = pending.length === 0;
  dot.className = 'q-dot' + ((w.phase === 'scanning' || w.phase === 'waiting_replay' || w.phase === 'in_match') ? ' busy' : (w.phase === 'error' ? ' err' : ''));
  if (w.message) st.textContent = w.message;

  if (!items.length) {{
    list.innerHTML = '<div class="q-empty">No queued sequences yet. Tap <strong>Select (Coin)</strong> during a Fightcade match (or click <strong>Scan Latest Match</strong>) to queue -6s Replay Takeovers.</div>';
    return;
  }}
  // Show pending first (newest match first, chronological inside match), then done
  const ordered = pending.concat(items.filter(it => it.status !== 'pending'));
  list.innerHTML = ordered.map((it, i) => {{
    const isDone = it.status !== 'pending';
    const isMob = it.source === 'mobile_note';
    const srcBadge = isMob ? '<span class="badge badge-seq" style="margin-right:6px;">Review</span>' : '';
    const lb = it.lookback_s ? ('-' + Number(it.lookback_s).toFixed(1) + 's window') : (isMob ? 'custom clip' : '-6.0s window');
    return `
      <div class="q-item ${{isDone ? 'done' : ''}}">
        <div class="q-meta">
          <div>${{srcBadge}}<strong>${{_esc(it.self_char_name)}} vs ${{_esc(it.opp_char_name)}}</strong> &middot; vs ${{_esc(it.opp_player_name)}} &middot; <span class="mono">R${{it.round_id || '?'}}</span></div>
          <div class="q-sub">${{_esc(it.note || 'Select mark')}} &middot; ${{lb}} (<code>gf${{it.start_gf}}&rarr;${{it.press_gf}}</code>) &middot; ${{_esc(it.quark_id)}}</div>
        </div>
        <div class="q-btns">
          <button class="btn-primary" onclick="launchQueuedItem('${{_esc(it.id)}}')">&#9654; Launch Takeover</button>
          ${{!isDone ? `<button class="btn" onclick="markDoneAndNext('${{_esc(it.id)}}')">&#10003; Done &amp; Next</button>` : `<button class="btn" onclick="setQueueStatus('${{_esc(it.id)}}','pending')">Re-queue</button>`}}
          <button class="btn" title="Dismiss" onclick="setQueueStatus('${{_esc(it.id)}}','dismissed')">&#10005;</button>
        </div>
      </div>
    `;
  }}).join('');
}}
async function fetchQueue() {{
  if (window.location.protocol === 'file:') {{
    document.getElementById('q-status').textContent = 'Open via http://127.0.0.1:8765/ (run .\\\\scripts\\\\serve_reports.ps1) to use the live Takeover Queue.';
    document.getElementById('q-list').innerHTML = '';
    return;
  }}
  try {{
    const r = await fetch('/api/queue');
    if (!r.ok) return;
    const data = await r.json();
    if (data && data.ok) renderQueue(data);
  }} catch (e) {{}}
}}
async function launchQueuedItem(selId) {{
  try {{
    const r = await fetch('/api/takeover', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ selection_id: selId, mode: 'takeover' }})
    }});
    const data = await r.json().catch(() => ({{}}));
    if (!r.ok || !data.ok) throw new Error(data.error || ('HTTP ' + r.status));
    showHubToast(data.warning || ('Takeover launched (P' + data.player + ') — Select Retry · Alt+2 Watch/Takeover · Alt+3 Loop'), Boolean(data.warning));
  }} catch (err) {{
    showHubToast('Launch failed: ' + (err.message || err), true);
  }}
}}
async function drillNextQueued() {{
  const next = _queueItems.find(it => it.status === 'pending');
  if (next) await launchQueuedItem(next.id);
}}
async function setQueueStatus(selId, status) {{
  await fetch('/api/queue/status', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ id: selId, status }})
  }}).catch(() => {{}});
  await fetchQueue();
}}
async function markDoneAndNext(selId) {{
  await setQueueStatus(selId, 'done');
  const next = _queueItems.find(it => it.status === 'pending');
  if (next) await launchQueuedItem(next.id);
}}
async function clearDoneQueue() {{
  await fetch('/api/queue/status', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ action: 'clear_done' }})
  }}).catch(() => {{}});
  await fetchQueue();
}}
async function scanLatestMatch() {{
  const inp = document.getElementById('q-username');
  const uname = (inp.value || '').trim();
  if (!uname) {{ showHubToast('Enter your Fightcade username first.', true); return; }}
  try {{ localStorage.setItem('fc_queue_username', uname); }} catch (e) {{}}
  const btn = document.getElementById('btn-scan-latest');
  btn.disabled = true;
  btn.textContent = 'Scanning...';
  try {{
    const r = await fetch('/api/queue/scan', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ username: uname, lookback_seconds: 6.0 }})
    }});
    const data = await r.json().catch(() => ({{}}));
    if (!r.ok || !data.ok) throw new Error(data.error || ('HTTP ' + r.status));
    showHubToast('Scanning latest match for ' + uname + ' (muted in background)...', false);
    await fetchQueue();
  }} catch (err) {{
    showHubToast('Scan failed: ' + (err.message || err), true);
  }} finally {{
    setTimeout(() => {{ btn.disabled = false; btn.textContent = 'Scan Latest Match'; }}, 1500);
  }}
}}
function syncHubThemeBtn() {{
  const light = document.documentElement.getAttribute('data-theme') === 'light';
  const b = document.getElementById('btn-hub-theme');
  if (b) {{
    b.innerHTML = light ? '&#127769;' : '&#9728;&#65039;';
    b.title = light ? 'Switch to dark mode' : 'Switch to light mode';
  }}
}}
function toggleHubTheme() {{
  const next = document.documentElement.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  if (next === 'light') document.documentElement.setAttribute('data-theme', 'light');
  else document.documentElement.removeAttribute('data-theme');
  try {{ localStorage.setItem('fc_theme', next); }} catch (e) {{}}
  syncHubThemeBtn();
}}
(function initQueue() {{
  syncHubThemeBtn();
  try {{
    const saved = localStorage.getItem('fc_queue_username');
    if (saved) document.getElementById('q-username').value = saved;
  }} catch (e) {{}}
  fetchQueue();
  if (window.location.protocol !== 'file:') {{
    setInterval(fetchQueue, 4000);
  }}
}})();
</script>
</body>
</html>
"""
    out_index.write_text(page, encoding="utf-8")
    return out_index
