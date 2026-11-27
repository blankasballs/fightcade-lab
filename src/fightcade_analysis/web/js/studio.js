/* 2-Column Desktop Replay Studio (#replay/<qid>): Stage, Overlays, Timeline, Moments, Set Notes & Top-Rank Search */

const video = document.getElementById("roundVideo");
const canvas = document.getElementById("timelineCanvas");
const ctx = canvas ? canvas.getContext("2d") : null;
const ovCanvas = document.getElementById("overlayCanvas");

function curRound() {
  if (!state.activeManifest || !state.activeManifest.rounds.length) return null;
  return state.activeManifest.rounds[state.activeRoundIdx] || state.activeManifest.rounds[0];
}

function roundGfSpan(r) {
  if (!r) return 1;
  if (r.start_gf != null && r.end_gf != null && r.end_gf > r.start_gf) {
    return Math.max(1, r.end_gf - r.start_gf);
  }
  return Math.max(1, (r.frame_count || 1) - 1);
}

function currentGf() {
  const r = curRound();
  if (!r) return 0;
  const maxVidOff = Math.max(1, (r.frame_count || 1) - 1);
  const gfSpan = roundGfSpan(r);
  const rawOff = Math.max(0, Math.min(maxVidOff, (video.currentTime || 0) * (r.fps || 60)));
  const off = (gfSpan === maxVidOff)
    ? Math.round(rawOff)
    : Math.round((rawOff / maxVidOff) * gfSpan);
  return r.start_gf + off;
}

function seekToGf(gf, autoPlay = false) {
  dismissStudioNextOverlay();
  const r = curRound();
  if (!r) return;
  const fps = r.fps || 60;
  const maxVidOff = Math.max(1, (r.frame_count || 1) - 1);
  const gfSpan = roundGfSpan(r);
  const gfOff = Math.max(0, Math.min(gfSpan, gf - r.start_gf));
  const vidOff = (gfSpan === maxVidOff)
    ? gfOff
    : (gfOff / gfSpan) * maxVidOff;
  video.currentTime = vidOff / fps;
  state.currentFrameOffset = gfOff;
  updateHudAndTimeline();
  if (autoPlay) {
    video.play().catch(() => {});
  }
}

let _studioLivePollTimer = null;

function stopStudioLivePoll() {
  if (_studioLivePollTimer) {
    clearTimeout(_studioLivePollTimer);
    _studioLivePollTimer = null;
  }
}

function startStudioLivePoll(qid) {
  stopStudioLivePoll();
  if (!qid || !state.activeManifest || !state.activeManifest.in_progress) return;
  _studioLivePollTimer = setTimeout(() => pollStudioLiveManifest(qid), 3000);
}

async function pollStudioLiveManifest(qid) {
  _studioLivePollTimer = null;
  if (state.activeView !== "studio" || !state.activeManifest || state.activeManifest.quark_id !== qid) {
    return;
  }
  try {
    const params = new URLSearchParams();
    if (state.username) params.set("username", state.username);
    if (state.activeManifest.perspective === 1 || state.activeManifest.perspective === 2) {
      params.set("perspective", String(state.activeManifest.perspective));
    }
    const qs = params.toString() ? `?${params.toString()}` : "";
    const data = await api(`/api/replays/${encodeURIComponent(qid)}/manifest${qs}`);
    if (state.activeView !== "studio" || !state.activeManifest || state.activeManifest.quark_id !== qid) {
      return;
    }
    const prevMf = state.activeManifest;
    const nextMf = data.manifest;
    const prevCount = (prevMf.rounds || []).length;
    const nextCount = (nextMf && nextMf.rounds ? nextMf.rounds.length : 0);
    const curR = curRound();
    const curRid = curR ? curR.round_id : null;

    if (nextMf && (nextCount !== prevCount || Boolean(nextMf.in_progress) !== Boolean(prevMf.in_progress))) {
      state.activeManifest = nextMf;
      state.annotations = data.annotations || state.annotations;
      state.selectMarks = data.select_marks || state.selectMarks;
      if (curRid != null) {
        const preservedIdx = (nextMf.rounds || []).findIndex(r => r.round_id === curRid);
        if (preservedIdx >= 0) state.activeRoundIdx = preservedIdx;
      }
      applyStudioHeader(nextMf);
      renderMatchAndRoundTabs();
      renderStudioSetMetaCard();
      if (!nextMf.in_progress) {
        toast(`All ${nextCount} rounds ready!`);
      } else if (nextCount > prevCount) {
        toast(`Round ${nextCount} ready!`);
      }
    }
    if (state.activeManifest && state.activeManifest.in_progress) {
      _studioLivePollTimer = setTimeout(() => pollStudioLiveManifest(qid), 3000);
    }
  } catch (_) {
    if (state.activeView === "studio" && state.activeManifest && state.activeManifest.quark_id === qid && state.activeManifest.in_progress) {
      _studioLivePollTimer = setTimeout(() => pollStudioLiveManifest(qid), 4000);
    }
  }
}

async function openMatch(qid, explicitPersp = null, jumpTarget = null, opts = {}) {
  const shouldUpdateHistory = opts.updateHistory !== false && !state._routingFromPopstate;
  stopStudioLivePoll();
  stopAllStudioCardPreviews();
  dismissStudioNextOverlay();
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  if (typeof closeWatchMode === "function" && state.watchPlaylistId) {
    closeWatchMode({ consumeSentinel: shouldUpdateHistory });
  }

  try {
    const params = new URLSearchParams();
    if (state.username) params.set("username", state.username);
    if (explicitPersp === 1 || explicitPersp === 2) params.set("perspective", String(explicitPersp));
    const qs = params.toString() ? `?${params.toString()}` : "";
    const data = await api(`/api/replays/${encodeURIComponent(qid)}/manifest${qs}`);

    state.activeManifest = data.manifest;
    state.annotations = data.annotations || [];
    state.selectMarks = data.select_marks || [];
    state.setNoteObj = data.set_note || { note: "", tags: [] };
    state.activeRoundIdx = 0;
    state.proSearchResults = null;
    cancelStudioInlineEdit(false);
    closeComposer(true);

    state.activeView = "studio";
    const targetUrl = `/#replay/${encodeURIComponent(qid)}`;
    if (shouldUpdateHistory && typeof updateAppHistory === "function") {
      updateAppHistory(targetUrl);
    }
    syncTopNav();

    applyStudioHeader(state.activeManifest);
    document.getElementById("setNoteInput").value = (state.setNoteObj && state.setNoteObj.note) || "";
    initTagPicker("setNote", (state.setNoteObj && state.setNoteObj.tags) || [], { buttonLabel: "+ Add tags" });
    const setTagHost = document.getElementById("setNoteTagPicker");
    if (setTagHost) setTagHost.innerHTML = renderTagPickerHtml("setNote", null, { buttonLabel: "+ Add tags" });

    let startIdx = 0;
    if (jumpTarget && jumpTarget.round_id) {
      const foundIdx = (state.activeManifest.rounds || []).findIndex(r => r.round_id === Number(jumpTarget.round_id));
      if (foundIdx >= 0) startIdx = foundIdx;
    }
    state.activeMatchId = (state.activeManifest.rounds[startIdx] && state.activeManifest.rounds[startIdx].match_id) || 1;

    renderComposerCollectionPicker();
    renderComposerControls();
    renderStudioSetMetaCard();
    syncProCorpusGateUi();
    setStudioSidebarTab("moments");
    renderMatchAndRoundTabs();
    loadRound(startIdx);
    if (state.activeManifest && state.activeManifest.in_progress) {
      startStudioLivePoll(qid);
    }

    if (jumpTarget && (jumpTarget.start_gf || jumpTarget.anchor_gf)) {
      const targetGf = jumpTarget.autoPlay
        ? (jumpTarget.start_gf || jumpTarget.anchor_gf)
        : (jumpTarget.anchor_gf || jumpTarget.start_gf);
      const stopGf = (jumpTarget.autoPlay && jumpTarget.end_gf && jumpTarget.end_gf > targetGf) ? jumpTarget.end_gf : null;
      const applyJump = () => {
        state.playStopGf = stopGf;
        seekToGf(Number(targetGf), Boolean(jumpTarget.autoPlay));
      };
      if (video.readyState >= 1) applyJump();
      else video.addEventListener("loadedmetadata", applyJump, { once: true });
    }
    window.scrollTo({ top: 0, behavior: "instant" });
  } catch (err) {
    if (state._activeModalHistory === "watch" && typeof popModalHistory === "function") {
      popModalHistory("watch");
    }
    toast("Could not open replay: " + err.message);
  }
}

function applyStudioHeader(mf) {
  const matchupHtml = formatMatchLabelHtml(mf);
  const studioHdr = document.getElementById("studioMatchupHeader");
  if (studioHdr) {
    studioHdr.innerHTML = matchupHtml;
  }
  const crumb = document.getElementById("crumbMatchup");
  if (crumb) {
    crumb.innerHTML = "";
  }
  const flipBtn = document.getElementById("btnFlipSide");
  if (flipBtn) flipBtn.textContent = `⇄ Flip P${mf.perspective === 1 ? 2 : 1}`;
  updateTimelineLegend(mf, curRound());
  if (typeof updateDocumentTitle === "function") updateDocumentTitle(mf);
}

function isTimelineFilterOn(key) {
  return !state.timelineFilters || state.timelineFilters[key] !== false;
}

function toggleTimelineFilter(key) {
  if (!state.timelineFilters) state.timelineFilters = {};
  state.timelineFilters[key] = !isTimelineFilterOn(key);
  try {
    localStorage.setItem("fc_timeline_filters", JSON.stringify(state.timelineFilters));
  } catch (_) {}
  syncTimelineLegendState();
  renderEventChips();
  updateHudAndTimeline();
}

