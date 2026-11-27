/* Application Router, Browser History, Top-Nav Synchronization, Polling & Global Keyboard Shortcuts */

function _currentAppUrl() {
  if (typeof window === "undefined" || !window.location) return "/";
  return `${window.location.pathname || "/"}${window.location.search || ""}${window.location.hash || ""}`;
}

function _normalizeAppUrl(targetUrl) {
  const raw = String(targetUrl || "").trim();
  if (!raw) return "/";
  if (raw.startsWith("#")) {
    const path = (typeof window !== "undefined" && window.location && window.location.pathname) || "/";
    const search = (typeof window !== "undefined" && window.location && window.location.search) || "";
    return `${path}${search}${raw}`;
  }
  return raw.startsWith("/") ? raw : `/${raw}`;
}

function updateAppHistory(targetUrl, { replace = false } = {}) {
  const norm = _normalizeAppUrl(targetUrl);
  const cur = _currentAppUrl();
  if (state._routingFromPopstate) {
    state._lastRoutedUrl = cur;
    return;
  }
  // If a modal/drawer sentinel is currently top-of-stack:
  // - navigating to a DIFFERENT URL replaces that sentinel cleanly (no async history.back race)
  // - staying on the SAME URL pops the sentinel cleanly
  if (state._activeModalHistory) {
    const modalKey = state._activeModalHistory;
    if (norm === cur) {
      popModalHistory(modalKey);
      return;
    }
    state._activeModalHistory = null;
    if (typeof history !== "undefined" && typeof history.replaceState === "function") {
      history.replaceState({ fcApp: true, depth: Number(state._historyDepth || 0) }, "", norm);
    }
    state._lastRoutedUrl = norm;
    return;
  }
  if (replace || norm === cur) {
    if (typeof history !== "undefined" && typeof history.replaceState === "function") {
      history.replaceState({ fcApp: true, depth: Number(state._historyDepth || 0) }, "", norm);
    }
    state._lastRoutedUrl = norm;
    return;
  }
  state._historyDepth = Number(state._historyDepth || 0) + 1;
  if (typeof history !== "undefined" && typeof history.pushState === "function") {
    history.pushState({ fcApp: true, depth: state._historyDepth }, "", norm);
  }
  state._lastRoutedUrl = norm;
}

function pushModalHistory(modalKey) {
  if (!modalKey || state._routingFromPopstate) return;
  if (state._activeModalHistory === modalKey) return;
  const cur = _currentAppUrl();
  state._historyDepth = Number(state._historyDepth || 0) + 1;
  state._activeModalHistory = modalKey;
  if (typeof history !== "undefined" && typeof history.pushState === "function") {
    history.pushState({ fcApp: true, fcModal: modalKey, depth: state._historyDepth }, "", cur);
  }
  state._lastRoutedUrl = cur;
}

function popModalHistory(modalKey) {
  if (!modalKey || state._activeModalHistory !== modalKey) return;
  state._activeModalHistory = null;
  state._suppressNextPopstate = true;
  state._historyDepth = Math.max(0, Number(state._historyDepth || 0) - 1);
  if (typeof window !== "undefined" && window.history && typeof window.history.back === "function") {
    window.history.back();
  } else if (typeof history !== "undefined" && typeof history.back === "function") {
    history.back();
  }
}

function updateDocumentTitle(ctx = null) {
  if (typeof document === "undefined") return;
  const v = state.activeView;
  let title = "Fightcade Lab";
  if (v === "matches") {
    title = "Matches — Fightcade Lab";
  } else if (v === "notes") {
    title = state.notesSubView === "playlists" ? "Playlists — Fightcade Lab" : "Notes — Fightcade Lab";
  } else if (v === "insights") {
    title = "Insights — Fightcade Lab";
  } else if (v === "about") {
    title = "About — Fightcade Lab";
  } else if (v === "report") {
    title = "Report — Fightcade Lab";
  } else if (v === "studio") {
    const mf = (ctx && ctx.quark_id) ? ctx : state.activeManifest;
    const mu = mf ? formatMatchLabelText(mf, { showRank: false }) : "";
    title = mu ? `Replay Studio · ${mu} — Fightcade Lab` : "Replay Studio — Fightcade Lab";
  } else if (v === "shared_moment") {
    const m = state.sharedMoment;
    let mu = "";
    if (m && typeof _sharedProjectedSides === "function") {
      const sides = _sharedProjectedSides(m, state.sharedPerspective || 1);
      mu = formatMatchLabelText({
        self_player_name: sides.selfName,
        opp_player_name: sides.oppName,
        self_char_name: sides.selfChar,
        opp_char_name: sides.oppChar,
        quark_id: m.quark_id,
      }, { allowPlaceholders: true, showRank: false });
    } else if (m) {
      mu = formatMatchLabelText(m, { allowPlaceholders: true, showRank: false });
    }
    title = mu ? `${mu} — Fightcade Lab` : "Shared Moment — Fightcade Lab";
  } else if (v === "shared_playlist") {
    const pl = state.sharedPlaylist;
    const nm = String((pl && (pl.name || pl.title)) || "").trim();
    title = nm ? `${nm} — Fightcade Lab` : "Shared Playlist — Fightcade Lab";
  }
  document.title = title;
}

