/* Shared Application State, Config Adapter (Local vs Hosted), API Helpers, Auth, Capabilities, Theme & Takeover Launcher */
const BOOT = window.__FC_BOOTSTRAP__ || {};
const APP_MODE = BOOT.mode || "local_companion";
const API_BASE = String(BOOT.apiBase || "").replace(/\/+$/, "");
const COMPANION_BASE = String(
  BOOT.companionBase !== undefined
    ? BOOT.companionBase
    : (APP_MODE === "hosted_web" ? (localStorage.getItem("fc_companion_base") || "http://127.0.0.1:8765") : ""),
).replace(/\/+$/, "");
const GITHUB_REPO_URL = BOOT.githubRepoUrl || "https://github.com/blankasballs/fightcade-lab";
const DMCA_CONTACT_EMAIL = BOOT.dmcaContactEmail || "dmca@fightcadelab.com";

const PRESET_TAGS = Array.isArray(BOOT.presetTags) ? BOOT.presetTags : [
  "Mistake", "Question", "Good Play", "Opp Habit",
  "Anti-Air", "Whiff Punish", "Throw / Tech", "Wakeup / Oki",
  "Corner", "Parry", "Meter / Super", "Footsies", "Combo / Confirm",
];
const DEFAULT_USER = BOOT.defaultUsername || "";

const LEGACY_KIND_TO_TAG = {
  mistake: "Mistake",
  question: "Question",
  good_play: "Good Play",
  opp_habit: "Opp Habit",
};

const KINDS = [
  { id: "mistake", label: "Mistake" },
  { id: "question", label: "Question" },
  { id: "good_play", label: "Good Play" },
  { id: "opp_habit", label: "Opp Habit" },
  { id: "note", label: "Note" },
];

const KIND_LABELS = {
  mistake: "Mistake",
  question: "Question",
  good_play: "Good Play",
  opp_habit: "Opp Habit",
  note: "Note",
  response: "Labbed Answer",
  select_mark: "Bookmark",
  set_takeaway: "Set Takeaway",
  pro_clip: "Top-Rank Clip",
};

const state = {
  apiBase: API_BASE,
  companionBase: COMPANION_BASE,
  username: localStorage.getItem("fc_mobile_user") || DEFAULT_USER,
  auth: {
    authenticated: false,
    user: null,
    quota: null,
    has_byoc: false,
  },
  capabilities: {
    mode: APP_MODE,
    owner: DEFAULT_USER,
    can_takeover: APP_MODE === "local_companion",
    interactive_desktop: APP_MODE === "local_companion",
    can_render: APP_MODE === "local_companion",
    can_generate_overlays: APP_MODE === "local_companion",
    has_grouflon: APP_MODE === "local_companion",
    grouflon_dir: null,
    watcher_active: false,
    has_gemini: false,
    has_pro_corpus: false,
    pro_corpus_sets: 0,
    pro_corpus_enabled: false,
    companion_paired: false,
    has_cloud_token: false,
    cloud_base_url: "https://fightcadelab.com",
    byoc: {
      connected: false,
      bucket_name: null,
      account_id_masked: null,
      access_key_id_masked: null,
      has_separate_read_token: false,
      auto_sync: true,
      cap_gb: 9.0,
      used_bytes: 0,
      synced_count: 0,
      synced_quark_ids: [],
      syncing: false,
      last_error: null,
    },
    disk_cache: { total_bytes: 0, total_gb: 0, match_count: 0, protected_count: 0, cap_gb: 5.0 },
    environment: null,
  },
  watcher: { phase: "idle", message: "Watching Fightcade", running: false },
  jobs: [],
  companionConnected: false,

  // Navigation & Browser History
  activeView: "home", // home | matches | notes | insights | studio | report | shared_moment | shared_playlist
  notesSubView: "notebook", // notebook | playlists
  returnView: "home",
  _historyDepth: 0,
  _routingFromPopstate: false,
  _activeModalHistory: null, // null | "watch" | "mobile_menu"
  _suppressNextPopstate: false,
  _lastRoutedUrl: "",

  // Home Workbench
  homeData: null,

  // Focus Goals & Playlists
  focusGoals: [],
  graduatedGoals: [],
  playlists: [],
  editingGoalId: null,
  editingPlaylistId: null,
  pendingGoalSeedMoments: null,

  // Matches Catalog
  filter: "all",
  selfCharFilter: "",
  oppCharFilter: "",
  availableChars: { self: [], opp: [] },
  matches: [],
  counts: {},
  lastRefreshed: null,

  // Replay Studio
  activeManifest: null,
  activeMatchId: 1,
  activeRoundIdx: 0,
  annotations: [],
  selectMarks: [],
  setNoteObj: null,
  studioSidebarTab: "moments", // moments | set_notes | pro_search
  noteScope: "round", // round | match | set
  currentFrameOffset: 0,
  studioRate: 1,
  composerOpen: false,
  rangeStartGf: null,
  rangeEndGf: null,
  loopPreviewActive: false,
  studioEditId: null,
  studioEditSource: "annotation", // annotation | select_mark | takeover_response
  studioEditKind: "note",
  studioEditTags: new Set(),
  studioEditCustomTags: "",
  studioEditText: "",
  studioEditShowMeta: false,
  studioEditHasRange: false,
  selectedKind: "note",
  selectedTags: new Set(),
  selectedCollectionIds: new Set(),
  composerInitialCollectionIds: new Set(),
  playStopGf: null,
  proSearchResults: null,
  proSearchLoading: false,
  pollTimer: null,

  // Overlays (shared by Studio + Watch Mode + Shared Moment/Playlist Views)
  overlayOpts: (() => {
    try {
      return {
        hitboxes: true,
        inputs: true,
        sticks: true,
        gauges: true,
        ...JSON.parse(localStorage.getItem("fc_overlay_opts") || "{}"),
      };
    } catch (_) {
      return { hitboxes: true, inputs: true, sticks: true, gauges: true };
    }
  })(),
  timelineFilters: (() => {
    const defaults = {
      self_hp: true,
      opp_hp: true,
      dmg_self: true,
      dmg_opp: true,
      super: true,
      notes: true,
    };
    try {
      return { ...defaults, ...JSON.parse(localStorage.getItem("fc_timeline_filters") || "{}") };
    } catch (_) {
      return defaults;
    }
  })(),
  overlayCache: {},
  activeOverlay: null,

  // Notes Hub
  hubNotes: [],
  hubCounts: {},
  hubAvailableChars: { self: [], opp: [] },
  hubLoaded: false,
  notesFilter: "all",
  notesSelfChar: "",
  notesOppChar: "",
  notesTags: [],
  notesTagFilterOpen: false,
  notesSearch: "",
  previewNoteId: null,
  previewRate: 1,
  previewRaf: null,
  hubEditId: null,
  hubEditKind: "note",
  hubEditTags: new Set(),
  hubOpenMenuId: null,

  // Insights
  insightsData: null,
  includeLegacyInsights: false,
  insightsFilter: "all",
  insightsSearch: "",

  // Watch Mode / Moment Viewer
  watchPlaylistId: null,
  watchAdhocItems: null,
  watchAdhocTitle: "",
  watchIndex: 0,
  watchRate: 1,
  watchRaf: null,
  watchOverlay: null,
  watchEditingNote: false,
  watchEditDraft: "",

  // Shared Moment / Shared Playlist Views (/m/:id, /p/:id)
  sharedMoment: null,
  sharedReplies: [],
  sharedPlaylist: null,
  sharedPlaylistIndex: 0,
  sharedPerspective: 1,
  sharedRate: 1,
  sharedOverlay: null,
  sharedRaf: null,

  // Report, Share Moment & BYOC Modals
  reportTarget: null,
  addToPlPayload: null,
  shareModal: null,
  byocModal: null,
  hostedByocConnected: false,
  hostedByocBucket: "",
  hostedByocUsedBytes: 0,
  hostedByocCapGb: 9.0,
};