function syncTimelineLegendState() {
  const el = document.getElementById("timelineLegend");
  if (!el) return;
  el.querySelectorAll(".lg-item[data-tl]").forEach(btn => {
    const on = isTimelineFilterOn(btn.dataset.tl);
    btn.classList.toggle("on", on);
    btn.classList.toggle("off", !on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  });
}

function updateTimelineLegend(mf, r) {
  const el = document.getElementById("timelineLegend");
  if (!el || !mf) return;
  const selfLbl = (r && r.self_char_name)
    ? `${mf.self_player_name} (${r.self_char_name})`
    : (mf.self_player_name || "You");
  const oppLbl = (r && r.opp_char_name)
    ? `${mf.opp_player_name} (${r.opp_char_name})`
    : (mf.opp_player_name || "Opp");
  const items = [
    { key: "self_hp", swatch: `<span class="lg-line" style="background:#38bdf8;"></span>`, label: `${selfLbl} HP`, tip: `Toggle ${selfLbl} HP line` },
    { key: "opp_hp", swatch: `<span class="lg-line" style="background:#fb7185;"></span>`, label: `${oppLbl} HP`, tip: `Toggle ${oppLbl} HP line` },
    { key: "dmg_self", swatch: `<span class="lg-tick" style="background:#f97316;"></span>`, label: "Took Dmg", tip: "Toggle Took Dmg markers & event pills" },
    { key: "dmg_opp", swatch: `<span class="lg-tick" style="background:#34d399;"></span>`, label: "Dealt Dmg", tip: "Toggle Dealt Dmg markers & event pills" },
    { key: "super", swatch: `<span class="lg-tick" style="background:#fbbf24;"></span>`, label: "Super", tip: "Toggle Super markers & event pills" },
    { key: "notes", swatch: `<span class="lg-dot" style="background:#c084fc;"></span>`, label: "Notes", tip: "Toggle saved note pins on timeline" },
  ];
  el.innerHTML = items.map(it => {
    const on = isTimelineFilterOn(it.key);
    return `<button type="button" class="lg-item ${on ? "on" : "off"}" data-tl="${esc(it.key)}" aria-pressed="${on ? "true" : "false"}" data-tip="${esc(it.tip)}" onclick="toggleTimelineFilter(${escJsAttr(it.key)})">${it.swatch}${esc(it.label)}</button>`;
  }).join("");
}

async function flipViewerPerspective() {
  if (!state.activeManifest) return;
  const nextPersp = state.activeManifest.perspective === 1 ? 2 : 1;
  const keepRoundIdx = state.activeRoundIdx;
  const keepTime = video.currentTime || 0;
  const wasPaused = video.paused;
  try {
    const data = await api(`/api/replays/${encodeURIComponent(state.activeManifest.quark_id)}/manifest?perspective=${nextPersp}`);
    state.activeManifest = data.manifest;
    state.annotations = data.annotations || [];
    state.selectMarks = data.select_marks || [];
    state.activeRoundIdx = Math.min(keepRoundIdx, Math.max(0, state.activeManifest.rounds.length - 1));
    applyStudioHeader(state.activeManifest);
    renderMatchAndRoundTabs();
    renderEventChips();
    renderNotesList();
    renderStudioSetMetaCard();
    video.currentTime = keepTime;
    updateHudAndTimeline();
    if (!wasPaused) video.play().catch(() => {});
    toast(`Viewing as P${nextPersp} (${state.activeManifest.self_player_name})`);
  } catch (err) {
    toast("Flip failed: " + err.message);
  }
}

function shareStudioMoment(id, source = "annotation") {
  const mf = state.activeManifest;
  const isSelect = source === "select_mark";
  const item = isSelect
    ? (state.selectMarks || []).find(x => x.id === id)
    : (state.annotations || []).find(x => x.id === id);
  if (!item || !mf) return;
  const isResp = Boolean(source === "takeover_response" || item.source === "takeover_response" || item.response_clip_url);
  const aRound = roundById(item.round_id) || curRound();
  const rStart = Number((aRound && aRound.start_gf) || 0);
  const rEnd = aRound ? (rStart + roundGfSpan(aRound)) : 0;
  const anchorGf = Number(item.anchor_gf || item.press_gf || item.start_gf || rStart);
  let startGf = Number(item.start_gf || 0);
  let endGf = Number(item.end_gf || 0);
  if (!isResp && aRound) {
    startGf = startGf > 0 ? Math.max(rStart, Math.min(rEnd - 1, startGf)) : Math.max(rStart, anchorGf - 120);
    endGf = endGf > startGf ? Math.min(rEnd, endGf) : Math.min(rEnd, startGf + 150);
  }
  const cleanNote = item.is_default_select_note ? "" : (item.note || "");
  const shareMeta = {
    self_player_name: item.self_player_name || mf.self_player_name || "",
    opp_player_name: item.opp_player_name || mf.opp_player_name || "",
    self_rank_letter: item.self_rank_letter || mf.self_rank_letter || "",
    opp_rank_letter: item.opp_rank_letter || mf.opp_rank_letter || "",
    self_char_name: item.self_char_name || (aRound && aRound.self_char_name) || mf.self_char_name || "",
    opp_char_name: item.opp_char_name || (aRound && aRound.opp_char_name) || mf.opp_char_name || "",
    quark_id: item.quark_id || mf.quark_id,
  };
  shareMomentBundle({
    ...item,
    ...shareMeta,
    source: isResp ? "takeover_response" : (isSelect ? "select_mark" : "annotation"),
    is_response_clip: isResp,
    round_id: Number(item.round_id || (aRound && aRound.round_id) || 0),
    perspective: Number(item.perspective || mf.perspective || 1),
    start_gf: startGf,
    end_gf: endGf,
    anchor_gf: anchorGf,
    note: cleanNote,
    round_label: item.round_label || (aRound ? formatRoundTag(aRound) : (item.round_id ? `R${item.round_id}` : "")),
    title: cleanNote || `${formatMatchLabelText(shareMeta, { showRank: false })} · R${item.round_id || (aRound && aRound.round_id) || 1}`,
  });
}

function formatRoundTag(r) {
  if (!r) return "";
  return r.match_id ? `M${r.match_id} · R${r.match_round || r.round_id}` : `R${r.round_id}`;
}

function roundById(roundId) {
  if (!state.activeManifest) return null;
  return (state.activeManifest.rounds || []).find(r => r.round_id === roundId) || null;
}

function selectMatch(matchId) {
  const mf = state.activeManifest;
  if (!mf) return;
  state.activeMatchId = matchId;
  const firstIdx = mf.rounds.findIndex(r => (r.match_id || 1) === matchId);
  if (firstIdx >= 0) loadRound(firstIdx);
  else renderMatchAndRoundTabs();
}

function renderMatchAndRoundTabs() {
  const mf = state.activeManifest;
  if (!mf) return;
  const matches = mf.matches || [];
  const cur = curRound();
  if (cur && cur.match_id) state.activeMatchId = cur.match_id;

  const matchHost = document.getElementById("matchTabs");
  if (matches.length) {
    matchHost.innerHTML = matches.map(m => {
      const wlCls = m.self_won === true ? "win" : (m.self_won === false ? "loss" : "");
      return `
        <button class="match-pill ${wlCls} ${m.match_id === state.activeMatchId ? "active" : ""}" data-tip="Switch to Match M${m.match_id} (${m.self_rounds_won}–${m.opp_rounds_won})" onclick="selectMatch(${m.match_id})">
          <span>M${m.match_id} ${m.self_rounds_won}–${m.opp_rounds_won}</span>
        </button>
      `;
    }).join("");
  } else {
    matchHost.innerHTML = `<span class="pill">Set</span>`;
  }

  const roundHost = document.getElementById("roundTabs");
  const visibleRounds = mf.rounds
    .map((r, idx) => ({ r, idx }))
    .filter(x => !matches.length || (x.r.match_id || 1) === state.activeMatchId);

  const streamBadge = mf.in_progress
    ? `<span class="pill busy" style="font-size:11px; margin-left:4px;" data-tip="New rounds appear automatically as the replay finishes streaming">◌ Streaming...</span>`
    : "";
  roundHost.innerHTML = visibleRounds.map(({ r, idx }) => {
    const won = r.self_won === true;
    const lost = r.self_won === false;
    const wl = won ? "W" : (lost ? "L" : "D");
    const wlLong = won ? "Win" : (lost ? "Loss" : "Draw");
    return `
      <button class="round-pill ${idx === state.activeRoundIdx ? "active" : ""}" data-tip="Load Round ${r.match_round || r.round_id} (${wlLong})" onclick="loadRound(${idx})">
        <span>R${r.match_round || r.round_id}</span>
        <span style="font-size:10.5px; color:${won ? "var(--emerald-fg)" : (lost ? "var(--rose-fg)" : "var(--amber-fg)")};">${wl}</span>
      </button>
    `;
  }).join("") + streamBadge;
}

const STUDIO_AUTOPLAY_NEXT_KEY = "fc_studio_autoplay_next";
const STUDIO_NEXT_COUNTDOWN_MS = 2000;
let _studioNextTimer = null;
let _studioNextDeadline = 0;
let _studioNextOverlayOpen = false;
let _studioPendingAutoPlay = false;

function isStudioAutoplayNextEnabled() {
  try {
    return localStorage.getItem(STUDIO_AUTOPLAY_NEXT_KEY) === "1";
  } catch (_) {
    return false;
  }
}

function setStudioAutoplayNextEnabled(enabled) {
  try {
    localStorage.setItem(STUDIO_AUTOPLAY_NEXT_KEY, enabled ? "1" : "0");
  } catch (_) {}
}

function isStudioNextOverlayOpen() {
  return _studioNextOverlayOpen;
}

function _isTypingElementFocused() {
  if (typeof document === "undefined") return false;
  const el = document.activeElement;
  if (!el) return false;
  const tag = String(el.tagName || "").toUpperCase();
  if (tag === "TEXTAREA") return true;
  if (tag === "INPUT") {
    const t = String(el.type || "text").toLowerCase();
    return !["checkbox", "radio", "button", "submit", "reset", "range", "color", "file", "image", "hidden"].includes(t);
  }
  if (el.isContentEditable) return true;
  if (typeof el.getAttribute === "function") {
    const ce = el.getAttribute("contenteditable");
    if (ce === "" || ce === "true" || ce === "plaintext-only") return true;
  }
  return false;
}

function _clearStudioNextCountdown() {
  if (_studioNextTimer) {
    clearInterval(_studioNextTimer);
    _studioNextTimer = null;
  }
  _studioNextDeadline = 0;
}

function dismissStudioNextOverlay() {
  _clearStudioNextCountdown();
  const wasOpen = _studioNextOverlayOpen;
  _studioNextOverlayOpen = false;
  const el = document.getElementById("studioNextRoundOverlay");
  if (el) {
    el.classList.add("hidden");
    el.innerHTML = "";
  }
  return wasOpen;
}

function _startStudioNextCountdown() {
  _clearStudioNextCountdown();
  const txtEl = document.getElementById("studioNextCountdownText");
  const trackEl = document.getElementById("studioNextProgTrack");
  const fillEl = document.getElementById("studioNextProgFill");

  if (_isTypingElementFocused()) {
    if (txtEl) {
      txtEl.textContent = "Auto-play paused while typing";
      txtEl.classList.remove("hidden");
    }
    if (trackEl) trackEl.classList.add("hidden");
    if (fillEl) fillEl.style.width = "0%";
    return;
  }

  _studioNextDeadline = Date.now() + STUDIO_NEXT_COUNTDOWN_MS;
  if (txtEl) {
    txtEl.textContent = `Playing in ${Math.ceil(STUDIO_NEXT_COUNTDOWN_MS / 1000)}s…`;
    txtEl.classList.remove("hidden");
  }
  if (trackEl) trackEl.classList.remove("hidden");
  if (fillEl) fillEl.style.width = "0%";

  _studioNextTimer = setInterval(() => {
    if (!_studioNextOverlayOpen || state.activeView !== "studio") {
      _clearStudioNextCountdown();
      return;
    }
    if (_isTypingElementFocused()) {
      _clearStudioNextCountdown();
      const t = document.getElementById("studioNextCountdownText");
      const tr = document.getElementById("studioNextProgTrack");
      if (t) {
        t.textContent = "Auto-play paused while typing";
        t.classList.remove("hidden");
      }
      if (tr) tr.classList.add("hidden");
      return;
    }
    const rem = Math.max(0, _studioNextDeadline - Date.now());
    const elapsedPct = Math.min(100, Math.max(0, ((STUDIO_NEXT_COUNTDOWN_MS - rem) / STUDIO_NEXT_COUNTDOWN_MS) * 100));
    const t = document.getElementById("studioNextCountdownText");
    const f = document.getElementById("studioNextProgFill");
    if (f) f.style.width = `${elapsedPct.toFixed(1)}%`;
    if (t) t.textContent = `Playing in ${Math.max(1, Math.ceil(rem / 1000))}s…`;
    if (rem <= 0) {
      _clearStudioNextCountdown();
      playNextStudioRound();
    }
  }, 50);
}

function toggleStudioAutoplayNext(checked) {
  const on = Boolean(checked);
  setStudioAutoplayNextEnabled(on);
  if (!_studioNextOverlayOpen) return;
  if (on) {
    _startStudioNextCountdown();
  } else {
    _clearStudioNextCountdown();
    const txtEl = document.getElementById("studioNextCountdownText");
    const trackEl = document.getElementById("studioNextProgTrack");
    const fillEl = document.getElementById("studioNextProgFill");
    if (txtEl) {
      txtEl.textContent = "";
      txtEl.classList.add("hidden");
    }
    if (trackEl) trackEl.classList.add("hidden");
    if (fillEl) fillEl.style.width = "0%";
  }
}

function playNextStudioRound() {
  const mf = state.activeManifest;
  if (!mf || !Array.isArray(mf.rounds)) {
    dismissStudioNextOverlay();
    return false;
  }
  const nextIdx = state.activeRoundIdx + 1;
  if (nextIdx >= mf.rounds.length) {
    dismissStudioNextOverlay();
    return false;
  }
  loadRound(nextIdx, { autoPlay: true });
  return true;
}

function replayCurrentStudioRound() {
  dismissStudioNextOverlay();
  if (!video) return;
  video.currentTime = 0;
  state.currentFrameOffset = 0;
  video.playbackRate = state.studioRate || 1;
  syncAudioControls();
  updateHudAndTimeline();
  video.play().catch(() => {
    video.muted = true;
    video.play().catch(() => {});
  });
}

function restartStudioSet() {
  loadRound(0, { autoPlay: true });
}

function stepStudioRound(delta, autoPlay = null) {
  const mf = state.activeManifest;
  if (!mf || !Array.isArray(mf.rounds) || !mf.rounds.length) return false;
  const nextIdx = state.activeRoundIdx + Number(delta || 0);
  if (nextIdx < 0 || nextIdx >= mf.rounds.length) return false;
  const shouldPlay = autoPlay == null
    ? Boolean((video && !video.paused) || _studioNextOverlayOpen)
    : Boolean(autoPlay);
  loadRound(nextIdx, { autoPlay: shouldPlay });
  return true;
}

function showStudioNextRoundOverlay() {
  if (state.activeView !== "studio") return;
  if (state.loopPreviewActive && hasActiveLoopRange()) return;
  if (_timelineDragMode) return;
  const mf = state.activeManifest;
  const rounds = (mf && Array.isArray(mf.rounds)) ? mf.rounds : [];
  if (!rounds.length) return;
  const host = document.getElementById("studioNextRoundOverlay");
  if (!host) return;

  _clearStudioNextCountdown();
  const nextIdx = state.activeRoundIdx + 1;
  if (nextIdx < rounds.length) {
    const curR = rounds[state.activeRoundIdx];
    const nextR = rounds[nextIdx];
    const hasMultiMatches = (Array.isArray(mf.matches) && mf.matches.length > 1)
      || new Set(rounds.map(r => r.match_id || 1)).size > 1;
    const mNum = nextR.match_id || 1;
    const mrNum = nextR.match_round || nextR.round_id || (nextIdx + 1);
    const titleText = hasMultiMatches ? `Match ${mNum} · Round ${mrNum}` : `Round ${mrNum}`;

    const curSelfC = String((curR && curR.self_char_name) || "").trim();
    const curOppC = String((curR && curR.opp_char_name) || "").trim();
    const nextSelfC = String(nextR.self_char_name || "").trim();
    const nextOppC = String(nextR.opp_char_name || "").trim();
    const switchedChar = Boolean(
      (nextSelfC && curSelfC && nextSelfC !== curSelfC) ||
      (nextOppC && curOppC && nextOppC !== curOppC)
    );
    const charSubHtml = switchedChar
      ? `<div class="studio-next-sub">${esc(nextSelfC || curSelfC || "?")} vs ${esc(nextOppC || curOppC || "?")}</div>`
      : "";

    const autoOn = isStudioAutoplayNextEnabled();
    host.innerHTML = `
      <div class="studio-next-card">
        <div class="studio-next-eyebrow">UP NEXT</div>
        <div class="studio-next-title">${esc(titleText)}</div>
        ${charSubHtml}
        <div id="studioNextCountdownText" class="studio-next-countdown${autoOn ? "" : " hidden"}"></div>
        <div id="studioNextProgTrack" class="studio-next-prog-track${autoOn ? "" : " hidden"}">
          <div id="studioNextProgFill" class="studio-next-prog-fill"></div>
        </div>
        <div class="studio-next-actions">
          <button type="button" class="sm emerald" onclick="playNextStudioRound()">▶ Play Next</button>
          <button type="button" class="sm studio-next-sec-btn" onclick="replayCurrentStudioRound()">↺ Replay</button>
          <button type="button" class="sm ghost studio-next-cancel-btn" onclick="dismissStudioNextOverlay()">Cancel</button>
        </div>
        <label class="studio-next-toggle">
          <input id="studioAutoplayNextCheck" type="checkbox" ${autoOn ? "checked" : ""} onchange="toggleStudioAutoplayNext(this.checked)" />
          <span>Auto-play next round</span>
        </label>
      </div>
    `;
    _studioNextOverlayOpen = true;
    host.classList.remove("hidden");
    if (autoOn) _startStudioNextCountdown();
    return;
  }

  if (!mf.in_progress) {
    const scorePart = (mf.self_score != null && mf.opp_score != null && (mf.self_score || mf.opp_score))
      ? ` · ${mf.self_score}–${mf.opp_score}`
      : "";
    const summaryTxt = `${rounds.length} round${rounds.length === 1 ? "" : "s"}${scorePart}`;
    host.innerHTML = `
      <div class="studio-next-card is-complete">
        <div class="studio-next-eyebrow">SET COMPLETE</div>
        <div class="studio-next-title">End of Set</div>
        <div class="studio-next-sub">${esc(summaryTxt)}</div>
        <div class="studio-next-actions">
          <button type="button" class="sm primary" onclick="replayCurrentStudioRound()">↺ Replay Round</button>
          <button type="button" class="sm studio-next-sec-btn" onclick="restartStudioSet()">⏮ Restart Set</button>
          <button type="button" class="sm ghost studio-next-cancel-btn" onclick="dismissStudioNextOverlay()">Dismiss</button>
        </div>
      </div>
    `;
    _studioNextOverlayOpen = true;
    host.classList.remove("hidden");
  }
}

function loadRound(idx, opts = {}) {
  const mf = state.activeManifest;
  if (!mf || !mf.rounds[idx]) return;
  dismissStudioNextOverlay();
  state.activeRoundIdx = idx;
  state.activeMatchId = mf.rounds[idx].match_id || state.activeMatchId;
  state.playStopGf = null;
  stopAllStudioCardPreviews();
  cancelStudioInlineEdit(false);
  closeComposer(false);
  clearRange();
  const r = mf.rounds[idx];
  hideTimelineTooltip();
  _studioPendingAutoPlay = Boolean(opts && opts.autoPlay);
  if (video) {
    video.pause();
    video.src = r.video_url;
    video.load();
  }
  state.currentFrameOffset = 0;
  renderMatchAndRoundTabs();
  renderEventChips();
  renderNotesList();
  updateTimelineLegend(mf, r);
  loadRoundOverlay(r);
  updateHudAndTimeline();
  updateComposerAnchorBadge();
}

async function loadRoundOverlay(r) {
  syncOverlayButtons();
  state.activeOverlay = null;
  if (ovCanvas) ovCanvas.getContext("2d").clearRect(0, 0, ovCanvas.width, ovCanvas.height);
  const genBtn = document.getElementById("btnGenOverlays");
  const stTxt = document.getElementById("ovStatusText");

  if (!r || !r.overlay_url) {
    const hasGrouflon = state.capabilities.has_grouflon !== false;
    const canGen = state.activeManifest && state.activeManifest.can_generate_overlays && state.capabilities.can_generate_overlays && hasGrouflon;
    if (genBtn) genBtn.classList.toggle("hidden", !canGen);
    if (stTxt) {
      stTxt.textContent = canGen
        ? ""
        : (isHostedMode() ? "No overlay data" : (!hasGrouflon ? "Hitboxes require Grouflon (optional)" : "No overlay data"));
    }
    syncGrouflonGuideUi();
    return;
  }
  if (genBtn) genBtn.classList.add("hidden");

  if (state.overlayCache[r.overlay_url]) {
    state.activeOverlay = state.overlayCache[r.overlay_url];
    if (stTxt) stTxt.textContent = "";
    updateHudAndTimeline();
    return;
  }

  if (stTxt) stTxt.textContent = "Loading overlays...";
  try {
    const data = await fetchOverlayData(r.overlay_url);
    if (curRound() && curRound().overlay_url === r.overlay_url) {
      state.activeOverlay = data;
      if (stTxt) stTxt.textContent = "";
      updateHudAndTimeline();
    }
  } catch (_) {
    if (stTxt) stTxt.textContent = "Overlay load failed";
  }
}

async function generateSetOverlays() {
  if (!state.activeManifest) return;
  const qid = state.activeManifest.quark_id;
  const btn = document.getElementById("btnGenOverlays");
  btn.disabled = true;
  btn.textContent = "Generating (~5s)...";
  try {
    const res = await api(`/api/replays/${encodeURIComponent(qid)}/overlays`, {
      method: "POST",
      body: JSON.stringify({ force: false, perspective: state.activeManifest.perspective, username: state.username }),
    });
    if (res.manifest) state.activeManifest = res.manifest;
    toast(`Overlays ready (${res.generated} rounds in ${res.elapsed_s}s)`);
    loadRoundOverlay(curRound());
  } catch (err) {
    toast("Overlay generation failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "+ Generate Overlays";
  }
}

function _loadAudioPrefs() {
  let muted = false;
  let vol = 0.7;
  try {
    muted = localStorage.getItem("fc_muted") === "1";
    const rawV = parseFloat(localStorage.getItem("fc_volume"));
    if (!Number.isNaN(rawV) && rawV >= 0 && rawV <= 1) vol = rawV;
  } catch (_) {}
  return { muted, vol };
}

function syncAudioControls() {
  const { muted, vol } = _loadAudioPrefs();
  const effectiveMuted = muted || vol <= 0.001;
  ["roundVideo", "watchVideo", "sharedVideo"].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.muted = effectiveMuted;
    el.volume = vol;
  });
  document.querySelectorAll("[data-audio-mute]").forEach(btn => {
    btn.textContent = effectiveMuted ? "🔇" : (vol < 0.45 ? "🔉" : "🔊");
    btn.classList.toggle("active", !effectiveMuted);
  });
  document.querySelectorAll("[data-audio-vol]").forEach(sl => {
    sl.value = String(effectiveMuted ? 0 : vol);
  });
}

function toggleVideoMute() {
  const { muted, vol } = _loadAudioPrefs();
  const nextMuted = !(muted || vol <= 0.001);
  try {
    localStorage.setItem("fc_muted", nextMuted ? "1" : "0");
    if (!nextMuted && vol <= 0.001) localStorage.setItem("fc_volume", "0.7");
  } catch (_) {}
  syncAudioControls();
}

function setVideoVolume(rawVal) {
  const v = Math.max(0, Math.min(1, parseFloat(rawVal) || 0));
  try {
    localStorage.setItem("fc_volume", String(v));
    localStorage.setItem("fc_muted", v <= 0.001 ? "1" : "0");
  } catch (_) {}
  syncAudioControls();
}

syncAudioControls();

function setRate(rate) {
  const r = Number(rate) || 1;
  state.studioRate = r;
  if (video) video.playbackRate = r;
  document.querySelectorAll(".speed-btn[data-rate]").forEach(b => b.classList.toggle("active", Number(b.dataset.rate) === r));
}

function togglePlay() {
  if (video.paused) {
    if (state.loopPreviewActive && hasActiveLoopRange()) {
      const cur = currentGf();
      if (cur < state.rangeStartGf || cur >= state.rangeEndGf - 2) {
        seekToGf(state.rangeStartGf, false);
      }
    }
    syncAudioControls();
    video.play().catch(() => {
      // Fallback to muted playback if browser autoplay policy blocks unmuted play
      video.muted = true;
      video.play().catch(() => {});
    });
  } else {
    video.pause();
  }
}

