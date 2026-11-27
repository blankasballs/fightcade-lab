"""Tests for real browser history (pushState / popstate) navigation & modal Back-swipe support."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
STATE_JS = WEB_DIR / "js" / "state.js"
APP_JS = WEB_DIR / "js" / "app.js"
NOTES_JS = WEB_DIR / "js" / "notes.js"
STUDIO_JS = WEB_DIR / "js" / "studio.js"
PLAYLISTS_JS = WEB_DIR / "js" / "playlists.js"
REPORT_JS = WEB_DIR / "js" / "report_viewer.js"
SHARED_JS = WEB_DIR / "js" / "shared.js"


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


def test_history_api_wiring_across_web_modules():
    app_src = APP_JS.read_text(encoding="utf-8")
    notes_src = NOTES_JS.read_text(encoding="utf-8")
    studio_src = STUDIO_JS.read_text(encoding="utf-8")
    playlists_src = PLAYLISTS_JS.read_text(encoding="utf-8")
    report_src = REPORT_JS.read_text(encoding="utf-8")
    shared_src = SHARED_JS.read_text(encoding="utf-8")

    # Core helpers and popstate listener live in app.js
    assert "function updateAppHistory(" in app_src
    assert "function pushModalHistory(" in app_src
    assert "function popModalHistory(" in app_src
    assert "function handlePopState(" in app_src
    assert 'window.addEventListener("popstate", handlePopState);' in app_src
    assert 'window.addEventListener("hashchange", handleHashChange);' in app_src

    # Downstream view modules route through updateAppHistory / pushModalHistory instead of raw replaceState
    for name, src in [
        ("notes.js", notes_src),
        ("studio.js", studio_src),
        ("report_viewer.js", report_src),
        ("shared.js", shared_src),
    ]:
        assert "history.replaceState" not in src, f"{name} still calls history.replaceState directly"
        assert "updateAppHistory(" in src, f"{name} should call updateAppHistory()"

    assert 'pushModalHistory("watch")' in playlists_src
    assert 'popModalHistory("watch")' in playlists_src


def _build_vm_harness(
    initial_url: str = "/",
    hosted: bool = False,
    pro_corpus: bool = True,
) -> str:
    """Return a JS snippet that sets up a simulated browser history + DOM + loads app/notes/studio/playlists/report/shared."""
    return f"""
const fs = require("fs");
const vm = require("vm");

function parseUrl(u) {{
  const raw = String(u || "/");
  const hashIdx = raw.indexOf("#");
  const hash = hashIdx >= 0 ? raw.slice(hashIdx) : "";
  const beforeHash = hashIdx >= 0 ? raw.slice(0, hashIdx) : raw;
  const qIdx = beforeHash.indexOf("?");
  const search = qIdx >= 0 ? beforeHash.slice(qIdx) : "";
  const pathname = (qIdx >= 0 ? beforeHash.slice(0, qIdx) : beforeHash) || "/";
  return {{ pathname, search, hash }};
}}

const loc = parseUrl({json.dumps(initial_url)});
loc.origin = "http://localhost:8765";

const historyStack = [{{ state: null, url: `${{loc.pathname}}${{loc.search}}${{loc.hash}}` }}];
let historyIdx = 0;
let pushCalls = 0;
let replaceCalls = 0;
let backCalls = 0;
const winListeners = Object.create(null);

function applyLoc(url) {{
  const p = parseUrl(url);
  loc.pathname = p.pathname;
  loc.search = p.search;
  loc.hash = p.hash;
}}

function dispatchWin(evName, evObj) {{
  const list = winListeners[evName] || [];
  for (const fn of list) fn(evObj);
}}

