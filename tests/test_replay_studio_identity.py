"""Tests for Replay Studio identity, dynamic document.title, and unified Open in Studio CTAs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
INDEX_HTML = WEB_DIR / "index.html"
STYLES_CSS = WEB_DIR / "styles.css"
STATE_JS = WEB_DIR / "js" / "state.js"
APP_JS = WEB_DIR / "js" / "app.js"
HOME_JS = WEB_DIR / "js" / "home.js"
MATCHES_JS = WEB_DIR / "js" / "matches.js"
NOTES_JS = WEB_DIR / "js" / "notes.js"
REPORT_JS = WEB_DIR / "js" / "report_viewer.js"
STUDIO_JS = WEB_DIR / "js" / "studio.js"
SHARED_JS = WEB_DIR / "js" / "shared.js"


def test_replay_studio_eyebrow_default_title_and_navbar():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert "<title>Fightcade Lab</title>" in html
    assert '<div class="studio-page-eyebrow">REPLAY STUDIO</div>' in html
    assert 'class="switcher-title-col"' in html
    assert ".studio-page-eyebrow" in css

    # Eyebrow must be inside #viewStudio before #studioMatchupHeader
    idx_studio = html.index('id="viewStudio"')
    idx_eyebrow = html.index('<div class="studio-page-eyebrow">REPLAY STUDIO</div>')
    idx_hdr = html.index('id="studioMatchupHeader"')
    assert idx_studio < idx_eyebrow < idx_hdr

    # Navbar keeps Home / Matches / Notes without a standalone Studio tab
    nav_start = html.index('id="navTabs"')
    nav_end = html.index("</nav>", nav_start)
    nav_html = html[nav_start:nav_end]
    assert 'data-tab="studio"' not in nav_html
    assert ">Studio<" not in nav_html


def test_studio_entry_ctas_unified_across_all_surfaces():
    files = {
        "index.html": INDEX_HTML.read_text(encoding="utf-8"),
        "app.js": APP_JS.read_text(encoding="utf-8"),
        "home.js": HOME_JS.read_text(encoding="utf-8"),
        "matches.js": MATCHES_JS.read_text(encoding="utf-8"),
        "notes.js": NOTES_JS.read_text(encoding="utf-8"),
        "report_viewer.js": REPORT_JS.read_text(encoding="utf-8"),
    }

    forbidden = (
        "Review Match",
        "Review →",
        "Open Replay →",
        "Watch Now",
        "Watch (1–",
        "Open Match in Studio",
        "Ready to Review",
    )
    for fname, src in files.items():
        for bad in forbidden:
            assert bad not in src, f"Legacy mismatched label {bad!r} still present in {fname}"
        assert "Open in Studio" in src, f"Expected 'Open in Studio' CTA in {fname}"


def test_dynamic_document_title_across_views():
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
        _set: new Set(),
        add(...cs) {{ cs.forEach(c => this._set.add(c)); }},
        remove(...cs) {{ cs.forEach(c => this._set.delete(c)); }},
        toggle(c, force) {{
          if (force === undefined) {{
            if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
          }} else if (force) this._set.add(c); else this._set.delete(c);
        }},
        contains(c) {{ return this._set.has(c); }},
      }},
      addEventListener() {{}},
      setAttribute() {{}},
    }};
  }}
  return elements[id];
}}

const doc = {{
  title: "Fightcade Lab",
  getElementById: (id) => makeEl(id),
  querySelectorAll: () => [],
  addEventListener: () => {{}},
  documentElement: {{ setAttribute() {{}}, getAttribute() {{ return null; }}, removeAttribute() {{}} }},
}};
const sandbox = {{
  console,
  window: {{
    location: {{ origin: "http://localhost:8765", search: "", hash: "", pathname: "/" }},
    addEventListener: () => {{}},
    matchMedia: () => ({{ matches: false }}),
    innerWidth: 1280,
    scrollY: 0,
  }},
  document: doc,
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
  history: {{ replaceState: () => {{}} }},
}};
sandbox.window.document = doc;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
const appSrc = fs.readFileSync({json.dumps(str(APP_JS))}, "utf8").replace("initApp();", "");
vm.runInContext(appSrc, sandbox);

const results = vm.runInContext(`
  const out = {{}};
  state.activeView = "home";
  updateDocumentTitle();
  out.home = document.title;

  state.activeView = "matches";
  updateDocumentTitle();
  out.matches = document.title;

  state.activeView = "notes";
  state.notesSubView = "notebook";
  updateDocumentTitle();
  out.notes = document.title;

  state.notesSubView = "playlists";
  updateDocumentTitle();
  out.playlists = document.title;

  state.activeView = "report";
  updateDocumentTitle();
  out.report = document.title;

  state.activeView = "studio";
  state.activeManifest = null;
  updateDocumentTitle();
  out.studioFallback = document.title;

  state.activeManifest = {{
    quark_id: "q_test_1",
    self_player_name: "blankasballs",
    self_char_name: "Akuma",
    opp_player_name: "kenplayer",
    opp_char_name: "Ken",
    opp_rank_letter: "A",
  }};
  updateDocumentTitle();
  out.studioMatch = document.title;

  state.activeView = "shared_moment";
  state.sharedPerspective = 1;
  state.sharedMoment = {{
    quark_id: "q_test_1",
    self_player_name: "blankasballs",
    self_char_name: "Akuma",
    opp_player_name: "kenplayer",
    opp_char_name: "Ken",
  }};
  updateDocumentTitle();
  out.sharedMoment = document.title;

  state.activeView = "shared_playlist";
  state.sharedPlaylist = {{ id: "pl_1", name: "Anti-Air Drills" }};
  updateDocumentTitle();
  out.sharedPlaylist = document.title;

  out;
`, sandbox);
console.log(JSON.stringify(results));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    res = json.loads(proc.stdout.strip())

    assert res["home"] == "Fightcade Lab"
    assert res["matches"] == "Matches — Fightcade Lab"
    assert res["notes"] == "Notes — Fightcade Lab"
    assert res["playlists"] == "Playlists — Fightcade Lab"
    assert res["report"] == "Report — Fightcade Lab"
    assert res["studioFallback"] == "Replay Studio — Fightcade Lab"
    assert res["studioMatch"] == "Replay Studio · blankasballs (Akuma) vs kenplayer (Ken) — Fightcade Lab"
    assert res["sharedMoment"] == "blankasballs (Akuma) vs kenplayer (Ken) — Fightcade Lab"
    assert res["sharedPlaylist"] == "Anti-Air Drills — Fightcade Lab"


def test_studio_match_and_round_pills_omit_r_dot():
    studio_src = STUDIO_JS.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert "r-dot" not in studio_src
    assert ".r-dot" not in css

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
      style: {{}},
      dataset: {{}},
      classList: {{ add() {{}}, remove() {{}}, toggle() {{}}, contains() {{ return false; }} }},
      setAttribute() {{}},
      addEventListener() {{}},
      getContext() {{ return {{ clearRect() {{}} }}; }},
      querySelectorAll: () => [],
    }};
  }}
  return elements[id];
}}

const doc = {{
  documentElement: {{ getAttribute: () => "dark" }},
  getElementById: (id) => makeEl(id),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {{}},
}};
const sandbox = {{
  console,
  window: {{ location: {{ origin: "http://localhost:8765", search: "", hash: "", pathname: "/" }}, addEventListener: () => {{}} }},
  document: doc,
  HTMLVideoElement: {{ prototype: {{}} }},
  localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
  Intl, Date, Set, Map, Math, Number, String, Array, Object, Boolean, JSON, URLSearchParams, encodeURIComponent,
  setTimeout: () => 0, clearTimeout: () => {{}}, setInterval: () => 0, clearInterval: () => {{}},
  _elements: elements,
}};
sandbox.window.document = doc;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STUDIO_JS))}, "utf8"), sandbox);

const out = vm.runInContext(`
  state.annotations = [{{ id: "a1", round_id: 1, note: "Saved moment" }}];
  state.activeManifest = {{
    quark_id: "q_dots",
    matches: [{{ match_id: 1, round_ids: [1, 2], self_rounds_won: 2, opp_rounds_won: 0, self_won: true }}],
    rounds: [
      {{ round_id: 1, match_id: 1, match_round: 1, self_won: true }},
      {{ round_id: 2, match_id: 1, match_round: 2, self_won: true }},
    ],
  }};
  state.activeMatchId = 1;
  state.activeRoundIdx = 0;
  renderMatchAndRoundTabs();
  ({{
    matchTabsHtml: _elements["matchTabs"].innerHTML,
    roundTabsHtml: _elements["roundTabs"].innerHTML,
  }});
`, sandbox);
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert proc.returncode == 0, f"node failed:\n{proc.stderr}"
    res = json.loads(proc.stdout.strip())
    assert "M1 2–0" in res["matchTabsHtml"]
    assert "R1" in res["roundTabsHtml"]
    assert "r-dot" not in res["matchTabsHtml"]
    assert "r-dot" not in res["roundTabsHtml"]
    assert "Has saved notes" not in res["matchTabsHtml"]
    assert "Has saved notes" not in res["roundTabsHtml"]