function stepFrames(delta) {
  dismissStudioNextOverlay();
  const r = curRound();
  if (!r) return;
  video.pause();
  const fps = r.fps || 60;
  const curOff = Math.round((video.currentTime || 0) * fps);
  const nextOff = Math.max(0, Math.min(Math.max(0, r.frame_count - 1), curOff + delta));
  video.currentTime = nextOff / fps;
  state.currentFrameOffset = nextOff;
  updateHudAndTimeline();
}

function _syncEventsToggleLabel(shownCount, totalCount) {
  const el = document.getElementById("eventChips");
  const btn = document.getElementById("btnToggleEvents");
  if (!btn) return;
  const hidden = !el || el.classList.contains("hidden");
  const arrow = hidden ? "▾" : "▴";
  if (typeof shownCount === "number" && typeof totalCount === "number") {
    btn.dataset.shownCount = String(shownCount);
    btn.dataset.totalCount = String(totalCount);
  }
  const shown = Number(btn.dataset.shownCount || 0);
  const total = Number(btn.dataset.totalCount || 0);
  const countTxt = total > 0 ? (shown === total ? ` (${total})` : ` (${shown}/${total})`) : "";
  btn.textContent = `Events${countTxt} ${arrow}`;
}

function toggleEventChipsRow() {
  const el = document.getElementById("eventChips");
  if (!el) return;
  el.classList.toggle("hidden");
  _syncEventsToggleLabel();
}

function renderEventChips() {
  const r = curRound();
  const host = document.getElementById("eventChips");
  if (!host) return;
  const allEvents = (r && r.markers ? r.markers : []).filter(m =>
    ["dmg_self", "super", "dmg_opp"].includes(m.type)
  );
  if (!r || !allEvents.length) {
    host.innerHTML = `<span style="font-size:12px; color:var(--muted);">No event markers in this round</span>`;
    _syncEventsToggleLabel(0, 0);
    return;
  }
  const visible = allEvents.filter(m => isTimelineFilterOn(m.type));
  _syncEventsToggleLabel(visible.length, allEvents.length);
  if (!visible.length) {
    host.innerHTML = `<span style="font-size:12px; color:var(--muted);">All event types filtered out in legend</span>`;
    return;
  }
  host.innerHTML = visible.map(m => {
    const sec = (m.offset_f / (r.fps || 60)).toFixed(1);
    return `<button class="chip ev-${esc(m.type)}" onclick="seekToGf(${Math.max(r.start_gf, m.gf - 90)}, true)">${esc(m.label)} · ${sec}s</button>`;
  }).join("");
  host.scrollLeft = 0;
}

(function bindEventChipsWheelScroll() {
  const host = document.getElementById("eventChips");
  if (!host) return;
  host.addEventListener("wheel", e => {
    if (host.scrollWidth <= host.clientWidth) return;
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
      e.preventDefault();
      host.scrollLeft += e.deltaY;
    }
  }, { passive: false });
})();

function updateHudAndTimeline() {
  const r = curRound();
  if (!r) return;
  const fps = r.fps || 60;
  const gf = currentGf();
  const off = gf - r.start_gf;
  const sec = (off / fps).toFixed(2);
  document.getElementById("frameHud").textContent = `${formatRoundTag(r)} · ${sec}s · gf${gf}`;
  document.getElementById("btnPlayPause").textContent = video.paused ? "▶" : "❚❚";
  syncLoopPreviewUi();
  drawTimeline(r, off);
  drawOverlayOnCanvas(ovCanvas, state.activeOverlay, r.start_gf, off, state.overlayOpts);
  updateComposerAnchorBadge();
}

let _timelineHitTargets = [];
let _hoveredTimelineKey = "";
let _hoveredTimelineTargets = [];

function hideTimelineTooltip(redraw = false) {
  const hadHover = _hoveredTimelineKey !== "";
  _hoveredTimelineKey = "";
  _hoveredTimelineTargets = [];
  const tipEl = document.getElementById("timelineTooltip");
  if (tipEl) {
    tipEl.classList.remove("show");
    tipEl.setAttribute("aria-hidden", "true");
  }
  if (redraw && hadHover) {
    const r = curRound();
    if (r) drawTimeline(r, currentGf() - r.start_gf);
  }
}

function _formatTimelineMarkerTip(m, fps) {
  const sec = (m.offset_f / (fps || 60)).toFixed(1);
  let lbl = (m.label || "").trim();
  if (!lbl) {
    if (m.type === "dmg_self") lbl = `Took -${Number(m.value || 0)} HP`;
    else if (m.type === "dmg_opp") lbl = `Dealt -${Number(m.value || 0)} HP`;
    else if (m.type === "super") lbl = m.actor === "opp" ? "Opp Super" : "Your Super";
    else lbl = m.type || "Event";
  }
  return `${lbl} · ${sec}s`;
}

function _clipText(str, maxLen = 42) {
  const s = String(str || "").replace(/\s+/g, " ").trim();
  if (s.length <= maxLen) return s;
  return s.slice(0, maxLen - 1) + "…";
}

const MIN_LOOP_FRAMES = 15;
const LOOP_BRACE_CANVAS_H = 22;

let _timelineDragMode = null; // null | "start" | "end" | "move" | "scrub"
let _timelineDragMoveOffsetGf = 0;
let _timelineDragMoveSpanGf = 0;
let _timelineDragResumePreview = false;
let _timelineHoverZone = null; // null | "start" | "end" | "move"

function _fillRoundRect(c, x, y, w, h, r) {
  const rad = Math.min(r, w / 2, h / 2);
  c.beginPath();
  c.moveTo(x + rad, y);
  c.arcTo(x + w, y, x + w, y + h, rad);
  c.arcTo(x + w, y + h, x, y + h, rad);
  c.arcTo(x, y + h, x, y, rad);
  c.arcTo(x, y, x + w, y, rad);
  c.closePath();
}

function hasActiveLoopRange() {
  return state.rangeStartGf != null && state.rangeEndGf != null && state.rangeEndGf > state.rangeStartGf;
}

function drawTimeline(r, curOff) {
  if (!ctx || !canvas) return;
  const w = canvas.width;
  const h = canvas.height;
  const total = roundGfSpan(r);
  const fps = r.fps || 60;
  const light = isLightTheme();
  ctx.clearRect(0, 0, w, h);

  ctx.fillStyle = light ? "#eef2f7" : "#0b101d";
  ctx.fillRect(0, 0, w, h);

  const hasLoop = hasActiveLoopRange();
  let loopX1 = 0;
  let loopX2 = 0;
  if (hasLoop) {
    const sOff = Math.max(0, Math.min(total, state.rangeStartGf - r.start_gf));
    const eOff = Math.max(sOff, Math.min(total, state.rangeEndGf - r.start_gf));
    loopX1 = (sOff / total) * w;
    loopX2 = Math.max(loopX1 + 4, (eOff / total) * w);
    // Warm interior highlight behind HP curves
    ctx.fillStyle = light ? "rgba(217, 119, 6, 0.13)" : "rgba(251, 191, 36, 0.11)";
    ctx.fillRect(loopX1, 0, loopX2 - loopX1, h);
  }

  // Subtle vertical hairline guide for currently hovered tick/pin
  if (_hoveredTimelineTargets.length) {
    const hx = _hoveredTimelineTargets[0].xFrac * w;
    ctx.save();
    ctx.strokeStyle = light ? "rgba(15, 23, 42, 0.28)" : "rgba(226, 232, 240, 0.30)";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(hx, 0);
    ctx.lineTo(hx, h);
    ctx.stroke();
    ctx.restore();
  }

  // HP curves (projected by perspective.py: pt[1] = Self HP, pt[2] = Opp HP)
  // Rendered as step-after paths because HP holds flat between hits and drops discretely on impact.
  const curve = r.hp_curve || [];
  if (curve.length > 1) {
    const hpTopPad = 26;
    const hpBottomPad = 18;
    const hpSpanY = h - hpTopPad - hpBottomPad;
    if (isTimelineFilterOn("self_hp")) {
      ctx.beginPath();
      ctx.strokeStyle = light ? "rgba(2, 132, 199, 0.92)" : "rgba(56, 189, 248, 0.9)";
      ctx.lineWidth = 2.6;
      let prevY = null;
      curve.forEach((pt, i) => {
        const x = (pt[0] / total) * w;
        const y = h - hpBottomPad - pt[1] * hpSpanY;
        if (i === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, prevY);
          ctx.lineTo(x, y);
        }
        prevY = y;
      });
      ctx.stroke();
    }

    if (isTimelineFilterOn("opp_hp")) {
      ctx.beginPath();
      ctx.strokeStyle = light ? "rgba(225, 29, 72, 0.85)" : "rgba(251, 113, 133, 0.8)";
      ctx.lineWidth = 2.2;
      let prevY = null;
      curve.forEach((pt, i) => {
        const x = (pt[0] / total) * w;
        const y = h - hpBottomPad - pt[2] * hpSpanY;
        if (i === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, prevY);
          ctx.lineTo(x, y);
        }
        prevY = y;
      });
      ctx.stroke();
    }
  }

  // Dimmed exterior veil outside [loopX1..loopX2] so the active loop window pops without hiding HP curves
  if (hasLoop) {
    ctx.fillStyle = light ? "rgba(15, 23, 42, 0.14)" : "rgba(4, 7, 15, 0.48)";
    if (loopX1 > 0) ctx.fillRect(0, 0, loopX1, h);
    if (loopX2 < w) ctx.fillRect(loopX2, 0, w - loopX2, h);
  }

  const hits = [];
  const hoveredIds = new Set(_hoveredTimelineTargets.map(t => t.id));

  // Event ticks along bottom rail (legend-backed event types: Took Dmg, Dealt Dmg, Super)
  const colors = light
    ? { dmg_self: "#ea580c", dmg_opp: "#059669", super: "#d97706" }
    : { dmg_self: "#f97316", dmg_opp: "#34d399", super: "#fbbf24" };
  const markerFilterKey = {
    dmg_self: "dmg_self",
    dmg_opp: "dmg_opp",
    super: "super",
  };
  (r.markers || []).forEach((m, idx) => {
    const fKey = markerFilterKey[m.type];
    if (!fKey || !isTimelineFilterOn(fKey)) return;
    const xFrac = Math.max(0, Math.min(1, m.offset_f / total));
    const x = xFrac * w;
    const color = colors[m.type] || "#94a3b8";
    const id = `mk:${idx}:${m.type}:${m.offset_f}`;
    const isHov = hoveredIds.has(id);
    const tickH = m.type === "dmg_self" ? 20 : 14;
    const effW = isHov ? 5 : 3;
    const effH = isHov ? tickH + 4 : tickH;
    ctx.fillStyle = color;
    ctx.fillRect(x - effW / 2, h - effH, effW, effH);
    hits.push({
      id,
      rail: "bottom",
      shape: "tick",
      xFrac,
      color,
      label: _formatTimelineMarkerTip(m, fps),
    });
  });

  // Saved note pins along top rail (shifted below top loop brace when active)
  const topDotY = hasLoop ? (LOOP_BRACE_CANVAS_H + 11) : 12;
  if (isTimelineFilterOn("notes")) {
    state.annotations.filter(a => a.round_id === r.round_id).forEach((a, idx) => {
      const off = Math.max(0, (a.anchor_gf || r.start_gf) - r.start_gf);
      const xFrac = Math.max(0, Math.min(1, off / total));
      const x = xFrac * w;
      const isResp = a.source === "takeover_response" || a.kind === "response";
      const color = isResp
        ? (light ? "#059669" : "#34d399")
        : (light ? "#9333ea" : "#c084fc");
      const id = `ann:${a.id || idx}`;
      const isHov = hoveredIds.has(id);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(x, topDotY, isHov ? 8 : 6, 0, Math.PI * 2);
      ctx.fill();
      if (isHov) {
        ctx.strokeStyle = light ? "#0f172a" : "#ffffff";
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      const sec = (off / fps).toFixed(1);
      const kindLbl = (typeof KIND_LABELS !== "undefined" && KIND_LABELS[a.kind]) ? KIND_LABELS[a.kind] : "Note";
      const txt = _clipText(a.note || (a.tags && a.tags.length ? a.tags.join(", ") : kindLbl));
      hits.push({
        id,
        rail: "top",
        shape: "dot",
        xFrac,
        color,
        label: `${txt} · ${sec}s`,
      });
    });
  }

  _timelineHitTargets = hits;

  // Interactive Practice Loop brace bar & draggable trim handles
  if (hasLoop) {
    const activeZone = _timelineDragMode || _timelineHoverZone;
    const braceW = Math.max(6, loopX2 - loopX1);
    const moveLit = activeZone === "move";
    const startLit = activeZone === "start";
    const endLit = activeZone === "end";

    // Top loop brace bar
    ctx.save();
    ctx.fillStyle = moveLit
      ? (light ? "#f59e0b" : "#fbbf24")
      : (light ? "rgba(217, 119, 6, 0.90)" : "rgba(245, 158, 11, 0.88)");
    _fillRoundRect(ctx, loopX1, 0, braceW, LOOP_BRACE_CANVAS_H, 4);
    ctx.fill();

    // Subtle center drag-grip lines on top brace
    if (braceW >= 28) {
      const cx = loopX1 + braceW / 2;
      const gripColor = light ? "rgba(255, 255, 255, 0.85)" : "rgba(11, 15, 25, 0.65)";
      ctx.fillStyle = gripColor;
      for (let g = -1; g <= 1; g++) {
        ctx.fillRect(cx + g * 5 - 1, 5, 2, LOOP_BRACE_CANVAS_H - 10);
      }
    }

    // Crisp left & right vertical trim lines
    const drawTrimHandle = (edgeX, side, lit) => {
      const strokeCol = lit
        ? (light ? "#b45309" : "#fde047")
        : (light ? "#d97706" : "#fbbf24");
      ctx.strokeStyle = strokeCol;
      ctx.lineWidth = lit ? 3.5 : 2.5;
      ctx.beginPath();
      ctx.moveTo(edgeX, 0);
      ctx.lineTo(edgeX, h);
      ctx.stroke();

      // Visible grab pill tab on the upper-mid edge
      const pillW = 12;
      const pillH = 38;
      const pillY = Math.round(LOOP_BRACE_CANVAS_H + (h - LOOP_BRACE_CANVAS_H) * 0.28);
      const rawPillX = side === "start" ? (edgeX - pillW / 2) : (edgeX - pillW / 2);
      const pillX = Math.max(1, Math.min(w - pillW - 1, rawPillX));

      ctx.fillStyle = lit
        ? (light ? "#b45309" : "#fde047")
        : (light ? "#d97706" : "#f59e0b");
      _fillRoundRect(ctx, pillX, pillY, pillW, pillH, 4);
      ctx.fill();
      ctx.strokeStyle = light ? "#ffffff" : "#0b101d";
      ctx.lineWidth = 1.5;
      ctx.stroke();

      // Twin vertical grip bars inside the handle pill
      ctx.fillStyle = light ? "#ffffff" : "#0b101d";
      ctx.fillRect(pillX + 3.5, pillY + 9, 1.8, pillH - 18);
      ctx.fillRect(pillX + pillW - 5.3, pillY + 9, 1.8, pillH - 18);
    };

    drawTrimHandle(loopX1, "start", startLit);
    drawTrimHandle(loopX2, "end", endLit);
    ctx.restore();
  }

  // Playhead
  const px = (curOff / total) * w;
  ctx.fillStyle = light ? "#0f172a" : "#ffffff";
  ctx.fillRect(px - 1.5, 0, 3, h);
}

function _hitTestTimelineLoop(e) {
  if (!canvas || !hasActiveLoopRange()) return null;
  const r = curRound();
  if (!r) return null;
  const rect = canvas.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  const xCss = e.clientX - rect.left;
  const yCss = e.clientY - rect.top;
  if (xCss < -10 || xCss > rect.width + 10 || yCss < 0 || yCss > rect.height) return null;

  const total = roundGfSpan(r);
  const x1 = ((state.rangeStartGf - r.start_gf) / total) * rect.width;
  const x2 = ((state.rangeEndGf - r.start_gf) / total) * rect.width;

  const dStart = Math.abs(xCss - x1);
  const dEnd = Math.abs(xCss - x2);
  if (dStart <= 10 || dEnd <= 10) {
    if (dStart <= 10 && dEnd <= 10) {
      return xCss <= (x1 + x2) / 2 ? "start" : "end";
    }
    return dStart <= 10 ? "start" : "end";
  }

  if (xCss >= x1 && xCss <= x2 && yCss >= 0 && yCss <= 18) {
    return "move";
  }
  return null;
}

function _pickHoveredTimelineTargets(e) {
  if (!canvas || !_timelineHitTargets.length) return [];
  const rect = canvas.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return [];
  const xCss = e.clientX - rect.left;
  const yCss = e.clientY - rect.top;
  if (xCss < 0 || xCss > rect.width || yCss < 0 || yCss > rect.height) return [];

  const findNearest = (rail, maxDist) => {
    let best = null;
    let bestDist = maxDist;
    for (const t of _timelineHitTargets) {
      if (t.rail !== rail) continue;
      const d = Math.abs(t.xFrac * rect.width - xCss);
      if (d <= bestDist) {
        bestDist = d;
        best = t;
      }
    }
    return best;
  };

  let anchor = null;
  const topZoneMax = hasActiveLoopRange() ? 34 : 22;
  if (yCss <= topZoneMax) {
    anchor = findNearest("top", 10) || findNearest("bottom", 8);
  } else {
    anchor = findNearest("bottom", 9);
  }
  if (!anchor) return [];

  // Group simultaneous / overlapping events on the same rail within 4px (e.g. trade hits)
  const anchorX = anchor.xFrac * rect.width;
  return _timelineHitTargets.filter(
    t => t.rail === anchor.rail && Math.abs(t.xFrac * rect.width - anchorX) <= 4
  );
}

