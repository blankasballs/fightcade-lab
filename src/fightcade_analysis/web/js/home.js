/* Home Workbench (#home): Latest Match Hero, Pinned Focus Goals (max 3), Playlists & Pulse Strip */

async function loadHomeWorkbench() {
  const host = document.getElementById("homeContent");
  try {
    const u = state.username || DEFAULT_USER;
    const data = await api(`/api/home?username=${encodeURIComponent(u)}`);
    state.homeData = data;
    if (data.capabilities) {
      state.capabilities = { ...state.capabilities, ...data.capabilities };
    }
    state.focusGoals = (data.focus_goals && data.focus_goals.active) || [];
    if (Array.isArray(data.playlists)) {
      // Keep full playlists list fresh if already loaded, otherwise seed from home
      if (!state.playlists.length) state.playlists = data.playlists;
    }
    renderHomeWorkbench();
  } catch (err) {
    if (host) {
      host.innerHTML = `<div class="card empty-card">Failed to load Home Workbench: ${esc(err.message)}</div>`;
    }
  }
}

function renderHomeWorkbench() {
  const host = document.getElementById("homeContent");
  if (!host) return;
  const d = state.homeData;
  if (!d) return;

  const latest = d.latest_match;
  const recent = d.recent_matches || [];
  const fg = d.focus_goals || { active: [], graduated_count: 0, max_active: 3 };
  const activeGoals = fg.active || [];
  const pls = d.playlists || [];
  const pulse = d.pulse || {};

  // ---- SECTION 1: LATEST MATCH HERO & FIRST-RUN WIZARD ----
  let heroHtml = "";
  if (!latest) {
    const env = (state.capabilities && state.capabilities.environment) || {};
    const checkBadge = (ok, label) =>
      `<span class="pill ${ok ? "ready" : "unproc"}">${ok ? "✓" : "○"} ${esc(label)}</span>`;
    const wizardChecks = !isHostedMode()
      ? `
        <div class="row" style="gap:6px; margin:8px 0 12px;">
          ${checkBadge(env.fbneo_found, "Fightcade FBNeo")}
          ${checkBadge(env.rom_found, "3S ROM")}
          ${checkBadge(env.has_grouflon, "Grouflon Hitboxes (optional)")}
          ${checkBadge(env.fast_fbneo_found, "Fast Capture Engine (optional)")}
        </div>
        <div class="row" style="gap:8px; margin-bottom:12px;">
          <button class="sm" onclick="registerCompanionUriFromUi()">⚡ Register fightcadelab:// Links</button>
          <button class="sm" onclick="pairLocalCompanionWithBrowser()">🔗 Pair with Browser</button>
        </div>
      `
      : "";
    heroHtml = `
      <div class="hero-card">
        <div class="hero-eyebrow"><span class="dot"></span><span>WELCOME TO FIGHTCADE LAB</span></div>
        <div class="hero-headline" style="font-size:22px;">Sync your Fightcade match history to get started</div>
        <div class="hero-meta">Pull your recent 3rd Strike sets, review 60fps replays with toggleable overlays, and jump straight into Replay Takeover.</div>
        ${wizardChecks}
        <div class="row" style="max-width:520px;">
          <input id="homeFirstUser" class="input" style="flex:1;" value="${esc(state.username || DEFAULT_USER)}" placeholder="Fightcade username" />
          <button class="primary" onclick="homeFirstRunProcessLatest()">▶ Process My Latest Match</button>
          <button class="sm" onclick="homeFirstRunRefresh()">Refresh Only</button>
        </div>
      </div>
    `;
  } else {
    const relTime = formatRelativeTime(latest.date, latest.date_str);
    const rankedLbl = latest.ranked ? "RANKED" : "CASUAL";
    const statusPill = latest.status === "ready"
      ? `<span class="pill ready">● Ready to Review</span>`
      : (latest.status === "processing" || latest.status === "queued")
        ? `<span class="pill busy">◌ ${esc((latest.job && latest.job.message) || "Rendering...")}</span>`
        : `<span class="pill unproc">● Unprocessed</span>`;

    const selfChar = latest.self_char_name ? `<span class="hero-char-self">(${esc(latest.self_char_name)})</span>` : "";
    const oppChar = latest.opp_char_name ? `<span class="hero-char-opp">(${esc(latest.opp_char_name)})</span>` : "";
    const oppRk = latest.opp_rank_letter ? `<span class="rank">${esc(latest.opp_rank_letter)}</span>` : "";

    const metaParts = [];
    if (latest.self_score != null && latest.opp_score != null && (latest.self_score || latest.opp_score)) {
      metaParts.push(`Score ${latest.self_score}–${latest.opp_score}`);
    }
    if (latest.mobile_round_count || latest.round_count) {
      metaParts.push(`${latest.mobile_round_count || latest.round_count} rounds`);
    }
    if (latest.duration_s > 0) metaParts.push(formatDuration(latest.duration_s));
    if (latest.status === "ready") {
      metaParts.push(latest.has_overlay_data ? "Overlays ready" : "Generate overlays in Studio");
    }

    const selCount = Number(latest.select_marks_count || 0);
    const selRoundsTxt = (latest.select_mark_rounds || []).length
      ? ` (${latest.select_mark_rounds.map(r => `R${r}`).join(", ")})`
      : "";
    const marksCallout = selCount > 0
      ? `<div class="hero-marks-callout">⚡ ${selCount} in-game Select mark${selCount === 1 ? "" : "s"} captured${esc(selRoundsTxt)}</div>`
      : "";

    const primaryCta = latest.status === "ready"
      ? `<button class="primary cta-primary" onclick="openMatchFromHome('${esc(latest.quark_id)}')">▶ Review Match</button>`
      : (latest.status === "processing" || latest.status === "queued")
        ? `<button class="cta-primary" disabled>Rendering...</button>`
        : `<button class="primary cta-primary" onclick="processMatch('${esc(latest.quark_id)}', ${latest.perspective || 1})">Process Match</button>`;

    const jumpMarksCta = (selCount > 0 && latest.status === "ready" && latest.first_select_mark)
      ? `<button class="amber cta-sec" onclick="openMatchAtFirstMark('${esc(latest.quark_id)}', ${latest.perspective || 1}, ${latest.first_select_mark.round_id || 1}, ${latest.first_select_mark.anchor_gf || 0})">Jump to ${selCount} Mark${selCount === 1 ? "" : "s"}</button>`
      : "";

    let aiCta = "";
    if (latest.has_report && latest.report_url) {
      aiCta = `<button type="button" class="cta-sec" onclick="openReport('${esc(latest.report_url)}', 'home')">✨ AI Breakdown</button>`;
    } else if (latest.has_telemetry && isProCorpusEnabled()) {
      aiCta = `<button class="cta-sec" onclick="triggerMatchBreakdown('${esc(latest.quark_id)}', ${latest.perspective || 1})">✨ AI Breakdown</button>`;
    }

    const previewInner = latest.preview_video_url
      ? `<video muted preload="metadata" playsinline src="${esc(latest.preview_video_url)}#t=2.5"></video>`
      : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px;">${latest.status === "ready" ? "4:3 Ready" : "Process to render 60fps"}</div>`;

    const recentPills = recent.map(m => {
      const sc = (m.self_score != null && m.opp_score != null && (m.self_score || m.opp_score))
        ? ` · ${m.self_score}–${m.opp_score}` : "";
      const ch = m.opp_char_name ? ` (${m.opp_char_name})` : "";
      const onclick = m.status === "ready"
        ? `openMatchFromHome('${esc(m.quark_id)}')`
        : `navigateTo('matches')`;
      return `<button class="recent-pill" onclick="${onclick}">vs ${esc(m.opp_player_name || "Opponent")}${esc(ch)}${esc(sc)}</button>`;
    }).join("");

    heroHtml = `
      <div class="hero-card">
        <div class="hero-top">
          <div>
            <div class="hero-eyebrow">
              <span class="dot"></span>
              <span>LATEST MATCH${relTime ? ` · ${esc(relTime.toUpperCase())}` : ""} · ${rankedLbl}</span>
              ${statusPill}
            </div>
            <div class="hero-headline">
              <span>${esc(latest.self_player_name || state.username)}</span>
              ${selfChar}
              <span class="hero-vs">vs</span>
              <span>${esc(latest.opp_player_name || "Opponent")}</span>
              ${oppRk}
              ${oppChar}
            </div>
            <div class="hero-meta">${esc(metaParts.join("  ·  "))}</div>
            ${marksCallout}
            <div class="hero-actions">
              ${primaryCta}
              ${jumpMarksCta}
              ${aiCta}
            </div>
          </div>
          <div class="hero-preview" onclick="${latest.status === "ready" ? `openMatchFromHome('${esc(latest.quark_id)}')` : `navigateTo('matches')`}">
            ${previewInner}
            <div class="hero-preview-play">
              <div class="hero-play-circle">▶</div>
            </div>
            <span class="hero-preview-tag mono">60fps · 4:3</span>
          </div>
        </div>
        <div class="hero-recent-strip">
          <div class="row" style="gap:6px;">
            <span style="font-size:12px; font-weight:700; color:var(--muted-dim);">Recent sets:</span>
            ${recentPills || `<span style="font-size:12px; color:var(--muted);">No earlier matches cached yet</span>`}
          </div>
          <a href="#matches" style="font-size:12.5px; font-weight:700;" onclick="event.preventDefault(); navigateTo('matches');">All Matches →</a>
        </div>
      </div>
    `;
  }

  // ---- SECTION 2: PINNED FOCUS GOALS + PLAYLISTS ----
  const atGoalCap = activeGoals.length >= (fg.max_active || 3);
  const plById = {};
  (state.playlists.length ? state.playlists : pls).forEach(p => { plById[p.id] = p; });

  const goalsBody = activeGoals.length
    ? activeGoals.map(g => {
        const linkedPl = g.playlist_id ? plById[g.playlist_id] : null;
        const badge = g.trend_hint
          ? `<span class="pill ready">${esc(g.trend_hint)}</span>`
          : (g.matchup_char
            ? `<span class="pill note">vs ${esc(g.matchup_char)}</span>`
            : (g.source_insight_id ? `<span class="pill cached">From AI Checkup</span>` : ""));
        const metaLine = linkedPl
          ? `Linked playlist: ${linkedPl.name} · ${linkedPl.item_count || (linkedPl.items || []).length} clips`
          : ((g.tags && g.tags.length) ? `Tags: ${g.tags.join(", ")}` : (g.description || "Active focus goal"));

        const watchAction = linkedPl
          ? `openWatchMode('${esc(linkedPl.id)}')`
          : `openNotesFilteredByGoal('${esc(g.id)}')`;
        const trainBtn = (canTakeoverNow() && linkedPl)
          ? `<button class="sm amber" onclick="openTrainMode('${esc(linkedPl.id)}')">▶ Train</button>`
          : "";

        return `
          <div class="goal-row">
            <div class="row-between" style="margin-bottom:2px;">
              <div class="goal-row-title">
                <span class="dot"></span>
                <span>${esc(g.title)}</span>
              </div>
              ${badge}
            </div>
            <div class="goal-row-meta">${esc(metaLine)}</div>
            <div class="goal-row-actions">
              <div class="row" style="gap:6px;">
                <button class="sm" onclick="${watchAction}">Watch</button>
                ${trainBtn}
                <button class="sm ghost" onclick="openGoalModal('${esc(g.id)}')" title="Edit focus goal">Edit</button>
              </div>
              <button class="sm ghost" onclick="graduateFocusGoal('${esc(g.id)}')" title="Mark this focus goal as mastered">Graduate ✓</button>
            </div>
          </div>
        `;
      }).join("")
    : `<div class="empty-inline">Pin up to 3 active training focuses to guide your post-match review.<br/><button class="sm primary" style="margin-top:10px;" onclick="openGoalModal()">+ Pin Your First Focus Goal</button></div>`;

  const plBody = pls.length
    ? pls.map(p => {
        const cnt = Number(p.item_count ?? (p.items || []).length);
        const readyCnt = Number(p.takeover_ready_count || 0);
        const linkedGoal = (p.linked_goals && p.linked_goals[0]) ? ` · Linked to ${p.linked_goals[0].title}` : "";
        const shareTxt = p.shareable ? " · Shareable" : "";
        const subLine = `${cnt} moment${cnt === 1 ? "" : "s"} · ${readyCnt} Takeover-ready${linkedGoal}${shareTxt}`;
        const trainBtn = canTakeoverNow()
          ? `<button class="sm amber" onclick="openTrainMode('${esc(p.id)}')" ${cnt === 0 ? "disabled" : ""}>▶ Train</button>`
          : "";
        return `
          <div class="pl-row">
            <div class="pl-icon ${esc(p.kind || "drill")}">▶</div>
            <div class="pl-info">
              <div class="pl-name">${esc(p.name)}</div>
              <div class="pl-meta">${esc(subLine)}</div>
            </div>
            <div class="row" style="gap:6px; flex-wrap:nowrap;">
              <button class="sm" onclick="openWatchMode('${esc(p.id)}')" ${cnt === 0 ? "disabled" : ""}>Watch</button>
              ${trainBtn}
            </div>
          </div>
        `;
      }).join("")
    : `<div class="empty-inline">Build Playlists of tough spacings, anti-air drills, or pro answers to watch on phone and train on desktop.<br/><button class="sm primary" style="margin-top:10px;" onclick="openPlaylistModal()">+ Create First Playlist</button></div>`;

  // ---- SECTION 3: COMPACT BOTTOM PULSE STRIP ----
  const notesSummary = `${pulse.notes_total || 0} saved moments across ${pulse.matchups_count || 0} matchups · ${pulse.untriaged_marks || 0} untriaged marks`;
  const li = pulse.latest_insight;
  const insightSummary = li
    ? `${li.title}${li.top_habit ? ` · ${li.top_habit}` : ""}`
    : "Run a Hybrid VLM + RAM checkup to discover recurring leaks";

  const compTip = canTakeoverNow()
    ? "Desktop Companion active · Tap Select in Fightcade to bookmark live · Alt+5 Sandbox Training · Alt+6/7 Speed throttle"
    : "Study Mode · Watch playlists & annotate replays from anywhere · Open on Windows Desktop for Replay Takeover";

  host.innerHTML = `
    ${heroHtml}

    <div class="workbench-grid">
      <div class="wb-card">
        <div class="wb-card-head">
          <div class="wb-card-title">
            <span>Pinned Focus Goals</span>
            <span class="pill ready mono">${activeGoals.length}/${fg.max_active || 3}</span>
          </div>
          <button class="sm" style="border-color:rgba(56,189,248,0.5); color:var(--accent-fg);" onclick="openGoalModal()" ${atGoalCap ? 'title="Max 3 active goals — graduate one first"' : ""}>+ Add Goal</button>
        </div>
        <div>${goalsBody}</div>
        <div class="wb-card-footer">
          <span>${fg.graduated_count || 0} graduated goal${fg.graduated_count === 1 ? "" : "s"} mastered</span>
          <a href="#" style="font-weight:700;" onclick="event.preventDefault(); openGraduatedModal();">View History →</a>
        </div>
      </div>

      <div class="wb-card">
        <div class="wb-card-head">
          <div>
            <div class="wb-card-title">Playlists</div>
            <div class="wb-card-sub">Watch on phone · Train on desktop</div>
          </div>
          <button class="sm" onclick="openPlaylistModal()">+ New List</button>
        </div>
        <div>${plBody}</div>
        <div class="wb-card-footer">
          <span>${d.playlists_total || pls.length} total playlist${(d.playlists_total || pls.length) === 1 ? "" : "s"}</span>
          <a href="#playlists" style="font-weight:700;" onclick="event.preventDefault(); navigateTo('playlists');">All Playlists →</a>
        </div>
      </div>
    </div>

    <div class="pulse-grid">
      <div class="pulse-card" onclick="navigateTo('notes')">
        <div class="pulse-icon notes">✎</div>
        <div style="min-width:0;">
          <div style="font-weight:800; font-size:14.5px;">Matchup Notebook</div>
          <div style="font-size:12.5px; color:var(--muted);">${esc(notesSummary)}</div>
        </div>
        <span class="pulse-cta">Open Notes →</span>
      </div>
      <div class="pulse-card ${isProCorpusEnabled() ? "" : "pro-gated"}" ${isProCorpusEnabled() ? `onclick="navigateTo('insights')"` : `aria-disabled="true" title="On the roadmap" onclick="toast('Insights & AI Checkups are on the roadmap.')"`}>
        <div class="pulse-icon insights">✦</div>
        <div style="min-width:0;">
          <div style="font-weight:800; font-size:14.5px;">Latest AI &amp; Pro Checkup</div>
          <div style="font-size:12.5px; color:var(--muted);">${esc(isProCorpusEnabled() ? insightSummary : "On the roadmap — opt in locally with FC_ENABLE_PRO_CORPUS=1")}</div>
        </div>
        <span class="pulse-cta">${isProCorpusEnabled() ? "Open Insights →" : "On the roadmap"}</span>
      </div>
    </div>

    <div class="footer-tip">${esc(compTip)}</div>
  `;
}

