// Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
// SPDX-License-Identifier: AGPL-3.0-or-later
/* Display language in the browser. The server puts the chosen language pack's "js." texts, locale
   and number format in window.BIFROST_I18N (templates/_i18n.html, from i18n.js_bundle); this file
   gives scripts window.i18n:
     i18n.t(key, {name: value})        text with {placeholders} filled in; unknown key → the key
     i18n.tn(key, n, {…})              key + ".one" when n is 1, otherwise key + ".other"
     i18n.number(1234.5, 1)            "1 234,5" / "1,234.5" as the language writes it
     i18n.months() / i18n.weekdays()   names from the browser's Intl for i18n.locale
   Norwegian fills in any key the chosen pack lacks (done on the server). */
(function () {
  var b = window.BIFROST_I18N || {};
  var strings = b.strings || {};
  var fmt = b.format || {thousands: " ", decimal: ","};
  var loc = b.locale || "nb-NO";

  function fill(text, values) {
    if (!values) return text;
    return text.replace(/\{(\w+)\}/g, function (m, k) {
      return Object.prototype.hasOwnProperty.call(values, k) ? String(values[k]) : m;
    });
  }

  function t(key, values) {
    var s = strings[key];
    return fill(s == null ? key : s, values);
  }

  function number(value, decimals) {
    var parts = Number(value).toFixed(decimals || 0).split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, "\u0000");
    return parts.join(fmt.decimal || ".").replace(/\u0000/g, fmt.thousands || " ");
  }

  function tn(key, n, values) {
    var v = Object.assign({}, values || {}, {n: number(n)});
    return t(key + (n === 1 ? ".one" : ".other"), v);
  }

  function names(opts, count, make) {
    var f = new Intl.DateTimeFormat(loc, opts), out = [];
    for (var i = 0; i < count; i++) out.push(f.format(make(i)));
    return out;
  }

  window.i18n = {
    lang: b.lang || "no",
    locale: loc,
    t: t,
    tn: tn,
    number: number,
    // January … December and Sunday … Saturday, in the chosen language
    months: function (style) {
      return names({month: style || "long", timeZone: "UTC"}, 12, function (i) { return new Date(Date.UTC(2021, i, 1)); });
    },
    weekdays: function (style) {
      return names({weekday: style || "long", timeZone: "UTC"}, 7, function (i) { return new Date(Date.UTC(2021, 0, 3 + i)); });
    }
  };
})();
