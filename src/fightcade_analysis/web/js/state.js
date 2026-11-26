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
  "Anti-Air", "Whiff Punish", "Throw / Tech", "Wakeup / Oki",
  "Corner", "Parry", "Meter / Super", "Footsies", "Combo / Confirm", "Good Decision",
];
const DEFAULT_USER = BOOT.defaultUsername || "";

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
  select_mark: "Select Mark",
  set_takeaway: "Set Takeaway",
  pro_clip: "Pro Clip",
};

const state = {
  apiBase: API_BASE,
  companionBase: COMPANION_BASE,
  username: localStorage.getItem("fc_mobile_user") || DEFAULT_USER,
  auth: {
    authenticated: false,
    user: null,
    quota: null,
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
    disk_cache: { total_bytes: 0, total_gb: 0, match_count: 0, protected_count: 0, cap_gb: 5.0 },
    environment: null,
  },
  watcher: { phase: "idle", message: "Watching Fightcade", running: false },
  jobs: [],

  // Navigation
  activeView: "home", // home | matches | notes | insights | studio | report | shared_moment | shared_playlist
  notesSubView: "notebook", // notebook | playlists
  returnView: "home",

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
  rangeStartGf: null,
  rangeEndGf: null,
  editingId: null,
  editingSource: "annotation", // annotation | select_mark
  selectedKind: "mistake",
  selectedTags: new Set(),
  selectedGoalIds: new Set(),
  selectedPlaylistId: "",
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
  notesGoalFilter: "",
  notesTag: "",
  notesDrillOnly: false,
  notesNeedsNote: false,
  notesSearch: "",
  notesGroupBy: "recent",
  previewNoteId: null,
  previewRate: 1,
  previewRaf: null,
  hubEditId: null,
  hubEditKind: "mistake",
  hubEditTags: new Set(),

  // Insights
  insightsData: null,
  includeLegacyInsights: false,
  insightsFilter: "all",
  insightsSearch: "",

  // Watch & Train Modes
  watchPlaylistId: null,
  watchIndex: 0,
  watchRate: 1,
  watchAutoAdvance: false,
  watchRaf: null,
  watchOverlay: null,

  trainPlaylistId: null,
  trainIndex: 0,
  trainSpeed: 1.0,
  trainSpeedScope: "all",
  trainAutoLoop: true,

  // Shared Moment / Shared Playlist Views (/m/:id, /p/:id)
  sharedMoment: null,
  sharedReplies: [],
  sharedPlaylist: null,
  sharedPlaylistIndex: 0,
  sharedPlaylistAutoAdvance: false,
  sharedPerspective: 1,
  sharedRate: 1,
  sharedOverlay: null,
  sharedRaf: null,

  // Report & Share Bundle Modals
  reportTarget: null,
  addToPlPayload: null,
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

function toast(msg) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(window._toastT);
  window._toastT = setTimeout(() => el.classList.remove("show"), 2800);
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
  return `fightcadelab://takeover?${qs.toString()}`;
}

async function probeLocalCompanion() {
  if (!isHostedMode()) return;
  try {
    const res = await companionApi("/api/companion/ping");
    state.companionBridge = {
      connected: Boolean(res.ok && res.token_valid && res.can_takeover),
      reachable: Boolean(res.ok),
      install_id: res.install_id || null,
    };
    if (state.companionBridge.connected) {
      state.capabilities.can_takeover = true;
    }
  } catch (_) {
    state.companionBridge = { connected: false, reachable: false, install_id: null };
  }
}

