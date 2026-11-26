-- Live Manual Interaction Annotator with Multi-Step Rewind (-5f / -20f / -100f) for fcadefbneo (Lua 5.1)
--
-- Watch any Fightcade replay live (quark:stream or offline), rewind/scrub seamlessly, and mark
-- [START .. END] of any interaction to:
--   1) Instantly generate a <1s Replay Takeover savestate (.fs) + recorded inputs (.inputs)
--   2) Save a bookmark in data/selections/bookmarks.jsonl for full-sequence semantic search
--
-- Controls (polled via input.get() so they work during live quark:stream AND while paused!):
--   Space           : Pause / Resume
--   ,  (comma)      : Rewind  -5 frames  (0.08s — pinpoint)
--   .  (period)     : Step    +5 frames  (0.08s — pinpoint)
--   Left Arrow      : Rewind  -20 frames (0.33s — 1 move back)
--   Right Arrow     : Step    +20 frames (0.33s — 1 move forward)
--   - or PageUp     : Rewind  -100 frames (1.67s — full setup back)
--   = or PageDown   : Step    +100 frames (1.67s — jump forward)
--   Tab             : Toggle TURBO fast-forward (skip intros/rounds)
--   [               : Mark START of interaction (saves .fs savestate immediately)
--   ]               : Mark END of interaction (finalizes .fs, .inputs + 90f tail, and bookmark)
--   Backspace       : Cancel current START mark
--
-- Environment variables:
--   FC_QUARK_ID        Fightcade quarkid (required for naming)
--   FC_USER_PLAYER     1 (default) or 2 — focus player perspective
--   FC_SELECTIONS_DIR  Directory to store .fs, .inputs, and bookmarks.jsonl
--   FC_NOTE            Optional note/tag saved on bookmarks created in this session

local ok_map, game_map = pcall(require, "sfiii3nr1_map")
if not ok_map then
  print("ERROR: annotate_replay.lua failed to require sfiii3nr1_map: " .. tostring(game_map))
  return
end

local QUARK_ID       = os.getenv("FC_QUARK_ID") or "unknown"
local USER_PLAYER    = tonumber(os.getenv("FC_USER_PLAYER")) or 1
local SELECTIONS_DIR = os.getenv("FC_SELECTIONS_DIR") or "selections"
local SESSION_NOTE   = os.getenv("FC_NOTE") or ""
local TAIL_FRAMES    = 90 -- 1.5s aftermath inputs appended after END for smooth Replay Takeover

-- Memory-bounded savestate snapshot cadence (~0.85 MB/slot; capped at 900 slots ~750 MB max)
local SNAP_INTERVAL_ACTIVE = 20  -- every 0.33s during active gameplay (<=19f seek latency ~30ms)
local SNAP_INTERVAL_COARSE = 60  -- widen to 1.0s after 550 slots so long FT10/FT15 sets stay RAM-safe
local SNAP_INTERVAL_IDLE   = 120 -- every 2.0s outside active rounds
local MAX_MEM_SNAPS        = 900

local BOOKMARKS_PATH = SELECTIONS_DIR .. "\\bookmarks.jsonl"

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

local function write_mask_to_table(tbl, prefix, mask)
  for i = 1, #BUTTON_BITS do
    local b = BUTTON_BITS[i]
    tbl[prefix .. b[2]] = (bit.band(mask, b[1]) ~= 0)
  end
end

local function json_escape(s)
  s = tostring(s or "")
  s = string.gsub(s, "\\", "\\\\")
  s = string.gsub(s, '"', '\\"')
  s = string.gsub(s, "\r", " ")
  s = string.gsub(s, "\n", " ")
  s = string.gsub(s, "\t", " ")
  return s
end

-- Master recorded input archive indexed by deterministic game_frame (`0x02007F00`)
local inputs_by_gf = {}
-- Sorted list of saved memory savestate game_frames
local saved_gfs = {}
local saved_gf_set = {}

