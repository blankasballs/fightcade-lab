/* Companion Status Pill & Live Watcher/Jobs Popover */

let _statusPopoverOpen = false;

function toggleStatusPopover() {
  _statusPopoverOpen = !_statusPopoverOpen;
  const pop = document.getElementById("statusPopover");
  if (!pop) return;
  pop.classList.toggle("hidden", !_statusPopoverOpen);
  if (_statusPopoverOpen) {
    renderStatusPopover();
    pollJobs();
  }
}

function closeStatusPopover() {
  _statusPopoverOpen = false;
  const pop = document.getElementById("statusPopover");
  if (pop) pop.classList.add("hidden");
}

function updateStatusPillUi() {
  const w = state.watcher || {};
  const activeJobs = (state.jobs || []).filter(j => ["queued", "running", "paused"].includes(j.state));
  const pill = document.getElementById("btnStatusPill");
  const pillDot = document.getElementById("statusPillDot");
  const pillTxt = document.getElementById("statusPillText");
  const brandDot = document.getElementById("statusDot");
  if (!pill || !pillDot || !pillTxt) return;

  const phase = String(w.phase || "idle");
  const isErr = phase === "error";
  const isBusy = activeJobs.length > 0 || ["in_match", "waiting_replay", "scanning"].includes(phase);

  pill.classList.toggle("busy", isBusy && !isErr);
  pill.classList.toggle("err", isErr);
  pillDot.classList.toggle("busy", isBusy && !isErr);
  pillDot.classList.toggle("err", isErr);
  if (brandDot) {
    brandDot.classList.toggle("busy", isBusy && !isErr);
    brandDot.classList.toggle("err", isErr);
  }

  if (isErr) {
    pillTxt.textContent = "Companion Error";
  } else if (phase === "in_match") {
    pillTxt.textContent = "Live Match Active";
  } else if (phase === "scanning" || phase === "waiting_replay") {
    pillTxt.textContent = "Scanning Match...";
  } else if (activeJobs.length > 0) {
    const j0 = activeJobs[0];
    pillTxt.textContent = j0.progress_total > 0
      ? `Working (${j0.progress_done}/${j0.progress_total})`
      : (j0.progress_done > 0 ? `Working (R${j0.progress_done})` : `Working (${activeJobs.length})`);
  } else if (w.running) {
    pillTxt.textContent = state.activeView === "studio" ? "Watching Live" : "Watching Fightcade";
  } else {
    pillTxt.textContent = "Companion Ready";
  }
}

