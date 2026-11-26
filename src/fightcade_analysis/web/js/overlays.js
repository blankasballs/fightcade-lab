/* Reusable Client-Side Grouflon <canvas> Overlay Renderer (Studio + Playlist Watch Mode) */

const OV_BOX_STYLES = {
  0: { stroke: "#3b82f6", fill: "rgba(59,130,246,0.10)" },   // vulnerability (blue)
  1: { stroke: "#ef4444", fill: "rgba(239,68,68,0.16)" },    // attack (red)
  2: { stroke: "#22c55e", fill: "rgba(34,197,94,0.10)" },    // throwable (green)
  3: { stroke: "#facc15", fill: "rgba(250,204,21,0.16)" },   // throw (yellow)
  4: { stroke: "#e879f9", fill: "rgba(232,121,249,0.08)" },  // push (magenta)
  5: { stroke: "#22d3ee", fill: "rgba(34,211,238,0.10)" },   // ext. vulnerability (cyan)
};

const DIR_ARROWS = { 1: "↙", 2: "↓", 3: "↘", 4: "←", 5: "·", 6: "→", 7: "↖", 8: "↑", 9: "↗" };

const BTN_DEFS = [
  { bit: 0x010, label: "LP", short: "P", bg: "#2563eb" },
  { bit: 0x020, label: "MP", short: "P", bg: "#eab308" },
  { bit: 0x040, label: "HP", short: "P", bg: "#dc2626" },
  { bit: 0x080, label: "LK", short: "K", bg: "#0891b2" },
  { bit: 0x100, label: "MK", short: "K", bg: "#d97706" },
  { bit: 0x200, label: "HK", short: "K", bg: "#e11d48" },
];

function maskToDir(m) {
  const up = (m & 1) !== 0, dn = (m & 2) !== 0, lf = (m & 4) !== 0, rt = (m & 8) !== 0;
  if (dn) return lf ? 1 : (rt ? 3 : 2);
  if (up) return lf ? 7 : (rt ? 9 : 8);
  return lf ? 4 : (rt ? 6 : 5);
}

function buildInputEvents(frames, maskIdx) {
  const evs = [];
  let prev = -1;
  for (let i = 0; i < frames.length; i++) {
    const m = frames[i][maskIdx] || 0;
    if (m !== prev) {
      evs.push({ f: i, m });
      prev = m;
    }
  }
  return evs;
}

function syncOverlayButtons() {
  document.querySelectorAll(".ov-btn").forEach(btn => {
    const k = btn.dataset.ov;
    btn.classList.toggle("on", Boolean(state.overlayOpts[k]));
  });
}

function toggleOverlayOpt(key) {
  state.overlayOpts[key] = !state.overlayOpts[key];
  try { localStorage.setItem("fc_overlay_opts", JSON.stringify(state.overlayOpts)); } catch (_) {}
  syncOverlayButtons();
  if (typeof updateHudAndTimeline === "function" && curRound()) {
    updateHudAndTimeline();
  }
  if (typeof drawWatchOverlayFrame === "function" && state.watchPlaylistId) {
    drawWatchOverlayFrame();
  }
  if (typeof drawSharedOverlayFrame === "function" && (state.activeView === "shared_moment" || state.activeView === "shared_playlist")) {
    drawSharedOverlayFrame();
  }
}

async function fetchOverlayData(url) {
  if (!url) return null;
  if (state.overlayCache[url]) return state.overlayCache[url];
  const resp = await fetch(url);
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  const data = await resp.json();
  data._p1Ev = buildInputEvents(data.frames || [], 0);
  data._p2Ev = buildInputEvents(data.frames || [], 1);
  state.overlayCache[url] = data;
  return data;
}

function drawTextOutline(c, txt, x, y, fill, align = "left") {
  c.textAlign = align;
  c.lineWidth = 3;
  c.strokeStyle = "rgba(8,12,24,0.92)";
  c.strokeText(txt, x, y);
  c.fillStyle = fill;
  c.fillText(txt, x, y);
}

function drawControllerWidget(c, mask, x, y) {
  const dir = maskToDir(mask);
  c.fillStyle = "rgba(10,14,26,0.72)";
  c.strokeStyle = "rgba(148,163,184,0.45)";
  c.lineWidth = 1.5;
  c.beginPath();
  c.roundRect(x - 2, y - 2, 74, 28, 6);
  c.fill();
  c.stroke();

  c.font = "bold 16px system-ui, sans-serif";
  c.textBaseline = "middle";
  drawTextOutline(c, DIR_ARROWS[dir] || "·", x + 11, y + 12, dir === 5 ? "#64748b" : "#f8fafc", "center");

  for (let i = 0; i < 6; i++) {
    const b = BTN_DEFS[i];
    const on = (mask & b.bit) !== 0;
    const bx = x + 28 + (i % 3) * 14;
    const by = y + (i < 3 ? 6 : 18);
    c.beginPath();
    c.arc(bx, by, 5, 0, Math.PI * 2);
    c.fillStyle = on ? b.bg : "rgba(148,163,184,0.22)";
    c.fill();
    c.strokeStyle = on ? "#ffffff" : "rgba(148,163,184,0.45)";
    c.lineWidth = 1;
    c.stroke();
  }
}

