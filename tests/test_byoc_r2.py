"""Tests for BYOC Part 2: Pure-Python R2 SigV4 client, rolling cap, mobile note pull, local API & hosted UI funnel."""

from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import subprocess
import threading
from typing import Any
from urllib.parse import urlsplit

import pytest
import requests

from fightcade_analysis.annotations import list_annotations
from fightcade_analysis.byoc_r2 import (
    ByocCloudAuthError,
    build_cors_xml,
    build_r2_endpoint,
    connect_byoc,
    get_byoc_status,
    load_byoc_config,
    pull_cloud_notes_to_local,
    put_bucket_cors,
    sign_r2_request,
    sync_match_to_byoc,
    validate_byoc_key,
)
from fightcade_analysis.companion_bridge import complete_pairing, issue_pairing_code
from fightcade_analysis.server import create_server

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "src" / "fightcade_analysis" / "web"
INDEX_HTML = WEB_DIR / "index.html"
STATE_JS = WEB_DIR / "js" / "state.js"
STATUS_POP_JS = WEB_DIR / "js" / "status_popover.js"
HOME_JS = WEB_DIR / "js" / "home.js"
MATCHES_JS = WEB_DIR / "js" / "matches.js"
APP_JS = WEB_DIR / "js" / "app.js"

VALID_ACCOUNT = "0123456789abcdef0123456789abcdef"
VALID_BUCKET = "my-3s-replays"
VALID_AK = "r2_primary_access_key"
VALID_SK = "r2_primary_secret_key_99999"
VALID_READ_AK = "r2_readonly_access_key"
VALID_READ_SK = "r2_readonly_secret_key_88888"


