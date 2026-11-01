# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.2
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G-135: branding stored in the database (not .env or an external CSS file).

Security: an uploaded `.css` file is never served. It is parsed, the theme tokens (`--name: value`)
are picked out, and each value is checked against the type its token has (a colour must be a valid
CSS colour, a font a font-family list, a length a number with a unit, and so on). A token the app
does not use, or a value of the wrong type, is not kept. The blocklist below stays as a second check.
The app then emits its own `:root{…}` from the kept tokens, and the same check runs again there.

The logo is processed in-house (Pillow: validated and scaled down) and stored as a data URI."""
import io
import re
import time
import base64
import logging

import auth
import i18n

log = logging.getLogger("bifrost.branding")

# Kort TTL-cache (samme mønster som settings._cache): tema/logo leses på HVER sidevisning
# via context processoren, men endres nesten aldri — uten cache ble det to ekstra
# DB-rundturer per request, også på login-sider (review 2026-08-09). Invalidieres
# eksplisitt av save_*/clear_* under, så en endring synes umiddelbart i samme prosess.
_TTL = 5.0
_cache = {}          # key -> (verdi, utløper_monotonic)


def _cached(key, loader):
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[1] > now:
        return hit[0]
    val = loader()
    _cache[key] = (val, now + _TTL)
    return val

# Forbudte konstruksjoner i en token-VERDI (case-insensitivt). Blokkerer ekstern-lasting,
# script og utbrytning av deklarasjonen.
_BAD_VALUE = re.compile(
    r'(url\s*\(|image-set\s*\(|cross-fade\s*\(|element\s*\(|image\s*\('
    r'|@import|expression|javascript:|</|[<>{};\\]|/\*|\*/)', re.I)
# ^ blokkerer ALLE CSS-bilde-/ekstern-ressurs-funksjoner (url(), image-set(), cross-fade(),
# element(), image()), ikke bare url() — de kan alle laste eksterne ressurser fra en token-verdi.

# Every theme token the app uses, with the type its value must have. The test in test_branding checks
# that this covers every token in app.css :root, every cvar() in the scripts, every var() in the
# stylesheet and templates, and the example theme.
TOKEN_TYPES = dict.fromkeys((
    "--accent", "--bad-ink", "--bg", "--bl-100", "--bl-200", "--bl-300", "--bl-500", "--bl-600",
    "--bl-800", "--bl-900", "--border", "--cap-crit", "--cap-high", "--cap-low", "--cap-mid", "--danger",
    "--down", "--faint", "--flow", "--gr-100", "--gr-300", "--gr-500", "--gr-700", "--grid", "--ink",
    "--ink-strong", "--line", "--lsp", "--muted", "--neu-100", "--neu-200", "--nf", "--nf-ink", "--ok-ink",
    "--on-bg", "--primary", "--primary-600", "--role-cdn-bg", "--role-cdn-ink", "--role-customer-ia",
    "--role-nni", "--role-peering-bg", "--role-pni-bg", "--role-pni-ink", "--sort", "--surface",
    "--surface-2", "--warning"), "color")
TOKEN_TYPES.update({"--grad": "gradient", "--ink-rgb": "rgb", "--primary-rgb": "rgb", "--danger-rgb": "rgb",
                    "--font": "font", "--font-display": "font", "--r": "length"})

# CSS named colours (CSS Color Module Level 4) plus transparent and currentcolor.
_NAMED_COLORS = frozenset("""
aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown
burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan
darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid
darkred darksalmon darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet
deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro
ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki
lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow
lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray
lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon mediumaquamarine
mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen mediumturquoise
mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive olivedrab
orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru
pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown
seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan
teal thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen transparent currentcolor
""".split())
_NUM = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
_SEP = r"(?:\s*,\s*|\s+)"
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")
_RGB_FN = re.compile(rf"rgba?\(\s*{_NUM}%?{_SEP}{_NUM}%?{_SEP}{_NUM}%?(?:\s*[,/]\s*{_NUM}%?)?\s*\)", re.I)
_HSL_FN = re.compile(rf"hsla?\(\s*{_NUM}(?:deg|turn|rad|grad)?{_SEP}{_NUM}%{_SEP}{_NUM}%(?:\s*[,/]\s*{_NUM}%?)?\s*\)",
                     re.I)
_VAR = re.compile(r"var\(\s*(--[a-z0-9-]{1,40})\s*\)")
_FAMILY = re.compile(r'"[A-Za-z0-9 _-]{1,60}"|\'[A-Za-z0-9 _-]{1,60}\'|-?[A-Za-z][A-Za-z0-9_-]*(?: [A-Za-z0-9_-]+)*')
_LENGTH = re.compile(rf"0|{_NUM}(?:px|rem|em|%|vh|vw|ch|ex)")
_ANGLE = re.compile(rf"{_NUM}(?:deg|turn|rad|grad)")
_SIDE = re.compile(r"to (?:left|right|top|bottom)(?: (?:left|right|top|bottom))?")
_STOP_POS = re.compile(rf"{_NUM}%|0|{_NUM}px")


def _is_color(v):
    if _HEX.fullmatch(v) or _RGB_FN.fullmatch(v) or _HSL_FN.fullmatch(v) or v.lower() in _NAMED_COLORS:
        return True
    m = _VAR.fullmatch(v)                     # a reference to another colour token
    return bool(m) and TOKEN_TYPES.get(m.group(1)) == "color"


def _is_font(v):
    parts = v.split(",")
    return len(parts) <= 10 and all(_FAMILY.fullmatch(p.strip()) for p in parts)


def _is_rgb(v):
    parts = [p.strip() for p in v.split(",")]
    return len(parts) == 3 and all(p.isdigit() and int(p) <= 255 for p in parts)


def _split_args(text):
    """Split on commas outside parentheses; None when the parentheses do not balance."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    return None if depth else out + [cur.strip()]


