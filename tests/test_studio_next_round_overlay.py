"""Tests for Replay Studio YouTube-style 'Up Next' & 'Set Complete' round overlay."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
INDEX_HTML = WEB_DIR / "index.html"
STYLES_CSS = WEB_DIR / "styles.css"
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


def test_studio_next_round_overlay_markup_css_and_shortcuts():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    app_src = APP_JS.read_text(encoding="utf-8")

    # Overlay container sits inside .video-stage immediately after #overlayCanvas
    stage_m = re.search(
        r'<div class="video-stage">(.*?)</div>\s*<div class="studio-video-bar"',
        html,
        flags=re.DOTALL,
    )
    assert stage_m is not None, "Could not locate .video-stage block in index.html"
    stage_inner = stage_m.group(1)
    assert 'id="overlayCanvas"' in stage_inner
    assert 'id="studioNextRoundOverlay"' in stage_inner
    assert stage_inner.index('id="overlayCanvas"') < stage_inner.index('id="studioNextRoundOverlay"')
    assert 'role="dialog"' in stage_inner
    assert 'aria-label="Next round"' in stage_inner

    # Responsive dark-player CSS classes present
    for cls in (
        ".studio-next-overlay",
        ".studio-next-card",
        ".studio-next-prog-track",
        ".studio-next-prog-fill",
        ".studio-next-actions",
        ".studio-next-toggle",
    ):
        assert cls in css, f"Missing CSS rule {cls}"

    # Keyboard shortcuts & cleanup wired in app.js
    assert 'e.shiftKey && e.key === "ArrowLeft"' in app_src
    assert "stepStudioRound(-1)" in app_src
    assert 'e.shiftKey && e.key === "ArrowRight"' in app_src
    assert "stepStudioRound(1)" in app_src
    assert "dismissStudioNextOverlay" in app_src


def test_studio_next_round_overlay_behavior_and_guardrails():
    script = f"""
const fs = require("fs");
const vm = require("vm");

const store = Object.create(null);
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
      width: 768,
      height: 576,
      scrollWidth: 100,
      clientWidth: 100,
      scrollLeft: 0,
      _attrs: {{}},
      _listeners: {{}},
      classList: {{
        _set: new Set(id === "studioNextRoundOverlay" ? ["hidden"] : []),
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
      dispatch(ev) {{
        (this._listeners[ev] || []).forEach(fn => fn({{}}));
      }},
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
videoEl.paused = true;
videoEl.currentTime = 0;
videoEl.playbackRate = 1;
videoEl.muted = false;
videoEl.volume = 0.7;
videoEl.src = "";
videoEl.playCalls = 0;
videoEl.loadCalls = 0;
videoEl.pause = function() {{ this.paused = true; }};
videoEl.load = function() {{ this.loadCalls += 1; this.playbackRate = 1; }};
videoEl.play = function() {{
  this.playCalls += 1;
  this.paused = false;
  this.dispatch("play");
  return Promise.resolve();
}};

let nowMs = 10000;
let nextIntervalId = 1;
const activeIntervals = new Map();

const doc = {{
  activeElement: null,
  documentElement: {{ getAttribute: () => "dark" }},
  getElementById: (id) => makeEl(id),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {{}},
}};

const sandbox = {{
  console,
  window: {{
    innerWidth: 390,
    location: {{ origin: "http://localhost:8765", search: "", hash: "#replay/q1", pathname: "/" }},
    addEventListener: () => {{}},
    scrollTo: () => {{}},
  }},
  document: doc,
  HTMLVideoElement: {{ prototype: {{}} }},
  localStorage: {{
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => {{ store[k] = String(v); }},
    removeItem: (k) => {{ delete store[k]; }},
  }},
  Date: {{ now: () => nowMs }},
  setInterval: (fn, ms) => {{
    const id = nextIntervalId++;
    activeIntervals.set(id, {{ fn, ms }});
    return id;
  }},
  clearInterval: (id) => {{ activeIntervals.delete(id); }},
  setTimeout: () => 0,
  clearTimeout: () => {{}},
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
  URLSearchParams,
  encodeURIComponent,
  syncOverlayButtons: () => {{}},
  drawOverlayOnCanvas: () => {{}},
  syncGrouflonGuideUi: () => {{}},
  isLightTheme: () => false,
  _elements: elements,
  _activeIntervals: activeIntervals,
  _videoEl: videoEl,
  _doc: doc,
  _store: store,
  _advanceClock: (ms) => {{
    nowMs += ms;
    for (const entry of [...activeIntervals.values()]) {{
      entry.fn();
    }}
  }},
}};
sandbox.window.document = doc;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STUDIO_JS))}, "utf8"), sandbox);