function syncTopNav() {
  const inStudio = state.activeView === "studio";
  const inReport = state.activeView === "report";
  const inShared = state.activeView === "shared_moment" || state.activeView === "shared_playlist";
  const showCrumb = inStudio || inReport;
  const mainEl = document.getElementById("appMain");
  if (mainEl) mainEl.classList.toggle("studio-mode", inStudio || inShared);

  const crumb = document.getElementById("studioBreadcrumb");
  const crumbMatchup = document.getElementById("crumbMatchup");
  const modeBadge = document.getElementById("modeBadge");
  if (crumb) crumb.classList.toggle("hidden", !showCrumb);
  if (crumbMatchup) crumbMatchup.classList.toggle("hidden", !inReport);
  if (modeBadge) modeBadge.classList.toggle("hidden", showCrumb);

  if (showCrumb) {
    const backLabels = { home: "Home", matches: "Matches", notes: "Notes", playlists: "Playlists", insights: "Insights", about: "About" };
    const lblText = backLabels[state.returnView] || (inReport ? "Insights" : "Matches");
    const backLbl = document.getElementById("btnBackLabel");
    if (backLbl) {
      backLbl.textContent = lblText;
    } else {
      const backBtn = document.getElementById("btnBack");
      if (backBtn) backBtn.textContent = `← ${lblText}`;
    }
  }

  setTopbarHidden(false);

  document.getElementById("navHome").classList.toggle("active", state.activeView === "home");
  const navMatches = document.getElementById("navMatches");
  if (navMatches) {
    navMatches.classList.remove("hidden");
    navMatches.classList.toggle("active", state.activeView === "matches");
  }
  document.getElementById("navNotes").classList.toggle("active", state.activeView === "notes");
  const navAbout = document.getElementById("navAbout");
  if (navAbout) navAbout.classList.toggle("active", state.activeView === "about");
  document.getElementById("navInsights").classList.toggle("active", state.activeView === "insights" || inReport);

  document.getElementById("viewHome").classList.toggle("hidden", state.activeView !== "home");
  document.getElementById("viewMatches").classList.toggle("hidden", state.activeView !== "matches");
  document.getElementById("viewNotes").classList.toggle("hidden", state.activeView !== "notes");
  const va = document.getElementById("viewAbout");
  if (va) va.classList.toggle("hidden", state.activeView !== "about");
  document.getElementById("viewInsights").classList.toggle("hidden", state.activeView !== "insights");
  const vr = document.getElementById("viewReport");
  if (vr) vr.classList.toggle("hidden", !inReport);
  document.getElementById("viewStudio").classList.toggle("hidden", !inStudio);
  const vs = document.getElementById("viewShared");
  if (vs) vs.classList.toggle("hidden", !inShared);

  updateStatusPillUi();
  updateDocumentTitle();
}

