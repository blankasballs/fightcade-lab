-- Instant <1s Savestate Replay Takeover + Two-Mode Grouflon Training Hybrid (Lua 5.1)
--
-- Booted alongside a cached .fs savestate:
--   fcadefbneo.exe -w <savestate.fs> <replay_takeover.lua>
--
-- Environment variables:
--   FC_INPUTS_PATH        CSV of `game_frame,p1_mask,p2_mask` exported from telemetry Parquet
--   FC_SAVESTATE_PATH     Disk path to the .fs savestate (fallback reload)
--   FC_TARGET_FRAME       Deterministic game_frame (0x02007F00) at decision point t0
--   FC_USER_PLAYER        1 (default) or 2 — which player the user controls during takeover
--   FC_TAKEOVER_LEAD      Frames before t0 to hand control to user automatically (default: 30 = 0.5s)
--   FC_AUTO_LOOP          "1" (default) to auto-rewind after sequence ends; "0" to keep playing
--   FC_WATCH_ONLY         "1" to start in Watch Original mode (replays both players untouched)
--   FC_LABEL              Optional HUD banner label
--   FC_SPEED              Initial speed multiplier (0.10..2.00, or percent e.g. 50 -> 0.50; default: 1.0)
--   FC_SPEED_SCOPE        "all" (default, whole drill) or "takeover_only" (100% setup, slow-mo on takeover)
--   FC_LOOP_DELAY         Optional countdown seconds (0..10) before each loop start (default: saved or 2)
--   FC_START_PAUSED       Optional "1"/"0" to open paused until any button press (default: saved or "1")
--   FC_GROUFLON_TRAINING  "1" (default) to enable Grouflon HUD + Start Menu + Alt+5 Sandbox Mode
--   FC_GROUFLON_DIR       Path to `3rd_training_lua-master` (default: "3rd_training_lua-master")
--   FC_TRAINING_SETTINGS  Path to persist Display/Dummy/Rules settings
--   FC_VERIFY_OUT         Optional path to log (gf, p1_x, p1_y, p1_hp, p2_x, p2_y, p2_hp) and exit
--   FC_VERIFY_EXIT_GF     Optional game_frame at which to flush FC_VERIFY_OUT and os.exit(0)
--
-- Hotkeys:
--   Any P1 Button       : From initial pause, starts the loop countdown (disappearing dots)
--   Select (P1/P2 Coin) : Instant retry (reloads drill savestate into countdown; Alt+1 also works)
--   Alt+2               : Toggle TAKEOVER vs WATCH ORIGINAL mode (and restarts from savestate)
--   Alt+3               : Toggle AUTO-LOOP ON/OFF (Takeover mode)
--   Alt+4               : Swap controlled player (P1 <-> P2) and restart from savestate
--   Alt+5               : Toggle TAKEOVER <-> SANDBOX TRAINING MODE (Dummy AI + Rules!)
--   Alt+6               : Slower game speed (25% / 50% / 65% / 75% / 85% / 100% / 125%)
--   Alt+7               : Faster game speed
--   Alt+8               : Quick toggle 100% <-> last slow-mo speed (default 50%)
--   Start               : Open / Close Grouflon Display, Dummy & Rules menu (also configures Loop Countdown)

local GAME_FRAME_ADDR  = 0x02007F00
local FREEZE_GAME_ADDR = 0x0201136F
local P1_BASE          = 0x02068C6C
local P2_BASE          = 0x02069104
local FPS              = 60

local INPUTS_PATH       = os.getenv("FC_INPUTS_PATH") or ""
local SAVESTATE_PATH    = os.getenv("FC_SAVESTATE_PATH") or ""
local TARGET_FRAME      = tonumber(os.getenv("FC_TARGET_FRAME")) or 0
local USER_PLAYER       = tonumber(os.getenv("FC_USER_PLAYER")) or 1
local TAKEOVER_LEAD     = tonumber(os.getenv("FC_TAKEOVER_LEAD")) or 30
local EXPLICIT_TO_GF    = tonumber(os.getenv("FC_TAKEOVER_GF")) or 0
local AUTO_LOOP         = (os.getenv("FC_AUTO_LOOP") or "1") ~= "0"
local WATCH_ONLY        = (os.getenv("FC_WATCH_ONLY") or "0") == "1"
local LABEL             = os.getenv("FC_LABEL") or ""
local VERIFY_OUT        = os.getenv("FC_VERIFY_OUT") or ""
local VERIFY_EXIT_GF    = tonumber(os.getenv("FC_VERIFY_EXIT_GF")) or 0
local GROUFLON_ENABLED  = (os.getenv("FC_GROUFLON_TRAINING") or "1") ~= "0"
local GROUFLON_DIR      = os.getenv("FC_GROUFLON_DIR") or ""
local TRAINING_SETTINGS = os.getenv("FC_TRAINING_SETTINGS") or ""
local SPEED_ENV_RAW     = tonumber(os.getenv("FC_SPEED")) or 1.0
local SPEED_SCOPE_ENV   = string.lower(os.getenv("FC_SPEED_SCOPE") or "")
local LOOP_DELAY_ENV    = os.getenv("FC_LOOP_DELAY") or ""
local START_PAUSED_ENV  = os.getenv("FC_START_PAUSED") or ""

