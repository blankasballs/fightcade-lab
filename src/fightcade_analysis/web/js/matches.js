/* Full Match History Catalog (#matches) */

function setFilter(f) {
  state.filter = f;
  document.querySelectorAll("#filterChips .chip").forEach(c => c.classList.toggle("active", c.dataset.filter === f));
  loadMatches();
}

function onCharFilterChange() {
  state.selfCharFilter = document.getElementById("selfCharFilter").value || "";
  state.oppCharFilter = document.getElementById("oppCharFilter").value || "";
  loadMatches();
}

function clearCharFilters() {
  state.selfCharFilter = "";
  state.oppCharFilter = "";
  loadMatches();
}

function toggleCharTagFilter(side, charName) {
  if (side === "self") state.selfCharFilter = (state.selfCharFilter === charName) ? "" : charName;
  else state.oppCharFilter = (state.oppCharFilter === charName) ? "" : charName;
  loadMatches();
}

function syncUserControls() {
  const btnMe = document.getElementById("btnResetUser");
  if (btnMe) {
    const isOther = state.username && DEFAULT_USER && state.username.toLowerCase() !== DEFAULT_USER.toLowerCase();
    btnMe.classList.toggle("hidden", !isOther);
    btnMe.textContent = isOther ? `← ${DEFAULT_USER}` : "Me";
  }
}

function resetToDefaultUser() {
  const userEl = document.getElementById("usernameInput");
  if (userEl) userEl.value = DEFAULT_USER;
  state.selfCharFilter = "";
  state.oppCharFilter = "";
  loadMatches();
}

function renderCharFilterControls() {
  const row = document.getElementById("charFilterRow");
  const selfList = (state.availableChars && state.availableChars.self) || [];
  const oppList = (state.availableChars && state.availableChars.opp) || [];
  if (!selfList.length && !oppList.length) {
    row.classList.add("hidden");
    return;
  }
  row.classList.remove("hidden");
  const selfSel = document.getElementById("selfCharFilter");
  const oppSel = document.getElementById("oppCharFilter");
  const isOther = state.username && DEFAULT_USER && state.username.toLowerCase() !== DEFAULT_USER.toLowerCase();
  const selfPrefix = isOther ? state.username : "You";
  selfSel.innerHTML = `<option value="">${esc(selfPrefix)}: Any</option>` + selfList.map(c =>
    `<option value="${esc(c)}" ${c === state.selfCharFilter ? "selected" : ""}>${esc(selfPrefix)}: ${esc(c)}</option>`
  ).join("");
  oppSel.innerHTML = `<option value="">Opp: Any</option>` + oppList.map(c =>
    `<option value="${esc(c)}" ${c === state.oppCharFilter ? "selected" : ""}>vs ${esc(c)}</option>`
  ).join("");
  document.getElementById("btnClearCharFilter").classList.toggle("hidden", !(state.selfCharFilter || state.oppCharFilter));
}

async function loadMatches() {
  const userEl = document.getElementById("usernameInput");
  const prevUser = state.username || "";
  const u = ((userEl && userEl.value) || state.username || DEFAULT_USER).trim();
  const userChanged = u.toLowerCase() !== prevUser.toLowerCase();
  if (userChanged) {
    state.selfCharFilter = "";
    state.oppCharFilter = "";
    state.matches = [];
    state.counts = {};
    state.availableChars = { self: [], opp: [] };
    state.lastRefreshed = null;
    document.getElementById("matchList").innerHTML = `<div class="card empty-card">Loading matches for <strong>${esc(u)}</strong>...</div>`;
  }
  state.username = u;
  syncUserControls();
  try {
    const params = new URLSearchParams({ username: u, filter: state.filter });
    if (state.selfCharFilter) params.set("self_char", state.selfCharFilter);
    if (state.oppCharFilter) params.set("opp_char", state.oppCharFilter);
    const data = await api(`/api/matches?${params.toString()}`);
    localStorage.setItem("fc_mobile_user", u);
    state.matches = data.matches || [];
    state.counts = data.counts || {};
    state.availableChars = data.available_chars || { self: [], opp: [] };
    state.lastRefreshed = data.last_refreshed;
    renderCharFilterControls();
    renderMatchList();
  } catch (err) {
    state.matches = [];
    state.counts = {};
    renderMatchList();
    toast("Failed to load matches: " + err.message);
  }
}

