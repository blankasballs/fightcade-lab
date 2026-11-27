/* Centralized Notes Hub (#notes -> Notebook sub-view) + Inline Mini-Clip Preview */

function setNotesSubView(sub, updateHistory = true) {
  state.notesSubView = sub;
  document.getElementById("subTabNotebook").classList.toggle("active", sub === "notebook");
  document.getElementById("subTabPlaylists").classList.toggle("active", sub === "playlists");
  document.getElementById("notesSubNotebook").classList.toggle("hidden", sub !== "notebook");
  document.getElementById("notesSubPlaylists").classList.toggle("hidden", sub !== "playlists");
  document.getElementById("btnNewPlaylistFromSub").classList.toggle("hidden", sub !== "playlists");
  if (typeof updateDocumentTitle === "function") updateDocumentTitle();
  if (sub === "playlists") {
    stopMiniPreview();
    closeAllHubCardMenus();
    closeNotesTagFilterMenu();
    if (updateHistory && typeof updateAppHistory === "function") {
      updateAppHistory("/#playlists");
    }
    loadPlaylists(true);
  } else {
    if (updateHistory && typeof updateAppHistory === "function") {
      updateAppHistory("/#notes");
    }
    loadHubNotes(false);
  }
}

async function loadHubNotes(force = false) {
  if (state.hubLoaded && !force) {
    renderHubNotesControls();
    renderHubNotes();
    return;
  }
  const u = (state.username || DEFAULT_USER).trim();
  try {
    const data = await api(`/api/notes?username=${encodeURIComponent(u)}`);
    state.hubNotes = data.notes || [];
    state.hubCounts = data.counts || {};
    state.hubAvailableChars = data.available_chars || { self: [], opp: [] };
    state.hubLoaded = true;
    if (state.activeView === "notes" && state.notesSubView === "notebook") {
      renderHubNotesControls();
      renderHubNotes();
    }
  } catch (err) {
    if (state.activeView === "notes") toast("Failed to load notes: " + err.message);
  }
}

function setNotesFilter(f) {
  state.notesFilter = f;
  document.querySelectorAll("#notesFilterChips .chip").forEach(c => c.classList.toggle("active", c.dataset.nfilter === f));
  renderHubNotesControls();
  renderHubNotes();
}

function hasActiveNotesFilters() {
  return Boolean(
    state.notesFilter !== "all" ||
    state.notesSelfChar ||
    state.notesOppChar ||
    (Array.isArray(state.notesTags) && state.notesTags.length > 0) ||
    state.notesSearch
  );
}

function closeNotesTagFilterMenu() {
  if (!state.notesTagFilterOpen) return false;
  state.notesTagFilterOpen = false;
  renderNotesTagFilterControl();
  return true;
}

function toggleNotesTagFilterMenu() {
  const nextOpen = !state.notesTagFilterOpen;
  if (nextOpen) {
    if (typeof closeAllHubCardMenus === "function") closeAllHubCardMenus();
    if (typeof closeAllCollectionPickers === "function") closeAllCollectionPickers();
    if (typeof closeAllTagPickers === "function") closeAllTagPickers();
  }
  state.notesTagFilterOpen = nextOpen;
  renderNotesTagFilterControl();
}

function toggleNotesTagFilter(tag, keepMenuOpen = false) {
  const clean = String(tag || "").trim();
  if (!clean) return;
  if (!Array.isArray(state.notesTags)) state.notesTags = [];
  const lower = clean.toLowerCase();
  const idx = state.notesTags.findIndex(t => String(t || "").toLowerCase() === lower);
  if (idx >= 0) {
    state.notesTags.splice(idx, 1);
  } else {
    state.notesTags.push(clean);
  }
  if (!keepMenuOpen) state.notesTagFilterOpen = false;
  renderHubNotesControls();
  renderHubNotes();
}

function toggleNotesCharFilter(_side, charName) {
  const clean = String(charName || "").trim();
  if (!clean) return;
  if (state.notesSelfChar === clean) {
    state.notesSelfChar = state.notesOppChar || "";
    state.notesOppChar = "";
  } else if (state.notesOppChar === clean) {
    state.notesOppChar = "";
  } else if (!state.notesSelfChar) {
    state.notesSelfChar = clean;
  } else if (!state.notesOppChar) {
    state.notesOppChar = clean;
  } else {
    state.notesOppChar = clean;
  }
  renderHubNotesControls();
  renderHubNotes();
}

function onNotesFilterChanged() {
  const searchEl = document.getElementById("notesSearchInput");
  const selfEl = document.getElementById("notesSelfChar");
  const oppEl = document.getElementById("notesOppChar");
  state.notesSearch = searchEl ? (searchEl.value || "").trim() : "";
  state.notesSelfChar = selfEl ? (selfEl.value || "") : "";
  state.notesOppChar = oppEl ? (oppEl.value || "") : "";
  renderHubNotesControls();
  renderHubNotes();
}

function clearNotesFilters() {
  state.notesFilter = "all";
  state.notesSelfChar = "";
  state.notesOppChar = "";
  state.notesTags = [];
  state.notesTagFilterOpen = false;
  state.notesSearch = "";
  const searchEl = document.getElementById("notesSearchInput");
  if (searchEl) searchEl.value = "";
  document.querySelectorAll("#notesFilterChips .chip").forEach(c => c.classList.toggle("active", c.dataset.nfilter === "all"));
  renderHubNotesControls();
  renderHubNotes();
}