local function clamp_loop_delay(val)
  local n = tonumber(val)
  if not n then return 2 end
  n = math.floor(n + 0.5)
  if n < 0 then n = 0 end
  if n > 10 then n = 10 end
  return n
end

local INIT_LOOP_DELAY = (LOOP_DELAY_ENV ~= "") and clamp_loop_delay(LOOP_DELAY_ENV) or 2
local INIT_START_PAUSED = (START_PAUSED_ENV ~= "") and (START_PAUSED_ENV ~= "0" and string.lower(START_PAUSED_ENV) ~= "false") or true

local SPEED_PRESETS = { 0.25, 0.50, 0.65, 0.75, 0.85, 1.00, 1.25 }
local SPEED_LABELS  = { "25%", "50%", "65%", "75%", "85%", "100%", "125%" }
local DEFAULT_SPEED_IDX = 6 -- 100%

local function nearest_speed_idx(val)
  local s = tonumber(val) or 1.0
  if s > 2.0 then s = s / 100.0 end
  local best_i = DEFAULT_SPEED_IDX
  local best_d = 999
  for i = 1, #SPEED_PRESETS do
    local d = math.abs(SPEED_PRESETS[i] - s)
    if d < best_d then
      best_d = d
      best_i = i
    end
  end
  return best_i
end

local INIT_SPEED_IDX = nearest_speed_idx(SPEED_ENV_RAW)
local INIT_SPEED_SCOPE = (SPEED_SCOPE_ENV == "takeover_only") and 2 or 1
local SPEED_SUPPORTED = (emu ~= nil and type(emu.setspeed) == "function")

-- Ensure sibling Lua modules (grouflon_training_bridge.lua) are importable regardless of cwd
do
  local info = debug and debug.getinfo and debug.getinfo(1, "S")
  local src = info and info.source or ""
  if string.sub(src, 1, 1) == "@" then
    local dir = string.match(string.sub(src, 2), "^(.*)[/\\][^/\\]+$")
    if dir and dir ~= "" then
      package.path = dir .. "\\?.lua;" .. package.path
    end
  end
end

local TAKEOVER_GF     = (EXPLICIT_TO_GF > 0) and EXPLICIT_TO_GF or math.max(1, TARGET_FRAME - TAKEOVER_LEAD)
local MEM_SLOT_ID     = 1

local BUTTON_BITS = {
  { 0x001, " Up" },
  { 0x002, " Down" },
  { 0x004, " Left" },
  { 0x008, " Right" },
  { 0x010, " Weak Punch" },
  { 0x020, " Medium Punch" },
  { 0x040, " Strong Punch" },
  { 0x080, " Weak Kick" },
  { 0x100, " Medium Kick" },
  { 0x200, " Strong Kick" },
}

-- Load recorded frame-by-frame inputs indexed by deterministic game_frame
local inputs_by_gf = {}
local min_gf = nil
local max_gf = 0
local loaded_count = 0

if INPUTS_PATH ~= "" then
  local f = io.open(INPUTS_PATH, "r")
  if f then
    for line in f:lines() do
      local gf_s, m1_s, m2_s = string.match(line, "^(%d+),(%d+),(%d+)")
      if gf_s then
        local gf = tonumber(gf_s)
        local m1 = tonumber(m1_s)
        local m2 = tonumber(m2_s)
        inputs_by_gf[gf] = { p1 = m1, p2 = m2 }
        if not min_gf or gf < min_gf then min_gf = gf end
        if gf > max_gf then max_gf = gf end
        loaded_count = loaded_count + 1
      end
    end
    f:close()
  end
end

local state = {
  mem_saved             = false,
  start_gf              = 0,
  last_gf               = 0,
  taken_over            = false,
  watch_only            = WATCH_ONLY,
  auto_loop             = AUTO_LOOP,
  sandbox_mode          = false,
  reload_pending        = false,
  prev_select_down      = false,
  prev_start_down       = false,
  prev_unpause_down     = true,
  menu_was_open         = false,
  wait_unpause_release  = false,
  phase                 = "running", -- "paused" | "countdown" | "running"
  start_paused          = INIT_START_PAUSED,
  loop_delay_sec        = INIT_LOOP_DELAY,
  countdown_frames      = 0,
  countdown_total_frames = 0,
  effective_input       = nil,
  banner_msg            = "",
  banner_timer          = 0,
  verify_rows           = {},
  speed_idx             = INIT_SPEED_IDX,
  last_slow_idx         = (INIT_SPEED_IDX < DEFAULT_SPEED_IDX) and INIT_SPEED_IDX or 2, -- default 50%
  speed_scope           = INIT_SPEED_SCOPE, -- 1 = Whole Drill, 2 = Takeover Only
  applied_speed         = 1.0,
}

