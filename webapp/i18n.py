# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Display language: language packs and text lookup.

Each language is one JSON file in webapp/lang/, named by its code (no.json, en.json, …):

    {"name": "Norsk", "strings": {"about.title": "Om", "about.stat.interval_value": "hvert {n}. sekund"}}

A new file there becomes a choice automatically, both for the installation ("language" under
Admin → Settings) and for each user (My page); the user's own choice wins. Norwegian ("no") is
the reference pack: a key missing from another pack falls back to Norwegian, and a key missing
everywhere is shown as the key itself, so a page never fails because of a missing text.

Values are filled in with {name} placeholders. Keys ending in "_html" may contain markup from the
pack (the packs are part of the code base); the values filled into them are escaped.

Besides "strings", a pack has "locale" (BCP 47, used by the browser for dates and numbers) and
"format" (thousands and decimal separator, strftime date formats). Counted texts use two keys,
"<key>.one" and "<key>.other", looked up with tn(). Keys starting with "js." are also sent to the
browser (see js_bundle and static/i18n.js)."""
import json
import logging
import os

from markupsafe import Markup

log = logging.getLogger("bifrost.i18n")

DEFAULT = "no"
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lang")


def _load():
    packs = {}
    for fn in sorted(os.listdir(_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data.get("strings"), dict):
                raise ValueError("missing 'strings'")
        except (OSError, ValueError) as e:              # a broken pack is skipped, not fatal
            log.warning("language pack %s ignored: %s", fn, e)
            continue
        packs[fn[:-5]] = data
    return packs


PACKS = _load()


def codes():
    """Available language codes, the default first."""
    return [DEFAULT] + sorted(c for c in PACKS if c != DEFAULT)


def names():
    """Code → the language's own name, e.g. {"no": "Norsk", "en": "English"}."""
    return {c: PACKS[c].get("name", c) for c in codes()}


def installation():
    """The installation's language (Admin → Settings); the default if unknown or unreadable."""
    try:
        import settings                                 # lazy: settings imports this module
        code = settings.get("language")
    except Exception:                                   # noqa: BLE001 - no DB → default language
        code = DEFAULT
    return code if code in PACKS else DEFAULT


def _user_choice():
    """The logged-in user's own language (My page), or None. The break-glass admin has no user
    row and follows the installation."""
    try:
        from flask import session
        uid = session.get("uid")
        if not uid:
            return None
        import auth
        code = auth.get_user_lang(uid)
    except Exception:                                   # noqa: BLE001 - no DB/session → installation
        return None
    return code if code in PACKS else None


def current():
    """The display language: the user's own choice, else the installation's, else Norwegian.
    Worked out once per request (kept on flask.g); outside a request, the installation's."""
    try:
        from flask import g, has_request_context
    except ImportError:                                 # pragma: no cover - Flask is always there
        return installation()
    if not has_request_context():
        return installation()
    if "i18n_lang" not in g:
        g.i18n_lang = _user_choice() or installation()
    return g.i18n_lang


def forget():
    """Drop this request's resolved language, e.g. right after the user or an admin changed it."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            g.pop("i18n_lang", None)
    except ImportError:                                 # pragma: no cover
        pass


class _KeepMissing(dict):
    """format_map helper: an unknown {placeholder} is left as it is instead of failing."""
    def __missing__(self, key):
        return "{" + key + "}"


def _lookup(key, lang):
    text = PACKS.get(lang, {}).get("strings", {}).get(key)
    if text is None:
        text = PACKS.get(DEFAULT, {}).get("strings", {}).get(key, key)
    return text


def t(key, lang=None, /, **values):
    """The text for key in lang (default: the current language), with {placeholders} filled in."""
    lang = lang or current()
    text = _lookup(key, lang)
    if key.endswith("_html"):
        return Markup(text).format_map(_KeepMissing(values)) if values else Markup(text)
    return text.format_map(_KeepMissing(values)) if values else text


def tn(key, n, lang=None, /, **values):
    """Counted text: "<key>.one" when n is 1, otherwise "<key>.other"; {n} is n, formatted."""
    lang = lang or current()
    form = "one" if n == 1 else "other"
    return t(f"{key}.{form}", lang, n=number(n, lang), **values)


def _format(lang):
    return {**PACKS.get(DEFAULT, {}).get("format", {}), **PACKS.get(lang, {}).get("format", {})}


def number(value, lang=None, decimals=0):
    """A number with the language's thousands and decimal separators, e.g. 1 234,5 or 1,234.5."""
    fmt = _format(lang or current())
    text = f"{value:,.{decimals}f}"
    return (text.replace(",", "\x00").replace(".", fmt.get("decimal", "."))
                .replace("\x00", fmt.get("thousands", " ")))


def date(value, lang=None, kind="date"):
    """A date or datetime in the language's format (kind "date" or "datetime"); "" for None."""
    if value is None:
        return ""
    return value.strftime(_format(lang or current()).get(kind, "%Y-%m-%d"))


def locale(lang=None):
    """BCP 47 locale for the browser (Intl), e.g. nb-NO or en-GB."""
    lang = lang or current()
    return PACKS.get(lang, {}).get("locale") or PACKS.get(DEFAULT, {}).get("locale", "nb-NO")


class lazy:
    """A text looked up when it is shown, not when it is created — for texts defined when a module
    is loaded (labels in registries and the like), so they follow the language chosen later."""
    def __init__(self, key, **values):
        self.key, self.values = key, values

    def __str__(self):
        return str(t(self.key, **self.values))

    def __format__(self, spec):
        return format(str(self), spec)

    def __bool__(self):
        return bool(str(self))

    def __eq__(self, other):
        return str(self) == str(other)

    def __hash__(self):
        return hash(self.key)


_JS_CACHE = {}


def js_bundle(lang=None):
    """What the browser gets (static/i18n.js): language, locale, format and every "js." text, with
    Norwegian filling in keys the chosen pack lacks."""
    lang = lang or current()
    if lang not in _JS_CACHE:
        strings = {k: v for k, v in PACKS.get(DEFAULT, {}).get("strings", {}).items() if k.startswith("js.")}
        strings.update({k: v for k, v in PACKS.get(lang, {}).get("strings", {}).items() if k.startswith("js.")})
        _JS_CACHE[lang] = {"lang": lang, "locale": locale(lang), "format": _format(lang), "strings": strings}
    return _JS_CACHE[lang]