function renderNotesTagFilterControl() {
  const wrap = document.getElementById("notesTagFilterWrap");
  const activeRow = document.getElementById("notesActiveTagsRow");
  const selected = Array.isArray(state.notesTags) ? state.notesTags : [];
  const selectedLower = new Set(selected.map(t => String(t || "").toLowerCase()));
  const rawTags = (state.hubCounts && Array.isArray(state.hubCounts.by_tag)) ? state.hubCounts.by_tag : [];
  const seen = new Set(rawTags.map(t => String(t.tag || "").toLowerCase()));
  const options = [...rawTags];
  selected.forEach(t => {
    const k = String(t || "").toLowerCase();
    if (k && !seen.has(k)) {
      seen.add(k);
      options.push({ tag: t, count: 0 });
    }
  });

  if (wrap) {
    const isOpen = Boolean(state.notesTagFilterOpen);
    const selCount = selected.length;
    const menuHtml = isOpen
      ? `
        <div class="notes-tag-filter-menu" role="listbox" aria-multiselectable="true" onclick="event.stopPropagation()">
          ${options.length
            ? options.map(t => {
                const isSel = selectedLower.has(String(t.tag || "").toLowerCase());
                return `
                  <button type="button" role="option" aria-selected="${isSel ? "true" : "false"}" class="notes-tag-filter-opt${isSel ? " is-selected" : ""}" onclick="event.stopPropagation(); toggleNotesTagFilter(${escJsAttr(t.tag)}, true)">
                    <span class="tag-picker-check" aria-hidden="true">${isSel ? "✓" : "○"}</span>
                    <span class="tag-picker-opt-lbl">${esc(t.tag)}</span>
                    <span class="notes-tag-opt-cnt mono">(${Number(t.count || 0)})</span>
                  </button>
                `;
              }).join("")
            : `<div class="tag-picker-empty">No tags in your notes yet</div>`}
        </div>
      `
      : "";
    wrap.innerHTML = `
      <button type="button" class="sm notes-tag-filter-btn${isOpen ? " is-open" : ""}${selCount ? " has-active" : ""}" aria-haspopup="listbox" aria-expanded="${isOpen ? "true" : "false"}" data-tip="Filter notes by one or more tags" onclick="event.stopPropagation(); toggleNotesTagFilterMenu()">Tags${selCount ? ` (${selCount})` : ""} ▾</button>
      ${menuHtml}
    `;
  }

  if (activeRow) {
    if (selected.length) {
      activeRow.classList.remove("hidden");
      activeRow.innerHTML = selected.map(t => `
        <span class="tag-chip-sel">
          <span>${esc(t)}</span>
          <button type="button" class="tag-chip-x" aria-label="Remove tag filter ${esc(t)}" onclick="toggleNotesTagFilter(${escJsAttr(t)})">×</button>
        </span>
      `).join("");
    } else {
      activeRow.classList.add("hidden");
      activeRow.innerHTML = "";
    }
  }
}

function _getAllNotesChars() {
  const chars = new Set();
  const add = val => {
    const clean = String(val || "").trim();
    if (clean && clean !== "?") chars.add(clean);
  };
  ((state.hubAvailableChars && state.hubAvailableChars.self) || []).forEach(add);
  ((state.hubAvailableChars && state.hubAvailableChars.opp) || []).forEach(add);
  (state.hubNotes || []).forEach(n => {
    add(n.self_char_name);
    add(n.opp_char_name);
  });
  add(state.notesSelfChar);
  add(state.notesOppChar);
  return [...chars].sort((a, b) => a.localeCompare(b));
}

function renderHubNotesControls() {
  const c = state.hubCounts || {};
  const bk = c.by_kind || {};
  const setCnt = (id, n) => { const el = document.getElementById(id); if (el) el.textContent = n != null ? `(${n})` : ""; };
  setCnt("nCntAll", c.total ?? state.hubNotes.length);
  setCnt("nCntMoment", c.moments ?? 0);
  setCnt("nCntResponse", bk.response ?? 0);
  setCnt("nCntSet", c.set_notes ?? 0);

  const allChars = _getAllNotesChars();
  const selfEl = document.getElementById("notesSelfChar");
  const oppEl = document.getElementById("notesOppChar");
  if (selfEl) {
    selfEl.innerHTML = `<option value="">Any Char</option>` + allChars.map(ch =>
      `<option value="${esc(ch)}" ${ch === state.notesSelfChar ? "selected" : ""}>${esc(ch)}</option>`
    ).join("");
  }
  if (oppEl) {
    oppEl.innerHTML = `<option value="">Any Char</option>` + allChars.map(ch =>
      `<option value="${esc(ch)}" ${ch === state.notesOppChar ? "selected" : ""}>${esc(ch)}</option>`
    ).join("");
  }

  const clearBtn = document.getElementById("btnClearNotesFilters");
  if (clearBtn) clearBtn.classList.toggle("hidden", !hasActiveNotesFilters());

  renderNotesTagFilterControl();
}