local function set_banner(msg, frames)
  state.banner_msg = msg
  state.banner_timer = frames or 120
end

local bridge = nil

local function desired_speed()
  if VERIFY_EXIT_GF > 0 then
    return SPEED_PRESETS[state.speed_idx] or 1.0
  end
  if bridge and bridge.menu_open then
    return 1.0
  end
  if state.phase == "paused" or state.phase == "countdown" then
    return 1.0
  end
  local target = SPEED_PRESETS[state.speed_idx] or 1.0
  if state.speed_scope == 2 and (not state.sandbox_mode) and (not state.watch_only) and (not state.taken_over) then
    return 1.0
  end
  return target
end

local function sync_emu_speed()
  if not SPEED_SUPPORTED then
    return
  end
  local want = desired_speed()
  local cur = state.applied_speed
  if emu and type(emu.getspeed) == "function" then
    local ok_g, g = pcall(emu.getspeed)
    if ok_g and type(g) == "number" then
      cur = g
      state.applied_speed = g
    end
  end
  if math.abs(want - cur) < 0.001 then
    return
  end
  local ok, actual = pcall(emu.setspeed, want)
  if ok and type(actual) == "number" then
    state.applied_speed = actual
  else
    state.applied_speed = want
  end
end

local function set_speed_idx(new_idx, announce)
  if not SPEED_SUPPORTED then
    if announce then
      set_banner("SPEED THROTTLE REQUIRES fcadefbneo_fast.exe", 120)
    end
    return
  end
  if new_idx < 1 then new_idx = 1 end
  if new_idx > #SPEED_PRESETS then new_idx = #SPEED_PRESETS end
  state.speed_idx = new_idx
  if new_idx < DEFAULT_SPEED_IDX then
    state.last_slow_idx = new_idx
  end
  sync_emu_speed()
  if announce then
    local scope_txt = (state.speed_scope == 2 and new_idx ~= DEFAULT_SPEED_IDX) and " (Takeover Only)" or ""
    set_banner("GAME SPEED: " .. SPEED_LABELS[new_idx] .. scope_txt, 90)
  end
end

if GROUFLON_ENABLED then
  local ok_b, mod = pcall(require, "grouflon_training_bridge")
  if ok_b and mod then
    local ok_i = mod.init(GROUFLON_DIR, TRAINING_SETTINGS, {
      get_host_state = function()
        return {
          sandbox_mode = state.sandbox_mode,
          watch_only = state.watch_only,
          auto_loop = state.auto_loop,
          user_player = USER_PLAYER,
          speed_idx = state.speed_idx,
          speed_scope = state.speed_scope,
          speed_supported = SPEED_SUPPORTED,
          start_paused = state.start_paused,
          loop_delay_sec = state.loop_delay_sec,
        }
      end,
      apply_host_state = function(hs)
        local mode_changed = (hs.sandbox_mode ~= state.sandbox_mode)
        local side_changed = (hs.user_player ~= USER_PLAYER)
        local watch_changed = (hs.watch_only ~= state.watch_only)
        local speed_changed = (hs.speed_idx ~= nil and hs.speed_idx ~= state.speed_idx)
        local delay_changed = (hs.loop_delay_sec ~= nil and clamp_loop_delay(hs.loop_delay_sec) ~= state.loop_delay_sec)
        state.sandbox_mode = hs.sandbox_mode and true or false
        state.watch_only = hs.watch_only and true or false
        state.auto_loop = hs.auto_loop and true or false
        USER_PLAYER = (hs.user_player == 2) and 2 or 1
        if hs.speed_scope == 1 or hs.speed_scope == 2 then
          state.speed_scope = hs.speed_scope
        end
        if type(hs.start_paused) == "boolean" then
          state.start_paused = hs.start_paused
        end
        if hs.loop_delay_sec ~= nil then
          state.loop_delay_sec = clamp_loop_delay(hs.loop_delay_sec)
        end
        if delay_changed and state.phase == "countdown" then
          state.countdown_total_frames = state.loop_delay_sec * FPS
          state.countdown_frames = state.countdown_total_frames
        end
        if speed_changed then
          set_speed_idx(hs.speed_idx, false)
        else
          sync_emu_speed()
        end
        if mode_changed then
          if state.sandbox_mode then
            state.phase = "running"
            state.countdown_frames = 0
            state.taken_over = true
            set_banner("SANDBOX TRAINING MODE (Start = Menu, Select = Reset)", 120)
          else
            state.reload_pending = true
            set_banner("TAKEOVER MODE (Reloading drill savestate)", 90)
          end
        elseif side_changed or watch_changed then
          state.reload_pending = true
        end
      end,
      reset_savestate = function()
        state.reload_pending = true
        set_banner("RESET TO DRILL SAVESTATE", 60)
      end,
    })
    if ok_i then
      bridge = mod
      if SPEED_SCOPE_ENV == "" and bridge.settings and (bridge.settings.speed_scope == 1 or bridge.settings.speed_scope == 2) then
        state.speed_scope = bridge.settings.speed_scope
      end
      if LOOP_DELAY_ENV == "" and bridge.settings and type(bridge.settings.loop_delay_sec) == "number" then
        state.loop_delay_sec = clamp_loop_delay(bridge.settings.loop_delay_sec)
      end
      if START_PAUSED_ENV == "" and bridge.settings and type(bridge.settings.start_paused) == "boolean" then
        state.start_paused = bridge.settings.start_paused
      end
    end
  end
