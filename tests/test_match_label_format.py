"""Contract & Node execution tests for unified Replay Studio match labeling helpers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "fightcade_analysis" / "web"
STATE_JS = WEB_DIR / "js" / "state.js"


def _run_node_eval(js_body: str) -> dict:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not found in PATH")
    driver = r"""
const fs = require("fs");
const vm = require("vm");

const stateSrc = fs.readFileSync(process.env.STATE_JS_PATH, "utf8");
const sandbox = {
  console,
  URLSearchParams,
  Intl,
  Date,
  Math,
  Number,
  String,
  Array,
  Set,
  Map,
  RegExp,
  JSON,
  Boolean,
  encodeURIComponent,
  decodeURIComponent,
  setTimeout,
  clearTimeout,
  window: { __FC_BOOTSTRAP__: {}, location: { origin: "http://127.0.0.1:8765", pathname: "/", search: "", hash: "" } },
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  document: {
    documentElement: { getAttribute: () => null, setAttribute: () => {}, removeAttribute: () => {} },
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
  },
};
vm.createContext(sandbox);
vm.runInContext(stateSrc, sandbox, { filename: "state.js" });
const fn = vm.runInContext("(function() {\n" + process.env.TEST_JS + "\n})", sandbox);
const out = fn();
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        [node_bin, "-e", driver],
        env={**os.environ, "STATE_JS_PATH": str(STATE_JS), "TEST_JS": js_body},
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert proc.returncode == 0, f"Node harness failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    return json.loads(proc.stdout)