class _FakeResp:
    def __init__(self, status_code: int = 200, json_data: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data
        self.text = text or (json.dumps(json_data) if json_data is not None else "")

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict[str, Any]:
        if self._json is None:
            raise ValueError("No JSON body")
        return self._json


class _FakeSession:
    """Offline mock session recording all R2 & Fightcade Lab cloud HTTP calls."""

    def __init__(self, *, cloud_moments: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.r2_objects: dict[str, bytes] = {}
        self.cloud_moments: list[dict[str, Any]] = list(cloud_moments or [])

    def _record(self, method: str, url: str, headers: dict[str, str] | None = None, data: Any = None, json_body: Any = None):
        rec = {
            "method": method.upper(),
            "url": url,
            "headers": dict(headers or {}),
            "data": data,
            "json": json_body,
        }
        self.calls.append(rec)
        return rec

    def head(self, url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> _FakeResp:
        self._record("HEAD", url, headers=headers)
        return _FakeResp(200)

    def put(self, url: str, headers: dict[str, str] | None = None, data: bytes = b"", timeout: float = 15.0) -> _FakeResp:
        self._record("PUT", url, headers=headers, data=data)
        parsed = urlsplit(url)
        if parsed.query != "cors=":
            self.r2_objects[parsed.path] = bytes(data)
        return _FakeResp(200)

    def delete(self, url: str, headers: dict[str, str] | None = None, timeout: float = 15.0) -> _FakeResp:
        self._record("DELETE", url, headers=headers)
        parsed = urlsplit(url)
        self.r2_objects.pop(parsed.path, None)
        return _FakeResp(204)

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> _FakeResp:
        self._record(method, url, headers=headers, json_body=json)
        if method.upper() == "GET" and url.endswith("/api/moments"):
            return _FakeResp(200, {"ok": True, "moments": self.cloud_moments})
        return _FakeResp(200, {"ok": True})


def _pair_with_cloud_token(data_dir: Path, cloud_token: str = "fcl_cloud_test_token_1234567890") -> None:
    issued = issue_pairing_code(data_dir)
    complete_pairing(
        issued["code"],
        origin="https://fightcadelab.com",
        cloud_token=cloud_token,
        data_dir=data_dir,
    )


def _create_fake_local_replay(
    data_dir: Path,
    qid: str,
    *,
    played_at: int,
    mp4_size: int = 1024,
    with_overlay: bool = True,
) -> Path:
    rep_dir = data_dir / "replays" / qid
    rep_dir.mkdir(parents=True, exist_ok=True)
    mp4_path = rep_dir / "r01.mp4"
    mp4_path.write_bytes(b"\x00\x00\x00\x18ftypisom" + (b"M" * max(0, mp4_size - 12)))
    if with_overlay:
        ov_path = rep_dir / "r01_overlay.json"
        ov_path.write_text(json.dumps({"start_gf": 1000, "frames": [[0, 0, [160], [160], []]]}), encoding="utf-8")
    mf = {
        "version": 3,
        "quark_id": qid,
        "date": played_at,
        "perspective": 1,
        "p1": {"side": 1, "name": "AkumaMain", "rank_letter": "B", "char_id": 14, "char_name": "Akuma", "chars": ["Akuma"]},
        "p2": {"side": 2, "name": "ChunOpp", "rank_letter": "A", "char_id": 2, "char_name": "Chun-Li", "chars": ["Chun-Li"]},
        "total_duration_s": 28.5,
        "round_count": 1,
        "rounds": [
            {
                "round_id": 1,
                "filename": "r01.mp4",
                "start_gf": 1000,
                "end_gf": 2710,
                "frame_count": 1711,
                "fps": 60,
                "duration_s": 28.5,
                "p1_char": 14,
                "p2_char": 2,
                "p1_char_name": "Akuma",
                "p2_char_name": "Chun-Li",
                "winner": 1,
                "hp_curve": [[0, 1.0, 1.0], [400, 0.8, 0.5]],
                "markers": [{"gf": 1400, "offset_f": 400, "kind": "dmg", "side": 2, "value": 80}],
            }
        ],
    }
    (rep_dir / "manifest.json").write_text(json.dumps(mf), encoding="utf-8")
    return rep_dir


def test_sigv4_signing_cors_xml_and_ssrf_guards():
    fixed_dt = datetime(2026, 11, 27, 16, 0, 0, tzinfo=timezone.utc)

    # Valid HEAD, PUT, DELETE signing on strict <32-hex>.r2.cloudflarestorage.com host
    url_h, hdr_h = sign_r2_request(
        method="HEAD",
        account_id=VALID_ACCOUNT.upper(),
        bucket_name=VALID_BUCKET,
        access_key_id=VALID_AK,
        secret_access_key=VALID_SK,
        now=fixed_dt,
    )
    assert url_h == f"https://{VALID_ACCOUNT}.r2.cloudflarestorage.com/{VALID_BUCKET}"
    assert hdr_h["X-Amz-Date"] == "20261127T160000Z"
    assert f"Credential={VALID_AK}/20261127/auto/s3/aws4_request" in hdr_h["Authorization"]
    assert "SignedHeaders=host;x-amz-content-sha256;x-amz-date" in hdr_h["Authorization"]

    # PutBucketCors XML includes https://fightcadelab.com & Range expose headers
    xml = build_cors_xml().decode("utf-8")
    assert "<AllowedOrigin>https://fightcadelab.com</AllowedOrigin>" in xml
    assert "<AllowedMethod>GET</AllowedMethod>" in xml
    assert "<AllowedMethod>HEAD</AllowedMethod>" in xml
    assert "<ExposeHeader>Content-Range</ExposeHeader>" in xml
    assert "<ExposeHeader>Accept-Ranges</ExposeHeader>" in xml

    sess = _FakeSession()
    put_bucket_cors(
        {
            "account_id": VALID_ACCOUNT,
            "bucket_name": VALID_BUCKET,
            "access_key_id": VALID_AK,
            "secret_access_key": VALID_SK,
        },
        session=sess,
    )
    assert len(sess.calls) == 1
    assert sess.calls[0]["method"] == "PUT"
    assert sess.calls[0]["url"].endswith(f"/{VALID_BUCKET}?cors=")
    assert "Content-MD5" in sess.calls[0]["headers"]
    assert "content-md5;" in sess.calls[0]["headers"]["Authorization"]

    # SSRF & traversal guards
    with pytest.raises(ValueError):
        build_r2_endpoint("evil.com/foo#", VALID_BUCKET)
    with pytest.raises(ValueError):
        build_r2_endpoint(VALID_ACCOUNT, "BAD_BUCKET/..")
    with pytest.raises(ValueError):
        validate_byoc_key("replays/1795443936010-2584/../../secret.mp4")
    with pytest.raises(ValueError):
        validate_byoc_key("/replays/1795443936010-2584/r01.mp4")
    with pytest.raises(ValueError):
        validate_byoc_key("other/r01.mp4")


def test_connect_separate_read_token_sync_rolling_eviction_and_note_pull(tmp_path: Path):
    # Without cloud_token -> raises ByocCloudAuthError
    sess = _FakeSession()
    with pytest.raises(ByocCloudAuthError):
        connect_byoc(
            {
                "account_id": VALID_ACCOUNT,
                "bucket_name": VALID_BUCKET,
                "access_key_id": VALID_AK,
                "secret_access_key": VALID_SK,
            },
            data_dir=tmp_path,
            session=sess,
        )

    _pair_with_cloud_token(tmp_path)

    # Connect with optional separate Read-Only token: cloud MUST receive ONLY the Read-Only token!
    st = connect_byoc(
        {
            "account_id": VALID_ACCOUNT,
            "bucket_name": VALID_BUCKET,
            "access_key_id": VALID_AK,
            "secret_access_key": VALID_SK,
            "read_access_key_id": VALID_READ_AK,
            "read_secret_access_key": VALID_READ_SK,
            "cap_gb": 0.5,
            "auto_sync": True,
        },
        data_dir=tmp_path,
        session=sess,
    )
    assert st["connected"] is True
    assert st["bucket_name"] == VALID_BUCKET
    assert st["has_separate_read_token"] is True
    assert VALID_SK not in json.dumps(st)
    assert VALID_READ_SK not in json.dumps(st)

    # Verify HEAD bucket + PUT ?cors= + PUT /api/byoc/config (with Read-Only creds!)
    cloud_puts = [c for c in sess.calls if c["method"] == "PUT" and "/api/byoc/config" in c["url"]]
    assert len(cloud_puts) == 1
    assert cloud_puts[0]["json"]["access_key_id"] == VALID_READ_AK
    assert cloud_puts[0]["json"]["secret_access_key"] == VALID_READ_SK

    # Sync Match 1 (with raw .json overlay -> gzipped on the fly with Content-Encoding: gzip)
    q1 = "1795000000001-1111"
    rep1 = _create_fake_local_replay(tmp_path, q1, played_at=1795000000001, mp4_size=4096, with_overlay=True)
    res1 = sync_match_to_byoc(q1, data_dir=tmp_path, session=sess)
    assert res1["ok"] is True
    assert res1["uploaded_objects"] == 2
    assert res1["has_overlay_data"] is True
    assert res1["evicted"] == []

    # Verify gzipped overlay upload header + valid gzip payload
    ov_puts = [c for c in sess.calls if c["method"] == "PUT" and c["url"].endswith(f"replays/{q1}/r01_overlay.json.gz")]
    assert len(ov_puts) == 1
    assert ov_puts[0]["headers"]["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(ov_puts[0]["data"]).decode("utf-8"))["start_gf"] == 1000

    # Verify compact manifest posted to cloud with validated video_r2_key & overlay_r2_key
    m_posts = [c for c in sess.calls if c["method"] == "POST" and "/api/byoc/matches" in c["url"]]
    assert len(m_posts) == 1
    mf_sent = m_posts[0]["json"]["manifest"]
    assert mf_sent["rounds"][0]["video_r2_key"] == f"replays/{q1}/r01.mp4"
    assert mf_sent["rounds"][0]["overlay_r2_key"] == f"replays/{q1}/r01_overlay.json.gz"

    # Simulate tight rolling cap by patching stored entry size so Match 2 triggers eviction of Match 1
    cfg = load_byoc_config(tmp_path)
    assert cfg is not None
    cfg["synced_matches"][q1]["total_bytes"] = 499_999_500  # just under 0.5 GB (500,000,000 B)
    (tmp_path / "byoc_r2.json").write_text(json.dumps(cfg), encoding="utf-8")

    q2 = "1795000000002-2222"
    _create_fake_local_replay(tmp_path, q2, played_at=1795000000002, mp4_size=4096, with_overlay=False)
    res2 = sync_match_to_byoc(q2, data_dir=tmp_path, session=sess)
    assert res2["evicted"] == [q1]

    # Verify R2 objects for q1 deleted + cloud DELETE /api/byoc/matches/q1 called, while local PC files stay untouched!
    assert any(c["method"] == "DELETE" and c["url"].endswith(f"replays/{q1}/r01.mp4") for c in sess.calls)
    assert any(c["method"] == "DELETE" and c["url"].endswith(f"/api/byoc/matches/{q1}") for c in sess.calls)
    assert (rep1 / "r01.mp4").is_file()
    assert (rep1 / "manifest.json").is_file()

    # Pull mobile-created cloud notes back to local annotations.jsonl without duplicating on repeat run
    sess.cloud_moments = [
        {
            "id": "m_cloud_mobile_1",
            "quark_id": q2,
            "round_id": 1,
            "perspective": 1,
            "anchor_gf": 1350,
            "start_gf": 1230,
            "end_gf": 1470,
            "kind": "note",
            "tags": ["Anti-Air"],
            "note": "Written on my phone on the train",
            "visibility": "private",
        }
    ]
    pull1 = pull_cloud_notes_to_local(tmp_path, session=sess)
    assert pull1 == {"ok": True, "imported": 1, "skipped": 0}
    local_anns = list_annotations(tmp_path, quark_id=q2)
    assert len(local_anns) == 1
    assert local_anns[0]["cloud_moment_id"] == "m_cloud_mobile_1"
    assert local_anns[0]["note"] == "Written on my phone on the train"
    assert local_anns[0]["tags"] == ["Anti-Air"]

    # Second pull must deduplicate cleanly
    pull2 = pull_cloud_notes_to_local(tmp_path, session=sess)
    assert pull2 == {"ok": True, "imported": 0, "skipped": 1}
    assert len(list_annotations(tmp_path, quark_id=q2)) == 1


def test_local_server_byoc_endpoints_never_leak_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "reports").mkdir()
    (tmp_path / "clips").mkdir()

    fake_sess = _FakeSession()
    monkeypatch.setattr("fightcade_analysis.byoc_r2._http_client", lambda s: fake_sess)

    server = create_server(host="127.0.0.1", port=0, data_dir=tmp_path, auto_refresh_index=False)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        hdrs = {"Origin": base}

        # 1. GET /api/byoc/status when disconnected
        r_st0 = requests.get(f"{base}/api/byoc/status", timeout=5)
        assert r_st0.status_code == 200
        assert r_st0.json()["connected"] is False

        # 2. POST /api/byoc/connect without cloud_token -> 401 cloud_not_connected
        r_no_cloud = requests.post(
            f"{base}/api/byoc/connect",
            json={
                "account_id": VALID_ACCOUNT,
                "bucket_name": VALID_BUCKET,
                "access_key_id": VALID_AK,
                "secret_access_key": VALID_SK,
            },
            headers=hdrs,
            timeout=5,
        )
        assert r_no_cloud.status_code == 401
        assert r_no_cloud.json()["code"] == "cloud_not_connected"

        # 3. Pair cloud token -> POST /api/byoc/connect succeeds & never leaks secrets
        _pair_with_cloud_token(tmp_path)
        r_conn = requests.post(
            f"{base}/api/byoc/connect",
            json={
                "account_id": VALID_ACCOUNT,
                "bucket_name": VALID_BUCKET,
                "access_key_id": VALID_AK,
                "secret_access_key": VALID_SK,
                "cap_gb": 8.0,
            },
            headers=hdrs,
            timeout=5,
        )
        assert r_conn.status_code == 200
        assert VALID_SK not in r_conn.text
        assert "secret_access_key" not in r_conn.text
        assert r_conn.json()["connected"] is True
        assert r_conn.json()["bucket_name"] == VALID_BUCKET

        # 4. /api/capabilities exposes safe byoc summary without secrets
        r_caps = requests.get(f"{base}/api/capabilities", timeout=5)
        assert r_caps.status_code == 200
        assert VALID_SK not in r_caps.text
        assert r_caps.json()["byoc"]["connected"] is True
        assert r_caps.json()["byoc"]["bucket_name"] == VALID_BUCKET

        # 5. PATCH /api/byoc/settings updates cap_gb & auto_sync
        r_set = requests.patch(
            f"{base}/api/byoc/settings",
            json={"cap_gb": 9.0, "auto_sync": False},
            headers=hdrs,
            timeout=5,
        )
        assert r_set.status_code == 200
        assert r_set.json()["cap_gb"] == 9.0
        assert r_set.json()["auto_sync"] is False

        # 6. POST /api/byoc/sync triggers cleanly
        r_sync = requests.post(f"{base}/api/byoc/sync", json={"limit": 3}, headers=hdrs, timeout=5)
        assert r_sync.status_code == 200
        assert r_sync.json()["ok"] is True

        # 7. DELETE /api/byoc/config disconnects cleanly
        r_del = requests.delete(f"{base}/api/byoc/config", headers=hdrs, timeout=5)
        assert r_del.status_code == 200
        assert r_del.json()["connected"] is False
    finally:
        server.shutdown()
        server.server_close()


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


def test_about_decentralized_explainer_hosted_funnel_and_byoc_ui():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="aboutDecentralized"' in html
    assert 'id="byocModal"' in html

    dec_start = html.index('<div id="aboutDecentralized"')
    dec_end = html.index('<div class="about-links">', dec_start)
    dec_html = html[dec_start:dec_end]
    assert "—" not in dec_html and "&mdash;" not in dec_html.lower()
    assert "How it stays free (decentralized by design)" in dec_html
    assert 'id="archJourneyLocal"' in dec_html
    assert 'id="archJourneyByoc"' in dec_html
    assert "Companion App" in dec_html
    assert "Upload replays" in dec_html

    script = f"""
const fs = require("fs");
const vm = require("vm");

const loc = {{ pathname: "/", search: "", hash: "", origin: "https://fightcadelab.com" }};
const elements = Object.create(null);
let scrolledIds = [];
function makeEl(id) {{
  if (!elements[id]) {{
    elements[id] = {{
      id,
      innerHTML: "",
      textContent: "",
      value: "",
      checked: true,
      disabled: false,
      style: {{}},
      dataset: {{}},
      _attrs: {{}},
      classList: {{
        _set: new Set(["byocModal", "viewAbout"].includes(id) ? ["hidden"] : []),
        add(...cs) {{ cs.forEach(c => this._set.add(c)); }},
        remove(...cs) {{ cs.forEach(c => this._set.delete(c)); }},
        toggle(c, force) {{
          if (force === undefined) {{ if (this._set.has(c)) this._set.delete(c); else this._set.add(c); }}
          else if (force) this._set.add(c); else this._set.delete(c);
        }},
        contains(c) {{ return this._set.has(c); }},
      }},
      setAttribute(k, v) {{ this._attrs[k] = String(v); }},
      getAttribute(k) {{ return this._attrs[k]; }},
      scrollIntoView() {{ scrolledIds.push(id); }},
      addEventListener() {{}},
      querySelector: () => null,
      querySelectorAll: () => [],
    }};
  }}
  return elements[id];
}}

let mockMatchesResp = {{ ok: true, matches: [], counts: {{ total: 0, ready: 0, unprocessed: 0, with_notes: 0 }}, available_chars: {{ self: [], opp: [] }}, byoc_connected: false }};

const sandbox = {{
  console,
  window: {{
    innerWidth: 1200,
    __FC_BOOTSTRAP__: {{ mode: "hosted_web", initialRoute: "" }},
    location: loc,
    history: {{ pushState() {{}}, replaceState() {{}}, back() {{}} }},
    addEventListener() {{}},
    scrollTo: () => {{}},
    matchMedia: () => ({{ matches: false }}),
  }},
  history: {{ pushState() {{}}, replaceState() {{}}, back() {{}} }},
  document: {{
    title: "",
    hidden: false,
    documentElement: {{ getAttribute: () => "dark", setAttribute: () => {{}}, classList: {{ toggle() {{}} }} }},
    body: {{ classList: {{ toggle() {{}} }} }},
    getElementById: (id) => makeEl(id),
    querySelector: (sel) => sel === "#viewMatches > .card" ? makeEl("matchesFilterCard") : null,
    querySelectorAll: () => [],
    addEventListener: () => {{}},
  }},
  HTMLVideoElement: {{ prototype: {{}} }},
  localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
  Date, Intl, Set, Map, Math, Number, String, Array, Object, Boolean, JSON, Promise, URLSearchParams, encodeURIComponent, decodeURIComponent,
  setInterval: () => 1, clearInterval: () => {{}}, setTimeout: () => 0, clearTimeout: () => {{}},
  requestAnimationFrame: (fn) => fn(),
  syncThemeUi: () => {{}},
  initMobileTopbarAutoHide: () => {{}},
  initDesktopTooltips: () => {{}},
  setTopbarHidden: () => {{}},
  updateStatusPillUi: () => {{}},
  checkForNewTakeoverResponses: () => {{}},
  _elements: elements,
  _scrolledIds: scrolledIds,
}};
sandbox.window.document = sandbox.document;
vm.createContext(sandbox);

vm.runInContext(fs.readFileSync({json.dumps(str(STATE_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(STATUS_POP_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(HOME_JS))}, "utf8"), sandbox);
vm.runInContext(fs.readFileSync({json.dumps(str(MATCHES_JS))}, "utf8"), sandbox);
const appSrc = fs.readFileSync({json.dumps(str(APP_JS))}, "utf8").replace(/initApp\\(\\);\\s*$/, "");
vm.runInContext(appSrc, sandbox);
vm.runInContext(`
  toast = () => {{}};
  api = async (url) => {{
    if (String(url).startsWith("/api/matches")) return _mockMatchesResp;
    return {{ ok: true }};
  }};
`, sandbox);
sandbox._mockMatchesResp = mockMatchesResp;

(async () => {{
  const out = await vm.runInContext(`(async () => {{
    // 1. Hosted mode keeps #navMatches visible
    state.activeView = "home";
    syncTopNav();
    const navMatchesVisible = !_elements["navMatches"].classList.contains("hidden");

    // 2. Hosted signed-in without BYOC -> renders guided funnel with "How this stays free"
    state.auth = {{ authenticated: true, user: {{ discord_username: "AkumaMain" }}, quota: null, has_byoc: false }};
    _mockMatchesResp = {{ ok: true, matches: [], counts: {{ total: 0, ready: 0, unprocessed: 0, with_notes: 0 }}, available_chars: {{ self: [], opp: [] }}, byoc_connected: false }};
    await loadMatches();
    const funnelHtml = _elements["matchList"].innerHTML;

    // 3. Hosted signed-in WITH BYOC -> unlocks matches + Open in Studio & hides username bar
    _mockMatchesResp = {{
      ok: true,
      byoc_connected: true,
      byoc_used_bytes: 1200000000,
      byoc_cap_gb: 9.0,
      counts: {{ total: 1, ready: 1, unprocessed: 0, with_notes: 0 }},
      available_chars: {{ self: ["Akuma"], opp: ["Chun-Li"] }},
      matches: [{{
        quark_id: "1795443936010-2584",
        status: "ready",
        self_player_name: "AkumaMain",
        opp_player_name: "ChunOpp",
        self_char_name: "Akuma",
        opp_char_name: "Chun-Li",
        self_chars: ["Akuma"],
        opp_chars: ["Chun-Li"],
        mobile_round_count: 3,
      }}],
    }};
    await loadMatches();
    const unlockedMatchesHtml = _elements["matchList"].innerHTML;
    const usernameRowHidden = _elements["matchesUsernameRow"].classList.contains("hidden");

    // 4. Hosted Home WITH latest_match renders full Latest Match Hero + preview_video_url + Open in Studio
    state.homeData = {{
      authenticated: true,
      has_byoc: true,
      latest_match: {{
        quark_id: "1795443936010-2584",
        status: "ready",
        self_player_name: "AkumaMain",
        opp_player_name: "ChunOpp",
        self_char_name: "Akuma",
        opp_char_name: "Chun-Li",
        round_count: 3,
        preview_video_url: "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com/my-3s-replays/replays/1795443936010-2584/r01.mp4?X-Amz-Signature=abc",
      }},
      recent_matches: [],
      focus_goals: {{ active: [], graduated_count: 0, max_active: 3 }},
      playlists: [],
      pulse: {{ notes_total: 1, matchups_count: 1 }},
    }};
    renderHomeWorkbench();
    const hostedLatestHeroHtml = _elements["homeContent"].innerHTML;

    // 5. Local Companion status popover + #byocModal (off & connected)
    state.capabilities.mode = "local_companion";
    state.capabilities.has_cloud_token = true;
    state.capabilities.byoc = {{ connected: false, cap_gb: 9.0, used_bytes: 0, synced_count: 0 }};
    const popOffHtml = buildStatusPopoverHtml();
    state.byocModal = {{ status: state.capabilities.byoc, capGb: "9.0", autoSync: true, showReadToken: false, busy: false, error: "" }};
    renderByocModal();
    const modalOffHtml = _elements["byocModalBody"].innerHTML;

    state.capabilities.byoc = {{ connected: true, bucket_name: "my-3s-replays", account_id_masked: "0123••••cdef", cap_gb: 9.0, used_bytes: 1200000000, synced_count: 4, auto_sync: true }};
    const popOnHtml = buildStatusPopoverHtml();
    state.byocModal = {{ status: state.capabilities.byoc, capGb: "9.0", autoSync: true, busy: false, error: "" }};
    renderByocModal();
    const modalOnHtml = _elements["byocModalBody"].innerHTML;

    // 6. openAboutExplainer() navigates to about & scrolls #aboutDecentralized
    openAboutExplainer();

    return {{
      navMatchesVisible,
      funnelHtml,
      unlockedMatchesHtml,
      usernameRowHidden,
      hostedLatestHeroHtml,
      popOffHtml,
      popOnHtml,
      modalOffHtml,
      modalOnHtml,
      activeViewAfterExplainer: state.activeView,
      scrolledIds: _scrolledIds,
    }};
  }})()`, sandbox);
  console.log(JSON.stringify(out));
}})();
"""
    res = _run_node(script)

    assert res["navMatchesVisible"] is True
    assert "Watch your full matches on mobile" in res["funnelHtml"]
    assert "How this stays free →" in res["funnelHtml"]
    assert "openAboutExplainer()" in res["funnelHtml"]
    assert "—" not in res["funnelHtml"] and "&mdash;" not in res["funnelHtml"].lower()

    assert res["usernameRowHidden"] is True
    assert "Open in Studio →" in res["unlockedMatchesHtml"]
    assert "ChunOpp" in res["unlockedMatchesHtml"]

    assert "▶ Open in Studio" in res["hostedLatestHeroHtml"]
    assert "r2.cloudflarestorage.com" in res["hostedLatestHeroHtml"]

    assert "Mobile Replay Sync" in res["popOffHtml"]
    assert "Set up R2 →" in res["popOffHtml"]
    assert "● my-3s-replays (1.2 / 9.0 GB)" in res["popOnHtml"]
    assert "Manage" in res["popOnHtml"]

    assert "Test &amp; Connect" in res["modalOffHtml"]
    assert "Optional: Use a separate Read-Only token for fightcadelab.com" in res["modalOffHtml"]
    assert "Why bring your own R2? How Fightcade Lab stays free →" in res["modalOffHtml"]
    assert "—" not in res["modalOffHtml"] and "&mdash;" not in res["modalOffHtml"].lower()

    assert "1.20 / 9.0 GB" in res["modalOnHtml"]
    assert "Sync Recent Sets" in res["modalOnHtml"]
    assert "Disconnect" in res["modalOnHtml"]
    assert "—" not in res["modalOnHtml"] and "&mdash;" not in res["modalOnHtml"].lower()

    assert res["activeViewAfterExplainer"] == "about"
    assert "aboutDecentralized" in res["scrolledIds"]