local state = {
  -- Stream & rewind state
  stream_head_gf    = 0,
  prev_raw_gf       = 0,
  last_gf           = 0,
  max_saved_gf      = 0,
  mem_snap_count    = 0,
  is_rewound        = false,
  seek_target_gf    = 0,     -- >0 while fast-seeking to an exact target frame
  pause_after_seek  = false,
  paused            = false,
  turbo             = false,
  speed_mode        = "normal",
  taken_over_eof    = false,
  gui_stall_ticks   = 0,
  last_emulated_gf  = -1,

  -- Manual annotation bookmark state
  marking           = false,
  start_gf          = 0,
  temp_fs_path      = "",
  start_snap        = nil,
  saved_count       = 0,
  tail_writers      = {},    -- {file, last_written_gf, stop_gf}

  -- Input debounce + HUD banner
  prev_keys         = {},
  banner_msg        = "READY: Space Pause | , / Left / - Rewind (-5/-20/-100f) | [ Start | ] End | Tab Turbo",
  banner_color      = "cyan",
  banner_timer      = 360,
}

local function set_speed(mode)
  if state.speed_mode ~= mode then
    pcall(emu.speedmode, mode)
    state.speed_mode = mode
  end
end

local function set_banner(msg, color, frames)
  state.banner_msg = msg
  state.banner_color = color or "green"
  state.banner_timer = frames or 180
end

local function set_paused(want_pause)
  state.paused = want_pause
  if want_pause then
    pcall(emu.pause)
  else
    pcall(emu.unpause)
  end
end

local function close_all_tails()
  for i = 1, #state.tail_writers do
    local tw = state.tail_writers[i]
    if tw.file then
      tw.file:flush()
      tw.file:close()
      tw.file = nil
    end
  end
  state.tail_writers = {}
end

local function find_nearest_snap_le(target_gf)
  local best = nil
  for i = #saved_gfs, 1, -1 do
    if saved_gfs[i] <= target_gf then
      best = saved_gfs[i]
      break
    end
  end
  return best
end

local function trigger_seek_to(target_gf, keep_paused)
  if #saved_gfs == 0 then
    set_banner("No rewind history buffered yet — wait 1 second", "yellow", 120)
    return
  end

  local earliest = saved_gfs[1]
  local latest = math.max(state.last_gf, state.stream_head_gf)
  if target_gf < earliest then target_gf = earliest end
  if latest > 0 and target_gf > latest then target_gf = latest end

  local cur_gf = memory.readdword(game_map.ADDR.frame_number)
  if target_gf == cur_gf then
    return
  end

  local was_paused = state.paused
  local should_pause = (keep_paused ~= nil) and keep_paused or was_paused

  if target_gf < cur_gf then
    -- Rewind: load nearest snapshot strictly BEFORE target_gf when possible so at least 1 normal
    -- frame renders (VidFrame updates screen cleanly even when rewinding while paused!)
    local snap_gf = find_nearest_snap_le(target_gf - 1) or find_nearest_snap_le(target_gf)
    if not snap_gf then
      set_banner("Already at earliest rewind snapshot (gf" .. tostring(earliest) .. ")", "yellow", 120)
      return
    end
    local ok = pcall(savestate.load_mem, snap_gf)
    if not ok then
      set_banner("Rewind load_mem failed at gf" .. tostring(snap_gf), "red", 150)
      return
    end
    state.is_rewound = true
    state.last_gf = memory.readdword(game_map.ADDR.frame_number)
    if state.last_gf < target_gf then
      state.seek_target_gf = target_gf
      state.pause_after_seek = should_pause
      set_speed((target_gf - state.last_gf > 3) and "turbo" or "normal")
      if state.paused then set_paused(false) end
    else
      state.seek_target_gf = 0
      set_speed(state.turbo and "turbo" or "normal")
      if should_pause then set_paused(true) end
    end
    set_banner(string.format("REWIND -> gf%d (%+df)", target_gf, target_gf - cur_gf), "cyan", 90)
  else
    -- Forward step: if a later snapshot exists closer to target_gf, jump straight to it!
    local snap_gf = find_nearest_snap_le(target_gf - 1)
    if snap_gf and snap_gf > cur_gf then
      pcall(savestate.load_mem, snap_gf)
      state.last_gf = memory.readdword(game_map.ADDR.frame_number)
    end
    if state.last_gf < target_gf then
      state.seek_target_gf = target_gf
      state.pause_after_seek = should_pause
      set_speed((target_gf - state.last_gf > 3) and "turbo" or "normal")
      if state.paused then set_paused(false) end
    else
      state.seek_target_gf = 0
      set_speed(state.turbo and "turbo" or "normal")
      if should_pause then set_paused(true) end
    end
    set_banner(string.format("STEP -> gf%d (+%df)", target_gf, target_gf - cur_gf), "cyan", 90)
  end