function getFilteredHubNotes() {
  const q = (state.notesSearch || "").toLowerCase();
  const c1 = (state.notesSelfChar || "").trim();
  const c2 = (state.notesOppChar || "").trim();
  const activeTagsLower = (Array.isArray(state.notesTags) ? state.notesTags : [])
    .map(t => String(t || "").toLowerCase())
    .filter(Boolean);
  return state.hubNotes.filter(n => {
    if (state.notesFilter === "moment" && n.entry_type !== "moment") return false;
    if (state.notesFilter === "set_note" && n.entry_type !== "set_note") return false;
    if (state.notesFilter === "response" && n.kind !== "response" && n.source !== "takeover_response") return false;
    const s = (n.self_char_name || "").trim();
    const o = (n.opp_char_name || "").trim();
    if (c1 && c2) {
      if (!((s === c1 && o === c2) || (s === c2 && o === c1))) return false;
    } else if (c1 || c2) {
      const target = c1 || c2;
      if (s !== target && o !== target) return false;
    }
    const effTags = getDisplayTagsForItem(n);
    if (activeTagsLower.length) {
      const itemTagSet = new Set(effTags.map(t => t.toLowerCase()));
      if (!activeTagsLower.every(req => itemTagSet.has(req))) return false;
    }
    if (q) {
      const hay = [
        n.note, n.opp_player_name, n.self_player_name, n.self_char_name,
        n.opp_char_name, n.kind, n.date_str, n.quark_id,
        ...effTags,
      ].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

const _hubMomentPayloads = {};

function _syncHubCardMenuDom() {
  if (typeof document === "undefined" || typeof document.querySelectorAll !== "function") return;
  const openId = state.hubOpenMenuId || null;
  document.querySelectorAll(".hub-card-more-wrap[data-hub-menu-id]").forEach(wrap => {
    const isOpen = Boolean(openId && wrap.dataset && wrap.dataset.hubMenuId === openId);
    const btn = wrap.querySelector ? wrap.querySelector(".hub-card-more-btn") : null;
    const menu = wrap.querySelector ? wrap.querySelector(".hub-card-more-menu") : null;
    if (btn) {
      if (btn.classList) btn.classList.toggle("is-open", isOpen);
      if (typeof btn.setAttribute === "function") btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
    }
    if (menu && menu.classList) {
      menu.classList.toggle("hidden", !isOpen);
    }
  });
}

function closeAllHubCardMenus(exceptId = null) {
  if (!state.hubOpenMenuId) return false;
  if (exceptId && state.hubOpenMenuId === exceptId) return false;
  state.hubOpenMenuId = null;
  _syncHubCardMenuDom();
  return true;
}

function toggleHubCardMenu(id) {
  if (!id) return;
  const nextOpen = state.hubOpenMenuId !== id;
  if (typeof closeAllCollectionPickers === "function") closeAllCollectionPickers();
  if (typeof closeAllTagPickers === "function") closeAllTagPickers();
  closeNotesTagFilterMenu();
  state.hubOpenMenuId = nextOpen ? id : null;
  _syncHubCardMenuDom();
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("click", ev => {
    const target = ev && ev.target;
    if (state.hubOpenMenuId) {
      if (!(target && typeof target.closest === "function" && target.closest(".hub-card-more-wrap"))) {
        closeAllHubCardMenus();
      }
    }
    if (state.notesTagFilterOpen) {
      if (!(target && typeof target.closest === "function" && target.closest(".notes-tag-filter"))) {
        closeNotesTagFilterMenu();
      }
    }
  });
}

async function toggleHubNoteInCollection(colId, noteId) {
  const col = findCollectionById(colId);
  const itemPayload = _hubMomentPayloads[noteId];
  if (!col || !itemPayload) return;
  const already = collectionHasItem(col, noteId);
  try {
    const res = await api(`/api/playlists/${encodeURIComponent(colId)}/items`, {
      method: "POST",
      body: JSON.stringify(
        already
          ? { action: "remove", item_id: noteId }
          : { action: "add", item: itemPayload }
      ),
    });
    const hubItem = (state.hubNotes || []).find(x => x.id === noteId);
    if (hubItem) {
      const plIds = new Set(hubItem.playlist_ids || []);
      const gIds = new Set(hubItem.goal_ids || []);
      if (already) {
        plIds.delete(colId);
        gIds.delete(colId);
      } else {
        plIds.add(colId);
        if (col.is_focus) gIds.add(colId);
      }
      hubItem.playlist_ids = [...plIds];
      hubItem.goal_ids = [...gIds];
    }
    toast(already ? `Removed from "${res.playlist.name}"` : `Added to "${res.playlist.name}"`);
    await loadPlaylists(false);
    renderHubNotes();
  } catch (err) {
    toast(err.message);
  }
}

function renderHubNoteCardHtml(n, nestedResponses = [], isNested = false) {
  const isResp = n.source === "takeover_response" || n.kind === "response";
  const isPreviewOpen = state.previewNoteId === n.id && n.has_video;
  const isEditing = state.hubEditId === n.id;
  const cardCls = [
    n.entry_type === "set_note" ? "is-set-note" : (n.entry_type === "select_mark" ? "is-select-mark" : ""),
    isResp ? "is-response" : "",
    isNested ? "response-card" : "",
    isPreviewOpen ? "is-expanded" : "",
    isEditing ? "is-editing" : "",
  ].filter(Boolean).join(" ");
  const kindLbl = KIND_LABELS[n.kind] || n.kind || "Note";
  const kindCls = `kind-${n.kind || "note"}`;
  const activeChars = [state.notesSelfChar, state.notesOppChar].filter(Boolean);
  const matchupHtml = `<span class="hub-card-matchup">${formatMatchLabelHtml(n, {
    defaultSelfName: state.username,
    charFilterFn: "toggleNotesCharFilter",
    activeChars,
    stopPropagation: true,
    stackedMobile: true,
  })}</span>`;
  const originPill = renderSystemOriginBadgeHtml(n);
  const respOrphanPill = (isResp && !isNested && n.parent_moment_id)
    ? `<span class="pill cached" data-tip="Linked to parent moment ${esc(n.parent_moment_id)}">↳ Response</span>` : "";
  const sharedPill = (n.cloud_visibility === "unlisted" && n.cloud_share_url)
    ? `<span class="pill shared-pill" data-tip="Active unlisted share link — click to copy or manage" onclick="event.stopPropagation(); shareHubNote(${escJsAttr(n.id)})">🔗 Shared</span>` : "";
  const hpPill = (isResp && typeof _formatHpSwingPill === "function") ? _formatHpSwingPill(n) : "";

  const displayTags = getDisplayTagsForItem(n);
  const tagPills = displayTags.map(t =>
    `<span class="pill note" style="cursor:pointer;" data-tip="Filter notes by tag: ${esc(t)}" onclick="toggleNotesTagFilter(${escJsAttr(t)})">${esc(t)}</span>`
  ).join("");

  let collectionPickerBlock = "";
  if (n.entry_type !== "set_note") {
    _hubMomentPayloads[n.id] = {
      item_id: n.id,
      source: n.source || "moment",
      quark_id: n.quark_id,
      round_id: Number(n.round_id || 0),
      perspective: Number(n.perspective || 1),
      start_gf: Number(n.start_gf || 0),
      end_gf: Number(n.end_gf || 0),
      anchor_gf: Number(n.anchor_gf || 0),
      label: (n.note || kindLbl).slice(0, 80),
      note: (n.note || "").slice(0, 600),
      parent_moment_id: n.parent_moment_id || "",
      response_clip_url: n.response_clip_url || "",
      response_overlay_url: n.response_overlay_url || "",
    };
    const collections = [...getActiveFocuses(), ...getReferencePlaylists()];
    const hasLoadedPlaylists = (state.playlists || []).length > 0;
    const assignedIds = collections
      .filter(c => {
        if (hasLoadedPlaylists && Array.isArray(c.items)) return collectionHasItem(c, n.id);
        return collectionHasItem(c, n.id)
          || (n.goal_ids || []).includes(c.id)
          || (n.playlist_ids || []).includes(c.id);
      })
      .map(c => c.id);
    collectionPickerBlock = `<div class="moment-picker-row hub-moment-picker">${renderCollectionPickerHtml(`hub_${n.id}`, assignedIds, {
      buttonLabel: "+ Playlist",
      onToggle: colId => toggleHubNoteInCollection(colId, n.id),
      onCreate: () => openPlaylistModal(null, async pl => {
        if (pl && pl.id) {
          await toggleHubNoteInCollection(pl.id, n.id);
        }
      }),
    })}</div>`;
  }

  let bodyHtml = "";
  if (n.needs_note) {
    const hint = "No written note yet — click Edit to add one";
    bodyHtml = `<div class="hub-note-placeholder">${hint}</div>`;
  } else {
    bodyHtml = `<div class="hub-note-body ${kindCls}">${esc(n.note)}</div>`;
  }

  // Collapsed Desktop-Only Clip Thumbnail
  const spanS = Math.max(0.2, (n.clip_end_s ?? 0) - (n.clip_start_s ?? 0)).toFixed(1);
  let thumbHtml = "";
  if (!isPreviewOpen && n.has_video && n.video_url && !isMobileMode()) {
    const rawThumbT = isResp ? 0 : Number(n.anchor_s ?? n.clip_start_s ?? 0);
    const thumbT = Math.max(0, Number.isFinite(rawThumbT) ? rawThumbT : 0).toFixed(2);
    const thumbSrc = isResp ? safeHref(n.video_url) : `${safeHref(n.video_url)}#t=${thumbT}`;
    thumbHtml = `
      <div class="hub-thumb-wrap" data-tip="Click to expand looping clip preview" onclick="toggleNotePreview(${escJsAttr(n.id)})">
        <video muted preload="metadata" playsinline src="${esc(thumbSrc)}"></video>
        <div class="hub-thumb-overlay">
          <div class="hub-thumb-play">▶</div>
        </div>
        <span class="hub-thumb-dur mono">${spanS}s</span>
      </div>
    `;
  }

  // Inline Mini-Clip Preview (Expanded)
  let previewHtml = "";
  if (isPreviewOpen) {
    previewHtml = `
      <div class="mini-preview" id="miniPreviewBox">
        <video id="miniPreviewVideo" playsinline webkit-playsinline muted preload="auto" src="${esc(n.video_url)}"></video>
        <div class="mini-prog-track" data-tip="Click to seek within clip loop" onclick="seekMiniPreview(event)">
          <div id="miniProgFill" class="mini-prog-fill"></div>
        </div>
        <div class="mini-preview-bar">
          <div class="row" style="gap:5px;">
            <button id="btnMiniPlay" class="sm primary" style="padding:3px 9px; font-size:12px;" data-tip="Play / Pause clip preview" onclick="toggleMiniPlay()">❚❚</button>
            <button class="sm" style="padding:3px 8px; font-size:12px;" data-tip="Restart clip from beginning" onclick="replayMiniPreview()">↺</button>
            <button class="speed-btn ${state.previewRate === 0.25 ? "active" : ""}" data-mrate="0.25" data-tip="Playback speed 0.25x" onclick="setMiniRate(0.25)">0.25x</button>
            <button class="speed-btn ${state.previewRate === 0.5 ? "active" : ""}" data-mrate="0.5" data-tip="Playback speed 0.5x" onclick="setMiniRate(0.5)">0.5x</button>
            <button class="speed-btn ${state.previewRate === 1 ? "active" : ""}" data-mrate="1" data-tip="Playback speed 1.0x" onclick="setMiniRate(1)">1.0x</button>
          </div>
          <span id="miniTimeHud" style="color:var(--muted);">Loop · ${spanS}s</span>
        </div>
      </div>
    `;
  }

  // Inline Editor Drawer
  let editHtml = "";
  if (isEditing) {
    const initialText = n.is_default_select_note ? "" : (n.note || "");
    const tagPickerHtml = renderTagPickerHtml("hubEdit", null, { buttonLabel: "+ Add tags" });
    editHtml = `
      <div class="inline-edit-box">
        <textarea id="hubEditNoteText" class="input inline-note-textarea" placeholder="Write your note, counterplay, or takeaway..." onkeydown="if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();saveHubEdit(${escJsAttr(n.id)});}else if(event.key==='Escape'){event.preventDefault();cancelHubEdit();}">${esc(initialText)}</textarea>
        ${tagPickerHtml}
        <div class="row" style="justify-content:flex-end; margin-top:8px;">
          <button type="button" class="sm" onclick="cancelHubEdit()">Cancel</button>
          <button id="btnSaveHubEdit" type="button" class="emerald sm" onclick="saveHubEdit(${escJsAttr(n.id)})">Save</button>
        </div>
      </div>
    `;
  }

  const previewBtn = n.has_video
    ? `<button class="sm hub-btn-desktop-only ${isPreviewOpen ? "primary" : ""}" data-tip="${isPreviewOpen ? "Collapse inline clip preview" : "Expand inline looping clip preview"}" onclick="toggleNotePreview(${escJsAttr(n.id)})">${isPreviewOpen ? "▾ Hide Preview" : "▶ Preview"}</button>` : "";
  const openViewerBtn = (n.entry_type !== "set_note" && n.has_video && n.video_url)
    ? `<button class="sm primary" data-tip="Open moment viewer with overlays &amp; note" onclick="openMomentViewerFromHub(${escJsAttr(n.id)})">Open</button>` : "";
  const openBtn = ((n.has_video || n.has_set_video) && n.quark_id)
    ? `<button class="sm" data-tip="Jump straight to this frame in Replay Studio" onclick="openMatchFromNote(${escJsAttr(n.id)})">Open in Studio →</button>` : "";
  const canPracticeCard = Boolean(
    canTakeoverNow() && n.entry_type !== "set_note" && (isResp ? n.can_launch_drill : (n.quark_id && (n.start_gf || n.anchor_gf)))
  );
  const practiceBtn = canPracticeCard
    ? `<button class="sm amber practice-btn hub-btn-desktop-only" data-tip="Launch Replay Takeover in FBNeo to practice this moment" onclick="practiceHubNote(${escJsAttr(n.id)}, this)">▶ Practice</button>` : "";
  const canShareCard = Boolean(n.entry_type !== "set_note" && (isResp ? (n.response_clip_url || n.video_url) : (n.quark_id && n.round_id && (n.has_video || n.has_set_video))));
  const shareBtn = canShareCard
    ? `<button class="sm hub-btn-desktop-only" data-tip="Share a revocable link or download MP4" onclick="shareHubNote(${escJsAttr(n.id)})">↗ Share Moment</button>`
    : "";
  const editBtn = n.editable
    ? `<button class="sm hub-btn-desktop-only" data-tip="${n.needs_note ? "Add written note and tags" : "Edit note and tags"}" onclick="startHubEdit(${escJsAttr(n.id)})">${n.needs_note ? "+ Add Note" : "Edit"}</button>` : "";
  const delBtn = n.deletable
    ? `<button class="sm danger hub-btn-desktop-only" data-tip="${n.entry_type === "select_mark" ? "Dismiss this bookmark" : "Delete this note"}" onclick="deleteHubNote(${escJsAttr(n.id)})">${n.entry_type === "select_mark" ? "Dismiss" : "Delete"}</button>` : "";

  const overflowItems = [];
  if (n.editable) {
    overflowItems.push(
      `<button type="button" role="menuitem" class="hub-card-more-item" onclick="event.stopPropagation(); closeAllHubCardMenus(); startHubEdit(${escJsAttr(n.id)})">${n.needs_note ? "+ Add Note" : "Edit"}</button>`
    );
  }
  if (canShareCard) {
    overflowItems.push(
      `<button type="button" role="menuitem" class="hub-card-more-item" onclick="event.stopPropagation(); closeAllHubCardMenus(); shareHubNote(${escJsAttr(n.id)})">↗ Share Moment</button>`
    );
  }
  if (n.deletable) {
    overflowItems.push(
      `<button type="button" role="menuitem" class="hub-card-more-item is-danger" onclick="event.stopPropagation(); closeAllHubCardMenus(); deleteHubNote(${escJsAttr(n.id)})">${n.entry_type === "select_mark" ? "Dismiss" : "Delete"}</button>`
    );
  }
  const isMenuOpen = state.hubOpenMenuId === n.id;
  const moreMenuBlock = overflowItems.length
    ? `
      <div class="hub-card-more-wrap" data-hub-menu-id="${esc(n.id)}">
        <button type="button" class="sm hub-card-more-btn${isMenuOpen ? " is-open" : ""}" aria-label="More note actions" aria-haspopup="menu" aria-expanded="${isMenuOpen ? "true" : "false"}" onclick="event.stopPropagation(); toggleHubCardMenu(${escJsAttr(n.id)})">⋯</button>
        <div class="hub-card-more-menu${isMenuOpen ? "" : " hidden"}" role="menu">
          ${overflowItems.join("")}
        </div>
      </div>
    `
    : "";

  const tsText = formatTimestampWithTz(n.date || n.updated_at || n.created_at, n.date_str || "");
  const headToggleAttrs = n.has_video
    ? `class="hub-card-head is-toggleable" data-tip="${isPreviewOpen ? "Click to collapse clip preview" : "Click to expand clip preview"}" onclick="toggleNotePreview(${escJsAttr(n.id)})"`
    : `class="hub-card-head"`;
  const chevronHtml = n.has_video
    ? `<span class="hub-expand-chevron" aria-hidden="true">${isPreviewOpen ? "▴" : "▾"}</span>`
    : "";

  const tagsBlock = tagPills ? `<div class="row hub-tags-row" style="margin-top:6px; gap:5px;">${tagPills}</div>` : "";
  const hasAnyAction = Boolean(openViewerBtn || previewBtn || practiceBtn || shareBtn || openBtn || editBtn || delBtn || moreMenuBlock);
  const actionsBlock = hasAnyAction
    ? `
      <div class="row hub-card-actions" style="margin-top:8px; gap:6px;">
        ${openViewerBtn}
        ${previewBtn}
        ${practiceBtn}
        ${shareBtn}
        ${openBtn}
        ${editBtn}
        ${delBtn}
        ${moreMenuBlock}
      </div>
    `
    : "";

  const threadHtml = nestedResponses.length
    ? `
      <div class="response-thread">
        <div class="response-thread-head">✅ Labbed Responses (${nestedResponses.length})</div>
        ${nestedResponses.map(child => renderHubNoteCardHtml(child, [], true)).join("")}
      </div>
    `
    : "";

  const innerContent = isPreviewOpen
    ? `
      <div class="hub-card-expanded-body">
        ${tagsBlock}
        ${bodyHtml}
        ${previewHtml}
        ${editHtml}
        ${actionsBlock}
        ${collectionPickerBlock}
      </div>
    `
    : (thumbHtml
      ? `
        <div class="hub-card-split">
          <div class="hub-card-main">
            ${tagsBlock}
            ${bodyHtml}
            ${editHtml}
            ${actionsBlock}
            ${collectionPickerBlock}
          </div>
          ${thumbHtml}
        </div>
      `
      : `
        ${tagsBlock}
        ${bodyHtml}
        ${editHtml}
        ${actionsBlock}
        ${collectionPickerBlock}
      `);

  const clipDurTxt = (n.entry_type !== "set_note")
    ? (formatClipDuration(n.start_gf, n.end_gf, n.round_fps || 60) || (n.has_video ? `${spanS}s` : ""))
    : "";
  const durCls = thumbHtml ? "pill mono hub-card-dur hub-dur-mobile-only" : "pill mono hub-card-dur";
  const durPill = clipDurTxt
    ? `<span class="${durCls}" data-tip="Clip duration">${esc(clipDurTxt)}</span>`
    : "";

  return `
    <div class="hub-note-card ${cardCls}">
      <div ${headToggleAttrs}>
        ${matchupHtml}
        ${chevronHtml}
        <div class="hub-card-submeta">
          <span class="hub-card-ts" data-tip="Match date &amp; time (local timezone)">${esc(tsText)}</span>
          ${durPill}
          ${originPill}
          ${respOrphanPill}
          ${sharedPill}
          ${hpPill}
        </div>
      </div>
      ${innerContent}
      ${threadHtml}
    </div>
  `;
}

function renderHubNotes() {
  const filtered = getFilteredHubNotes();
  if (state.hubOpenMenuId && !filtered.some(x => x.id === state.hubOpenMenuId)) {
    state.hubOpenMenuId = null;
  }
  const total = state.hubNotes.length;
  const summaryRow = document.getElementById("notesSummaryRow");
  const summaryText = document.getElementById("notesSummaryText");
  const isFiltered = hasActiveNotesFilters();
  if (summaryRow) summaryRow.classList.toggle("hidden", !isFiltered);
  if (summaryText) {
    summaryText.textContent = isFiltered ? `Showing ${filtered.length} of ${total}` : "";
  }

  const host = document.getElementById("notesHubList");
  if (!filtered.length) {
    host.innerHTML = `<div class="card empty-card">No notes match these filters yet. Pin moments inside Replay Studio!</div>`;
    return;
  }

  const visibleParentIds = new Set(filtered.filter(x => x.source !== "takeover_response").map(x => x.id));
  const childrenByParent = new Map();
  const nestedIds = new Set();
  filtered.forEach(item => {
    if (item.source === "takeover_response" && item.parent_moment_id && visibleParentIds.has(item.parent_moment_id)) {
      if (!childrenByParent.has(item.parent_moment_id)) childrenByParent.set(item.parent_moment_id, []);
      childrenByParent.get(item.parent_moment_id).push(item);
      nestedIds.add(item.id);
    }
  });
  host.innerHTML = filtered
    .filter(item => !nestedIds.has(item.id))
    .map(item => renderHubNoteCardHtml(item, childrenByParent.get(item.id) || [], false))
    .join("");

  if (state.previewNoteId && filtered.some(x => x.id === state.previewNoteId && x.has_video)) {
    mountMiniPreview(state.previewNoteId);
  } else if (state.previewNoteId) {
    stopMiniPreview();
    state.previewNoteId = null;
  }
}

// ---------- INLINE MINI-CLIP PREVIEW ENGINE ----------
function stopMiniPreview() {
  if (state.previewRaf) {
    cancelAnimationFrame(state.previewRaf);
    state.previewRaf = null;
  }
  const v = document.getElementById("miniPreviewVideo");
  if (v) {
    try { v.pause(); v.removeAttribute("src"); v.load(); } catch (_) {}
  }
}

function toggleNotePreview(id) {
  closeAllHubCardMenus();
  if (state.previewNoteId === id) {
    stopMiniPreview();
    state.previewNoteId = null;
    renderHubNotes();
    return;
  }
  stopMiniPreview();
  state.previewNoteId = id;
  renderHubNotes();
}

function activePreviewNote() {
  return state.hubNotes.find(x => x.id === state.previewNoteId) || null;
}

function mountMiniPreview(id) {
  if (state.previewRaf) { cancelAnimationFrame(state.previewRaf); state.previewRaf = null; }
  const n = state.hubNotes.find(x => x.id === id);
  const v = document.getElementById("miniPreviewVideo");
  if (!n || !v) return;

  const startS = Math.max(0, Number(n.clip_start_s ?? 0));
  const endS = Math.max(startS + 0.4, Number(n.clip_end_s ?? (startS + 2.0)));
  const spanS = Math.max(0.1, endS - startS);

  const updateBar = () => {
    const fill = document.getElementById("miniProgFill");
    const hud = document.getElementById("miniTimeHud");
    const btn = document.getElementById("btnMiniPlay");
    if (!fill || !v) return;
    const rel = Math.max(0, Math.min(spanS, (v.currentTime || startS) - startS));
    fill.style.width = `${(rel / spanS) * 100}%`;
    if (hud) hud.textContent = `${rel.toFixed(1)}s / ${spanS.toFixed(1)}s · Loop`;
    if (btn) btn.textContent = v.paused ? "▶" : "❚❚";
  };

  const tick = () => {
    const curV = document.getElementById("miniPreviewVideo");
    if (!curV || state.previewNoteId !== id) { state.previewRaf = null; return; }
    if (!curV.paused && curV.currentTime >= endS - 0.02) curV.currentTime = startS;
    updateBar();
    state.previewRaf = requestAnimationFrame(tick);
  };

  v.playbackRate = state.previewRate || 1;
  const startPlayback = () => {
    v.playbackRate = state.previewRate || 1;
    v.currentTime = startS;
    v.play().catch(() => {});
    updateBar();
  };
  if (v.readyState >= 1) startPlayback();
  else v.addEventListener("loadedmetadata", startPlayback, { once: true });
  v.addEventListener("ended", () => { v.currentTime = startS; v.play().catch(() => {}); });
  v.addEventListener("play", updateBar);
  v.addEventListener("pause", updateBar);
  state.previewRaf = requestAnimationFrame(tick);
}

function toggleMiniPlay() {
  const v = document.getElementById("miniPreviewVideo");
  if (!v) return;
  if (v.paused) v.play().catch(() => {}); else v.pause();
}

function replayMiniPreview() {
  const v = document.getElementById("miniPreviewVideo");
  const n = activePreviewNote();
  if (!v || !n) return;
  v.currentTime = Math.max(0, Number(n.clip_start_s ?? 0));
  v.play().catch(() => {});
}

function setMiniRate(r) {
  state.previewRate = r;
  const v = document.getElementById("miniPreviewVideo");
  if (v) v.playbackRate = r;
  document.querySelectorAll("#miniPreviewBox .speed-btn[data-mrate]").forEach(b => b.classList.toggle("active", Number(b.dataset.mrate) === r));
}

function seekMiniPreview(e) {
  const v = document.getElementById("miniPreviewVideo");
  const n = activePreviewNote();
  if (!v || !n) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / Math.max(1, rect.width)));
  const startS = Math.max(0, Number(n.clip_start_s ?? 0));
  const endS = Math.max(startS + 0.4, Number(n.clip_end_s ?? (startS + 2.0)));
  v.currentTime = startS + frac * (endS - startS);
}

// ---------- INLINE EDIT / DELETE / DEEP-JUMP ----------
function startHubEdit(id) {
  const n = state.hubNotes.find(x => x.id === id);
  if (!n) return;
  closeAllHubCardMenus();
  state.hubEditId = id;
  initTagPicker("hubEdit", getDisplayTagsForItem(n), { buttonLabel: "+ Add tags" });
  renderHubNotes();
  const ta = document.getElementById("hubEditNoteText");
  if (ta) {
    ta.focus();
    const len = ta.value.length;
    try { ta.setSelectionRange(len, len); } catch (_) {}
  }
}
function cancelHubEdit() {
  closeAllTagPickers();
  state.hubEditId = null;
  renderHubNotes();
}

async function saveHubEdit(id) {
  const n = state.hubNotes.find(x => x.id === id);
  if (!n) return;
  const tags = getTagPickerTags("hubEdit");
  const note = (document.getElementById("hubEditNoteText").value || "").trim();
  const savedKind = resolveSavedKindForItem(n.source, note, tags);
  const btn = document.getElementById("btnSaveHubEdit");
  if (btn) btn.disabled = true;
  try {
    if (n.source === "annotation" || n.source === "takeover_response") {
      await api(`/api/annotations/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ kind: savedKind, tags, note }) });
    } else if (n.source === "select_mark") {
      await api(`/api/queue/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ kind: savedKind, tags, note }) });
    } else if (n.source === "set_note") {
      await api("/api/set-notes", { method: "POST", body: JSON.stringify({ quark_id: n.quark_id, tags, note }) });
    }
    closeAllTagPickers();
    state.hubEditId = null;
    toast("Note saved");
    await loadHubNotes(true);
  } catch (err) {
    toast("Save failed: " + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteHubNote(id) {
  const n = state.hubNotes.find(x => x.id === id);
  if (!n) return;
  const promptMsg = n.entry_type === "select_mark"
    ? "Dismiss this bookmark?"
    : (n.entry_type === "set_note"
      ? "Clear this set takeaway note?"
      : (n.source === "takeover_response" ? "Delete this labbed answer and its clip files?" : "Delete this moment note?"));
  if (!confirm(promptMsg)) return;
  try {
    if (n.source === "annotation" || n.source === "takeover_response") {
      await api(`/api/annotations/${encodeURIComponent(id)}`, { method: "DELETE" });
    } else if (n.source === "select_mark") {
      await api("/api/queue/status", { method: "POST", body: JSON.stringify({ id, status: "dismissed" }) });
    } else if (n.source === "set_note") {
      await api("/api/set-notes", { method: "POST", body: JSON.stringify({ quark_id: n.quark_id, note: "", tags: [] }) });
    }
    if (state.previewNoteId === id) { stopMiniPreview(); state.previewNoteId = null; }
    if (state.hubEditId === id) state.hubEditId = null;
    toast(n.entry_type === "select_mark" ? "Dismissed" : "Deleted");
    await loadHubNotes(true);
  } catch (err) {
    toast(err.message);
  }
}

async function practiceHubNote(id, btnEl = null) {
  const n = state.hubNotes.find(x => x.id === id);
  if (!n) return;
  const fullNote = (!n.needs_note && !n.is_default_select_note && n.note) ? n.note : "";
  if (n.source === "takeover_response") {
    const opts = {
      mode: "takeover",
      note: fullNote,
      round_id: Number(n.round_id || 0),
    };
    if (n.parent_moment_id) opts.parent_moment_id = n.parent_moment_id;
    await launchDrill(n.id, opts, btnEl);
    return;
  }
  if (n.entry_type === "select_mark" || n.can_launch_drill) {
    await launchDrill(n.id, {
      note: fullNote,
      parent_moment_id: n.id,
      round_id: Number(n.round_id || 0),
    }, btnEl);
    return;
  }
  const kindLbl = KIND_LABELS[n.kind] || n.kind || "Moment";
  await launchTakeoverPayload({
    quark_id: n.quark_id,
    frame: n.anchor_gf || n.start_gf || 0,
    start_gf: n.start_gf || 0,
    end_gf: n.end_gf || 0,
    player: n.perspective || 1,
    bake_if_missing: true,
    label: (fullNote || kindLbl).slice(0, 80),
    note: fullNote,
    parent_moment_id: n.id,
    round_id: Number(n.round_id || 0),
  }, btnEl);
}

function openMomentViewerFromHub(id) {
  const n = state.hubNotes.find(x => x.id === id);
  if (!n || !n.has_video || !n.video_url) {
    toast("No rendered video available for this moment yet.");
    return;
  }
  const isResp = Boolean(n.is_response_clip || n.source === "takeover_response" || n.kind === "response");
  const isSel = n.source === "select_mark" || n.entry_type === "select_mark";
  const frameCnt = Number(n.frame_count || 0);
  const clipStartS = isResp ? 0 : Math.max(0, Number(n.clip_start_s ?? 0));
  const clipEndS = isResp
    ? (frameCnt > 1 ? Math.max(0.25, (frameCnt - 1) / 60) : Math.max(0.25, Number(n.clip_end_s ?? 2.0)))
    : Math.max(clipStartS + 0.5, Number(n.clip_end_s ?? (clipStartS + 2.5)));
  const src = isResp ? "takeover_response" : (isSel ? "select_mark" : (n.source || "annotation"));
  const clipItem = {
    item_id: n.id,
    source: src,
    edit_source: n.editable ? src : null,
    editable: Boolean(n.editable),
    is_response_clip: isResp,
    is_standalone_clip: false,
    has_video: true,
    video_url: n.video_url,
    overlay_url: n.response_overlay_url || n.overlay_url || "",
    clip_start_s: clipStartS,
    clip_end_s: clipEndS,
    round_start_gf: Number(n.round_start_gf ?? n.start_gf ?? 0),
    round_fps: Number(n.round_fps || 60),
    quark_id: n.quark_id || "",
    round_id: Number(n.round_id || 0),
    perspective: Number(n.perspective || 1),
    start_gf: Number(n.start_gf || 0),
    end_gf: Number(n.end_gf || 0),
    anchor_gf: Number(n.anchor_gf || n.start_gf || 0),
    parent_moment_id: n.parent_moment_id || "",
    kind: n.kind || (isResp ? "response" : (isSel ? "select_mark" : "note")),
    note: n.note || "",
    is_default_select_note: Boolean(n.is_default_select_note),
    needs_note: Boolean(n.needs_note),
    tags: n.tags || [],
    hp_delta_self: n.hp_delta_self,
    hp_delta_opp: n.hp_delta_opp,
    self_char_name: n.self_char_name || "",
    opp_char_name: n.opp_char_name || "",
    self_player_name: n.self_player_name || "",
    opp_player_name: n.opp_player_name || "",
    self_rank_letter: n.self_rank_letter || "",
    opp_rank_letter: n.opp_rank_letter || "",
    round_label: n.round_label || (n.round_id ? `R${n.round_id}` : (isResp ? "Labbed Answer" : "Moment")),
    selection_id: (isSel || n.can_launch_drill) ? n.id : null,
    savestate_ready: Boolean(isSel || n.can_launch_drill),
    cloud_moment_id: n.cloud_moment_id || null,
    cloud_share_token: n.cloud_share_token || null,
    cloud_share_url: n.cloud_share_url || null,
    cloud_visibility: n.cloud_visibility || null,
    shared_caption: n.shared_caption || "",
    include_note_on_share: Boolean(n.include_note_on_share),
    hide_player_names: Boolean(n.hide_player_names),
  };
  if (typeof openSingleClipWatchModal === "function") {
    openSingleClipWatchModal(clipItem, isResp ? "Labbed Answer" : "Moment Viewer");
  }
}

function shareHubNote(id) {
  const n = state.hubNotes.find(x => x.id === id);
  if (!n) return;
  const isResp = Boolean(n.is_response_clip || n.source === "takeover_response" || n.kind === "response");
  shareMomentBundle({
    ...n,
    is_response_clip: isResp,
    note: n.is_default_select_note ? "" : (n.note || ""),
    response_clip_url: isResp ? (n.response_clip_url || n.video_url || "") : (n.response_clip_url || ""),
    response_overlay_url: isResp ? (n.response_overlay_url || n.overlay_url || "") : (n.response_overlay_url || ""),
  });
}

function openMatchFromNote(id) {
  const n = state.hubNotes.find(x => x.id === id);
  if (!n) return;
  state.returnView = "notes";
  openMatch(n.quark_id, n.perspective, {
    round_id: n.round_id,
    start_gf: n.start_gf,
    end_gf: n.end_gf,
    anchor_gf: n.anchor_gf,
    autoPlay: false,
  });
}

if (window.matchMedia) {
  const _notesBpMq = window.matchMedia("(max-width: 899px)");
  const _onNotesBpChange = () => {
    if (state.activeView === "notes" && state.notesSubView === "notebook" && state.hubLoaded) {
      renderHubNotes();
    }
  };
  if (typeof _notesBpMq.addEventListener === "function") _notesBpMq.addEventListener("change", _onNotesBpChange);
}