function drawInputHistoryCol(c, events, curFrame, anchorX, startY, isRight) {
  if (!events || !events.length) return;
  let lo = 0, hi = events.length - 1, idx = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (events[mid].f <= curFrame) { idx = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  if (idx < 0) return;

  const maxRows = 13;
  const stepY = 23;
  c.textBaseline = "middle";
  for (let row = 0; row < maxRows && (idx - row) >= 0; row++) {
    const eIdx = idx - row;
    const ev = events[eIdx];
    const nextF = (eIdx + 1 < events.length && events[eIdx + 1].f <= curFrame) ? events[eIdx + 1].f : curFrame;
    const dur = Math.max(1, nextF - ev.f);
    const durTxt = dur < 999 ? String(dur) : "-";
    const dir = maskToDir(ev.m);
    const y = startY + row * stepY;
    const sign = isRight ? -1 : 1;

    c.font = "bold 12px ui-monospace, monospace";
    drawTextOutline(c, durTxt, anchorX + sign * 2, y, "#e2e8f0", isRight ? "right" : "left");

    c.font = "bold 15px system-ui, sans-serif";
    drawTextOutline(c, DIR_ARROWS[dir] || "·", anchorX + sign * 32, y, dir === 5 ? "#94a3b8" : "#ffffff", "center");

    let bx = anchorX + sign * 50;
    for (let bi = 0; bi < 6; bi++) {
      const b = BTN_DEFS[bi];
      if ((ev.m & b.bit) !== 0) {
        c.beginPath();
        c.arc(bx, y, 8, 0, Math.PI * 2);
        c.fillStyle = b.bg;
        c.fill();
        c.strokeStyle = "#0f172a";
        c.lineWidth = 1.5;
        c.stroke();
        c.font = "bold 9px system-ui, sans-serif";
        c.fillStyle = "#ffffff";
        c.textAlign = "center";
        c.fillText(b.short, bx, y + 0.5);
        bx += sign * 18;
      }
    }
  }
}

function drawOverlayOnCanvas(targetCanvas, ov, startGf, frameOff, opts) {
  if (!targetCanvas) return;
  const c = targetCanvas.getContext("2d");
  const W = targetCanvas.width, H = targetCanvas.height;
  c.clearRect(0, 0, W, H);
  if (!ov || !ov.frames || !ov.frames.length) return;
  if (!opts.hitboxes && !opts.inputs && !opts.sticks && !opts.gauges) return;

  const gf = (startGf || 0) + frameOff;
  const idx = Math.max(0, Math.min(ov.frames.length - 1, gf - (ov.start_gf || startGf || 0)));
  const frame = ov.frames[idx];
  if (!frame) return;

  const m1 = frame[0] || 0;
  const m2 = frame[1] || 0;
  const g1 = frame[2] || [];
  const g2 = frame[3] || [];
  const boxes = frame[4] || [];
  const sx = W / (ov.w || 384);
  const sy = H / (ov.h || 224);

  if (opts.hitboxes && boxes.length) {
    c.lineWidth = 2;
    for (let i = 0; i < boxes.length; i++) {
      const b = boxes[i];
      const st = OV_BOX_STYLES[b[0]] || OV_BOX_STYLES[0];
      const x = b[1] * sx, y = b[2] * sy, bw = b[3] * sx, bh = b[4] * sy;
      c.fillStyle = st.fill;
      c.fillRect(x, y, bw, bh);
      c.strokeStyle = st.stroke;
      c.strokeRect(x + 0.5, y + 0.5, bw, bh);
    }
  }

  if (opts.gauges) {
    c.font = "bold 13px ui-monospace, monospace";
    c.textBaseline = "top";
    if (g1.length >= 7) {
      drawTextOutline(c, `${g1[0]}/160`, 13 * sx, 20 * sy, "#fffb63", "left");
      drawTextOutline(c, `${g1[5]}/${g1[6]}`, 118 * sx, 29 * sy, "#f87171", "left");
      drawTextOutline(c, `${g1[1]}/${g1[2]}`, 53 * sx, 211 * sy, "#2dd4bf", "left");
    }
    if (g2.length >= 7) {
      drawTextOutline(c, `${g2[0]}/160`, (383 - 11) * sx, 20 * sy, "#fffb63", "right");
      drawTextOutline(c, `${g2[5]}/${g2[6]}`, (383 - 116) * sx, 29 * sy, "#f87171", "right");
      drawTextOutline(c, `${g2[1]}/${g2[2]}`, (383 - 51) * sx, 211 * sy, "#2dd4bf", "right");
    }
  }

  if (opts.sticks) {
    drawControllerWidget(c, m1, 44 * sx, 34 * sy);
    drawControllerWidget(c, m2, (383 - 78) * sx, 34 * sy);
  }

  if (opts.inputs) {
    drawInputHistoryCol(c, ov._p1Ev, idx, 8, 52 * sy, false);
    drawInputHistoryCol(c, ov._p2Ev, idx, W - 8, 52 * sy, true);
  }
}
