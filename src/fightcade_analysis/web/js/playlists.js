/* Playlists Manager (#playlists), Add-to-Playlist Picker, Watch Mode (Study Reel + Overlays) & Train Mode (Takeover Runner) */

let _onPlaylistCreatedCb = null;

async function loadPlaylists(renderIfActive = false) {
  try {
    const u = state.username || DEFAULT_USER;
    const res = await api(`/api/playlists?owner=${encodeURIComponent(u)}&username=${encodeURIComponent(u)}`);
    state.playlists = res.playlists || [];
    const badge = document.getElementById("navPlaylistsBadge");
    if (badge) {
      if (state.playlists.length > 0) {
        badge.textContent = String(state.playlists.length);
        badge.classList.remove("hidden");
      } else {
        badge.classList.add("hidden");
      }
    }
    if (typeof renderComposerPlaylistSelect === "function") renderComposerPlaylistSelect();
    if (renderIfActive || (state.activeView === "notes" && state.notesSubView === "playlists")) {
      renderPlaylistsManager();
    }
  } catch (err) {
    if (renderIfActive) toast("Failed to load playlists: " + err.message);
  }
}

function renderPlaylistsManager() {
  const host = document.getElementById("playlistsManagerHost");
  if (!host) return;
  const pls = state.playlists || [];
  if (!pls.length) {
    host.innerHTML = `
      <div class="card empty-card">
        No playlists yet. Create one to group mistakes, matchup drills, or pro answers into a looping Watch reel &amp; Takeover training list.<br/>
        <button class="primary sm" style="margin-top:12px;" onclick="openPlaylistModal()">+ Create First Playlist</button>
      </div>
    `;
    return;
  }

  host.innerHTML = pls.map(p => {
    const items = p.items || [];
    const readyCnt = Number(p.takeover_ready_count || 0);
    const missingBake = items.filter(it => !it.savestate_ready && it.can_bake_drill).length;
    const linkedHtml = (p.linked_goals || []).map(g => `<span class="pill ready">● Goal: ${esc(g.title.slice(0, 32))}</span>`).join("");
    const sharePill = p.shareable ? `<span class="pill cached">Shareable</span>` : "";
    const trainBtn = canTakeoverNow()
      ? `<button class="sm amber" onclick="openTrainMode('${esc(p.id)}')" ${items.length === 0 ? "disabled" : ""}>▶ Train</button>`
      : "";
    const bakeBtn = (missingBake > 0 && state.capabilities.can_render)
      ? `<button class="sm" onclick="prebakePlaylistDrills('${esc(p.id)}')">⚡ Pre-Bake ${missingBake} Drill${missingBake === 1 ? "" : "s"}</button>`
      : "";

    const itemsHtml = items.length
      ? items.map((it, idx) => {
          const mu = (it.self_char_name || it.opp_char_name)
            ? `${esc(it.self_char_name || "?")} vs ${esc(it.opp_char_name || "?")}`
            : esc(it.quark_id);
          const opp = (it.opp_player_name && it.opp_player_name !== "P1" && it.opp_player_name !== "P2")
            ? ` · vs ${esc(it.opp_player_name)}` : "";
          const readyBadge = it.savestate_ready
            ? `<span class="pill ready">⚡ .fs ready</span>`
            : (it.can_bake_drill ? `<span class="pill cached">Auto-bake</span>` : "");
          const srcBadge = it.source === "pro_clip" ? `<span class="pill kind-pro_clip">Pro Clip</span>` : "";
          return `
            <div class="pl-item-row">
              <div style="min-width:0; flex:1;">
                <div class="row" style="gap:6px;">
                  <span class="mono" style="font-size:11.5px; color:var(--muted-dim);">#${idx + 1}</span>
                  <strong style="font-size:13px;">${esc(it.label || it.note || mu)}</strong>
                  ${srcBadge}
                  <span class="pill cached">${esc(it.round_label || "")}</span>
                  ${readyBadge}
                </div>
                <div style="font-size:12px; color:var(--muted); margin-top:2px;">
                  ${mu}${opp}${it.note && it.note !== it.label ? ` — ${esc(it.note.slice(0, 120))}` : ""}
                </div>
              </div>
              <div class="row" style="gap:4px; flex-wrap:nowrap;">
                <button class="sm ghost" onclick="movePlaylistItem('${esc(p.id)}', ${idx}, -1)" ${idx === 0 ? "disabled" : ""} title="Move up">↑</button>
                <button class="sm ghost" onclick="movePlaylistItem('${esc(p.id)}', ${idx}, 1)" ${idx === items.length - 1 ? "disabled" : ""} title="Move down">↓</button>
                <button class="sm danger" onclick="removePlaylistItem('${esc(p.id)}', '${esc(it.item_id)}')" title="Remove from playlist">✕</button>
              </div>
            </div>
          `;
        }).join("")
      : `<div style="font-size:12.5px; color:var(--muted); margin-top:8px;">Empty playlist — add moments from Replay Studio, Notes, or Pro Search.</div>`;

    return `
      <div class="pl-manager-card">
        <div class="row-between">
          <div>
            <div class="row" style="gap:8px;">
              <strong style="font-size:15.5px;">${esc(p.name)}</strong>
              <span class="pill">${items.length} clip${items.length === 1 ? "" : "s"}</span>
              <span class="pill drill">${readyCnt} Takeover-ready</span>
              ${sharePill}
              ${linkedHtml}
            </div>
            ${p.description ? `<div style="font-size:12.5px; color:var(--muted); margin-top:3px;">${esc(p.description)}</div>` : ""}
          </div>
          <div class="row" style="gap:6px;">
            <button class="sm primary" onclick="openWatchMode('${esc(p.id)}')" ${items.length === 0 ? "disabled" : ""}>Watch</button>
            ${trainBtn}
            ${bakeBtn}
            <button class="sm" onclick="openPlaylistModal('${esc(p.id)}')">Edit</button>
            <button class="sm danger" onclick="deletePlaylistById('${esc(p.id)}')">Delete</button>
          </div>
        </div>
        ${itemsHtml}
      </div>
    `;
  }).join("");
}

