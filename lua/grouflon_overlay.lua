-- Read-only Grouflon (3rd_training_lua-master) presentation overlay module (Lua 5.1)
--
-- Loads Grouflon's native hitbox, input history, live controller, and gauge drawing modules
-- WITHOUT modifying any file in `3rd_training_lua-master`, WITHOUT writing to RAM, and
-- WITHOUT registering any hotkeys or `joypad.set` hooks.
--
-- Environment variables:
--   FC_GROUFLON_DIR  Path to `3rd_training_lua-master` (default: "3rd_training_lua-master"
--                    relative to fcadefbneo.exe's working directory)

local M = {
  loaded = false,
  error = nil,
  last_input = nil,
}

local function normalize_dir(p)
  if not p or p == "" then return "3rd_training_lua-master" end
  return (string.gsub(p, "[/\\]+$", ""))
end

function M.init(custom_dir)
  if M.loaded then return true end
  local dir = normalize_dir(custom_dir or os.getenv("FC_GROUFLON_DIR"))

  -- Quick check that Grouflon's startup module exists before loading
  local probe = io.open(dir .. "\\src\\startup.lua", "r")
  if not probe then
    M.error = "3rd_training_lua-master not found at: " .. dir
    return false
  end
  probe:close()

  -- Prepend Grouflon dir to package.path so `require("src/...")` resolves cleanly
  local prefix = dir .. "\\?.lua;"
  if not string.find(package.path, prefix, 1, true) then
    package.path = prefix .. package.path
  end

  local ok, err = pcall(function()
    -- Patch gd.createFromPng so Grouflon's relative `"images/..."` loads regardless of cwd
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
  end)

  if not ok then
    M.error = tostring(err)
    return false
  end

  M.loaded = true
  M.error = nil
  return true
end

function M.reset()
  if not M.loaded then return end
  pcall(clear_input_history)
  pcall(reset_player_objects)
  M.last_input = nil
end

function M.step(input_tbl)
  if not M.loaded then return end
  local inp = input_tbl or joypad.get()
  M.last_input = inp
  draw_read()
  gamestate_read()
  if is_in_match then
    input_history_update(input_history[1], "P1", inp)
    input_history_update(input_history[2], "P2", inp)
  else
    clear_input_history()
  end
end

function M.draw(opts)
  if not M.loaded then return end
  opts = opts or {}
  local show_hitboxes    = (opts.hitboxes ~= false)
  local show_history     = (opts.input_history ~= false)
  local show_controllers = (opts.controllers ~= false)
  local show_gauges      = (opts.gauges ~= false)

  if not is_in_match then
    return
  end

  if show_hitboxes then
    display_draw_hitboxes()
  end

  if show_gauges then
    display_draw_life(player_objects[1])
    display_draw_life(player_objects[2])
    display_draw_meter(player_objects[1])
    display_draw_meter(player_objects[2])
    display_draw_stun_gauge(player_objects[1])
    display_draw_stun_gauge(player_objects[2])
    display_draw_bonuses(player_objects[1])
    display_draw_bonuses(player_objects[2])
  end

  if show_history then
    input_history_draw(input_history[1], 4, 49, false)
    input_history_draw(input_history[2], screen_width - 4, 49, true)
  end

  if show_controllers then
    local inp = M.last_input or joypad.get()
    local p1_entry = make_input_history_entry("P1", inp)
    local p2_entry = make_input_history_entry("P2", inp)
    draw_controller_big(p1_entry, 44, 34)
    draw_controller_big(p2_entry, 310, 34)
  end
end

local BOX_TYPE_CODES = {
  ["vulnerability"]      = 0,
  ["attack"]             = 1,
  ["throwable"]          = 2,
  ["throw"]              = 3,
  ["push"]               = 4,
  ["ext. vulnerability"] = 5,
}

local function append_screen_boxes(out, pos_x, pos_y, flip_x, boxes)
  if not boxes then return end
  local px, py = game_to_screen_space(pos_x, pos_y)
  for i = 1, #boxes do
    local box = boxes[i]
    local tc = BOX_TYPE_CODES[box.type]
    if tc ~= nil then
      local l
      if flip_x == 0 then
        l = px + box.left
      else
        l = px - box.left - box.width
      end
      local b = py - box.bottom
      local t = b - box.height
      out[#out + 1] = string.format("%d,%d,%d,%d,%d", tc, math.floor(l + 0.5), math.floor(t + 0.5), math.floor(box.width + 0.5), math.floor(box.height + 0.5))
    end
  end
end

local function format_player_gauges(p)
  if not p then return "0,0,0,0,0,0,0" end
  local mg = p.meter_gauge or 0
  if p.meter_count and p.max_meter_count and p.meter_count == p.max_meter_count then
    mg = p.max_meter_gauge or mg
  end
  return string.format(
    "%d,%d,%d,%d,%d,%d,%d",
    p.life or 0,
    mg,
    p.max_meter_gauge or 0,
    p.meter_count or 0,
    p.max_meter_count or 0,
    p.stun_bar or 0,
    p.stun_max or 0
  )
end

-- Serialize current frame's Grouflon screen-space hitboxes & gauges to one compact TSV line.
-- Must be called after `M.step(...)` for the current frame.
function M.serialize_frame_tsv(gf, p1_mask, p2_mask)
  if not M.loaded or not is_in_match then
    return string.format("%d\t%d\t%d\t0\t0\t\n", gf or 0, p1_mask or 0, p2_mask or 0)
  end
  local boxes_parts = {}
  if player_objects[1] then
    append_screen_boxes(boxes_parts, player_objects[1].pos_x, player_objects[1].pos_y, player_objects[1].flip_x, player_objects[1].boxes)
  end
  if player_objects[2] then
    append_screen_boxes(boxes_parts, player_objects[2].pos_x, player_objects[2].pos_y, player_objects[2].flip_x, player_objects[2].boxes)
  end
  if projectiles then
    for _, obj in pairs(projectiles) do
      append_screen_boxes(boxes_parts, obj.pos_x, obj.pos_y, obj.flip_x, obj.boxes)
    end
  end
  return string.format(
    "%d\t%d\t%d\t%s\t%s\t%s\n",
    gf or 0,
    p1_mask or 0,
    p2_mask or 0,
    format_player_gauges(player_objects[1]),
    format_player_gauges(player_objects[2]),
    table.concat(boxes_parts, ";")
  )
end

return M
