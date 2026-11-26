-- Milestone 3: Deep-link fast-forward + savestate + native Replay Takeover for fcadefbneo (Lua 5.1)
--
-- Uses deterministic sfiii3nr1 in-game RAM frame counter (`0x02007F00`, `game_frame`) so seeking
-- lands on the exact target frame regardless of GGPO/network buffering ticks.
--
-- Environment variables:
--   FC_TARGET_FRAME    Deterministic game_frame (0x02007F00) at t0 (required, integer)
--   FC_LEAD_FRAMES     Frames before target_frame to snap to normal speed & save state (default: 90 = 1.5s)
--   FC_TAKEOVER_MODE   "takeover" (default) to call emu.takeover() at seek point, or "watch" to keep playing replay
--   FC_SAVESTATE_PATH  Optional disk path to also persist the savestate (.fs)
--   FC_LABEL           Optional short banner text shown on HUD

local GAME_FRAME_ADDR  = 0x02007F00
local P1_LOCKED_ADDR   = 0x020154C6
local P2_LOCKED_ADDR   = 0x020154C8
local MATCH_STATE_ADDR = 0x020154A7
local P1_VALID_PTR     = 0x02068C6C + 0x2A0
local P2_VALID_PTR     = 0x02069104 + 0x2A0

local TARGET_FRAME   = tonumber(os.getenv("FC_TARGET_FRAME")) or 0
local LEAD_FRAMES    = tonumber(os.getenv("FC_LEAD_FRAMES")) or 90
local MODE           = os.getenv("FC_TAKEOVER_MODE") or "takeover"
local SAVESTATE_PATH = os.getenv("FC_SAVESTATE_PATH") or ""
local LABEL          = os.getenv("FC_LABEL") or ""

local SEEK_FRAME = math.max(1, TARGET_FRAME - LEAD_FRAMES)
local MEM_SLOT_ID = 1

local state = {
  phase            = "seeking", -- "seeking" -> "ready"
  match_started    = false,
  turbo_applied    = false,
  state_saved      = false,
  taken_over       = false,
  banner_frames    = 0,
  banner_text      = "",
  reload_pending   = false,
  prev_select_down = false,
  takeover_req     = false,
  last_gf          = 0,
}

local function set_banner(msg, frames)
  state.banner_text = msg
  state.banner_frames = frames or 180
end

local function do_save_state()
  pcall(savestate.save_mem, MEM_SLOT_ID)
  if SAVESTATE_PATH ~= "" then
    pcall(savestate.save, SAVESTATE_PATH)
  end
  state.state_saved = true
end

local function do_takeover()
  if state.taken_over then return end
  local ok = pcall(emu.takeover)
  if ok then
    state.taken_over = true
    set_banner("REPLAY TAKEOVER ACTIVE! (Select to retry)", 240)
  end
end

local function on_frame_before()
  if state.phase ~= "ready" or not state.state_saved then return end
  local live = joypad.get()
  local select_down = (live["P1 Coin"] == true) or (live["P2 Coin"] == true)
  if select_down and not state.prev_select_down then
    state.reload_pending = true
  end
  state.prev_select_down = select_down
  if select_down then
    joypad.set({ ["P1 Coin"] = false, ["P2 Coin"] = false })
  end
end

local function on_frame_after()
  local gf = memory.readdword(GAME_FRAME_ADDR)
  state.last_gf = gf

  if state.banner_frames > 0 then
    state.banner_frames = state.banner_frames - 1
  end

  if state.reload_pending then
    state.reload_pending = false
    if state.state_saved then
      pcall(savestate.load_mem, MEM_SLOT_ID)
      set_banner("RELOADED SAVESTATE (gf" .. tostring(SEEK_FRAME) .. ")", 90)
    end
    return
  end

  if state.takeover_req then
    state.takeover_req = false
    do_takeover()
    return
  end

  if TARGET_FRAME <= 0 then
    return
  end

  if not state.match_started then
    local p1_locked = memory.readbyte(P1_LOCKED_ADDR)
    local p2_locked = memory.readbyte(P2_LOCKED_ADDR)
    local mstate    = memory.readbyte(MATCH_STATE_ADDR)
    if (p1_locked == 0xFF or p2_locked == 0xFF) and mstate == 0x02
       and memory.readdword(P1_VALID_PTR) ~= 0 and memory.readdword(P2_VALID_PTR) ~= 0 then
      state.match_started = true
    else
      if not state.turbo_applied then
        pcall(emu.speedmode, "turbo")
        state.turbo_applied = true
      end
      return
    end
  end

  if state.phase == "seeking" then
    if gf < SEEK_FRAME then
      if not state.turbo_applied then
        pcall(emu.speedmode, "turbo")
        state.turbo_applied = true
      end
    else
      pcall(emu.speedmode, "normal")
      state.turbo_applied = false
      do_save_state()
      state.phase = "ready"

      if MODE == "takeover" then
        do_takeover()
      else
        set_banner("WATCH MODE: target gf" .. tostring(TARGET_FRAME) .. " in " .. tostring(LEAD_FRAMES) .. "f (Alt+2 takeover)", 240)
      end
    end
  end
end

local function on_gui()
  if state.phase == "seeking" and TARGET_FRAME > 0 then
    local pct = math.min(100, math.floor((state.last_gf / math.max(1, SEEK_FRAME)) * 100))
    gui.text(8, 8, string.format("SEEKING (TURBO): gf %d / %d (%d%%)", state.last_gf, SEEK_FRAME, pct), "yellow", "black")
    if LABEL ~= "" then
      gui.text(8, 18, LABEL, "cyan", "black")
    end
  elseif state.banner_frames > 0 and state.banner_text ~= "" then
    gui.text(8, 8, state.banner_text, "green", "black")
    if LABEL ~= "" then
      gui.text(8, 18, LABEL, "cyan", "black")
    end
  end
end

input.registerhotkey(1, function()
  state.reload_pending = true
end)

input.registerhotkey(2, function()
  state.takeover_req = true
end)

emu.registerbefore(function()
  pcall(on_frame_before)
end)

emu.registerafter(function()
  pcall(on_frame_after)
end)

gui.register(function()
  pcall(on_gui)
end)