def test_match_label_helpers_all_edge_cases():
    res = _run_node_eval(
        r"""
        const named = {
          self_player_name: "player1",
          self_char_name: "Akuma",
          opp_player_name: "player2",
          opp_rank_letter: "B",
          opp_char_name: "Ken",
        };
        const multi = {
          self_player_name: "player1",
          self_chars: ["Akuma", "Dudley", "Akuma"],
          opp_player_name: "player2",
          opp_rank_letter: "A",
          opp_chars: ["Ken", "Chun-Li"],
        };
        const unproc = {
          self_player_name: "player1",
          opp_player_name: "player2",
          opp_rank_letter: "B",
        };
        const oneSide = {
          self_player_name: "player1",
          self_char_name: "Akuma",
          opp_player_name: "P2",
          opp_char_name: "Ken",
        };
        const noNames = {
          self_player_name: "P1",
          opp_player_name: "P2",
          self_char_name: "Akuma",
          opp_char_name: "Ken",
        };
        const placeholderFallbackSelf = {
          self_player_name: "P1",
          opp_player_name: "rival",
          self_char_name: "Akuma",
          opp_char_name: "Ryu",
        };
        const emptyItem = { quark_id: "1795-9999" };
        const xssItem = {
          self_player_name: '<img src=x onerror=alert(1)>',
          self_char_name: "O'Neal",
          opp_player_name: 'bad"opp',
          opp_char_name: "<Ken>",
        };

        return {
          namedText: formatMatchLabelText(named),
          namedTextNoRank: formatMatchLabelText(named, { showRank: false }),
          namedHtml: formatMatchLabelHtml(named),
          multiText: formatMatchLabelText(multi),
          multiFilterHtml: formatMatchLabelHtml(multi, {
            charFilterFn: "toggleCharTagFilter",
            activeSelfChar: "Dudley",
            activeOppChar: "Ken",
            stopPropagation: true,
          }),
          unprocText: formatMatchLabelText(unproc),
          unprocHtml: formatMatchLabelHtml(unproc),
          oneSideText: formatMatchLabelText(oneSide),
          oneSideHtml: formatMatchLabelHtml(oneSide),
          noNamesText: formatMatchLabelText(noNames),
          noNamesHtml: formatMatchLabelHtml(noNames),
          anonText: formatMatchLabelText(noNames, { allowPlaceholders: true }),
          anonHtml: formatMatchLabelHtml(noNames, { allowPlaceholders: true }),
          fallbackSelfText: formatMatchLabelText(placeholderFallbackSelf, { defaultSelfName: "me_user" }),
          emptyText: formatMatchLabelText(emptyItem),
          emptyDefaultText: formatMatchLabelText({}),
          xssHtml: formatMatchLabelHtml(xssItem, { charFilterFn: "toggleCharTagFilter" }),
          badFilterFnHtml: formatMatchLabelHtml(named, { charFilterFn: "alert(1)//" }),
        };
        """
    )

    assert res["namedText"] == "player1 (Akuma) vs player2 [B] (Ken)"
    assert res["namedTextNoRank"] == "player1 (Akuma) vs player2 (Ken)"
    assert res["namedHtml"] == (
        'player1 <span class="match-char self">(Akuma)</span> '
        '<span class="match-vs">vs</span> '
        'player2 <span class="rank rank-b" data-rank="B" data-tip="Fightcade Rank B" aria-label="Fightcade Rank B">B</span> '
        '<span class="match-char opp">(Ken)</span>'
    )

    assert res["multiText"] == "player1 (Akuma/Dudley) vs player2 [A] (Ken/Chun-Li)"
    assert "match-char-filter active-f" in res["multiFilterHtml"]
    assert "event.stopPropagation(); toggleCharTagFilter(&quot;self&quot;, &quot;Dudley&quot;)" in res["multiFilterHtml"]
    assert "event.stopPropagation(); toggleCharTagFilter(&quot;opp&quot;, &quot;Ken&quot;)" in res["multiFilterHtml"]
    assert '<span class="match-char-sep">/</span>' in res["multiFilterHtml"]

    assert res["unprocText"] == "player1 vs player2 [B]"
    assert "()" not in res["unprocHtml"]
    assert "(?)" not in res["unprocHtml"]
    assert res["unprocHtml"] == (
        'player1 <span class="match-vs">vs</span> '
        'player2 <span class="rank rank-b" data-rank="B" data-tip="Fightcade Rank B" aria-label="Fightcade Rank B">B</span>'
    )

    assert res["oneSideText"] == "player1 (Akuma) vs Ken"
    assert res["oneSideHtml"] == (
        'player1 <span class="match-char self">(Akuma)</span> '
        '<span class="match-vs">vs</span> '
        '<span class="match-char opp">Ken</span>'
    )

    assert res["noNamesText"] == "Akuma vs Ken"
    assert res["noNamesHtml"] == (
        '<span class="match-char self">Akuma</span> '
        '<span class="match-vs">vs</span> '
        '<span class="match-char opp">Ken</span>'
    )

    assert res["anonText"] == "P1 (Akuma) vs P2 (Ken)"
    assert res["anonHtml"] == (
        'P1 <span class="match-char self">(Akuma)</span> '
        '<span class="match-vs">vs</span> '
        'P2 <span class="match-char opp">(Ken)</span>'
    )

    assert res["fallbackSelfText"] == "me_user (Akuma) vs rival (Ryu)"
    assert res["emptyText"] == "1795-9999"
    assert res["emptyDefaultText"] == "Match"

    assert "<img" not in res["xssHtml"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in res["xssHtml"]
    assert "toggleCharTagFilter(&quot;self&quot;, &quot;O&#39;Neal&quot;)" in res["xssHtml"]
    assert "alert(1)" not in res["badFilterFnHtml"]
    assert "match-char-filter" not in res["badFilterFnHtml"]


def test_all_web_views_use_canonical_match_label_helpers():
    view_files = [
        "studio.js",
        "home.js",
        "matches.js",
        "notes.js",
        "playlists.js",
        "shared.js",
        "insights.js",
        "report_viewer.js",
    ]
    for fname in view_files:
        text = (WEB_DIR / "js" / fname).read_text(encoding="utf-8")
        assert "formatMatchLabelHtml(" in text or "formatMatchLabelText(" in text, (
            f"{fname} should delegate match labels to formatMatchLabelHtml / formatMatchLabelText"
        )
        # Ensure no accidental double-vs regressions in template literals
        assert not re.search(r"· vs \$\{", text), f"Found double-vs separator pattern in {fname}"


def test_render_rank_badge_html_all_tiers_and_both_players():
    res = _run_node_eval(
        r"""
        const tiers = ["S", "a", "B", "c", "D", "e", "U", "?", "", null, "Z", "<script>alert(1)</script>"];
        const badges = {};
        for (const t of tiers) {
          badges[String(t)] = renderRankBadgeHtml(t);
        }
        const bothHtml = formatMatchLabelHtml({
          self_player_name: "player1",
          self_rank_letter: "A",
          self_char_name: "Akuma",
          opp_player_name: "rival",
          opp_rank_letter: "S",
          opp_char_name: "Chun-Li",
        });
        const unrankedHtml = formatMatchLabelHtml({
          self_player_name: "player1",
          self_rank_letter: "U",
          self_char_name: "Akuma",
          opp_player_name: "rookie",
          opp_rank_letter: "?",
          opp_char_name: "Ryu",
        });
        return { badges, bothHtml, unrankedHtml };
        """
    )

    badges = res["badges"]
    for tier, cls, norm, glyph, tip in [
        ("S", "rank-s", "S", "S", "Fightcade Rank S"),
        ("a", "rank-a", "A", "A", "Fightcade Rank A"),
        ("B", "rank-b", "B", "B", "Fightcade Rank B"),
        ("c", "rank-c", "C", "C", "Fightcade Rank C"),
        ("D", "rank-d", "D", "D", "Fightcade Rank D"),
        ("e", "rank-e", "E", "E", "Fightcade Rank E"),
        ("U", "rank-u", "U", "?", "Fightcade Unranked"),
        ("?", "rank-u", "U", "?", "Fightcade Unranked"),
    ]:
        html = badges[tier]
        assert f'class="rank {cls}"' in html
        assert f'data-rank="{norm}"' in html
        assert f'data-tip="{tip}"' in html
        assert f'aria-label="{tip}"' in html
        assert html.endswith(f">{glyph}</span>")

    for bad in ("", "None", "Z", "<script>alert(1)</script>"):
        assert badges.get(bad, "") == ""

    assert 'class="rank rank-a"' in res["bothHtml"]
    assert 'class="rank rank-s"' in res["bothHtml"]
    assert 'class="rank rank-u"' in res["unrankedHtml"]
    assert res["unrankedHtml"].count(">?</span>") == 2


def test_notes_hub_emits_both_self_and_opp_rank_letters(tmp_path: Path):
    from fightcade_analysis.annotations import create_annotation, upsert_set_note
    from fightcade_analysis.notes_hub import list_collated_notes
    from fightcade_analysis.select_queue import upsert_queue_items

    qid = "1795-8888"
    # Seed cached Fightcade replay metadata with P1=Rank A (5) and P2=Rank S (6)
    (tmp_path / "player1_sfiii3nr1_replays.json").write_text(
        json.dumps(
            {
                "raw": [
                    {
                        "quarkid": qid,
                        "date": 1795000000000,
                        "players": [
                            {"name": "player1", "rank": 5, "score": 3},
                            {"name": "boss_opp", "rank": 6, "score": 2},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    # 1. Regular Moment
    ann = create_annotation(
        {
            "quark_id": qid,
            "round_id": 1,
            "perspective": 1,
            "anchor_gf": 1000,
            "kind": "note",
            "tags": ["Anti-Air"],
            "note": "Late DP",
            "queued_drill": False,
        },
        data_dir=tmp_path,
    )
    # 2. Labbed Response
    create_annotation(
        {
            "quark_id": qid,
            "round_id": 1,
            "perspective": 1,
            "anchor_gf": 1000,
            "kind": "response",
            "source": "takeover_response",
            "parent_moment_id": ann["id"],
            "tags": ["Good Play"],
            "note": "Clean crouch fierce",
            "queued_drill": False,
        },
        data_dir=tmp_path,
    )
    # 3. Set Takeaway Note
    upsert_set_note(qid, note="Respect cr.MK spacing", tags=["Footsies"], data_dir=tmp_path)
    # 4. In-Game Select Mark
    upsert_queue_items(
        [
            {
                "id": "sel_rank_test",
                "quark_id": qid,
                "round_id": 1,
                "perspective": 1,
                "start_gf": 800,
                "end_gf": 1050,
                "press_gf": 1000,
                "source": "in_game_select",
                "note": "In-game Select mark #1",
                "status": "pending",
            }
        ],
        data_dir=tmp_path,
    )

    hub_default = list_collated_notes(username="player1", data_dir=tmp_path)
    assert all(n["source"] != "select_mark" for n in hub_default["notes"])

    hub = list_collated_notes(username="player1", data_dir=tmp_path, include_select_marks=True)
    by_source = {n["source"]: n for n in hub["notes"]}
    assert {"annotation", "takeover_response", "set_note", "select_mark"}.issubset(by_source.keys())
    for src in ("annotation", "takeover_response", "set_note", "select_mark"):
        assert by_source[src]["self_rank_letter"] == "A", f"{src} missing self_rank_letter"
        assert by_source[src]["opp_rank_letter"] == "S", f"{src} missing opp_rank_letter"
