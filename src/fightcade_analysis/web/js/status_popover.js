/* Companion Status Pill & Clean Player-Focused Control Panel Popover */

let _statusPopoverOpen = false;
let _popStorageOpen = false;
let _popDiagnosticsOpen = null; // null = auto (collapsed when healthy, expanded on setup issue)

function isStatusPopoverOpen() {
  return _statusPopoverOpen;
}

function _isLocalSetupMissing(caps) {
  if (!caps || caps.mode !== "local_companion") return false;
  const env = caps.environment;
  if (!env || typeof env !== "object") return false;
  if (!("fbneo_found" in env) && !("rom_found" in env)) return false;
  return !env.fbneo_found || !env.rom_found;
}

function _formatPopoverJobTitle(job) {
  if (!job || typeof job !== "object") return "Replay processing";
  const qid = String(job.quark_id || "").trim();
  if (qid) {
    const candidates = [
      ...(Array.isArray(state.matches) ? state.matches : []),
      ...(state.homeData && state.homeData.latest_match ? [state.homeData.latest_match] : []),
      ...((state.homeData && Array.isArray(state.homeData.recent_matches)) ? state.homeData.recent_matches : []),
    ];
    const match = candidates.find(m => m && String(m.quark_id || "") === qid);
    if (match && typeof formatMatchLabelText === "function") {
      return formatMatchLabelText(match, { defaultSelfName: state.username, showRank: false });
    }
  }
  if (job.opp_name) {
    return `vs ${String(job.opp_name).trim()}`;
  }
  const phase = String(job.phase || job.kind || "").toLowerCase();
  if (phase.includes("scan")) return "Match scan";
  return "Replay processing";
}

function togglePopoverStorageEditor(ev) {
  if (ev && typeof ev.stopPropagation === "function") ev.stopPropagation();
  _popStorageOpen = !_popStorageOpen;
  renderStatusPopover();
}

function togglePopoverDiagnostics(ev) {
  if (ev && typeof ev.stopPropagation === "function") ev.stopPropagation();
  const caps = state.capabilities || {};
  const current = _popDiagnosticsOpen !== null ? _popDiagnosticsOpen : _isLocalSetupMissing(caps);
  _popDiagnosticsOpen = !current;
  renderStatusPopover();
}

function positionStatusPopover() {
  const pop = document.getElementById("statusPopover");
  const btn = document.getElementById("btnStatusPill");
  if (!pop || !btn || !_statusPopoverOpen) return;
  const vw = window.innerWidth || document.documentElement.clientWidth || 360;
  const vh = window.innerHeight || document.documentElement.clientHeight || 640;
  const rect = btn.getBoundingClientRect();
  let anchorBottom = rect.bottom;
  if (vw < 900 || rect.height === 0) {
    const bar = document.getElementById("appTopbar");
    anchorBottom = bar ? bar.getBoundingClientRect().bottom : 54;
  }
  const topPx = Math.max(12, Math.min(Math.round(anchorBottom + 8), Math.max(12, vh - 200)));

  pop.style.top = `${topPx}px`;
  pop.style.maxHeight = `${Math.max(180, vh - topPx - 16)}px`;

  if (vw < 900) {
    pop.style.left = "12px";
    pop.style.right = "12px";
    pop.style.width = "auto";
  } else {
    const popW = Math.min(360, vw - 24);
    const rightEdge = Math.min(vw - 12, rect.right);
    const leftPx = Math.max(12, Math.round(rightEdge - popW));
    pop.style.left = `${leftPx}px`;
    pop.style.right = "auto";
    pop.style.width = `${popW}px`;
  }
}

function toggleStatusPopover() {
  _statusPopoverOpen = !_statusPopoverOpen;
  const pop = document.getElementById("statusPopover");
  if (!pop) return;
  if (_statusPopoverOpen) {
    if (typeof closeMobileNavMenu === "function") closeMobileNavMenu();
    if (typeof setTopbarHidden === "function") setTopbarHidden(false);
  }
  pop.classList.toggle("hidden", !_statusPopoverOpen);
  if (_statusPopoverOpen) {
    renderStatusPopover();
    positionStatusPopover();
    if (typeof pollJobs === "function") pollJobs();
  }
}

function closeStatusPopover() {
  _statusPopoverOpen = false;
  const pop = document.getElementById("statusPopover");
  if (pop) pop.classList.add("hidden");
}

