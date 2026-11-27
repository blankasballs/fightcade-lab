/* Insights Tab (#insights): Featured Verified Habits, + Pin as Focus Goal, Analysis Triggers & Report Archive */

async function loadInsights() {
  const host = document.getElementById("insightsContent");
  try {
    const inc = state.includeLegacyInsights ? "1" : "0";
    const data = await api(`/api/insights?include_legacy=${inc}`);
    state.insightsData = data;
    if (!state.matches.length) {
      loadMatches().then(() => { if (state.activeView === "insights") renderInsights(); });
    }
    renderInsights();
  } catch (err) {
    if (host) host.innerHTML = `<div class="card empty-card">Failed to load Insights: ${esc(err.message)}</div>`;
  }
}

function setInsightsFilter(f) {
  state.insightsFilter = f;
  renderInsights();
}

function onInsightsSearchInput(val) {
  state.insightsSearch = (val || "").trim().toLowerCase();
  renderInsights();
}

function toggleLegacyInsights() {
  state.includeLegacyInsights = !state.includeLegacyInsights;
  loadInsights();
}

async function triggerMatchBreakdown(qid, perspective = 1) {
  if (!isProCorpusEnabled()) {
    return toast("Top-Rank comparison is on the roadmap.");
  }
  try {
    const res = await api("/api/insights/run", {
      method: "POST",
      body: JSON.stringify({ kind: "match_breakdown", quark_id: qid, perspective }),
    });
    toast(res.job.message || "Queued AI Match Breakdown!");
    pollJobs();
  } catch (err) {
    toast("Breakdown failed: " + err.message);
  }
}

async function triggerMatchBreakdownFromPicker() {
  const sel = document.getElementById("insightMatchPicker");
  if (!sel || !sel.value) return toast("Select a telemetry-ready match first");
  const [qid, persp] = sel.value.split("|");
  await triggerMatchBreakdown(qid, Number(persp || 1));
}

async function triggerVerifiedHabitsRun() {
  if (!isProCorpusEnabled()) {
    return toast("Top-Rank comparison is on the roadmap.");
  }
  try {
    const res = await api("/api/insights/run", {
      method: "POST",
      body: JSON.stringify({ kind: "verified_habits", username: state.username || DEFAULT_USER }),
    });
    toast(res.job.message || "Queued Verified Habits Checkup!");
    pollJobs();
  } catch (err) {
    toast("Checkup failed: " + err.message);
  }
}

async function pinHabitAsFocusGoal(habitIdx, btnEl = null) {
  const lv = state.insightsData && state.insightsData.latest_verified_habits;
  if (!lv || !lv.habits || !lv.habits[habitIdx]) return;
  if (getActiveFocuses().length >= 3) {
    toast("Max 3 Active Focuses — graduate or unpin one first.");
    return;
  }
  const h = lv.habits[habitIdx];
  const seedMoments = Array.isArray(h.seed_moments) ? h.seed_moments : [];
  if (btnEl) { btnEl.disabled = true; btnEl.textContent = "Pinning..."; }
  try {
    const res = await api("/api/focus-goals", {
      method: "POST",
      body: JSON.stringify({
        owner: state.username || DEFAULT_USER,
        title: `Fix: ${h.label}`,
        description: h.summary || `Recurring leak across ${h.sets_affected}/${h.total_sets} sets (-${h.total_hp_lost} HP total).`,
        tags: [h.label],
        trend_hint: "From AI Checkup",
        source_insight_id: h.source_insight_id || `${lv.id}:${habitIdx}`,
        items: seedMoments,
      }),
    });
    const cnt = Number((res.goal && res.goal.item_count) ?? seedMoments.length);
    toast(`Pinned Active Focus "${res.goal.title}" with ${cnt} clip${cnt === 1 ? "" : "s"} ready to Watch & Train!`);
    if (btnEl) btnEl.textContent = "✓ Pinned";
    await Promise.all([loadPlaylists(false), loadFocusGoals()]);
  } catch (err) {
    toast(err.message);
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = "+ Pin as Focus Goal"; }
  }
}

