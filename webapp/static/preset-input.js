// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
// SPDX-License-Identifier: AGPL-3.0-or-later
// Combobox for numeriske innstillinger: ÉN ramme der du enten velger et hurtigvalg fra en synlig
// nedtrekksliste ELLER skriver fritt — med lesbart tall (mellomrom hver 3. siffer, «10 000 000»).
// CSP-vennlig (lastes fra 'self', ingen inline-handlere). Deklareres med ett felt:
//   <input class="grouped-number combo-input" name="..." value="..."
//          data-presets="5000000:5 Tbps,10000000:10 Tbps,...">
// JS-en bygger nedtrekket + pilen rundt feltet. Uten data-presets blir det bare et gruppert tallfelt.
// Ved innsending strippes mellomrommene så rene siffer postes (serveren tåler også mellomrom).
(function () {
  "use strict";

  function group(digits) {
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  }

  function formatField(el) {
    var raw = (el.value || "").replace(/\D/g, "");
    el.value = raw ? group(raw) : "";
  }

  function parsePresets(s) {
    return (s || "").split(",").map(function (p) {
      var i = p.indexOf(":");
      return i < 0 ? null : [p.slice(0, i).trim(), p.slice(i + 1).trim()];
    }).filter(Boolean);
  }

  function buildCombo(input) {
    var presets = parsePresets(input.dataset.presets);
    if (!presets.length) return;
    var wrap = document.createElement("span");
    wrap.className = "combo";
    input.parentNode.insertBefore(wrap, input);
    wrap.appendChild(input);

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "combo-btn";
    btn.setAttribute("aria-label", i18n.t("js.preset.show_presets"));
    btn.textContent = "▾";          // ▾
    wrap.appendChild(btn);

    var list = document.createElement("ul");
    list.className = "combo-list";
    list.hidden = true;
    presets.forEach(function (p) {
      var li = document.createElement("li");
      li.textContent = p[1];
      li.dataset.value = p[0];
      list.appendChild(li);
    });
    wrap.appendChild(list);

    btn.addEventListener("click", function (e) {
      e.preventDefault();
      list.hidden = !list.hidden;
    });
    list.addEventListener("click", function (e) {
      var li = e.target.closest("li");
      if (!li) return;
      input.value = group(li.dataset.value.replace(/\D/g, ""));
      input.dispatchEvent(new Event("input", { bubbles: true }));
      list.hidden = true;
      input.focus();
    });
    document.addEventListener("click", function (e) {
      if (!wrap.contains(e.target)) list.hidden = true;
    });
  }

  function init() {
    document.querySelectorAll("input.grouped-number").forEach(function (el) {
      if (el.dataset.presets) buildCombo(el);
      formatField(el);
      el.addEventListener("input", function () {
        var pos = el.value.length - el.selectionStart;   // hold caret nær samme sted
        formatField(el);
        var np = el.value.length - pos;
        try { el.setSelectionRange(np, np); } catch (e) { /* ignore */ }
      });
      if (el.form) {
        el.form.addEventListener("submit", function () {
          el.value = (el.value || "").replace(/\D/g, "");
        });
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
