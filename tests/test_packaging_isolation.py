"""Companion packaging isolation, frozen-path resolution, icon wiring & FFmpeg auto-install tests."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Any
import zipfile

import pytest
import requests

from fightcade_analysis.asset_installer import (
    FFMPEG_DEFAULT_RELEASE_URL,
    FFMPEG_DEFAULT_SHA256_URL,
    install_ffmpeg_from_zip,
    install_ffmpeg_latest,
)
from fightcade_analysis.env_config import (
    inspect_environment,
    is_frozen_runtime,
    require_local_ffmpeg,
    resolve_app_root,
    resolve_default_data_dir,
    resolve_local_ffmpeg,
)
from fightcade_analysis.server import create_server

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "src" / "fightcade_analysis" / "web"


def test_subprocess_import_isolation_blocks_ml_and_imageio_ffmpeg() -> None:
    """Block faiss/ruptures/sklearn/matplotlib/imageio_ffmpeg/PIL and verify core Companion modules import cleanly."""
    script = r"""
import importlib
import sys

BLOCKED = frozenset({"faiss", "ruptures", "sklearn", "matplotlib", "imageio_ffmpeg", "PIL"})

class BlockExcludedImports:
    def find_spec(self, fullname, path, target=None):
        top = fullname.split(".", 1)[0]
        if top in BLOCKED:
            raise ModuleNotFoundError(f"Blocked excluded packaged module: {fullname}")
        return None

sys.meta_path.insert(0, BlockExcludedImports())

for mod in (
    "fightcade_analysis.characters",
    "fightcade_analysis.env_config",
    "fightcade_analysis.mobile_ui",
    "fightcade_analysis.clip_renderer",
    "fightcade_analysis.round_renderer",
    "fightcade_analysis.select_queue",
    "fightcade_analysis.match_catalog",
    "fightcade_analysis.notes_hub",
    "fightcade_analysis.perspective",
    "fightcade_analysis.share_export",
    "fightcade_analysis.takeover_responses",
    "fightcade_analysis.asset_installer",
    "fightcade_analysis.server",
    "fightcade_analysis.tray_app",
):
    importlib.import_module(mod)