const historyObj = {{
  get length() {{ return historyStack.length; }},
  get state() {{ return historyStack[historyIdx] ? historyStack[historyIdx].state : null; }},
  pushState(st, _title, url) {{
    pushCalls++;
    const nextUrl = String(url || `${{loc.pathname}}${{loc.search}}${{loc.hash}}`);
    historyStack.splice(historyIdx + 1);
    historyStack.push({{ state: st ? JSON.parse(JSON.stringify(st)) : null, url: nextUrl }});
    historyIdx = historyStack.length - 1;
    applyLoc(nextUrl);
  }},
  replaceState(st, _title, url) {{
    replaceCalls++;
    const nextUrl = String(url || `${{loc.pathname}}${{loc.search}}${{loc.hash}}`);
    historyStack[historyIdx] = {{ state: st ? JSON.parse(JSON.stringify(st)) : null, url: nextUrl }};
    applyLoc(nextUrl);
  }},
  back() {{
    backCalls++;
    if (historyIdx <= 0) return;
    const prevHash = loc.hash;
    historyIdx--;
    const entry = historyStack[historyIdx];
    applyLoc(entry.url);
    dispatchWin("popstate", {{ state: entry.state }});
    if (loc.hash !== prevHash) {{
      dispatchWin("hashchange", {{}});
    }}
  }},
  forward() {{
    if (historyIdx >= historyStack.length - 1) return;
    const prevHash = loc.hash;
    historyIdx++;
    const entry = historyStack[historyIdx];
    applyLoc(entry.url);
    dispatchWin("popstate", {{ state: entry.state }});
    if (loc.hash !== prevHash) {{
      dispatchWin("hashchange", {{}});
    }}
  }},
}};

const elements = Object.create(null);
function makeEl(id) {{
  if (!elements[id]) {{
    const initialHidden = ["watchModal", "mobileNavMenu", "mobileNavBackdrop", "composerCard", "studioNextRoundOverlay", "practiceLoopBar", "viewStudio", "viewReport", "viewShared", "viewMatches", "viewNotes", "viewInsights", "studioBreadcrumb", "crumbMatchup"].includes(id);
    elements[id] = {{
      id,
      innerHTML: "",
      textContent: "",
      value: "",
      disabled: false,
      style: {{}},
      dataset: {{}},
      width: 768,
      height: 576,
      scrollHeight: 80,
      scrollWidth: 100,
      clientWidth: 100,
      scrollLeft: 0,
      paused: true,
      currentTime: 0,
      playbackRate: 1,
      muted: false,
      volume: 0.7,
      src: "",
      _attrs: {{}},
      _listeners: {{}},
      classList: {{
        _set: new Set(initialHidden ? ["hidden"] : []),
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
      addEventListener(ev, fn) {{
        if (!this._listeners[ev]) this._listeners[ev] = [];
        this._listeners[ev].push(fn);
      }},
      pause() {{ this.paused = true; }},
      load() {{}},
      play() {{ this.paused = false; return Promise.resolve(); }},
      focus() {{}},
      scrollIntoView() {{}},
      appendChild() {{}},
      closest: () => null,
      getContext() {{
        return {{
          clearRect() {{}}, fillRect() {{}}, beginPath() {{}}, moveTo() {{}},
          lineTo() {{}}, stroke() {{}}, fill() {{}}, arc() {{}}, arcTo() {{}},
          closePath() {{}}, save() {{}}, restore() {{}}, setLineDash() {{}},
        }};
      }},
      querySelector: () => null,
      querySelectorAll: () => [],
      getBoundingClientRect: () => ({{ left: 0, top: 0, width: 400, height: 76 }}),
    }};
  }}
  return elements[id];
}}

const counters = {{
  loadHome: 0,
  loadMatches: 0,
  loadHubNotes: 0,
  loadPlaylists: 0,
  loadInsights: 0,
  openMatchApi: [],
  openReportApi: [],
  openSharedMomentApi: [],
  openSharedPlaylistApi: [],
}};

const sandbox = {{
  console,
  window: {{
    innerWidth: 390,
    __FC_BOOTSTRAP__: {{
      mode: {json.dumps("hosted_web" if hosted else "local_companion")},
      initialRoute: "",
    }},
    location: loc,
    history: historyObj,
    addEventListener(ev, fn) {{
      if (!winListeners[ev]) winListeners[ev] = [];
      winListeners[ev].push(fn);
    }},
    scrollTo: () => {{}},
    matchMedia: () => ({{ matches: false }}),
  }},
  history: historyObj,
  document: {{
    title: "",
    hidden: false,
    activeElement: null,
    documentElement: {{ getAttribute: () => "dark", setAttribute: () => {{}}, classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }} }},
    body: {{ classList: {{ toggle() {{}}, add() {{}}, remove() {{}} }} }},
    getElementById: (id) => makeEl(id),
    createElement: () => ({{ className: "", textContent: "", style: {{}}, setAttribute() {{}}, appendChild() {{}} }}),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {{}},
  }},
  HTMLVideoElement: {{ prototype: {{}} }},
  localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
  Date,
  Intl,
  Set,
  Map,
  Math,
  Number,
  String,
  Array,
  Object,
  Boolean,
  JSON,
  Promise,
  URLSearchParams,
  encodeURIComponent,
  decodeURIComponent,
  setInterval: () => 1,
  clearInterval: () => {{}},
  setTimeout: () => 0,
  clearTimeout: () => {{}},
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {{}},
  // Minimal stubs for non-history UI helpers
  esc: (s) => String(s == null ? "" : s),
  toast: () => {{}},
  isHostedMode: () => {json.dumps(hosted)},
  isProCorpusEnabled: () => {json.dumps(pro_corpus)},
  isMobileMode: () => true,
  canTakeoverNow: () => false,
  formatMatchLabelText: (m) => (m && m.quark_id) ? String(m.quark_id) : "Match",
  formatMatchLabelHtml: (m) => (m && m.quark_id) ? String(m.quark_id) : "Match",
  formatClipDuration: () => "3.0s",
  renderSystemOriginBadgeHtml: () => "",
  syncThemeUi: () => {{}},
  initMobileTopbarAutoHide: () => {{}},
  initDesktopTooltips: () => {{}},
  setTopbarHidden: () => {{}},
  updateStatusPillUi: () => {{}},
  loadCapabilities: async () => {{}},
  loadAuthState: async () => {{}},
  probeLocalCompanion: () => {{}},
  loadFocusGoals: () => {{}},
  loadHomeWorkbench: () => {{ counters.loadHome++; }},
  loadMatches: () => {{ counters.loadMatches++; }},
  loadInsights: () => {{ counters.loadInsights++; }},
  syncOverlayButtons: () => {{}},
  drawOverlayOnCanvas: () => {{}},
  syncGrouflonGuideUi: () => {{}},
  syncAudioControls: () => {{}},
  initTagPicker: () => {{}},
  renderTagPickerHtml: () => "",
  closeAllTagPickers: () => {{}},
  closeAllCollectionPickers: () => {{}},
  checkForNewTakeoverResponses: () => {{}},
  _historyStack: historyStack,
  _getHistoryIdx: () => historyIdx,
  _getPushCalls: () => pushCalls,
  _getReplaceCalls: () => replaceCalls,
  _getBackCalls: () => backCalls,
  _counters: counters,
  _elements: elements,
}};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);

vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(`state.capabilities.pro_corpus_enabled = {json.dumps(pro_corpus)};`, sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(NOTES_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STUDIO_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(PLAYLISTS_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(REPORT_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(SHARED_JS))}, "utf8"), sandbox);

// Load app.js without firing initApp() automatically so tests control boot timing
const appSrc = fs.readFileSync({json.dumps(str(APP_JS))}, "utf8").replace(/initApp\\(\\);\\s*$/, "");
vm.runInContext(appSrc, sandbox);

// Override network-heavy loaders & api() after loading modules
vm.runInContext(`
  loadHubNotes = () => {{ _counters.loadHubNotes++; }};
  loadPlaylists = async () => {{ _counters.loadPlaylists++; }};
  renderPlaylistsManager = () => {{}};
  renderWatchSidePanel = () => {{}};
  mountWatchItem = async () => {{}};
  applyStudioHeader = () => {{}};
  renderMatchAndRoundTabs = () => {{}};
  renderStudioSetMetaCard = () => {{}};
  loadRound = () => {{}};
  renderNotesList = () => {{}};
  renderSharedMomentView = () => {{}};
  renderSharedPlaylistView = () => {{}};
  api = async (url) => {{
    if (url.includes("/api/replays/") && url.includes("/manifest")) {{
      const qid = decodeURIComponent(url.split("/api/replays/")[1].split("/manifest")[0]);
      _counters.openMatchApi.push(qid);
      return {{
        manifest: {{
          quark_id: qid,
          perspective: 1,
          self_player_name: "p1",
          opp_player_name: "p2",
          matches: [{{ match_id: 1, round_ids: [1], self_rounds_won: 1, opp_rounds_won: 0 }}],
          rounds: [{{ round_id: 1, match_id: 1, match_round: 1, start_gf: 1000, end_gf: 2000, frame_count: 1001, fps: 60, video_url: "/r1.mp4" }}],
        }},
        annotations: [],
        select_marks: [],
        set_note: {{ note: "", tags: [] }},
      }};
    }}
    if (url.startsWith("/api/reports/")) {{
      const rid = decodeURIComponent(url.slice("/api/reports/".length));
      _counters.openReportApi.push(rid);
      return {{ report: {{ id: rid, title: "Weekly Checkup", summary: "ok", sections: [] }} }};
    }}
    if (url.startsWith("/api/share/moments/")) {{
      const mid = decodeURIComponent(url.slice("/api/share/moments/".length));
      _counters.openSharedMomentApi.push(mid);
      return {{ moment: {{ id: mid, quark_id: "q_shared", note: "shared", start_gf: 1000, end_gf: 1200 }} }};
    }}
    if (url.startsWith("/api/share/playlists/")) {{
      const pid = decodeURIComponent(url.slice("/api/share/playlists/".length));
      _counters.openSharedPlaylistApi.push(pid);
      return {{ playlist: {{ id: pid, name: "Shared Set", items: [] }} }};
    }}
    return {{ ok: true }};
  }};
`, sandbox);
"""


def test_history_navigation_stack_back_forward_and_studio_back():
    script = _build_vm_harness(initial_url="/") + """
