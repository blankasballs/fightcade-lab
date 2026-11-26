-- Headless 60fps clip frame dumper + instant .fs savestate generator for fcadefbneo (Lua 5.1)
--
-- Supports BOTH single-target env vars and multi-target single-pass batch capture (`FC_JOBS_FILE`)
-- so multiple moments from the same replay are captured in ONE turbo pass without re-seeking from frame 0.
--
-- Uses deterministic sfiii3nr1 in-game RAM frame counter (`0x02007F00`, `game_frame`) rather than
-- `emu.framecount()` (which drifts with GGPO/network buffering ticks in run.cpp).
--
-- Single-target environment variables (legacy / single-clip mode):
--   FC_TARGET_FRAME    Deterministic game_frame (0x02007F00) at decision point t0
--   FC_PRE_FRAMES      Frames before t0 to start clip (default: 210 = 3.5s)
--   FC_POST_FRAMES     Frames after t0 to end clip (default: 150 = 2.5s)
--   FC_SAVESTATE_LEAD  Frames before t0 to drop the .fs savestate (default: 120 = 2.0s)
--   FC_SAVESTATE_OUT   Optional .fs savestate output path
--   FC_RAW_FRAMES_OUT  Binary output path for concatenated GD frames
--   FC_LABEL           Optional HUD text rendered onto captured frames
--
-- Multi-target batch mode:
--   FC_JOBS_FILE       TSV file with one job per line:
--                      target_frame<TAB>pre_frames<TAB>post_frames<TAB>savestate_lead<TAB>savestate_out<TAB>raw_frames_out<TAB>label

local GAME_FRAME_ADDR = 0x02007F00
local P1_LOCKED_ADDR  = 0x020154C6
local P2_LOCKED_ADDR  = 0x020154C8
local MATCH_STATE_ADDR= 0x020154A7
local P1_VALID_PTR    = 0x02068C6C + 0x2A0
local P2_VALID_PTR    = 0x02069104 + 0x2A0

local JOBS_FILE        = os.getenv("FC_JOBS_FILE") or ""
local OFFLINE_INPUTS   = os.getenv("FC_OFFLINE_INPUTS") or ""
local HIDE_T0_BANNER   = (os.getenv("FC_HIDE_T0_BANNER") == "1")
local WRITE_GF_RANGE   = (os.getenv("FC_WRITE_GF_RANGE") == "1")
local GROUFLON_OVERLAY = (os.getenv("FC_GROUFLON_OVERLAY") == "1")
local OVERLAY_DUMP_OUT = os.getenv("FC_OVERLAY_DUMP_OUT") or ""
local GROUFLON_DIR     = os.getenv("FC_GROUFLON_DIR") or ""
local FFMPEG_EXE       = os.getenv("FC_FFMPEG_EXE") or ""
local VIDEO_SCALE      = tonumber(os.getenv("FC_VIDEO_SCALE")) or 2
local VIDEO_CRF        = tonumber(os.getenv("FC_VIDEO_CRF")) or 26
local VIDEO_GOP        = tonumber(os.getenv("FC_VIDEO_GOP")) or 30
local ERR_PATH         = (JOBS_FILE ~= "" and JOBS_FILE or (os.getenv("FC_RAW_FRAMES_OUT") or "capture_clip")) .. ".err"
local HAS_DIRECT_PIPE  = (type(emu.capture_pipe) == "function") and (FFMPEG_EXE ~= "")

local function build_ffmpeg_pipe_cmd(mp4_path, w, h)
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
    gop = string.format(" -g %d -keyint_min %d -sc_threshold 0", VIDEO_GOP, VIDEO_GOP)
  end
  return string.format(
    '"%s" -y -f rawvideo -vcodec rawvideo -pix_fmt rgb24 -s %dx%d -r 60 -i -%s -c:v libx264 -preset fast -crf %d -pix_fmt yuv420p -aspect 4:3%s -movflags +faststart "%s"',
    FFMPEG_EXE, w, h, vf, VIDEO_CRF, gop, mp4_path
  )
end

-- Ensure sibling Lua helpers (e.g. grouflon_overlay.lua) are importable regardless of cwd
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

local overlay = nil
local overlay_dump_file = nil
if GROUFLON_OVERLAY or OVERLAY_DUMP_OUT ~= "" then
  local ok_req, mod = pcall(require, "grouflon_overlay")
  if ok_req and mod then
    if mod.init(GROUFLON_DIR) then
      overlay = mod
    else
      local ef = io.open(ERR_PATH, "a")
      if ef then ef:write("grouflon_overlay init failed: " .. tostring(mod.error) .. "\n"); ef:close() end
    end
  else
    local ef = io.open(ERR_PATH, "a")
    if ef then ef:write("grouflon_overlay require failed: " .. tostring(mod) .. "\n"); ef:close() end
  end
