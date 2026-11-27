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
    pinLbl.textContent = `Pin to current time (${formatRelativeClipTime(gf, startGf)})`;
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
  toast(`Jumped to ${formatRelativeClipTime(targetGf, startGf)}`);
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
    sv.currentTime = 0;
    sv.play().catch(() => {});
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

function practiceSharedItem(btnEl = null) {
  if (isMobileMode()) {
    toast("Practice / Replay Takeover is only available on desktop.");
    return;
  }
  const item = _activeSharedItem();
  if (!item) return;
  const sides = _sharedProjectedSides(item, state.sharedPerspective);
  const label = item.title || item.question || item.label || formatMatchLabelText({
    self_player_name: sides.selfName,
    opp_player_name: sides.oppName,
    self_char_name: sides.selfChar,
    opp_char_name: sides.oppChar,
    quark_id: item.quark_id,
  }, { allowPlaceholders: true, showRank: false });
  if (isHostedMode() && !state.auth.authenticated) {
    openCompanionModal(label, null, { signInRequired: true });
    return;
  }
  const cap = String(item.shared_caption || "").trim();
  const rawNote = String(item.note || "").trim();
  const qText = String(item.question || "").trim();
  let fullNote = "";
  if (cap && rawNote && rawNote !== cap) {
    fullNote = `${cap}\n\n${rawNote}`;
  } else {
    fullNote = cap || rawNote || qText;
  }
  const rawParentId = String(item.id || item.item_id || "").trim();
  const payload = {
    quark_id: item.quark_id,
    round_id: Number(item.round_id || 1),
    frame: Number(item.anchor_gf || item.start_gf || 1),
    start_gf: Number(item.start_gf || 0),
    end_gf: Number(item.end_gf || 0),
    player: state.sharedPerspective,
    bake_if_missing: true,
    label,
    note: fullNote,
  };
  if (/^[A-Za-z0-9_-]{3,128}$/.test(rawParentId)) {
    payload.parent_moment_id = rawParentId;
  }
  if (!canTakeoverNow()) {
    openCompanionModal(label, payload, { signInRequired: false });
    return;
  }
  launchTakeoverPayload(payload, btnEl);
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
  const sides = _sharedProjectedSides(item, state.sharedPerspective);
  const fallbackLabel = formatMatchLabelText({
    self_player_name: sides.selfName,
    opp_player_name: sides.oppName,
    self_char_name: sides.selfChar,
    opp_char_name: sides.oppChar,
    quark_id: item.quark_id,
  }, { allowPlaceholders: true, showRank: false });
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
    label: (item.title || item.question || item.label || fallbackLabel).slice(0, 120),
    note: (item.note || "").slice(0, 600),
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
        <div class="row" style="gap:6px;">
          <button class="speed-btn audio-btn" data-audio-mute="1" data-tip="Mute / Unmute audio (Shortcut: M)" aria-label="Mute or unmute audio" onclick="toggleVideoMute()">🔊</button>
          <div class="speed-group">
            <button class="speed-btn ${state.sharedRate === 0.25 ? "active" : ""}" data-srate="0.25" onclick="setSharedRate(0.25)">0.25x</button>
            <button class="speed-btn ${state.sharedRate === 0.5 ? "active" : ""}" data-srate="0.5" onclick="setSharedRate(0.5)">0.5x</button>
            <button class="speed-btn ${state.sharedRate === 1 ? "active" : ""}" data-srate="1" onclick="setSharedRate(1)">1.0x</button>
          </div>
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
        <button id="btnSharedFlip" class="sm" data-tip="Switch P1 / P2 perspective" onclick="flipSharedPerspective()">⇄ Flip P${state.sharedPerspective === 1 ? 2 : 1}</button>
        <button class="sm" data-tip="Save this clip to one of your playlists" onclick="saveSharedItemToMyPlaylist()">+ Save to My Playlist</button>
        <button class="sm amber-cta practice-btn" data-tip="Launch Replay Takeover in FBNeo for this clip" onclick="practiceSharedItem(this)">▶ Practice</button>
      </div>
    </div>
  `;
}

// =========================================================================
// SHARED MOMENT VIEW (/m/:id)
// =========================================================================
async function openSharedMoment(momentId, opts = {}) {
  if (typeof video !== "undefined" && video) video.pause();
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  stopSharedPlayback();

  state.activeView = "shared_moment";
  state.sharedMoment = null;
  state.sharedReplies = [];
  if (opts.updateHistory !== false && !state._routingFromPopstate && typeof updateAppHistory === "function") {
    const targetUrl = isHostedMode()
      ? `/m/${encodeURIComponent(momentId)}`
      : `/#m/${encodeURIComponent(momentId)}`;
    updateAppHistory(targetUrl);
  }
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
    titleEl.innerHTML = formatMatchLabelHtml({
      self_player_name: sides.selfName,
      opp_player_name: sides.oppName,
      self_char_name: sides.selfChar,
      opp_char_name: sides.oppChar,
      quark_id: m.quark_id,
    }, { allowPlaceholders: true });
  }
  const flipBtn = document.getElementById("btnSharedFlip");
  if (flipBtn) flipBtn.textContent = `⇄ Flip P${state.sharedPerspective === 1 ? 2 : 1}`;
}