(async () => {
  const out = await vm.runInContext(`(async () => {
    await initApp();
    const afterBoot = {
      activeView: state.activeView,
      depth: state._historyDepth,
      stackLen: _historyStack.length,
      idx: _getHistoryIdx(),
      url: _historyStack[_getHistoryIdx()].url,
      topState: _historyStack[_getHistoryIdx()].state,
      pushCalls: _getPushCalls(),
    };

    // Home -> Matches -> Studio
    navigateTo("matches");
    await openMatch("q_set_1");
    const afterStudio = {
      activeView: state.activeView,
      depth: state._historyDepth,
      stackLen: _historyStack.length,
      idx: _getHistoryIdx(),
      urls: _historyStack.map(e => e.url),
      pushCalls: _getPushCalls(),
    };

    // Android Back swipe #1: Studio -> Matches (popstate + hashchange should NOT push duplicates)
    const pushesBeforeBack = _getPushCalls();
    const loadMatchesBeforeBack = _counters.loadMatches;
    window.history.back();
    const afterBack1 = {
      activeView: state.activeView,
      depth: state._historyDepth,
      idx: _getHistoryIdx(),
      url: _historyStack[_getHistoryIdx()].url,
      pushDelta: _getPushCalls() - pushesBeforeBack,
      loadMatchesDelta: _counters.loadMatches - loadMatchesBeforeBack,
    };

    // Android Back swipe #2: Matches -> Home
    window.history.back();
    const afterBack2 = {
      activeView: state.activeView,
      depth: state._historyDepth,
      idx: _getHistoryIdx(),
      url: _historyStack[_getHistoryIdx()].url,
      pushDelta: _getPushCalls() - pushesBeforeBack,
    };

    // Browser Forward #1 & #2: Home -> Matches -> Studio
    window.history.forward();
    const afterFwd1 = { activeView: state.activeView, depth: state._historyDepth, idx: _getHistoryIdx() };
    window.history.forward();
    await Promise.resolve();
    await Promise.resolve();
    const afterFwd2 = {
      activeView: state.activeView,
      depth: state._historyDepth,
      idx: _getHistoryIdx(),
      stackLen: _historyStack.length,
      pushDelta: _getPushCalls() - pushesBeforeBack,
    };

    // Topbar Back button when depth > 0 delegates to history.back()
    const backCallsBeforeBtn = _getBackCalls();
    goBackFromStudio();
    const afterTopbarBack = {
      activeView: state.activeView,
      depth: state._historyDepth,
      idx: _getHistoryIdx(),
      backCallsDelta: _getBackCalls() - backCallsBeforeBtn,
    };

    return { afterBoot, afterStudio, afterBack1, afterBack2, afterFwd1, afterFwd2, afterTopbarBack };
  })()`, sandbox);
  console.log(JSON.stringify(out));
})();
"""
    res = _run_node(script)

    assert res["afterBoot"] == {
        "activeView": "home",
        "depth": 0,
        "stackLen": 1,
        "idx": 0,
        "url": "/",
        "topState": {"fcApp": True, "depth": 0},
        "pushCalls": 0,
    }
    assert res["afterStudio"]["activeView"] == "studio"
    assert res["afterStudio"]["depth"] == 2
    assert res["afterStudio"]["urls"] == ["/", "/#matches", "/#replay/q_set_1"]
    assert res["afterStudio"]["pushCalls"] == 2

    # First Back swipe unwinds Studio -> Matches with zero extra pushState calls and single loadMatches
    assert res["afterBack1"] == {
        "activeView": "matches",
        "depth": 1,
        "idx": 1,
        "url": "/#matches",
        "pushDelta": 0,
        "loadMatchesDelta": 1,
    }
    # Second Back swipe unwinds Matches -> Home
    assert res["afterBack2"] == {
        "activeView": "home",
        "depth": 0,
        "idx": 0,
        "url": "/",
        "pushDelta": 0,
    }

    # Forward restores Matches -> Studio without growing history stack
    assert res["afterFwd1"] == {"activeView": "matches", "depth": 1, "idx": 1}
    assert res["afterFwd2"] == {
        "activeView": "studio",
        "depth": 2,
        "idx": 2,
        "stackLen": 3,
        "pushDelta": 0,
    }

    # goBackFromStudio() calls window.history.back() when depth > 0
    assert res["afterTopbarBack"] == {
        "activeView": "matches",
        "depth": 1,
        "idx": 1,
        "backCallsDelta": 1,
    }


def test_direct_deep_link_topbar_back_falls_back_to_return_view():
    script = _build_vm_harness(initial_url="/#replay/q_deep_1") + """