end

local function begin_mark()
  local gf = memory.readdword(game_map.ADDR.frame_number)
  if gf <= 0 then
    set_banner("Wait for match to load before marking START", "yellow", 120)
    return
  end
  if state.turbo then
    state.turbo = false
    if state.seek_target_gf == 0 then set_speed("normal") end
  end

  -- Clean up any prior uncommitted temp savestate if re-marking START
  if state.marking and state.temp_fs_path ~= "" then
    pcall(os.remove, state.temp_fs_path)
  end

  local tmp_fs = SELECTIONS_DIR .. "\\_tmp_" .. QUARK_ID .. "_gf" .. tostring(gf) .. ".fs"
  local ok, err = pcall(savestate.save, tmp_fs)
  if not ok then
    set_banner("ERROR saving savestate: " .. tostring(err), "red", 240)
    return
  end

  local snap = game_map.read_frame(joypad.get())
  state.marking = true
  state.start_gf = gf
  state.temp_fs_path = tmp_fs
  state.start_snap = {
    p1_char = snap.p1 and snap.p1.char_id or 0,
    p2_char = snap.p2 and snap.p2.char_id or 0,
    p1_sa   = snap.p1 and snap.p1.sa or 0,
    p2_sa   = snap.p2 and snap.p2.sa or 0,
  }
  set_banner("MARKED START @ gf" .. tostring(gf) .. " — Scrub/play forward & press ] to Mark END", "yellow", 240)
  pcall(emu.message, "MARKED START @ gf" .. tostring(gf))
end

local function cancel_mark()
  if not state.marking then
    set_banner("No active START mark to cancel", "white", 90)
    return
  end
  if state.temp_fs_path ~= "" then
    pcall(os.remove, state.temp_fs_path)
  end
  state.marking = false
  state.start_gf = 0
  state.temp_fs_path = ""
  state.start_snap = nil
  set_banner("Cancelled START mark", "white", 120)
end

