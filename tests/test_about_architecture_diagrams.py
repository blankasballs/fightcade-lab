"""Tests for the responsive interactive architecture diagrams on the About page."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import threading

from PIL import Image
import requests

from fightcade_analysis.server import create_server

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
IMG_DIR = WEB_DIR / "img"
INDEX_HTML = WEB_DIR / "index.html"
STYLES_CSS = WEB_DIR / "styles.css"
STATE_JS = WEB_DIR / "js" / "state.js"
APP_JS = WEB_DIR / "js" / "app.js"


def _extract_about_decentralized(html: str) -> str:
    start = html.index('<div id="aboutDecentralized"')
    end = html.index("<h2>Credits</h2>", start)
    return html[start:end]


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


def test_v4_hardware_icon_assets_and_http_serving(tmp_path: Path):
    for filename in ("icon-desktop-192.png", "icon-mobile-192.png"):
        path = IMG_DIR / filename
        assert path.is_file(), f"Missing icon asset: {path}"
        assert path.stat().st_size <= 50 * 1024, f"{filename} exceeds 50 KB ({path.stat().st_size} B)"
        with Image.open(path) as im:
            assert im.format == "PNG"
            assert im.size == (192, 192)

    (tmp_path / "reports").mkdir()
    (tmp_path / "clips").mkdir()
    server = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        for filename in ("icon-desktop-192.png", "icon-mobile-192.png"):
            resp = requests.get(f"{base}/assets/img/{filename}", timeout=5)
            assert resp.status_code == 200
            assert resp.headers.get("Content-Type", "").startswith("image/png")
            assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        server.shutdown()
        server.server_close()


def test_about_architecture_diagrams_markup_copy_rules_and_css():
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    dec_html = _extract_about_decentralized(html)

    # Required journey containers and all 5 interactive nodes + popovers
    assert 'id="archJourneyLocal"' in dec_html
    assert 'id="archJourneyByoc"' in dec_html
    for node_id in ("local-web", "local-pc", "byoc-fcl", "byoc-r2", "byoc-mobile"):
        assert f'data-arch-node="{node_id}"' in dec_html
        assert f'aria-controls="archPop-{node_id}"' in dec_html
        assert f'id="archPop-{node_id}"' in dec_html

    # Required exact labels & images
    assert "Companion App" in dec_html
    assert "Upload replays" in dec_html
    assert "/assets/img/icon-192.png" in dec_html
    assert "/assets/img/icon-desktop-192.png" in dec_html
    assert "/assets/img/icon-mobile-192.png" in dec_html

    # Accurate scoping: FIGHTCADELAB.COM and YOUR PC (LOCAL) live in separate zones
    assert "FIGHTCADELAB.COM" in dec_html
    assert "YOUR PC (LOCAL)" in dec_html
    local_start = dec_html.index('id="archJourneyLocal"')
    local_end = dec_html.index('id="archJourneyByoc"')
    local_html = dec_html[local_start:local_end]
    assert local_html.count('class="arch-zone"') == 2
    assert local_html.index("FIGHTCADELAB.COM") < local_html.index('class="arch-connector') < local_html.index("YOUR PC (LOCAL)")

    # Hard copy regression guards
    assert "—" not in dec_html and "&mdash;" not in dec_html.lower()
    assert "$0" not in dec_html and "0$" not in dec_html
    assert not re.search(r"pc\s+(?:is\s+)?off", dec_html, re.IGNORECASE)
    assert "FBNeo + Companion" not in dec_html
    assert "Upload MP4s" not in dec_html

    # Responsive + light/dark CSS hooks present
    for selector in (
        ".arch-journey-card {",
        ".arch-zone {",
        ".arch-node-btn {",
        ".arch-icon-tile {",
        ".arch-popover {",
        "html[data-theme=\"light\"] .arch-popover {",
        "@media (max-width: 680px)",
    ):
        assert selector in css


def test_arch_diagram_popover_controller_runtime():
    script = f"""
const fs = require("fs");
const vm = require("vm");

const docListeners = Object.create(null);

function makeClassList(initial = []) {{
  const set = new Set(initial);
  return {{
    add(...cs) {{ cs.forEach(c => set.add(c)); }},
    remove(...cs) {{ cs.forEach(c => set.delete(c)); }},
    toggle(c, force) {{
      if (force === undefined) {{ if (set.has(c)) set.delete(c); else set.add(c); }}
      else if (force) set.add(c); else set.delete(c);
    }},
    contains(c) {{ return set.has(c); }},
  }};
}}

function makeZone(nodeId) {{
  const zone = {{
    nodeId,
    classList: makeClassList(["arch-zone"]),
    btn: null,
    querySelector(sel) {{ return sel === ".arch-node-btn" ? this.btn : null; }},
  }};
  const btn = {{
    nodeId,
    attrs: {{ "aria-expanded": "false" }},
    classList: makeClassList(["arch-node-btn"]),
    blurred: false,
    setAttribute(k, v) {{ this.attrs[k] = String(v); }},
    getAttribute(k) {{ return this.attrs[k]; }},
    blur() {{ this.blurred = true; }},
    closest(sel) {{
      if (sel === ".arch-node-btn") return btn;
      if (sel === ".arch-zone") return zone;
      return null;
    }},
  }};
  zone.btn = btn;
  return zone;
}}

