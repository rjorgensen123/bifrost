// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.2
// SPDX-License-Identifier: AGPL-3.0-or-later
/* Trafikk-kart: nav (oss) + eiker (ASN/roller). Bruker fmtBps + bindSeg fra charts.js. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  // Colours are read from :root (the app.css default or an uploaded theme).
  const cvar = (n, f) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f;
  const ROLE_COLOR = { IX: cvar("--bl-800","#3a14c6"), PNI: cvar("--bl-600","#4e4feb"),
                       transit: cvar("--bl-500","#8181f1"), peering: cvar("--nf","#1484b6"),
                       CDN: cvar("--gr-700","#14dd86"), NNI: cvar("--role-nni","#16a085"),
                       customer_ia: cvar("--role-customer-ia","#2e86de"), customer: cvar("--bl-900","#181c56") };
  const MUTED = cvar("--muted","#6e72a6"), INK = cvar("--bl-900","#181c56");
  const HUB = cvar("--nf","#1484b6"), HUBSUB = cvar("--gr-100","#d5ffe9");
  const ROLE_LABEL = { IX:"IX", PNI:"PNI", CDN:"CDN", transit: i18n.t("js.role.transit"),
                       peering: i18n.t("js.role.peering"), NNI:"NNI",
                       customer_ia: i18n.t("js.role.customer_ia"), customer: i18n.t("js.role.customer") };
  const MAX_NODES = 15;

  const state = { range: "now", dir: "total", mode: "asn" };   // G-277: Fordeling default = Nå (øyeblikks)
  const svg = document.getElementById("map-svg");
  const tip = document.getElementById("map-tip");
  const wrap = document.getElementById("map-wrap");
  const legend = document.getElementById("map-legend");
  const API = (svg.getAttribute("data-api") || "/api/flowmap");
  let cache = null;

  function el(n, a) { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); return e; }
  function val(n) { return state.dir === "in" ? n.inn : state.dir === "out" ? n.ut : n.inn + n.ut; }
  function colorFor(r) { return ROLE_COLOR[r] || MUTED; }

  function note(msg) {
    svg.innerHTML = "";
    const t = el("text", { x: 340, y: 260, "text-anchor": "middle", fill: MUTED, "font-size": 13 });
    t.textContent = msg; svg.appendChild(t);
  }

  function load() {
    fetch(`${API}?range=${state.range}&mode=${state.mode}`, { headers: { "X-Requested-With": "fetch" } })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(d => {
        cache = { our_name: d.our_name, our_asn: d.our_asn,
                  nodes: (d.nodes || []).map(n => ({ label: n.label, asn: n.asn, role: n.role, inn: n.in, ut: n.out })) };
        render();
      })
      .catch(() => note(i18n.t("js.flowmap.load_failed")));
  }

  function render() {
    if (!cache) return;
    svg.innerHTML = "";
    const cx = 340, cy = 262, R = 168;

    let nodes = cache.nodes.slice().sort((a, b) => val(b) - val(a));
    if (nodes.length > MAX_NODES) {
      const rest = nodes.slice(MAX_NODES);
      const sum = rest.reduce((a, n) => ({ inn: a.inn + n.inn, ut: a.ut + n.ut }), { inn: 0, ut: 0 });
      nodes = nodes.slice(0, MAX_NODES);
      nodes.push({ label: i18n.t("js.flowmap.others", {n: rest.length}), asn: null, role: "_other", inn: sum.inn, ut: sum.ut });
    }

    if (!nodes.length) {
      const t = el("text", { x: cx, y: cy + 92, "text-anchor": "middle", fill: MUTED, "font-size": 13 });
      t.textContent = i18n.t("js.flowmap.no_traffic");
      drawHub(cx, cy); svg.appendChild(t); buildLegend(nodes); return;
    }

    const mx = Math.max.apply(null, nodes.map(val)) || 1;
    const wScale = v => 1.5 + Math.sqrt(v / mx) * 13;
    const rScale = v => 12 + Math.sqrt(v / mx) * 21;

    nodes.forEach((n, i) => {
      const ang = (-90 + i * 360 / nodes.length) * Math.PI / 180;
      n._x = cx + R * Math.cos(ang); n._y = cy + R * Math.sin(ang);
      n._co = Math.cos(ang); n._si = Math.sin(ang);
      const w = wScale(val(n)), col = colorFor(n.role);
      const line = el("line", { x1: cx, y1: cy, x2: n._x, y2: n._y, stroke: col, "stroke-opacity": 0.5, "stroke-width": w, "stroke-linecap": "round" });
      const hit = el("line", { x1: cx, y1: cy, x2: n._x, y2: n._y, stroke: "transparent", "stroke-width": Math.max(18, w + 8), "stroke-linecap": "round", "class": "bf-cur-pointer" });
      n._line = line;
      hit.addEventListener("mousemove", e => show(e, n));
      hit.addEventListener("mouseleave", () => hide(n));
      svg.appendChild(line); svg.appendChild(hit);
    });

    nodes.forEach(n => {
      const r = rScale(val(n)), col = colorFor(n.role);
      const c = el("circle", { cx: n._x, cy: n._y, r: r, fill: col, stroke: "#fff", "stroke-width": 2, "class": "bf-cur-pointer" });
      c.addEventListener("mousemove", e => show(e, n));
      c.addEventListener("mouseleave", () => hide(n));
      svg.appendChild(c);
      const name = state.mode === "func" ? (ROLE_LABEL[n.role] || n.label) : n.label;
      const sub = state.mode === "asn" && n.asn ? "AS" + n.asn : "";
      const lx = n._x + n._co * (r + 9), ly = n._y + n._si * (r + 9);
      const anchor = n._co > 0.3 ? "start" : (n._co < -0.3 ? "end" : "middle");
      const off = n._si < -0.3 ? -6 : (n._si > 0.3 ? 13 : 4);
      const t1 = el("text", { x: lx, y: ly + off, "text-anchor": anchor, fill: INK, "font-size": 13, "font-weight": 500 });
      t1.textContent = name; svg.appendChild(t1);
      if (sub) {
        const t2 = el("text", { x: lx, y: ly + off + 13, "text-anchor": anchor, fill: MUTED, "font-size": 11 });
        t2.textContent = sub; svg.appendChild(t2);
      }
    });

    drawHub(cx, cy);
    buildLegend(nodes);
  }

  function drawHub(cx, cy) {
    svg.appendChild(el("circle", { cx: cx, cy: cy, r: 46, fill: HUB, stroke: "#fff", "stroke-width": 3 }));
    const ct = el("text", { x: cx, y: cy - 1, "text-anchor": "middle", fill: "#fff", "font-size": 15, "font-weight": 500 });
    ct.textContent = cache.our_name || i18n.t("js.flowmap.us"); svg.appendChild(ct);
    if (cache.our_asn) {
      const cs = el("text", { x: cx, y: cy + 15, "text-anchor": "middle", fill: HUBSUB, "font-size": 11 });
      cs.textContent = cache.our_asn; svg.appendChild(cs);
    }
  }

  // R-063: legend bygges med createElement/textContent — n.role kommer fra API-data og skal
  // aldri interpoleres i innerHTML.
  function buildLegend(nodes) {
    legend.textContent = "";
    const seen = {};
    nodes.forEach(n => {
      if (n.role === "_other" || seen[n.role]) return;
      seen[n.role] = 1;
      const s = document.createElement("span");
      const dot = document.createElement("span");
      dot.className = "dot"; dot.style.background = colorFor(n.role);
      s.appendChild(dot);
      s.appendChild(document.createTextNode(ROLE_LABEL[n.role] || n.role));
      legend.appendChild(s);
    });
  }

  function show(e, n) {
    const our = cache.our_name || i18n.t("js.flowmap.us");
    const name = state.mode === "func" ? (ROLE_LABEL[n.role] || n.label) : n.label;
    // R-063: tooltip med DOM-API i stedet for innerHTML — our_name/label/role er API-data.
    tip.textContent = "";
    const b = document.createElement("b");
    b.textContent = `${our} → ${name}`;
    tip.appendChild(b);
    tip.appendChild(document.createTextNode(`: ${fmtBps(n.ut)}`));
    tip.appendChild(document.createElement("br"));
    tip.appendChild(document.createTextNode(`${name} → ${our}: ${fmtBps(n.inn)}`));
    const rc = wrap.getBoundingClientRect();
    let x = e.clientX - rc.left + 14, y = e.clientY - rc.top + 14;
    tip.style.left = x + "px"; tip.style.top = y + "px"; tip.style.opacity = 1;
    // G-315: klamp innenfor rammen på BEGGE akser etter faktisk tooltip-størrelse
    // (var hardkodet 190px uten y-clamp; map_renderer.js gjør allerede dette riktig).
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    if (x + tw > rc.width - 4) x = Math.max(4, e.clientX - rc.left - tw - 12);
    if (y + th > rc.height - 4) y = Math.max(4, e.clientY - rc.top - th - 12);
    tip.style.left = x + "px"; tip.style.top = y + "px";
    if (n._line) n._line.setAttribute("stroke-opacity", 0.95);
  }
  function hide(n) { tip.style.opacity = 0; if (n._line) n._line.setAttribute("stroke-opacity", 0.5); }

  bindSeg("range", v => { state.range = v; load(); });
  bindSeg("mode",  v => { state.mode = v; load(); });
  bindSeg("dir",   v => { state.dir = v; render(); });
  load();
})();