(async () => {
  const out = await vm.runInContext(`(async () => {
    await initApp();
    await Promise.resolve();
    await Promise.resolve();
    const afterDeepBoot = {
      activeView: state.activeView,
      depth: state._historyDepth,
      stackLen: _historyStack.length,
      url: _historyStack[_getHistoryIdx()].url,
    };

    // Depth is 0 -> goBackFromStudio() should navigateTo(returnView || "matches") instead of history.back()
    const backCallsBefore = _getBackCalls();
    goBackFromStudio();
    const afterFallbackBack = {
      activeView: state.activeView,
      depth: state._historyDepth,
      stackLen: _historyStack.length,
      url: _historyStack[_getHistoryIdx()].url,
      backCallsDelta: _getBackCalls() - backCallsBefore,
    };
    return { afterDeepBoot, afterFallbackBack };
  })()`, sandbox);
  console.log(JSON.stringify(out));
})();
"""
    res = _run_node(script)
    assert res["afterDeepBoot"] == {
        "activeView": "studio",
        "depth": 0,
        "stackLen": 1,
        "url": "/#replay/q_deep_1",
    }
    assert res["afterFallbackBack"] == {
        "activeView": "home",
        "depth": 1,
        "stackLen": 2,
        "url": "/#home",
        "backCallsDelta": 0,
    }


def test_playlists_single_history_entry_and_subtab_unwind():
    script = _build_vm_harness(initial_url="/") + """
(async () => {
  const out = await vm.runInContext(`(async () => {
    await initApp();
    navigateTo("playlists");
    const afterPlaylists = {
      activeView: state.activeView,
      notesSubView: state.notesSubView,
      depth: state._historyDepth,
      urls: _historyStack.map(e => e.url),
      pushCalls: _getPushCalls(),
    };

    // Switch sub-tab from Playlists -> Notebook
    setNotesSubView("notebook");
    const afterNotebook = {
      activeView: state.activeView,
      notesSubView: state.notesSubView,
      depth: state._historyDepth,
      urls: _historyStack.map(e => e.url),
    };

    // Back swipe #1: Notebook -> Playlists
    window.history.back();
    const afterBack1 = {
      activeView: state.activeView,
      notesSubView: state.notesSubView,
      depth: state._historyDepth,
      url: _historyStack[_getHistoryIdx()].url,
    };

    // Back swipe #2: Playlists -> Home
    window.history.back();
    const afterBack2 = {
      activeView: state.activeView,
      depth: state._historyDepth,
      url: _historyStack[_getHistoryIdx()].url,
    };

    return { afterPlaylists, afterNotebook, afterBack1, afterBack2 };
  })()`, sandbox);
  console.log(JSON.stringify(out));
})();
"""
    res = _run_node(script)
    assert res["afterPlaylists"] == {
        "activeView": "notes",
        "notesSubView": "playlists",
        "depth": 1,
        "urls": ["/", "/#playlists"],
        "pushCalls": 1,
    }
    assert res["afterNotebook"] == {
        "activeView": "notes",
        "notesSubView": "notebook",
        "depth": 2,
        "urls": ["/", "/#playlists", "/#notes"],
    }
    assert res["afterBack1"] == {
        "activeView": "notes",
        "notesSubView": "playlists",
        "depth": 1,
        "url": "/#playlists",
    }
    assert res["afterBack2"] == {
        "activeView": "home",
        "depth": 0,
        "url": "/",
    }


def test_watch_modal_and_mobile_menu_back_swipe_and_open_in_studio():
    script = _build_vm_harness(initial_url="/") + """