def _is_gradient(v):
    m = re.fullmatch(r"linear-gradient\((.*)\)", v, re.I | re.S)
    args = _split_args(m.group(1)) if m else None
    if not args:
        return False
    if _ANGLE.fullmatch(args[0]) or _SIDE.fullmatch(args[0]):
        args = args[1:]

    def stop(a):
        if _is_color(a):
            return True
        head, _, tail = a.rpartition(" ")
        return bool(head) and bool(_STOP_POS.fullmatch(tail)) and _is_color(head.strip())
    return 2 <= len(args) <= 10 and all(stop(a) for a in args)


_CHECKS = {"color": _is_color, "font": _is_font, "length": lambda v: bool(_LENGTH.fullmatch(v)),
           "rgb": _is_rgb, "gradient": _is_gradient}


def valid_token(name, value):
    """True when `name` is a theme token the app uses and `value` has its type (and passes the
    blocklist as well)."""
    kind = TOKEN_TYPES.get(name) if isinstance(name, str) else None
    if not kind or not isinstance(value, str):
        return False
    value = value.strip()
    if not value or len(value) > 200 or _BAD_VALUE.search(value):
        return False
    return _CHECKS[kind](value)


_THEME_KEY = "theme_tokens"        # app_setting: JSON {token: verdi}
_LOGO_KEY = "brand_logo_data"      # app_setting: data-URI (PNG)


# Kun avsluttede kommentarer: en uavsluttet «/*» gjør bare sin egen deklarasjon ugyldig (navnet
# matcher ikke), i stedet for å sluke resten av teksten — inkludert `}` som lukker :root.
_CSS_COMMENT = re.compile(r'/\*.*?\*/', re.S)


def parse_theme_css(text, max_tokens=150):
    """Pick the `:root` tokens out of a CSS text. Returns {name: value}. A token the app does not use
    or a value of the wrong type is skipped (ignored_theme_tokens names them), so the result is
    always safe to emit.
    Kommentarer fjernes først, ellers ville en kommentar foran en deklarasjon (eller et kolon i
    en kommentar) gjort deklarasjonen ugyldig og tokenet droppet."""
    tokens = {}
    text = _CSS_COMMENT.sub(" ", text or "")
    for block in re.findall(r':root\s*\{([^}]*)\}', text, re.I):
        for decl in block.split(';'):
            name, sep, val = decl.partition(':')
            if not sep:
                continue
            name, val = name.strip(), val.strip()
            if not valid_token(name, val):
                continue
            tokens[name] = val
            if len(tokens) >= max_tokens:
                return tokens
    return tokens


def theme_tokens():
    """Lagrede tema-tokens (dict) eller {}."""
    import json
    try:
        raw = auth.get_setting(_THEME_KEY)
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):   # korrupt lagret JSON → tom (forventet)
        return {}
    except Exception:                            # noqa: BLE001 — G-325: annen feil (DB o.l.) LOGGES
        log.exception("theme_tokens: kunne ikke lese tema (ikke JSON-feil)")
        return {}


