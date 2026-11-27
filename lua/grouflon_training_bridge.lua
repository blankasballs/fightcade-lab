-- Grouflon Training Mode + Display Bridge for Replay Takeover (Lua 5.1)
--
-- Supports Two-Mode Hybrid operation inside `replay_takeover.lua` with ZERO conflicts:
--   1. TAKEOVER MODE (default): Read-only Grouflon presentation + analytical HUD
--      (Hitboxes, P1/P2 Input History, Big Controllers, Numeric Gauges, Damage/Combo Info,
--      Frame Advantage, Distances, and P1/P2-aware Parry Timing Gauge). Zero RAM mutations
--      and zero dummy input interference so recorded match playback stays 100% deterministic.
--   2. SANDBOX TRAINING MODE (Alt+5 or Start Menu): Suspends recorded opponent inputs &
--      auto-loop from the current drill position and activates Dummy AI (Pose, Blocking,
--      Tech Throws, Fast Wakeup, Reversals) + Rules (Infinite Time, Life/Meter/Stun refill).
--
-- Menu Navigation (`Start` button, freezes game cleanly while open):
--   P1 Stick Up/Down/Left/Right : Navigate tabs & adjust values
--   P1 LP                       : Validate / Toggle
--   P1 MP                       : Reset value
--   P1 LK or Start              : Close menu

local M = {
  loaded = false,
  error = nil,
  menu_open = false,
  last_input = nil,
  settings_path = "",
  _menu = nil,
  _callbacks = nil,
  _notes_scroll = 0,
  _notes_tab_idx = 5,
  _notes_visible_lines = 13,
  _prev_menu_keys = {},
  _hold_timers = {},
  _dummy_state = {
    was_hit = false,
    rev_frames = 0,
    rev_action = 1,
    block_rand_ok = true,
    last_atk_frame = -1,
    tech_cooldown = 0,
  },
}

M.settings = {
  -- Display (active in BOTH Takeover & Sandbox)
  display_hitboxes = true,
  display_p1_input_history = true,
  display_p2_input_history = true,
  display_controllers = true,
  display_gauges = true,
  display_attack_data = true,
  display_frame_advantage = true,
  display_distances = false,
  display_parry_gauge = false,

  -- Sandbox Dummy (active ONLY in Sandbox Mode; automatically targets opponent!)
  pose = 1,            -- 1:Standing, 2:Crouching, 3:Jumping, 4:Walk Back
  blocking_mode = 1,   -- 1:None, 2:Always, 3:After 1st Hit, 4:Random 50%
  blocking_style = 1,  -- 1:Guard All, 2:Standing, 3:Crouching
  tech_throws = 1,     -- 1:Off, 2:Always, 3:Random 50%
  fast_wakeup = 1,     -- 1:Off, 2:Always, 3:Random 50%
  reversal = 1,        -- 1:Off, 2..11 moves

  -- Sandbox Rules (active ONLY in Sandbox Mode)
  infinite_time = true,
  life_mode = 2,       -- 1:Normal, 2:Auto-Refill, 3:Infinite Full
  meter_mode = 1,      -- 1:Normal, 2:Auto-Refill, 3:Infinite Full
  stun_mode = 1,       -- 1:Normal, 2:No Stun
  infinite_sa_time = false,

  -- Speed Throttle Scope (persisted)
  speed_scope = 1,     -- 1:Whole Drill, 2:Takeover Only

  -- Takeover Start & Loop Countdown UX (persisted)
  start_paused = true, -- Open takeover paused until any button is pressed
  loop_delay_sec = 2,  -- Sub-second countdown seconds (0..10) before each loop start
}

local POSE_LABELS = { "Standing", "Crouching", "Jumping", "Walk Back" }
local BLOCK_MODE_LABELS = { "None", "Always", "After 1st Hit", "Random 50%" }
local BLOCK_STYLE_LABELS = { "Guard All", "Standing", "Crouching" }
local TOGGLE_3_LABELS = { "Off", "Always", "Random 50%" }
local REVERSAL_LABELS = { "Off", "LP", "MP", "HP", "LK", "MK", "HK", "cr.LK", "cr.MK", "Throw", "Jump Back" }
local LIFE_MODE_LABELS = { "Normal", "Auto-Refill", "Infinite Full" }
local METER_MODE_LABELS = { "Normal", "Auto-Refill", "Infinite Full" }
local STUN_MODE_LABELS = { "Normal", "No Stun" }

local function normalize_dir(p)
  if not p or p == "" then return "3rd_training_lua-master" end
  return (string.gsub(p, "[/\\]+$", ""))
end

function M.load_settings()
  if not M.settings_path or M.settings_path == "" then return end
  local f = io.open(M.settings_path, "r")
  if not f then return end
  for line in f:lines() do
    local k, v = string.match(line, "^([A-Za-z0-9_]+)%s*=%s*(.-)%s*$")
    if k and M.settings[k] ~= nil then
      local t = type(M.settings[k])
      if t == "boolean" then
        M.settings[k] = (v == "true" or v == "1")
      elseif t == "number" then
        local n = tonumber(v)
        if n then M.settings[k] = math.floor(n) end
      end
    end
  end
  f:close()
  if type(M.settings.loop_delay_sec) ~= "number" then
    M.settings.loop_delay_sec = 2
  else
    if M.settings.loop_delay_sec < 0 then M.settings.loop_delay_sec = 0 end
    if M.settings.loop_delay_sec > 10 then M.settings.loop_delay_sec = 10 end
  end
