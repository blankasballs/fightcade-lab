/* 2-Column Desktop Replay Studio (#replay/<qid>): Stage, Overlays, Timeline, Moments, Set Notes & Pro Search */

const video = document.getElementById("roundVideo");
const canvas = document.getElementById("timelineCanvas");
const ctx = canvas ? canvas.getContext("2d") : null;
const ovCanvas = document.getElementById("overlayCanvas");

function curRound() {
  if (!state.activeManifest || !state.activeManifest.rounds.length) return null;
  return state.activeManifest.rounds[state.activeRoundIdx] || state.activeManifest.rounds[0];
}

function currentGf() {
  const r = curRound();
  if (!r) return 0;
  const maxOff = Math.max(0, (r.frame_count || 1) - 1);
  const off = Math.max(0, Math.min(maxOff, Math.round((video.currentTime || 0) * (r.fps || 60))));
  return r.start_gf + off;
}

function seekToGf(gf, autoPlay = false) {
  const r = curRound();
  if (!r) return;
  const fps = r.fps || 60;
  const off = Math.max(0, Math.min(Math.max(0, r.frame_count - 1), gf - r.start_gf));
  video.currentTime = off / fps;
  state.currentFrameOffset = off;
  updateHudAndTimeline();
  if (autoPlay) {
    video.play().catch(() => {});
  }
}

async function openMatch(qid, explicitPersp = null, jumpTarget = null) {
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  if (typeof closeWatchMode === "function") closeWatchMode();

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
    clearRange();
    resetComposerDraft();

    state.activeView = "studio";
    const targetHash = `#replay/${encodeURIComponent(qid)}`;
    if (!window.location.hash.startsWith(targetHash)) {
      history.replaceState(null, "", window.location.pathname + window.location.search + targetHash);
    }
    syncTopNav();

    applyStudioHeader(state.activeManifest);
    document.getElementById("setNoteInput").value = (state.setNoteObj && state.setNoteObj.note) || "";
    document.getElementById("setNoteTagsInput").value = ((state.setNoteObj && state.setNoteObj.tags) || []).join(", ");

    let startIdx = 0;
    if (jumpTarget && jumpTarget.round_id) {
      const foundIdx = (state.activeManifest.rounds || []).findIndex(r => r.round_id === Number(jumpTarget.round_id));
      if (foundIdx >= 0) startIdx = foundIdx;
    }
    state.activeMatchId = (state.activeManifest.rounds[startIdx] && state.activeManifest.rounds[startIdx].match_id) || 1;

    renderStudioGoalsStrip();
    renderComposerPlaylistSelect();
    renderComposerControls();
    renderStudioSetMetaCard();
    syncProCorpusGateUi();
    setStudioSidebarTab("moments");
    renderMatchAndRoundTabs();
    loadRound(startIdx);

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
    toast("Could not open replay: " + err.message);
  }
}