def theme_css():
    """Build a safe `:root{…}` string from the stored tokens (empty when none). Returned as Markup so
    quotes (font names) are not HTML-escaped inside <style>. Safe because every token is checked
    again here with the same type check as on upload: whatever is in the database, no value can hold
    < > { } ; or leave <style>. A stored token that fails the check is left out and logged. TTL-cached."""
    from markupsafe import Markup
    def _build():
        stored = theme_tokens() or {}
        toks = {k: v.strip() for k, v in stored.items() if valid_token(k, v)}
        dropped = sorted(str(k) for k in stored if k not in toks)
        if dropped:                                    # stored before the type check: not emitted
            from db import warn_throttled
            warn_throttled(log, "theme_dropped", "theme tokens not emitted (unknown or wrong type): %s",
                           ", ".join(dropped[:20]))
        if not toks:
            return Markup("")
        return Markup(":root{" + "".join(f"{k}:{v};" for k, v in toks.items()) + "}")
    return _cached("theme_css", _build)


def ignored_theme_tokens(text, tokens):
    """Navn på `--token`-deklarasjoner i `:root` som parse_theme_css IKKE beholdt (ugyldig navn,
    utrygg eller for lang verdi) — så opplasteren ser hva som ble droppet, ikke bare et antall."""
    text = _CSS_COMMENT.sub(" ", text or "")
    seen = []
    for block in re.findall(r':root\s*\{([^}]*)\}', text, re.I):
        for m in re.finditer(r'(--[A-Za-z0-9_-]+)\s*:', block):
            if m.group(1) not in tokens and m.group(1) not in seen:
                seen.append(m.group(1))
    return seen


def save_theme_from_css(text):
    """Parse + lagre tema-tokens fra en opplastet CSS. Returnerer (antall lagrede, ignorerte navn)."""
    import json
    toks = parse_theme_css(text)
    auth.set_setting(_THEME_KEY, json.dumps(toks))
    _cache.pop("theme_css", None)
    return len(toks), ignored_theme_tokens(text, toks)


def clear_theme():
    auth.set_setting(_THEME_KEY, "")
    _cache.pop("theme_css", None)


def logo_data_uri():
    """Lagret logo som data-URI, eller None. TTL-cachet."""
    return _cached("logo", lambda: auth.get_setting(_LOGO_KEY) or None)


def save_logo(filestream):
    """In-house: valider + nedskaler logo (Pillow) og lagre som PNG data-URI. (ok, melding)."""
    from PIL import Image
    try:
        img = Image.open(filestream, formats=("PNG", "JPEG", "WEBP"))   # kun disse dekoderne prøves; leser kun headeren
    except Exception:                          # noqa: BLE001 - tydelig format-feil
        return False, i18n.t("msg.branding.bad_format")
    # Håndhev en FORMAT-allowlist (ikke bare «kan Pillow åpne den») — begrenser CVE-flaten til de
    # tre formatene vi faktisk støtter, i stedet for alt Pillow kan dekode (TIFF/GIF/ICO/…).
    if (img.format or "").upper() not in ("PNG", "JPEG", "WEBP"):
        return False, i18n.t("msg.branding.unsupported_format")
    # R-039: sjekk piksel-taket FØR img.load() dekoder hele bildet til minne — en
    # preparert «liten» fil med enorme dimensjoner kunne ellers OOM-e workeren
    # (Pillows egen bombe-grense slår først inn ved ~178 Mpx).
    if img.width * img.height > 40_000_000:
        return False, i18n.t("msg.branding.too_large")
    try:
        img.load()
    except Exception:                          # noqa: BLE001 - korrupt bildedata
        return False, i18n.t("msg.branding.bad_format")
    img = img.convert("RGBA")
    if img.width > 400 or img.height > 400:
        img.thumbnail((400, 400))
    out = io.BytesIO(); img.save(out, format="PNG", optimize=True)
    uri = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    auth.set_setting(_LOGO_KEY, uri)
    _cache.pop("logo", None)
    return True, i18n.t("msg.branding.logo_saved", w=img.width, h=img.height)


def clear_logo():
    auth.set_setting(_LOGO_KEY, "")
    _cache.pop("logo", None)
