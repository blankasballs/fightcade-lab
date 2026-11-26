-- Single-pass headless In-Game Select (Coin) Mark Extractor for fcadefbneo (Lua 5.1)
--
-- Scans a Fightcade sfiii3nr1 replay in turbo mode for rising-edge presses of the player's
-- Select (`P1 Coin` or `P2 Coin`) button during active gameplay (or within the post-KO grace
-- window), copies the rolling -6.0s savestate (.fs) and exports deterministic recorded
-- `.inputs` so every marked interaction is immediately ready for <1s Replay Takeover.
--
-- Environment variables:
--   FC_QUARK_ID          Fightcade quarkid (required)
--   FC_USER_PLAYER       1 (default) or 2 — only detects Coin presses on this player's side
--   FC_SELECTIONS_DIR    Output directory for .fs and .inputs files
--   FC_RING_DIR          Temporary directory for rolling ring-buffer .fs files
--   FC_MARKS_OUT         JSONL file where detected marks are appended immediately
--   FC_HEARTBEAT_PATH    Heartbeat file updated every 60 frames so Python detects stream EOF
--   FC_LOOKBACK_FRAMES   Frames before Select press to anchor takeover start (default: 360 = 6.0s)
--   FC_TAIL_FRAMES       Frames after Select press to keep recording inputs (default: 90 = 1.5s)
--   FC_DEBOUNCE_FRAMES   Min frames between distinct marks (default: 150 = 2.5s)
--   FC_POST_ROUND_GRACE  Frames after KO/round end where Select still marks the round (default: 240 = 4.0s)

local ok_map, game_map = pcall(require, "sfiii3nr1_map")
if not ok_map then
  os.exit(1)
end

local QUARK_ID          = os.getenv("FC_QUARK_ID") or "unknown"
local USER_PLAYER       = tonumber(os.getenv("FC_USER_PLAYER")) or 1
local SELECTIONS_DIR    = os.getenv("FC_SELECTIONS_DIR") or "selections"
local RING_DIR          = os.getenv("FC_RING_DIR") or (SELECTIONS_DIR .. "\\_ring")
local MARKS_OUT         = os.getenv("FC_MARKS_OUT") or (SELECTIONS_DIR .. "\\_marks_run.jsonl")
local HEARTBEAT_PATH    = os.getenv("FC_HEARTBEAT_PATH") or ""
local LOOKBACK_FRAMES   = tonumber(os.getenv("FC_LOOKBACK_FRAMES")) or 360
local TAIL_FRAMES       = tonumber(os.getenv("FC_TAIL_FRAMES")) or 90
local DEBOUNCE_FRAMES   = tonumber(os.getenv("FC_DEBOUNCE_FRAMES")) or 150
local POST_ROUND_GRACE  = tonumber(os.getenv("FC_POST_ROUND_GRACE")) or 240
local TAKEOVER_ORIENT_F = tonumber(os.getenv("FC_TAKEOVER_ORIENT_FRAMES")) or 45
local MAX_EMU_FRAMES    = tonumber(os.getenv("FC_MAX_FRAMES")) or 180000
local TEST_MARK_GF      = tonumber(os.getenv("FC_TEST_MARK_GF")) or 0

local RING_INTERVAL     = 45   -- save a ring savestate every 45f (~0.75s) for fast turbo scanning
local RING_SLOTS        = 12   -- 12 * 45f = 540f (~9.0s) rolling history > 6.0s lookback
local COIN_KEY          = (USER_PLAYER == 2) and "P2 Coin" or "P1 Coin"

local function json_escape(s)
  s = tostring(s or "")
  s = string.gsub(s, "\\", "\\\\")
  s = string.gsub(s, '"', '\\"')
  s = string.gsub(s, "\r", " ")
  s = string.gsub(s, "\n", " ")
  s = string.gsub(s, "\t", " ")
  return s
end

local function copy_binary_file(src_path, dst_path)
  local inf = io.open(src_path, "rb")
  if not inf then return false end
  local data = inf:read("*all")
  inf:close()
  if not data or #data < 1024 then return false end
  local outf = io.open(dst_path, "wb")
  if not outf then return false end
  outf:write(data)
  outf:flush()
  outf:close()
  return true
end

local function write_heartbeat(emu_f, gf, marks_n, rounds_n)
  if HEARTBEAT_PATH == "" then return end
  local hf = io.open(HEARTBEAT_PATH, "w")
  if hf then
    hf:write(string.format("%d,%d,%d,%d\n", emu_f, gf, marks_n, rounds_n))
    hf:flush()
    hf:close()
  end
end

-- Rolling disk ring buffer of savestates: ring[slot_idx] = { path, gf, round_id, p1_char, p2_char }
local ring = {}
local next_ring_slot = 1

