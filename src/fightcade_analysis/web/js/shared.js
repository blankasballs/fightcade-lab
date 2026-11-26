/* Zero-Login Shared Moment (/m/:id) & Shared Playlist (/p/:id) Views
 * Supports looping 4:3 playback, toggleable Grouflon Hitboxes/Inputs/Sticks/Gauges,
 * symmetric P1/P2 Flip, clickable frame-pinned coaching replies, + Save to My Playlist,
 * cross-user Replay Takeover (or calm Companion explainer modal), and quiet Report button.
 */

function stopSharedPlayback() {
  if (state.sharedRaf) {
    cancelAnimationFrame(state.sharedRaf);
    state.sharedRaf = null;
  }
  if (typeof removeTurnstileWidget === "function") {
    removeTurnstileWidget("reply");
  }
  const sv = document.getElementById("sharedVideo");
  if (sv) {
    try { sv.pause(); sv.removeAttribute("src"); sv.load(); } catch (_) {}
  }
}

function _sharedProjectedSides(item, persp) {
  const p1Name = item.p1_name || "P1";
  const p2Name = item.p2_name || "P2";
  const p1Char = item.p1_char || (item.perspective === 1 ? item.self_char_name : item.opp_char_name) || "";
  const p2Char = item.p2_char || (item.perspective === 2 ? item.self_char_name : item.opp_char_name) || "";
  if (persp === 2) {
    return { selfName: p2Name, oppName: p1Name, selfChar: p2Char, oppChar: p1Char };
  }
  return { selfName: p1Name, oppName: p2Name, selfChar: p1Char, oppChar: p2Char };
}

function _activeSharedItem() {
  if (state.activeView === "shared_moment") return state.sharedMoment;
  if (state.activeView === "shared_playlist" && state.sharedPlaylist) {
    return (state.sharedPlaylist.items || [])[state.sharedPlaylistIndex] || null;
  }
  return null;
}

function sharedCurrentGf() {
  const item = _activeSharedItem();
  const sv = document.getElementById("sharedVideo");
  if (!item || !sv) return 0;
  const startGf = Number(item.start_gf || 0);
  const off = Math.max(0, Math.round((sv.currentTime || 0) * 60));
  return startGf + off;
}

function drawSharedOverlayFrame() {
  const item = _activeSharedItem();
  const sv = document.getElementById("sharedVideo");
  const sc = document.getElementById("sharedOverlayCanvas");
  const hud = document.getElementById("sharedFrameHud");
  if (!item || !sv) return;

  const startGf = Number(item.start_gf || 0);
  const off = Math.max(0, Math.round((sv.currentTime || 0) * 60));
  const gf = startGf + off;
  if (hud) {
    hud.textContent = `R${item.round_id || 1} · ${(sv.currentTime || 0).toFixed(2)}s · gf${gf}`;
  }
  const pinLbl = document.getElementById("sharedReplyPinLabel");
  if (pinLbl) {
    pinLbl.textContent = `Pin to current frame (gf${gf})`;
  }
  const playBtn = document.getElementById("btnSharedPlayPause");
  if (playBtn) {
    playBtn.textContent = sv.paused ? "▶" : "❚❚";
  }

  if (sc) {
    if (state.sharedOverlay) {
      drawOverlayOnCanvas(sc, state.sharedOverlay, startGf, off, state.overlayOpts);
    } else {
      sc.getContext("2d").clearRect(0, 0, sc.width, sc.height);
    }
  }
}

function seekSharedToGf(targetGf, pauseAfter = true) {
  const item = _activeSharedItem();
  const sv = document.getElementById("sharedVideo");
  if (!item || !sv) return;
  const startGf = Number(item.start_gf || 0);
  const off = Math.max(0, Number(targetGf) - startGf);
  sv.currentTime = off / 60.0;
  if (pauseAfter) sv.pause();
  drawSharedOverlayFrame();
  toast(`Jumped to gf${targetGf}`);
}

function toggleSharedPlay() {
  const sv = document.getElementById("sharedVideo");
  if (!sv) return;
  if (sv.paused) sv.play().catch(() => {});
  else sv.pause();
}