async function homeFirstRunRefresh() {
  const inp = document.getElementById("homeFirstUser");
  const u = (inp ? inp.value : state.username || DEFAULT_USER).trim();
  if (!u) return;
  state.username = u;
  localStorage.setItem("fc_mobile_user", u);
  const matchInp = document.getElementById("usernameInput");
  if (matchInp) matchInp.value = u;
  try { await api("/api/onboarding", { method: "POST", body: JSON.stringify({ username: u }) }); } catch (_) {}
  await refreshFromFightcade(false);
  await loadHomeWorkbench();
}

async function homeFirstRunProcessLatest() {
  const inp = document.getElementById("homeFirstUser");
  const u = (inp ? inp.value : state.username || DEFAULT_USER).trim();
  if (!u) return toast("Enter your Fightcade username first.");
  state.username = u;
  localStorage.setItem("fc_mobile_user", u);
  try { await api("/api/onboarding", { method: "POST", body: JSON.stringify({ username: u, completed: true }) }); } catch (_) {}
  await refreshFromFightcade(false);
  await loadHomeWorkbench();
  const latest = state.homeData && state.homeData.latest_match;
  if (latest && latest.quark_id) {
    if (latest.status === "ready") {
      openMatchFromHome(latest.quark_id);
    } else {
      await processMatch(latest.quark_id, latest.perspective || 1);
    }
  }
}