function updateStatusPillUi() {
  const w = state.watcher || {};
  const caps = state.capabilities || {};
  const activeJobs = (state.jobs || []).filter(j => ["queued", "running", "paused"].includes(j.state));
  const pill = document.getElementById("btnStatusPill");
  const pillDot = document.getElementById("statusPillDot");
  const pillTxt = document.getElementById("statusPillText");
  const brandDot = document.getElementById("statusDot");
  if (!pill || !pillDot || !pillTxt) return;

  const isLocal = caps.mode === "local_companion";
  const phase = String(w.phase || "idle");
  const isErr = isLocal && phase === "error";
  const setupMissing = _isLocalSetupMissing(caps);
  const isBusy = isLocal && (
    setupMissing || activeJobs.length > 0 || ["in_match", "waiting_replay", "scanning"].includes(phase)
  );

  pill.classList.toggle("busy", isBusy && !isErr);
  pill.classList.toggle("err", isErr);
  pillDot.classList.toggle("busy", isBusy && !isErr);
  pillDot.classList.toggle("err", isErr);
  if (brandDot) {
    brandDot.classList.toggle("busy", isBusy && !isErr);
    brandDot.classList.toggle("err", isErr);
  }

  if (!isLocal) {
    const pairedOnline = Boolean(caps.companion_paired && caps.can_takeover);
    pillTxt.textContent = pairedOnline ? "Companion Ready" : "Companion Offline";
    return;
  }

  if (isErr) {
    pillTxt.textContent = "Attention";
  } else if (setupMissing) {
    pillTxt.textContent = "Needs Setup";
  } else if (phase === "in_match") {
    pillTxt.textContent = "In Match";
  } else if (phase === "scanning" || phase === "waiting_replay") {
    pillTxt.textContent = "Scanning...";
  } else if (activeJobs.length > 0) {
    const j0 = activeJobs[0];
    pillTxt.textContent = j0.progress_total > 0
      ? `Rendering ${j0.progress_done}/${j0.progress_total}`
      : "Working...";
  } else if (w.running) {
    pillTxt.textContent = "Watching";
  } else {
    pillTxt.textContent = "Companion Ready";
  }
}

function _renderPopoverActivitySection(jobsList) {
  const activityJobs = (jobsList || [])
    .filter(j => j && ["queued", "running", "paused", "error"].includes(j.state))
    .slice(0, 6);
  if (!activityJobs.length) return "";

  const stateLabels = {
    running: "Working",
    queued: "Queued",
    paused: "Paused",
    error: "Failed",
  };

  const rowsHtml = activityJobs.map(j => {
    const pct = j.progress_total > 0
      ? Math.min(100, Math.round((j.progress_done / j.progress_total) * 100))
      : (j.progress_done > 0 ? Math.min(90, 15 + j.progress_done * 10) : 18);
    const pillCls = j.state === "error" ? "err" : "busy";
    const title = _formatPopoverJobTitle(j);
    const subMsg = j.state === "error"
      ? (j.error || j.message || "Processing failed.")
      : (j.message || (j.progress_total > 0 ? `Round ${j.progress_done} of ${j.progress_total}` : "Processing replay..."));
    const isActive = ["queued", "running", "paused"].includes(j.state);
    return `
      <div class="pop-job-item">
        <div class="row-between" style="font-size:12px; gap:8px;">
          <strong style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${esc(title)}</strong>
          <span class="pill ${pillCls}">${esc(stateLabels[j.state] || j.state)}</span>
        </div>
        <div style="font-size:11.5px; color:var(--muted); margin-top:2px;">${esc(subMsg)}</div>
        ${isActive ? `<div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div>` : ""}
      </div>
    `;
  }).join("");

  return `
    <div class="pop-section">
      <div class="pop-title">Activity</div>
      ${rowsHtml}
    </div>
  `;
}

