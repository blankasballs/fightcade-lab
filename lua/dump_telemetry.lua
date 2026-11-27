-- Headless-safe 60 Hz RAM telemetry dumper for fcadefbneo (Lua 5.1)
-- Target v1: sfiii3nr1 (Street Fighter III: 3rd Strike)
--
-- Configuration via environment variables:
--   FC_TELEMETRY_OUT      Output CSV path (default: telemetry_out.csv in script dir)
--   FC_TURBO              "1" (default) to enable emu.speedmode("turbo"), "0" for normal
--   FC_AUTO_EXIT          "1" (default) to call os.exit(0) on replay end / idle timeout
--   FC_MAX_FRAMES         Hard cap on total emulated frames before exit (default: 180000 = 50 min)
--   FC_POST_MATCH_IDLE    Frames out of match after at least 1 round before auto-exit (default: 3600 = 60s)
--   FC_POST_ROUND_TAIL    Extra frames to log after in_match drops 1->0 to capture KO hit HP=0 (default: 15)
--   FC_RECORD_ALL_FRAMES  "1" to log even pre/post-round frames when player structs exist; "0" (default) logs in_match + tail
--   FC_SCOUT_ONLY         "1" to log the first active in_match frame (char_id/SA peek) and exit immediately
--   FC_ROUND_CACHE_DIR    Optional dir to save one .fs savestate at the start of every round (`r01_gfXXXX.fs`)
--                         for instant offline 19x clip rendering with zero second Fightcade stream!
--   FC_HEARTBEAT_PATH     Optional heartbeat file updated every 60 frames (for EOF detection)
--   FC_MARKS_OUT          Optional JSONL output to ALSO extract in-game Select (Coin) marks in the same pass
--                         (requires FC_QUARK_ID, FC_USER_PLAYER, FC_SELECTIONS_DIR, FC_RING_DIR)

local OUT_PATH        = os.getenv("FC_TELEMETRY_OUT") or "telemetry_out.csv"
local ERR_PATH        = OUT_PATH .. ".err"
local USE_TURBO       = (os.getenv("FC_TURBO") or "1") ~= "0"
local AUTO_EXIT       = (os.getenv("FC_AUTO_EXIT") or "1") ~= "0"
local MAX_FRAMES      = tonumber(os.getenv("FC_MAX_FRAMES")) or 180000
local POST_MATCH_IDLE = tonumber(os.getenv("FC_POST_MATCH_IDLE")) or 3600
local POST_ROUND_TAIL = tonumber(os.getenv("FC_POST_ROUND_TAIL")) or 15
local RECORD_ALL      = (os.getenv("FC_RECORD_ALL_FRAMES") or "0") == "1"
local SCOUT_ONLY      = (os.getenv("FC_SCOUT_ONLY") or "0") == "1"
local ROUND_CACHE_DIR = os.getenv("FC_ROUND_CACHE_DIR") or ""
local HEARTBEAT_PATH  = os.getenv("FC_HEARTBEAT_PATH") or ""

-- Optional single-pass mobile MP4 + overlay + silent RAM audio capture (uses C++ emu.capture_pipe -> ffmpeg)
local MOBILE_OUT_DIR  = os.getenv("FC_MOBILE_OUT_DIR") or ""
local FFMPEG_EXE      = os.getenv("FC_FFMPEG_EXE") or ""
local VIDEO_SCALE     = tonumber(os.getenv("FC_VIDEO_SCALE")) or 2
local VIDEO_CRF       = tonumber(os.getenv("FC_VIDEO_CRF")) or 26
local VIDEO_GOP       = tonumber(os.getenv("FC_VIDEO_GOP")) or 30
local VIDEO_ENCODER   = os.getenv("FC_VIDEO_ENCODER") or "libx264:fast"
local FFMPEG_THREADS  = tonumber(os.getenv("FC_FFMPEG_THREADS")) or 0
local CAPTURE_AUDIO   = (os.getenv("FC_CAPTURE_AUDIO") or "1") ~= "0"
local PIPE_TAG        = os.getenv("FC_PIPE_TAG") or tostring(os.time())
local GROUFLON_DIR    = os.getenv("FC_GROUFLON_DIR") or ""
local USE_DISK_RING   = (os.getenv("FC_USE_DISK_RING") == "1") or (ROUND_CACHE_DIR == "")
local SINGLE_PASS_VIDEO = (MOBILE_OUT_DIR ~= "") and (FFMPEG_EXE ~= "") and (type(emu.capture_pipe) == "function")
local HAS_DUAL_PIPE_AV  = CAPTURE_AUDIO and (type(emu.capture_pipe_av) == "function")
local HAS_AUDIO_CAPTURE = CAPTURE_AUDIO and (HAS_DUAL_PIPE_AV or type(emu.capture_audio_open) == "function")
local _pipe_seq = 0

