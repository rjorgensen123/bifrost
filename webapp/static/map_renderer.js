// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.1
// SPDX-License-Identifier: AGPL-3.0-or-later
/* Generisk kart-renderer-kjerne (G-168). Eier alt som er FELLES for fysisk og
   logisk kart: SVG-oppsett + bakgrunn, dra/pinn av noder, node-meny (lås/omdøp +
   adapter-tillegg), tooltip, pil-/koordinat-helpere, tema-farger, kreditt-stempel,
   tegne-loopen og de generiske kontrollene (rediger/lagre/auto-arranger/bygg).

   Det TYPE-SPESIFIKKE leveres av en «adapter» (se map_logical.js): node-form,
   lenke-farge/lense, synlighets-filter, ekstra menyvalg, ekstra kontroller og
   ekstra lag (afterDraw). Kjernen kaller adapterens hooks og gir dem en `ctx`
   med alle delte helpere + delt tilstand.

   Ingen eksterne libs — ren vanilla. CSRF løses av den globale fetch-wrapperen
   i base.html (X-CSRFToken settes automatisk på alle ikke-GET-kall). */
(function (global) {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";

  function el(name, attrs) {
    const e = document.createElementNS(NS, name);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }
  const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const cvar = (n, f) =>
    getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f;

  // Node-etikett: vis HELE navnet, men vrapp over opptil 2 linjer så boksen ikke
  // blir kjempebred. Deler en lang streng nær midten (mellomrom/«.»/«-»).
  function wrapLabel(s, maxChars) {
    const MAX = maxChars || 22;   // ~150px ved font-size 12
    const v = String(s == null ? "" : s).trim();
    if (v.length <= MAX) return [v];
    const mid = Math.floor(v.length / 2);
    let best = -1, bestDist = Infinity;
    for (let i = 1; i < v.length - 1; i++) {
      const c = v[i];
      if (c === " " || c === "." || c === "-") {
        const d = Math.abs(i - mid);
        if (d < bestDist) { bestDist = d; best = i; }
      }
    }
    if (best < 0) best = mid;
    const sep = v[best];
    const cut = sep === " " ? best : best + 1;
    const line1 = v.slice(0, cut).trim();
    const line2 = v.slice(cut).trim();
    return line2 ? [line1, line2] : [line1];
  }

  // Pilspiss inn mot node `tip` langs retningen from->tip (G-088). Plasseres litt
  // før node-boksen (pad) så spissen ikke gjemmes under den. Returnerer <polygon>.
  function arrowHead(from, tip, fill, w, pad) {
    const dx = tip.x - from.x, dy = tip.y - from.y, len = Math.hypot(dx, dy) || 1;
    const ux = dx / len, uy = dy / len;
    const tx = tip.x - ux * pad, ty = tip.y - uy * pad;
    const size = 5 + w * 0.8;
    const back = size * 1.6, side = size;
    const bx = tx - ux * back, by = ty - uy * back;
    const px = -uy, py = ux;
    return el("polygon", { points:
      `${tx},${ty} ${bx + px * side},${by + py * side} ${bx - px * side},${by - py * side}`,
      fill: fill, "fill-opacity": 0.9 });
  }

  // Punkt langs en linje a→b, frac 0..1. Brukes til etikett-/chip-plassering.
  function endLabelPos(a, b, frac) {
    return { x: a.x + (b.x - a.x) * frac, y: a.y + (b.y - a.y) * frac };
  }

  // MOTOREN EIER LINJA (G-168 spec §5 / Roger 2026-06-18): geometrien for én lenke a→b
  // med valgfri bue (`bow`, px-forskyvning for parallelle lenker). Kvadratisk Bézier a→C→b,
  // delt på t=0.5 (De Casteljau) i `bmid` — så HVER ENDE kan fylle SIN halvdel (node→bmid)
  // uavhengig, langs nøyaktig samme linje. bow=0 → C på linja → kurven blir helt rett.
  // Returnerer { mid, C, Qa, Qb, bmid }: Qa/Qb = del-kontrollpunkt nærmest hhv. a/b.
  function linkGeom(a, b, bow) {
    const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    const llen = Math.hypot(b.x - a.x, b.y - a.y) || 1;
    const nx = -(b.y - a.y) / llen, ny = (b.x - a.x) / llen;   // enhetsnormal
    const C = { x: mid.x + nx * 2 * (bow || 0), y: mid.y + ny * 2 * (bow || 0) };
    const Qa = { x: (a.x + C.x) / 2, y: (a.y + C.y) / 2 };
    const Qb = { x: (C.x + b.x) / 2, y: (C.y + b.y) / 2 };
    const bmid = { x: (Qa.x + Qb.x) / 2, y: (Qa.y + Qb.y) / 2 };
    return { mid, C, Qa, Qb, bmid };
  }

  // Bygg-knapp (admin): POST til discovery, så reload. Generisk — finnes også
  // uten kart-data (tom topologi), derfor egen frittstående wiring.
  function wireBuild(buildBtn, runEl) {
    if (!buildBtn) return;
    buildBtn.addEventListener("click", async () => {
      const url = buildBtn.dataset.url;
      if (!url) return;
      buildBtn.disabled = true;
      const before = buildBtn.textContent;
      buildBtn.textContent = i18n.t("js.renderer.build_running");
      try {
        const r = await fetch(url, { method: "POST" });   // X-CSRFToken via wrapper
        if (!r.ok) throw new Error("http " + r.status);
        if (runEl) runEl.textContent = i18n.t("js.renderer.build_started");
        setTimeout(() => location.reload(), 1200);
      } catch (e) {
        console.error("kart build feilet", e);
        if (runEl) runEl.textContent = i18n.t("js.renderer.build_failed");
        buildBtn.disabled = false;
        buildBtn.textContent = before;
      }
    });
  }

  // The attribution for the map background that is drawn: the live source's text for live tiles,
  // the stored text for a snapshot (physical maps call it bg_attr, logical maps bg_attribution).
  // Empty when there is no geographic background, or for an uploaded image.
  function bgAttribution(cfg) {
    if (cfg.bg_mode === "live" && cfg.bg_live && cfg.bg_live.url) return cfg.bg_live.attribution || "";
    if (cfg.bg_mode !== "live" && cfg.has_bg_image && cfg.bg_url) return cfg.bg_attr || cfg.bg_attribution || "";
    return "";
  }

  // --- MapRenderer: den generiske kjernen. ---
  // opts = { svg, wrap, tip, cfg, adapter, isAdmin, adoptUrl,
  //          controls: { editToggle, save, relayout, status } }
  function MapRenderer(opts) {
    const svg = opts.svg, cfg = opts.cfg, adapter = opts.adapter;
    const wrap = opts.wrap, tip = opts.tip;
    const C = opts.controls || {};

    const theme = {
      INK: cvar("--ink", "#1a1e3a"), FAINT: cvar("--faint", "#9aa0d0"),
      GRID: cvar("--grid", "#d7dbf0"), OK: cvar("--ok-ink", "#2ecc71"),
      BAD: cvar("--bad-ink", "#e74c3c"), DOWN: cvar("--down", "#ff1f3d"),
      WARNING: cvar("--warning", "#c77d00")
    };

    // Node-oppslag etter router-id (lenker refererer noder via rid).
    const byRid = {};
    (cfg.nodes || []).forEach(n => { byRid[n.rid] = n; });

    // G-181: har admin et lagret UTKAST, last det inn over live-posisjonene (rid-match) så
    // redigeringen fortsetter på utkastet. View-brukere ser alltid live (utkast = admin-WIP).
    let hasDraft = false;
    if (opts.isAdmin && Array.isArray(cfg.draft) && cfg.draft.length) {
      cfg.draft.forEach(d => {
        const nd = byRid[d && d.rid];
        if (nd) { nd.x = d.x; nd.y = d.y; nd.pinned = !!d.pinned; if (d.label != null) nd.label = d.label; }
      });
      hasDraft = true;
    }

    // Delt tilstand (lever på tvers av redraws). naming/showGhosts brukes
    // i hovedsak av adaptere; editing er felles (dra/meny).
    const state = { editing: false, naming: "name", showGhosts: true, showAreas: true,
                    showMetric: true, gridSize: 0, gpsMode: "off" };

    // --- dirty/status (lagre) ---
    // G-260: `dirty` = har vi ulagrede endringer i nettleseren (drag/lås/visningsnavn) som IKKE er
    // persistert til arbeidskopien ennå? Avgjør «Forkast»-grenen (to-trinns peel — se ctx.forkast).
    let dirty = false;
    function setStatus(txt) { if (C.status) C.status.textContent = txt || ""; }
    function markDirty() { dirty = true; if (C.save) C.save.classList.add("dirty"); setStatus(i18n.t("js.renderer.unsaved")); }
    function setClean() { dirty = false; if (C.save) C.save.classList.remove("dirty"); }
    // Felles layout-payload (brukes av både «Lagre» (commit) og «Lagre utkast»).
    function layoutPayload() {
      return (cfg.nodes || []).map(n => ({
        rid: n.rid, x: Math.round(n.x), y: Math.round(n.y), pinned: !!n.pinned, label: n.label }));
    }

    // --- tooltip (gjenbruker .map-tip fra app.css) ---
    // 1.8.0 (Gitea #12): KLAMP tooltipen til kart-rammen — mål faktisk størrelse og hold den
    // innenfor alle fire kanter (før: kun en grov høyre-sjekk → kunne renne utenfor skjermen,
    // særlig på mobil og nederst i kartet).
    function show(e, html) {
      if (!tip) return;
      tip.innerHTML = html;
      const rc = wrap.getBoundingClientRect();
      let x = e.clientX - rc.left + 14, y = e.clientY - rc.top + 14;
      const tw = tip.offsetWidth || 200, th = tip.offsetHeight || 60;
      if (x + tw > rc.width - 4) x = Math.max(4, e.clientX - rc.left - tw - 10);
      if (y + th > rc.height - 4) y = Math.max(4, e.clientY - rc.top - th - 10);
      tip.style.left = x + "px"; tip.style.top = y + "px"; tip.style.opacity = 1;
    }
    function hide() { if (tip) tip.style.opacity = 0; }

    // --- 1.8.2: interaktiv node-POPUP (.map-pop) — delt av begge karttyper. Trykk-flaten for
    // touch (hover finnes ikke der): samme info som tooltipen + valgfri handlings-lenke øverst.
    // Én om gangen; lukkes ved ✕ eller klikk utenfor; klampes alltid PÅ skjermen. ---
    let popEl = null;
    function closePop() {
      if (popEl) { popEl.remove(); popEl = null; }
      document.removeEventListener("click", _popOutside, true);
    }
    function _popOutside(e) { if (popEl && !popEl.contains(e.target)) closePop(); }
    function showPop(ev, infoHtml, actionHtml) {
      closePop(); hide();
      const d = document.createElement("div");
      d.className = "map-pop";
      d.innerHTML = '<div class="map-pop-actions">' + (actionHtml || "<span></span>")
        + '<button type="button" class="pop-close" aria-label="' + i18n.t("js.renderer.close") + '">\u2715</button></div>' + infoHtml;
      wrap.appendChild(d);
      const rc = wrap.getBoundingClientRect();
      let x = ev.clientX - rc.left + 10, y = ev.clientY - rc.top + 10;
      x = Math.max(4, Math.min(rc.width - d.offsetWidth - 4, x));
      y = Math.max(4, Math.min(rc.height - d.offsetHeight - 4, y));
      d.style.left = x + "px"; d.style.top = y + "px";
      d.querySelector(".pop-close").addEventListener("click", closePop);
      setTimeout(() => document.addEventListener("click", _popOutside, true), 0);
      popEl = d;
    }

    // Skjerm-peker → SVG-koord (robust mot responsiv skalering via getScreenCTM).
    function toSvgPoint(ev) {
      const pt = svg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      const ctm = svg.getScreenCTM();
      if (!ctm) return { x: pt.x, y: pt.y };
      const p = pt.matrixTransform(ctm.inverse());
      return { x: p.x, y: p.y };
    }

    // 1.8.3 (G-290): kiosk-modus (infoscreen-slide, window.BIFROST_KIOSK) → INGEN pan/zoom/knapper;
    // hele kartet vises tilpasset rammen (viewBox fra templaten står urørt). Bevisst: kiosk = ren visning.
    if (!window.BIFROST_KIOSK) {
    // --- 1.8.0 (Gitea #12): kart-VIEWPORT pan/zoom (visning) — IKKE G-218 (site-LOD). ---
    // Ren viewBox-transform: zoom inn = mindre utsnitt, dra = panorer. Koordinat-konvertering
    // (toSvgPoint/getScreenCTM) er viewBox-bevisst, så klikk/dra forblir korrekte. Regler:
    //  · pan/pinch-GESTER er av i redigering (editoren eier draget); +/−-knappene virker alltid
    //    (nyttig for presis plassering — CTM holder editor-draget korrekt også zoomet).
    //  · touch-action: 'pan-y' ved s=1 (vanlig side-scroll bevart), 'none' ved s>1 (kartet eier gesten).
    //  · smal skjerm starter innzoomet (kartet holder lesbar «ekte» størrelse — bruker panorerer).
    const VZMAX = 8;
    let vz = { x: 0, y: 0, s: 1 };
    let vzPanned = 0;                      // px flyttet siden pointerdown — skiller pan fra klikk
    const vzPtrs = new Map();              // aktive pekere (pinch)
    let vzPinchD = 0;
    function vzApply() {
      const w = cfg.width / vz.s, h = cfg.height / vz.s;
      vz.x = Math.max(0, Math.min(cfg.width - w, vz.x));
      vz.y = Math.max(0, Math.min(cfg.height - h, vz.y));
      svg.setAttribute("viewBox", `${vz.x} ${vz.y} ${w} ${h}`);
      svg.style.touchAction = vz.s > 1 ? "none" : "pan-y";
    }
    function vzZoom(f, cx, cy) {           // cx/cy = kart-punkt som skal stå stille under zoom
      const ns = Math.max(1, Math.min(VZMAX, vz.s * f));
      if (cx == null) { cx = vz.x + cfg.width / vz.s / 2; cy = vz.y + cfg.height / vz.s / 2; }
      vz.x = cx - (cx - vz.x) * (vz.s / ns);
      vz.y = cy - (cy - vz.y) * (vz.s / ns);
      vz.s = ns; vzApply();
    }
    function vzReset() { vz = { x: 0, y: 0, s: 1 }; vzApply(); }
    // +/−/⟲-knapper i kart-rammen (40px tap-mål). Injiseres av kjernen → begge karttyper likt.
    (function () {
      const c = document.createElement("div");
      c.className = "map-zoomctl";
      c.innerHTML = '<button type="button" data-z="in" aria-label="' + i18n.t("js.renderer.zoom_in") + '">+</button>'
                  + '<button type="button" data-z="out" aria-label="' + i18n.t("js.renderer.zoom_out") + '">−</button>'
                  + '<button type="button" data-z="reset" aria-label="' + i18n.t("js.renderer.zoom_reset") + '">⟲</button>';
      c.addEventListener("click", (e) => {
        const b = e.target.closest("button"); if (!b) return;
        if (b.dataset.z === "in") vzZoom(1.5);
        else if (b.dataset.z === "out") vzZoom(1 / 1.5);
        else vzReset();
      });
      wrap.appendChild(c);
    })();
    svg.addEventListener("pointerdown", (ev) => {
      if (state.editing) return;                    // editoren eier gestene
      vzPtrs.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      vzPanned = 0;
      if (vzPtrs.size === 2) {
        const p = [...vzPtrs.values()];
        vzPinchD = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
      }
      // NB: INGEN setPointerCapture her — capture retargeter click til svg-en og ville brutt
      // node-/link-klikk i visning (ghost-popup, flyt-velg, link-modal). Pan gjelder mens
      // pekeren er over kartet; opprydding skjer på window-nivå (pointerup utenfor svg).
    });
    svg.addEventListener("pointermove", (ev) => {
      if (state.editing || !vzPtrs.has(ev.pointerId)) return;
      const prev = vzPtrs.get(ev.pointerId);
      vzPtrs.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (vzPtrs.size === 2) {                      // pinch-zoom rundt midtpunktet
        const p = [...vzPtrs.values()];
        const d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y);
        if (vzPinchD > 0) {
          const mid = toSvgPoint({ clientX: (p[0].x + p[1].x) / 2, clientY: (p[0].y + p[1].y) / 2 });
          vzZoom(d / vzPinchD, mid.x, mid.y);
          vzPanned = 99;                            // pinch skal aldri utløse klikk
        }
        vzPinchD = d;
      } else if (vz.s > 1) {                        // én-finger/mus-dra = panorer (kun zoomet)
        const scale = (cfg.width / vz.s) / (svg.clientWidth || cfg.width);
        vz.x -= (ev.clientX - prev.x) * scale;
        vz.y -= (ev.clientY - prev.y) * scale;
        vzPanned += Math.abs(ev.clientX - prev.x) + Math.abs(ev.clientY - prev.y);
        vzApply();
      }
    });
    function vzEnd(ev) { vzPtrs.delete(ev.pointerId); vzPinchD = 0; }
    window.addEventListener("pointerup", vzEnd);      // window-nivå: fanger også slipp utenfor svg
    window.addEventListener("pointercancel", vzEnd);  // (uten capture kan pekeren forlate kartet)
    // etter et reelt pan/pinch skal slipp IKKE tolkes som klikk på node/link (capture-fase).
    svg.addEventListener("click", (ev) => {
      if (vzPanned > 5) { ev.stopPropagation(); ev.preventDefault(); vzPanned = 0; }
    }, true);
    // G-293 (2.0.0): default-STØRRELSE = contain-fit i VINDUET — X/Y-forholdet bevares og
    // INGEN av dimensjonene går utenfor skjermen; er høyden knappest, styrer den (før: kartet
    // fylte bredden isolert og rant under vinduet). Elementet settes til eksakt tegnet
    // størrelse (ingen letterbox → pan-/popup-matten med clientWidth forblir korrekt),
    // og sentreres når høyden begrenser bredden. Skalerer fortsatt OPP (G-291) til den
    // dimensjonen som først treffer vinduskanten.
    function vzFit() {
      const availW = wrap.clientWidth || cfg.width;
      const availH = Math.max(260, window.innerHeight - svg.getBoundingClientRect().top - 14);
      const s = Math.min(availW / cfg.width, availH / cfg.height);
      svg.style.width = Math.round(cfg.width * s) + "px";   // høyden følger viewBox-forholdet
      svg.style.display = "block";
      svg.style.margin = "0 auto";
      return s;
    }
    let vzScale = vzFit();
    window.addEventListener("resize", () => { vzScale = vzFit(); });
    // Smal skjerm: start innzoomet så kartet holder lesbar størrelse (Rogers min-oppløsning-
    // modell); sentrert. Bruker panorerer/zoomer ut selv. ~0.55 effektive px per kart-px som
    // lesbarhets-gulv — nå målt mot den FITTEDE skalaen (dekker også høyde-begrensede kart).
    (function () {
      const eff = vzScale;
      if (eff < 0.55) {
        vz.s = Math.min(4, 0.55 / eff);
        vz.x = (cfg.width - cfg.width / vz.s) / 2;
        vz.y = (cfg.height - cfg.height / vz.s) / 2;
      }
      vzApply();
    })();
    }   // slutt kiosk-gate (G-290)

    // --- dra-tilstand på det PERSISTENTE svg-elementet (ikke node-<g> som
    // forsvinner ved redraw), så draget ikke stopper når draw() bygger om DOM-en. ---
    let dragNode = null, dragMoved = false, dragStartX = 0, dragStartY = 0,
        dragOffX = 0, dragOffY = 0, dragPointerId = null;

    svg.addEventListener("pointermove", (ev) => {
      if (!dragNode) return;
      if (!state.editing) return;   // G-186: dra-flytt kun i redigering; ren klikk (flyt-velg/meny) virker uansett
      if (Math.abs(ev.clientX - dragStartX) > 3 || Math.abs(ev.clientY - dragStartY) > 3) dragMoved = true;
      const p = toSvgPoint(ev);
      const nx = p.x + dragOffX, ny = p.y + dragOffY;   // grep-offset
      let fx = Math.max(0, Math.min(cfg.width || 0, nx));
      let fy = Math.max(0, Math.min(cfg.height || 0, ny));
      const g = state.gridSize;                          // snap-til-rutenett (0 = fritt)
      if (g > 0) { fx = Math.round(fx / g) * g; fy = Math.round(fy / g) * g; }
      dragNode.x = fx; dragNode.y = fy;
      draw();
    });
    svg.addEventListener("pointerup", (ev) => {
      if (!dragNode) return;
      const n = dragNode; dragNode = null;
      try { if (dragPointerId != null) svg.releasePointerCapture(dragPointerId); } catch (e) { /* noop */ }
      dragPointerId = null;
      // dra = pinn (logisk). Fysisk editor (G-034): flytt UTEN å låse → opts.pinOnDrag=false.
      if (dragMoved) { if (opts.pinOnDrag !== false) n.pinned = true; markDirty(); draw(); }
      // ren klikk → adapterens egen klikk-håndtering (fysisk editor: egen meny), ellers generisk meny.
      else if (adapter.onNodeClick) adapter.onNodeClick(n, ev, ctx);
      else openMenu(n, ev);
    });

    // pointerdown på node-<g>: registrer drag + flytt capture til persistent svg.
    function attachEdit(g, n) {
      g.addEventListener("pointerdown", (ev) => {
        ev.preventDefault(); ev.stopPropagation();
        closeMenu();
        dragNode = n; dragMoved = false;
        dragStartX = ev.clientX; dragStartY = ev.clientY;
        const p = toSvgPoint(ev);
        dragOffX = n.x - p.x; dragOffY = n.y - p.y;
        dragPointerId = ev.pointerId;
        try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* noop */ }
      });
    }

    // --- node-meny: generisk Lås/Omdøp + adapter-tillegg (f.eks. Adopter ghost). ---
    let menuEl = null, openMenuRid = null;
    function closeMenu() { if (menuEl) { menuEl.remove(); menuEl = null; } openMenuRid = null; }
    function openMenu(n, ev) {
      closeMenu();
      if (!state.editing) return;   // G-233: ingen node-meny (rediger-handlinger) når Rediger er av
      openMenuRid = n.rid;
      const rc = wrap.getBoundingClientRect();
      const m = document.createElement("div");
      m.className = "ospf-node-menu";
      m.style.position = "absolute";
      m.style.left = (ev.clientX - rc.left + 8) + "px";
      m.style.top = (ev.clientY - rc.top + 8) + "px";
      m.style.zIndex = 20;
      m.style.background = cfg.background || "#fff";
      m.style.border = "1px solid " + theme.INK;
      m.style.borderRadius = "8px";
      m.style.padding = "6px";
      m.style.boxShadow = "0 2px 8px rgba(0,0,0,.2)";
      m.style.font = "12px sans-serif";
      m.style.minWidth = "150px";

      // Adapter-spesifikke valg øverst (returneres som {label, className, onClick}).
      const extra = adapter.menuItems ? (adapter.menuItems(n, ctx) || []) : [];
      extra.forEach(it => {
        const b = document.createElement("button");
        b.type = "button"; b.className = it.className || "sm";
        b.style.display = "block"; b.style.width = "100%"; b.style.marginBottom = "4px";
        b.textContent = it.label;
        b.addEventListener("click", (e) => { e.stopPropagation(); it.onClick(); });
        m.appendChild(b);
      });

      // Lås / lås opp (generisk)
      const lockBtn = document.createElement("button");
      lockBtn.type = "button"; lockBtn.className = "ghost sm";
      lockBtn.style.display = "block"; lockBtn.style.width = "100%"; lockBtn.style.marginBottom = "4px";
      lockBtn.textContent = n.pinned ? i18n.t("js.renderer.unlock") : i18n.t("js.renderer.lock_position");
      lockBtn.addEventListener("click", (e) => {
        e.stopPropagation(); n.pinned = !n.pinned; markDirty(); closeMenu(); draw();
      });
      m.appendChild(lockBtn);

      // Omdøp (generisk, inline input → setter node.label)
      const renameBtn = document.createElement("button");
      renameBtn.type = "button"; renameBtn.className = "ghost sm";
      renameBtn.style.display = "block"; renameBtn.style.width = "100%";
      renameBtn.textContent = i18n.t("js.renderer.rename");
      renameBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        m.innerHTML = "";
        const inp = document.createElement("input");
        inp.type = "text"; inp.value = n.label || n.rid || "";
        inp.style.width = "100%"; inp.style.boxSizing = "border-box";
        m.appendChild(inp);
        const apply = () => { const v = inp.value.trim(); n.label = v || (n.rid || ""); markDirty(); closeMenu(); draw(); };
        inp.addEventListener("keydown", (ke) => {
          if (ke.key === "Enter") { ke.preventDefault(); apply(); }
          else if (ke.key === "Escape") { ke.preventDefault(); closeMenu(); }
        });
        inp.addEventListener("blur", apply);
        inp.focus(); inp.select();
      });
      m.appendChild(renameBtn);

      m.addEventListener("pointerdown", (e) => e.stopPropagation());
      wrap.appendChild(m);
      menuEl = m;
    }
    document.addEventListener("pointerdown", () => { if (menuEl) closeMenu(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

    // G-237: FELLES «📍 GPS / NetBox-info…»-panel — universelt på ALLE kart (fysisk + logisk). ÉN kilde.
    // Henter per-node info fra opts.nodeGpsUrl (mal m/ «/0/» → node-nøkkel), viser kilde/site/GPS/
    // manuelt-låst/kart-pos, og «GPS plasser» flytter noden til GPS-posisjonen (uten å lagre).
    let gpsPanel = null;
    function closeGpsPanel() { if (gpsPanel) { gpsPanel.remove(); gpsPanel = null; } }
    function _nKey(n) { return adapter.nodeKey ? adapter.nodeKey(n) : (n.id != null ? n.id : n.rid); }
    function showGpsInfo(n) {
      const tmpl = opts.nodeGpsUrl;
      if (!tmpl) return;
      closeMenu(); closeGpsPanel();
      const rc = wrap.getBoundingClientRect();
      const p = document.createElement("div");
      p.className = "ospf-node-menu";
      // G-235: egne, selvstendige kontrastfarger (mørkt kort + lys tekst) — IKKE arv kart-bakgrunnen,
      // ellers blir det blå-på-blå når intet bakgrunnskart er valgt (kanvas-fargen er mørk).
      p.style.cssText = "position:absolute;z-index:21;min-width:220px;padding:8px;border-radius:8px;"
        + "box-shadow:0 2px 10px rgba(0,0,0,.45);font:12px sans-serif;"
        + "background:#0e1530;color:#e4e8ff;border:1px solid #3a4480";
      p.style.left = ((n.x / (cfg.width || 1)) * rc.width + 12) + "px";
      p.style.top = ((n.y / (cfg.height || 1)) * rc.height - 6) + "px";
      p.addEventListener("pointerdown", (e) => e.stopPropagation());
      const head = document.createElement("div");
      head.style.cssText = "font-weight:600;margin:0 0 4px;color:#ffffff";
      head.textContent = "GPS / NetBox — " + (n.label || n.rid || n.id || "");
      const body = document.createElement("div");
      body.style.cssText = "white-space:pre-line;font-family:monospace;font-size:11px;margin:0 0 6px;color:#dbe4ff";
      body.textContent = i18n.t("js.renderer.loading");
      const placeBtn = document.createElement("button");
      placeBtn.type = "button"; placeBtn.className = "ghost sm";
      placeBtn.style.cssText = "display:block;width:100%;margin-bottom:4px";
      placeBtn.textContent = i18n.t("js.renderer.gps_place"); placeBtn.disabled = true;
      const closeBtn = document.createElement("button");
      closeBtn.type = "button"; closeBtn.className = "ghost sm";
      closeBtn.style.cssText = "display:block;width:100%";
      closeBtn.textContent = i18n.t("js.renderer.close"); closeBtn.addEventListener("click", closeGpsPanel);
      p.appendChild(head); p.appendChild(body); p.appendChild(placeBtn); p.appendChild(closeBtn);
      wrap.appendChild(p); gpsPanel = p;
      const url = tmpl.replace("/0/", "/" + encodeURIComponent(_nKey(n)) + "/");
      const fmt = v => (v === null || v === undefined) ? "—" : v;
      fetch(url, { headers: { "Accept": "application/json" } }).then(r => {
        if (!r.ok || r.redirected) throw new Error("HTTP " + r.status);
        return r.json();
      }).then(d => {
        const ll = (d.lat !== null && d.lon !== null) ? (d.lat + ", " + d.lon) : i18n.t("js.renderer.gps_none");
        const lines = [
          i18n.t("js.renderer.gps_source", { v: d.source + (d.netbox_id ? " #" + d.netbox_id : "") }),
          (d.ghost ? i18n.t("js.renderer.gps_type_ghost") : null),
          "Site:         " + fmt(d.site),
          "GPS lat,lon:  " + ll,
          i18n.t("js.renderer.gps_pinned", { v: d.pinned ? i18n.t("js.renderer.yes") : i18n.t("js.renderer.no") }),   // x/y på kartet (GPS-lås er ikke bygget ennå)
          i18n.t("js.renderer.gps_geo_bg", { v: d.has_geo_bg ? i18n.t("js.renderer.yes") : i18n.t("js.renderer.gps_geo_bg_none") }),
        ].filter(x => x !== null);
        if (d.canvas) {
          lines.push(i18n.t("js.renderer.gps_map_pos", { v: d.canvas[0] + ", " + d.canvas[1]
            + (d.on_map ? "" : "  " + i18n.t("js.renderer.gps_off_view")) }));
          lines.push(i18n.t("js.renderer.gps_raw_px", {
            px: d.raw_bg_px ? d.raw_bg_px[0] + ", " + d.raw_bg_px[1] : "—",
            bg: d.bg_dims ? d.bg_dims.join("×") : "?", canvas: d.canvas_dims.join("×") }));
        } else if (d.has_geo_bg) {
          lines.push(i18n.t("js.renderer.gps_map_pos_none"));
        }
        body.textContent = lines.join("\n");
        if (d.canvas) {
          placeBtn.disabled = false;
          placeBtn.addEventListener("click", () => {
            n.x = Math.min(cfg.width, Math.max(0, d.canvas[0]));
            n.y = Math.min(cfg.height, Math.max(0, d.canvas[1]));
            markDirty(); draw(); closeGpsPanel();
          });
        }
      }).catch(() => { body.textContent = i18n.t("js.renderer.gps_failed"); });
    }
    document.addEventListener("pointerdown", () => { if (gpsPanel) closeGpsPanel(); });

    // --- tegne-loop: bakgrunn → lenker → noder → adapter.afterDraw → kreditt. ---
    function draw() {
      svg.innerHTML = "";
      svg.appendChild(el("rect", { x: 0, y: 0, width: cfg.width, height: cfg.height,
        fill: cfg.background || "#f0f1f7" }));   // 1.8.2: gråhvit default
      // G-181/G-168 (fase 2): bakgrunns-lag — live-fliser ELLER lagret snapshot, dempet + diffust.
      // Engine-parity m/ det fysiske kartet. Live-fliser via den delte bg_live.js (drawLiveBg);
      // vi shimmer den flate cfg-en til formen drawLiveBg forventer ({canvas:{...}, bg_live, ...}).
      const bgOp = (cfg.bg_opacity == null ? 22 : cfg.bg_opacity) / 100;
      // I redigeringsmodus: bakgrunnen skal IKKE fange klikk (så drag/tom-klikk når noder/bg-rect).
      const bgNoEvents = state.editing;
      if (cfg.bg_mode === "live" && cfg.bg_live && cfg.bg_live.url && typeof window.drawLiveBg === "function") {
        drawLiveBg(svg, { canvas: { width: cfg.width, height: cfg.height },
                          bg_live: cfg.bg_live, google_session: cfg.google_session }, bgOp, bgNoEvents);
      } else if (cfg.bg_mode !== "live" && cfg.has_bg_image && cfg.bg_url) {   // a live map has no stored image
        const im = el("image", { x: 0, y: 0, width: cfg.width, height: cfg.height,
          preserveAspectRatio: "xMidYMid slice", opacity: bgOp });
        im.style.filter = "blur(1.5px) saturate(0.65)";
        if (bgNoEvents) im.style.pointerEvents = "none";
        im.setAttributeNS("http://www.w3.org/1999/xlink", "href", cfg.bg_url);
        im.setAttribute("href", cfg.bg_url);
        svg.appendChild(im);
      }
      // Rutenett-overlegg — KUN i redigeringsmodus (aldri i visning), som det fysiske kartet.
      if (state.editing && state.gridSize > 0) {
        const g = state.gridSize;
        for (let gx = g; gx < (cfg.width || 0); gx += g)
          svg.appendChild(el("line", { x1: gx, y1: 0, x2: gx, y2: cfg.height,
            stroke: theme.GRID, "stroke-width": 0.5, "stroke-opacity": 0.6 }));
        for (let gy = g; gy < (cfg.height || 0); gy += g)
          svg.appendChild(el("line", { x1: 0, y1: gy, x2: cfg.width, y2: gy,
            stroke: theme.GRID, "stroke-width": 0.5, "stroke-opacity": 0.6 }));
      }
      const linkVisible = l => adapter.linkVisible ? adapter.linkVisible(l, ctx) : true;
      const nodeVisible = n => adapter.nodeVisible ? adapter.nodeVisible(n, ctx) : true;
      (cfg.links || []).forEach(l => { if (linkVisible(l)) adapter.drawLink(svg, l, ctx); });
      (cfg.nodes || []).forEach(n => { if (nodeVisible(n)) adapter.drawNode(svg, n, ctx, { bubbles: true }); });
      if (adapter.afterDraw) adapter.afterDraw(svg, ctx);
      drawCredit();
      drawAttribution();
    }

    // The background's attribution, bottom left, on every map type (the tile sources require it).
    function drawAttribution() {
      const text = bgAttribution(cfg);
      if (!text) return;
      const at = el("text", { x: 8, y: cfg.height - 8, fill: theme.FAINT, "font-size": 11, opacity: 0.85 });
      at.textContent = text;
      svg.appendChild(at);
    }

    // Kreditt + tidsstempel nederst til høyre (G-155/158). «Generert» i visning,
    // «Redigert» i redigeringsmodus.
    function drawCredit() {
      const _st = el("text", { x: cfg.width - 14, y: cfg.height - 14, "text-anchor": "end",
        fill: theme.FAINT, "font-size": 16, "font-weight": 600, opacity: 0.8 });
      // G-307: tidsstempel i appens tidssone (window.APP_TZ), ikke nettleserens lokaltid.
      const _pt = {};
      new Intl.DateTimeFormat(i18n.locale, { timeZone: window.APP_TZ || "Europe/Oslo",
        hour: "2-digit", minute: "2-digit", day: "2-digit", month: "2-digit", year: "numeric" })
        .formatToParts(new Date()).forEach(p => { _pt[p.type] = p.value; });
      const _tv = { HH: _pt.hour, mm: _pt.minute, DD: _pt.day, MM: _pt.month, YYYY: _pt.year };
      _st.textContent = state.editing ? i18n.t("js.renderer.credit_edited", _tv)
                                      : i18n.t("js.renderer.credit_generated", _tv);
      svg.appendChild(_st);
    }

    // G-233: lås/lås opp ALLE felles edit-kontroller etter redigeringstilstand. Når Rediger er AV
    // skal ingen av dem være klikkbare (vi vet da nøyaktig hva som er redigerbart).
    function _setEnabled(el, on) {
      if (!el) return;
      if (el.tagName === "A") { el.style.pointerEvents = on ? "" : "none"; el.toggleAttribute("disabled", !on); }
      else el.disabled = !on;
    }
    function setCommonEditEnabled(on) {
      document.querySelectorAll("#eb-arrange button, #eb-flip button").forEach(b => _setEnabled(b, on));
      ["eb-save-draft", "eb-discard-draft", "eb-activate", "eb-grid-sel", "eb-gps-sel", "eb-bg-link"]
        .forEach(id => _setEnabled(document.getElementById(id), on));
    }

    // --- generiske kontroller (rediger / lagre / auto-arranger) ---
    if (C.editToggle) {
      C.editToggle.addEventListener("click", () => {
        state.editing = !state.editing;
        C.editToggle.classList.toggle("on", state.editing);
        setCommonEditEnabled(state.editing);   // G-233: lås/lås opp HELE edit-raden
        closeMenu();
        if (!state.editing) { setStatus(hasDraft ? i18n.t("js.renderer.draft_active") : ""); }
        else setStatus(hasDraft ? i18n.t("js.renderer.editing_draft") : i18n.t("js.renderer.edit_hint"));
        if (adapter.onEditChange) adapter.onEditChange(state.editing, ctx);
        draw();
      });
    }
    setCommonEditEnabled(state.editing);   // G-233: initial låsing (start uten redigering → alt låst)
    // G-233: etter Forkast (reload) — bli værende i redigeringsmodus (flagg overlever reload).
    try {
      if (C.editToggle && (sessionStorage.getItem("bf_edit_keep") === "1"
                           || svg.dataset.startEditing === "1")) {   // G-267: arbeidskopi åpnes alltid i redigering
        sessionStorage.removeItem("bf_edit_keep");
        state.editing = true;
        C.editToggle.classList.add("on");
        setCommonEditEnabled(true);
        if (adapter.onEditChange) adapter.onEditChange(true, ctx);
      }
    } catch (e) { /* noop */ }
    if (C.save) {
      C.save.addEventListener("click", async () => {
        const payload = { nodes: layoutPayload() };
        C.save.disabled = true; setStatus(i18n.t("js.renderer.saving"));
        try {
          // url_for-sti fra template (subpath-trygt) — ikke hardkodet.
          const r = await fetch(svg.dataset.saveUrl, { method: "POST",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
          if (!r.ok) throw new Error("http " + r.status);
          const j = await r.json();
          if (!j || j.ok !== true) throw new Error(i18n.t("js.renderer.reply_not_ok"));
          if (j.url) { window.location = j.url; return; }   // G-267: logisk «Aktiver» merger arbeidskopi→live → naviger til live-visningen
          setClean();   // G-260: lagret = ingen ulagrede endringer
          hasDraft = false;   // commit tømmer utkastet på server
          setStatus(i18n.t("js.renderer.saved_activated", { count: j.saved != null ? j.saved : payload.nodes.length }));
        } catch (e) {
          console.error("kart lagre feilet", e);
          setStatus(i18n.t("js.renderer.save_failed", { err: e && e.message ? e.message : i18n.t("js.renderer.unknown_error") }));
        } finally { C.save.disabled = false; }
      });
    }
    // G-181: «Lagre utkast» — lagre WIP uten å aktivere (live urørt).
    if (C.saveDraft) {
      C.saveDraft.addEventListener("click", async () => {
        const payload = { nodes: layoutPayload() };
        C.saveDraft.disabled = true; setStatus(i18n.t("js.renderer.saving_draft"));
        try {
          const r = await fetch(svg.dataset.saveDraftUrl, { method: "POST",
            headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
          if (!r.ok) throw new Error("http " + r.status);
          const j = await r.json();
          if (!j || j.ok !== true) throw new Error(i18n.t("js.renderer.reply_not_ok"));
          setClean();   // G-260: utkast lagret = ingen ulagrede endringer (rent → neste Forkast forkaster draften)
          hasDraft = true;
          setStatus(i18n.t("js.renderer.draft_saved", { count: j.saved != null ? j.saved : payload.nodes.length }));
        } catch (e) {
          console.error("lagre utkast feilet", e);
          setStatus(i18n.t("js.renderer.save_draft_failed", { err: e && e.message ? e.message : i18n.t("js.renderer.unknown_error") }));
        } finally { C.saveDraft.disabled = false; }
      });
    }
    // G-260: «Forkast» = ÉTT steg tilbake (to-trinns peel, identisk på fysisk + logisk):
    //   (1) har du ULAGREDE endringer → revert til sist lagrede (reload → re-les persistert arbeidskopi);
    //   (2) er du REN (ingen ulagrede endringer) → forkast HELE arbeidskopien → tilbake til aktiv/live.
    // Slik kommer du progressivt: ulagrede endringer → lagret draft → aktiv. (Erstatter G-181 som alltid
    // forkastet, og G-233-fysisk som alltid bare reloadet.) Begge adaptere kaller denne ENE funksjonen.
    async function forkast(o) {
      const opt = o || {};
      const reloadToSaved = opt.reloadToSaved || (() => location.reload());
      const discardUrl = opt.discardUrl || svg.dataset.discardDraftUrl;
      try { sessionStorage.setItem("bf_edit_keep", "1"); } catch (e) { /* noop */ }   // bli i redigering etter reload
      if (dirty) {                       // (1) peel ulagrede endringer → sist lagrede
        reloadToSaved();
        return;
      }
      // (2) rent → forkast arbeidskopi → aktiv
      setStatus(i18n.t("js.renderer.discarding_draft"));
      if (C.discardDraft) C.discardDraft.disabled = true;
      try {
        const r = await fetch(discardUrl, { method: "POST" });
        if (!r.ok) throw new Error("http " + r.status);
        if (r.redirected && r.url) { window.location = r.url; } else { location.reload(); }
      } catch (e) {
        console.error("forkast feilet", e);
        setStatus(i18n.t("js.renderer.discard_failed")); if (C.discardDraft) C.discardDraft.disabled = false;
      }
    }
    if (C.discardDraft) {
      // logisk (og evt. andre som sender controls): reload re-leser persistert arbeidskopi.
      C.discardDraft.addEventListener("click", () => forkast({
        reloadToSaved: () => location.reload(), discardUrl: svg.dataset.discardDraftUrl }));
    }
    if (C.relayout) {
      C.relayout.addEventListener("click", async () => {
        if (!window.confirm(i18n.t("js.renderer.confirm_relayout"))) return;
        C.relayout.disabled = true; setStatus(i18n.t("js.renderer.arranging"));
        try {
          const r = await fetch(svg.dataset.relayoutUrl, { method: "POST" });
          if (!r.ok) throw new Error("http " + r.status);
          const j = await r.json();
          if (!j || j.ok !== true) throw new Error(i18n.t("js.renderer.reply_not_ok"));
          location.reload();
        } catch (e) {
          console.error("kart auto-arranger feilet", e);
          setStatus(i18n.t("js.renderer.relayout_failed")); C.relayout.disabled = false;
        }
      });
    }

    // G-233: FELLES auto-layout (Auto/Forbedre/Nytt) for ALLE kart. Plasserer enhetene og anvender
    // resultatet på arbeidskopien — lagrer IKKE, forlater IKKE editor (angre = forkast utkast).
    // Server beregner + returnerer {positions}. opts: {url, key, gps, onDone}. key(n) gir node-nøkkelen
    // (fysisk: n.id · logisk: n.rid) som posisjonene er nøklet på.
    async function runAutoLayout(mode, o) {
      o = o || {};
      const url = o.url || svg.dataset.relayoutUrl;
      if (!url) return;
      const key = o.key || (n => (n.id != null ? n.id : n.rid));
      const body = { mode: mode,
        nodes: JSON.stringify((cfg.nodes || []).map(n => ({ id: key(n), x: n.x, y: n.y, pinned: !!n.pinned }))) };
      if (o.gps) body.gps = o.gps;
      setStatus(i18n.t("js.renderer.placing"));
      try {
        const r = await fetch(url, { method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams(body) });
        const d = r.redirected ? {} : await r.json().catch(() => ({}));
        if (!r.ok || r.redirected || d.error) {
          setStatus("⚠ " + (d.msg || d.error || (r.redirected ? i18n.t("js.renderer.session_expired") : i18n.t("js.renderer.error_status", { status: r.status }))));
          return;
        }
        const p = d.positions || {};
        let maxMove = 0, applied = 0;
        (cfg.nodes || []).forEach(n => {
          const q = p[String(key(n))];
          if (q) { maxMove = Math.max(maxMove, Math.hypot(q[0] - n.x, q[1] - n.y));
                   n.x = q[0]; n.y = q[1]; markDirty(); applied++; }
        });
        draw();
        // G-239: tilbakemelding når lite/ingenting flyttet seg — ellers ser det ut som knappen var død.
        // Synlig bevegelse er sin egen kvittering, så da sier vi ingenting.
        // G-261: «eksakt GPS» uten geo-referert bakgrunn falt tilbake til relativ — si det tydelig.
        if (d.gps_fallback) setStatus(i18n.t("js.renderer.gps_fallback"));
        else if (!applied) setStatus(i18n.t("js.renderer.no_nodes"));
        else if (maxMove < 6) setStatus(mode === "improve"
          ? i18n.t("js.renderer.improve_nochange")
          : i18n.t("js.renderer.layout_nochange"));
        else setStatus("");
        if (o.onDone) o.onDone();
      } catch (e) { console.error("auto-layout feilet", e); setStatus(i18n.t("js.renderer.place_failed")); }
    }

    // ctx: alt adapteren trenger (delte helpere + tema + delt tilstand + callbacks).
    const ctx = {
      el, esc, cvar, wrapLabel, arrowHead, endLabelPos, linkGeom, show, hide,
      theme, cfg, state, byRid, svg,
      isAdmin: !!opts.isAdmin, adoptUrl: opts.adoptUrl || "/admin",
      public: !!opts.public,   // G-183: public-visning (read-only, ingen navigasjon til innloggede ruter)
      attachEdit, markDirty, draw, openMenu, runAutoLayout, setCommonEditEnabled,   // G-186/G-233
      setClean, isDirty: () => dirty, forkast,   // G-260: to-trinns «Forkast» (delt logikk, begge kart)
      closeMenu, showGpsInfo,   // G-237: universelt GPS/NetBox-info-panel
      showPop, closePop,        // 1.8.2: delt node-popup (trykk-info, valgfri handling)
      INK: theme.INK, FAINT: theme.FAINT, GRID: theme.GRID,
      OK: theme.OK, BAD: theme.BAD, DOWN: theme.DOWN, WARNING: theme.WARNING
    };

    if (adapter.initControls) adapter.initControls(ctx);

    // G-233: FELLES, LÅST redigerings-rad (#eb-*) — wires ÉN gang her i kjernen for ALLE kart.
    // Adapteren gir kun node-nøkkel (id vs rid). GPS av/på + Rutenett er felles tilstand.
    const _nodeKey = adapter.nodeKey || (n => (n.id != null ? n.id : n.rid));
    const ebArrange = document.getElementById("eb-arrange");
    if (ebArrange) {
      ebArrange.querySelectorAll("button[data-mode]").forEach(btn => {
        btn.addEventListener("click", () => {
          runAutoLayout(btn.dataset.mode, {
            url: svg.dataset.relayoutUrl, key: _nodeKey,
            gps: (state.gpsMode !== "off" && btn.dataset.mode !== "improve") ? state.gpsMode : undefined
          });
        });
      });
    }
    const ebGrid = document.getElementById("eb-grid-sel");
    if (ebGrid) {
      ebGrid.addEventListener("change", () => { state.gridSize = parseInt(ebGrid.value, 10) || 0; draw(); });
    }
    const ebGps = document.getElementById("eb-gps-sel");
    if (ebGps) {
      state.gpsMode = ebGps.value || "off";
      ebGps.addEventListener("change", () => { state.gpsMode = ebGps.value || "off"; });
    }

    // G-292: flipp/flopp — speil ALLE noder om deres egen bounding-box-senterlinje (klyngen blir
    // stående, bare vendt). Ignorerer pinning bevisst: hele bildet snus, den speilvendte plasseringen
    // BLIR den nye. Arbeidskopi (markDirty) — ikke persistert før Lagre; klikk igjen = ufarlig angre.
    function flipNodes(axis) {
      const ns = cfg.nodes || [];
      if (!ns.length) return;
      const k = axis === "y" ? "y" : "x";
      let lo = Infinity, hi = -Infinity;
      ns.forEach(n => { if (n[k] < lo) lo = n[k]; if (n[k] > hi) hi = n[k]; });
      const sum = lo + hi;                       // speil om (lo+hi)/2  ⇒  n' = (lo+hi) − n
      ns.forEach(n => { n[k] = sum - n[k]; });
      markDirty();
      draw();
    }
    const ebFlipH = document.getElementById("eb-flip-h");
    if (ebFlipH) ebFlipH.addEventListener("click", () => flipNodes("x"));
    const ebFlipV = document.getElementById("eb-flip-v");
    if (ebFlipV) ebFlipV.addEventListener("click", () => flipNodes("y"));

    // G-181: gjør admin oppmerksom på et aktivt utkast ved innlasting.
    if (hasDraft) setStatus(i18n.t("js.renderer.draft_active_hint"));

    this.ctx = ctx;
    this.draw = draw;
    draw();
  }

  global.BifrostMap = global.BifrostMap || {};
  Object.assign(global.BifrostMap, {
    el, esc, cvar, wrapLabel, arrowHead, endLabelPos, linkGeom, wireBuild, bgAttribution, MapRenderer
  });
})(window);
