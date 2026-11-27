/* Unified Focuses & Playlists Manager (#playlists), Unified Collection Picker & Watch Mode */

let _onPlaylistCreatedCb = null;

async function loadPlaylists(renderIfActive = false) {
  try {
    const u = state.username || DEFAULT_USER;
    const res = await api(`/api/playlists?owner=${encodeURIComponent(u)}&username=${encodeURIComponent(u)}`);
    state.playlists = res.playlists || [];
    state.focusGoals = state.playlists.filter(p => p.is_focus && p.focus_status === "active");
    state.graduatedGoals = state.playlists.filter(p => p.is_focus && p.focus_status === "graduated");

    if (typeof renderComposerCollectionPicker === "function") renderComposerCollectionPicker();
    if (typeof renderNotesList === "function" && state.activeView === "studio") renderNotesList();
    if (typeof renderHubNotesControls === "function" && state.activeView === "notes" && state.notesSubView === "notebook") {
      renderHubNotesControls();
      if (typeof renderHubNotes === "function" && state.hubLoaded) renderHubNotes();
    }
    if (renderIfActive || (state.activeView === "notes" && state.notesSubView === "playlists")) {
      renderPlaylistsManager();
    }
  } catch (err) {
    if (renderIfActive) toast("Failed to load playlists: " + err.message);
  }
}

function _renderPlaylistManagerCardHtml(p, sectionType) {
  const items = p.items || [];
  const sharePill = p.shareable ? `<span class="pill cached">Shareable</span>` : "";
  const matchupPill = p.matchup_char ? `<span class="pill note">vs ${esc(p.matchup_char)}</span>` : "";
  const gradDatePill = (sectionType === "graduated" && p.graduated_at)
    ? `<span class="pill ready">✓ ${esc(p.graduated_at.slice(0, 10))}</span>` : "";
  const clipPill = (sectionType === "focus" && items.length === 0)
    ? `<span class="pill">Mental cue · 0 clips</span>`
    : `<span class="pill">${items.length} clip${items.length === 1 ? "" : "s"}</span>`;

  const watchBtn = items.length > 0
    ? `<button class="sm primary" onclick="openWatchMode(${escJsAttr(p.id)})">Watch</button>`
    : "";

  let lifecycleBtns = "";
  if (sectionType === "focus") {
    lifecycleBtns = `
      <button class="sm" onclick="openGoalModal(${escJsAttr(p.id)})">Edit</button>
      <button class="sm ghost" data-tip="Unpin back to Playlists (keeps all clips)" onclick="unpinFocusToPlaylist(${escJsAttr(p.id)})">Unpin</button>
      <button class="sm emerald" data-tip="Graduate to mastered history (keeps clips archived)" onclick="graduateFocusGoal(${escJsAttr(p.id)})">Graduate ✓</button>
    `;
  } else if (sectionType === "reference") {
    const atCap = getActiveFocuses().length >= 3;
    lifecycleBtns = `
      <button class="sm pl-focus-btn" data-tip="${atCap ? "Max 3 Active Focuses — graduate or unpin one first" : "Pin as an Active Focus"}" onclick="promotePlaylistToFocus(${escJsAttr(p.id)})">📌 Focus</button>
      <button class="sm" onclick="openPlaylistModal(${escJsAttr(p.id)})">Edit</button>
    `;
  } else {
    lifecycleBtns = `
      <button class="sm primary" data-tip="Reactivate into an Active Focus slot" onclick="reactivateFocusGoal(${escJsAttr(p.id)})">Reactivate</button>
      <button class="sm ghost" data-tip="Move back to Playlists" onclick="unpinFocusToPlaylist(${escJsAttr(p.id)})">Unpin</button>
    `;
  }

  const itemsHtml = items.length
    ? items.map((it, idx) => {
        const matchupText = formatMatchLabelText(it, { defaultSelfName: state.username, showRank: false });
        const matchupHtml = formatMatchLabelHtml(it, { defaultSelfName: state.username });
        const srcBadge = it.source === "pro_clip" ? `<span class="pill kind-pro_clip">Top-Rank Clip</span>` : "";
        const itemDur = formatClipDuration(it.start_gf, it.end_gf, it.round_fps || 60)
          || ((Number(it.clip_end_s) > Number(it.clip_start_s)) ? `${Math.max(0.1, Number(it.clip_end_s) - Number(it.clip_start_s)).toFixed(1)}s` : "");
        const durBadge = itemDur ? `<span class="pill mono" style="color:var(--muted);" data-tip="Clip duration">${esc(itemDur)}</span>` : "";
        return `
          <div class="pl-item-row">
            <div style="min-width:0; flex:1;">
              <div class="row" style="gap:6px;">
                <span class="mono" style="font-size:11.5px; color:var(--muted-dim);">#${idx + 1}</span>
                <strong style="font-size:13px;">${esc(it.label || it.note || matchupText)}</strong>
                ${srcBadge}
                ${durBadge}
              </div>
              <div style="font-size:12px; color:var(--muted); margin-top:2px;">
                ${matchupHtml}${it.note && it.note !== it.label ? ` — ${esc(it.note.slice(0, 120))}` : ""}
              </div>
            </div>
            <div class="row" style="gap:4px; flex-wrap:nowrap;">
              <button class="sm ghost" onclick="movePlaylistItem(${escJsAttr(p.id)}, ${idx}, -1)" ${idx === 0 ? "disabled" : ""} data-tip="Move up" aria-label="Move up">↑</button>
              <button class="sm ghost" onclick="movePlaylistItem(${escJsAttr(p.id)}, ${idx}, 1)" ${idx === items.length - 1 ? "disabled" : ""} data-tip="Move down" aria-label="Move down">↓</button>
              <button class="sm danger" onclick="removePlaylistItem(${escJsAttr(p.id)}, ${escJsAttr(it.item_id)})" data-tip="Remove clip" aria-label="Remove clip">✕</button>
            </div>
          </div>
        `;
      }).join("")
    : `<div style="font-size:12.5px; color:var(--muted); margin-top:8px;">${sectionType === "focus" ? "Mental cue — add clips directly from Replay Studio or Notes." : "Empty playlist — add moments from Replay Studio, Notes, or Top-Rank Search."}</div>`;

  const cardCls = sectionType === "focus" ? "is-focus" : (sectionType === "graduated" ? "is-graduated" : "");
  const titlePrefix = sectionType === "focus"
    ? `<span class="dot"></span>`
    : (sectionType === "graduated" ? `<span style="color:var(--emerald-fg);">✓</span>` : "");

  return `
    <div class="pl-manager-card ${cardCls}">
      <div class="row-between">
        <div>
          <div class="row" style="gap:8px;">
            ${titlePrefix}
            <strong style="font-size:15.5px;">${esc(p.name)}</strong>
            ${clipPill}
            ${matchupPill}
            ${sharePill}
            ${gradDatePill}
          </div>
          ${p.description ? `<div style="font-size:12.5px; color:var(--muted); margin-top:3px;">${esc(p.description)}</div>` : ""}
        </div>
        <div class="row" style="gap:6px;">
          ${watchBtn}
          ${lifecycleBtns}
          <button class="sm danger" onclick="deletePlaylistById(${escJsAttr(p.id)})">Delete</button>
        </div>
      </div>
      ${itemsHtml}
    </div>
  `;
}

