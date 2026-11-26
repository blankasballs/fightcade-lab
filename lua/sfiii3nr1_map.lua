-- Verified Street Fighter III: 3rd Strike (sfiii3nr1) RAM addresses
-- Sourced from Grouflon's 3rd_training_lua (gamestate.lua, draw.lua, memory_adresses.lua)
-- and fbneo-training-mode (games/sfiii3/sfiii3.lua).
-- Strict Lua 5.1 (uses FBNeo's built-in LuaBitOp `bit` library).

local M = {}

M.GAME = "sfiii3nr1"

M.ADDR = {
  frame_number  = 0x02007F00, -- dword
  p1_locked     = 0x020154C6, -- byte (0xFF when locked)
  p2_locked     = 0x020154C8, -- byte (0xFF when locked)
  match_state   = 0x020154A7, -- byte (0x02 = active round)
  timer         = 0x02011377, -- byte (0..99)
  screen_x      = 0x02026CB0, -- signed word
  screen_y      = 0x02026CB4, -- signed word
  proj_index    = 0x02068A96, -- projectile linked list head table (list 3 at +0x6)
  proj_initial  = 0x02028990, -- projectile object pool base

  p1 = {
    base            = 0x02068C6C,
    gauge           = 0x020695B5,
    max_gauge       = 0x020695B3,
    meter_stock     = 0x020695BF,
    max_meter_stock = 0x020695BD,
    selected_sa     = 0x0201138B, -- +1 for SA1/2/3
    stun_max        = 0x020695F7,
    stun_timer      = 0x020695F9, -- stun_max + 0x2
    stun_bar        = 0x020695FD, -- stun_max + 0x6 (dword >> 24)
    combo           = 0x020696C5,
    superfreeze     = 0x02069520,
    score           = 0x020113A2,
  },

  p2 = {
    base            = 0x02069104,
    gauge           = 0x020695E1,
    max_gauge       = 0x020695DF,
    meter_stock     = 0x020695EB,
    max_meter_stock = 0x020695E9,
    selected_sa     = 0x0201138C, -- +1 for SA1/2/3
    stun_max        = 0x0206960B,
    stun_timer      = 0x0206960D, -- stun_max + 0x2
    stun_bar        = 0x02069611, -- stun_max + 0x6 (dword >> 24)
    combo           = 0x0206961D,
    superfreeze     = 0x02069088,
    score           = 0x020113AE,
  },
}

-- Offsets relative to player base (P1: 0x02068C6C, P2: 0x02069104)
M.OFF = {
  flip_x        = 0x0A,  -- signed byte
  recovery_flag = 0x3B,  -- byte
  freeze_frames = 0x45,  -- byte
  pos_x         = 0x64,  -- signed word
  pos_y         = 0x68,  -- signed word
  hp            = 0x9F,  -- byte
  action_id     = 0xAC,  -- dword
  movement_type = 0xAD,  -- byte
  movement_type2= 0xAF,  -- byte
  recovery_time = 0x187, -- byte
  anim_id       = 0x202, -- word
  posture       = 0x20E, -- byte (0x00 stand, 0x20 crouch, 0x26 knockdown, etc.)
  standing_state= 0x297, -- byte
  valid_ptr     = 0x2A0, -- dword (0 when player object not initialized)
  char_id       = 0x3C0, -- word
  being_thrown  = 0x3CF, -- byte
  busy_flag     = 0x3D1, -- word
  blocking_id   = 0x3D3, -- byte
  attacking     = 0x428, -- byte
  input_cap     = 0x46C, -- word
}

-- Input bitmask layout (10 bits, Lua 5.1 safe)
-- bit 0: Up, 1: Down, 2: Left, 3: Right
-- bit 4: LP, 5: MP, 6: HP, 7: LK, 8: MK, 9: HK
function M.encode_input_mask(inputs, prefix)
  local mask = 0
  if inputs[prefix .. " Up"]           then mask = bit.bor(mask, 0x001) end
  if inputs[prefix .. " Down"]         then mask = bit.bor(mask, 0x002) end
  if inputs[prefix .. " Left"]         then mask = bit.bor(mask, 0x004) end
  if inputs[prefix .. " Right"]        then mask = bit.bor(mask, 0x008) end
  if inputs[prefix .. " Weak Punch"]   then mask = bit.bor(mask, 0x010) end
  if inputs[prefix .. " Medium Punch"] then mask = bit.bor(mask, 0x020) end
  if inputs[prefix .. " Strong Punch"] then mask = bit.bor(mask, 0x040) end
  if inputs[prefix .. " Weak Kick"]    then mask = bit.bor(mask, 0x080) end
  if inputs[prefix .. " Medium Kick"]  then mask = bit.bor(mask, 0x100) end
  if inputs[prefix .. " Strong Kick"]  then mask = bit.bor(mask, 0x200) end
  return mask
end

function M.read_player(paddr, inputs, prefix)
  local base = paddr.base
  if memory.readdword(base + M.OFF.valid_ptr) == 0 then
    return nil
  end

  -- In sfiii3nr1, KO sets the unsigned HP byte to 0xFF (255). Clamp >200 to 0.
  local hp = memory.readbyte(base + M.OFF.hp)
  if hp > 200 then hp = 0 end

  return {
    char_id         = memory.readword(base + M.OFF.char_id),
    sa              = memory.readbyte(paddr.selected_sa) + 1,
    x               = memory.readwordsigned(base + M.OFF.pos_x),
    y               = memory.readwordsigned(base + M.OFF.pos_y),
    facing          = memory.readbytesigned(base + M.OFF.flip_x),
    hp              = hp,
    meter_gauge     = memory.readbyte(paddr.gauge),
    meter_max_gauge = memory.readbyte(paddr.max_gauge),
    meter_stock     = memory.readbyte(paddr.meter_stock),
    meter_max_stock = memory.readbyte(paddr.max_meter_stock),
    stun            = bit.rshift(memory.readdword(paddr.stun_bar), 24),
    stun_max        = memory.readbyte(paddr.stun_max),
    action_id       = memory.readdword(base + M.OFF.action_id),
    anim_id         = memory.readword(base + M.OFF.anim_id),
    posture         = memory.readbyte(base + M.OFF.posture),
    attacking       = memory.readbyte(base + M.OFF.attacking),
    blocking        = memory.readbyte(base + M.OFF.blocking_id),
    combo           = memory.readbyte(paddr.combo),
    freeze          = memory.readbyte(base + M.OFF.freeze_frames),
    superfreeze     = memory.readbyte(paddr.superfreeze),
    proj_count      = 0,
    input_mask      = M.encode_input_mask(inputs, prefix),
  }
end

function M.count_projectiles()
  local p1_count = 0
  local p2_count = 0
  local obj_index = memory.readwordsigned(M.ADDR.proj_index + 6) -- list 3 * 2
  local slot = 1
  while slot <= 30 and obj_index ~= -1 do
    local base = M.ADDR.proj_initial + bit.lshift(obj_index, 11)
    if memory.readdword(base + M.OFF.valid_ptr) ~= 0 then
      local emitter = memory.readbyte(base + 0x2) + 1
      if emitter == 1 then
        p1_count = p1_count + 1
      elseif emitter == 2 then
        p2_count = p2_count + 1
      end
    end
    obj_index = memory.readwordsigned(base + 0x1C)
    slot = slot + 1
  end
  return p1_count, p2_count
end

function M.read_frame(inputs)
  local p1_locked   = memory.readbyte(M.ADDR.p1_locked)
  local p2_locked   = memory.readbyte(M.ADDR.p2_locked)
  local match_state = memory.readbyte(M.ADDR.match_state)
  local in_match    = ((p1_locked == 0xFF or p2_locked == 0xFF) and match_state == 0x02)

  local p1 = M.read_player(M.ADDR.p1, inputs, "P1")
  local p2 = M.read_player(M.ADDR.p2, inputs, "P2")
  if p1 ~= nil and p2 ~= nil then
    local c1, c2 = M.count_projectiles()
    p1.proj_count = c1
    p2.proj_count = c2
  end

  return {
    game_frame  = memory.readdword(M.ADDR.frame_number),
    in_match    = in_match and 1 or 0,
    match_state = match_state,
    timer       = memory.readbyte(M.ADDR.timer),
    screen_x    = memory.readwordsigned(M.ADDR.screen_x),
    p1          = p1,
    p2          = p2,
  }
end

return M
