"""Tests for the About page (#about / /about) and final pre-launch UI cleanup sweep."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
INDEX_HTML = WEB_DIR / "index.html"
STYLES_CSS = WEB_DIR / "styles.css"
STATE_JS = WEB_DIR / "js" / "state.js"
HOME_JS = WEB_DIR / "js" / "home.js"
STUDIO_JS = WEB_DIR / "js" / "studio.js"
PLAYLISTS_JS = WEB_DIR / "js" / "playlists.js"
SHARED_JS = WEB_DIR / "js" / "shared.js"
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


def test_about_view_nav_footer_and_zero_em_dashes():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")

    # Top nav: Home · Matches · Notes · About, with Insights hidden by default
    assert 'id="navAbout"' in html
    assert "onclick=\"navigateTo('about')\">About</button>" in html
    assert 'id="navInsights" class="nav-tab pro-gated hidden"' in html

    # Footer starts with About button before Feedback / Bug Report
    footer_start = html.index('<div class="app-footer-links">')
    footer_end = html.index("</div>", footer_start)
    footer_links = html[footer_start:footer_end]
    assert "onclick=\"navigateTo('about')\">About</button>" in footer_links
    assert footer_links.index("navigateTo('about')") < footer_links.index("Feedback / Bug Report")

    # #viewAbout exists with banner, sections, links, no Credits section, and ZERO em dashes
    about_start = html.index('<section id="viewAbout"')
    about_end = html.index("</section>", about_start)
    about_html = html[about_start:about_end]

    assert "—" not in about_html, "About page copy must contain zero em dashes"
    assert "&mdash;" not in about_html.lower()
    assert "/assets/img/about-evo-moment37.jpg" in about_html
    assert "EVO 2004 · Pomona, CA" in about_html
    from PIL import Image
    evo_banner = WEB_DIR / "img" / "about-evo-moment37.jpg"
    assert evo_banner.is_file()
    with Image.open(evo_banner) as im:
        assert im.format == "JPEG"
        assert im.size == (960, 707)
    assert "About Fightcade Lab" in about_html
    assert "Why I built this" in about_html
    assert "Free as in beer, free as in speech" in about_html
    assert "Local-first and respectful of Fightcade" in about_html
    assert "<h2>Credits</h2>" not in about_html
    assert "Credits" not in about_html
    assert "https://github.com/blankasballs/fightcade-lab" in about_html
    assert "https://github.com/blankasballs/fightcade-lab/issues" in about_html
    assert "openTermsModal()" in about_html

    # Editorial styles & single-col pulse-grid present
    assert ".about-card {" in css
    assert ".about-banner-frame {" in css
    banner_img_start = css.index(".about-banner-img {")
    banner_img_end = css.index("}", banner_img_start)
    assert "object-fit: contain" in css[banner_img_start:banner_img_end]
    assert ".pulse-grid.single-col {" in css


def test_removed_studio_link_train_mode_loop_toggles_and_copy_polish():
    html = INDEX_HTML.read_text(encoding="utf-8")
    home_src = HOME_JS.read_text(encoding="utf-8")
    studio_src = STUDIO_JS.read_text(encoding="utf-8")
    playlists_src = PLAYLISTS_JS.read_text(encoding="utf-8")
    shared_src = SHARED_JS.read_text(encoding="utf-8")
    state_src = STATE_JS.read_text(encoding="utf-8")
    app_src = APP_JS.read_text(encoding="utf-8")

    # Studio 🔗 Link removed; ⇄ Flip P1/P2 stays
    assert 'id="btnShareLink"' not in html
    assert "copyStudioDeepLink" not in html
    assert "copyStudioDeepLink" not in studio_src
    assert 'id="btnFlipSide"' in html

    # Placeholder trimmed
    assert "(Voice dictation works great!)" not in html
    assert 'placeholder="What happened here? What should you try instead?"' in html

    # Separate Train Mode & Loop Current/Auto-Advance toggles removed across HTML & JS
    for needle in ("btnWatchSwitchTrain", "trainModal", "openTrainMode", "closeTrainMode", "switchWatchToTrain", "btnWatchLoopMode", "toggleWatchAutoAdvance", "btnSharedLoopToggle", "toggleSharedPlaylistAutoAdvance"):
        assert needle not in html, f"{needle} should be removed from index.html"
        assert needle not in home_src, f"{needle} should be removed from home.js"
        assert needle not in playlists_src, f"{needle} should be removed from playlists.js"
        assert needle not in shared_src, f"{needle} should be removed from shared.js"
        assert needle not in app_src, f"{needle} should be removed from app.js"

    assert "watchAutoAdvance" not in state_src
    assert "sharedPlaylistAutoAdvance" not in state_src

    # Direct ▶ Practice inside Watch Mode stays intact
    assert "async function launchPlaylistItemTakeover(" in playlists_src
    assert "launchPlaylistItemTakeover(" in playlists_src
    assert "▶ Practice" in playlists_src

    # Public Home never prints FC_ENABLE_PRO_CORPUS=1
    assert "FC_ENABLE_PRO_CORPUS=1" not in home_src


def test_pro_corpus_gate_hiding_and_about_routing_runtime():
    script = f"""
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

