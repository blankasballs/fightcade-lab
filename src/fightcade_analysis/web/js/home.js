/* Home Workbench (#home): Latest Match Hero, Active Focuses (max 3), Reference Playlists & Pulse Strip */

async function loadHomeWorkbench() {
  const host = document.getElementById("homeContent");
  try {
    const u = state.username || DEFAULT_USER;
    const data = await api(`/api/home?username=${encodeURIComponent(u)}`);
    state.homeData = data;
    if (data.capabilities) {
      state.capabilities = { ...state.capabilities, ...data.capabilities };
    }
    const activeGoals = (data.focus_goals && data.focus_goals.active) || [];
    state.focusGoals = activeGoals;
    if (!state.playlists.length && (activeGoals.length || (data.playlists || []).length)) {
      state.playlists = [
        ...activeGoals.map(g => ({ ...g, name: g.name || g.title, is_focus: true, focus_status: "active" })),
        ...(data.playlists || []),
      ];
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
  const pls = (d.playlists || []).filter(p => !p.is_focus);
  const pulse = d.pulse || {};

  // ---- SECTION 1: LATEST MATCH HERO & FIRST-RUN WIZARD ----
  let heroHtml = "";
  if (isHostedMode() && (!state.auth.authenticated || !latest)) {
    const releasesHref = esc(safeHref(`${GITHUB_REPO_URL}/releases`));
    if (!state.auth.authenticated) {
      heroHtml = `
        <div class="hero-card">
          <div class="hero-welcome-grid">
            <div>
              <div class="hero-eyebrow"><span class="dot"></span><span>FIGHTCADE LAB</span></div>
              <div class="hero-headline" style="font-size:22px;">3rd Strike replay review, matchup notes, and Replay Takeover.</div>
              <div class="hero-meta">Full-match Replay Studio and Replay Takeover run locally in the free Windows Companion. Sign in here to access your shared clips, playlists, and coaching replies from any device.</div>
              <div class="hero-actions">
                <a class="btn-link primary" href="/api/auth/discord/start?next=%2F">Sign in with Discord</a>
                <a class="btn-link" href="${releasesHref}" target="_blank" rel="noopener">Download Windows Companion →</a>
              </div>
            </div>
            <div class="hero-welcome-art">
              <img src="/assets/img/header-astro-lab.jpg" alt="Fightcade Lab Astro City candy cabinet and lab flask" loading="eager" />
            </div>
          </div>
        </div>
      `;
      host.innerHTML = heroHtml;
      return;
    }

    const displayName = (state.auth.user && state.auth.user.discord_username) || d.username || "Player";
    const companionReady = Boolean(
      state.companionConnected ||
      (state.companionBridge && state.companionBridge.connected) ||
      (state.capabilities && state.capabilities.companion_paired)
    );
    const companionCta = !companionReady
      ? `<a class="btn-link primary sm" href="${releasesHref}" target="_blank" rel="noopener">Get Windows Companion →</a>`
      : "";
    const clipsCta = Number(pulse.notes_total || 0) > 0
      ? `<button class="sm" onclick="navigateTo('notes')">Open My Shared Clips →</button>`
      : "";
    const byocSetupCta = `<button class="sm" onclick="navigateTo('matches')">Set up mobile replay sync (free) →</button>`;
    const hostedActions = `<div class="hero-actions">${companionCta}${byocSetupCta}${clipsCta}</div>`;

    heroHtml = `
      <div class="hero-card">
        <div class="hero-welcome-grid">
          <div>
            <div class="hero-eyebrow"><span class="dot"></span><span>FIGHTCADE LAB · CLOUD</span></div>
            <div class="hero-headline" style="font-size:22px;">Welcome back, ${esc(displayName)}</div>
            <div class="hero-meta">Full-match Replay Studio and Takeover run locally on your PC. Your shared moments, playlists, and coaching replies stay accessible here on any device.</div>
            ${hostedActions}
          </div>
          <div class="hero-welcome-art">
            <img src="/assets/img/header-astro-lab.jpg" alt="Fightcade Lab Astro City candy cabinet and lab flask" loading="eager" />
          </div>
        </div>
      </div>
    `;
  } else if (!latest) {
    const env = (state.capabilities && state.capabilities.environment) || {};
    const checkBadge = (ok, label) =>
      `<span class="pill ${ok ? "ready" : "unproc"}">${ok ? "✓" : "○"} ${esc(label)}</span>`;
    const wizardChecks = `
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
      `;
    heroHtml = `
      <div class="hero-card">
        <div class="hero-welcome-grid">
          <div>
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
          <div class="hero-welcome-art">
            <img src="/assets/img/header-astro-lab.jpg" alt="Fightcade Lab — Astro City candy cabinet and lab flask" loading="eager" />
          </div>
        </div>
      </div>
    `;
  } else {
    const relTime = formatRelativeTime(latest.date, latest.date_str);
    const rankedLbl = latest.ranked ? "RANKED" : "CASUAL";
    const statusPill = (latest.status === "processing" || latest.status === "queued")
      ? `<span class="pill busy">◌ ${esc((latest.job && latest.job.message) || "Rendering...")}</span>`
      : "";

    const heroHeadlineHtml = formatMatchLabelHtml(latest, { defaultSelfName: state.username });

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

    const playableWhileStreaming = (latest.status === "processing" || latest.status === "queued") && Number(latest.mobile_round_count || 0) > 0;
    const primaryCta = latest.status === "ready"
      ? `<button class="primary cta-primary" onclick="openMatchFromHome(${escJsAttr(latest.quark_id)})">▶ Open in Studio</button>`
      : playableWhileStreaming
        ? `<button class="emerald cta-primary" onclick="openMatchFromHome(${escJsAttr(latest.quark_id)})">▶ Open in Studio (1–${Number(latest.mobile_round_count || 0)}R)</button>`
        : (latest.status === "processing" || latest.status === "queued")
          ? `<button class="cta-primary" disabled>Rendering...</button>`
          : `<button class="primary cta-primary" onclick="processMatch(${escJsAttr(latest.quark_id)}, ${Number(latest.perspective || 1)})">Process Match</button>`;

    let aiCta = "";
    if (latest.has_report && latest.report_url) {
      aiCta = `<button type="button" class="cta-sec" onclick="openReport(${escJsAttr(latest.report_url)}, 'home')">✨ AI Breakdown</button>`;
    } else if (latest.has_telemetry && isProCorpusEnabled()) {
      aiCta = `<button class="cta-sec" onclick="triggerMatchBreakdown(${escJsAttr(latest.quark_id)}, ${Number(latest.perspective || 1)})">✨ AI Breakdown</button>`;
    }

    const previewInner = latest.preview_video_url
      ? `<video muted preload="metadata" playsinline src="${esc(safeHref(latest.preview_video_url))}#t=2.5"></video>`
      : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:12px;">${latest.status === "ready" ? "4:3 Ready" : "Process to render 60fps"}</div>`;

    const recentPills = recent.map(m => {
      const sc = (m.self_score != null && m.opp_score != null && (m.self_score || m.opp_score))
        ? ` · ${m.self_score}–${m.opp_score}` : "";
      const onclick = m.status === "ready"
        ? `openMatchFromHome(${escJsAttr(m.quark_id)})`
        : `navigateTo('matches')`;
      const lbl = formatMatchLabelText(m, { defaultSelfName: state.username, showRank: false });
      return `<button class="recent-pill" onclick="${onclick}">${esc(lbl)}${esc(sc)}</button>`;
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
            <div class="hero-headline">${heroHeadlineHtml}</div>
            <div class="hero-meta">${esc(metaParts.join("  ·  "))}</div>
            <div class="hero-actions">
              ${primaryCta}
              ${aiCta}
            </div>
          </div>
          <div class="hero-preview" onclick="${(latest.status === "ready" || playableWhileStreaming) ? `openMatchFromHome(${escJsAttr(latest.quark_id)})` : `navigateTo('matches')`}">
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

  // ---- SECTION 2: ACTIVE FOCUSES + REFERENCE PLAYLISTS ----
  const atGoalCap = activeGoals.length >= (fg.max_active || 3);

  const goalsBody = activeGoals.length
    ? activeGoals.map(g => {
        const cnt = Number(g.item_count ?? (g.items || []).length);
        const badge = g.trend_hint
          ? `<span class="pill ready">${esc(g.trend_hint)}</span>`
          : (g.matchup_char
            ? `<span class="pill note">vs ${esc(g.matchup_char)}</span>`
            : (g.source_insight_id ? `<span class="pill cached">From AI Checkup</span>` : ""));

        const clipSummary = cnt > 0
          ? `${cnt} clip${cnt === 1 ? "" : "s"}`
          : "Mental cue · 0 clips";
        const extraDesc = g.description
          ? ` · ${g.description.slice(0, 110)}${g.description.length > 110 ? "…" : ""}`
          : ((g.tags && g.tags.length) ? ` · ${g.tags.join(", ")}` : "");

        const playControls = cnt > 0
          ? `<button class="sm" data-tip="Watch clips in looping study reel" onclick="openWatchMode(${escJsAttr(g.id)})">Watch</button>`
          : `<span class="goal-empty-hint">Add clips during review</span>`;

        return `
          <div class="goal-row">
            <div class="row-between" style="margin-bottom:2px;">
              <div class="goal-row-title">
                <span class="dot"></span>
                <span>${esc(g.title || g.name)}</span>
              </div>
              ${badge}
            </div>
            <div class="goal-row-meta">${esc(clipSummary + extraDesc)}</div>
            <div class="goal-row-actions">
              <div class="row" style="gap:6px;">
                ${playControls}
                <button class="sm ghost" onclick="openGoalModal(${escJsAttr(g.id)})" data-tip="Edit focus">Edit</button>
              </div>
              <button class="sm ghost" onclick="graduateFocusGoal(${escJsAttr(g.id)})" data-tip="Mark this focus as mastered (clips stay archived)">Graduate ✓</button>
            </div>
          </div>
        `;
      }).join("")
    : `<div class="empty-inline">Pinned playlists (max 3) — your current training priorities.<br/><button class="sm primary" style="margin-top:10px;" onclick="openGoalModal()">+ Add First Focus</button></div>`;

  const plBody = pls.length
    ? pls.map(p => {
        const cnt = Number(p.item_count ?? (p.items || []).length);
        const shareTxt = p.shareable ? " · Shareable" : "";
        const subLine = `${cnt} clip${cnt === 1 ? "" : "s"}${shareTxt}`;
        return `
          <div class="pl-row">
            <div class="pl-icon ${esc(p.kind || "drill")}">▶</div>
            <div class="pl-info">
              <div class="pl-name">${esc(p.name)}</div>
              <div class="pl-meta">${esc(subLine)}</div>
            </div>
            <div class="row" style="gap:5px; flex-wrap:nowrap;">
              <button class="sm" data-tip="Open looping study reel with toggleable overlays" onclick="openWatchMode(${escJsAttr(p.id)})" ${cnt === 0 ? "disabled" : ""}>Watch</button>
              <button class="sm ghost pl-focus-btn" data-tip="${atGoalCap ? "Max 3 active focuses — graduate or unpin one first" : "Pin as an Active Focus"}" onclick="promotePlaylistToFocus(${escJsAttr(p.id)})">📌 Focus</button>
            </div>
          </div>
        `;
      }).join("")
    : `<div class="empty-inline">All playlists — watch, practice in Takeover, or pin any as an Active Focus.<br/><button class="sm primary" style="margin-top:10px;" onclick="openPlaylistModal()">+ New Playlist</button></div>`;

  // ---- SECTION 3: COMPACT BOTTOM PULSE STRIP ----
  const notesSummary = `${pulse.notes_total || 0} saved moments across ${pulse.matchups_count || 0} matchups`;
  const li = pulse.latest_insight;
  const insightSummary = li
    ? `${li.title}${li.top_habit ? ` · ${li.top_habit}` : ""}`
    : "Run a Hybrid VLM + RAM checkup to discover recurring leaks";

  host.innerHTML = `
    ${heroHtml}

    <div class="workbench-grid">
      <div class="wb-card">
        <div class="wb-card-head">
          <div>
            <div class="wb-card-title">
              <span>Active Focuses</span>
              <span class="pill ready mono">${activeGoals.length}/${fg.max_active || 3}</span>
            </div>
            <div class="wb-card-sub">Pinned playlists (max 3) — your current training priorities.</div>
          </div>
          <button class="sm" style="border-color:rgba(56,189,248,0.5); color:var(--accent-fg);" onclick="openGoalModal()" data-tip="${atGoalCap ? "Max 3 active focuses — graduate or unpin one first" : "Add a new Active Focus (max 3 active)"}">+ Add Focus</button>
        </div>
        <div>${goalsBody}</div>
        <div class="wb-card-footer">
          <span>${fg.graduated_count || 0} graduated focus${fg.graduated_count === 1 ? "" : "es"} mastered</span>
          <a href="#" style="font-weight:700;" onclick="event.preventDefault(); openGraduatedModal();">Graduated history →</a>
        </div>
      </div>

      <div class="wb-card">
        <div class="wb-card-head">
          <div>
            <div class="wb-card-title">Playlists</div>
            <div class="wb-card-sub">All playlists — watch, practice in Takeover, or pin any as an Active Focus.</div>
          </div>
          <button class="sm" data-tip="Create a new playlist" onclick="openPlaylistModal()">+ New Playlist</button>
        </div>
        <div>${plBody}</div>
        <div class="wb-card-footer">
          <span>${d.playlists_total ?? pls.length} playlist${(d.playlists_total ?? pls.length) === 1 ? "" : "s"}</span>
          <a href="#playlists" style="font-weight:700;" onclick="event.preventDefault(); navigateTo('playlists');">All Playlists →</a>
        </div>
      </div>
    </div>

    <div class="pulse-grid ${isProCorpusEnabled() ? "" : "single-col"}">
      <div class="pulse-card" onclick="navigateTo('notes')">
        <div class="pulse-icon notes">✎</div>
        <div style="min-width:0;">
          <div style="font-weight:800; font-size:14.5px;">Matchup Notebook</div>
          <div style="font-size:12.5px; color:var(--muted);">${esc(notesSummary)}</div>
        </div>
        <span class="pulse-cta">Open Notes →</span>
      </div>
      ${isProCorpusEnabled() ? `
      <div class="pulse-card" onclick="navigateTo('insights')">
        <div class="pulse-icon insights">✦</div>
        <div style="min-width:0;">
          <div style="font-weight:800; font-size:14.5px;">Latest AI &amp; Top-Rank Checkup</div>
          <div style="font-size:12.5px; color:var(--muted);">${esc(insightSummary)}</div>
        </div>
        <span class="pulse-cta">Open Insights →</span>
      </div>` : ""}
    </div>
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

// ---------- UNIFIED FOCUS LIFECYCLE & MODALS ----------
async function loadFocusGoals() {
  try {
    const u = state.username || DEFAULT_USER;
    const res = await api(`/api/focus-goals?status=all&owner=${encodeURIComponent(u)}&username=${encodeURIComponent(u)}`);
    const all = res.goals || [];
    state.focusGoals = all.filter(g => g.status === "active");
    state.graduatedGoals = all.filter(g => g.status === "graduated");
    if (typeof renderComposerCollectionPicker === "function") renderComposerCollectionPicker();
    if (typeof renderNotesList === "function" && state.activeView === "studio") renderNotesList();
    if (typeof renderHubNotesControls === "function" && state.activeView === "notes") renderHubNotesControls();
  } catch (_) {}
}

async function _syncCollectionsAfterChange() {
  await Promise.all([loadPlaylists(state.activeView === "notes" && state.notesSubView === "playlists"), loadFocusGoals()]);
  if (state.activeView === "home") await loadHomeWorkbench();
  if (state.activeView === "studio") {
    if (typeof renderComposerCollectionPicker === "function") renderComposerCollectionPicker();
    if (typeof renderNotesList === "function") renderNotesList();
  }
}

async function promotePlaylistToFocus(plId) {
  try {
    const res = await api(`/api/playlists/${encodeURIComponent(plId)}`, {
      method: "PATCH",
      body: JSON.stringify({ is_focus: true, focus_status: "active" }),
    });
    toast(`Pinned "${res.playlist.name}" as Active Focus!`);
    await _syncCollectionsAfterChange();
  } catch (err) {
    toast(err.message);
  }
}

async function unpinFocusToPlaylist(plId) {
  try {
    const res = await api(`/api/playlists/${encodeURIComponent(plId)}`, {
      method: "PATCH",
      body: JSON.stringify({ is_focus: false, focus_status: null }),
    });
    toast(`Moved "${res.playlist.name}" to Playlists`);
    await _syncCollectionsAfterChange();
  } catch (err) {
    toast(err.message);
  }
}

function openGoalModal(goalId = null, prefill = null) {
  return openPlaylistModal(goalId, null, { defaultPinFocus: true, prefill });
}

function closeGoalModal() {
  return closePlaylistModal();
}

async function graduateFocusGoal(goalId) {
  if (!confirm("Graduate this Active Focus to your mastered history? (Its clips will stay archived and viewable.)")) return;
  try {
    await api(`/api/playlists/${encodeURIComponent(goalId)}`, {
      method: "PATCH",
      body: JSON.stringify({ focus_status: "graduated" }),
    });
    toast("Focus graduated! 🎉");
    await _syncCollectionsAfterChange();
  } catch (err) {
    toast(err.message);
  }
}

async function openGraduatedModal() {
  await Promise.all([loadFocusGoals(), loadPlaylists(false)]);
  const host = document.getElementById("graduatedList");
  const list = getGraduatedFocuses();
  if (!list.length) {
    host.innerHTML = `<div class="empty-inline">No graduated focuses yet. Master an Active Focus and click <strong>Graduate ✓</strong> to archive your progress!</div>`;
  } else {
    host.innerHTML = list.map(g => {
      const cnt = Number(g.item_count ?? (g.items || []).length);
      return `
        <div class="goal-row">
          <div class="row-between">
            <strong>✓ ${esc(g.title || g.name)}</strong>
            <div class="row" style="gap:6px;">
              <span class="pill">${cnt} clip${cnt === 1 ? "" : "s"}</span>
              <span class="pill ready">${esc((g.graduated_at || g.updated_at || "").slice(0, 10))}</span>
            </div>
          </div>
          ${g.description ? `<div style="font-size:12.5px; color:var(--muted); margin-top:4px;">${esc(g.description)}</div>` : ""}
          <div class="row" style="margin-top:8px;">
            ${cnt > 0 ? `<button class="sm" onclick="closeGraduatedModal(); openWatchMode(${escJsAttr(g.id)})">Watch</button>` : ""}
            <button class="sm primary" onclick="reactivateFocusGoal(${escJsAttr(g.id)}, true)">Reactivate</button>
            <button class="sm" onclick="unpinFocusToPlaylist(${escJsAttr(g.id)}).then(() => openGraduatedModal())">Move to Playlists</button>
            <button class="sm danger" onclick="deleteFocusGoalById(${escJsAttr(g.id)}, true)">Delete</button>
          </div>
        </div>
      `;
    }).join("");
  }
  document.getElementById("graduatedModal").classList.remove("hidden");
}

function closeGraduatedModal() {
  document.getElementById("graduatedModal").classList.add("hidden");
}

async function reactivateFocusGoal(goalId, reopenModal = false) {
  try {
    await api(`/api/playlists/${encodeURIComponent(goalId)}`, {
      method: "PATCH",
      body: JSON.stringify({ focus_status: "active" }),
    });
    toast("Focus reactivated!");
    await _syncCollectionsAfterChange();
    if (reopenModal) openGraduatedModal();
  } catch (err) {
    toast(err.message);
  }
}

async function deleteFocusGoalById(goalId, reopenModal = false) {
  if (!confirm("Permanently delete this Focus? (Your underlying notes and replays will not be deleted.)")) return;
  try {
    await api(`/api/playlists/${encodeURIComponent(goalId)}`, { method: "DELETE" });
    toast("Deleted");
    await _syncCollectionsAfterChange();
    if (reopenModal) openGraduatedModal();
  } catch (err) {
    toast(err.message);
  }
}
