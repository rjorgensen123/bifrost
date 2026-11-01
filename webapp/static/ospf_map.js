// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v1.5.0
// SPDX-License-Identifier: AGPL-3.0-or-later
/* Glue for det logiske OSPF-kartet (G-168 fase 1). All tegne-, redigerings- og
   meny-logikk ligger nå i den generiske kjernen (map_renderer.js) + den logiske
   adapteren (map_logical.js). Her gjør vi KUN DOM-oppkobling: koble bygg-knappen,
   lese topologi-dataene og instansiere MapRenderer med den logiske adapteren.
   Ingen eksterne libs. CSRF via global fetch-wrapper i base.html. */
(function () {
  "use strict";
  const M = window.BifrostMap;

  // Bygg-knapper finnes alltid (admin), også uten topologi-data. G-195: «Rebygg OSPF» + «Rebygg LSP»
  // (egne ruter, hver POST-er sin data-url og laster på nytt via den delte wireBuild).
  const buildBtn = document.getElementById("ospf-build");
  const lspBtn = document.getElementById("ospf-build-lsp");
  const runEl = document.getElementById("ospf-run");
  if (M && buildBtn) M.wireBuild(buildBtn, runEl);
  if (M && lspBtn) M.wireBuild(lspBtn, runEl);

  // Ingen topologi ennå (eller kjerne ikke lastet) → ingenting å tegne.
  const dataEl = document.getElementById("ospf-data");
  const svg = document.getElementById("ospf-canvas");
  if (!dataEl || !svg || !M) return;

  const cfg = JSON.parse(dataEl.textContent);
  new M.MapRenderer({
    svg: svg,
    wrap: document.getElementById("ospf-wrap"),
    tip: document.getElementById("ospf-tip"),
    cfg: cfg,
    adapter: M.makeLogicalAdapter(),
    isAdmin: svg.dataset.isAdmin === "1",
    public: svg.dataset.public === "1",   // G-183: read-only public-visning
    adoptUrl: svg.dataset.adoptUrl || "/admin",
    nodeGpsUrl: svg.dataset.nodeGpsUrl || null,   // G-237: universelt GPS/NetBox-info-panel
    // G-239: dra = FLYTT uten å låse (likt fysisk kart). Før låste logisk ved drag, så
    // manuelt plasserte noder ble pinnet og Auto/Nytt «gjorde ingenting». Eksplisitt lås via menyen.
    pinOnDrag: false,
    // G-233: felles redigerings-rad (#eb-*) fra delt partial — wires i kjernen.
    controls: {
      editToggle: document.getElementById("eb-edit-toggle"),
      save: document.getElementById("eb-activate"),
      saveDraft: document.getElementById("eb-save-draft"),
      discardDraft: document.getElementById("eb-discard-draft"),
      status: document.getElementById("eb-edit-status")
    }
  });
})();
