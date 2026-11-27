"""Unit & HTTP integration tests for End-to-End Share Moment Cloud Publish, Rotate & Revoke."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
import threading
from typing import Any

import pytest
import requests

from fightcade_analysis.annotations import create_annotation, get_annotation
from fightcade_analysis.companion_bridge import (
    complete_pairing,
    get_cloud_token,
    get_pairing_state,
    is_allowed_companion_origin,
    issue_pairing_code,
)
from fightcade_analysis.notes_hub import list_collated_notes
from fightcade_analysis.server import create_server
from fightcade_analysis.share_export import (
    CloudNotConnectedError,
    publish_moment_to_cloud,
    revoke_cloud_share_link,
    rotate_cloud_share_link,
)


class _FakeResp:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeCloudSession:
    """Deterministic mock for Fightcade Lab Cloud Worker API (moments + presign/PUT/complete + rotate/revoke)."""

    def __init__(self, base_url: str = "http://127.0.0.1:8787") -> None:
        self.base_url = base_url.rstrip("/")
        self.calls: list[dict[str, Any]] = []
        self.uploads: dict[str, bytes] = {}
        self.moments: dict[str, dict[str, Any]] = {}
        self._seq = 0
        self._token_seq = 0

    def request(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _FakeResp:
        hdrs = dict(headers or {})
        self.calls.append({"method": method, "url": url, "json": json, "data_len": len(data) if data else 0, "headers": hdrs})

        if method == "PUT":
            # Verify PUT NEVER sends Authorization header and sends exact Content-Length + Content-Type
            assert "Authorization" not in hdrs
            assert data is not None and len(data) > 0
            assert int(hdrs["Content-Length"]) == len(data)
            self.uploads[url] = data
            return _FakeResp(200, {"ok": True, "size_bytes": len(data)})

        # JSON API calls MUST include Bearer cloud_token
        assert str(hdrs.get("Authorization") or "").startswith("Bearer fcl_")

        path = url.replace(self.base_url, "")
        if method == "POST" and path == "/api/moments":
            self._seq += 1
            self._token_seq += 1
            mid = f"m_cloud_{self._seq:04d}"
            tok = f"s_tok_{self._token_seq:04d}"
            body = json or {}
            assert body.get("kind") in {"mistake", "question", "good_play", "opp_habit", "note"}
            m_doc = {
                "id": mid,
                "visibility": body.get("visibility", "unlisted"),
                "share_token": tok,
                "share_url": f"{self.base_url}/m/{tok}",
                "shared_caption": body.get("shared_caption", ""),
                "include_note_on_share": bool(body.get("include_note_on_share", False)),
                "hide_player_names": bool(body.get("hide_player_names", False)),
            }
            self.moments[mid] = m_doc
            return _FakeResp(200, {"ok": True, "moment": m_doc})

        if method == "POST" and path == "/api/uploads/presign":
            body = json or {}
            kind = str(body["artifact_kind"])
            upl_id = f"upl_{kind}_{len(self.calls)}"
            # Exercise both worker_signed_put and r2_sigv4 URL styles
            if kind == "clip":
                upl_url = f"{self.base_url}/api/uploads/put/{upl_id}?token=sig_grant"
            else:
                upl_url = f"https://r2.example.invalid/moments/{upl_id}?X-Amz-Signature=deadbeef"
            return _FakeResp(
                200,
                {
                    "ok": True,
                    "upload_id": upl_id,
                    "r2_key": f"moments/u1/{body['moment_id']}/{upl_id}",
                    "artifact_kind": kind,
                    "upload_url": upl_url,
                    "method": "PUT",
                    "required_headers": {
                        "Content-Type": body["content_type"],
                        "Content-Length": str(body["content_length"]),
                    },
                },
            )

        if method == "POST" and path == "/api/uploads/complete":
            body = json or {}
            return _FakeResp(200, {"ok": True, "upload_id": body["upload_id"], "moment_id": body["moment_id"]})

        if method == "POST" and path.startswith("/api/moments/") and path.endswith("/rotate-share"):
            mid = path.split("/")[3]
            m_doc = self.moments[mid]
            self._token_seq += 1
            new_tok = f"s_tok_{self._token_seq:04d}"
            m_doc["share_token"] = new_tok
            m_doc["share_url"] = f"{self.base_url}/m/{new_tok}"
            m_doc["visibility"] = "unlisted"
            return _FakeResp(200, {"ok": True, "moment": dict(m_doc)})

        if method == "PATCH" and path.startswith("/api/moments/"):
            mid = path.split("/")[3]
            m_doc = self.moments[mid]
            body = json or {}
            if "shared_caption" in body:
                m_doc["shared_caption"] = body["shared_caption"]
            if "include_note_on_share" in body:
                m_doc["include_note_on_share"] = bool(body["include_note_on_share"])
            if "hide_player_names" in body:
                m_doc["hide_player_names"] = bool(body["hide_player_names"])
            if "visibility" in body:
                m_doc["visibility"] = body["visibility"]
                if body["visibility"] == "private":
                    m_doc["share_token"] = None
                    m_doc["share_url"] = None
                elif not m_doc.get("share_token"):
                    self._token_seq += 1
                    new_tok = f"s_tok_{self._token_seq:04d}"
                    m_doc["share_token"] = new_tok
                    m_doc["share_url"] = f"{self.base_url}/m/{new_tok}"
            return _FakeResp(200, {"ok": True, "moment": dict(m_doc)})

        return _FakeResp(404, {"ok": False, "error": f"Unhandled mock route: {method} {path}"})


def _pair_with_cloud_token(data_dir: Path, cloud_token: str = "fcl_cloud_test_token_12345") -> None:
    issued = issue_pairing_code(data_dir)
    complete_pairing(
        issued["code"],
        origin="https://fightcadelab.com",
        cloud_token=cloud_token,
        data_dir=data_dir,
    )
    assert get_cloud_token(data_dir) == cloud_token


def _seed_fake_bundle(tmp_path: Path, *, has_overlay: bool = True) -> dict[str, Any]:
    bdir = tmp_path / "share_exports" / "sh_test_bundle"
    bdir.mkdir(parents=True, exist_ok=True)
    clip_p = bdir / "clip.mp4"
    clip_p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"X" * 2048)
    ov_p = bdir / "overlay.json.gz"
    ov_payload = {"v": 1, "w": 384, "h": 224, "start_gf": 1100, "frame_count": 1 if has_overlay else 0, "frames": [[0, 0, [], [], []]] if has_overlay else []}
    ov_p.write_bytes(gzip.compress(json.dumps(ov_payload).encode("utf-8"), mtime=0))
    thumb_p = bdir / "thumb.jpg"
    thumb_p.write_bytes(b"\xff\xd8\xff\xe0" + b"J" * 512)
    return {
        "ok": True,
        "bundle_id": "sh_test_bundle",
        "quark_id": "1795572328477-6666",
        "game_id": "sfiii3nr1",
        "round_id": 1,
        "perspective": 1,
        "start_gf": 1100,
        "end_gf": 1280,
        "anchor_gf": 1190,
        "duration_s": 3.0,
        "width": 768,
        "height": 448,
        "dar": "4:3",
        "sar": "7:9",
        "p1_name": "player1",
        "p2_name": "Chuxster22",
        "p1_char": "Akuma",
        "p2_char": "Ken",
        "self_char": "Akuma",
        "opp_char": "Ken",
        "title": "Anti-air timing",
        "question": "",
        "note": "Private study note",
        "tags": ["Anti-Air"],
        "kind": "mistake",
        "has_hitboxes": True,
        "has_overlay": has_overlay,
        "total_bytes": clip_p.stat().st_size + ov_p.stat().st_size + thumb_p.stat().st_size,
        "artifacts": {
            "clip": {"filename": "clip.mp4", "path": str(clip_p), "url": "/share_exports/sh_test_bundle/clip.mp4", "content_type": "video/mp4", "size_bytes": clip_p.stat().st_size},
            "overlay": {"filename": "overlay.json.gz", "path": str(ov_p), "url": "/share_exports/sh_test_bundle/overlay.json.gz", "content_type": "application/gzip", "size_bytes": ov_p.stat().st_size},
            "thumb": {"filename": "thumb.jpg", "path": str(thumb_p), "url": "/share_exports/sh_test_bundle/thumb.jpg", "content_type": "image/jpeg", "size_bytes": thumb_p.stat().st_size, "width": 768, "height": 576},
        },
    }


def test_is_allowed_companion_origin_honors_fc_cloud_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FC_CLOUD_BASE_URL", raising=False)
    monkeypatch.delenv("FC_EXTRA_ALLOWED_ORIGINS", raising=False)
    assert is_allowed_companion_origin("https://fightcadelab.com") is True
    assert is_allowed_companion_origin("http://127.0.0.1:8787", host_header="127.0.0.1:8765") is False

    monkeypatch.setenv("FC_CLOUD_BASE_URL", "http://127.0.0.1:8787")
    assert is_allowed_companion_origin("http://127.0.0.1:8787", host_header="127.0.0.1:8765") is True
    assert is_allowed_companion_origin("http://127.0.0.1:9999", host_header="127.0.0.1:8765") is False
    assert is_allowed_companion_origin("https://evil.com", host_header="127.0.0.1:8765") is False


def test_publish_moment_full_sequence_update_rotate_and_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_with_cloud_token(tmp_path)
    pairing_st = get_pairing_state(tmp_path)
    assert pairing_st["has_cloud_token"] is True
    assert "cloud_token" not in pairing_st

    ann = create_annotation(
        {
            "quark_id": "1795572328477-6666",
            "round_id": 1,
            "perspective": 1,
            "anchor_gf": 1190,
            "start_gf": 1100,
            "end_gf": 1280,
            "kind": "mistake",
            "tags": ["Anti-Air"],
            "note": "Keep this private unless opted in",
        },
        data_dir=tmp_path,
        async_bake=False,
    )

    fake_bundle = _seed_fake_bundle(tmp_path, has_overlay=True)
    monkeypatch.setattr("fightcade_analysis.share_export.prepare_share_bundle", lambda *a, **kw: fake_bundle)

    sess = _FakeCloudSession("http://127.0.0.1:8787")
    res = publish_moment_to_cloud(
        {
            "moment_id": ann["id"],
            "quark_id": ann["quark_id"],
            "round_id": 1,
            "shared_caption": "How do you anti-air this spacing?",
            "include_note_on_share": False,
            "hide_player_names": True,
        },
        data_dir=tmp_path,
        cloud_base_url="http://127.0.0.1:8787",
        session=sess,
    )

    assert res["ok"] is True
    assert res["uploaded"] is True
    assert res["uploaded_artifacts"] == ["clip", "overlay", "thumb"]
    assert res["cloud_moment_id"] == "m_cloud_0001"
    assert res["cloud_share_token"] == "s_tok_0001"
    assert res["cloud_share_url"] == "http://127.0.0.1:8787/m/s_tok_0001"
    assert res["cloud_visibility"] == "unlisted"

    # Verify exact sequence: POST /api/moments + 3x (presign -> PUT -> complete)
    seq = [(c["method"], c["url"].split("?")[0].replace("http://127.0.0.1:8787", "")) for c in sess.calls]
    assert seq[0] == ("POST", "/api/moments")
    assert [m for m, _ in seq[1:]] == ["POST", "PUT", "POST", "POST", "PUT", "POST", "POST", "PUT", "POST"]
    assert sess.calls[0]["json"]["shared_caption"] == "How do you anti-air this spacing?"
    assert sess.calls[0]["json"]["include_note_on_share"] is False
    assert sess.calls[0]["json"]["hide_player_names"] is True

    # Verify persisted locally onto annotations.jsonl & surfaced in notes_hub
    saved = get_annotation(ann["id"], data_dir=tmp_path)
    assert saved is not None
    assert saved["cloud_moment_id"] == "m_cloud_0001"
    assert saved["cloud_share_token"] == "s_tok_0001"
    assert saved["cloud_share_url"] == "http://127.0.0.1:8787/m/s_tok_0001"
    assert saved["cloud_visibility"] == "unlisted"
    assert saved["shared_caption"] == "How do you anti-air this spacing?"
    assert saved["include_note_on_share"] is False
    assert saved["hide_player_names"] is True

    collated = list_collated_notes(data_dir=tmp_path)["notes"]
    hub_row = next(n for n in collated if n["id"] == ann["id"])
    assert hub_row["cloud_share_url"] == "http://127.0.0.1:8787/m/s_tok_0001"
    assert hub_row["cloud_visibility"] == "unlisted"
    assert hub_row["hide_player_names"] is True

    # Updating privacy/caption on already-published moment MUST PATCH without re-uploading media
    prev_call_count = len(sess.calls)
    res_upd = publish_moment_to_cloud(
        {
            "moment_id": ann["id"],
            "shared_caption": "Updated caption",
            "include_note_on_share": True,
            "hide_player_names": False,
        },
        data_dir=tmp_path,
        cloud_base_url="http://127.0.0.1:8787",
        session=sess,
    )
    assert res_upd["ok"] is True
    assert res_upd["updated"] is True
    assert res_upd["uploaded"] is False
    assert len(sess.calls) == prev_call_count + 1
    assert sess.calls[-1]["method"] == "PATCH"

    # Rotate share link -> new token persisted locally
    res_rot = rotate_cloud_share_link(
        ann["id"],
        data_dir=tmp_path,
        cloud_base_url="http://127.0.0.1:8787",
        session=sess,
    )
    assert res_rot["ok"] is True
    assert res_rot["cloud_share_token"] == "s_tok_0002"
    assert res_rot["cloud_share_url"] == "http://127.0.0.1:8787/m/s_tok_0002"
    saved_rot = get_annotation(ann["id"], data_dir=tmp_path)
    assert saved_rot is not None and saved_rot["cloud_share_token"] == "s_tok_0002"

    # Revoke share link -> cloud_share_url & token cleared, visibility = private
    res_rev = revoke_cloud_share_link(
        ann["id"],
        data_dir=tmp_path,
        cloud_base_url="http://127.0.0.1:8787",
        session=sess,
    )
    assert res_rev["ok"] is True
    assert res_rev["cloud_share_token"] is None
    assert res_rev["cloud_share_url"] is None
    assert res_rev["cloud_visibility"] == "private"
    saved_rev = get_annotation(ann["id"], data_dir=tmp_path)
    assert saved_rev is not None
    assert saved_rev["cloud_share_url"] is None
    assert saved_rev["cloud_share_token"] is None
    assert saved_rev["cloud_visibility"] == "private"


def test_publish_without_cloud_token_returns_401_cloud_not_connected(tmp_path: Path) -> None:
    with pytest.raises(CloudNotConnectedError):
        publish_moment_to_cloud({"quark_id": "1795572328477-6666", "round_id": 1}, data_dir=tmp_path)

    srv = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        caps = requests.get(f"{base}/api/capabilities", timeout=5).json()
        assert caps["has_cloud_token"] is False
        assert "cloud_token" not in caps

        r_pub = requests.post(
            f"{base}/api/share/publish",
            json={"quark_id": "1795572328477-6666", "round_id": 1},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_pub.status_code == 401
        body = r_pub.json()
        assert body["ok"] is False
        assert body["code"] == "cloud_not_connected"

        # /api/companion/pair-start alias works locally
        r_start = requests.post(f"{base}/api/companion/pair-start", json={}, headers={"Origin": base}, timeout=5)
        assert r_start.status_code == 200
        assert "/pair?code=" in r_start.json()["pair_url"]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)


def test_takeover_response_moment_publish_reuses_response_clip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair_with_cloud_token(tmp_path)

    resp_id = "resp_20261126_140000_abcd"
    resp_dir = tmp_path / "responses" / resp_id
    resp_dir.mkdir(parents=True, exist_ok=True)
    clip_bytes = b"\x00\x00\x00\x18ftypmp42" + b"R" * 4096
    (resp_dir / "clip.mp4").write_bytes(clip_bytes)
    (resp_dir / "overlay.json").write_text(
        json.dumps(
            {
                "v": 1,
                "w": 384,
                "h": 224,
                "start_gf": 17660,
                "frame_count": 2,
                "frames": [
                    [0, 0, [], [], [[1, 100, 80, 40, 30]]],
                    [0, 0, [], [], [[0, 110, 82, 35, 32]]],
                ],
            }
        ),
        encoding="utf-8",
    )

    # Seed takeover_response record directly into annotations.jsonl
    ann = create_annotation(
        {
            "id": resp_id,
            "source": "takeover_response",
            "quark_id": "1795443936010-2584",
            "round_id": 2,
            "perspective": 1,
            "anchor_gf": 17660,
            "start_gf": 17660,
            "end_gf": 17750,
            "kind": "response",
            "note": "Delayed crouch tech + punish",
            "response_clip_url": f"/responses/{resp_id}/clip.mp4",
            "response_overlay_url": f"/responses/{resp_id}/overlay.json",
            "frame_count": 91,
        },
        data_dir=tmp_path,
        async_bake=False,
    )

    def _fake_thumb(_src: Path, out_jpg: Path, **_kw: Any) -> int:
        out_jpg.parent.mkdir(parents=True, exist_ok=True)
        payload = b"\xff\xd8\xff\xe0" + b"T" * 600
        out_jpg.write_bytes(payload)
        return len(payload)

    monkeypatch.setattr("fightcade_analysis.share_export.render_hitbox_thumb_jpg", _fake_thumb)

    sess = _FakeCloudSession("http://127.0.0.1:8787")
    res = publish_moment_to_cloud(
        {
            "moment_id": ann["id"],
            "source": "takeover_response",
            "shared_caption": "Clean parry punish labbed in Takeover",
        },
        data_dir=tmp_path,
        cloud_base_url="http://127.0.0.1:8787",
        session=sess,
    )

    assert res["ok"] is True
    assert res["uploaded"] is True
    assert res["uploaded_artifacts"] == ["clip", "overlay", "thumb"]
    # Cloud kind mapped safely from local "response" -> "note"
    assert sess.calls[0]["json"]["kind"] == "note"
    # Verify uploaded clip bytes are byte-for-byte identical to data/responses/<id>/clip.mp4
    clip_puts = [data for url, data in sess.uploads.items() if "upl_clip_" in url]
    assert len(clip_puts) == 1
    assert clip_puts[0] == clip_bytes

    saved = get_annotation(ann["id"], data_dir=tmp_path)
    assert saved is not None
    assert saved["cloud_share_url"] == "http://127.0.0.1:8787/m/s_tok_0001"
    assert saved["cloud_visibility"] == "unlisted"