end

function M.save_settings()
  if not M.settings_path or M.settings_path == "" then return end
  local f = io.open(M.settings_path, "w")
  if not f then return end
  local keys = {}
  for k, _ in pairs(M.settings) do keys[#keys + 1] = k end
  table.sort(keys)
  for i = 1, #keys do
    local k = keys[i]
    f:write(k .. "=" .. tostring(M.settings[k]) .. "\n")
  end
  f:close()
end

function M.init(custom_dir, settings_path, callbacks)
  if M.loaded then return true end
  M.settings_path = settings_path or os.getenv("FC_TRAINING_SETTINGS") or ""
  M._callbacks = callbacks or {}
  M.load_settings()

  local dir = normalize_dir(custom_dir or os.getenv("FC_GROUFLON_DIR"))
  local probe = io.open(dir .. "\\src\\startup.lua", "r")
  if not probe then
    M.error = "3rd_training_lua-master not found at: " .. dir
    return false
  end
  probe:close()

  local prefix = dir .. "\\?.lua;"
  if not string.find(package.path, prefix, 1, true) then
    package.path = prefix .. package.path
  end

  local ok, err = pcall(function()
    require("gd")
    if gd and gd.createFromPng and not gd._fc_wrapped then
      local orig_png = gd.createFromPng
      gd.createFromPng = function(path)
        if type(path) == "string" and not string.match(path, "^%a:[/\\]") and not string.match(path, "^[/\\]") then
          local candidate = dir .. "\\" .. path
          local f = io.open(candidate, "rb")
          if f then
            f:close()
            path = candidate
          end
        end
        return orig_png(path)
      end
      gd._fc_wrapped = true
    end

    developer_mode = false
    require("src/startup")
    require("src/tools")
    require("src/memory_adresses")
    require("src/draw")
    require("src/display")
    require("src/framedata")
    require("src/gamestate")
    require("src/input_history")
    require("src/attack_data")
    require("src/frame_advantage")
    require("src/menu_widgets")
  end)

  if not ok then
    M.error = tostring(err)
    return false
  end

  M._build_menu()
  M.loaded = true
  M.error = nil
  return true
end

local function _wrap_note_lines(text, max_chars)
  local lines = {}
  if not text or text == "" then return lines end
  max_chars = max_chars or 76
  local norm = string.gsub(tostring(text), "\r\n", "\n")
  norm = string.gsub(norm, "\r", "\n")
  local start_pos = 1
  while true do
    local nl = string.find(norm, "\n", start_pos, true)
    local para
    if nl then
      para = string.sub(norm, start_pos, nl - 1)
    else
      para = string.sub(norm, start_pos)
    end
    para = string.gsub(para, "%s+$", "")
    if para == "" then
      lines[#lines + 1] = ""
    else
      local cur = ""
      local pos = 1
      local plen = #para
      while pos <= plen do
        if cur ~= "" then
          local _, sp_end = string.find(para, "^%s+", pos)
          if sp_end then pos = sp_end + 1 end
          if pos > plen then break end
        end
        local ws, we = string.find(para, "%S+", pos)
        if not ws then break end
        local leading = (cur == "") and string.sub(para, pos, ws - 1) or ""
        local word = string.sub(para, ws, we)
        local piece = leading .. word
        pos = we + 1
        if cur == "" then
          if #piece <= max_chars then
            cur = piece
          else
            local rem = piece
            while #rem > max_chars do
              lines[#lines + 1] = string.sub(rem, 1, max_chars)
              rem = string.sub(rem, max_chars + 1)
            end
            cur = rem
          end
        else
          if (#cur + 1 + #word) <= max_chars then
            cur = cur .. " " .. word
          else
            lines[#lines + 1] = cur
            if #word <= max_chars then
              cur = word
            else
              local rem = word
              while #rem > max_chars do
                lines[#lines + 1] = string.sub(rem, 1, max_chars)
                rem = string.sub(rem, max_chars + 1)
              end
              cur = rem
            end
          end
        end
      end
      if cur ~= "" then
        lines[#lines + 1] = cur
      end
    end
    if not nl then break end
    start_pos = nl + 1
  end
  while #lines > 0 and lines[1] == "" do table.remove(lines, 1) end
  while #lines > 0 and lines[#lines] == "" do table.remove(lines, #lines) end
  return lines
end

function M._get_note_lines()
  local cb = M._callbacks
  local raw = ""
  if cb and type(cb.get_drill_note) == "function" then
    local ok, val = pcall(cb.get_drill_note)
    if ok and type(val) == "string" then
      raw = val
    end
  end
  return _wrap_note_lines(raw, 76)
end

local function _make_notes_viewer_item()
  local item = {}
  function item:draw(_x, _y, _selected)
    local lines = M._get_note_lines()
    local vis = M._notes_visible_lines or 13
    local menu_bottom = (M._menu and M._menu.bottom) or 196
    if #lines == 0 then
      gui.text(_x, _y + 4, "No note attached to this drill.", 0x94A3B8FF, text_default_border_color)
      gui.text(_x, menu_bottom - 12, "Left/Right: Switch Tab   LK/Start: Close", text_disabled_color, text_default_border_color)
      return
    end
    local max_scroll = math.max(0, #lines - vis)
    if M._notes_scroll < 0 then M._notes_scroll = 0 end
    if M._notes_scroll > max_scroll then M._notes_scroll = max_scroll end
    local count = math.min(vis, #lines - M._notes_scroll)
    for i = 1, count do
      local ln = lines[M._notes_scroll + i]
      if ln and ln ~= "" then
        gui.text(_x, _y + (i - 1) * 10, ln, text_default_color, text_default_border_color)
      end
    end
    if max_scroll > 0 then
      local first_ln = M._notes_scroll + 1
      local last_ln = math.min(#lines, M._notes_scroll + vis)
      local hint = string.format("Up/Down: Scroll (%d-%d/%d)   Left/Right: Tabs   LK/Start: Close", first_ln, last_ln, #lines)
      gui.text(_x, menu_bottom - 12, hint, 0x38BDF8FF, text_default_border_color)
    else
      gui.text(_x, menu_bottom - 12, "Left/Right: Switch Tab   LK/Start: Close", text_disabled_color, text_default_border_color)
    end
  end
  function item:legend()
    local lines = M._get_note_lines()
    local vis = M._notes_visible_lines or 13
    if #lines > vis then
      return "Up/Down: Scroll"
    end
    return "Left/Right: Switch Tab"
  end
  return item
end

local function _make_dynamic_button_item(label_fn, validate_fn, enabled_fn, legend_fn)
  local item = { last_frame_validated = 0 }
  function item:draw(_x, _y, _selected)
    local enabled = (enabled_fn == nil) or enabled_fn()
    local c = enabled and text_default_color or text_disabled_color
    if _selected then
      c = enabled and text_selected_color or 0xB45309FF
      if self.last_frame_validated > frame_number then
        self.last_frame_validated = 0
      end
      if (frame_number - self.last_frame_validated) < 5 then
        c = 0xFFFF00FF
      end
    end
    local txt = label_fn and label_fn() or ""
    gui.text(_x, _y, txt, c, text_default_border_color)
  end
  function item:validate()
    if enabled_fn and not enabled_fn() then
      return
    end
    self.last_frame_validated = frame_number
    if validate_fn then
      validate_fn()
    end
  end
  function item:legend()
    if legend_fn then
      return legend_fn()
    end
    if enabled_fn and not enabled_fn() then
      return "Play an attempt (>= 0.25s), then press any button to save"
    end
    return "Any Button: Save attempt to Fightcade Lab"
  end
  item._is_action_button = true
  item._is_enabled = enabled_fn
  return item
end

function M._build_menu()
  local cb = M._callbacks or {}
  local mode_proxy = {
    mode_idx = 1,
    watch_only = false,
    auto_loop = true,
    user_player = 1,
    speed_idx = 6,
  }
  M._mode_proxy = mode_proxy

  local function sync_from_host()
    if cb.get_host_state then
      local hs = cb.get_host_state()
      mode_proxy.mode_idx = hs.sandbox_mode and 2 or 1
      mode_proxy.watch_only = hs.watch_only and true or false
      mode_proxy.auto_loop = hs.auto_loop and true or false
      mode_proxy.user_player = (hs.user_player == 2) and 2 or 1
      if hs.speed_idx then
        mode_proxy.speed_idx = hs.speed_idx
      end
      if hs.speed_scope == 1 or hs.speed_scope == 2 then
        M.settings.speed_scope = hs.speed_scope
      end
      if type(hs.start_paused) == "boolean" then
        M.settings.start_paused = hs.start_paused
      end
      if type(hs.loop_delay_sec) == "number" then
        local d = math.floor(hs.loop_delay_sec)
        if d < 0 then d = 0 end
        if d > 10 then d = 10 end
        M.settings.loop_delay_sec = d
      end
    end
  end
  M._sync_mode_proxy = sync_from_host

  local function on_menu_change()
    M.save_settings()
    if cb.apply_host_state then
      cb.apply_host_state({
        sandbox_mode = (mode_proxy.mode_idx == 2),
        watch_only = mode_proxy.watch_only,
        auto_loop = mode_proxy.auto_loop,
        user_player = mode_proxy.user_player,
        speed_idx = mode_proxy.speed_idx,
        speed_scope = M.settings.speed_scope,
        start_paused = M.settings.start_paused,
        loop_delay_sec = M.settings.loop_delay_sec,
      })
    end
  end

  local function _get_rep_info()
    if cb and type(cb.get_rep_export_info) == "function" then
      local ok, info = pcall(cb.get_rep_export_info)
      if ok and type(info) == "table" then
        return info
      end
    end
    return nil
  end

  local function _fmt_rep_meta(frames, opp_delta, is_saved)
    local parts = string.format("%.1fs", frames / 60.0)
    if (tonumber(opp_delta) or 0) < 0 then
      parts = parts .. string.format(", %d HP", tonumber(opp_delta))
    end
    local suffix = is_saved and " [SAVED]" or ""
    return "(" .. parts .. ")" .. suffix
  end

  local function _fmt_latest_label()
    local info = _get_rep_info()
    if info and info.can_export then
      local cf = tonumber(info.current_frames) or 0
      local lf = tonumber(info.last_frames) or 0
      if cf >= 15 then
        return "Save Attempt to Lab " .. _fmt_rep_meta(cf, info.current_opp_delta, info.current_saved)
      elseif lf >= 15 then
        return "Save Attempt to Lab " .. _fmt_rep_meta(lf, info.last_opp_delta, info.last_saved)
      end
    end
    return "Save Attempt to Lab (play a rep)"
  end

  local function _latest_ready()
    local info = _get_rep_info()
    if not info or not info.can_export then return false end
    return ((tonumber(info.current_frames) or 0) >= 15) or ((tonumber(info.last_frames) or 0) >= 15)
  end

  local function _latest_legend()
    local info = _get_rep_info()
    if not info or not info.can_export then
      return "Launch Takeover from Fightcade Lab to export attempts"
    end
    local cf = tonumber(info.current_frames) or 0
    local lf = tonumber(info.last_frames) or 0
    if cf >= 15 then
      return "Any Button: Save the attempt you just played to Lab"
    elseif lf >= 15 then
      return "Any Button: Save the attempt from just before reset"
    end
    return "Play an attempt (>= 0.25s), then press any button"
  end

  local function _prior_ready()
    local info = _get_rep_info()
    if not info or not info.can_export then return false end
    return ((tonumber(info.current_frames) or 0) >= 15) and ((tonumber(info.last_frames) or 0) >= 15)
  end

  local function _fmt_prior_label()
    local info = _get_rep_info()
    if info and info.can_export and ((tonumber(info.current_frames) or 0) >= 15) and ((tonumber(info.last_frames) or 0) >= 15) then
      return "Save Prior Attempt " .. _fmt_rep_meta(tonumber(info.last_frames) or 0, info.last_opp_delta, info.last_saved)
    end
    return "Save Prior Attempt (none)"
  end

  local function _prior_legend()
    if _prior_ready() then
      return "Any Button: Save earlier attempt (before last reset)"
    end
    return "Only needed if you already started a new attempt after reset"
  end

  local tabs = {
    {
      name = "Mode",
      entries = {
        _make_dynamic_button_item(
          _fmt_latest_label,
          function()
            if cb and type(cb.save_rep) == "function" then
              local ok, saved = pcall(cb.save_rep, "latest")
              if ok and saved then M.close_menu() end
            end
          end,
          _latest_ready,
          _latest_legend
        ),
        _make_dynamic_button_item(
          _fmt_prior_label,
          function()
            if cb and type(cb.save_rep) == "function" then
              local ok, saved = pcall(cb.save_rep, "prior")
              if ok and saved then M.close_menu() end
            end
          end,
          _prior_ready,
          _prior_legend
        ),
        list_menu_item("Active Mode", mode_proxy, "mode_idx", { "Takeover", "Sandbox Training" }),
        list_menu_item("Game Speed (Alt+6/7/8)", mode_proxy, "speed_idx", { "25%", "50%", "65%", "75%", "85%", "100%", "125%" }),
        list_menu_item("Slow-Mo Scope", M.settings, "speed_scope", { "Whole Drill", "Takeover Only" }),
        integer_menu_item("Loop Countdown (sec)", M.settings, "loop_delay_sec", 0, 10, false, 2),
        checkbox_menu_item("Start Paused", M.settings, "start_paused", true),
        checkbox_menu_item("Watch Original (Takeover)", mode_proxy, "watch_only"),
        checkbox_menu_item("Auto-Loop (Takeover)", mode_proxy, "auto_loop"),
        list_menu_item("Control Side", mode_proxy, "user_player", { "P1", "P2" }),
        button_menu_item("Reset to Drill Savestate", function()
          M.close_menu()
          if cb.reset_savestate then cb.reset_savestate() end
        end),
      },
    },
    {
      name = "Display",
      entries = {
        checkbox_menu_item("Display Hitboxes", M.settings, "display_hitboxes"),
        checkbox_menu_item("Display Controllers", M.settings, "display_controllers"),
        checkbox_menu_item("Display Gauges Numbers", M.settings, "display_gauges"),
        checkbox_menu_item("Display P1 Input History", M.settings, "display_p1_input_history"),
        checkbox_menu_item("Display P2 Input History", M.settings, "display_p2_input_history"),
        checkbox_menu_item("Display Damage Info", M.settings, "display_attack_data"),
        checkbox_menu_item("Display Frame Advantage", M.settings, "display_frame_advantage"),
        checkbox_menu_item("Display Distances", M.settings, "display_distances"),
        checkbox_menu_item("Display Parry Gauge", M.settings, "display_parry_gauge"),
      },
    },
    {
      name = "Dummy",
      entries = {
        list_menu_item("Pose (Sandbox)", M.settings, "pose", POSE_LABELS),
        list_menu_item("Blocking (Sandbox)", M.settings, "blocking_mode", BLOCK_MODE_LABELS),
        list_menu_item("Blocking Style", M.settings, "blocking_style", BLOCK_STYLE_LABELS),
        list_menu_item("Tech Throws (Sandbox)", M.settings, "tech_throws", TOGGLE_3_LABELS),
        list_menu_item("Fast Wake Up (Sandbox)", M.settings, "fast_wakeup", TOGGLE_3_LABELS),
        list_menu_item("Reversal (Sandbox)", M.settings, "reversal", REVERSAL_LABELS),
      },
    },
    {
      name = "Rules",
      entries = {
        checkbox_menu_item("Infinite Time (Sandbox)", M.settings, "infinite_time"),
        list_menu_item("Life Refill (Sandbox)", M.settings, "life_mode", LIFE_MODE_LABELS),
        list_menu_item("Meter Refill (Sandbox)", M.settings, "meter_mode", METER_MODE_LABELS),
        list_menu_item("Stun Mode (Sandbox)", M.settings, "stun_mode", STUN_MODE_LABELS),
        checkbox_menu_item("Infinite SA Time (Sandbox)", M.settings, "infinite_sa_time"),
      },
    },
    {
      name = "Notes",
      entries = {
        _make_notes_viewer_item(),
      },
    },
  }

  M._notes_tab_idx = #tabs
  M._menu = make_multitab_menu(23, 18, 360, 196, tabs, on_menu_change)
end

function M.open_menu()
  if not M.loaded or not M._menu then return end
  if M._sync_mode_proxy then M._sync_mode_proxy() end
  M._notes_scroll = 0
  M.menu_open = true
  menu_stack_clear()
  menu_stack_push(M._menu)
end

function M.close_menu()
  if not M.loaded then return end
  M.menu_open = false
  menu_stack_clear()
  M.save_settings()
end

function M.toggle_menu()
  if M.menu_open then
    M.close_menu()
  else
    M.open_menu()
  end
end

function M.on_reload_state()
  if not M.loaded then return end
  pcall(reset_player_objects)
  pcall(attack_data_reset)
  pcall(frame_advantage_reset)
  pcall(clear_input_history)
  pcall(clear_printed_geometry)
  M._dummy_state.was_hit = false
  M._dummy_state.rev_frames = 0
  M._dummy_state.tech_cooldown = 0
  M.last_input = nil
end

local function _menu_edge_or_repeat(live, key, autofire)
  local down = (live[key] == true)
  local prev = (M._prev_menu_keys[key] == true)
  M._prev_menu_keys[key] = down
  if not down then
    M._hold_timers[key] = 0
    return false
  end
  if not prev then
    M._hold_timers[key] = 0
    return true
  end
  if autofire and autofire > 0 then
    local t = (M._hold_timers[key] or 0) + 1
    M._hold_timers[key] = t
    if t >= 14 and (t % autofire) == 0 then
      return true
    end
  end
  return false
end

function M.update_menu(live)
  if not M.loaded or not M.menu_open then return end
  -- Always freeze emulation cleanly while the training menu is open
  pcall(write_game_vars, { freeze = true })

  local lp_hit = _menu_edge_or_repeat(live, "P1 Weak Punch", 0)
  local mp_hit = _menu_edge_or_repeat(live, "P1 Medium Punch", 0)
  local hp_hit = _menu_edge_or_repeat(live, "P1 Strong Punch", 0)
  local lk_hit = _menu_edge_or_repeat(live, "P1 Weak Kick", 0)
  local mk_hit = _menu_edge_or_repeat(live, "P1 Medium Kick", 0)
  local hk_hit = _menu_edge_or_repeat(live, "P1 Strong Kick", 0)

  local top_menu = menu_stack_top()
  local cur_entry = (top_menu == M._menu and not M._menu.is_main_menu_selected)
    and M._menu:current_entry()
    or nil
  local on_enabled_action = false
  if cur_entry and cur_entry.validate ~= nil and cur_entry.left == nil and cur_entry.right == nil then
    on_enabled_action = (cur_entry._is_enabled == nil) or cur_entry._is_enabled()
  end

  local any_attack = lp_hit or mp_hit or hp_hit or lk_hit or mk_hit or hk_hit
  local confirm_btn = lp_hit or hp_hit or mk_hit or hk_hit

  local m_inp = {
    up       = _menu_edge_or_repeat(live, "P1 Up", 5),
    down     = _menu_edge_or_repeat(live, "P1 Down", 5),
    left     = _menu_edge_or_repeat(live, "P1 Left", 5),
    right    = _menu_edge_or_repeat(live, "P1 Right", 5),
    validate = on_enabled_action and any_attack or confirm_btn,
    reset    = (not on_enabled_action) and mp_hit or false,
    cancel   = (not on_enabled_action) and lk_hit or false,
  }
  -- Allow any confirm button (LP/HP/MK/HK) to also toggle/step setting items (checkboxes/lists/integers)
  if cur_entry and cur_entry.right ~= nil and cur_entry.validate == nil and confirm_btn then
    m_inp.right = true
  end

  if m_inp.cancel and top_menu == M._menu then
    M.close_menu()
    return
  end

  -- Dedicated Notes tab: Up/Down scroll full note, Left/Right always switch tabs cleanly
  if menu_stack_top() == M._menu and M._menu.main_menu_selected_index == M._notes_tab_idx then
    M._menu.is_main_menu_selected = true
    if m_inp.up or m_inp.down then
      local lines = M._get_note_lines()
      local vis = M._notes_visible_lines or 13
      local max_scroll = math.max(0, #lines - vis)
      if m_inp.up and M._notes_scroll > 0 then
        M._notes_scroll = M._notes_scroll - 1
      elseif m_inp.down and M._notes_scroll < max_scroll then
        M._notes_scroll = M._notes_scroll + 1
      end
      m_inp.up = false
      m_inp.down = false
    end
    if m_inp.left or m_inp.right then
      M._notes_scroll = 0
    end
  end

  pcall(menu_stack_update, m_inp)
end

-- Helper: set directional back/forward relative to player's facing (`flip_x == 0` -> facing right)
local function _set_back(out_tbl, p_obj)
  if p_obj.flip_x == 0 then
    out_tbl[p_obj.prefix .. " Left"] = true
    out_tbl[p_obj.prefix .. " Right"] = false
  else
    out_tbl[p_obj.prefix .. " Right"] = true
    out_tbl[p_obj.prefix .. " Left"] = false
  end
end

local function _clear_player_tbl(out_tbl, prefix)
  out_tbl[prefix .. " Up"] = false
  out_tbl[prefix .. " Down"] = false
  out_tbl[prefix .. " Left"] = false
  out_tbl[prefix .. " Right"] = false
  out_tbl[prefix .. " Weak Punch"] = false
  out_tbl[prefix .. " Medium Punch"] = false
  out_tbl[prefix .. " Strong Punch"] = false
  out_tbl[prefix .. " Weak Kick"] = false
  out_tbl[prefix .. " Medium Kick"] = false
  out_tbl[prefix .. " Strong Kick"] = false
end

local function _player_has_active_threat(attacker, dummy)
  if attacker.is_attacking or attacker.has_just_attacked then
    return true
  end
  if attacker.boxes then
    for i = 1, #attacker.boxes do
      local bt = attacker.boxes[i].type
      if bt == "attack" then
        return true
      end
    end
  end
  if projectiles then
    for _, proj in pairs(projectiles) do
      if proj.emitter_id == attacker.id then
        local dx = math.abs((proj.pos_x or 0) - (dummy.pos_x or 0))
        if dx < 120 then
          return true
        end
      end
    end
  end
  return false
end

local function _apply_reversal_input(out_tbl, dummy, rev_idx)
  local p = dummy.prefix
  if rev_idx == 2 then out_tbl[p .. " Weak Punch"] = true
  elseif rev_idx == 3 then out_tbl[p .. " Medium Punch"] = true
  elseif rev_idx == 4 then out_tbl[p .. " Strong Punch"] = true
  elseif rev_idx == 5 then out_tbl[p .. " Weak Kick"] = true
  elseif rev_idx == 6 then out_tbl[p .. " Medium Kick"] = true
  elseif rev_idx == 7 then out_tbl[p .. " Strong Kick"] = true
  elseif rev_idx == 8 then out_tbl[p .. " Down"] = true; out_tbl[p .. " Weak Kick"] = true
  elseif rev_idx == 9 then out_tbl[p .. " Down"] = true; out_tbl[p .. " Medium Kick"] = true
  elseif rev_idx == 10 then out_tbl[p .. " Weak Punch"] = true; out_tbl[p .. " Weak Kick"] = true
  elseif rev_idx == 11 then out_tbl[p .. " Up"] = true; _set_back(out_tbl, dummy)
  end
end

function M.apply_sandbox_dummy(out_tbl, user_player)
  if not M.loaded or not is_in_match then return end
  local attacker = (user_player == 1) and player_objects[1] or player_objects[2]
  local dummy    = (user_player == 1) and player_objects[2] or player_objects[1]
  if not attacker or not dummy then return end

  _clear_player_tbl(out_tbl, dummy.prefix)
  local ds = M._dummy_state

  -- 1. Base Pose
  if M.settings.pose == 2 then
    out_tbl[dummy.prefix .. " Down"] = true
  elseif M.settings.pose == 3 then
    out_tbl[dummy.prefix .. " Up"] = true
  elseif M.settings.pose == 4 then
    _set_back(out_tbl, dummy)
  end

  -- 2. Track hit / wakeup / recovery for After-1st-Hit Blocking & Reversals
  if dummy.has_just_been_hit then
    ds.was_hit = true
  elseif dummy.is_idle and (dummy.idle_time or 0) > 15 then
    ds.was_hit = false
  end

  -- 3. Fast Wake Up
  if M.settings.fast_wakeup > 1 and dummy.has_just_started_wake_up then
    if M.settings.fast_wakeup == 2 or math.random() > 0.5 then
      out_tbl[dummy.prefix .. " Down"] = true
    end
  end

  -- 4. Blocking
  local bm = M.settings.blocking_mode
  if bm > 1 then
    local threat = _player_has_active_threat(attacker, dummy)
    if attacker.has_just_attacked then
      ds.block_rand_ok = (bm ~= 4) or (math.random() > 0.5)
    end
    local should_guard = false
    if threat or (dummy.is_blocking) then
      if bm == 2 then
        should_guard = true
      elseif bm == 3 then
        should_guard = ds.was_hit or dummy.is_blocking
      elseif bm == 4 then
        should_guard = ds.block_rand_ok
      end
    end
    if should_guard then
      _set_back(out_tbl, dummy)
      if M.settings.blocking_style == 3 then
        out_tbl[dummy.prefix .. " Down"] = true
      elseif M.settings.blocking_style == 2 then
        out_tbl[dummy.prefix .. " Down"] = false
      else
        -- Guard All: stand against airborne/jumping attackers, crouch against grounded/crouching lows
        if (attacker.pos_y or 0) > 12 then
          out_tbl[dummy.prefix .. " Down"] = false
        elseif attacker.is_crouched then
          out_tbl[dummy.prefix .. " Down"] = true
        end
      end
    end
  end

  -- 5. Tech Throws
  if ds.tech_cooldown > 0 then ds.tech_cooldown = ds.tech_cooldown - 1 end
  if M.settings.tech_throws > 1 and ds.tech_cooldown == 0 then
    if (attacker.throw_countdown or 0) > 0 and test_collision and dummy.boxes and attacker.boxes then
      local hit_throw = test_collision(
        dummy.pos_x, dummy.pos_y, dummy.flip_x, dummy.boxes,
        attacker.pos_x, attacker.pos_y, attacker.flip_x, attacker.boxes,
        { { { "throwable" }, { "throw" } } },
        0, 0
      )
      if hit_throw then
        ds.tech_cooldown = 18
        if M.settings.tech_throws == 2 or math.random() > 0.5 then
          out_tbl[dummy.prefix .. " Weak Punch"] = true
          out_tbl[dummy.prefix .. " Weak Kick"] = true
        end
      end
    end
  end

  -- 6. Wakeup / Block-Recovery Reversal
  if M.settings.reversal > 1 then
    local trigger = false
    if dummy.has_just_woken_up or dummy.has_just_started_fast_wake_up then
      trigger = true
    elseif (dummy.previous_recovery_time or 0) == 1 and (dummy.recovery_time or 0) == 0 then
      trigger = true
    end
    if trigger then
      ds.rev_frames = 3
    end
    if ds.rev_frames > 0 then
      _clear_player_tbl(out_tbl, dummy.prefix)
      _apply_reversal_input(out_tbl, dummy, M.settings.reversal)
      ds.rev_frames = ds.rev_frames - 1
    end
  else
    ds.rev_frames = 0
  end
end

local function _apply_player_rules(p_obj)
  if not p_obj then return end

  -- Life refill
  if M.settings.life_mode == 2 then
    if p_obj.is_idle and (p_obj.idle_time or 0) > 20 then
      local life = memory.readbyte(p_obj.base + 0x9F)
      if life < 160 then
        life = math.min(life + 6, 160)
        memory.writebyte(p_obj.base + 0x9F, life)
        p_obj.life = life
      end
    end
  elseif M.settings.life_mode == 3 then
    memory.writebyte(p_obj.base + 0x9F, 160)
    p_obj.life = 160
  end

  -- Meter refill
  if not p_obj.is_in_timed_sa then
    if M.settings.meter_mode == 3 then
      local prev_cnt = memory.readbyte(p_obj.meter_addr[2])
      if prev_cnt ~= p_obj.max_meter_count then
        local is_timed = character_specific[p_obj.char_str] and character_specific[p_obj.char_str].timed_sa[p_obj.selected_sa]
        memory.writebyte(p_obj.gauge_addr, is_timed and p_obj.max_meter_gauge or 0)
        memory.writebyte(p_obj.meter_addr[2], p_obj.max_meter_count)
        memory.writebyte(p_obj.meter_update_flag, 0x01)
      end
    elseif M.settings.meter_mode == 2 then
      if p_obj.is_idle and (p_obj.idle_time or 0) > 25 then
        local g = memory.readbyte(p_obj.gauge_addr)
        local c = memory.readbyte(p_obj.meter_addr[2])
        if c < p_obj.max_meter_count then
          g = g + 6
          if g >= p_obj.max_meter_gauge then
            g = 0
            c = math.min(c + 1, p_obj.max_meter_count)
            memory.writebyte(p_obj.meter_addr[2], c)
            memory.writebyte(p_obj.meter_update_flag, 0x01)
          end
          memory.writebyte(p_obj.gauge_addr, g)
        end
      end
    end
  end

  if M.settings.infinite_sa_time and p_obj.is_in_timed_sa then
    memory.writebyte(p_obj.gauge_addr, p_obj.max_meter_gauge)
  end

  -- Stun
  if M.settings.stun_mode == 2 then
    memory.writebyte(p_obj.stun_timer_addr, 0)
    memory.writedword(p_obj.stun_bar_addr, 0)
  end
end

function M.apply_sandbox_rules()
  if not M.loaded or not is_in_match or M.menu_open then return end
  pcall(write_game_vars, {
    freeze = false,
    infinite_time = M.settings.infinite_time,
  })
  pcall(_apply_player_rules, player_objects[1])
  pcall(_apply_player_rules, player_objects[2])
end

function M.step_frame(effective_input, user_player, sandbox_mode, is_frozen)
  if not M.loaded then return end
  draw_read()
  gamestate_read()

  if M.menu_open or is_frozen then
    return
  end

  if sandbox_mode then
    M.apply_sandbox_rules()
  else
    -- Ensure game is unfrozen when returning from menu in Takeover Mode (zero other RAM writes!)
    pcall(write_game_vars, { freeze = false })
  end

  local inp = effective_input or joypad.get()
  M.last_input = inp

  local attacker = (user_player == 1) and player_objects[1] or player_objects[2]
  local defender = (user_player == 1) and player_objects[2] or player_objects[1]
  if is_in_match and attacker and defender then
    pcall(attack_data_update, attacker, defender)
    pcall(frame_advantage_update, attacker, defender)
    pcall(input_history_update, input_history[1], "P1", inp)
    pcall(input_history_update, input_history[2], "P2", inp)
  else
    pcall(clear_input_history)
    pcall(attack_data_reset)
    pcall(frame_advantage_reset)
  end
end

local function _draw_parry_gauge(p_obj)
  if not p_obj or not p_obj.parry_forward then return end
  local x = (p_obj.id == 1) and 235 or 55
  local y = 40
  local x_scale = 4
  local y_off = 0

  local function draw_one(py, po)
    if not po or not po.max_validity or po.max_validity <= 0 then return 0 end
    local gh = 4
    local bg = 0xD6E7EF77
    local vw = po.max_validity * x_scale
    local cw = po.max_cooldown * x_scale
    local vl = math.floor(x + (cw - vw) * 0.5)
    local cr = x + cw + 1
    local vt = string.format("%d", po.validity_time or 0)
    local vc = text_default_color
    if po.delta then
      vc = po.success and 0x10FB00FF or 0xE70000FF
      vt = (po.delta >= 0) and string.format("%d", -po.delta) or string.format("+%d", -po.delta)
    end
    gui.text(x + 1, py, po.name or "Parry", text_default_color, text_default_border_color)
    draw_gauge(vl, py + 8, vw, gh + 1, (po.validity_time or 0) / po.max_validity, 0x08CF00FF, bg, nil, true)
    draw_gauge(x, py + 8 + gh + 2, cw, gh, (po.cooldown_time or 0) / po.max_cooldown, 0xFF7939FF, bg, nil, true)
    gui.text(cr + 4, py + 7, vt, vc, text_default_border_color)
    gui.text(cr + 4, py + 13, string.format("%d", po.cooldown_time or 0), text_default_color, text_default_border_color)
    return 8 + 5 + (gh * 2) + 6
  end

  y_off = y_off + draw_one(y + y_off, p_obj.parry_forward)
  y_off = y_off + draw_one(y + y_off, p_obj.parry_down)
  y_off = y_off + draw_one(y + y_off, p_obj.parry_air)
end

function M.draw(user_player)
  if not M.loaded then return end

  if is_in_match then
    if M.settings.display_hitboxes then
      display_draw_hitboxes()
    end

    if M.settings.display_gauges then
      display_draw_life(player_objects[1])
      display_draw_life(player_objects[2])
      display_draw_meter(player_objects[1])
      display_draw_meter(player_objects[2])
      display_draw_stun_gauge(player_objects[1])
      display_draw_stun_gauge(player_objects[2])
      display_draw_bonuses(player_objects[1])
      display_draw_bonuses(player_objects[2])
    end

    if M.settings.display_distances then
      display_draw_distances(player_objects[1], player_objects[2], 70, 2, 2)
    end

    if M.settings.display_p1_input_history then
      input_history_draw(input_history[1], 4, 49, false)
    end
    if M.settings.display_p2_input_history then
      input_history_draw(input_history[2], screen_width - 4, 49, true)
    end

    if M.settings.display_controllers then
      local inp = M.last_input or joypad.get()
      draw_controller_big(make_input_history_entry("P1", inp), 44, 34)
      draw_controller_big(make_input_history_entry("P2", inp), 310, 34)
    end

    if M.settings.display_attack_data and not M.settings.display_parry_gauge then
      pcall(attack_data_display)
    end

    if M.settings.display_frame_advantage then
      pcall(frame_advantage_display)
    end

    if M.settings.display_parry_gauge then
      local p_obj = (user_player == 1) and player_objects[1] or player_objects[2]
      pcall(_draw_parry_gauge, p_obj)
    end
  end

  if M.menu_open then
    pcall(menu_stack_draw)
  end
end

return M
