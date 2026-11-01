// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.2.2
// SPDX-License-Identifier: AGPL-3.0-or-later
/* G-129/R-083: delt live-bakgrunn (kartfliser) for visning OG editor — felles for de fysiske
   kart-skriptene. Plasserer kartfliser direkte i SVG-en ved det låste utsnittet
   (samme geo-ref som et snapshot ville hatt, så node-piksler er 1:1). Tiles hentes fra kilden
   ved visning (online), nodene forblir offline. Google trenger en session-token.
   R-067: session-PROMISET caches (ikke bare resultatet) så raske redraw aldri starter doble
   POSTer, og flis-gruppen får klassen "bg-live" + eksisterende lag fjernes før innsetting —
   en sen fullføring kan dermed aldri stable doble flislag. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  function el(n, a) { const e = document.createElementNS(NS, n); for (const k in a) e.setAttribute(k, a[k]); return e; }

  // Plasser flisene i svg-en. passThrough=true => klikk går gjennom til bakgrunns-rect (editor).
  function placeLiveTiles(svg, cfg, urlTmpl, opacity, passThrough) {
    const bg = cfg.bg_live, TS = 256;
    const W = cfg.canvas.width, H = cfg.canvas.height;
    const ox = bg.origin_px, oy = bg.origin_py, bw = bg.w, bh = bg.h, z = bg.zoom;
    if (!bw || !bh) return;
    const scale = Math.max(W / bw, H / bh);            // cover (som snapshotets preserveAspectRatio slice)
    const offx = (W - bw * scale) / 2, offy = (H - bh * scale) / 2;
    const outer = el("g", { opacity, "class": "bg-live" });   // R-067: gjenfinnbar -> aldri doble lag
    outer.style.filter = "blur(1.5px) saturate(0.65)";        // klippes til canvas + dempes/diffuseres
    if (passThrough) outer.style.pointerEvents = "none";
    const clip = el("clipPath", { id: "bglive-clip" });
    clip.appendChild(el("rect", { x: 0, y: 0, width: W, height: H }));
    outer.appendChild(clip);
    outer.setAttribute("clip-path", "url(#bglive-clip)");
    const inner = el("g", { transform: `translate(${offx},${offy}) scale(${scale})` });
    const tx0 = Math.floor(ox / TS), tx1 = Math.floor((ox + bw) / TS);
    const ty0 = Math.floor(oy / TS), ty1 = Math.floor((oy + bh) / TS);
    for (let tx = tx0; tx <= tx1; tx++) for (let ty = ty0; ty <= ty1; ty++) {
      const u = urlTmpl.replace("{z}", z).replace("{x}", tx).replace("{y}", ty);
      const im = el("image", { x: tx * TS - ox, y: ty * TS - oy, width: TS, height: TS });
      im.setAttributeNS("http://www.w3.org/1999/xlink", "href", u);
      im.setAttribute("href", u);
      inner.appendChild(im);
    }
    outer.appendChild(inner);
    svg.insertBefore(outer, svg.firstChild ? svg.firstChild.nextSibling : null);  // bak noder/linker
  }

  let _livePromise = null;   // R-067: cache PROMISET — én session-POST uansett hvor raskt draw() kalles

  // Tegn live bakgrunn i `svg` fra kartets cfg (bg_live + google_session).
  window.drawLiveBg = function (svg, cfg, opacity, passThrough) {
    const bg = cfg.bg_live;
    if (!bg || !bg.url) return;
    const place = (tmpl) => {
      const old = svg.querySelector(".bg-live");       // R-067: fjern eksisterende lag før innsetting
      if (old) old.remove();
      placeLiveTiles(svg, cfg, tmpl, opacity, passThrough);
    };
    if (bg.needs_session) {                            // Google: hent session-token (én gang, caches)
      if (!_livePromise) {
        // Eksplisitt CSRF-header: public-siden har ikke base.html sin fetch-wrapper.
        const CSRF = (document.querySelector('meta[name=csrf-token]') || {}).content || "";
        _livePromise = fetch(cfg.google_session, { method: "POST", headers: { "X-CSRFToken": CSRF } })
          .then(r => r.json().then(d => (r.ok && d.session)
            ? bg.url.replace("{session}", d.session).replace("{key}", d.key) : null))
          .catch(() => null);
      }
      // G-315: ikke cache en MISLYKKET session-henting for alltid — nullstill promise ved null,
      // så neste tegning prøver på nytt (ellers mister live-kartet geo-bakgrunn resten av økta).
      _livePromise.then(u => { if (u) place(u); else _livePromise = null; });
    } else {
      place(bg.url);
    }
  };
})();
