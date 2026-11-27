"""Unit & integration tests for Local Takeover Response Moments."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

from fightcade_analysis.annotations import (
    create_annotation,
    delete_annotation,
    get_annotation,
    list_annotations,
)
from fightcade_analysis.notes_hub import list_collated_notes
from fightcade_analysis.playlists import create_playlist, list_playlists, mutate_playlist_items
from fightcade_analysis.server import create_server
from fightcade_analysis.takeover import TakeoverManager, validate_takeover_payload
from fightcade_analysis.takeover_responses import (
    TakeoverExportWatcher,
    compute_perspective_hp_deltas,
    render_takeover_response,
    takeover_exports_dir,
    validate_takeover_export,
)


def _seed_export(
    data_dir: Path,
    *,
    export_id: str = "resp_20261126_120000_ab12",
    parent_moment_id: str | None = "ann_parent_001",
    quark_id: str = "1795443936010-2584",
    round_id: int = 2,
    perspective: int = 1,
    start_gf: int = 17660,
    frame_count: int = 90,
    start_p1_hp: int = 160,
    start_p2_hp: int = 160,
    end_p1_hp: int = 160,
    end_p2_hp: int = 88,
) -> tuple[Path, Path, Path]:
    sel_dir = data_dir / "selections"
    exp_dir = takeover_exports_dir(data_dir)
    sel_dir.mkdir(parents=True, exist_ok=True)
    exp_dir.mkdir(parents=True, exist_ok=True)

    fs_path = sel_dir / f"{quark_id}_p{perspective}_gf{start_gf}.fs"
    fs_path.write_bytes(b"FBNEO_SAVESTATE_STUB" + (b"\x00" * 5000))

    inp_path = exp_dir / f"{export_id}.inputs"
    lines = [f"{start_gf + i} 0 0\n" for i in range(frame_count)]
    inp_path.write_text("".join(lines), encoding="utf-8")

    json_path = exp_dir / f"{export_id}.json"
    doc = {
        "export_id": export_id,
        "parent_moment_id": parent_moment_id or "",
        "quark_id": quark_id,
        "round_id": round_id,
        "perspective": perspective,
        "savestate_path": str(fs_path),
        "inputs_path": str(inp_path),
        "start_gf": start_gf,
        "end_gf": start_gf + frame_count - 1,
        "frame_count": frame_count,
        "start_p1_hp": start_p1_hp,
        "start_p2_hp": start_p2_hp,
        "end_p1_hp": end_p1_hp,
        "end_p2_hp": end_p2_hp,
    }
    json_path.write_text(json.dumps(doc), encoding="utf-8")
    return json_path, fs_path, inp_path


def _fake_render_media(**kwargs):
    resp_dir: Path = kwargs["resp_dir"]
    clip_path = resp_dir / "clip.mp4"
    overlay_path = resp_dir / "overlay.json"
    clip_path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + (b"A" * 4096))
    overlay_path.write_text(
        json.dumps({"start_gf": kwargs["start_gf"], "frames": [{"gf": kwargs["start_gf"], "boxes": []}]}),
        encoding="utf-8",
    )
    return {
        "clip_path": clip_path,
        "overlay_path": overlay_path,
        "first_gf": kwargs["start_gf"],
        "last_gf": kwargs["end_gf"],
        "frame_count": max(1, kwargs["end_gf"] - kwargs["start_gf"] + 1),
        "has_audio": True,
        "render_s": 0.42,
    }


def test_validate_takeover_export_and_hp_deltas(tmp_path: Path):
    data_dir = tmp_path / "data"
    json_path, fs_path, inp_path = _seed_export(
        data_dir, perspective=2, start_p1_hp=160, end_p1_hp=90, start_p2_hp=150, end_p2_hp=140
    )
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    parsed = validate_takeover_export(raw, data_dir=data_dir)
    assert parsed["export_id"] == raw["export_id"]
    assert parsed["perspective"] == 2
    assert parsed["savestate_path"] == fs_path.resolve()
    assert parsed["inputs_path"] == inp_path.resolve()

    # Perspective P2: self is P2 (-10), opp is P1 (-70)
    assert compute_perspective_hp_deltas(parsed) == (-10, -70)
    # Perspective P1: self is P1 (-70), opp is P2 (-10)
    parsed_p1 = {**parsed, "perspective": 1}
    assert compute_perspective_hp_deltas(parsed_p1) == (-70, -10)

    # Reject path traversal outside data_dir
    outside_fs = tmp_path / "outside.fs"
    outside_fs.write_bytes(b"nope")
    bad_path = {**raw, "savestate_path": str(outside_fs)}
    with pytest.raises(ValueError, match="escapes data_dir"):
        validate_takeover_export(bad_path, data_dir=data_dir)

    # Reject short reps (<15 frames)
    with pytest.raises(ValueError, match="frame_count"):
        validate_takeover_export({**raw, "frame_count": 10}, data_dir=data_dir)

    # Reject invalid quark_id
    with pytest.raises(ValueError, match="quark_id"):
        validate_takeover_export({**raw, "quark_id": "../bad"}, data_dir=data_dir)


def test_render_takeover_response_lifecycle_and_delete(tmp_path: Path):
    data_dir = tmp_path / "data"
    parent = create_annotation(
        {
            "owner": "sixtwo1",
            "quark_id": "1795443936010-2584",
            "round_id": 3,
            "perspective": 1,
            "anchor_gf": 17700,
            "start_gf": 17660,
            "end_gf": 17820,
            "kind": "mistake",
            "note": "Ate low forward",
        },
        data_dir=data_dir,
        async_bake=False,
    )
    json_path, _, inp_path = _seed_export(
        data_dir,
        export_id="resp_test_001",
        parent_moment_id=parent["id"],
        round_id=0,  # Should inherit round_id=3 from parent!
    )

    res = render_takeover_response(json_path, data_dir=data_dir, render_media_fn=_fake_render_media)
    assert res["ok"] is True
    assert res["response_id"] == "resp_test_001"

    resp_dir = data_dir / "responses" / "resp_test_001"
    assert (resp_dir / "start.fs").is_file()
    assert (resp_dir / "rep.inputs").is_file()
    assert (resp_dir / "clip.mp4").is_file()
    assert (resp_dir / "overlay.json").is_file()

    # Export files archived to processed/
    assert not json_path.exists()
    assert not inp_path.exists()
    assert (takeover_exports_dir(data_dir) / "processed" / json_path.name).is_file()
    assert (takeover_exports_dir(data_dir) / "processed" / inp_path.name).is_file()

    ann = get_annotation("resp_test_001", data_dir=data_dir)
    assert ann is not None
    assert ann["kind"] == "response"
    assert ann["source"] == "takeover_response"
    assert ann["parent_moment_id"] == parent["id"]
    assert ann["round_id"] == 3
    assert ann["owner"] == "sixtwo1"
    assert ann["response_clip_url"] == "/responses/resp_test_001/clip.mp4"
    assert ann["response_overlay_url"] == "/responses/resp_test_001/overlay.json"
    assert ann["hp_delta_self"] == 0
    assert ann["hp_delta_opp"] == -72
    assert ann["frame_count"] == 90

    # Delete removes both annotation record and data/responses/resp_test_001/
    assert delete_annotation("resp_test_001", data_dir=data_dir) is True
    assert get_annotation("resp_test_001", data_dir=data_dir) is None
    assert not resp_dir.exists()


def test_watcher_success_and_quarantine_failure(tmp_path: Path):
    data_dir = tmp_path / "data"
    ok_json, _, _ = _seed_export(data_dir, export_id="resp_ok_1", parent_moment_id=None)

    # Also drop a corrupt JSON to verify quarantine without looping
    bad_json = takeover_exports_dir(data_dir) / "resp_bad_1.json"
    bad_json.write_text("{not valid json", encoding="utf-8")

    watcher = TakeoverExportWatcher(
        data_dir=data_dir,
        render_fn=lambda jp, **kw: render_takeover_response(jp, data_dir=data_dir, render_media_fn=_fake_render_media),
    )
    processed = watcher.process_once()
    assert processed == 1

    st = watcher.status()
    assert st["last_response_id"] == "resp_ok_1"
    assert st["last_response_at"] is not None
    assert st["pending_responses"] == 0
    assert st["processing"] is False
    assert watcher.is_busy() is False

    failed_dir = takeover_exports_dir(data_dir) / "failed"
    assert (failed_dir / "resp_bad_1.json").is_file()
    assert (failed_dir / "resp_bad_1.err").is_file()
    # Second poll finds nothing left to process
    assert watcher.process_once() == 0


def test_notes_hub_and_playlists_routing_for_responses(tmp_path: Path):
    data_dir = tmp_path / "data"
    json_path, _, _ = _seed_export(data_dir, export_id="resp_hub_001", parent_moment_id="ann_parent_xyz")
    render_takeover_response(json_path, data_dir=data_dir, render_media_fn=_fake_render_media)

    hub = list_collated_notes(username="sixtwo1", data_dir=data_dir)
    resp_notes = [n for n in hub["notes"] if n["id"] == "resp_hub_001"]
    assert len(resp_notes) == 1
    rn = resp_notes[0]
    assert rn["source"] == "takeover_response"
    assert rn["kind"] == "response"
    assert rn["is_response_clip"] is True
    assert rn["has_video"] is True
    assert rn["video_url"] == "/responses/resp_hub_001/clip.mp4"
    assert rn["overlay_url"] == "/responses/resp_hub_001/overlay.json"
    assert rn["clip_start_s"] == 0.0
    assert rn["parent_moment_id"] == "ann_parent_xyz"
    assert rn["hp_delta_opp"] == -72
    assert rn["can_launch_drill"] is True
    assert rn["needs_note"] is False
    assert hub["counts"]["by_kind"].get("response") == 1

    # Add to a Playlist and verify enriched watch fields
    pl = create_playlist({"name": "Counterplay Reel"}, data_dir=data_dir)
    mutate_playlist_items(
        pl["id"],
        {
            "action": "add",
            "item": {
                "item_id": "resp_hub_001",
                "source": "takeover_response",
                "quark_id": "1795443936010-2584",
                "round_id": 2,
                "perspective": 1,
                "start_gf": 17660,
                "end_gf": 17749,
                "anchor_gf": 17660,
                "parent_moment_id": "ann_parent_xyz",
                "response_clip_url": "/responses/resp_hub_001/clip.mp4",
                "response_overlay_url": "/responses/resp_hub_001/overlay.json",
                "label": "Punish cr.MK",
            },
        },
        data_dir=data_dir,
    )
    pls = list_playlists(data_dir=data_dir)
    item = pls[0]["items"][0]
    assert item["source"] == "takeover_response"
    assert item["is_response_clip"] is True
    assert item["is_standalone_clip"] is False
    assert item["has_video"] is True
    assert item["video_url"] == "/responses/resp_hub_001/clip.mp4"
    assert item["overlay_url"] == "/responses/resp_hub_001/overlay.json"
    assert item["clip_start_s"] == 0.0
    assert item["round_start_gf"] == 17660
    assert item["savestate_ready"] is True


def test_takeover_validation_and_response_launch_env(tmp_path: Path, monkeypatch):
    data_dir = tmp_path / "data"
    json_path, _, _ = _seed_export(data_dir, export_id="resp_launch_01", parent_moment_id="ann_parent_777")
    render_takeover_response(json_path, data_dir=data_dir, render_media_fn=_fake_render_media)

    cleaned = validate_takeover_payload(
        {"selection_id": "resp_launch_01", "mode": "watch", "parent_moment_id": "ann_parent_777", "round_id": 2}
    )
    assert cleaned["parent_moment_id"] == "ann_parent_777"
    assert cleaned["round_id"] == 2
    assert cleaned["mode"] == "watch"

    with pytest.raises(ValueError, match="parent_moment_id"):
        validate_takeover_payload({"selection_id": "resp_launch_01", "parent_moment_id": "../evil"})

    fake_exe = tmp_path / "fcadefbneo.exe"
    fake_exe.write_bytes(b"MZ")
    fake_lua = tmp_path / "replay_takeover.lua"
    fake_lua.write_text("-- stub", encoding="utf-8")

    captured: dict[str, object] = {}

    class _FakeProc:
        pid = 42424

        def poll(self):
            return None

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("fightcade_analysis.takeover.subprocess.Popen", _fake_popen)
    monkeypatch.setattr("fightcade_analysis.takeover.ensure_fbneo_config_inheritance", lambda *a, **k: None)

    mgr = TakeoverManager(
        data_dir=data_dir,
        fbneo_exe=fake_exe,
        replay_takeover_lua=fake_lua,
        popen_fn=_fake_popen,
    )
    res = mgr.launch(
        {
            "selection_id": "resp_launch_01",
            "mode": "watch",
            "parent_moment_id": "ann_parent_777",
            "round_id": 2,
        }
    )
    assert res["ok"] is True
    assert res["mode"] == "watch"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["FC_WATCH_ONLY"] == "1"
    assert env["FC_PARENT_MOMENT_ID"] == "ann_parent_777"
    assert env["FC_QUARK_ID"] == "1795443936010-2584"
    assert env["FC_ROUND_ID"] == "2"
    assert Path(env["FC_EXPORT_DIR"]).resolve() == takeover_exports_dir(data_dir).resolve()
    assert Path(env["FC_INPUTS_PATH"]).resolve() == (data_dir / "responses" / "resp_launch_01" / "rep.inputs").resolve()


def test_server_serves_responses_range_gzip_security_and_status(tmp_path: Path):
    data_dir = tmp_path / "data"
    json_path, _, _ = _seed_export(data_dir, export_id="resp_http_01", parent_moment_id="ann_p1")
    watcher = TakeoverExportWatcher(
        data_dir=data_dir,
        render_fn=lambda jp, **kw: render_takeover_response(jp, data_dir=data_dir, render_media_fn=_fake_render_media),
    )
    assert watcher.process_once() == 1

    server = create_server(host="127.0.0.1", port=0, data_dir=data_dir, response_watcher=watcher)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"

        # 1. Range request on clip.mp4 -> 206 Partial Content
        req_mp4 = urllib.request.Request(f"{base}/responses/resp_http_01/clip.mp4", headers={"Range": "bytes=0-127"})
        with urllib.request.urlopen(req_mp4) as r:
            assert r.status == 206
            assert len(r.read()) == 128

        # 2. Gzip request on overlay.json
        req_ov = urllib.request.Request(
            f"{base}/responses/resp_http_01/overlay.json",
            headers={"Accept-Encoding": "gzip"},
        )
        with urllib.request.urlopen(req_ov) as r:
            assert r.status == 200
            assert r.headers.get("Content-Encoding") == "gzip"
            body = json.loads(gzip.decompress(r.read()).decode("utf-8"))
            assert body["start_gf"] == 17660

        # 3. Traversal blocked (403/404)
        with pytest.raises(urllib.error.HTTPError) as exc_trav:
            urllib.request.urlopen(f"{base}/responses/../annotations.jsonl")
        assert exc_trav.value.code in (403, 404)

        # 4. Cross-origin GET blocked without API token
        req_xo = urllib.request.Request(
            f"{base}/responses/resp_http_01/overlay.json",
            headers={"Origin": "https://evil.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_xo:
            urllib.request.urlopen(req_xo)
        assert exc_xo.value.code == 403

        # 5. /api/status exposes watcher telemetry
        with urllib.request.urlopen(f"{base}/api/status") as r:
            st = json.loads(r.read().decode("utf-8"))
            assert st["last_response_id"] == "resp_http_01"
            assert st["last_response_at"] is not None
            assert st["pending_responses"] == 0
    finally:
        server.shutdown()
        server.server_close()