async function registerCompanionUriFromUi() {
  try {
    const res = await api("/api/onboarding", { method: "POST", body: JSON.stringify({ action: "register_uri" }) });
    toast(res.ok ? "Registered fightcadelab:// Takeover links!" : "URI registration failed.");
  } catch (err) {
    toast("URI registration failed: " + err.message);
  }
}

async function pairLocalCompanionWithBrowser() {
  try {
    const res = await api("/api/companion/pair-code", { method: "POST", body: JSON.stringify({}) });
    if (res.pair_url) {
      window.open(res.pair_url, "_blank", "noopener");
      toast("Opened Fightcade Lab browser pairing link!");
    }
  } catch (err) {
    toast("Could not start pairing: " + err.message);
  }
}

function openMatchFromHome(qid) {
  state.returnView = "home";
  openMatch(qid);
}

function openMatchAtFirstMark(qid, perspective, roundId, anchorGf) {
  state.returnView = state.activeView === "matches" ? "matches" : "home";
  openMatch(qid, perspective, {
    round_id: roundId,
    anchor_gf: anchorGf,
    autoPlay: false,
  });
}

function openNotesFilteredByGoal(goalId) {
  state.notesGoalFilter = goalId;
  navigateTo("notes");
}

// ---------- FOCUS GOALS CRUD & MODALS ----------
async function loadFocusGoals() {
  try {
    const res = await api(`/api/focus-goals?status=all&owner=${encodeURIComponent(state.username || DEFAULT_USER)}`);
    const all = res.goals || [];
    state.focusGoals = all.filter(g => g.status === "active");
    state.graduatedGoals = all.filter(g => g.status === "graduated");
    if (typeof renderStudioGoalsStrip === "function") renderStudioGoalsStrip();
    if (typeof renderHubNotesControls === "function" && state.activeView === "notes") renderHubNotesControls();
  } catch (_) {}
}