function renderInsights() {
  const host = document.getElementById("insightsContent");
  if (!host) return;
  const d = state.insightsData;
  if (!d) return;

  const caps = state.capabilities || {};
  const proEnabled = isProCorpusEnabled();
  const canRunAi = Boolean(proEnabled && caps.can_render);
  const aiTip = !proEnabled
    ? "On the roadmap"
    : (!caps.has_gemini ? "GEMINI_API_KEY not set — runs RAM-only without Vision summaries" : "Run ADR-001 Hybrid VLM + RAM analysis");

  const telemMatches = (state.matches || []).filter(m => m.has_telemetry).slice(0, 20);
  const pickerOpts = telemMatches.map(m =>
    `<option value="${esc(m.quark_id)}|${m.perspective || 1}">${esc(m.date_str || m.quark_id)} · ${esc(formatMatchLabelText(m, { defaultSelfName: state.username, showRank: false }))}</option>`
  ).join("");

  const activeInsightJobs = (state.jobs || []).filter(j => j.source === "insights" && ["queued", "running", "paused"].includes(j.state));
  const runningHtml = activeInsightJobs.length
    ? `<div class="card" style="border-color:var(--accent); background:rgba(56,189,248,0.08);">
        <div class="row-between">
          <strong>◌ ${esc(activeInsightJobs[0].message || "Running AI analysis...")}</strong>
          <span class="pill busy">${esc(activeInsightJobs[0].state)}</span>
        </div>
        <div class="prog-bar"><div class="prog-fill" style="width:${activeInsightJobs[0].progress_done || 25}%"></div></div>
      </div>`
    : "";

  // Featured Latest Verified Habits Summary Panel
  const lv = d.latest_verified_habits;
  let featuredHtml = "";
  if (lv && lv.habits && lv.habits.length) {
    const cards = lv.habits.slice(0, 5).map((h, idx) => `
      <div class="habit-card">
        <div class="row-between">
          <strong style="font-size:14.5px;">#${idx + 1} ${esc(h.label)}</strong>
          <span class="pill err">-${h.total_hp_lost} HP</span>
        </div>
        <div style="font-size:12.5px; color:var(--muted);">
          ${h.verified_clips} visually verified clip${h.verified_clips === 1 ? "" : "s"} across ${h.sets_affected}/${h.total_sets} sets · ${h.verified_pro_count} verified top-rank solution${h.verified_pro_count === 1 ? "" : "s"}
        </div>
        ${h.summary ? `<div style="font-size:13px; color:var(--text-body);">${esc(h.summary)}</div>` : ""}
        <div class="row-between" style="margin-top:auto; padding-top:6px;">
          <a href="${esc(safeHref(h.report_href))}" style="font-size:12px; font-weight:700;" onclick="event.preventDefault(); openReport(${escJsAttr(h.report_id || lv.id)}, 'insights');">Open Report Clips →</a>
          <button class="sm emerald" onclick="pinHabitAsFocusGoal(${idx}, this)">+ Pin as Focus Goal</button>
        </div>
      </div>
    `).join("");

    featuredHtml = `
      <div class="card">
        <div class="row-between">
          <div>
            <div class="row" style="gap:8px;">
              <strong style="font-size:16px;">✦ Latest Verified Habits Checkup</strong>
              <span class="pill ready">${esc(lv.date)}</span>
            </div>
            <div style="font-size:12.5px; color:var(--muted); margin-top:2px;">${esc(lv.matchup)} · ${esc(lv.summary)}</div>
          </div>
          <button type="button" class="sm primary" onclick="openReport(${escJsAttr(lv.id)}, 'insights')">Open Full Report →</button>
        </div>
        <div class="habits-grid">${cards}</div>
      </div>
    `;
  }

  // Reports Archive
  const allReports = d.reports || [];
  const filtered = allReports.filter(r => {
    if (state.insightsFilter !== "all" && r.type !== state.insightsFilter) return false;
    if (state.insightsSearch) {
      const hay = `${r.date} ${r.type_label} ${r.matchup} ${r.summary} ${r.metric} ${r.filename}`.toLowerCase();
      if (!hay.includes(state.insightsSearch)) return false;
    }
    return true;
  });

  const c = d.counts || {};
  const rowsHtml = filtered.length
    ? filtered.map(r => {
        const badgeCls = r.type === "hybrid_habit_rollup"
          ? "ready"
          : (r.type === "full_set" ? "cached" : (r.type === "sequence_search" ? "note" : "drill"));
        return `
          <tr>
            <td class="mono" style="white-space:nowrap; font-size:12.5px; color:var(--muted);">${esc(r.date)}</td>
            <td><span class="pill ${badgeCls}">${esc(r.type_label)}</span></td>
            <td>
              <a href="${esc(safeHref(r.href))}" style="font-weight:700;" onclick="event.preventDefault(); openReport(${escJsAttr(r.id)}, 'insights');">${esc(r.matchup)}</a>
              <div class="mono" style="font-size:11px; color:var(--muted-dim);">${esc(r.id)}</div>
            </td>
            <td style="font-size:13px; color:var(--text-soft);">${esc(r.summary)}</td>
            <td style="font-size:12.5px; font-weight:700; color:var(--emerald-fg);">${esc(r.metric)}</td>
          </tr>
        `;
      }).join("")
    : `<tr><td colspan="5" style="text-align:center; color:var(--muted); padding:24px;">No reports match this filter yet.</td></tr>`;

  host.innerHTML = `
    <div class="card">
      <div class="row-between" style="gap:12px;">
        <div>
          <strong style="font-size:16px;">AI &amp; Top-Rank Insights</strong>
          <div style="font-size:12.5px; color:var(--muted);">
            Run Hybrid VLM + RAM checkups without PowerShell · Top-Rank corpus: ${caps.pro_corpus_sets || 0} sets · Gemini Vision: ${caps.has_gemini ? "Ready" : "Not configured"}
          </div>
        </div>
        <div class="row" style="gap:8px;">
          <button class="emerald sm" onclick="triggerVerifiedHabitsRun()" ${!canRunAi ? `disabled title="${esc(aiTip)}"` : ""}>✦ Run Verified Habits Checkup</button>
        </div>
      </div>
      <div class="row" style="margin-top:10px; padding-top:10px; border-top:1px solid var(--border-soft);">
        <select id="insightMatchPicker" class="input compact-select" style="flex:1; min-width:220px;">
          ${pickerOpts || `<option value="">No telemetry-ready matches yet — process a match first</option>`}
        </select>
        <button class="primary sm" onclick="triggerMatchBreakdownFromPicker()" ${(!canRunAi || !telemMatches.length) ? `disabled title="${esc(aiTip)}"` : ""}>✨ Analyze Selected Match</button>
      </div>
    </div>

    ${runningHtml}
    ${featuredHtml}

    <div class="card">
      <div class="row-between" style="margin-bottom:8px;">
        <div class="chips" style="padding:0;">
          <button class="chip ${state.insightsFilter === "all" ? "active" : ""}" onclick="setInsightsFilter('all')">All (${allReports.length})</button>
          <button class="chip ${state.insightsFilter === "hybrid_habit_rollup" ? "active" : ""}" onclick="setInsightsFilter('hybrid_habit_rollup')">Verified Habits (${c.verified_habits || 0})</button>
          <button class="chip ${state.insightsFilter === "full_set" ? "active" : ""}" onclick="setInsightsFilter('full_set')">Match Breakdowns (${c.full_set || 0})</button>
          <button class="chip ${state.insightsFilter === "sequence_search" ? "active" : ""}" onclick="setInsightsFilter('sequence_search')">Sequence Searches (${c.sequence_search || 0})</button>
        </div>
        <label style="display:inline-flex; align-items:center; gap:6px; font-size:12px; color:var(--muted); cursor:pointer;">
          <input type="checkbox" ${state.includeLegacyInsights ? "checked" : ""} onchange="toggleLegacyInsights()" />
          <span>Show Legacy v1 Rollups (${c.legacy_rollups || 0})</span>
        </label>
      </div>
      <input class="input compact-input" value="${esc(state.insightsSearch)}" placeholder="Filter reports by player, character, or habit..." oninput="onInsightsSearchInput(this.value)" />
      <div class="reports-table-wrap">
        <table class="reports-table">
          <thead>
            <tr><th>Date</th><th>Type</th><th>Matchup / Report</th><th>Summary</th><th>Key Metric</th></tr>
          </thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
    </div>
  `;
}
