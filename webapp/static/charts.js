// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
// SPDX-License-Identifier: AGPL-3.0-or-later
/* uPlot helpers for bifrost */

/* Read the brand colours from :root — the app.css default OR an uploaded theme — instead of
   hard-coded hex, so the graphs follow the theme. The second argument is the fallback if the
   token is missing. */
const cvar = (n, f) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f;
function brandPalette() {
  const T = { "--bl-800": "#3a14c6", "--gr-700": "#14dd86", "--bl-500": "#8181f1",
              "--bl-600": "#4e4feb", "--gr-500": "#64f7af", "--bl-900": "#181c56",
              "--bl-300": "#b3b3f6", "--gr-300": "#a5face", "--muted": "#6e72a6",
              "--danger": "#d23a57" };
  return Object.entries(T).map(([n, f]) => cvar(n, f));
}

function fmtBps(v) {
  if (v == null || isNaN(v)) return "—";
  const u = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"];
  let i = 0;
  while (Math.abs(v) >= 1000 && i < u.length - 1) { v /= 1000; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}

/* G-273: ADAPTIV y-akse-bredde — mål faktisk bredeste etikett i stedet for en fast px-verdi.
   Fast «size: 70» klippet det første sifferet («80,0 Gbps») når font/zoom/DPI gjorde teksten
   bredere enn renna. uPlots kanoniske auto-size-oppskrift: mål bredeste formaterte verdi med
   aksefonten på den (DPR-skalerte) canvasen, del på devicePixelRatio for CSS-px, legg til
   ticks+gap. Re-måler av seg selv → robust for alle font/zoom/DPI OG forward-kompatibel med
   den kommende dynamiske skaleringen ([[G-274]]), som bare endrer fonten — gutteren følger med. */
function yAxisSize(self, values, axisIdx, cycleNum) {
  const axis = self.axes[axisIdx];
  if (cycleNum > 1) return axis._size;                 // stabiliser etter første måle-pass
  let sz = (axis.ticks ? axis.ticks.size : 0) + (axis.gap || 0);
  const longest = (values || []).reduce((a, v) => (v && v.length > a.length ? v : a), "");
  if (longest) {
    self.ctx.font = axis.font[0];                      // samme font uPlot tegner med (DPR-inkludert)
    sz += self.ctx.measureText(longest).width / (window.devicePixelRatio || 1);
  }
  return Math.ceil(sz + 4);                            // liten ekstra klaring mot kanten
}

/* 24-hour clock + month and weekday names and date formats from the chosen language pack */
const _L = (k) => i18n.t(k).split(",");
const NO_NAMES = {
  MMMM: _L("js.date.months"), MMM: _L("js.date.months_short"),
  WWWW: _L("js.date.weekdays"), WWW: _L("js.date.weekdays_short"),
};
const fmtHM   = uPlot.fmtDate("{HH}:{mm}", NO_NAMES);
const fmtDM   = uPlot.fmtDate(i18n.t("js.date.axis_day_month"), NO_NAMES);
const fmtMon  = uPlot.fmtDate("{MMM}", NO_NAMES);
const fmtFull = uPlot.fmtDate(i18n.t("js.date.full"), NO_NAMES);

/* A placeholder message in a chart box; set as text, so a translation is never parsed as HTML. */
function emptyNote(el, text) {
  el.innerHTML = '<div class="empty"></div>';
  el.firstChild.textContent = text;
}

/* Velg format etter synlig spenn: timer→HH:mm, dager→D.M, måneder→MMM */
function xAxisVals(u, splits) {
  const span = splits.length > 1 ? splits[splits.length - 1] - splits[0] : 0;
  const f = span > 86400 * 120 ? fmtMon : span > 86400 * 3 ? fmtDM : fmtHM;
  return splits.map(v => f(new Date(v * 1000)));
}

function makeChart(elId, colors, opts) {
  const el = document.getElementById(elId);
  let u = null;
  let lastLabels = null;        // serie-navn fra forrige lasting
  let showRecord = !!(opts && opts.record);   // G-156: rekord-linje av/på (default fra opts)
  const onZoom = opts && opts.onZoom;             // G-278: dra-marker → zoom-vindu (fra,til) epoch-sek
  const onResetZoom = opts && opts.onResetZoom;   // G-278: dobbeltklikk → tilbake til valgt preset

  function size() {
    // 1.8.0 (Gitea #12): responsiv høyde — proporsjonal med bredden, klampet (desktop ~320, mobil ~200).
    // ResizeObserver kaller setSize ved resize, så høyden følger skjermen.
    const w = el.clientWidth || 320;
    return { width: w, height: Math.max(200, Math.min(320, Math.round(w * 0.4))) };
  }

  /* G-156: tegn en dempet, stiplet horisontal rekord-linje per SYNLIG serie som har _alltime
     (all-time high for valgt retning), i seriens farge — så man ser hvor nær rekorden man er.
     Av/på via setRecord(); kun når showRecord er på. Default settes per graf (opts.record). */
  function drawRecordLines(u) {
    if (!showRecord) return;
    const ctx = u.ctx, L = u.bbox.left, W = u.bbox.width;
    ctx.save();
    ctx.setLineDash([5, 4]); ctx.lineWidth = 1.5; ctx.globalAlpha = 0.7;
    for (let i = 1; i < u.series.length; i++) {
      const sr = u.series[i];
      if (sr.show === false || !sr._alltime) continue;
      const y = u.valToPos(sr._alltime, "y", true);
      if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) continue;   // utenfor synlig y -> hopp
      ctx.strokeStyle = sr.stroke();
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + W, y); ctx.stroke();
      // verdi-merkelapp (all-time = peak 1t-snitt; ligger derfor litt under 5-min-toppene)
      ctx.setLineDash([]); ctx.fillStyle = sr.stroke();
      ctx.font = "10px system-ui,sans-serif"; ctx.textAlign = "left"; ctx.textBaseline = "bottom";
      ctx.fillText(i18n.t("js.chart.alltime", {v: fmtBps(sr._alltime)}), L + 4, y - 1);
      ctx.setLineDash([5, 4]);
    }
    ctx.restore();
  }

  // setData(.. , true) re-kjører Y-range-funksjonen over så aksen utvides/krymper med rekord av/på.
  function setRecord(on) { showRecord = !!on; if (u) u.setData(u.data, true); }

  /* G-278: dra-marker i grafen → les tidsvindu (epoch-sek) og be templaten hente FINERE data for
     akkurat det vinduet (span→tier i backend). setScale:false → uPlot zoomer ikke visuelt selv;
     vi nullstiller markeringen (2. arg false = ingen re-fyring) og lar refetch tegne det nye vinduet. */
  function onSetSelect(u) {
    if (!onZoom) return;
    const sel = u.select;
    if (!sel || sel.width < 5) return;
    const a = u.posToVal(sel.left, "x"), b = u.posToVal(sel.left + sel.width, "x");
    u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
    if (b > a) onZoom(Math.round(a), Math.round(b));
  }

  function destroy() { if (u) { u.destroy(); u = null; } }

  function setData(payload, viewLabel) {
    const xs = payload.t || [];
    if (!xs.length) {
      destroy(); lastLabels = null;
      emptyNote(el, i18n.t("js.chart.no_data"));
      return;
    }

    const newLabels = (payload.series || []).map(s => s.name);

    // Hvis seriene er de samme som sist (typisk: kun retning/tidsrom endret),
    // ta vare på hvilke serier brukeren har slått av/på i forklaringen, så de
    // ikke nullstilles når grafen tegnes på nytt.
    let showState = null;
    if (u && lastLabels && lastLabels.length === newLabels.length
        && lastLabels.every((l, i) => l === newLabels[i])) {
      showState = u.series.slice(1).map(s => s.show !== false);
    }

    destroy();
    el.innerHTML = "";

    const pal = (colors && colors.length) ? colors : brandPalette();
    const axc = cvar("--muted", "#6e72a6"), gridc = cvar("--grid", "#ececf8");
    const vl = viewLabel || "";
    const series = [{
      label: "",
      value: (_, v) => v == null ? "" :
        fmtFull(new Date(v * 1000)) + (vl ? "  ·  " + vl : ""),
    }];
    const data = [xs];
    payload.series.forEach((s, idx) => {
      const c = pal[idx % pal.length];
      series.push({
        label: s.name, stroke: c, width: 2,
        // s.hidden => default avskrudd (kan slås på i forklaringen); ellers på.
        show: showState ? showState[idx] : !s.hidden,
        fill: idx === 0 && payload.series.length <= 3 ? c + "18" : undefined,
        // skjul trafikktallet i forklaringen når serien er valgt bort
        value: (u, v, sidx) => (u.series[sidx] && u.series[sidx].show === false) ? "—" : fmtBps(v),
        _alltime: (s.alltime != null && s.alltime > 0) ? s.alltime : null,   // G-156: rekord-nivå
      });
      data.push(s.data);
    });

    u = new uPlot({
      ...size(),
      /* G-152: aksetider/legende i appens valgte tidssone, ikke nettleserens */
      tzDate: ts => uPlot.tzDate(new Date(ts * 1e3), window.APP_TZ || "Europe/Oslo"),
      cursor: { points: { size: 5 },
        ...(onZoom ? { drag: { x: true, y: false, setScale: false } } : {}) },   // G-278: dra-zoom i x
      legend: { live: true },
      /* G-156: når rekord-linjer er på, utvid Y-aksen så høyeste synlige all-time får plass —
         ellers havner en historisk topp (høyere enn dagens trafikk) utenfor grafen og linja
         droppes (charts.js:66). Hele poenget er å se dagens nivå mot historisk maks. */
      scales: { y: { range: (u, min, max) => {
        let top = max;
        if (showRecord && u.series) {
          for (let i = 1; i < u.series.length; i++) {
            const sr = u.series[i];
            if (sr.show !== false && sr._alltime && sr._alltime > top) top = sr._alltime;
          }
        }
        return [0, top * 1.08 || 1];
      } } },
      axes: [
        { stroke: axc, grid: { stroke: gridc }, ticks: { stroke: gridc },
          values: xAxisVals },
        { stroke: axc, grid: { stroke: gridc }, ticks: { stroke: gridc },
          values: (_, vals) => vals.map(fmtBps), size: yAxisSize },
      ],
      series,
      hooks: { draw: [drawRecordLines],     // G-156: stiplede all-time-rekord-linjer
        ...(onZoom ? { setSelect: [onSetSelect] } : {}) },   // G-278: dra-marker → zoom-refetch
    }, data, el);
    lastLabels = newLabels;

    // G-158: diskré kreditt + «sist oppdatert»-stempel i selve graf-rammen, så krediten og
    // ferskheten følger med hvis noen tar screenshot. Tid i appens valgte tidssone (G-152).
    const cap = document.createElement("div");
    cap.className = "chart-credit";
    const t = new Date().toLocaleString(i18n.locale, { hour: "2-digit", minute: "2-digit",
      second: "2-digit", timeZone: window.APP_TZ || "Europe/Oslo" });
    cap.textContent = i18n.t("js.chart.credit", {time: t});
    el.appendChild(cap);
  }

  function clear() { destroy(); lastLabels = null; emptyNote(el, i18n.t("js.chart.pick_group")); }

  /* 1.8.1: nullstill chartet HELT (også husket av/på-tilstand i forklaringen) uten plassholder-tekst —
     brukes når link-modalen åpnes på nytt (ny link/retning skal ikke arve forrige valg). */
  function reset() { destroy(); lastLabels = null; el.innerHTML = ""; }

  /* R-062: hentefeil skal IKKE se ut som «Ingen data i perioden» — det sender feilsøking i feil retning */
  function fail() { destroy(); lastLabels = null; emptyNote(el, i18n.t("js.chart.load_failed")); }

  new ResizeObserver(() => { if (u) u.setSize(size()); }).observe(el);
  if (onResetZoom) el.addEventListener("dblclick", () => onResetZoom());   // G-278: tilbake til preset
  return { setData, clear, fail, setRecord, reset };
}

