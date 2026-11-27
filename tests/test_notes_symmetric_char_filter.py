"""Tests for deprecating + Add ID on Matches and symmetric Char vs Char filtering in Notes Hub."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
INDEX_HTML = WEB_DIR / "index.html"
STATE_JS = WEB_DIR / "js" / "state.js"
MATCHES_JS = WEB_DIR / "js" / "matches.js"
NOTES_JS = WEB_DIR / "js" / "notes.js"


def _run_node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"node failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return json.loads(proc.stdout.strip())


def test_matches_add_id_removed_and_notes_char_dropdowns_neutral():
    html = INDEX_HTML.read_text(encoding="utf-8")
    matches_src = MATCHES_JS.read_text(encoding="utf-8")

    # + Add ID UI & helpers removed from Matches
    for removed in ("+ Add ID", 'id="addPanel"', 'id="manualQuarkInput"', 'id="manualPersp"', "toggleAddPanel", "processManualQuark"):
        assert removed not in html, f"Expected removed Matches Add ID artifact in index.html: {removed}"
        assert removed not in matches_src, f"Expected removed Matches Add ID helper in matches.js: {removed}"

    # Matches character filter stays scoped to searched user
    assert 'id="selfCharFilter"' in html
    assert 'id="oppCharFilter"' in html

    # Notes Hub character dropdowns use neutral "Any Char"
    notes_section = html[html.index('id="viewNotes"'):html.index('id="viewInsights"')]
    assert '<option value="">Any Char</option>' in notes_section
    assert "You: Any" not in notes_section
    assert "Opp: Any" not in notes_section


def test_notes_symmetric_char_filter_union_and_card_toggle():
    script = f"""
const fs = require("fs");
const vm = require("vm");

const elements = Object.create(null);
function makeEl(id) {{
  if (!elements[id]) {{
    elements[id] = {{
      id,
      innerHTML: "",
      textContent: "",
      value: "",
      dataset: {{}},
      classList: {{
        _set: new Set(["notesActiveTagsRow", "notesSummaryRow", "btnClearNotesFilters"].includes(id) ? ["hidden"] : []),
        add(...cs) {{ cs.forEach(c => this._set.add(c)); }},
        remove(...cs) {{ cs.forEach(c => this._set.delete(c)); }},
        toggle(c, force) {{
          if (force === undefined) {{
            if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
          }} else if (force) {{
            this._set.add(c);
          }} else {{
            this._set.delete(c);
          }}
        }},
        contains(c) {{ return this._set.has(c); }},
      }},
    }};
  }}
  return elements[id];
}}

const sandbox = {{
  console,
  window: {{
    innerWidth: 1024,
    location: {{ origin: "http://localhost:8765", search: "", hash: "#notes", pathname: "/" }},
    matchMedia: () => ({{ matches: false, addEventListener: () => {{}} }}),
  }},
  document: {{
    getElementById: (id) => makeEl(id),
    querySelectorAll: () => [],
    addEventListener: () => {{}},
  }},
  localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
  Intl,
  Date,
  Set,
  Map,
  Math,
  Number,
  String,
  Array,
  Object,
  Boolean,
  JSON,
  URLSearchParams,
  encodeURIComponent,
  clearTimeout: () => {{}},
  setTimeout: () => 0,
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {{}},
  _elements: elements,
}};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(NOTES_JS))}, "utf8"), sandbox);

