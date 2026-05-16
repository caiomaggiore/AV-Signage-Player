-- AV Signage Player — Clock overlay for mpv
-- Exibido apenas em telas de standby (status screen e background).
-- Opções via --script-opts:
--   clock_overlay-position=<pos>      top-right | top-left | bottom-right | bottom-left
--   clock_overlay-rotation=<degrees>  0 | 90 | 270  (corresponde ao --video-rotate do mpv)

local mp = require 'mp'

local WEEKDAYS = {
  ['Sun'] = 'DOM', ['Mon'] = 'SEG', ['Tue'] = 'TER',
  ['Wed'] = 'QUA', ['Thu'] = 'QUI', ['Fri'] = 'SEX', ['Sat'] = 'SAB'
}

local MARGIN_X = 56   -- margem esquerda/direita (portrait: distância da borda x física)
local MARGIN_Y = 50   -- margem topo/baixo      (portrait: distância da borda y física)

local FS_DATE  = 24
local FS_TIME  = 88
local FS_SECS  = 44
local LINE_GAP = 12

-- ===========================================================================
-- ANÁLISE DE MAPEAMENTO FÍSICO
-- ===========================================================================
-- Para --video-rotate=90  (90° CW visual):
--   physical_x = OSD_y
--   physical_y = rx - OSD_x
--   Texto com \frz270 se estende em +OSD_x a partir da âncora.
--   Para hora acima e gap=LINE_GAP:
--     Posições TOP:    x_time = rx-MARGIN_Y-FS_TIME, x_date = x_time - (FS_DATE+LINE_GAP)
--     Posições BOTTOM: x_date = MARGIN_Y,             x_time = MARGIN_Y+(FS_DATE+LINE_GAP)
--
-- Para --video-rotate=270 (270° CW = 90° CCW visual):
--   physical_x = ry - OSD_y
--   physical_y = OSD_x
--   Texto com \frz90 se estende em +OSD_x a partir da âncora.
--   Para hora acima e gap=LINE_GAP:
--     Posições TOP:    x_time = MARGIN_Y,              x_date = MARGIN_Y+FS_TIME+LINE_GAP
--     Posições BOTTOM: x_date = rx-MARGIN_Y,           x_time = rx-MARGIN_Y-(FS_TIME+LINE_GAP)
-- ===========================================================================

local function get_layout(pos, rx, ry, rotation)

  if rotation == 90 then
    -- physical_x = OSD_y  →  para direita física: OSD_y grande
    -- physical_y = rx-OSD_x  →  para topo físico: OSD_x grande
    -- Texto estende em +OSD_x a partir da âncora (\frz270 = 90°CW visual).
    -- Gap = step - FS_TEXT_DO_SEGUNDO.
    -- Para gap=LINE_GAP com hora acima: step = FS_DATE + LINE_GAP
    local gap_step = FS_DATE + LINE_GAP   -- 36: garante gap=12 entre hora(baixo) e data(cima)
    local frz      = '\\frz270'

    if pos == 'top-right' then
      -- Âncoras: hora começa em physical_y=MARGIN_Y → OSD_x = rx-MARGIN_Y-FS_TIME
      local x_time = rx - MARGIN_Y - FS_TIME
      local x_date = x_time - gap_step
      return { portrait=true, frz=frz, an=3,
               x1=x_time, y1=ry-MARGIN_X,
               x2=x_date, y2=ry-MARGIN_X }
    elseif pos == 'top-left' then
      local x_time = rx - MARGIN_Y - FS_TIME
      local x_date = x_time - gap_step
      return { portrait=true, frz=frz, an=1,
               x1=x_time, y1=MARGIN_X,
               x2=x_date, y2=MARGIN_X }
    elseif pos == 'bottom-right' then
      -- Data na borda inferior → OSD_x=MARGIN_Y; hora acima da data
      local x_date = MARGIN_Y
      local x_time = MARGIN_Y + gap_step
      return { portrait=true, frz=frz, an=3,
               x1=x_time, y1=ry-MARGIN_X,
               x2=x_date, y2=ry-MARGIN_X }
    else -- bottom-left
      local x_date = MARGIN_Y
      local x_time = MARGIN_Y + gap_step
      return { portrait=true, frz=frz, an=1,
               x1=x_time, y1=MARGIN_X,
               x2=x_date, y2=MARGIN_X }
    end

  elseif rotation == 270 then
    -- physical_x = ry-OSD_y  →  para direita física: OSD_y pequeno
    -- physical_y = OSD_x     →  para topo físico:    OSD_x pequeno
    -- \frz90 = 90° CCW visual. Após rotação, text extends em +OSD_x a partir da âncora.
    --
    -- Valores de `an` corretos (anchor no canto físico correto após CCW):
    --   top-right  → an=9  (âncora no top-right FÍSICO do bloco)
    --   top-left   → an=7  (âncora no top-left  FÍSICO do bloco)
    --   bottom-right → an=3 (âncora no bottom-right FÍSICO)
    --   bottom-left  → an=1 (âncora no bottom-left  FÍSICO)
    --
    -- Gap formula:
    --   TOP:    gap = step_top - FS_TIME → step_top = FS_TIME + LINE_GAP = 100
    --   BOTTOM: gap = step_bot - FS_DATE → step_bot = FS_DATE + LINE_GAP = 36
    local step_top = FS_TIME + LINE_GAP   -- 100
    local step_bot = FS_DATE + LINE_GAP   -- 36
    local frz      = '\\frz90'

    if pos == 'top-right' then
      return { portrait=true, frz=frz, an=9,
               x1=MARGIN_Y,           y1=MARGIN_X,
               x2=MARGIN_Y+step_top,  y2=MARGIN_X }
    elseif pos == 'top-left' then
      return { portrait=true, frz=frz, an=7,
               x1=MARGIN_Y,           y1=ry-MARGIN_X,
               x2=MARGIN_Y+step_top,  y2=ry-MARGIN_X }
    elseif pos == 'bottom-right' then
      return { portrait=true, frz=frz, an=3,
               x1=rx-MARGIN_Y-step_bot, y1=MARGIN_X,
               x2=rx-MARGIN_Y,          y2=MARGIN_X }
    else -- bottom-left
      return { portrait=true, frz=frz, an=1,
               x1=rx-MARGIN_Y-step_bot, y1=ry-MARGIN_X,
               x2=rx-MARGIN_Y,          y2=ry-MARGIN_X }
    end

  else
    -- Landscape (rotation=0 ou 180)
    -- an=9/7 âncora no topo → hora acima (y1), data abaixo (y2)
    -- an=3/1 âncora na base → hora acima (y1), data abaixo (y2)
    if pos == 'top-left' then
      return { portrait=false, frz='', an=7,
               x=MARGIN_X,
               y1=MARGIN_Y,
               y2=MARGIN_Y + FS_TIME + LINE_GAP }
    elseif pos == 'top-right' then
      return { portrait=false, frz='', an=9,
               x=rx-MARGIN_X,
               y1=MARGIN_Y,
               y2=MARGIN_Y + FS_TIME + LINE_GAP }
    elseif pos == 'bottom-left' then
      local yd = ry - MARGIN_Y
      return { portrait=false, frz='', an=1,
               x=MARGIN_X,
               y2=yd,
               y1=yd - FS_DATE - LINE_GAP }
    else -- bottom-right
      local yd = ry - MARGIN_Y
      return { portrait=false, frz='', an=3,
               x=rx-MARGIN_X,
               y2=yd,
               y1=yd - FS_DATE - LINE_GAP }
    end
  end