end

if VERIFY_EXIT_GF > 0 then
  state.start_paused = false
  state.loop_delay_sec = 0
  state.phase = "running"
elseif state.sandbox_mode then
  state.phase = "running"
elseif state.start_paused then
  state.phase = "paused"
elseif state.loop_delay_sec > 0 then
  state.phase = "countdown"
  state.countdown_total_frames = state.loop_delay_sec * FPS
  state.countdown_frames = state.countdown_total_frames
else
  state.phase = "running"
  state.taken_over = (not state.watch_only)
end

sync_emu_speed()

local function write_mask_to_table(tbl, prefix, mask)
  for i = 1, #BUTTON_BITS do
    local b = BUTTON_BITS[i]
    tbl[prefix .. b[2]] = (bit.band(mask, b[1]) ~= 0)
  end
end

local function any_player_pressed(live, prefix)
  for i = 1, #BUTTON_BITS do
    if live[prefix .. BUTTON_BITS[i][2]] then
      return true
    end
  end
  return false
end

local function any_unpause_pressed(live)
  if any_player_pressed(live, "P1") then
    return true
  end
  if (live["P1 Coin"] == true) or (live["P2 Coin"] == true) then
    return true
  end
  return false
end

local function copy_live_to_player(tbl, live, dst_prefix)
  -- Drive P2 strictly from physical P1 hardware inputs (`GIT_SWITCH` inputs are freshly polled
  -- every frame by InputMake; unmapped P2 inputs have nInput==0 and retain stale pVal from
  -- previous Lua joypad.set frames in inp_interface.cpp:231!).
  for i = 1, #BUTTON_BITS do
    local suffix = BUTTON_BITS[i][2]
    local pressed = (live["P1" .. suffix] == true)
    tbl[dst_prefix .. suffix] = pressed
  end
end

local function make_empty_table()
  local tbl = {}
  write_mask_to_table(tbl, "P1", 0)
  write_mask_to_table(tbl, "P2", 0)
  tbl["P1 Start"] = false
  tbl["P2 Start"] = false
  tbl["P1 Coin"] = false
  tbl["P2 Coin"] = false
  return tbl
end

local function build_effective_input(live, out_tbl)
  local eff = {}
  for i = 1, #BUTTON_BITS do
    local suf = BUTTON_BITS[i][2]
    local k1 = "P1" .. suf
    local k2 = "P2" .. suf
    eff[k1] = (out_tbl[k1] ~= nil) and out_tbl[k1] or (live[k1] == true)
    eff[k2] = (out_tbl[k2] ~= nil) and out_tbl[k2] or (live[k2] == true)
  end
  return eff
end

local function do_reload()
  local ok = false
  if state.mem_saved then
    ok = pcall(savestate.load_mem, MEM_SLOT_ID)
  end
  if not ok and SAVESTATE_PATH ~= "" then
    pcall(savestate.load, SAVESTATE_PATH)
  end
  -- Hand immediate Frame-1 control to the user in interactive Takeover / Sandbox runs
  -- (keep legacy TAKEOVER_GF lead-in only for automated VERIFY_EXIT_GF determinism runs)
  state.taken_over = state.sandbox_mode or ((VERIFY_EXIT_GF == 0) and (not state.watch_only))
  state.reload_pending = false
  if bridge then
    pcall(bridge.on_reload_state)
  end
end