function openGoalModal(goalId = null, prefill = null) {
  const existing = goalId ? state.focusGoals.find(g => g.id === goalId) : null;
  if (!existing && state.focusGoals.length >= 3) {
    toast("Max 3 active Focus Goals — graduate one before pinning a new goal.");
    return;
  }
  state.editingGoalId = existing ? existing.id : null;
  state.pendingGoalSeedMoments = (prefill && Array.isArray(prefill.seed_moments) && prefill.seed_moments.length)
    ? prefill.seed_moments
    : null;

  document.getElementById("goalModalTitle").textContent = existing ? "Edit Focus Goal" : "Pin Focus Goal";
  document.getElementById("goalTitleInput").value = existing ? existing.title : ((prefill && prefill.title) || "");
  document.getElementById("goalDescInput").value = existing ? existing.description : ((prefill && prefill.description) || "");
  document.getElementById("goalMatchupInput").value = existing ? existing.matchup_char : ((prefill && prefill.matchup_char) || "");
  document.getElementById("goalTagsInput").value = existing ? (existing.tags || []).join(", ") : ((prefill && (prefill.tags || []).join(", ")) || "");
  document.getElementById("goalTrendInput").value = existing ? existing.trend_hint : ((prefill && prefill.trend_hint) || "");
  document.getElementById("goalModal").dataset.sourceInsightId = (prefill && prefill.source_insight_id) || (existing && existing.source_insight_id) || "";

  const plSel = document.getElementById("goalPlaylistSelect");
  const curPl = existing ? (existing.playlist_id || "") : "";
  const autoSeedOpt = state.pendingGoalSeedMoments
    ? `<option value="__auto_seed__">✦ Auto-create playlist from ${state.pendingGoalSeedMoments.length} verified clips</option>`
    : "";
  plSel.innerHTML = `${autoSeedOpt}<option value="">None</option>` + (state.playlists || []).map(p =>
    `<option value="${esc(p.id)}" ${p.id === curPl ? "selected" : ""}>${esc(p.name)}</option>`
  ).join("");
  if (state.pendingGoalSeedMoments) {
    plSel.value = "__auto_seed__";
  }

  document.getElementById("goalModal").classList.remove("hidden");
  document.getElementById("goalTitleInput").focus();
}