const out = vm.runInContext(`
  const defaultAutoplay = isStudioAutoplayNextEnabled();
  const countdownMs = STUDIO_NEXT_COUNTDOWN_MS;

  state.activeView = "studio";
  state.activeManifest = {{
    quark_id: "q_multi",
    perspective: 1,
    self_player_name: "blankasballs",
    opp_player_name: "rival",
    self_score: 2,
    opp_score: 1,
    in_progress: false,
    matches: [
      {{ match_id: 1, round_ids: [1, 2], self_rounds_won: 1, opp_rounds_won: 1, self_won: null }},
      {{ match_id: 2, round_ids: [3], self_rounds_won: 1, opp_rounds_won: 0, self_won: true }},
    ],
    rounds: [
      {{ round_id: 1, match_id: 1, match_round: 1, start_gf: 100, end_gf: 700, frame_count: 601, fps: 60, video_url: "/r1.mp4", self_char_name: "Akuma", opp_char_name: "Ken", self_won: true }},
      {{ round_id: 2, match_id: 1, match_round: 2, start_gf: 800, end_gf: 1400, frame_count: 601, fps: 60, video_url: "/r2.mp4", self_char_name: "Akuma", opp_char_name: "Chun-Li", self_won: false }},
      {{ round_id: 3, match_id: 2, match_round: 1, start_gf: 1500, end_gf: 2100, frame_count: 601, fps: 60, video_url: "/r3.mp4", self_char_name: "Akuma", opp_char_name: "Chun-Li", self_won: true }},
    ],
  }};
  state.activeRoundIdx = 0;

  // 1. Round 1 ends with auto-play OFF (default): overlay shown, no countdown timer
  _videoEl.dispatch("ended");
  const overlayOffHtml = _elements["studioNextRoundOverlay"].innerHTML;
  const overlayOffVisible = !_elements["studioNextRoundOverlay"].classList.contains("hidden");
  const intervalsWhenOff = _activeIntervals.size;

  // 2. Toggle auto-play ON while overlay is open -> persists "1" & starts 2s countdown
  setRate(0.5);
  toggleStudioAutoplayNext(true);
  const storedAfterOn = _store["fc_studio_autoplay_next"];
  const intervalsWhenOn = _activeIntervals.size;
  const countdownTextStart = _elements["studioNextCountdownText"].textContent;

  // Toggle OFF -> cancels timer, overlay remains visible
  toggleStudioAutoplayNext(false);
  const intervalsAfterToggleOff = _activeIntervals.size;
  const stillVisibleAfterToggleOff = !_elements["studioNextRoundOverlay"].classList.contains("hidden");

  // 3. Typing guardrail: TEXTAREA focused + auto-play ON -> pauses countdown & never advances
  _doc.activeElement = {{ tagName: "TEXTAREA" }};
  toggleStudioAutoplayNext(true);
  const intervalsWhileTyping = _activeIntervals.size;
  const typingPausedMsg = _elements["studioNextCountdownText"].textContent;
  _advanceClock(3000);
  const idxAfterTypingClock = state.activeRoundIdx;

  // Checkbox focus itself does NOT block countdown
  _doc.activeElement = {{ tagName: "INPUT", type: "checkbox" }};
  toggleStudioAutoplayNext(true);
  const intervalsWithCheckboxFocus = _activeIntervals.size;

  // Advance 2000ms -> automatically loads round index 1 and plays on loadedmetadata preserving 0.5x rate
  const playCallsBefore = _videoEl.playCalls;
  _advanceClock(2000);
  const idxAfterCountdown = state.activeRoundIdx;
  const overlayHiddenAfterAutoAdvance = _elements["studioNextRoundOverlay"].classList.contains("hidden");
  _videoEl.dispatch("loadedmetadata");
  const rateAfterMeta = _videoEl.playbackRate;
  const playCallsAfter = _videoEl.playCalls;

  // 4. Loop preview guard: active loop preview wraps instead of showing overlay
  state.rangeStartGf = 900;
  state.rangeEndGf = 1100;
  state.loopPreviewActive = true;
  _videoEl.dispatch("ended");
  const overlayDuringLoopPreview = !_elements["studioNextRoundOverlay"].classList.contains("hidden");
  state.loopPreviewActive = false;
  clearRange();

  // 5. Final round -> Set Complete card (no countdown)
  state.activeRoundIdx = 2;
  _videoEl.dispatch("ended");
  const setCompleteHtml = _elements["studioNextRoundOverlay"].innerHTML;
  const setCompleteVisible = !_elements["studioNextRoundOverlay"].classList.contains("hidden");
  const setCompleteIntervals = _activeIntervals.size;

  // in_progress final round -> should NOT show Set Complete
  dismissStudioNextOverlay();
  state.activeManifest.in_progress = true;
  _videoEl.dispatch("ended");
  const inProgressOverlayVisible = !_elements["studioNextRoundOverlay"].classList.contains("hidden");
  state.activeManifest.in_progress = false;

  // 6. Auto-dismiss on stepFrames & stepStudioRound auto-play inheritance
  state.activeRoundIdx = 0;
  setStudioAutoplayNextEnabled(false);
  _videoEl.dispatch("ended");
  stepFrames(-5);
  const overlayAfterStepFrames = !_elements["studioNextRoundOverlay"].classList.contains("hidden");

  // stepStudioRound(1) when overlay is open auto-plays next round
  _videoEl.dispatch("ended");
  const playBeforeStepRound = _videoEl.playCalls;
  const steppedNext = stepStudioRound(1);
  _videoEl.dispatch("loadedmetadata");
  const idxAfterStepRound = state.activeRoundIdx;
  const playAfterStepRound = _videoEl.playCalls;

  // 7. Single-match set omits "Match 1 ·" prefix
  state.activeManifest.matches = [{{ match_id: 1, round_ids: [1, 2], self_rounds_won: 1, opp_rounds_won: 1 }}];
  state.activeManifest.rounds = state.activeManifest.rounds.slice(0, 2);
  state.activeRoundIdx = 0;
  _videoEl.dispatch("ended");
  const singleMatchHtml = _elements["studioNextRoundOverlay"].innerHTML;

  ({{
    defaultAutoplay,
    countdownMs,
    overlayOffHtml,
    overlayOffVisible,
    intervalsWhenOff,
    storedAfterOn,
    intervalsWhenOn,
    countdownTextStart,
    intervalsAfterToggleOff,
    stillVisibleAfterToggleOff,
    intervalsWhileTyping,
    typingPausedMsg,
    idxAfterTypingClock,
    intervalsWithCheckboxFocus,
    idxAfterCountdown,
    overlayHiddenAfterAutoAdvance,
    rateAfterMeta,
    playDelta: playCallsAfter - playCallsBefore,
    overlayDuringLoopPreview,
    setCompleteHtml,
    setCompleteVisible,
    setCompleteIntervals,
    inProgressOverlayVisible,
    overlayAfterStepFrames,
    steppedNext,
    idxAfterStepRound,
    stepRoundPlayDelta: playAfterStepRound - playBeforeStepRound,
    singleMatchHtml,
  }});
`, sandbox);
console.log(JSON.stringify(out));
"""
    res = _run_node(script)

    # Defaults & countdown constant
    assert res["defaultAutoplay"] is False
    assert res["countdownMs"] == 2000

    # Up Next card on multi-match set with character switch (Ken -> Chun-Li)
    assert res["overlayOffVisible"] is True
    assert res["intervalsWhenOff"] == 0
    assert "UP NEXT" in res["overlayOffHtml"]
    assert "Match 1 · Round 2" in res["overlayOffHtml"]
    assert "Akuma vs Chun-Li" in res["overlayOffHtml"]
    assert "▶ Play Next" in res["overlayOffHtml"]
    assert "↺ Replay" in res["overlayOffHtml"]
    assert "Cancel" in res["overlayOffHtml"]
    assert "Auto-play next round" in res["overlayOffHtml"]

    # Toggle auto-play ON / OFF
    assert res["storedAfterOn"] == "1"
    assert res["intervalsWhenOn"] == 1
    assert "Playing in 2s" in res["countdownTextStart"]
    assert res["intervalsAfterToggleOff"] == 0
    assert res["stillVisibleAfterToggleOff"] is True

    # Typing guardrail blocks countdown when focused in TEXTAREA
    assert res["intervalsWhileTyping"] == 0
    assert res["typingPausedMsg"] == "Auto-play paused while typing"
    assert res["idxAfterTypingClock"] == 0

    # Checkbox focus allows countdown -> auto-advances after 2s and preserves 0.5x rate + plays
    assert res["intervalsWithCheckboxFocus"] == 1
    assert res["idxAfterCountdown"] == 1
    assert res["overlayHiddenAfterAutoAdvance"] is True
    assert res["rateAfterMeta"] == 0.5
    assert res["playDelta"] == 1

    # Loop preview suppresses overlay
    assert res["overlayDuringLoopPreview"] is False

    # Set Complete card on final round (and suppressed while in_progress)
    assert res["setCompleteVisible"] is True
    assert res["setCompleteIntervals"] == 0
    assert "SET COMPLETE" in res["setCompleteHtml"]
    assert "3 rounds · 2–1" in res["setCompleteHtml"]
    assert "↺ Replay Round" in res["setCompleteHtml"]
    assert "⏮ Restart Set" in res["setCompleteHtml"]
    assert "Dismiss" in res["setCompleteHtml"]
    assert res["inProgressOverlayVisible"] is False

    # Auto-dismiss on stepFrames & stepStudioRound auto-play
    assert res["overlayAfterStepFrames"] is False
    assert res["steppedNext"] is True
    assert res["idxAfterStepRound"] == 1
    assert res["stepRoundPlayDelta"] == 1

    # Single-match set title omits Match prefix
    assert "Round 2" in res["singleMatchHtml"]
    assert "Match 1 · Round 2" not in res["singleMatchHtml"]
