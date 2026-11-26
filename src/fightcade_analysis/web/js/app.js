/* Application Router, Top-Nav Synchronization, Polling & Global Keyboard Shortcuts */

function syncTopNav() {
  const inStudio = state.activeView === "studio";
  const inReport = state.activeView === "report";
  const inShared = state.activeView === "shared_moment" || state.activeView === "shared_playlist";
  const showCrumb = inStudio || inReport;
  const mainEl = document.getElementById("appMain");
  if (mainEl) mainEl.classList.toggle("studio-mode", inStudio || inShared);

  const crumb = document.getElementById("studioBreadcrumb");
  const modeBadge = document.getElementById("modeBadge");
  if (crumb) crumb.classList.toggle("hidden", !showCrumb);
  if (modeBadge) modeBadge.classList.toggle("hidden", showCrumb);

  if (showCrumb) {
    const backLabels = { home: "← Home", matches: "← Matches", notes: "← Notes", playlists: "← Playlists", insights: "← Insights" };
    const backBtn = document.getElementById("btnBack");
    if (backBtn) backBtn.textContent = backLabels[state.returnView] || (inReport ? "← Insights" : "← Matches");
  }

  document.getElementById("navHome").classList.toggle("active", state.activeView === "home");
  document.getElementById("navMatches").classList.toggle("active", state.activeView === "matches");
  document.getElementById("navNotes").classList.toggle("active", state.activeView === "notes");
  document.getElementById("navInsights").classList.toggle("active", state.activeView === "insights" || inReport);

  document.getElementById("viewHome").classList.toggle("hidden", state.activeView !== "home");
  document.getElementById("viewMatches").classList.toggle("hidden", state.activeView !== "matches");
  document.getElementById("viewNotes").classList.toggle("hidden", state.activeView !== "notes");
  document.getElementById("viewInsights").classList.toggle("hidden", state.activeView !== "insights");
  const vr = document.getElementById("viewReport");
  if (vr) vr.classList.toggle("hidden", !inReport);
  document.getElementById("viewStudio").classList.toggle("hidden", !inStudio);
  const vs = document.getElementById("viewShared");
  if (vs) vs.classList.toggle("hidden", !inShared);

  updateStatusPillUi();
}