function stepSharedFrames(delta) {
  const sv = document.getElementById("sharedVideo");
  if (!sv) return;
  sv.pause();
  const curOff = Math.round((sv.currentTime || 0) * 60);
  const nextOff = Math.max(0, curOff + delta);
  sv.currentTime = nextOff / 60.0;
  drawSharedOverlayFrame();
}

function setSharedRate(rate) {
  state.sharedRate = rate;
  const sv = document.getElementById("sharedVideo");
  if (sv) sv.playbackRate = rate;
  document.querySelectorAll(".speed-btn[data-srate]").forEach(b => {
    b.classList.toggle("active", Number(b.dataset.srate) === rate);
  });
}

function flipSharedPerspective() {
  state.sharedPerspective = state.sharedPerspective === 1 ? 2 : 1;
  if (state.activeView === "shared_moment") {
    renderSharedMomentHeaderOnly();
  } else if (state.activeView === "shared_playlist") {
    renderSharedPlaylistHeaderOnly();
  }
  toast(`Viewing as P${state.sharedPerspective}`);
}

function _mountSharedStageMedia(item, autoLoopSingle = true) {
  stopSharedPlayback();
  state.sharedOverlay = null;
  const sv = document.getElementById("sharedVideo");
  const sc = document.getElementById("sharedOverlayCanvas");
  const stTxt = document.getElementById("sharedOvStatus");
  if (sc) sc.getContext("2d").clearRect(0, 0, sc.width, sc.height);
  if (!sv || !item) return;

  const vidUrl = item.clip_url || item.video_url;
  const ovUrl = item.overlay_url;
  sv.loop = Boolean(autoLoopSingle);

  if (ovUrl) {
    if (stTxt) stTxt.textContent = "Loading overlays...";
    fetchOverlayData(apiUrl(ovUrl))
      .then(ov => {
        if (_activeSharedItem() === item) {
          state.sharedOverlay = ov;
          if (stTxt) stTxt.textContent = "";
          drawSharedOverlayFrame();
        }
      })
      .catch(() => {
        if (stTxt) stTxt.textContent = "No overlay data";
      });
  } else if (stTxt) {
    stTxt.textContent = "No overlay data";
  }

  if (!vidUrl) return;
  sv.src = apiUrl(vidUrl);
  sv.playbackRate = state.sharedRate || 1;
  sv.load();
  sv.addEventListener("loadedmetadata", () => {
    sv.playbackRate = state.sharedRate || 1;
    sv.play().catch(() => {});
    drawSharedOverlayFrame();
  }, { once: true });

  sv.onended = () => {
    if (state.activeView === "shared_playlist" && state.sharedPlaylistAutoAdvance && state.sharedPlaylist && (state.sharedPlaylist.items || []).length > 1) {
      stepSharedPlaylist(1);
    } else {
      sv.currentTime = 0;
      sv.play().catch(() => {});
    }
  };

  const tick = () => {
    if (state.activeView !== "shared_moment" && state.activeView !== "shared_playlist") {
      state.sharedRaf = null;
      return;
    }
    drawSharedOverlayFrame();
    state.sharedRaf = requestAnimationFrame(tick);
  };
  state.sharedRaf = requestAnimationFrame(tick);
}

function practiceSharedItem() {
  const item = _activeSharedItem();
  if (!item) return;
  const sides = _sharedProjectedSides(item, state.sharedPerspective);
  const label = item.title || item.question || item.label || `${sides.selfChar || "3S"} vs ${sides.oppChar || ""}`;
  const payload = {
    quark_id: item.quark_id,
    round_id: Number(item.round_id || 1),
    frame: Number(item.anchor_gf || item.start_gf || 1),
    start_gf: Number(item.start_gf || 0),
    end_gf: Number(item.end_gf || 0),
    player: state.sharedPerspective,
    bake_if_missing: true,
    label,
  };
  if (!canTakeoverNow()) {
    openCompanionModal(label, payload);
    return;
  }
  launchTakeoverPayload(payload);
}