const loc = parseUrl("/");
loc.origin = "http://localhost:8765";
const historyStack = [{{ state: null, url: "/" }}];
let historyIdx = 0;
const winListeners = Object.create(null);

function applyLoc(url) {{
  const p = parseUrl(url);
  loc.pathname = p.pathname;
  loc.search = p.search;
  loc.hash = p.hash;
}}

const historyObj = {{
  pushState(st, _t, url) {{
    historyStack.splice(historyIdx + 1);
    historyStack.push({{ state: st, url: String(url) }});
    historyIdx = historyStack.length - 1;
    applyLoc(url);
  }},
  replaceState(st, _t, url) {{
    historyStack[historyIdx] = {{ state: st, url: String(url) }};
    applyLoc(url);
  }},
  back() {{
    if (historyIdx <= 0) return;
    historyIdx--;
    const e = historyStack[historyIdx];
    applyLoc(e.url);
    (winListeners["popstate"] || []).forEach(fn => fn({{ state: e.state }}));
  }},
}};

const elements = Object.create(null);
function makeEl(id) {{
  if (!elements[id]) {{
    const initHidden = ["navInsights", "sideTabProSearch", "viewAbout", "viewMatches", "viewNotes", "viewInsights", "viewStudio", "viewReport", "viewShared", "studioBreadcrumb", "crumbMatchup", "mobileNavMenu", "mobileNavBackdrop", "watchModal"].includes(id);
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
      pause() {{}},
      load() {{}},
      play() {{ return Promise.resolve(); }},
      getContext() {{ return {{}}; }},
      querySelector: () => null,
      querySelectorAll: () => [],
    }};
  }}
  return elements[id];
}}

const sandbox = {{
  console,
  window: {{
    innerWidth: 1200,
    __FC_BOOTSTRAP__: {{ mode: "local_companion", initialRoute: "" }},
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
    documentElement: {{ getAttribute: () => "dark", setAttribute: () => {{}}, classList: {{ toggle() {{}} }} }},
    body: {{ classList: {{ toggle() {{}} }} }},
    getElementById: (id) => makeEl(id),
    querySelector: () => null,
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
  loadCapabilities: async () => {{}},
  loadAuthState: async () => {{}},
  loadFocusGoals: () => {{}},
  loadPlaylists: async () => {{}},
  loadHubNotes: () => {{}},
  loadMatches: () => {{}},
  loadInsights: () => {{}},
  checkForNewTakeoverResponses: () => {{}},
  _elements: elements,
  _historyStack: historyStack,
}};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);

vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(HOME_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STUDIO_JS))}, "utf8"), sandbox);
const appSrc = fs.readFileSync({json.dumps(str(APP_JS))}, "utf8").replace(/initApp\\(\\);\\s*$/, "");
vm.runInContext(appSrc, sandbox);
vm.runInContext(`loadHomeWorkbench = () => {{}};`, sandbox);

