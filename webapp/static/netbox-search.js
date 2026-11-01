// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
// SPDX-License-Identifier: AGPL-3.0-or-later
// G-286: live NetBox device-søk på «Oppdag via SNMP»-adressefeltet. Skriv et hostnavn (min 3 tegn) →
// forslag fra NetBox → velg → enhetens faktiske IP hentes (primær hvis satt, ellers aktiv IPv4 fra
// IPAM) og fylles i feltet (dagens discover kjører som før). Skriver du en IP direkte, søker vi IKKE
// underveis (eksakt NetBox-oppslag skjer i backend ved discover). CSP-vennlig: 'self', ingen inline-handlere.
(function () {
  "use strict";

  function looksLikeIp(s) {
    return /^[0-9a-fA-F:.]+$/.test(s) && /[.:]/.test(s);   // grovt: siffer/hex + . eller :
  }
  function debounce(fn, ms) {
    let t;
    return function () { clearTimeout(t); const a = arguments, self = this;
      t = setTimeout(function () { fn.apply(self, a); }, ms); };
  }

  function wire(inp) {
    const url = inp.dataset.nbSearch;
    const resolveUrl = url.replace(/\/search$/, "/resolve");
    const wrap = document.createElement("span");
    wrap.className = "nb-wrap";
    inp.parentNode.insertBefore(wrap, inp);
    wrap.appendChild(inp);
    const list = document.createElement("ul");
    list.className = "nb-sug"; list.hidden = true;
    wrap.appendChild(list);
    const close = function () { list.hidden = true; list.innerHTML = ""; };

    // Operasjon 2: slå opp den valgte enhetens IP fra NetBox (detalj → primær, ellers IPAM) og fyll feltet.
    function pick(d, li, site) {
      li.textContent = d.name + "  (" + i18n.t("js.nbsearch.fetching_ip") + ")" + site;
      fetch(resolveUrl + "?id=" + encodeURIComponent(d.netbox_id), { headers: { Accept: "application/json" } })
        .then(function (r) { if (!r.ok || r.redirected) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (o) {
          if (o && o.address) { inp.value = o.address; close(); inp.focus(); }
          else { li.textContent = d.name + "  (" + i18n.t("js.nbsearch.no_ip") + ")" + site; li.className = "disabled"; }
        })
        .catch(function () { li.textContent = d.name + "  (" + i18n.t("js.nbsearch.lookup_failed") + ")" + site; li.className = "disabled"; });
    }

    const run = debounce(function () {
      const q = inp.value.trim();
      if (q.length < 3 || looksLikeIp(q)) { close(); return; }
      fetch(url + "?q=" + encodeURIComponent(q), { headers: { Accept: "application/json" } })
        .then(function (r) { if (!r.ok || r.redirected) throw new Error("HTTP " + r.status); return r.json(); })
        .then(function (rows) {
          list.innerHTML = "";
          if (!rows || !rows.length) { close(); return; }
          rows.slice(0, 50).forEach(function (d) {
            const li = document.createElement("li");
            const site = d.site_name ? (" · " + d.site_name) : "";
            li.textContent = d.name + site;   // operasjon 1: kun navn + site; IP hentes ved valg
            li.addEventListener("click", function () { pick(d, li, site); });
            list.appendChild(li);
          });
          list.hidden = false;
        })
        .catch(close);
    }, 250);

    inp.addEventListener("input", run);
    document.addEventListener("click", function (e) { if (!wrap.contains(e.target)) close(); });
  }

  function init() {
    document.querySelectorAll("input[data-nb-search]").forEach(wire);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