end
if overlay and OVERLAY_DUMP_OUT ~= "" then
  local of, oerr = io.open(OVERLAY_DUMP_OUT, "w")
  if of then
    overlay_dump_file = of
  else
    local ef = io.open(ERR_PATH, "a")
    if ef then ef:write("Failed to open FC_OVERLAY_DUMP_OUT: " .. tostring(oerr) .. "\n"); ef:close() end
  end
end

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

local offline_by_gf = nil
local offline_max_gf = 0
if OFFLINE_INPUTS ~= "" then
  local inf = io.open(OFFLINE_INPUTS, "r")
  if inf then
    offline_by_gf = {}
    for line in inf:lines() do
      local gf_s, m1_s, m2_s = string.match(line, "^(%d+),(%d+),(%d+)")
      if gf_s then
        local g = tonumber(gf_s)
        offline_by_gf[g] = { p1 = tonumber(m1_s), p2 = tonumber(m2_s) }
        if g > offline_max_gf then offline_max_gf = g end
      end
    end
    inf:close()
  end
end

local function write_mask_to_table(tbl, prefix, mask)
  for i = 1, #BUTTON_BITS do
    local b = BUTTON_BITS[i]
    tbl[prefix .. b[2]] = (bit.band(mask, b[1]) ~= 0)
  end
end

local function log_error(msg)
  local ef = io.open(ERR_PATH, "a")
  if ef then
    ef:write(tostring(msg) .. "\n")
    ef:close()
  end
end

