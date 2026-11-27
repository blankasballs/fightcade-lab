"""Tests for the unified Focus/Playlist modal and collapsed '+ Playlist' picker."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fightcade_analysis import playlists as pl_mod

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
INDEX_HTML = WEB_DIR / "index.html"
STATE_JS = WEB_DIR / "js" / "state.js"
STUDIO_JS = WEB_DIR / "js" / "studio.js"
NOTES_JS = WEB_DIR / "js" / "notes.js"
PLAYLISTS_JS = WEB_DIR / "js" / "playlists.js"
HOME_JS = WEB_DIR / "js" / "home.js"


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


def test_unified_modal_markup_replaces_goal_modal():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="goalModal"' not in html
    assert 'id="plKindSelect"' not in html
    assert 'id="plShareableCheck"' not in html
    assert 'id="goalTrendInput"' not in html
    assert 'id="goalTagsInput"' not in html

    for expected_id in (
        'id="playlistModal"',
        'id="plSeedNotice"',
        'id="plNameInput"',
        'id="plDescInput"',
        'id="plPinFocusCheck"',
        'id="plPinFocusLabel"',
        'id="plPinFocusHint"',
        'id="plTagPicker"',
        'id="plMatchupInput"',
        'id="btnSavePlaylist"',
    ):
        assert expected_id in html, f"Missing {expected_id} in index.html"


def test_mental_model_copy_consistent_in_home_and_playlists():
    home_src = HOME_JS.read_text(encoding="utf-8")
    playlists_src = PLAYLISTS_JS.read_text(encoding="utf-8")
    focus_copy = "Pinned playlists (max 3) — your current training priorities."
    pl_copy = "All playlists — watch, practice in Takeover, or pin any as an Active Focus."
    assert focus_copy in home_src
    assert focus_copy in playlists_src
    assert pl_copy in home_src
    assert pl_copy in playlists_src


def test_collapsed_collection_picker_zero_ghost_pills_and_dropdown_grouping():
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
      checked: false,
      disabled: false,
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
      focus() {{}},
      getContext() {{ return {{}}; }},
      addEventListener() {{}},
    }};
  }}
  return elements[id];
}}

const sandbox = {{
  console,
  window: {{ location: {{ origin: "http://localhost:8765", search: "" }}, addEventListener: () => {{}}, matchMedia: () => ({{ matches: false }}) }},
  document: {{
    getElementById: (id) => makeEl(id),
    querySelectorAll: () => [],
    addEventListener: () => {{}},
    createElement: () => makeEl("_tmp"),
    body: {{ appendChild: () => {{}} }},
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
  HTMLVideoElement: {{ prototype: {{}} }},
}};
sandbox.window.document = sandbox.document;
sandbox._elements = elements;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STUDIO_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(NOTES_JS))}, "utf8"), sandbox);

vm.runInContext(`
  state.username = "blankasballs";
  state.playlists = [
    {{ id: "f1", name: "Anti-Air", is_focus: true, focus_status: "active", items: [{{ item_id: "m_assigned" }}] }},
    {{ id: "f2", name: "Corner Escape", is_focus: true, focus_status: "active", items: [] }},
    {{ id: "p1", name: "Ken Matchup", is_focus: false, focus_status: null, items: [{{ item_id: "m_assigned" }}] }},
    {{ id: "p2", name: "O'Brien <script>\\\\"x\\\\"</script>", is_focus: false, focus_status: null, items: [] }},
  ];
  state.focusGoals = state.playlists.filter(p => p.is_focus);
  isMobileMode = () => false;

  // 1. Composer closed with zero selections -> zero ghost pills, no verbose eyebrow
  state.selectedCollectionIds = new Set();
  renderComposerCollectionPicker();
  const composerClosed = _elements["composerCollectionsPicker"].innerHTML;

  // 2. Saved Studio card unassigned -> zero ghost pills
  const studioUnassigned = _renderSavedMomentPickerHtml({{
    item_id: "m_unassigned",
    quark_id: "q1",
    round_id: 1,
  }});

  // 3. Saved Studio card assigned to f1 + p1 -> renders 📌 Focus chip and ▶ Playlist chip, NOT f2 or p2
  const studioAssigned = _renderSavedMomentPickerHtml({{
    item_id: "m_assigned",
    quark_id: "q1",
    round_id: 1,
  }});

  // 4. Open dropdown on composer -> grouped ACTIVE FOCUSES, PLAYLISTS, + New Playlist...
  state.selectedCollectionIds = new Set(["f1", "p1"]);
  renderComposerCollectionPicker();
  toggleColPickerMenu("composer");
  const composerOpen = _elements["cpWrap_composer"].innerHTML;

  // Clicking a row inside dropdown keeps menu open for multi-select
  triggerColPickerToggle("composer", "f2", true);
  const stillOpenAfterToggle = hasOpenCollectionPicker();
  const closedByEscape = closeAllCollectionPickers();
  const openAfterEscape = hasOpenCollectionPicker();

  // 5. Notes Hub card -> no goalPills in header, no openAddToPlaylistModal button, collapsed picker present
  const hubUnassigned = renderHubNoteCardHtml({{
    id: "m_unassigned",
    entry_type: "annotation",
    kind: "note",
    note: "Missed anti-air",
    quark_id: "q1",
    round_id: 1,
    perspective: 1,
    start_gf: 100,
    end_gf: 280,
    anchor_gf: 200,
    has_video: false,
    goal_ids: [],
    playlist_ids: [],
    tags: ["Anti-Air"],
  }});

  const hubAssigned = renderHubNoteCardHtml({{
    id: "m_assigned",
    entry_type: "annotation",
    kind: "note",
    note: "Good punish",
    quark_id: "q1",
    round_id: 1,
    perspective: 1,
    start_gf: 100,
    end_gf: 280,
    anchor_gf: 200,
    has_video: false,
    goal_ids: ["f1"],
    playlist_ids: ["f1", "p1"],
    tags: [],
  }});

  console.log(JSON.stringify({{
    composerClosed,
    studioUnassigned,
    studioAssigned,
    composerOpen,
    stillOpenAfterToggle,
    closedByEscape,
    openAfterEscape,
    hubUnassigned,
    hubAssigned,
  }}));
`, sandbox);
"""
    res = _run_node(script)

    # Zero ghost pills on unassigned Composer, Studio card, and Notes Hub card
    for surface_name in ("composerClosed", "studioUnassigned", "hubUnassigned"):
        html = res[surface_name]
        assert "col-chip" not in html, f"{surface_name} should have zero chips when unassigned"
        assert "goal-chip" not in html, f"{surface_name} should not render legacy goal-chip"
        assert "Corner Escape" not in html, f"{surface_name} leaked unselected Active Focus pill"
        assert "+ Playlist ▾" in html, f"{surface_name} missing collapsed toggle button"

    assert "SAVE TO FOCUS / PLAYLIST" not in res["composerClosed"]
    assert "Anti-Air" not in res["composerClosed"]
    assert "Anti-Air" not in res["studioUnassigned"]

    # Assigned Studio card shows 📌 Focus + ▶ Playlist chips, not unselected ones
    sa = res["studioAssigned"]
    assert "col-chip is-focus" in sa
    assert "📌 Anti-Air" in sa
    assert "▶ Ken Matchup" in sa
    assert "Corner Escape" not in sa
    assert "O&#39;Brien" not in sa

    # Open dropdown groups ACTIVE FOCUSES before PLAYLISTS and ends with New Playlist…
    co = res["composerOpen"]
    idx_focus = co.index("ACTIVE FOCUSES")
    idx_pl = co.index("PLAYLISTS")
    idx_new = co.index("New Playlist…")
    assert idx_focus < idx_pl < idx_new
    assert "<script>" not in co  # XSS escaped
    assert "O&#39;Brien &lt;script&gt;" in co
    assert res["stillOpenAfterToggle"] is True
    assert res["closedByEscape"] is True
    assert res["openAfterEscape"] is False

    # Notes Hub card uses collapsed picker, removes legacy header goal pill & modal button
    hu = res["hubUnassigned"]
    assert "openAddToPlaylistModal" not in hu
    assert "hub-moment-picker" in hu
    ha = res["hubAssigned"]
    assert "openAddToPlaylistModal" not in ha
    assert "● Anti-Air" not in ha
    assert "📌 Anti-Air" in ha
    assert "▶ Ken Matchup" in ha