-- Chronological archive of recorded inputs for exporting .inputs sidecars
local input_hist_gfs = {}
local input_hist_p1  = {}
local input_hist_p2  = {}

local state = {
  emu_frames         = 0,
  match_started      = false,
  turbo_applied      = false,
  round_id           = 0,
  prev_in_match      = 0,
  round_start_gf     = 0,
  last_in_match_gf   = 0,
  out_of_match_run   = 0,
  last_ring_gf       = -9999,
  prev_coin_down     = false,
  last_mark_gf       = -9999,
  mark_count         = 0,
  last_p1_char       = 0,
  last_p2_char       = 0,
  tail_writers       = {}, -- { file, stop_gf, last_gf, pending_flush }
}

local function save_ring_snapshot(gf, round_id, p1_char, p2_char)
  local slot = next_ring_slot
  next_ring_slot = (next_ring_slot % RING_SLOTS) + 1
  local path = string.format("%s\\ring_%02d.fs", RING_DIR, slot)
  local ok = pcall(savestate.save, path)
  if ok then
    ring[slot] = {
      path     = path,
      gf       = gf,
      round_id = round_id,
      p1_char  = p1_char,
      p2_char  = p2_char,
    }
    state.last_ring_gf = gf
    return ring[slot]
  end
  return nil
end

local function find_best_ring_snapshot(want_start_gf, round_id)
  local best_le = nil
  local best_any = nil
  for i = 1, RING_SLOTS do
    local entry = ring[i]
    if entry and entry.round_id == round_id then
      if entry.gf <= want_start_gf then
        if not best_le or entry.gf > best_le.gf then
          best_le = entry
        end
      end
      if not best_any or math.abs(entry.gf - want_start_gf) < math.abs(best_any.gf - want_start_gf) then
        best_any = entry
      end
    end
  end
  if best_le then return best_le end
  if best_any then return best_any end
  -- Fallback: any snapshot closest to want_start_gf
  local fallback = nil
  for i = 1, RING_SLOTS do
    local entry = ring[i]
    if entry then
      if not fallback or math.abs(entry.gf - want_start_gf) < math.abs(fallback.gf - want_start_gf) then
        fallback = entry
      end
    end
  end
  return fallback
end

local function flush_and_close_tails()
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

local function emit_mark(press_gf, frame_data)
  local anchor_gf = (frame_data.in_match == 1) and press_gf or state.last_in_match_gf
  if anchor_gf <= 0 then anchor_gf = press_gf end
  local want_start_gf = math.max(state.round_start_gf, anchor_gf - LOOKBACK_FRAMES)
  local snap = find_best_ring_snapshot(want_start_gf, state.round_id)
  if not snap then return end

  state.mark_count = state.mark_count + 1
  local sel_id = string.format("%s_p%d_sel%d_gf%d_%d", QUARK_ID, USER_PLAYER, state.mark_count, snap.gf, press_gf)
  local fs_dst = string.format("%s\\%s.fs", SELECTIONS_DIR, sel_id)
  local inp_dst = string.format("%s\\%s.inputs", SELECTIONS_DIR, sel_id)

  if not copy_binary_file(snap.path, fs_dst) then
    return
  end

  local inp_file = io.open(inp_dst, "w")
  if inp_file then
    inp_file:write("game_frame,p1_mask,p2_mask\n")
    local last_w = -1
    for i = 1, #input_hist_gfs do
      local g = input_hist_gfs[i]
      if g >= snap.gf and g > last_w then
        inp_file:write(string.format("%d,%d,%d\n", g, input_hist_p1[i], input_hist_p2[i]))
        last_w = g
      end
    end
    inp_file:flush()
    state.tail_writers[#state.tail_writers + 1] = {
      file          = inp_file,
      stop_gf       = press_gf + TAIL_FRAMES,
      last_gf       = last_w,
      pending_flush = 0,
    }
  end

  local p1_c = (frame_data.p1 and frame_data.p1.char_id) or snap.p1_char or state.last_p1_char
  local p2_c = (frame_data.p2 and frame_data.p2.char_id) or snap.p2_char or state.last_p2_char
  local self_c = (USER_PLAYER == 2) and p2_c or p1_c
  local opp_c  = (USER_PLAYER == 2) and p1_c or p2_c
  local takeover_gf = snap.gf + TAKEOVER_ORIENT_F

  local line = string.format(
    '{"id":"%s","source":"in_game_select","status":"pending","quark_id":"%s","perspective":%d,"round_id":%d,"start_gf":%d,"press_gf":%d,"end_gf":%d,"takeover_gf":%d,"lookback_s":%.2f,"self_char":%d,"opp_char":%d,"savestate":"%s","inputs":"%s","note":"In-game Select mark #%d (R%d)"}\n',
    json_escape(sel_id),
    json_escape(QUARK_ID),
    USER_PLAYER,
    state.round_id,
    snap.gf,
    press_gf,
    press_gf,
    takeover_gf,
    (press_gf - snap.gf) / 60.0,
    self_c,
    opp_c,
    json_escape(fs_dst),
    json_escape(inp_dst),
    state.mark_count,
    state.round_id
  )
  local mf = io.open(MARKS_OUT, "a")
  if mf then
    mf:write(line)
    mf:flush()
    mf:close()
  end

  state.last_mark_gf = press_gf