function navigateTo(view, updateHash = true) {
  const canConsumeSentinel = Boolean(updateHash && !state._routingFromPopstate);
  closeMobileNavMenu({ consumeSentinel: canConsumeSentinel });
  if (state.watchPlaylistId && typeof closeWatchMode === "function") {
    closeWatchMode({ consumeSentinel: canConsumeSentinel });
  }
  if (typeof video !== "undefined" && video) video.pause();
  if (view !== "studio") {
    state.loopPreviewActive = false;
    if (typeof syncLoopPreviewUi === "function") syncLoopPreviewUi();
    if (typeof stopAllStudioCardPreviews === "function") stopAllStudioCardPreviews();
    if (typeof stopStudioLivePoll === "function") stopStudioLivePoll();
    if (typeof dismissStudioNextOverlay === "function") dismissStudioNextOverlay();
    if (typeof closeComposer === "function" && state.composerOpen) closeComposer(true);
  }
  if (view !== "notes") {
    if (typeof stopMiniPreview === "function") stopMiniPreview();
    if (typeof closeAllHubCardMenus === "function") closeAllHubCardMenus();
    if (typeof closeNotesTagFilterMenu === "function") closeNotesTagFilterMenu();
  }
  if (view !== "about" && typeof closeAllArchPopovers === "function") closeAllArchPopovers();
  if (view !== "shared_moment" && view !== "shared_playlist" && typeof stopSharedPlayback === "function") stopSharedPlayback();
  state.playStopGf = null;

  if (view === "playlists") {
    state.activeView = "notes";
    state.notesSubView = "playlists";
    if (updateHash) {
      updateAppHistory("/#playlists");
    }
    syncTopNav();
    setNotesSubView("playlists", false);
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
  if (updateHash) {
    updateAppHistory(`/${targetHash}`);
  }
  syncTopNav();

  if (view === "home") {
    loadHomeWorkbench();
  } else if (view === "matches") {
    loadMatches();
  } else if (view === "notes") {
    setNotesSubView("notebook", false);
    loadHubNotes(true);
  } else if (view === "insights") {
    loadInsights();
  }
}

// Backward-compatible view helpers
function showHistoryView() { navigateTo("matches"); }
function showNotesView() { navigateTo("notes"); }

function openAboutExplainer() {
  navigateTo("about");
  const scrollToSec = () => {
    const el = document.getElementById("aboutDecentralized");
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(scrollToSec);
  } else {
    scrollToSec();
  }
}

// ---------- ABOUT PAGE INTERACTIVE ARCHITECTURE POPOVERS ----------
function closeAllArchPopovers() {
  const openZones = document.querySelectorAll ? document.querySelectorAll(".arch-zone.is-open") : [];
  let closedAny = false;
  if (openZones && openZones.length) {
    openZones.forEach(zone => {
      zone.classList.remove("is-open");
      const btn = zone.querySelector ? zone.querySelector(".arch-node-btn") : null;
      if (btn) btn.setAttribute("aria-expanded", "false");
      closedAny = true;
    });
  }
  const activeEl = typeof document !== "undefined" ? document.activeElement : null;
  if (activeEl && activeEl.classList && activeEl.classList.contains("arch-node-btn") && typeof activeEl.blur === "function") {
    activeEl.blur();
    closedAny = true;
  }
  return closedAny;
}

function toggleArchNodePopover(btn) {
  if (!btn || typeof btn.closest !== "function") return false;
  const zone = btn.closest(".arch-zone");
  if (!zone) return false;
  const wasOpen = zone.classList.contains("is-open");
  closeAllArchPopovers();
  if (!wasOpen) {
    zone.classList.add("is-open");
    btn.setAttribute("aria-expanded", "true");
    return true;
  }
  return false;
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("click", e => {
    const tgt = e && e.target;
    if (!tgt || typeof tgt.closest !== "function") return;
    const nodeBtn = tgt.closest(".arch-node-btn");
    if (nodeBtn) {
      e.preventDefault();
      toggleArchNodePopover(nodeBtn);
      return;
    }
    if (!tgt.closest(".arch-zone")) {
      closeAllArchPopovers();
    }
  });
}

function goBackFromStudio() {
  if (isMobileNavMenuOpen()) closeMobileNavMenu({ fromPopstate: false });
  if (Number(state._historyDepth || 0) > 0 && typeof window !== "undefined" && window.history && typeof window.history.back === "function") {
    window.history.back();
    return;
  }
  navigateTo(state.returnView || "matches");
}

function routeFromHash(fromPopstate = false) {
  const path = window.location.pathname || "/";
  const raw = (window.location.hash || "").replace(/^#/, "");
  state._lastRoutedUrl = _currentAppUrl();

  // Clean path routes for Companion pairing (/pair?code=...) & Shared Moment/Playlist (/m/:id, /p/:id)
  if (path === "/pair" || path === "/pair/" || BOOT.initialRoute === "pair" || raw.startsWith("pair")) {
    const qs = new URLSearchParams(window.location.search || (raw.includes("?") ? raw.split("?")[1] : ""));
    handleBrowserPairRoute(qs.get("code") || "");
    return;
  }

  // Clean path routes for Shared Moment (/m/:id), Shared Playlist (/p/:id), & About (/about)
  if (!raw) {
    if (path === "/about" || path === "/about/" || BOOT.initialRoute === "about") {
      navigateTo("about", false);
      return;
    }
    const mPath = /^\/m\/([^/?#]+)\/?$/.exec(path);
    if (mPath) {
      openSharedMoment(decodeURIComponent(mPath[1]), { updateHistory: false });
      return;
    }
    const pPath = /^\/p\/([^/?#]+)\/?$/.exec(path);
    if (pPath) {
      openSharedPlaylist(decodeURIComponent(pPath[1]), { updateHistory: false });
      return;
    }
    if (BOOT.initialRoute) {
      if (BOOT.initialRoute.startsWith("m/")) {
        openSharedMoment(BOOT.initialRoute.slice(2), { updateHistory: false });
        return;
      }
      if (BOOT.initialRoute.startsWith("p/")) {
        openSharedPlaylist(BOOT.initialRoute.slice(2), { updateHistory: false });
        return;
      }
    }
  }

  if (raw.startsWith("m/")) {
    openSharedMoment(decodeURIComponent(raw.slice(2).split("?")[0]), { updateHistory: false });
    return;
  }
  if (raw.startsWith("p/")) {
    openSharedPlaylist(decodeURIComponent(raw.slice(2).split("?")[0]), { updateHistory: false });
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
  if (raw === "about") {
    navigateTo("about", false);
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
    openReport(rid, "insights", { updateHistory: false });
    return;
  }
  if (raw.startsWith("replay/")) {
    const [pathPart, queryPart] = raw.split("?");
    const qid = decodeURIComponent(pathPart.slice("replay/".length));
    const qs = new URLSearchParams(queryPart || "");
    const r = qs.get("r") ? Number(qs.get("r")) : null;
    const gf = qs.get("gf") ? Number(qs.get("gf")) : null;
    const p = qs.get("p") ? Number(qs.get("p")) : null;
    openMatch(qid, p, (r || gf) ? { round_id: r, anchor_gf: gf, autoPlay: false } : null, { updateHistory: false });
    return;
  }
  navigateTo("home", false);
}

function handlePopState(ev) {
  const st = (ev && ev.state) || {};
  if (state._suppressNextPopstate) {
    state._suppressNextPopstate = false;
    if (typeof st.depth === "number") {
      state._historyDepth = st.depth;
    }
    state._lastRoutedUrl = _currentAppUrl();
    return;
  }

  if (typeof st.depth === "number") {
    state._historyDepth = st.depth;
  } else {
    state._historyDepth = Math.max(0, Number(state._historyDepth || 0) - 1);
  }

  // Close open Moment Viewer (#watchModal) on Back swipe without leaving underlying view
  if (state.watchPlaylistId && st.fcModal !== "watch") {
    state._activeModalHistory = null;
    if (typeof closeWatchMode === "function") closeWatchMode({ fromPopstate: true });
    state._lastRoutedUrl = _currentAppUrl();
    return;
  }

  // Close open mobile hamburger drawer on Back swipe without leaving underlying view
  if (isMobileNavMenuOpen() && st.fcModal !== "mobile_menu") {
    state._activeModalHistory = null;
    closeMobileNavMenu({ fromPopstate: true });
    state._lastRoutedUrl = _currentAppUrl();
    return;
  }

  const curUrl = _currentAppUrl();
  if (curUrl === state._lastRoutedUrl) return;
  state._routingFromPopstate = true;
  try {
    routeFromHash(true);
  } finally {
    state._routingFromPopstate = false;
    state._lastRoutedUrl = _currentAppUrl();
  }
}

function handleHashChange() {
  const curUrl = _currentAppUrl();
  if (curUrl === state._lastRoutedUrl) return;
  state._lastRoutedUrl = curUrl;
  routeFromHash(false);
}

// ---------- LIVE JOB & WATCHER POLLING ----------
let _hadActiveJobs = false;
const _notifiedPlayableJobs = new Set();
const _lastJobPlayableCount = {};
let _activePlayableJobQid = null;

function watchActiveStreamingJob() {
  if (!_activePlayableJobQid) return;
  if (state.activeView !== "studio") state.returnView = state.activeView || "matches";
  openMatch(_activePlayableJobQid);
}

async function pollJobs() {
  if (isHostedMode()) return;
  try {
    const data = await api("/api/jobs");
    state.watcher = data.watcher || state.watcher;
    state.jobs = data.jobs || [];
    updateStatusPillUi();
    renderStatusPopover();

    const active = state.jobs.filter(j => ["queued", "running", "paused"].includes(j.state));
    const activeQids = new Set(active.map(j => j.quark_id));
    for (const qid of Array.from(_notifiedPlayableJobs)) {
      if (!activeQids.has(qid)) {
        _notifiedPlayableJobs.delete(qid);
        delete _lastJobPlayableCount[qid];
      }
    }

    const banner = document.getElementById("jobBanner");
    const watchBtn = document.getElementById("jobBannerWatchBtn");
    if (active.length) {
      _hadActiveJobs = true;
      banner.classList.add("show");
      const j = active[0];
      const shortId = String(j.quark_id || "").split("-").pop();
      const label = j.opp_name ? `vs ${j.opp_name}` : shortId;
      const playable = Number(j.playable_rounds || 0);
      const inThisStudio = state.activeView === "studio" && state.activeManifest && state.activeManifest.quark_id === j.quark_id;

      document.getElementById("jobBannerText").textContent = `${label}: ${j.message}`;
      document.getElementById("jobBannerPct").textContent = j.progress_total > 0
        ? `${j.progress_done}/${j.progress_total}`
        : (playable > 0 ? `${playable}R ready` : (j.progress_done > 0 ? `R${j.progress_done}` : `${active.length} active`));

      if (watchBtn) {
        if (playable > 0 && !inThisStudio) {
          _activePlayableJobQid = j.quark_id;
          watchBtn.textContent = `▶ Open in Studio (1–${playable}R)`;
          watchBtn.classList.remove("hidden");
        } else {
          _activePlayableJobQid = null;
          watchBtn.classList.add("hidden");
        }
      }

      // Notify once with an actionable emerald toast the moment Round 1 becomes playable
      let playableChanged = false;
      for (const aj of active) {
        const pCnt = Number(aj.playable_rounds || 0);
        const prevCnt = Number(_lastJobPlayableCount[aj.quark_id] || 0);
        if (pCnt > prevCnt) {
          _lastJobPlayableCount[aj.quark_id] = pCnt;
          playableChanged = true;
        }
        const watchingThis = state.activeView === "studio" && state.activeManifest && state.activeManifest.quark_id === aj.quark_id;
        if (pCnt >= 1 && !_notifiedPlayableJobs.has(aj.quark_id) && !watchingThis) {
          _notifiedPlayableJobs.add(aj.quark_id);
          const who = aj.opp_name ? `vs ${aj.opp_name}` : String(aj.quark_id || "").split("-").pop();
          const targetQid = aj.quark_id;
          toast(`● ${who} — Round 1 ready while remaining rounds stream`, {
            variant: "emerald",
            actionLabel: "▶ Open in Studio",
            durationMs: 10000,
            onAction: () => {
              if (state.activeView !== "studio") state.returnView = state.activeView || "matches";
              openMatch(targetQid);
            },
          });
        }
      }

      if (state.activeView === "matches") loadMatches();
      if (state.activeView === "home" && playableChanged) loadHomeWorkbench();
      if (state.activeView === "insights") renderInsights();
      clearTimeout(state.pollTimer);
      state.pollTimer = setTimeout(pollJobs, 2500);
    } else {
      _activePlayableJobQid = null;
      if (watchBtn) watchBtn.classList.add("hidden");
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
    if (typeof closeNotesTagFilterMenu === "function" && closeNotesTagFilterMenu()) {
      e.preventDefault();
      return;
    }
    if (typeof closeAllHubCardMenus === "function" && closeAllHubCardMenus()) {
      e.preventDefault();
      return;
    }
    if (typeof closeAllCollectionPickers === "function" && closeAllCollectionPickers()) {
      e.preventDefault();
      return;
    }
    if (typeof closeAllTagPickers === "function" && closeAllTagPickers()) {
      e.preventDefault();
      return;
    }
    if (isMobileNavMenuOpen()) {
      e.preventDefault();
      closeMobileNavMenu();
      return;
    }
    closeStatusPopover();
    if (typeof closeAccountMenu === "function") closeAccountMenu();
    if (state.watchPlaylistId) {
      if (state.watchEditingNote && typeof cancelWatchNoteEdit === "function") {
        e.preventDefault();
        cancelWatchNoteEdit();
        return;
      }
      closeWatchMode();
      return;
    }
    if (!document.getElementById("graduatedModal")?.classList.contains("hidden")) { closeGraduatedModal(); return; }
    if (!document.getElementById("playlistModal")?.classList.contains("hidden")) { closePlaylistModal(); return; }
    if (!document.getElementById("addToPlModal")?.classList.contains("hidden")) { closeAddToPlaylistModal(); return; }
    if (!document.getElementById("companionModal")?.classList.contains("hidden")) { closeCompanionModal(); return; }
    if (!document.getElementById("reportModal")?.classList.contains("hidden")) { closeReportModal(); return; }
    if (!document.getElementById("termsModal")?.classList.contains("hidden")) { closeTermsModal(); return; }
    if (!document.getElementById("shareBundleModal")?.classList.contains("hidden")) { closeShareBundleModal(); return; }
    if (!document.getElementById("byocModal")?.classList.contains("hidden")) { closeByocModal(); return; }
    if (state.activeView === "about" && typeof closeAllArchPopovers === "function" && closeAllArchPopovers()) {
      e.preventDefault();
      return;
    }
    if (state.activeView === "studio" && typeof dismissStudioNextOverlay === "function" && dismissStudioNextOverlay()) {
      e.preventDefault();
      return;
    }
    if (state.activeView === "studio" && state.composerOpen && typeof closeComposer === "function") {
      e.preventDefault();
      closeComposer(true);
      return;
    }
    if (state.activeView === "studio" && state.studioEditId && typeof cancelStudioInlineEdit === "function") {
      e.preventDefault();
      cancelStudioInlineEdit();
      return;
    }
  }

  if (inInput) return;

  // Watch Mode shortcuts
  if (state.watchPlaylistId) {
    if (e.code === "Space") { e.preventDefault(); toggleWatchPlay(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); prevWatchItem(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); nextWatchItem(); }
    else if (e.key === "m" || e.key === "M") { e.preventDefault(); toggleVideoMute(); }
    return;
  }

  // Shared Moment / Playlist shortcuts
  if (state.activeView === "shared_moment" || state.activeView === "shared_playlist") {
    if (e.code === "Space") { e.preventDefault(); toggleSharedPlay(); }
    else if (e.key === ",") { e.preventDefault(); stepSharedFrames(-5); }
    else if (e.key === ".") { e.preventDefault(); stepSharedFrames(5); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); stepSharedFrames(-20); }
    else if (e.key === "ArrowRight") { e.preventDefault(); stepSharedFrames(20); }
    else if (e.key === "m" || e.key === "M") { e.preventDefault(); toggleVideoMute(); }
    return;
  }

  // Replay Studio shortcuts
  if (state.activeView === "studio") {
    if (e.code === "Space") { e.preventDefault(); togglePlay(); }
    else if (e.key === ",") { e.preventDefault(); stepFrames(-5); }
    else if (e.key === ".") { e.preventDefault(); stepFrames(5); }
    else if (e.shiftKey && e.key === "ArrowLeft") { e.preventDefault(); stepStudioRound(-1); }
    else if (e.shiftKey && e.key === "ArrowRight") { e.preventDefault(); stepStudioRound(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); stepFrames(-20); }
    else if (e.key === "ArrowRight") { e.preventDefault(); stepFrames(20); }
    else if (e.key === "[") { e.preventDefault(); markRangeBound("start"); }
    else if (e.key === "]") { e.preventDefault(); markRangeBound("end"); }
    else if (e.key === "m" || e.key === "M" || e.key === "n" || e.key === "N") { e.preventDefault(); openComposer(); }
  }
});

// ---------- MOBILE TOPBAR HAMBURGER DRAWER ----------
let _mobileNavMenuOpen = false;

function isMobileNavMenuOpen() {
  return _mobileNavMenuOpen;
}

function _syncMobileNavMenuDom() {
  const drawer = document.getElementById("topbarMenuDrawer");
  const backdrop = document.getElementById("mobileMenuBackdrop");
  const btn = document.getElementById("btnMobileMenu");
  if (drawer) drawer.classList.toggle("open", _mobileNavMenuOpen);
  if (backdrop) backdrop.classList.toggle("hidden", !_mobileNavMenuOpen);
  if (btn) {
    btn.classList.toggle("open", _mobileNavMenuOpen);
    btn.setAttribute("aria-expanded", _mobileNavMenuOpen ? "true" : "false");
    btn.setAttribute("aria-label", _mobileNavMenuOpen ? "Close navigation menu" : "Open navigation menu");
    const icon = btn.querySelector(".hamburger-icon");
    if (icon) icon.textContent = _mobileNavMenuOpen ? "✕" : "☰";
  }
}

function closeMobileNavMenu({ fromPopstate = false, consumeSentinel = false } = {}) {
  if (!_mobileNavMenuOpen) return;
  _mobileNavMenuOpen = false;
  _syncMobileNavMenuDom();
  if (fromPopstate) {
    if (state._activeModalHistory === "mobile_menu") state._activeModalHistory = null;
  } else if (!consumeSentinel) {
    popModalHistory("mobile_menu");
  }
}

function toggleMobileNavMenu() {
  const nextOpen = !_mobileNavMenuOpen;
  if (nextOpen) {
    _mobileNavMenuOpen = true;
    if (typeof closeStatusPopover === "function") closeStatusPopover();
    if (typeof closeAccountMenu === "function") closeAccountMenu();
    setTopbarHidden(false);
    _syncMobileNavMenuDom();
    pushModalHistory("mobile_menu");
  } else {
    closeMobileNavMenu();
  }
}

if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
  const _desktopNavMq = window.matchMedia("(min-width: 900px)");
  const _onDesktopNavChange = ev => {
    if (ev.matches) closeMobileNavMenu();
  };
  if (typeof _desktopNavMq.addEventListener === "function") {
    _desktopNavMq.addEventListener("change", _onDesktopNavChange);
  } else if (typeof _desktopNavMq.addListener === "function") {
    _desktopNavMq.addListener(_onDesktopNavChange);
  }
}

// ---------- MOBILE TOPBAR AUTO-HIDE ON SCROLL ----------
let _lastScrollY = 0;
let _scrollDir = 0;
let _scrollAnchorY = 0;
let _lastMaxScroll = 0;
let _lastViewportW = 0;
let _suppressRevealUntil = 0;
let _topbarHidden = false;

function isCollapsibleTopbarViewport() {
  if (window.innerWidth < 900) return true;
  return Boolean(window.matchMedia && window.matchMedia("(pointer: coarse) and (max-width: 1024px)").matches);
}

function setTopbarHidden(hidden) {
  const bar = document.getElementById("appTopbar") || document.querySelector(".topbar");
  const shouldHide = Boolean(hidden) && isCollapsibleTopbarViewport() && !isMobileNavMenuOpen();
  _topbarHidden = shouldHide;
  if (bar) bar.classList.toggle("topbar-hidden", shouldHide);
}

function initMobileTopbarAutoHide() {
  const getScrollY = () => Math.max(0, window.scrollY || document.documentElement.scrollTop || 0);
  const getMaxScroll = () => Math.max(0, (document.documentElement.scrollHeight || 0) - (window.innerHeight || 0));

  _lastScrollY = getScrollY();
  _scrollAnchorY = _lastScrollY;
  _lastMaxScroll = getMaxScroll();
  _lastViewportW = window.innerWidth || 0;
  _scrollDir = 0;

  const syncAfterVerticalResize = () => {
    const curW = window.innerWidth || 0;
    if (!isCollapsibleTopbarViewport()) {
      if (_topbarHidden) setTopbarHidden(false);
      _lastViewportW = curW;
      return;
    }
    // Mobile browser address-bar collapse/expand changes viewport height (not width)
    // and can shift scrollY backwards; suppress accidental navbar reveal while it settles.
    if (curW === _lastViewportW) {
      _suppressRevealUntil = performance.now() + 450;
      const y = getScrollY();
      _lastScrollY = y;
      _scrollAnchorY = y;
      _lastMaxScroll = getMaxScroll();
      _scrollDir = 0;
    } else {
      _lastViewportW = curW;
    }
  };

  window.addEventListener("touchstart", () => {
    const y = getScrollY();
    _lastScrollY = y;
    _scrollAnchorY = y;
    _scrollDir = 0;
  }, { passive: true });

  window.addEventListener("scroll", () => {
    const y = getScrollY();
    const maxScroll = getMaxScroll();
    if (!isCollapsibleTopbarViewport()) {
      if (_topbarHidden) setTopbarHidden(false);
      _lastScrollY = y;
      _scrollAnchorY = y;
      _lastMaxScroll = maxScroll;
      _scrollDir = 0;
      return;
    }
    if ((typeof isStatusPopoverOpen === "function" && isStatusPopoverOpen()) || isMobileNavMenuOpen()) {
      if (_topbarHidden) setTopbarHidden(false);
      _lastScrollY = y;
      _scrollAnchorY = y;
      _lastMaxScroll = maxScroll;
      _scrollDir = 0;
      return;
    }

    const delta = y - _lastScrollY;
    if (delta === 0) {
      _lastMaxScroll = maxScroll;
      return;
    }

    if (y <= 2) {
      if (_topbarHidden) setTopbarHidden(false);
      _lastScrollY = y;
      _scrollAnchorY = y;
      _lastMaxScroll = maxScroll;
      _scrollDir = 0;
      return;
    }

    // Ignore negative scroll shifts caused by mobile URL-bar collapse, layout shrink, or bottom rubber-band bounce
    if (delta < 0) {
      const maxShrank = maxScroll < _lastMaxScroll - 1;
      const atBottomBounce = maxScroll > 0 && y >= maxScroll - 4;
      if (maxShrank || atBottomBounce || performance.now() < _suppressRevealUntil) {
        _lastScrollY = y;
        _scrollAnchorY = y;
        _lastMaxScroll = maxScroll;
        _scrollDir = 0;
        return;
      }
    }

    const dir = delta > 0 ? 1 : -1;
    if (dir !== _scrollDir) {
      _scrollDir = dir;
      _scrollAnchorY = _lastScrollY;
    }
    const travel = y - _scrollAnchorY;

    if (dir > 0 && travel >= 4) {
      if (!_topbarHidden) setTopbarHidden(true);
    } else if (dir < 0 && travel <= -64) {
      if (_topbarHidden) setTopbarHidden(false);
    }
    _lastScrollY = y;
    _lastMaxScroll = maxScroll;
  }, { passive: true });

  window.addEventListener("resize", syncAfterVerticalResize, { passive: true });
  if (window.visualViewport && typeof window.visualViewport.addEventListener === "function") {
    window.visualViewport.addEventListener("resize", syncAfterVerticalResize, { passive: true });
  }
}

// ---------- DESKTOP FLOATING TOOLTIPS ----------
function initDesktopTooltips() {
  const tipEl = document.getElementById("desktopTooltip");
  if (!tipEl) return;

  let activeTarget = null;
  let showTimer = null;

  const canShowTips = () => {
    const fineHover = Boolean(window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches);
    return fineHover && !isMobileMode();
  };

  const hideTip = () => {
    clearTimeout(showTimer);
    showTimer = null;
    activeTarget = null;
    tipEl.classList.remove("show");
    tipEl.setAttribute("aria-hidden", "true");
  };

  const placeAndShow = (target) => {
    if (!target || !document.body.contains(target) || !canShowTips()) return hideTip();
    const txt = (target.getAttribute("data-tip") || "").trim();
    if (!txt) return hideTip();
    tipEl.textContent = txt;
    tipEl.style.left = "-9999px";
    tipEl.style.top = "-9999px";
    tipEl.classList.add("show");
    tipEl.setAttribute("aria-hidden", "false");

    const rect = target.getBoundingClientRect();
    const tipRect = tipEl.getBoundingClientRect();
    const vw = window.innerWidth || document.documentElement.clientWidth || 1024;
    const vh = window.innerHeight || document.documentElement.clientHeight || 768;
    const margin = 8;

    let left = rect.left + (rect.width - tipRect.width) / 2;
    left = Math.max(margin, Math.min(vw - tipRect.width - margin, left));

    let top = rect.top - tipRect.height - 8;
    if (top < margin) {
      top = Math.min(vh - tipRect.height - margin, rect.bottom + 8);
    }

    tipEl.style.left = `${Math.round(left)}px`;
    tipEl.style.top = `${Math.round(top)}px`;
  };

  document.addEventListener("pointerover", e => {
    if (!canShowTips() || e.pointerType === "touch") return;
    const target = e.target && e.target.closest ? e.target.closest("[data-tip]") : null;
    if (target === activeTarget) return;
    hideTip();
    if (!target) return;
    activeTarget = target;
    showTimer = setTimeout(() => placeAndShow(target), 180);
  });

  document.addEventListener("pointerout", e => {
    if (!activeTarget) return;
    const toEl = e.relatedTarget;
    if (toEl && activeTarget.contains(toEl)) return;
    hideTip();
  });

  document.addEventListener("focusin", e => {
    if (!canShowTips()) return;
    const target = e.target && e.target.closest ? e.target.closest("[data-tip]") : null;
    if (!target) return;
    hideTip();
    activeTarget = target;
    placeAndShow(target);
  });

  document.addEventListener("focusout", hideTip);
  document.addEventListener("pointerdown", hideTip, { passive: true });
  window.addEventListener("scroll", hideTip, { passive: true, capture: true });
  window.addEventListener("resize", hideTip, { passive: true });
}

// ---------- AUTO-REFRESH ON TAB FOCUS FOR NEW TAKEOVER RESPONSES ----------
let _lastKnownResponseAt = null;
let _responseStatusInitialized = false;
let _checkingResponseStatus = false;

async function checkForNewTakeoverResponses(silentInit = false) {
  if (isHostedMode() || _checkingResponseStatus) return;
  _checkingResponseStatus = true;
  try {
    const st = await api("/api/status");
    const ts = st && st.last_response_at ? String(st.last_response_at) : null;
    if (!_responseStatusInitialized) {
      _lastKnownResponseAt = ts;
      _responseStatusInitialized = true;
      return;
    }
    if (ts && ts !== _lastKnownResponseAt) {
      _lastKnownResponseAt = ts;
      if (!silentInit) {
        if (state.activeView === "studio" && typeof refreshAnnotationsQuiet === "function") {
          await refreshAnnotationsQuiet();
        }
        if (typeof loadHubNotes === "function") {
          loadHubNotes(true);
        }
        toast("✅ Labbed Response ready!", { variant: "emerald", durationMs: 4200 });
      }
    }
  } catch (_) {
  } finally {
    _checkingResponseStatus = false;
  }
}

window.addEventListener("focus", () => {
  if (!isHostedMode()) {
    if (state.shareModal && !state.capabilities.has_cloud_token && typeof refreshShareModalConnectionStatus === "function") {
      refreshShareModalConnectionStatus(true);
    } else if (state.byocModal && !state.capabilities.has_cloud_token) {
      loadCapabilities().then(() => { if (state.byocModal) renderByocModal(); }).catch(() => {});
    } else if (typeof isStatusPopoverOpen === "function" && isStatusPopoverOpen()) {
      loadCapabilities().then(() => { if (isStatusPopoverOpen()) renderStatusPopover(); }).catch(() => {});
    }
    if (state.activeView === "studio" || state.activeView === "notes" || state.activeView === "home") {
      checkForNewTakeoverResponses(false);
    }
  }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && !isHostedMode()) {
    if (state.shareModal && !state.capabilities.has_cloud_token && typeof refreshShareModalConnectionStatus === "function") {
      refreshShareModalConnectionStatus(true);
    }
    if (state.activeView === "studio" || state.activeView === "notes" || state.activeView === "home") {
      checkForNewTakeoverResponses(false);
    }
  }
});

// ---------- BOOT ----------
async function initApp() {
  syncThemeUi();
  initMobileTopbarAutoHide();
  initDesktopTooltips();
  const userInp = document.getElementById("usernameInput");
  if (userInp) {
    userInp.value = state.username;
    userInp.addEventListener("keydown", e => { if (e.key === "Enter") loadMatches(); });
  }
  await loadCapabilities();
  await loadAuthState();
  if (isHostedMode() && state.auth.authenticated) {
    probeLocalCompanion();
  }
  if (!isHostedMode() || state.auth.authenticated) {
    loadFocusGoals();
    loadPlaylists(false);
    loadHubNotes(true);
  }
  const initUrl = _currentAppUrl();
  state._historyDepth = 0;
  state._lastRoutedUrl = initUrl;
  if (typeof history !== "undefined" && typeof history.replaceState === "function") {
    history.replaceState({ fcApp: true, depth: 0 }, "", initUrl);
  }
  routeFromHash(false);
  if (!isHostedMode()) {
    checkForNewTakeoverResponses(true);
    pollJobs();
    setInterval(pollJobs, 10000);
  }
}

window.addEventListener("popstate", handlePopState);
window.addEventListener("hashchange", handleHashChange);
initApp();