print("OK")
"""
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"Isolated import failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    assert proc.stdout.strip().endswith("OK")


def test_frozen_path_resolution_and_web_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app_dir = tmp_path / "FightcadeLab"
    fake_app_dir.mkdir(parents=True)
    fake_exe = fake_app_dir / "FightcadeLabCompanion.exe"
    fake_exe.write_bytes(b"MZ")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(fake_exe))

    assert is_frozen_runtime() is True
    assert resolve_app_root() == fake_app_dir.resolve()
    assert resolve_default_data_dir() == (fake_app_dir / "data").resolve()


def test_companion_lua_staging_and_icon_wiring(tmp_path: Path) -> None:
    sys.path.insert(0, str(REPO_ROOT / "packaging"))
    try:
        import build_companion  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    assert "sfiii3nr1_overlay.lua" in build_companion.COMPANION_LUA_FILES
    stage_dir = tmp_path / "dist_stage"
    build_companion.stage_companion_assets(stage_dir)
    assert (stage_dir / "lua" / "sfiii3nr1_overlay.lua").is_file()
    assert (stage_dir / "web" / "favicon.ico").is_file()

    cmd = build_companion.build_nuitka(tmp_path / "nuitka_out", dry_run=True)
    assert any(arg.startswith("--windows-icon-from-ico=") and arg.endswith("favicon.ico") for arg in cmd)

    iss_text = (REPO_ROOT / "packaging" / "fightcade_lab.iss").read_text(encoding="utf-8")
    assert "#ifndef AppVersion" in iss_text
    assert r"SetupIconFile=..\src\fightcade_analysis\web\favicon.ico" in iss_text
    assert r"UninstallDisplayIcon={app}\{#AppExeName}" in iss_text

    wf_text = (REPO_ROOT / ".github" / "workflows" / "release-companion.yml").read_text(encoding="utf-8")
    assert "—" not in wf_text
    assert "/DAppVersion=" in wf_text
    assert "More info -> Run anyway" in wf_text


def test_install_ffmpeg_auto_sha256_and_onboarding_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Build a synthetic upstream FFmpeg Essentials zip with ffmpeg.exe + LICENSE
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zf:
        zf.writestr("ffmpeg-7.1-essentials_build/bin/ffmpeg.exe", b"MZ_SYNTH_FFMPEG_BIN")
        zf.writestr("ffmpeg-7.1-essentials_build/LICENSE", b"GNU GENERAL PUBLIC LICENSE Version 3\n")
    zip_bytes = zbuf.getvalue()
    zip_sha = hashlib.sha256(zip_bytes).hexdigest()

    class FakeResp:
        def __init__(self, status: int, content: bytes = b"") -> None:
            self.status_code = status
            self._content = content
            self.headers = {"Content-Length": str(len(content))}
            self.text = content.decode("utf-8", errors="ignore")

        def iter_content(self, chunk_size: int = 65536):
            yield self._content

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    seen_urls: list[str] = []

    class FakeSession:
        def get(self, url: str, **kwargs: Any) -> FakeResp:
            seen_urls.append(url)
            if url == FFMPEG_DEFAULT_SHA256_URL:
                return FakeResp(200, f"{zip_sha}  ffmpeg-release-essentials.zip\n".encode("utf-8"))
            if url == FFMPEG_DEFAULT_RELEASE_URL:
                return FakeResp(200, zip_bytes)
            return FakeResp(404)

    # Consent strictly enforced
    with pytest.raises(PermissionError):
        install_ffmpeg_latest(tmp_path, consent=False, session=FakeSession())  # type: ignore[arg-type]

    # Auto-fetch .sha256 + verify + extract ffmpeg.exe & LICENSE + remove zip
    res = install_ffmpeg_from_zip(tmp_path, consent=True, session=FakeSession())  # type: ignore[arg-type]
    assert res["ok"] is True
    assert res["license_installed"] is True
    assert FFMPEG_DEFAULT_SHA256_URL in seen_urls
    assert FFMPEG_DEFAULT_RELEASE_URL in seen_urls
    extracted = tmp_path / "tools" / "ffmpeg.exe"
    assert extracted.is_file()
    assert extracted.read_bytes() == b"MZ_SYNTH_FFMPEG_BIN"
    assert (tmp_path / "tools" / "ffmpeg_LICENSE.txt").is_file()
    assert not (tmp_path / "tools" / "ffmpeg_download.zip").exists()

    # resolve_local_ffmpeg & require_local_ffmpeg pick up data/tools/ffmpeg.exe first
    assert resolve_local_ffmpeg(tmp_path) == extracted.resolve()
    assert require_local_ffmpeg(tmp_path) == str(extracted.resolve())

    # inspect_environment exposes ffmpeg_found and fast_fbneo_found
    env_info = inspect_environment(data_dir=tmp_path)
    assert env_info["ffmpeg_found"] is True
    assert "fast_fbneo_found" in env_info

    # Test POST /api/onboarding action=install_ffmpeg consent gate + success
    ep_data = tmp_path / "server_data"
    ep_data.mkdir()

    def fake_install_latest(data_dir: Path, *, consent: bool = False, **_kw: Any) -> dict[str, Any]:
        if not consent:
            raise PermissionError("Explicit user consent is required")
        tdir = data_dir / "tools"
        tdir.mkdir(parents=True, exist_ok=True)
        exe = tdir / "ffmpeg.exe"
        exe.write_bytes(b"MZ_ENDPOINT_FFMPEG")
        return {"ok": True, "path": str(exe), "license_installed": True}

    monkeypatch.setattr("fightcade_analysis.server.install_ffmpeg_latest", fake_install_latest)

    srv = create_server(host="127.0.0.1", port=0, data_dir=ep_data, auto_refresh_index=False, pro_corpus_enabled=False)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # Rejects missing consent
        r_no = requests.post(
            f"{base}/api/onboarding",
            json={"action": "install_ffmpeg", "consent": False},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_no.status_code == 400

        # Succeeds with explicit consent
        r_ok = requests.post(
            f"{base}/api/onboarding",
            json={"action": "install_ffmpeg", "consent": True},
            headers={"Origin": base},
            timeout=5,
        )
        assert r_ok.status_code == 200
        body = r_ok.json()
        assert body["ok"] is True
        assert body["onboarding"]["checks"]["ffmpeg_found"] is True

        # Capabilities surfaces ffmpeg_found & fast_fbneo_found inside environment
        caps = requests.get(f"{base}/api/capabilities", timeout=5).json()
        assert caps["environment"]["ffmpeg_found"] is True
        assert "fast_fbneo_found" in caps["environment"]
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=3)


def test_onboarding_wizard_and_popover_render_one_click_tool_installers() -> None:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not found in PATH")

    driver = f"""