async function loadInto(chart, url, viewLabel) {
  /* G-147: sekvens-vakt — bytter brukeren spenn/retning raskt kan et TREGT, eldre svar lande
     ETTER det nyeste og stille overskrive grafen med feil tall. Bare siste utsendte vinner. */
  const seq = (chart._seq = (chart._seq || 0) + 1);
  try {
    const r = await fetch(url);
    if (seq !== chart._seq) return;                  // nyere lasting i gang — forkast
    if (!r.ok || r.redirected) { chart.fail(); return; }   // R-062: vis feil, ikke «ingen data»
    const payload = await r.json();
    if (seq !== chart._seq) return;
    chart.setData(payload, viewLabel);
  } catch (e) {
    console.error("load failed", url, e);
    if (seq === chart._seq) chart.fail();
  }
}

function bindSeg(id, cb) {
  const box = document.getElementById(id);
  box.addEventListener("click", e => {
    const b = e.target.closest("button");
    if (!b) return;
    box.querySelectorAll("button").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    cb(b.dataset.v);
  });
}

/* G-281: oppløsnings-etikett per spenn (variant C — vises ved range-knappene). Speiler
   den faktiske kilden: 36h=rå 5-min, 7d/30d=1t-cagg, 12/24/36/48mo=1d-cagg (tz-bevisst). */