-- Optional single-pass Select (Coin) mark extraction (disabled by default; opt-in via FC_ENABLE_SELECT_MARKS=1)
local ENABLE_SELECT_MARKS = (os.getenv("FC_ENABLE_SELECT_MARKS") == "1")
local MARKS_OUT         = ENABLE_SELECT_MARKS and (os.getenv("FC_MARKS_OUT") or "") or ""
local QUARK_ID          = os.getenv("FC_QUARK_ID") or "unknown"
local USER_PLAYER       = tonumber(os.getenv("FC_USER_PLAYER")) or 1
local SELECTIONS_DIR    = os.getenv("FC_SELECTIONS_DIR") or "selections"
local RING_DIR          = os.getenv("FC_RING_DIR") or ""
local LOOKBACK_FRAMES   = tonumber(os.getenv("FC_LOOKBACK_FRAMES")) or 360
local TAIL_FRAMES       = tonumber(os.getenv("FC_TAIL_FRAMES")) or 90
local DEBOUNCE_FRAMES   = tonumber(os.getenv("FC_DEBOUNCE_FRAMES")) or 150
local POST_ROUND_GRACE  = tonumber(os.getenv("FC_POST_ROUND_GRACE")) or 240
local TAKEOVER_ORIENT_F = tonumber(os.getenv("FC_TAKEOVER_ORIENT_FRAMES")) or 45
local TEST_MARK_GF      = tonumber(os.getenv("FC_TEST_MARK_GF")) or 0
local RING_INTERVAL     = 45
local RING_SLOTS        = 12
local COIN_KEY          = (USER_PLAYER == 2) and "P2 Coin" or "P1 Coin"

local function log_error(msg)
  local ef = io.open(ERR_PATH, "a")
  if ef then
    ef:write(tostring(msg) .. "\n")
    ef:close()
  end
end

-- Load game map safely (FBNeo chdir's to the Lua script's directory before loading)
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

local ok_map, game_map = pcall(require, "sfiii3nr1_map")
if not ok_map then
  log_error("Failed to require sfiii3nr1_map: " .. tostring(game_map))
  if AUTO_EXIT then os.exit(1) end
  return
end

local overlay = nil
if SINGLE_PASS_VIDEO then
  -- Fast native sfiii3nr1 RAM hitbox/gauge extractor (~10x faster, zero PNG/GD boot overhead)
  local ok_fast, fast_ov = pcall(require, "sfiii3nr1_overlay")
  if ok_fast and fast_ov and fast_ov.init() then
    overlay = fast_ov
  elseif GROUFLON_DIR ~= "" then
    local ok_ov, mod = pcall(require, "grouflon_overlay")
    if ok_ov and mod and mod.init(GROUFLON_DIR) then
      overlay = mod
    end
  end
end

