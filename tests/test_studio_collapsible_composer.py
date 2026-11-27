"""Tests for the unified collapsible + New Moment -> Save Moment flow in Replay Studio."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
INDEX_HTML = WEB_DIR / "index.html"
STATE_JS = WEB_DIR / "js" / "state.js"
STUDIO_JS = WEB_DIR / "js" / "studio.js"
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


def test_collapsible_composer_markup_and_shortcuts():
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_src = APP_JS.read_text(encoding="utf-8")

    # Collapsible composer starts hidden; + Pin Moment replaced by + New Moment
    assert 'id="composerCard" class="composer-card hidden"' in html
    assert "+ Pin Moment" not in html
    assert 'id="btnNewMomentTransport"' in html
    assert 'id="btnNewMomentSidebar"' in html
    assert "+ New Moment</button>" in html
    assert 'onclick="openComposer()"' in html
    assert 'onclick="closeComposer()"' in html
    assert ">Cancel</button>" in html
    assert 'id="btnSaveNote"' in html and ">Save Moment</button>" in html
    assert 'id="composerLoopRow"' not in html
    assert 'id="composerLoopBadge"' not in html

    # Keyboard shortcuts: M / N opens composer; Escape closes pickers BEFORE closeComposer
    assert 'e.key === "m" || e.key === "M" || e.key === "n" || e.key === "N"' in app_src
    assert "openComposer()" in app_src
    esc_block = app_src[app_src.index('if (e.key === "Escape")'):]
    assert "closeComposer(true)" in esc_block
    assert esc_block.index("closeAllTagPickers()") < esc_block.index("closeComposer(true)")
    assert esc_block.index("closeAllCollectionPickers()") < esc_block.index("closeComposer(true)")


def test_collapsible_composer_lifecycle_and_visible_loop_sync():
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
      disabled: false,
      style: {{}},
      dataset: {{}},
      width: 768,
      height: 576,
      scrollHeight: 80,
      scrollWidth: 100,
      clientWidth: 100,
      scrollLeft: 0,
      _attrs: {{}},
      _listeners: {{}},
      focused: false,
      scrolledIntoView: false,
      classList: {{
        _set: new Set(["composerCard", "studioNextRoundOverlay", "practiceLoopBar"].includes(id) ? ["hidden"] : []),
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
      addEventListener(ev, fn) {{
        if (!this._listeners[ev]) this._listeners[ev] = [];
        this._listeners[ev].push(fn);
      }},
      focus() {{ this.focused = true; }},
      setSelectionRange() {{}},
      scrollIntoView() {{ this.scrolledIntoView = true; }},
      appendChild() {{}},
      closest: () => null,
      getContext() {{
        return {{
          clearRect() {{}}, fillRect() {{}}, beginPath() {{}}, moveTo() {{}},
          lineTo() {{}}, stroke() {{}}, fill() {{}}, arc() {{}}, arcTo() {{}},
          closePath() {{}}, save() {{}}, restore() {{}}, setLineDash() {{}},
        }};
      }},
      querySelectorAll: () => [],
      getBoundingClientRect: () => ({{ left: 0, top: 0, width: 400, height: 76 }}),
    }};
  }}
  return elements[id];
}}

const videoEl = makeEl("roundVideo");
videoEl.paused = false;
videoEl.currentTime = 5.0;
videoEl.playbackRate = 1;
videoEl.muted = false;
videoEl.volume = 0.7;
videoEl.src = "";
videoEl.pause = function() {{ this.paused = true; }};
videoEl.load = function() {{}};
videoEl.play = function() {{ this.paused = false; return Promise.resolve(); }};

const apiCalls = [];
const sandbox = {{
  console,
  window: {{
    innerWidth: 390,
    location: {{ origin: "http://localhost:8765", search: "", hash: "#replay/q1", pathname: "/" }},
    addEventListener: () => {{}},
    scrollTo: () => {{}},
  }},
  document: {{
    activeElement: null,
    documentElement: {{ getAttribute: () => "dark" }},
    getElementById: (id) => makeEl(id),
    createElement: () => ({{ className: "", textContent: "", setAttribute() {{}} }}),
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
  setInterval: () => 1,
  clearInterval: () => {{}},
  setTimeout: () => 0,
  clearTimeout: () => {{}},
  syncOverlayButtons: () => {{}},
  drawOverlayOnCanvas: () => {{}},
  syncGrouflonGuideUi: () => {{}},
  loadHubNotes: () => {{}},
  loadPlaylists: () => Promise.resolve(),
  _elements: elements,
  _videoEl: videoEl,
  _apiCalls: apiCalls,
}};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STUDIO_JS))}, "utf8"), sandbox);

(async () => {{
  const out = await vm.runInContext(`(async () => {{
    // Stub api() to record POST /api/annotations
    api = async (url, opts) => {{
      const body = opts && opts.body ? JSON.parse(opts.body) : null;
      _apiCalls.push({{ url, method: (opts && opts.method) || "GET", body }});
      return {{
        ok: true,
        annotation: {{
          id: "ann_new_1",
          quark_id: body.quark_id,
          round_id: body.round_id,
          perspective: body.perspective,
          anchor_gf: body.anchor_gf,
          start_gf: body.start_gf,
          end_gf: body.end_gf,
          kind: body.kind,
          tags: body.tags,
          note: body.note,
        }},
      }};
    }};

    state.activeView = "studio";
    state.activeManifest = {{
      quark_id: "q_comp",
      perspective: 1,
      self_player_name: "blankasballs",
      opp_player_name: "rival",
      matches: [{{ match_id: 1, round_ids: [1], self_rounds_won: 1, opp_rounds_won: 0 }}],
      rounds: [
        {{ round_id: 1, match_id: 1, match_round: 1, start_gf: 1000, end_gf: 2200, frame_count: 1201, fps: 60, video_url: "/r1.mp4", self_char_name: "Akuma", opp_char_name: "Ken" }},
      ],
    }};
    state.activeRoundIdx = 0;

    const initialComposerOpen = state.composerOpen;
    const initialCardHidden = document.getElementById("composerCard").classList.contains("hidden");

    // 1. openComposer() pauses video, reveals card, hides sidebar CTA, drops visible loop, focuses textarea, scrolls on mobile
    openComposer();
    const afterOpen = {{
      composerOpen: state.composerOpen,
      cardHidden: _elements["composerCard"].classList.contains("hidden"),
      sidebarBtnRowHidden: _elements["sidebarNewMomentRow"].classList.contains("hidden"),
      transportExpanded: _elements["btnNewMomentTransport"].getAttribute("aria-expanded"),
      paused: _videoEl.paused,
      hasLoop: hasActiveLoopRange(),
      rangeStartGf: state.rangeStartGf,
      rangeEndGf: state.rangeEndGf,
      anchorText: _elements["composerAnchor"].textContent,
      taFocused: _elements["noteTextarea"].focused,
      scrolledMobile: _elements["composerCard"].scrolledIntoView,
    }};

    // 2. Clearing loop with X while composer is open shows "· ±2s auto"
    clearRange();
    const anchorAfterClear = _elements["composerAnchor"].textContent;

    // Clicking anchor recenterComposerLoop() restores visible loop
    recenterComposerLoop();
    const hasLoopAfterRecenter = hasActiveLoopRange();

    // 3. Escape keydown on textarea closes open tag picker first, then closes composer on second Escape
    toggleTagPickerMenu("composer");
    onComposerTextareaKeydown({{ key: "Escape", preventDefault() {{}}, stopPropagation() {{}} }});
    const openAfterFirstEsc = state.composerOpen;
    const tagPickerOpenAfterFirstEsc = hasOpenTagPicker();
    onComposerTextareaKeydown({{ key: "Escape", preventDefault() {{}}, stopPropagation() {{}} }});
    const afterSecondEsc = {{
      composerOpen: state.composerOpen,
      cardHidden: _elements["composerCard"].classList.contains("hidden"),
      sidebarBtnRowHidden: _elements["sidebarNewMomentRow"].classList.contains("hidden"),
      hasLoop: hasActiveLoopRange(),
    }};

    // 4. Open again, type note, Ctrl+Enter saves with visible loop bounds and collapses composer + clears loop
    openComposer();
    _elements["noteTextarea"].value = "Anti-air crouch fierce here";
    await saveAnnotation();
    const afterSave = {{
      composerOpen: state.composerOpen,
      cardHidden: _elements["composerCard"].classList.contains("hidden"),
      hasLoop: hasActiveLoopRange(),
      savedPayload: _apiCalls[0] ? _apiCalls[0].body : null,
    }};

    // 5. Starting inline edit on saved card collapses an open composer cleanly
    openComposer();
    startStudioInlineEdit("ann_new_1", "annotation");
    const afterStartInlineEdit = {{
      composerOpen: state.composerOpen,
      cardHidden: _elements["composerCard"].classList.contains("hidden"),
      studioEditId: state.studioEditId,
    }};

    return {{
      initialComposerOpen,
      initialCardHidden,
      afterOpen,
      anchorAfterClear,
      hasLoopAfterRecenter,
      openAfterFirstEsc,
      tagPickerOpenAfterFirstEsc,
      afterSecondEsc,
      afterSave,
      afterStartInlineEdit,
    }};
  }})()`, sandbox);
  console.log(JSON.stringify(out));
}})();
"""
    res = _run_node(script)

    assert res["initialComposerOpen"] is False
    assert res["initialCardHidden"] is True

    ao = res["afterOpen"]
    assert ao["composerOpen"] is True
    assert ao["cardHidden"] is False
    assert ao["sidebarBtnRowHidden"] is True
    assert ao["transportExpanded"] == "true"
    assert ao["paused"] is True
    assert ao["hasLoop"] is True
    assert ao["rangeStartGf"] == 1180  # 1300 - 120
    assert ao["rangeEndGf"] == 1390    # 1300 + 90
    assert "(" in ao["anchorText"] and "s)" in ao["anchorText"]
    assert ao["taFocused"] is True
    assert ao["scrolledMobile"] is True

    assert "±2s auto" in res["anchorAfterClear"]
    assert res["hasLoopAfterRecenter"] is True

    # First Esc closes tag picker; second Esc closes composer & clears loop
    assert res["tagPickerOpenAfterFirstEsc"] is False
    assert res["openAfterFirstEsc"] is True
    assert res["afterSecondEsc"] == {
        "composerOpen": False,
        "cardHidden": True,
        "sidebarBtnRowHidden": False,
        "hasLoop": False,
    }

    # Save sends explicit start_gf / end_gf and collapses composer + clears loop
    asv = res["afterSave"]
    assert asv["composerOpen"] is False
    assert asv["cardHidden"] is True
    assert asv["hasLoop"] is False
    assert asv["savedPayload"]["note"] == "Anti-air crouch fierce here"
    assert asv["savedPayload"]["start_gf"] == 1180
    assert asv["savedPayload"]["end_gf"] == 1390

    # Inline edit collapses composer automatically
    assert res["afterStartInlineEdit"] == {
        "composerOpen": False,
        "cardHidden": True,
        "studioEditId": "ann_new_1",
    }