function buildStatusPopoverHtml() {
  const w = state.watcher || {};
  const caps = state.capabilities || {};
  const isLocal = caps.mode === "local_companion";
  const activityHtml = _renderPopoverActivitySection(state.jobs || []);

  // HOSTED WEB MODE
  if (!isLocal) {
    const pairedOnline = Boolean(caps.companion_paired && caps.can_takeover);
    const badgeHtml = pairedOnline
      ? `<span class="pill ready">● Connected</span>`
      : `<span class="pill unproc">○ Offline</span>`;
    const summaryTxt = pairedOnline
      ? "Paired with your local Windows Companion — Replay Takeover is ready."
      : "Install and pair the Windows Companion to launch 1-click Replay Takeover from shared clips.";
    const actionsHtml = !pairedOnline
      ? `
        <div class="pop-section">
          <div class="row" style="gap:8px;">
            <button type="button" class="sm primary" onclick="event.stopPropagation(); closeStatusPopover(); openCompanionModal('Replay Takeover', null, { signInRequired: false })">Open Setup Guide →</button>
            <a class="btn-link sm" href="${esc(safeHref(`${GITHUB_REPO_URL}/releases`))}" target="_blank" rel="noopener">Download Companion</a>
          </div>
        </div>
      `
      : "";

    return `
      <div class="row-between" style="margin-bottom:6px;">
        <strong style="font-size:13.5px;">Desktop Companion</strong>
        ${badgeHtml}
      </div>
      <div class="pop-summary">${esc(summaryTxt)}</div>
      ${activityHtml}
      ${actionsHtml}
    `;
  }

  // LOCAL COMPANION MODE
  const env = caps.environment || {};
  const cache = caps.disk_cache || { total_gb: 0, cap_gb: 5.0, match_count: 0, protected_count: 0 };
  const activeJobs = (state.jobs || []).filter(j => ["queued", "running", "paused"].includes(j.state));
  const phase = String(w.phase || "idle");
  const setupMissing = _isLocalSetupMissing(caps);
  const canRender = Boolean(caps.can_render);

  let statusPillLabel = "Ready";
  let statusPillCls = "ready";
  let summaryText = "Ready for replay review and Takeover.";

  if (phase === "error") {
    statusPillLabel = "Error";
    statusPillCls = "err";
    summaryText = w.message || "Desktop Companion hit an error. Check Setup & Diagnostics below.";
  } else if (setupMissing) {
    statusPillLabel = "Needs Setup";
    statusPillCls = "busy";
    summaryText = "Finish setup below to enable replay processing and Takeover.";
  } else if (phase === "in_match") {
    statusPillLabel = "In Match";
    statusPillCls = "busy";
    summaryText = "Fightcade match in progress — ready to capture when the set ends.";
  } else if (activeJobs.length > 0 || phase === "scanning" || phase === "waiting_replay") {
    statusPillLabel = "Working";
    statusPillCls = "busy";
    const j0 = activeJobs[0];
    summaryText = (j0 && (j0.message || j0.phase)) || w.message || "Processing replay in the background.";
  } else if (w.running || w.enabled) {
    statusPillLabel = "Watching";
    statusPillCls = "ready";
    summaryText = "Watching Fightcade for finished matches.";
  }

  let warningHtml = "";
  if (setupMissing) {
    const missingItems = [];
    if (!env.fbneo_found) missingItems.push("Fightcade FBNeo not found");
    if (!env.rom_found) missingItems.push("3rd Strike ROM not detected");
    warningHtml = `
      <div class="pop-warning" role="alert">
        <strong>Setup needed:</strong> ${esc(missingItems.join(" · "))}.
      </div>
    `;
  }

  // Cloud Sharing key-value row
  const cloudConnected = Boolean(caps.has_cloud_token);
  const cloudRightHtml = cloudConnected
    ? `<span class="row" style="gap:8px; flex-wrap:nowrap;">
         <span class="env-ok" style="font-size:12px;">● Connected</span>
         <button type="button" class="link-btn" onclick="event.stopPropagation(); pairLocalCompanionWithBrowser()">Reconnect</button>
       </span>`
    : `<span class="row" style="gap:8px; flex-wrap:nowrap;">
         <span style="font-size:12px; color:var(--muted);">Not connected</span>
         <button type="button" class="sm primary" style="padding:3px 9px; font-size:11.5px;" onclick="event.stopPropagation(); pairLocalCompanionWithBrowser()">Connect →</button>
       </span>`;

  // Replay Storage key-value row + collapsible cap editor
  const totalGb = Number(cache.total_gb || 0).toFixed(2);
  const capNum = Number(cache.cap_gb || 0);
  const storageSummary = capNum > 0 ? `${totalGb} / ${capNum} GB` : `${totalGb} GB · Unlimited`;
  const storageEditorHtml = _popStorageOpen
    ? `
      <div class="pop-storage-edit" onclick="event.stopPropagation()">
        <div class="row" style="gap:6px;">
          <input id="popDiskCapGb" type="number" step="0.5" min="0" max="500" class="input mono" style="width:88px; padding:5px 8px; font-size:12px;" value="${esc(cache.cap_gb ?? 5)}" title="Rolling full-match MP4 cap in GB (0 = unlimited)" />
          <span style="font-size:12px; color:var(--muted);">GB</span>
          <button id="btnSaveDiskCap" type="button" class="sm" onclick="event.stopPropagation(); saveDiskCacheCapFromPopover()">Save Cap</button>
        </div>
        <div style="font-size:11px; color:var(--muted-dim); margin-top:5px;">
          ${Number(cache.match_count || 0)} sets cached · ${Number(cache.protected_count || 0)} protected by notes/playlists
        </div>
      </div>
    `
    : "";

  // Setup & Diagnostics disclosure (collapsed by default when healthy; auto-expanded on issue)
  const diagOpen = _popDiagnosticsOpen !== null ? _popDiagnosticsOpen : setupMissing;
  const fbneoText = env.fbneo_found ? (env.fast_fbneo_found ? "Fast Engine ✓" : "Stock FBNeo ✓") : "Not found";
  const romText = env.rom_found ? "Detected ✓" : "Check fcadefbneo.ini";
  const grouflonText = caps.has_grouflon ? "Ready ✓" : "Optional (not installed)";
  const diagBodyHtml = diagOpen
    ? `
      <div class="pop-diag-body" onclick="event.stopPropagation()">
        <div class="env-row"><span>Fightcade FBNeo</span><span class="${env.fbneo_found ? "env-ok" : "env-warn"}">${esc(fbneoText)}</span></div>
        <div class="env-row"><span>3rd Strike ROM</span><span class="${env.rom_found ? "env-ok" : "env-warn"}">${esc(romText)}</span></div>
        <div class="env-row"><span>Grouflon Hitboxes</span><span class="${caps.has_grouflon ? "env-ok" : "env-warn"}">${esc(grouflonText)}</span></div>
        ${canRender ? `<button id="btnPopScan" type="button" class="sm" style="margin-top:8px; width:100%;" onclick="event.stopPropagation(); triggerScanLatestFromPopover()">Scan Latest Match</button>` : ""}
      </div>
    `
    : "";

  return `
    <div class="row-between" style="margin-bottom:4px;">
      <strong style="font-size:13.5px;">Desktop Companion</strong>
      <span class="pill ${statusPillCls}">${esc(statusPillLabel)}</span>
    </div>
    <div class="pop-summary">${esc(summaryText)}</div>
    ${warningHtml}
    ${activityHtml}
    <div class="pop-section">
      <div class="pop-kv-row">
        <span class="pop-kv-label">Cloud Sharing</span>
        ${cloudRightHtml}
      </div>
      <div class="pop-kv-row">
        <span class="pop-kv-label">Replay Storage</span>
        <span class="row" style="gap:6px; flex-wrap:nowrap;">
          <span class="mono" style="font-size:12px; color:var(--text-soft);">${esc(storageSummary)}</span>
          <button type="button" class="sm ghost pop-inline-toggle" aria-expanded="${_popStorageOpen ? "true" : "false"}" onclick="togglePopoverStorageEditor(event)">${_popStorageOpen ? "Done ▴" : "Adjust ▾"}</button>
        </span>
      </div>
      ${storageEditorHtml}
    </div>
    <div class="pop-section">
      <button type="button" class="pop-disclosure-btn" aria-expanded="${diagOpen ? "true" : "false"}" onclick="togglePopoverDiagnostics(event)">
        <span>${diagOpen ? "▾" : "▸"} Setup &amp; Diagnostics</span>
      </button>
      ${diagBodyHtml}
    </div>
  `;
}

