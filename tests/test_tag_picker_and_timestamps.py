"""Tests for unified preset tags, reusable collapsed tag multi-select, legacy kind bridging, and human timestamps."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from fightcade_analysis.annotations import PRESET_TAGS, VALID_KINDS, create_annotation
from fightcade_analysis.notes_hub import list_collated_notes

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "fightcade_analysis" / "web"
STATE_JS = WEB_DIR / "js" / "state.js"


def _run_node_eval(js_body: str) -> dict:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not found in PATH")
    driver = r"""
const fs = require("fs");
const vm = require("vm");

const stateSrc = fs.readFileSync(process.env.STATE_JS_PATH, "utf8");
const sandbox = {
  console,
  URLSearchParams,
  Intl,
  Date,
  Math,
  Number,
  String,
  Array,
  Set,
  Map,
  RegExp,
  JSON,
  Boolean,
  Object,
  encodeURIComponent,
  decodeURIComponent,
  setTimeout,
  clearTimeout,
  window: { __FC_BOOTSTRAP__: {}, location: { origin: "http://127.0.0.1:8765", pathname: "/", search: "", hash: "" } },
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  document: {
    documentElement: { getAttribute: () => null, setAttribute: () => {}, removeAttribute: () => {} },
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
};
vm.createContext(sandbox);
vm.runInContext(stateSrc, sandbox, { filename: "state.js" });
const fn = vm.runInContext("(function() {\n" + process.env.TEST_JS + "\n})", sandbox);
const out = fn();
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        [node_bin, "-e", driver],
        env={**os.environ, "STATE_JS_PATH": str(STATE_JS), "TEST_JS": js_body},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=15,
    )
    assert proc.returncode == 0, f"Node harness failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    return json.loads(proc.stdout)


def test_preset_tags_unified_and_backward_compatible():
    expected = (
        "Mistake",
        "Question",
        "Good Play",
        "Opp Habit",
        "Anti-Air",
        "Whiff Punish",
        "Throw / Tech",
        "Wakeup / Oki",
        "Corner",
        "Parry",
        "Meter / Super",
        "Footsies",
        "Combo / Confirm",
    )
    assert PRESET_TAGS == expected
    assert "Good Decision" not in PRESET_TAGS
    # Legacy kinds remain valid for backward compatibility with older JSONL/D1 records
    assert {"mistake", "question", "good_play", "opp_habit", "note", "response"}.issubset(VALID_KINDS)

    js_res = _run_node_eval("return { presetTags: PRESET_TAGS };")
    assert tuple(js_res["presetTags"]) == expected


def test_tag_picker_behavior_dedup_and_xss():
    res = _run_node_eval(
        r"""
        initTagPicker("p1", ["Anti-Air", "anti-air", "Corner"]);
        const closedHtml = renderTagPickerHtml("p1");

        toggleTagPickerMenu("p1");
        const openHtml = renderTagPickerHtml("p1");
        const isOpenAfterToggle = hasOpenTagPicker();

        // Toggle preset tag on, then off while menu remains open
        toggleTagPickerTag("p1", "Mistake");
        const afterAddPreset = getTagPickerTags("p1");
        const stillOpenAfterSelect = hasOpenTagPicker();

        // Enter on exact preset in different case deduplicates
        onTagPickerSearchInput("p1", "corner");
        let prevented = false;
        let stopped = false;
        onTagPickerSearchKeydown("p1", {
          key: "Enter",
          preventDefault: () => { prevented = true; },
          stopPropagation: () => { stopped = true; },
        });
        const afterDupEnter = getTagPickerTags("p1");

        // Enter on novel custom tag creates & selects it
        onTagPickerSearchInput("p1", "  Safe   Jump  ");
        onTagPickerSearchKeydown("p1", {
          key: "Enter",
          preventDefault: () => {},
          stopPropagation: () => {},
        });
        const afterCustomEnter = getTagPickerTags("p1");

        // XSS payload tag must be escaped cleanly in chips and options
        addTagPickerCustom("p1", `<img src=x onerror=alert('xss')>`);
        const xssHtml = renderTagPickerHtml("p1");

        // Remove chip
        removeTagPickerTag("p1", "Corner");
        const afterRemove = getTagPickerTags("p1");

        // Escape closes dropdown cleanly
        onTagPickerSearchKeydown("p1", {
          key: "Escape",
          preventDefault: () => {},
          stopPropagation: () => {},
        });
        const openAfterEsc = hasOpenTagPicker();

        return {
          closedHtml,
          openHtml,
          isOpenAfterToggle,
          afterAddPreset,
          stillOpenAfterSelect,
          prevented,
          stopped,
          afterDupEnter,
          afterCustomEnter,
          xssHtml,
          afterRemove,
          openAfterEsc,
        };
        """
    )

    assert "tag-picker-menu" not in res["closedHtml"]
    assert "Anti-Air" in res["closedHtml"]
    assert "+ Add tags" in res["closedHtml"]
    assert res["isOpenAfterToggle"] is True
    assert "tag-picker-menu" in res["openHtml"]
    assert res["afterAddPreset"] == ["Anti-Air", "Corner", "Mistake"]
    assert res["stillOpenAfterSelect"] is True
    assert res["prevented"] is True and res["stopped"] is True
    assert res["afterDupEnter"] == ["Anti-Air", "Corner", "Mistake"]
    assert res["afterCustomEnter"] == ["Anti-Air", "Corner", "Mistake", "Safe Jump"]
    assert "<img src=x" not in res["xssHtml"]
    assert "&lt;img src=x onerror=alert(&#39;xss&#39;)&gt;" in res["xssHtml"]
    assert "Corner" not in res["afterRemove"]
    assert res["openAfterEsc"] is False