local function build_ffmpeg_pipe_cmd(mp4_path, w, h, with_faststart, aud_pipe_name, aud_in_rate)
  local s = (VIDEO_SCALE and VIDEO_SCALE >= 1) and math.floor(VIDEO_SCALE) or 1
  local out_w = w * s
  local out_h = h * s
  out_w = out_w + (out_w % 2)
  out_h = out_h + (out_h % 2)
  local vf = ""
  if out_w ~= w or out_h ~= h then
    vf = string.format(" -vf scale=%d:%d:flags=neighbor", out_w, out_h)
  end
  local gop = ""
  if VIDEO_GOP and VIDEO_GOP > 0 then
    if VIDEO_ENCODER == "h264_nvenc" then
      gop = string.format(" -g %d", VIDEO_GOP)
    else
      gop = string.format(" -g %d -keyint_min %d -sc_threshold 0", VIDEO_GOP, VIDEO_GOP)
    end
  end
  local enc_args = ""
  if VIDEO_ENCODER == "h264_nvenc" then
    enc_args = string.format("-c:v h264_nvenc -preset p4 -rc vbr -cq %d -b:v 0", VIDEO_CRF)
  else
    local preset = string.match(VIDEO_ENCODER, "^libx264:(.+)$") or "fast"
    local thr = (FFMPEG_THREADS and FFMPEG_THREADS > 0) and string.format(" -threads %d", math.floor(FFMPEG_THREADS)) or ""
    enc_args = string.format("-c:v libx264 -preset %s -crf %d%s", preset, VIDEO_CRF, thr)
  end
  local faststart = (with_faststart ~= false) and " -movflags +faststart" or ""
  if aud_pipe_name and aud_pipe_name ~= "" then
    local ar = math.max(8000, tonumber(aud_in_rate) or 44400)
    return string.format(
      '"%s" -y -f rawvideo -vcodec rawvideo -pix_fmt rgb24 -s %dx%d -r 60 -i - -f s16le -ar %d -ac 2 -i "%s" -map 0:v:0 -map 1:a:0%s %s -pix_fmt yuv420p -aspect 4:3%s -c:a aac -b:a 96k -ar 48000 -shortest%s "%s"',
      FFMPEG_EXE, w, h, ar, aud_pipe_name, vf, enc_args, gop, faststart, mp4_path
    )
  end
  return string.format(
    '"%s" -y -f rawvideo -vcodec rawvideo -pix_fmt rgb24 -s %dx%d -r 60 -i -%s %s -pix_fmt yuv420p -aspect 4:3%s%s "%s"',
    FFMPEG_EXE, w, h, vf, enc_args, gop, faststart, mp4_path
  )
end

local CSV_HEADER = table.concat({
  "emu_frame", "game_frame", "round_id", "in_match", "match_state", "timer", "screen_x",
  "p1_char", "p1_sa", "p1_x", "p1_y", "p1_facing", "p1_hp",
  "p1_meter_gauge", "p1_meter_max_gauge", "p1_meter_stock", "p1_meter_max_stock",
  "p1_stun", "p1_stun_max", "p1_action", "p1_anim", "p1_posture",
  "p1_attacking", "p1_blocking", "p1_combo", "p1_freeze", "p1_superfreeze", "p1_proj_count", "p1_input_mask",
  "p2_char", "p2_sa", "p2_x", "p2_y", "p2_facing", "p2_hp",
  "p2_meter_gauge", "p2_meter_max_gauge", "p2_meter_stock", "p2_meter_max_stock",
  "p2_stun", "p2_stun_max", "p2_action", "p2_anim", "p2_posture",
  "p2_attacking", "p2_blocking", "p2_combo", "p2_freeze", "p2_superfreeze", "p2_proj_count", "p2_input_mask",
}, ",") .. "\n"

local out_file = nil
local buffer = {}
local BUFFER_FLUSH_LINES = 60 -- flush every ~1 second of game time so stream EOF never drops tail frames

-- Rolling ring buffer & input archive (used only when MARKS_OUT ~= "")
local ring = {}
local next_ring_slot = 1
local input_hist_gfs = {}
local input_hist_p1  = {}
local input_hist_p2  = {}

local state = {
  total_frames      = 0,
  logged_frames     = 0,
  round_id          = 0,
  prev_in_match     = 0,
  saw_match         = false,
  out_of_match_run  = 0,
  prev_movie_mode   = nil,
  turbo_applied     = false,
  closed            = false,
  round_start_gf    = 0,
  last_in_match_gf  = 0,
  last_ring_gf      = -9999,
  prev_coin_down    = false,
  last_mark_gf      = -9999,
  mark_count        = 0,
  last_p1_char      = 0,
  last_p2_char      = 0,
  tail_writers      = {},
  round_video       = nil,
}