const fs = require("fs");
const vm = require("vm");
const stateSrc = fs.readFileSync({json.dumps(str(WEB_DIR / "js" / "state.js"))}, "utf8");
const homeSrc = fs.readFileSync({json.dumps(str(WEB_DIR / "js" / "home.js"))}, "utf8");
const popSrc = fs.readFileSync({json.dumps(str(WEB_DIR / "js" / "status_popover.js"))}, "utf8");

const homeEl = {{ innerHTML: "" }};
const sandbox = {{
  console,
  URLSearchParams,
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
  window: {{
    __FC_BOOTSTRAP__: {{ mode: "local_companion" }},
    innerWidth: 1280,
    innerHeight: 800,
    location: {{ origin: "http://127.0.0.1:8765", pathname: "/", search: "", hash: "" }},
    addEventListener: () => {{}},
  }},
  localStorage: {{ getItem: () => null, setItem: () => {{}}, removeItem: () => {{}} }},
  document: {{
    documentElement: {{ clientWidth: 1280, clientHeight: 800, getAttribute: () => null }},
    getElementById: id => (id === "homeContent" ? homeEl : null),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {{}},
  }},
}};
vm.createContext(sandbox);
vm.runInContext(stateSrc, sandbox);
vm.runInContext(homeSrc, sandbox);
vm.runInContext(popSrc, sandbox);
const out = vm.runInContext(`(function() {{
  state.capabilities = {{
    mode: "local_companion",
    can_render: true,
    has_grouflon: false,
    environment: {{
      fbneo_found: true,
      rom_found: true,
      fast_fbneo_found: false,
      ffmpeg_found: false,
      has_grouflon: false,
    }},
    disk_cache: {{ total_gb: 0, cap_gb: 5, match_count: 0, protected_count: 0 }},
  }};
  state.homeData = {{
    latest_match: null,
    recent_matches: [],
    focus_goals: {{ active: [], graduated_count: 0, max_active: 3 }},
    playlists: [],
    pulse: {{ notes_total: 0, matchups_count: 0 }},
  }};
  renderHomeWorkbench();
  const homeMissing = document.getElementById("homeContent").innerHTML;
  const popMissing = buildStatusPopoverHtml();

  state.capabilities.environment.fast_fbneo_found = true;
  state.capabilities.environment.ffmpeg_found = true;
  renderHomeWorkbench();
  const homeReady = document.getElementById("homeContent").innerHTML;
  const popReady = buildStatusPopoverHtml();

  return {{ homeMissing, popMissing, homeReady, popReady }};
}})()`, sandbox);

process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run([node_bin, "-e", driver], capture_output=True, text=True, encoding="utf-8", check=False, timeout=15)
    assert proc.returncode == 0, f"Node test failed:\n{proc.stderr}"
    res = json.loads(proc.stdout)

    assert "Download Fast Capture Engine" in res["homeMissing"]
    assert "Download FFmpeg" in res["homeMissing"]
    assert "FFmpeg" in res["homeMissing"]
    assert "Grouflon (optional)" in res["homeMissing"]
    assert "Download Fast Capture Engine" in res["popMissing"]
    assert "Download FFmpeg" in res["popMissing"]
    assert "FFmpeg not installed" in res["popMissing"]

    assert "Download Fast Capture Engine" not in res["homeReady"]
    assert "Download FFmpeg" not in res["homeReady"]
    assert "Download Fast Capture Engine" not in res["popReady"]
    assert "Download FFmpeg" not in res["popReady"]