async function refreshFromFightcade(loadOlder = false) {
  const userEl = document.getElementById("usernameInput");
  const u = ((userEl && userEl.value) || state.username || DEFAULT_USER).trim();
  if (!u) return;
  const userChanged = u.toLowerCase() !== (state.username || "").toLowerCase();
  if (userChanged) {
    state.selfCharFilter = "";
    state.oppCharFilter = "";
    state.matches = [];
    state.counts = {};
    state.availableChars = { self: [], opp: [] };
    state.lastRefreshed = null;
    state.username = u;
    syncUserControls();
    document.getElementById("matchList").innerHTML = `<div class="card empty-card">Syncing Fightcade history for <strong>${esc(u)}</strong>...</div>`;
  }
  const btn = document.getElementById("btnRefresh");
  if (btn) { btn.disabled = true; btn.textContent = loadOlder ? "Loading..." : "Syncing..."; }
  try {
    const res = await api("/api/matches/refresh", {
      method: "POST",
      body: JSON.stringify({ username: u, load_older: loadOlder }),
    });
    toast(`Synced ${res.fetched} matches (+${res.added} new)`);
    await loadMatches();
  } catch (err) {
    if (userChanged) renderMatchList();
    toast("Sync failed: " + err.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "Refresh"; }
  }
}

async function processMatch(qid, perspective) {
  try {
    await api("/api/matches/process", {
      method: "POST",
      body: JSON.stringify({ quark_id: qid, perspective }),
    });
    toast("Queued for processing");
    if (state.activeView === "matches") await loadMatches();
    if (state.activeView === "home") await loadHomeWorkbench();
    pollJobs();
  } catch (err) {
    toast("Error: " + err.message);
  }
}

function statusBadgeHtml(m) {
  if (!m || m.status === "ready") return "";
  if (m.status === "processing" || m.status === "queued") {
    const msg = m.job ? m.job.message : "Queued";
    return `<span class="pill busy">◌ ${esc(msg)}</span>`;
  }
  if (m.status === "cached_no_video") return `<span class="pill cached">Cached (~15s)</span>`;
  if (m.status === "telemetry_only") return `<span class="pill cached">Telemetry Only</span>`;
  if (m.status === "error" && !(m.job && m.job.state === "error")) {
    return `<span class="pill err">Error</span>`;
  }
  return "";
}

function openMatchFromMatches(qid) {
  state.returnView = "matches";
  openMatch(qid);
}