function applyStudioHeader(mf) {
  const selfChar = (mf.self_chars && mf.self_chars.length) ? mf.self_chars.join("/") : mf.self_char_name;
  const oppChar = (mf.opp_chars && mf.opp_chars.length) ? mf.opp_chars.join("/") : mf.opp_char_name;
  const rk = mf.opp_rank_letter ? ` [${mf.opp_rank_letter}]` : "";
  const crumb = document.getElementById("crumbMatchup");
  if (crumb) {
    crumb.innerHTML = `${esc(mf.self_player_name)} <span style="color:var(--accent-fg);">(${esc(selfChar)})</span> <span style="color:var(--muted-dim);">vs</span> ${esc(mf.opp_player_name)}${rk ? ` <span class="rank">${esc(mf.opp_rank_letter)}</span>` : ""} <span style="color:var(--rose-fg);">(${esc(oppChar)})</span>`;
  }
  const flipBtn = document.getElementById("btnFlipSide");
  if (flipBtn) flipBtn.textContent = `⇄ Flip P${mf.perspective === 1 ? 2 : 1}`;
  updateTimelineLegend(mf, curRound());
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
  el.innerHTML = `
    <span><span class="lg-line" style="background:#38bdf8;"></span>${esc(selfLbl)} HP</span>
    <span><span class="lg-line" style="background:#fb7185;"></span>${esc(oppLbl)} HP</span>
    <span><span class="lg-tick" style="background:#f97316;"></span>Took Dmg</span>
    <span><span class="lg-tick" style="background:#34d399;"></span>Dealt Dmg</span>
    <span><span class="lg-tick" style="background:#fbbf24;"></span>Super</span>
    <span><span class="lg-tick" style="background:#38bdf8;"></span>Select</span>
    <span><span class="lg-dot" style="background:#c084fc;"></span>Notes</span>
  `;
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

function copyStudioDeepLink() {
  const mf = state.activeManifest;
  const r = curRound();
  if (!mf || !r) return;
  const url = `${window.location.origin}/#replay/${encodeURIComponent(mf.quark_id)}?r=${r.round_id}&gf=${currentGf()}`;
  copyText(url, `Copied deep-link (R${r.round_id} · gf${currentGf()})`);
}

function exportCurrentStudioClipBundle() {
  const mf = state.activeManifest;
  const r = curRound();
  if (!mf || !r) return;
  const anchor = currentGf();
  const startGf = state.rangeStartGf != null ? state.rangeStartGf : Math.max(r.start_gf, anchor - 180);
  const endGf = state.rangeEndGf != null ? state.rangeEndGf : Math.min(r.end_gf, anchor + 120);
  prepareLocalShareBundle({
    quark_id: mf.quark_id,
    round_id: r.round_id,
    start_gf: startGf,
    end_gf: endGf,
    anchor_gf: anchor,
    perspective: mf.perspective,
    title: `${mf.self_char_name || ""} vs ${mf.opp_char_name || ""} · R${r.round_id}`,
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
      const rids = new Set(m.round_ids || []);
      const hasNotes = state.annotations.some(a => rids.has(a.round_id));
      const hasSel = state.selectMarks.some(s => rids.has(s.round_id)) ||
        mf.rounds.some(r => rids.has(r.round_id) && (r.markers || []).some(mk => mk.type === "select_mark"));
      const wlCls = m.self_won === true ? "win" : (m.self_won === false ? "loss" : "");
      return `
        <button class="match-pill ${wlCls} ${m.match_id === state.activeMatchId ? "active" : ""}" onclick="selectMatch(${m.match_id})">
          <span>M${m.match_id} ${m.self_rounds_won}–${m.opp_rounds_won}</span>
          ${hasNotes ? `<span class="r-dot" title="Has notes"></span>` : ""}
          ${hasSel ? `<span class="r-dot" style="background:#38bdf8;" title="Select mark"></span>` : ""}
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

  roundHost.innerHTML = visibleRounds.map(({ r, idx }) => {
    const hasNotes = state.annotations.some(a => a.round_id === r.round_id);
    const hasSel = state.selectMarks.some(s => s.round_id === r.round_id) ||
      (r.markers || []).some(m => m.type === "select_mark");
    const won = r.self_won === true;
    const lost = r.self_won === false;
    const wl = won ? "W" : (lost ? "L" : "D");
    return `
      <button class="round-pill ${idx === state.activeRoundIdx ? "active" : ""}" onclick="loadRound(${idx})">
        <span>R${r.match_round || r.round_id}</span>
        <span style="font-size:10.5px; color:${won ? "var(--emerald-fg)" : (lost ? "var(--rose-fg)" : "var(--amber-fg)")};">${wl}</span>
        ${hasNotes ? `<span class="r-dot" title="Has notes"></span>` : ""}
        ${hasSel ? `<span class="r-dot" style="background:#38bdf8;" title="Select mark"></span>` : ""}
      </button>
    `;
  }).join("");
}

function loadRound(idx) {
  const mf = state.activeManifest;
  if (!mf || !mf.rounds[idx]) return;
  state.activeRoundIdx = idx;
  state.activeMatchId = mf.rounds[idx].match_id || state.activeMatchId;
  state.playStopGf = null;
  clearRange();
  const r = mf.rounds[idx];
  video.pause();
  video.src = r.video_url;
  video.load();
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
        : (!hasGrouflon ? "Hitboxes require Grouflon (optional)" : "No overlay data");
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

function setRate(rate) {
  video.playbackRate = rate;
  document.querySelectorAll(".speed-btn[data-rate]").forEach(b => b.classList.toggle("active", Number(b.dataset.rate) === rate));
}

function togglePlay() {
  if (video.paused) video.play().catch(() => {});
  else video.pause();
}

function stepFrames(delta) {
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

function toggleEventChipsRow() {
  const el = document.getElementById("eventChips");
  const btn = document.getElementById("btnToggleEvents");
  if (!el) return;
  const show = el.classList.toggle("hidden");
  if (btn) btn.textContent = show ? "Events ▾" : "Events ▴";
}

function renderEventChips() {
  const r = curRound();
  const host = document.getElementById("eventChips");
  if (!host) return;
  if (!r || !(r.markers || []).length) {
    host.innerHTML = `<span style="font-size:12px; color:var(--muted);">No event markers in this round</span>`;
    return;
  }
  const important = r.markers.filter(m => ["dmg_self", "select_mark", "super", "dmg_opp"].includes(m.type)).slice(0, 20);
  host.innerHTML = important.map(m => {
    const sec = (m.offset_f / (r.fps || 60)).toFixed(1);
    return `<button class="chip ev-${esc(m.type)}" onclick="seekToGf(${Math.max(r.start_gf, m.gf - 90)}, true)">${esc(m.label)} · ${sec}s</button>`;
  }).join("");
}

function updateHudAndTimeline() {
  const r = curRound();
  if (!r) return;
  const fps = r.fps || 60;
  const gf = currentGf();
  const off = gf - r.start_gf;
  const sec = (off / fps).toFixed(2);
  document.getElementById("frameHud").textContent = `${formatRoundTag(r)} · ${sec}s · gf${gf}`;
  document.getElementById("btnPlayPause").textContent = video.paused ? "▶" : "❚❚";
  drawTimeline(r, off);
  drawOverlayOnCanvas(ovCanvas, state.activeOverlay, r.start_gf, off, state.overlayOpts);
  if (!state.editingId) updateComposerAnchorBadge();
}

function drawTimeline(r, curOff) {
  if (!ctx || !canvas) return;
  const w = canvas.width;
  const h = canvas.height;
  const total = Math.max(1, r.frame_count || 1);
  const light = isLightTheme();
  ctx.clearRect(0, 0, w, h);

  ctx.fillStyle = light ? "#eef2f7" : "#0b101d";
  ctx.fillRect(0, 0, w, h);

  // Highlighted amber [Start..End] bracket
  if (state.rangeStartGf != null || state.rangeEndGf != null) {
    const sOff = Math.max(0, (state.rangeStartGf ?? (r.start_gf + curOff)) - r.start_gf);
    const eOff = Math.min(total, (state.rangeEndGf ?? (r.start_gf + curOff)) - r.start_gf);
    const x1 = (Math.min(sOff, eOff) / total) * w;
    const x2 = (Math.max(sOff, eOff) / total) * w;
    ctx.fillStyle = light ? "rgba(217, 119, 6, 0.20)" : "rgba(251, 191, 36, 0.18)";
    ctx.fillRect(x1, 0, Math.max(4, x2 - x1), h);
    ctx.strokeStyle = light ? "#d97706" : "#fbbf24";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(x1, 0); ctx.lineTo(x1, h);
    ctx.moveTo(x2, 0); ctx.lineTo(x2, h);
    ctx.stroke();
  }

  // HP curves (projected by perspective.py: pt[1] = Self HP, pt[2] = Opp HP)
  const curve = r.hp_curve || [];
  if (curve.length > 1) {
    ctx.beginPath();
    ctx.strokeStyle = light ? "rgba(2, 132, 199, 0.92)" : "rgba(56, 189, 248, 0.9)";
    ctx.lineWidth = 2.6;
    curve.forEach((pt, i) => {
      const x = (pt[0] / total) * w;
      const y = h - 18 - pt[1] * (h - 36);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();

    ctx.beginPath();
    ctx.strokeStyle = light ? "rgba(225, 29, 72, 0.85)" : "rgba(251, 113, 133, 0.8)";
    ctx.lineWidth = 2.2;
    curve.forEach((pt, i) => {
      const x = (pt[0] / total) * w;
      const y = h - 18 - pt[2] * (h - 36);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  // Event ticks along bottom rail
  const colors = light
    ? { dmg_self: "#ea580c", dmg_opp: "#059669", super: "#d97706", select_mark: "#0284c7", kd_self: "#dc2626", kd_opp: "#65a30d" }
    : { dmg_self: "#f97316", dmg_opp: "#34d399", super: "#fbbf24", select_mark: "#38bdf8", kd_self: "#ef4444", kd_opp: "#a3e635" };
  (r.markers || []).forEach(m => {
    const x = (m.offset_f / total) * w;
    ctx.fillStyle = colors[m.type] || "#94a3b8";
    const tickH = m.type === "select_mark" ? 26 : (m.type === "dmg_self" ? 20 : 14);
    ctx.fillRect(x - 1.5, h - tickH, 3, tickH);
  });

  // Select mark pins + saved note pins along top rail
  state.selectMarks.filter(s => s.round_id === r.round_id).forEach(s => {
    const x = (((s.anchor_gf || s.press_gf || s.start_gf) - r.start_gf) / total) * w;
    ctx.fillStyle = light ? "#0284c7" : "#38bdf8";
    ctx.beginPath();
    ctx.arc(x, 12, 5.5, 0, Math.PI * 2);
    ctx.fill();
  });
  state.annotations.filter(a => a.round_id === r.round_id).forEach(a => {
    const x = ((a.anchor_gf - r.start_gf) / total) * w;
    ctx.fillStyle = a.queued_drill ? (light ? "#d97706" : "#fbbf24") : (light ? "#9333ea" : "#c084fc");
    ctx.beginPath();
    ctx.arc(x, 12, 6, 0, Math.PI * 2);
    ctx.fill();
  });

  // Playhead
  const px = (curOff / total) * w;
  ctx.fillStyle = light ? "#0f172a" : "#ffffff";
  ctx.fillRect(px - 1.5, 0, 3, h);
}

// Timeline pointer scrubbing
let scrubbing = false;
function scrubFromEvent(e) {
  const r = curRound();
  if (!r) return;
  const rect = canvas.getBoundingClientRect();
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  const gf = r.start_gf + Math.round(frac * Math.max(1, r.frame_count - 1));
  seekToGf(gf, false);
}
if (canvas) {
  canvas.addEventListener("pointerdown", e => { scrubbing = true; video.pause(); canvas.setPointerCapture(e.pointerId); scrubFromEvent(e); });
  canvas.addEventListener("pointermove", e => { if (scrubbing) scrubFromEvent(e); });
  canvas.addEventListener("pointerup", () => { scrubbing = false; });
}

if (video) {
  video.addEventListener("timeupdate", () => {
    updateHudAndTimeline();
    if (state.playStopGf != null && currentGf() >= state.playStopGf) {
      video.pause();
      state.playStopGf = null;
    }
  });
  video.addEventListener("play", () => updateHudAndTimeline());
  video.addEventListener("pause", () => updateHudAndTimeline());
  video.addEventListener("loadedmetadata", () => updateHudAndTimeline());
  if ("requestVideoFrameCallback" in HTMLVideoElement.prototype) {
    const onVfc = () => {
      updateHudAndTimeline();
      video.requestVideoFrameCallback(onVfc);
    };
    video.requestVideoFrameCallback(onVfc);
  }
}

// ---------- RANGE & CONTEXTUAL PRACTICE ----------
function markRangeBound(which) {
  const r = curRound();
  if (!r) return;
  const gf = currentGf();
  if (which === "start") {
    state.rangeStartGf = gf;
    if (state.rangeEndGf != null && state.rangeEndGf <= gf) state.rangeEndGf = gf + 60;
  } else {
    state.rangeEndGf = gf;
    if (state.rangeStartGf != null && state.rangeStartGf >= gf) state.rangeStartGf = Math.max(r.start_gf, gf - 60);
  }
  updateRangeBanner();
  updateHudAndTimeline();
  updateComposerAnchorBadge();
}

function clearRange() {
  state.rangeStartGf = null;
  state.rangeEndGf = null;
  updateRangeBanner();
  updateComposerAnchorBadge();
}

function updateRangeBanner() {
  const banner = document.getElementById("rangeStatus");
  const clearBtn = document.getElementById("btnClearRange");
  const proPill = document.getElementById("proSearchRangePill");
  if (!banner || !clearBtn) return;
  if (state.rangeStartGf == null && state.rangeEndGf == null) {
    banner.classList.add("hidden");
    clearBtn.classList.add("hidden");
    if (proPill) proPill.textContent = `gf${currentGf()}`;
    return;
  }
  clearBtn.classList.remove("hidden");
  banner.classList.remove("hidden");
  const s = state.rangeStartGf ?? currentGf();
  const e = state.rangeEndGf ?? (s + 90);
  const dur = Math.max(0.1, (e - s) / 60).toFixed(1);
  banner.textContent = `Range: gf${s}–${e} (${dur}s)`;
  if (proPill) proPill.textContent = `gf${s}–${e} (${dur}s)`;
}

async function practiceCurrentFrame() {
  const mf = state.activeManifest;
  const r = curRound();
  if (!mf || !r) return;
  const anchor = currentGf();
  const startGf = state.rangeStartGf != null ? state.rangeStartGf : Math.max(r.start_gf, anchor - 180);
  const endGf = state.rangeEndGf != null ? state.rangeEndGf : Math.min(r.end_gf, anchor + 90);
  const btn = document.getElementById("btnPracticeTakeover");
  if (btn) { btn.disabled = true; btn.textContent = "Launching..."; }
  try {
    await launchTakeoverPayload({
      quark_id: mf.quark_id,
      frame: anchor,
      start_gf: startGf,
      end_gf: endGf,
      player: mf.perspective,
      bake_if_missing: true,
      label: `${mf.self_char_name} vs ${mf.opp_char_name} R${r.round_id} gf${anchor}`,
    });
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "▶ Practice Takeover"; }
  }
}

// ---------- SIDEBAR SUB-TABS ----------
function setStudioSidebarTab(tab) {
  if (tab === "pro_search" && !isProCorpusEnabled()) {
    toast("Pro Search is on the roadmap.");
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

// ---------- FOCUS GOALS QUICK-STRIP & INLINE COMPOSER ----------
function renderStudioGoalsStrip() {
  const host = document.getElementById("studioGoalsStrip");
  if (!host) return;
  const goals = state.focusGoals || [];
  if (!goals.length) {
    host.classList.add("hidden");
    return;
  }
  host.classList.remove("hidden");
  host.innerHTML = `
    <div class="section-eyebrow">LINK TO PINNED FOCUS GOAL</div>
    <div class="goals-chip-row">
      ${goals.map(g => {
        const on = state.selectedGoalIds.has(g.id);
        return `<button type="button" class="goal-chip ${on ? "on" : ""}" onclick="toggleStudioGoalChip('${esc(g.id)}')">
          <span class="g-dot"></span><span>${esc(g.title)}</span>
        </button>`;
      }).join("")}
    </div>
  `;
}

function toggleStudioGoalChip(goalId) {
  if (state.selectedGoalIds.has(goalId)) {
    state.selectedGoalIds.delete(goalId);
  } else {
    state.selectedGoalIds.add(goalId);
    const goal = (state.focusGoals || []).find(g => g.id === goalId);
    if (goal && goal.playlist_id && !state.selectedPlaylistId) {
      state.selectedPlaylistId = goal.playlist_id;
      const sel = document.getElementById("composerPlaylistSelect");
      if (sel) sel.value = goal.playlist_id;
      const pl = (state.playlists || []).find(p => p.id === goal.playlist_id);
      if (pl && pl.kind === "drill") {
        document.getElementById("drillToggle").checked = true;
      }
    }
  }
  renderStudioGoalsStrip();
}

function renderComposerPlaylistSelect() {
  const sel = document.getElementById("composerPlaylistSelect");
  if (!sel) return;
  const curVal = state.selectedPlaylistId || "";
  sel.innerHTML = `<option value="">Playlist: None</option>` + (state.playlists || []).map(p =>
    `<option value="${esc(p.id)}" ${p.id === curVal ? "selected" : ""}>Playlist: ${esc(p.name)}</option>`
  ).join("") + `<option value="__new__">+ New Playlist...</option>`;
}

function onComposerPlaylistChange() {
  const sel = document.getElementById("composerPlaylistSelect");
  if (!sel) return;
  if (sel.value === "__new__") {
    sel.value = state.selectedPlaylistId || "";
    openPlaylistModal(null, pl => {
      state.selectedPlaylistId = pl.id;
      renderComposerPlaylistSelect();
      document.getElementById("composerPlaylistSelect").value = pl.id;
    });
    return;
  }
  state.selectedPlaylistId = sel.value || "";
  const pl = (state.playlists || []).find(p => p.id === state.selectedPlaylistId);
  if (pl && pl.kind === "drill") {
    document.getElementById("drillToggle").checked = true;
  }
}

function updateComposerAnchorBadge() {
  const el = document.getElementById("composerAnchor");
  const r = curRound();
  if (!el || !r) return;
  const rTag = formatRoundTag(r);
  const anchor = currentGf();
  if (state.rangeStartGf != null || state.rangeEndGf != null) {
    el.textContent = `${rTag} · gf${state.rangeStartGf ?? anchor}–${state.rangeEndGf ?? (anchor + 90)}`;
  } else {
    el.textContent = `${rTag} · gf${anchor}`;
  }
}

function renderComposerControls() {
  const kg = document.getElementById("kindGrid");
  const tg = document.getElementById("tagGrid");
  if (!kg || !tg) return;
  kg.innerHTML = KINDS.map(k =>
    `<button type="button" class="kind-btn ${state.selectedKind === k.id ? "on" : ""}" data-kind="${k.id}" onclick="pickKind('${k.id}')">${k.label}</button>`
  ).join("");
  tg.innerHTML = PRESET_TAGS.map(t =>
    `<button type="button" class="tag-btn ${state.selectedTags.has(t) ? "on" : ""}" onclick="toggleTag('${esc(t)}')">${esc(t)}</button>`
  ).join("");
}

function pickKind(k) { state.selectedKind = k; renderComposerControls(); }
function toggleTag(t) {
  if (state.selectedTags.has(t)) state.selectedTags.delete(t); else state.selectedTags.add(t);
  renderComposerControls();
}

function resetComposerDraft() {
  state.editingId = null;
  state.editingSource = "annotation";
  state.selectedKind = "mistake";
  state.selectedTags = new Set();
  state.selectedGoalIds = new Set();
  state.selectedPlaylistId = "";
  const titleEl = document.getElementById("composerTitle");
  if (titleEl) titleEl.textContent = "New Moment Note";
  const customEl = document.getElementById("customTagInput");
  if (customEl) customEl.value = "";
  const noteEl = document.getElementById("noteTextarea");
  if (noteEl) noteEl.value = "";
  const drillEl = document.getElementById("drillToggle");
  if (drillEl) drillEl.checked = false;
  const plSel = document.getElementById("composerPlaylistSelect");
  if (plSel) plSel.value = "";
  renderStudioGoalsStrip();
  renderComposerControls();
  updateComposerAnchorBadge();
}

function openComposer(defaultDrill = false, existing = null, source = "annotation") {
  video.pause();
  setStudioSidebarTab("moments");
  const r = curRound();
  if (!r) return;

  state.editingId = existing ? existing.id : null;
  state.editingSource = source || "annotation";
  state.selectedKind = existing ? (existing.kind && existing.kind !== "select_mark" ? existing.kind : "mistake") : "mistake";
  state.selectedTags = new Set(existing ? (existing.tags || []).filter(t => PRESET_TAGS.includes(t)) : []);
  state.selectedGoalIds = new Set(existing ? (existing.goal_ids || []) : []);
  state.selectedPlaylistId = existing && (existing.playlist_ids || [])[0] ? existing.playlist_ids[0] : "";

  const extraTags = existing ? (existing.tags || []).filter(t => !PRESET_TAGS.includes(t)) : [];
  const titleTxt = source === "select_mark"
    ? "Annotate Select Mark"
    : (existing ? "Edit Moment Note" : "New Moment Note");
  document.getElementById("composerTitle").textContent = titleTxt;

  const anchor = existing ? (existing.anchor_gf || existing.press_gf || existing.start_gf) : currentGf();
  const rObj = existing ? (roundById(existing.round_id) || r) : r;
  const rTag = formatRoundTag(rObj);
  const rangeTxt = existing && existing.start_gf && existing.end_gf
    ? `${rTag} · gf${existing.start_gf}–${existing.end_gf}`
    : (state.rangeStartGf != null || state.rangeEndGf != null)
      ? `${rTag} · gf${state.rangeStartGf ?? anchor}–${state.rangeEndGf ?? (anchor + 90)}`
      : `${rTag} · gf${anchor}`;
  document.getElementById("composerAnchor").textContent = rangeTxt;
  document.getElementById("customTagInput").value = extraTags.join(", ");
  document.getElementById("noteTextarea").value = existing
    ? (existing.is_default_select_note ? "" : (existing.note || ""))
    : "";
  document.getElementById("drillToggle").checked = source === "select_mark"
    ? true
    : (existing ? Boolean(existing.queued_drill) : Boolean(defaultDrill));

  renderStudioGoalsStrip();
  renderComposerPlaylistSelect();
  renderComposerControls();
  document.getElementById("noteTextarea").focus();
}

async function saveAnnotation() {
  const r = curRound();
  const mf = state.activeManifest;
  if (!r || !mf) return;
  const custom = document.getElementById("customTagInput").value.split(",").map(s => s.trim()).filter(Boolean);
  const tags = [...state.selectedTags, ...custom];
  const note = document.getElementById("noteTextarea").value.trim();
  const queued_drill = document.getElementById("drillToggle").checked;
  const goal_ids = [...state.selectedGoalIds];
  const playlist_ids = state.selectedPlaylistId ? [state.selectedPlaylistId] : [];

  const btn = document.getElementById("btnSaveNote");
  btn.disabled = true;
  try {
    let savedItem = null;
    if (state.editingId && state.editingSource === "select_mark") {
      const res = await api(`/api/queue/${encodeURIComponent(state.editingId)}`, {
        method: "PATCH",
        body: JSON.stringify({ kind: state.selectedKind, tags, note, goal_ids, playlist_ids }),
      });
      const idx = state.selectMarks.findIndex(x => x.id === state.editingId);
      if (idx >= 0) {
        state.selectMarks[idx] = { ...state.selectMarks[idx], ...res.item, needs_note: !note, is_default_select_note: false };
        savedItem = state.selectMarks[idx];
      }
      toast("Select mark annotated!");
    } else if (state.editingId) {
      const res = await api(`/api/annotations/${encodeURIComponent(state.editingId)}`, {
        method: "PATCH",
        body: JSON.stringify({ kind: state.selectedKind, tags, note, queued_drill, goal_ids, playlist_ids }),
      });
      const idx = state.annotations.findIndex(a => a.id === state.editingId);
      if (idx >= 0) state.annotations[idx] = res.annotation;
      savedItem = res.annotation;
      toast("Moment updated");
    } else {
      const payload = {
        quark_id: mf.quark_id,
        round_id: r.round_id,
        perspective: mf.perspective,
        anchor_gf: currentGf(),
        kind: state.selectedKind,
        tags,
        note,
        queued_drill,
        goal_ids,
        playlist_ids,
        owner: state.username || DEFAULT_USER,
      };
      if (state.rangeStartGf != null) payload.start_gf = state.rangeStartGf;
      if (state.rangeEndGf != null) payload.end_gf = state.rangeEndGf;
      const res = await api("/api/annotations", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      state.annotations.push(res.annotation);
      savedItem = res.annotation;
      clearRange();
      toast(queued_drill ? "Saved + baking .fs Takeover!" : "Moment saved");
    }

    // Also attach item to selected Playlist if chosen
    if (state.selectedPlaylistId && savedItem) {
      await api(`/api/playlists/${encodeURIComponent(state.selectedPlaylistId)}/items`, {
        method: "POST",
        body: JSON.stringify({
          action: "add",
          item: {
            item_id: savedItem.id,
            source: state.editingSource === "select_mark" ? "select_mark" : "annotation",
            quark_id: mf.quark_id,
            round_id: savedItem.round_id || r.round_id,
            perspective: mf.perspective,
            start_gf: savedItem.start_gf || Math.max(r.start_gf, (savedItem.anchor_gf || currentGf()) - 180),
            end_gf: savedItem.end_gf || ((savedItem.anchor_gf || currentGf()) + 90),
            anchor_gf: savedItem.anchor_gf || savedItem.press_gf || currentGf(),
            label: (note || `${mf.self_char_name} vs ${mf.opp_char_name} R${savedItem.round_id || r.round_id}`).slice(0, 120),
            note,
            tags,
          },
        }),
      });
      loadPlaylists();
    }

    resetComposerDraft();
    renderMatchAndRoundTabs();
    renderNotesList();
    updateHudAndTimeline();
    loadHubNotes(true);
    if (queued_drill) setTimeout(refreshAnnotationsQuiet, 1800);
  } catch (err) {
    toast("Save failed: " + err.message);
  } finally {
    btn.disabled = false;
  }
}

async function refreshAnnotationsQuiet() {
  if (!state.activeManifest) return;
  try {
    const res = await api(`/api/annotations?quark_id=${encodeURIComponent(state.activeManifest.quark_id)}`);
    state.annotations = res.annotations || [];
    renderNotesList();
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
    state.selectMarks = state.selectMarks.filter(s => s.id !== id);
    renderMatchAndRoundTabs();
    renderNotesList();
    updateHudAndTimeline();
    loadHubNotes(true);
    toast("Select mark dismissed");
  } catch (err) {
    toast(err.message);
  }
}

async function removeNote(id) {
  if (!confirm("Delete this moment note?")) return;
  try {
    await api(`/api/annotations/${encodeURIComponent(id)}`, { method: "DELETE" });
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

async function practiceSavedMoment(id, source, qid, startGf, endGf, anchorGf, persp, label) {
  if (source === "select_mark") {
    await launchDrill(id);
    return;
  }
  const ann = state.annotations.find(a => a.id === id);
  if (ann && ann.queued_drill && ann.drill_status === "ready") {
    await launchDrill(id);
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
  });
}

function renderNotesList() {
  const r = curRound();
  const mf = state.activeManifest;
  if (!mf) return;

  // Combine annotations + untriaged select marks (dedup by id)
  const seen = new Set(state.annotations.map(a => a.id));
  const combined = [
    ...state.annotations.map(a => ({ ...a, _src: "annotation" })),
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
  const plNameById = {};
  (state.playlists || []).forEach(p => { plNameById[p.id] = p.name; });

  if (!list.length) {
    host.innerHTML = `<div class="empty-inline">No moments in this ${state.noteScope} yet. Scrub to a frame and save a note above.</div>`;
  } else {
    host.innerHTML = list.map(a => {
      const aRound = roundById(a.round_id);
      const aTag = aRound ? formatRoundTag(aRound) : `R${a.round_id}`;
      const sec = aRound ? ((Math.max(0, (a.anchor_gf || a.start_gf) - aRound.start_gf)) / (aRound.fps || 60)).toFixed(2) + "s" : "";

      if (a._src === "select_mark") {
        const needs = Boolean(a.needs_note);
        const pracBtn = canTakeoverNow()
          ? `<button class="sm amber" onclick="practiceSavedMoment(${escJsAttr(a.id)},'select_mark',${escJsAttr(mf.quark_id)},${a.start_gf || 0},${a.end_gf || 0},${a.anchor_gf || 0},${mf.perspective},'Select mark')">▶ Practice</button>`
          : "";
        return `
          <div class="note-item is-select">
            <div class="row-between">
              <div class="row" style="gap:6px;">
                <span class="pill kind-select_mark">Select Mark</span>
                <span class="note-ts mono" onclick="jumpToMoment(${a.round_id},${a.start_gf || 0},${a.end_gf || 0},${a.anchor_gf || 0},false)">${esc(aTag)}${sec ? ` · ${sec}` : ""} · gf${a.anchor_gf}</span>
              </div>
              ${needs ? `<span style="font-size:11px; font-weight:700; color:var(--amber-fg);">Needs note</span>` : `<span class="pill kind-${esc(a.kind || "note")}">${esc(KIND_LABELS[a.kind] || a.kind)}</span>`}
            </div>
            <div class="note-body" style="${needs ? "color:var(--muted);" : ""}">${esc(needs ? "Marked live during match (-6.0s window ready)." : a.note)}</div>
            <div class="row" style="gap:5px;">
              <button class="sm primary" onclick="openComposer(true, state.selectMarks.find(x => x.id === ${escJsAttr(a.id)}), 'select_mark')">${needs ? "Add Note" : "Edit"}</button>
              ${pracBtn}
              <button class="sm ghost" onclick="dismissSelectMarkInStudio(${escJsAttr(a.id)})">Dismiss</button>
            </div>
          </div>
        `;
      }

      const kindLbl = KIND_LABELS[a.kind] || a.kind || "Note";
      const plBadge = (a.playlist_ids || []).map(pid => plNameById[pid] ? `<span class="pill drill">${esc(plNameById[pid])}</span>` : "").join("");
      const tagPills = (a.tags || []).map(t => `<span class="pill note">${esc(t)}</span>`).join("");
      const pracLbl = a.queued_drill && a.drill_status === "ready" ? "▶ Practice (.fs)" : "▶ Practice";
      const safeNoteLabel = (a.note || kindLbl).slice(0, 80);
      const pracBtn = canTakeoverNow()
        ? `<button class="sm amber" onclick="practiceSavedMoment(${escJsAttr(a.id)},'annotation',${escJsAttr(mf.quark_id)},${a.start_gf || 0},${a.end_gf || 0},${a.anchor_gf || 0},${mf.perspective},${escJsAttr(safeNoteLabel)})">${pracLbl}</button>`
        : "";
      const proEnabled = isProCorpusEnabled();
      const proClipsBtn = proEnabled
        ? `<button class="sm pro-btn" onclick="runProSearchForRange(${a.round_id},${a.start_gf},${a.end_gf})">🔍 Pro Clips</button>`
        : `<button class="sm pro-btn pro-gated" aria-disabled="true" title="On the roadmap" onclick="runProSearchForRange(${a.round_id},${a.start_gf},${a.end_gf})">🔍 Pro Clips</button>`;
      return `
        <div class="note-item">
          <div class="row-between">
            <div class="row" style="gap:6px;">
              <span class="pill kind-${esc(a.kind || "note")}">${esc(kindLbl)}</span>
              <span class="note-ts mono" onclick="jumpToMoment(${a.round_id},${a.start_gf || 0},${a.end_gf || 0},${a.anchor_gf || 0},false)">${esc(aTag)}${sec ? ` · ${sec}` : ""} · gf${a.anchor_gf}</span>
            </div>
            ${plBadge}
          </div>
          ${tagPills ? `<div class="row" style="margin-top:5px; gap:4px;">${tagPills}</div>` : ""}
          ${a.note ? `<div class="note-body">${esc(a.note)}</div>` : ""}
          <div class="row" style="gap:5px; margin-top:6px;">
            ${pracBtn}
            <button class="sm" onclick="openAddToPlaylistModal({item_id:${escJsAttr(a.id)},source:'annotation',quark_id:${escJsAttr(mf.quark_id)},round_id:${a.round_id},perspective:${mf.perspective},start_gf:${a.start_gf || 0},end_gf:${a.end_gf || 0},anchor_gf:${a.anchor_gf || 0},label:${escJsAttr(safeNoteLabel)}})">+ Playlist</button>
            ${proClipsBtn}
            <button class="sm ghost" onclick="openComposer(false, state.annotations.find(x => x.id === ${escJsAttr(a.id)}), 'annotation')">Edit</button>
            <button class="sm danger" onclick="removeNote(${escJsAttr(a.id)})">✕</button>
          </div>
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
    <div style="font-size:12.5px; color:var(--muted); margin-bottom:10px;">
      ${esc(scoreTxt)}${mf.round_count} rounds · ${formatDuration(mf.total_duration_s)} · Viewing P${mf.perspective} · <span class="mono">${esc(mf.quark_id)}</span>
    </div>
    <button class="sm primary ${proEnabled ? "" : "pro-gated"}" ${proEnabled ? `onclick="triggerMatchBreakdown('${esc(mf.quark_id)}', ${mf.perspective})"` : `aria-disabled="true" title="On the roadmap" onclick="toast('Pro Corpus comparison is on the roadmap.')"`}>✨ Run / Open AI Match Breakdown</button>
  `;
}

async function saveSetNote() {
  if (!state.activeManifest) return;
  const note = document.getElementById("setNoteInput").value;
  const tags = (document.getElementById("setNoteTagsInput").value || "").split(",").map(s => s.trim()).filter(Boolean);
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
    toast("Set takeaway saved");
    loadHubNotes(true);
  } catch (err) {
    toast("Save failed: " + err.message);
  }
}

// ---------- IN-STUDIO PRO SEARCH ----------
function runProSearchForRange(roundId, startGf, endGf) {
  if (!isProCorpusEnabled()) {
    toast("Pro Search is on the roadmap.");
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
    toast("Pro Search is on the roadmap.");
    return;
  }
  const mf = state.activeManifest;
  const r = curRound();
  if (!mf || !r) return;
  setStudioSidebarTab("pro_search");

  if (!state.capabilities.has_pro_corpus) {
    document.getElementById("proSearchResults").innerHTML = `<div class="empty-inline">Pro corpus Parquets not found in <code>data/parquet/</code>.</div>`;
    return;
  }

  const anchor = currentGf();
  const sGf = explicitStart || state.rangeStartGf || Math.max(r.start_gf, anchor - 60);
  const eGf = explicitEnd || state.rangeEndGf || Math.min(r.end_gf, anchor + 30);
  const tier = document.getElementById("proSearchTier") ? document.getElementById("proSearchTier").value : "";
  const host = document.getElementById("proSearchResults");
  const btn = document.getElementById("btnRunProSearch");

  if (btn) { btn.disabled = true; btn.textContent = "Searching..."; }
  host.innerHTML = `<div class="empty-inline">Searching ${state.capabilities.pro_corpus_sets || ""} pro sets for gf${sGf}–${eGf}...</div>`;

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
    host.innerHTML = `<div class="empty-inline">No matching pro trajectories found for this range. Try widening the bracket or switching tier to Universal.</div>`;
    return;
  }

  const tierLbl = { exact_matchup: "Exact Matchup", same_self_char: "Same Character", universal: "Universal" }[res.retrieval_tier] || res.retrieval_tier;
  const headerHtml = `<div style="font-size:11.5px; color:var(--muted); margin-bottom:8px;">
    Found <strong>${matches.length}</strong> pro sequences · Tier: <strong>${esc(tierLbl)}</strong> · Scanned ${res.rounds_scanned} rounds in ${res.elapsed_s}s
  </div>`;

  host.innerHTML = headerHtml + matches.map((m, idx) => {
    const sRk = m.self_rank_letter ? `<span class="rank">${esc(m.self_rank_letter)}</span>` : "";
    const oRk = m.opp_rank_letter ? `<span class="rank">${esc(m.opp_rank_letter)}</span>` : "";
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
      ? `<button class="sm amber" onclick="launchTakeoverPayload({quark_id:${escJsAttr(m.quark_id)},frame:${m.mid_gf},player:${m.perspective},label:${escJsAttr(`Pro: ${m.self_player_name} (${m.self_char_name})`)}})">▶ Practice Pro Clip</button>`
      : "";
    const proLabel = `${m.self_player_name} (${m.self_char_name}) vs ${m.opp_player_name} (${m.opp_char_name}) — ${m.action_coarse || "Pro answer"}`;

    return `
      <div class="pro-card">
        <div class="row-between">
          <span class="pill ready">${m.similarity_pct}% match</span>
          <span class="pill ${outCls}">${outTxt}</span>
        </div>
        <div style="font-weight:700; font-size:13px; margin-top:6px;">
          ${esc(m.self_player_name)} ${sRk} (${esc(m.self_char_name)}) <span style="color:var(--muted-dim);">vs</span> ${esc(m.opp_player_name)} ${oRk} (${esc(m.opp_char_name)})
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
    toast("Pro clip ready!");
  } catch (err) {
    toast("Clip render failed: " + err.message);
    if (btn) { btn.disabled = false; btn.textContent = "Render Clip"; }
  }
}