local function finish_mark()
  if not state.marking then
    set_banner("Press [ to mark START before ] (END)", "yellow", 120)
    return
  end
  local end_gf = memory.readdword(game_map.ADDR.frame_number)
  if end_gf <= state.start_gf + 5 then
    set_banner("END must be after START (gf" .. tostring(state.start_gf) .. ") — step/play forward first", "yellow", 150)
    return
  end

  local sel_id = string.format("%s_p%d_gf%d_%d", QUARK_ID, USER_PLAYER, state.start_gf, end_gf)
  local final_fs = SELECTIONS_DIR .. "\\" .. sel_id .. ".fs"
  local final_inp = SELECTIONS_DIR .. "\\" .. sel_id .. ".inputs"

  pcall(os.remove, final_fs)
  local ok_ren = os.rename(state.temp_fs_path, final_fs)
  if not ok_ren then
    local src = io.open(state.temp_fs_path, "rb")
    local dst = io.open(final_fs, "wb")
    if src and dst then
      dst:write(src:read("*a"))
      src:close()
      dst:close()
      pcall(os.remove, state.temp_fs_path)
    end
  end

  -- Write all recorded inputs we already have from start_gf + 1 through end_gf + TAIL_FRAMES
  local stop_gf = end_gf + TAIL_FRAMES
  local last_written = state.start_gf
  local inf = io.open(final_inp, "w")
  if inf then
    inf:write("game_frame,p1_mask,p2_mask\n")
    for gf = state.start_gf + 1, stop_gf do
      local rec = inputs_by_gf[gf]
      if rec then
        inf:write(string.format("%d,%d,%d\n", gf, rec.p1, rec.p2))
        last_written = gf
      elseif gf > end_gf then
        break
      end
    end
    inf:flush()
    if last_written < stop_gf then
      state.tail_writers[#state.tail_writers + 1] = {
        file = inf,
        last_written_gf = last_written,
        stop_gf = stop_gf,
      }
    else
      inf:close()
    end
  end

  local dur_s = (end_gf - state.start_gf) / 60.0
  local sp = state.start_snap or { p1_char = 0, p2_char = 0, p1_sa = 0, p2_sa = 0 }
  local bf = io.open(BOOKMARKS_PATH, "a")
  if bf then
    local line = string.format(
      '{"id":"%s","quark_id":"%s","perspective":%d,"start_gf":%d,"end_gf":%d,"duration_s":%.2f,"p1_char":%d,"p2_char":%d,"p1_sa":%d,"p2_sa":%d,"savestate":"%s","inputs":"%s","note":"%s","created_at":%d}\n',
      json_escape(sel_id),
      json_escape(QUARK_ID),
      USER_PLAYER,
      state.start_gf,
      end_gf,
      dur_s,
      sp.p1_char,
      sp.p2_char,
      sp.p1_sa,
      sp.p2_sa,
      json_escape(final_fs),
      json_escape(final_inp),
      json_escape(SESSION_NOTE),
      os.time()
    )
    bf:write(line)
    bf:flush()
    bf:close()
  end

  local saved_start_gf = state.start_gf
  state.saved_count = state.saved_count + 1
  state.marking = false
  state.start_gf = 0
  state.temp_fs_path = ""
  state.start_snap = nil

  set_banner(string.format("SAVED #%d: %s (%.1fs) — Ready for Takeover & Search!", state.saved_count, sel_id, dur_s), "green", 240)
  pcall(emu.message, string.format("SAVED #%d: gf%d..gf%d (%.1fs)", state.saved_count, saved_start_gf, end_gf, dur_s))
end

local function poll_keyboard()
  local keys = input.get() or {}
  local function pressed(name)
    return (keys[name] == true) and (state.prev_keys[name] ~= true)
  end

  local cur_gf = memory.readdword(game_map.ADDR.frame_number)

  if pressed("space") then
    state.seek_target_gf = 0
    state.pause_after_seek = false
    set_paused(not state.paused)
    set_banner(state.paused and ("PAUSED @ gf" .. tostring(cur_gf)) or "PLAYING", "cyan", 90)
  elseif pressed("comma") then
    trigger_seek_to(cur_gf - 5)
  elseif pressed("period") then
    trigger_seek_to(cur_gf + 5)
  elseif pressed("left") then
    trigger_seek_to(cur_gf - 20)
  elseif pressed("right") then
    trigger_seek_to(cur_gf + 20)
  elseif pressed("minus") or pressed("pageup") then
    trigger_seek_to(cur_gf - 100)
  elseif pressed("plus") or pressed("pagedown") then
    trigger_seek_to(cur_gf + 100)
  elseif pressed("tab") then
    state.turbo = not state.turbo
    if state.seek_target_gf == 0 then
      set_speed(state.turbo and "turbo" or "normal")
    end
    set_banner(state.turbo and "TURBO FAST-FORWARD ON (Tab = normal)" or "NORMAL SPEED", "cyan", 90)
  elseif pressed("leftbracket") then
    begin_mark()
  elseif pressed("rightbracket") then
    finish_mark()
  elseif pressed("backspace") then
    cancel_mark()
  end

  state.prev_keys = keys