function resLabel(range) {
  if (range === "36h") return i18n.t("js.res.5min");
  if (range === "7d" || range === "30d") return i18n.t("js.res.1h");
  return i18n.t("js.res.1d");            // 12/24/36/48mo
}

/* G-278: oppløsning for et zoom-vindu — speiler backend _window_tier() så etiketten viser den
   FAKTISKE oppløsningen vinduet får (5-min for korte nære, ellers 1-time, ellers 1-døgn). */
function zoomResLabel(frm, to) {
  const day = 86400, span = Math.max(60, to - frm), now = Date.now() / 1000;
  if (span <= 5 * day && frm >= now - 90 * day) return i18n.t("js.res.5min");
  if (span <= 100 * day) return i18n.t("js.res.1h");
  return i18n.t("js.res.1d");
}

/* G-281: normaliser backend-oppløsning (interface_history.resolution, f.eks. «rå (5 minutes)»,
   «1-times snitt», «1-dags snitt (…)») til SAMME konsise ordlyd som hovedgrafene bruker. */
function tidyRes(r, code) {
  if (code) return i18n.t("js.res." + code);   // the server's resolution code (5min/30min/1h/1d)
  if (!r) return "";
  if (/5[\s-]?min/.test(r)) return i18n.t("js.res.5min");
  if (/30[\s-]?min/.test(r)) return i18n.t("js.res.30min");
  if (/1-tim|1-time/.test(r)) return i18n.t("js.res.1h");
  if (/døgn|dag/.test(r)) return i18n.t("js.res.1d");
  return r;
}

