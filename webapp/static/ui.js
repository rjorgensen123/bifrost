// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
// SPDX-License-Identifier: AGPL-3.0-or-later
/* G-136: CSP-trygge, delegerte hendelser — erstatter inline on*-handlere (som blokkeres
   av en streng script-src). Delegering på document fanger også htmx-innsatt innhold.
   Funksjonene (toggleGrantGroup/toggleHostIfaces/hostPanelOpened) er globale (base.html/charts.js). */
(function () {
  document.addEventListener("submit", function (e) {
    // data-confirm-delete på et VANLIG (ikke-htmx) skjema: samme dobbel-bekreftelses-modal
    // som htmx-veien under (Avbryt som default). form.submit() ved ja går utenom denne
    // lytteren (native submit fyrer ikke submit-event), så ingen løkke.
    var d = e.target.getAttribute && e.target.getAttribute("data-confirm-delete");
    if (d) {
      e.preventDefault();
      confirmDelete(d, function () { e.target.submit(); });
      return;
    }
    var c = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (c && !window.confirm(c)) e.preventDefault();
  });
  // CSP-trygg erstatning for inline hx-on::after-request (htmx kompilerer hx-on med
  // new Function → EvalError under vår script-src uten 'unsafe-eval', så opprydningen
  // døde stille). data-reset-on-success = nullstill skjemaet ved vellykket request;
  // data-clear-on-success="<selector>" = tøm nærmeste matchende container.
  document.addEventListener("htmx:afterRequest", function (e) {
    var el = e.detail && e.detail.elt;
    if (!el || !el.getAttribute || !e.detail.successful) return;
    if (el.hasAttribute("data-reset-on-success") && el.reset) el.reset();
    var sel = el.getAttribute("data-clear-on-success");
    if (sel) {
      var t = el.closest && el.closest(sel);
      if (t) t.innerHTML = "";
    }
  });
  // Feilsvar fra en htmx-handling (4xx/5xx) swappes ikke av htmx — uten dette «gjorde knappen
  // ingenting». Vis serverens korte tekst (409/400-backstopene svarer med en setning), ellers en
  // melding ut fra status (403/500 svarer med en hel HTML-side som ikke skal inn i et fragment).
  var STATUS_MSG = {
    400: i18n.t("js.ui.http_400"),
    403: i18n.t("js.ui.http_403"),
    404: i18n.t("js.ui.http_404"),
    409: i18n.t("js.ui.http_409"),
    500: i18n.t("js.ui.http_500"),
    503: i18n.t("js.ui.http_503")
  };
  function showError(text) {
    var old = document.getElementById("hx-error");
    if (old) old.remove();
    var bar = document.createElement("div"); bar.id = "hx-error"; bar.className = "hx-error"; bar.setAttribute("role", "alert");
    var msg = document.createElement("span"); msg.textContent = text;
    var x = document.createElement("button"); x.type = "button"; x.className = "ghost sm"; x.textContent = i18n.t("js.ui.close");
    x.addEventListener("click", function () { bar.remove(); });
    bar.appendChild(msg); bar.appendChild(x);
    document.body.appendChild(bar);
  }
  document.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail && e.detail.xhr;
    if (!xhr) return;
    var body = (xhr.responseText || "").trim();
    var ctype = xhr.getResponseHeader("Content-Type") || "";
    var text = null;
    if (ctype.indexOf("text/html") === 0 && body && body.charAt(0) !== "<" && body.length <= 300) text = body;
    showError(text || STATUS_MSG[xhr.status] || i18n.t("js.ui.http_failed", {status: xhr.status}));
  });
  document.addEventListener("htmx:sendError", function () {
    showError(i18n.t("js.ui.send_error"));
  });
  document.addEventListener("change", function (e) {
    var t = e.target;
    if (t.matches && t.matches("[data-autosubmit]") && t.form) t.form.submit();
    else if (t.matches && t.matches("[data-grant-group]") && window.toggleGrantGroup) window.toggleGrantGroup(t);
  });
  // Egen dobbel-bekreftelse for destruktive handlinger (data-confirm-delete): modal med
  // «Avbryt» som DEFAULT (autofokus + Enter avbryter), så man ikke sletter ved et uhell.
  // window.confirm kan ikke ha Avbryt som default — derfor egen modal. Andre hx-confirm røres ikke.
  function confirmDelete(msg, onYes) {
    var ov = document.createElement("div"); ov.className = "confirm-overlay";
    var box = document.createElement("div"); box.className = "confirm-box card";
    var p = document.createElement("p"); p.className = "confirm-msg"; p.textContent = msg;
    var row = document.createElement("div"); row.className = "row"; row.style.justifyContent = "flex-end";
    var cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "ghost"; cancel.textContent = i18n.t("js.ui.cancel");
    var del = document.createElement("button"); del.type = "button"; del.className = "ghost sm danger"; del.textContent = i18n.t("js.ui.delete_permanent");
    function close() { ov.remove(); document.removeEventListener("keydown", onKey, true); }
    function onKey(ev) { if (ev.key === "Escape") { ev.preventDefault(); close(); } }
    cancel.addEventListener("click", close);
    del.addEventListener("click", function () { close(); onYes(); });
    row.appendChild(cancel); row.appendChild(del);
    box.appendChild(p); box.appendChild(row);
    ov.appendChild(box); document.body.appendChild(ov);
    document.addEventListener("keydown", onKey, true);
    cancel.focus();   // DEFAULT = Avbryt (Enter avbryter, Escape avbryter)
  }
  document.addEventListener("htmx:confirm", function (e) {
    var el = e.detail && e.detail.elt;
    var msg = el && el.getAttribute && el.getAttribute("data-confirm-delete");
    if (!msg) return;                 // ikke en slett-knapp -> normal htmx-flyt (vanlig hx-confirm uberørt)
    e.preventDefault();
    confirmDelete(msg, function () { e.detail.issueRequest(true); });
  });

  document.addEventListener("click", function (e) {
    // «Tilbake»-lenke uten javascript:-URL (blokkeres av CSP). Uten historikk følges href.
    var hb = e.target.closest && e.target.closest("[data-history-back]");
    if (hb && window.history.length > 1) { e.preventDefault(); window.history.back(); return; }
    var v = e.target.closest && e.target.closest("[data-toggle-virtual]");
    if (v) {
      var form = v.closest("form"), tbl = form && form.querySelector(".disc-table");
      if (tbl) tbl.classList.toggle("show-virtual", v.checked);
      return;
    }
    var ht = e.target.closest && e.target.closest("[data-host-toggle]");
    if (ht && window.toggleHostIfaces) { window.toggleHostIfaces(ht, ht.getAttribute("data-host-toggle")); return; }
    var ho = e.target.closest && e.target.closest("[data-host-opened]");
    if (ho && window.hostPanelOpened) { window.hostPanelOpened(ho); }
  });
})();