function updateTimelineHover(e) {
  if (_timelineDragMode || e.pointerType === "touch") return;
  const tipEl = document.getElementById("timelineTooltip");
  if (!tipEl || !canvas) return;
  const matched = _pickHoveredTimelineTargets(e);
  if (!matched.length) {
    hideTimelineTooltip(true);
    return;
  }

  const key = matched.map(t => t.id).join("|");
  const changed = key !== _hoveredTimelineKey;
  _hoveredTimelineKey = key;
  _hoveredTimelineTargets = matched;

  if (changed) {
    tipEl.innerHTML = matched.map(t => (
      `<span class="tl-tip-row"><span class="tl-tip-swatch ${t.shape === "dot" ? "dot" : ""}" style="background:${t.color};"></span><span class="tl-tip-text">${esc(t.label)}</span></span>`
    )).join("");
    const r = curRound();
    if (r) drawTimeline(r, currentGf() - r.start_gf);
  }

  tipEl.style.left = "-9999px";
  tipEl.style.top = "-9999px";
  tipEl.classList.add("show");
  tipEl.setAttribute("aria-hidden", "false");

  const rect = canvas.getBoundingClientRect();
  const tipRect = tipEl.getBoundingClientRect();
  const vw = window.innerWidth || document.documentElement.clientWidth || 1024;
  const vh = window.innerHeight || document.documentElement.clientHeight || 768;
  const margin = 8;

  const anchorX = rect.left + matched[0].xFrac * rect.width;
  const left = Math.max(margin, Math.min(vw - tipRect.width - margin, anchorX - tipRect.width / 2));
  let top = rect.top - tipRect.height - 8;
  if (top < margin) {
    top = Math.min(vh - tipRect.height - margin, rect.bottom + 8);
  }

  tipEl.style.left = `${Math.round(left)}px`;
  tipEl.style.top = `${Math.round(top)}px`;
}

// Timeline pointer scrubbing, interactive loop handle/brace dragging & tick hover tooltips
function _pointerClientX(e) {
  return (e.touches && e.touches[0]) ? e.touches[0].clientX : e.clientX;
}

function _gfFromPointerEvent(e) {
  const r = curRound();
  if (!r || !canvas) return 0;
  const rect = canvas.getBoundingClientRect();
  if (rect.width <= 0) return r.start_gf;
  const frac = Math.max(0, Math.min(1, (_pointerClientX(e) - rect.left) / rect.width));
  return r.start_gf + Math.round(frac * roundGfSpan(r));
}

function scrubFromEvent(e) {
  const r = curRound();
  if (!r) return;
  seekToGf(_gfFromPointerEvent(e), false);
}

function _applyTimelineDrag(e) {
  const r = curRound();
  if (!r || !canvas) return;
  const rStart = r.start_gf;
  const rSpan = roundGfSpan(r);
  const rEnd = rStart + rSpan;
  const minSpan = Math.min(MIN_LOOP_FRAMES, Math.max(1, rSpan));
  const gf = _gfFromPointerEvent(e);

  if (_timelineDragMode === "start" && hasActiveLoopRange()) {
    state.rangeStartGf = Math.max(rStart, Math.min(state.rangeEndGf - minSpan, gf));
    updateRangeBanner();
    seekToGf(state.rangeStartGf, false);
    updateComposerAnchorBadge();
  } else if (_timelineDragMode === "end" && hasActiveLoopRange()) {
    state.rangeEndGf = Math.min(rEnd, Math.max(state.rangeStartGf + minSpan, gf));
    updateRangeBanner();
    seekToGf(state.rangeEndGf, false);
    updateComposerAnchorBadge();
  } else if (_timelineDragMode === "move" && hasActiveLoopRange()) {
    const span = Math.max(minSpan, _timelineDragMoveSpanGf);
    const newStart = Math.max(rStart, Math.min(rEnd - span, gf - _timelineDragMoveOffsetGf));
    state.rangeStartGf = Math.round(newStart);
    state.rangeEndGf = Math.round(newStart + span);
    updateRangeBanner();
    seekToGf(state.rangeStartGf, false);
    updateComposerAnchorBadge();
  } else {
    scrubFromEvent(e);
  }
}

function _cursorForZone(zone, dragging = false) {
  if (zone === "start" || zone === "end") return "ew-resize";
  if (zone === "move") return dragging ? "grabbing" : "grab";
  return "pointer";
}

function _finishTimelineDrag(e) {
  if (!_timelineDragMode) return;
  const shouldResume = _timelineDragResumePreview;
  _timelineDragMode = null;
  _timelineDragResumePreview = false;
  const nextZone = e ? _hitTestTimelineLoop(e) : null;
  _timelineHoverZone = nextZone;
  if (canvas) canvas.style.cursor = _cursorForZone(nextZone, false);

  if (shouldResume && hasActiveLoopRange()) {
    state.loopPreviewActive = true;
    state.playStopGf = null;
    seekToGf(state.rangeStartGf, false);
    syncAudioControls();
    video.play().catch(() => {
      video.muted = true;
      video.play().catch(() => {});
    });
    syncLoopPreviewUi();
  } else {
    updateHudAndTimeline();
  }
}

if (canvas) {
  canvas.addEventListener("pointerdown", e => {
    const r = curRound();
    if (!r) return;
    dismissStudioNextOverlay();
    hideTimelineTooltip(false);
    const zone = _hitTestTimelineLoop(e);
    _timelineDragMode = zone || "scrub";
    _timelineHoverZone = zone;

    if (_timelineDragMode !== "scrub") {
      _timelineDragResumePreview = Boolean(state.loopPreviewActive && !video.paused);
    } else {
      _timelineDragResumePreview = false;
      if (state.loopPreviewActive) {
        state.loopPreviewActive = false;
        syncLoopPreviewUi();
      }
    }

    video.pause();
    try { canvas.setPointerCapture(e.pointerId); } catch (_) {}
    canvas.style.cursor = _cursorForZone(_timelineDragMode, true);

    if (_timelineDragMode === "move" && hasActiveLoopRange()) {
      const ptrGf = _gfFromPointerEvent(e);
      _timelineDragMoveSpanGf = state.rangeEndGf - state.rangeStartGf;
      _timelineDragMoveOffsetGf = ptrGf - state.rangeStartGf;
      seekToGf(state.rangeStartGf, false);
    } else {
      _applyTimelineDrag(e);
    }
  });

  canvas.addEventListener("pointermove", e => {
    if (_timelineDragMode) {
      hideTimelineTooltip(false);
      _applyTimelineDrag(e);
      return;
    }
    const zone = _hitTestTimelineLoop(e);
    canvas.style.cursor = _cursorForZone(zone, false);
    if (zone !== _timelineHoverZone) {
      _timelineHoverZone = zone;
      const r = curRound();
      if (r) drawTimeline(r, currentGf() - r.start_gf);
    }
    if (zone) {
      hideTimelineTooltip(false);
      return;
    }
    updateTimelineHover(e);
  });

  canvas.addEventListener("pointerup", e => _finishTimelineDrag(e));
  canvas.addEventListener("pointercancel", () => _finishTimelineDrag(null));
  canvas.addEventListener("pointerleave", () => {
    if (!_timelineDragMode) {
      const hadZone = _timelineHoverZone != null;
      _timelineHoverZone = null;
      canvas.style.cursor = "pointer";
      hideTimelineTooltip(true);
      if (hadZone) {
        const r = curRound();
        if (r) drawTimeline(r, currentGf() - r.start_gf);
      }
    }
  });
  window.addEventListener("scroll", () => hideTimelineTooltip(false), { passive: true, capture: true });
  window.addEventListener("resize", () => hideTimelineTooltip(false), { passive: true });
}

function checkLoopPreviewWrap() {
  if (state.loopPreviewActive && video && !video.paused && hasActiveLoopRange()) {
    if (currentGf() >= state.rangeEndGf) {
      seekToGf(state.rangeStartGf, false);
      if (video.paused) {
        video.play().catch(() => {});
      }
    }
  }
}

if (video) {
  video.addEventListener("timeupdate", () => {
    checkLoopPreviewWrap();
    updateHudAndTimeline();
    if (!state.loopPreviewActive && state.playStopGf != null && currentGf() >= state.playStopGf) {
      video.pause();
      state.playStopGf = null;
    }
  });
  video.addEventListener("ended", () => {
    if (state.loopPreviewActive && hasActiveLoopRange()) {
      seekToGf(state.rangeStartGf, false);
      video.play().catch(() => {});
    } else {
      updateHudAndTimeline();
      showStudioNextRoundOverlay();
    }
  });
  video.addEventListener("play", () => {
    dismissStudioNextOverlay();
    stopAllStudioCardPreviews();
    updateHudAndTimeline();
  });
  video.addEventListener("pause", () => updateHudAndTimeline());
  video.addEventListener("loadedmetadata", () => {
    video.playbackRate = state.studioRate || 1;
    syncAudioControls();
    updateHudAndTimeline();
    if (_studioPendingAutoPlay && state.activeView === "studio") {
      _studioPendingAutoPlay = false;
      video.play().catch(() => {
        video.muted = true;
        video.play().catch(() => {});
      });
    } else {
      _studioPendingAutoPlay = false;
    }
  });
  if ("requestVideoFrameCallback" in HTMLVideoElement.prototype) {
    const onVfc = () => {
      checkLoopPreviewWrap();
      updateHudAndTimeline();
      video.requestVideoFrameCallback(onVfc);
    };
    video.requestVideoFrameCallback(onVfc);
  }
}

// ---------- PRACTICE LOOP & CONTEXTUAL RANGE ----------
function computeDefaultLoopBounds(r, anchorGf) {
  const rStart = Number(r.start_gf || 0);
  const rSpan = roundGfSpan(r);
  const rEnd = rStart + rSpan;
  const minSpan = Math.min(MIN_LOOP_FRAMES, Math.max(1, rSpan));
  let s = Math.max(rStart, anchorGf - 120);
  let e = Math.min(rEnd, anchorGf + 90);
  if (e - s < minSpan) {
    if (s + minSpan <= rEnd) {
      e = s + minSpan;
    } else {
      e = rEnd;
      s = Math.max(rStart, e - minSpan);
    }
  }
  return { start: Math.round(s), end: Math.round(e) };
}

function setDefaultLoopAroundPlayhead(pauseVideo = true) {
  const r = curRound();
  if (!r) return;
  if (pauseVideo && video) video.pause();
  state.loopPreviewActive = false;
  state.playStopGf = null;
  const { start, end } = computeDefaultLoopBounds(r, currentGf());
  state.rangeStartGf = start;
  state.rangeEndGf = end;
  updateRangeBanner();
  updateHudAndTimeline();
  updateComposerAnchorBadge();
}

function markRangeBound(which) {
  const r = curRound();
  if (!r) return;
  const rStart = r.start_gf;
  const rSpan = roundGfSpan(r);
  const rEnd = rStart + rSpan;
  const minSpan = Math.min(MIN_LOOP_FRAMES, Math.max(1, rSpan));
  const gf = Math.max(rStart, Math.min(rEnd, currentGf()));

  if (which === "start") {
    let s = gf;
    let e = state.rangeEndGf != null ? Math.min(rEnd, state.rangeEndGf) : Math.min(rEnd, gf + 90);
    if (e - s < minSpan) {
      if (s + minSpan <= rEnd) e = s + minSpan;
      else {
        e = rEnd;
        s = Math.max(rStart, e - minSpan);
      }
    }
    state.rangeStartGf = Math.round(s);
    state.rangeEndGf = Math.round(e);
  } else {
    let e = gf;
    let s = state.rangeStartGf != null ? Math.max(rStart, state.rangeStartGf) : Math.max(rStart, gf - 120);
    if (e - s < minSpan) {
      if (e - minSpan >= rStart) s = e - minSpan;
      else {
        s = rStart;
        e = Math.min(rEnd, s + minSpan);
      }
    }
    state.rangeStartGf = Math.round(s);
    state.rangeEndGf = Math.round(e);
  }
  updateRangeBanner();
  updateHudAndTimeline();
  updateComposerAnchorBadge();
}

function clearRange() {
  state.loopPreviewActive = false;
  state.rangeStartGf = null;
  state.rangeEndGf = null;
  _timelineHoverZone = null;
  if (canvas && !_timelineDragMode) canvas.style.cursor = "pointer";
  updateRangeBanner();
  updateComposerAnchorBadge();
  const r = curRound();
  if (r) drawTimeline(r, currentGf() - r.start_gf);
}

function startLoopPreview() {
  if (!hasActiveLoopRange() || !video) return;
  state.loopPreviewActive = true;
  state.playStopGf = null;
  const cur = currentGf();
  if (cur < state.rangeStartGf || cur >= state.rangeEndGf - 2) {
    seekToGf(state.rangeStartGf, false);
  }
  syncAudioControls();
  video.play().catch(() => {
    video.muted = true;
    video.play().catch(() => {});
  });
  syncLoopPreviewUi();
}

function stopLoopPreview(pauseVideo = true) {
  state.loopPreviewActive = false;
  if (pauseVideo && video && !video.paused) video.pause();
  syncLoopPreviewUi();
}

function toggleLoopPreview() {
  if (!hasActiveLoopRange()) return;
  if (state.loopPreviewActive && video && !video.paused) {
    stopLoopPreview(true);
  } else {
    startLoopPreview();
  }
}

function syncLoopPreviewUi() {
  const btn = document.getElementById("btnPreviewLoop");
  if (!btn) return;
  const playingLoop = Boolean(state.loopPreviewActive && video && !video.paused && hasActiveLoopRange());
  btn.textContent = playingLoop ? "❚❚ Stop Preview" : "▶ Preview Loop";
  btn.classList.toggle("playing", playingLoop);
  btn.setAttribute("aria-pressed", playingLoop ? "true" : "false");
}

function _formatRoundGfClock(r, gf) {
  if (!r || gf == null) return "0:00";
  const fps = (r && r.fps) || 60;
  const sec = Math.max(0, (Number(gf) - Number(r.start_gf || 0)) / fps);
  return formatRoundClock(sec);
}

function _formatLoopRangeClockText(r, startGf, endGf) {
  const sClk = _formatRoundGfClock(r, startGf);
  const eClk = _formatRoundGfClock(r, endGf);
  return sClk === eClk ? sClk : `${sClk}–${eClk}`;
}

function _formatStudioMomentTimestamp(item, aRound, mf, scope) {
  const anchorGf = Number(item.anchor_gf || item.press_gf || item.start_gf || 0);
  const hasExplicitRange = Boolean(
    item.start_gf && item.end_gf && Number(item.end_gf) > Number(item.start_gf) && item.source !== "takeover_response"
  );
  const rTag = aRound ? formatRoundTag(aRound) : (item.round_id ? `R${item.round_id}` : "");
  const tipFrames = hasExplicitRange
    ? `gf${Number(item.start_gf)}–${Number(item.end_gf)}`
    : (anchorGf > 0 ? `gf${anchorGf}` : "");
  const tip = `Jump to ${[rTag, tipFrames].filter(Boolean).join(" · ") || "frame"}`;

  let timePart = "";
  if (aRound) {
    timePart = hasExplicitRange
      ? _formatLoopRangeClockText(aRound, item.start_gf, item.end_gf)
      : _formatRoundGfClock(aRound, anchorGf);
  }

  let prefix = "";
  if (scope === "match" || scope === "set") {
    const multiMatch = Boolean(mf && Array.isArray(mf.matches) && mf.matches.length > 1);
    if (scope === "set" && multiMatch && aRound && aRound.match_id) {
      prefix = `M${aRound.match_id} R${aRound.match_round || aRound.round_id}`;
    } else if (aRound) {
      prefix = `R${aRound.match_round || aRound.round_id}`;
    } else if (item.round_id) {
      prefix = `R${item.round_id}`;
    }
  }

  const text = [prefix, timePart].filter(Boolean).join(" · ") || (rTag || "0:00");
  return { text, tip };
}

function updateRangeBanner() {
  const bar = document.getElementById("practiceLoopBar");
  const banner = document.getElementById("rangeStatus");
  const proPill = document.getElementById("proSearchRangePill");
  const active = hasActiveLoopRange();
  const r = curRound();

  if (!active) {
    if (bar) bar.classList.add("hidden");
    if (proPill) {
      proPill.textContent = _formatRoundGfClock(r, currentGf());
      proPill.setAttribute("data-tip", `Current frame: gf${currentGf()}`);
    }
    syncLoopPreviewUi();
    updateComposerAnchorBadge();
    updateStudioInlineLoopPill();
    return;
  }

  if (bar) bar.classList.remove("hidden");
  const fps = (r && r.fps) || 60;
  const s = state.rangeStartGf;
  const e = state.rangeEndGf;
  const dur = formatClipDuration(s, e, fps) || "0.1s";
  const clk = _formatLoopRangeClockText(r, s, e);
  if (banner) {
    banner.textContent = `${clk} (${dur})`;
    banner.setAttribute("data-tip", `Practice Loop: gf${s}–${e}`);
  }
  if (proPill) {
    proPill.textContent = `${clk} (${dur})`;
    proPill.setAttribute("data-tip", `Practice Loop: gf${s}–${e}`);
  }
  syncLoopPreviewUi();
  updateComposerAnchorBadge();
  updateStudioInlineLoopPill();
}