(async () => {{
  const out = await vm.runInContext(`(async () => {{
    // 1. Default public mode (pro_corpus_enabled = false)
    state.capabilities.pro_corpus_enabled = false;
    syncProCorpusGateUi();
    const publicGate = {{
      navInsightsHidden: _elements["navInsights"].classList.contains("hidden"),
      sideTabProHidden: _elements["sideTabProSearch"].classList.contains("hidden"),
    }};

    // Home workbench omits AI Checkup card & uses single-col pulse-grid
    state.homeData = {{
      latest_match: {{ quark_id: "q1", status: "ready", self_player_name: "p1", opp_player_name: "p2", self_char_name: "Akuma", opp_char_name: "Ken", round_count: 2 }},
      focus_goals: {{ active: [], max_active: 3, graduated_count: 0 }},
      playlists: [],
      pulse: {{ notes_total: 4, matchups_count: 2, latest_insight: null }},
    }};
    renderHomeWorkbench();
    const homePublicHtml = _elements["homeContent"].innerHTML;

    // Studio Set Meta card omits AI Match Breakdown button when disabled
    state.activeManifest = {{ quark_id: "q1", perspective: 1, round_count: 3, total_duration_s: 95, matches: [] }};
    renderStudioSetMetaCard();
    const setMetaPublicHtml = _elements["studioSetMetaCard"].innerHTML;

    // 2. Opt-in dev mode (pro_corpus_enabled = true) restores hidden tabs & buttons
    state.capabilities.pro_corpus_enabled = true;
    syncProCorpusGateUi();
    const enabledGate = {{
      navInsightsHidden: _elements["navInsights"].classList.contains("hidden"),
      sideTabProHidden: _elements["sideTabProSearch"].classList.contains("hidden"),
    }};
    renderHomeWorkbench();
    const homeEnabledHtml = _elements["homeContent"].innerHTML;
    renderStudioSetMetaCard();
    const setMetaEnabledHtml = _elements["studioSetMetaCard"].innerHTML;

    // 3. About routing + document.title + Back navigation
    state.capabilities.pro_corpus_enabled = false;
    await initApp();
    navigateTo("about");
    const onAbout = {{
      activeView: state.activeView,
      title: document.title,
      navAboutActive: _elements["navAbout"].classList.contains("active"),
      viewAboutHidden: _elements["viewAbout"].classList.contains("hidden"),
      url: _historyStack[_historyStack.length - 1].url,
    }};
    window.history.back();
    const afterBack = {{
      activeView: state.activeView,
      viewAboutHidden: _elements["viewAbout"].classList.contains("hidden"),
    }};

    // Direct /about route
    window.location.pathname = "/about";
    window.location.hash = "";
    routeFromHash(false);
    const onDirectAboutPath = {{
      activeView: state.activeView,
      title: document.title,
    }};

    return {{
      publicGate,
      enabledGate,
      homePublicHasSingleCol: homePublicHtml.includes("pulse-grid single-col"),
      homePublicHasRoadmapCard: homePublicHtml.includes("Latest AI") || homePublicHtml.includes("FC_ENABLE_PRO_CORPUS"),
      homeEnabledHasCheckupCard: homeEnabledHtml.includes("Latest AI"),
      setMetaPublicHasBreakdown: setMetaPublicHtml.includes("AI Match Breakdown"),
      setMetaEnabledHasBreakdown: setMetaEnabledHtml.includes("AI Match Breakdown"),
      onAbout,
      afterBack,
      onDirectAboutPath,
    }};
  }})()`, sandbox);
  console.log(JSON.stringify(out));
}})();
"""
    res = _run_node(script)

    assert res["publicGate"] == {"navInsightsHidden": True, "sideTabProHidden": True}
    assert res["enabledGate"] == {"navInsightsHidden": False, "sideTabProHidden": False}
    assert res["homePublicHasSingleCol"] is True
    assert res["homePublicHasRoadmapCard"] is False
    assert res["homeEnabledHasCheckupCard"] is True
    assert res["setMetaPublicHasBreakdown"] is False
    assert res["setMetaEnabledHasBreakdown"] is True
    assert res["onAbout"] == {
        "activeView": "about",
        "title": "About — Fightcade Lab",
        "navAboutActive": True,
        "viewAboutHidden": False,
        "url": "/#about",
    }
    assert res["afterBack"] == {"activeView": "home", "viewAboutHidden": True}
    assert res["onDirectAboutPath"] == {"activeView": "about", "title": "About — Fightcade Lab"}