end

local function on_frame_after()
  state.emu_frames = state.emu_frames + 1
  if not state.turbo_applied then
    pcall(emu.speedmode, "turbo")
    state.turbo_applied = true
  end

  local inputs = joypad.get()
  local fd = game_map.read_frame(inputs)
  local gf = fd.game_frame or 0

  if (state.emu_frames % 60) == 0 then
    write_heartbeat(state.emu_frames, gf, state.mark_count, state.round_id)
  end

  if state.emu_frames >= MAX_EMU_FRAMES then
    flush_and_close_tails()
    os.exit(0)
    return
  end

  -- Gate everything behind first real in_match frame (ignores CPS3 boot / attract mode / char select)
  if not state.match_started then
    if fd.in_match == 1 and fd.p1 ~= nil and fd.p2 ~= nil then
      state.match_started = true
    else
      return
    end
  end

  if fd.p1 then state.last_p1_char = fd.p1.char_id end
  if fd.p2 then state.last_p2_char = fd.p2.char_id end

  -- Track rounds
  if fd.in_match == 1 then
    if state.prev_in_match == 0 then
      state.round_id = state.round_id + 1
      state.round_start_gf = gf
      state.last_ring_gf = -9999
    end
    state.last_in_match_gf = gf
    state.out_of_match_run = 0
  else
    state.out_of_match_run = state.out_of_match_run + 1
  end
  state.prev_in_match = fd.in_match

  -- Archive frame inputs (for exact .inputs export)
  local m1 = (fd.p1 and fd.p1.input_mask) or game_map.encode_input_mask(inputs, "P1")
  local m2 = (fd.p2 and fd.p2.input_mask) or game_map.encode_input_mask(inputs, "P2")
  local n_hist = #input_hist_gfs
  if n_hist == 0 or input_hist_gfs[n_hist] ~= gf then
    input_hist_gfs[n_hist + 1] = gf
    input_hist_p1[n_hist + 1]  = m1
    input_hist_p2[n_hist + 1]  = m2
  end

  -- Append to active post-mark tail writers
  if #state.tail_writers > 0 then
    local alive = {}
    for i = 1, #state.tail_writers do
      local tw = state.tail_writers[i]
      if tw.file then
        if gf > tw.last_gf and gf <= tw.stop_gf then
          tw.file:write(string.format("%d,%d,%d\n", gf, m1, m2))
          tw.last_gf = gf
          tw.pending_flush = tw.pending_flush + 1
          if tw.pending_flush >= 15 then
            tw.file:flush()
            tw.pending_flush = 0
          end
        end
        if gf >= tw.stop_gf then
          tw.file:flush()
          tw.file:close()
          tw.file = nil
        else
          alive[#alive + 1] = tw
        end
      end
    end
    state.tail_writers = alive
  end

  -- Save rolling ring savestates during active gameplay
  if fd.in_match == 1 and fd.p1 ~= nil and fd.p2 ~= nil then
    if (gf - state.last_ring_gf) >= RING_INTERVAL then
      save_ring_snapshot(gf, state.round_id, fd.p1.char_id, fd.p2.char_id)
    end
  end

  -- Detect rising edge of player's Select (Coin) button during round or post-KO grace window
  local coin_down = (inputs[COIN_KEY] == true)
  local test_hit = (TEST_MARK_GF > 0 and gf >= TEST_MARK_GF and state.mark_count == 0)
  local eligible = (fd.in_match == 1) or (state.round_id > 0 and state.out_of_match_run <= POST_ROUND_GRACE)
  if ((coin_down and not state.prev_coin_down) or test_hit) and eligible then
    if (gf - state.last_mark_gf) >= DEBOUNCE_FRAMES then
      emit_mark(gf, fd)
      write_heartbeat(state.emu_frames, gf, state.mark_count, state.round_id)
    end
  end
  if TEST_MARK_GF > 0 and state.mark_count > 0 and #state.tail_writers == 0 then
    flush_and_close_tails()
    os.exit(0)
    return
  end
  state.prev_coin_down = coin_down
end

emu.registerafter(function()
  pcall(on_frame_after)
end)

emu.registerexit(function()
  flush_and_close_tails()
end)
