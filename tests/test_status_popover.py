"""Tests for the reworked Companion Status Popover, top-bar pill copy, and light-mode rank badge CSS."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "fightcade_analysis" / "web"
STATE_JS = WEB_DIR / "js" / "state.js"
POPOVER_JS = WEB_DIR / "js" / "status_popover.js"
STYLES_CSS = WEB_DIR / "styles.css"


def _run_popover_eval(js_body: str) -> dict:
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not found in PATH")
    driver = r"""
const fs = require("fs");
const vm = require("vm");

const stateSrc = fs.readFileSync(process.env.STATE_JS_PATH, "utf8");
const popSrc = fs.readFileSync(process.env.POPOVER_JS_PATH, "utf8");

function makeMockEl(id) {
  const classes = new Set();
  return {
    id,
    textContent: "",
    innerHTML: "",
    style: {},
    classList: {
      add: (...c) => c.forEach(x => classes.add(x)),
      remove: (...c) => c.forEach(x => classes.delete(x)),
      toggle: (c, force) => {
        if (force === undefined) {
          if (classes.has(c)) classes.delete(c); else classes.add(c);
        } else if (force) classes.add(c); else classes.delete(c);
      },
      contains: c => classes.has(c),
      toArray: () => [...classes],
    },
    getBoundingClientRect: () => ({ top: 0, bottom: 54, left: 0, right: 360, width: 120, height: 32 }),
    contains: () => false,
  };
}