(async () => {
  const out = await vm.runInContext(`(async () => {
    await initApp();
    navigateTo("notes");
    const clip = {
      item_id: "ann_1",
      quark_id: "q_clip_1",
      round_id: 1,
      anchor_gf: 1200,
      start_gf: 1100,
      end_gf: 1300,
      perspective: 1,
      note: "Punish sweep",
    };

    // 1. Open Moment Viewer -> pushes watch modal sentinel
    openSingleClipWatchModal(clip, "Moment Viewer");
    const afterOpenWatch = {
      watchOpen: Boolean(state.watchPlaylistId),
      modalHidden: _elements["watchModal"].classList.contains("hidden"),
      depth: state._historyDepth,
      activeModal: state._activeModalHistory,
      topState: _historyStack[_getHistoryIdx()].state,
      url: _historyStack[_getHistoryIdx()].url,
    };

    // Back swipe closes #watchModal without leaving Notes or reloading hub notes
    const hubLoadsBeforeBack = _counters.loadHubNotes;
    window.history.back();
    const afterWatchBack = {
      activeView: state.activeView,
      watchOpen: Boolean(state.watchPlaylistId),
      modalHidden: _elements["watchModal"].classList.contains("hidden"),
      depth: state._historyDepth,
      activeModal: state._activeModalHistory,
      hubLoadsDelta: _counters.loadHubNotes - hubLoadsBeforeBack,
    };

    // 2. Open #watchModal again, close via X button -> pops sentinel cleanly without re-routing
    openSingleClipWatchModal(clip, "Moment Viewer");
    const hubLoadsBeforeCloseBtn = _counters.loadHubNotes;
    closeWatchMode();
    const afterWatchCloseBtn = {
      activeView: state.activeView,
      watchOpen: Boolean(state.watchPlaylistId),
      depth: state._historyDepth,
      idx: _getHistoryIdx(),
      activeModal: state._activeModalHistory,
      hubLoadsDelta: _counters.loadHubNotes - hubLoadsBeforeCloseBtn,
    };

    // 3. Open #watchModal -> click "Open in Studio": replaces modal sentinel with /#replay/q_clip_1
    openSingleClipWatchModal(clip, "Moment Viewer");
    await openWatchItemInStudio();
    const afterWatchToStudio = {
      activeView: state.activeView,
      watchOpen: Boolean(state.watchPlaylistId),
      depth: state._historyDepth,
      urls: _historyStack.map(e => e.url),
      activeModal: state._activeModalHistory,
    };

    // Back from Studio returns directly to Notes (not the modal!)
    window.history.back();
    const afterStudioBackToNotes = {
      activeView: state.activeView,
      watchOpen: Boolean(state.watchPlaylistId),
      depth: state._historyDepth,
      url: _historyStack[_getHistoryIdx()].url,
    };

    // 4. Open mobile hamburger drawer -> Back swipe closes drawer while staying on Notes
    toggleMobileNavMenu();
    const afterMenuOpen = {
      menuOpen: isMobileNavMenuOpen(),
      depth: state._historyDepth,
      activeModal: state._activeModalHistory,
      topState: _historyStack[_getHistoryIdx()].state,
    };
    window.history.back();
    const afterMenuBack = {
      activeView: state.activeView,
      menuOpen: isMobileNavMenuOpen(),
      depth: state._historyDepth,
      activeModal: state._activeModalHistory,
    };

    // 5. Open mobile drawer -> tap Matches: replaces drawer sentinel cleanly with /#matches
    toggleMobileNavMenu();
    navigateTo("matches");
    const afterMenuNavMatches = {
      activeView: state.activeView,
      menuOpen: isMobileNavMenuOpen(),
      depth: state._historyDepth,
      urls: _historyStack.map(e => e.url),
      activeModal: state._activeModalHistory,
    };
    window.history.back();
    const afterMatchesBackToNotes = {
      activeView: state.activeView,
      depth: state._historyDepth,
      url: _historyStack[_getHistoryIdx()].url,
    };

    return {
      afterOpenWatch,
      afterWatchBack,
      afterWatchCloseBtn,
      afterWatchToStudio,
      afterStudioBackToNotes,
      afterMenuOpen,
      afterMenuBack,
      afterMenuNavMatches,
      afterMatchesBackToNotes,
    };
  })()`, sandbox);
  console.log(JSON.stringify(out));
})();
"""
    res = _run_node(script)

    assert res["afterOpenWatch"] == {
        "watchOpen": True,
        "modalHidden": False,
        "depth": 2,
        "activeModal": "watch",
        "topState": {"fcApp": True, "fcModal": "watch", "depth": 2},
        "url": "/#notes",
    }
    assert res["afterWatchBack"] == {
        "activeView": "notes",
        "watchOpen": False,
        "modalHidden": True,
        "depth": 1,
        "activeModal": None,
        "hubLoadsDelta": 0,
    }
    assert res["afterWatchCloseBtn"] == {
        "activeView": "notes",
        "watchOpen": False,
        "depth": 1,
        "idx": 1,
        "activeModal": None,
        "hubLoadsDelta": 0,
    }
    assert res["afterWatchToStudio"] == {
        "activeView": "studio",
        "watchOpen": False,
        "depth": 2,
        "urls": ["/", "/#notes", "/#replay/q_clip_1"],
        "activeModal": None,
    }
    assert res["afterStudioBackToNotes"] == {
        "activeView": "notes",
        "watchOpen": False,
        "depth": 1,
        "url": "/#notes",
    }
    assert res["afterMenuOpen"] == {
        "menuOpen": True,
        "depth": 2,
        "activeModal": "mobile_menu",
        "topState": {"fcApp": True, "fcModal": "mobile_menu", "depth": 2},
    }
    assert res["afterMenuBack"] == {
        "activeView": "notes",
        "menuOpen": False,
        "depth": 1,
        "activeModal": None,
    }
    assert res["afterMenuNavMatches"] == {
        "activeView": "matches",
        "menuOpen": False,
        "depth": 2,
        "urls": ["/", "/#notes", "/#matches"],
        "activeModal": None,
    }
    assert res["afterMatchesBackToNotes"] == {
        "activeView": "notes",
        "depth": 1,
        "url": "/#notes",
    }


def test_report_and_shared_routes_push_and_unwind():
    # Local mode: report + hash-based shared moment/playlist
    script_local = _build_vm_harness(initial_url="/", hosted=False, pro_corpus=True) + """