function renderPlaylistsManager() {
  const host = document.getElementById("playlistsManagerHost");
  if (!host) return;
  const pls = state.playlists || [];
  const activeFocuses = pls.filter(p => p.is_focus && p.focus_status === "active");
  const refPlaylists = pls.filter(p => !p.is_focus);
  const graduatedFocuses = pls.filter(p => p.is_focus && p.focus_status === "graduated");

  if (!pls.length) {
    host.innerHTML = `
      <div class="card empty-card">
        No focuses or playlists yet. Pin up to 3 Active Focuses as your current training priorities, or create playlists to watch &amp; practice in Takeover.<br/>
        <div class="row" style="justify-content:center; margin-top:12px;">
          <button class="emerald sm" onclick="openGoalModal()">+ Add Active Focus</button>
          <button class="primary sm" onclick="openPlaylistModal()">+ New Playlist</button>
        </div>
      </div>
    `;
    return;
  }

  const focusSection = `
    <div class="pl-section-head">
      <div>
        <div class="pl-section-title">
          <span class="dot"></span>
          <span>Active Focuses</span>
          <span class="pill ready mono">${activeFocuses.length}/3</span>
        </div>
        <div class="pl-section-sub">Pinned playlists (max 3) — your current training priorities.</div>
      </div>
      <button class="sm" onclick="openGoalModal()" ${activeFocuses.length >= 3 ? `data-tip="Max 3 active focuses — graduate or unpin one first"` : ""}>+ Add Focus</button>
    </div>
    ${activeFocuses.length
      ? activeFocuses.map(p => _renderPlaylistManagerCardHtml(p, "focus")).join("")
      : `<div class="card empty-inline">Pinned playlists (max 3) — your current training priorities. Pin any playlist with <strong>📌 Focus</strong> or click <strong>+ Add Focus</strong>.</div>`}
  `;

  const refSection = `
    <div class="pl-section-head">
      <div>
        <div class="pl-section-title">
          <span>Playlists</span>
          <span class="pill mono">${refPlaylists.length}</span>
        </div>
        <div class="pl-section-sub">All playlists — watch, practice in Takeover, or pin any as an Active Focus.</div>
      </div>
      <button class="sm" onclick="openPlaylistModal()">+ New Playlist</button>
    </div>
    ${refPlaylists.length
      ? refPlaylists.map(p => _renderPlaylistManagerCardHtml(p, "reference")).join("")
      : `<div class="card empty-inline">All playlists — watch, practice in Takeover, or pin any as an Active Focus.</div>`}
  `;

  const gradSection = graduatedFocuses.length
    ? `
      <div class="pl-section-head">
        <div class="pl-section-title">
          <span>Graduated Focuses</span>
          <span class="pill mono">${graduatedFocuses.length}</span>
        </div>
      </div>
      ${graduatedFocuses.map(p => _renderPlaylistManagerCardHtml(p, "graduated")).join("")}
    `
    : "";

  host.innerHTML = focusSection + refSection + gradSection;
}

// ---------- UNIFIED CREATE / EDIT PLAYLIST & ACTIVE FOCUS MODAL ----------
function syncPlaylistModalFocusUi() {
  const isEdit = Boolean(state.editingPlaylistId);
  const pinEl = document.getElementById("plPinFocusCheck");
  const isFocus = Boolean(pinEl && pinEl.checked);
  const titleEl = document.getElementById("playlistModalTitle");
  const btnEl = document.getElementById("btnSavePlaylist");
  if (titleEl) {
    titleEl.textContent = isEdit
      ? (isFocus ? "Edit Active Focus" : "Edit Playlist")
      : (isFocus ? "New Active Focus" : "New Playlist");
  }
  if (btnEl) {
    btnEl.textContent = isFocus ? "Save Focus" : "Save Playlist";
  }
}