const elems = {
  btnStatusPill: makeMockEl("btnStatusPill"),
  statusPillDot: makeMockEl("statusPillDot"),
  statusPillText: makeMockEl("statusPillText"),
  statusDot: makeMockEl("statusDot"),
  statusPopover: makeMockEl("statusPopover"),
  appTopbar: makeMockEl("appTopbar"),
};

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
  window: {
    __FC_BOOTSTRAP__: {},
    innerWidth: 1280,
    innerHeight: 800,
    location: { origin: "http://127.0.0.1:8765", pathname: "/", search: "", hash: "" },
    addEventListener: () => {},
  },
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  document: {
    documentElement: {
      clientWidth: 1280,
      clientHeight: 800,
      getAttribute: () => null,
      setAttribute: () => {},
      removeAttribute: () => {},
    },
    getElementById: id => elems[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
  },
  __elems: elems,
};
vm.createContext(sandbox);
vm.runInContext(stateSrc, sandbox, { filename: "state.js" });
vm.runInContext(popSrc, sandbox, { filename: "status_popover.js" });
const fn = vm.runInContext("(function() {\n" + process.env.TEST_JS + "\n})", sandbox);
const out = fn();
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(
        [node_bin, "-e", driver],
        env={
            "STATE_JS_PATH": str(STATE_JS),
            "POPOVER_JS_PATH": str(POPOVER_JS),
            "TEST_JS": js_body,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert proc.returncode == 0, f"Node harness failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    return json.loads(proc.stdout)


def test_local_healthy_idle_popover_and_disclosures():
    res = _run_popover_eval(
        r"""
        state.capabilities = {
          mode: "local_companion",
          can_render: true,
          has_grouflon: true,
          has_cloud_token: false,
          environment: { fbneo_found: true, fast_fbneo_found: true, rom_found: true },
          disk_cache: { total_gb: 0.84, cap_gb: 5, match_count: 3, protected_count: 2 },
        };
        state.watcher = { running: true, phase: "idle", message: "Waiting..." };
        state.jobs = [];
        const idleNotConnected = buildStatusPopoverHtml();

        state.capabilities.has_cloud_token = true;
        const idleConnected = buildStatusPopoverHtml();

        // Toggle storage cap editor open
        togglePopoverStorageEditor({ stopPropagation: () => {} });
        const withStorageOpen = buildStatusPopoverHtml();

        // Toggle diagnostics open
        togglePopoverDiagnostics({ stopPropagation: () => {} });
        const withDiagOpen = buildStatusPopoverHtml();

        return { idleNotConnected, idleConnected, withStorageOpen, withDiagOpen };
        """
    )

    idle = res["idleNotConnected"]
    assert "Desktop Companion" in idle
    assert "Watching Fightcade for finished matches." in idle
    assert "Cloud Sharing" in idle
    assert "Not connected" in idle
    assert "Connect →" in idle
    assert "Replay Storage" in idle
    assert "0.84 / 5 GB" in idle
    assert "Adjust ▾" in idle
    assert "▸ Setup &amp; Diagnostics" in idle
    # No developer noise or empty sections when idle & healthy
    assert "Phase:" not in idle
    assert "Background Jobs" not in idle
    assert "No recent background jobs" not in idle
    assert "popScanUser" not in idle
    assert "P1 Coin" not in idle
    assert "popDiskCapGb" not in idle
    assert "Fast Engine ✓" not in idle

    assert "● Connected" in res["idleConnected"]
    assert "Reconnect" in res["idleConnected"]

    assert 'id="popDiskCapGb"' in res["withStorageOpen"]
    assert "Save Cap" in res["withStorageOpen"]
    assert "3 sets cached · 2 protected by notes/playlists" in res["withStorageOpen"]

    assert "▾ Setup &amp; Diagnostics" in res["withDiagOpen"]
    assert "Fast Engine ✓" in res["withDiagOpen"]
    assert "Detected ✓" in res["withDiagOpen"]
    assert "Ready ✓" in res["withDiagOpen"]
    assert "Scan Latest Match" in res["withDiagOpen"]
    assert "popScanUser" not in res["withDiagOpen"]


def test_local_setup_missing_warning_and_activity_labels():
    res = _run_popover_eval(
        r"""
        state.capabilities = {
          mode: "local_companion",
          can_render: true,
          has_grouflon: false,
          has_cloud_token: false,
          environment: { fbneo_found: false, fast_fbneo_found: false, rom_found: false },
          disk_cache: { total_gb: 0, cap_gb: 0, match_count: 0, protected_count: 0 },
        };
        state.watcher = { running: false, phase: "idle", message: "" };
        state.jobs = [];
        const setupHtml = buildStatusPopoverHtml();

        // Active & errored jobs with human match label resolution (no raw quark suffix!)
        state.capabilities.environment = { fbneo_found: true, fast_fbneo_found: false, rom_found: true };
        state.matches = [
          {
            quark_id: "1795443936010-5771",
            self_player_name: "player1",
            self_char_name: "Akuma",
            opp_player_name: "rival",
            opp_char_name: "Ken",
          },
        ];
        state.jobs = [
          {
            quark_id: "1795443936010-5771",
            state: "running",
            progress_done: 2,
            progress_total: 4,
            message: "Rendering Round 2 of 4...",
          },
          {
            quark_id: "1795443936999-1969",
            state: "error",
            error: "Stream unavailable",
          },
          {
            quark_id: "1795443936000-0001",
            state: "done",
            message: "Done",
          },
        ];
        const jobsHtml = buildStatusPopoverHtml();
        return { setupHtml, jobsHtml };
        """
    )

    assert "Needs Setup" in res["setupHtml"]
    assert "Fightcade FBNeo not found" in res["setupHtml"]
    assert "3rd Strike ROM not detected" in res["setupHtml"]
    assert "▾ Setup &amp; Diagnostics" in res["setupHtml"]
    assert "Check fcadefbneo.ini" in res["setupHtml"]
    assert "0.00 GB · Unlimited" in res["setupHtml"]

    jobs = res["jobsHtml"]
    assert "Activity" in jobs
    assert "player1 (Akuma) vs rival (Ken)" in jobs
    assert "Replay processing" in jobs
    assert "Stream unavailable" in jobs
    assert "Failed" in jobs
    assert "5771" not in jobs
    assert "1969" not in jobs
    assert "0001" not in jobs


def test_hosted_mode_popover_and_pill_labels():
    res = _run_popover_eval(
        r"""
        // Hosted offline
        state.capabilities = { mode: "hosted_web", companion_paired: false, can_takeover: false };
        state.jobs = [];
        updateStatusPillUi();
        const hostedOfflinePill = __elems.statusPillText.textContent;
        const hostedOfflineHtml = buildStatusPopoverHtml();

        // Hosted connected
        state.capabilities = { mode: "hosted_web", companion_paired: true, can_takeover: true };
        updateStatusPillUi();
        const hostedConnectedPill = __elems.statusPillText.textContent;
        const hostedConnectedHtml = buildStatusPopoverHtml();

        // Local pill states
        state.capabilities = {
          mode: "local_companion",
          environment: { fbneo_found: true, rom_found: true },
        };
        const pillFor = (watcher, jobs, envOverride) => {
          state.watcher = watcher;
          state.jobs = jobs || [];
          state.capabilities.environment = envOverride || { fbneo_found: true, rom_found: true };
          updateStatusPillUi();
          return __elems.statusPillText.textContent;
        };

        return {
          hostedOfflinePill,
          hostedOfflineHtml,
          hostedConnectedPill,
          hostedConnectedHtml,
          pErr: pillFor({ phase: "error" }),
          pSetup: pillFor({ phase: "idle" }, [], { fbneo_found: false, rom_found: true }),
          pMatch: pillFor({ phase: "in_match", running: true }),
          pScan: pillFor({ phase: "scanning", running: true }),
          pRender: pillFor({ phase: "idle", running: true }, [{ state: "running", progress_done: 2, progress_total: 4 }]),
          pWork: pillFor({ phase: "idle", running: true }, [{ state: "queued", progress_done: 0, progress_total: 0 }]),
          pWatch: pillFor({ phase: "idle", running: true }),
          pReady: pillFor({ phase: "idle", running: false }),
        };
        """
    )

    assert res["hostedOfflinePill"] == "Companion Offline"
    assert "○ Offline" in res["hostedOfflineHtml"]
    assert "Open Setup Guide →" in res["hostedOfflineHtml"]
    assert "Replay Storage" not in res["hostedOfflineHtml"]
    assert "Setup &amp; Diagnostics" not in res["hostedOfflineHtml"]

    assert res["hostedConnectedPill"] == "Companion Ready"
    assert "● Connected" in res["hostedConnectedHtml"]
    assert "Paired with your local Windows Companion" in res["hostedConnectedHtml"]
    assert "Replay Storage" not in res["hostedConnectedHtml"]

    assert res["pErr"] == "Attention"
    assert res["pSetup"] == "Needs Setup"
    assert res["pMatch"] == "In Match"
    assert res["pScan"] == "Scanning..."
    assert res["pRender"] == "Rendering 2/4"
    assert res["pWork"] == "Working..."
    assert res["pWatch"] == "Watching"
    assert res["pReady"] == "Companion Ready"


def test_light_mode_rank_badge_css_hollow_ring_polish():
    css = STYLES_CSS.read_text(encoding="utf-8")
    # Dark mode keeps #141925 fill
    assert "background: #141925; color: #f8fafc;" in css
    # Light mode switches to white / tinted light surface with #0f172a text and deepened rings
    light_block_start = css.index('html[data-theme="light"] .rank {')
    light_block = css[light_block_start : light_block_start + 700]
    assert "background: #ffffff;" in light_block
    assert "color-mix(in srgb, var(--rk-ring) 10%, #ffffff)" in light_block
    assert "color: #0f172a;" in light_block
    assert "box-shadow: none;" in light_block
    assert "#141925" not in light_block
    for rule in (
        'html[data-theme="light"] .rank-s { --rk-ring: #dc2626; }',
        'html[data-theme="light"] .rank-a { --rk-ring: #9333ea; }',
        'html[data-theme="light"] .rank-b { --rk-ring: #0284c7; }',
        'html[data-theme="light"] .rank-c { --rk-ring: #ca8a04; }',
        'html[data-theme="light"] .rank-d { --rk-ring: #64748b; }',
        'html[data-theme="light"] .rank-e { --rk-ring: #a16227; }',
        'html[data-theme="light"] .rank-u { --rk-ring: #94a3b8; color: #475569; }',
    ):
        assert rule in light_block
