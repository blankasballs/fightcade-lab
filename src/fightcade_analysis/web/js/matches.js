/* Full Match History Catalog (#matches) */

function setFilter(f) {
  state.filter = f;
  document.querySelectorAll("#filterChips .chip").forEach(c => c.classList.toggle("active", c.dataset.filter === f));
  loadMatches();
}

function toggleAddPanel() {
  document.getElementById("addPanel").classList.toggle("hidden");
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

async function processManualQuark() {
  const raw = document.getElementById("manualQuarkInput").value.trim();
  const persp = Number(document.getElementById("manualPersp").value || 1);
  if (!raw) return toast("Paste a Quark ID or Fightcade URL");
  try {
    const res = await api("/api/matches/process", {
      method: "POST",
      body: JSON.stringify({ quark_id: raw, perspective: persp }),
    });
    document.getElementById("manualQuarkInput").value = "";
    toast(`Queued ${res.job.quark_id}`);
    await loadMatches();
    pollJobs();
  } catch (err) {
    toast(err.message);
  }
}

function statusBadgeHtml(m) {
  if (m.status === "ready") return `<span class="pill ready">● Ready (${m.mobile_round_count}R)</span>`;
  if (m.status === "processing" || m.status === "queued") {
    const msg = m.job ? m.job.message : "Queued";
    return `<span class="pill busy">◌ ${esc(msg)}</span>`;
  }
  if (m.status === "cached_no_video") return `<span class="pill cached">Cached · Fast Render (~15s)</span>`;
  if (m.status === "telemetry_only") return `<span class="pill cached">Telemetry Only</span>`;
  return `<span class="pill unproc">Not Processed</span>`;
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
    const sChars = (m.self_chars && m.self_chars.length) ? m.self_chars : (m.self_char_name ? [m.self_char_name] : []);
    const oChars = (m.opp_chars && m.opp_chars.length) ? m.opp_chars : (m.opp_char_name ? [m.opp_char_name] : []);
    let chars = "";
    if (sChars.length || oChars.length) {
      const sHtml = sChars.map(ch =>
        `<span class="pill char-self ${state.selfCharFilter === ch ? "active-f" : ""}" onclick="toggleCharTagFilter('self', '${esc(ch)}')">${esc(ch)}</span>`
      ).join("");
      const oHtml = oChars.map(ch =>
        `<span class="pill char-opp ${state.oppCharFilter === ch ? "active-f" : ""}" onclick="toggleCharTagFilter('opp', '${esc(ch)}')">${esc(ch)}</span>`
      ).join("");
      chars = `${sHtml}<span class="char-vs">vs</span>${oHtml}`;
    }
    const score = (m.self_score != null && m.opp_score != null && (m.self_score || m.opp_score))
      ? `<span class="pill">${m.self_score}–${m.opp_score}</span>` : "";
    const rk = m.opp_rank_letter ? `<span class="rank">${esc(m.opp_rank_letter)}</span>` : "";
    const dur = m.duration_s > 0 ? `${Math.round(m.duration_s / 60)}m` : "";
    const notePill = m.notes_count > 0 ? `<span class="pill note">📝 ${m.notes_count}</span>` : "";
    const selCount = Number(m.select_marks_count || 0);
    const selPill = selCount > 0
      ? (m.status === "ready" && m.first_select_mark
          ? `<button class="pill drill" style="cursor:pointer;" onclick="openMatchAtFirstMark('${esc(m.quark_id)}', ${m.perspective || 1}, ${m.first_select_mark.round_id || 1}, ${m.first_select_mark.anchor_gf || 0})">⚡ ${selCount} mark${selCount === 1 ? "" : "s"}</button>`
          : `<span class="pill drill">⚡ ${selCount} mark${selCount === 1 ? "" : "s"}</span>`)
      : "";

    let aiPill = "";
    if (m.has_report && m.report_url) {
      aiPill = `<button class="pill ready" style="cursor:pointer;" onclick="openReport('${esc(m.report_url)}', 'matches')">✨ AI Report</button>`;
    } else if (m.has_telemetry && isProCorpusEnabled() && state.capabilities.has_gemini) {
      aiPill = `<button class="pill cached" style="cursor:pointer;" onclick="triggerMatchBreakdown('${esc(m.quark_id)}', ${m.perspective || 1})">✨ Run Breakdown</button>`;
    }
    const errPill = (m.job && m.job.state === "error") ? `<span class="pill err">Error: ${esc(m.job.error)}</span>` : "";

    let progHtml = "";
    if (m.job && (m.job.state === "running" || m.job.state === "queued" || m.job.state === "paused")) {
      const pct = m.job.progress_total > 0
        ? Math.round((m.job.progress_done / m.job.progress_total) * 100)
        : (m.job.progress_done > 0 ? Math.min(90, 15 + m.job.progress_done * 10) : 15);
      progHtml = `<div class="prog-bar"><div class="prog-fill" style="width:${pct}%"></div></div>`;
    }

    const primaryBtn = m.status === "ready"
      ? `<button class="emerald sm" onclick="openMatchFromMatches('${esc(m.quark_id)}')">Review →</button>`
      : (m.status === "processing" || m.status === "queued")
        ? `<button class="sm" disabled>Working...</button>`
        : `<button class="primary sm" onclick="processMatch('${esc(m.quark_id)}', ${m.perspective})" ${!state.capabilities.can_render ? "disabled title='Requires local companion'" : ""}>Process</button>`;

    const rawOpp = String(m.opp_player_name || "").trim();
    const isPlaceholderOpp = !rawOpp || rawOpp === "P1" || rawOpp === "P2";
    const oppDisplay = isPlaceholderOpp
      ? (m.opp_char_name ? `${m.opp_char_name} (${rawOpp || "Opp"})` : (rawOpp || "Opponent"))
      : rawOpp;

    return `
      <div class="card match-card">
        <div class="row-between">
          <div class="match-title">
            <span>vs ${esc(oppDisplay)}</span>
            ${rk}
            ${score}
          </div>
          ${primaryBtn}
        </div>
        <div class="row">
          ${statusBadgeHtml(m)}
          ${chars}
          ${selPill}
          ${notePill}
          ${aiPill}
          ${errPill}
        </div>
        <div class="match-meta">
          <span>${esc(m.date_str || m.quark_id)}</span>
          ${dur ? `<span>· ${dur}</span>` : ""}
          ${m.ranked ? `<span>· Ranked</span>` : ""}
          <span>· P${m.perspective}</span>
        </div>
        ${m.set_note_preview ? `<div style="font-size:12.5px; color:var(--text-soft); border-left:2px solid var(--violet); padding-left:8px;">${esc(m.set_note_preview)}</div>` : ""}
        ${progHtml}
      </div>
    `;
  }).join("");
}