function renderStatusPopover() {
  const pop = document.getElementById("statusPopover");
  if (!pop || !_statusPopoverOpen) return;
  pop.innerHTML = buildStatusPopoverHtml();
  positionStatusPopover();
}

async function saveDiskCacheCapFromPopover() {
  const inp = document.getElementById("popDiskCapGb");
  const btn = document.getElementById("btnSaveDiskCap");
  if (!inp) return;
  const val = Number(inp.value);
  if (Number.isNaN(val) || val < 0) return toast("Enter 0 (unlimited) or a positive GB cap");
  if (btn) { btn.disabled = true; btn.textContent = "Saving..."; }
  try {
    const res = await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ disk_cache_cap_gb: val, prune_now: true }),
    });
    if (res.disk_cache && state.capabilities) {
      state.capabilities.disk_cache = res.disk_cache;
    }
    const pCnt = (res.pruned || []).length;
    toast(`Cache cap saved (${val === 0 ? "Unlimited" : val + " GB"})${pCnt ? ` · pruned ${pCnt} old set(s)` : ""}`);
    renderStatusPopover();
  } catch (err) {
    toast("Failed to save cap: " + err.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Save Cap"; }
  }
}

async function triggerScanLatestFromPopover() {
  const btn = document.getElementById("btnPopScan");
  const uname = String(state.username || DEFAULT_USER || "").trim();
  if (!uname) return toast("Set your Fightcade handle in Matches first");
  if (btn) { btn.disabled = true; btn.textContent = "Scanning..."; }
  try {
    await api("/api/queue/scan", {
      method: "POST",
      body: JSON.stringify({ username: uname }),
    });
    toast(`Started scanning latest match for ${uname}`);
    if (typeof pollJobs === "function") pollJobs();
  } catch (err) {
    toast("Scan failed: " + err.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Scan Latest Match"; }
  }
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("click", e => {
    if (!_statusPopoverOpen) return;
    const wrap = document.querySelector(".status-wrap");
    const pop = document.getElementById("statusPopover");
    const inWrap = wrap && wrap.contains(e.target);
    const inPop = pop && pop.contains(e.target);
    if (!inWrap && !inPop) {
      closeStatusPopover();
    }
  });
}

if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  window.addEventListener("resize", () => {
    if (_statusPopoverOpen) positionStatusPopover();
  });
}
