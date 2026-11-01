// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.2
// SPDX-License-Identifier: AGPL-3.0-or-later
/* G-184 (G-168 fase 3): fysisk kart-EDITOR på den DELTE kart-kjernen (map_renderer.js).
   Adapter + glue for det fysiske kartet.

   Kjernen leverer: tegne-loop, bakgrunn, rutenett, dra/pinn (her: flytt UTEN lås, G-034 →
   pinOnDrag=false), linje-geometri (ctx.linkGeom) og kreditt-stempel. Adapteren leverer det
   fysisk-spesifikke: editor-node-farger (valgt/link-A/låst), enkle redigerbare linker + link-meny,
   node-meny (lås/omdøp/størrelse/X-Y/bytt enhet/slett), «Lag link»-picker, lagre/publiser,
   oppløsning, node-størrelse, snap-rutenett, kartnavn-omdøp og scratch-livssyklus. URL-er fra
   url_for via map-data (subpath-trygt). */
(function () {
  "use strict";
  const M = window.BifrostMap;
  const el = M.el, cvar = M.cvar;

  const raw = JSON.parse(document.getElementById("map-data").textContent);
  const svg = document.getElementById("edit-svg");
  const wrap = document.getElementById("map-wrap");
  const status = document.getElementById("save-status");
  const hint = document.getElementById("edit-hint");
  const linkBtn = document.getElementById("link-btn");
  const picker = document.getElementById("link-picker");
  const lpList = document.getElementById("lp-list");
  const ctxMenu = document.getElementById("ctx-menu");
  const U = raw.urls;

  const SEL = cvar("--gr-700", "#14dd86"), PICK = cvar("--cap-mid", "#f1c40f"),
        PINNED = cvar("--bl-800", "#3a14c6"), NODE = cvar("--nf", "#1484b6"),
        LINK = cvar("--bl-500", "#8181f1");
  const INK = cvar("--ink", "#181c56");     // 1.8.4/G-291-oppfølging: mørk tekst for lys kart-bakgrunn
  const HALO = (raw.canvas && raw.canvas.background) || "#f0f1f7";   // 1.8.4: lys default (var mørk-æraens #0d132d)
  // 1.8.4: samme etikett-klamping som visningen (kant-noder rant utenfor rammen).
  function clampLabelX(x, text, fs) {
    const half = Math.min((String(text).length * fs * 0.62) / 2, (cfg.width || 2) / 2 - 4);
    return Math.max(half + 4, Math.min((cfg.width || 0) - half - 4, x));
  }
  function labelY(n, r) {
    const y = n.y + r + 16;
    return y > (cfg.height || 0) - 6 ? n.y - r - 8 : y;
  }
  const SIZE_PX = { S: 11, M: 16, L: 22 };

  const cv = raw.canvas || {};
  const cfg = {
    width: cv.width, height: cv.height, background: cv.background, node_size: cv.node_size,
    bg_opacity: cv.bg_opacity, bg_mode: raw.bg_mode, bg_live: raw.bg_live,
    google_session: raw.google_session, has_bg_image: !!raw.has_bg, bg_url: raw.bg_url, bg_attr: raw.bg_attr,
    nodes: (raw.nodes || []).map(n => Object.assign({}, n, { rid: String(n.id), pinned: !!n.pinned })),
    links: raw.links || []
  };
  const byId = {};
  cfg.nodes.forEach(n => byId[n.id] = n);

  let mapNodeSize = cv.node_size || "L";
  let gridSize = cv.grid_size || 0;
  const nodeR = n => SIZE_PX[(n && n.size) || mapNodeSize] || 22;
  const nodeFill = n => (n.id === selected) ? SEL : (n.id === connectA) ? PICK : (n.pinned ? PINNED : NODE);

  let selected = null, mode = "normal", connectA = null;
  let candCtx = null, candMode = "likely";
  let redraw = () => {};            // settes til ctx.draw i initControls
  let _ctx = null;                  // G-233: kjerne-ctx (for state.gridSize ved lagring)
  const setHint = s => { if (hint) hint.textContent = s; };

  // --- 1.3.14: bue parallelle interne lenker (statisk; reberegnes ved reload) ---
  function assignBows() {
    const groups = {};
    cfg.links.forEach(l => {
      l._bow = 0;
      if (l.kind !== "internal" || !l.enabled || !l.b_node_id) return;
      const lo = Math.min(l.a_node_id, l.b_node_id), hi = Math.max(l.a_node_id, l.b_node_id);
      (groups[lo + "-" + hi] ||= { lo, rows: [] }).rows.push(l);
    });
    const GAP = 22, capOf = l => (l.speed_mbps || 0), idOf = l => (l.interface_id || l.id || 0);
    Object.values(groups).forEach(({ lo, rows }) => {
      if (rows.length < 2) return;
      const fwd = rows.filter(l => l.a_node_id === lo).sort((x, y) => capOf(y) - capOf(x) || idOf(x) - idOf(y));
      const bwd = rows.filter(l => l.a_node_id !== lo).sort((x, y) => capOf(y) - capOf(x) || idOf(x) - idOf(y));
      const k = Math.max(fwd.length, bwd.length), clusters = [];
      for (let i = 0; i < k; i++) {
        const members = [fwd[i], bwd[i]].filter(Boolean);
        clusters.push({ members, cap: Math.max(...members.map(capOf)) });
      }
      if (clusters.length < 2) return;
      const maxCap = Math.max(...clusters.map(c => c.cap));
      const atMax = clusters.filter(c => c.cap === maxCap);
      const straight = atMax.length === 1 ? atMax[0] : null;
      const bowed = clusters.filter(c => c !== straight).sort((x, y) => y.cap - x.cap);
      const setBow = (c, v) => c.members.forEach(l => l._bow = (l.a_node_id === lo ? 1 : -1) * v);
      if (straight) bowed.forEach((c, i) => setBow(c, (i % 2 ? -1 : 1) * ((i >> 1) + 1) * GAP));
      else { const n = bowed.length; bowed.forEach((c, i) => setBow(c, (i - (n - 1) / 2) * GAP)); }
    });
  }

  // G-109: naviger bort på en måte VI styrer → ikke forkast scratch.
  // G-240: edit-operasjoner (opprett/slett/rediger link, slett/bytt node, node-størrelse) reloader
  // editor-siden. Sett «bli i redigeringsmodus»-flagget så vi IKKE faller ut av redigering ved reload
  // (samme flagg som Forkast bruker). Gjelder kun reload TIL editor-siden, ikke f.eks. Aktiver→visning.
  function go(url) {
    try { if (url === U.edit) sessionStorage.setItem("bf_edit_keep", "1"); } catch (e) { /* noop */ }
    window.location = url;
  }

  // --- lagre / scratch ---
  async function saveState(asDraft) {
    const links = Array.from(document.querySelectorAll(".link-tgl")).map(c => ({ id: +c.dataset.id, enabled: c.checked }));
    const nodes = cfg.nodes.map(n => ({ id: n.id, x: n.x, y: n.y, pinned: n.pinned, label: n.label, size: n.size || null }));
    const payload = { nodes, links };
    if (asDraft) payload.as_draft = true;
    // G-233: ÉN dropdown for «utenfor nettet» — huket av kategori = VIS den. show_external utledes
    // (minst én avhuket), hidden_ext_roles = de IKKE-avhukede (som visningen skjuler).
    const shownExt = Array.from(document.querySelectorAll(".show-ext:checked")).map(c => c.value);
    const allExt = Array.from(document.querySelectorAll(".show-ext")).map(c => c.value);
    payload.show_external = shownExt.length > 0;
    payload.hidden_ext_roles = allExt.filter(r => shownExt.indexOf(r) === -1);
    payload.node_size = mapNodeSize;
    payload.width = cfg.width; payload.height = cfg.height;
    payload.grid_size = (_ctx ? _ctx.state.gridSize : gridSize);   // G-233: rutenett wires i kjernen
    const r = await fetch(U.save, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (r.status === 410) {
      let msg = i18n.t("js.physedit.scratch_expired");
      try { const d = await r.json(); if (d && d.msg) msg = d.msg; } catch (e) { /* noop */ }
      status.textContent = "⚠ " + msg; return false;
    }
    if (r.redirected || !r.ok) { status.textContent = i18n.t("js.physedit.save_session_failed"); return false; }
    if (_ctx) _ctx.setClean();   // G-260: lagret (mellomlagret/utkast) = ingen ulagrede endringer
    return true;
  }
  async function ensureSaved() {
    let ok = false, threw = false;
    try { ok = await saveState(); } catch (e) { threw = true; }
    if (!ok && threw) status.textContent = i18n.t("js.physedit.interim_save_failed");
    return ok;
  }

  // --- kandidat-picker («Lag link») ---
  // G-315: replaceId = gammel link som skal ERSTATTES — slettes FØRST når en erstatning faktisk
  // er valgt (i createLink), aldri før (ellers mister «Avbryt» linken stille).
  async function openPicker(a, b, replaceId) { candCtx = { a, b, replaceId: replaceId || null }; candMode = "likely"; await loadCandidates(); }
  async function loadCandidates() {
    const { a, b } = candCtx;
    try {
      const r = await fetch(`${U.candidates}?a=${a}&b=${b}&mode=${candMode}`);
      if (!r.ok || r.redirected) throw new Error("HTTP " + r.status);
      renderCandidates((await r.json()).candidates || []);
    } catch (e) { lpError(i18n.t("js.physedit.candidates_failed")); picker.hidden = false; }
  }
  function lpError(msg) { const p = document.createElement("p"); p.className = "err"; p.textContent = msg; lpList.replaceChildren(p); }
  function renderCandidates(cands) {
    document.getElementById("lp-title").textContent =
      i18n.t("js.physedit.pick_title", { mode: candMode === "likely" ? i18n.t("js.physedit.cand_likely") : i18n.t("js.physedit.cand_all"), n: cands.length });
    if (!cands.length) { lpList.innerHTML = "<p class='hint'>" + i18n.t("js.physedit.no_candidates") + "</p>"; }
    else {
      lpList.innerHTML = "";
      cands.forEach(c => {
        const node = byId[c.node_id];
        const b = document.createElement("button");
        b.className = "ghost sm"; b.style.cssText = "display:block;width:100%;text-align:left;margin:2px 0";
        b.textContent = `${node ? (node.label || node.name) : c.node_id} · ${c.if_name || "?"} · ${c.role}` +
                        (c.ifalias ? `  «${c.ifalias}»` : "") + (c.likely ? "  ✓" : "");
        b.addEventListener("click", () => createLink(c));
        lpList.appendChild(b);
      });
    }
    picker.hidden = false;
  }
  async function createLink(c) {
    const body = new URLSearchParams({ interface_id: c.interface_id, a_node_id: c.node_id, b_node_id: c.peer_node_id });
    try {
      if (!(await ensureSaved())) return;
      const r = await fetch(U.link, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
      const d = r.redirected ? {} : await r.json().catch(() => ({}));
      if (r.ok && d.status === "ok") {
        // G-315: erstatning lyktes → NÅ er det trygt å slette den gamle linken (ikke før).
        if (candCtx && candCtx.replaceId) {
          let del = null;
          try { del = await postForm(nurl(U.link_delete, candCtx.replaceId), {}, false); } catch (e) { del = null; }
          const delErr = formFailed(del);
          if (delErr) alert(i18n.t("js.physedit.old_link_not_deleted", { err: delErr }));
        }
        closePicker(); go(U.edit);
      } else lpError(d.msg || d.error || (r.redirected ? i18n.t("js.physedit.session_expired") : i18n.t("js.physedit.error_status", { status: r.status })));
    } catch (e) { lpError(i18n.t("js.physedit.save_error")); }
  }
  function closePicker() { picker.hidden = true; mode = "normal"; connectA = null; linkBtn.classList.remove("on"); candCtx = null; }

  // --- kontekstmeny (node + link) ---
  const nurl = (tmpl, id) => tmpl.replace("/0/", "/" + id + "/");
  const hideMenu = () => { ctxMenu.hidden = true; ctxMenu.innerHTML = ""; };
  function pageXY(n) {
    const r = svg.getBoundingClientRect();
    return { x: window.scrollX + r.left + n.x / cfg.width * r.width + 26,
             y: window.scrollY + r.top + n.y / cfg.height * r.height - 10 };
  }
  function menuButton(label, cls, fn) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "ghost sm" + (cls ? " " + cls : "");
    b.style.cssText = "display:block;width:100%;text-align:left;margin:2px 0";
    b.textContent = label; b.addEventListener("click", fn); return b;
  }
  function showMenuAt(px, py, build) {
    ctxMenu.innerHTML = ""; build(ctxMenu);
    ctxMenu.style.left = px + "px"; ctxMenu.style.top = py + "px"; ctxMenu.hidden = false;
  }
  // Feiltekst for et svar fra et skjema-endepunkt, eller null ved OK. Endepunktene svarer med redirect
  // til editoren ved suksess (fetch følger den → ok), så kun redirect til innlogging regnes som feil.
  function formFailed(r) {
    if (!r) return i18n.t("js.physedit.no_contact");
    if (r.redirected && /\/login/.test(r.url || "")) return i18n.t("js.physedit.session_expired");
    if (!r.ok) return i18n.t("js.physedit.error_status", { status: r.status });
    return null;
  }
  async function postForm(url, obj, reload) {
    if (reload && !(await ensureSaved())) return null;
    let r = null;
    try {
      r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams(obj) });
    } catch (e) {
      if (!reload) throw e;                  // kallere uten reload håndterer nettverksfeil selv (som før)
      r = null;
    }
    if (reload) {
      const err = formFailed(r);
      if (err) { status.textContent = i18n.t("js.physedit.not_done", { err: err }); return r; }   // ingen stille reload ved feil
      go(U.edit);
    }
    return r;
  }

  function showNodeMenu(n) {
    const p = pageXY(n);
    showMenuAt(p.x, p.y, (box) => {
      const h = document.createElement("div"); h.className = "hint"; h.style.cssText = "margin:0 0 4px";
      h.textContent = (n.label || n.name); box.appendChild(h);
      box.appendChild(menuButton(n.pinned ? i18n.t("js.physedit.unlock_pos") : i18n.t("js.physedit.lock_pos"), "", () => {
        n.pinned = !n.pinned; redraw(); if (_ctx) _ctx.markDirty();   // G-260: ulagret endring
        setHint(i18n.t(n.pinned ? "js.physedit.hint_pos_locked" : "js.physedit.hint_pos_unlocked", { name: n.label || n.name })); hideMenu();
      }));
      box.appendChild(menuButton(i18n.t("js.physedit.menu_display_name", { name: n.label || n.name }), "", () => nodeRenameForm(n)));
      const szRow = document.createElement("div");
      szRow.style.cssText = "display:flex;align-items:center;gap:4px;margin:3px 0;font-size:12px";
      const szLab = document.createElement("span"); szLab.className = "hint"; szLab.textContent = i18n.t("js.physedit.size_label"); szRow.appendChild(szLab);
      ["S", "M", "L"].forEach(s => {
        const b = document.createElement("button"); b.type = "button";
        b.className = "ghost sm" + (((n.size || mapNodeSize) === s) ? " on" : ""); b.textContent = s;
        b.addEventListener("click", () => { n.size = s; redraw(); if (_ctx) _ctx.markDirty(); setHint(i18n.t("js.physedit.hint_size", { name: n.label || n.name, size: s })); hideMenu(); });
        szRow.appendChild(b);
      });
      box.appendChild(szRow);
      const xyRow = document.createElement("div");
      xyRow.style.cssText = "display:flex;align-items:center;gap:4px;margin:3px 0;font-size:12px";
      const xyLab = document.createElement("span"); xyLab.className = "hint"; xyLab.textContent = "✛ X/Y:";
      const inX = document.createElement("input"), inY = document.createElement("input");
      [inX, inY].forEach(i => { i.type = "number"; i.style.cssText = "width:62px"; });
      inX.value = Math.round(n.x); inY.value = Math.round(n.y);
      const setBtn = document.createElement("button"); setBtn.type = "button"; setBtn.className = "ghost sm"; setBtn.textContent = i18n.t("js.physedit.set");
      setBtn.title = i18n.t("js.physedit.set_title");
      const applyXY = () => {
        const x = parseInt(inX.value, 10), y = parseInt(inY.value, 10);
        if (isNaN(x) || isNaN(y)) return;
        n.x = Math.min(cfg.width, Math.max(0, x)); n.y = Math.min(cfg.height, Math.max(0, y));
        redraw(); if (_ctx) _ctx.markDirty(); setHint(i18n.t("js.physedit.hint_placed", { name: n.label || n.name, x: n.x, y: n.y })); hideMenu();
      };
      setBtn.addEventListener("click", applyXY);
      [inX, inY].forEach(i => i.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); applyXY(); } }));
      xyRow.appendChild(xyLab); xyRow.appendChild(inX); xyRow.appendChild(inY); xyRow.appendChild(setBtn); box.appendChild(xyRow);
      box.appendChild(menuButton(i18n.t("js.physedit.menu_switch_device"), "", () => nodeDeviceForm(n)));
      box.appendChild(menuButton(i18n.t("js.physedit.menu_gps_info"), "", () => { hideMenu(); _ctx.showGpsInfo(n); }));
      box.appendChild(menuButton(i18n.t("js.physedit.menu_delete_node"), "danger", () => {
        if (confirm(i18n.t("js.physedit.confirm_remove_node", { name: n.label || n.name }))) postForm(nurl(U.node_remove, n.id), {}, true);
      }));
    });
  }
  function nodeRenameForm(n) {
    const p = pageXY(n);
    showMenuAt(p.x, p.y, (box) => {
      const lab = document.createElement("div"); lab.className = "hint"; lab.style.cssText = "margin:0 0 4px";
      lab.textContent = i18n.t("js.physedit.rename_hint"); box.appendChild(lab);
      const inp = document.createElement("input"); inp.value = n.label || ""; inp.placeholder = n.name;
      inp.style.cssText = "width:100%;margin-bottom:6px"; box.appendChild(inp);
      const dev = document.createElement("div"); dev.className = "hint"; dev.style.cssText = "margin:0 0 6px";
      dev.textContent = i18n.t("js.physedit.device_name", { name: n.name }); box.appendChild(dev);
      const commit = () => { n.label = inp.value.trim(); redraw(); if (_ctx) _ctx.markDirty(); hideMenu(); setHint(i18n.t("js.physedit.hint_display_name", { name: n.label || n.name })); };
      const row = document.createElement("div"); row.className = "row"; row.style.gap = "4px";
      const ok = document.createElement("button"); ok.type = "button"; ok.className = "ghost"; ok.textContent = "OK"; ok.addEventListener("click", commit);
      const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "ghost"; cancel.textContent = i18n.t("js.physedit.cancel"); cancel.addEventListener("click", hideMenu);
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } else if (e.key === "Escape") { e.preventDefault(); hideMenu(); } });
      row.appendChild(ok); row.appendChild(cancel); box.appendChild(row); inp.focus();
    });
  }
  function nodeDeviceForm(n) {
    const p = pageXY(n);
    showMenuAt(p.x, p.y, (box) => {
      const lab = document.createElement("div"); lab.className = "hint"; lab.style.cssText = "margin:0 0 4px";
      lab.textContent = i18n.t("js.physedit.switch_intro"); box.appendChild(lab);
      if (!cfg.nodes || !raw.available || !raw.available.length) {
        const p2 = document.createElement("p"); p2.className = "hint"; p2.textContent = i18n.t("js.physedit.no_free_hosts");
        box.appendChild(p2); box.appendChild(menuButton(i18n.t("js.physedit.close"), "", hideMenu)); return;
      }
      const sel = document.createElement("select"); sel.style.cssText = "width:100%;margin-bottom:6px";
      const gActive = document.createElement("optgroup"); gActive.label = i18n.t("js.physedit.active_device");
      const oCur = document.createElement("option"); oCur.value = n.host_id;
      oCur.textContent = (n.name || "?") + (n.address ? " · " + n.address : ""); gActive.appendChild(oCur); sel.appendChild(gActive);
      const gOther = document.createElement("optgroup"); gOther.label = i18n.t("js.physedit.switch_to");
      raw.available.forEach(host => { const o = document.createElement("option"); o.value = host.id; o.textContent = host.name + " · " + host.address; gOther.appendChild(o); });
      sel.appendChild(gOther); sel.value = n.host_id; box.appendChild(sel);
      const opt = (label, checked) => {
        const w = document.createElement("label"); w.style.cssText = "display:block;font-size:12px;margin:1px 0";
        const cb = document.createElement("input"); cb.type = "checkbox"; cb.checked = checked;
        w.appendChild(cb); w.appendChild(document.createTextNode(" " + label)); box.appendChild(w); return cb;
      };
      const keepLinks = opt(i18n.t("js.physedit.keep_links"), false);
      const relink = opt(i18n.t("js.physedit.relink"), true);
      const keepLabel = opt(i18n.t("js.physedit.keep_label"), false);
      const row = document.createElement("div"); row.className = "row"; row.style.cssText = "gap:4px;margin-top:6px";
      const ok = document.createElement("button"); ok.type = "button"; ok.className = "ghost"; ok.textContent = i18n.t("js.physedit.switch_device");
      ok.addEventListener("click", async () => {
        if (String(sel.value) === String(n.host_id)) { setHint(i18n.t("js.physedit.already_active")); return; }
        if (!(await ensureSaved())) return;
        let res = null;
        try {
          res = await postForm(nurl(U.node_device, n.id), {
            host_id: sel.value, keep_links: keepLinks.checked ? "1" : "0",
            relink: relink.checked ? "1" : "0", keep_label: keepLabel.checked ? "1" : "0"
          }, false);
        } catch (e) { res = null; }
        if (res && res.status === 409) { alert(i18n.t("js.physedit.device_on_map")); return; }
        const err = formFailed(res);
        if (err) { hideMenu(); status.textContent = i18n.t("js.physedit.not_done", { err: err }); return; }   // ingen stille reload
        go(U.edit);
      });
      const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "ghost"; cancel.textContent = i18n.t("js.physedit.cancel"); cancel.addEventListener("click", hideMenu);
      row.appendChild(ok); row.appendChild(cancel); box.appendChild(row);
    });
  }
  // G-237: «📍 GPS / NetBox-info…» bor nå i den DELTE kjernen (ctx.showGpsInfo) — universelt på alle
  // kart. Node-menyen her kaller bare det felles panelet (se showNodeMenu).
  function showLinkMenu(l, e) {
    if (_ctx && !_ctx.state.editing) return;   // G-248: ingen link-meny (slett/bytt) når Rediger er av (som node-meny)
    const a = byId[l.a_node_id], b = byId[l.b_node_id];
    const px = window.scrollX + e.clientX + 6, py = window.scrollY + e.clientY + 6;
    showMenuAt(px, py, (box) => {
      const h = document.createElement("div"); h.className = "hint"; h.style.cssText = "margin:0 0 4px";
      h.textContent = `${a ? (a.label || a.name) : "?"} ↔ ${b ? (b.label || b.name) : (l.b_external || "?")}`; box.appendChild(h);
      const info = document.createElement("div"); info.className = "mono"; info.style.cssText = "font-size:12px;margin-bottom:4px";
      info.textContent = `interface: ${l.if_name || l.if_index || "?"} · ${l.role || ""}` + (l.counterparty ? ` · ${l.counterparty}` : ""); box.appendChild(info);
      box.appendChild(menuButton(i18n.t("js.physedit.menu_switch_iface"), "", () => {
        hideMenu(); openPicker(l.a_node_id, l.b_node_id, l.id);   // G-315: slett IKKE før erstatning valgt
      }));
      box.appendChild(menuButton(i18n.t("js.physedit.menu_delete_link"), "danger", () => {
        if (confirm(i18n.t("js.physedit.confirm_remove_link"))) postForm(nurl(U.link_delete, l.id), {}, true);
      }));
    });
  }

  function applyResolution(w, h) {
    const ow = cfg.width, oh = cfg.height;
    if (w === ow && h === oh) return;
    const sx = w / ow, sy = h / oh;
    cfg.nodes.forEach(n => { n.x = Math.round(n.x * sx); n.y = Math.round(n.y * sy); });
    cfg.width = w; cfg.height = h;
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    redraw(); setHint(i18n.t("js.physedit.hint_resolution", { w: w, h: h }));
  }

  // ============================ ADAPTER ============================
  const adapter = {
    nodeKey: n => n.id,   // G-233: node-nøkkel for felles auto-layout (fysisk = composer node-id)
    linkVisible: l => l.kind === "internal" && l.enabled && l.b_node_id != null,

    drawLink(svgEl, l, ctx) {
      const a = byId[l.a_node_id], b = byId[l.b_node_id]; if (!a || !b) return;
      const geom = ctx.linkGeom(a, b, l._bow);     // motoren eier linja
      const dp = `M ${a.x} ${a.y} Q ${geom.C.x} ${geom.C.y} ${b.x} ${b.y}`;
      const hit = el("path", { d: dp, fill: "none", stroke: "transparent", "stroke-width": 16, "class": "bf-cur-pointer" });
      hit.addEventListener("pointerdown", e => e.stopPropagation());
      hit.addEventListener("click", e => { e.stopPropagation(); showLinkMenu(l, e); });
      const ln = el("path", { d: dp, fill: "none", stroke: LINK, "stroke-width": 3, "stroke-opacity": 0.7 });
      ln.style.pointerEvents = "none";
      svgEl.appendChild(hit); svgEl.appendChild(ln);
    },

    drawNode(svgEl, n, ctx) {
      const g = el("g", { "class": "bf-cur-grab" });
      g.appendChild(el("circle", { cx: n.x, cy: n.y, r: nodeR(n), fill: nodeFill(n),
        stroke: HALO, "stroke-width": n.id === selected ? 5 : 3 }));
      const _lbl = (n.label || n.name) + (n.pinned ? " 📌" : "");
      const t = el("text", { x: clampLabelX(n.x, _lbl, 13), y: labelY(n, nodeR(n)),
        "text-anchor": "middle", fill: INK,
        "font-size": 13, "font-weight": 600, stroke: HALO, "stroke-width": 3.5,
        "paint-order": "stroke", "stroke-linejoin": "round" });
      t.textContent = _lbl;
      g.appendChild(t);
      ctx.attachEdit(g, n);     // kjernens dra (pinOnDrag=false)
      svgEl.appendChild(g);
    },

    onNodeClick(n, ev, ctx) {
      if (!ctx.state.editing) return;   // G-233: ingen node-meny/redigering når Rediger er av
      if (mode === "connect") {
        if (!connectA) { connectA = n.id; setHint(i18n.t("js.physedit.hint_link_b", { name: n.label || n.name })); redraw(); }
        else if (n.id !== connectA) openPicker(connectA, n.id);
        return;
      }
      selected = n.id; setHint(i18n.t(n.pinned ? "js.physedit.hint_selected_locked" : "js.physedit.hint_selected", { name: n.label || n.name }));
      redraw(); showNodeMenu(n);
    },

    initControls(ctx) {
      redraw = ctx.draw;
      _ctx = ctx;                      // G-233: for å lese state.gridSize ved lagring
      // G-233: fysisk er toggle-basert (start i visning). Etter Forkast settes et flagg så vi BLIR i
      // redigering etter reload (revert til sist lagrede uten å forlate redigeringsmodus).
      let _keepEdit = false;
      try { _keepEdit = sessionStorage.getItem("bf_edit_keep") === "1"; if (_keepEdit) sessionStorage.removeItem("bf_edit_keep"); } catch (e) { /* noop */ }
      ctx.state.editing = _keepEdit;
      ctx.state.gridSize = gridSize;   // G-125: snap-rutenett
      // G-233: synk den FELLES rutenett-dropdownen (#eb-grid-sel) til kartets lagrede verdi.
      const _ebGrid = document.getElementById("eb-grid-sel");
      if (_ebGrid) _ebGrid.value = String(gridSize);

      // bakgrunns-klikk → avvelg (kjernen tegner bg-rect; delegert sjekk på persistent svg)
      svg.addEventListener("pointerdown", (e) => {
        if (mode === "normal" && e.target && e.target.tagName === "rect") {
          selected = null; setHint(i18n.t("js.physedit.hint_click_node")); redraw();
        }
      });

      // «Lag link»-modus
      linkBtn.addEventListener("click", () => {
        if (mode === "connect") { mode = "normal"; connectA = null; linkBtn.classList.remove("on"); selected = null; redraw(); return; }
        mode = "connect"; connectA = null; linkBtn.classList.add("on"); selected = null; setHint(i18n.t("js.physedit.hint_link_a")); redraw();
      });
      document.getElementById("lp-likely").addEventListener("click", () => { candMode = "likely"; loadCandidates(); });
      document.getElementById("lp-all").addEventListener("click", () => { candMode = "all"; loadCandidates(); });
      document.getElementById("lp-cancel").addEventListener("click", () => { closePicker(); selected = null; setHint(i18n.t("js.physedit.hint_click_node")); redraw(); });

      // G-233: felles redigerings-rad (#eb-*). Fysisk sin velprøvde lagring re-pointes til de FELLES
      // knappene. Auto/Forbedre/Nytt + Rutenett + GPS av/på wires i KJERNEN (likt for alle kart).
      const ebSaveDraft = document.getElementById("eb-save-draft");
      if (ebSaveDraft) ebSaveDraft.addEventListener("click", async () => {
        status.textContent = i18n.t("js.physedit.saving");
        try { if (await saveState(true)) { status.textContent = i18n.t("js.physedit.saved_draft"); setTimeout(() => status.textContent = "", 2500); } }
        catch (e) { status.textContent = i18n.t("js.physedit.error"); }
      });
      const touchUrl = U.save.replace(/\/save$/, "/touch");
      setInterval(() => { try { fetch(touchUrl, { method: "POST" }).catch(() => {}); } catch (e) { /* noop */ } }, 4 * 60 * 1000);
      const ebActivate = document.getElementById("eb-activate");
      if (ebActivate) ebActivate.addEventListener("click", async () => {
        status.textContent = i18n.t("js.physedit.activating");
        try {
          if (!(await ensureSaved())) return;
          const r = await fetch(U.publish, { method: "POST" });
          if (r.ok || r.redirected) { go((r.redirected && r.url) ? r.url : U.view); }
          else status.textContent = i18n.t("js.physedit.publish_failed");
        } catch (e) { status.textContent = i18n.t("js.physedit.publish_failed"); }
      });
      const ebDiscard = document.getElementById("eb-discard-draft");
      // G-260: «Forkast» = delt to-trinns peel i kjernen. Ulagrede endringer → revert til sist lagrede
      // (reload editor-siden → re-les persistert arbeidskopi, bli i redigering). Rent → forkast HELE
      // arbeidskopien → tilbake til aktiv (maps.discard_draft redirecter til editoren på det aktive kartet).
      if (ebDiscard) ebDiscard.addEventListener("click", () => ctx.forkast({
        reloadToSaved: () => go(U.edit), discardUrl: U.discard_draft }));
      // G-233: Rediger-toggle (fysisk er nå toggle-basert, som logisk). Når AV låses ALLE edit-
      // kontroller: felles rad (kjernen) + fysiske spesial-knapper. Drag/node-meny er også gated på
      // state.editing. (Senere ACL-gating på selve Rediger-knappen — G-234.)
      const ebToggle = document.getElementById("eb-edit-toggle");
      const _physSpecials = ["link-btn", "res-sel", "mapsize-btn", "add-host-btn"];
      const _applyEdit = () => {
        const on = ctx.state.editing;
        if (ebToggle) ebToggle.classList.toggle("on", on);
        if (ctx.setCommonEditEnabled) ctx.setCommonEditEnabled(on);   // felles rad (auto/save/grid/gps/bg)
        _physSpecials.forEach(id => { const b = document.getElementById(id); if (b) b.disabled = !on; });
        const ext = document.getElementById("ext-show");              // Kategorier-dropdown
        if (ext) { ext.style.pointerEvents = on ? "" : "none"; ext.style.opacity = on ? "" : "0.45"; if (!on) ext.removeAttribute("open"); }
        redraw();
      };
      if (ebToggle) ebToggle.addEventListener("click", () => { ctx.state.editing = !ctx.state.editing; _applyEdit(); });
      _applyEdit();   // init: editing=false → alt låst, ikke i redigering
      const addHostBtn = document.getElementById("add-host-btn");
      if (addHostBtn) addHostBtn.addEventListener("click", async () => {
        const sel = document.getElementById("add-host-sel"); if (!sel || !sel.value) return;
        status.textContent = i18n.t("js.physedit.adding");
        try {
          if (!(await ensureSaved())) return;
          const r = await fetch(U.add_host, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ host_id: sel.value }) });
          const j = await r.json().catch(() => ({}));
          if (r.status === 409) { status.textContent = ""; alert(j.message || i18n.t("js.physedit.map_full")); return; }
          if (!r.ok || r.redirected) { status.textContent = "⚠ " + (j.message || j.msg || (r.redirected ? i18n.t("js.physedit.session_expired") : i18n.t("js.physedit.error_status", { status: r.status }))); return; }
          if (j.last && j.message) alert(j.message);
          go(U.edit);
        } catch (e) { status.textContent = i18n.t("js.physedit.error"); }
      });

      // oppløsning / node-størrelse / rutenett
      const resSel = document.getElementById("res-sel");
      if (resSel) {
        const cur = cfg.width + "x" + cfg.height;
        if (![...resSel.options].some(o => o.value === cur)) {
          const o = document.createElement("option"); o.value = cur; o.textContent = cfg.width + "×" + cfg.height;
          resSel.insertBefore(o, resSel.lastElementChild);
        }
        resSel.value = cur;
        resSel.addEventListener("change", () => {
          const back = cfg.width + "x" + cfg.height; let w, h;
          if (resSel.value === "custom") {
            const s = prompt(i18n.t("js.physedit.res_prompt"), back);
            if (!s) { resSel.value = back; return; }
            const m = s.toLowerCase().split(/[x×]/); w = parseInt(m[0], 10); h = parseInt(m[1], 10);
            if (!(w >= 200 && w <= 10000 && h >= 200 && h <= 10000)) { alert(i18n.t("js.physedit.res_invalid")); resSel.value = back; return; }
          } else { const m = resSel.value.split("x"); w = +m[0]; h = +m[1]; }
          applyResolution(w, h);
        });
      }
      // G-233: node-størrelse er en KNAPP som veksler Liten/Medium/Stor (i stedet for dropdown).
      const mapSizeBtn = document.getElementById("mapsize-btn");
      if (mapSizeBtn) {
        const SZ = ["S", "M", "L"], LBL = { S: i18n.t("js.physedit.size_s"), M: i18n.t("js.physedit.size_m"), L: i18n.t("js.physedit.size_l") };
        const syncSize = () => { mapSizeBtn.textContent = "Node: " + (LBL[mapNodeSize] || mapNodeSize); };
        syncSize();
        mapSizeBtn.addEventListener("click", () => {
          mapNodeSize = SZ[(SZ.indexOf(mapNodeSize) + 1) % SZ.length];
          syncSize(); redraw(); setHint(i18n.t("js.physedit.hint_default_size", { size: LBL[mapNodeSize] }));
        });
      }
      // G-233: Rutenett (#eb-grid-sel) wires nå i KJERNEN (felles). saveState leser state.gridSize.

      // klikk utenfor kontekstmenyen lukker den
      document.addEventListener("pointerdown", (e) => { if (!ctxMenu.hidden && !ctxMenu.contains(e.target)) hideMenu(); }, true);

      // G-232: BEVAR arbeidskopien ved exit/navigering (ikke forkast) — slik at du kan gå tilbake
      // og fortsette der du slapp. Server gjenopptar eksisterende scratch/draft ved re-inngang;
      // bakgrunns-sweepen (grace-vindu) rydder ekte forlatte scratcher. Forkast skjer KUN eksplisitt
      // via «Forkast». Dette gjør exit-oppførselen lik på alle kart (logisk forkaster heller ikke).
      // (Tidligere G-109: pagehide-beacon forkastet scratchen — fjernet, mistet arbeid ved navigering.)

      // G-157: kartnavn inline-omdøp
      initRename();
    }
  };

  function initRename() {
    const span = document.getElementById("map-name");
    if (!span || !U.rename) return;
    span.addEventListener("click", () => {
      const cur = span.textContent.trim();
      const inp = document.createElement("input");
      inp.value = cur; inp.style.cssText = "font:inherit;width:60%;max-width:420px";
      const note = document.createElement("div"); note.className = "hint"; note.style.margin = "4px 0 0";
      span.replaceWith(inp); inp.after(note); inp.focus(); inp.select();
      let done = false;
      const restore = (text) => {
        const ns = document.createElement("span");
        ns.id = "map-name"; ns.className = "editable-name"; ns.title = i18n.t("js.physedit.rename_title"); ns.textContent = text;
        note.remove(); inp.replaceWith(ns); initRename();
      };
      const commit = async () => {
        if (done) return;
        const v = inp.value.trim();
        if (!v || v === cur) { done = true; restore(cur); return; }
        try {
          const r = await fetch(U.rename, { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body: new URLSearchParams({ name: v }) });
          const d = await r.json().catch(() => ({}));
          if (r.ok && d.status === "ok") { done = true; restore(d.name || v); setHint(i18n.t("js.physedit.hint_renamed", { name: d.name || v })); }
          else { note.style.color = "var(--danger)"; note.textContent = "⚠ " + (d.msg || i18n.t("js.physedit.rename_failed")); inp.focus(); }
        } catch (e) { note.style.color = "var(--danger)"; note.textContent = i18n.t("js.physedit.network_error"); inp.focus(); }
      };
      inp.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); commit(); } else if (e.key === "Escape") { e.preventDefault(); done = true; restore(cur); } });
      inp.addEventListener("blur", commit);
    });
  }

  // --- start ---
  assignBows();
  new M.MapRenderer({ svg, wrap, tip: null, cfg, adapter, isAdmin: true, pinOnDrag: false,
                      nodeGpsUrl: U.node_gps,      // G-237: universelt GPS/NetBox-info-panel (felles kjerne)
                      // Kjernens statusmeldinger (auto-arranger o.l.) til fysisk sitt eget statusfelt
                      controls: { status } });
})();