end

local function on_before()
  local cur_gf = memory.readdword(game_map.ADDR.frame_number)
  local live = joypad.get()
  local m1 = game_map.encode_input_mask(live, "P1")
  local m2 = game_map.encode_input_mask(live, "P2")

  -- Track & archive every incoming live quark:stream frame so rewinding NEVER loses future frames
  if not state.taken_over_eof then
    if state.stream_head_gf == 0 then
      if cur_gf > 0 and state.prev_raw_gf > 0 and cur_gf > state.prev_raw_gf then
        state.stream_head_gf = cur_gf + 1
        inputs_by_gf[state.stream_head_gf] = { p1 = m1, p2 = m2 }
      end
      state.prev_raw_gf = cur_gf
    else
      state.stream_head_gf = state.stream_head_gf + 1
      inputs_by_gf[state.stream_head_gf] = { p1 = m1, p2 = m2 }
    end
  end

  local next_gf = cur_gf + 1
  if state.is_rewound or state.taken_over_eof then
    local rec = inputs_by_gf[next_gf]
    if rec then
      local out_tbl = {}
      write_mask_to_table(out_tbl, "P1", rec.p1)
      write_mask_to_table(out_tbl, "P2", rec.p2)
      joypad.set(out_tbl)
    elseif state.taken_over_eof then
      -- Reached end of archived replay after stream EOF: pause cleanly so user can rewind
      set_paused(true)
      set_banner("END OF REPLAY — Use , / Left / - to rewind and annotate!", "yellow", 240)
    end
  end
end