// ---------- SIDEBAR SUB-TABS ----------
function setStudioSidebarTab(tab) {
  if (tab === "pro_search" && !isProCorpusEnabled()) {
    toast("Top-Rank Search is on the roadmap.");
    return;
  }
  state.studioSidebarTab = tab;
  document.getElementById("sideTabMoments").classList.toggle("active", tab === "moments");
  document.getElementById("sideTabSetNotes").classList.toggle("active", tab === "set_notes");
  document.getElementById("sideTabProSearch").classList.toggle("active", tab === "pro_search");
  document.getElementById("sidePaneMoments").classList.toggle("hidden", tab !== "moments");
  document.getElementById("sidePaneSetNotes").classList.toggle("hidden", tab !== "set_notes");
  document.getElementById("sidePaneProSearch").classList.toggle("hidden", tab !== "pro_search");
  if (tab === "pro_search") updateRangeBanner();
}

// ---------- UNIFIED COLLECTION PICKER (ACTIVE FOCUSES + PLAYLISTS) ----------
function renderComposerCollectionPicker() {
  const host = document.getElementById("composerCollectionsPicker");
  if (!host) return;
  host.innerHTML = renderCollectionPickerHtml("composer", [...state.selectedCollectionIds], {
    buttonLabel: "+ Playlist",
    onToggle: colId => toggleComposerCollection(colId),
    onCreate: () => openPlaylistModal(null, pl => {
      if (pl && pl.id) {
        state.selectedCollectionIds.add(pl.id);
        renderComposerCollectionPicker();
      }
    }),
  });
}

function renderStudioGoalsStrip() { renderComposerCollectionPicker(); }
function renderComposerPlaylistSelect() { renderComposerCollectionPicker(); }

function toggleComposerCollection(colId) {
  if (state.selectedCollectionIds.has(colId)) {
    state.selectedCollectionIds.delete(colId);
  } else {
    state.selectedCollectionIds.add(colId);
  }
  renderComposerCollectionPicker();
}

function _buildMomentPlaylistItem(savedItem, mf, r, note, tags, source) {
  const anchorGf = savedItem.anchor_gf || savedItem.press_gf || currentGf();
  const roundObj = roundById(savedItem.round_id) || r;
  const rStart = roundObj ? roundObj.start_gf : 0;
  const isResp = savedItem.source === "takeover_response" || Boolean(savedItem.response_clip_url) || source === "takeover_response";
  const fallbackMatchTxt = formatMatchLabelText({
    ...mf,
    self_char_name: (roundObj && roundObj.self_char_name) || mf.self_char_name,
    opp_char_name: (roundObj && roundObj.opp_char_name) || mf.opp_char_name,
  }, { showRank: false });
  const item = {
    item_id: savedItem.id,
    source: isResp ? "takeover_response" : (source === "select_mark" ? "select_mark" : "annotation"),
    quark_id: mf.quark_id,
    round_id: savedItem.round_id || (r ? r.round_id : 0),
    perspective: savedItem.perspective || mf.perspective,
    start_gf: savedItem.start_gf || Math.max(rStart, anchorGf - 180),
    end_gf: savedItem.end_gf || (anchorGf + 90),
    anchor_gf: anchorGf,
    label: (note || (isResp ? `Labbed Answer · R${savedItem.round_id || (r ? r.round_id : 1)}` : `${fallbackMatchTxt} · R${savedItem.round_id || (r ? r.round_id : 1)}`)).slice(0, 120),
    note: note || "",
    tags: tags || [],
  };
  if (isResp) {
    if (savedItem.parent_moment_id) item.parent_moment_id = savedItem.parent_moment_id;
    if (savedItem.response_clip_url) item.response_clip_url = savedItem.response_clip_url;
    if (savedItem.response_overlay_url) item.response_overlay_url = savedItem.response_overlay_url;
  }
  return item;
}

const _studioMomentPayloads = {};

async function toggleMomentInCollection(colId, itemOrId) {
  const col = findCollectionById(colId);
  const itemPayload = typeof itemOrId === "string" ? _studioMomentPayloads[itemOrId] : itemOrId;
  if (!col || !itemPayload) return;
  const already = collectionHasItem(col, itemPayload.item_id);
  try {
    const res = await api(`/api/playlists/${encodeURIComponent(colId)}/items`, {
      method: "POST",
      body: JSON.stringify(
        already
          ? { action: "remove", item_id: itemPayload.item_id }
          : { action: "add", item: itemPayload }
      ),
    });
    toast(already ? `Removed from "${res.playlist.name}"` : `Added to "${res.playlist.name}"`);
    await loadPlaylists(false);
    renderNotesList();
    renderComposerCollectionPicker();
  } catch (err) {
    toast(err.message);
  }
}

function _renderSavedMomentPickerHtml(itemPayload) {
  _studioMomentPayloads[itemPayload.item_id] = itemPayload;
  const collections = [...getActiveFocuses(), ...getReferencePlaylists()];
  const assignedIds = collections
    .filter(c => collectionHasItem(c, itemPayload.item_id))
    .map(c => c.id);
  const pickerId = `studio_${itemPayload.item_id}`;
  return `<div class="moment-picker-row">${renderCollectionPickerHtml(pickerId, assignedIds, {
    buttonLabel: "+ Playlist",
    onToggle: colId => toggleMomentInCollection(colId, itemPayload.item_id),
    onCreate: () => openPlaylistModal(null, async pl => {
      if (pl && pl.id) {
        await toggleMomentInCollection(pl.id, itemPayload.item_id);
      }
    }),
  })}</div>`;
}

function _syncComposerOpenUi() {
  const isOpen = Boolean(state.composerOpen);
  const card = document.getElementById("composerCard");
  if (card) card.classList.toggle("hidden", !isOpen);
  const sidebarRow = document.getElementById("sidebarNewMomentRow");
  if (sidebarRow) sidebarRow.classList.toggle("hidden", isOpen);
  const transportBtn = document.getElementById("btnNewMomentTransport");
  if (transportBtn) {
    transportBtn.classList.toggle("is-active", isOpen);
    transportBtn.setAttribute("aria-expanded", isOpen ? "true" : "false");
  }
}

function recenterComposerLoop() {
  if (!curRound()) return;
  setDefaultLoopAroundPlayhead(true);
  updateRangeBanner();
  updateComposerAnchorBadge();
}

function updateComposerAnchorBadge() {
  const el = document.getElementById("composerAnchor");
  const r = curRound();
  if (el && r) {
    const rTag = formatRoundTag(r);
    const fps = r.fps || 60;
    if (!state.studioEditId && hasActiveLoopRange()) {
      const clk = _formatLoopRangeClockText(r, state.rangeStartGf, state.rangeEndGf);
      const dur = formatClipDuration(state.rangeStartGf, state.rangeEndGf, fps) || "0.1s";
      el.textContent = `${clk} (${dur})`;
      el.setAttribute("data-tip", `${rTag} · gf${state.rangeStartGf}–${state.rangeEndGf} (click to recenter at playhead)`);
    } else {
      const gf = currentGf();
      const clk = _formatRoundGfClock(r, gf);
      el.textContent = state.composerOpen ? `${clk} · ±2s auto` : clk;
      el.setAttribute("data-tip", `${rTag} · gf${gf} (click to show loop handles)`);
    }
  }
}

function renderComposerControls() {
  const host = document.getElementById("composerTagPicker");
  if (!host) return;
  host.innerHTML = renderTagPickerHtml("composer", null, { buttonLabel: "+ Add tags" });
}

function resetComposerDraft() {
  state.selectedKind = "note";
  state.selectedTags = new Set();
  state.selectedCollectionIds = new Set();
  state.composerInitialCollectionIds = new Set();
  initTagPicker("composer", [], { buttonLabel: "+ Add tags" });
  if (typeof closeAllCollectionPickers === "function") closeAllCollectionPickers();
  const titleEl = document.getElementById("composerTitle");
  if (titleEl) titleEl.textContent = "New Moment";
  const noteEl = document.getElementById("noteTextarea");
  if (noteEl) noteEl.value = "";
  renderComposerCollectionPicker();
  renderComposerControls();
  updateComposerAnchorBadge();
}

function onComposerTextareaKeydown(ev) {
  if (!ev) return;
  if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
    ev.preventDefault();
    ev.stopPropagation();
    saveAnnotation();
    return;
  }
  if (ev.key === "Escape") {
    ev.preventDefault();
    ev.stopPropagation();
    if (typeof closeAllCollectionPickers === "function" && closeAllCollectionPickers()) return;
    if (typeof closeAllTagPickers === "function" && closeAllTagPickers()) return;
    closeComposer(true);
  }
}