local function close_round_video(wait_proc)
  local rv = state.round_video
  if not rv then return end
  state.round_video = nil
  pcall(emu.capture_close, wait_proc == true)
  if rv.ov_file then
    rv.ov_file:flush()
    rv.ov_file:close()
    rv.ov_file = nil
  end
  -- Flush buffered CSV rows so progressive publishing sees the full completed round immediately
  if out_file and #buffer > 0 then
    out_file:write(table.concat(buffer))
    out_file:flush()
    buffer = {}
  end
  if rv.frames_written > 0 then
    local gff = io.open(rv.gf_path, "w")
    if gff then
      if rv.aud_rate and rv.aud_rate > 0 and rv.aud_spf and rv.aud_spf > 0 then
        gff:write(string.format("%d,%d,%d,%d,%d,%d\n", rv.first_gf, rv.last_gf, rv.frames_written, rv.aud_rate, rv.aud_spf, rv.muxed_inline and 1 or 0))
      else
        gff:write(string.format("%d,%d,%d\n", rv.first_gf, rv.last_gf, rv.frames_written))
      end
      gff:flush()
      gff:close()
    end
  end
end

local function open_round_video(round_id)
  if not SINGLE_PASS_VIDEO then return end
  close_round_video(false)
  local stem = string.format("r%02d", round_id)
  local mp4_path = string.format("%s\\%s.mp4", MOBILE_OUT_DIR, stem)
  local w = (emu.screenwidth and emu.screenwidth()) or 384
  local h = (emu.screenheight and emu.screenheight()) or 224
  if w <= 0 then w = 384 end
  if h <= 0 then h = 224 end

  local aud_rate, aud_spf = 0, 0
  local muxed_inline = false
  local vid_path = mp4_path
  local pcm_path = string.format("%s\\_%s.pcm", MOBILE_OUT_DIR, stem)

  if HAS_DUAL_PIPE_AV then
    _pipe_seq = _pipe_seq + 1
    local pipe_name = string.format("\\\\.\\pipe\\fclab_aud_%s_%d_%d", PIPE_TAG, round_id, _pipe_seq)
    local r_info, spf_info = 44100, 740
    if type(emu.capture_audio_info) == "function" then
      r_info, spf_info = emu.capture_audio_info()
    end
    local cmd = build_ffmpeg_pipe_cmd(mp4_path, w, h, true, pipe_name, (tonumber(spf_info) or 740) * 60)
    local ok_p, p_err, _, r_a, spf_a = emu.capture_pipe_av(cmd, pipe_name)
    if ok_p then
      aud_rate = tonumber(r_a) or tonumber(r_info) or 44100
      aud_spf = tonumber(spf_a) or tonumber(spf_info) or 740
      muxed_inline = true
    else
      log_error("Single-pass emu.capture_pipe_av failed for " .. stem .. ": " .. tostring(p_err))
      return
    end
  else
    vid_path = HAS_AUDIO_CAPTURE and string.format("%s\\_%s_vid.mp4", MOBILE_OUT_DIR, stem) or mp4_path
    local cmd = build_ffmpeg_pipe_cmd(vid_path, w, h, not HAS_AUDIO_CAPTURE)
    local ok_p, perr = emu.capture_pipe(cmd)
    if not ok_p then
      log_error("Single-pass emu.capture_pipe failed for " .. stem .. ": " .. tostring(perr))
      return
    end
    if HAS_AUDIO_CAPTURE then
      local ok_a, r_a, spf_a = emu.capture_audio_open(pcm_path)
      if ok_a then
        aud_rate = tonumber(r_a) or 44100
        aud_spf = tonumber(spf_a) or 740
      else
        log_error("Single-pass emu.capture_audio_open failed for " .. stem .. ": " .. tostring(r_a))
      end
    end
  end
  local ovf = nil
  local ov_path = string.format("%s\\_%s.overlay.tsv", MOBILE_OUT_DIR, stem)
  if overlay then
    ovf = io.open(ov_path, "w")
  end
  state.round_video = {
    round_id = round_id,
    mp4_path = mp4_path,
    vid_path = vid_path,
    pcm_path = pcm_path,
    aud_rate = aud_rate,
    aud_spf = aud_spf,
    muxed_inline = muxed_inline,
    gf_path = mp4_path .. ".gf",
    ov_path = ov_path,
    ov_file = ovf,
    first_gf = -1,
    last_gf = -1,
    frames_written = 0,
  }
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