local function on_after()
  local inputs = joypad.get()
  local snap = game_map.read_frame(inputs)
  local gf = snap.game_frame
  state.last_gf = gf
  state.gui_stall_ticks = 0

  -- Ensure current frame's inputs are archived even before stream_head_gf initializes
  if gf > 0 and not inputs_by_gf[gf] then
    local m1 = snap.p1 and snap.p1.input_mask or game_map.encode_input_mask(inputs, "P1")
    local m2 = snap.p2 and snap.p2.input_mask or game_map.encode_input_mask(inputs, "P2")
    inputs_by_gf[gf] = { p1 = m1, p2 = m2 }
  end

  -- Flush any newly arrived stream frames into open post-END tail writers
  local i = 1
  while i <= #state.tail_writers do
    local tw = state.tail_writers[i]
    if tw.file then
      while tw.last_written_gf < tw.stop_gf do
        local nxt = tw.last_written_gf + 1
        local rec = inputs_by_gf[nxt]
        if not rec then break end
        tw.file:write(string.format("%d,%d,%d\n", nxt, rec.p1, rec.p2))
        tw.last_written_gf = nxt
      end
      tw.file:flush()
      if tw.last_written_gf >= tw.stop_gf then
        tw.file:close()
        tw.file = nil
        table.remove(state.tail_writers, i)
      else
        i = i + 1
      end
    else
      table.remove(state.tail_writers, i)
    end
  end

  -- Periodically save compressed in-memory rewind snapshots (strictly monotonic, RAM-bounded)
  if gf > state.max_saved_gf and state.mem_snap_count < MAX_MEM_SNAPS then
    local cadence = SNAP_INTERVAL_IDLE
    if snap.in_match == 1 then
      cadence = (state.mem_snap_count < 550) and SNAP_INTERVAL_ACTIVE or SNAP_INTERVAL_COARSE
    end
    if state.max_saved_gf == 0 or (gf - state.max_saved_gf) >= cadence then
      if not saved_gf_set[gf] then
        local ok = pcall(savestate.save_mem, gf)
        if ok then
          saved_gfs[#saved_gfs + 1] = gf
          saved_gf_set[gf] = true
          state.max_saved_gf = gf
          state.mem_snap_count = state.mem_snap_count + 1
        end
      end
    end
  end

  -- Slow down for the final 2 frames of a seek so VidFrame() renders a crisp frame before pausing
  if state.seek_target_gf > 0 then
    if gf >= state.seek_target_gf then
      state.seek_target_gf = 0
      set_speed(state.turbo and "turbo" or "normal")
      if state.pause_after_seek then
        state.pause_after_seek = false
        set_paused(true)
      end
    elseif (state.seek_target_gf - gf) <= 2 then
      set_speed("normal")
    end
  end

  if state.banner_timer > 0 then
    state.banner_timer = state.banner_timer - 1
  end
end

local function on_gui()
  -- Poll keyboard here so Pause, Rewind (-5/-20/-100f), Step, and Mark [ / ] work EVEN WHILE PAUSED!
  poll_keyboard()

  -- Detect quark:stream EOF stall (emulator stops advancing RunFrame) and seamlessly unlock local rewind!
  if not state.paused and not state.taken_over_eof and state.stream_head_gf > 0 and #saved_gfs > 0 then
    if state.last_gf == state.last_emulated_gf then
      state.gui_stall_ticks = state.gui_stall_ticks + 1
      if state.gui_stall_ticks >= 90 then
        state.taken_over_eof = true
        state.is_rewound = true
        pcall(emu.takeover)
        set_paused(true)
        set_banner("STREAM FINISHED — Paused! Rewind (, / Left / -) & annotate anytime", "yellow", 360)
      end
    else
      state.last_emulated_gf = state.last_gf
      state.gui_stall_ticks = 0
    end
  end

  local gf = memory.readdword(game_map.ADDR.frame_number)
  local mode_tag = state.paused and " [PAUSED]" or (state.turbo and " [TURBO]" or "")
  local buf_s = (#saved_gfs > 0) and math.max(0, (gf - saved_gfs[1]) / 60.0) or 0.0
  local top = string.format(
    "ANNOTATE P%d | gf=%d | Buf=%.0fs | Saved=%d%s",
    USER_PLAYER, gf, buf_s, state.saved_count, mode_tag
  )
  gui.text(6, 6, top, "white", "black")
  gui.text(6, 16, "Space Pause | ,/. -/+5f | Left/Right -/+20f | -/= -/+100f | [ Start | ] End | Tab Turbo", "cyan", "black")

  if state.marking then
    local dur = (gf - state.start_gf) / 60.0
    local col = (dur >= 0) and "yellow" or "red"
    gui.text(6, 26, string.format("[REC] START=gf%d | Span=%+.2fs — Press ] for END (Backspace cancel)", state.start_gf, dur), col, "black")
  elseif state.banner_timer > 0 and state.banner_msg ~= "" then
    gui.text(6, 26, state.banner_msg, state.banner_color, "black")
  end
end

local function log_lua_err(where, err)
  local ef = io.open(SELECTIONS_DIR .. "\\annotate_error.log", "a")
  if ef then
    ef:write(string.format("[%s] %s: %s\n", tostring(os.time()), where, tostring(err)))
    ef:close()
  end
end

emu.registerbefore(function()
  local ok, err = pcall(on_before)
  if not ok then log_lua_err("on_before", err) end
end)

emu.registerafter(function()
  local ok, err = pcall(on_after)
  if not ok then log_lua_err("on_after", err) end
end)

gui.register(function()
  local ok, err = pcall(on_gui)
  if not ok then log_lua_err("on_gui", err) end
end)

emu.registerexit(function()
  close_all_tails()
  if state.marking and state.temp_fs_path ~= "" then
    pcall(os.remove, state.temp_fs_path)
  end
end)