def test_legacy_kind_bridging_and_system_origin_badges():
    res = _run_node_eval(
        r"""
        return {
          legacyMistakeTags: getDisplayTagsForItem({ kind: "mistake", tags: ["Anti-Air"] }),
          legacyNoDupTags: getDisplayTagsForItem({ kind: "good_play", tags: ["good play", "Parry"] }),
          noteTags: getDisplayTagsForItem({ kind: "note", tags: ["Corner"] }),
          regularNoteBadge: renderSystemOriginBadgeHtml({ kind: "note", source: "annotation" }),
          legacyMistakeBadge: renderSystemOriginBadgeHtml({ kind: "mistake", source: "annotation" }),
          selectBadge: getSystemOriginBadge({ entry_type: "select_mark", kind: "note" }),
          respBadge: getSystemOriginBadge({ source: "takeover_response", kind: "response" }),
          setBadge: getSystemOriginBadge({ entry_type: "set_note", kind: "set_takeaway" }),
          proBadge: getSystemOriginBadge({ source: "pro_clip", kind: "pro_clip" }),
          savedAnnotationKind: resolveSavedKindForItem("annotation", "text", ["Mistake"]),
          savedRespKind: resolveSavedKindForItem("takeover_response", "text", ["Good Play"]),
          savedTriagedSelectKind: resolveSavedKindForItem("select_mark", "punish sweep", []),
          savedUntriagedSelectKind: resolveSavedKindForItem("select_mark", "", []),
        };
        """
    )

    assert res["legacyMistakeTags"] == ["Anti-Air", "Mistake"]
    assert res["legacyNoDupTags"] == ["good play", "Parry"]
    assert res["noteTags"] == ["Corner"]
    assert res["regularNoteBadge"] == ""
    assert res["legacyMistakeBadge"] == ""
    assert res["selectBadge"]["label"] == "Bookmark"
    assert res["respBadge"]["label"] == "Labbed Answer"
    assert res["setBadge"]["label"] == "Set Takeaway"
    assert res["proBadge"]["label"] == "Top-Rank Clip"
    assert res["savedAnnotationKind"] == "note"
    assert res["savedRespKind"] == "response"
    assert res["savedTriagedSelectKind"] == "note"
    assert res["savedUntriagedSelectKind"] == "select_mark"


def test_human_time_formatting_helpers():
    res = _run_node_eval(
        r"""
        return {
          zero: formatRoundClock(0),
          neg: formatRoundClock(-5),
          sec14: formatRoundClock(14.8),
          sec62: formatRoundClock(62.2),
          dur35: formatClipDuration(18422, 18632, 60),
          durInvalid: formatClipDuration(100, 100, 60),
          durNull: formatClipDuration(null, 200, 60),
          rel24: formatRelativeClipTime(18566, 18422, 60),
          relClamp: formatRelativeClipTime(100, 200, 60),
        };
        """
    )

    assert res["zero"] == "0:00"
    assert res["neg"] == "0:00"
    assert res["sec14"] == "0:14"
    assert res["sec62"] == "1:02"
    assert res["dur35"] == "3.5s"
    assert res["durInvalid"] == ""
    assert res["durNull"] == ""
    assert res["rel24"] == "2.4s"
    assert res["relClamp"] == "0.0s"


def test_notes_hub_tag_cloud_bridges_legacy_kinds(tmp_path: Path):
    create_annotation(
        {
            "quark_id": "1795-0001",
            "round_id": 1,
            "perspective": 1,
            "anchor_gf": 1200,
            "kind": "mistake",
            "tags": ["Anti-Air"],
            "note": "Missed crouch fierce",
            "queued_drill": False,
        },
        data_dir=tmp_path,
    )
    create_annotation(
        {
            "quark_id": "1795-0001",
            "round_id": 1,
            "perspective": 1,
            "anchor_gf": 1500,
            "kind": "mistake",
            "tags": ["Mistake", "Corner"],
            "note": "Already tagged mistake",
            "queued_drill": False,
        },
        data_dir=tmp_path,
    )

    hub = list_collated_notes(username="player1", data_dir=tmp_path)
    tag_map = {t["tag"]: t["count"] for t in hub["counts"]["by_tag"]}
    assert tag_map["Mistake"] == 2
    assert tag_map["Anti-Air"] == 1
    assert tag_map["Corner"] == 1


def test_composer_layout_and_no_opaque_gf_card_labels():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    # Composer puts noteTextarea BEFORE composerTagPicker, and removes old kindGrid/tagGrid/customTagInput
    assert html.index('id="noteTextarea"') < html.index('id="composerTagPicker"')
    for removed_id in ('id="kindGrid"', 'id="tagGrid"', 'id="customTagInput"', 'id="setNoteTagsInput"'):
        assert removed_id not in html
    assert 'id="setNoteTagPicker"' in html
    # Notes Hub filter chips keep only the 4 clean source filters
    for removed_cnt in ('id="nCntMistake"', 'id="nCntQuestion"', 'id="nCntOppHabit"', 'id="nCntGoodPlay"', 'id="nCntSelect"'):
        assert removed_cnt not in html
    for kept_cnt in ('id="nCntAll"', 'id="nCntMoment"', 'id="nCntResponse"', 'id="nCntSet"'):
        assert kept_cnt in html

    # Card renderers in notes.js and playlists.js never print raw `gf`
    notes_js = (WEB_DIR / "js" / "notes.js").read_text(encoding="utf-8")
    playlists_js = (WEB_DIR / "js" / "playlists.js").read_text(encoding="utf-8")
    assert "· gf" not in notes_js
    assert "· gf" not in playlists_js
