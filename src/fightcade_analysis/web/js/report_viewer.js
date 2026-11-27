/* Native Fightcade Lab Report Viewer (#report/<id>)
 * Onboards all individual report types (Verified Habits, Hybrid/Legacy Match Breakdowns,
 * Sequence Searches, and Legacy v1 Habit Rollups) directly into the unified dark/light UX
 * with 1-click Takeover, + Add to Playlist, + Pin as Focus Goal, and Open in Studio.
 */

let _activeReportDoc = null;

async function openReport(reportId, returnView = null, opts = {}) {
  if (typeof video !== "undefined" && video) video.pause();
  if (typeof stopMiniPreview === "function") stopMiniPreview();
  const cleanId = String(reportId || "").replace(/^\/?#?report\//, "").replace(/^\/?reports\//, "").replace(/\.html$/i, "");
  if (!cleanId) return;

  if (returnView) {
    state.returnView = returnView;
  } else if (state.activeView && state.activeView !== "report") {
    state.returnView = state.activeView;
  }

  state.activeView = "report";
  const targetUrl = `/#report/${encodeURIComponent(cleanId)}`;
  if (opts.updateHistory !== false && !state._routingFromPopstate && typeof updateAppHistory === "function") {
    updateAppHistory(targetUrl);
  }
  syncTopNav();

  const host = document.getElementById("reportViewerContent");
  const crumb = document.getElementById("crumbMatchup");
  if (crumb) crumb.textContent = "Loading report...";
  if (host) host.innerHTML = `<div class="card empty-card">Loading report...</div>`;

  try {
    const res = await api(`/api/insights/${encodeURIComponent(cleanId)}`);
    _activeReportDoc = res.report;
    if (crumb) {
      crumb.innerHTML = `<span class="pill cached" style="margin-right:6px;">${esc(_activeReportDoc.type_label || "Report")}</span> ${esc(_activeReportDoc.title || cleanId)}`;
    }
    renderReportViewer(_activeReportDoc);
    window.scrollTo({ top: 0, behavior: "instant" });
  } catch (err) {
    if (host) host.innerHTML = `<div class="card empty-card">Could not load report: ${esc(err.message)}</div>`;
  }
}

function _videoTagHtml(clipUrl) {
  if (!clipUrl) {
    return `<div style="aspect-ratio:4/3; border-radius:8px; background:var(--surface-inset); border:1px dashed var(--border); display:flex; align-items:center; justify-content:center; color:var(--muted); font-size:12px;">Clip not rendered locally</div>`;
  }
  return `<video controls playsinline muted preload="metadata" src="${esc(clipUrl)}"></video>`;
}

function _openReportMomentInStudio(qid, persp, roundId, gf) {
  state.returnView = "insights";
  openMatch(qid, persp || 1, { round_id: roundId || 1, anchor_gf: gf || 0, autoPlay: false });
}

async function _pinReportHabitAsGoal(habitIdx, btnEl = null) {
  const doc = _activeReportDoc;
  if (!doc || !doc.habits || !doc.habits[habitIdx]) return;
  if (getActiveFocuses().length >= 3) {
    toast("Max 3 Active Focuses — graduate or unpin one first.");
    return;
  }
  const h = doc.habits[habitIdx];
  const title = h.label || h.interaction || "Recurring Habit";
  const firstEx = (h.examples || [])[0] || {};
  const firstVt = firstEx.visual_tag || {};
  const desc = firstVt.self_mistake || firstVt.what_happened ||
    (h.total_hp_lost ? `Recurring leak across ${h.sets_affected}/${h.total_sets} sets (-${h.total_hp_lost} HP).` : `Recurring leak (${h.occurrences || 0} occurrences).`);

  let seedMoments = Array.isArray(h.seed_moments) && h.seed_moments.length ? h.seed_moments : [];
  if (!seedMoments.length && Array.isArray(h.examples) && h.examples.length) {
    seedMoments = h.examples.slice(0, 6).filter(ep => ep.quark_id && (ep.t_cause_gf || ep.game_frame)).map(ep => {
      const gf = Number(ep.t_cause_gf || ep.game_frame || 0);
      const vt = ep.visual_tag || {};
      return {
        item_id: `rep_${ep.quark_id}_gf${gf}`,
        source: "moment",
        quark_id: ep.quark_id,
        round_id: Number(ep.round_id || 0),
        perspective: Number(ep.perspective || 1),
        start_gf: Math.max(1, gf - 180),
        end_gf: gf + 90,
        anchor_gf: gf,
        label: `${title}: ${vt.opp_move || ep.opp_char_name || "Clip"}`.slice(0, 120),
        note: vt.self_mistake || vt.what_happened || "",
        tags: [title],
      };
    });
  }

  if (btnEl) { btnEl.disabled = true; btnEl.textContent = "Pinning..."; }
  try {
    const res = await api("/api/focus-goals", {
      method: "POST",
      body: JSON.stringify({
        owner: state.username || DEFAULT_USER,
        title: `Fix: ${title}`,
        description: desc,
        tags: [title],
        trend_hint: "From AI Checkup",
        source_insight_id: h.source_insight_id || `${doc.id}:${h.category || habitIdx}`,
        items: seedMoments,
      }),
    });
    const cnt = Number((res.goal && res.goal.item_count) ?? seedMoments.length);
    toast(`Pinned Active Focus "${res.goal.title}" with ${cnt} clip${cnt === 1 ? "" : "s"} ready to Watch & Train!`);
    if (btnEl) btnEl.textContent = "✓ Pinned";
    await Promise.all([loadPlaylists(false), loadFocusGoals()]);
  } catch (err) {
    toast(err.message);
    if (btnEl) { btnEl.disabled = false; btnEl.textContent = "+ Pin as Focus Goal"; }
  }
}

function _renderClipActions(opts) {
  const { qid, gf, roundId, persp, label, note, source, hasStudio, isPro } = opts;
  const startGf = Math.max(1, (Number(gf) || 0) - 180);
  const endGf = (Number(gf) || 0) + 90;
  const pracBtn = canTakeoverNow()
    ? `<button class="sm ${isPro ? "emerald" : "amber"} practice-btn" data-tip="Launch Replay Takeover in FBNeo for this clip" onclick="launchTakeoverPayload({quark_id:${escJsAttr(qid)},frame:${Number(gf) || 0},start_gf:${startGf},end_gf:${endGf},player:${Number(persp) || 1},bake_if_missing:true,label:${escJsAttr((label || "").slice(0, 90))},note:${escJsAttr((note || "").slice(0, 600))}}, this)">${isPro ? "▶ Practice Top-Rank Clip" : "▶ Practice Takeover"}</button>`
    : "";
  const studioBtn = (!isPro && hasStudio)
    ? `<button class="sm" onclick="_openReportMomentInStudio(${escJsAttr(qid)},${Number(persp) || 1},${Number(roundId) || 1},${Number(gf) || 0})">Open in Studio →</button>`
    : "";
  const plItemId = `${isPro ? "pro" : "rep"}_${qid}_gf${Number(gf) || 0}`;
  const addPlBtn = `<button class="sm" onclick="openAddToPlaylistModal({item_id:${escJsAttr(plItemId)},source:${escJsAttr(source || (isPro ? "pro_clip" : "moment"))},quark_id:${escJsAttr(qid)},round_id:${Number(roundId) || 0},perspective:${Number(persp) || 1},start_gf:${startGf},end_gf:${endGf},anchor_gf:${Number(gf) || 0},label:${escJsAttr((label || "").slice(0, 120))},note:${escJsAttr((note || "").slice(0, 300))}})">+ Playlist</button>`;
  return `<div class="row" style="gap:6px; margin-top:auto; padding-top:4px;">${pracBtn}${addPlBtn}${studioBtn}</div>`;
}

function _renderAiSynthesisBlock(synth) {
  if (!synth || (!synth.headline && !(synth.top_priorities || []).length)) return "";
  const priorities = (synth.top_priorities || []).map((p, idx) => `
    <div class="priority-card">
      <div class="row-between">
        <strong style="font-size:13.5px; color:var(--emerald-fg);">#${p.habit_rank || (idx + 1)} ${esc(p.title || "")}</strong>
      </div>
      ${p.diagnosis ? `<div style="font-size:12.5px; color:var(--text-body);">${esc(p.diagnosis)}</div>` : ""}
      ${p.drill ? `<div style="font-size:12px; color:var(--muted); border-top:1px solid var(--border-soft); padding-top:6px;"><strong>Practice:</strong> ${esc(p.drill)}</div>` : ""}
    </div>
  `).join("");
  return `
    <div class="report-synth-card">
      <div class="row-between" style="margin-bottom:6px;">
        <strong style="font-size:15px;">✦ AI Coach Synthesis</strong>
        ${synth.model ? `<span class="pill ready mono">${esc(synth.model)}</span>` : ""}
      </div>
      ${synth.headline ? `<div style="font-size:14.5px; font-weight:700; color:var(--text);">${esc(synth.headline)}</div>` : ""}
      ${priorities ? `<div class="priority-grid">${priorities}</div>` : ""}
      ${synth.next_training_partner_focus ? `<div style="font-size:12.5px; color:var(--muted); margin-top:10px;"><strong>Next Focus:</strong> ${esc(synth.next_training_partner_focus)}</div>` : ""}
    </div>
  `;
}

function _renderVerifiedHabitsReport(doc) {
  const habitsHtml = (doc.habits || []).map((h, idx) => {
    const opps = (h.top_opponents || []).map(o => `<span class="pill">vs ${esc(o.char)} (${o.count})</span>`).join("");
    const moves = (h.common_opp_moves || []).slice(0, 4).map(m => `<span class="pill err">${esc(m.move)}</span>`).join("");

    const exCards = (h.examples || []).map(ep => {
      const vt = ep.visual_tag || {};
      const muHtml = formatMatchLabelHtml(ep, { defaultSelfName: doc.username || state.username });
      const muText = formatMatchLabelText(ep, { defaultSelfName: doc.username || state.username, showRank: false });
      return `
        <div class="report-clip-card">
          <div class="row-between">
            <span class="pill err">-${ep.hp_lost || 0} HP (${ep.hp_lost_pct || 0}%)</span>
            <span class="pill cached mono">${esc(ep.match_date || "")} · R${ep.round_id || "?"}</span>
          </div>
          <div style="font-weight:700; font-size:13px;">${muHtml}</div>
          ${_videoTagHtml(ep.clip_url)}
          ${vt.what_happened ? `<div class="report-callout">${esc(vt.what_happened)}</div>` : ""}
          ${vt.self_mistake ? `<div style="font-size:12px; color:var(--muted);"><strong>Mistake:</strong> ${esc(vt.self_mistake)}</div>` : ""}
          ${_renderClipActions({
            qid: ep.quark_id,
            gf: ep.t_cause_gf || ep.game_frame,
            roundId: ep.round_id,
            persp: ep.perspective,
            label: `${h.label}: ${vt.opp_move || muText}`,
            note: vt.self_mistake || vt.what_happened || "",
            source: "moment",
            hasStudio: ep.has_studio_replay,
            isPro: false,
          })}
        </div>
      `;
    }).join("");

    const proCards = (h.verified_pro_solutions || []).map(ps => {
      const ver = ps.verification || ps.pass2_verdict || {};
      return `
        <div class="report-clip-card pro">
          <div class="row-between">
            <span class="pill ready">Verified Top-Rank Solution${ps.similarity_pct ? ` · ${ps.similarity_pct}%` : ""}</span>
            <span class="pill mono">R${ps.round_id || "?"} · gf${ps.game_frame || 0}</span>
          </div>
          <div style="font-weight:700; font-size:13px;">
            ${formatMatchLabelHtml(ps)}
          </div>
          ${_videoTagHtml(ps.clip_url)}
          ${ver.pro_solution ? `<div class="report-callout emerald">${esc(ver.pro_solution)}</div>` : ""}
          ${_renderClipActions({
            qid: ps.quark_id,
            gf: ps.game_frame,
            roundId: ps.round_id,
            persp: ps.perspective,
            label: `Top-Rank Answer: ${ps.self_player_name || ""} (${ps.self_char_name || ""})`,
            note: ver.pro_solution || "",
            source: "pro_clip",
            hasStudio: false,
            isPro: true,
          })}
        </div>
      `;
    }).join("");

    return `
      <div class="report-section-card">
        <div class="row-between" style="margin-bottom:8px;">
          <div>
            <div class="row" style="gap:8px;">
              <strong style="font-size:16.5px;">#${idx + 1} ${esc(h.label)}</strong>
              <span class="pill err">-${h.total_hp_lost || 0} HP</span>
              <span class="pill">${h.verified_clips || (h.examples || []).length} clips · ${h.sets_affected || 0}/${h.total_sets || doc.num_sets || 0} sets</span>
            </div>
            <div class="row" style="gap:5px; margin-top:6px;">${opps}${moves}</div>
          </div>
          <button class="emerald sm" onclick="_pinReportHabitAsGoal(${idx}, this)">+ Pin as Focus Goal</button>
        </div>

        <div class="section-eyebrow" style="margin-top:10px;">YOUR VERIFIED EVIDENCE CLIPS</div>
        <div class="report-clip-grid">${exCards || `<div class="empty-inline">No clips saved.</div>`}</div>

        ${proCards ? `
          <div class="section-eyebrow" style="margin-top:16px; color:var(--emerald-fg);">VISUALLY VERIFIED TOP-RANK SOLUTIONS</div>
          <div class="report-clip-grid">${proCards}</div>
        ` : ""}
      </div>
    `;
  }).join("");

  return `
    <div class="report-header-card">
      <div class="row-between">
        <div>
          <div class="row" style="gap:8px; margin-bottom:6px;">
            <span class="pill ready">Verified Habits</span>
            <span class="pill mono">${esc(doc.match_date || "")}</span>
          </div>
          <div style="font-size:21px; font-weight:800;">${esc(doc.title)}</div>
          <div style="font-size:13px; color:var(--muted); margin-top:4px;">
            ${doc.num_sets || 0} sets · ${doc.total_rounds || 0} rounds · ${doc.total_damage_episodes || 0} Hit-#1 episodes · ${doc.visually_verified_clips || 0} visually verified clips · ${(doc.corpus_size || 0).toLocaleString()} top-rank reference points
          </div>
        </div>
      </div>
    </div>
    ${_renderAiSynthesisBlock(doc.ai_synthesis)}
    ${habitsHtml}
  `;
}

function _renderFullSetReport(doc) {
  const isV2 = doc.pipeline === "hybrid_v2";
  const qid = doc.target_quark_id || "";
  const studioBtn = doc.has_studio_replay
    ? `<button class="primary sm" onclick="_openReportMomentInStudio(${escJsAttr(qid)},${Number(doc.perspective || 1)},1,0)">▶ Open in Studio</button>`
    : "";

  const momentsHtml = (doc.moments || []).map((m, idx) => {
    const vt = m.visual_tag || {};
    const ai = m.ai_summary || {};
    const badge = isV2
      ? `<span class="pill err">-${m.hp_lost || 0} HP (${m.hp_lost_pct || 0}%)</span>`
      : `<span class="pill drill">Regret ${Number(m.regret || 0) >= 0 ? "+" : ""}${Number(m.regret || 0).toFixed(2)}</span>`;

    // Pro solutions (hybrid_v2) OR top proofs from legacy options
    let proList = m.verified_pro_solutions || [];
    if (!proList.length && Array.isArray(m.options)) {
      proList = [];
      m.options.slice(0, 2).forEach(opt => {
        if (opt.proofs && opt.proofs[0]) {
          proList.push({ ...opt.proofs[0], _optAction: opt.action_coarse, _optEv: opt.ev });
        }
      });
    }

    const proCards = proList.map((ps, pi) => {
      const ver = ps.verification || {};
      const caption = ver.pro_solution || (ai.clip_captions && ai.clip_captions[`alt_${pi + 1}`]) || "";
      return `
        <div class="report-clip-card pro">
          <div class="row-between">
            <span class="pill ready">Top-Rank Option #${pi + 1}${ps.similarity_pct ? ` · ${ps.similarity_pct}% match` : ""}</span>
            <span class="pill mono">R${ps.round_id || "?"} · gf${ps.game_frame || 0}</span>
          </div>
          <div style="font-weight:700; font-size:13px;">
            ${formatMatchLabelHtml(ps)}
          </div>
          ${_videoTagHtml(ps.clip_url)}
          ${caption ? `<div class="report-callout emerald">${esc(caption)}</div>` : ""}
          ${_renderClipActions({
            qid: ps.quark_id,
            gf: ps.game_frame,
            roundId: ps.round_id,
            persp: ps.perspective,
            label: `Top-Rank Answer: ${ps.self_player_name || ""} (${ps.self_char_name || ""})`,
            note: caption,
            source: "pro_clip",
            hasStudio: false,
            isPro: true,
          })}
        </div>
      `;
    }).join("");

    const userExplain = vt.what_happened || ai.situation_summary || "";
    const takeaway = vt.self_mistake || ai.alternative_takeaway || "";

    return `
      <div class="report-section-card">
        <div class="row-between" style="margin-bottom:8px;">
          <div class="row" style="gap:8px;">
            <strong style="font-size:15.5px;">#${idx + 1} ${esc(m.label || m.category_label || "Moment")}</strong>
            ${badge}
            <span class="pill cached mono">R${m.round_id || "?"} · gf${m.game_frame || 0}</span>
          </div>
        </div>

        ${userExplain ? `<div class="report-callout" style="margin-bottom:8px;">${esc(userExplain)}</div>` : ""}
        ${takeaway ? `<div class="report-callout amber" style="margin-bottom:10px;"><strong>Takeaway:</strong> ${esc(takeaway)}</div>` : ""}

        <div class="report-clip-grid ${proList.length >= 2 ? "cols-3" : ""}">
          <div class="report-clip-card">
            <div class="row-between">
              <span class="pill cached">Your Choice</span>
              <span class="pill mono">R${m.round_id || "?"} · gf${m.game_frame || 0}</span>
            </div>
            <div style="font-weight:700; font-size:13px;">${esc(doc.self_player_name || "You")} (${esc(m.self_char_name || doc.self_char_name || "")})</div>
            ${_videoTagHtml(m.clip_url)}
            ${_renderClipActions({
              qid: m.quark_id || qid,
              gf: m.game_frame,
              roundId: m.round_id,
              persp: m.perspective || doc.perspective,
              label: m.label || m.category_label || "Moment",
              note: takeaway || userExplain,
              source: "moment",
              hasStudio: m.has_studio_replay || doc.has_studio_replay,
              isPro: false,
            })}
          </div>
          ${proCards}
        </div>
      </div>
    `;
  }).join("");

  return `
    <div class="report-header-card">
      <div class="row-between">
        <div>
          <div class="row" style="gap:8px; margin-bottom:6px;">
            <span class="pill cached">${isV2 ? "Match Breakdown (Hybrid Vision + RAM)" : "Match Breakdown"}</span>
            <span class="pill mono">${esc(doc.match_date || "")}</span>
          </div>
          <div style="font-size:21px; font-weight:800;">${esc(doc.title)}</div>
          <div style="font-size:13px; color:var(--muted); margin-top:4px;">
            ${(doc.moments || []).length} key moments · ${doc.total_damage_episodes || doc.total_decision_points || 0} analyzed situations · ${(doc.corpus_size || 0).toLocaleString()} top-rank points
          </div>
        </div>
        ${studioBtn}
      </div>
    </div>
    ${momentsHtml || `<div class="card empty-card">No key moments in this report.</div>`}
  `;
}

function _renderSequenceSearchReport(doc) {
  const q = doc.query || {};
  const ai = doc.ai_summary || {};
  const matchesHtml = (doc.matches || []).map((m, idx) => {
    return `
      <div class="report-clip-card pro">
        <div class="row-between">
          <span class="pill ready">#${idx + 1} · ${m.similarity_pct || 0}% match</span>
          <span class="pill mono">R${m.round_id || "?"} · gf${m.game_frame || m.mid_gf || 0}</span>
        </div>
        <div style="font-weight:700; font-size:13px;">
          ${formatMatchLabelHtml(m)}
        </div>
        <div style="font-size:12px; color:var(--muted);">Action: <strong>${esc(m.action_coarse || "—")}</strong> · Outcome: ${Number(m.outcome_value || 0) >= 0 ? "+" : ""}${Math.round(Number(m.outcome_value || 0) * 100)}%</div>
        ${_videoTagHtml(m.clip_url)}
        ${_renderClipActions({
          qid: m.quark_id,
          gf: m.game_frame || m.mid_gf,
          roundId: m.round_id,
          persp: m.perspective,
          label: `Top-Rank Match: ${m.self_player_name || ""} (${m.self_char_name || ""})`,
          note: m.label || "",
          source: "pro_clip",
          hasStudio: false,
          isPro: true,
        })}
      </div>
    `;
  }).join("");

  return `
    <div class="report-header-card">
      <div class="row-between">
        <div>
          <div class="row" style="gap:8px; margin-bottom:6px;">
            <span class="pill note">Sequence Search</span>
            <span class="pill mono">${esc(doc.match_date || "")}</span>
            <span class="pill cached">${esc(doc.retrieval_tier || "")}</span>
          </div>
          <div style="font-size:20px; font-weight:800;">${esc(doc.note || doc.title)}</div>
          <div style="font-size:13px; color:var(--muted); margin-top:4px;">
            ${esc(doc.title)} · R${q.round_id || "?"} · gf${q.start_gf || 0}–${q.end_gf || 0} (${q.duration_s || 0}s) · Scanned ${doc.rounds_scanned || 0} top-rank rounds
          </div>
        </div>
        ${q.has_studio_replay ? `<button class="primary sm" onclick="_openReportMomentInStudio(${escJsAttr(q.quark_id)},${Number(q.perspective || 1)},${Number(q.round_id || 1)},${Number(q.start_gf || 0)})">▶ Open in Studio</button>` : ""}
      </div>
    </div>

    ${(ai.pattern_summary || ai.pro_takeaway) ? `
      <div class="report-synth-card">
        <strong style="font-size:14.5px;">✦ AI Pattern Summary</strong>
        ${ai.pattern_summary ? `<div style="margin-top:6px; font-size:13.5px;">${esc(ai.pattern_summary)}</div>` : ""}
        ${ai.pro_takeaway ? `<div class="report-callout emerald" style="margin-top:8px;"><strong>Takeaway:</strong> ${esc(ai.pro_takeaway)}</div>` : ""}
      </div>
    ` : ""}

    <div class="report-section-card">
      <div class="section-eyebrow">YOUR QUERY SEQUENCE</div>
      <div class="report-clip-grid">
        <div class="report-clip-card">
          <div class="row-between">
            <span class="pill cached">Query Sequence</span>
            <span class="pill mono">R${q.round_id || "?"} · gf${q.start_gf || 0}–${q.end_gf || 0}</span>
          </div>
          ${_videoTagHtml(q.clip_url)}
          ${_renderClipActions({
            qid: q.quark_id,
            gf: q.mid_gf || q.start_gf,
            roundId: q.round_id,
            persp: q.perspective,
            label: doc.note || q.label || "Query sequence",
            note: ai.pro_takeaway || "",
            source: "moment",
            hasStudio: q.has_studio_replay,
            isPro: false,
          })}
        </div>
      </div>
    </div>

    <div class="report-section-card">
      <div class="section-eyebrow">TOP MATCHING TOP-RANK SEQUENCES (${(doc.matches || []).length})</div>
      <div class="report-clip-grid">${matchesHtml}</div>
    </div>
  `;
}

function _renderLegacyRollupReport(doc) {
  const habitsHtml = (doc.habits || []).slice(0, 8).map((h, idx) => {
    const exCards = (h.examples || []).slice(0, 4).map(ep => `
      <div class="report-clip-card">
        <div class="row-between">
          <span class="pill drill">Regret ${Number(ep.regret || 0).toFixed(2)}</span>
          <span class="pill cached mono">${esc(ep.match_date || "")} · R${ep.round_id || "?"}</span>
        </div>
        <div style="font-weight:700; font-size:13px;">${formatMatchLabelHtml(ep, { defaultSelfName: doc.username || state.username })}</div>
        ${_videoTagHtml(ep.clip_url)}
        ${_renderClipActions({
          qid: ep.quark_id,
          gf: ep.game_frame,
          roundId: ep.round_id,
          persp: ep.perspective,
          label: ep.label || h.interaction,
          note: "",
          source: "moment",
          hasStudio: ep.has_studio_replay,
          isPro: false,
        })}
      </div>
    `).join("");
    return `
      <div class="report-section-card">
        <div class="row-between">
          <div class="row" style="gap:8px;">
            <strong style="font-size:15.5px;">#${idx + 1} ${esc(h.interaction)}</strong>
            <span class="pill drill">Regret ${Number(h.total_regret || 0).toFixed(2)}</span>
            <span class="pill">${h.occurrences || 0} leaks · ${h.sets_affected || 0}/${h.total_sets || 0} sets</span>
          </div>
          <button class="emerald sm" onclick="_pinReportHabitAsGoal(${idx}, this)">+ Pin as Focus Goal</button>
        </div>
        <div class="report-clip-grid">${exCards}</div>
      </div>
    `;
  }).join("");

  return `
    <div class="report-header-card">
      <div class="row" style="gap:8px; margin-bottom:6px;">
        <span class="pill drill">Legacy v1 Habit Rollup</span>
        <span class="pill mono">${esc(doc.match_date || "")}</span>
      </div>
      <div style="font-size:20px; font-weight:800;">${esc(doc.title)}</div>
      <div style="font-size:13px; color:var(--muted); margin-top:4px;">
        ${doc.num_sets || 0} sets · ${doc.total_rounds || 0} rounds · ${doc.total_leaks || 0} detected regret leaks
      </div>
    </div>
    ${_renderAiSynthesisBlock(doc.ai_synthesis)}
    ${habitsHtml}
  `;
}

function renderReportViewer(doc) {
  const host = document.getElementById("reportViewerContent");
  if (!host || !doc) return;
  if (doc.report_type === "hybrid_habit_rollup") {
    host.innerHTML = _renderVerifiedHabitsReport(doc);
  } else if (doc.report_type === "sequence_search") {
    host.innerHTML = _renderSequenceSearchReport(doc);
  } else if (doc.report_type === "habit_rollup") {
    host.innerHTML = _renderLegacyRollupReport(doc);
  } else {
    host.innerHTML = _renderFullSetReport(doc);
  }
}