function _renderRepliesListHtml(replies) {
  if (!replies || !replies.length) {
    return `<div class="empty-inline">No coaching replies yet. Pause at a key frame and drop a pinned tip below!</div>`;
  }
  const activeItem = _activeSharedItem();
  const baseStartGf = Number((activeItem && activeItem.start_gf) || 0);
  return replies.map(rep => {
    const relTime = rep.game_frame != null ? formatRelativeClipTime(rep.game_frame, baseStartGf) : "";
    const frameBtn = rep.game_frame != null
      ? `<button type="button" class="pill cached mono" style="cursor:pointer;" onclick="seekSharedToGf(${Number(rep.game_frame)}, true)" data-tip="Seek to ${esc(relTime)} (gf${Number(rep.game_frame)})" title="Seek to ${esc(relTime)} (gf${Number(rep.game_frame)})">🎯 ${esc(relTime)}</button>`
      : "";
    return `
      <div class="reply-card">
        <div class="row-between" style="margin-bottom:4px;">
          <div class="row" style="gap:6px;">
            <strong style="font-size:13px;">${esc(rep.author_name || "Coach")}</strong>
            ${frameBtn}
          </div>
          <span style="font-size:11px; color:var(--muted);" data-tip="Reply timestamp (local timezone)">${esc(formatTimestampWithTz(rep.created_at, (rep.created_at || "").slice(0, 10)))}</span>
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
  if (typeof updateDocumentTitle === "function") updateDocumentTitle();
  const sides = _sharedProjectedSides(m, state.sharedPerspective);
  const tagsHtml = getDisplayTagsForItem(m).map(t => `<span class="pill note">${esc(t)}</span>`).join("");
  const originPill = renderSystemOriginBadgeHtml(m);
  const clipDur = formatClipDuration(m.start_gf, m.end_gf, 60);
  const clipDurPill = clipDur
    ? `<span class="pill cached mono" data-tip="R${Number(m.round_id || 1)} · gf${Number(m.start_gf || 0)}–${Number(m.end_gf || 0)}">${esc(clipDur)} clip</span>`
    : "";
  const initPinRel = formatRelativeClipTime(m.anchor_gf || m.start_gf || 0, m.start_gf || 0);
  const shareKey = m.share_token || m.id;
  const shareUrl = m.share_url || `${window.location.origin}/m/${encodeURIComponent(shareKey)}`;

  const replyFormHtml = (!isHostedMode() || state.auth.authenticated)
    ? `
      <div class="composer-card" style="margin-top:10px;">
        <div class="row-between" style="margin-bottom:6px;">
          <strong style="font-size:13px;">Add Coaching Reply</strong>
          <label class="row" style="gap:5px; font-size:11.5px; color:var(--muted); cursor:pointer;">
            <input id="sharedReplyPinCheck" type="checkbox" checked />
            <span id="sharedReplyPinLabel" class="mono">Pin to current time (${esc(initPinRel)})</span>
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
        <a class="btn-link primary sm" href="${esc(safeHref(apiUrl(`/api/auth/discord/start?next=${encodeURIComponent(`/m/${shareKey}`)}`)))}">Sign in with Discord</a>
      </div>
    `;

  const metaRowHtml = `${originPill}${clipDurPill}${m.author_name ? `<span style="font-size:12px; color:var(--muted);">Shared by <strong>${esc(m.author_name)}</strong></span>` : ""}`;
  host.innerHTML = `
    <div class="shared-grid">
      <div class="shared-left">
        <div class="card" style="margin-bottom:10px; padding:12px 14px;">
          <div class="row-between">
            <div>
              ${metaRowHtml ? `<div class="row" style="gap:6px; margin-bottom:4px;">${metaRowHtml}</div>` : ""}
              <div id="sharedMatchupLine" style="font-size:17px; font-weight:800;">
                ${formatMatchLabelHtml({
                  self_player_name: sides.selfName,
                  opp_player_name: sides.oppName,
                  self_char_name: sides.selfChar,
                  opp_char_name: sides.oppChar,
                  quark_id: m.quark_id,
                }, { allowPlaceholders: true })}
              </div>
            </div>
            <div class="row" style="gap:6px;">
              <button class="sm" onclick="copyText(${escJsAttr(shareUrl)}, 'Copied share link')">↗ Copy Link</button>
              <button class="sm ghost" onclick="openReportModal('moment', ${escJsAttr(m.id)})" title="Report inappropriate content">Report</button>
            </div>
          </div>
          ${m.question ? `<div class="report-callout" style="margin-top:8px;"><strong>Question:</strong> ${esc(m.question)}</div>` : ""}
          ${m.shared_caption ? `<div class="note-body" style="margin-top:8px;">${esc(m.shared_caption)}</div>` : ""}
          ${m.note && m.note !== m.shared_caption ? `<div class="note-body" style="margin-top:8px;">${esc(m.note)}</div>` : ""}
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
    const res = await api(`/api/moments/${encodeURIComponent(m.share_token || m.id)}/replies`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (res.reply) state.sharedReplies.push(res.reply);
    if (ta) ta.value = "";
    const host = document.getElementById("sharedRepliesHost");
    if (host) host.innerHTML = _renderRepliesListHtml(state.sharedReplies);
    const cnt = document.getElementById("sharedReplyCnt");
    if (cnt) cnt.textContent = String(state.sharedReplies.length);
    toast(game_frame != null ? `Reply pinned at ${formatRelativeClipTime(game_frame, m.start_gf)}` : "Reply posted");
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
async function openSharedPlaylist(playlistId, opts = {}) {
  if (typeof video !== "undefined" && video) video.pause();
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  stopSharedPlayback();

  state.activeView = "shared_playlist";
  state.sharedPlaylist = null;
  state.sharedPlaylistIndex = 0;
  if (opts.updateHistory !== false && !state._routingFromPopstate && typeof updateAppHistory === "function") {
    const targetUrl = isHostedMode()
      ? `/p/${encodeURIComponent(playlistId)}`
      : `/#p/${encodeURIComponent(playlistId)}`;
    updateAppHistory(targetUrl);
  }
  syncTopNav();

  const host = document.getElementById("sharedContent");
  if (host) host.innerHTML = `<div class="card empty-card">Loading shared playlist...</div>`;

  try {
    const res = await api(`/api/playlists/${encodeURIComponent(playlistId)}`);
    state.sharedPlaylist = res.playlist;
    const first = (res.playlist.items || [])[0];
    state.sharedPerspective = first && Number(first.perspective) === 2 ? 2 : 1;
    renderSharedPlaylistView();
    if (first) _mountSharedStageMedia(first, true);
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
  _mountSharedStageMedia(cur, true);
  syncOverlayButtons();
}

function stepSharedPlaylist(delta) {
  const pl = state.sharedPlaylist;
  if (!pl || !(pl.items || []).length) return;
  const n = pl.items.length;
  selectSharedPlaylistItem((state.sharedPlaylistIndex + delta + n) % n);
}

function renderSharedPlaylistHeaderOnly() {
  const cur = _activeSharedItem();
  if (!cur) return;
  const sides = _sharedProjectedSides(cur, state.sharedPerspective);
  const el = document.getElementById("sharedPlClipMatchup");
  if (el) {
    el.innerHTML = formatMatchLabelHtml({
      self_player_name: sides.selfName,
      opp_player_name: sides.oppName,
      self_char_name: sides.selfChar,
      opp_char_name: sides.oppChar,
      quark_id: cur.quark_id,
    }, { allowPlaceholders: true });
  }
  const flipBtn = document.getElementById("btnSharedFlip");
  if (flipBtn) flipBtn.textContent = `⇄ Flip P${state.sharedPerspective === 1 ? 2 : 1}`;
}

function renderSharedPlaylistView() {
  const host = document.getElementById("sharedContent");
  const pl = state.sharedPlaylist;
  if (!host || !pl) return;
  if (typeof updateDocumentTitle === "function") updateDocumentTitle();
  const items = pl.items || [];
  const cur = items[state.sharedPlaylistIndex] || null;
  const plShareKey = pl.share_token || pl.id;
  const shareUrl = pl.share_url || `${window.location.origin}/p/${encodeURIComponent(plShareKey)}`;

  const listHtml = items.length
    ? items.map((it, idx) => {
        const active = idx === state.sharedPlaylistIndex;
        const rowSides = _sharedProjectedSides(it, Number(it.perspective) === 2 ? 2 : 1);
        const rowMatchObj = {
          self_player_name: rowSides.selfName,
          opp_player_name: rowSides.oppName,
          self_char_name: rowSides.selfChar,
          opp_char_name: rowSides.oppChar,
          quark_id: it.quark_id,
        };
        const muText = formatMatchLabelText(rowMatchObj, { allowPlaceholders: true, showRank: false });
        const muHtml = formatMatchLabelHtml(rowMatchObj, { allowPlaceholders: true });
        const mLinkKey = it.moment_share_token || it.moment_id;
        const rowDur = formatClipDuration(it.start_gf, it.end_gf, 60);
        return `
          <div class="pl-item-row shared-pl-row ${active ? "active" : ""}" onclick="selectSharedPlaylistItem(${idx})">
            <div style="min-width:0; flex:1;">
              <div class="row" style="gap:6px;">
                <span class="mono" style="font-size:11px; color:var(--muted-dim);">#${idx + 1}</span>
                <strong style="font-size:13px;">${esc(it.label || muText)}</strong>
                ${rowDur ? `<span class="pill mono" style="color:var(--muted);">${esc(rowDur)}</span>` : ""}
              </div>
              <div style="font-size:11.5px; color:var(--muted); margin-top:2px;">${muHtml}</div>
            </div>
            ${mLinkKey ? `<a class="sm ghost" href="${esc(safeHref(`/m/${encodeURIComponent(mLinkKey)}`))}" onclick="event.stopPropagation(); event.preventDefault(); openSharedMoment(${escJsAttr(mLinkKey)});" title="Open full moment + replies">↗</a>` : ""}
          </div>
        `;
      }).join("")
    : `<div class="empty-inline">This playlist has no clips yet.</div>`;

  const curSides = cur ? _sharedProjectedSides(cur, state.sharedPerspective) : null;
  const curMatchupHtml = curSides
    ? formatMatchLabelHtml({
        self_player_name: curSides.selfName,
        opp_player_name: curSides.oppName,
        self_char_name: curSides.selfChar,
        opp_char_name: curSides.oppChar,
        quark_id: cur.quark_id,
      }, { allowPlaceholders: true })
    : "";

  const curDur = cur ? formatClipDuration(cur.start_gf, cur.end_gf, 60) : "";
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
          <button class="sm" onclick="copyText(${escJsAttr(shareUrl)}, 'Copied playlist link')">↗ Copy Link</button>
          <button class="sm ghost" onclick="openReportModal('playlist', ${escJsAttr(pl.id)})">Report</button>
        </div>
      </div>
    </div>

    <div class="shared-grid">
      <div class="shared-left">
        ${cur ? `
          <div class="card" style="margin-bottom:10px; padding:10px 14px;">
            <div class="row-between">
              <strong id="sharedPlClipMatchup" style="font-size:14.5px;">${curMatchupHtml}</strong>
              <span class="pill cached mono">Clip ${state.sharedPlaylistIndex + 1} / ${items.length}${curDur ? ` · ${esc(curDur)}` : ""}</span>
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
