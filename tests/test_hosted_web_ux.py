"""Tests for hosted_web mode Home Hero, Matches zero-404 guard, and Notes empty states."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
INDEX_HTML = WEB_DIR / "index.html"
STATE_JS = WEB_DIR / "js" / "state.js"
HOME_JS = WEB_DIR / "js" / "home.js"
MATCHES_JS = WEB_DIR / "js" / "matches.js"
NOTES_JS = WEB_DIR / "js" / "notes.js"
APP_JS = WEB_DIR / "js" / "app.js"


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


def _build_vm_script(mode: str, body_js: str) -> str:
    return f"""
const fs = require("fs");
const vm = require("vm");

const loc = {{ pathname: "/", search: "", hash: "", origin: "https://fightcadelab.com" }};
const elements = Object.create(null);
function makeEl(id) {{
  if (!elements[id]) {{
    const initHidden = ["navInsights", "viewAbout", "viewMatches", "viewNotes", "viewInsights", "viewStudio", "viewReport", "viewShared", "studioBreadcrumb", "crumbMatchup"].includes(id);
    elements[id] = {{
      id,
      innerHTML: "",
      textContent: "",
      value: "",
      disabled: false,
      style: {{}},
      dataset: {{}},
      _attrs: {{}},
      classList: {{
        _set: new Set(initHidden ? ["hidden"] : []),
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
      setAttribute(k, v) {{ this._attrs[k] = String(v); }},
      getAttribute(k) {{ return this._attrs[k]; }},
      removeAttribute(k) {{ delete this._attrs[k]; }},
      addEventListener() {{}},
      querySelector: () => null,
      querySelectorAll: () => [],
    }};
  }}
  return elements[id];
}}

const apiCalls = [];
const toastCalls = [];

const sandbox = {{
  console,
  window: {{
    innerWidth: 1200,
    __FC_BOOTSTRAP__: {{ mode: {json.dumps(mode)}, initialRoute: "" }},
    location: loc,
    history: {{ pushState() {{}}, replaceState() {{}}, back() {{}} }},
    addEventListener() {{}},
    scrollTo: () => {{}},
    matchMedia: () => ({{ matches: false }}),
  }},
  history: {{ pushState() {{}}, replaceState() {{}}, back() {{}} }},
  document: {{
    title: "",
    hidden: false,
    documentElement: {{ getAttribute: () => "dark", setAttribute: () => {{}}, classList: {{ toggle() {{}} }} }},
    body: {{ classList: {{ toggle() {{}} }} }},
    getElementById: (id) => makeEl(id),
    querySelector: (sel) => sel === "#viewMatches > .card" ? makeEl("matchesFilterCard") : null,
    querySelectorAll: () => [],
    addEventListener: () => {{}},
  }},
  HTMLVideoElement: {{ prototype: {{}} }},
  localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
  Date, Intl, Set, Map, Math, Number, String, Array, Object, Boolean, JSON, Promise, URLSearchParams, encodeURIComponent, decodeURIComponent,
  setInterval: () => 1, clearInterval: () => {{}}, setTimeout: () => 0, clearTimeout: () => {{}},
  syncThemeUi: () => {{}},
  initMobileTopbarAutoHide: () => {{}},
  initDesktopTooltips: () => {{}},
  setTopbarHidden: () => {{}},
  updateStatusPillUi: () => {{}},
  checkForNewTakeoverResponses: () => {{}},
  _elements: elements,
  _apiCalls: apiCalls,
  _toastCalls: toastCalls,
}};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);

vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(HOME_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(MATCHES_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(NOTES_JS))}, "utf8"), sandbox);
const appSrc = fs.readFileSync({json.dumps(str(APP_JS))}, "utf8").replace(/initApp\\(\\);\\s*$/, "");
vm.runInContext(appSrc, sandbox);

vm.runInContext(`
  toast = (msg) => {{ _toastCalls.push(String(msg)); }};
  api = async (url) => {{
    _apiCalls.push(String(url));
    if (String(url).startsWith("/api/matches")) {{
      return {{ ok: true, matches: [], counts: {{ total: 0, ready: 0, unprocessed: 0, with_notes: 0 }}, available_chars: {{ self: [], opp: [] }}, last_refreshed: null }};
    }}
    return {{ ok: true }};
  }};
`, sandbox);