function openPlaylistModal(plId = null, onCreated = null, opts = null) {
  const existing = plId ? findCollectionById(plId) : null;
  const prefill = (opts && opts.prefill) || null;
  const defaultPinFocus = Boolean(opts && opts.defaultPinFocus);

  state.editingPlaylistId = existing ? existing.id : null;
  state.editingGoalId = existing ? existing.id : null;
  state.pendingGoalSeedMoments = (prefill && Array.isArray(prefill.seed_moments) && prefill.seed_moments.length)
    ? prefill.seed_moments
    : null;
  _onPlaylistCreatedCb = onCreated;

  const modalEl = document.getElementById("playlistModal");
  const nameEl = document.getElementById("plNameInput");
  const descEl = document.getElementById("plDescInput");
  const matchupEl = document.getElementById("plMatchupInput");
  const pinEl = document.getElementById("plPinFocusCheck");
  const pinRowEl = document.getElementById("plPinFocusRow");
  const pinLblEl = document.getElementById("plPinFocusLabel");
  const pinHintEl = document.getElementById("plPinFocusHint");
  const seedNotice = document.getElementById("plSeedNotice");
  const tagHost = document.getElementById("plTagPicker");

  const activeCount = getActiveFocuses().length;
  const isAlreadyActiveFocus = Boolean(
    existing && existing.is_focus && (existing.focus_status || existing.status || "active") === "active"
  );
  const atCapForNewFocus = !isAlreadyActiveFocus && activeCount >= 3;

  if (nameEl) nameEl.value = existing ? (existing.name || existing.title || "") : ((prefill && (prefill.name || prefill.title)) || "");
  if (descEl) descEl.value = existing ? (existing.description || "") : ((prefill && prefill.description) || "");
  if (matchupEl) matchupEl.value = existing ? (existing.matchup_char || "") : ((prefill && prefill.matchup_char) || "");

  if (modalEl) {
    modalEl.dataset.sourceInsightId = (prefill && prefill.source_insight_id) || (existing && existing.source_insight_id) || "";
    modalEl.dataset.trendHint = (prefill && prefill.trend_hint) || "";
  }

  if (pinLblEl) pinLblEl.textContent = `Pin as Active Focus (${activeCount}/3)`;
  if (pinEl) {
    if (isAlreadyActiveFocus) {
      pinEl.checked = true;
      pinEl.disabled = false;
    } else if (atCapForNewFocus) {
      pinEl.checked = false;
      pinEl.disabled = true;
    } else {
      pinEl.checked = defaultPinFocus;
      pinEl.disabled = false;
    }
  }
  if (pinRowEl) pinRowEl.classList.toggle("is-disabled", Boolean(atCapForNewFocus));
  if (pinHintEl) {
    if (atCapForNewFocus) {
      pinHintEl.textContent = "(3/3 active — graduate or unpin one first)";
      pinHintEl.classList.remove("hidden");
    } else {
      pinHintEl.textContent = "";
      pinHintEl.classList.add("hidden");
    }
  }

  const initialTags = existing
    ? (existing.tags || [])
    : ((prefill && Array.isArray(prefill.tags)) ? prefill.tags : []);
  initTagPicker("playlistModal", initialTags, { buttonLabel: "+ Add tags" });
  if (tagHost) {
    tagHost.innerHTML = renderTagPickerHtml("playlistModal", initialTags, { buttonLabel: "+ Add tags" });
  }

  if (seedNotice) {
    if (state.pendingGoalSeedMoments && state.pendingGoalSeedMoments.length) {
      seedNotice.textContent = `✦ Includes ${state.pendingGoalSeedMoments.length} verified clip${state.pendingGoalSeedMoments.length === 1 ? "" : "s"} ready to Watch & Train`;
      seedNotice.classList.remove("hidden");
    } else {
      seedNotice.classList.add("hidden");
    }
  }

  syncPlaylistModalFocusUi();

  if (modalEl) modalEl.classList.remove("hidden");
  if (nameEl) nameEl.focus();
}

function closePlaylistModal() {
  if (typeof closeAllTagPickers === "function") closeAllTagPickers();
  state.editingPlaylistId = null;
  state.editingGoalId = null;
  state.pendingGoalSeedMoments = null;
  _onPlaylistCreatedCb = null;
  const modalEl = document.getElementById("playlistModal");
  if (modalEl) modalEl.classList.add("hidden");
}

async function savePlaylistModal() {
  const name = document.getElementById("plNameInput").value.trim();
  if (!name) return toast("Enter a name");
  const description = document.getElementById("plDescInput").value.trim();
  const matchupEl = document.getElementById("plMatchupInput");
  const matchup_char = matchupEl ? matchupEl.value.trim() : "";
  const tags = getTagPickerTags("playlistModal");
  const pinEl = document.getElementById("plPinFocusCheck");
  const pinFocus = Boolean(pinEl && !pinEl.disabled && pinEl.checked);
  const modalEl = document.getElementById("playlistModal");
  const sourceInsightId = (modalEl && modalEl.dataset.sourceInsightId) || "";
  const trendHint = (modalEl && modalEl.dataset.trendHint) || "";

  const btn = document.getElementById("btnSavePlaylist");
  btn.disabled = true;
  try {
    let savedPl = null;
    if (state.editingPlaylistId) {
      const patchBody = {
        name,
        description,
        matchup_char,
        tags,
        is_focus: pinFocus,
        focus_status: pinFocus ? "active" : null,
      };
      const res = await api(`/api/playlists/${encodeURIComponent(state.editingPlaylistId)}`, {
        method: "PATCH",
        body: JSON.stringify(patchBody),
      });
      savedPl = res.playlist;
      toast(pinFocus ? "Active Focus updated" : "Playlist updated");
    } else {
      const createBody = {
        name,
        description,
        kind: "drill",
        matchup_char,
        tags,
        is_focus: pinFocus,
        focus_status: pinFocus ? "active" : null,
        owner: state.username || DEFAULT_USER,
      };
      if (sourceInsightId) createBody.source_insight_id = sourceInsightId;
      if (trendHint) createBody.trend_hint = trendHint;
      if (state.pendingGoalSeedMoments && state.pendingGoalSeedMoments.length) {
        createBody.items = state.pendingGoalSeedMoments;
      }
      const res = await api("/api/playlists", {
        method: "POST",
        body: JSON.stringify(createBody),
      });
      savedPl = res.playlist;
      toast(pinFocus ? "Active Focus pinned!" : "Playlist created!");
    }
    const cb = _onPlaylistCreatedCb;
    closePlaylistModal();
    if (typeof _syncCollectionsAfterChange === "function") {
      await _syncCollectionsAfterChange();
    } else {
      await loadPlaylists(state.activeView === "notes" && state.notesSubView === "playlists");
      if (state.activeView === "home") await loadHomeWorkbench();
    }
    if (cb && savedPl) cb(savedPl);
  } catch (err) {
    toast(err.message);
  } finally {
    btn.disabled = false;
  }
}