function navigateTo(view, updateHash = true) {
  if (typeof video !== "undefined" && video) video.pause();
  if (view !== "notes" && typeof stopMiniPreview === "function") stopMiniPreview();
  if (view !== "shared_moment" && view !== "shared_playlist" && typeof stopSharedPlayback === "function") stopSharedPlayback();
  state.playStopGf = null;

  if (view === "playlists") {
    state.activeView = "notes";
    state.notesSubView = "playlists";
    if (updateHash && window.location.hash !== "#playlists") {
      history.replaceState(null, "", "/#playlists");
    }
    syncTopNav();
    setNotesSubView("playlists");
    return;
  }

  if (view === "insights" && !isProCorpusEnabled()) {
    toast("Insights & AI Checkups are on the roadmap.");
    if (!state.activeView || state.activeView === "insights") {
      navigateTo("home", updateHash);
    }
    return;
  }

  state.activeView = view;
  const targetHash = `#${view}`;
  if (updateHash && (window.location.hash !== targetHash || /^\/[mp]\//.test(window.location.pathname))) {
    history.replaceState(null, "", `/${targetHash}`);
  }
  syncTopNav();

  if (view === "home") {
    loadHomeWorkbench();
  } else if (view === "matches") {
    loadMatches();
  } else if (view === "notes") {
    setNotesSubView("notebook");
    loadHubNotes(true);
  } else if (view === "insights") {
    loadInsights();
  }
}

// Backward-compatible view helpers
function showHistoryView() { navigateTo("matches"); }
function showNotesView() { navigateTo("notes"); }

function goBackFromStudio() {
  navigateTo(state.returnView || "matches");
}

function routeFromHash() {
  const path = window.location.pathname || "/";
  const raw = (window.location.hash || "").replace(/^#/, "");

  // Clean path routes for Companion pairing (/pair?code=...) & Shared Moment/Playlist (/m/:id, /p/:id)
  if (path === "/pair" || path === "/pair/" || BOOT.initialRoute === "pair" || raw.startsWith("pair")) {
    const qs = new URLSearchParams(window.location.search || (raw.includes("?") ? raw.split("?")[1] : ""));
    handleBrowserPairRoute(qs.get("code") || "");
    return;
  }

  // Clean path routes for Shared Moment (/m/:id) & Shared Playlist (/p/:id)
  if (!raw) {
    const mPath = /^\/m\/([^/?#]+)\/?$/.exec(path);
    if (mPath) {
      openSharedMoment(decodeURIComponent(mPath[1]));
      return;
    }
    const pPath = /^\/p\/([^/?#]+)\/?$/.exec(path);
    if (pPath) {
      openSharedPlaylist(decodeURIComponent(pPath[1]));
      return;
    }
    if (BOOT.initialRoute) {
      if (BOOT.initialRoute.startsWith("m/")) {
        openSharedMoment(BOOT.initialRoute.slice(2));
        return;
      }
      if (BOOT.initialRoute.startsWith("p/")) {
        openSharedPlaylist(BOOT.initialRoute.slice(2));
        return;
      }
    }
  }

  if (raw.startsWith("m/")) {
    openSharedMoment(decodeURIComponent(raw.slice(2).split("?")[0]));
    return;
  }
  if (raw.startsWith("p/")) {
    openSharedPlaylist(decodeURIComponent(raw.slice(2).split("?")[0]));
    return;
  }

  if (!raw || raw === "home") {
    navigateTo("home", false);
    return;
  }
  if (raw === "matches") {
    navigateTo("matches", false);
    return;
  }
  if (raw === "notes") {
    navigateTo("notes", false);
    return;
  }
  if (raw === "playlists") {
    navigateTo("playlists", false);
    return;
  }
  if (raw === "insights") {
    if (!isProCorpusEnabled()) {
      navigateTo("home", false);
      toast("Insights & AI Checkups are on the roadmap.");
      return;
    }
    navigateTo("insights", false);
    return;
  }
  if (raw.startsWith("report/")) {
    if (!isProCorpusEnabled()) {
      navigateTo("home", false);
      toast("Insights & AI Checkups are on the roadmap.");
      return;
    }
    const rid = decodeURIComponent(raw.slice("report/".length).split("?")[0]);
    openReport(rid, "insights");
    return;
  }
  if (raw.startsWith("replay/")) {
    const [pathPart, queryPart] = raw.split("?");
    const qid = decodeURIComponent(pathPart.slice("replay/".length));
    const qs = new URLSearchParams(queryPart || "");
    const r = qs.get("r") ? Number(qs.get("r")) : null;
    const gf = qs.get("gf") ? Number(qs.get("gf")) : null;
    const p = qs.get("p") ? Number(qs.get("p")) : null;
    openMatch(qid, p, (r || gf) ? { round_id: r, anchor_gf: gf, autoPlay: false } : null);
    return;
  }
  navigateTo("home", false);
}

// ---------- LIVE JOB & WATCHER POLLING ----------
let _hadActiveJobs = false;

async function pollJobs() {
  if (isHostedMode()) return;
  try {
    const data = await api("/api/jobs");
    state.watcher = data.watcher || state.watcher;
    state.jobs = data.jobs || [];
    updateStatusPillUi();
    renderStatusPopover();

    const active = state.jobs.filter(j => ["queued", "running", "paused"].includes(j.state));
    const banner = document.getElementById("jobBanner");
    if (active.length) {
      _hadActiveJobs = true;
      banner.classList.add("show");
      const j = active[0];
      const shortId = String(j.quark_id || "").split("-").pop();
      document.getElementById("jobBannerText").textContent = `${shortId}: ${j.message}`;
      document.getElementById("jobBannerPct").textContent = j.progress_total > 0
        ? `${j.progress_done}/${j.progress_total}`
        : (j.progress_done > 0 ? `R${j.progress_done}` : `${active.length} active`);
      if (state.activeView === "matches") loadMatches();
      if (state.activeView === "insights") renderInsights();
      clearTimeout(state.pollTimer);
      state.pollTimer = setTimeout(pollJobs, 2500);
    } else {
      banner.classList.remove("show");
      if (_hadActiveJobs) {
        _hadActiveJobs = false;
        if (state.activeView === "home") loadHomeWorkbench();
        else if (state.activeView === "matches") loadMatches();
        else if (state.activeView === "insights") loadInsights();
      }
    }
  } catch (_) {}
}

// ---------- GLOBAL KEYBOARD SHORTCUTS ----------
document.addEventListener("keydown", e => {
  const tag = (e.target && e.target.tagName || "").toUpperCase();
  const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

  if (e.key === "Escape") {
    closeStatusPopover();
    if (state.watchPlaylistId) { closeWatchMode(); return; }
    if (state.trainPlaylistId) { closeTrainMode(); return; }
    if (!document.getElementById("goalModal").classList.contains("hidden")) { closeGoalModal(); return; }
    if (!document.getElementById("graduatedModal").classList.contains("hidden")) { closeGraduatedModal(); return; }
    if (!document.getElementById("playlistModal").classList.contains("hidden")) { closePlaylistModal(); return; }
    if (!document.getElementById("addToPlModal").classList.contains("hidden")) { closeAddToPlaylistModal(); return; }
    if (!document.getElementById("companionModal")?.classList.contains("hidden")) { closeCompanionModal(); return; }
    if (!document.getElementById("reportModal")?.classList.contains("hidden")) { closeReportModal(); return; }
    if (!document.getElementById("termsModal")?.classList.contains("hidden")) { closeTermsModal(); return; }
    if (!document.getElementById("shareBundleModal")?.classList.contains("hidden")) { closeShareBundleModal(); return; }
  }

  if (inInput) return;

  // Watch Mode shortcuts
  if (state.watchPlaylistId) {
    if (e.code === "Space") { e.preventDefault(); toggleWatchPlay(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); prevWatchItem(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); nextWatchItem(); }
    return;
  }

  // Shared Moment / Playlist shortcuts
  if (state.activeView === "shared_moment" || state.activeView === "shared_playlist") {
    if (e.code === "Space") { e.preventDefault(); toggleSharedPlay(); }
    else if (e.key === ",") { e.preventDefault(); stepSharedFrames(-5); }
    else if (e.key === ".") { e.preventDefault(); stepSharedFrames(5); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); stepSharedFrames(-20); }
    else if (e.key === "ArrowRight") { e.preventDefault(); stepSharedFrames(20); }
    return;
  }

  // Replay Studio shortcuts
  if (state.activeView === "studio") {
    if (e.code === "Space") { e.preventDefault(); togglePlay(); }
    else if (e.key === ",") { e.preventDefault(); stepFrames(-5); }
    else if (e.key === ".") { e.preventDefault(); stepFrames(5); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); stepFrames(-20); }
    else if (e.key === "ArrowRight") { e.preventDefault(); stepFrames(20); }
    else if (e.key === "[") { e.preventDefault(); markRangeBound("start"); }
    else if (e.key === "]") { e.preventDefault(); markRangeBound("end"); }
  }
});

// ---------- BOOT ----------
async function initApp() {
  syncThemeUi();
  const userInp = document.getElementById("usernameInput");
  if (userInp) {
    userInp.value = state.username;
    userInp.addEventListener("keydown", e => { if (e.key === "Enter") loadMatches(); });
  }
  await loadCapabilities();
  await loadAuthState();
  if (isHostedMode()) {
    probeLocalCompanion();
  }
  if (!isHostedMode() || state.auth.authenticated) {
    loadFocusGoals();
    loadPlaylists(false);
    loadHubNotes(true);
  }
  routeFromHash();
  if (!isHostedMode()) {
    pollJobs();
    setInterval(pollJobs, 10000);
  }
}

window.addEventListener("hashchange", routeFromHash);
initApp();