end

-- ---------------------------------------------------------------------------
-- Atualização do relógio
-- ---------------------------------------------------------------------------
local function update_clock()
  local rx, ry = mp.get_osd_size()
  if not rx or rx == 0 then rx, ry = 1920, 1080 end

  local pos      = mp.get_opt('clock_overlay-position') or 'top-right'
  local rotation = tonumber(mp.get_opt('clock_overlay-rotation') or '0') or 0
  local L        = get_layout(pos, rx, ry, rotation)

  local h   = os.date('%H')
  local m   = os.date('%M')
  local s   = os.date('%S')
  local day = WEEKDAYS[os.date('%a')] or os.date('%a'):upper()
  local dt  = os.date('%d/%m/%Y')

  local line_time, line_date

  if L.portrait then
    -- Hora na primeira posição (x1,y1), data na segunda (x2,y2)
    line_time = string.format(
      '{\\an%d\\pos(%d,%d)\\fs%d%s\\fnDejaVu Sans\\b0\\c&H00FFFFFF&\\bord1\\shad4\\fscy100\\fscx100}%s:%s{\\fs%d\\fscy100\\fscx100\\c&HA8A8A8&\\bord0\\shad2}:%s',
      L.an, L.x1, L.y1, FS_TIME, L.frz, h, m, FS_SECS, s
    )
    line_date = string.format(
      '{\\an%d\\pos(%d,%d)\\fs%d%s\\fnDejaVu Sans\\b0\\c&HA8A8A8&\\bord0\\shad2\\fscy100\\fscx100}%s  %s',
      L.an, L.x2, L.y2, FS_DATE, L.frz, day, dt
    )
  else
    -- Landscape: hora em y1 (topo), data em y2 (abaixo)
    line_time = string.format(
      '{\\an%d\\pos(%d,%d)\\fs%d\\fnDejaVu Sans\\b0\\c&H00FFFFFF&\\bord1\\shad4\\fscy115\\fscx100}%s:%s{\\fs%d\\fscy100\\fscx100\\c&HA8A8A8&\\bord0\\shad2}:%s',
      L.an, L.x, L.y1, FS_TIME, h, m, FS_SECS, s
    )
    line_date = string.format(
      '{\\an%d\\pos(%d,%d)\\fs%d\\fnDejaVu Sans\\b0\\c&HA8A8A8&\\bord0\\shad2\\fscy125\\fscx100}%s  %s',
      L.an, L.x, L.y2, FS_DATE, day, dt
    )
  end

  mp.set_osd_ass(rx, ry, line_time .. '\n' .. line_date)
end

mp.add_periodic_timer(1, update_clock)
update_clock()
