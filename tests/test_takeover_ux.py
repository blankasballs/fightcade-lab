"""Tests for Replay Takeover UX upgrade:
- sanitize_note() Unicode normalization, newline preservation, 600-char cap, empty-note handling
- encode_env_note() single-line Windows env var encoding
- validate_takeover_payload() explicit note field & generic fallback label filtering
- TakeoverManager.launch() FC_NOTE propagation & bookmark default-note handling
- fightcadelab:// URI note round-trip
- Lua 5.1 structural regression checks for replay_takeover.lua & grouflon_training_bridge.lua
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fightcade_analysis.takeover import (
    TakeoverManager,
    encode_env_note,
    sanitize_note,
    validate_takeover_payload,
)
from fightcade_analysis.uri_scheme import build_takeover_uri, parse_companion_uri

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sanitize_note_normalizes_unicode_preserves_newlines_and_caps_600() -> None:
    raw = (
        "  \u201cBait throw\u201d \u2192 crouch tech \u2014 don\u2019t press early\u2026 \U0001f525\n"
        "\u2022 Punish whiffed cr.MK \u2013 confirm SA2\r\n"
        "\r\n\r\n\r\n"
        "Watch meter \u2190 backdash corner\u00a0escape \u2194 neutral  "
    )
    cleaned = sanitize_note(raw)
    assert cleaned == (
        '"Bait throw" -> crouch tech - don\'t press early...\n'
        "- Punish whiffed cr.MK - confirm SA2\n\n"
        "Watch meter <- backdash corner escape <-> neutral"
    )
    assert all(ch == "\n" or 32 <= ord(ch) <= 126 for ch in cleaned)

    # Empty, whitespace-only, None, and emoji-only inputs all sanitize to ""
    assert sanitize_note(None) == ""
    assert sanitize_note("") == ""
    assert sanitize_note("   \r\n\t  ") == ""
    assert sanitize_note("\U0001f525\U0001f3af\u2728") == ""

    # 600-char cap enforced cleanly
    long_note = "a" * 595 + " bcdefghijk"
    capped = sanitize_note(long_note)
    assert len(capped) == 600
    assert capped == ("a" * 595 + " bcde")

    # encode_env_note escapes backslashes and newlines cleanly
    encoded = encode_env_note("Line 1\nPath C:\\foo\nLine 3")
    assert encoded == "Line 1\\nPath C:\\\\foo\\nLine 3"
    assert "\n" not in encoded
    assert encode_env_note("") == ""


def test_validate_takeover_payload_note_and_generic_label_filtering() -> None:
    base = {"quark_id": "1795499797641-7540", "frame": 14174, "player": 1}

    # Explicit multiline note preserved with Unicode normalized
    req1 = validate_takeover_payload(
        {
            **base,
            "label": "Mistake",
            "note": "Block high\u2014then punish\n- Watch jump-in",
        }
    )
    assert req1["label"] == "Mistake"
    assert req1["note"] == "Block high-then punish\n- Watch jump-in"

    # Explicit empty note does NOT fall back to label
    req2 = validate_takeover_payload({**base, "label": "Anti-air with cr.HP", "note": ""})
    assert req2["note"] == ""

    # Legacy caller omitting `note` falls back to real descriptive label
    req3 = validate_takeover_payload({**base, "label": "Anti-air with cr.HP"})
    assert req3["note"] == "Anti-air with cr.HP"

    # Legacy caller omitting `note` with generic fallback label yields ""
    for generic in (
        "Mistake",
        "Question",
        "Good Play",
        "Opp Habit",
        "Note",
        "Moment",
        "Select Mark",
        "Manual Selection gf19525..gf19898",
        "In-game Select mark #2 (R1)",
        "",
    ):
        req_g = validate_takeover_payload({**base, "label": generic})
        assert req_g["note"] == "", f"Generic label {generic!r} should not become coaching note"


def test_takeover_manager_passes_fc_note_and_filters_default_bookmark_notes(tmp_path: Path) -> None:
    sav_dir = tmp_path / "savestates"
    sel_dir = tmp_path / "selections"
    sav_dir.mkdir()
    sel_dir.mkdir()

    fs_path = sav_dir / "1795499797641-7540_gf14174.fs"
    inp_path = sav_dir / "1795499797641-7540_gf14174.inputs"
    fs_path.write_bytes(b"fakefs")
    inp_path.write_text("game_frame,p1_mask,p2_mask\n14100,0,0\n", encoding="utf-8")

    sel_fs = sel_dir / "sel_1.fs"
    sel_inp = sel_dir / "sel_1.inputs"
    sel_fs.write_bytes(b"fakefs")
    sel_inp.write_text("game_frame,p1_mask,p2_mask\n19525,0,0\n", encoding="utf-8")

    (sel_dir / "queue.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "sel_default",
                        "quark_id": "1795499797641-7540",
                        "start_gf": 19525,
                        "end_gf": 19898,
                        "perspective": 1,
                        "savestate": str(sel_fs),
                        "inputs": str(sel_inp),
                        "note": "In-game Select mark #1 (R1)",
                    }
                ),
                json.dumps(
                    {
                        "id": "sel_real",
                        "quark_id": "1795499797641-7540",
                        "start_gf": 19525,
                        "end_gf": 19898,
                        "perspective": 1,
                        "savestate": str(sel_fs),
                        "inputs": str(sel_inp),
                        "note": "Parry low\u2192punish\nSecond line",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    fake_exe = tmp_path / "fcadefbneo_fast.exe"
    fake_exe.write_bytes(b"MZ")
    fake_lua = tmp_path / "replay_takeover.lua"
    fake_lua.write_text("-- lua", encoding="utf-8")

    spawned: list[dict[str, Any]] = []

    class FakeProc:
        pid = 5555

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd: str, **kwargs):
        spawned.append({"cmd": cmd, "env": kwargs["env"]})
        return FakeProc()

    mgr = TakeoverManager(
        data_dir=tmp_path,
        fbneo_exe=fake_exe,
        replay_takeover_lua=fake_lua,
        seek_lua=fake_lua,
        popen_fn=fake_popen,
    )

    # 1. Explicit multiline note encoded into FC_NOTE
    res1 = mgr.launch(
        {
            "quark_id": "1795499797641-7540",
            "frame": 14174,
            "player": 1,
            "label": "Mistake",
            "note": "Line 1\nLine 2 \u2014 cr.HP",
        }
    )
    assert res1["note"] == "Line 1\nLine 2 - cr.HP"
    assert spawned[-1]["env"]["FC_NOTE"] == "Line 1\\nLine 2 - cr.HP"

    # 2. Default Select mark note is NOT treated as a coaching note
    res2 = mgr.launch({"selection_id": "sel_default"})
    assert res2["note"] == ""
    assert spawned[-1]["env"]["FC_NOTE"] == ""

    # 3. Annotated Select mark bookmark propagates its full note
    res3 = mgr.launch({"selection_id": "sel_real"})
    assert res3["note"] == "Parry low->punish\nSecond line"
    assert spawned[-1]["env"]["FC_NOTE"] == "Parry low->punish\\nSecond line"

    # 4. Explicit empty note overrides bookmark note
    res4 = mgr.launch({"selection_id": "sel_real", "note": ""})
    assert res4["note"] == ""
    assert spawned[-1]["env"]["FC_NOTE"] == ""


def test_uri_scheme_takeover_note_round_trip() -> None:
    uri = build_takeover_uri(
        quark_id="1728857242828-5771",
        frame=19501,
        player=2,
        label="Mistake",
        note="Parry cr.MK\nPunish SA2 \u2014 corner carry",
    )
    assert len(uri) <= 2048
    parsed = parse_companion_uri(uri)
    assert parsed["action"] == "takeover"
    assert parsed["payload"]["note"] == "Parry cr.MK\nPunish SA2 - corner carry"


def test_lua_takeover_and_bridge_structural_invariants() -> None:
    takeover_lua = (REPO_ROOT / "lua" / "replay_takeover.lua").read_text(encoding="utf-8")
    bridge_lua = (REPO_ROOT / "lua" / "grouflon_training_bridge.lua").read_text(encoding="utf-8")

    # Dot countdown and persistent bottom LABEL strip removed
    assert "draw_dot" not in takeover_lua
    assert 'elseif LABEL ~= ""' not in takeover_lua

    # New UX + CPS-3 sprite-scramble fix present
    assert "FC_NOTE" in takeover_lua
    assert "freeze_settle_frames" in takeover_lua
    assert "Full note in Start > Notes" in takeover_lua
    assert 'STARTING IN %.2fs' in takeover_lua
    assert "get_drill_note" in takeover_lua

    # Dedicated Notes tab in Grouflon training bridge
    assert 'name = "Notes"' in bridge_lua
    assert "No note attached to this drill." in bridge_lua
    assert "_notes_scroll" in bridge_lua
