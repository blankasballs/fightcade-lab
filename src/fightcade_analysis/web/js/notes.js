/* Centralized Notes Hub (#notes -> Notebook sub-view) + Inline Mini-Clip Preview */

function setNotesSubView(sub) {
  state.notesSubView = sub;
  document.getElementById("subTabNotebook").classList.toggle("active", sub === "notebook");
  document.getElementById("subTabPlaylists").classList.toggle("active", sub === "playlists");
  document.getElementById("notesSubNotebook").classList.toggle("hidden", sub !== "notebook");
  document.getElementById("notesSubPlaylists").classList.toggle("hidden", sub !== "playlists");
  document.getElementById("btnNewPlaylistFromSub").classList.toggle("hidden", sub !== "playlists");
  if (sub === "playlists") {
    stopMiniPreview();
    if (window.location.hash !== "#playlists") {
      history.replaceState(null, "", window.location.pathname + window.location.search + "#playlists");
    }
    loadPlaylists(true);
  } else {
    if (window.location.hash !== "#notes") {
      history.replaceState(null, "", window.location.pathname + window.location.search + "#notes");
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
    updateNotesNavBadge();
    if (state.activeView === "notes" && state.notesSubView === "notebook") {
      renderHubNotesControls();
      renderHubNotes();
    }
  } catch (err) {
    if (state.activeView === "notes") toast("Failed to load notes: " + err.message);
  }
}

function updateNotesNavBadge() {
  const b = document.getElementById("navNotesBadge");
  if (!b) return;
  const total = Number((state.hubCounts && state.hubCounts.total) || state.hubNotes.length || 0);
  if (total > 0) {
    b.textContent = String(total);
    b.classList.remove("hidden");
  } else {
    b.classList.add("hidden");
  }
}

function setNotesFilter(f) {
  state.notesFilter = f;
  document.querySelectorAll("#notesFilterChips .chip").forEach(c => c.classList.toggle("active", c.dataset.nfilter === f));
  renderHubNotes();
}

function toggleNotesQuickFlag(which) {
  if (which === "drill") state.notesDrillOnly = !state.notesDrillOnly;
  if (which === "needs_note") state.notesNeedsNote = !state.notesNeedsNote;
  renderHubNotesControls();
  renderHubNotes();
}

function toggleNotesTagFilter(tag) {
  state.notesTag = (state.notesTag === tag) ? "" : tag;
  renderHubNotesControls();
  renderHubNotes();
}

function toggleNotesCharFilter(side, charName) {
  if (!charName) return;
  if (side === "self") state.notesSelfChar = (state.notesSelfChar === charName) ? "" : charName;
  else state.notesOppChar = (state.notesOppChar === charName) ? "" : charName;
  renderHubNotesControls();
  renderHubNotes();
}

function onNotesFilterChanged() {
  state.notesSearch = (document.getElementById("notesSearchInput").value || "").trim();
  state.notesGroupBy = document.getElementById("notesGroupBy").value || "recent";
  state.notesSelfChar = document.getElementById("notesSelfChar").value || "";
  state.notesOppChar = document.getElementById("notesOppChar").value || "";
  const gf = document.getElementById("notesGoalFilter");
  state.notesGoalFilter = gf ? (gf.value || "") : "";
  renderHubNotesControls();
  renderHubNotes();
}

function clearNotesFilters() {
  state.notesFilter = "all";
  state.notesSelfChar = "";
  state.notesOppChar = "";
  state.notesGoalFilter = "";
  state.notesTag = "";
  state.notesDrillOnly = false;
  state.notesNeedsNote = false;
  state.notesSearch = "";
  document.getElementById("notesSearchInput").value = "";
  document.querySelectorAll("#notesFilterChips .chip").forEach(c => c.classList.toggle("active", c.dataset.nfilter === "all"));
  renderHubNotesControls();
  renderHubNotes();
}

function renderHubNotesControls() {
  const c = state.hubCounts || {};
  const bk = c.by_kind || {};
  const setCnt = (id, n) => { const el = document.getElementById(id); if (el) el.textContent = n != null ? `(${n})` : ""; };
  setCnt("nCntAll", c.total ?? state.hubNotes.length);
  setCnt("nCntMoment", c.moments ?? 0);
  setCnt("nCntSelect", c.select_marks ?? 0);
  setCnt("nCntSet", c.set_notes ?? 0);
  setCnt("nCntMistake", bk.mistake ?? 0);
  setCnt("nCntQuestion", bk.question ?? 0);
  setCnt("nCntOppHabit", bk.opp_habit ?? 0);
  setCnt("nCntGoodPlay", bk.good_play ?? 0);

  const selfList = (state.hubAvailableChars && state.hubAvailableChars.self) || [];
  const oppList = (state.hubAvailableChars && state.hubAvailableChars.opp) || [];
  document.getElementById("notesSelfChar").innerHTML = `<option value="">You: Any</option>` + selfList.map(ch =>
    `<option value="${esc(ch)}" ${ch === state.notesSelfChar ? "selected" : ""}>You: ${esc(ch)}</option>`
  ).join("");
  document.getElementById("notesOppChar").innerHTML = `<option value="">Opp: Any</option>` + oppList.map(ch =>
    `<option value="${esc(ch)}" ${ch === state.notesOppChar ? "selected" : ""}>vs ${esc(ch)}</option>`
  ).join("");

  const goalSel = document.getElementById("notesGoalFilter");
  if (goalSel) {
    const goals = state.focusGoals || [];
    if (goals.length) {
      goalSel.classList.remove("hidden");
      goalSel.innerHTML = `<option value="">Goal: Any</option>` + goals.map(g =>
        `<option value="${esc(g.id)}" ${g.id === state.notesGoalFilter ? "selected" : ""}>Goal: ${esc(g.title.slice(0, 32))}</option>`
      ).join("");
    } else {
      goalSel.classList.add("hidden");
    }
  }

  document.getElementById("btnNotesDrillOnly").classList.toggle("amber", state.notesDrillOnly);
  document.getElementById("btnNotesNeedsNote").classList.toggle("primary", state.notesNeedsNote);

  const hasAny = Boolean(
    state.notesFilter !== "all" || state.notesSelfChar || state.notesOppChar ||
    state.notesGoalFilter || state.notesTag || state.notesDrillOnly || state.notesNeedsNote || state.notesSearch
  );
  document.getElementById("btnClearNotesFilters").classList.toggle("hidden", !hasAny);

  const tagCloud = document.getElementById("notesTagCloud");
  const tags = c.by_tag || [];
  tagCloud.innerHTML = tags.length
    ? tags.map(t => `<button type="button" class="tag-filter-btn ${state.notesTag === t.tag ? "on" : ""}" onclick="toggleNotesTagFilter(${escJsAttr(t.tag)})">${esc(t.tag)} (${t.count})</button>`).join("")
    : "";
}

function getFilteredHubNotes() {
  const q = state.notesSearch.toLowerCase();
  const goalObj = state.notesGoalFilter ? (state.focusGoals || []).find(g => g.id === state.notesGoalFilter) : null;
  return state.hubNotes.filter(n => {
    if (state.notesFilter === "moment" && n.entry_type !== "moment") return false;
    if (state.notesFilter === "select_mark" && n.entry_type !== "select_mark") return false;
    if (state.notesFilter === "set_note" && n.entry_type !== "set_note") return false;
    if (["mistake", "question", "opp_habit", "good_play"].includes(state.notesFilter) && n.kind !== state.notesFilter) return false;
    if (state.notesSelfChar && n.self_char_name !== state.notesSelfChar) return false;
    if (state.notesOppChar && n.opp_char_name !== state.notesOppChar) return false;
    if (state.notesGoalFilter) {
      const hasDirect = (n.goal_ids || []).includes(state.notesGoalFilter);
      const hasTagMatch = goalObj && (goalObj.tags || []).some(t => (n.tags || []).includes(t));
      if (!hasDirect && !hasTagMatch) return false;
    }
    if (state.notesTag && !(n.tags || []).includes(state.notesTag)) return false;
    if (state.notesDrillOnly && !(n.can_launch_drill || n.queued_drill)) return false;
    if (state.notesNeedsNote && !n.needs_note) return false;
    if (q) {
      const hay = [
        n.note, n.opp_player_name, n.self_player_name, n.self_char_name,
        n.opp_char_name, n.kind, n.round_label, n.date_str, n.quark_id,
        ...(n.tags || []),
      ].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
}

function groupHubNotes(items) {
  const mode = state.notesGroupBy || "recent";
  if (mode === "recent") return [{ title: null, items }];
  const map = new Map();
  const pushTo = (key, title, item) => {
    if (!map.has(key)) map.set(key, { title, items: [] });
    map.get(key).items.push(item);
  };
  if (mode === "matchup") {
    items.forEach(n => {
      const mu = (n.self_char_name || n.opp_char_name)
        ? `${n.self_char_name || "?"} vs ${n.opp_char_name || "?"}` : "Unknown Matchup";
      pushTo(mu, mu, n);
    });
    return [...map.values()].sort((a, b) => b.items.length - a.items.length || a.title.localeCompare(b.title));
  }
  if (mode === "tag") {
    items.forEach(n => {
      const tags = (n.tags && n.tags.length) ? n.tags : ["Untagged"];
      tags.forEach(t => pushTo(t, t, n));
    });
    return [...map.values()].sort((a, b) => {
      if (a.title === "Untagged") return 1;
      if (b.title === "Untagged") return -1;
      return b.items.length - a.items.length || a.title.localeCompare(b.title);
    });
  }
  if (mode === "set") {
    items.forEach(n => {
      const opp = n.opp_player_name || "Opponent";
      const mu = (n.self_char_name && n.opp_char_name) ? `${n.self_char_name} vs ${n.opp_char_name}` : "";
      const title = `${n.date_str || n.quark_id} · vs ${opp}${mu ? " (" + mu + ")" : ""}`;
      pushTo(n.quark_id, title, n);
    });
    const groups = [...map.values()];
    groups.forEach(g => {
      g.items.sort((a, b) => {
        if (a.entry_type === "set_note" && b.entry_type !== "set_note") return -1;
        if (b.entry_type === "set_note" && a.entry_type !== "set_note") return 1;
        return (a.round_id || 0) - (b.round_id || 0) || (a.anchor_gf || 0) - (b.anchor_gf || 0);
      });
    });
    return groups;
  }
  return [{ title: null, items }];
}

function renderHubNoteCardHtml(n) {
  const cardCls = n.entry_type === "set_note"
    ? "is-set-note"
    : (n.entry_type === "select_mark" ? "is-select-mark" : "");
  const kindLbl = KIND_LABELS[n.kind] || n.kind || "Note";
  const kindCls = `kind-${n.kind || "note"}`;
  const rk = n.opp_rank_letter ? `<span class="rank">${esc(n.opp_rank_letter)}</span>` : "";
  const muHtml = (n.self_char_name || n.opp_char_name)
    ? `<span class="pill char-self ${state.notesSelfChar === n.self_char_name ? "active-f" : ""}" onclick="toggleNotesCharFilter('self',${escJsAttr(n.self_char_name)})">${esc(n.self_char_name || "?")}</span><span class="char-vs">vs</span><span class="pill char-opp ${state.notesOppChar === n.opp_char_name ? "active-f" : ""}" onclick="toggleNotesCharFilter('opp',${escJsAttr(n.opp_char_name)})">${esc(n.opp_char_name || "?")}</span>`
    : "";
  const gfPart = (n.anchor_gf != null && n.anchor_gf > 0) ? ` · gf${n.anchor_gf}` : "";
  const roundPill = `<span class="pill cached">${esc(n.round_label || "Note")}${gfPart}</span>`;
  const srcPill = (n.entry_type === "select_mark" && n.kind !== "select_mark")
    ? `<span class="pill kind-select_mark">Select Mark</span>` : "";
  const drillPill = n.can_launch_drill
    ? `<span class="pill ready">⚡ Drill Ready</span>`
    : (n.queued_drill && n.drill_status === "baking" ? `<span class="pill busy">Baking .fs...</span>` : "");

  const goalById = {};
  (state.focusGoals || []).forEach(g => { goalById[g.id] = g.title; });
  const goalPills = (n.goal_ids || []).map(gid =>
    goalById[gid] ? `<span class="pill ready">● ${esc(goalById[gid].slice(0, 26))}</span>` : ""
  ).join("");
  const tagPills = (n.tags || []).map(t =>
    `<span class="pill note" style="cursor:pointer;" onclick="toggleNotesTagFilter(${escJsAttr(t)})">${esc(t)}</span>`
  ).join("");

  let bodyHtml = "";
  if (n.needs_note) {
    const hint = n.entry_type === "select_mark"
      ? `${esc(n.note || "In-game Select mark")} — click + Add Note to annotate this moment`
      : "No written note yet — click Edit to add one";
    bodyHtml = `<div class="hub-note-placeholder">${hint}</div>`;
  } else {
    bodyHtml = `<div class="hub-note-body">${esc(n.note)}</div>`;
  }

  // Inline Mini-Clip Preview
  const isPreviewOpen = state.previewNoteId === n.id && n.has_video;
  let previewHtml = "";
  if (isPreviewOpen) {
    const spanS = Math.max(0.2, (n.clip_end_s ?? 0) - (n.clip_start_s ?? 0)).toFixed(1);
    previewHtml = `
      <div class="mini-preview" id="miniPreviewBox">
        <video id="miniPreviewVideo" playsinline webkit-playsinline muted preload="auto" src="${esc(n.video_url)}"></video>
        <div class="mini-prog-track" onclick="seekMiniPreview(event)">
          <div id="miniProgFill" class="mini-prog-fill"></div>
        </div>
        <div class="mini-preview-bar">
          <div class="row" style="gap:5px;">
            <button id="btnMiniPlay" class="sm primary" style="padding:3px 9px; font-size:12px;" onclick="toggleMiniPlay()">❚❚</button>
            <button class="sm" style="padding:3px 8px; font-size:12px;" onclick="replayMiniPreview()">↺</button>
            <button class="speed-btn ${state.previewRate === 0.5 ? "active" : ""}" onclick="setMiniRate(0.5)">0.5x</button>
            <button class="speed-btn ${state.previewRate === 1 ? "active" : ""}" onclick="setMiniRate(1)">1x</button>
          </div>
          <span id="miniTimeHud" style="color:var(--muted);">Loop · ${spanS}s</span>
        </div>
      </div>
    `;
  }

  // Inline Editor Drawer
  const isEditing = state.hubEditId === n.id;
  let editHtml = "";
  if (isEditing) {
    const isSet = n.entry_type === "set_note";
    const extraTags = (n.tags || []).filter(t => !PRESET_TAGS.includes(t));
    const kindChoices = isSet ? [] : (n.entry_type === "select_mark"
      ? [{ id: "select_mark", label: "Select Mark" }, ...KINDS]
      : KINDS);
    const kindRow = kindChoices.length
      ? `<div class="kind-grid">${kindChoices.map(k => `<button type="button" class="kind-btn ${state.hubEditKind === k.id ? "on" : ""}" onclick="pickHubEditKind('${k.id}')">${k.label}</button>`).join("")}</div>`
      : "";
    const tagRow = `<div class="tag-grid">${PRESET_TAGS.map(t => `<button type="button" class="tag-btn ${state.hubEditTags.has(t) ? "on" : ""}" onclick="toggleHubEditTag('${esc(t)}')">${esc(t)}</button>`).join("")}</div>`;
    const initialText = n.is_default_select_note ? "" : (n.note || "");
    editHtml = `
      <div class="inline-edit-box">
        ${kindRow}
        ${tagRow}
        <input id="hubEditCustomTags" class="input compact-input" value="${esc(extraTags.join(", "))}" placeholder="Optional custom tags (comma-separated)" />
        <textarea id="hubEditNoteText" class="input" placeholder="Write your note, counterplay, or takeaway...">${esc(initialText)}</textarea>
        <div class="row" style="justify-content:flex-end; margin-top:8px;">
          <button class="sm" onclick="cancelHubEdit()">Cancel</button>
          <button id="btnSaveHubEdit" class="emerald sm" onclick="saveHubEdit('${esc(n.id)}')">Save</button>
        </div>
      </div>
    `;
  }

  const previewBtn = n.has_video
    ? `<button class="sm ${isPreviewOpen ? "primary" : ""}" onclick="toggleNotePreview('${esc(n.id)}')">${isPreviewOpen ? "▾ Hide Preview" : "▶ Preview"}</button>` : "";
  const openBtn = (n.has_video || n.has_set_video)
    ? `<button class="sm" onclick="openMatchFromNote('${esc(n.id)}')">Open Replay →</button>` : "";
  const launchBtn = (n.can_launch_drill && canTakeoverNow())
    ? `<button class="sm emerald" onclick="launchDrill('${esc(n.id)}')">▶ Launch Drill</button>` : "";
  const addPlBtn = n.entry_type !== "set_note"
    ? `<button class="sm" onclick="openAddToPlaylistModal({item_id:${escJsAttr(n.id)},source:${escJsAttr(n.source || "moment")},quark_id:${escJsAttr(n.quark_id)},round_id:${n.round_id || 0},perspective:${n.perspective || 1},start_gf:${n.start_gf || 0},end_gf:${n.end_gf || 0},anchor_gf:${n.anchor_gf || 0},label:${escJsAttr((n.note || kindLbl).slice(0, 80))},note:${escJsAttr((n.note || "").slice(0, 200))}})">+ Playlist</button>`
    : "";
  const editBtn = n.editable
    ? `<button class="sm" onclick="startHubEdit('${esc(n.id)}')">${n.needs_note ? "+ Add Note" : "Edit"}</button>` : "";
  const delBtn = n.deletable
    ? `<button class="sm danger" onclick="deleteHubNote('${esc(n.id)}')">${n.entry_type === "select_mark" ? "Dismiss" : "Delete"}</button>` : "";
  const oppName = n.opp_player_name && n.opp_player_name !== "P1" && n.opp_player_name !== "P2"
    ? `vs ${esc(n.opp_player_name)}` : "";

  return `
    <div class="hub-note-card ${cardCls}">
      <div class="row-between">
        <div class="row" style="gap:6px;">
          ${muHtml}
          ${oppName ? `<span style="font-weight:700; font-size:13.5px;">${oppName}</span>` : ""}
          ${rk}
          ${roundPill}
          <span class="pill ${kindCls}">${esc(kindLbl)}</span>
          ${srcPill}
          ${drillPill}
          ${goalPills}
        </div>
        <span style="font-size:12px; color:var(--muted);">${esc(n.date_str || "")}</span>
      </div>
      ${tagPills ? `<div class="row" style="margin-top:7px; gap:5px;">${tagPills}</div>` : ""}
      ${bodyHtml}
      ${previewHtml}
      ${editHtml}
      <div class="row" style="margin-top:8px; gap:6px;">
        ${previewBtn}
        ${launchBtn}
        ${addPlBtn}
        ${openBtn}
        ${editBtn}
        ${delBtn}
      </div>
    </div>
  `;
}

function renderHubNotes() {
  const filtered = getFilteredHubNotes();
  const total = state.hubNotes.length;
  document.getElementById("notesSummaryText").textContent =
    filtered.length === total
      ? `Showing all ${total} item${total === 1 ? "" : "s"} across your replays`
      : `Showing ${filtered.length} of ${total} item${total === 1 ? "" : "s"}`;

  const host = document.getElementById("notesHubList");
  if (!filtered.length) {
    host.innerHTML = `<div class="card empty-card">No notes match these filters yet. Pin notes inside Replay Studio or tap Select during a match!</div>`;
    return;
  }

  const groups = groupHubNotes(filtered);
  host.innerHTML = groups.map(g => {
    const hdr = g.title
      ? `<div class="notes-group-head"><span>${esc(g.title)}</span><span class="pill">${g.items.length}</span></div>`
      : "";
    return hdr + g.items.map(renderHubNoteCardHtml).join("");
  }).join("");

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
  document.querySelectorAll("#miniPreviewBox .speed-btn").forEach(b => b.classList.toggle("active", b.textContent === `${r}x`));
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
  state.hubEditId = id;
  state.hubEditKind = n.kind || "mistake";
  state.hubEditTags = new Set((n.tags || []).filter(t => PRESET_TAGS.includes(t)));
  renderHubNotes();
  const ta = document.getElementById("hubEditNoteText");
  if (ta) ta.focus();
}
function cancelHubEdit() { state.hubEditId = null; renderHubNotes(); }
function pickHubEditKind(k) { state.hubEditKind = k; renderHubNotes(); }
function toggleHubEditTag(t) {
  if (state.hubEditTags.has(t)) state.hubEditTags.delete(t); else state.hubEditTags.add(t);
  renderHubNotes();
}

async function saveHubEdit(id) {
  const n = state.hubNotes.find(x => x.id === id);
  if (!n) return;
  const custom = (document.getElementById("hubEditCustomTags").value || "").split(",").map(s => s.trim()).filter(Boolean);
  const tags = [...state.hubEditTags, ...custom];
  const note = (document.getElementById("hubEditNoteText").value || "").trim();
  const btn = document.getElementById("btnSaveHubEdit");
  if (btn) btn.disabled = true;
  try {
    if (n.source === "annotation") {
      await api(`/api/annotations/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ kind: state.hubEditKind, tags, note }) });
    } else if (n.source === "select_mark") {
      await api(`/api/queue/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ kind: state.hubEditKind, tags, note }) });
    } else if (n.source === "set_note") {
      await api("/api/set-notes", { method: "POST", body: JSON.stringify({ quark_id: n.quark_id, tags, note }) });
    }
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
    ? "Dismiss this in-game Select mark?"
    : (n.entry_type === "set_note" ? "Clear this set takeaway note?" : "Delete this moment note?");
  if (!confirm(promptMsg)) return;
  try {
    if (n.source === "annotation") {
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
