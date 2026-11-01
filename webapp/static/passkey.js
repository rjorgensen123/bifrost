// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
// SPDX-License-Identifier: AGPL-3.0-or-later
// 2.1.0 (Gitea #14): passkey/WebAuthn-seremonier — registrering (Min side) + innlogging
// (login-siden, brukernavn-løst). In-house: base64url-konvertering + navigator.credentials,
// ingen eksterne script. CSRF sendes av den globale fetch-wrapperen.
(function () {
  "use strict";
  function b64uToBuf(s) {
    s = String(s).replace(/-/g, "+").replace(/_/g, "/");
    const raw = atob(s + "=".repeat((4 - (s.length % 4)) % 4));
    const b = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) b[i] = raw.charCodeAt(i);
    return b.buffer;
  }
  function bufToB64u(buf) {
    let s = "";
    new Uint8Array(buf).forEach((x) => { s += String.fromCharCode(x); });
    return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function credToJSON(cred) {
    const r = cred.response;
    const out = { id: cred.id, rawId: bufToB64u(cred.rawId), type: cred.type,
                  authenticatorAttachment: cred.authenticatorAttachment || null,
                  clientExtensionResults: cred.getClientExtensionResults(),
                  response: { clientDataJSON: bufToB64u(r.clientDataJSON) } };
    if (r.attestationObject) {                       // registrering
      out.response.attestationObject = bufToB64u(r.attestationObject);
      if (r.getTransports) out.response.transports = r.getTransports();
    }
    if (r.authenticatorData) {                       // innlogging
      out.response.authenticatorData = bufToB64u(r.authenticatorData);
      out.response.signature = bufToB64u(r.signature);
      out.response.userHandle = r.userHandle ? bufToB64u(r.userHandle) : null;
    }
    return JSON.stringify(out);
  }
  function csrfHeaders(extra) {
    const h = Object.assign({ "Content-Type": "application/json" }, extra || {});
    const m = document.querySelector('meta[name=csrf-token]');   // sett SELV — ikke stol på
    if (m) h["X-CSRFToken"] = m.content;                          // global wrapper (kjører for sent)
    return h;
  }
  async function postFor(url, body) {
    const resp = await fetch(url, { method: "POST",
      headers: csrfHeaders(), body: body || "{}" });
    if (!resp.ok) {
      let msg = i18n.t("js.passkey.error_status", {status: resp.status});
      try { const je = await resp.json(); msg = je.msg || je.error || msg; } catch (e) { /* tom */ }
      throw new Error(msg);
    }
    return resp.json();
  }

  function hostIsBareIP() {
    var h = location.hostname || "";
    if (h === "[::1]" || h.indexOf(":") !== -1) return true;          // IPv6 (inkl. ::1)
    return /^\d{1,3}(\.\d{1,3}){3}$/.test(h);                       // IPv4
  }
  window.bfPasskey = {
    // Finnes API-et i det hele tatt?
    supported: function () { return !!(window.PublicKeyCredential && navigator.credentials); },
    // Kan passkey FAKTISK kjøre her? Nettleseren krever secure context (HTTPS/localhost) OG
    // et domenenavn som RP ID — rå IP-adresse avvises uansett gyldig TLS. Skjuler UI-et
    // der det er dømt til «operation is insecure», så bruker får ren passord-innlogging.
    usable: function () {
      return this.supported() && window.isSecureContext === true
             && location.hostname !== "" && !hostIsBareIP();
    },
    // Kort, ærlig forklaring på HVORFOR passkey ikke tilbys her.
    whyUnavailable: function () {
      if (!this.supported()) return i18n.t("js.passkey.unsupported");
      if (window.isSecureContext !== true)
        return i18n.t("js.passkey.needs_https");
      if (hostIsBareIP())
        return i18n.t("js.passkey.needs_hostname");
      return i18n.t("js.passkey.unavailable");
    },

    // «Legg til passkey» fra Min side. URL-ene kommer fra url_for i malen —
    // aldri hardkodet her (appen kan ligge bak sti-prefiks, f.eks. /bifrost/).
    register: async function (optsUrl, verifyUrl, name) {
      const o = await postFor(optsUrl);
      o.challenge = b64uToBuf(o.challenge);
      o.user.id = b64uToBuf(o.user.id);
      (o.excludeCredentials || []).forEach((c) => { c.id = b64uToBuf(c.id); });
      const cred = await navigator.credentials.create({ publicKey: o });
      const vurl = verifyUrl + "?name=" + encodeURIComponent(name || "");
      const resp = await fetch(vurl, { method: "POST",
        headers: csrfHeaders(), body: credToJSON(cred) });
      if (!resp.ok) {
        let msg = i18n.t("js.passkey.register_failed");
        try { const je = await resp.json(); msg = je.msg || je.error || msg; } catch (e) { /* tom */ }
        throw new Error(msg);
      }
      return true;
    },

    // Hent innloggings-options ÉN gang (serveren stasher challenge i sesjonen). Deles
    // mellom conditional-autofyll og knappen, så de ikke overskriver hverandres challenge
    // (racet «Client data challenge was not expected challenge» — to options-kall clobbet
    // sesjonens ene challenge-slot).
    loginOptions: async function (optsUrl) {
      const o = await postFor(optsUrl);
      o.challenge = b64uToBuf(o.challenge);
      (o.allowCredentials || []).forEach((c) => { c.id = b64uToBuf(c.id); });
      return o;
    },
    // Kjør get()-seremonien med FERDIGHENTEDE options (samme challenge for begge veier).
    // conditional=true → stille autofill; signal → AbortController (så knappen kan avbryte
    // det ventende conditional-kallet før den kjører sin egen modale get()).
    loginWith: async function (opts, verifyUrl, conditional, signal) {
      const req = { publicKey: opts };
      if (conditional) req.mediation = "conditional";
      if (signal) req.signal = signal;
      const cred = await navigator.credentials.get(req);
      const resp = await fetch(verifyUrl,
        { method: "POST", headers: csrfHeaders(), body: credToJSON(cred) });
      // G-315: sjekk resp.ok FØR json-parsing — en proxy-502 (HTML) ville ellers gitt SyntaxError
      // i stedet for en forståelig «innlogging feilet».
      if (!resp.ok) {
        let msg = i18n.t("js.passkey.login_failed");
        try { const je = await resp.json(); msg = je.msg || je.error || msg; } catch (_e) { /* ikke-JSON svar */ }
        throw new Error(msg);
      }
      const j = await resp.json();
      window.location = j.next || "/";
      return true;
    }
  };
})();