function apiUrl(path) {
  if (!path) return API_BASE || "";
  if (/^https?:\/\//i.test(path)) return path;
  return `${API_BASE}${path.startsWith("/") ? path : "/" + path}`;
}

function companionUrl(path) {
  if (!path) return COMPANION_BASE || "";
  if (/^https?:\/\//i.test(path)) return path;
  return `${COMPANION_BASE}${path.startsWith("/") ? path : "/" + path}`;
}

function isHostedMode() {
  return (state.capabilities && state.capabilities.mode === "hosted_web") || APP_MODE === "hosted_web";
}

function getAllCollections() {
  const byId = new Map();
  (state.playlists || []).forEach(p => { if (p && p.id) byId.set(p.id, p); });
  (state.focusGoals || []).forEach(g => {
    if (g && g.id && !byId.has(g.id)) {
      byId.set(g.id, { ...g, name: g.name || g.title, is_focus: true, focus_status: g.focus_status || g.status || "active" });
    }
  });
  (state.graduatedGoals || []).forEach(g => {
    if (g && g.id && !byId.has(g.id)) {
      byId.set(g.id, { ...g, name: g.name || g.title, is_focus: true, focus_status: "graduated" });
    }
  });
  if (state.homeData) {
    ((state.homeData.focus_goals && state.homeData.focus_goals.active) || []).forEach(g => {
      if (g && g.id && !byId.has(g.id)) {
        byId.set(g.id, { ...g, name: g.name || g.title, is_focus: true, focus_status: "active" });
      }
    });
    (state.homeData.playlists || []).forEach(p => {
      if (p && p.id && !byId.has(p.id)) byId.set(p.id, p);
    });
  }
  return [...byId.values()];
}

function getActiveFocuses() {
  const fromPl = (state.playlists || []).filter(p => p.is_focus && p.focus_status === "active");
  if (fromPl.length || (state.playlists || []).length) return fromPl;
  return (state.focusGoals || []).filter(g => (g.focus_status || g.status || "active") === "active");
}

function getReferencePlaylists() {
  return (state.playlists || []).filter(p => !p.is_focus);
}

function getGraduatedFocuses() {
  const fromPl = (state.playlists || []).filter(p => p.is_focus && p.focus_status === "graduated");
  if (fromPl.length || (state.playlists || []).length) return fromPl;
  return state.graduatedGoals || [];
}

function findCollectionById(id) {
  if (!id) return null;
  return getAllCollections().find(c => c.id === id) || null;
}

function collectionHasItem(col, itemId) {
  if (!col || !itemId) return false;
  return (col.items || []).some(it => it.item_id === itemId);
}

function dismissToast() {
  const el = document.getElementById("toast");
  if (!el) return;
  clearTimeout(window._toastT);
  el.classList.remove("show", "actionable", "toast-emerald");
}

function toast(msg, opts = null) {
  const el = document.getElementById("toast");
  if (!el) return;
  clearTimeout(window._toastT);
  el.classList.remove("actionable", "toast-emerald");
  el.innerHTML = "";

  const textSpan = document.createElement("span");
  textSpan.className = "toast-msg";
  textSpan.textContent = String(msg ?? "");
  el.appendChild(textSpan);

  let durationMs = 2800;
  if (opts && typeof opts === "object") {
    if (opts.variant === "emerald") el.classList.add("toast-emerald");
    if (opts.durationMs) durationMs = Number(opts.durationMs);
    if (opts.actionLabel && typeof opts.onAction === "function") {
      el.classList.add("actionable");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = opts.variant === "emerald" ? "emerald sm" : "primary sm";
      btn.textContent = opts.actionLabel;
      btn.onclick = () => {
        dismissToast();
        opts.onAction();
      };
      el.appendChild(btn);

      const closeBtn = document.createElement("button");
      closeBtn.type = "button";
      closeBtn.className = "toast-close";
      closeBtn.setAttribute("aria-label", "Dismiss notification");
      closeBtn.textContent = "✕";
      closeBtn.onclick = dismissToast;
      el.appendChild(closeBtn);
    }
  }

  el.classList.add("show");
  window._toastT = setTimeout(dismissToast, durationMs);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function escJsAttr(s) {
  return esc(
    JSON.stringify(String(s ?? ""))
      .replace(/\u2028/g, "\\u2028")
      .replace(/\u2029/g, "\\u2029"),
  );
}

function safeHref(url) {
  const raw = String(url ?? "").trim();
  if (!raw) return "#";
  if (raw.startsWith("/") || raw.startsWith("#")) {
    if (raw.startsWith("//")) return "#";
    return raw;
  }
  if (/^https?:\/\//i.test(raw)) return raw;
  return "#";
}

// ---------- CANONICAL MATCH LABEL HELPERS (REPLAY STUDIO CONVENTION) ----------
function _isPlaceholderPlayerName(name) {
  const s = String(name || "").trim();
  return !s || /^P[12]$/i.test(s);
}

function _dedupMatchChars(arr, singleFallback) {
  const raw = (Array.isArray(arr) && arr.length) ? arr : (singleFallback ? [singleFallback] : []);
  const out = [];
  const seen = new Set();
  for (const c of raw) {
    const clean = String(c || "").trim();
    if (!clean || clean === "?") continue;
    if (!seen.has(clean)) {
      seen.add(clean);
      out.push(clean);
    }
  }
  return out;
}

function _resolveMatchLabelParts(item, opts = {}) {
  const it = item || {};
  const allowPh = Boolean(opts.allowPlaceholders);
  const showRank = opts.showRank !== false;

  const ownSelf = String(it.self_player_name || "").trim();
  const fallbackSelf = String(opts.defaultSelfName || "").trim();
  const selfName = allowPh
    ? (ownSelf || fallbackSelf)
    : (!_isPlaceholderPlayerName(ownSelf)
        ? ownSelf
        : (!_isPlaceholderPlayerName(fallbackSelf) ? fallbackSelf : ""));

  const ownOpp = String(it.opp_player_name || "").trim();
  const oppName = allowPh
    ? ownOpp
    : (_isPlaceholderPlayerName(ownOpp) ? "" : ownOpp);

  const selfChars = _dedupMatchChars(it.self_chars, it.self_char_name);
  const oppChars = _dedupMatchChars(it.opp_chars, it.opp_char_name);

  const selfRank = showRank ? String(it.self_rank_letter || "").trim() : "";
  const oppRank = showRank ? String(it.opp_rank_letter || "").trim() : "";

  return {
    selfName,
    oppName,
    selfChars,
    oppChars,
    selfRank,
    oppRank,
    quarkId: String(it.quark_id || "").trim(),
  };
}

function formatMatchLabelText(item, opts = {}) {
  const { selfName, oppName, selfChars, oppChars, selfRank, oppRank, quarkId } = _resolveMatchLabelParts(item, opts);
  const hasNames = Boolean(selfName || oppName);
  const hasChars = Boolean(selfChars.length || oppChars.length);

  if (!hasNames && !hasChars) {
    return quarkId || "Match";
  }
  if (!hasNames) {
    return `${selfChars.length ? selfChars.join("/") : "?"} vs ${oppChars.length ? oppChars.join("/") : "?"}`;
  }

  const left = selfName
    ? `${selfName}${selfRank ? ` [${selfRank}]` : ""}${selfChars.length ? ` (${selfChars.join("/")})` : ""}`
    : (selfChars.length ? selfChars.join("/") : "P1");
  const right = oppName
    ? `${oppName}${oppRank ? ` [${oppRank}]` : ""}${oppChars.length ? ` (${oppChars.join("/")})` : ""}`
    : (oppChars.length ? oppChars.join("/") : "Opponent");
  return `${left} vs ${right}`;
}

function renderRankBadgeHtml(rankLetter) {
  const raw = String(rankLetter ?? "").trim().toUpperCase();
  if (!raw) return "";
  const map = {
    S: { cls: "s", norm: "S", glyph: "S", tip: "Rank S" },
    A: { cls: "a", norm: "A", glyph: "A", tip: "Rank A" },
    B: { cls: "b", norm: "B", glyph: "B", tip: "Rank B" },
    C: { cls: "c", norm: "C", glyph: "C", tip: "Rank C" },
    D: { cls: "d", norm: "D", glyph: "D", tip: "Rank D" },
    E: { cls: "e", norm: "E", glyph: "E", tip: "Rank E" },
    U: { cls: "u", norm: "U", glyph: "?", tip: "Unranked" },
    "?": { cls: "u", norm: "U", glyph: "?", tip: "Unranked" },
  };
  const info = map[raw];
  if (!info) return "";
  return `<span class="rank rank-${info.cls}" data-rank="${esc(info.norm)}" data-tip="Fightcade ${esc(info.tip)}" aria-label="Fightcade ${esc(info.tip)}">${esc(info.glyph)}</span>`;
}

function formatMatchLabelHtml(item, opts = {}) {
  const { selfName, oppName, selfChars, oppChars, selfRank, oppRank, quarkId } = _resolveMatchLabelParts(item, opts);
  const hasNames = Boolean(selfName || oppName);
  const hasChars = Boolean(selfChars.length || oppChars.length);

  if (!hasNames && !hasChars) {
    const fallback = esc(quarkId || "Match");
    return opts.stackedMobile
      ? `<span class="match-side self"><span class="match-player-name">${fallback}</span></span>`
      : fallback;
  }

  const rawFn = String(opts.charFilterFn || "").trim();
  const filterFn = /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(rawFn) ? rawFn : "";
  const stopStr = opts.stopPropagation ? "event.stopPropagation(); " : "";

  const activeCharsSet = opts.activeChars
    ? new Set((Array.isArray(opts.activeChars) ? opts.activeChars : [...opts.activeChars]).filter(Boolean))
    : null;

  const renderChars = (side, chars) => {
    if (!chars.length) return "?";
    if (!filterFn) return esc(chars.join("/"));
    const activeVal = side === "self" ? opts.activeSelfChar : opts.activeOppChar;
    return chars.map(ch => {
      const isAct = activeCharsSet ? activeCharsSet.has(ch) : (activeVal === ch);
      const tip = activeCharsSet ? `Filter by ${ch}` : (side === "self" ? `Filter by ${ch}` : `Filter vs ${ch}`);
      const callExpr = `${filterFn}(${escJsAttr(side)}, ${escJsAttr(ch)})`;
      return `<span class="match-char-filter${isAct ? " active-f" : ""}" role="button" tabindex="0" data-tip="${esc(tip)}" onclick="${stopStr}${callExpr}" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();${stopStr}${callExpr}}">${esc(ch)}</span>`;
    }).join(`<span class="match-char-sep">/</span>`);
  };

  if (!hasNames) {
    if (opts.stackedMobile) {
      return `<span class="match-side self"><span class="match-char self">${renderChars("self", selfChars)}</span></span><span class="match-side opp"><span class="match-vs">vs</span><span class="match-char opp">${renderChars("opp", oppChars)}</span></span>`;
    }
    return `<span class="match-char self">${renderChars("self", selfChars)}</span> <span class="match-vs">vs</span> <span class="match-char opp">${renderChars("opp", oppChars)}</span>`;
  }

  const selfRankBadge = renderRankBadgeHtml(selfRank);
  const oppRankBadge = renderRankBadgeHtml(oppRank);

  if (opts.stackedMobile) {
    const leftSide = selfName
      ? `<span class="match-player-name" title="${esc(selfName)}">${esc(selfName)}</span>${selfRankBadge}${selfChars.length ? `<span class="match-char self">(${renderChars("self", selfChars)})</span>` : ""}`
      : (selfChars.length ? `<span class="match-char self">${renderChars("self", selfChars)}</span>` : `<span class="match-player-name">P1</span>`);
    const rightSide = oppName
      ? `<span class="match-player-name" title="${esc(oppName)}">${esc(oppName)}</span>${oppRankBadge}${oppChars.length ? `<span class="match-char opp">(${renderChars("opp", oppChars)})</span>` : ""}`
      : (oppChars.length ? `<span class="match-char opp">${renderChars("opp", oppChars)}</span>` : `<span class="match-player-name">Opponent</span>`);
    return `<span class="match-side self">${leftSide}</span><span class="match-side opp"><span class="match-vs">vs</span>${rightSide}</span>`;
  }

  const leftHtml = selfName
    ? `${esc(selfName)}${selfRankBadge ? ` ${selfRankBadge}` : ""}${selfChars.length ? ` <span class="match-char self">(${renderChars("self", selfChars)})</span>` : ""}`
    : (selfChars.length ? `<span class="match-char self">${renderChars("self", selfChars)}</span>` : "P1");
  const rightHtml = oppName
    ? `${esc(oppName)}${oppRankBadge ? ` ${oppRankBadge}` : ""}${oppChars.length ? ` <span class="match-char opp">(${renderChars("opp", oppChars)})</span>` : ""}`
    : (oppChars.length ? `<span class="match-char opp">${renderChars("opp", oppChars)}</span>` : "Opponent");
  return `${leftHtml} <span class="match-vs">vs</span> ${rightHtml}`;
}

// ---------- HUMAN-READABLE TIME FORMATTING HELPERS ----------
function formatRoundClock(seconds) {
  const num = Number(seconds);
  const total = Math.max(0, Math.floor(Number.isFinite(num) ? num : 0));
  const mins = Math.floor(total / 60);
  const secs = total % 60;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function formatClipDuration(startGf, endGf, fps = 60) {
  if (startGf == null || endGf == null || startGf === "" || endGf === "") return "";
  const s = Number(startGf);
  const e = Number(endGf);
  const f = Number(fps) > 0 ? Number(fps) : 60;
  if (!Number.isFinite(s) || !Number.isFinite(e) || e <= s) return "";
  const sec = (e - s) / f;
  return `${Math.max(0.1, sec).toFixed(1)}s`;
}

function formatRelativeClipTime(gameFrame, startGf, fps = 60) {
  const gf = Number(gameFrame);
  const s = Number(startGf);
  const f = Number(fps) > 0 ? Number(fps) : 60;
  if (!Number.isFinite(gf)) return "0.0s";
  const base = Number.isFinite(s) ? s : 0;
  const sec = Math.max(0, (gf - base) / f);
  return `${sec.toFixed(1)}s`;
}

// ---------- LEGACY KIND BRIDGING & SYSTEM-ORIGIN BADGES ----------
function normalizeTagList(rawTags) {
  const arr = Array.isArray(rawTags)
    ? rawTags
    : (typeof rawTags === "string" ? rawTags.split(",") : []);
  const out = [];
  const seen = new Set();
  for (const t of arr) {
    const clean = String(t || "").trim().replace(/\s+/g, " ").slice(0, 40);
    if (!clean) continue;
    const key = clean.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(clean);
    if (out.length >= 12) break;
  }
  return out;
}

function getDisplayTagsForItem(item) {
  if (!item || typeof item !== "object") return [];
  const base = Array.isArray(item.tags) ? [...item.tags] : [];
  const kindKey = String(item.kind || "").trim().toLowerCase();
  const legacyLabel = LEGACY_KIND_TO_TAG[kindKey];
  if (legacyLabel && !base.some(t => String(t || "").trim().toLowerCase() === legacyLabel.toLowerCase())) {
    base.push(legacyLabel);
  }
  return normalizeTagList(base);
}

function getSystemOriginBadge(item) {
  if (!item || typeof item !== "object") return null;
  const src = String(item.source || item.edit_source || "").trim().toLowerCase();
  const entryType = String(item.entry_type || "").trim().toLowerCase();
  const kind = String(item.kind || "").trim().toLowerCase();
  if (src === "takeover_response" || item.is_response_clip || kind === "response") {
    return { cls: "kind-response", label: "Labbed Answer", tip: "Recorded via FBNeo Replay Takeover" };
  }
  if (entryType === "select_mark" || src === "select_mark" || kind === "select_mark") {
    return { cls: "kind-select_mark", label: "Bookmark", tip: "Saved moment bookmark" };
  }
  if (entryType === "set_note" || src === "set_note" || kind === "set_takeaway") {
    return { cls: "kind-set_takeaway", label: "Set Takeaway", tip: "Overall takeaway note for this set" };
  }
  if (src === "pro_clip" || kind === "pro_clip") {
    return { cls: "kind-pro_clip", label: "Top-Rank Clip", tip: "Retrieved from top-rank corpus" };
  }
  return null;
}

function renderSystemOriginBadgeHtml(item) {
  const b = getSystemOriginBadge(item);
  if (!b) return "";
  return `<span class="pill ${esc(b.cls)}" data-tip="${esc(b.tip)}">${esc(b.label)}</span>`;
}

function resolveSavedKindForItem(source, noteText, tagsArr) {
  const src = String(source || "").trim().toLowerCase();
  if (src === "takeover_response" || src === "response") return "response";
  if (src === "select_mark") {
    const hasNote = Boolean(String(noteText || "").trim());
    const hasTags = Array.isArray(tagsArr) && tagsArr.length > 0;
    return (hasNote || hasTags) ? "note" : "select_mark";
  }
  return "note";
}

// ---------- REUSABLE COLLAPSED TAG MULTI-SELECT COMPONENT ----------
const _tagPickers = Object.create(null);

function _ensureTagPicker(pickerId) {
  const id = String(pickerId || "").trim();
  if (!_tagPickers[id]) {
    _tagPickers[id] = {
      id,
      tags: [],
      open: false,
      query: "",
      buttonLabel: "+ Add tags",
      placeholder: "Filter or type new tag… (Enter to add)",
      onChange: null,
    };
  }
  return _tagPickers[id];
}

function initTagPicker(pickerId, initialTags = [], opts = {}) {
  const p = _ensureTagPicker(pickerId);
  p.tags = normalizeTagList(initialTags);
  p.open = false;
  p.query = "";
  if (opts && opts.buttonLabel) p.buttonLabel = String(opts.buttonLabel);
  if (opts && opts.placeholder) p.placeholder = String(opts.placeholder);
  if (opts && typeof opts.onChange === "function") p.onChange = opts.onChange;
  return p;
}

function getTagPickerTags(pickerId) {
  const p = _tagPickers[String(pickerId || "").trim()];
  return p ? [...p.tags] : [];
}

function setTagPickerTags(pickerId, tags, opts = {}) {
  const p = _ensureTagPicker(pickerId);
  p.tags = normalizeTagList(tags);
  if (opts.close) {
    p.open = false;
    p.query = "";
  }
  refreshTagPickerDom(p.id, { focusInput: false });
  return [...p.tags];
}

function _tagPickerOptionPool(p) {
  const pool = [...PRESET_TAGS];
  const seen = new Set(pool.map(t => t.toLowerCase()));
  for (const t of (p.tags || [])) {
    const k = t.toLowerCase();
    if (!seen.has(k)) {
      seen.add(k);
      pool.push(t);
    }
  }
  return pool;
}

function _renderTagPickerListHtml(p) {
  const rawQ = String(p.query || "").trim().replace(/\s+/g, " ").slice(0, 40);
  const qLower = rawQ.toLowerCase();
  const selLower = new Set((p.tags || []).map(t => t.toLowerCase()));
  const pool = _tagPickerOptionPool(p);
  const filtered = qLower
    ? pool.filter(t => t.toLowerCase().includes(qLower))
    : pool;
  const exactMatch = qLower ? pool.some(t => t.toLowerCase() === qLower) : false;

  const optsHtml = filtered.map(t => {
    const isSel = selLower.has(t.toLowerCase());
    return `<button type="button" class="tag-picker-opt${isSel ? " is-selected" : ""}" onclick="event.stopPropagation(); toggleTagPickerTag(${escJsAttr(p.id)}, ${escJsAttr(t)})"><span class="tag-picker-check">${isSel ? "✓" : ""}</span><span class="tag-picker-opt-lbl">${esc(t)}</span></button>`;
  }).join("");

  const createHtml = (rawQ && !exactMatch)
    ? `<button type="button" class="tag-picker-opt is-create" onclick="event.stopPropagation(); addTagPickerCustom(${escJsAttr(p.id)})"><span class="tag-picker-check">+</span><span class="tag-picker-opt-lbl">Create "${esc(rawQ)}"</span></button>`
    : "";

  if (!optsHtml && !createHtml) {
    return `<div class="tag-picker-empty">No matching tags</div>`;
  }
  return optsHtml + createHtml;
}

function _renderTagPickerInnerHtml(p) {
  const chipsHtml = (p.tags || []).map(t =>
    `<span class="tag-chip-sel"><span>${esc(t)}</span><button type="button" class="tag-chip-x" aria-label="Remove ${esc(t)}" onclick="event.stopPropagation(); removeTagPickerTag(${escJsAttr(p.id)}, ${escJsAttr(t)})">×</button></span>`
  ).join("");

  const menuHtml = p.open
    ? `
      <div class="tag-picker-menu" onclick="event.stopPropagation()">
        <input type="text"
               id="tpInput_${esc(p.id)}"
               class="tag-picker-search"
               value="${esc(p.query || "")}"
               placeholder="${esc(p.placeholder)}"
               autocomplete="off"
               oninput="onTagPickerSearchInput(${escJsAttr(p.id)}, this.value)"
               onkeydown="onTagPickerSearchKeydown(${escJsAttr(p.id)}, event)" />
        <div class="tag-picker-list" id="tpList_${esc(p.id)}">${_renderTagPickerListHtml(p)}</div>
      </div>
    `
    : "";

  return `
    <div class="tag-picker-row">
      ${chipsHtml}
      <span class="tag-picker-anchor">
        <button type="button"
                class="tag-picker-toggle${p.open ? " is-open" : ""}"
                aria-expanded="${p.open ? "true" : "false"}"
                onclick="event.stopPropagation(); toggleTagPickerMenu(${escJsAttr(p.id)})">${esc(p.buttonLabel)} ▾</button>
        ${menuHtml}
      </span>
    </div>
  `;
}

function renderTagPickerHtml(pickerId, selectedTags = null, opts = {}) {
  const p = _ensureTagPicker(pickerId);
  if (selectedTags !== null && selectedTags !== undefined) {
    p.tags = normalizeTagList(selectedTags);
  }
  if (opts && opts.buttonLabel) p.buttonLabel = String(opts.buttonLabel);
  if (opts && opts.placeholder) p.placeholder = String(opts.placeholder);
  if (opts && typeof opts.onChange === "function") p.onChange = opts.onChange;
  return `<div class="tag-picker" id="tpWrap_${esc(p.id)}" data-tag-picker="${esc(p.id)}">${_renderTagPickerInnerHtml(p)}</div>`;
}

function refreshTagPickerDom(pickerId, opts = {}) {
  const id = String(pickerId || "").trim();
  const p = _tagPickers[id];
  if (!p || typeof document === "undefined") return;
  const wrap = document.getElementById(`tpWrap_${id}`);
  if (!wrap) return;
  wrap.innerHTML = _renderTagPickerInnerHtml(p);
  if (opts.focusInput && p.open) {
    const inp = document.getElementById(`tpInput_${id}`);
    if (inp) {
      inp.focus({ preventScroll: true });
      const len = inp.value.length;
      try { inp.setSelectionRange(len, len); } catch (_) {}
    }
  }
}

function hasOpenTagPicker() {
  return Object.keys(_tagPickers).some(k => _tagPickers[k] && _tagPickers[k].open);
}

function closeAllTagPickers(exceptId = null) {
  let closed = false;
  for (const k of Object.keys(_tagPickers)) {
    if (exceptId && k === exceptId) continue;
    const p = _tagPickers[k];
    if (p && p.open) {
      p.open = false;
      p.query = "";
      closed = true;
      refreshTagPickerDom(k, { focusInput: false });
    }
  }
  return closed;
}

function toggleTagPickerMenu(pickerId) {
  const p = _ensureTagPicker(pickerId);
  const nextOpen = !p.open;
  closeAllTagPickers(p.id);
  if (typeof closeAllCollectionPickers === "function") closeAllCollectionPickers();
  if (typeof closeAllHubCardMenus === "function") closeAllHubCardMenus();
  if (typeof closeNotesTagFilterMenu === "function") closeNotesTagFilterMenu();
  p.open = nextOpen;
  if (!nextOpen) p.query = "";
  refreshTagPickerDom(p.id, { focusInput: nextOpen });
}

function toggleTagPickerTag(pickerId, tag) {
  const p = _ensureTagPicker(pickerId);
  const clean = String(tag || "").trim().replace(/\s+/g, " ").slice(0, 40);
  if (!clean) return;
  const key = clean.toLowerCase();
  const idx = p.tags.findIndex(t => t.toLowerCase() === key);
  if (idx >= 0) {
    p.tags.splice(idx, 1);
  } else {
    p.tags = normalizeTagList([...p.tags, clean]);
  }
  if (typeof p.onChange === "function") p.onChange([...p.tags]);
  refreshTagPickerDom(p.id, { focusInput: p.open });
}

function removeTagPickerTag(pickerId, tag) {
  const p = _ensureTagPicker(pickerId);
  const key = String(tag || "").trim().toLowerCase();
  p.tags = p.tags.filter(t => t.toLowerCase() !== key);
  if (typeof p.onChange === "function") p.onChange([...p.tags]);
  refreshTagPickerDom(p.id, { focusInput: p.open });
}

function addTagPickerCustom(pickerId, explicitVal = null) {
  const p = _ensureTagPicker(pickerId);
  const raw = explicitVal != null ? String(explicitVal) : String(p.query || "");
  const clean = raw.trim().replace(/\s+/g, " ").slice(0, 40);
  if (!clean) return false;
  const key = clean.toLowerCase();
  // Match canonical preset casing if user typed an existing preset in different case
  const canonical = PRESET_TAGS.find(t => t.toLowerCase() === key) || clean;
  if (!p.tags.some(t => t.toLowerCase() === key)) {
    p.tags = normalizeTagList([...p.tags, canonical]);
  }
  p.query = "";
  if (typeof p.onChange === "function") p.onChange([...p.tags]);
  refreshTagPickerDom(p.id, { focusInput: p.open });
  return true;
}

function onTagPickerSearchInput(pickerId, value) {
  const p = _ensureTagPicker(pickerId);
  p.query = String(value || "");
  if (typeof document === "undefined") return;
  const listEl = document.getElementById(`tpList_${p.id}`);
  if (listEl) {
    listEl.innerHTML = _renderTagPickerListHtml(p);
  }
}

function onTagPickerSearchKeydown(pickerId, ev) {
  if (!ev) return;
  const p = _ensureTagPicker(pickerId);
  if (ev.key === "Escape") {
    ev.preventDefault();
    ev.stopPropagation();
    p.open = false;
    p.query = "";
    refreshTagPickerDom(p.id, { focusInput: false });
    return;
  }
  if (ev.key === "Enter") {
    ev.preventDefault();
    ev.stopPropagation();
    const rawQ = String(p.query || "").trim();
    if (!rawQ) return;
    const pool = _tagPickerOptionPool(p);
    const exact = pool.find(t => t.toLowerCase() === rawQ.toLowerCase());
    if (exact) {
      // If exact match exists and isn't selected, select it; otherwise clear query
      const key = exact.toLowerCase();
      if (!p.tags.some(t => t.toLowerCase() === key)) {
        p.tags = normalizeTagList([...p.tags, exact]);
        if (typeof p.onChange === "function") p.onChange([...p.tags]);
      }
      p.query = "";
      refreshTagPickerDom(p.id, { focusInput: true });
    } else {
      addTagPickerCustom(p.id, rawQ);
    }
  }
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("click", ev => {
    if (!hasOpenTagPicker()) return;
    const target = ev && ev.target;
    if (target && typeof target.closest === "function" && target.closest(".tag-picker")) return;
    closeAllTagPickers();
  });
}

// ---------- REUSABLE COLLAPSED COLLECTION (+ PLAYLIST) MULTI-SELECT COMPONENT ----------
const _colPickers = Object.create(null);

function _ensureColPicker(pickerId) {
  const id = String(pickerId || "").trim();
  if (!_colPickers[id]) {
    _colPickers[id] = {
      id,
      selectedIds: [],
      open: false,
      buttonLabel: "+ Playlist",
      onToggle: null,
      onCreate: null,
    };
  }
  return _colPickers[id];
}

function _normalizeColIds(ids) {
  const out = [];
  const seen = new Set();
  for (const raw of (ids || [])) {
    const s = String(raw || "").trim();
    if (!s || seen.has(s)) continue;
    seen.add(s);
    out.push(s);
  }
  return out;
}

function _renderColPickerInnerHtml(p) {
  const selectedSet = new Set(p.selectedIds || []);
  const activeFocuses = getActiveFocuses();
  const refPlaylists = getReferencePlaylists();

  const assignedFocusChips = activeFocuses
    .filter(g => selectedSet.has(g.id))
    .map(g => {
      const lbl = g.name || g.title || "Focus";
      return `<span class="col-chip is-focus" data-col-id="${esc(g.id)}" data-tip="Active Focus: ${esc(lbl)}"><span class="col-chip-lbl">📌 ${esc(lbl)}</span><button type="button" class="col-chip-x" aria-label="Remove ${esc(lbl)}" onclick="event.stopPropagation(); triggerColPickerToggle(${escJsAttr(p.id)}, ${escJsAttr(g.id)}, false)">×</button></span>`;
    })
    .join("");

  const assignedPlChips = refPlaylists
    .filter(pl => selectedSet.has(pl.id))
    .map(pl => {
      const lbl = pl.name || "Playlist";
      return `<span class="col-chip" data-col-id="${esc(pl.id)}" data-tip="Playlist: ${esc(lbl)}"><span class="col-chip-lbl">▶ ${esc(lbl)}</span><button type="button" class="col-chip-x" aria-label="Remove ${esc(lbl)}" onclick="event.stopPropagation(); triggerColPickerToggle(${escJsAttr(p.id)}, ${escJsAttr(pl.id)}, false)">×</button></span>`;
    })
    .join("");

  let menuHtml = "";
  if (p.open) {
    const focusSectionHtml = activeFocuses.length
      ? `
        <div class="col-picker-sec-lbl">ACTIVE FOCUSES</div>
        ${activeFocuses.map(g => {
          const isSel = selectedSet.has(g.id);
          const lbl = g.name || g.title || "Focus";
          return `<button type="button" class="col-picker-opt is-focus${isSel ? " is-selected" : ""}" onclick="event.stopPropagation(); triggerColPickerToggle(${escJsAttr(p.id)}, ${escJsAttr(g.id)}, true)"><span class="col-picker-mark">${isSel ? "✓" : "○"}</span><span class="col-picker-icon" aria-hidden="true">📌</span><span class="col-picker-opt-lbl">${esc(lbl)}</span></button>`;
        }).join("")}
      `
      : "";

    const plSectionHtml = refPlaylists.length
      ? `
        <div class="col-picker-sec-lbl">PLAYLISTS</div>
        ${refPlaylists.map(pl => {
          const isSel = selectedSet.has(pl.id);
          const lbl = pl.name || "Playlist";
          return `<button type="button" class="col-picker-opt${isSel ? " is-selected" : ""}" onclick="event.stopPropagation(); triggerColPickerToggle(${escJsAttr(p.id)}, ${escJsAttr(pl.id)}, true)"><span class="col-picker-mark">${isSel ? "✓" : "○"}</span><span class="col-picker-icon" aria-hidden="true">▶</span><span class="col-picker-opt-lbl">${esc(lbl)}</span></button>`;
        }).join("")}
      `
      : "";

    const emptyHtml = (!activeFocuses.length && !refPlaylists.length)
      ? `<div class="col-picker-empty">No focuses or playlists yet</div>`
      : "";

    const createHtml = `<button type="button" class="col-picker-opt is-create" onclick="event.stopPropagation(); triggerColPickerCreate(${escJsAttr(p.id)})"><span class="col-picker-mark">+</span><span class="col-picker-opt-lbl">New Playlist…</span></button>`;

    menuHtml = `
      <div class="col-picker-menu" onclick="event.stopPropagation()">
        <div class="col-picker-list">
          ${focusSectionHtml}
          ${plSectionHtml}
          ${emptyHtml}
        </div>
        ${createHtml}
      </div>
    `;
  }

  return `
    <div class="col-picker-row">
      ${assignedFocusChips}
      ${assignedPlChips}
      <span class="col-picker-anchor">
        <button type="button"
                class="col-picker-toggle${p.open ? " is-open" : ""}"
                aria-expanded="${p.open ? "true" : "false"}"
                data-tip="Add or remove from Active Focuses &amp; Playlists"
                onclick="event.stopPropagation(); toggleColPickerMenu(${escJsAttr(p.id)})">${esc(p.buttonLabel)} ▾</button>
        ${menuHtml}
      </span>
    </div>
  `;
}

function renderCollectionPickerHtml(pickerId, selectedIds = [], opts = {}) {
  const p = _ensureColPicker(pickerId);
  if (selectedIds !== null && selectedIds !== undefined) {
    p.selectedIds = _normalizeColIds(selectedIds);
  }
  if (opts && opts.buttonLabel) p.buttonLabel = String(opts.buttonLabel);
  if (opts && typeof opts.onToggle === "function") p.onToggle = opts.onToggle;
  if (opts && typeof opts.onCreate === "function") p.onCreate = opts.onCreate;
  return `<div class="col-picker" id="cpWrap_${esc(p.id)}" data-col-picker="${esc(p.id)}">${_renderColPickerInnerHtml(p)}</div>`;
}

function refreshColPickerDom(pickerId) {
  const id = String(pickerId || "").trim();
  const p = _colPickers[id];
  if (!p || typeof document === "undefined") return;
  const wrap = document.getElementById(`cpWrap_${id}`);
  if (!wrap) return;
  wrap.innerHTML = _renderColPickerInnerHtml(p);
}

function hasOpenCollectionPicker() {
  return Object.keys(_colPickers).some(k => _colPickers[k] && _colPickers[k].open);
}

function closeAllCollectionPickers(exceptId = null) {
  let closed = false;
  for (const k of Object.keys(_colPickers)) {
    if (exceptId && k === exceptId) continue;
    const p = _colPickers[k];
    if (p && p.open) {
      p.open = false;
      closed = true;
      refreshColPickerDom(k);
    }
  }
  return closed;
}

function toggleColPickerMenu(pickerId) {
  const p = _ensureColPicker(pickerId);
  const nextOpen = !p.open;
  closeAllCollectionPickers(p.id);
  if (typeof closeAllTagPickers === "function") closeAllTagPickers();
  if (typeof closeAllHubCardMenus === "function") closeAllHubCardMenus();
  if (typeof closeNotesTagFilterMenu === "function") closeNotesTagFilterMenu();
  p.open = nextOpen;
  refreshColPickerDom(p.id);
}

function triggerColPickerToggle(pickerId, colId, keepOpen = true) {
  const p = _ensureColPicker(pickerId);
  if (keepOpen) p.open = true;
  if (typeof p.onToggle === "function") {
    p.onToggle(colId, p.id);
  }
}

function triggerColPickerCreate(pickerId) {
  const p = _ensureColPicker(pickerId);
  p.open = false;
  refreshColPickerDom(p.id);
  if (typeof p.onCreate === "function") {
    p.onCreate(p.id);
  }
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
  document.addEventListener("click", ev => {
    if (!hasOpenCollectionPicker()) return;
    const target = ev && ev.target;
    if (target && typeof target.closest === "function" && target.closest(".col-picker")) return;
    closeAllCollectionPickers();
  });
}

async function api(url, opts = {}) {
  const res = await fetch(apiUrl(url), {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({ ok: false, error: "Invalid server response" }));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

async function companionApi(url, opts = {}) {
  const tok = getCompanionToken();
  const extraHeaders = tok ? { "X-Fightcade-Lab-Token": tok } : {};
  const res = await fetch(companionUrl(url), {
    headers: { "Content-Type": "application/json", ...extraHeaders, ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({ ok: false, error: "Invalid Companion response" }));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

function getCompanionToken() {
  try { return localStorage.getItem("fc_companion_token") || ""; } catch (_) { return ""; }
}

function setCompanionToken(tok) {
  try {
    if (tok) localStorage.setItem("fc_companion_token", tok);
    else localStorage.removeItem("fc_companion_token");
  } catch (_) {}
}

function buildTakeoverUri(payload) {
  const qs = new URLSearchParams();
  if (payload && payload.quark_id) qs.set("quark_id", String(payload.quark_id));
  if (payload && payload.frame) qs.set("frame", String(payload.frame));
  if (payload && payload.round_id) qs.set("round_id", String(payload.round_id));
  if (payload && payload.start_gf) qs.set("start_gf", String(payload.start_gf));
  if (payload && payload.end_gf) qs.set("end_gf", String(payload.end_gf));
  if (payload && payload.player) qs.set("player", String(payload.player));
  if (payload && payload.label) qs.set("label", String(payload.label).slice(0, 100));
  if (payload && payload.note) qs.set("note", String(payload.note).slice(0, 600));
  return `fightcadelab://takeover?${qs.toString()}`;
}

async function probeLocalCompanion() {
  if (!isHostedMode()) return;
  try {
    const res = await companionApi("/api/companion/ping");
    const connected = Boolean(res.ok && res.token_valid && res.can_takeover);
    state.companionBridge = {
      connected,
      reachable: Boolean(res.ok),
      install_id: res.install_id || null,
    };
    state.companionConnected = connected;
    if (connected) {
      state.capabilities.can_takeover = true;
      if (state.activeView === "home" && state.homeData && typeof renderHomeWorkbench === "function") {
        renderHomeWorkbench();
      }
    }
  } catch (_) {
    state.companionBridge = { connected: false, reachable: false, install_id: null };
    state.companionConnected = false;
  }
}

async function handleBrowserPairRoute(code) {
  const cleanCode = String(code || "").trim();
  if (!cleanCode) {
    toast("Missing Companion pairing code.");
    navigateTo("home");
    return;
  }
  if (isHostedMode() && !state.auth.authenticated) {
    const nextPath = `/pair?code=${encodeURIComponent(cleanCode)}`;
    toast("Sign in with Discord to connect your Fightcade Lab Companion...");
    window.location.href = apiUrl(`/api/auth/discord/start?next=${encodeURIComponent(nextPath)}`);
    return;
  }
  try {
    let cloudToken = "";
    if (isHostedMode()) {
      const tokRes = await api("/api/auth/companion-token", {
        method: "POST",
        body: JSON.stringify({ label: "Windows Companion" }),
      });
      cloudToken = String((tokRes && tokRes.token) || "").trim();
      if (!cloudToken) {
        throw new Error("Could not mint cloud Companion token — try signing in again.");
      }
    }
    const pairPayload = { code: cleanCode, origin: window.location.origin };
    if (cloudToken) pairPayload.cloud_token = cloudToken;
    const res = await companionApi("/api/companion/pair", {
      method: "POST",
      body: JSON.stringify(pairPayload),
    });
    if (res.token) {
      setCompanionToken(res.token);
      state.companionBridge = { connected: true, reachable: true, install_id: res.install_id || null };
      state.companionConnected = true;
      state.capabilities.can_takeover = true;
      state.capabilities.has_cloud_token = Boolean(cloudToken || state.capabilities.has_cloud_token);
      toast("Connected! Your Fightcade Lab Companion is paired — you can return to the app to share clips.");
    }
  } catch (err) {
    toast("Pairing failed: " + err.message + " — make sure Fightcade Lab Companion is running locally.");
  }
  navigateTo("home");
}

function copyText(text, successMsg = "Copied to clipboard") {
  if (!text) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => toast(successMsg)).catch(() => _fallbackCopy(text, successMsg));
  } else {
    _fallbackCopy(text, successMsg);
  }
}

function _fallbackCopy(text, successMsg) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch (_) {}
  document.body.removeChild(ta);
  toast(successMsg);
}

function formatDuration(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return m > 0 ? `${m}m ${rem}s` : `${rem}s`;
}

function formatBytes(bytes) {
  const n = Number(bytes || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function formatRelativeTime(epochMs, fallbackStr = "") {
  const val = Number(epochMs || 0);
  if (!val || val <= 0) return fallbackStr || "";
  const ms = val > 10_000_000_000 ? val : val * 1000;
  const diffS = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (diffS < 60) return "just now";
  if (diffS < 3600) return `${Math.floor(diffS / 60)} min ago`;
  if (diffS < 86400) return `${Math.floor(diffS / 3600)}h ago`;
  const days = Math.floor(diffS / 86400);
  if (days === 1) return "yesterday";
  if (days < 14) return `${days}d ago`;
  return fallbackStr || new Date(ms).toISOString().slice(0, 10);
}

function formatTimestampWithTz(epochOrIso, fallbackStr = "") {
  let dt = null;
  if (typeof epochOrIso === "number" && epochOrIso > 0) {
    dt = new Date(epochOrIso > 10_000_000_000 ? epochOrIso : epochOrIso * 1000);
  } else if (typeof epochOrIso === "string" && epochOrIso.trim()) {
    const s = epochOrIso.trim();
    if (/^\d{4}-\d{2}-\d{2}T/.test(s)) {
      const parsed = new Date(s);
      if (!Number.isNaN(parsed.getTime())) dt = parsed;
    }
  }
  if (!dt || Number.isNaN(dt.getTime())) {
    return fallbackStr || "";
  }
  const pad = n => String(n).padStart(2, "0");
  const ymd = `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`;
  const hm = `${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  let tz = "";
  try {
    const parts = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(dt);
    tz = (parts.find(p => p.type === "timeZoneName") || {}).value || "";
  } catch (_) {}
  return tz ? `${ymd} ${hm} ${tz}` : `${ymd} ${hm}`;
}

function canTakeoverNow() {
  return Boolean(state.capabilities && state.capabilities.can_takeover);
}

function isProCorpusEnabled() {
  return Boolean(state.capabilities && state.capabilities.pro_corpus_enabled);
}

function isTouchDevice() {
  return Boolean(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
}

function isMobileMode() {
  const narrow = Boolean(window.matchMedia && window.matchMedia("(max-width: 899px)").matches);
  return narrow || isTouchDevice();
}

function syncProCorpusGateUi() {
  const enabled = isProCorpusEnabled();
  const tip = "On the roadmap";
  const applyTip = (el, txt) => {
    el.removeAttribute("title");
    el.dataset.tip = txt;
  };
  const navIns = document.getElementById("navInsights");
  if (navIns) {
    navIns.disabled = false;
    navIns.setAttribute("aria-disabled", enabled ? "false" : "true");
    navIns.classList.toggle("pro-gated", !enabled);
    navIns.classList.toggle("hidden", !enabled);
    applyTip(navIns, enabled ? "AI & Top-Rank Insights" : tip);
  }
  const proTab = document.getElementById("sideTabProSearch");
  if (proTab) {
    proTab.disabled = false;
    proTab.setAttribute("aria-disabled", enabled ? "false" : "true");
    proTab.classList.toggle("pro-gated", !enabled);
    proTab.classList.toggle("hidden", !enabled);
    applyTip(proTab, enabled ? "Search similar top-rank sequences" : tip);
  }
  const runProBtn = document.getElementById("btnRunProSearch");
  if (runProBtn) {
    runProBtn.disabled = false;
    runProBtn.setAttribute("aria-disabled", enabled ? "false" : "true");
    runProBtn.classList.toggle("pro-gated", !enabled);
    applyTip(runProBtn, enabled ? "Search top-rank sets" : tip);
  }
}

function syncGrouflonGuideUi() {
  const banner = document.getElementById("grouflonBanner");
  if (!banner) return;
  const caps = state.capabilities || {};
  const dismissed = localStorage.getItem("fc_dismiss_grouflon_banner") === "1";
  const show = caps.mode === "local_companion" && caps.has_grouflon === false && !dismissed;
  banner.classList.toggle("hidden", !show);
  const targetEl = document.getElementById("grouflonTargetDir");
  if (targetEl) {
    const fbDir = (caps.environment && caps.environment.fbneo_dir) || "your Fightcade\\emulator\\fbneo folder";
    targetEl.textContent = fbDir;
  }
}

function dismissGrouflonBanner() {
  try { localStorage.setItem("fc_dismiss_grouflon_banner", "1"); } catch (_) {}
  const banner = document.getElementById("grouflonBanner");
  if (banner) banner.classList.add("hidden");
}

function copyGrouflonInstallDir() {
  const caps = state.capabilities || {};
  const fbDir = (caps.environment && caps.environment.fbneo_dir) || "";
  if (!fbDir) {
    toast("Fightcade FBNeo folder not detected yet.");
    return;
  }
  copyText(fbDir, "Copied Fightcade FBNeo folder path");
}

async function loadCapabilities() {
  try {
    const caps = await api("/api/capabilities");
    state.capabilities = { ...state.capabilities, ...caps };
    const badge = document.getElementById("modeBadge");
    if (badge) {
      badge.textContent = caps.mode === "local_companion" ? "3S · Local" : "3S · Web";
    }
    const statusWrap = document.querySelector(".status-wrap");
    if (statusWrap) {
      statusWrap.classList.toggle("hidden", caps.mode === "hosted_web");
    }
    syncProCorpusGateUi();
    syncGrouflonGuideUi();
    renderAuthControls();
  } catch (_) {}
}

async function loadAuthState() {
  if (!isHostedMode()) {
    renderAuthControls();
    return;
  }
  try {
    const res = await api("/api/auth/me");
    state.auth = {
      authenticated: Boolean(res.authenticated && res.user),
      user: res.user || null,
      quota: res.quota || null,
      has_byoc: Boolean(res.has_byoc),
    };
    if (state.auth.user && !state.username) {
      state.username = state.auth.user.fightcade_username || state.auth.user.discord_username || "";
    }
  } catch (_) {
    state.auth = { authenticated: false, user: null, quota: null, has_byoc: false };
  }
  renderAuthControls();
}

function renderAuthControls() {
  const host = document.getElementById("authControls");
  if (!host) return;
  if (!isHostedMode()) {
    host.classList.add("hidden");
    return;
  }
  host.classList.remove("hidden");
  if (!state.auth.authenticated || !state.auth.user) {
    const next = encodeURIComponent(window.location.pathname + window.location.search + window.location.hash);
    host.innerHTML = `<a class="btn-link primary sm" href="${esc(safeHref(apiUrl(`/api/auth/discord/start?next=${next}`)))}">Sign in with Discord</a>`;
    return;
  }
  const u = state.auth.user;
  host.innerHTML = `
    <div class="auth-pill-wrap">
      <button class="sm" onclick="toggleAccountMenu()" title="Account, data export &amp; settings">
        ${esc(u.discord_username || "Account")} ▾
      </button>
      <div id="accountMenu" class="account-menu hidden">
        <div style="font-size:12px; color:var(--muted); margin-bottom:8px;">
          Signed in as <strong>${esc(u.discord_username)}</strong>
          ${state.auth.quota ? `<div style="margin-top:2px;">Clip Storage: ${formatBytes(state.auth.quota.user_used_bytes)} / ${formatBytes(state.auth.quota.user_cap_bytes)}</div>` : ""}
        </div>
        <button class="sm" style="width:100%; margin-bottom:6px;" onclick="exportMyAccountData()">⬇ Export My Data (JSON)</button>
        <button class="sm" style="width:100%; margin-bottom:6px;" onclick="logoutAccount()">Sign Out</button>
        <button class="sm danger" style="width:100%;" onclick="deleteMyAccount()">Delete Account &amp; Clips</button>
      </div>
    </div>
  `;
}

function toggleAccountMenu() {
  const el = document.getElementById("accountMenu");
  if (el) el.classList.toggle("hidden");
}

function exportMyAccountData() {
  window.location.href = apiUrl("/api/account/export");
}

async function logoutAccount() {
  try {
    await api("/api/auth/logout", { method: "POST", body: JSON.stringify({}) });
    state.auth = { authenticated: false, user: null, quota: null };
    state.hubNotes = [];
    state.hubCounts = {};
    state.hubAvailableChars = { self: [], opp: [] };
    state.playlists = [];
    state.focusGoals = [];
    state.graduatedGoals = [];
    renderAuthControls();
    if (state.activeView === "home" && typeof loadHomeWorkbench === "function") {
      loadHomeWorkbench();
    } else if (state.activeView === "notes" && typeof renderHubNotes === "function") {
      renderHubNotes();
    }
    toast("Signed out");
  } catch (err) {
    toast("Sign out failed: " + err.message);
  }
}

async function deleteMyAccount() {
  if (!confirm("Permanently delete your account, notes, playlists, and all uploaded clips? This cannot be undone.")) return;
  try {
    await api("/api/account", { method: "DELETE" });
    state.auth = { authenticated: false, user: null, quota: null };
    renderAuthControls();
    toast("Account and all uploaded clips permanently deleted.");
    navigateTo("home");
  } catch (err) {
    toast("Delete failed: " + err.message);
  }
}

async function runPracticeButton(btnEl, asyncFn) {
  if (btnEl && btnEl.dataset && btnEl.dataset.busy === "1") return null;
  let prevHtml = "";
  let prevMinW = "";
  if (btnEl) {
    btnEl.dataset.busy = "1";
    prevHtml = btnEl.innerHTML;
    prevMinW = btnEl.style ? (btnEl.style.minWidth || "") : "";
    const rect = typeof btnEl.getBoundingClientRect === "function" ? btnEl.getBoundingClientRect() : null;
    if (rect && rect.width > 0 && btnEl.style) {
      btnEl.style.minWidth = `${Math.ceil(rect.width)}px`;
    }
    btnEl.disabled = true;
    btnEl.classList.add("is-busy");
    btnEl.innerHTML = `<span class="btn-spin" aria-hidden="true"></span>Preparing...`;
  }
  try {
    return await asyncFn();
  } finally {
    if (btnEl) {
      delete btnEl.dataset.busy;
      btnEl.innerHTML = prevHtml;
      if (btnEl.style) btnEl.style.minWidth = prevMinW;
      btnEl.classList.remove("is-busy");
      btnEl.disabled = false;
    }
  }
}

async function launchDrill(selectionId, extraOpts = {}, btnEl = null) {
  if (extraOpts && typeof extraOpts === "object" && "nodeType" in extraOpts) {
    btnEl = extraOpts;
    extraOpts = {};
  }
  if (isMobileMode()) {
    toast("Practice / Replay Takeover is only available on desktop.");
    return null;
  }
  if (!canTakeoverNow()) {
    openCompanionModal("Replay Takeover");
    return null;
  }
  return runPracticeButton(btnEl, async () => {
    try {
      const res = await companionApi("/api/takeover", {
        method: "POST",
        body: JSON.stringify({ selection_id: selectionId, ...(extraOpts || {}) }),
      });
      if (res.warning) {
        toast("Launched (note: " + res.warning + ")");
      } else {
        toast("Launched Takeover in FBNeo (Select Retry · Alt+5 Sandbox · Alt+6/7/8 Speed)");
      }
      return res;
    } catch (err) {
      toast("Launch failed: " + err.message);
      return null;
    }
  });
}

async function launchTakeoverPayload(payload, btnEl = null) {
  if (isMobileMode()) {
    toast("Practice / Replay Takeover is only available on desktop.");
    return null;
  }
  if (!canTakeoverNow()) {
    openCompanionModal(payload && payload.label ? payload.label : "Replay Takeover");
    return null;
  }
  return runPracticeButton(btnEl, async () => {
    try {
      const res = await companionApi("/api/takeover", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      if (res.warning) {
        toast("Launched (note: " + res.warning + ")");
      } else {
        const via = res.baked_ad_hoc
          ? "Baked & launched instant Takeover"
          : (res.method === "instant_savestate" ? "Launched instant Takeover" : "Launched stream-seek Takeover");
        toast(`${via} (P${res.player} · ${res.speed || 1}x) — Select Retry · Alt+5 Sandbox`);
      }
      return res;
    } catch (err) {
      toast("Launch failed: " + err.message);
      return null;
    }
  });
}

// ---------- CALM COMPANION EXPLAINER, SIGN-IN ONBOARDING, REPORT, TERMS & SHARE MOMENT MODALS ----------
function openCompanionModal(contextLabel = "", takeoverPayload = null, opts = null) {
  const modal = document.getElementById("companionModal");
  if (!modal) return;
  const signInMode = Boolean(
    (opts && opts.signInRequired) || (isHostedMode() && !state.auth.authenticated)
  );
  const titleEl = document.getElementById("companionModalTitle");
  const ctxEl = document.getElementById("companionModalContext");
  const subEl = document.getElementById("companionModalSubtext");
  const dismissBtn = document.getElementById("btnCompanionDismiss");
  const signInBtn = document.getElementById("btnCompanionSignIn");
  const uriBtn = document.getElementById("btnCompanionUriFallback");
  const dlBtn = document.getElementById("btnCompanionDownload");

  if (signInMode) {
    if (titleEl) titleEl.textContent = "▶ Practice in Replay Takeover";
    if (ctxEl) {
      ctxEl.textContent = "Practice this exact moment inside FBNeo with 1-frame retry & slow-mo — sign in with Discord to set up the free Windows Companion.";
    }
    if (subEl) {
      subEl.textContent = "Watching clips, overlays, and replies never requires an account. You'll return right back to this page after signing in.";
    }
    if (dismissBtn) dismissBtn.textContent = "Keep Watching";
    if (signInBtn) {
      const next = encodeURIComponent(window.location.pathname + window.location.search + window.location.hash);
      signInBtn.href = apiUrl(`/api/auth/discord/start?next=${next}`);
      signInBtn.classList.remove("hidden");
    }
    if (uriBtn) {
      uriBtn.href = "#";
      uriBtn.classList.add("hidden");
    }
    if (dlBtn) dlBtn.classList.add("hidden");
  } else {
    if (titleEl) titleEl.textContent = "▶ Practice in Replay Takeover";
    if (ctxEl) {
      ctxEl.textContent = contextLabel
        ? `Practice "${contextLabel}" straight inside FBNeo using your own local Fightcade ROMs.`
        : "Practice any shared clip straight inside FBNeo with instant 1-frame retry, slow-mo, and Sandbox Training.";
    }
    if (subEl) {
      subEl.textContent = "Install or pair the free, open-source Windows Desktop Companion alongside Fightcade — it uses your own local ROMs, never modifies live netplay, and keeps full-match recordings on your PC.";
    }
    if (dismissBtn) dismissBtn.textContent = "Keep Studying on Web";
    if (signInBtn) signInBtn.classList.add("hidden");
    if (dlBtn) dlBtn.classList.remove("hidden");
    if (uriBtn) {
      if (takeoverPayload && takeoverPayload.quark_id) {
        uriBtn.href = buildTakeoverUri(takeoverPayload);
        uriBtn.classList.remove("hidden");
      } else {
        uriBtn.href = "#";
        uriBtn.classList.add("hidden");
      }
    }
  }
  modal.classList.remove("hidden");
}

function closeCompanionModal() {
  const modal = document.getElementById("companionModal");
  if (modal) modal.classList.add("hidden");
}

// ---------- EXPLICIT CLOUDFLARE TURNSTILE LIFECYCLE (SPA-SAFE) ----------
const _turnstileWidgets = {};
const _turnstileTokens = {};
const _turnstileTimers = {};

function getTurnstileSiteKey() {
  const capKey = state.capabilities && state.capabilities.turnstile_site_key;
  const bootKey = BOOT && BOOT.turnstileSiteKey;
  return String(capKey || bootKey || "").trim();
}

function getTurnstileToken(surfaceKey) {
  return String(_turnstileTokens[surfaceKey] || "").trim();
}

function removeTurnstileWidget(surfaceKey) {
  if (_turnstileTimers[surfaceKey]) {
    clearTimeout(_turnstileTimers[surfaceKey]);
    delete _turnstileTimers[surfaceKey];
  }
  const wid = _turnstileWidgets[surfaceKey];
  delete _turnstileWidgets[surfaceKey];
  delete _turnstileTokens[surfaceKey];
  if (wid !== undefined && wid !== null && window.turnstile && typeof window.turnstile.remove === "function") {
    try { window.turnstile.remove(wid); } catch (_) {}
  }
}

function resetTurnstileWidget(surfaceKey) {
  delete _turnstileTokens[surfaceKey];
  const wid = _turnstileWidgets[surfaceKey];
  if (wid !== undefined && wid !== null && window.turnstile && typeof window.turnstile.reset === "function") {
    try { window.turnstile.reset(wid); } catch (_) {}
  }
}

function mountTurnstileWidget(surfaceKey, containerId, action, attempt = 0) {
  const el = document.getElementById(containerId);
  const sitekey = getTurnstileSiteKey();
  if (!el) return;
  if (!isHostedMode() || !sitekey) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  removeTurnstileWidget(surfaceKey);
  el.innerHTML = "";

  if (!window.turnstile || typeof window.turnstile.render !== "function") {
    if (attempt < 40) {
      _turnstileTimers[surfaceKey] = setTimeout(() => {
        mountTurnstileWidget(surfaceKey, containerId, action, attempt + 1);
      }, 150);
    }
    return;
  }

  try {
    _turnstileWidgets[surfaceKey] = window.turnstile.render(el, {
      sitekey,
      action,
      theme: isLightTheme() ? "light" : "dark",
      callback: (token) => {
        _turnstileTokens[surfaceKey] = String(token || "");
      },
      "expired-callback": () => {
        delete _turnstileTokens[surfaceKey];
      },
      "error-callback": () => {
        delete _turnstileTokens[surfaceKey];
      },
    });
  } catch (_) {}
}

function openReportModal(targetType, targetId) {
  state.reportTarget = { target_type: targetType, target_id: targetId };
  const modal = document.getElementById("reportModal");
  if (!modal) return;
  const rSel = document.getElementById("reportReasonSelect");
  const dInp = document.getElementById("reportDetailsInput");
  if (rSel) rSel.value = "inappropriate_content";
  if (dInp) dInp.value = "";
  modal.classList.remove("hidden");
  mountTurnstileWidget("report", "reportTurnstileBox", "report");
}

function closeReportModal() {
  state.reportTarget = null;
  removeTurnstileWidget("report");
  const modal = document.getElementById("reportModal");
  if (modal) modal.classList.add("hidden");
}

async function submitContentReport() {
  if (!state.reportTarget) return;
  const reason = (document.getElementById("reportReasonSelect")?.value || "other").trim();
  const details = (document.getElementById("reportDetailsInput")?.value || "").trim();
  const sitekey = getTurnstileSiteKey();
  const tsToken = getTurnstileToken("report");
  if (isHostedMode() && sitekey && !tsToken) {
    toast("Please complete the human verification check first.");
    return;
  }
  const btn = document.getElementById("btnSubmitReport");
  if (btn) btn.disabled = true;
  try {
    const payload = {
      target_type: state.reportTarget.target_type,
      target_id: state.reportTarget.target_id,
      reason,
      details,
    };
    if (tsToken) {
      payload["cf-turnstile-response"] = tsToken;
    }
    await api("/api/reports", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    closeReportModal();
    toast("Report submitted to the moderation queue. Thank you.");
  } catch (err) {
    resetTurnstileWidget("report");
    toast("Report failed: " + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function openTermsModal() {
  const modal = document.getElementById("termsModal");
  if (!modal) return;
  const emailEl = document.getElementById("termsDmcaEmail");
  if (emailEl) {
    emailEl.textContent = DMCA_CONTACT_EMAIL;
    emailEl.href = `mailto:${DMCA_CONTACT_EMAIL}`;
  }
  modal.classList.remove("hidden");
}

function closeTermsModal() {
  const modal = document.getElementById("termsModal");
  if (modal) modal.classList.add("hidden");
}

function closeShareBundleModal() {
  state.shareModal = null;
  const modal = document.getElementById("shareBundleModal");
  if (modal) modal.classList.add("hidden");
}

function _formatShareModalSubtitle(it) {
  if (!it) return "";
  const muPart = formatMatchLabelText(it, { defaultSelfName: state.username, showRank: false });
  const rPart = it.round_label || (it.round_id ? `R${it.round_id}` : "");
  let durPart = "";
  const frameCnt = Number(it.frame_count || 0);
  const sGf = Number(it.start_gf || 0);
  const eGf = Number(it.end_gf || 0);
  if (frameCnt > 1) {
    durPart = `${((frameCnt - 1) / 60).toFixed(1)}s`;
  } else if (eGf > sGf) {
    durPart = `${((eGf - sGf) / 60).toFixed(1)}s`;
  } else if (it.clip_end_s != null && it.clip_start_s != null && Number(it.clip_end_s) > Number(it.clip_start_s)) {
    durPart = `${(Number(it.clip_end_s) - Number(it.clip_start_s)).toFixed(1)}s`;
  }
  return [muPart, rPart, durPart].filter(Boolean).join(" · ");
}

function _syncShareModalInputsToState() {
  if (!state.shareModal) return;
  const capEl = document.getElementById("shareCaptionInput");
  if (capEl) state.shareModal.caption = capEl.value;
  const incEl = document.getElementById("shareIncludeNoteCheck");
  if (incEl) state.shareModal.includeNote = Boolean(incEl.checked);
  const hideEl = document.getElementById("shareHideNamesCheck");
  if (hideEl) state.shareModal.hideNames = Boolean(hideEl.checked);
}

function _propagateCloudShareAcrossState(momentId, patch) {
  if (!momentId || !patch) return;
  const apply = rec => {
    if (!rec) return;
    if ("cloud_moment_id" in patch) rec.cloud_moment_id = patch.cloud_moment_id;
    if ("cloud_share_token" in patch) rec.cloud_share_token = patch.cloud_share_token;
    if ("cloud_share_url" in patch) rec.cloud_share_url = patch.cloud_share_url;
    if ("cloud_visibility" in patch) rec.cloud_visibility = patch.cloud_visibility;
    if ("shared_caption" in patch) rec.shared_caption = patch.shared_caption;
    if ("include_note_on_share" in patch) rec.include_note_on_share = Boolean(patch.include_note_on_share);
    if ("hide_player_names" in patch) rec.hide_player_names = Boolean(patch.hide_player_names);
  };
  if (Array.isArray(state.annotations)) {
    state.annotations.forEach(a => { if (a && a.id === momentId) apply(a); });
  }
  if (Array.isArray(state.selectMarks)) {
    state.selectMarks.forEach(s => { if (s && s.id === momentId) apply(s); });
  }
  if (Array.isArray(state.hubNotes)) {
    state.hubNotes.forEach(n => { if (n && n.id === momentId) apply(n); });
  }
  [state.playlists, state.focusGoals, state.graduatedGoals].forEach(list => {
    if (!Array.isArray(list)) return;
    list.forEach(col => {
      if (col && Array.isArray(col.items)) {
        col.items.forEach(it => { if (it && it.item_id === momentId) apply(it); });
      }
    });
  });
  if (Array.isArray(state.watchAdhocItems)) {
    state.watchAdhocItems.forEach(it => { if (it && it.item_id === momentId) apply(it); });
  }
  if (state.shareModal && state.shareModal.item) {
    const smId = state.shareModal.item.id || state.shareModal.item.item_id;
    if (!smId || smId === momentId) apply(state.shareModal.item);
  }

  if (state.activeView === "studio" && typeof renderNotesList === "function") renderNotesList();
  if (state.activeView === "notes" && state.notesSubView === "notebook" && typeof renderHubNotes === "function") renderHubNotes();
  if (state.watchPlaylistId && typeof renderWatchSidePanel === "function") renderWatchSidePanel();
}

function _triggerDirectDownload(url, filename = "clip.mp4") {
  const safe = safeHref(url);
  if (!safe || safe === "#") return;
  const a = document.createElement("a");
  a.href = safe;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function renderShareModal() {
  const modal = document.getElementById("shareBundleModal");
  const titleEl = document.getElementById("shareBundleTitle");
  const body = document.getElementById("shareBundleBody");
  const sm = state.shareModal;
  if (!modal || !body || !sm) return;
  if (titleEl) titleEl.textContent = "Share Moment";

  const it = sm.item || {};
  const subtitle = _formatShareModalSubtitle(it);
  const connected = Boolean(state.capabilities && state.capabilities.has_cloud_token);
  const hasLiveLink = Boolean(it.cloud_visibility === "unlisted" && it.cloud_share_url);
  const busy = Boolean(sm.busy);
  const dlBusy = Boolean(sm.downloading);
  const errHtml = sm.error ? `<div class="share-error-box">${esc(sm.error)}</div>` : "";
  const dlLabel = dlBusy ? `<span class="btn-spin" aria-hidden="true"></span>Preparing MP4...` : "Download MP4 locally";

  if (!connected) {
    // State C — Cloud Account Not Connected Yet
    body.innerHTML = `
      <div class="share-modal-stack">
        ${subtitle ? `<div class="share-subtitle">${esc(subtitle)}</div>` : ""}
        <p style="margin:0; font-size:13.5px; color:var(--text-body); line-height:1.5;">
          Connect your Fightcade Lab (Discord) account once to publish instant shareable links with toggleable hitboxes &amp; inputs.
        </p>
        ${errHtml}
        <div class="row" style="gap:8px; flex-wrap:wrap;">
          <button id="btnShareConnectAccount" type="button" class="primary sm" onclick="connectCloudAccountFromShareModal()" ${busy ? "disabled" : ""}>
            ${busy ? `<span class="btn-spin" aria-hidden="true"></span>Opening...` : "Connect Account →"}
          </button>
          <button type="button" class="sm" onclick="refreshShareModalConnectionStatus(false)" ${busy ? "disabled" : ""}>Refresh Status</button>
        </div>
        <div class="share-footer-row">
          <span>Prefer an offline file?</span>
          <button type="button" class="sm" onclick="downloadShareMomentMp4()" ${dlBusy ? "disabled" : ""}>${dlLabel}</button>
        </div>
      </div>
    `;
    modal.classList.remove("hidden");
    return;
  }

  const controlsHtml = `
    <label class="field-lbl">Caption / Question
      <input id="shareCaptionInput" class="input" value="${esc(sm.caption || "")}" placeholder="Add a question or caption for viewers (optional)" oninput="if(state.shareModal) state.shareModal.caption = this.value" ${busy ? "disabled" : ""} />
    </label>
    <div class="share-check-list">
      <label class="share-check">
        <input id="shareIncludeNoteCheck" type="checkbox" ${sm.includeNote ? "checked" : ""} onchange="if(state.shareModal) state.shareModal.includeNote = this.checked" ${busy ? "disabled" : ""} />
        <span>Include my personal study note</span>
      </label>
      <label class="share-check">
        <input id="shareHideNamesCheck" type="checkbox" ${sm.hideNames ? "checked" : ""} onchange="if(state.shareModal) state.shareModal.hideNames = this.checked" ${busy ? "disabled" : ""} />
        <span>Hide player names (show P1 vs P2)</span>
      </label>
    </div>
  `;

  if (hasLiveLink) {
    // State B — Live Unlisted Link
    const shareUrl = String(it.cloud_share_url || "");
    body.innerHTML = `
      <div class="share-modal-stack">
        ${subtitle ? `<div class="share-subtitle">${esc(subtitle)}</div>` : ""}
        <div class="share-url-row">
          <input id="shareUrlInput" class="input share-url-input" readonly value="${esc(shareUrl)}" onclick="this.select()" />
          <button type="button" class="sm primary" onclick="copyActiveShareLink()">Copy Link</button>
          <a class="btn-link sm" href="${esc(safeHref(shareUrl))}" target="_blank" rel="noopener">↗ Open</a>
        </div>
        ${controlsHtml}
        ${errHtml}
        <div class="share-lifecycle-row">
          <div class="row" style="gap:6px;">
            <button type="button" class="sm" data-tip="Mint a new link and invalidate the previous URL" onclick="rotateActiveShareLink()" ${busy ? "disabled" : ""}>
              ${busy && sm.busyAction === "rotate" ? `<span class="btn-spin" aria-hidden="true"></span>Rotating...` : "↻ Rotate Link"}
            </button>
            <button type="button" class="sm danger" data-tip="Make this moment private immediately" onclick="revokeActiveShareLink()" ${busy ? "disabled" : ""}>
              ${busy && sm.busyAction === "revoke" ? `<span class="btn-spin" aria-hidden="true"></span>Revoking...` : "🔒 Revoke Link"}
            </button>
          </div>
          <button id="btnSaveShareSettings" type="button" class="sm emerald" onclick="publishActiveShareMoment(true)" ${busy ? "disabled" : ""}>
            ${busy && sm.busyAction === "save" ? `<span class="btn-spin" aria-hidden="true"></span>Saving...` : "Save Settings"}
          </button>
        </div>
        <div class="share-footer-row">
          <span>Unlisted link · Anyone with the link can view</span>
          <button type="button" class="link-btn" onclick="downloadShareMomentMp4()" ${dlBusy ? "disabled" : ""}>${dlLabel}</button>
        </div>
      </div>
    `;
    modal.classList.remove("hidden");
    return;
  }

  // State A — Ready to Share
  body.innerHTML = `
    <div class="share-modal-stack">
      ${subtitle ? `<div class="share-subtitle">${esc(subtitle)}</div>` : ""}
      ${controlsHtml}
      ${errHtml}
      <div class="row" style="justify-content:flex-end; gap:8px;">
        <button type="button" class="sm" onclick="closeShareBundleModal()" ${busy ? "disabled" : ""}>Cancel</button>
        <button id="btnCreateShareLink" type="button" class="sm primary" onclick="publishActiveShareMoment(false)" ${busy ? "disabled" : ""}>
          ${busy ? `<span class="btn-spin" aria-hidden="true"></span>Publishing...` : "Create Share Link"}
        </button>
      </div>
      <div class="share-footer-row">
        <span>Private until you create a link</span>
        <button type="button" class="link-btn" onclick="downloadShareMomentMp4()" ${dlBusy ? "disabled" : ""}>${dlLabel}</button>
      </div>
    </div>
  `;
  modal.classList.remove("hidden");
}

function shareMomentBundle(itemPayload) {
  if (!itemPayload) return null;
  const isResp = Boolean(
    itemPayload.is_response_clip ||
    itemPayload.source === "takeover_response" ||
    itemPayload.response_clip_url
  );
  if (!isResp && (!String(itemPayload.quark_id || "").trim() || !Number(itemPayload.round_id || 0))) {
    toast("Missing match/round coordinates for this moment.");
    return null;
  }
  const copy = {
    ...itemPayload,
    is_response_clip: isResp,
    id: itemPayload.id || itemPayload.item_id || "",
  };
  state.shareModal = {
    item: copy,
    caption: String(copy.shared_caption || ""),
    includeNote: Boolean(copy.include_note_on_share),
    hideNames: Boolean(copy.hide_player_names),
    busy: false,
    busyAction: "",
    downloading: false,
    error: "",
  };
  renderShareModal();
  if (!isHostedMode()) {
    loadCapabilities().then(() => {
      if (state.shareModal) renderShareModal();
    }).catch(() => {});
  }
  return state.shareModal;
}

function copyActiveShareLink() {
  const url = state.shareModal && state.shareModal.item && state.shareModal.item.cloud_share_url;
  if (!url) return;
  copyText(url, "Share link copied!");
}

async function installExternalToolFromUi(action, btnEl = null) {
  const isFfmpeg = action === "install_ffmpeg";
  const label = isFfmpeg ? "FFmpeg" : "Fast Capture Engine";
  const origHtml = btnEl ? btnEl.innerHTML : "";
  if (btnEl) {
    if (btnEl.disabled) return;
    btnEl.disabled = true;
    btnEl.innerHTML = `<span class="btn-spin"></span> Downloading ${esc(label)}...`;
  }
  try {
    await api("/api/onboarding", {
      method: "POST",
      body: JSON.stringify({ action, consent: true }),
    });
    if (typeof loadCapabilities === "function") {
      await loadCapabilities();
    }
    if (state.activeView === "home" && typeof renderHomeWorkbench === "function") {
      renderHomeWorkbench();
    }
    if (typeof isStatusPopoverOpen === "function" && isStatusPopoverOpen() && typeof renderStatusPopover === "function") {
      renderStatusPopover();
    }
    if (typeof updateStatusPillUi === "function") {
      updateStatusPillUi();
    }
    toast(`${label} installed and verified!`);
  } catch (err) {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.innerHTML = origHtml;
    }
    toast(`Could not download ${label}: ${err.message}`);
  }
}

async function pairLocalCompanionWithBrowser() {
  try {
    const res = await api("/api/companion/pair-start", { method: "POST", body: JSON.stringify({}) });
    if (res.pair_url) {
      window.open(res.pair_url, "_blank", "noopener");
      toast("Sign in on the opened tab, then return here.");
    }
    return res;
  } catch (err) {
    toast("Could not start account pairing: " + err.message);
    return null;
  }
}

async function connectCloudAccountFromShareModal() {
  if (!state.shareModal) return;
  state.shareModal.busy = true;
  state.shareModal.error = "";
  renderShareModal();
  try {
    const res = await api("/api/companion/pair-start", { method: "POST", body: JSON.stringify({}) });
    if (res.pair_url) {
      window.open(res.pair_url, "_blank", "noopener");
      toast("Sign in on the opened tab, then return here.");
    }
  } catch (err) {
    if (state.shareModal) state.shareModal.error = err.message;
    toast("Could not start account pairing: " + err.message);
  } finally {
    if (state.shareModal) {
      state.shareModal.busy = false;
      renderShareModal();
    }
  }
}

async function refreshShareModalConnectionStatus(silent = false) {
  try {
    await loadCapabilities();
    if (state.shareModal) {
      state.shareModal.error = "";
      renderShareModal();
    }
    if (!silent) {
      toast(
        state.capabilities.has_cloud_token
          ? "Account connected! Ready to create share links."
          : "Not connected yet — finish signing in on the opened browser tab."
      );
    }
  } catch (err) {
    if (!silent) toast("Status refresh failed: " + err.message);
  }
}

async function _postShareJson(path, bodyObj) {
  const res = await fetch(apiUrl(path), {
    credentials: "same-origin",
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(bodyObj),
  });
  const data = await res.json().catch(() => ({ ok: false, error: "Invalid server response" }));
  if (!res.ok || data.ok === false) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = data.code || "";
    throw err;
  }
  return data;
}

async function publishActiveShareMoment(isSettingsSave = false) {
  const sm = state.shareModal;
  if (!sm || sm.busy) return null;
  _syncShareModalInputsToState();
  const it = sm.item || {};
  const isResp = Boolean(it.is_response_clip || it.source === "takeover_response" || it.response_clip_url);
  const anchorGf = Number(it.anchor_gf || it.press_gf || it.start_gf || 0);
  const startGf = (it.start_gf && Number(it.start_gf) > 0) ? Number(it.start_gf) : Math.max(1, anchorGf - 180);
  const endGf = (it.end_gf && Number(it.end_gf) > startGf)
    ? Number(it.end_gf)
    : (anchorGf > 0 ? anchorGf + 120 : startGf + 180);

  sm.busy = true;
  sm.busyAction = isSettingsSave ? "save" : "publish";
  sm.error = "";
  renderShareModal();

  try {
    const res = await _postShareJson("/api/share/publish", {
      moment_id: it.id || it.item_id || "",
      source: it.source || (isResp ? "takeover_response" : "annotation"),
      is_response_clip: isResp,
      response_clip_url: it.response_clip_url || "",
      response_overlay_url: it.response_overlay_url || "",
      cloud_moment_id: it.cloud_moment_id || "",
      quark_id: it.quark_id || "",
      round_id: Number(it.round_id || 1),
      perspective: Number(it.perspective || 1),
      start_gf: startGf,
      end_gf: endGf,
      anchor_gf: anchorGf || startGf,
      kind: it.kind || "note",
      title: String(it.title || it.label || "").slice(0, 160),
      note: String(it.note || "").slice(0, 2000),
      tags: Array.isArray(it.tags) ? it.tags : [],
      shared_caption: String(sm.caption || "").trim(),
      include_note_on_share: Boolean(sm.includeNote),
      hide_player_names: Boolean(sm.hideNames),
    });

    const patch = {
      cloud_moment_id: res.cloud_moment_id,
      cloud_share_token: res.cloud_share_token,
      cloud_share_url: res.cloud_share_url,
      cloud_visibility: res.cloud_visibility || "unlisted",
      shared_caption: res.shared_caption ?? sm.caption,
      include_note_on_share: Boolean(res.include_note_on_share),
      hide_player_names: Boolean(res.hide_player_names),
    };
    const mid = res.local_moment_id || it.id || it.item_id || "";
    if (state.shareModal && state.shareModal.item) {
      Object.assign(state.shareModal.item, patch);
    }
    if (mid) _propagateCloudShareAcrossState(mid, patch);

    if (state.shareModal) {
      state.shareModal.busy = false;
      state.shareModal.busyAction = "";
      renderShareModal();
    }
    if (isSettingsSave) {
      toast("Share settings saved");
    } else if (res.cloud_share_url) {
      copyText(res.cloud_share_url, "Share link created & copied!");
    } else {
      toast("Share link created!");
    }
    return res;
  } catch (err) {
    if (err.status === 401 || err.code === "cloud_not_connected") {
      state.capabilities.has_cloud_token = false;
    }
    if (state.shareModal) {
      state.shareModal.busy = false;
      state.shareModal.busyAction = "";
      state.shareModal.error = err.message;
      renderShareModal();
    }
    toast("Share failed: " + err.message);
    return null;
  }
}

async function rotateActiveShareLink() {
  const sm = state.shareModal;
  if (!sm || sm.busy) return null;
  const it = sm.item || {};
  sm.busy = true;
  sm.busyAction = "rotate";
  sm.error = "";
  renderShareModal();
  try {
    const res = await _postShareJson("/api/share/rotate", {
      moment_id: it.id || it.item_id || "",
      source: it.source || "",
      cloud_moment_id: it.cloud_moment_id || "",
    });
    const patch = {
      cloud_moment_id: res.cloud_moment_id,
      cloud_share_token: res.cloud_share_token,
      cloud_share_url: res.cloud_share_url,
      cloud_visibility: res.cloud_visibility || "unlisted",
    };
    const mid = res.local_moment_id || it.id || it.item_id || "";
    if (state.shareModal && state.shareModal.item) Object.assign(state.shareModal.item, patch);
    if (mid) _propagateCloudShareAcrossState(mid, patch);
    if (state.shareModal) {
      state.shareModal.busy = false;
      state.shareModal.busyAction = "";
      renderShareModal();
    }
    if (res.cloud_share_url) copyText(res.cloud_share_url, "New share link copied (old link invalidated)");
    else toast("Share link rotated");
    return res;
  } catch (err) {
    if (err.status === 401 || err.code === "cloud_not_connected") {
      state.capabilities.has_cloud_token = false;
    }
    if (state.shareModal) {
      state.shareModal.busy = false;
      state.shareModal.busyAction = "";
      state.shareModal.error = err.message;
      renderShareModal();
    }
    toast("Rotate failed: " + err.message);
    return null;
  }
}

async function revokeActiveShareLink() {
  const sm = state.shareModal;
  if (!sm || sm.busy) return null;
  const it = sm.item || {};
  sm.busy = true;
  sm.busyAction = "revoke";
  sm.error = "";
  renderShareModal();
  try {
    const res = await _postShareJson("/api/share/revoke", {
      moment_id: it.id || it.item_id || "",
      source: it.source || "",
      cloud_moment_id: it.cloud_moment_id || "",
    });
    const patch = {
      cloud_moment_id: res.cloud_moment_id,
      cloud_share_token: null,
      cloud_share_url: null,
      cloud_visibility: "private",
    };
    const mid = res.local_moment_id || it.id || it.item_id || "";
    if (state.shareModal && state.shareModal.item) Object.assign(state.shareModal.item, patch);
    if (mid) _propagateCloudShareAcrossState(mid, patch);
    if (state.shareModal) {
      state.shareModal.busy = false;
      state.shareModal.busyAction = "";
      renderShareModal();
    }
    toast("Share link revoked — moment is now private");
    return res;
  } catch (err) {
    if (err.status === 401 || err.code === "cloud_not_connected") {
      state.capabilities.has_cloud_token = false;
    }
    if (state.shareModal) {
      state.shareModal.busy = false;
      state.shareModal.busyAction = "";
      state.shareModal.error = err.message;
      renderShareModal();
    }
    toast("Revoke failed: " + err.message);
    return null;
  }
}

async function downloadShareMomentMp4() {
  const sm = state.shareModal;
  if (!sm || sm.downloading) return null;
  const it = sm.item || {};
  const isResp = Boolean(it.is_response_clip || it.source === "takeover_response" || it.response_clip_url);
  const respClipUrl = it.response_clip_url || (isResp ? it.video_url : "") || "";
  if (isResp && respClipUrl) {
    _triggerDirectDownload(respClipUrl, `${it.id || "response"}_clip.mp4`);
    toast("Downloading MP4...");
    return { ok: true, clip_url: respClipUrl };
  }

  const qid = String(it.quark_id || "").trim();
  const roundId = Number(it.round_id || 0);
  if (!qid || !roundId) {
    toast("Missing match/round coordinates for local MP4 export.");
    return null;
  }
  const anchorGf = Number(it.anchor_gf || it.press_gf || it.start_gf || 0);
  const startGf = (it.start_gf && Number(it.start_gf) > 0) ? Number(it.start_gf) : Math.max(1, anchorGf - 180);
  const endGf = (it.end_gf && Number(it.end_gf) > startGf)
    ? Number(it.end_gf)
    : (anchorGf > 0 ? anchorGf + 120 : startGf + 180);

  sm.downloading = true;
  sm.error = "";
  renderShareModal();
  try {
    const res = await api("/api/share/prepare", {
      method: "POST",
      body: JSON.stringify({
        quark_id: qid,
        round_id: roundId,
        start_gf: startGf,
        end_gf: endGf,
        anchor_gf: anchorGf || startGf,
        perspective: Number(it.perspective || 1),
        title: String(it.title || it.label || "").slice(0, 140),
        note: String(it.note || "").slice(0, 600),
      }),
    });
    const clipUrl = res && res.artifacts && res.artifacts.clip && res.artifacts.clip.url;
    if (clipUrl) {
      _triggerDirectDownload(clipUrl, `${qid}_r${roundId}_gf${startGf}.mp4`);
      toast("MP4 ready — downloading...");
    } else {
      toast("Local MP4 export completed.");
    }
    return res;
  } catch (err) {
    if (state.shareModal) state.shareModal.error = err.message;
    toast("Local MP4 export failed: " + err.message);
    return null;
  } finally {
    if (state.shareModal) {
      state.shareModal.downloading = false;
      renderShareModal();
    }
  }
}

async function prepareLocalShareBundle(payload) {
  return shareMomentBundle(payload);
}

// ---------- MOBILE REPLAY SYNC (CLOUDFLARE R2 BYOC) MODAL ----------
function closeByocModal() {
  state.byocModal = null;
  const modal = document.getElementById("byocModal");
  if (modal) modal.classList.add("hidden");
}

function toggleByocReadTokenDrawer() {
  if (!state.byocModal) return;
  _syncByocModalInputsToState();
  state.byocModal.showReadToken = !state.byocModal.showReadToken;
  renderByocModal();
}

function _syncByocModalInputsToState() {
  const bm = state.byocModal;
  if (!bm) return;
  const getVal = id => {
    const el = document.getElementById(id);
    return el ? el.value : null;
  };
  const acct = getVal("byocAccountId");
  if (acct !== null) bm.accountId = acct;
  const bucket = getVal("byocBucketName");
  if (bucket !== null) bm.bucketName = bucket;
  const ak = getVal("byocAccessKeyId");
  if (ak !== null) bm.accessKeyId = ak;
  const sk = getVal("byocSecretKey");
  if (sk !== null) bm.secretAccessKey = sk;
  const rak = getVal("byocReadAccessKeyId");
  if (rak !== null) bm.readAccessKeyId = rak;
  const rsk = getVal("byocReadSecretKey");
  if (rsk !== null) bm.readSecretAccessKey = rsk;
  const cap = getVal("byocCapGb");
  if (cap !== null) bm.capGb = cap;
  const autoEl = document.getElementById("byocAutoSync");
  if (autoEl) bm.autoSync = Boolean(autoEl.checked);
}

function renderByocModal() {
  const modal = document.getElementById("byocModal");
  const body = document.getElementById("byocModalBody");
  const bm = state.byocModal;
  if (!modal || !body || !bm) return;

  const caps = state.capabilities || {};
  const byoc = bm.status || caps.byoc || {};
  const hasCloud = Boolean(caps.has_cloud_token);
  const busy = Boolean(bm.busy);
  const errHtml = bm.error ? `<div class="share-error-box">${esc(bm.error)}</div>` : "";
  const footerLinkHtml = `
    <div class="share-footer-row">
      <button type="button" class="link-btn" onclick="closeByocModal(); openAboutExplainer();">Why bring your own R2? How Fightcade Lab stays free →</button>
    </div>
  `;

  if (byoc.connected) {
    const usedGb = (Number(byoc.used_bytes || 0) / 1e9).toFixed(2);
    const capGb = Number(byoc.cap_gb || 9.0).toFixed(1);
    const cnt = Number(byoc.synced_count || 0);
    const syncLabel = byoc.syncing
      ? `<span class="btn-spin" aria-hidden="true"></span>Syncing...`
      : "Sync Recent Sets";
    body.innerHTML = `
      <div class="share-modal-stack">
        <div class="byoc-usage-box">
          <div class="row-between" style="margin-bottom:4px;">
            <strong style="font-size:13.5px;">● ${esc(byoc.bucket_name || "R2 Bucket")}</strong>
            <span class="pill ready mono">${usedGb} / ${capGb} GB</span>
          </div>
          <div style="font-size:12px; color:var(--muted);">
            ${cnt} set${cnt === 1 ? "" : "s"} synced · Account ${esc(byoc.account_id_masked || "")}${byoc.has_separate_read_token ? " · Read-Only cloud token" : ""}
          </div>
          ${byoc.last_error ? `<div style="font-size:12px; color:var(--rose-fg); margin-top:4px;">Last sync error: ${esc(byoc.last_error)}</div>` : ""}
        </div>

        <label class="share-check">
          <input id="byocAutoSync" type="checkbox" ${bm.autoSync ? "checked" : ""} onchange="saveByocSettingsFromModal()" ${busy ? "disabled" : ""} />
          <span>Auto-sync finished matches to R2</span>
        </label>

        <div class="row" style="gap:8px; align-items:flex-end;">
          <label class="field-lbl" style="flex:1; margin:0;">Rolling Cap (GB)
            <input id="byocCapGb" type="number" step="0.5" min="0.5" max="500" class="input mono" value="${esc(bm.capGb ?? capGb)}" ${busy ? "disabled" : ""} />
          </label>
          <button type="button" class="sm" onclick="saveByocSettingsFromModal()" ${busy ? "disabled" : ""}>Save Cap</button>
        </div>

        ${errHtml}

        <div class="share-lifecycle-row">
          <button type="button" class="sm danger" onclick="disconnectByocFromModal()" ${busy ? "disabled" : ""}>Disconnect</button>
          <button type="button" class="sm primary" onclick="triggerByocSyncNow()" ${busy || byoc.syncing ? "disabled" : ""}>${syncLabel}</button>
        </div>
        ${footerLinkHtml}
      </div>
    `;
    modal.classList.remove("hidden");
    return;
  }

  const cloudBannerHtml = !hasCloud
    ? `
      <div class="pop-warning" style="margin-top:0;">
        <div class="row-between" style="gap:8px;">
          <span>Connect your Fightcade Lab cloud account first so your phone can sign in.</span>
          <button type="button" class="sm primary" onclick="pairLocalCompanionWithBrowser()">Connect Account →</button>
        </div>
      </div>
    `
    : "";

  const readDrawerHtml = bm.showReadToken
    ? `
      <div class="pop-storage-edit" style="display:flex; flex-direction:column; gap:8px;">
        <label class="field-lbl" style="margin:0;">Read Access Key ID
          <input id="byocReadAccessKeyId" class="input mono" value="${esc(bm.readAccessKeyId || "")}" placeholder="Optional Read-Only token Access Key ID" autocomplete="off" ${busy ? "disabled" : ""} />
        </label>
        <label class="field-lbl" style="margin:0;">Read Secret Access Key
          <input id="byocReadSecretKey" type="password" class="input mono" value="${esc(bm.readSecretAccessKey || "")}" placeholder="Optional Read-Only token Secret Key" autocomplete="off" ${busy ? "disabled" : ""} />
        </label>
      </div>
    `
    : "";

  body.innerHTML = `
    <div class="share-modal-stack">
      <ol class="byoc-steps">
        <li>Create a free R2 bucket in Cloudflare (10 GB free, $0 video streaming).</li>
        <li>Create an R2 API token scoped to that bucket.</li>
        <li>Paste your credentials below. Fightcade Lab configures CORS automatically and caps sync at 9.0 GB so it stays free.</li>
      </ol>
      ${cloudBannerHtml}
      <div class="form-stack">
        <label class="field-lbl">Account ID
          <input id="byocAccountId" class="input mono" value="${esc(bm.accountId || "")}" placeholder="32-character Cloudflare Account ID" autocomplete="off" ${busy ? "disabled" : ""} />
        </label>
        <label class="field-lbl">Bucket Name
          <input id="byocBucketName" class="input mono" value="${esc(bm.bucketName || "")}" placeholder="e.g. fightcade-replays" autocomplete="off" ${busy ? "disabled" : ""} />
        </label>
        <label class="field-lbl">Access Key ID
          <input id="byocAccessKeyId" class="input mono" value="${esc(bm.accessKeyId || "")}" placeholder="R2 API Token Access Key ID" autocomplete="off" ${busy ? "disabled" : ""} />
        </label>
        <label class="field-lbl">Secret Access Key
          <input id="byocSecretKey" type="password" class="input mono" value="${esc(bm.secretAccessKey || "")}" placeholder="R2 API Token Secret Access Key" autocomplete="off" ${busy ? "disabled" : ""} />
        </label>
        <div>
          <button type="button" class="pop-disclosure-btn" onclick="toggleByocReadTokenDrawer()">
            <span>${bm.showReadToken ? "▾" : "▸"} Optional: Use a separate Read-Only token for fightcadelab.com</span>
          </button>
          ${readDrawerHtml}
        </div>
        <div class="row" style="gap:12px; align-items:center; justify-content:space-between;">
          <label class="field-lbl" style="width:130px; margin:0;">Rolling Cap (GB)
            <input id="byocCapGb" type="number" step="0.5" min="0.5" max="500" class="input mono" value="${esc(bm.capGb ?? "9.0")}" ${busy ? "disabled" : ""} />
          </label>
          <label class="share-check" style="margin-top:16px;">
            <input id="byocAutoSync" type="checkbox" ${bm.autoSync !== false ? "checked" : ""} ${busy ? "disabled" : ""} />
            <span>Auto-sync finished matches</span>
          </label>
        </div>
      </div>
      ${errHtml}
      <div class="row" style="justify-content:flex-end; gap:8px;">
        <button type="button" class="sm" onclick="closeByocModal()" ${busy ? "disabled" : ""}>Cancel</button>
        <button id="btnByocConnect" type="button" class="sm primary" onclick="submitByocConnect()" ${busy || !hasCloud ? "disabled" : ""}>
          ${busy ? `<span class="btn-spin" aria-hidden="true"></span>Testing &amp; Connecting...` : "Test &amp; Connect"}
        </button>
      </div>
      ${footerLinkHtml}
    </div>
  `;
  modal.classList.remove("hidden");
}

async function openByocModal() {
  const curByoc = (state.capabilities && state.capabilities.byoc) || {};
  state.byocModal = {
    status: curByoc,
    accountId: "",
    bucketName: curByoc.bucket_name || "",
    accessKeyId: "",
    secretAccessKey: "",
    readAccessKeyId: "",
    readSecretAccessKey: "",
    showReadToken: false,
    capGb: String(curByoc.cap_gb ?? 9.0),
    autoSync: curByoc.auto_sync !== false,
    busy: false,
    error: "",
  };
  renderByocModal();
  if (!isHostedMode()) {
    try {
      await loadCapabilities();
      const st = await api("/api/byoc/status");
      if (state.capabilities) state.capabilities.byoc = st;
      if (state.byocModal) {
        state.byocModal.status = st;
        state.byocModal.capGb = String(st.cap_gb ?? 9.0);
        state.byocModal.autoSync = st.auto_sync !== false;
        renderByocModal();
      }
    } catch (_) {}
  }
}

async function submitByocConnect() {
  const bm = state.byocModal;
  if (!bm || bm.busy) return;
  _syncByocModalInputsToState();
  bm.busy = true;
  bm.error = "";
  renderByocModal();
  try {
    const payload = {
      account_id: String(bm.accountId || "").trim(),
      bucket_name: String(bm.bucketName || "").trim(),
      access_key_id: String(bm.accessKeyId || "").trim(),
      secret_access_key: String(bm.secretAccessKey || "").trim(),
      cap_gb: Number(bm.capGb || 9.0),
      auto_sync: Boolean(bm.autoSync),
    };
    if (bm.readAccessKeyId || bm.readSecretAccessKey) {
      payload.read_access_key_id = String(bm.readAccessKeyId || "").trim();
      payload.read_secret_access_key = String(bm.readSecretAccessKey || "").trim();
    }
    const res = await api("/api/byoc/connect", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (state.capabilities) state.capabilities.byoc = res;
    if (state.byocModal) {
      state.byocModal.status = res;
      state.byocModal.busy = false;
      state.byocModal.secretAccessKey = "";
      state.byocModal.readSecretAccessKey = "";
      renderByocModal();
    }
    if (typeof renderStatusPopover === "function") renderStatusPopover();
    if (state.activeView === "matches" && typeof renderMatchList === "function") renderMatchList();
    toast(`Connected to R2 bucket "${res.bucket_name}"! CORS configured automatically.`);
  } catch (err) {
    if (state.byocModal) {
      state.byocModal.busy = false;
      state.byocModal.error = err.message;
      renderByocModal();
    }
    toast("R2 connection failed: " + err.message);
  }
}

async function saveByocSettingsFromModal() {
  const bm = state.byocModal;
  if (!bm || bm.busy) return;
  _syncByocModalInputsToState();
  bm.busy = true;
  bm.error = "";
  renderByocModal();
  try {
    const res = await api("/api/byoc/settings", {
      method: "PATCH",
      body: JSON.stringify({
        auto_sync: Boolean(bm.autoSync),
        cap_gb: Number(bm.capGb || 9.0),
      }),
    });
    if (state.capabilities) state.capabilities.byoc = res;
    if (state.byocModal) {
      state.byocModal.status = res;
      state.byocModal.busy = false;
      renderByocModal();
    }
    if (typeof renderStatusPopover === "function") renderStatusPopover();
    toast("Mobile Replay Sync settings saved");
  } catch (err) {
    if (state.byocModal) {
      state.byocModal.busy = false;
      state.byocModal.error = err.message;
      renderByocModal();
    }
    toast("Could not save settings: " + err.message);
  }
}

async function triggerByocSyncNow(quarkId = null, btnEl = null) {
  if (btnEl) {
    btnEl.disabled = true;
    btnEl.textContent = "Syncing...";
  }
  try {
    const body = quarkId ? { quark_id: quarkId } : { limit: 5 };
    const res = await api("/api/byoc/sync", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (res.status && state.capabilities) {
      state.capabilities.byoc = res.status;
    }
    if (state.byocModal && res.status) {
      state.byocModal.status = res.status;
      renderByocModal();
    }
    toast(quarkId ? "Syncing match to your R2 bucket..." : "Syncing recent matches to your R2 bucket...");
    setTimeout(async () => {
      try {
        const st = await api("/api/byoc/status");
        if (state.capabilities) state.capabilities.byoc = st;
        if (state.byocModal) { state.byocModal.status = st; renderByocModal(); }
        if (typeof renderStatusPopover === "function") renderStatusPopover();
        if (state.activeView === "matches" && typeof renderMatchList === "function") renderMatchList();
      } catch (_) {}
    }, 3000);
  } catch (err) {
    toast("Sync failed: " + err.message);
  } finally {
    if (btnEl) {
      btnEl.disabled = false;
      btnEl.textContent = "☁ Sync";
    }
  }
}

async function disconnectByocFromModal() {
  if (!confirm("Disconnect Mobile Replay Sync? Your local PC replays and cloud notes will not be deleted.")) return;
  const bm = state.byocModal;
  if (bm) { bm.busy = true; bm.error = ""; renderByocModal(); }
  try {
    const res = await api("/api/byoc/config", { method: "DELETE" });
    if (state.capabilities) state.capabilities.byoc = res;
    if (state.byocModal) {
      state.byocModal.status = res;
      state.byocModal.busy = false;
      renderByocModal();
    }
    if (typeof renderStatusPopover === "function") renderStatusPopover();
    if (state.activeView === "matches" && typeof renderMatchList === "function") renderMatchList();
    toast("Mobile Replay Sync disconnected");
  } catch (err) {
    if (state.byocModal) {
      state.byocModal.busy = false;
      state.byocModal.error = err.message;
      renderByocModal();
    }
    toast("Disconnect failed: " + err.message);
  }
}

// ---------- THEME (LIGHT / DARK) ----------
function isLightTheme() {
  return document.documentElement.getAttribute("data-theme") === "light";
}

function syncThemeUi() {
  const light = isLightTheme();
  const btn = document.getElementById("btnThemeToggle");
  if (btn) {
    btn.textContent = light ? "🌙" : "☀️";
    btn.title = light ? "Switch to dark mode" : "Switch to light mode";
  }
  const meta = document.getElementById("metaThemeColor");
  if (meta) meta.setAttribute("content", light ? "#f1f5f9" : "#0b0f19");
}

function toggleTheme() {
  const next = isLightTheme() ? "dark" : "light";
  if (next === "light") {
    document.documentElement.setAttribute("data-theme", "light");
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  try { localStorage.setItem("fc_theme", next); } catch (_) {}
  syncThemeUi();
  if (typeof curRound === "function" && curRound()) {
    updateHudAndTimeline();
  }
}