async function handleBrowserPairRoute(code) {
  const cleanCode = String(code || "").trim();
  if (!cleanCode) {
    toast("Missing Companion pairing code.");
    navigateTo("home");
    return;
  }
  try {
    const res = await companionApi("/api/companion/pair", {
      method: "POST",
      body: JSON.stringify({ code: cleanCode, origin: window.location.origin }),
    });
    if (res.token) {
      setCompanionToken(res.token);
      state.companionBridge = { connected: true, reachable: true, install_id: res.install_id || null };
      state.capabilities.can_takeover = true;
      toast("Paired with local Windows Companion! Cross-user Replay Takeover is ready.");
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

function canTakeoverNow() {
  return Boolean(state.capabilities && state.capabilities.can_takeover);
}

function isProCorpusEnabled() {
  return Boolean(state.capabilities && state.capabilities.pro_corpus_enabled);
}

function isTouchDevice() {
  return Boolean(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
}

function syncProCorpusGateUi() {
  const enabled = isProCorpusEnabled();
  const tip = "On the roadmap";
  const navIns = document.getElementById("navInsights");
  if (navIns) {
    navIns.disabled = false;
    navIns.setAttribute("aria-disabled", enabled ? "false" : "true");
    navIns.classList.toggle("pro-gated", !enabled);
    navIns.title = enabled ? "AI & Pro Corpus Insights" : tip;
  }
  const proTab = document.getElementById("sideTabProSearch");
  if (proTab) {
    proTab.disabled = false;
    proTab.setAttribute("aria-disabled", enabled ? "false" : "true");
    proTab.classList.toggle("pro-gated", !enabled);
    proTab.title = enabled ? "Search similar pro trajectories" : tip;
  }
  const proBtn = document.getElementById("btnFindProAnswers");
  if (proBtn) {
    proBtn.disabled = false;
    proBtn.setAttribute("aria-disabled", enabled ? "false" : "true");
    proBtn.classList.toggle("pro-gated", !enabled);
    proBtn.title = enabled ? "Find matching pro answers for current frame/range" : tip;
  }
  const runProBtn = document.getElementById("btnRunProSearch");
  if (runProBtn) {
    runProBtn.disabled = false;
    runProBtn.setAttribute("aria-disabled", enabled ? "false" : "true");
    runProBtn.classList.toggle("pro-gated", !enabled);
    runProBtn.title = enabled ? "Search pro corpus" : tip;
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
    const pracBtn = document.getElementById("btnPracticeTakeover");
    if (pracBtn) {
      pracBtn.classList.toggle("hidden", !canTakeoverNow());
    }
    const tipEl = document.getElementById("studioTakeoverTip");
    if (tipEl) {
      tipEl.classList.toggle("hidden", !canTakeoverNow());
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
    };
    if (state.auth.user && !state.username) {
      state.username = state.auth.user.fightcade_username || state.auth.user.discord_username || "";
    }
  } catch (_) {
    state.auth = { authenticated: false, user: null, quota: null };
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
    host.innerHTML = `<a class="btn-link primary sm" href="${esc(apiUrl(`/api/auth/discord/start?next=${next}`))}">Sign in with Discord</a>`;
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
    renderAuthControls();
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

async function launchDrill(selectionId, extraOpts = {}) {
  if (!canTakeoverNow()) {
    openCompanionModal("Replay Takeover");
    return null;
  }
  try {
    const res = await companionApi("/api/takeover", {
      method: "POST",
      body: JSON.stringify({ selection_id: selectionId, ...extraOpts }),
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
}

async function launchTakeoverPayload(payload) {
  if (!canTakeoverNow()) {
    openCompanionModal(payload && payload.label ? payload.label : "Replay Takeover");
    return null;
  }
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
}

// ---------- CALM COMPANION EXPLAINER, REPORT, TERMS & LOCAL SHARE MODALS ----------
function openCompanionModal(contextLabel = "", takeoverPayload = null) {
  const modal = document.getElementById("companionModal");
  if (!modal) return;
  const ctxEl = document.getElementById("companionModalContext");
  if (ctxEl) {
    ctxEl.textContent = contextLabel
      ? `Practice "${contextLabel}" straight inside FBNeo using your own local Fightcade ROMs.`
      : "Practice any shared clip straight inside FBNeo with instant 1-frame retry, slow-mo, and Sandbox Training.";
  }
  const uriBtn = document.getElementById("btnCompanionUriFallback");
  if (uriBtn) {
    if (takeoverPayload && takeoverPayload.quark_id) {
      uriBtn.href = buildTakeoverUri(takeoverPayload);
      uriBtn.classList.remove("hidden");
    } else {
      uriBtn.classList.add("hidden");
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
  const modal = document.getElementById("shareBundleModal");
  if (modal) modal.classList.add("hidden");
}

async function prepareLocalShareBundle(payload) {
  const modal = document.getElementById("shareBundleModal");
  const body = document.getElementById("shareBundleBody");
  if (modal && body) {
    body.innerHTML = `<div class="empty-inline">Exporting 4:3 clip.mp4 + overlay.json.gz + hitbox thumb.jpg...</div>`;
    modal.classList.remove("hidden");
  }
  try {
    const res = await api("/api/share/prepare", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const arts = res.artifacts || {};
    if (body) {
      body.innerHTML = `
        <div style="display:flex; flex-direction:column; gap:10px;">
          <div class="row-between">
            <span class="pill ready">✓ Ready for R2 Upload</span>
            <span class="pill cached mono">${formatBytes(res.total_bytes)} total · ${esc(res.dar)} DAR</span>
          </div>
          ${arts.thumb && arts.thumb.url ? `<img src="${esc(arts.thumb.url)}" alt="Hitbox thumbnail preview" style="width:100%; aspect-ratio:4/3; border-radius:9px; border:1px solid var(--border); background:#000; object-fit:contain;" />` : ""}
          <div class="share-bundle-list">
            <div class="env-row"><span>1. <code>clip.mp4</code> (${res.width}×${res.height}, SAR ${esc(res.sar)})</span><strong class="mono">${formatBytes(arts.clip?.size_bytes)}</strong></div>
            <div class="env-row"><span>2. <code>overlay.json.gz</code> (${res.has_overlay ? "Hitboxes + Inputs" : "Empty"})</span><strong class="mono">${formatBytes(arts.overlay?.size_bytes)}</strong></div>
            <div class="env-row"><span>3. <code>thumb.jpg</code> (${arts.thumb?.width || 768}×${arts.thumb?.height || 576} hitbox card)</span><strong class="mono">${formatBytes(arts.thumb?.size_bytes)}</strong></div>
          </div>
          <div style="font-size:12px; color:var(--muted);">
            Coordinates: <span class="mono">${esc(res.quark_id)} · R${res.round_id} · gf${res.start_gf}–${res.end_gf} (${res.duration_s}s)</span>
          </div>
          <div class="row" style="justify-content:flex-end; gap:6px;">
            ${arts.clip?.url ? `<a class="btn-link sm" href="${esc(arts.clip.url)}" target="_blank" rel="noopener">Open clip.mp4</a>` : ""}
            <button class="sm primary" onclick="closeShareBundleModal()">Done</button>
          </div>
        </div>
      `;
    }
    toast(`Share bundle ready (${formatBytes(res.total_bytes)})`);
    return res;
  } catch (err) {
    if (body) {
      body.innerHTML = `<div class="empty-inline" style="color:var(--rose-fg);">Share export failed: ${esc(err.message)}</div>`;
    }
    toast("Share export failed: " + err.message);
    return null;
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