local function enter_countdown_or_run(live, from_unpause)
  do_reload()
  if (not state.sandbox_mode) and state.loop_delay_sec > 0 and VERIFY_EXIT_GF == 0 then
    state.phase = "countdown"
    state.countdown_total_frames = state.loop_delay_sec * FPS
    state.countdown_frames = state.countdown_total_frames
    state.wait_unpause_release = false
    memory.writebyte(FREEZE_GAME_ADDR, 0xFF)
    sync_emu_speed()
    joypad.set(make_empty_table())
    return true -- froze into countdown; caller should return
  end
  if from_unpause and live and any_unpause_pressed(live) and VERIFY_EXIT_GF == 0 then
    -- Loop delay is 0s: wait for the unpause button to be released while frozen so it doesn't leak a stray input on frame 1
    state.phase = "countdown"
    state.countdown_total_frames = 0
    state.countdown_frames = 0
    state.wait_unpause_release = true
    memory.writebyte(FREEZE_GAME_ADDR, 0xFF)
    sync_emu_speed()
    joypad.set(make_empty_table())
    return true
  end
  memory.writebyte(FREEZE_GAME_ADDR, 0x00)
  state.phase = "running"
  state.countdown_frames = 0
  state.wait_unpause_release = false
  return false
end

local function on_before()
  local live = joypad.get()

  -- Capture pristine savestate on frame 1 BEFORE any freeze write so reloads are 100% bit-exact
  if not state.mem_saved then
    local boot_gf = memory.readdword(GAME_FRAME_ADDR)
    state.start_gf = boot_gf
    state.last_gf = boot_gf
    pcall(savestate.save_mem, MEM_SLOT_ID)
    state.mem_saved = true
    state.prev_unpause_down = any_unpause_pressed(live)
  end

  -- Start button toggles Grouflon Display / Dummy / Rules menu (interactive runs only)
  local start_down = (live["P1 Start"] == true)
  if bridge and VERIFY_EXIT_GF == 0 then
    if start_down and not state.prev_start_down then
      bridge.toggle_menu()
    end
  end
  state.prev_start_down = start_down

  -- While menu is open: freeze emulation & drive menu cleanly from P1 physical stick at 100% speed
  if bridge and bridge.menu_open then
    state.menu_was_open = true
    sync_emu_speed()
    bridge.update_menu(live)
    joypad.set(make_empty_table())
    return
  end

  -- Guard against menu-close button (LK/Start) accidentally triggering unpause or retry on the next frame
  if state.menu_was_open then
    state.menu_was_open = false
    state.prev_unpause_down = true
    state.prev_select_down = true
  end

  local select_down = (live["P1 Coin"] == true) or (live["P2 Coin"] == true)

  -- PHASE 1: Initial Pause (waits for any P1 button rising edge, then starts first countdown)
  if state.phase == "paused" and (not state.sandbox_mode) and VERIFY_EXIT_GF == 0 then
    if state.reload_pending then
      do_reload()
    end
    local unpause_now = any_unpause_pressed(live)
    if unpause_now and not state.prev_unpause_down then
      state.prev_unpause_down = true
      state.prev_select_down = select_down
      if enter_countdown_or_run(live, true) then
        return
      end
      -- If loop_delay_sec == 0 and button already released, fall through directly into running on this frame
    else
      state.prev_unpause_down = unpause_now
      state.prev_select_down = select_down
      memory.writebyte(FREEZE_GAME_ADDR, 0xFF)
      sync_emu_speed()
      joypad.set(make_empty_table())
      return
    end
  else
    if select_down and not state.prev_select_down and VERIFY_EXIT_GF == 0 then
      state.reload_pending = true
      set_banner("RETRYING FROM SAVESTATE", 60)
    end
    state.prev_select_down = select_down
  end

  -- PHASE 2: Loop-Start Countdown (disappearing dots; 100% speed; reloads pristine state at 0)
  if state.phase == "countdown" and (not state.sandbox_mode) and VERIFY_EXIT_GF == 0 then
    if state.reload_pending then
      if enter_countdown_or_run(live, false) then
        return
      end
    elseif state.wait_unpause_release then
      if any_unpause_pressed(live) then
        memory.writebyte(FREEZE_GAME_ADDR, 0xFF)
        sync_emu_speed()
        joypad.set(make_empty_table())
        return
      else
        state.wait_unpause_release = false
        do_reload()
        memory.writebyte(FREEZE_GAME_ADDR, 0x00)
        state.phase = "running"
        -- Fall through so frame 1 of the drill executes seamlessly on this exact frame
      end
    else
      if state.countdown_frames > 0 then
        state.countdown_frames = state.countdown_frames - 1
      end
      if state.countdown_frames <= 0 then
        do_reload()
        memory.writebyte(FREEZE_GAME_ADDR, 0x00)
        state.phase = "running"
        -- Accept live user inputs immediately on Frame 1 (including held block/charge directions!)
      else
        memory.writebyte(FREEZE_GAME_ADDR, 0xFF)
        sync_emu_speed()
        joypad.set(make_empty_table())
        return
      end
    end
  end

  -- PHASE 3: Active Running (Takeover / Watch Original / Sandbox Training)
  if state.reload_pending then
    if enter_countdown_or_run(live, false) then
      return
    end
  end

  local cur_gf = memory.readdword(GAME_FRAME_ADDR)
  local next_gf = cur_gf + 1
  state.last_gf = next_gf

  -- Auto-loop ONLY in Takeover / Watch Mode (suspended in Sandbox Training Mode)
  if (not state.sandbox_mode) and state.auto_loop and max_gf > 0 and next_gf > (max_gf + 90) and VERIFY_EXIT_GF == 0 then
    if enter_countdown_or_run(live, false) then
      return
    end
    cur_gf = memory.readdword(GAME_FRAME_ADDR)
    next_gf = cur_gf + 1
    state.last_gf = next_gf
  end

  local rec = inputs_by_gf[next_gf]
  local out_tbl = {}
  local need_set = false

  -- Suppress Select/Coin and Start while pressed so restarting/menu doesn't spam credit chimes or start-screen
  if (select_down or start_down) and VERIFY_EXIT_GF == 0 then
    out_tbl["P1 Coin"] = false
    out_tbl["P2 Coin"] = false
    out_tbl["P1 Start"] = false
    out_tbl["P2 Start"] = false
    need_set = true
  end

  if state.sandbox_mode then
    -- SANDBOX TRAINING MODE: User has full live control; Grouflon Dummy AI drives the opponent!
    if USER_PLAYER == 2 then
      copy_live_to_player(out_tbl, live, "P2")
    end
    if bridge then
      pcall(bridge.apply_sandbox_dummy, out_tbl, USER_PLAYER)
    else
      write_mask_to_table(out_tbl, (USER_PLAYER == 1) and "P2" or "P1", 0)
    end
    need_set = true
  elseif state.watch_only then
    if rec then
      write_mask_to_table(out_tbl, "P1", rec.p1)
      write_mask_to_table(out_tbl, "P2", rec.p2)
      need_set = true
    end
  else
    -- Trigger takeover automatically at (t0 - TAKEOVER_LEAD) OR early when user presses any input.
    if not state.taken_over then
      local user_hit = any_player_pressed(live, "P1")
      if user_hit or (TARGET_FRAME > 0 and next_gf >= TAKEOVER_GF) then
        state.taken_over = true
        set_banner("TAKEOVER! You control P" .. tostring(USER_PLAYER), 90)
      end
    end

    if not state.taken_over then
      if rec then
        write_mask_to_table(out_tbl, "P1", rec.p1)
        write_mask_to_table(out_tbl, "P2", rec.p2)
        need_set = true
      end
    else
      local opp_mask = 0
      if rec then
        opp_mask = (USER_PLAYER == 1) and rec.p2 or rec.p1
      end
      if USER_PLAYER == 1 then
        write_mask_to_table(out_tbl, "P2", opp_mask)
        need_set = true
      else
        write_mask_to_table(out_tbl, "P1", opp_mask)
        copy_live_to_player(out_tbl, live, "P2")
        need_set = true
      end
    end
  end

  state.effective_input = build_effective_input(live, out_tbl)
  sync_emu_speed()
  if need_set then
    joypad.set(out_tbl)
  end