function renderMatchList() {
  const c = state.counts;
  document.getElementById("cntAll").textContent = c.total != null ? `(${c.total})` : "";
  document.getElementById("cntReady").textContent = c.ready != null ? `(${c.ready})` : "";
  document.getElementById("cntUnproc").textContent = c.unprocessed != null ? `(${c.unprocessed})` : "";
  document.getElementById("cntNotes").textContent = c.with_notes != null ? `(${c.with_notes})` : "";
  document.getElementById("lastRefreshedText").textContent = state.lastRefreshed
    ? `History synced: ${state.lastRefreshed}`
    : "Tap Refresh to pull Fightcade history";

  const host = document.getElementById("matchList");
  if (!state.matches.length) {
    host.innerHTML = `<div class="card empty-card">No matches in this filter. Tap <strong>Refresh</strong> to sync from Fightcade.</div>`;
    return;
  }

  host.innerHTML = state.matches.map(m => {
    const titleHtml = formatMatchLabelHtml(m, {
      defaultSelfName: state.username,
      charFilterFn: "toggleCharTagFilter",
      activeSelfChar: state.selfCharFilter,
      activeOppChar: state.oppCharFilter,
      stackedMobile: true,
    });
    const score = (m.self_score != null && m.opp_score != null && (m.self_score || m.opp_score))
      ? `<span class="pill match-score-pill">${m.self_score}–${m.opp_score}</span>` : "";
    const dur = m.duration_s > 0 ? `${Math.round(m.duration_s / 60)}m` : "";
    const statusBadge = statusBadgeHtml(m);
    const notePill = m.notes_count > 0 ? `<span class="pill note">📝 ${m.notes_count}</span>` : "";

    let aiPill = "";
    if (m.has_report && m.report_url) {
      aiPill = `<button class="pill ready" style="cursor:pointer;" onclick="openReport(${escJsAttr(m.report_url)}, 'matches')">✨ AI Report</button>`;
    } else if (m.has_telemetry && isProCorpusEnabled() && state.capabilities.has_gemini) {
      aiPill = `<button class="pill cached" style="cursor:pointer;" onclick="triggerMatchBreakdown(${escJsAttr(m.quark_id)}, ${Number(m.perspective || 1)})">✨ Run Breakdown</button>`;
    }
    const errPill = (m.job && m.job.state === "error") ? `<span class="pill err">Error: ${esc(m.job.error)}</span>` : "";
    const badges = [statusBadge, notePill, aiPill, errPill].filter(Boolean);
    const badgesHtml = badges.length ? `<div class="row match-badges">${badges.join("")}</div>` : "";

    let progHtml = "";
    if (m.job && (m.job.state === "running" || m.job.state === "queued" || m.job.state === "paused")) {
      const pct = m.job.progress_total > 0
        ? Math.round((m.job.progress_done / m.job.progress_total) * 100)
        : (m.job.progress_done > 0 ? Math.min(90, 15 + m.job.progress_done * 10) : 15);
      progHtml = `<div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div>`;
    }

    const playableDuringStream = (m.status === "processing" || m.status === "queued") && Number(m.mobile_round_count || 0) > 0;
    const primaryBtn = m.status === "ready"
      ? `<button class="emerald sm" data-tip="Open this set in Replay Studio" onclick="openMatchFromMatches(${escJsAttr(m.quark_id)})">Open in Studio →</button>`
      : playableDuringStream
        ? `<button class="emerald sm" data-tip="Open completed rounds in Replay Studio while the rest stream in background" onclick="openMatchFromMatches(${escJsAttr(m.quark_id)})">Open in Studio (1–${Number(m.mobile_round_count || 0)}R)</button>`
        : (m.status === "processing" || m.status === "queued")
          ? `<button class="sm" disabled>Working...</button>`
          : `<button class="primary sm" data-tip="${state.capabilities.can_render ? "Ingest telemetry and render 60fps review video locally" : "Requires local companion"}" onclick="processMatch(${escJsAttr(m.quark_id)}, ${Number(m.perspective || 1)})" ${!state.capabilities.can_render ? "disabled" : ""}>Process</button>`;

    const metaParts = [
      esc(formatTimestampWithTz(m.date, m.date_str || m.quark_id)),
      Number(m.mobile_round_count || 0) > 0 ? `${Number(m.mobile_round_count)}R` : "",
      dur,
      m.ranked ? "Ranked" : "",
    ].filter(Boolean);

    return `
      <div class="card match-card">
        <div class="match-card-grid">
          <div class="match-title">
            ${titleHtml}
            ${score}
          </div>
          ${badgesHtml}
          <div class="match-meta">
            ${metaParts.map((part, i) => `<span>${i > 0 ? "· " : ""}${part}</span>`).join("")}
          </div>
          <div class="match-card-cta">${primaryBtn}</div>
        </div>
        ${m.set_note_preview ? `<div style="font-size:12.5px; color:var(--text-soft); border-left:2px solid var(--violet); padding-left:8px;">${esc(m.set_note_preview)}</div>` : ""}
        ${progHtml}
      </div>
    `;
  }).join("");
}