(async () => {
  const out = await vm.runInContext(`(async () => {
    await initApp();
    navigateTo("insights");
    await openReport("rep_42");
    await openSharedMoment("m_abc");
    await openSharedPlaylist("p_xyz");
    const urls = _historyStack.map(e => e.url);
    const depthBeforeBack = state._historyDepth;

    window.history.back();
    await Promise.resolve();
    const afterBack1 = { activeView: state.activeView, depth: state._historyDepth };

    window.history.back();
    await Promise.resolve();
    const afterBack2 = { activeView: state.activeView, depth: state._historyDepth };

    window.history.back();
    const afterBack3 = { activeView: state.activeView, depth: state._historyDepth };

    return { urls, depthBeforeBack, afterBack1, afterBack2, afterBack3 };
  })()`, sandbox);
  console.log(JSON.stringify(out));
})();
"""
    res_local = _run_node(script_local)
    assert res_local["urls"] == ["/", "/#insights", "/#report/rep_42", "/#m/m_abc", "/#p/p_xyz"]
    assert res_local["depthBeforeBack"] == 4
    assert res_local["afterBack1"] == {"activeView": "shared_moment", "depth": 3}
    assert res_local["afterBack2"] == {"activeView": "report", "depth": 2}
    assert res_local["afterBack3"] == {"activeView": "insights", "depth": 1}

    # Hosted mode: clean /m/:id and /p/:id URLs
    script_hosted = _build_vm_harness(initial_url="/", hosted=True, pro_corpus=False) + """
(async () => {
  const out = await vm.runInContext(`(async () => {
    await initApp();
    await openSharedMoment("m_cloud");
    await openSharedPlaylist("p_cloud");
    return { urls: _historyStack.map(e => e.url), depth: state._historyDepth };
  })()`, sandbox);
  console.log(JSON.stringify(out));
})();
"""
    res_hosted = _run_node(script_hosted)
    assert res_hosted == {
        "urls": ["/", "/m/m_cloud", "/p/p_cloud"],
        "depth": 2,
    }