def test_unified_modal_create_edit_cap_and_goal_wrapper():
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
      checked: false,
      disabled: false,
      dataset: {{}},
      classList: {{
        _set: new Set(["hidden"]),
        add(...cs) {{ cs.forEach(c => this._set.add(c)); }},
        remove(...cs) {{ cs.forEach(c => this._set.delete(c)); }},
        toggle(c, force) {{
          if (force === undefined) {{
            if (this._set.has(c)) this._set.delete(c); else this._set.add(c);
          }} else if (force) this._set.add(c); else this._set.delete(c);
        }},
        contains(c) {{ return this._set.has(c); }},
      }},
      focus() {{}},
      getContext() {{ return {{}}; }},
      addEventListener() {{}},
    }};
  }}
  return elements[id];
}}

const apiCalls = [];
const toasts = [];
const sandbox = {{
  console,
  window: {{ location: {{ origin: "http://localhost:8765", search: "" }}, addEventListener: () => {{}}, matchMedia: () => ({{ matches: false }}) }},
  document: {{
    getElementById: (id) => makeEl(id),
    querySelectorAll: () => [],
    addEventListener: () => {{}},
    createElement: () => makeEl("_tmp"),
    body: {{ appendChild: () => {{}} }},
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
  HTMLVideoElement: {{ prototype: {{}} }},
}};
sandbox.window.document = sandbox.document;
sandbox._elements = elements;
sandbox._apiCalls = apiCalls;
sandbox._toasts = toasts;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(PLAYLISTS_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(HOME_JS))}, "utf8"), sandbox);