function openComposer() {
  if (video) video.pause();
  dismissStudioNextOverlay();
  state.loopPreviewActive = false;
  state.playStopGf = null;
  setStudioSidebarTab("moments");
  if (!curRound()) return;
  if (state.studioEditId) cancelStudioInlineEdit(true);

  state.composerOpen = true;
  _syncComposerOpenUi();

  const cur = currentGf();
  const outsideExisting = hasActiveLoopRange() && (cur < state.rangeStartGf || cur > state.rangeEndGf);
  if (!hasActiveLoopRange() || outsideExisting) {
    setDefaultLoopAroundPlayhead(true);
  }

  const titleEl = document.getElementById("composerTitle");
  if (titleEl) titleEl.textContent = "New Moment";
  updateRangeBanner();
  updateComposerAnchorBadge();
  renderComposerCollectionPicker();
  renderComposerControls();
  const ta = document.getElementById("noteTextarea");
  if (ta) {
    if (typeof ta.focus === "function") ta.focus();
    const len = (ta.value || "").length;
    if (typeof ta.setSelectionRange === "function") {
      try { ta.setSelectionRange(len, len); } catch (_) {}
    }
  }
  if (typeof window !== "undefined" && window.innerWidth < 900) {
    const card = document.getElementById("composerCard");
    if (card && typeof card.scrollIntoView === "function") {
      card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

function closeComposer(clearLoop = true) {
  const wasOpen = Boolean(state.composerOpen);
  if (typeof closeAllTagPickers === "function") closeAllTagPickers();
  resetComposerDraft();
  state.composerOpen = false;
  state.loopPreviewActive = false;
  _syncComposerOpenUi();
  if (clearLoop) {
    clearRange();
  } else {
    updateRangeBanner();
  }
  return wasOpen;
}

async function saveAnnotation() {
  const r = curRound();
  const mf = state.activeManifest;
  if (!r || !mf) return;
  const tags = getTagPickerTags("composer");
  const noteEl = document.getElementById("noteTextarea");
  const note = (noteEl ? noteEl.value : "").trim();
  const selectedIds = [...state.selectedCollectionIds];
  const activeFocusIds = new Set(getActiveFocuses().map(g => g.id));
  const goal_ids = selectedIds.filter(id => activeFocusIds.has(id));
  const playlist_ids = selectedIds;

  const anchorGf = currentGf();
  let startGf = state.rangeStartGf;
  let endGf = state.rangeEndGf;
  if (!hasActiveLoopRange()) {
    const bounds = computeDefaultLoopBounds(r, anchorGf);
    startGf = bounds.start;
    endGf = bounds.end;
  }

  const btn = document.getElementById("btnSaveNote");
  if (btn) btn.disabled = true;
  try {
    const payload = {
      quark_id: mf.quark_id,
      round_id: r.round_id,
      perspective: mf.perspective,
      anchor_gf: anchorGf,
      start_gf: startGf,
      end_gf: endGf,
      kind: "note",
      tags,
      note,
      queued_drill: false,
      goal_ids,
      playlist_ids,
      owner: state.username || DEFAULT_USER,
    };
    const res = await api("/api/annotations", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.annotations.push(res.annotation);
    const savedItem = res.annotation;

    // Sync membership directly on every selected Focus & Reference Playlist
    if (savedItem && selectedIds.length) {
      const itemPayload = _buildMomentPlaylistItem(savedItem, mf, r, note, tags, savedItem.source || "annotation");
      await Promise.all(selectedIds.map(cid =>
        api(`/api/playlists/${encodeURIComponent(cid)}/items`, {
          method: "POST",
          body: JSON.stringify({ action: "add", item: itemPayload }),
        })
      ));
      await loadPlaylists(false);
    }

    closeComposer(true);
    toast("Moment saved");
    renderMatchAndRoundTabs();
    renderNotesList();
    updateHudAndTimeline();
    loadHubNotes(true);
  } catch (err) {
    toast("Save failed: " + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ---------- DIRECT IN-PLACE MOMENT EDITING IN REPLAY STUDIO ----------
function _autoSizeInlineTextarea(ta) {
  if (!ta) return;
  ta.style.height = "auto";
  const nextH = Math.max(92, Math.min(360, ta.scrollHeight + 10));
  ta.style.height = `${nextH}px`;
}

function _studioInlineLoopPillText() {
  if (!hasActiveLoopRange()) return "";
  const r = curRound();
  const fps = (r && r.fps) || 60;
  const dur = formatClipDuration(state.rangeStartGf, state.rangeEndGf, fps) || "0.1s";
  const clk = _formatLoopRangeClockText(r, state.rangeStartGf, state.rangeEndGf);
  return `🔁 ${clk} (${dur})`;
}

function updateStudioInlineLoopPill() {
  const pill = document.getElementById("studioInlineLoopPill");
  if (!pill) return;
  if (!state.studioEditId || state.studioEditSource !== "annotation" || !hasActiveLoopRange()) {
    pill.classList.add("hidden");
    return;
  }
  pill.textContent = _studioInlineLoopPillText();
  pill.setAttribute("data-tip", `Drag loop handles on the timeline to adjust (gf${state.rangeStartGf}–${state.rangeEndGf})`);
  pill.classList.remove("hidden");
}

function _syncStudioInlineDraftFromDom() {
  if (!state.studioEditId) return;
  const ta = document.getElementById("studioInlineNoteText");
  if (ta) state.studioEditText = ta.value;
}

function _renderStudioInlineEditorHtml(item, opts = {}) {
  const isResp = Boolean(opts.isResp);
  const isSelectMark = Boolean(opts.isSelectMark);
  const placeholder = isResp
    ? "Describe this labbed counterplay, spacing, or timing..."
    : "What happened here? What should you try instead?";
  const showLoopPill = !isResp && !isSelectMark && hasActiveLoopRange();
  const loopTxt = showLoopPill ? _studioInlineLoopPillText() : "";
  const loopTip = showLoopPill
    ? `Drag loop handles on the timeline to adjust (gf${state.rangeStartGf}–${state.rangeEndGf})`
    : "Drag loop handles on the timeline to adjust this moment's range";
  return `
    <div class="inline-studio-editor">
      <textarea id="studioInlineNoteText"
                class="inline-note-textarea"
                placeholder="${esc(placeholder)}"
                oninput="onStudioInlineTextareaInput(this)"
                onkeydown="onStudioInlineEditorKeydown(event)">${esc(state.studioEditText || "")}</textarea>
      ${renderTagPickerHtml("studioInline", null, { buttonLabel: "+ Add tags" })}
      <div class="inline-edit-actions">
        <div class="row" style="gap:6px;">
          <span id="studioInlineLoopPill" class="composer-loop-badge inline-loop-pill mono ${showLoopPill ? "" : "hidden"}" data-tip="${esc(loopTip)}">${esc(loopTxt)}</span>
        </div>
        <div class="row" style="gap:6px;">
          <button type="button" class="sm" data-tip="Cancel editing (Esc)" onclick="cancelStudioInlineEdit()">Cancel</button>
          <button id="btnSaveStudioInlineEdit" type="button" class="sm emerald" data-tip="Save changes (Ctrl+Enter)" onclick="saveStudioInlineEdit()">Save</button>
        </div>
      </div>
    </div>
  `;
}

function startStudioInlineEdit(id, source = "annotation") {
  const isSelect = source === "select_mark";
  const item = isSelect
    ? state.selectMarks.find(x => x.id === id)
    : state.annotations.find(x => x.id === id);
  if (!item) return;

  if (video) video.pause();
  state.loopPreviewActive = false;
  state.playStopGf = null;
  if (state.composerOpen) closeComposer(false);
  setStudioSidebarTab("moments");

  const isResp = item.source === "takeover_response" || item.kind === "response" || source === "takeover_response";
  const resolvedSource = isResp ? "takeover_response" : (isSelect ? "select_mark" : "annotation");

  let r = curRound();
  let switchedRound = false;
  if (!isResp && item.round_id && r && Number(item.round_id) !== Number(r.round_id) && state.activeManifest) {
    const rIdx = (state.activeManifest.rounds || []).findIndex(x => x.round_id === Number(item.round_id));
    if (rIdx >= 0) {
      loadRound(rIdx);
      r = curRound();
      switchedRound = true;
    }
  }

  state.studioEditId = item.id;
  state.studioEditSource = resolvedSource;
  state.studioEditKind = isResp ? "response" : (isSelect ? (item.kind || "select_mark") : "note");
  initTagPicker("studioInline", getDisplayTagsForItem(item), { buttonLabel: "+ Add tags" });
  state.studioEditText = item.is_default_select_note ? "" : (item.note || "");
  state.studioEditHasRange = false;

  const rObj = roundById(item.round_id) || r;
  if (!isResp && item.start_gf && item.end_gf && Number(item.end_gf) > Number(item.start_gf) && rObj) {
    const rStart = rObj.start_gf;
    const rEnd = rStart + roundGfSpan(rObj);
    const minSpan = Math.min(MIN_LOOP_FRAMES, Math.max(1, rEnd - rStart));
    const s = Math.max(rStart, Math.min(rEnd - minSpan, Number(item.start_gf)));
    const e = Math.min(rEnd, Math.max(s + minSpan, Number(item.end_gf)));
    state.rangeStartGf = Math.round(s);
    state.rangeEndGf = Math.round(e);
    state.studioEditHasRange = true;
    const seekTarget = Number(item.anchor_gf || item.press_gf || s);
    if (switchedRound) {
      video.addEventListener("loadedmetadata", () => seekToGf(seekTarget, false), { once: true });
    } else {
      seekToGf(seekTarget, false);
    }
    updateRangeBanner();
    updateHudAndTimeline();
  } else {
    if (!isResp) clearRange();
    else updateRangeBanner();
    updateComposerAnchorBadge();
  }

  renderNotesList();

  const ta = document.getElementById("studioInlineNoteText");
  if (ta) {
    _autoSizeInlineTextarea(ta);
    const card = ta.closest(".note-item, .response-card");
    if (card && typeof card.scrollIntoView === "function") {
      card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    ta.focus({ preventScroll: true });
    const len = ta.value.length;
    ta.setSelectionRange(len, len);
  }
}

function cancelStudioInlineEdit(rerender = true) {
  const hadEdit = Boolean(state.studioEditId);
  const hadRange = Boolean(state.studioEditHasRange);
  closeAllTagPickers();
  state.studioEditId = null;
  state.studioEditSource = "annotation";
  state.studioEditKind = "note";
  state.studioEditTags = new Set();
  state.studioEditCustomTags = "";
  state.studioEditText = "";
  state.studioEditShowMeta = false;
  state.studioEditHasRange = false;
  if (hadEdit && hadRange) {
    clearRange();
  }
  if (rerender && hadEdit) {
    renderNotesList();
    updateComposerAnchorBadge();
  }
}

function onStudioInlineTextareaInput(ta) {
  if (!ta) return;
  state.studioEditText = ta.value;
  _autoSizeInlineTextarea(ta);
}

function onStudioInlineEditorKeydown(ev) {
  if (!ev) return;
  if (ev.key === "Escape") {
    ev.preventDefault();
    ev.stopPropagation();
    if (typeof closeAllTagPickers === "function" && closeAllTagPickers()) return;
    cancelStudioInlineEdit(true);
    return;
  }
  if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
    ev.preventDefault();
    ev.stopPropagation();
    saveStudioInlineEdit();
  }
}

async function saveStudioInlineEdit() {
  const editId = state.studioEditId;
  const editSource = state.studioEditSource;
  if (!editId) return;

  _syncStudioInlineDraftFromDom();
  const tags = getTagPickerTags("studioInline");
  const note = String(state.studioEditText || "").trim();
  const savedKind = resolveSavedKindForItem(editSource, note, tags);

  const btn = document.getElementById("btnSaveStudioInlineEdit");
  if (btn) btn.disabled = true;
  try {
    if (editSource === "select_mark") {
      const res = await api(`/api/queue/${encodeURIComponent(editId)}`, {
        method: "PATCH",
        body: JSON.stringify({ kind: savedKind, tags, note }),
      });
      const idx = state.selectMarks.findIndex(x => x.id === editId);
      if (idx >= 0) {
        state.selectMarks[idx] = { ...state.selectMarks[idx], ...res.item, needs_note: !note, is_default_select_note: false };
      }
      if (state.studioEditHasRange) clearRange();
      toast("Bookmark updated");
    } else {
      const isResp = editSource === "takeover_response";
      const patchPayload = { kind: savedKind, tags, note };
      if (!isResp && hasActiveLoopRange()) {
        patchPayload.start_gf = state.rangeStartGf;
        patchPayload.end_gf = state.rangeEndGf;
      }
      const res = await api(`/api/annotations/${encodeURIComponent(editId)}`, {
        method: "PATCH",
        body: JSON.stringify(patchPayload),
      });
      const idx = state.annotations.findIndex(a => a.id === editId);
      if (idx >= 0) state.annotations[idx] = res.annotation;
      if (!isResp) clearRange();
      toast(isResp ? "Labbed answer updated" : "Moment updated");
    }

    cancelStudioInlineEdit(false);
    renderMatchAndRoundTabs();
    renderNotesList();
    updateHudAndTimeline();
    loadPlaylists(false);
    loadHubNotes(true);
  } catch (err) {
    toast("Save failed: " + err.message);
    if (btn) btn.disabled = false;
  }
}

async function refreshAnnotationsQuiet() {
  if (!state.activeManifest) return;
  try {
    const res = await api(`/api/annotations?quark_id=${encodeURIComponent(state.activeManifest.quark_id)}`);
    state.annotations = res.annotations || [];
    const activeEl = document.activeElement;
    const wasInInlineTa = Boolean(activeEl && activeEl.id === "studioInlineNoteText");
    const selStart = wasInInlineTa ? activeEl.selectionStart : null;
    const selEnd = wasInInlineTa ? activeEl.selectionEnd : null;
    _syncStudioInlineDraftFromDom();
    renderNotesList();
    if (wasInInlineTa) {
      const ta = document.getElementById("studioInlineNoteText");
      if (ta) {
        _autoSizeInlineTextarea(ta);
        ta.focus({ preventScroll: true });
        if (selStart != null && selEnd != null) ta.setSelectionRange(selStart, selEnd);
      }
    }
    if (state.annotations.some(a => a.drill_status === "baking")) {
      setTimeout(refreshAnnotationsQuiet, 2000);
    }
  } catch (_) {}
}

function setNoteScope(scope) {
  state.noteScope = scope;
  document.getElementById("btnScopeRound").classList.toggle("active", scope === "round");
  document.getElementById("btnScopeMatch").classList.toggle("active", scope === "match");
  document.getElementById("btnScopeSet").classList.toggle("active", scope === "set");
  renderNotesList();
}

function jumpToMoment(roundId, startGf, endGf, anchorGf, playRange = false) {
  if (!state.activeManifest) return;
  state.loopPreviewActive = false;
  const rIdx = state.activeManifest.rounds.findIndex(r => r.round_id === Number(roundId));
  const target = playRange ? (startGf || anchorGf) : (anchorGf || startGf);
  const stop = (playRange && endGf && endGf > target) ? endGf : null;
  if (rIdx >= 0 && rIdx !== state.activeRoundIdx) {
    loadRound(rIdx);
    video.addEventListener("loadedmetadata", () => {
      state.playStopGf = stop;
      seekToGf(target, playRange);
    }, { once: true });
  } else {
    state.playStopGf = stop;
    seekToGf(target, playRange);
  }
  if (startGf && endGf && endGf > startGf) {
    state.rangeStartGf = startGf;
    state.rangeEndGf = endGf;
    updateRangeBanner();
    updateHudAndTimeline();
  }
}

async function dismissSelectMarkInStudio(id) {
  try {
    await api("/api/queue/status", {
      method: "POST",
      body: JSON.stringify({ id, status: "dismissed" }),
    });
    if (state.studioEditId === id) cancelStudioInlineEdit(false);
    state.selectMarks = state.selectMarks.filter(s => s.id !== id);
    renderMatchAndRoundTabs();
    renderNotesList();
    updateHudAndTimeline();
    loadHubNotes(true);
    toast("Bookmark dismissed");
  } catch (err) {
    toast(err.message);
  }
}

async function removeNote(id) {
  if (!confirm("Delete this moment note?")) return;
  try {
    await api(`/api/annotations/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (state.studioEditId === id) cancelStudioInlineEdit(false);
    state.annotations = state.annotations.filter(x => x.id !== id);
    renderMatchAndRoundTabs();
    renderNotesList();
    updateHudAndTimeline();
    loadHubNotes(true);
    toast("Deleted");
  } catch (err) {
    toast(err.message);
  }
}

async function practiceSavedMoment(id, source, qid, startGf, endGf, anchorGf, persp, label, btnEl = null) {
  if (source === "select_mark") {
    const sm = state.selectMarks.find(x => x.id === id);
    const fullNote = (sm && !sm.needs_note && !sm.is_default_select_note && sm.note) ? sm.note : "";
    await launchDrill(id, {
      note: fullNote,
      parent_moment_id: id,
      round_id: Number((sm && sm.round_id) || (curRound() && curRound().round_id) || 0),
    }, btnEl);
    return;
  }
  const ann = state.annotations.find(a => a.id === id);
  const fullNote = (ann && ann.note) ? ann.note : "";
  const rId = Number((ann && ann.round_id) || (curRound() && curRound().round_id) || 0);
  if (source === "takeover_response" || (ann && ann.source === "takeover_response")) {
    await launchDrill(id, {
      mode: "takeover",
      note: fullNote,
      parent_moment_id: (ann && ann.parent_moment_id) || "",
      round_id: rId,
    }, btnEl);
    return;
  }
  if (ann && ann.queued_drill && ann.drill_status === "ready") {
    await launchDrill(id, { note: fullNote, parent_moment_id: id, round_id: rId }, btnEl);
    return;
  }
  await launchTakeoverPayload({
    quark_id: qid,
    frame: anchorGf,
    start_gf: startGf,
    end_gf: endGf,
    player: persp,
    bake_if_missing: true,
    label,
    note: fullNote,
    parent_moment_id: id,
    round_id: rId,
  }, btnEl);
}

function _formatHpSwingPill(item) {
  if (!item || (item.hp_delta_self == null && item.hp_delta_opp == null)) return "";
  const dSelf = Number(item.hp_delta_self || 0);
  const dOpp = Number(item.hp_delta_opp || 0);
  const oppDmg = Math.max(0, -dOpp);
  const selfDmg = Math.max(0, -dSelf);
  const net = dSelf - dOpp;
  let txt = "";
  let cls = "hp-swing-neutral";
  if (oppDmg > 0 && selfDmg === 0) {
    txt = `-${oppDmg} opp HP`;
    cls = "hp-swing-good";
  } else if (oppDmg > 0 || selfDmg > 0) {
    txt = `Opp ${dOpp > 0 ? "+" : ""}${dOpp} / You ${dSelf > 0 ? "+" : ""}${dSelf}`;
    cls = net > 0 ? "hp-swing-good" : "hp-swing-neutral";
  } else {
    txt = "0 dmg · Reset/Escape";
    cls = "hp-swing-neutral";
  }
  return `<span class="pill ${cls}" data-tip="Perspective-projected HP delta across this labbed rep">${esc(txt)}</span>`;
}

function _findTakeoverResponseById(respId) {
  return (state.annotations || []).find(x => x.id === respId)
    || (state.hubNotes || []).find(x => x.id === respId)
    || null;
}

function openMomentViewer(id, sourceHint = "") {
  const mf = state.activeManifest;
  let item = null;
  let isSelect = sourceHint === "select_mark";
  if (isSelect) {
    item = (state.selectMarks || []).find(x => x.id === id)
      || (state.hubNotes || []).find(x => x.id === id)
      || null;
  } else {
    item = (state.annotations || []).find(x => x.id === id)
      || (state.selectMarks || []).find(x => x.id === id)
      || (state.hubNotes || []).find(x => x.id === id)
      || null;
    if (item && (item.source === "select_mark" || item.entry_type === "select_mark")) {
      isSelect = true;
    }
  }
  if (!item) {
    toast("Moment not found.");
    return;
  }

  const isResp = Boolean(
    sourceHint === "takeover_response" ||
    item.source === "takeover_response" ||
    item.is_response_clip ||
    item.kind === "response" ||
    item.response_clip_url
  );

  if (isResp) {
    const clipUrl = item.response_clip_url || item.video_url || "";
    const overlayUrl = item.response_overlay_url || item.overlay_url || "";
    if (!clipUrl) {
      toast("No rendered clip available for this response yet.");
      return;
    }
    const rRound = roundById(item.round_id);
    const frameCnt = Number(item.frame_count || 0);
    const durS = frameCnt > 1 ? Math.max(0.25, (frameCnt - 1) / 60) : Math.max(0.25, Number(item.clip_end_s || 2.0));
    const clipItem = {
      item_id: item.id,
      source: "takeover_response",
      edit_source: "takeover_response",
      editable: true,
      is_response_clip: true,
      is_standalone_clip: false,
      has_video: true,
      video_url: clipUrl,
      overlay_url: overlayUrl,
      clip_start_s: 0,
      clip_end_s: durS,
      round_start_gf: Number(item.start_gf || 0),
      round_fps: 60,
      quark_id: item.quark_id || (mf && mf.quark_id) || "",
      round_id: Number(item.round_id || 0),
      perspective: Number(item.perspective || (mf && mf.perspective) || 1),
      start_gf: Number(item.start_gf || 0),
      end_gf: Number(item.end_gf || 0),
      anchor_gf: Number(item.anchor_gf || item.start_gf || 0),
      parent_moment_id: item.parent_moment_id || "",
      kind: item.kind || "response",
      note: item.note || "",
      tags: item.tags || [],
      hp_delta_self: item.hp_delta_self,
      hp_delta_opp: item.hp_delta_opp,
      self_char_name: item.self_char_name || (rRound && rRound.self_char_name) || (mf && mf.self_char_name) || "",
      opp_char_name: item.opp_char_name || (rRound && rRound.opp_char_name) || (mf && mf.opp_char_name) || "",
      self_player_name: item.self_player_name || (mf && mf.self_player_name) || "",
      opp_player_name: item.opp_player_name || (mf && mf.opp_player_name) || "",
      self_rank_letter: item.self_rank_letter || (mf && mf.self_rank_letter) || "",
      opp_rank_letter: item.opp_rank_letter || (mf && mf.opp_rank_letter) || "",
      round_label: item.round_label || (rRound ? formatRoundTag(rRound) : (item.round_id ? `R${item.round_id}` : "Labbed Answer")),
      cloud_moment_id: item.cloud_moment_id || null,
      cloud_share_token: item.cloud_share_token || null,
      cloud_share_url: item.cloud_share_url || null,
      cloud_visibility: item.cloud_visibility || null,
      shared_caption: item.shared_caption || "",
      include_note_on_share: Boolean(item.include_note_on_share),
      hide_player_names: Boolean(item.hide_player_names),
    };
    if (typeof openSingleClipWatchModal === "function") {
      openSingleClipWatchModal(clipItem, "Labbed Answer");
    }
    return;
  }

  const aRound = roundById(item.round_id);
  const videoUrl = (aRound && aRound.video_url) || item.video_url || "";
  if (!videoUrl) {
    toast("No rendered video available for this moment yet.");
    return;
  }
  const overlayUrl = (aRound && aRound.overlay_url) || item.overlay_url || "";
  let clipStartS = 0;
  let clipEndS = 2.5;
  let startGf = Number(item.start_gf || 0);
  let endGf = Number(item.end_gf || 0);
  let anchorGf = Number(item.anchor_gf || item.press_gf || item.start_gf || 0);
  let roundStartGf = Number((aRound && aRound.start_gf) || item.round_start_gf || 0);
  let roundFps = Number((aRound && aRound.fps) || item.round_fps || 60);

  if (aRound) {
    const rStart = Number(aRound.start_gf || 0);
    const rSpan = roundGfSpan(aRound);
    const rEnd = rStart + rSpan;
    anchorGf = Number(item.anchor_gf || item.press_gf || item.start_gf || rStart);
    startGf = (item.start_gf && Number(item.start_gf) > 0)
      ? Math.max(rStart, Math.min(rEnd - 1, Number(item.start_gf)))
      : Math.max(rStart, anchorGf - 120);
    endGf = (item.end_gf && Number(item.end_gf) > startGf)
      ? Math.min(rEnd, Number(item.end_gf))
      : Math.min(rEnd, startGf + 150);
    clipStartS = _gfToVideoSec(aRound, startGf);
    clipEndS = Math.max(clipStartS + 0.5, _gfToVideoSec(aRound, endGf));
  } else {
    clipStartS = Math.max(0, Number(item.clip_start_s ?? 0));
    clipEndS = Math.max(clipStartS + 0.5, Number(item.clip_end_s ?? (clipStartS + 2.5)));
  }

  const readyDrill = Boolean(isSelect || (item.queued_drill && item.drill_status === "ready"));
  const clipItem = {
    item_id: item.id,
    source: isSelect ? "select_mark" : "annotation",
    edit_source: isSelect ? "select_mark" : "annotation",
    editable: item.editable !== false,
    is_response_clip: false,
    is_standalone_clip: false,
    has_video: true,
    video_url: videoUrl,
    overlay_url: overlayUrl,
    clip_start_s: clipStartS,
    clip_end_s: clipEndS,
    round_start_gf: roundStartGf,
    round_fps: roundFps,
    quark_id: item.quark_id || (mf && mf.quark_id) || "",
    round_id: Number(item.round_id || (aRound && aRound.round_id) || 0),
    perspective: Number(item.perspective || (mf && mf.perspective) || 1),
    start_gf: startGf,
    end_gf: endGf,
    anchor_gf: anchorGf,
    kind: item.kind || (isSelect ? "select_mark" : "note"),
    note: item.note || "",
    is_default_select_note: Boolean(item.is_default_select_note),
    needs_note: Boolean(item.needs_note),
    tags: item.tags || [],
    self_char_name: item.self_char_name || (aRound && aRound.self_char_name) || (mf && mf.self_char_name) || "",
    opp_char_name: item.opp_char_name || (aRound && aRound.opp_char_name) || (mf && mf.opp_char_name) || "",
    self_player_name: item.self_player_name || (mf && mf.self_player_name) || "",
    opp_player_name: item.opp_player_name || (mf && mf.opp_player_name) || "",
    self_rank_letter: item.self_rank_letter || (mf && mf.self_rank_letter) || "",
    opp_rank_letter: item.opp_rank_letter || (mf && mf.opp_rank_letter) || "",
    round_label: item.round_label || (aRound ? formatRoundTag(aRound) : (item.round_id ? `R${item.round_id}` : "Moment")),
    selection_id: readyDrill ? item.id : null,
    savestate_ready: readyDrill,
    cloud_moment_id: item.cloud_moment_id || null,
    cloud_share_token: item.cloud_share_token || null,
    cloud_share_url: item.cloud_share_url || null,
    cloud_visibility: item.cloud_visibility || null,
    shared_caption: item.shared_caption || "",
    include_note_on_share: Boolean(item.include_note_on_share),
    hide_player_names: Boolean(item.hide_player_names),
  };
  if (typeof openSingleClipWatchModal === "function") {
    openSingleClipWatchModal(clipItem, "Moment Viewer");
  }
}

async function practiceResponseInFbneo(respId, btnEl = null) {
  const r = _findTakeoverResponseById(respId);
  const opts = {
    mode: "takeover",
    note: (r && r.note) || "",
    round_id: Number((r && r.round_id) || 0),
  };
  if (r && r.parent_moment_id) opts.parent_moment_id = r.parent_moment_id;
  await launchDrill(respId, opts, btnEl);
}

// ---------- STUDIO MOMENT CARD LOOP PREVIEWS (HOVER / CLICK-TO-TOGGLE) ----------
let _studioActiveCardPreview = null;
let _studioPinnedCardPreviewId = null;
let _studioCardPreviewRaf = null;

function _gfToVideoSec(r, gf) {
  if (!r) return 0;
  const fps = r.fps || 60;
  const maxVidOff = Math.max(1, (r.frame_count || 1) - 1);
  const gfSpan = roundGfSpan(r);
  const gfOff = Math.max(0, Math.min(gfSpan, gf - r.start_gf));
  const vidOff = (gfSpan === maxVidOff) ? gfOff : (gfOff / gfSpan) * maxVidOff;
  return vidOff / fps;
}

function _syncStudioCardBadge(wrapEl) {
  if (!wrapEl) return;
  const badge = wrapEl.querySelector(".studio-card-preview-badge");
  if (!badge) return;
  const dur = wrapEl.dataset.durLabel || "";
  if (wrapEl.classList.contains("is-pinned")) {
    badge.textContent = `❚❚ Loop · ${dur}`;
  } else if (wrapEl.classList.contains("is-playing")) {
    badge.textContent = `🔁 Looping · ${dur}`;
  } else {
    badge.textContent = `🔁 ${dur}`;
  }
}

function _tickStudioCardPreview() {
  _studioCardPreviewRaf = null;
  const wrapEl = _studioActiveCardPreview;
  if (!wrapEl || !wrapEl.isConnected) {
    _studioActiveCardPreview = null;
    return;
  }
  const vid = wrapEl.querySelector("video");
  if (!vid || vid.paused) return;
  const s = parseFloat(wrapEl.dataset.startS) || 0;
  const e = parseFloat(wrapEl.dataset.endS) || (s + 2);
  if (vid.currentTime >= e || vid.ended) {
    vid.currentTime = s;
    if (vid.paused) vid.play().catch(() => {});
  }
  const fill = wrapEl.querySelector(".studio-card-prog-fill");
  if (fill) {
    const pct = Math.max(0, Math.min(100, ((vid.currentTime - s) / Math.max(0.05, e - s)) * 100));
    fill.style.width = `${pct.toFixed(1)}%`;
  }
  _studioCardPreviewRaf = requestAnimationFrame(_tickStudioCardPreview);
}

function _stopStudioCardPreview(wrapEl = null, resetToStart = true) {
  const target = wrapEl || _studioActiveCardPreview;
  if (_studioCardPreviewRaf) {
    cancelAnimationFrame(_studioCardPreviewRaf);
    _studioCardPreviewRaf = null;
  }
  if (!target) {
    _studioActiveCardPreview = null;
    return;
  }
  const vid = target.querySelector("video");
  const s = parseFloat(target.dataset.startS) || 0;
  if (vid) {
    try { vid.pause(); } catch (_) {}
    if (resetToStart && vid.readyState >= 1) {
      try { vid.currentTime = s; } catch (_) {}
    }
  }
  target.classList.remove("is-playing", "is-pinned");
  const fill = target.querySelector(".studio-card-prog-fill");
  if (fill) fill.style.width = "0%";
  _syncStudioCardBadge(target);
  if (_studioActiveCardPreview === target) _studioActiveCardPreview = null;
  if (_studioPinnedCardPreviewId === target.dataset.previewId) _studioPinnedCardPreviewId = null;
}

function stopAllStudioCardPreviews() {
  _studioPinnedCardPreviewId = null;
  _stopStudioCardPreview(_studioActiveCardPreview, true);
}

function _startStudioCardPreview(wrapEl, pinned = false) {
  if (!wrapEl) return;
  if (_studioActiveCardPreview && _studioActiveCardPreview !== wrapEl) {
    _stopStudioCardPreview(_studioActiveCardPreview, true);
  }
  const vid = wrapEl.querySelector("video");
  if (!vid) return;
  const s = parseFloat(wrapEl.dataset.startS) || 0;
  const e = parseFloat(wrapEl.dataset.endS) || (s + 2);
  vid.muted = true;
  if (vid.currentTime < s || vid.currentTime >= e - 0.05) {
    try { vid.currentTime = s; } catch (_) {}
  }
  _studioActiveCardPreview = wrapEl;
  if (pinned) _studioPinnedCardPreviewId = wrapEl.dataset.previewId || null;
  wrapEl.classList.add("is-playing");
  wrapEl.classList.toggle("is-pinned", Boolean(pinned));
  _syncStudioCardBadge(wrapEl);

  const playPromise = vid.play();
  if (playPromise && typeof playPromise.catch === "function") {
    playPromise.catch(() => {});
  }
  if (_studioCardPreviewRaf) cancelAnimationFrame(_studioCardPreviewRaf);
  _studioCardPreviewRaf = requestAnimationFrame(_tickStudioCardPreview);
}

function onStudioCardPreviewEnter(wrapEl) {
  if (!wrapEl) return;
  if (typeof isMobileMode === "function" && isMobileMode()) return;
  const id = wrapEl.dataset.previewId || "";
  if (_studioPinnedCardPreviewId) {
    // Respect an explicitly clicked/pinned loop on another card until clicked
    if (_studioPinnedCardPreviewId === id) return;
    return;
  }
  _startStudioCardPreview(wrapEl, false);
}

function onStudioCardPreviewLeave(wrapEl) {
  if (!wrapEl) return;
  if (_studioPinnedCardPreviewId && _studioPinnedCardPreviewId === wrapEl.dataset.previewId) return;
  if (_studioActiveCardPreview === wrapEl) {
    _stopStudioCardPreview(wrapEl, true);
  }
}

function toggleStudioCardPreview(wrapEl, ev) {
  if (ev) ev.stopPropagation();
  if (!wrapEl) return;
  const id = wrapEl.dataset.previewId || "";
  if (_studioPinnedCardPreviewId === id && _studioActiveCardPreview === wrapEl) {
    _stopStudioCardPreview(wrapEl, true);
  } else {
    _startStudioCardPreview(wrapEl, true);
  }
}

function _renderStudioCardPreviewHtml(a, aRound) {
  if (!aRound || !aRound.video_url) return "";
  const rStart = Number(aRound.start_gf || 0);
  const rSpan = roundGfSpan(aRound);
  const rEnd = rStart + rSpan;
  const anchorGf = Number(a.anchor_gf || a.press_gf || a.start_gf || rStart);
  const startGf = (a.start_gf && Number(a.start_gf) > 0)
    ? Math.max(rStart, Math.min(rEnd - 1, Number(a.start_gf)))
    : Math.max(rStart, anchorGf - 120);
  const endGf = (a.end_gf && Number(a.end_gf) > startGf)
    ? Math.min(rEnd, Number(a.end_gf))
    : Math.min(rEnd, startGf + 150);
  const startSec = _gfToVideoSec(aRound, startGf);
  const endSec = Math.max(startSec + 0.25, _gfToVideoSec(aRound, endGf));
  const durLabel = `${Math.max(0.2, endSec - startSec).toFixed(1)}s`;
  return `
    <div class="studio-card-preview"
         data-preview-id="${esc(a.id)}"
         data-start-s="${startSec.toFixed(3)}"
         data-end-s="${endSec.toFixed(3)}"
         data-dur-label="${durLabel}"
         data-tip="Hover to preview loop · Click to toggle repeat"
         onmouseenter="onStudioCardPreviewEnter(this)"
         onmouseleave="onStudioCardPreviewLeave(this)"
         onclick="toggleStudioCardPreview(this, event)">
      <video muted playsinline webkit-playsinline preload="metadata" src="${esc(aRound.video_url)}#t=${startSec.toFixed(3)}"></video>
      <div class="studio-card-preview-overlay">
        <div class="studio-card-play-icon">▶</div>
      </div>
      <span class="studio-card-preview-badge mono">🔁 ${durLabel}</span>
      <div class="studio-card-prog"><div class="studio-card-prog-fill"></div></div>
    </div>
  `;
}

function _renderResponseCardPreviewHtml(resp) {
  const clipUrl = resp && resp.response_clip_url;
  if (!clipUrl) return "";
  const frameCnt = Number(resp.frame_count || 0);
  const durSec = frameCnt > 1 ? Math.max(0.25, (frameCnt - 1) / 60) : 2.0;
  const durLabel = `${durSec.toFixed(1)}s`;
  return `
    <div class="studio-card-preview"
         data-preview-id="${esc(resp.id)}"
         data-start-s="0"
         data-end-s="${durSec.toFixed(3)}"
         data-dur-label="${durLabel}"
         data-tip="Hover to preview labbed answer · Click to toggle repeat"
         onmouseenter="onStudioCardPreviewEnter(this)"
         onmouseleave="onStudioCardPreviewLeave(this)"
         onclick="toggleStudioCardPreview(this, event)">
      <video muted playsinline webkit-playsinline preload="metadata" src="${esc(clipUrl)}"></video>
      <div class="studio-card-preview-overlay">
        <div class="studio-card-play-icon">▶</div>
      </div>
      <span class="studio-card-preview-badge mono">🔁 ${durLabel}</span>
      <div class="studio-card-prog"><div class="studio-card-prog-fill"></div></div>
    </div>
  `;
}

function _renderStudioResponseCardHtml(resp, mf, curR, isNested = false) {
  const isEditing = state.studioEditId === resp.id;
  const rRound = roundById(resp.round_id);
  const tsInfo = _formatStudioMomentTimestamp(resp, rRound, mf, state.noteScope);
  const durLabel = resp.frame_count ? `${(Number(resp.frame_count) / 60).toFixed(1)}s` : "";
  const originPill = renderSystemOriginBadgeHtml({ ...resp, source: "takeover_response", kind: "response" });
  const kindCls = "kind-response";
  const hpPill = _formatHpSwingPill(resp);
  const orphanPill = (!isNested && resp.parent_moment_id)
    ? `<span class="pill cached" data-tip="Linked to parent moment ${esc(resp.parent_moment_id)}">↳ Response</span>` : "";
  const sharedPill = (resp.cloud_visibility === "unlisted" && resp.cloud_share_url)
    ? `<span class="pill shared-pill" data-tip="Active unlisted share link — click to copy or manage" onclick="shareStudioMoment(${escJsAttr(resp.id)}, 'takeover_response')">🔗 Shared</span>` : "";
  const tagPills = (!isEditing ? getDisplayTagsForItem(resp) : []).map(t => `<span class="pill note">${esc(t)}</span>`).join("");
  const previewHtml = _renderResponseCardPreviewHtml(resp);
  const openViewerBtn = resp.response_clip_url
    ? `<button class="sm primary" data-tip="Open moment viewer with overlays &amp; note" onclick="openMomentViewer(${escJsAttr(resp.id)}, 'takeover_response')">Open</button>` : "";
  const pracBtn = canTakeoverNow()
    ? `<button class="sm amber practice-btn" data-tip="Launch Replay Takeover from this response's starting savestate" onclick="practiceResponseInFbneo(${escJsAttr(resp.id)}, this)">▶ Practice</button>` : "";
  const respItemPayload = _buildMomentPlaylistItem(resp, mf, rRound || curR, resp.note || "Labbed Answer", getDisplayTagsForItem(resp), "takeover_response");
  const pickerHtml = _renderSavedMomentPickerHtml(respItemPayload);
  const cardCls = `${isNested ? "response-card" : "note-item is-response"}${isEditing ? " is-editing" : ""}`;
  const bodyAndActionsHtml = isEditing
    ? _renderStudioInlineEditorHtml(resp, { isResp: true, isSelectMark: false })
    : `
      ${resp.note ? `<div class="note-body ${kindCls}">${esc(resp.note)}</div>` : ""}
      <div class="row" style="gap:5px; margin-top:6px;">
        ${openViewerBtn}
        ${pracBtn}
        ${resp.response_clip_url ? `<button class="sm" data-tip="Share a revocable link or download MP4" onclick="shareStudioMoment(${escJsAttr(resp.id)}, 'takeover_response')">↗ Share Moment</button>` : ""}
        <button class="sm" data-tip="Edit note and tags in place" onclick="startStudioInlineEdit(${escJsAttr(resp.id)}, 'takeover_response')">Edit</button>
        <button class="sm danger" data-tip="Delete this labbed answer and its local clip files" aria-label="Delete labbed answer" onclick="removeNote(${escJsAttr(resp.id)})">✕</button>
      </div>
    `;
  return `
    <div class="${cardCls}">
      <div class="row-between">
        <div class="row" style="gap:5px;">
          ${originPill}
          ${orphanPill}
          ${sharedPill}
          ${hpPill}
          ${durLabel ? `<span class="pill mono">${esc(durLabel)}</span>` : ""}
        </div>
        ${resp.round_id ? `<span class="note-ts mono" data-tip="${esc(tsInfo.tip)}" onclick="jumpToMoment(${Number(resp.round_id || 0)},${Number(resp.start_gf || 0)},${Number(resp.end_gf || 0)},${Number(resp.anchor_gf || resp.start_gf || 0)},false)">${esc(tsInfo.text)}</span>` : ""}
      </div>
      ${tagPills ? `<div class="row" style="margin-top:5px; gap:4px;">${tagPills}</div>` : ""}
      ${previewHtml}
      ${bodyAndActionsHtml}
      ${pickerHtml}
    </div>
  `;
}

function renderNotesList() {
  const r = curRound();
  const mf = state.activeManifest;
  if (!mf) return;
  const sidebarEl = document.querySelector(".studio-right");
  const savedScrollTop = sidebarEl ? sidebarEl.scrollTop : 0;
  stopAllStudioCardPreviews();

  // Combine annotations + any legacy queue bookmarks (dedup by id)
  const seen = new Set(state.annotations.map(a => a.id));
  const combined = [
    ...state.annotations.map(a => ({ ...a, _src: a.source === "takeover_response" ? "takeover_response" : "annotation" })),
    ...state.selectMarks.filter(s => !seen.has(s.id)).map(s => ({ ...s, _src: "select_mark" })),
  ].sort((a, b) => (a.round_id || 0) - (b.round_id || 0) || (a.anchor_gf || 0) - (b.anchor_gf || 0));

  document.getElementById("sidebarMomentsCnt").textContent = String(combined.length);

  let list = combined;
  if (state.noteScope === "round" && r) {
    list = combined.filter(a => a.round_id === r.round_id);
    document.getElementById("momentsSectionTitle").textContent = `SAVED IN ROUND ${r.match_round || r.round_id}`;
  } else if (state.noteScope === "match") {
    const m = (mf.matches || []).find(x => x.match_id === state.activeMatchId);
    const rids = new Set(m ? m.round_ids : (r ? [r.round_id] : []));
    list = combined.filter(a => rids.has(a.round_id));
    document.getElementById("momentsSectionTitle").textContent = `SAVED IN MATCH ${state.activeMatchId}`;
  } else {
    document.getElementById("momentsSectionTitle").textContent = `ALL SAVED IN SET (${combined.length})`;
  }

  const host = document.getElementById("notesList");

  if (!list.length) {
    host.innerHTML = `<div class="empty-inline">No moments in this ${state.noteScope} yet. Click <strong>+ New Moment</strong> (or press <span class="kbd">M</span>) to save one.</div>`;
  } else {
    // Nest takeover_response moments under their parent moment when the parent is visible in this scope
    const visibleParentIds = new Set(list.filter(x => x._src !== "takeover_response").map(x => x.id));
    const responsesByParent = new Map();
    const nestedRespIds = new Set();
    list.forEach(item => {
      if (item._src === "takeover_response" && item.parent_moment_id && visibleParentIds.has(item.parent_moment_id)) {
        if (!responsesByParent.has(item.parent_moment_id)) responsesByParent.set(item.parent_moment_id, []);
        responsesByParent.get(item.parent_moment_id).push(item);
        nestedRespIds.add(item.id);
      }
    });
    const renderThread = parentId => {
      const kids = responsesByParent.get(parentId) || [];
      if (!kids.length) return "";
      return `
        <div class="response-thread">
          <div class="response-thread-head">✅ Labbed Responses (${kids.length})</div>
          ${kids.map(k => _renderStudioResponseCardHtml(k, mf, r, true)).join("")}
        </div>
      `;
    };

    host.innerHTML = list.filter(a => !nestedRespIds.has(a.id)).map(a => {
      if (a._src === "takeover_response") {
        return _renderStudioResponseCardHtml(a, mf, r, false);
      }
      const isEditing = state.studioEditId === a.id;
      const aRound = roundById(a.round_id);
      const tsInfo = _formatStudioMomentTimestamp(a, aRound, mf, state.noteScope);
      const previewHtml = _renderStudioCardPreviewHtml(a, aRound);
      const threadHtml = renderThread(a.id);

      if (a._src === "select_mark") {
        const needs = Boolean(a.needs_note);
        const selKindCls = `kind-${esc(needs ? "select_mark" : (a.kind || "select_mark"))}`;
        const displayTags = getDisplayTagsForItem(a);
        const tagPills = (!isEditing ? displayTags : []).map(t => `<span class="pill note">${esc(t)}</span>`).join("");
        const sharedPill = (a.cloud_visibility === "unlisted" && a.cloud_share_url)
          ? `<span class="pill shared-pill" data-tip="Active unlisted share link — click to copy or manage" onclick="shareStudioMoment(${escJsAttr(a.id)}, 'select_mark')">🔗 Shared</span>`
          : "";
        const openViewerBtn = (aRound && aRound.video_url)
          ? `<button class="sm primary" data-tip="Open moment viewer with overlays &amp; note" onclick="openMomentViewer(${escJsAttr(a.id)}, 'select_mark')">Open</button>`
          : "";
        const pracBtn = canTakeoverNow()
          ? `<button class="sm amber practice-btn" data-tip="Launch Replay Takeover in FBNeo for this moment" onclick="practiceSavedMoment(${escJsAttr(a.id)},'select_mark',${escJsAttr(mf.quark_id)},${a.start_gf || 0},${a.end_gf || 0},${a.anchor_gf || 0},${mf.perspective},'Bookmark',this)">▶ Practice</button>`
          : "";
        const selItemPayload = _buildMomentPlaylistItem(a, mf, aRound || r, needs ? "Bookmark" : (a.note || ""), displayTags, "select_mark");
        const pickerHtml = _renderSavedMomentPickerHtml(selItemPayload);
        const bodyAndActionsHtml = isEditing
          ? _renderStudioInlineEditorHtml(a, { isResp: false, isSelectMark: true })
          : `
            <div class="note-body ${selKindCls} ${needs ? "is-placeholder" : ""}">${esc(needs ? "Saved bookmark." : a.note)}</div>
            <div class="row" style="gap:5px;">
              ${openViewerBtn}
              ${pracBtn}
              ${(aRound && aRound.video_url) ? `<button class="sm" data-tip="Share a revocable link or download MP4" onclick="shareStudioMoment(${escJsAttr(a.id)}, 'select_mark')">↗ Share Moment</button>` : ""}
              <button class="sm ${openViewerBtn ? "" : "primary"}" data-tip="Annotate this bookmark in place" onclick="startStudioInlineEdit(${escJsAttr(a.id)}, 'select_mark')">${needs ? "Add Note" : "Edit"}</button>
              <button class="sm ghost" data-tip="Dismiss this bookmark" onclick="dismissSelectMarkInStudio(${escJsAttr(a.id)})">Dismiss</button>
            </div>
          `;
        return `
          <div class="note-item is-select${isEditing ? " is-editing" : ""}">
            <div class="row-between">
              <div class="row" style="gap:6px;">
                <span class="pill kind-select_mark">Bookmark</span>
                ${sharedPill}
                <span class="note-ts mono" data-tip="${esc(tsInfo.tip)}" onclick="jumpToMoment(${a.round_id},${a.start_gf || 0},${a.end_gf || 0},${a.anchor_gf || 0},false)">${esc(tsInfo.text)}</span>
              </div>
              ${needs ? `<span style="font-size:11px; font-weight:700; color:var(--amber-fg);">Needs note</span>` : ""}
            </div>
            ${tagPills ? `<div class="row" style="margin-top:5px; gap:4px;">${tagPills}</div>` : ""}
            ${previewHtml}
            ${bodyAndActionsHtml}
            ${pickerHtml}
            ${threadHtml}
          </div>
        `;
      }

      const kindCls = `kind-${esc(a.kind || "note")}`;
      const originPill = renderSystemOriginBadgeHtml(a);
      const displayTags = getDisplayTagsForItem(a);
      const tagPills = (!isEditing ? displayTags : []).map(t => `<span class="pill note">${esc(t)}</span>`).join("");
      const sharedPill = (a.cloud_visibility === "unlisted" && a.cloud_share_url)
        ? `<span class="pill shared-pill" data-tip="Active unlisted share link — click to copy or manage" onclick="shareStudioMoment(${escJsAttr(a.id)}, 'annotation')">🔗 Shared</span>`
        : "";
      const openViewerBtn = (aRound && aRound.video_url)
        ? `<button class="sm primary" data-tip="Open moment viewer with overlays &amp; note" onclick="openMomentViewer(${escJsAttr(a.id)}, 'annotation')">Open</button>`
        : "";
      const pracTip = "Launch Replay Takeover in FBNeo to practice this moment";
      const safeNoteLabel = (a.note || displayTags[0] || "Moment").slice(0, 80);
      const pracBtn = canTakeoverNow()
        ? `<button class="sm amber practice-btn" data-tip="${pracTip}" onclick="practiceSavedMoment(${escJsAttr(a.id)},'annotation',${escJsAttr(mf.quark_id)},${a.start_gf || 0},${a.end_gf || 0},${a.anchor_gf || 0},${mf.perspective},${escJsAttr(safeNoteLabel)},this)">▶ Practice</button>`
        : "";
      const annItemPayload = _buildMomentPlaylistItem(a, mf, aRound || r, a.note || displayTags[0] || "Moment", displayTags, "annotation");
      const pickerHtml = _renderSavedMomentPickerHtml(annItemPayload);
      const bodyAndActionsHtml = isEditing
        ? _renderStudioInlineEditorHtml(a, { isResp: false, isSelectMark: false })
        : `
          ${a.note ? `<div class="note-body ${kindCls}">${esc(a.note)}</div>` : ""}
          <div class="row" style="gap:5px; margin-top:6px;">
            ${openViewerBtn}
            ${pracBtn}
            ${(aRound && aRound.video_url) ? `<button class="sm" data-tip="Share a revocable link or download MP4" onclick="shareStudioMoment(${escJsAttr(a.id)}, 'annotation')">↗ Share Moment</button>` : ""}
            <button class="sm" data-tip="Edit this moment note in place" onclick="startStudioInlineEdit(${escJsAttr(a.id)}, 'annotation')">Edit</button>
            <button class="sm danger" data-tip="Delete this moment note" aria-label="Delete moment note" onclick="removeNote(${escJsAttr(a.id)})">✕</button>
          </div>
        `;
      return `
        <div class="note-item${isEditing ? " is-editing" : ""}">
          <div class="row-between">
            <div class="row" style="gap:6px;">
              ${originPill}
              <span class="note-ts mono" data-tip="${esc(tsInfo.tip)}" onclick="jumpToMoment(${a.round_id},${a.start_gf || 0},${a.end_gf || 0},${a.anchor_gf || 0},false)">${esc(tsInfo.text)}</span>
              ${sharedPill}
            </div>
          </div>
          ${tagPills ? `<div class="row" style="margin-top:5px; gap:4px;">${tagPills}</div>` : ""}
          ${previewHtml}
          ${bodyAndActionsHtml}
          ${pickerHtml}
          ${threadHtml}
        </div>
      `;
    }).join("");
  }

  // Other rounds summary hint
  const hint = document.getElementById("otherRoundsHint");
  if (hint) {
    const other = combined.filter(x => !list.some(y => y.id === x.id));
    if (other.length > 0 && state.noteScope !== "set") {
      const rNames = [...new Set(other.map(x => `R${x.round_id}`))].slice(0, 4).join(" / ");
      hint.textContent = `+ ${other.length} more moment${other.length === 1 ? "" : "s"} in ${rNames} (click to show all)`;
      hint.onclick = () => setNoteScope("set");
      hint.classList.remove("hidden");
    } else {
      hint.classList.add("hidden");
    }
  }

  if (sidebarEl) sidebarEl.scrollTop = savedScrollTop;
}

// ---------- SET NOTES & META ----------
function renderStudioSetMetaCard() {
  const host = document.getElementById("studioSetMetaCard");
  const mf = state.activeManifest;
  if (!host || !mf) return;
  const matchCount = (mf.matches || []).length;
  const scoreTxt = matchCount > 0 && mf.set_self_wins != null ? `Set ${mf.set_self_wins}–${mf.set_opp_wins} · ` : "";
  const proEnabled = isProCorpusEnabled();
  host.innerHTML = `
    <div style="font-weight:700; font-size:13.5px; margin-bottom:4px;">Match Metadata</div>
    <div style="font-size:12.5px; color:var(--muted);${proEnabled ? " margin-bottom:10px;" : ""}">
      ${esc(scoreTxt)}${mf.round_count} rounds · ${formatDuration(mf.total_duration_s)} · Viewing P${mf.perspective} · <span class="mono">${esc(mf.quark_id)}</span>
    </div>
    ${proEnabled ? `<button class="sm primary" onclick="triggerMatchBreakdown(${escJsAttr(mf.quark_id)}, ${Number(mf.perspective || 1)})">✨ Run / Open AI Match Breakdown</button>` : ""}
  `;
}

async function saveSetNote() {
  if (!state.activeManifest) return;
  const note = document.getElementById("setNoteInput").value;
  const tags = getTagPickerTags("setNote");
  try {
    await api("/api/set-notes", {
      method: "POST",
      body: JSON.stringify({
        quark_id: state.activeManifest.quark_id,
        note,
        tags,
        owner: state.username || DEFAULT_USER,
      }),
    });
    closeAllTagPickers();
    toast("Set takeaway saved");
    loadHubNotes(true);
  } catch (err) {
    toast("Save failed: " + err.message);
  }
}

// ---------- IN-STUDIO TOP-RANK SEARCH ----------
function runProSearchForRange(roundId, startGf, endGf) {
  if (!isProCorpusEnabled()) {
    toast("Top-Rank Search is on the roadmap.");
    return;
  }
  state.rangeStartGf = startGf;
  state.rangeEndGf = endGf;
  updateRangeBanner();
  updateHudAndTimeline();
  runProSearchFromStudio(roundId, startGf, endGf);
}

async function runProSearchFromStudio(explicitRoundId = null, explicitStart = null, explicitEnd = null) {
  if (!isProCorpusEnabled()) {
    toast("Top-Rank Search is on the roadmap.");
    return;
  }
  const mf = state.activeManifest;
  const r = curRound();
  if (!mf || !r) return;
  setStudioSidebarTab("pro_search");

  if (!state.capabilities.has_pro_corpus) {
    document.getElementById("proSearchResults").innerHTML = `<div class="empty-inline">Top-rank corpus Parquets not found in <code>data/parquet/</code>.</div>`;
    return;
  }

  const anchor = currentGf();
  const sGf = explicitStart || state.rangeStartGf || Math.max(r.start_gf, anchor - 60);
  const eGf = explicitEnd || state.rangeEndGf || Math.min(r.end_gf, anchor + 30);
  const tier = document.getElementById("proSearchTier") ? document.getElementById("proSearchTier").value : "";
  const host = document.getElementById("proSearchResults");
  const btn = document.getElementById("btnRunProSearch");

  if (btn) { btn.disabled = true; btn.textContent = "Searching..."; }
  host.innerHTML = `<div class="empty-inline">Searching ${state.capabilities.pro_corpus_sets || ""} top-rank sets for gf${sGf}–${eGf}...</div>`;

  try {
    const res = await api("/api/pro-search", {
      method: "POST",
      body: JSON.stringify({
        quark_id: mf.quark_id,
        perspective: mf.perspective,
        round_id: explicitRoundId || r.round_id,
        start_gf: sGf,
        end_gf: eGf,
        top_k: 6,
        tier_override: tier || null,
      }),
    });
    state.proSearchResults = res;
    renderProSearchResults();
  } catch (err) {
    host.innerHTML = `<div class="empty-inline" style="color:var(--rose-fg);">Search failed: ${esc(err.message)}</div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🔍 Search"; }
  }
}

function renderProSearchResults() {
  const host = document.getElementById("proSearchResults");
  const res = state.proSearchResults;
  if (!host || !res) return;
  const matches = res.matches || [];
  if (!matches.length) {
    host.innerHTML = `<div class="empty-inline">No matching top-rank sequences found for this range. Try widening the bracket or switching tier to Universal.</div>`;
    return;
  }

  const tierLbl = { exact_matchup: "Exact Matchup", same_self_char: "Same Character", universal: "Universal" }[res.retrieval_tier] || res.retrieval_tier;
  const headerHtml = `<div style="font-size:11.5px; color:var(--muted); margin-bottom:8px;">
    Found <strong>${matches.length}</strong> top-rank sequences · Tier: <strong>${esc(tierLbl)}</strong> · Scanned ${res.rounds_scanned} rounds in ${res.elapsed_s}s
  </div>`;

  host.innerHTML = headerHtml + matches.map((m, idx) => {
    const outVal = Number(m.outcome_value || 0);
    const outCls = outVal >= 0 ? "ready" : "err";
    const outTxt = `${outVal >= 0 ? "+" : ""}${Math.round(outVal * 100)}% HP swing`;
    const clipHtml = m.has_clip && m.clip_url
      ? `<video controls playsinline muted loop preload="metadata" src="${esc(m.clip_url)}"></video>`
      : "";
    const renderBtn = (!m.has_clip && state.capabilities.can_render)
      ? `<button class="sm primary" id="btnRenderPro_${idx}" onclick="renderProClipOnDemand(${idx})">Render Clip</button>`
      : "";
    const pracBtn = canTakeoverNow()
      ? `<button class="sm amber practice-btn" data-tip="Launch Replay Takeover in FBNeo for this top-rank clip" onclick="launchTakeoverPayload({quark_id:${escJsAttr(m.quark_id)},frame:${m.mid_gf},player:${m.perspective},label:${escJsAttr(`Top-Rank: ${m.self_player_name} (${m.self_char_name})`)},note:''},this)">▶ Practice Top-Rank Clip</button>`
      : "";
    const proLabel = `${formatMatchLabelText(m, { showRank: false })} — ${m.action_coarse || "Top-Rank Answer"}`;

    return `
      <div class="pro-card">
        <div class="row-between">
          <span class="pill ready">${m.similarity_pct}% match</span>
          <span class="pill ${outCls}">${outTxt}</span>
        </div>
        <div style="font-weight:700; font-size:13px; margin-top:6px;">
          ${formatMatchLabelHtml(m)}
        </div>
        <div style="font-size:12px; color:var(--muted); margin-top:2px;">
          Action: <strong>${esc(m.action_coarse || "—")}</strong> · R${m.round_id} · gf${m.mid_gf}
        </div>
        ${clipHtml}
        <div class="row" style="margin-top:8px; gap:6px;">
          ${renderBtn}
          ${pracBtn}
          <button class="sm" onclick="openAddToPlaylistModal({item_id:${escJsAttr(`pro_${m.quark_id}_gf${m.mid_gf}`)},source:'pro_clip',quark_id:${escJsAttr(m.quark_id)},round_id:${m.round_id || 0},perspective:${m.perspective || 1},start_gf:${m.start_gf || Math.max(1, m.mid_gf - 120)},end_gf:${m.end_gf || (m.mid_gf + 150)},anchor_gf:${m.mid_gf},label:${escJsAttr(proLabel.slice(0, 120))}})">+ Add to Playlist</button>
        </div>
      </div>
    `;
  }).join("");
}

async function renderProClipOnDemand(idx) {
  const m = state.proSearchResults && state.proSearchResults.matches && state.proSearchResults.matches[idx];
  if (!m) return;
  const btn = document.getElementById(`btnRenderPro_${idx}`);
  if (btn) { btn.disabled = true; btn.textContent = "Rendering..."; }
  try {
    const res = await api("/api/pro-search/render-clip", {
      method: "POST",
      body: JSON.stringify({ quark_id: m.quark_id, frame: m.mid_gf, label: m.action_coarse || "" }),
    });
    m.has_clip = res.has_clip;
    m.clip_url = res.clip_url;
    m.has_savestate = res.has_savestate;
    renderProSearchResults();
    toast("Top-Rank clip ready!");
  } catch (err) {
    toast("Clip render failed: " + err.message);
    if (btn) { btn.disabled = false; btn.textContent = "Render Clip"; }
  }
}