// ---------- CREATE / EDIT / DELETE PLAYLIST ----------
function openPlaylistModal(plId = null, onCreated = null) {
  const existing = plId ? state.playlists.find(p => p.id === plId) : null;
  state.editingPlaylistId = existing ? existing.id : null;
  _onPlaylistCreatedCb = onCreated;

  document.getElementById("playlistModalTitle").textContent = existing ? "Edit Playlist" : "New Playlist";
  document.getElementById("plNameInput").value = existing ? existing.name : "";
  document.getElementById("plDescInput").value = existing ? existing.description : "";
  document.getElementById("plKindSelect").value = existing ? (existing.kind || "drill") : "drill";
  document.getElementById("plShareableCheck").checked = existing ? Boolean(existing.shareable) : false;

  document.getElementById("playlistModal").classList.remove("hidden");
  document.getElementById("plNameInput").focus();
}

function closePlaylistModal() {
  state.editingPlaylistId = null;
  _onPlaylistCreatedCb = null;
  document.getElementById("playlistModal").classList.add("hidden");
}

async function savePlaylistModal() {
  const name = document.getElementById("plNameInput").value.trim();
  if (!name) return toast("Enter a playlist name");
  const description = document.getElementById("plDescInput").value.trim();
  const kind = document.getElementById("plKindSelect").value || "drill";
  const shareable = document.getElementById("plShareableCheck").checked;
  const btn = document.getElementById("btnSavePlaylist");
  btn.disabled = true;
  try {
    let savedPl = null;
    if (state.editingPlaylistId) {
      const res = await api(`/api/playlists/${encodeURIComponent(state.editingPlaylistId)}`, {
        method: "PATCH",
        body: JSON.stringify({ name, description, kind, shareable }),
      });
      savedPl = res.playlist;
      toast("Playlist updated");
    } else {
      const res = await api("/api/playlists", {
        method: "POST",
        body: JSON.stringify({ name, description, kind, shareable, owner: state.username || DEFAULT_USER }),
      });
      savedPl = res.playlist;
      toast("Playlist created!");
    }
    const cb = _onPlaylistCreatedCb;
    closePlaylistModal();
    await loadPlaylists(state.activeView === "notes" && state.notesSubView === "playlists");
    if (state.activeView === "home") await loadHomeWorkbench();
    if (cb && savedPl) cb(savedPl);
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function deletePlaylistById(plId) {
  if (!confirm("Delete this playlist? (Your underlying notes and replays will not be deleted.)")) return;
  try {
    await api(`/api/playlists/${encodeURIComponent(plId)}`, { method: "DELETE" });
    toast("Playlist deleted");
    await Promise.all([loadPlaylists(true), loadFocusGoals()]);
    if (state.activeView === "home") await loadHomeWorkbench();
  } catch (err) {
    toast(err.message);
  }
}

async function movePlaylistItem(plId, idx, delta) {
  const pl = state.playlists.find(p => p.id === plId);
  if (!pl || !pl.items) return;
  const target = idx + delta;
  if (target < 0 || target >= pl.items.length) return;
  const ids = pl.items.map(x => x.item_id);
  const tmp = ids[idx];
  ids[idx] = ids[target];
  ids[target] = tmp;
  try {
    await api(`/api/playlists/${encodeURIComponent(plId)}/items`, {
      method: "POST",
      body: JSON.stringify({ action: "reorder", order: ids }),
    });
    await loadPlaylists(true);
  } catch (err) {
    toast(err.message);
  }
}

async function removePlaylistItem(plId, itemId) {
  try {
    await api(`/api/playlists/${encodeURIComponent(plId)}/items`, {
      method: "POST",
      body: JSON.stringify({ action: "remove", item_id: itemId }),
    });
    toast("Removed from playlist");
    await loadPlaylists(true);
    if (state.activeView === "home") await loadHomeWorkbench();
  } catch (err) {
    toast(err.message);
  }
}

async function prebakePlaylistDrills(plId) {
  try {
    const res = await api(`/api/playlists/${encodeURIComponent(plId)}/ensure-drills`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    toast(res.queued > 0 ? `Baking ${res.queued} Takeover savestate(s) in background...` : "All drills already ready!");
    if (res.queued > 0) setTimeout(() => loadPlaylists(true), 3000);
  } catch (err) {
    toast(err.message);
  }
}

// ---------- QUICK ADD-TO-PLAYLIST PICKER ----------
function openAddToPlaylistModal(itemPayload) {
  state.addToPlPayload = itemPayload;
  const host = document.getElementById("addToPlBody");
  const pls = state.playlists || [];
  const rows = pls.map(p => {
    const already = (p.items || []).some(it => it.item_id === itemPayload.item_id);
    return `
      <div class="pl-item-row" style="margin-top:0; margin-bottom:8px;">
        <div>
          <strong style="font-size:13.5px;">${esc(p.name)}</strong>
          <div style="font-size:11.5px; color:var(--muted);">${(p.items || []).length} clips</div>
        </div>
        <button class="sm ${already ? "emerald" : "primary"}" onclick="confirmAddItemToPlaylist('${esc(p.id)}')">
          ${already ? "✓ Added" : "+ Add"}
        </button>
      </div>
    `;
  }).join("");

  host.innerHTML = `
    <div style="font-size:12.5px; color:var(--muted); margin-bottom:10px;">
      Moment: <strong>${esc(itemPayload.label || itemPayload.quark_id)}</strong>
    </div>
    ${rows || `<div class="empty-inline">No playlists yet. Create one below!</div>`}
    <div style="margin-top:10px; border-top:1px solid var(--border-soft); padding-top:10px; text-align:right;">
      <button class="sm" onclick="createPlaylistAndAddCurrent()">+ Create New Playlist</button>
    </div>
  `;
  document.getElementById("addToPlModal").classList.remove("hidden");
}

function closeAddToPlaylistModal() {
  state.addToPlPayload = null;
  document.getElementById("addToPlModal").classList.add("hidden");
}

function createPlaylistAndAddCurrent() {
  const item = state.addToPlPayload;
  closeAddToPlaylistModal();
  openPlaylistModal(null, async pl => {
    if (item && pl) {
      await api(`/api/playlists/${encodeURIComponent(pl.id)}/items`, {
        method: "POST",
        body: JSON.stringify({ action: "add", item }),
      });
      toast(`Added to "${pl.name}"`);
      await loadPlaylists(false);
    }
  });
}

async function confirmAddItemToPlaylist(plId) {
  if (!state.addToPlPayload) return;
  try {
    const res = await api(`/api/playlists/${encodeURIComponent(plId)}/items`, {
      method: "POST",
      body: JSON.stringify({ action: "add", item: state.addToPlPayload }),
    });
    toast(`Saved to "${res.playlist.name}"`);
    closeAddToPlaylistModal();
    await loadPlaylists(false);
    if (state.activeView === "home") await loadHomeWorkbench();
  } catch (err) {
    toast(err.message);
  }
}

// =========================================================================
// WATCH MODE (LOOPING STUDY REEL + GROUFLON CANVAS OVERLAYS)
// =========================================================================
function _activeWatchPlaylist() {
  return (state.playlists || []).find(p => p.id === state.watchPlaylistId) || null;
}

async function openWatchMode(plId, startIdx = 0) {
  if (typeof video !== "undefined" && video) video.pause();
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  if (!state.playlists.some(p => p.id === plId)) {
    await loadPlaylists(false);
  }
  const pl = (state.playlists || []).find(p => p.id === plId);
  if (!pl || !(pl.items || []).length) {
    toast("Playlist has no moments yet.");
    return;
  }
  state.watchPlaylistId = plId;
  state.watchIndex = Math.max(0, Math.min(pl.items.length - 1, startIdx));
  state.watchOverlay = null;

  document.getElementById("watchTitle").textContent = pl.name;
  const swBtn = document.getElementById("btnWatchSwitchTrain");
  if (swBtn) swBtn.classList.toggle("hidden", !canTakeoverNow());
  document.getElementById("watchModal").classList.remove("hidden");
  syncOverlayButtons();
  mountWatchItem();
}

function closeWatchMode() {
  if (state.watchRaf) { cancelAnimationFrame(state.watchRaf); state.watchRaf = null; }
  const wv = document.getElementById("watchVideo");
  if (wv) { try { wv.pause(); wv.removeAttribute("src"); wv.load(); } catch (_) {} }
  state.watchPlaylistId = null;
  state.watchOverlay = null;
  const modal = document.getElementById("watchModal");
  if (modal) modal.classList.add("hidden");
}

function switchWatchToTrain() {
  const plId = state.watchPlaylistId;
  const idx = state.watchIndex;
  closeWatchMode();
  if (plId) openTrainMode(plId, idx);
}

function toggleWatchAutoAdvance() {
  state.watchAutoAdvance = !state.watchAutoAdvance;
  const btn = document.getElementById("btnWatchLoopMode");
  if (btn) {
    btn.textContent = state.watchAutoAdvance ? "Auto-Advance" : "Loop Current";
    btn.classList.toggle("primary", state.watchAutoAdvance);
  }
}

function toggleWatchPlay() {
  const wv = document.getElementById("watchVideo");
  if (!wv) return;
  if (wv.paused) wv.play().catch(() => {}); else wv.pause();
}

function setWatchRate(rate) {
  state.watchRate = rate;
  const wv = document.getElementById("watchVideo");
  if (wv) wv.playbackRate = rate;
  document.querySelectorAll(".speed-btn[data-wrate]").forEach(b => b.classList.toggle("active", Number(b.dataset.wrate) === rate));
}

function prevWatchItem() {
  const pl = _activeWatchPlaylist();
  if (!pl || !pl.items.length) return;
  state.watchIndex = (state.watchIndex - 1 + pl.items.length) % pl.items.length;
  mountWatchItem();
}

function nextWatchItem() {
  const pl = _activeWatchPlaylist();
  if (!pl || !pl.items.length) return;
  state.watchIndex = (state.watchIndex + 1) % pl.items.length;
  mountWatchItem();
}

function seekWatchReel(e) {
  const pl = _activeWatchPlaylist();
  const wv = document.getElementById("watchVideo");
  if (!pl || !wv) return;
  const it = pl.items[state.watchIndex];
  if (!it) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / Math.max(1, rect.width)));
  const startS = Math.max(0, Number(it.clip_start_s ?? 0));
  const endS = Math.max(startS + 0.5, Number(it.clip_end_s ?? (startS + 3.0)));
  wv.currentTime = startS + frac * (endS - startS);
}

function drawWatchOverlayFrame() {
  const pl = _activeWatchPlaylist();
  const wv = document.getElementById("watchVideo");
  const wc = document.getElementById("watchOverlayCanvas");
  if (!pl || !wv || !wc) return;
  const it = pl.items[state.watchIndex];
  if (!it || !state.watchOverlay || it.is_standalone_clip) {
    wc.getContext("2d").clearRect(0, 0, wc.width, wc.height);
    return;
  }
  const fps = Number(it.round_fps || 60);
  const off = Math.max(0, Math.round((wv.currentTime || 0) * fps));
  drawOverlayOnCanvas(wc, state.watchOverlay, it.round_start_gf || 0, off, state.overlayOpts);
}

async function mountWatchItem() {
  if (state.watchRaf) { cancelAnimationFrame(state.watchRaf); state.watchRaf = null; }
  const pl = _activeWatchPlaylist();
  if (!pl || !pl.items.length) return;
  const it = pl.items[state.watchIndex];
  if (!it) return;

  document.getElementById("watchProgressPill").textContent = `Clip ${state.watchIndex + 1} of ${pl.items.length}`;
  const loopBtn = document.getElementById("btnWatchLoopMode");
  if (loopBtn) {
    loopBtn.textContent = state.watchAutoAdvance ? "Auto-Advance" : "Loop Current";
    loopBtn.classList.toggle("primary", state.watchAutoAdvance);
  }

  // Render right/bottom study notes panel
  const side = document.getElementById("watchSidePanel");
  const mu = `${it.self_char_name || "?"} vs ${it.opp_char_name || "?"}`;
  const opp = (it.opp_player_name && it.opp_player_name !== "P1" && it.opp_player_name !== "P2")
    ? `vs ${it.opp_player_name}` : "";
  const tagsHtml = (it.tags || []).map(t => `<span class="pill note">${esc(t)}</span>`).join("");
  const openStudioBtn = it.source !== "pro_clip"
    ? `<button class="sm primary" onclick="openWatchItemInStudio()">Open in Studio →</button>` : "";
  const pracBtn = canTakeoverNow()
    ? `<button class="sm amber" onclick="launchPlaylistItemTakeover('${esc(pl.id)}', ${state.watchIndex})">▶ Practice Now</button>` : "";

  side.innerHTML = `
    <div class="row" style="gap:6px;">
      <span class="pill kind-${esc(it.kind || "note")}">${esc(KIND_LABELS[it.kind] || it.kind || "Moment")}</span>
      <span class="pill cached">${esc(it.round_label || "")}</span>
    </div>
    <div style="font-weight:800; font-size:15px;">${esc(mu)} ${opp ? `<span style="color:var(--muted); font-weight:600; font-size:13px;">${esc(opp)}</span>` : ""}</div>
    ${it.label ? `<div style="font-weight:700; font-size:13.5px; color:var(--accent-fg);">${esc(it.label)}</div>` : ""}
    ${tagsHtml ? `<div class="row" style="gap:5px;">${tagsHtml}</div>` : ""}
    <div class="note-body" style="margin:0; font-size:13.5px;">${esc(it.note || "No written note for this clip yet.")}</div>
    <div class="row" style="margin-top:auto; padding-top:8px; gap:6px;">
      ${openStudioBtn}
      ${pracBtn}
    </div>
  `;

  const wv = document.getElementById("watchVideo");
  const wc = document.getElementById("watchOverlayCanvas");
  if (wc) wc.getContext("2d").clearRect(0, 0, wc.width, wc.height);
  state.watchOverlay = null;

  if (!it.has_video || !it.video_url) {
    try { wv.pause(); wv.removeAttribute("src"); wv.load(); } catch (_) {}
    toast("No rendered video for this clip yet — use Open in Studio or Train Mode.");
    return;
  }

  // Load Grouflon overlay JSON if available
  if (it.overlay_url && !it.is_standalone_clip) {
    fetchOverlayData(it.overlay_url).then(ov => {
      if (state.watchPlaylistId === pl.id && pl.items[state.watchIndex] === it) {
        state.watchOverlay = ov;
        drawWatchOverlayFrame();
      }
    }).catch(() => {});
  }

  const startS = Math.max(0, Number(it.clip_start_s ?? 0));
  const endS = Math.max(startS + 0.5, Number(it.clip_end_s ?? (startS + 3.0)));
  const spanS = Math.max(0.1, endS - startS);

  wv.src = it.video_url;
  wv.playbackRate = state.watchRate || 1;
  wv.load();

  const startPlay = () => {
    wv.playbackRate = state.watchRate || 1;
    wv.currentTime = startS;
    wv.play().catch(() => {});
  };
  if (wv.readyState >= 1) startPlay();
  else wv.addEventListener("loadedmetadata", startPlay, { once: true });

  const myIndex = state.watchIndex;
  const myPlId = pl.id;
  const tick = () => {
    if (state.watchPlaylistId !== myPlId || state.watchIndex !== myIndex) {
      state.watchRaf = null;
      return;
    }
    const fill = document.getElementById("watchProgFill");
    const playBtn = document.getElementById("btnWatchPlay");
    if (fill) {
      const rel = Math.max(0, Math.min(spanS, (wv.currentTime || startS) - startS));
      fill.style.width = `${(rel / spanS) * 100}%`;
    }
    if (playBtn) playBtn.textContent = wv.paused ? "▶" : "❚❚";
    drawWatchOverlayFrame();

    if (!wv.paused && wv.currentTime >= endS - 0.03) {
      if (state.watchAutoAdvance && pl.items.length > 1) {
        nextWatchItem();
        return;
      }
      wv.currentTime = startS;
    }
    state.watchRaf = requestAnimationFrame(tick);
  };
  state.watchRaf = requestAnimationFrame(tick);
}

function openWatchItemInStudio() {
  const pl = _activeWatchPlaylist();
  if (!pl) return;
  const it = pl.items[state.watchIndex];
  if (!it) return;
  closeWatchMode();
  state.returnView = "playlists";
  openMatch(it.quark_id, it.perspective, {
    round_id: it.round_id,
    start_gf: it.start_gf,
    end_gf: it.end_gf,
    anchor_gf: it.anchor_gf,
    autoPlay: false,
  });
}

// =========================================================================
// TRAIN MODE (DESKTOP TAKEOVER RUNNER)
// =========================================================================
async function openTrainMode(plId, startIdx = 0) {
  if (!canTakeoverNow()) {
    toast("Train Mode requires the Windows Desktop Companion. Opening Watch Mode instead.");
    return openWatchMode(plId, startIdx);
  }
  if (!state.playlists.some(p => p.id === plId)) await loadPlaylists(false);
  const pl = (state.playlists || []).find(p => p.id === plId);
  if (!pl || !(pl.items || []).length) {
    toast("Playlist has no drills yet.");
    return;
  }
  state.trainPlaylistId = plId;
  state.trainIndex = Math.max(0, Math.min(pl.items.length - 1, startIdx));
  document.getElementById("trainTitle").textContent = `Train: ${pl.name}`;
  document.getElementById("trainModal").classList.remove("hidden");
  renderTrainMode();
}

function closeTrainMode() {
  state.trainPlaylistId = null;
  document.getElementById("trainModal").classList.add("hidden");
}

function switchTrainToWatch() {
  const plId = state.trainPlaylistId;
  const idx = state.trainIndex;
  closeTrainMode();
  if (plId) openWatchMode(plId, idx);
}

function setTrainSpeed(sp) { state.trainSpeed = sp; renderTrainMode(); }
function setTrainScope(sc) { state.trainSpeedScope = sc; renderTrainMode(); }
function toggleTrainAutoLoop() { state.trainAutoLoop = !state.trainAutoLoop; renderTrainMode(); }

async function launchPlaylistItemTakeover(plId, idx) {
  const pl = (state.playlists || []).find(p => p.id === plId);
  if (!pl || !pl.items[idx]) return;
  const it = pl.items[idx];
  const opts = {
    speed: state.trainSpeed || 1.0,
    speed_scope: state.trainSpeedScope || "all",
    auto_loop: Boolean(state.trainAutoLoop),
  };
  if (it.selection_id && it.savestate_ready) {
    await launchDrill(it.selection_id, opts);
    return;
  }
  await launchTakeoverPayload({
    quark_id: it.quark_id,
    frame: it.anchor_gf || it.start_gf,
    start_gf: it.start_gf,
    end_gf: it.end_gf,
    player: it.perspective || 1,
    bake_if_missing: true,
    label: it.label || it.note || pl.name,
    ...opts,
  });
}

async function trainLaunchCurrent() {
  if (!state.trainPlaylistId) return;
  const btn = document.getElementById("btnTrainLaunch");
  if (btn) { btn.disabled = true; btn.textContent = "Launching..."; }
  try {
    await launchPlaylistItemTakeover(state.trainPlaylistId, state.trainIndex);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "▶ Launch Current"; }
  }
}