function closeGoalModal() {
  state.editingGoalId = null;
  state.pendingGoalSeedMoments = null;
  document.getElementById("goalModal").classList.add("hidden");
}

async function saveFocusGoalModal() {
  const title = document.getElementById("goalTitleInput").value.trim();
  if (!title) return toast("Enter a Focus Goal title");
  const description = document.getElementById("goalDescInput").value.trim();
  const matchup_char = document.getElementById("goalMatchupInput").value.trim();
  const tags = document.getElementById("goalTagsInput").value.split(",").map(s => s.trim()).filter(Boolean);
  const trend_hint = document.getElementById("goalTrendInput").value.trim();
  let playlist_id = document.getElementById("goalPlaylistSelect").value || null;
  const source_insight_id = document.getElementById("goalModal").dataset.sourceInsightId || null;

  const btn = document.getElementById("btnSaveGoal");
  btn.disabled = true;
  try {
    if (playlist_id === "__auto_seed__" && state.pendingGoalSeedMoments) {
      const plRes = await api("/api/playlists", {
        method: "POST",
        body: JSON.stringify({
          name: title.slice(0, 60),
          description: description || "Seeded from Verified Habits checkup",
          kind: "drill",
          owner: state.username || DEFAULT_USER,
          items: state.pendingGoalSeedMoments,
        }),
      });
      playlist_id = plRes.playlist.id;
    }

    if (state.editingGoalId) {
      await api(`/api/focus-goals/${encodeURIComponent(state.editingGoalId)}`, {
        method: "PATCH",
        body: JSON.stringify({ title, description, matchup_char, tags, playlist_id, trend_hint }),
      });
      toast("Focus Goal updated");
    } else {
      await api("/api/focus-goals", {
        method: "POST",
        body: JSON.stringify({
          owner: state.username || DEFAULT_USER,
          title,
          description,
          matchup_char,
          tags,
          playlist_id,
          source_insight_id,
          trend_hint,
        }),
      });
      toast("Focus Goal pinned!");
    }
    closeGoalModal();
    await Promise.all([loadFocusGoals(), loadPlaylists()]);
    if (state.activeView === "home") await loadHomeWorkbench();
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function graduateFocusGoal(goalId) {
  if (!confirm("Graduate this Focus Goal to your mastered history?")) return;
  try {
    await api(`/api/focus-goals/${encodeURIComponent(goalId)}`, {
      method: "PATCH",
      body: JSON.stringify({ status: "graduated" }),
    });
    toast("Focus Goal graduated! 🎉");
    await loadFocusGoals();
    if (state.activeView === "home") await loadHomeWorkbench();
  } catch (err) {
    toast(err.message);
  }
}

async function openGraduatedModal() {
  await loadFocusGoals();
  const host = document.getElementById("graduatedList");
  const list = state.graduatedGoals || [];
  if (!list.length) {
    host.innerHTML = `<div class="empty-inline">No graduated goals yet. Master an active Focus Goal and click <strong>Graduate ✓</strong> to log your progress!</div>`;
  } else {
    host.innerHTML = list.map(g => `
      <div class="goal-row">
        <div class="row-between">
          <strong>✓ ${esc(g.title)}</strong>
          <span class="pill ready">${esc((g.graduated_at || g.updated_at || "").slice(0, 10))}</span>
        </div>
        ${g.description ? `<div style="font-size:12.5px; color:var(--muted); margin-top:4px;">${esc(g.description)}</div>` : ""}
        <div class="row" style="margin-top:8px;">
          <button class="sm" onclick="reactivateFocusGoal('${esc(g.id)}')">Reactivate</button>
          <button class="sm danger" onclick="deleteFocusGoalById('${esc(g.id)}')">Delete</button>
        </div>
      </div>
    `).join("");
  }
  document.getElementById("graduatedModal").classList.remove("hidden");
}

function closeGraduatedModal() {
  document.getElementById("graduatedModal").classList.add("hidden");
}

async function reactivateFocusGoal(goalId) {
  try {
    await api(`/api/focus-goals/${encodeURIComponent(goalId)}`, {
      method: "PATCH",
      body: JSON.stringify({ status: "active" }),
    });
    toast("Focus Goal reactivated");
    await loadFocusGoals();
    openGraduatedModal();
    if (state.activeView === "home") await loadHomeWorkbench();
  } catch (err) {
    toast(err.message);
  }
}

async function deleteFocusGoalById(goalId) {
  if (!confirm("Permanently delete this Focus Goal?")) return;
  try {
    await api(`/api/focus-goals/${encodeURIComponent(goalId)}`, { method: "DELETE" });
    toast("Deleted");
    await loadFocusGoals();
    openGraduatedModal();
    if (state.activeView === "home") await loadHomeWorkbench();
  } catch (err) {
    toast(err.message);
  }
}