local function split_tabs(line)
  local fields = {}
  local start = 1
  while true do
    local idx = string.find(line, "\t", start, true)
    if not idx then
      fields[#fields + 1] = string.sub(line, start)
      break
    end
    fields[#fields + 1] = string.sub(line, start, idx - 1)
    start = idx + 1
  end
  return fields
end

local function make_job(target_frame, pre_frames, post_frames, savestate_lead, savestate_out, raw_out, label)
  local start_f = math.max(1, target_frame - pre_frames)
  local end_f   = target_frame + post_frames
  local sav_f   = math.max(1, target_frame - savestate_lead)
  local slow_f  = math.max(1, math.min(start_f, sav_f) - 18)
  local raw_s   = raw_out or ""
  local lbl_s   = label or ""
  local is_mp4  = (string.sub(string.lower(raw_s), -4) == ".mp4")
  local use_pipe = HAS_DIRECT_PIPE and is_mp4 and (not GROUFLON_OVERLAY) and HIDE_T0_BANNER and (lbl_s == "")
  return {
    target_frame     = target_frame,
    start_frame      = start_f,
    end_frame        = end_f,
    savestate_frame  = sav_f,
    slowdown_frame   = slow_f,
    savestate_out    = savestate_out or "",
    raw_out          = raw_s,
    label            = lbl_s,
    direct_pipe      = use_pipe,
    pipe_open        = false,
    out_file         = nil,
    savestate_done   = (savestate_out == nil or savestate_out == ""),
    first_captured_gf = -1,
    last_captured_gf = -1,
    frames_written   = 0,
    finished         = false,
  }
end

local jobs = {}

if JOBS_FILE ~= "" then
  local jf, err = io.open(JOBS_FILE, "r")
  if not jf then
    log_error("Failed to open FC_JOBS_FILE '" .. JOBS_FILE .. "': " .. tostring(err))
    os.exit(1)
  end
  for raw_line in jf:lines() do
    local line = string.gsub(raw_line, "[\r\n]+$", "")
    if line ~= "" then
      local f = split_tabs(line)
      local tf = tonumber(f[1]) or 0
      if tf > 0 then
        jobs[#jobs + 1] = make_job(
          tf,
          tonumber(f[2]) or 210,
          tonumber(f[3]) or 150,
          tonumber(f[4]) or 120,
          f[5] or "",
          f[6] or "",
          f[7] or ""
        )
      end
    end
  end
  jf:close()
else
  local tf = tonumber(os.getenv("FC_TARGET_FRAME")) or 0
  if tf > 0 then
    jobs[1] = make_job(
      tf,
      tonumber(os.getenv("FC_PRE_FRAMES")) or 210,
      tonumber(os.getenv("FC_POST_FRAMES")) or 150,
      tonumber(os.getenv("FC_SAVESTATE_LEAD")) or 120,
      os.getenv("FC_SAVESTATE_OUT") or "",
      os.getenv("FC_RAW_FRAMES_OUT") or "",
      os.getenv("FC_LABEL") or ""
    )
  end
end

if #jobs == 0 then
  log_error("No valid clip capture jobs provided")
  os.exit(1)
end

table.sort(jobs, function(a, b) return a.target_frame < b.target_frame end)

local state = {
  speed_mode      = "normal",
  done            = false,
  match_started   = (offline_by_gf ~= nil),
  last_input      = nil,
  last_p1_mask    = 0,
  last_p2_mask    = 0,
  last_dumped_gf  = -1,
}

local function write_gf_sidecar(job)
  if WRITE_GF_RANGE and job.raw_out ~= "" and job.frames_written > 0 then
    local gff = io.open(job.raw_out .. ".gf", "w")
    if gff then
      gff:write(
        tostring(job.first_captured_gf) .. "," ..
        tostring(job.last_captured_gf) .. "," ..
        tostring(job.frames_written) .. "\n"
      )
      gff:close()
    end
  end
end

local function close_job_file(job)
  if job.pipe_open then
    pcall(emu.capture_close)
    job.pipe_open = false
    write_gf_sidecar(job)
  end
  if job.out_file then
    job.out_file:flush()
    job.out_file:close()
    job.out_file = nil
    write_gf_sidecar(job)
  end
end

local function close_all_files()
  for i = 1, #jobs do
    close_job_file(jobs[i])
  end
  if overlay_dump_file then
    overlay_dump_file:flush()
    overlay_dump_file:close()
    overlay_dump_file = nil
  end
end

local function finish(code)
  if state.done then return end
  state.done = true
  close_all_files()
  os.exit(code or 0)
end

local function set_speed(mode)
  if state.speed_mode ~= mode then
    pcall(emu.speedmode, mode)
    state.speed_mode = mode
  end
end

local function read_game_frame()
  return memory.readdword(GAME_FRAME_ADDR)
end

local function is_real_match_active()
  local p1_locked = memory.readbyte(P1_LOCKED_ADDR)
  local p2_locked = memory.readbyte(P2_LOCKED_ADDR)
  local mstate    = memory.readbyte(MATCH_STATE_ADDR)
  if (p1_locked == 0xFF or p2_locked == 0xFF) and mstate == 0x02 then
    if memory.readdword(P1_VALID_PTR) ~= 0 and memory.readdword(P2_VALID_PTR) ~= 0 then
      return true
    end
  end
  return false
end

local function on_frame_after()
  if state.done then return end
  -- Ignore CPS3 BIOS boot & attract-mode demo frames where 0x02007F00 can hold
  -- uninitialized/demo values before resetting to 0 at match start!
  if not state.match_started then
    if is_real_match_active() then
      state.match_started = true
    else
      set_speed("turbo")
      return
    end
  end

  local gf = read_game_frame()

  local any_unfinished = false
  local need_normal = false
  local need_overlay = false
  local need_dump = false

  for i = 1, #jobs do
    local job = jobs[i]
    if not job.finished then
      if not job.savestate_done and gf >= job.savestate_frame then
        local ok, err = pcall(savestate.save, job.savestate_out)
        if not ok then
          log_error("savestate.save failed for gf" .. tostring(job.target_frame) .. ": " .. tostring(err))
        end
        job.savestate_done = true
      end

      local savestate_only = (job.raw_out == "" and OVERLAY_DUMP_OUT == "")
      if (savestate_only and job.savestate_done) or (gf > job.end_frame and job.savestate_done) then
        close_job_file(job)
        job.finished = true
      else
        any_unfinished = true
        if job.direct_pipe then
          if gf >= job.start_frame and gf <= job.end_frame and gf ~= job.last_captured_gf then
            if not job.pipe_open then
              local w = (emu.screenwidth and emu.screenwidth()) or 384
              local h = (emu.screenheight and emu.screenheight()) or 224
              if w <= 0 then w = 384 end
              if h <= 0 then h = 224 end
              local cmd = build_ffmpeg_pipe_cmd(job.raw_out, w, h)
              local ok_p, perr = emu.capture_pipe(cmd)
              if not ok_p then
                log_error("emu.capture_pipe failed: " .. tostring(perr))
                finish(2)
                return
              end
              job.pipe_open = true
            end
            if emu.capture_frame() then
              job.frames_written = job.frames_written + 1
              if job.first_captured_gf < 0 then
                job.first_captured_gf = gf
              end
              job.last_captured_gf = gf
            end
          end
        elseif job.raw_out ~= "" and gf >= job.slowdown_frame then
          need_normal = true
        end
        if (job.raw_out ~= "" or OVERLAY_DUMP_OUT ~= "") and gf >= (job.start_frame - 60) and gf <= job.end_frame then
          need_overlay = true
        end
        if overlay_dump_file and gf >= job.start_frame and gf <= job.end_frame then
          need_dump = true
        end
      end
    end
  end

  if overlay and need_overlay then
    pcall(overlay.step, state.last_input)
    if overlay_dump_file and need_dump and gf ~= state.last_dumped_gf then
      local ok_s, line = pcall(overlay.serialize_frame_tsv, gf, state.last_p1_mask, state.last_p2_mask)
      if ok_s and line then
        overlay_dump_file:write(line)
        state.last_dumped_gf = gf
      end
    end
  end

  if not any_unfinished then
    finish(0)
    return
  end

  if need_normal then
    set_speed("normal")
  else
    set_speed("turbo")
  end
end

local function on_gui()
  if state.done or not state.match_started then return end
  local gf = read_game_frame()

  -- Find active jobs needing frames right now
  local active_jobs = nil
  local primary_label = ""
  local show_t0_banner = false

  for i = 1, #jobs do
    local job = jobs[i]
    if not job.finished and not job.direct_pipe and gf >= job.start_frame and gf <= job.end_frame and gf ~= job.last_captured_gf then
      if job.raw_out ~= "" then
        if not active_jobs then active_jobs = {} end
        active_jobs[#active_jobs + 1] = job
      end
      if primary_label == "" and job.label ~= "" then
        primary_label = job.label
      end
      if not HIDE_T0_BANNER and math.abs(gf - job.target_frame) <= 15 then
        show_t0_banner = true
      end
    end
  end

  if not active_jobs then
    return
  end

  if overlay and GROUFLON_OVERLAY then
    pcall(overlay.draw)
  end

  if primary_label ~= "" then
    gui.text(6, 6, primary_label, "white", "black")
  end
  if show_t0_banner then
    gui.text(6, 16, ">>> DECISION POINT (t0) <<<", "yellow", "black")
  end

  local gd_str = gui.gdscreenshot()
  if gd_str and #gd_str > 11 then
    for i = 1, #active_jobs do
      local job = active_jobs[i]
      if not job.out_file then
        local f, err = io.open(job.raw_out, "wb")
        if not f then
          log_error("Failed to open raw output '" .. job.raw_out .. "': " .. tostring(err))
          finish(2)
          return
        end
        job.out_file = f
      end
      job.out_file:write(gd_str)
      job.frames_written = job.frames_written + 1
      if job.first_captured_gf < 0 then
        job.first_captured_gf = gf
      end
      job.last_captured_gf = gf
    end
  end
end

local function table_to_mask(tbl, prefix)
  local m = 0
  if not tbl then return 0 end
  for i = 1, #BUTTON_BITS do
    local b = BUTTON_BITS[i]
    if tbl[prefix .. b[2]] then
      m = bit.bor(m, b[1])
    end
  end
  return m
end

local function on_frame_before()
  if state.done then return end
  if offline_by_gf then
    local next_gf = read_game_frame() + 1
    local rec = offline_by_gf[next_gf]
    local out_tbl = {}
    if rec then
      write_mask_to_table(out_tbl, "P1", rec.p1)
      write_mask_to_table(out_tbl, "P2", rec.p2)
      state.last_p1_mask = rec.p1
      state.last_p2_mask = rec.p2
    else
      write_mask_to_table(out_tbl, "P1", 0)
      write_mask_to_table(out_tbl, "P2", 0)
      state.last_p1_mask = 0
      state.last_p2_mask = 0
    end
    state.last_input = out_tbl
    joypad.set(out_tbl)
  elseif overlay then
    local live = joypad.get()
    state.last_input = live
    state.last_p1_mask = table_to_mask(live, "P1")
    state.last_p2_mask = table_to_mask(live, "P2")
  end
end

if offline_by_gf or overlay then
  emu.registerbefore(function()
    local ok, err = pcall(on_frame_before)
    if not ok then
      log_error("on_frame_before error: " .. tostring(err))
      finish(5)
    end
  end)
end

emu.registerafter(function()
  local ok, err = pcall(on_frame_after)
  if not ok then
    log_error("on_frame_after error: " .. tostring(err))
    finish(3)
  end
end)

gui.register(function()
  local ok, err = pcall(on_gui)
  if not ok then
    log_error("on_gui error: " .. tostring(err))
    finish(4)
  end
end)

emu.registerexit(function()
  close_all_files()
end)