const out = vm.runInContext(`
  state.username = "blankasballs";
  state.hubNotes = [
    {{ id: "n_ak_ch", entry_type: "moment", source: "annotation", kind: "note", quark_id: "q1", self_char_name: "Akuma", opp_char_name: "Chun-Li", self_player_name: "p1", opp_player_name: "p2", note: "A", date_str: "2026-11-26" }},
    {{ id: "n_ch_ak", entry_type: "moment", source: "annotation", kind: "note", quark_id: "q2", self_char_name: "Chun-Li", opp_char_name: "Akuma", self_player_name: "proA", opp_player_name: "proB", note: "B", date_str: "2026-11-26" }},
    {{ id: "n_ken_ken", entry_type: "moment", source: "annotation", kind: "note", quark_id: "q3", self_char_name: "Ken", opp_char_name: "Ken", self_player_name: "k1", opp_player_name: "k2", note: "C", date_str: "2026-11-26" }},
    {{ id: "n_ryu_q", entry_type: "moment", source: "annotation", kind: "note", quark_id: "q4", self_char_name: "Ryu", opp_char_name: "Q", self_player_name: "r1", opp_player_name: "q1", note: "D", date_str: "2026-11-26" }},
  ];
  state.hubAvailableChars = {{ self: ["Akuma", "Ken"], opp: ["Chun-Li", "Q"] }};
  state.hubCounts = {{ total: 4, moments: 4, set_notes: 0, by_kind: {{ response: 0 }}, by_tag: [] }};

  renderHubNotesControls();
  const selfOptsHtml = _elements["notesSelfChar"].innerHTML;
  const oppOptsHtml = _elements["notesOppChar"].innerHTML;

  // Case 1: Single char in slot 1 (Q appears only on opp side -> still matched!)
  state.notesSelfChar = "Q";
  state.notesOppChar = "";
  const singleOppSide = getFilteredHubNotes().map(x => x.id);

  // Case 2: Single char in slot 2 (Ryu appears only on self side -> still matched!)
  state.notesSelfChar = "";
  state.notesOppChar = "Ryu";
  const singleSelfSide = getFilteredHubNotes().map(x => x.id);

  // Case 3: 2-char matchup in EITHER order matches both Akuma vs Chun-Li AND Chun-Li vs Akuma
  state.notesSelfChar = "Akuma";
  state.notesOppChar = "Chun-Li";
  const pairForward = getFilteredHubNotes().map(x => x.id);

  state.notesSelfChar = "Chun-Li";
  state.notesOppChar = "Akuma";
  const pairReverse = getFilteredHubNotes().map(x => x.id);

  // Active highlight appears on BOTH sides of Chun-Li vs Akuma card
  const highlightedCardHtml = renderHubNoteCardHtml(state.hubNotes[1]);

  // Case 4: Mirror matchup Ken vs Ken matches ONLY Ken vs Ken
  state.notesSelfChar = "Ken";
  state.notesOppChar = "Ken";
  const mirrorMatch = getFilteredHubNotes().map(x => x.id);

  // Card-click toggle flow:
  // empty -> click Akuma -> [Akuma, ""]
  // click Chun-Li -> [Akuma, Chun-Li]
  // click Ken -> replaces slot 2 -> [Akuma, Ken]
  // click Akuma (in slot 1) -> promotes slot 2 to slot 1 -> [Ken, ""]
  // click Ken (in slot 1) -> ["", ""]
  clearNotesFilters();
  toggleNotesCharFilter("opp", "Akuma");
  const t1 = [state.notesSelfChar, state.notesOppChar];
  toggleNotesCharFilter("self", "Chun-Li");
  const t2 = [state.notesSelfChar, state.notesOppChar];
  toggleNotesCharFilter("opp", "Ken");
  const t3 = [state.notesSelfChar, state.notesOppChar];
  toggleNotesCharFilter("self", "Akuma");
  const t4 = [state.notesSelfChar, state.notesOppChar];
  toggleNotesCharFilter("opp", "Ken");
  const t5 = [state.notesSelfChar, state.notesOppChar];

  ({{
    selfOptsHtml,
    oppOptsHtml,
    singleOppSide,
    singleSelfSide,
    pairForward,
    pairReverse,
    highlightedCardHtml,
    mirrorMatch,
    t1, t2, t3, t4, t5,
  }});
`, sandbox);
console.log(JSON.stringify(out));
"""
    res = _run_node(script)

    # Both dropdowns use neutral "Any Char" and union of all 5 characters (including fallback scan of Ryu!)
    for opts_html in (res["selfOptsHtml"], res["oppOptsHtml"]):
        assert '<option value="">Any Char</option>' in opts_html
        assert "You:" not in opts_html
        assert "Opp:" not in opts_html
        assert ">vs " not in opts_html
        for ch in ("Akuma", "Chun-Li", "Ken", "Q", "Ryu"):
            assert f">{ch}</option>" in opts_html

    # Symmetric matching across all 4 cases
    assert res["singleOppSide"] == ["n_ryu_q"]
    assert res["singleSelfSide"] == ["n_ryu_q"]
    assert res["pairForward"] == ["n_ak_ch", "n_ch_ak"]
    assert res["pairReverse"] == ["n_ak_ch", "n_ch_ak"]
    assert res["mirrorMatch"] == ["n_ken_ken"]

    # Both sides highlighted as active-f on reverse-order card
    assert res["highlightedCardHtml"].count("match-char-filter active-f") == 2

    # Card-click toggle transitions
    assert res["t1"] == ["Akuma", ""]
    assert res["t2"] == ["Akuma", "Chun-Li"]
    assert res["t3"] == ["Akuma", "Ken"]
    assert res["t4"] == ["Ken", ""]
    assert res["t5"] == ["", ""]