local function write_heartbeat(emu_f, gf)
  if HEARTBEAT_PATH == "" then return end
  local hf = io.open(HEARTBEAT_PATH, "w")
  if hf then
    hf:write(string.format("%d,%d,%d,%d\n", emu_f, gf, state.mark_count, state.round_id))
    hf:flush()
    hf:close()
  end
end

local function save_ring_snapshot(gf, round_id, p1_char, p2_char)
  if RING_DIR == "" then return nil end
  local slot = next_ring_slot
  next_ring_slot = (next_ring_slot % RING_SLOTS) + 1
  local path = string.format("%s\\ring_%02d.fs", RING_DIR, slot)
  local ok = pcall(savestate.save, path)
  if ok then
    ring[slot] = { path = path, gf = gf, round_id = round_id, p1_char = p1_char, p2_char = p2_char }
    state.last_ring_gf = gf
    return ring[slot]
  end
  return nil
end

local function find_best_ring_snapshot(want_start_gf, round_id)
  local best_le, best_any, fallback = nil, nil, nil
  for i = 1, RING_SLOTS do
    local entry = ring[i]
    if entry then
      if entry.round_id == round_id then
        if entry.gf <= want_start_gf and (not best_le or entry.gf > best_le.gf) then
          best_le = entry
        end
        if not best_any or math.abs(entry.gf - want_start_gf) < math.abs(best_any.gf - want_start_gf) then
          best_any = entry
        end
      end
      if not fallback or math.abs(entry.gf - want_start_gf) < math.abs(fallback.gf - want_start_gf) then
        fallback = entry
      end
    end
  end
  return best_le or best_any or fallback
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

local function emit_select_mark(press_gf, snap_frame)
  local anchor_gf = (snap_frame.in_match == 1) and press_gf or state.last_in_match_gf
  if anchor_gf <= 0 then anchor_gf = press_gf end
  local want_start_gf = math.max(state.round_start_gf, anchor_gf - LOOKBACK_FRAMES)
  local snap = USE_DISK_RING and find_best_ring_snapshot(want_start_gf, state.round_id) or nil

  local start_gf = snap and snap.gf or want_start_gf
  state.mark_count = state.mark_count + 1
  local sel_id = string.format("%s_p%d_sel%d_gf%d_%d", QUARK_ID, USER_PLAYER, state.mark_count, start_gf, press_gf)
  local fs_dst = ""
  local inp_dst = ""
  local needs_bake = false

  if snap then
    fs_dst = string.format("%s\\%s.fs", SELECTIONS_DIR, sel_id)
    inp_dst = string.format("%s\\%s.inputs", SELECTIONS_DIR, sel_id)
    if not copy_binary_file(snap.path, fs_dst) then return end
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
        file = inp_file, stop_gf = press_gf + TAIL_FRAMES, last_gf = last_w, pending_flush = 0,
      }
    end
  elseif ROUND_CACHE_DIR ~= "" then
    -- Fast path: zero disk ring savestates during stream; select_queue.py bakes exact .fs from replay_cache!
    needs_bake = true
  else
    return
  end

  local p1_c = (snap_frame.p1 and snap_frame.p1.char_id) or (snap and snap.p1_char) or state.last_p1_char
  local p2_c = (snap_frame.p2 and snap_frame.p2.char_id) or (snap and snap.p2_char) or state.last_p2_char
  local self_c = (USER_PLAYER == 2) and p2_c or p1_c
  local opp_c  = (USER_PLAYER == 2) and p1_c or p2_c
  local takeover_gf = start_gf + TAKEOVER_ORIENT_F

  local line = string.format(
    '{"id":"%s","source":"in_game_select","status":"pending","quark_id":"%s","perspective":%d,"round_id":%d,"start_gf":%d,"press_gf":%d,"end_gf":%d,"takeover_gf":%d,"lookback_s":%.2f,"self_char":%d,"opp_char":%d,"savestate":"%s","inputs":"%s","needs_bake":%s,"note":"In-game Select mark #%d (R%d)"}\n',
    json_escape(sel_id), json_escape(QUARK_ID), USER_PLAYER, state.round_id,
    start_gf, press_gf, press_gf, takeover_gf, (press_gf - start_gf) / 60.0,
    self_c, opp_c, json_escape(fs_dst), json_escape(inp_dst), needs_bake and "true" or "false", state.mark_count, state.round_id
  )
  local mf = io.open(MARKS_OUT, "a")
  if mf then
    mf:write(line)
    mf:flush()
    mf:close()
  end
  state.last_mark_gf = press_gf