end

local function on_after()
  if state.banner_timer > 0 then
    state.banner_timer = state.banner_timer - 1
  end

  local is_frozen = (not state.sandbox_mode) and (state.phase == "paused" or state.phase == "countdown")
  if bridge then
    pcall(bridge.step_frame, state.effective_input, USER_PLAYER, state.sandbox_mode, is_frozen)
  end

  if VERIFY_OUT ~= "" and VERIFY_EXIT_GF > 0 then
    local gf = memory.readdword(GAME_FRAME_ADDR)
    local p1x = memory.readwordsigned(P1_BASE + 0x64)
    local p1y = memory.readwordsigned(P1_BASE + 0x68)
    local p1hp = memory.readbyte(P1_BASE + 0x9F)
    if p1hp > 200 then p1hp = 0 end
    local p2x = memory.readwordsigned(P2_BASE + 0x64)
    local p2y = memory.readwordsigned(P2_BASE + 0x68)
    local p2hp = memory.readbyte(P2_BASE + 0x9F)
    if p2hp > 200 then p2hp = 0 end
    state.verify_rows[#state.verify_rows + 1] = string.format("%d,%d,%d,%d,%d,%d,%d\n", gf, p1x, p1y, p1hp, p2x, p2y, p2hp)
    if gf >= VERIFY_EXIT_GF then
      local vf = io.open(VERIFY_OUT, "w")
      if vf then
        vf:write("game_frame,p1_x,p1_y,p1_hp,p2_x,p2_y,p2_hp\n")
        vf:write(table.concat(state.verify_rows))
        vf:close()
      end
      os.exit(0)
    end
  end
end

local function draw_dot(cx, cy, fill_col, outline_col, hi_col)
  -- Crisp 9x9 pixel arcade circle built from native FBNeo gui.box primitives
  gui.box(cx - 4, cy - 2, cx + 4, cy + 2, outline_col, outline_col)
  gui.box(cx - 2, cy - 4, cx + 2, cy + 4, outline_col, outline_col)
  gui.box(cx - 3, cy - 3, cx + 3, cy + 3, outline_col, outline_col)

  gui.box(cx - 3, cy - 1, cx + 3, cy + 1, fill_col, fill_col)
  gui.box(cx - 1, cy - 3, cx + 1, cy + 3, fill_col, fill_col)
  gui.box(cx - 2, cy - 2, cx + 2, cy + 2, fill_col, fill_col)

  if hi_col then
    gui.box(cx - 2, cy - 2, cx - 1, cy - 1, hi_col, hi_col)
  end
end

local function draw_pause_or_countdown_overlay()
  if state.sandbox_mode or VERIFY_EXIT_GF > 0 or (bridge and bridge.menu_open) then
    return
  end

  local cx = 192
  local step = 16
  local total = state.loop_delay_sec

  if state.phase == "paused" then
    local title = "PAUSED - PRESS ANY BUTTON"
    local sub = (total > 0)
      and string.format("Countdown: %ds  |  Start: Menu", total)
      or "Instant Start  |  Start: Menu"
    local text_w = math.max(#title, #sub) * 4
    local dots_w = (total > 0) and ((total - 1) * step + 12) or 0
    local half_w = math.floor(math.max(text_w, dots_w) / 2) + 12
    local top_y = 56
    local bot_y = (total > 0) and 100 or 86

    gui.box(cx - half_w, top_y, cx + half_w, bot_y, 0x0B1220D8, 0x38BDF8EE)
    gui.text(cx - math.floor((#title * 4) / 2), top_y + 6, title, 0xF8FAFCFF, 0x090D16FF)

    if total > 0 then
      local start_x = cx - math.floor(((total - 1) * step) / 2)
      local dot_y = top_y + 23
      for i = 1, total do
        draw_dot(start_x + (i - 1) * step, dot_y, 0x38BDF8FF, 0x090D16FF, 0xBAE6FDFF)
      end
      gui.text(cx - math.floor((#sub * 4) / 2), top_y + 34, sub, 0x94A3B8FF, 0x090D16FF)
    else
      gui.text(cx - math.floor((#sub * 4) / 2), top_y + 19, sub, 0x94A3B8FF, 0x090D16FF)
    end
    return
  end

  if state.phase == "countdown" and total > 0 and state.countdown_frames > 0 then
    local rem = math.ceil(state.countdown_frames / FPS)
    if rem < 1 then rem = 1 end
    if rem > total then rem = total end

    local dots_w = (total - 1) * step + 10
    local half_w = math.floor(dots_w / 2) + 10
    local cy = 74
    local top_y = cy - 9
    local bot_y = cy + 9

    gui.box(cx - half_w, top_y, cx + half_w, bot_y, 0x0B1220B8, 0x475569CC)

    local fill_col = (rem == 1) and 0x4ADE80FF or 0xFACC15FF
    local hi_col   = (rem == 1) and 0xBBF7D0FF or 0xFEF08AFF
    local start_x  = cx - math.floor(((total - 1) * step) / 2)
    for i = 1, rem do
      draw_dot(start_x + (i - 1) * step, cy, fill_col, 0x090D16FF, hi_col)
    end
  end
end

local function on_gui()
  if bridge then
    pcall(bridge.draw, USER_PLAYER)
  end

  draw_pause_or_countdown_overlay()

  -- Compact status footer at bottom-center (clear of Grouflon's top HP/controllers/gauges!)
  local footer_y = 204
  local spd_lbl = SPEED_LABELS[state.speed_idx] or "100%"
  local spd_tag = ""
  if state.speed_idx ~= DEFAULT_SPEED_IDX then
    spd_tag = (state.speed_scope == 2) and (" [" .. spd_lbl .. " TO]") or (" [" .. spd_lbl .. "]")
  end

  if state.sandbox_mode then
    gui.text(4, footer_y, string.format("SANDBOX (P%d)%s | Start Menu | Select Reset | Alt+6/7 Spd", USER_PLAYER, spd_tag), "cyan", "black")
  elseif loaded_count == 0 then
    gui.text(4, footer_y, "WARNING: No .inputs loaded" .. spd_tag .. " | Alt+5 Sandbox | Start Menu", "red", "black")
  elseif state.phase == "paused" then
    gui.text(4, footer_y, string.format("PAUSED (P%d)%s | Any Button Start | Start Menu | Alt+5 Sandbox", USER_PLAYER, spd_tag), "cyan", "black")
  elseif state.phase == "countdown" then
    local rem = math.max(1, math.ceil(state.countdown_frames / FPS))
    gui.text(4, footer_y, string.format("STARTING IN %ds (P%d)%s | Select Reset | Start Menu", rem, USER_PLAYER, spd_tag), "yellow", "black")
  elseif state.watch_only then
    local loop_txt = state.auto_loop and "Loop:ON" or "Loop:OFF"
    gui.text(4, footer_y, "WATCH" .. spd_tag .. " | Select Retry | Alt+2 Takeover | Alt+3 " .. loop_txt .. " | Alt+6/7 Spd", "cyan", "black")
  elseif not state.taken_over then
    local rem = math.max(0, (TAKEOVER_GF - state.last_gf) / 60.0)
    gui.text(4, footer_y, string.format("SETUP P%d in %.1fs%s | Select Retry | Alt+6/7 Spd | Start Menu", USER_PLAYER, rem, spd_tag), "yellow", "black")
  else
    local opp_s = (state.last_gf <= max_gf) and "Replaying" or "Done"
    gui.text(4, footer_y, string.format("TAKEOVER P%d (%s)%s | Select Retry | Alt+6/7 Spd | Start Menu", USER_PLAYER, opp_s, spd_tag), "green", "black")
  end

  if state.speed_idx ~= DEFAULT_SPEED_IDX and not (bridge and bridge.menu_open) then
    local active_pct = math.floor((state.applied_speed * 100.0) + 0.5)
    local badge = string.format("SPD %d%%", active_pct)
    if state.speed_scope == 2 and math.abs(state.applied_speed - 1.0) < 0.01 then
      badge = string.format("SPD 100%% -> %s", spd_lbl)
    end
    gui.text(312, 4, badge, (active_pct < 100) and "orange" or "cyan", "black")
  end

  if state.banner_timer > 0 and state.banner_msg ~= "" then
    gui.text(6, 194, state.banner_msg, "yellow", "black")
  elseif LABEL ~= "" and not (bridge and bridge.menu_open) then
    gui.text(6, 194, LABEL, "white", "black")
  end
end

input.registerhotkey(1, function()
  if state.phase == "paused" then
    state.phase = (state.loop_delay_sec > 0) and "countdown" or "running"
    state.countdown_total_frames = state.loop_delay_sec * FPS
    state.countdown_frames = state.countdown_total_frames
  end
  state.reload_pending = true
  set_banner("RETRYING FROM SAVESTATE", 60)
end)

input.registerhotkey(2, function()
  state.sandbox_mode = false
  state.watch_only = not state.watch_only
  state.reload_pending = true
  set_banner(state.watch_only and "WATCH ORIGINAL MODE" or "TAKEOVER MODE", 90)
end)

input.registerhotkey(3, function()
  state.auto_loop = not state.auto_loop
  set_banner("AUTO-LOOP " .. (state.auto_loop and "ON" or "OFF"), 90)
end)

input.registerhotkey(4, function()
  USER_PLAYER = (USER_PLAYER == 1) and 2 or 1
  state.reload_pending = true
  set_banner("SWAPPED CONTROL TO P" .. tostring(USER_PLAYER), 90)
end)

input.registerhotkey(5, function()
  state.sandbox_mode = not state.sandbox_mode
  if state.sandbox_mode then
    state.watch_only = false
    state.phase = "running"
    state.countdown_frames = 0
    state.taken_over = true
    memory.writebyte(FREEZE_GAME_ADDR, 0x00)
    set_banner("SANDBOX TRAINING MODE (Start = Menu, Select = Reset)", 120)
  else
    state.reload_pending = true
    set_banner("TAKEOVER MODE (Reloading drill savestate)", 90)
  end
  sync_emu_speed()
end)

input.registerhotkey(6, function()
  set_speed_idx(state.speed_idx - 1, true)
end)

input.registerhotkey(7, function()
  set_speed_idx(state.speed_idx + 1, true)
end)

input.registerhotkey(8, function()
  if state.speed_idx == DEFAULT_SPEED_IDX then
    set_speed_idx(state.last_slow_idx, true)
  else
    set_speed_idx(DEFAULT_SPEED_IDX, true)
  end
end)

emu.registerbefore(function()
  pcall(on_before)
end)

emu.registerafter(function()
  pcall(on_after)
end)

gui.register(function()
  pcall(on_gui)
end)
