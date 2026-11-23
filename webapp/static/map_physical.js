// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.5.2
// SPDX-License-Identifier: AGPL-3.0-or-later
/* G-184 (G-168 fase 3): fysisk kart-VISNING på den DELTE kart-kjernen
   (map_renderer.js). Adapter + glue for det fysiske kartet.

   PRINSIPP (Roger 2026-06-18, spec §5): MOTOREN eier selve linja (geometri/bue —
   `ctx.linkGeom`). Hver ENDE fyller SIN halvdel uavhengig: data hvis vi har det,
   ellers «outage»-variant (NEDE / ingen data). Asymmetri (én ende nede, andre med
   trafikk) faller ut nativt.

   En fysisk link lagres som to interface-rader (A→B + B→A) = de TO ENDENE av ÉN
   link. Vi slår dem sammen til ÉN «render-lenke» med to ender → linja tegnes alltid
   som én helhet med begge halvdeler (fikser «halve linker», G-187). Er bare den ene
   siden mappet (foreldreløs) måles begge retninger fra den ene raden, som eksterne. */
(function () {
  "use strict";
  const M = window.BifrostMap;
  const el = M.el, esc = M.esc, cvar = M.cvar;

  const raw = JSON.parse(document.getElementById("map-data").textContent);
  const svg = document.getElementById("map-svg");
  const tip = document.getElementById("map-tip");
  const wrap = document.getElementById("map-wrap");
  const API = svg.getAttribute("data-api");
  const HOST_URL = svg.getAttribute("data-host-url") || "";   // G-286: satt for operatør+admin (admin- vs inventar-side)

  // --- normaliser fysisk cfg til kjerne-formen (flat width/height/bg_* + rid på noder) ---
  const cv = raw.canvas || {};
  const rawLinks = raw.links || [];
  const cfg = {
    width: cv.width, height: cv.height, background: cv.background,
    node_size: cv.node_size, bg_opacity: cv.bg_opacity,
    bg_mode: raw.bg_mode, bg_live: raw.bg_live, google_session: raw.google_session,
    has_bg_image: !!raw.has_bg, bg_url: raw.bg_url, bg_attr: raw.bg_attr,
    link_hist: raw.link_hist,
    nodes: (raw.nodes || []).map(n => Object.assign({}, n, { rid: String(n.id) })),
    links: []   // settes til render-lenker under (det kjernen itererer)
  };

  // farger (samme :root-tokens som før)
  const CAP = [cvar("--cap-low", "#2ecc71"), cvar("--cap-mid", "#f1c40f"),
               cvar("--cap-high", "#e67e22"), cvar("--cap-crit", "#e74c3c")];
  const MUTED = cvar("--muted", "#6e72a6"), DOWN = cvar("--down", "#ff1f3d");
  const NODE = cvar("--nf", "#1484b6"), EXT = cvar("--bl-300", "#b3b3f6"),
        EXTTXT = cvar("--bl-600", "#4e4feb");
  const INK = cvar("--ink", "#181c56");            // 1.8.0: mørk node-tekst for lys kart-bakgrunn
  const HALO = cfg.background || "#f0f1f7";        // 1.8.2: gråhvit default (ren hvit var for skarp); halo = bakgrunnsfarge
  const SIZE_PX = { S: 11, M: 16, L: 22 };
  const nodeR = n => SIZE_PX[(n && n.size) || cfg.node_size] || 22;

  const nodeById = {};
  cfg.nodes.forEach(n => nodeById[n.id] = n);

  // 1.8.4 (Roger): etiketter ved kartkanten rant UTENFOR rammen. Klamp: hold estimert tekstbredde
  // innenfor viewBox horisontalt; treffer teksten bunnen, legges den OVER noden i stedet.
  function clampLabelX(x, text, fs) {
    const half = Math.min((String(text).length * fs * 0.62) / 2, (cfg.width || 2) / 2 - 4);
    return Math.max(half + 4, Math.min((cfg.width || 0) - half - 4, x));
  }
  function labelY(n, r) {
    const y = n.y + r + 16;
    return y > (cfg.height || 0) - 6 ? n.y - r - 8 : y;   // under kanten → over noden
  }

  let live = {};            // link_id (interface-rad) -> {in,out,capacity,down,nodata}
  let flowMode = false;     // G-173: trafikkflyt-overlegg på/av
  // G-173: antall flyt-stier fra Innstillinger (flow_max_paths), hard klampet 2–6, default 3.
  // Leses ved hver tegning, så en endring på admin slår inn neste gang flyt slås på.
  const MAXFLOWS = Math.max(2, Math.min(6, parseInt(raw.flow_max_paths, 10) || 3));

  function color(util, cap) {
    if (!cap) return MUTED;
    if (util < 0.25) return CAP[0];
    if (util < 0.60) return CAP[1];
    if (util < 0.85) return CAP[2];
    return CAP[3];
  }
  // The line width shows the link's capacity in fixed steps (cap in bps); the colour shows the utilisation.
  // A link below 10G, or with unknown capacity, gets the thinnest line.
  function capWidth(cap) {
    if (cap >= 100e9) return 8;
    if (cap >= 40e9) return 6;
    if (cap >= 10e9) return 4;
    return 2;
  }
  const fmtCap = mbps => !mbps ? "" : (mbps >= 1000 ? (mbps / 1000) + "G" : mbps + "M");
  const ifn = row => row ? (row.if_name || ("if" + (row.if_index || ""))) : "?";

  // G-088: pilspiss INN mot node (port av map_view.js — size=6+w*0.8, pad=nodeR+2).
  function arrowHead(from, tipP, fill, w, nr) {
    const dx = tipP.x - from.x, dy = tipP.y - from.y, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len, pad = (nr || 22) + 2;
    const tx = tipP.x - ux * pad, ty = tipP.y - uy * pad;
    const size = 6 + w * 0.8, back = size * 1.6, side = size;
    const bx = tx - ux * back, by = ty - uy * back, px = -uy, py = ux;
    return el("polygon", { points:
      `${tx},${ty} ${bx + px * side},${by + py * side} ${bx - px * side},${by - py * side}`,
      fill: fill, "fill-opacity": 0.9 });
  }

  // --- bygg RENDER-lenker: eksterne (én rad) + interne (slå sammen gjensidige rader → to ender) ---
  function buildRenderLinks() {
    const out = [];
    rawLinks.forEach(l => {
      if (l.kind === "external")
        out.push({ kind: "external", a_node_id: l.a_node_id, b_external: l.b_external,
                   aRow: l, bRow: null, speed_mbps: l.speed_mbps, _bow: 0, _ext: null });
    });
    // interne: grupper på node-par; par opp gjensidige rader til fysiske lenker; bue parallelle.
    const groups = {};
    rawLinks.forEach(l => {
      if (l.kind !== "internal" || l.b_node_id == null) return;
      const lo = Math.min(l.a_node_id, l.b_node_id), hi = Math.max(l.a_node_id, l.b_node_id);
      (groups[lo + "-" + hi] ||= { lo, hi, rows: [] }).rows.push(l);
    });
    const GAP = 22, capOf = l => (l && l.speed_mbps) || 0, idOf = l => (l && (l.id || l.interface_id)) || 0;
    Object.values(groups).forEach(({ lo, hi, rows }) => {
      const fwd = rows.filter(l => l.a_node_id === lo).sort((x, y) => capOf(y) - capOf(x) || idOf(x) - idOf(y));
      const bwd = rows.filter(l => l.a_node_id !== lo).sort((x, y) => capOf(y) - capOf(x) || idOf(x) - idOf(y));
      const k = Math.max(fwd.length, bwd.length), clusters = [];
      for (let i = 0; i < k; i++) {
        const aRow = fwd[i] || null, bRow = bwd[i] || null;
        clusters.push({ aRow, bRow, cap: Math.max(capOf(aRow), capOf(bRow)) });
      }
      const bows = clusters.map(() => 0);
      if (clusters.length >= 2) {                 // 1.3.14: parallelle fysiske lenker bues fra hverandre
        const maxCap = Math.max(...clusters.map(c => c.cap));
        const atMax = clusters.filter(c => c.cap === maxCap);
        const straight = atMax.length === 1 ? atMax[0] : null;   // unik størst → rett
        const bowed = clusters.filter(c => c !== straight).sort((x, y) => y.cap - x.cap);
        const idx = c => clusters.indexOf(c);
        if (straight) bowed.forEach((c, i) => bows[idx(c)] = (i % 2 ? -1 : 1) * ((i >> 1) + 1) * GAP);
        else { const n = bowed.length; bowed.forEach((c, i) => bows[idx(c)] = (i - (n - 1) / 2) * GAP); }
      }
      clusters.forEach((c, i) => out.push({ kind: "internal", a_node_id: lo, b_node_id: hi,
        aRow: c.aRow, bRow: c.bRow, speed_mbps: c.cap, _bow: bows[i] }));
    });
    return out;
  }

  // Hent rater + status pr. ende for en render-lenke (felles for drawLink + flyt).
  // a = render-lenkens a_node_id, b = b_node_id. intoA/intoB = bps inn mot hver ende.
  function endRates(rl) {
    if (rl.kind === "internal" && rl.aRow && rl.bRow) {
      const da = live[rl.aRow.id] || {}, db = live[rl.bRow.id] || {};
      return { twoSided: true, intoA: da.in || 0, intoB: db.in || 0,
               aDown: da.down === true, bDown: db.down === true,
               aNodata: da.nodata === true, bNodata: db.nodata === true,
               aCap: da.capacity || 0, bCap: db.capacity || 0, aRow: rl.aRow, bRow: rl.bRow,
               aSerie: "in", bSerie: "in" };   // 1.8.1: to-sidig — hver halvdel = «Inn» på SIN rad
    }
    // single-sided: én rad måler begge retninger (ekstern, eller foreldreløs intern).
    const row = rl.aRow || rl.bRow;
    const d = (row && live[row.id]) || {};
    const fromA = !row || row.a_node_id === rl.a_node_id;
    return { twoSided: false, intoA: fromA ? (d.in || 0) : (d.out || 0),
             intoB: fromA ? (d.out || 0) : (d.in || 0),
             aDown: d.down === true, bDown: d.down === true,
             aNodata: d.nodata === true, bNodata: d.nodata === true,
             aCap: d.capacity || 0, bCap: d.capacity || 0, aRow: row, bRow: row,
             aSerie: fromA ? "in" : "out", bSerie: fromA ? "out" : "in" };   // 1.8.1: én rad — halvdel → retning
  }

  // Eksterne endepunkter: pseudo-posisjon på en eike ut fra eier-noden (G-050/053). Statisk.
  function placeExternals(renderLinks) {
    const exts = renderLinks.filter(l => l.kind === "external");
    const ints = renderLinks.filter(l => l.kind === "internal");
    const groups = {};
    exts.forEach(l => (groups[l.a_node_id] ||= []).push(l));
    const hosts = cfg.nodes;
    const gx = hosts.reduce((s, n) => s + n.x, 0) / (hosts.length || 1);
    const gy = hosts.reduce((s, n) => s + n.y, 0) / (hosts.length || 1);
    const W = cfg.width, H = cfg.height, MM = 26;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const segs = ints.map(l => { const a = nodeById[l.a_node_id], b = nodeById[l.b_node_id]; return (a && b) ? [a, b] : null; }).filter(Boolean);
    const segDist = (px, py, a, b) => {
      const dx = b.x - a.x, dy = b.y - a.y, l2 = dx * dx + dy * dy;
      let t = l2 ? ((px - a.x) * dx + (py - a.y) * dy) / l2 : 0; t = Math.max(0, Math.min(1, t));
      return Math.hypot(px - (a.x + t * dx), py - (a.y + t * dy));
    };
    const placed = [];
    const collides = (x, y, rad) => {
      for (const n of hosts) if (Math.hypot(x - n.x, y - n.y) < rad + 34) return true;
      for (const o of placed) if (Math.hypot(x - o.x, y - o.y) < rad + o.rad) return true;
      for (const [a, b] of segs) if (segDist(x, y, a, b) < rad + 6) return true;
      return false;
    };
    Object.entries(groups).forEach(([aid, links]) => {
      const a = nodeById[aid]; if (!a) return;
      let base = Math.atan2(a.y - gy, a.x - gx);
      if (!isFinite(base) || (a.x === gx && a.y === gy)) base = Math.atan2(a.y - H / 2, a.x - W / 2);
      const n = links.length, span = Math.min(Math.PI * 1.5, 0.5 + n * 0.45);
      links.forEach((l, i) => {
        const rad = Math.max(15, (l.b_external || "").length * 3.2 + 7);
        let ang = base + (n > 1 ? (i / (n - 1) - 0.5) * span : 0), R = 90, x = a.x, y = a.y;
        for (let t = 0; t < 60; t++) {
          x = clamp(a.x + R * Math.cos(ang), MM, W - MM);
          y = clamp(a.y + R * Math.sin(ang), MM, H - MM);
          if (!collides(x, y, rad)) break;
          R += 12; if (R > 260) { R = 90; ang += 0.22; }
        }
        l._ext = { x, y }; placed.push({ x, y, rad });
      });
    });
  }

  // --- link-graf-modal (G-097) ---
  const lmodal = document.getElementById("link-modal");
  let lmChart = null;
  const lmState = { row: null, range: "36h" };
  function openLinkGraph(row, serie, infoHtml) {
    if (!lmodal || !row) return;
    lmState.row = row; lmState.range = "36h";
    // 1.8.2: hover-linja (øyeblikks-retning + rate) øverst i modalen — trykk-flaten dekker hover.
    const lmInfo = document.getElementById("lm-info");
    if (lmInfo) { lmInfo.innerHTML = infoHtml || ""; lmInfo.hidden = !infoHtml; }
    // 1.8.1: retningen (halvdelen) du trykket fremheves i grafen; motsatt serie startes «av».
    // (1.8.0s #lm-info-felt fjernet — tittelen har node/if/kapasitet alt, resten viser grafen.)
    lmState.serie = serie || null;
    if (lmChart) lmChart.reset();   // nullstill forklarings-valg fra forrige link/retning
    const a = nodeById[row.a_node_id] || {};
    const lmCap = fmtCap(row.speed_mbps);
    // 1.8.2 (Roger): modalen er en DRILL-DOWN → tittelen bruker EKSAKT routernavn (host.name),
    // ikke visningsnavnet (label). Kartet/hover/feltet snakker fortsatt visningsnavn.
    document.getElementById("lm-title").textContent =
      `${a.name || a.label || ""} · ${ifn(row)}` + (row.role ? ` [${row.role}]` : "") + (lmCap ? ` (${lmCap})` : "");
    document.querySelectorAll("#lm-range button").forEach(b => b.classList.toggle("on", b.dataset.v === "36h"));
    lmodal.hidden = false;
    if (!lmChart) lmChart = makeChart("lm-chart");
    loadLinkGraph();
  }
  async function loadLinkGraph() {
    const row = lmState.row; if (!row) return;
    const url = cfg.link_hist.replace("/0/", "/" + row.id + "/") + "?range=" + lmState.range;
    try {
      const r = await fetch(url);
      if (!r.ok || r.redirected) { lmChart.clear(); document.getElementById("lm-res").textContent = i18n.t("js.physical.history_failed"); return; }
      const d = await r.json();
      document.getElementById("lm-res").textContent = d.resolution ? i18n.t("js.physical.resolution", { res: d.resolution }) : "";
      // 1.8.1: fremhev trykket retning — motsatt serie hidden (uPlot-forklaringen slår den på igjen).
      if (lmState.serie && d.series) d.series = d.series.map(
        se => Object.assign({}, se, { hidden: (se.code || se.name) !== lmState.serie }));
      if (d.t && d.t.length) lmChart.setData(d, ""); else lmChart.clear();
    } catch (e) { console.error("link-historikk feilet", e); }
  }

  // Fyll ÉN halvdel (node→bmid) ut fra ENDENS status: data (trafikk-farge) eller outage (NEDE/ingen data).
  function fillHalf(svgEl, node, ctrl, bmid, end, ctx) {
    const dp = `M ${node.x} ${node.y} Q ${ctrl.x} ${ctrl.y} ${bmid.x} ${bmid.y}`;
    let stroke, w, op, dash = null, info, labelTxt = null;
    if (end.down) {
      stroke = DOWN; w = 3.5; op = 0.8; dash = "7 5"; labelTxt = i18n.t("js.physical.down");
      info = `<b class="down-text">● ${i18n.t("js.physical.down")} — ${esc(end.title)}</b><br>oper down`;   // colour from CSS, never a theme value in HTML
    } else if (end.nodata) {
      stroke = MUTED; w = 2; op = 0.6; dash = "2 5"; labelTxt = i18n.t("js.physical.no_data");
      info = `<b>${esc(end.title)}</b><br>${i18n.t("js.physical.no_fresh_data")}`;
    } else {
      const util = end.cap ? end.rate / end.cap : 0;
      w = capWidth(end.cap);
      stroke = color(util, end.cap); op = 0.75;
      const pct = end.cap ? ` (${(util * 100).toFixed(0)}%)` : " (" + i18n.t("js.physical.unknown_cap") + ")";
      info = `<b>${esc(end.title)}</b><br>${esc(end.dir)}: ${fmtBps(end.rate)}${pct}`;
    }
    const line = el("path", { d: dp, fill: "none", stroke, "stroke-width": w,
      "stroke-opacity": op, "stroke-linecap": "round" });
    if (dash) line.setAttribute("stroke-dasharray", dash);
    const hit = el("path", { d: dp, fill: "none", stroke: "transparent",
      "stroke-width": Math.max(16, w + 8), "class": "bf-cur-pointer" });
    hit.addEventListener("mousemove", e => ctx.show(e, info));
    hit.addEventListener("mouseleave", ctx.hide);
    // 1.8.1/1.8.2 (Roger): hover beholdes; trykket åpner grafen RETNINGSBEVISST (halvdelen du
    // trykket = serien som vises) OG viser hover-linja øverst i modalen (røk i 1.8.1-ryddingen —
    // tilbake på Rogers ord: det er DEN ene linja som gir øyeblikks-tallet på touch).
    hit.addEventListener("click", () => openLinkGraph(end.row, end.serie, info));
    svgEl.appendChild(line); svgEl.appendChild(hit);
    if (!end.down && !end.nodata) {
      svgEl.appendChild(arrowHead(ctrl, node, stroke, w, end.nodeRad));
    } else if (end.showLabel && labelTxt) {
      const mm = { x: (node.x + bmid.x) / 2, y: (node.y + bmid.y) / 2 };
      const t = el("text", { x: mm.x, y: mm.y - 6, "text-anchor": "middle",
        fill: stroke, "font-size": end.down ? 11 : 10, "font-weight": 600 });
      t.textContent = labelTxt; svgEl.appendChild(t);
    }
  }

  // ============================ ADAPTER ============================
  const adapter = {
    drawNode(svgEl, n, ctx) {
      const circ = el("circle", { cx: n.x, cy: n.y, r: nodeR(n), fill: NODE,
        stroke: HALO, "stroke-width": 3 });
      // 1.8.2 (Roger): trykk på node → info + link TILBAKE til hosten (admin-sida, #if-<id>-ankeret).
      // Kun når data-admin-url er satt (innlogget admin-visning) — public/visning ellers uberørt;
      // ikke i redigering (editoren eier node-klikk/dra der).
      if (HOST_URL && ctx && !ctx.state.editing) {
        circ.style.cursor = "pointer";
        circ.addEventListener("click", (ev) => {
          ev.stopPropagation();
          let info = `<b>${esc(n.label || n.name)}</b><br>${i18n.t("js.physical.pop_host", { name: esc(n.name) })}`
            + (n.address ? `<br>${i18n.t("js.physical.pop_address", { address: esc(n.address) })}` : "");
          if (n.site_slug || n.site) {              // G-286: site-slug + GPS (host\u2192site), operat\u00f8r/admin
            const gps = (n.lat !== null && n.lat !== undefined && n.lon !== null && n.lon !== undefined)
              ? ` (${n.lat}, ${n.lon})` : " (" + i18n.t("js.physical.no_gps") + ")";
            info += `<br>\u{1F4CD} ${esc(n.site_slug || n.site)}${gps}`;
          }
          const act = n.host_id
            ? '<a class="pop-act" href="' + HOST_URL + "#host-" + n.host_id + '">' + i18n.t("js.physical.open_host") + '</a>' : "";
          ctx.showPop(ev, info, act);
        });
      }
      svgEl.appendChild(circ);
      const _lbl = n.label || n.name;
      const t = el("text", { x: clampLabelX(n.x, _lbl, 13), y: labelY(n, nodeR(n)),
        "text-anchor": "middle", fill: INK,
        "font-size": 13, "font-weight": 600, stroke: HALO, "stroke-width": 3.5,
        "paint-order": "stroke", "stroke-linejoin": "round" });
      t.textContent = _lbl; svgEl.appendChild(t);
    },

    drawLink(svgEl, rl, ctx) {
      const a = nodeById[rl.a_node_id];
      const b = rl.kind === "internal" ? nodeById[rl.b_node_id] : rl._ext;
      if (!a || !b) return;
      const geom = ctx.linkGeom(a, b, rl._bow);     // MOTOREN eier linja
      const er = endRates(rl);
      const linkCap = (rl.speed_mbps || 0) * 1e6;
      const aN = a.label || a.name || "A";
      const bN = rl.kind === "internal" ? (b.label || b.name || "B") : (rl.b_external || "B");
      const capSuffix = (() => { const c = fmtCap(rl.speed_mbps); return c ? ` · ${c}` : ""; })();
      const ifA = ifn(er.aRow);   // G-251: var identisk ternær (begge grener like) — forenklet
      const ifB = er.twoSided ? ifn(er.bRow) : ifn(er.aRow);

      const aEnd = { rate: er.intoA, down: er.aDown, nodata: er.aNodata,
        cap: er.aCap || linkCap, row: er.aRow, nodeRad: nodeR(a), showLabel: true,
        title: `${aN} · ${ifA}${capSuffix}`, dir: `${bN} → ${aN}`, serie: er.aSerie };
      const bEnd = { rate: er.intoB, down: er.bDown, nodata: er.bNodata,
        cap: er.bCap || linkCap, row: er.bRow,
        nodeRad: rl.kind === "external" ? 7 : nodeR(b),
        showLabel: rl.kind !== "external",   // ekstern: ikke dobbel NEDE-label
        title: rl.kind === "external" ? `${aN} · ${ifA}${capSuffix}` : `${bN} · ${ifB}${capSuffix}`,
        dir: `${aN} → ${bN}`, serie: er.bSerie };

      // hver ende fyller SIN halvdel uavhengig
      fillHalf(svgEl, a, geom.Qa, geom.bmid, aEnd, ctx);
      fillHalf(svgEl, b, geom.Qb, geom.bmid, bEnd, ctx);

      // eksternt pseudo-endepunkt (liten node + label)
      if (rl.kind === "external" && rl._ext) {
        svgEl.appendChild(el("circle", { cx: rl._ext.x, cy: rl._ext.y, r: 7, fill: EXT,
          stroke: HALO, "stroke-width": 1.5 }));
        const _ey = rl._ext.y - 11 < 12 ? rl._ext.y + 20 : rl._ext.y - 11;   // 1.8.4: klamp mot toppen
        const t = el("text", { x: clampLabelX(rl._ext.x, rl.b_external || "", 11), y: _ey,
          "text-anchor": "middle", fill: EXTTXT,
          "font-size": 11, stroke: HALO, "stroke-width": 3, "paint-order": "stroke", "stroke-linejoin": "round" });
        t.textContent = rl.b_external || ""; svgEl.appendChild(t);
      }
    },

    afterDraw(svgEl, ctx) {
      if (flowMode) drawFlow(svgEl, ctx);
    },

    initControls(ctx) {
      const state = { at: null };
      let timer = null;
      const apiUrl = () => API + (state.at ? ("?at=" + state.at) : "");
      let failCount = 0, staleSince = null, staleEl = null;
      function showStaleBanner() {
        if (!staleEl) {
          staleEl = document.createElement("div");
          staleEl.style.cssText = "position:absolute;top:10px;left:50%;transform:translateX(-50%);" +
            "z-index:40;background:rgba(178,28,46,.93);color:#fff;font-weight:600;font-size:14px;" +
            "padding:8px 16px;border-radius:8px;pointer-events:none;white-space:nowrap";
          wrap.appendChild(staleEl);
        }
        // G-307: vis klokkeslett i appens tidssone (window.APP_TZ), ikke nettleserens lokaltid.
        const _hm = staleSince.toLocaleTimeString(i18n.locale,
          { hour: "2-digit", minute: "2-digit", timeZone: window.APP_TZ || "Europe/Oslo" });
        staleEl.textContent = i18n.t("js.physical.stale", { time: _hm });
        staleEl.style.display = "";
      }
      const hideStaleBanner = () => { if (staleEl) staleEl.style.display = "none"; };
      async function refresh() {
        try {
          const r = await fetch(apiUrl());
          if (!r.ok || r.redirected) throw new Error("http " + r.status);
          const d = await r.json();
          if (!d || !Array.isArray(d.links)) throw new Error("svar uten links-felt");
          live = {};
          d.links.forEach(l => live[l.id] = l);
          failCount = 0; staleSince = null; hideStaleBanner();
          ctx.draw();
        } catch (e) {
          console.error("map data failed", e);
          failCount++;
          if (!staleSince) staleSince = new Date();
          if (failCount >= 2) showStaleBanner();
        }
      }
      const startLive = () => { if (!timer) timer = setInterval(refresh, 30000); };
      const stopLive = () => { if (timer) { clearInterval(timer); timer = null; } };

      const slider = document.getElementById("t-slider");
      const tLabel = document.getElementById("t-label");
      const fmtTime = epoch => new Date(epoch * 1000).toLocaleString(i18n.locale,
        { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
          timeZone: window.APP_TZ || "Europe/Oslo" });   // G-307: appens tidssone
      function applyTime() {
        const v = parseInt(slider.value, 10);
        if (v >= 0) { state.at = null; tLabel.textContent = i18n.t("js.physical.now_live"); startLive(); }
        else { state.at = Math.floor(Date.now() / 1000) + v * 60;
               tLabel.textContent = "↩ " + fmtTime(state.at); stopLive(); }
        refresh();
      }
      if (slider) {
        const step = d => { slider.value = Math.min(0, Math.max(+slider.min, +slider.value + d)); applyTime(); };
        slider.addEventListener("input", applyTime);
        document.getElementById("t-now").addEventListener("click", () => { slider.value = 0; applyTime(); });
        document.getElementById("t-back").addEventListener("click", () => step(-5));
        document.getElementById("t-fwd").addEventListener("click", () => step(5));
      }

      if (lmodal) {
        document.getElementById("lm-close").addEventListener("click", () => lmodal.hidden = true);
        lmodal.addEventListener("click", e => { if (e.target === lmodal) lmodal.hidden = true; });
        document.querySelectorAll("#lm-range button").forEach(b =>
          b.addEventListener("click", () => {
            document.querySelectorAll("#lm-range button").forEach(x => x.classList.remove("on"));
            b.classList.add("on"); lmState.range = b.dataset.v; loadLinkGraph();
          }));
      }

      const flowBtn = document.getElementById("flow-toggle");
      if (flowBtn) flowBtn.addEventListener("click", () => {
        flowMode = !flowMode; flowBtn.classList.toggle("on", flowMode); ctx.draw();
      });

      refresh(); startLive();
    }
  };

  // G-173: trafikkflyt-overlegg (elve-modell) — de 3 største flytene som bølgende piler.
  function drawFlow(svgEl, ctx) {
    const byKey = {};
    cfg.links.forEach(rl => {
      if (rl.kind !== "internal") return;
      const a = nodeById[rl.a_node_id], b = nodeById[rl.b_node_id];
      if (!a || !b) return;
      const er = endRates(rl);
      if (er.aDown || er.bDown || er.aNodata || er.bNodata) return;
      const rate = Math.max(er.intoA, er.intoB);
      if (rate <= 0) return;
      const towardB = er.intoB >= er.intoA, k = rl.a_node_id + "-" + rl.b_node_id;
      if (!byKey[k] || rate > byKey[k].rate)
        byKey[k] = { from: towardB ? a.id : b.id, to: towardB ? b.id : a.id, rate, key: k };
    });
    const linkFlows = Object.values(byKey);
    if (!linkFlows.length) return;
    const maxRate = Math.max(...linkFlows.map(f => f.rate));
    const out = {}, inc = {};
    linkFlows.forEach(f => { (out[f.from] ||= []).push(f); (inc[f.to] ||= []).push(f); });
    Object.values(out).forEach(arr => arr.sort((x, y) => y.rate - x.rate));
    Object.values(inc).forEach(arr => arr.sort((x, y) => y.rate - x.rate));
    const used = new Set(), paths = [];
    for (const seed of linkFlows.slice().sort((x, y) => y.rate - x.rate)) {
      if (paths.length >= MAXFLOWS) break;
      if (used.has(seed.key) || seed.rate < 0.15 * maxRate) continue;
      const ids = [seed.from, seed.to]; used.add(seed.key);
      const inPath = new Set(ids);
      let cur = seed.to, g = 0;
      while (g++ < 8) { const nx = (out[cur] || []).find(f => !used.has(f.key) && !inPath.has(f.to)); if (!nx) break; used.add(nx.key); ids.push(nx.to); inPath.add(nx.to); cur = nx.to; }
      cur = seed.from; g = 0;
      while (g++ < 8) { const pv = (inc[cur] || []).find(f => !used.has(f.key) && !inPath.has(f.from)); if (!pv) break; used.add(pv.key); ids.unshift(pv.from); inPath.add(pv.from); cur = pv.from; }
      paths.push({ nodes: ids.map(id => nodeById[id]), rate: seed.rate });
    }
    const FLOW = cvar("--flow", "#7c3aed");
    const catmull = pts => {
      let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
      for (let i = 0; i < pts.length - 1; i++) {
        const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || pts[i + 1];
        d += ` C ${(p1.x + (p2.x - p0.x) / 6).toFixed(1)} ${(p1.y + (p2.y - p0.y) / 6).toFixed(1)}` +
             ` ${(p2.x - (p3.x - p1.x) / 6).toFixed(1)} ${(p2.y - (p3.y - p1.y) / 6).toFixed(1)}` +
             ` ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`;
      }
      return d;
    };
    paths.forEach(p => {
      const ns = p.nodes, OFF = 20;
      const pts = ns.map((n, i) => {
        const prev = ns[i - 1] || n, next = ns[i + 1] || n;
        const dx = next.x - prev.x, dy = next.y - prev.y, L = Math.hypot(dx, dy) || 1;
        return { x: n.x + (-dy / L) * OFF, y: n.y + (dx / L) * OFF };
      });
      const last = ns[ns.length - 1], pen = ns[ns.length - 2] || ns[0];
      const ldx = last.x - pen.x, ldy = last.y - pen.y, LL = Math.hypot(ldx, ldy) || 1;
      const stop = nodeR(last) + 26;
      pts[pts.length - 1] = { x: last.x - (ldx / LL) * stop, y: last.y - (ldy / LL) * stop };
      const w = 3 + (p.rate / maxRate) * 7;
      const d = catmull(pts);
      svgEl.appendChild(el("path", { d, fill: "none", stroke: FLOW, "stroke-width": w,
        "stroke-opacity": 0.82, "stroke-linecap": "round", "stroke-linejoin": "round" }));
      svgEl.appendChild(arrowHead(pts[pts.length - 2] || pts[0], pts[pts.length - 1], FLOW, w, 0));
      const route = ns.map(n => n.label || n.name).join(" → ");
      const hit = el("path", { d, fill: "none", stroke: "transparent",
        "stroke-width": Math.max(18, w + 10), "class": "bf-cur-help" });
      hit.addEventListener("mousemove", e => ctx.show(e, `<b>${i18n.t("js.physical.flow_estimate")}</b><br>${esc(route)}<br>~${fmtBps(p.rate)}`));
      hit.addEventListener("mouseleave", ctx.hide);
      svgEl.appendChild(hit);
    });
  }

  // --- bygg render-lenker + statiske pre-pass, så start kjernen ---
  const renderLinks = buildRenderLinks();
  placeExternals(renderLinks);
  cfg.links = renderLinks;
  new M.MapRenderer({ svg, wrap, tip, cfg, adapter, isAdmin: false });
})();