end

local function flush_buffer()
  if out_file and #buffer > 0 then
    out_file:write(table.concat(buffer))
    out_file:flush()
    buffer = {}
  end
end

local function close_output()
  if state.closed then return end
  state.closed = true
  close_round_video(true)
  if type(emu.capture_wait_all) == "function" then
    pcall(emu.capture_wait_all)
  end
  flush_and_close_tails()
  flush_buffer()
  if out_file then
    out_file:flush()
    out_file:close()
    out_file = nil
  end
end

local function finish_and_exit(code)
  close_output()
  if AUTO_EXIT then
    os.exit(code or 0)
  end
end

local function init_output()
  local f, err = io.open(OUT_PATH, "w")
  if not f then
    log_error("Failed to open output CSV '" .. OUT_PATH .. "': " .. tostring(err))
    if AUTO_EXIT then os.exit(2) end
    return false
  end
  out_file = f
  out_file:write(CSV_HEADER)
  return true
end

local function on_frame_after()
  if state.closed then return end

  if USE_TURBO and not state.turbo_applied then
    pcall(emu.speedmode, "turbo")
    state.turbo_applied = true
  end

  state.total_frames = state.total_frames + 1

  local inputs = joypad.get()
  local snap = game_map.read_frame(inputs)
  local gf = snap.game_frame or 0

  if HEARTBEAT_PATH ~= "" and (state.total_frames % 60) == 0 then
    write_heartbeat(state.total_frames, gf)
  end

  -- Detect round start (0 -> 1 rising edge of in_match)
  if snap.in_match == 1 and state.prev_in_match == 0 then
    close_round_video()
    state.round_id = state.round_id + 1
    state.saw_match = true
    state.round_start_gf = gf
    state.last_ring_gf = -9999
    if ROUND_CACHE_DIR ~= "" and snap.p1 ~= nil and snap.p2 ~= nil then
      local rfs = string.format("%s\\r%02d_gf%d.fs", ROUND_CACHE_DIR, state.round_id, gf)
      pcall(savestate.save, rfs)
    end
    if SINGLE_PASS_VIDEO and snap.p1 ~= nil and snap.p2 ~= nil then
      open_round_video(state.round_id)
    end
    write_heartbeat(state.total_frames, gf)
  end
  state.prev_in_match = snap.in_match

  if snap.p1 then state.last_p1_char = snap.p1.char_id end
  if snap.p2 then state.last_p2_char = snap.p2.char_id end

  if snap.in_match == 1 then
    state.out_of_match_run = 0
    state.last_in_match_gf = gf
  elseif state.saw_match then
    state.out_of_match_run = state.out_of_match_run + 1
  end

  -- Optional single-pass Select (Coin) mark extraction
  if MARKS_OUT ~= "" and state.saw_match then
    local m1 = (snap.p1 and snap.p1.input_mask) or game_map.encode_input_mask(inputs, "P1")
    local m2 = (snap.p2 and snap.p2.input_mask) or game_map.encode_input_mask(inputs, "P2")
    local n_hist = #input_hist_gfs
    if n_hist == 0 or input_hist_gfs[n_hist] ~= gf then
      input_hist_gfs[n_hist + 1] = gf
      input_hist_p1[n_hist + 1]  = m1
      input_hist_p2[n_hist + 1]  = m2
    end
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
    if USE_DISK_RING and snap.in_match == 1 and snap.p1 ~= nil and snap.p2 ~= nil and (gf - state.last_ring_gf) >= RING_INTERVAL then
      save_ring_snapshot(gf, state.round_id, snap.p1.char_id, snap.p2.char_id)
    end
    local coin_down = (inputs[COIN_KEY] == true)
    local test_hit = (TEST_MARK_GF > 0 and gf >= TEST_MARK_GF and state.mark_count == 0)
    local eligible = (snap.in_match == 1) or (state.round_id > 0 and state.out_of_match_run <= POST_ROUND_GRACE)
    if ENABLE_SELECT_MARKS and ((coin_down and not state.prev_coin_down) or test_hit) and eligible then
      if (gf - state.last_mark_gf) >= DEBOUNCE_FRAMES then
        emit_select_mark(gf, snap)
        write_heartbeat(state.total_frames, gf)
      end
    end
    state.prev_coin_down = coin_down
  end

  local in_tail = state.saw_match and state.out_of_match_run > 0 and state.out_of_match_run <= POST_ROUND_TAIL
  local should_log = (snap.p1 ~= nil and snap.p2 ~= nil) and (RECORD_ALL or snap.in_match == 1 or in_tail)
  if should_log then
    local p1 = snap.p1
    local p2 = snap.p2
    local emu_frame = emu.framecount()
    local line = string.format(
      "%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n",
      emu_frame, snap.game_frame, state.round_id, snap.in_match, snap.match_state, snap.timer, snap.screen_x,
      p1.char_id, p1.sa, p1.x, p1.y, p1.facing, p1.hp,
      p1.meter_gauge, p1.meter_max_gauge, p1.meter_stock, p1.meter_max_stock,
      p1.stun, p1.stun_max, p1.action_id, p1.anim_id, p1.posture,
      p1.attacking, p1.blocking, p1.combo, p1.freeze, p1.superfreeze, p1.proj_count, p1.input_mask,
      p2.char_id, p2.sa, p2.x, p2.y, p2.facing, p2.hp,
      p2.meter_gauge, p2.meter_max_gauge, p2.meter_stock, p2.meter_max_stock,
      p2.stun, p2.stun_max, p2.action_id, p2.anim_id, p2.posture,
      p2.attacking, p2.blocking, p2.combo, p2.freeze, p2.superfreeze, p2.proj_count, p2.input_mask
    )
    buffer[#buffer + 1] = line
    state.logged_frames = state.logged_frames + 1
    if state.round_video ~= nil then
      local rv = state.round_video
      if overlay then
        pcall(overlay.step, inputs)
      end
      if gf ~= rv.last_gf then
        if emu.capture_frame() then
          rv.frames_written = rv.frames_written + 1
          if rv.first_gf < 0 then
            rv.first_gf = gf
          end
          rv.last_gf = gf
        end
        if overlay and rv.ov_file then
          local ok_s, ov_line = pcall(overlay.serialize_frame_tsv, gf, p1.input_mask, p2.input_mask)
          if ok_s and ov_line then
            rv.ov_file:write(ov_line)
          end
        end
      end
    end
    if SCOUT_ONLY and snap.in_match == 1 then
      finish_and_exit(0)
      return
    end
    if #buffer >= BUFFER_FLUSH_LINES or (in_tail and state.out_of_match_run == POST_ROUND_TAIL) then
      flush_buffer()
    end
    if in_tail and state.out_of_match_run == POST_ROUND_TAIL then
      close_round_video(false)
    end
  elseif state.round_video ~= nil and state.out_of_match_run > POST_ROUND_TAIL then
    close_round_video(false)
  end

  -- Auto-exit conditions:
  -- 1. Local .fr replay ended (movie.mode() transitioned from "playback" to nil)
  local cur_movie_mode = movie.mode()
  if state.prev_movie_mode == "playback" and cur_movie_mode == nil then
    finish_and_exit(0)
    return
  end
  state.prev_movie_mode = cur_movie_mode

  -- 2. Post-match idle timeout (handles quark:stream / quark:replay where emulator remains running after match)
  if state.saw_match and state.out_of_match_run >= POST_MATCH_IDLE then
    finish_and_exit(0)
    return
  end

  -- 3. Hard frame cap safety net
  if state.total_frames >= MAX_FRAMES then
    finish_and_exit(0)
    return
  end
end

local function safe_after()
  local ok, err = pcall(on_frame_after)
  if not ok then
    log_error("Runtime error at frame " .. tostring(state.total_frames) .. ": " .. tostring(err))
    finish_and_exit(3)
  end
end

if init_output() then
  emu.registerafter(safe_after)
  emu.registerexit(function()
    pcall(close_output)
  end)
end