(async () => {{
  const out = await vm.runInContext(`(async () => {{
{body_js}
  }})()`, sandbox);
  console.log(JSON.stringify(out));
}})();
"""


def test_hosted_home_hero_signed_out_and_signed_in_vs_local_wizard():
    hosted_script = _build_vm_script(
        "hosted_web",
        """
    state.homeData = {
      latest_match: null,
      recent_matches: [],
      focus_goals: { active: [], graduated_count: 0, max_active: 3 },
      playlists: [],
      playlists_total: 0,
      pulse: { notes_total: 0, matchups_count: 0, untriaged_marks: 0, latest_insight: null },
    };

    // 1. Signed-out hosted web
    state.auth = { authenticated: false, user: null, quota: null };
    state.companionConnected = false;
    renderHomeWorkbench();
    const signedOutHtml = _elements["homeContent"].innerHTML;

    // 2. Signed-in hosted web (Companion not connected, 2 shared clips)
    state.auth = { authenticated: true, user: { discord_username: "AkumaMain" }, quota: null };
    state.homeData.pulse = { notes_total: 2, matchups_count: 2, untriaged_marks: 0, latest_insight: null };
    state.companionConnected = false;
    renderHomeWorkbench();
    const signedInNoCompHtml = _elements["homeContent"].innerHTML;

    // 3. Signed-in hosted web (Companion connected, 0 shared clips)
    state.companionConnected = true;
    state.homeData.pulse = { notes_total: 0, matchups_count: 0, untriaged_marks: 0, latest_insight: null };
    renderHomeWorkbench();
    const signedInCompHtml = _elements["homeContent"].innerHTML;

    return { signedOutHtml, signedInNoCompHtml, signedInCompHtml };
        """,
    )
    res = _run_node(hosted_script)

    so = res["signedOutHtml"]
    assert "homeFirstUser" not in so
    assert "homeFirstRunProcessLatest" not in so
    assert "Refresh Only" not in so
    assert "—" not in so and "&mdash;" not in so.lower()
    assert "FIGHTCADE LAB" in so
    assert "3rd Strike replay review, matchup notes, and Replay Takeover." in so
    assert "/assets/img/header-astro-lab.jpg" in so
    assert 'href="/api/auth/discord/start?next=%2F"' in so
    assert "Sign in with Discord" in so
    assert "Download Windows Companion →" in so
    assert "https://github.com/blankasballs/fightcade-lab/releases" in so
    # Empty workbench cards hidden when signed out
    assert "workbench-grid" not in so
    assert "Active Focuses" not in so
    assert "Matchup Notebook" not in so

    si_no = res["signedInNoCompHtml"]
    assert "homeFirstUser" not in si_no
    assert "homeFirstRunProcessLatest" not in si_no
    assert "FIGHTCADE LAB · CLOUD" in si_no
    assert "Welcome back, AkumaMain" in si_no
    assert "Get Windows Companion →" in si_no
    assert "Open My Shared Clips →" in si_no
    assert "Active Focuses" in si_no
    assert "Playlists" in si_no
    assert "Matchup Notebook" in si_no
    assert "2 saved moments across 2 matchups" in si_no
    # Hosted hero section itself must have zero em dashes
    si_hero_only = si_no.split("workbench-grid")[0]
    assert "—" not in si_hero_only and "&mdash;" not in si_hero_only.lower()

    si_comp = res["signedInCompHtml"]
    assert "Get Windows Companion →" not in si_comp
    assert "Open My Shared Clips →" not in si_comp
    assert "Active Focuses" in si_comp

    # Local Companion First-Run Wizard must remain intact!
    local_script = _build_vm_script(
        "local_companion",
        """
    state.homeData = {
      latest_match: null,
      recent_matches: [],
      focus_goals: { active: [], graduated_count: 0, max_active: 3 },
      playlists: [],
      playlists_total: 0,
      pulse: { notes_total: 0, matchups_count: 0, untriaged_marks: 0, latest_insight: null },
    };
    renderHomeWorkbench();
    return { localHtml: _elements["homeContent"].innerHTML };
        """,
    )
    local_res = _run_node(local_script)
    assert 'id="homeFirstUser"' in local_res["localHtml"]
    assert "homeFirstRunProcessLatest()" in local_res["localHtml"]
    assert "Refresh Only" in local_res["localHtml"]


def test_hosted_keeps_nav_matches_visible_and_renders_guided_funnel():
    assert 'id="matchesFilterCard"' in INDEX_HTML.read_text(encoding="utf-8")

    hosted_script = _build_vm_script(
        "hosted_web",
        """
    state.activeView = "home";
    state.auth = { authenticated: false, user: null, quota: null, has_byoc: false };
    syncTopNav();
    const navMatchesHiddenOnHome = _elements["navMatches"].classList.contains("hidden");

    // Signed-out /#matches visit in hosted mode renders the guided funnel without calling /api/matches
    await loadMatches();
    await refreshFromFightcade(false);

    return {
      navMatchesHiddenOnHome,
      filterCardHidden: _elements["matchesFilterCard"].classList.contains("hidden"),
      matchListHtml: _elements["matchList"].innerHTML,
      apiCalls: _apiCalls,
      toastCalls: _toastCalls,
    };
        """,
    )
    res = _run_node(hosted_script)
    assert res["navMatchesHiddenOnHome"] is False
    assert res["filterCardHidden"] is True
    assert res["apiCalls"] == []
    assert res["toastCalls"] == []
    assert "Watch your full matches on mobile" in res["matchListHtml"]
    assert "How this stays free →" in res["matchListHtml"]
    assert "Download Windows Companion →" in res["matchListHtml"]
    assert "https://github.com/blankasballs/fightcade-lab/releases" in res["matchListHtml"]

    local_script = _build_vm_script(
        "local_companion",
        """
    state.activeView = "matches";
    syncTopNav();
    const navMatchesHiddenLocal = _elements["navMatches"].classList.contains("hidden");
    await loadMatches();
    return {
      navMatchesHiddenLocal,
      filterCardHiddenLocal: _elements["matchesFilterCard"].classList.contains("hidden"),
      apiCalls: _apiCalls,
    };
        """,
    )
    local_res = _run_node(local_script)
    assert local_res["navMatchesHiddenLocal"] is False
    assert local_res["filterCardHiddenLocal"] is False
    assert any(u.startswith("/api/matches?") for u in local_res["apiCalls"])


def test_hosted_notes_empty_states_signed_out_and_signed_in():
    hosted_script = _build_vm_script(
        "hosted_web",
        """
    // 1. Hosted signed-out empty state
    state.auth = { authenticated: false, user: null, quota: null };
    state.hubNotes = [];
    renderHubNotes();
    const signedOutNotesHtml = _elements["notesHubList"].innerHTML;

    // 2. Hosted signed-in with 0 cloud moments
    state.auth = { authenticated: true, user: { discord_username: "KenMain" }, quota: null };
    state.hubNotes = [];
    renderHubNotes();
    const signedInEmptyHtml = _elements["notesHubList"].innerHTML;

    // 3. Hosted signed-in with moments, filtered to 0 results
    state.hubNotes = [{ id: "m1", entry_type: "moment", kind: "note", note: "Anti-air cr.HP", self_char_name: "Akuma", opp_char_name: "Ken", tags: ["Anti-Air"] }];
    state.notesSearch = "nonexistent-filter-xyz";
    renderHubNotes();
    const signedInFilteredEmptyHtml = _elements["notesHubList"].innerHTML;

    return { signedOutNotesHtml, signedInEmptyHtml, signedInFilteredEmptyHtml };
        """,
    )
    res = _run_node(hosted_script)
    assert "Sign in with Discord to view your shared moments and playlists from any device." in res["signedOutNotesHtml"]
    assert 'href="/api/auth/discord/start?next=%2F%23notes"' in res["signedOutNotesHtml"]
    assert "Pin moments inside Replay Studio" not in res["signedOutNotesHtml"]

    assert "No shared moments in the cloud yet. Use Share Moment in the Windows Companion to publish clips here for mobile review and Discord sharing." in res["signedInEmptyHtml"]
    assert "Pin moments inside Replay Studio" not in res["signedInEmptyHtml"]

    assert "No shared moments match these filters." in res["signedInFilteredEmptyHtml"]

    # Local mode empty state stays intact
    local_script = _build_vm_script(
        "local_companion",
        """
    state.hubNotes = [];
    renderHubNotes();
    return { localNotesHtml: _elements["notesHubList"].innerHTML };
        """,
    )
    local_res = _run_node(local_script)
    assert "Pin moments inside Replay Studio!" in local_res["localNotesHtml"]
