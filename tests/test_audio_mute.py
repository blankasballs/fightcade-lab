"""Unit tests for Windows per-PID WASAPI audio muting helper."""

from __future__ import annotations

import threading

from fightcade_analysis.audio_mute import _make_guid, mute_process_audio, start_mute_watcher


def test_make_guid_parses_standard_com_guids():
    g = _make_guid("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
    assert g.Data1 == 0x87CE5498
    assert g.Data2 == 0x68D6
    assert g.Data3 == 0x44E5
    assert list(g.Data4) == [0x92, 0x15, 0x6D, 0xA4, 0x7E, 0xF8, 0x83, 0xD8]


def test_mute_nonexistent_pid_is_safe_noop():
    assert mute_process_audio(0) is False
    assert mute_process_audio(99999999) is False


def test_start_mute_watcher_stops_cleanly():
    ev = threading.Event()
    t = start_mute_watcher(99999999, ev, poll_interval=0.02, max_watch_seconds=5.0)
    ev.set()
    t.join(timeout=1.0)
    assert not t.is_alive()