async function deletePlaylistById(plId) {
  if (!confirm("Delete this collection? (Your underlying notes and replays will not be deleted.)")) return;
  try {
    await api(`/api/playlists/${encodeURIComponent(plId)}`, { method: "DELETE" });
    toast("Deleted");
    await Promise.all([loadPlaylists(true), loadFocusGoals()]);
    if (state.activeView === "home") await loadHomeWorkbench();
  } catch (err) {
    toast(err.message);
  }
}

async function movePlaylistItem(plId, idx, delta) {
  const pl = findCollectionById(plId);
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
    toast("Removed clip");
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
    toast(res.queued > 0 ? `Creating ${res.queued} Takeover state(s) in background...` : "All moments are ready to train!");
    if (res.queued > 0) setTimeout(() => loadPlaylists(true), 3000);
  } catch (err) {
    toast(err.message);
  }
}

// ---------- UNIFIED ADD-TO-COLLECTION PICKER MODAL ----------
function openAddToPlaylistModal(itemPayload) {
  state.addToPlPayload = itemPayload;
  const host = document.getElementById("addToPlBody");
  const activeFocuses = getActiveFocuses();
  const refPlaylists = getReferencePlaylists();

  const renderRow = (p, isFocus) => {
    const already = collectionHasItem(p, itemPayload.item_id);
    const cnt = Number(p.item_count ?? (p.items || []).length);
    return `
      <div class="pl-item-row" style="margin-top:0; margin-bottom:7px;">
        <div class="row" style="gap:7px; min-width:0;">
          ${isFocus ? `<span class="dot"></span>` : ""}
          <div style="min-width:0;">
            <strong style="font-size:13.5px;">${esc(p.name || p.title)}</strong>
            <div style="font-size:11.5px; color:var(--muted);">${cnt} clip${cnt === 1 ? "" : "s"}</div>
          </div>
        </div>
        <button class="sm ${already ? "emerald" : "primary"}" onclick="toggleItemInAddModal(${escJsAttr(p.id)})">
          ${already ? "✓ Added" : "+ Add"}
        </button>
      </div>
    `;
  };

  const focusBlock = activeFocuses.length
    ? `<div class="section-eyebrow" style="margin-bottom:6px;">ACTIVE FOCUSES</div>` + activeFocuses.map(p => renderRow(p, true)).join("")
    : "";
  const refBlock = refPlaylists.length
    ? `<div class="section-eyebrow" style="margin:${activeFocuses.length ? "10px" : "0"} 0 6px;">REFERENCE PLAYLISTS</div>` + refPlaylists.map(p => renderRow(p, false)).join("")
    : "";

  host.innerHTML = `
    <div style="font-size:12.5px; color:var(--muted); margin-bottom:10px;">
      Moment: <strong>${esc(itemPayload.label || itemPayload.quark_id)}</strong>
    </div>
    ${focusBlock}${refBlock}
    ${(!focusBlock && !refBlock) ? `<div class="empty-inline">No focuses or playlists yet. Create one below!</div>` : ""}
    <div style="margin-top:10px; border-top:1px solid var(--border-soft); padding-top:10px; text-align:right;">
      <button class="sm" onclick="createPlaylistAndAddCurrent()">+ New Playlist</button>
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

async function toggleItemInAddModal(plId) {
  if (!state.addToPlPayload) return;
  const col = findCollectionById(plId);
  const already = collectionHasItem(col, state.addToPlPayload.item_id);
  try {
    const res = await api(`/api/playlists/${encodeURIComponent(plId)}/items`, {
      method: "POST",
      body: JSON.stringify(
        already
          ? { action: "remove", item_id: state.addToPlPayload.item_id }
          : { action: "add", item: state.addToPlPayload }
      ),
    });
    toast(already ? `Removed from "${res.playlist.name}"` : `Saved to "${res.playlist.name}"`);
    await loadPlaylists(false);
    if (state.activeView === "home") await loadHomeWorkbench();
    openAddToPlaylistModal(state.addToPlPayload);
  } catch (err) {
    toast(err.message);
  }
}

async function confirmAddItemToPlaylist(plId) {
  return toggleItemInAddModal(plId);
}

// =========================================================================
// WATCH MODE / MOMENT VIEWER (LOOPING CLIP + GROUFLON CANVAS OVERLAYS)
// =========================================================================
function _activeWatchPlaylist() {
  if (state.watchPlaylistId === "__adhoc__" && Array.isArray(state.watchAdhocItems)) {
    return {
      id: "__adhoc__",
      name: state.watchAdhocTitle || "Moment Viewer",
      items: state.watchAdhocItems,
    };
  }
  return findCollectionById(state.watchPlaylistId);
}

function openSingleClipWatchModal(clipItem, title = "Moment Viewer") {
  if (!clipItem) return;
  if (typeof video !== "undefined" && video) video.pause();
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  if (typeof stopAllStudioCardPreviews === "function") stopAllStudioCardPreviews();
  state.watchAdhocItems = [clipItem];
  state.watchAdhocTitle = title || "Moment Viewer";
  state.watchPlaylistId = "__adhoc__";
  state.watchIndex = 0;
  state.watchOverlay = null;
  state.watchEditingNote = false;
  state.watchEditDraft = "";

  document.getElementById("watchTitle").textContent = state.watchAdhocTitle;
  document.getElementById("watchModal").classList.remove("hidden");
  if (typeof pushModalHistory === "function") pushModalHistory("watch");
  if (typeof syncAudioControls === "function") syncAudioControls();
  syncOverlayButtons();
  mountWatchItem();
}

async function openWatchMode(plId, startIdx = 0) {
  if (typeof video !== "undefined" && video) video.pause();
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  if (typeof stopAllStudioCardPreviews === "function") stopAllStudioCardPreviews();
  if (!findCollectionById(plId)) {
    await loadPlaylists(false);
  }
  const pl = findCollectionById(plId);
  if (!pl || !(pl.items || []).length) {
    toast("No clips in this collection yet — add clips during review.");
    return;
  }
  state.watchAdhocItems = null;
  state.watchAdhocTitle = "";
  state.watchPlaylistId = plId;
  state.watchIndex = Math.max(0, Math.min(pl.items.length - 1, startIdx));
  state.watchOverlay = null;
  state.watchEditingNote = false;
  state.watchEditDraft = "";

  document.getElementById("watchTitle").textContent = pl.name || pl.title || "Watch Mode";
  document.getElementById("watchModal").classList.remove("hidden");
  if (typeof pushModalHistory === "function") pushModalHistory("watch");
  if (typeof syncAudioControls === "function") syncAudioControls();
  syncOverlayButtons();
  mountWatchItem();
}

function closeWatchMode({ fromPopstate = false, consumeSentinel = false } = {}) {
  const wasOpen = Boolean(state.watchPlaylistId);
  if (state.watchRaf) { cancelAnimationFrame(state.watchRaf); state.watchRaf = null; }
  const wv = document.getElementById("watchVideo");
  if (wv) { try { wv.pause(); wv.removeAttribute("src"); wv.load(); } catch (_) {} }
  state.watchPlaylistId = null;
  state.watchAdhocItems = null;
  state.watchAdhocTitle = "";
  state.watchOverlay = null;
  state.watchEditingNote = false;
  state.watchEditDraft = "";
  const modal = document.getElementById("watchModal");
  if (modal) modal.classList.add("hidden");
  if (fromPopstate) {
    if (state._activeModalHistory === "watch") state._activeModalHistory = null;
  } else if (wasOpen && !consumeSentinel && typeof popModalHistory === "function") {
    popModalHistory("watch");
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
  if (!pl || pl.items.length <= 1) return;
  state.watchIndex = (state.watchIndex - 1 + pl.items.length) % pl.items.length;
  mountWatchItem();
}

function nextWatchItem() {
  const pl = _activeWatchPlaylist();
  if (!pl || pl.items.length <= 1) return;
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
  const isResp = Boolean(it.is_response_clip || it.source === "takeover_response");
  const startS = isResp ? 0 : Math.max(0, Number(it.clip_start_s ?? 0));
  const endS = isResp
    ? Math.max(0.25, (Number.isFinite(wv.duration) && wv.duration > 0) ? wv.duration : Number(it.clip_end_s ?? 2.0))
    : Math.max(startS + 0.5, Number(it.clip_end_s ?? (startS + 3.0)));
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
  drawOverlayOnCanvas(wc, state.watchOverlay, it.round_start_gf || it.start_gf || 0, off, state.overlayOpts);
}

function _resolveWatchItemEditTarget(it) {
  if (!it || !it.item_id || it.editable === false) return null;
  const src = String(it.edit_source || it.source || "").toLowerCase();
  if (src === "pro_clip" || src === "bookmark" || src === "set_note") return null;
  if (src === "select_mark") return { id: it.item_id, apiSource: "select_mark" };
  if (src === "takeover_response" || it.is_response_clip) return { id: it.item_id, apiSource: "takeover_response" };
  if (src === "annotation") return { id: it.item_id, apiSource: "annotation" };
  if ((state.selectMarks || []).some(x => x.id === it.item_id)) {
    return { id: it.item_id, apiSource: "select_mark" };
  }
  const ann = (state.annotations || []).find(x => x.id === it.item_id);
  if (ann) {
    return { id: it.item_id, apiSource: ann.source === "takeover_response" ? "takeover_response" : "annotation" };
  }
  const hub = (state.hubNotes || []).find(x => x.id === it.item_id);
  if (hub && hub.editable) {
    if (hub.source === "select_mark") return { id: it.item_id, apiSource: "select_mark" };
    if (hub.source === "takeover_response") return { id: it.item_id, apiSource: "takeover_response" };
    if (hub.source === "annotation") return { id: it.item_id, apiSource: "annotation" };
  }
  return null;
}

function _autoSizeWatchNoteTextarea(ta) {
  if (!ta) return;
  ta.style.height = "auto";
  const nextH = Math.max(92, Math.min(340, ta.scrollHeight + 10));
  ta.style.height = `${nextH}px`;
}

function _isDefaultSelectNoteText(it) {
  if (!it) return false;
  if (it.is_default_select_note) return true;
  const raw = String(it.note || "").trim();
  return (it.source === "select_mark" || it.edit_source === "select_mark") && /^In-game Select mark #\d+\b/i.test(raw);
}

function _formatWatchMatchupHtml(it) {
  return formatMatchLabelHtml(it, { defaultSelfName: state.username });
}

function shareActiveWatchItem() {
  const pl = _activeWatchPlaylist();
  if (!pl) return;
  const it = pl.items[state.watchIndex];
  if (!it) return;
  shareMomentBundle({
    ...it,
    note: _isDefaultSelectNoteText(it) ? "" : (it.note || ""),
    response_clip_url: it.is_response_clip ? (it.response_clip_url || it.video_url || "") : (it.response_clip_url || ""),
    response_overlay_url: it.is_response_clip ? (it.response_overlay_url || it.overlay_url || "") : (it.response_overlay_url || ""),
  });
}

function renderWatchSidePanel() {
  const pl = _activeWatchPlaylist();
  const side = document.getElementById("watchSidePanel");
  if (!pl || !side) return;
  const it = pl.items[state.watchIndex];
  if (!it) return;

  const isResp = Boolean(it.is_response_clip || it.source === "takeover_response");
  const matchupHtml = _formatWatchMatchupHtml(it);
  const hpPill = (isResp && typeof _formatHpSwingPill === "function") ? _formatHpSwingPill(it) : "";
  const originPill = renderSystemOriginBadgeHtml(it);
  const watchDurTxt = formatClipDuration(it.start_gf, it.end_gf, it.round_fps || 60)
    || ((Number(it.clip_end_s) > Number(it.clip_start_s)) ? `${Math.max(0.1, Number(it.clip_end_s) - Number(it.clip_start_s)).toFixed(1)}s` : "");
  const watchDurPill = watchDurTxt
    ? `<span class="pill mono" style="color:var(--muted);" data-tip="Clip duration">${esc(watchDurTxt)}</span>`
    : "";
  const displayTags = getDisplayTagsForItem(it);
  const tagsHtml = (!state.watchEditingNote && displayTags.length)
    ? displayTags.map(t => `<span class="pill note">${esc(t)}</span>`).join("")
    : "";

  const rawNote = String(it.note || "").trim();
  const displayNote = _isDefaultSelectNoteText(it) ? "" : rawNote;
  const rawLabel = String(it.label || "").trim();
  const showLabel = Boolean(rawLabel && rawLabel !== displayNote && rawLabel !== rawNote);

  const editTarget = _resolveWatchItemEditTarget(it);
  const editBtnHtml = (editTarget && !state.watchEditingNote)
    ? `<button type="button" class="sm ghost watch-note-edit-btn" data-tip="Edit note and tags in place" onclick="startWatchNoteEdit()">Edit</button>`
    : "";

  const noteAreaHtml = (editTarget && state.watchEditingNote)
    ? `
      <div class="watch-inline-editor">
        <textarea id="watchInlineNoteText"
                  class="inline-note-textarea"
                  placeholder="Write your note, counterplay, or takeaway..."
                  oninput="onWatchInlineNoteInput(this)"
                  onkeydown="onWatchInlineNoteKeydown(event)">${esc(state.watchEditDraft)}</textarea>
        ${renderTagPickerHtml("watchInline", null, { buttonLabel: "+ Add tags" })}
        <div class="watch-inline-edit-actions">
          <button type="button" class="sm" data-tip="Cancel editing (Esc)" onclick="cancelWatchNoteEdit()">Cancel</button>
          <button id="btnSaveWatchInlineNote" type="button" class="sm emerald" data-tip="Save changes (Ctrl+Enter)" onclick="saveWatchNoteEdit()">Save</button>
        </div>
      </div>
    `
    : `<div class="note-body ${displayNote ? "" : "is-placeholder"}" style="margin:0; font-size:13.5px;">${esc(displayNote || "No written note for this clip yet.")}</div>`;

  const alreadyInSameMatchStudio = Boolean(
    state.activeView === "studio" &&
    state.activeManifest &&
    it.quark_id &&
    state.activeManifest.quark_id === it.quark_id
  );
  const openStudioBtn = (it.source !== "pro_clip" && it.quark_id && !alreadyInSameMatchStudio)
    ? `<button class="sm primary" onclick="openWatchItemInStudio()">Open in Studio →</button>` : "";
  const canShareWatch = Boolean(it.source !== "pro_clip" && (isResp ? (it.video_url || it.response_clip_url) : (it.quark_id && it.round_id)));
  const shareBtn = canShareWatch
    ? `<button class="sm" data-tip="Share a revocable link or download MP4" onclick="shareActiveWatchItem()">↗ Share Moment</button>` : "";
  const sharedPill = (it.cloud_visibility === "unlisted" && it.cloud_share_url)
    ? `<span class="pill shared-pill" data-tip="Active unlisted share link — click to copy or manage" onclick="shareActiveWatchItem()">🔗 Shared</span>` : "";
  const pracBtn = canTakeoverNow()
    ? `<button class="sm amber practice-btn" data-tip="Launch Replay Takeover in FBNeo for this clip" onclick="launchPlaylistItemTakeover(${escJsAttr(pl.id)}, ${state.watchIndex}, this)">▶ Practice</button>` : "";

  const headerBadgesHtml = `${originPill}${watchDurPill}${sharedPill}${hpPill}`;
  side.innerHTML = `
    ${headerBadgesHtml ? `<div class="row" style="gap:6px;">${headerBadgesHtml}</div>` : ""}
    <div style="font-weight:800; font-size:15px;">${matchupHtml}</div>
    ${showLabel ? `<div style="font-weight:700; font-size:13.5px; color:var(--accent-fg);">${esc(rawLabel)}</div>` : ""}
    ${tagsHtml ? `<div class="row" style="gap:5px;">${tagsHtml}</div>` : ""}
    <div class="watch-note-section">
      <div class="watch-note-head">
        <span class="section-eyebrow">NOTE</span>
        ${editBtnHtml}
      </div>
      ${noteAreaHtml}
    </div>
    <div class="row" style="margin-top:auto; padding-top:8px; gap:6px;">
      ${openStudioBtn}
      ${pracBtn}
      ${shareBtn}
    </div>
  `;
}

function startWatchNoteEdit() {
  const pl = _activeWatchPlaylist();
  if (!pl) return;
  const it = pl.items[state.watchIndex];
  if (!_resolveWatchItemEditTarget(it)) return;
  state.watchEditingNote = true;
  state.watchEditDraft = _isDefaultSelectNoteText(it) ? "" : String(it.note || "");
  initTagPicker("watchInline", getDisplayTagsForItem(it), { buttonLabel: "+ Add tags" });
  renderWatchSidePanel();
  const ta = document.getElementById("watchInlineNoteText");
  if (ta) {
    _autoSizeWatchNoteTextarea(ta);
    ta.focus();
    const len = ta.value.length;
    ta.setSelectionRange(len, len);
  }
}

function cancelWatchNoteEdit() {
  if (!state.watchEditingNote) return;
  closeAllTagPickers();
  state.watchEditingNote = false;
  state.watchEditDraft = "";
  renderWatchSidePanel();
}

function onWatchInlineNoteInput(ta) {
  if (!ta) return;
  state.watchEditDraft = ta.value;
  _autoSizeWatchNoteTextarea(ta);
}

function onWatchInlineNoteKeydown(ev) {
  if (!ev) return;
  if (ev.key === "Escape") {
    ev.preventDefault();
    ev.stopPropagation();
    cancelWatchNoteEdit();
    return;
  }
  if ((ev.ctrlKey || ev.metaKey) && ev.key === "Enter") {
    ev.preventDefault();
    ev.stopPropagation();
    saveWatchNoteEdit();
  }
}

function _propagateEditedNoteAcrossState(itemId, apiSource, newNote, oldNote = "", newTags = null, newKind = null) {
  const isResp = apiSource === "takeover_response";
  const isSel = apiSource === "select_mark";
  const tagsCopy = Array.isArray(newTags) ? [...newTags] : null;

  if (Array.isArray(state.annotations)) {
    state.annotations.forEach(a => {
      if (a && a.id === itemId) {
        a.note = newNote;
        if (tagsCopy !== null) a.tags = [...tagsCopy];
        if (newKind) a.kind = newKind;
      }
    });
  }
  if (Array.isArray(state.selectMarks)) {
    state.selectMarks.forEach(s => {
      if (s && s.id === itemId) {
        s.note = newNote;
        if (tagsCopy !== null) s.tags = [...tagsCopy];
        if (newKind) s.kind = newKind;
        s.needs_note = !newNote;
        s.is_default_select_note = false;
      }
    });
  }
  if (Array.isArray(state.hubNotes)) {
    state.hubNotes.forEach(n => {
      if (n && n.id === itemId) {
        n.note = newNote;
        if (tagsCopy !== null) n.tags = [...tagsCopy];
        if (newKind) n.kind = newKind;
        n.needs_note = !newNote && !isResp;
        if (isSel) n.is_default_select_note = false;
      }
    });
  }
  const updateItem = item => {
    if (!item || item.item_id !== itemId) return;
    const prevNote = String(item.note || oldNote || "").trim();
    const prevLabel = String(item.label || "").trim();
    item.note = newNote;
    if (tagsCopy !== null) item.tags = [...tagsCopy];
    if (newKind) item.kind = newKind;
    item.needs_note = !newNote && !isResp;
    if (isSel) item.is_default_select_note = false;
    if (prevLabel && (prevLabel === prevNote || prevNote.startsWith(prevLabel))) {
      item.label = newNote ? newNote.slice(0, 120) : "";
    }
  };
  [state.playlists, state.focusGoals, state.graduatedGoals].forEach(list => {
    if (!Array.isArray(list)) return;
    list.forEach(col => {
      if (col && Array.isArray(col.items)) col.items.forEach(updateItem);
    });
  });
  if (Array.isArray(state.watchAdhocItems)) {
    state.watchAdhocItems.forEach(updateItem);
  }
}

async function saveWatchNoteEdit() {
  const pl = _activeWatchPlaylist();
  if (!pl) return;
  const it = pl.items[state.watchIndex];
  const target = _resolveWatchItemEditTarget(it);
  if (!target) return;

  const ta = document.getElementById("watchInlineNoteText");
  const note = String(ta ? ta.value : state.watchEditDraft || "").trim();
  const tags = getTagPickerTags("watchInline");
  const savedKind = resolveSavedKindForItem(target.apiSource, note, tags);
  const oldNote = String(it.note || "");
  const btn = document.getElementById("btnSaveWatchInlineNote");
  if (btn) btn.disabled = true;

  try {
    if (target.apiSource === "select_mark") {
      await api(`/api/queue/${encodeURIComponent(target.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ note, tags, kind: savedKind }),
      });
    } else {
      await api(`/api/annotations/${encodeURIComponent(target.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ note, tags, kind: savedKind }),
      });
    }

    closeAllTagPickers();
    _propagateEditedNoteAcrossState(target.id, target.apiSource, note, oldNote, tags, savedKind);
    state.watchEditingNote = false;
    state.watchEditDraft = "";
    renderWatchSidePanel();

    if (state.activeView === "studio") {
      if (typeof renderNotesList === "function") renderNotesList();
      if (typeof updateHudAndTimeline === "function") updateHudAndTimeline();
    } else if (state.activeView === "notes") {
      if (state.notesSubView === "notebook" && typeof renderHubNotes === "function") renderHubNotes();
      else if (state.notesSubView === "playlists") renderPlaylistsManager();
    }
    toast("Note saved");
  } catch (err) {
    toast("Save failed: " + err.message);
    if (btn) btn.disabled = false;
  }
}

async function mountWatchItem() {
  if (state.watchRaf) { cancelAnimationFrame(state.watchRaf); state.watchRaf = null; }
  const pl = _activeWatchPlaylist();
  if (!pl || !pl.items.length) return;
  const it = pl.items[state.watchIndex];
  if (!it) return;
  const isResp = Boolean(it.is_response_clip || it.source === "takeover_response");
  const isSingleClip = state.watchPlaylistId === "__adhoc__" || pl.items.length <= 1;

  state.watchEditingNote = false;
  state.watchEditDraft = "";

  const progPill = document.getElementById("watchProgressPill");
  if (progPill) {
    progPill.textContent = `Clip ${state.watchIndex + 1} of ${pl.items.length}`;
    progPill.classList.toggle("hidden", isSingleClip);
  }
  const prevBtn = document.getElementById("btnWatchPrev");
  if (prevBtn) prevBtn.classList.toggle("hidden", isSingleClip);
  const nextBtn = document.getElementById("btnWatchNext");
  if (nextBtn) nextBtn.classList.toggle("hidden", isSingleClip);

  renderWatchSidePanel();

  const wv = document.getElementById("watchVideo");
  const wc = document.getElementById("watchOverlayCanvas");
  if (wc) wc.getContext("2d").clearRect(0, 0, wc.width, wc.height);
  state.watchOverlay = null;

  if (!it.has_video || !it.video_url) {
    try { wv.pause(); wv.removeAttribute("src"); wv.load(); } catch (_) {}
    toast("No rendered video for this clip yet — use Open in Studio.");
    return;
  }

  if (it.overlay_url && !it.is_standalone_clip) {
    fetchOverlayData(it.overlay_url).then(ov => {
      if (state.watchPlaylistId === pl.id && pl.items[state.watchIndex] === it) {
        state.watchOverlay = ov;
        drawWatchOverlayFrame();
      }
    }).catch(() => {});
  }

  const startS = isResp ? 0 : Math.max(0, Number(it.clip_start_s ?? 0));
  const fixedEndS = Math.max(startS + 0.5, Number(it.clip_end_s ?? (startS + 3.0)));
  const getEndS = () => isResp
    ? Math.max(0.25, (Number.isFinite(wv.duration) && wv.duration > 0) ? wv.duration : Number(it.clip_end_s ?? 2.0))
    : fixedEndS;

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
    const endS = getEndS();
    const spanS = Math.max(0.1, endS - startS);
    const fill = document.getElementById("watchProgFill");
    const playBtn = document.getElementById("btnWatchPlay");
    if (fill) {
      const rel = Math.max(0, Math.min(spanS, (wv.currentTime || startS) - startS));
      fill.style.width = `${(rel / spanS) * 100}%`;
    }
    if (playBtn) playBtn.textContent = wv.paused ? "▶" : "❚❚";
    drawWatchOverlayFrame();

    if (!wv.paused && (wv.ended || wv.currentTime >= endS - 0.03)) {
      wv.currentTime = startS;
      if (wv.paused) wv.play().catch(() => {});
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
  const prevReturn = (state.activeView === "notes" && state.notesSubView === "playlists")
    ? "playlists"
    : (state.activeView && state.activeView !== "studio" ? state.activeView : (state.returnView || "playlists"));
  closeWatchMode({ consumeSentinel: true });
  state.returnView = prevReturn;
  openMatch(it.quark_id, it.perspective, {
    round_id: it.round_id,
    start_gf: it.start_gf,
    end_gf: it.end_gf,
    anchor_gf: it.anchor_gf,
    autoPlay: false,
  });
}

// =========================================================================
// DIRECT PLAYLIST CLIP PRACTICE (REPLAY TAKEOVER FROM WATCH MODE)
// =========================================================================
async function launchPlaylistItemTakeover(plId, idx, btnEl = null) {
  const pl = plId === "__adhoc__" ? _activeWatchPlaylist() : findCollectionById(plId);
  if (!pl || !pl.items[idx]) return;
  const it = pl.items[idx];
  const rawNote = String(it.note || "").trim();
  const fullNote = (rawNote && !/^In-game Select mark #\d+\b/i.test(rawNote)) ? rawNote : "";
  const isResp = Boolean(it.is_response_clip || it.source === "takeover_response");
  const candidateParent = isResp ? String(it.parent_moment_id || "") : String(it.item_id || "");
  const parentMomentId = (it.source !== "pro_clip" && /^[A-Za-z0-9_-]{3,128}$/.test(candidateParent))
    ? candidateParent
    : "";
  const opts = {
    speed: 1.0,
    speed_scope: "all",
    auto_loop: true,
    note: fullNote,
    round_id: Number(it.round_id || 0),
    ...(parentMomentId ? { parent_moment_id: parentMomentId } : {}),
  };
  if (isResp && it.item_id) {
    await launchDrill(it.item_id, opts, btnEl);
    return;
  }
  if (it.selection_id && it.savestate_ready) {
    await launchDrill(it.selection_id, opts, btnEl);
    return;
  }
  await launchTakeoverPayload({
    quark_id: it.quark_id,
    frame: it.anchor_gf || it.start_gf,
    start_gf: it.start_gf,
    end_gf: it.end_gf,
    player: it.perspective || 1,
    bake_if_missing: true,
    label: it.label || it.note || pl.name || pl.title,
    ...opts,
  }, btnEl);
}