async function trainDoneAndNext() {
  const pl = (state.playlists || []).find(p => p.id === state.trainPlaylistId);
  if (!pl || !pl.items.length) return;
  state.trainIndex = (state.trainIndex + 1) % pl.items.length;
  renderTrainMode();
  await trainLaunchCurrent();
}

function trainStep(delta) {
  const pl = (state.playlists || []).find(p => p.id === state.trainPlaylistId);
  if (!pl || !pl.items.length) return;
  state.trainIndex = (state.trainIndex + delta + pl.items.length) % pl.items.length;
  renderTrainMode();
}

function renderTrainMode() {
  const pl = (state.playlists || []).find(p => p.id === state.trainPlaylistId);
  const host = document.getElementById("trainBody");
  if (!pl || !host) return;
  const it = pl.items[state.trainIndex];
  if (!it) return;

  document.getElementById("trainProgressPill").textContent = `Drill ${state.trainIndex + 1} of ${pl.items.length}`;
  const mu = `${it.self_char_name || "?"} vs ${it.opp_char_name || "?"}`;
  const linkedGoal = (pl.linked_goals && pl.linked_goals[0])
    ? `<div class="pill ready" style="margin-bottom:8px;">● Focus Goal: ${esc(pl.linked_goals[0].title)}</div>` : "";
  const speeds = [0.5, 0.75, 1.0, 1.25];

  host.innerHTML = `
    ${linkedGoal}
    <div class="card" style="background:var(--surface-note); margin-bottom:12px;">
      <div class="row-between" style="margin-bottom:4px;">
        <strong style="font-size:15px;">${esc(mu)} · ${esc(it.round_label || "")}</strong>
        <span class="pill ${it.savestate_ready ? "ready" : "cached"}">${it.savestate_ready ? "⚡ Instant .fs Ready" : "Auto-Bake (<2s)"}</span>
      </div>
      ${it.label ? `<div style="font-weight:700; font-size:13.5px; color:var(--accent-fg); margin-bottom:4px;">${esc(it.label)}</div>` : ""}
      <div class="note-body" style="margin:0;">${esc(it.note || "Focus on clean execution and spacing.")}</div>
    </div>

    <div class="row-between" style="margin-bottom:10px; gap:10px;">
      <div class="row" style="gap:5px;">
        <span class="section-eyebrow">SPEED</span>
        ${speeds.map(sp => `<button class="speed-btn ${state.trainSpeed === sp ? "active" : ""}" onclick="setTrainSpeed(${sp})">${sp.toFixed(2).replace(/\.00$/, ".0")}x</button>`).join("")}
      </div>
      <div class="row" style="gap:5px;">
        <button class="sm ${state.trainSpeedScope === "all" ? "primary" : ""}" onclick="setTrainScope('all')">Whole Drill</button>
        <button class="sm ${state.trainSpeedScope === "takeover_only" ? "primary" : ""}" onclick="setTrainScope('takeover_only')">Takeover Only</button>
        <button class="sm ${state.trainAutoLoop ? "emerald" : ""}" onclick="toggleTrainAutoLoop()">Loop: ${state.trainAutoLoop ? "ON" : "OFF"}</button>
      </div>
    </div>

    <div class="row-between" style="gap:8px;">
      <button class="sm" onclick="trainStep(-1)">← Prev</button>
      <div class="row" style="gap:8px;">
        <button id="btnTrainLaunch" class="amber-cta" onclick="trainLaunchCurrent()">▶ Launch Current</button>
        <button class="emerald" onclick="trainDoneAndNext()">✓ Done &amp; Next</button>
      </div>
      <button class="sm" onclick="trainStep(1)">Skip →</button>
    </div>

    <div style="font-size:11.5px; color:var(--muted-dim); margin-top:12px; padding-top:10px; border-top:1px solid var(--border-soft);">
      In-emulator hotkeys: <span class="kbd">Select</span> (<span class="kbd">P1 Coin</span>) or <span class="kbd">Alt+1</span> 1f Retry · <span class="kbd">Alt+4</span> Swap P1/P2 · <span class="kbd">Alt+5</span> Sandbox Training · <span class="kbd">Alt+6/7/8</span> Speed · <span class="kbd">Start</span> Menu
    </div>
  `;
}
