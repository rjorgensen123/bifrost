// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
// SPDX-License-Identifier: AGPL-3.0-or-later
/* Logisk OSPF-adapter for den generiske kart-kjernen (G-168, map_renderer.js).
   Leverer det type-spesifikke for det logiske kartet:
     - node-form: avrundet rektangel; ghost (umonitorert) stiplet/grå; ABR-ring,
     - lenke: alltid farget etter naboskap-tilstand (full = grønn / ellers stiplet
       rød) + cost-«chips» pr. retning (manuelt satt cost = fylt oransje chip + «M»),
     - synlighets-filter for ghost-noder/-lenker,
     - expand-in-place: klikkbare bobler ved ABR + utfoldet delgraf inline,
     - meny-tillegg «Adopter» for ghost-noder,
     - kontrollene «Vis/skjul ukjente» og navn/IP-toggle.

   Topologien OPPDAGES (SNMP) av backend (ospf.py) — derfor capabilities uten
   opprett-node/-lenke. Ingen eksterne libs. */
(function (global) {
  "use strict";

  // G-179: stabil, distinkt farge pr. OSPF-area — area-ets identitet, brukt konsistent
  // overalt (expand-bobler + dropdown/area-meny-swatch). Backbone (0.0.0.0) fast farge;
  // de andre fra en palett som UNNGÅR grønn/rød (de er reservert lenke-tilstand: full/nede).
  const AREA_PALETTE = ["#2f6fdb", "#7c4dff", "#e8820c", "#0b9e9e", "#c2389a", "#3f51b5",
                        "#0097c7", "#9c27b0", "#d2622a", "#5c6bc0", "#8d6e63", "#c79a00"];
  function areaColor(areaId) {
    const s = String(areaId == null ? "" : areaId);
    if (s === "0.0.0.0") return "#1a237e";   // backbone (0.0.0.0) — sterk, dominant indigo
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
    return AREA_PALETTE[h % AREA_PALETTE.length];
  }

  // Cost-«chip»: ugjennomsiktig boble bak metric-tallet, lesbar mot enhver bakgrunn.
  // manual=true → fylt accent-chip (oransje) + «M»; false → hvit chip; null → ukjent.
  function costChip(x, y, value, manual, ctx) {
    const g = ctx.el("g", {});
    const on = manual === true;
    const s = String(value) + (on ? " M" : "");
    const fs = 14;
    const w = s.length * 8.4 + 16, h = 22;
    const accent = ctx.WARNING;
    g.appendChild(ctx.el("rect", {
      x: x - w / 2, y: y - h / 2, width: w, height: h, rx: 6, ry: 6,
      fill: on ? accent : "#ffffff", "fill-opacity": 1,
      stroke: on ? accent : ctx.INK, "stroke-width": on ? 2 : 1.5, "stroke-opacity": 1
    }));
    const t = ctx.el("text", {
      x: x, y: y + 5, "text-anchor": "middle",
      fill: on ? "#ffffff" : ctx.INK, "font-size": fs, "font-weight": 700, "fill-opacity": 1
    });
    t.textContent = s;
    g.appendChild(t);
    const ti = ctx.el("title", {});
    ti.textContent = on ? i18n.t("js.logical.cost_manual")
      : manual === false ? i18n.t("js.logical.cost_auto") : i18n.t("js.logical.cost_unknown");
    g.appendChild(ti);
    return g;
  }

  // G-175: tilstand for ÉN ende av et naboskap (per-halvdel). `near` = denne endens state,
  // `far` = motpartens (brukes når near mangler — ghost/uleste; 'full' krever toveis → oppe).
  function _halfState(near, far) {
    const s = near != null ? near : far;
    if (s == null) return "unknown";
    if (s === "full") return "up";
    if (s === "down") return "down";
    return "oneway";   // init/2way/exchange = forming/1-vei
  }
  function _halfStyle(st, ctx) {
    if (st === "up") return { stroke: ctx.OK, dash: "", w: 2.5, op: 0.95 };
    if (st === "oneway") return { stroke: ctx.WARNING, dash: "7 5", w: 3, op: 0.9 };   // amber/1-vei
    if (st === "down") return { stroke: ctx.BAD, dash: "7 5", w: 3, op: 0.85 };         // dempet rød (= ifOperDown)
    return { stroke: ctx.FAINT, dash: "2 5", w: 2, op: 0.7 };                            // ukjent — grå
  }

  // Lenke: deles i to halvdeler fra midtpunktet ut mot hver node, hver med pilspiss inn mot
  // noden + cost-chip for sin retning. G-175: HVER halvdel farges av SIN endes tilstand, så
  // asymmetri (1-vei / én side nede) vises nativt. `rids` = rid→node-oppslag for endene.
  function drawLink(parent, l, rids, ctx) {
    const a = rids[l.a], b = rids[l.b];
    if (!a || !b) return;
    // Per-halvdel tilstand: halvdelen nær A av A-endens state, nær B av B-endens.
    const aState = _halfState(l.a_state, l.b_state);
    const bState = _halfState(l.b_state, l.a_state);

    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    const PAD = 40;
    [[a, aState], [b, bState]].forEach(([node, st]) => {
      const sy = _halfStyle(st, ctx);
      const seg = ctx.el("line", {
        x1: mid.x, y1: mid.y, x2: node.x, y2: node.y,
        stroke: sy.stroke, "stroke-width": sy.w, "stroke-opacity": sy.op, "stroke-linecap": "round"
      });
      if (sy.dash) seg.setAttribute("stroke-dasharray", sy.dash);
      parent.appendChild(seg);
      parent.appendChild(ctx.arrowHead(mid, node, sy.stroke, sy.w, PAD));
    });

    // Usynlig «hit»-linje for tooltip (hele lenken).
    const hit = ctx.el("line", {
      x1: a.x, y1: a.y, x2: b.x, y2: b.y,
      stroke: "transparent", "stroke-width": 16
    });
    const src = m => m === true ? " (" + i18n.t("js.logical.src_manual") + ")" : m === false ? " (auto)" : "";
    const word = { oneway: i18n.t("js.logical.state_oneway"), down: i18n.t("js.logical.state_down"),
                   unknown: i18n.t("js.logical.state_unknown") };
    const ends = [];
    if (aState !== "up") ends.push(`${ctx.esc(a.label)}: ${word[aState]}`);
    if (bState !== "up") ends.push(`${ctx.esc(b.label)}: ${word[bState]}`);
    const info = `<b>${ctx.esc(a.label)} ↔ ${ctx.esc(b.label)}</b>`
      + (ends.length ? `<br>⚠ ${ends.join(" · ")}` : "")
      + `<br>cost ${ctx.esc(a.label)}→: ${ctx.esc(l.a_metric)}${src(l.a_manual)} · `
      + `cost ${ctx.esc(b.label)}→: ${ctx.esc(l.b_metric)}${src(l.b_manual)}`;
    hit.addEventListener("mousemove", e => ctx.show(e, info));
    hit.addEventListener("mouseleave", ctx.hide);
    parent.appendChild(hit);

    // Cost-chip pr. retning, nær sin pil: a→b nær b (frac 0.74), b→a nær a (0.26).
    const pAB = ctx.endLabelPos(a, b, 0.74);
    const pBA = ctx.endLabelPos(a, b, 0.26);
    // G-200: cost-chips vises kun når OSPF-metric-toggelen er på (reduserer info på skjermen).
    if (ctx.state.showMetric !== false) {
      if (l.a_metric != null) parent.appendChild(costChip(pAB.x, pAB.y, l.a_metric, l.a_manual, ctx));
      if (l.b_metric != null) parent.appendChild(costChip(pBA.x, pBA.y, l.b_metric, l.b_manual, ctx));
    }
  }

  // Bredde til en node-boks gitt etiketten (delt av node-tegner + delgraf-bbox).
  // Speiler drawNode: vrapp over opptil 2 linjer, bredden følger lengste linje.
  function nodeWidth(disp, ctx) {
    const lines = ctx.wrapLabel(disp);
    const longest = lines.reduce((mx, s) => Math.max(mx, s.length), 0);
    return Math.max(70, longest * 7.2 + 22);
  }
  function nodeLabel(n, ctx) {
    const host = n.label || n.rid || "";
    const ip = n.rid || "";
    return (ctx.state.naming === "ip") ? (ip || host) : (host || ip);
  }

  // Naboskap-oppsummering pr. ruter (fra lenkene i DETTE området): {n, down}.
  // Memoisert på ctx (lenkene endres ikke mellom redraws). Brukes i ruter-hoveren.
  function adjFor(rid, ctx) {
    if (!ctx._adj) {
      const m = {};
      (ctx.cfg.links || []).forEach(l => {
        [l.a, l.b].forEach(r => {
          m[r] = m[r] || { n: 0, down: 0 };
          m[r].n++;
          if (l.up !== true) m[r].down++;
        });
      });
      ctx._adj = m;
    }
    return ctx._adj[rid] || { n: 0, down: 0 };
  }

  // Node: avrundet rektangel, 1–2 vrappede linjer, ghost/ABR-styling, pinn-prikk.
  // opts.bubbles === false → delgraf-node (ingen rekursiv utfolding, ingen drag).
  function drawNode(parent, n, ctx, opts) {
    opts = opts || {};
    const ghost = n.ghost === true;
    const stale = n.stale === true && !ghost;   // our host, but no fresh data in the last build
    const faint = ghost || stale;
    const abr = n.abr === true;
    const host = n.label || n.rid || "";
    const ip = n.rid || "";
    const disp = nodeLabel(n, ctx);
    const lines = ctx.wrapLabel(disp);
    const W = nodeWidth(disp, ctx), H = lines.length > 1 ? 44 : 30;
    const x = n.x - W / 2, y = n.y - H / 2;

    const g = ctx.el("g", {});

    // Hover (alfa & omega): ROLLE + områder + ekte OSPF-status for ruteren.
    const _areas = [ctx.cfg.area].concat(n.other_areas || []);
    const _role = ghost ? i18n.t("js.logical.role_ghost")
      : abr ? i18n.t("js.logical.role_abr")
      : i18n.t("js.logical.role_internal");
    const _adj = adjFor(n.rid, ctx);
    const _status = _adj.n
      ? i18n.t("js.logical.adj_count", {n: _adj.n})
        + (_adj.down ? " · <b>" + i18n.t("js.logical.adj_not_up", {n: _adj.down}) + "</b>" : " · " + i18n.t("js.logical.adj_all_up"))
      : i18n.t("js.logical.adj_none");
    const _tipBase = `<b>${ctx.esc(host)}</b><br>`
      + i18n.t("js.logical.tip_ip", {ip: ctx.esc(ip)}) + "<br>"
      + i18n.t("js.logical.tip_role", {role: ctx.esc(_role)}) + "<br>"
      + i18n.t("js.logical.tip_areas", {areas: ctx.esc(_areas.join(", "))}) + "<br>"
      + _status
      + (stale ? "<br><i>" + i18n.t("js.logical.no_answer") + "</i>" : "");
    // 1.8.0/K2: hover-tip beholder hintet; popup-en (trykk) får _tipBase — den HAR Adopter-knappen.
    const _tip = _tipBase
      + (ghost && ctx.isAdmin && !ctx.state.editing ? "<br><i>" + i18n.t("js.logical.click_adopt") + "</i>" : "");
    g.addEventListener("mousemove", e => ctx.show(e, _tip));
    g.addEventListener("mouseleave", ctx.hide);

    const rect = ctx.el("rect", {
      x: x, y: y, width: W, height: H, rx: 8, ry: 8,
      fill: faint ? "none" : ctx.GRID,
      stroke: faint ? ctx.FAINT : ctx.INK,
      "stroke-width": faint ? 1.5 : (abr ? 3 : 2),   // G-179: ABR = kraftigere kant
      "fill-opacity": faint ? 1 : 0.85
    });
    if (faint) rect.setAttribute("stroke-dasharray", "5 4");
    // 1.8.2 (Roger): ALLE noder får trykk-info (samme innhold som hover) — kun ghost har
    // «Adopter»-handling (admin). Ikke i redigering (editor eier klikk/dra), ikke i flyt-modus
    // (klikk = velg node), ikke på public (ingen interaktive popups der).
    if (!ctx.state.editing && !ctx.state.flowMode && !ctx.public) {
      g.style.cursor = "pointer";
      const _act = (ghost && ctx.isAdmin)
        ? '<a class="pop-act" href="' + ctx.adoptUrl + "?adopt_ip=" + encodeURIComponent(n.rid) + '">' + i18n.t("js.logical.adopt") + ' \u25b8</a>'
        : "";
      g.addEventListener("click", (ev) => { ev.stopPropagation(); ctx.showPop(ev, _tipBase, _act); });
    }
    g.appendChild(rect);

    // ABR-markør: tydelig dobbel-ring («dør» mellom areaer) — G-179: gjort større/kraftigere
    // så grense-routere popper. Selve area-identiteten kommer fra de fargede boblene.
    if (abr) {
      const cx = x + W - 8, cy = y + 8;
      g.appendChild(ctx.el("circle", { cx: cx, cy: cy, r: 7.5, fill: ctx.cfg.background || "#fff", stroke: ctx.INK, "stroke-width": 2.2 }));
      g.appendChild(ctx.el("circle", { cx: cx, cy: cy, r: 3.5, fill: ctx.INK, stroke: "none" }));
    }

    // Etikett: 1–2 vrappede linjer, sentrert.
    if (lines.length > 1) {
      [[lines[0], n.y - 3], [lines[1], n.y + 13]].forEach(([txt, ly]) => {
        const t = ctx.el("text", { x: n.x, y: ly, "text-anchor": "middle",
          fill: ghost ? ctx.FAINT : ctx.INK, "font-size": 12, "font-weight": 600 });
        t.textContent = txt; g.appendChild(t);
      });
    } else {
      const t = ctx.el("text", { x: n.x, y: n.y + 4, "text-anchor": "middle",
        fill: ghost ? ctx.FAINT : ctx.INK, "font-size": 12, "font-weight": 600 });
      t.textContent = lines[0] || disp; g.appendChild(t);
    }

    // Pinn-indikator (låst posisjon).
    if (n.pinned === true) {
      g.appendChild(ctx.el("circle", {
        cx: x + 7, cy: y + 7, r: 3.5,
        fill: ctx.INK, stroke: ctx.cfg.background || "#fff", "stroke-width": 1
      }));
      const pt = ctx.el("title", {});
      pt.textContent = i18n.t("js.logical.locked_position");
      g.appendChild(pt);
    }

    // G-180: synlig ROLLE + STATUS på selve noden (ikke bare i hover).
    // Status-prikk (nede-venstre): grønn = alle naboskap oppe, amber = noen ikke oppe (1-vei/degradert),
    // rød = ingen oppe, grå = ghost/ingen naboskap. Gir et raskt helse-blikk pr. ruter.
    const _stCol = ghost ? ctx.FAINT
      : (_adj.n === 0 ? ctx.FAINT : (_adj.down === 0 ? ctx.OK : (_adj.down < _adj.n ? ctx.WARNING : ctx.BAD)));
    const sd = ctx.el("circle", { cx: x + 7, cy: y + H - 7, r: 4, fill: _stCol,
      stroke: ctx.cfg.background || "#fff", "stroke-width": 1 });
    const sdt = ctx.el("title", {});
    sdt.textContent = ghost ? i18n.t("js.logical.unmonitored") : (_adj.n === 0 ? i18n.t("js.logical.no_adj")
      : (_adj.down === 0 ? i18n.t("js.logical.all_adj_up") : i18n.t("js.logical.adj_down_ratio", {down: _adj.down, n: _adj.n})));
    sd.appendChild(sdt); g.appendChild(sd);
    // Rolle-tag under noden for ikke-interne roller (ABR / umonitorert).
    const _roleTag = ghost ? i18n.t("js.logical.unmonitored") : (abr ? "ABR" : "");
    if (_roleTag) {
      const rt = ctx.el("text", { x: n.x, y: y + H + 13, "text-anchor": "middle",
        fill: ghost ? ctx.FAINT : ctx.INK, "font-size": 10, "font-weight": 700 });
      rt.textContent = _roleTag; g.appendChild(rt);
    }

    // Redigeringsmodus: dra-for-flytt + klikk-for-meny på ekte noder (ikke delgraf).
    // G-186: i flyt-modus skal noder være klikkbare (velg A/B) selv om vi ikke redigerer.
    // attachEdit gir klikk → onNodeClick; selve dra-flyttingen er gated på editing i kjernen.
    if ((ctx.state.editing || ctx.state.flowMode) && opts.bubbles !== false) {
      g.style.cursor = ctx.state.editing ? "move" : "pointer";
      ctx.attachEdit(g, n);
    }

    parent.appendChild(g);

    // Area-bobler i tett ring rundt ABR (av i redigeringsmodus, på delgraf-noder,
    // eller når «Skjul områder» er på).
    if (abr && opts.bubbles !== false && !ctx.state.editing && ctx.state.showAreas !== false) {
      drawAreaBubbles(parent, n, x, y, W, H, ctx);
    }
  }

  // Area-bobler i en TETT RING rundt ABR-routeren (delvis overlappende), slik kategoriene
  // fordeles på det fysiske kartet — viser at OSPF-områdene «henger på» routeren. Hver boble
  // er i area-fargen, +25 % tekst. Klikk = HOPP til area-kartet; hover = info om området.
  function drawAreaBubbles(parent, n, x, y, W, H, ctx) {
    const expandAreas = ctx.cfg.expand_areas || {};
    const others = n.other_areas || [];
    const k = others.length;
    if (!k) return;
    // Best-effort plassering (den strukturerte løsningen kommer i plasserings-motoren, [[G-180]]):
    // legg boblene på den «frie» siden av routeren — motsatt av lenke-tyngdepunktet — så de ikke
    // dekker linkene, og tett inntil node-kanten (senter på kanten → delvis overlapp). Stram vifte.
    let lx = 0, ly = 0, nl = 0;
    (ctx.cfg.links || []).forEach(l => {
      const other = l.a === n.rid ? ctx.byRid[l.b] : (l.b === n.rid ? ctx.byRid[l.a] : null);
      if (other) { const dx = other.x - n.x, dy = other.y - n.y, d = Math.hypot(dx, dy) || 1; lx += dx / d; ly += dy / d; nl++; }
    });
    let base;
    if (nl && (lx || ly)) base = Math.atan2(-ly, -lx);   // motsatt av lenkene = fri side
    else {
      const ccx = (ctx.cfg.width || 0) / 2, ccy = (ctx.cfg.height || 0) / 2;
      base = Math.atan2(n.y - ccy, n.x - ccx);
    }
    if (!isFinite(base)) base = -Math.PI / 2;
    const span = Math.min(Math.PI * 0.7, (k - 1) * 0.5);   // stram vifte = stablet på fri side
    const ringR = Math.max(W, H) / 2;                      // senter på node-kanten → delvis overlapp
    const FS = 15;                                         // +25 % vs node-etiketten (12)

    others.forEach((oa, i) => {
      const area = expandAreas[oa];
      const lbl = area && area.label ? area.label : String(oa);   // area-id (f.eks. «0.0.0.0»), ikke «core»
      const col = areaColor(oa);
      const ang = base + (k > 1 ? (i / (k - 1) - 0.5) * span : 0);
      const cxB = n.x + ringR * Math.cos(ang);
      const cyB = n.y + ringR * Math.sin(ang);
      const bw = Math.max(40, String(lbl).length * 8.4 + 14), bh = 22;

      const gb = ctx.el("g", {});
      if (!ctx.public) gb.style.cursor = "pointer";   // G-183: ikke klikkbar på public
      gb.appendChild(ctx.el("rect", {
        x: cxB - bw / 2, y: cyB - bh / 2, width: bw, height: bh, rx: 11, ry: 11,
        fill: col, stroke: ctx.cfg.background || "#fff", "stroke-width": 1.5, "fill-opacity": 0.95
      }));
      const tt = ctx.el("text", {
        x: cxB, y: cyB + 5, "text-anchor": "middle",
        fill: "#ffffff", "font-size": FS, "font-weight": 700
      });
      tt.textContent = lbl;
      gb.appendChild(tt);

      const nR = area ? (area.nodes || []).length : 0;
      const nA = area ? (area.nodes || []).filter(d => d.abr).length : 0;
      const hint = ctx.public ? "" : "<br><i>" + i18n.t("js.logical.click_open_area") + "</i>";
      const info = area
        ? "<b>" + i18n.t("js.logical.area_title", {area: ctx.esc(lbl)}) + "</b><br>"
          + i18n.t("js.logical.area_counts", {routers: nR, abrs: nA}) + hint
        : "<b>" + i18n.t("js.logical.area_title", {area: ctx.esc(lbl)}) + "</b>" + hint;
      gb.addEventListener("mousemove", e => ctx.show(e, info));
      gb.addEventListener("mouseleave", ctx.hide);
      if (!ctx.public) {   // G-183: ingen navigasjon til innloggede /logical-ruter fra public-siden
        gb.addEventListener("click", (ev) => {
          ev.stopPropagation();
          // G-315: naviger til area-ruten /logical/<area>. Forrige «strip siste segment» ga 404 fra et
          // custom-kart (/logical/map/5 → /logical/map/<area>). Anker på «/logical/» beholder evt. prefiks.
          const p = location.pathname, i = p.indexOf("/logical/");
          const base = i >= 0 ? p.slice(0, i + "/logical/".length) : "/logical/";
          window.location = base + encodeURIComponent(oa);
        });
      }
      parent.appendChild(gb);
    });
  }

  // --- adapteren ---
  function makeLogicalAdapter() {
    let _applyEditGrey = () => {};   // G-190: settes i initControls, kalles av onEditChange (grå ut bolker)

    // --- G-186/G-191: forwarding-/trafikkflyt-lense (velg node A → node B). 1 node = LSP-er fra A;
    // 2 noder = routing (OSPF+LSP = faktisk routing, LSP vinner; eller kun OSPF). ---
    // mode = to-node-routing («Velg noder»); nodeMode = LSP-fra-node (G-195 steg 3, egen modus).
    // De to er gjensidig utelukkende — begge gjør noder klikkbare (ctx.state.flowMode).
    const _flow = { mode: false, nodeMode: false, a: null, b: null, lspMode: "ospf+lsp",   // a/b = rids
                    ospfPath: null, lspWorking: null, lspBackup: null, nodeLsps: [], msg: "" };
    let _flowSync = () => {};                                               // settes i initControls
    const _lbl = (ctx, rid) => { const n = ctx.byRid[rid]; return n ? (n.label || n.rid) : rid; };
    // Dijkstra over rettet, vektet OSPF-graf: a→b vekt = a_metric, b→a = b_metric.
    // G-194: ghost-noder (umonitorert) er gyldige på stien. Et naboskap brukes med mindre en KJENT
    // (ikke-ghost) ende er ikke-full — ghost-enden er ukjent, men OSPF-naboskap er toveis, så ghost-
    // enden av et full naboskap ER full. Ghost-ens UTGÅENDE metric er ukjent → estimeres symmetrisk
    // (= motsatt ende), som er linkspeed-basert auto-cost i ~98% av tilfellene; segmentet merkes `est`.
    // Returnerer {nodes:[rid…], cost, estimated, ghostsOnPath} eller null.
    // G-198: `penalize` (Set av lenke-nøkler «a|b») legger en stor straff på de lenkene, så en ny
    // SPF-kjøring unngår dem der det går (backup = færrest mulig delte hopp med primær), men kan
    // dele en lenke hvis ingen disjunkt sti finnes. PENALTY >> enhver realistisk sti-kost.
    const _ekey = (a, b) => (a < b ? a + "|" + b : b + "|" + a);
    // penalize = Set av lenke-nøkler å unngå; penalizeNodes = Set av noder (utstyr) å unngå. Brukes
    // til node+link-disjunkt backup: en backup skal overleve enhver enkeltfeil på primær, så den deler
    // verken link eller router med primær (kun A/B-endepunktene er felles). Straff >> sti-kost.
    function _spf(ctx, src, dst, penalize, penalizeNodes) {
      const byRid = ctx.byRid, adj = {}, PENALTY = 1e9;
      (ctx.cfg.links || []).forEach(l => {
        const aGhost = !!(byRid[l.a] && byRid[l.a].ghost), bGhost = !!(byRid[l.b] && byRid[l.b].ghost);
        if (!aGhost && l.a_state !== "full") return;     // kjent ende må være full
        if (!bGhost && l.b_state !== "full") return;
        let wa = l.a_metric, waEst = false;              // a→b = a's utgående cost
        if (wa == null) { wa = (l.b_metric == null ? 1 : l.b_metric); waEst = true; }
        let wb = l.b_metric, wbEst = false;              // b→a = b's utgående cost
        if (wb == null) { wb = (l.a_metric == null ? 1 : l.a_metric); wbEst = true; }
        const pen = (penalize && penalize.has(_ekey(l.a, l.b))) ? PENALTY : 0;
        (adj[l.a] || (adj[l.a] = [])).push({ to: l.b, w: wa + pen, est: waEst });
        (adj[l.b] || (adj[l.b] = [])).push({ to: l.a, w: wb + pen, est: wbEst });
      });
      const dist = { [src]: 0 }, prev = {}, prevEst = {}, seen = {}, pq = [{ rid: src, d: 0 }];
      while (pq.length) {
        pq.sort((x, y) => x.d - y.d);
        const cur = pq.shift();
        if (seen[cur.rid]) continue;
        seen[cur.rid] = true;
        if (cur.rid === dst) break;
        (adj[cur.rid] || []).forEach(e => {
          const np = (penalizeNodes && e.to !== dst && penalizeNodes.has(e.to)) ? PENALTY : 0;
          const nd = cur.d + e.w + np;          // straff for å gå INN i en primær-node (delt utstyr)
          if (dist[e.to] == null || nd < dist[e.to]) {
            dist[e.to] = nd; prev[e.to] = cur.rid; prevEst[e.to] = e.est; pq.push({ rid: e.to, d: nd });
          }
        });
      }
      if (dist[dst] == null) return null;
      const path = [dst]; let p = dst, estimated = false;
      while (p !== src && prev[p] != null) { if (prevEst[p]) estimated = true; p = prev[p]; path.unshift(p); }
      if (path[0] !== src) return null;
      return { nodes: path, cost: dist[dst], estimated: estimated,
               ghostsOnPath: path.filter(rid => byRid[rid] && byRid[rid].ghost) };
    }
    const _lspFind = (ctx, a, b, role) =>
      (ctx.cfg.lsps || []).find(L => L.from_rid === a && L.to_rid === b && L.role === role);
    // G-198: lenke-settet en sti bruker (uretta nøkler) + antall delte hopp mellom to stier.
    const _pathEdges = (nodes) => {
      const s = new Set();
      for (let i = 0; i < (nodes || []).length - 1; i++) s.add(_ekey(nodes[i], nodes[i + 1]));
      return s;
    };
    const _sharedHops = (p1, p2) => {
      const e1 = _pathEdges(p1), e2 = _pathEdges(p2); let n = 0;
      e2.forEach(k => { if (e1.has(k)) n++; });
      return n;
    };
    function _flowCompute(ctx) {
      _flow.ospfPath = _flow.lspWorking = _flow.lspBackup = null; _flow.nodeLsps = [];
      _flow.pathGhosts = []; _flow.estimated = false; _flow.lspEstimated = false;   // G-194/G-198
      if (_flow.nodeMode) {                                  // G-195 steg 3: LSP-er som STARTER i A
        if (_flow.a) {
          // Roger: i node-modus er det bare AT en LSP finnes som er interessant → kun working
          // (backup vises kun i «Velg to noder»). Unngår forvirrende overlappende stiplete linjer.
          const from = (ctx.cfg.lsps || []).filter(L => L.from_rid === _flow.a && L.role === "working");
          _flow.nodeLsps = from;
          _flow.msg = from.length
            ? i18n.t("js.logical.lsps_from", {node: _lbl(ctx, _flow.a), n: from.length,
                list: from.map(L => _lbl(ctx, L.to_rid)).join(", ")})
            : i18n.t("js.logical.no_lsps_from", {node: _lbl(ctx, _flow.a)});
        } else {
          _flow.msg = "";                                  // (Roger: ingen «klikk en node…»-hjelpetekst — støy)
        }
        return;
      }
      if (_flow.a && _flow.b) {
        if (_flow.lspMode === "ospf+lsp") {                 // faktisk routing: LSP vinner der den finnes
          const w = _lspFind(ctx, _flow.a, _flow.b, "working");
          if (w) {
            // G-198: vi KJENNER ikke LSP-ens faktiske rute (det krever ARHop/NETCONF — se G-199).
            // Vi TEGNER en BEREGNET rute: primær = OSPF korteste vei, backup = OSPF-sti som deler
            // færrest mulig hopp med primær (penaliser primærens lenker). Ærlig flagget «beregnet».
            const bk = _lspFind(ctx, _flow.a, _flow.b, "protect");
            const prim = _spf(ctx, _flow.a, _flow.b);
            if (prim) {
              _flow.lspWorking = prim.nodes;
              _flow.pathGhosts = prim.ghostsOnPath.slice();
              _flow.lspEstimated = true;
              let shared = null;
              if (bk) {
                // node+link-disjunkt: unngå primærens lenker OG mellom-noder (ikke A/B-endepunktene).
                const back = _spf(ctx, _flow.a, _flow.b, _pathEdges(prim.nodes),
                                  new Set(prim.nodes.slice(1, -1)));
                if (back) {
                  _flow.lspBackup = back.nodes;
                  shared = _sharedHops(prim.nodes, back.nodes);
                  back.ghostsOnPath.forEach(g => { if (_flow.pathGhosts.indexOf(g) < 0) _flow.pathGhosts.push(g); });
                }
              }
              _flow.msg = i18n.t("js.logical.lsp_route", {a: _lbl(ctx, _flow.a), b: _lbl(ctx, _flow.b)})
                + (_flow.lspBackup ? " · " + i18n.t("js.logical.backup_shared", {n: shared}) : "");
              return;
            }
            // ingen SPF-sti (nede/usammenhengende) → fall gjennom til OSPF-grenen under
          }
        }
        const r = _spf(ctx, _flow.a, _flow.b);              // OSPF (ghost-bevisst, G-194)
        if (r) {
          _flow.ospfPath = r.nodes; _flow.pathGhosts = r.ghostsOnPath; _flow.estimated = r.estimated;
          let m = i18n.t("js.logical.ospf_path", {a: _lbl(ctx, _flow.a), b: _lbl(ctx, _flow.b),
                                                 cost: r.cost, hops: r.nodes.length - 1});
          // G-194: transit gjennom en umonitorert (ghost) node ⇒ utgående metric er ESTIMERT (symmetrisk).
          if (r.estimated) {
            const gnames = r.ghostsOnPath.map(g => _lbl(ctx, g)).join(", ");
            m += " · " + i18n.t("js.logical.via_ghosts", {names: gnames});
          }
          _flow.msg = m;
        } else { _flow.msg = i18n.t("js.logical.no_path", {a: _lbl(ctx, _flow.a), b: _lbl(ctx, _flow.b)}); }
      } else if (_flow.a) {                                  // 1 node valgt: marker A, vent på B
        // (G-191/Roger: vis IKKE LSP-er bare ved å velge A — routing vises først når B er valgt.)
        _flow.msg = i18n.t("js.logical.pick_b", {a: _lbl(ctx, _flow.a)});
      } else { _flow.msg = ""; }                             // (Roger: ingen «klikk node A…»-hjelpetekst)
    }
    function _flowSelect(n, ctx) {
      if (_flow.nodeMode) { _flow.a = n.rid; _flow.b = null; }        // LSP-fra-node: én node om gangen
      else if (!_flow.a || _flow.b) { _flow.a = n.rid; _flow.b = null; }   // routing: (re)start på A
      else if (n.rid !== _flow.a) { _flow.b = n.rid; }
      _flowCompute(ctx); _flowSync(); ctx.draw();
    }
    function _drawFlow(parent, ctx) {
      const FLOW = ctx.cvar("--flow", "#7c3aed");      // primær LSP/OSPF/flyt = lilla
      const BACKUP = ctx.cvar("--lsp", "#14b8a6");     // G-198: backup-LSP = teal, så den skiller seg ut
      // tegn en sti (liste av rids) segment-for-segment med valgfri pil/stipling.
      const seg = (rids, col, o) => {
        let pts = (rids || []).map(r => ctx.byRid[r]).filter(Boolean);
        if (pts.length < 2) return;
        // G-198: o.pull ⇒ trekk endene UTENFOR node-symbolet (LSP/flyt). OSPF lar være (går til senter).
        if (o.pull) {
          const STOP = 30, pb = (i, j) => {
            const a = pts[i], b = pts[j], dx = b.x - a.x, dy = b.y - a.y, L = Math.hypot(dx, dy) || 1;
            const s = Math.min(STOP, L * 0.4);
            return { x: a.x + (dx / L) * s, y: a.y + (dy / L) * s, ghost: a.ghost };
          };
          pts = pts.slice();
          pts[0] = pb(0, 1); pts[pts.length - 1] = pb(pts.length - 1, pts.length - 2);
        }
        for (let i = 0; i < pts.length - 1; i++) {
          const a = pts[i], b = pts[i + 1];
          const ln = ctx.el("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: col,
            "stroke-width": o.w, "stroke-opacity": o.op, "stroke-linecap": "round" });
          // G-194: dashGhost ⇒ stiple segmenter som rører en umonitorert (ghost) node — der er
          // (utgående) metric estimert, ikke målt. Ellers fast dash om satt.
          const dash = o.dash || (o.dashGhost && (a.ghost === true || b.ghost === true) ? "7 5" : null);
          if (dash) ln.setAttribute("stroke-dasharray", dash);
          parent.appendChild(ln);
          if (o.arrow) parent.appendChild(ctx.arrowHead(a, b, col, o.w, 22));
        }
      };
      // G-198: «elve/slange»-stil for LSP — glatt lilla Catmull-kurve, lik trafikkflyt-overlegget på
      // det fysiske kartet (/maps/). Primær solid, backup stiplet; begge lilla (FLOW).
      const river = (rids, o) => {
        const ns = (rids || []).map(r => ctx.byRid[r]).filter(Boolean);
        if (ns.length < 2) return;
        // G-198 (Roger): svingen skal ligge på INNSIDEN = inn mot MIDTEN AV BILDET (kanvas-senter),
        // ikke bulke utover. Vi forskyver hver mellom-node mot kanvas-senter (klarer noden på inner-
        // siden), og trekker endepunktene tilbake langs lenka så elven ender UTENFOR noden. Justerbart.
        const OFF = 20, STOP = 34;
        const CX = (ctx.cfg.width || 0) / 2, CY = (ctx.cfg.height || 0) / 2;
        const pts = ns.map((n, i) => {
          if (i === 0 || i === ns.length - 1) return { x: n.x, y: n.y };   // endepunkt → kun pullback
          let ox = CX - n.x, oy = CY - n.y, oL = Math.hypot(ox, oy);       // node → bildets midte
          if (oL < 1) return { x: n.x, y: n.y };
          return { x: n.x + (ox / oL) * OFF, y: n.y + (oy / oL) * OFF };
        });
        const pullBack = (i, towards) => {
          const dx = ns[towards].x - ns[i].x, dy = ns[towards].y - ns[i].y, L = Math.hypot(dx, dy) || 1;
          const s = Math.min(STOP, L * 0.4);            // klamp så endene ikke krysser på korte lenker
          pts[i] = { x: pts[i].x + (dx / L) * s, y: pts[i].y + (dy / L) * s };
        };
        pullBack(0, 1); pullBack(pts.length - 1, pts.length - 2);
        let d = `M ${pts[0].x.toFixed(1)} ${pts[0].y.toFixed(1)}`;
        for (let i = 0; i < pts.length - 1; i++) {
          const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || pts[i + 1];
          d += ` C ${(p1.x + (p2.x - p0.x) / 6).toFixed(1)} ${(p1.y + (p2.y - p0.y) / 6).toFixed(1)}`
             + ` ${(p2.x - (p3.x - p1.x) / 6).toFixed(1)} ${(p2.y - (p3.y - p1.y) / 6).toFixed(1)}`
             + ` ${p2.x.toFixed(1)} ${p2.y.toFixed(1)}`;
        }
        const col = o.col || FLOW;
        const path = ctx.el("path", { d, fill: "none", stroke: col, "stroke-width": o.w,
          "stroke-opacity": o.op, "stroke-linecap": "round", "stroke-linejoin": "round" });
        if (o.dash) path.setAttribute("stroke-dasharray", o.dash);
        parent.appendChild(path);
        if (o.arrow) parent.appendChild(ctx.arrowHead(pts[pts.length - 2], pts[pts.length - 1], col, o.w, 22));
      };
      // 2-node: OSPF = rett lilla linje PÅ TOPPEN av nodene (litt tykkere); LSP = lilla elve/slange-kurve.
      seg(_flow.ospfPath, FLOW, { w: 7, op: 0.85, arrow: true, dashGhost: true });
      river(_flow.lspBackup, { w: 5, op: 0.85, dash: "9 7", arrow: true, col: BACKUP }); // backup: teal elv (under)
      river(_flow.lspWorking, { w: 8, op: 0.9, arrow: true });               // primær: tykk lilla elv (over)
      // 1-node («LSP fra node»): LSP-er fra A som RETTE lilla linjer node-til-node (som OSPF, men
      // ender UTENFOR noden — kun OSPF går til senter). Aktiv heltrukken, backup stiplet.
      (_flow.nodeLsps || []).forEach(x => seg(x.hops || [x.from_rid, x.to_rid], FLOW,
        { w: 6, op: 0.9, arrow: true, pull: true }));   // kun working (eksistens+retning); backup = to-node
      // uthev valgte noder
      const ring = (rid, col) => {
        const n = ctx.byRid[rid]; if (!n) return;
        parent.appendChild(ctx.el("circle", { cx: n.x, cy: n.y, r: 30, fill: "none",
          stroke: col, "stroke-width": 3, "stroke-opacity": 0.95 }));
      };
      if (_flow.a) ring(_flow.a, ctx.OK);
      if (_flow.b) ring(_flow.b, ctx.cvar("--primary", "#2d6cdf"));
      // G-194: marker umonitorerte (ghost) TRANSIT-noder på stien med stiplet varsel-ring (estimert).
      // Endepunkt-ghost (B) er eksakt og får sin vanlige ring over — ikke transit.
      (_flow.pathGhosts || []).forEach(rid => {
        if (rid === _flow.a || rid === _flow.b) return;
        const n = ctx.byRid[rid]; if (!n) return;
        const c = ctx.el("circle", { cx: n.x, cy: n.y, r: 26, fill: "none",
          stroke: ctx.cvar("--warning", "#c77d00"), "stroke-width": 2.5, "stroke-opacity": 0.95 });
        c.setAttribute("stroke-dasharray", "4 4");
        parent.appendChild(c);
      });
    }

    return {
      kind: "logical",
      capabilities: { create_nodes: false, create_links: false, edit_links: false },
      nodeKey: n => n.rid,   // G-233: node-nøkkel for felles auto-layout (logisk = router_id)

      // Ghost-filter: skjul umonitorerte noder + lenker med ghost-ende.
      // G-194: auto-vis ghosts som ligger på den valgte stien selv om «Skjul ukjente» er på,
      // så en sti gjennom/til en umonitorert node aldri «forsvinner» gjennom en usynlig node.
      nodeVisible(n, ctx) {
        return ctx.state.showGhosts || n.ghost !== true || (_flow.pathGhosts || []).indexOf(n.rid) >= 0;
      },
      linkVisible(l, ctx) {
        if (ctx.state.showGhosts) return true;
        const a = ctx.byRid[l.a], b = ctx.byRid[l.b];
        return !(a && a.ghost === true) && !(b && b.ghost === true);
      },

      drawLink(parent, l, ctx) { drawLink(parent, l, ctx.byRid, ctx); },
      drawNode(parent, n, ctx, opts) { drawNode(parent, n, ctx, opts); },

      // G-186: i flyt-modus velger klikk node A→B (vis OSPF-sti); ellers vanlig node-meny.
      onNodeClick(n, ev, ctx) { if (_flow.mode || _flow.nodeMode) _flowSelect(n, ctx); else ctx.openMenu(n, ev); },
      // G-186: tegn forwarding-stien + uthev valgte noder OVER resten.
      afterDraw(parent, ctx) { _drawFlow(parent, ctx); },

      // Meny-tillegg: GPS/NetBox-info (universelt, G-237) + Adopter (ghost → discovery m/ IP forhåndsutfylt).
      menuItems(n, ctx) {
        const items = [{
          label: i18n.t("js.logical.gps_info"), className: "ghost sm",
          onClick: () => ctx.showGpsInfo(n)
        }];
        if (n.ghost === true && ctx.isAdmin) {
          items.push({
            label: i18n.t("js.logical.adopt_add_host"), className: "sm",
            onClick: () => { window.location = ctx.adoptUrl + "?adopt_ip=" + encodeURIComponent(n.rid); }
          });
        }
        return items;
      },

      // Type-spesifikke kontroller: vis/skjul ukjente + navn/IP-toggle.
      initControls(ctx) {
        const ghostBtn = document.getElementById("ospf-ghosts");
        if (ghostBtn) {
          const sync = () => {
            ghostBtn.classList.toggle("on", ctx.state.showGhosts);
            ghostBtn.textContent = ctx.state.showGhosts ? i18n.t("js.logical.hide_unknown") : i18n.t("js.logical.show_unknown");
          };
          sync();
          ghostBtn.addEventListener("click", () => {
            ctx.state.showGhosts = !ctx.state.showGhosts; sync(); ctx.draw();
          });
        }
        // Toggle: vis/skjul OSPF-område-boblene (multi-area) som henger på routerne.
        const areasBtn = document.getElementById("ospf-area-badges-toggle");
        if (areasBtn) {
          const syncA = () => {
            const on = ctx.state.showAreas !== false;
            areasBtn.classList.toggle("on", on);
            areasBtn.textContent = on ? i18n.t("js.logical.hide_areas") : i18n.t("js.logical.show_areas");
          };
          syncA();
          areasBtn.addEventListener("click", () => {
            ctx.state.showAreas = (ctx.state.showAreas === false);
            syncA(); ctx.draw();
          });
        }
        // G-200: Navn/IP slått sammen til ÉN toggle-knapp som viser gjeldende modus; klikk bytter.
        const nameBtn = document.getElementById("ospf-name-toggle");
        if (nameBtn) {
          const syncN = () => { nameBtn.textContent = (ctx.state.naming === "ip") ? "IP" : i18n.t("js.logical.name"); };
          syncN();
          nameBtn.addEventListener("click", () => {
            ctx.state.naming = (ctx.state.naming === "ip") ? "name" : "ip";
            syncN(); ctx.draw();
          });
        }
        // G-200/G-276: OSPF-metric (cost-chips på lenkene) av/på — kun logisk kart. Default AV (G-276):
        // logisk laster med metric skjult; toggle slår den på. (Visnings-preferanse, ikke persistert.)
        // G-315: default AV på ALT logisk kart — også public (som ikke har toggle-knappen);
        // tidligere ble showMetric=false satt inne i toggle-guarden → public viste cost-chips.
        ctx.state.showMetric = false;                   // G-276/G-315: skjult uansett om toggle finnes
        const metricBtn = document.getElementById("ospf-metric-toggle");
        if (metricBtn) {
          const syncM = () => {
            const on = ctx.state.showMetric !== false;
            metricBtn.classList.toggle("on", on);
            metricBtn.textContent = on ? i18n.t("js.logical.hide_metric") : i18n.t("js.logical.show_metric");
          };
          syncM();
          metricBtn.addEventListener("click", () => {
            ctx.state.showMetric = (ctx.state.showMetric === false);
            syncM(); ctx.draw();
          });
        }
        // G-178: «core ▾»-dropdown for area-navigasjon (skalerer til mange areas).
        const moreBtn = document.getElementById("ospf-area-more");
        const areaMenu = document.getElementById("ospf-area-menu");
        if (moreBtn && areaMenu) {
          const closeAreaMenu = () => { areaMenu.hidden = true; moreBtn.setAttribute("aria-expanded", "false"); };
          moreBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            const willOpen = areaMenu.hidden;
            areaMenu.hidden = !willOpen;
            moreBtn.setAttribute("aria-expanded", willOpen ? "true" : "false");
          });
          // Klikk utenfor / Escape lukker (menyens lenker navigerer selv).
          document.addEventListener("click", () => { if (!areaMenu.hidden) closeAreaMenu(); });
          document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAreaMenu(); });
        }
        // G-179: farge-swatch på area-lenkene (inline + dropdown) i area-fargen.
        document.querySelectorAll("#ospf-areas a[data-area], #ospf-area-menu a[data-area]").forEach(a => {
          if (a.querySelector(".area-swatch")) return;
          const sw = document.createElement("span");
          sw.className = "area-swatch";
          sw.style.background = areaColor(a.dataset.area);
          a.insertBefore(sw, a.firstChild);
        });
        // G-233: Rutenett + Arranger (Auto/Forbedre/Nytt) wires nå i KJERNEN (#eb-grid-sel/#eb-arrange),
        // felles for alle kart — ikke lenger her i adapteren. Adapteren gir bare node-nøkkelen (rid).
        // G-186/G-191: routing-lense. «Velg noder» toggler velg-modus (re-trykk = av + nullstill).
        // 1 node = LSP-er fra A; 2 noder = routing. «OSPF+LSP»↔«OSPF» velger routing-kilde.
        const flowPick = document.getElementById("ospf-flow-pick");
        const flowModeBtn = document.getElementById("ospf-flow-mode");
        const flowStatus = document.getElementById("ospf-flow-status");
        // G-191: finnes det LSP-data i det hele tatt? (tomt = innsamling av, eller gear uten LSP-er).
        // Da er «OSPF+LSP» meningsløst → tving til «OSPF» og deaktiver toggelen m/ forklarende hint.
        const _hasLsps = ((ctx.cfg && ctx.cfg.lsps) || []).length > 0;
        if (!_hasLsps) _flow.lspMode = "ospf";
        if (flowPick) {
          // G-195 steg 3: ÉN knapp som sykler gjennom 3 tilstander (som OSPF/OSPF+LSP-toggelen ved
          // siden). AV => node-klikk åpner node-menyen (Adopter/Lås/Omdøp). «Velg to noder» => routing
          // A→B. «LSP fra node» => klikk én node, se alle LSP-ene fra den. Uten LSP-data er LSP-modusen
          // grået ut + viser advarsel (men man kan fortsatt toggle videre forbi den).
          _flowSync = () => {
            const active = _flow.mode || _flow.nodeMode;
            ctx.state.flowMode = active;       // noder klikkbare i begge lense-moduser; av => node-meny
            flowPick.classList.toggle("on", active);
            flowPick.textContent = _flow.nodeMode ? i18n.t("js.logical.lsp_from_node")
              : (_flow.mode ? i18n.t("js.logical.pick_two_nodes") : i18n.t("js.logical.traffic_flow"));
            const lspNoData = _flow.nodeMode && !_hasLsps;
            flowPick.style.opacity = lspNoData ? "0.55" : "";   // «grået ut» uten LSP-data
            if (flowModeBtn) {
              flowModeBtn.style.display = _flow.mode ? "" : "none";   // kilde-toggle kun i routing-modus
              flowModeBtn.textContent = _flow.lspMode === "ospf+lsp" ? "OSPF+LSP" : "OSPF";
              flowModeBtn.classList.toggle("on", _flow.lspMode === "ospf+lsp");
              flowModeBtn.disabled = !_hasLsps;
              flowModeBtn.title = _hasLsps
                ? i18n.t("js.logical.routing_source_title")
                : i18n.t("js.logical.no_lsp_data_title");
            }
            if (flowStatus) {
              flowStatus.textContent = !active ? ""
                : lspNoData ? "⚠ " + i18n.t("js.logical.no_lsp_data")
                : _flow.msg;
            }
          };
          flowPick.addEventListener("click", () => {
            if (!_flow.mode && !_flow.nodeMode) { _flow.mode = true; }          // av → Velg to noder
            else if (_flow.mode) { _flow.mode = false; _flow.nodeMode = true; } // → LSP fra node
            else { _flow.nodeMode = false; }                                    // → av
            _flow.a = _flow.b = null;                                           // overgang nullstiller valg
            _flowCompute(ctx); _flowSync(); ctx.draw();
          });
          if (flowModeBtn) flowModeBtn.addEventListener("click", () => {
            if (!_hasLsps) return;             // låst til OSPF når det ikke finnes LSP-data
            _flow.lspMode = _flow.lspMode === "ospf+lsp" ? "ospf" : "ospf+lsp";
            _flowCompute(ctx); _flowSync(); ctx.draw();
          });
          _flowSync();
        }

        // G-190: grå ut redigerings-bolkene utenfor Rediger (synlige, men disabled). Rediger-knappen
        // (bolke-right) røres ikke. Kalles initialt (ikke-redigering) + ved hver toggle (onEditChange).
        _applyEditGrey = (editing) => {
          document.querySelectorAll("#ospf-editbar .editgrp").forEach(g => {
            g.classList.toggle("greyed", !editing);
            g.querySelectorAll("button, select, a").forEach(elx => {
              if (editing) { elx.removeAttribute("disabled"); elx.style.pointerEvents = ""; }
              else { elx.setAttribute("disabled", ""); elx.style.pointerEvents = "none"; }
            });
          });
        };
        _applyEditGrey(!!ctx.state.editing);
      },

      // G-190: kjernen kaller denne ved Rediger-toggle → grå ut / aktiver redigerings-bolkene.
      onEditChange(editing) { _applyEditGrey(editing); }
    };
  }

  global.BifrostMap = global.BifrostMap || {};
  global.BifrostMap.makeLogicalAdapter = makeLogicalAdapter;
})(window);
