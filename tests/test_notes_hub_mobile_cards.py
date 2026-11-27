"""Tests for uniform mobile Notes Hub cards, 2-line stacked matchups, and overflow menu."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
STATE_JS = WEB_DIR / "js" / "state.js"
NOTES_JS = WEB_DIR / "js" / "notes.js"
APP_JS = WEB_DIR / "js" / "app.js"
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


def test_notes_hub_cards_stacked_matchup_and_overflow_menu_rendering():
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
const mockMenus = new Map();
function registerMenuDom(id) {{
  const btnAttrs = {{ "aria-expanded": "false" }};
  const btnClasses = new Set();
  const menuClasses = new Set(["hidden"]);
  const btn = {{
    classList: {{
      toggle(c, f) {{ if (f) btnClasses.add(c); else btnClasses.delete(c); }},
      contains(c) {{ return btnClasses.has(c); }},
    }},
    setAttribute(k, v) {{ btnAttrs[k] = String(v); }},
    getAttribute(k) {{ return btnAttrs[k]; }},
  }};
  const menu = {{
    classList: {{
      toggle(c, f) {{ if (f) menuClasses.add(c); else menuClasses.delete(c); }},
      contains(c) {{ return menuClasses.has(c); }},
    }},
  }};
  const wrap = {{
    dataset: {{ hubMenuId: id }},
    querySelector(sel) {{
      if (sel === ".hub-card-more-btn") return btn;
      if (sel === ".hub-card-more-menu") return menu;
      return null;
    }},
    _state() {{
      return {{
        expanded: btnAttrs["aria-expanded"],
        btnOpen: btnClasses.has("is-open"),
        menuHidden: menuClasses.has("hidden"),
      }};
    }},
  }};
  mockMenus.set(id, wrap);
  return wrap;
}}

const sandbox = {{
  console,
  window: {{
    innerWidth: 390,
    location: {{ origin: "http://localhost:8765", search: "", hash: "#notes", pathname: "/" }},
    matchMedia: () => ({{ matches: true, addEventListener: () => {{}} }}),
  }},
  document: {{
    getElementById: (id) => makeEl(id),
    querySelectorAll: (sel) => {{
      if (sel === ".hub-card-more-wrap[data-hub-menu-id]") return [...mockMenus.values()];
      return [];
    }},
    addEventListener: (ev, fn) => {{
      if (ev === "click") clickListeners.push(fn);
    }},
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
  registerMenuDom,
  clickListeners,
  SPECIAL_ID: "sel_'\\"quote",
}};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(NOTES_JS))}, "utf8"), sandbox);

const out = vm.runInContext(`
  state.username = "blankasballs";
  state.capabilities.can_takeover = true;
  state.capabilities.interactive_desktop = true;

  const momentCard = renderHubNoteCardHtml({{
    id: "ann_1",
    source: "annotation",
    entry_type: "moment",
    kind: "note",
    quark_id: "1795547282177-8026",
    round_id: 2,
    perspective: 1,
    start_gf: 1000,
    end_gf: 1210,
    anchor_gf: 1120,
    round_fps: 60,
    has_video: true,
    has_set_video: true,
    video_url: "/replays/q1/r02.mp4",
    clip_start_s: 10.0,
    clip_end_s: 13.5,
    self_player_name: "blankasballs",
    self_rank_letter: "B",
    self_char_name: "Akuma",
    opp_player_name: "super_long_fightcade_handle_28chars",
    opp_rank_letter: "A",
    opp_char_name: "Chun-Li",
    note: "Whiff punished st.HK cleanly after walking out of range.",
    tags: ["Whiff Punish"],
    date_str: "2026-11-26",
    editable: true,
    deletable: true,
    needs_note: false,
    cloud_visibility: "unlisted",
    cloud_share_url: "https://fightcadelab.com/m/s_abc",
  }});

  const selectCard = renderHubNoteCardHtml({{
    id: SPECIAL_ID,
    source: "select_mark",
    entry_type: "select_mark",
    kind: "select_mark",
    quark_id: "1795547282177-8026",
    round_id: 1,
    perspective: 1,
    start_gf: 500,
    end_gf: 680,
    anchor_gf: 600,
    has_video: true,
    has_set_video: true,
    video_url: "/replays/q1/r01.mp4",
    self_player_name: "blankasballs",
    self_char_name: "Akuma",
    opp_player_name: "rival",
    opp_char_name: "Ken",
    note: "In-game Select mark #1",
    date_str: "2026-11-26",
    editable: true,
    deletable: true,
    needs_note: true,
  }});

  const setCard = renderHubNoteCardHtml({{
    id: "set_1",
    source: "set_note",
    entry_type: "set_note",
    kind: "set_takeaway",
    quark_id: "1795547282177-8026",
    has_video: false,
    has_set_video: true,
    self_player_name: "blankasballs",
    self_char_name: "Akuma",
    opp_player_name: "rival",
    opp_char_name: "Ken",
    note: "Stop jumping into EX legs in the corner.",
    date_str: "2026-11-26",
    editable: true,
    deletable: true,
    needs_note: false,
  }});

  const readonlyCard = renderHubNoteCardHtml({{
    id: "bm_readonly",
    source: "bookmark",
    entry_type: "moment",
    kind: "note",
    quark_id: "1795547282177-8026",
    has_video: false,
    has_set_video: false,
    self_player_name: "blankasballs",
    opp_player_name: "rival",
    note: "Read-only bookmark",
    date_str: "2026-11-26",
    editable: false,
    deletable: false,
    needs_note: false,
  }});

  // Test single-open & close behavior
  const w1 = registerMenuDom("ann_1");
  const w2 = registerMenuDom("ann_2");
  toggleHubCardMenu("ann_1");
  const afterOpen1 = {{ openId: state.hubOpenMenuId, w1: w1._state(), w2: w2._state() }};
  toggleHubCardMenu("ann_2");
  const afterOpen2 = {{ openId: state.hubOpenMenuId, w1: w1._state(), w2: w2._state() }};

  // Click inside menu wrapper keeps it open
  clickListeners.forEach(fn => fn({{ target: {{ closest: (sel) => sel === ".hub-card-more-wrap" ? {{}} : null }} }}));
  const afterInsideClick = state.hubOpenMenuId;

  // Click outside closes it
  clickListeners.forEach(fn => fn({{ target: {{ closest: () => null }} }}));
  const afterOutsideClick = {{ openId: state.hubOpenMenuId, w2: w2._state() }};

  // closeAllHubCardMenus return boolean semantics
  toggleHubCardMenu("ann_1");
  const closedTrue = closeAllHubCardMenus();
  const closedFalse = closeAllHubCardMenus();

  ({{
    momentCard,
    selectCard,
    setCard,
    readonlyCard,
    afterOpen1,
    afterOpen2,
    afterInsideClick,
    afterOutsideClick,
    closedTrue,
    closedFalse,
  }});
`, sandbox);
console.log(JSON.stringify(out));
"""
    res = _run_node(script)

    moment = res["momentCard"]
    # 1. Fixed 2-line stacked matchup markup + single timestamp in submeta
    assert 'class="hub-card-matchup"' in moment
    assert 'class="match-side self"' in moment
    assert 'class="match-side opp"' in moment
    assert 'class="match-player-name"' in moment
    assert 'class="hub-card-submeta"' in moment
    assert moment.count('class="hub-card-ts"') == 1
    assert "3.5s" in moment
    assert "🔗 Shared" in moment
    assert 'class="hub-expand-chevron"' in moment

    # 2. Primary vs desktop-only buttons & overflow menu for regular moment
    assert 'class="sm primary" data-tip="Open moment viewer' in moment
    assert "Open in Studio →</button>" in moment
    assert 'class="sm hub-btn-desktop-only ' in moment  # Preview
    assert "practice-btn hub-btn-desktop-only" in moment  # Practice
    assert 'aria-label="More note actions"' in moment
    assert 'aria-expanded="false"' in moment
    assert 'role="menu"' in moment
    assert 'class="hub-card-more-item" onclick="event.stopPropagation(); closeAllHubCardMenus(); startHubEdit(&quot;ann_1&quot;)">Edit</button>' in moment
    assert 'class="hub-card-more-item" onclick="event.stopPropagation(); closeAllHubCardMenus(); shareHubNote(&quot;ann_1&quot;)">↗ Share Moment</button>' in moment
    assert 'class="hub-card-more-item is-danger" onclick="event.stopPropagation(); closeAllHubCardMenus(); deleteHubNote(&quot;ann_1&quot;)">Delete</button>' in moment

    # 3. Select mark card: + Add Note, Share, Dismiss + escJsAttr escaping on special ID
    select = res["selectCard"]
    assert "+ Add Note</button>" in select
    assert "Dismiss</button>" in select
    assert "Bookmark" in select
    assert "toggleHubCardMenu(&quot;sel_&#39;\\&quot;quote&quot;)" in select
    assert "startHubEdit(&quot;sel_&#39;\\&quot;quote&quot;)" in select
    assert "deleteHubNote(&quot;sel_&#39;\\&quot;quote&quot;)" in select

    # 4. Set takeaway card: Edit & Delete only (no Share Moment, no clip duration)
    set_card = res["setCard"]
    assert "Set Takeaway" in set_card
    assert "hub-card-dur" not in set_card
    assert "↗ Share Moment" not in set_card
    assert 'startHubEdit(&quot;set_1&quot;)">Edit</button>' in set_card
    assert 'deleteHubNote(&quot;set_1&quot;)">Delete</button>' in set_card

    # 5. Read-only card: zero overflow menu rendered
    assert "hub-card-more-wrap" not in res["readonlyCard"]

    # 6. Single-open & close state semantics
    assert res["afterOpen1"]["openId"] == "ann_1"
    assert res["afterOpen1"]["w1"] == {"expanded": "true", "btnOpen": True, "menuHidden": False}
    assert res["afterOpen1"]["w2"] == {"expanded": "false", "btnOpen": False, "menuHidden": True}

    assert res["afterOpen2"]["openId"] == "ann_2"
    assert res["afterOpen2"]["w1"] == {"expanded": "false", "btnOpen": False, "menuHidden": True}
    assert res["afterOpen2"]["w2"] == {"expanded": "true", "btnOpen": True, "menuHidden": False}

    assert res["afterInsideClick"] == "ann_2"
    assert res["afterOutsideClick"]["openId"] is None
    assert res["afterOutsideClick"]["w2"] == {"expanded": "false", "btnOpen": False, "menuHidden": True}
    assert res["closedTrue"] is True
    assert res["closedFalse"] is False


def test_notes_hub_mobile_css_and_escape_wiring():
    css = STYLES_CSS.read_text(encoding="utf-8")
    notes_src = NOTES_JS.read_text(encoding="utf-8")
    app_src = APP_JS.read_text(encoding="utf-8")

    # notes.js passes stackedMobile: true
    assert "stackedMobile: true" in notes_src
    # app.js closes hub card overflow menus on Escape
    assert "closeAllHubCardMenus" in app_src

    # Mobile CSS rules for Notes Hub cards
    assert ".hub-card-submeta" in css
    assert ".hub-card-matchup > .match-side.self" in css
    assert ".hub-card-matchup > .match-side.opp" in css
    assert ".hub-card-head > .hub-expand-chevron" in css
    assert ".hub-btn-desktop-only" in css
    assert ".hub-card-more-wrap" in css
    assert ".hub-card-more-btn" in css
    assert ".hub-card-more-menu" in css
    assert ".hub-card-more-item.is-danger" in css
    assert ".hub-note-card:not(.is-expanded) .hub-note-body" in css
    assert "-webkit-line-clamp: 3" in css
    assert "flex-wrap: nowrap" in css