function renderStatusPopover() {
  const pop = document.getElementById("statusPopover");
  if (!pop || !_statusPopoverOpen) return;
  const w = state.watcher || {};
  const caps = state.capabilities || {};
  const env = caps.environment || {};
  const cache = caps.disk_cache || { total_gb: 0, cap_gb: 5.0, match_count: 0, protected_count: 0 };
  const jobs = (state.jobs || []).slice(0, 6);
  const canRender = Boolean(caps.can_render);

  const jobsHtml = jobs.length
    ? jobs.map(j => {
        const pct = j.progress_total > 0
          ? Math.min(100, Math.round((j.progress_done / j.progress_total) * 100))
          : (j.state === "done" ? 100 : (j.progress_done > 0 ? Math.min(90, 15 + j.progress_done * 10) : 18));
        const pillCls = j.state === "done" ? "ready" : (j.state === "error" ? "err" : "busy");
        const shortId = String(j.quark_id || "").split("-").pop();
        return `
          <div style="margin-bottom:8px;">
            <div class="row-between" style="font-size:12px;">
              <span class="mono" style="font-weight:700;">${esc(shortId)}</span>
              <span class="pill ${pillCls}">${esc(j.state)}</span>
            </div>
            <div style="font-size:11.5px; color:var(--muted); margin-top:2px;">${esc(j.message || j.phase || "")}</div>
            ${["queued", "running", "paused"].includes(j.state)
              ? `<div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div>`
              : ""}
          </div>
        `;
      }).join("")
    : `<div style="font-size:12px; color:var(--muted);">No recent background jobs.</div>`;

  const envHtml = caps.mode === "local_companion"
    ? `
      <div class="pop-section">
        <div class="pop-title">Local Environment</div>
        <div class="env-row"><span>Fightcade FBNeo</span><span class="${env.fbneo_found ? "env-ok" : "env-warn"}">${env.fbneo_found ? (env.fast_fbneo_found ? "✓ Fast Engine" : "✓ Stock FBNeo") : "Not found"}</span></div>
        <div class="env-row"><span>3rd Strike ROM</span><span class="${env.rom_found ? "env-ok" : "env-warn"}">${env.rom_found ? "✓ Detected" : "Check fcadefbneo.ini"}</span></div>
        <div class="env-row"><span>Grouflon Training</span><span class="${caps.has_grouflon ? "env-ok" : "env-warn"}">${caps.has_grouflon ? "✓ Ready" : "Optional (Not installed)"}</span></div>
      </div>
    `
    : "";

  const cacheCapTxt = Number(cache.cap_gb || 0) > 0 ? `${cache.cap_gb} GB` : "Unlimited";
  const cacheHtml = caps.mode === "local_companion"
    ? `
      <div class="pop-section">
        <div class="pop-title">Local Replay Cache Cap</div>
        <div style="font-size:11.5px; color:var(--muted); margin-bottom:6px;">
          Using <strong class="mono">${Number(cache.total_gb || 0).toFixed(2)} GB</strong> / <span class="mono">${esc(cacheCapTxt)}</span>
          (${cache.match_count || 0} sets · ${cache.protected_count || 0} protected by notes/playlists)
        </div>
        <div class="row" style="gap:6px;">
          <input id="popDiskCapGb" type="number" step="0.5" min="0" max="500" class="input mono" style="width:92px; padding:5px 8px; font-size:12px;" value="${esc(cache.cap_gb ?? 5)}" title="Rolling full-match MP4 cap in GB (0 = unlimited)" />
          <span style="font-size:12px; color:var(--muted);">GB</span>
          <button id="btnSaveDiskCap" class="sm" onclick="saveDiskCacheCapFromPopover()">Save Cap</button>
        </div>
      </div>
    `
    : "";

  const scanHtml = canRender
    ? `
      <div class="pop-section">
        <div class="pop-title">Scan Latest Match (Select Marks + Cache)</div>
        <div class="row" style="gap:6px;">
          <input id="popScanUser" class="input" style="flex:1; padding:6px 9px; font-size:12.5px;" value="${esc(state.username || DEFAULT_USER)}" placeholder="Fightcade handle" />
          <button id="btnPopScan" class="primary sm" onclick="triggerScanLatestFromPopover()">Scan Latest</button>
        </div>
      </div>
    `
    : "";

  pop.innerHTML = `
    <div class="row-between" style="margin-bottom:6px;">
      <strong style="font-size:13.5px;">Desktop Companion</strong>
      <span class="pill ${(w.running || w.enabled) ? "ready" : "unproc"}">${(w.running || w.enabled) ? "Watcher Active" : "Idle"}</span>
    </div>
    <div style="font-size:12px; color:var(--muted);">
      <div><strong>Phase:</strong> ${esc(w.phase || "idle")}</div>
      <div style="margin-top:2px;">${esc(w.message || "Waiting for Fightcade match to finish...")}</div>
    </div>

    ${envHtml}

    <div class="pop-section">
      <div class="pop-title">Background Jobs</div>
      ${jobsHtml}
    </div>

    ${cacheHtml}
    ${scanHtml}

    <div class="pop-section" style="font-size:11.5px; color:var(--muted-dim);">
      Tip: Bind <span class="kbd">P1 Coin</span> (<span class="kbd">Select</span>) in FBNeo to bookmark live &amp; 1-frame retry in Takeover (or use <span class="kbd">Alt+1</span> on keyboard).
    </div>
  `;
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
  const inp = document.getElementById("popScanUser");
  const btn = document.getElementById("btnPopScan");
  const uname = (inp ? inp.value : state.username || DEFAULT_USER).trim();
  if (!uname) return toast("Enter a Fightcade username");
  if (btn) { btn.disabled = true; btn.textContent = "Scanning..."; }
  try {
    await api("/api/queue/scan", {
      method: "POST",
      body: JSON.stringify({ username: uname }),
    });
    toast(`Started scanning latest match for ${uname}`);
    pollJobs();
  } catch (err) {
    toast("Scan failed: " + err.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Scan Latest"; }
  }
}

document.addEventListener("click", e => {
  if (!_statusPopoverOpen) return;
  const wrap = document.querySelector(".status-wrap");
  if (wrap && !wrap.contains(e.target)) {
    closeStatusPopover();
  }
});