function saveSharedItemToMyPlaylist() {
  const item = _activeSharedItem();
  if (!item) return;
  if (isHostedMode() && !state.auth.authenticated) {
    const next = encodeURIComponent(window.location.pathname + window.location.search + window.location.hash);
    toast("Sign in with Discord to save moments to your playlists.");
    window.location.href = apiUrl(`/api/auth/discord/start?next=${next}`);
    return;
  }
  openAddToPlaylistModal({
    item_id: item.id || item.item_id,
    moment_id: state.activeView === "shared_moment" ? item.id : (item.moment_id || null),
    source: "moment",
    quark_id: item.quark_id,
    round_id: Number(item.round_id || 1),
    perspective: state.sharedPerspective,
    start_gf: Number(item.start_gf || 0),
    end_gf: Number(item.end_gf || 0),
    anchor_gf: Number(item.anchor_gf || item.start_gf || 0),
    label: (item.title || item.question || item.label || `${item.self_char_name || ""} vs ${item.opp_char_name || ""}`).slice(0, 120),
    note: (item.note || "").slice(0, 300),
  });
}

function _sharedStageHtml(hasVideo, isPlaylist) {
  return `
    <div class="video-shell">
      <div class="video-stage">
        ${hasVideo
          ? `<video id="sharedVideo" playsinline webkit-playsinline preload="metadata" muted loop></video>
             <canvas id="sharedOverlayCanvas" width="768" height="576"></canvas>`
          : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:13px;">No clip uploaded for this moment yet</div>`}
      </div>
      <div class="studio-video-bar">
        <div class="row" style="gap:5px;">
          <span class="bar-lbl">OVERLAYS</span>
          <button class="ov-btn" data-ov="hitboxes" onclick="toggleOverlayOpt('hitboxes')">Hitboxes</button>
          <button class="ov-btn" data-ov="inputs" onclick="toggleOverlayOpt('inputs')">Inputs</button>
          <button class="ov-btn" data-ov="sticks" onclick="toggleOverlayOpt('sticks')">Sticks</button>
          <button class="ov-btn" data-ov="gauges" onclick="toggleOverlayOpt('gauges')">Gauges</button>
          <span id="sharedOvStatus" style="font-size:11px; color:var(--muted);"></span>
        </div>
        <span id="sharedFrameHud" class="frame-hud">R1 · 0.00s · gf0</span>
        <div class="speed-group">
          <button class="speed-btn ${state.sharedRate === 0.25 ? "active" : ""}" data-srate="0.25" onclick="setSharedRate(0.25)">0.25x</button>
          <button class="speed-btn ${state.sharedRate === 0.5 ? "active" : ""}" data-srate="0.5" onclick="setSharedRate(0.5)">0.5x</button>
          <button class="speed-btn ${state.sharedRate === 1 ? "active" : ""}" data-srate="1" onclick="setSharedRate(1)">1.0x</button>
        </div>
      </div>
    </div>
    <div class="transport-bar">
      <div class="transport-steps">
        ${isPlaylist ? `<button class="step-btn" onclick="stepSharedPlaylist(-1)">← Prev</button>` : ""}
        <button class="step-btn" onclick="stepSharedFrames(-20)">-20f</button>
        <button class="step-btn" onclick="stepSharedFrames(-5)">-5f</button>
        <button id="btnSharedPlayPause" class="step-btn primary play-btn" onclick="toggleSharedPlay()">❚❚</button>
        <button class="step-btn" onclick="stepSharedFrames(5)">+5f</button>
        <button class="step-btn" onclick="stepSharedFrames(20)">+20f</button>
        ${isPlaylist ? `<button class="step-btn" onclick="stepSharedPlaylist(1)">Next →</button>` : ""}
      </div>
      <div class="transport-actions">
        ${isPlaylist ? `<button id="btnSharedLoopToggle" class="sm ${state.sharedPlaylistAutoAdvance ? "primary" : ""}" onclick="toggleSharedPlaylistAutoAdvance()">${state.sharedPlaylistAutoAdvance ? "Auto-Advance" : "Loop Current"}</button>` : ""}
        <button id="btnSharedFlip" class="sm" onclick="flipSharedPerspective()">⇄ Flip P${state.sharedPerspective === 1 ? 2 : 1}</button>
        <button class="sm" onclick="saveSharedItemToMyPlaylist()">+ Save to My Playlist</button>
        <button class="sm amber-cta" onclick="practiceSharedItem()">▶ Practice in Takeover</button>
      </div>
    </div>
  `;
}

// =========================================================================
// SHARED MOMENT VIEW (/m/:id)
// =========================================================================
async function openSharedMoment(momentId) {
  if (typeof video !== "undefined" && video) video.pause();
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  stopSharedPlayback();

  state.activeView = "shared_moment";
  state.sharedMoment = null;
  state.sharedReplies = [];
  syncTopNav();

  const host = document.getElementById("sharedContent");
  if (host) host.innerHTML = `<div class="card empty-card">Loading shared moment...</div>`;

  try {
    const res = await api(`/api/moments/${encodeURIComponent(momentId)}`);
    state.sharedMoment = res.moment;
    state.sharedReplies = res.replies || [];
    state.sharedPerspective = Number(res.moment.perspective === 2 ? 2 : 1);
    renderSharedMomentView();
    _mountSharedStageMedia(state.sharedMoment, true);
    syncOverlayButtons();
  } catch (err) {
    if (host) {
      host.innerHTML = `
        <div class="card empty-card">
          <strong style="font-size:16px;">Moment unavailable</strong>
          <div style="margin-top:6px; color:var(--muted);">${esc(err.message)}</div>
          <div style="margin-top:12px;"><button class="sm primary" onclick="navigateTo('home')">← Back to Fightcade Lab</button></div>
        </div>
      `;
    }
  }
}

function renderSharedMomentHeaderOnly() {
  const m = state.sharedMoment;
  if (!m) return;
  const sides = _sharedProjectedSides(m, state.sharedPerspective);
  const titleEl = document.getElementById("sharedMatchupLine");
  if (titleEl) {
    titleEl.innerHTML = `<span style="color:var(--accent-fg);">${esc(sides.selfChar || "P1")}</span> <span style="color:var(--muted-dim);">vs</span> <span style="color:var(--rose-fg);">${esc(sides.oppChar || "P2")}</span> <span style="font-size:13px; color:var(--muted); font-weight:600;">(${esc(sides.selfName)} vs ${esc(sides.oppName)})</span>`;
  }
  const flipBtn = document.getElementById("btnSharedFlip");
  if (flipBtn) flipBtn.textContent = `⇄ Flip P${state.sharedPerspective === 1 ? 2 : 1}`;
}

function _renderRepliesListHtml(replies) {
  if (!replies || !replies.length) {
    return `<div class="empty-inline">No coaching replies yet. Pause at a key frame and drop a frame-pinned tip below!</div>`;
  }
  return replies.map(rep => {
    const frameBtn = rep.game_frame != null
      ? `<button type="button" class="pill cached mono" style="cursor:pointer;" onclick="seekSharedToGf(${Number(rep.game_frame)}, true)" title="Seek straight to frame gf${Number(rep.game_frame)}">🎯 gf${Number(rep.game_frame)}</button>`
      : "";
    return `
      <div class="reply-card">
        <div class="row-between" style="margin-bottom:4px;">
          <div class="row" style="gap:6px;">
            <strong style="font-size:13px;">${esc(rep.author_name || "Coach")}</strong>
            ${frameBtn}
          </div>
          <span style="font-size:11px; color:var(--muted);">${esc((rep.created_at || "").slice(0, 10))}</span>
        </div>
        <div class="note-body" style="margin:4px 0 0;">${esc(rep.body)}</div>
      </div>
    `;
  }).join("");
}

function renderSharedMomentView() {
  const host = document.getElementById("sharedContent");
  const m = state.sharedMoment;
  if (!host || !m) return;
  const sides = _sharedProjectedSides(m, state.sharedPerspective);
  const tagsHtml = (m.tags || []).map(t => `<span class="pill note">${esc(t)}</span>`).join("");
  const shareUrl = `${window.location.origin}/m/${encodeURIComponent(m.id)}`;

  const replyFormHtml = (!isHostedMode() || state.auth.authenticated)
    ? `
      <div class="composer-card" style="margin-top:10px;">
        <div class="row-between" style="margin-bottom:6px;">
          <strong style="font-size:13px;">Add Coaching Reply</strong>
          <label class="row" style="gap:5px; font-size:11.5px; color:var(--muted); cursor:pointer;">
            <input id="sharedReplyPinCheck" type="checkbox" checked />
            <span id="sharedReplyPinLabel" class="mono">Pin to current frame (gf${Number(m.anchor_gf || m.start_gf || 0)})</span>
          </label>
        </div>
        <textarea id="sharedReplyText" class="input compact-textarea" placeholder="Share counterplay, spacing cues, or option selects..."></textarea>
        <div id="replyTurnstileBox" class="turnstile-box hidden"></div>
        <div class="row" style="justify-content:flex-end; margin-top:7px;">
          <button id="btnPostSharedReply" class="emerald sm" onclick="postSharedMomentReply()">Post Reply</button>
        </div>
      </div>
    `
    : `
      <div class="card" style="margin-top:10px; background:var(--surface-note); text-align:center;">
        <div style="font-size:12.5px; color:var(--muted); margin-bottom:8px;">Sign in with Discord to post frame-pinned coaching replies or save clips to your playlists.</div>
        <a class="btn-link primary sm" href="${esc(apiUrl(`/api/auth/discord/start?next=${encodeURIComponent(`/m/${m.id}`)}`))}">Sign in with Discord</a>
      </div>
    `;

  host.innerHTML = `
    <div class="shared-grid">
      <div class="shared-left">
        <div class="card" style="margin-bottom:10px; padding:12px 14px;">
          <div class="row-between">
            <div>
              <div class="row" style="gap:6px; margin-bottom:4px;">
                <span class="pill kind-${esc(m.kind || "note")}">${esc(KIND_LABELS[m.kind] || m.kind || "Moment")}</span>
                <span class="pill cached mono">R${m.round_id || 1} · gf${m.start_gf}–${m.end_gf}</span>
                ${m.author_name ? `<span style="font-size:12px; color:var(--muted);">Shared by <strong>${esc(m.author_name)}</strong></span>` : ""}
              </div>
              <div id="sharedMatchupLine" style="font-size:17px; font-weight:800;">
                <span style="color:var(--accent-fg);">${esc(sides.selfChar || "P1")}</span>
                <span style="color:var(--muted-dim);">vs</span>
                <span style="color:var(--rose-fg);">${esc(sides.oppChar || "P2")}</span>
                <span style="font-size:13px; color:var(--muted); font-weight:600;">(${esc(sides.selfName)} vs ${esc(sides.oppName)})</span>
              </div>
            </div>
            <div class="row" style="gap:6px;">
              <button class="sm" onclick="copyText('${esc(shareUrl)}', 'Copied share link')">↗ Copy Link</button>
              <button class="sm ghost" onclick="openReportModal('moment', '${esc(m.id)}')" title="Report inappropriate content">Report</button>
            </div>
          </div>
          ${m.question ? `<div class="report-callout" style="margin-top:8px;"><strong>Question:</strong> ${esc(m.question)}</div>` : ""}
          ${m.note ? `<div class="note-body" style="margin-top:8px;">${esc(m.note)}</div>` : ""}
          ${tagsHtml ? `<div class="row" style="gap:5px; margin-top:6px;">${tagsHtml}</div>` : ""}
        </div>

        ${_sharedStageHtml(Boolean(m.clip_url), false)}
      </div>

      <aside class="studio-right">
        <div class="row-between" style="margin-bottom:8px;">
          <strong style="font-size:14px;">Frame-Pinned Coaching Replies (<span id="sharedReplyCnt">${state.sharedReplies.length}</span>)</strong>
        </div>
        <div id="sharedRepliesHost">${_renderRepliesListHtml(state.sharedReplies)}</div>
        ${replyFormHtml}
      </aside>
    </div>
  `;
  if (typeof mountTurnstileWidget === "function") {
    mountTurnstileWidget("reply", "replyTurnstileBox", "reply");
  }
}

async function postSharedMomentReply() {
  const m = state.sharedMoment;
  if (!m) return;
  const ta = document.getElementById("sharedReplyText");
  const body = (ta ? ta.value : "").trim();
  if (!body) return toast("Write a coaching reply first");
  const pin = Boolean(document.getElementById("sharedReplyPinCheck")?.checked);
  const game_frame = pin ? sharedCurrentGf() : null;
  const sitekey = typeof getTurnstileSiteKey === "function" ? getTurnstileSiteKey() : "";
  const tsToken = typeof getTurnstileToken === "function" ? getTurnstileToken("reply") : "";
  if (isHostedMode() && sitekey && !tsToken) {
    toast("Please complete the human verification check first.");
    return;
  }
  const btn = document.getElementById("btnPostSharedReply");
  if (btn) btn.disabled = true;
  try {
    const payload = { body, game_frame };
    if (tsToken) {
      payload["cf-turnstile-response"] = tsToken;
    }
    const res = await api(`/api/moments/${encodeURIComponent(m.id)}/replies`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (res.reply) state.sharedReplies.push(res.reply);
    if (ta) ta.value = "";
    const host = document.getElementById("sharedRepliesHost");
    if (host) host.innerHTML = _renderRepliesListHtml(state.sharedReplies);
    const cnt = document.getElementById("sharedReplyCnt");
    if (cnt) cnt.textContent = String(state.sharedReplies.length);
    toast(game_frame != null ? `Reply pinned at gf${game_frame}` : "Reply posted");
  } catch (err) {
    toast("Reply failed: " + err.message);
  } finally {
    if (typeof resetTurnstileWidget === "function") {
      resetTurnstileWidget("reply");
    }
    if (btn) btn.disabled = false;
  }
}

// =========================================================================
// SHARED PLAYLIST VIEW (/p/:id)
// =========================================================================
async function openSharedPlaylist(playlistId) {
  if (typeof video !== "undefined" && video) video.pause();
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  stopSharedPlayback();

  state.activeView = "shared_playlist";
  state.sharedPlaylist = null;
  state.sharedPlaylistIndex = 0;
  syncTopNav();

  const host = document.getElementById("sharedContent");
  if (host) host.innerHTML = `<div class="card empty-card">Loading shared playlist...</div>`;

  try {
    const res = await api(`/api/playlists/${encodeURIComponent(playlistId)}`);
    state.sharedPlaylist = res.playlist;
    const first = (res.playlist.items || [])[0];
    state.sharedPerspective = first && Number(first.perspective) === 2 ? 2 : 1;
    renderSharedPlaylistView();
    if (first) _mountSharedStageMedia(first, !state.sharedPlaylistAutoAdvance);
    syncOverlayButtons();
  } catch (err) {
    if (host) {
      host.innerHTML = `
        <div class="card empty-card">
          <strong style="font-size:16px;">Playlist unavailable</strong>
          <div style="margin-top:6px; color:var(--muted);">${esc(err.message)}</div>
          <div style="margin-top:12px;"><button class="sm primary" onclick="navigateTo('home')">← Back to Fightcade Lab</button></div>
        </div>
      `;
    }
  }
}

function selectSharedPlaylistItem(idx) {
  const pl = state.sharedPlaylist;
  if (!pl || !(pl.items || []).length) return;
  state.sharedPlaylistIndex = Math.max(0, Math.min(pl.items.length - 1, idx));
  const cur = pl.items[state.sharedPlaylistIndex];
  state.sharedPerspective = Number(cur.perspective) === 2 ? 2 : 1;
  renderSharedPlaylistView();
  _mountSharedStageMedia(cur, !state.sharedPlaylistAutoAdvance);
  syncOverlayButtons();
}

function stepSharedPlaylist(delta) {
  const pl = state.sharedPlaylist;
  if (!pl || !(pl.items || []).length) return;
  const n = pl.items.length;
  selectSharedPlaylistItem((state.sharedPlaylistIndex + delta + n) % n);
}

function toggleSharedPlaylistAutoAdvance() {
  state.sharedPlaylistAutoAdvance = !state.sharedPlaylistAutoAdvance;
  const btn = document.getElementById("btnSharedLoopToggle");
  if (btn) {
    btn.textContent = state.sharedPlaylistAutoAdvance ? "Auto-Advance" : "Loop Current";
    btn.classList.toggle("primary", state.sharedPlaylistAutoAdvance);
  }
  const sv = document.getElementById("sharedVideo");
  if (sv) sv.loop = !state.sharedPlaylistAutoAdvance;
}

function renderSharedPlaylistHeaderOnly() {
  const cur = _activeSharedItem();
  if (!cur) return;
  const sides = _sharedProjectedSides(cur, state.sharedPerspective);
  const el = document.getElementById("sharedPlClipMatchup");
  if (el) el.textContent = `${sides.selfChar || "P1"} vs ${sides.oppChar || "P2"}`;
  const flipBtn = document.getElementById("btnSharedFlip");
  if (flipBtn) flipBtn.textContent = `⇄ Flip P${state.sharedPerspective === 1 ? 2 : 1}`;
}

function renderSharedPlaylistView() {
  const host = document.getElementById("sharedContent");
  const pl = state.sharedPlaylist;
  if (!host || !pl) return;
  const items = pl.items || [];
  const cur = items[state.sharedPlaylistIndex] || null;
  const shareUrl = `${window.location.origin}/p/${encodeURIComponent(pl.id)}`;

  const listHtml = items.length
    ? items.map((it, idx) => {
        const active = idx === state.sharedPlaylistIndex;
        const mu = (it.self_char_name || it.opp_char_name)
          ? `${it.self_char_name || "?"} vs ${it.opp_char_name || "?"}`
          : it.quark_id;
        return `
          <div class="pl-item-row shared-pl-row ${active ? "active" : ""}" onclick="selectSharedPlaylistItem(${idx})">
            <div style="min-width:0; flex:1;">
              <div class="row" style="gap:6px;">
                <span class="mono" style="font-size:11px; color:var(--muted-dim);">#${idx + 1}</span>
                <strong style="font-size:13px;">${esc(it.label || mu)}</strong>
              </div>
              <div style="font-size:11.5px; color:var(--muted); margin-top:2px;">${esc(mu)} · R${it.round_id || 1}</div>
            </div>
            ${it.moment_id ? `<a class="sm ghost" href="/m/${encodeURIComponent(it.moment_id)}" onclick="event.stopPropagation(); event.preventDefault(); openSharedMoment('${esc(it.moment_id)}');" title="Open full moment + replies">↗</a>` : ""}
          </div>
        `;
      }).join("")
    : `<div class="empty-inline">This playlist has no clips yet.</div>`;

  const curSides = cur ? _sharedProjectedSides(cur, state.sharedPerspective) : null;

  host.innerHTML = `
    <div class="card" style="margin-bottom:12px;">
      <div class="row-between">
        <div>
          <div class="row" style="gap:8px; margin-bottom:4px;">
            <span class="pill cached">Shared Playlist</span>
            <span class="pill">${items.length} clip${items.length === 1 ? "" : "s"}</span>
            ${pl.author_name ? `<span style="font-size:12px; color:var(--muted);">Curated by <strong>${esc(pl.author_name)}</strong></span>` : ""}
          </div>
          <div style="font-size:19px; font-weight:800;">${esc(pl.name)}</div>
          ${pl.description ? `<div style="font-size:13px; color:var(--muted); margin-top:3px;">${esc(pl.description)}</div>` : ""}
        </div>
        <div class="row" style="gap:6px;">
          <button class="sm" onclick="copyText('${esc(shareUrl)}', 'Copied playlist link')">↗ Copy Link</button>
          <button class="sm ghost" onclick="openReportModal('playlist', '${esc(pl.id)}')">Report</button>
        </div>
      </div>
    </div>

    <div class="shared-grid">
      <div class="shared-left">
        ${cur ? `
          <div class="card" style="margin-bottom:10px; padding:10px 14px;">
            <div class="row-between">
              <strong id="sharedPlClipMatchup" style="font-size:14.5px;">${esc(curSides.selfChar || "P1")} vs ${esc(curSides.oppChar || "P2")}</strong>
              <span class="pill cached mono">Clip ${state.sharedPlaylistIndex + 1} / ${items.length} · R${cur.round_id || 1}</span>
            </div>
            ${cur.label ? `<div style="font-weight:700; font-size:13px; color:var(--accent-fg); margin-top:4px;">${esc(cur.label)}</div>` : ""}
            ${cur.note ? `<div class="note-body" style="margin-top:4px;">${esc(cur.note)}</div>` : ""}
          </div>
          ${_sharedStageHtml(Boolean(cur.video_url), true)}
        ` : `<div class="card empty-card">No clips in this playlist.</div>`}
      </div>

      <aside class="studio-right">
        <div class="section-eyebrow" style="margin-bottom:6px;">PLAYLIST CLIPS (${items.length})</div>
        <div>${listHtml}</div>
      </aside>
    </div>
  `;
}