/* G-281: range-velger med BÅDE knapper (36t/7d/30d) OG dropdown (12/24/36/48mnd i samme seg).
   Én av delene er aktiv om gangen — knapp får .on, ellers får dropdownen .on. */
function bindRange(cb) {
  const box = document.getElementById("range");
  if (!box) return;
  const sel = box.querySelector(".seg-sel");
  box.addEventListener("click", e => {
    const b = e.target.closest("button");
    if (!b) return;
    box.querySelectorAll("button").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    if (sel) sel.classList.remove("on");
    cb(b.dataset.v);
  });
  if (sel) sel.addEventListener("change", () => {
    box.querySelectorAll("button").forEach(x => x.classList.remove("on"));
    sel.classList.add("on");
    cb(sel.value);
  });
}

/* Admin: expand/collapse interface-panel per host.
   "Åpen" = panelet har innhold; "lukket" = tomt. Lukking tømmer panelet. */
function toggleHostIfaces(btn, id) {
  const p = document.getElementById("if-" + id);
  if (!p) return;
  const arr = btn.querySelector(".arr");
  if (p.innerHTML.trim()) {                 // åpent → lukk
    p.innerHTML = "";
    if (arr) arr.textContent = "▾";
  } else {                                  // lukket → åpne (last inn)
    if (arr) arr.textContent = "▴";
    htmx.ajax("GET", btn.dataset.url, { target: "#if-" + id, swap: "innerHTML" });
  }
}

/* Re-skann fyller samme panel — hold pilen på Interface-knappen i synk. */
function hostPanelOpened(btn) {
  const arr = btn.parentElement.querySelector(".arr");
  if (arr) arr.textContent = "▴";
}