vm.runInContext(`
  toast = (m) => _toasts.push(m);
  _syncCollectionsAfterChange = async () => {{}};
  api = async (url, opts) => {{
    const parsed = opts && opts.body ? JSON.parse(opts.body) : null;
    _apiCalls.push({{ url, method: (opts && opts.method) || "GET", body: parsed }});
    return {{ ok: true, playlist: {{ id: "saved_1", ...(parsed || {{}}) }} }};
  }};

  state.username = "blankasballs";
  state.playlists = [
    {{ id: "f1", name: "Focus 1", description: "Cue 1", is_focus: true, focus_status: "active", kind: "matchup", shareable: true, tags: ["Anti-Air"], matchup_char: "Ken", items: [] }},
    {{ id: "p1", name: "Regular Pl", description: "Desc", is_focus: false, focus_status: null, kind: "study", shareable: true, tags: ["Spacing"], matchup_char: "", items: [] }},
  ];

  (async () => {{
    // 1. openPlaylistModal() defaults unchecked (1/3)
    openPlaylistModal();
    const newPlState = {{
      title: _elements["playlistModalTitle"].textContent,
      checked: _elements["plPinFocusCheck"].checked,
      disabled: _elements["plPinFocusCheck"].disabled,
      label: _elements["plPinFocusLabel"].textContent,
      tagPickerHtml: _elements["plTagPicker"].innerHTML,
    }};

    // 2. openGoalModal wrapper opens unified modal pre-checked with seed notice & tags
    openGoalModal(null, {{
      title: "Check Dash",
      description: "Watch spacing",
      tags: ["Neutral", "CustomCue"],
      matchup_char: "Chun-Li",
      source_insight_id: "ins_1",
      trend_hint: "Trending ↓",
      seed_moments: [{{ item_id: "m1" }}],
    }});
    const newGoalState = {{
      title: _elements["playlistModalTitle"].textContent,
      checked: _elements["plPinFocusCheck"].checked,
      disabled: _elements["plPinFocusCheck"].disabled,
      name: _elements["plNameInput"].value,
      matchup: _elements["plMatchupInput"].value,
      seedHidden: _elements["plSeedNotice"].classList.contains("hidden"),
      tags: getTagPickerTags("playlistModal"),
    }};
    await savePlaylistModal();

    // 3. Edit existing Active Focus -> uncheck to unpin (must NOT send kind/shareable)
    openGoalModal("f1");
    const editFocusCheckedBefore = _elements["plPinFocusCheck"].checked;
    _elements["plPinFocusCheck"].checked = false;
    syncPlaylistModalFocusUi();
    await savePlaylistModal();

    // 4. At 3/3 cap -> creating new or editing non-active disables checkbox with hint,
    //    but editing an existing active focus keeps it enabled so user can unpin!
    state.playlists = [
      {{ id: "f1", name: "F1", is_focus: true, focus_status: "active", items: [] }},
      {{ id: "f2", name: "F2", is_focus: true, focus_status: "active", items: [] }},
      {{ id: "f3", name: "F3", is_focus: true, focus_status: "active", items: [] }},
      {{ id: "p1", name: "P1", is_focus: false, focus_status: null, items: [] }},
    ];
    openGoalModal();
    const atCapNew = {{
      checked: _elements["plPinFocusCheck"].checked,
      disabled: _elements["plPinFocusCheck"].disabled,
      label: _elements["plPinFocusLabel"].textContent,
      hint: _elements["plPinFocusHint"].textContent,
      hintHidden: _elements["plPinFocusHint"].classList.contains("hidden"),
    }};

    openPlaylistModal("p1");
    const atCapEditRegular = {{
      checked: _elements["plPinFocusCheck"].checked,
      disabled: _elements["plPinFocusCheck"].disabled,
      hintHidden: _elements["plPinFocusHint"].classList.contains("hidden"),
    }};

    openPlaylistModal("f2");
    const atCapEditActive = {{
      checked: _elements["plPinFocusCheck"].checked,
      disabled: _elements["plPinFocusCheck"].disabled,
      hintHidden: _elements["plPinFocusHint"].classList.contains("hidden"),
    }};

    console.log(JSON.stringify({{
      newPlState,
      newGoalState,
      editFocusCheckedBefore,
      atCapNew,
      atCapEditRegular,
      atCapEditActive,
      apiCalls: _apiCalls,
    }}));
  }})();
`, sandbox);
"""
    res = _run_node(script)

    assert res["newPlState"]["title"] == "New Playlist"
    assert res["newPlState"]["checked"] is False
    assert res["newPlState"]["disabled"] is False
    assert res["newPlState"]["label"] == "Pin as Active Focus (1/3)"
    assert "tag-picker" in res["newPlState"]["tagPickerHtml"]

    assert res["newGoalState"]["title"] == "New Active Focus"
    assert res["newGoalState"]["checked"] is True
    assert res["newGoalState"]["disabled"] is False
    assert res["newGoalState"]["name"] == "Check Dash"
    assert res["newGoalState"]["matchup"] == "Chun-Li"
    assert res["newGoalState"]["seedHidden"] is False
    assert res["newGoalState"]["tags"] == ["Neutral", "CustomCue"]

    assert res["editFocusCheckedBefore"] is True

    # Verify API payloads
    post_call = res["apiCalls"][0]
    assert post_call["method"] == "POST"
    assert post_call["url"] == "/api/playlists"
    assert post_call["body"]["is_focus"] is True
    assert post_call["body"]["focus_status"] == "active"
    assert post_call["body"]["tags"] == ["Neutral", "CustomCue"]
    assert post_call["body"]["matchup_char"] == "Chun-Li"
    assert post_call["body"]["source_insight_id"] == "ins_1"
    assert post_call["body"]["trend_hint"] == "Trending ↓"
    assert post_call["body"]["items"] == [{"item_id": "m1"}]

    patch_call = res["apiCalls"][1]
    assert patch_call["method"] == "PATCH"
    assert patch_call["url"] == "/api/playlists/f1"
    assert patch_call["body"]["is_focus"] is False
    assert patch_call["body"]["focus_status"] is None
    assert "kind" not in patch_call["body"]
    assert "shareable" not in patch_call["body"]
    assert "trend_hint" not in patch_call["body"]

    # 3/3 Cap checks
    assert res["atCapNew"]["checked"] is False
    assert res["atCapNew"]["disabled"] is True
    assert res["atCapNew"]["label"] == "Pin as Active Focus (3/3)"
    assert "(3/3 active — graduate or unpin one first)" in res["atCapNew"]["hint"]
    assert res["atCapNew"]["hintHidden"] is False

    assert res["atCapEditRegular"]["checked"] is False
    assert res["atCapEditRegular"]["disabled"] is True
    assert res["atCapEditRegular"]["hintHidden"] is False

    assert res["atCapEditActive"]["checked"] is True
    assert res["atCapEditActive"]["disabled"] is False
    assert res["atCapEditActive"]["hintHidden"] is True


def test_backend_patch_preserves_existing_kind_shareable_and_trend_hint(tmp_path):
    created = pl_mod.create_playlist(
        {
            "name": "Ken Matchup",
            "description": "Watch whiff punishes",
            "kind": "matchup",
            "shareable": True,
            "owner": "blankasballs",
            "is_focus": True,
            "focus_status": "active",
            "matchup_char": "Ken",
            "tags": ["Whiff Punish"],
            "trend_hint": "From AI Checkup",
            "items": [{"item_id": "m_test_1", "quark_id": "q_test_1", "round_id": 1, "start_gf": 100, "end_gf": 250}],
        },
        data_dir=tmp_path,
    )

    updated = pl_mod.update_playlist(
        created["id"],
        {
            "name": "Ken Matchup Prep",
            "description": "Updated cue",
            "matchup_char": "Ken",
            "tags": ["Whiff Punish", "Anti-Air"],
            "is_focus": False,
            "focus_status": None,
        },
        data_dir=tmp_path,
    )
    assert updated["name"] == "Ken Matchup Prep"
    assert updated["is_focus"] is False
    assert updated["focus_status"] is None
    assert updated["kind"] == "matchup"
    assert updated["shareable"] is True
    assert updated["trend_hint"] == "From AI Checkup"
    assert len(updated["items"]) == 1


def test_legacy_takeover_ready_pills_and_playlist_nav_badge_removed():
    html = INDEX_HTML.read_text(encoding="utf-8")
    pl_src = PLAYLISTS_JS.read_text(encoding="utf-8")
    home_src = HOME_JS.read_text(encoding="utf-8")

    # 1. Playlists sub-tab button has no count badge
    assert 'id="navPlaylistsBadge"' not in html
    assert "navPlaylistsBadge" not in pl_src

    # 2. Legacy Takeover-ready / Auto-prep / Pre-bake indicators removed
    for forbidden in ("Takeover-ready", "Auto-prep", "Auto-Prep", "Instant Takeover Ready", "⚡ Ready", "Prepare ${missingBake}"):
        assert forbidden not in pl_src, f"Found legacy indicator {forbidden!r} in playlists.js"
        assert forbidden not in home_src, f"Found legacy indicator {forbidden!r} in home.js"

