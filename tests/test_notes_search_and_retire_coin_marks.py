"""Tests for streamlined 3-row Notes Search Card and retirement of automatic in-game Coin markers."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
INDEX_HTML = WEB_DIR / "index.html"
STATE_JS = WEB_DIR / "js" / "state.js"
NOTES_JS = WEB_DIR / "js" / "notes.js"
LUA_DUMP = ROOT / "lua" / "dump_telemetry.lua"
LUA_TAKEOVER = ROOT / "lua" / "replay_takeover.lua"
README_MD = ROOT / "README.md"


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


def test_notes_search_card_cleanup_and_ui_copy_scrub():
    html = INDEX_HTML.read_text(encoding="utf-8")

    # Removed bloat from Notes Search Card
    for removed in (
        'id="notesGroupBy"',
        'id="btnNotesNeedsNote"',
        'id="notesGoalFilter"',
        'id="notesTagCloud"',
        'id="nCntSelect"',
        'data-nfilter="select_mark"',
        'aria-label="Reload notes"',
        'data-tl="select_mark"',
    ):
        assert removed not in html, f"Expected removed element/attr to be absent: {removed}"

    # Present streamlined 3-row search card controls
    for present in (
        'id="notesSearchInput"',
        'placeholder="Search notes, opponents, characters..."',
        'id="nCntAll"',
        'id="nCntMoment"',
        'id="nCntResponse"',
        'id="nCntSet"',
        'id="notesSelfChar"',
        'id="notesOppChar"',
        'id="notesTagFilterWrap"',
        'id="notesActiveTagsRow"',
        'id="notesSummaryRow"',
        'id="btnClearNotesFilters"',
    ):
        assert present in html, f"Expected streamlined Notes element to exist: {present}"

    # Updated tooltips
    assert 'id="navNotes"' in html and 'data-tip="Search moment notes &amp; playlists"' in html
    assert 'id="subTabNotebook"' in html and 'data-tip="Search and filter moment notes &amp; set takeaways"' in html
    assert 'id="sideTabMoments"' in html and 'data-tip="Create and browse saved moment notes"' in html

    # Zero user-facing Select mark / untriaged marks / Select (Coin) bookmark copy across HTML + JS
    bad_patterns = [
        re.compile(r"untriaged marks", re.IGNORECASE),
        re.compile(r"Select \(Coin\) bookmark", re.IGNORECASE),
        re.compile(r"in-game Select \(Coin\)", re.IGNORECASE),
        re.compile(r"tap Select during a match", re.IGNORECASE),
        re.compile(r"Has Select mark", re.IGNORECASE),
        re.compile(r"Toggle Select marks", re.IGNORECASE),
        re.compile(r"in-game Select mark", re.IGNORECASE),
        re.compile(r"Dismiss this Select mark", re.IGNORECASE),
        re.compile(r"Annotate this Select mark", re.IGNORECASE),
    ]
    files_to_audit = [INDEX_HTML, *sorted((WEB_DIR / "js").glob("*.js"))]
    for fpath in files_to_audit:
        text = fpath.read_text(encoding="utf-8")
        # Ignore internal backward-compat regex stripping old default note text in playlists.js
        filtered_lines = [ln for ln in text.splitlines() if "/^In-game Select mark #" not in ln]
        joined = "\n".join(filtered_lines)
        for pat in bad_patterns:
            assert not pat.search(joined), f"Found retired copy matching {pat.pattern!r} in {fpath.name}"

    # Lua: dump_telemetry gates emit_select_mark behind FC_ENABLE_SELECT_MARKS == "1",
    # while replay_takeover.lua still retains Coin 1-frame retry intact.
    dump_lua = LUA_DUMP.read_text(encoding="utf-8")
    assert 'os.getenv("FC_ENABLE_SELECT_MARKS") == "1"' in dump_lua
    takeover_lua = LUA_TAKEOVER.read_text(encoding="utf-8")
    assert "P1 Coin" in takeover_lua

    # README scrubbed live-match Select bullet while preserving Replay Takeover retry control
    readme = README_MD.read_text(encoding="utf-8")
    assert "Tap `Select` during live matches" not in readme
    assert "During live Fightcade matches" not in readme
    assert "During Replay Takeover" in readme
    assert "**`Select` (`Coin`)** or **`Alt+1`** — Instant 1-frame retry" in readme


def test_notes_collapsed_multi_select_tag_filter_and_summary_visibility():
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

const clickListeners = [];
const sandbox = {{
  console,
  window: {{
    innerWidth: 390,
    location: {{ origin: "http://localhost:8765", search: "", hash: "#notes", pathname: "/" }},
    matchMedia: () => ({{ matches: true, addEventListener: () => {{}} }}),
  }},
  document: {{
    getElementById: (id) => makeEl(id),
    querySelectorAll: () => [],
    addEventListener: (ev, fn) => {{ if (ev === "click") clickListeners.push(fn); }},
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
  _clickListeners: clickListeners,
}};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(NOTES_JS))}, "utf8"), sandbox);

const out = vm.runInContext(`
  state.username = "blankasballs";
  state.hubNotes = [
    {{
      id: "n1", source: "annotation", entry_type: "moment", kind: "note", quark_id: "q1",
      round_id: 1, perspective: 1, has_video: true, video_url: "/r1.mp4",
      self_char_name: "Akuma", opp_char_name: "Ken", note: "Missed crouch fierce",
      tags: ["Anti-Air", "Corner"], date_str: "2026-11-26", editable: true, deletable: true,
    }},
    {{
      id: "n2", source: "annotation", entry_type: "moment", kind: "note", quark_id: "q1",
      round_id: 2, perspective: 1, has_video: true, video_url: "/r2.mp4",
      self_char_name: "Akuma", opp_char_name: "Chun-Li", note: "Good anti-air DP",
      tags: ["Anti-Air"], date_str: "2026-11-26", editable: true, deletable: true,
    }},
    {{
      id: "n3", source: "set_note", entry_type: "set_note", kind: "set_takeaway", quark_id: "q2",
      has_video: false, self_char_name: "Akuma", opp_char_name: "Ryu", note: "Walk back more",
      tags: ["Footsies"], date_str: "2026-11-25", editable: true, deletable: true,
    }},
  ];
  state.hubCounts = {{
    total: 3, moments: 2, set_notes: 1, by_kind: {{ response: 0 }},
    by_tag: [
      {{ tag: "Anti-Air", count: 2 }},
      {{ tag: "Corner", count: 1 }},
      {{ tag: "Footsies", count: 1 }},
    ],
  }};
  state.hubAvailableChars = {{ self: ["Akuma"], opp: ["Chun-Li", "Ken", "Ryu"] }};

  // Initial unfiltered render: tag dropdown closed, active tags row & summary row hidden
  renderHubNotesControls();
  renderHubNotes();
  const initialWrapHtml = _elements["notesTagFilterWrap"].innerHTML;
  const initialSummaryHidden = _elements["notesSummaryRow"].classList.contains("hidden");
  const initialActiveTagsHidden = _elements["notesActiveTagsRow"].classList.contains("hidden");
  const initialClearHidden = _elements["btnClearNotesFilters"].classList.contains("hidden");

  // Open Tags dropdown -> shows tags with counts
  toggleNotesTagFilterMenu();
  const openMenuHtml = _elements["notesTagFilterWrap"].innerHTML;

  // Select 2 tags via dropdown (stay-open multi-select) -> narrows to n1 (both Anti-Air & Corner)
  toggleNotesTagFilter("Anti-Air", true);
  toggleNotesTagFilter("Corner", true);
  const afterTwoTagsMenuOpen = state.notesTagFilterOpen;
  const afterTwoTagsWrapHtml = _elements["notesTagFilterWrap"].innerHTML;
  const afterTwoTagsChipsHtml = _elements["notesActiveTagsRow"].innerHTML;
  const afterTwoTagsFilteredCount = getFilteredHubNotes().length;
  const afterTwoTagsSummaryHidden = _elements["notesSummaryRow"].classList.contains("hidden");
  const afterTwoTagsSummaryText = _elements["notesSummaryText"].textContent;
  const afterTwoTagsClearHidden = _elements["btnClearNotesFilters"].classList.contains("hidden");

  // Outside click closes tag filter dropdown
  _clickListeners.forEach(fn => fn({{ target: {{ closest: () => null }} }}));
  const openAfterOutsideClick = state.notesTagFilterOpen;

  // Clicking Clear resets everything and hides summary + active tag chips
  clearNotesFilters();
  const afterClearCount = getFilteredHubNotes().length;
  const afterClearSummaryHidden = _elements["notesSummaryRow"].classList.contains("hidden");
  const afterClearActiveTagsHidden = _elements["notesActiveTagsRow"].classList.contains("hidden");

  ({{
    initialWrapHtml,
    initialSummaryHidden,
    initialActiveTagsHidden,
    initialClearHidden,
    openMenuHtml,
    afterTwoTagsMenuOpen,
    afterTwoTagsWrapHtml,
    afterTwoTagsChipsHtml,
    afterTwoTagsFilteredCount,
    afterTwoTagsSummaryHidden,
    afterTwoTagsSummaryText,
    afterTwoTagsClearHidden,
    openAfterOutsideClick,
    afterClearCount,
    afterClearSummaryHidden,
    afterClearActiveTagsHidden,
  }});
`, sandbox);
console.log(JSON.stringify(out));
"""
    res = _run_node(script)

    # Collapsed & ultra-compact when unfiltered
    assert "notes-tag-filter-menu" not in res["initialWrapHtml"]
    assert "Tags ▾" in res["initialWrapHtml"]
    assert res["initialSummaryHidden"] is True
    assert res["initialActiveTagsHidden"] is True
    assert res["initialClearHidden"] is True

    # Open dropdown lists tags with counts
    assert "notes-tag-filter-menu" in res["openMenuHtml"]
    assert "Anti-Air" in res["openMenuHtml"]
    assert "(2)" in res["openMenuHtml"]
    assert "Corner" in res["openMenuHtml"]

    # Multi-tag selection stays open, updates button count, renders removable chips & compact summary
    assert res["afterTwoTagsMenuOpen"] is True
    assert "Tags (2) ▾" in res["afterTwoTagsWrapHtml"]
    assert "Anti-Air" in res["afterTwoTagsChipsHtml"]
    assert "Corner" in res["afterTwoTagsChipsHtml"]
    assert "tag-chip-x" in res["afterTwoTagsChipsHtml"]
    assert res["afterTwoTagsFilteredCount"] == 1
    assert res["afterTwoTagsSummaryHidden"] is False
    assert res["afterTwoTagsSummaryText"] == "Showing 1 of 3"
    assert res["afterTwoTagsClearHidden"] is False

    # Outside click closes dropdown
    assert res["openAfterOutsideClick"] is False

    # Clear restores unfiltered compact state
    assert res["afterClearCount"] == 3
    assert res["afterClearSummaryHidden"] is True
    assert res["afterClearActiveTagsHidden"] is True
