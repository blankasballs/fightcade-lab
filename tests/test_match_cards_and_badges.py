"""Tests for uniform mobile Match cards, handle truncation, and status badge cleanup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
STATE_JS = WEB_DIR / "js" / "state.js"
MATCHES_JS = WEB_DIR / "js" / "matches.js"
HOME_JS = WEB_DIR / "js" / "home.js"
STYLES_CSS = WEB_DIR / "styles.css"


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


def test_status_badge_and_match_card_rendering():
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
        toggle() {{}},
        contains(c) {{ return this._set.has(c); }},
      }},
    }};
  }}
  return elements[id];
}}

const sandbox = {{
  console,
  window: {{ location: {{ origin: "http://localhost:8765", search: "" }} }},
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
}};
sandbox.window.document = sandbox.document;
sandbox._elements = elements;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(MATCHES_JS))}, "utf8"), sandbox);

const out = vm.runInContext(`
  const badges = {{
    ready: statusBadgeHtml({{ status: "ready", mobile_round_count: 4 }}),
    unprocessed: statusBadgeHtml({{ status: "unprocessed" }}),
    processing: statusBadgeHtml({{ status: "processing", job: {{ message: "Rendering R2" }} }}),
    queued: statusBadgeHtml({{ status: "queued" }}),
    cached_no_video: statusBadgeHtml({{ status: "cached_no_video" }}),
    telemetry_only: statusBadgeHtml({{ status: "telemetry_only" }}),
    errorNoJob: statusBadgeHtml({{ status: "error" }}),
  }};

  state.username = "blankasballs";
  state.counts = {{ total: 2, ready: 1, unprocessed: 1, with_notes: 0 }};
  state.capabilities = {{ can_render: true, has_gemini: false, pro_corpus_enabled: false }};
  state.matches = [
    {{
      quark_id: "q_ready_1",
      status: "ready",
      perspective: 1,
      self_player_name: "blankasballs",
      self_rank_letter: "B",
      self_char_name: "Akuma",
      opp_player_name: "super_long_fightcade_handle_28chars",
      opp_rank_letter: "A",
      opp_char_name: "Chun-Li",
      self_score: 2,
      opp_score: 1,
      mobile_round_count: 4,
      duration_s: 360,
      ranked: true,
      date_str: "2026-11-26",
      notes_count: 0,
      select_marks_count: 0,
    }},
    {{
      quark_id: "q_unproc_1",
      status: "unprocessed",
      perspective: 2,
      self_player_name: "blankasballs",
      opp_player_name: "rival_two",
      opp_rank_letter: "C",
      mobile_round_count: 0,
      duration_s: 180,
      ranked: false,
      date_str: "2026-11-25",
      notes_count: 0,
      select_marks_count: 0,
    }},
  ];
  renderMatchList();
  const listHtml = _elements["matchList"].innerHTML;

  // Also test a card that DOES have a note pill -> .match-badges should render
  state.matches[0].notes_count = 2;
  renderMatchList();
  const listWithBadgesHtml = _elements["matchList"].innerHTML;

  ({{ badges, listHtml, listWithBadgesHtml }});
`, sandbox);
console.log(JSON.stringify(out));
"""
    res = _run_node(script)
    badges = res["badges"]

    # Redundant ready & unprocessed badges return empty string
    assert badges["ready"] == ""
    assert badges["unprocessed"] == ""
    # Transitional / special states preserved
    assert "Rendering R2" in badges["processing"]
    assert "Queued" in badges["queued"]
    assert "Cached (~15s)" in badges["cached_no_video"]
    assert "Telemetry Only" in badges["telemetry_only"]
    assert "Error" in badges["errorNoJob"]

    # Match card HTML checks
    html = res["listHtml"]
    assert 'class="match-card-grid"' in html
    assert 'class="match-card-cta"' in html
    assert 'class="match-side self"' in html
    assert 'class="match-side opp"' in html
    assert 'class="match-player-name"' in html
    assert 'class="pill match-score-pill"' in html
    # Zero empty badge row when no badges exist
    assert 'match-badges' not in html
    assert "Not Processed" not in html
    assert "● Ready" not in html
    # Meta includes round count, duration, Ranked, and drops P1 / P2
    assert "· 4R" in html
    assert "· 6m" in html
    assert "· Ranked" in html
    assert "· P1" not in html
    assert "· P2" not in html

    # When badges ARE present, .match-badges row renders cleanly
    assert 'match-badges' in res["listWithBadgesHtml"]
    assert "📝 2" in res["listWithBadgesHtml"]


def test_mobile_match_card_css_and_home_hero_cleanup():
    css = STYLES_CSS.read_text(encoding="utf-8")
    home_src = HOME_JS.read_text(encoding="utf-8")

    # Mobile responsive rules present in styles.css
    assert "@media (max-width: 640px)" in css
    assert ".match-card-grid" in css
    assert ".match-player-name" in css
    assert "text-overflow: ellipsis" in css
    assert ".match-side.self" in css
    assert ".match-side.opp" in css
    assert ".match-card-cta" in css

    # Home Latest Match hero removes redundant Ready / Unprocessed pills
    assert "● Ready" not in home_src
    assert "● Unprocessed" not in home_src
    assert 'latest.status === "processing" || latest.status === "queued"' in home_src