const zones = [makeZone("local-web"), makeZone("local-pc"), makeZone("byoc-r2")];
const outsideTarget = {{ closest: () => null }};
const elements = Object.create(null);
function makeEl(id) {{
  if (!elements[id]) {{
    const initHidden = ["viewAbout", "graduatedModal", "playlistModal", "addToPlModal", "companionModal", "reportModal", "termsModal", "shareBundleModal", "byocModal"].includes(id);
    elements[id] = {{
      id,
      innerHTML: "",
      textContent: "",
      value: "",
      style: {{}},
      dataset: {{}},
      classList: makeClassList(initHidden ? ["hidden"] : []),
      setAttribute() {{}},
      getAttribute() {{ return ""; }},
      addEventListener() {{}},
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
    location: {{ pathname: "/", search: "", hash: "#about", origin: "http://127.0.0.1:8765" }},
    history: {{ pushState() {{}}, replaceState() {{}}, back() {{}} }},
    addEventListener() {{}},
    scrollTo: () => {{}},
    matchMedia: () => ({{ matches: false }}),
  }},
  history: {{ pushState() {{}}, replaceState() {{}}, back() {{}} }},
  document: {{
    title: "",
    hidden: false,
    activeElement: null,
    documentElement: {{ getAttribute: () => "dark", setAttribute: () => {{}}, classList: makeClassList() }},
    body: {{ classList: makeClassList() }},
    getElementById: (id) => makeEl(id),
    querySelector: () => null,
    querySelectorAll(sel) {{
      if (sel === ".arch-zone.is-open") return zones.filter(z => z.classList.contains("is-open"));
      return [];
    }},
    addEventListener(ev, fn) {{
      if (!docListeners[ev]) docListeners[ev] = [];
      docListeners[ev].push(fn);
    }},
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
  closeStatusPopover: () => {{}},
  loadHomeWorkbench: () => {{}},
  loadMatches: () => {{}},
  loadHubNotes: () => {{}},
  loadInsights: () => {{}},
  checkForNewTakeoverResponses: () => {{}},
}};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);

vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
const appSrc = fs.readFileSync({json.dumps(str(APP_JS))}, "utf8").replace(/initApp\\(\\);\\s*$/, "");
vm.runInContext(appSrc, sandbox);

function fireClick(target) {{
  let prevented = false;
  (docListeners["click"] || []).forEach(fn => fn({{ target, preventDefault() {{ prevented = true; }} }}));
  return prevented;
}}
function fireEscape() {{
  let prevented = false;
  (docListeners["keydown"] || []).forEach(fn => fn({{ key: "Escape", target: {{ tagName: "BODY" }}, preventDefault() {{ prevented = true; }} }}));
  return prevented;
}}
function snapshot() {{
  return zones.map(z => ({{ id: z.nodeId, open: z.classList.contains("is-open"), expanded: z.btn.getAttribute("aria-expanded") }}));
}}

vm.runInContext('state.activeView = "about";', sandbox);
fireClick(zones[0].btn);
const afterClick0 = snapshot();

fireClick(zones[1].btn);
const afterClick1 = snapshot();

fireClick(zones[1].btn);
const afterToggleOff1 = snapshot();

fireClick(zones[2].btn);
fireClick(outsideTarget);
const afterOutside = snapshot();

fireClick(zones[0].btn);
const escHandled = fireEscape();
const afterEsc = snapshot();

fireClick(zones[1].btn);
vm.runInContext('navigateTo("home", false);', sandbox);
const afterNavigateAway = snapshot();

console.log(JSON.stringify({{
  afterClick0,
  afterClick1,
  afterToggleOff1,
  afterOutside,
  escHandled,
  afterEsc,
  afterNavigateAway,
}}));
"""
    res = _run_node(script)

    assert res["afterClick0"] == [
        {"id": "local-web", "open": True, "expanded": "true"},
        {"id": "local-pc", "open": False, "expanded": "false"},
        {"id": "byoc-r2", "open": False, "expanded": "false"},
    ]
    assert res["afterClick1"] == [
        {"id": "local-web", "open": False, "expanded": "false"},
        {"id": "local-pc", "open": True, "expanded": "true"},
        {"id": "byoc-r2", "open": False, "expanded": "false"},
    ]
    assert all(not z["open"] and z["expanded"] == "false" for z in res["afterToggleOff1"])
    assert all(not z["open"] and z["expanded"] == "false" for z in res["afterOutside"])
    assert res["escHandled"] is True
    assert all(not z["open"] and z["expanded"] == "false" for z in res["afterEsc"])
    assert all(not z["open"] and z["expanded"] == "false" for z in res["afterNavigateAway"])
