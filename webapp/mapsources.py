# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register of tile sources for the geographic map background.

The default source is set by `MAP_SOURCE` in .env (Kartverket greyscale, Norway). An own source is set
by `MAP_SOURCE_URL` (+ `MAP_SOURCE_ATTRIBUTION`). The global sources (OpenTopoMap, CARTO, OSM, Google)
cover networks outside Norway.

Two background modes:
  * snapshot -> the server stitches the tiles into one image stored in the database (works offline).
                Needs `snapshot_ok=True`: the source's terms allow storing its tiles on a server.
  * live     -> the browser fetches the tiles from the source when the map is shown.
                Needs `live_ok=True`.

`snapshot_ok=False` (CARTO, OSM, Google): the terms forbid storing or caching the tiles on a server,
so these sources can only be used live.

Some sources need an API key: `key` names the setting that holds it, and without the key the source
cannot be used. Google gets its tiles through a session token made by the server (`session`); for the
others the key is added to the tile URL as the `key_param` query parameter. A tile key always reaches
the browser, so it should be restricted in the provider's console."""
import os
from urllib.parse import quote

import i18n
import settings           # the API keys are read from app_setting (database) with .env as fallback


def google_key():
    """G-133: gjeldende Google-nøkkel (DB hvis satt, ellers .env). Tom = ikke konfigurert."""
    return settings.get("google_maps_api_key")


def carto_key():
    """The CARTO API key (database if set, otherwise .env). Empty = not configured."""
    return settings.get("carto_api_key")


# name -> {label, url (XYZ template with {z}/{x}/{y}), attribution, max_zoom, snapshot_ok, live_ok,
#          key (setting holding the API key), key_param (query parameter for the key), session}
SOURCES = {
    "kartverket": {
        "label": i18n.lazy("msg.mapsources.kartverket"),
        "url": "https://cache.kartverket.no/v1/wmts/1.0.0/topograatone/default/webmercator/{z}/{y}/{x}.png",
        "attribution": "© Kartverket",
        "max_zoom": 18, "snapshot_ok": True, "live_ok": True,
    },
    "kartverket_farge": {
        "label": i18n.lazy("msg.mapsources.kartverket_colour"),
        "url": "https://cache.kartverket.no/v1/wmts/1.0.0/topo/default/webmercator/{z}/{y}/{x}.png",
        "attribution": "© Kartverket",
        "max_zoom": 18, "snapshot_ok": True, "live_ok": True,
    },
    "kartverket_toporaster": {
        "label": i18n.lazy("msg.mapsources.kartverket_toporaster"),
        "url": "https://cache.kartverket.no/v1/wmts/1.0.0/toporaster/default/webmercator/{z}/{y}/{x}.png",
        "attribution": "© Kartverket",
        "max_zoom": 18, "snapshot_ok": True, "live_ok": True,
    },
    "kartverket_sjokart": {
        "label": i18n.lazy("msg.mapsources.kartverket_sjokart"),
        "url": "https://cache.kartverket.no/v1/wmts/1.0.0/sjokartraster/default/webmercator/{z}/{y}/{x}.png",
        "attribution": "© Kartverket",
        "max_zoom": 18, "snapshot_ok": True, "live_ok": True,
    },
    "opentopomap": {
        "label": i18n.lazy("msg.mapsources.opentopomap"),
        "url": "https://tile.opentopomap.org/{z}/{x}/{y}.png",
        "attribution": "Map data: © OpenStreetMap contributors, SRTM | Map style: © OpenTopoMap (CC-BY-SA)",
        "max_zoom": 17, "snapshot_ok": True, "live_ok": True,
    },
    # CARTO's basemap terms need an API key on every tile URL and forbid caching the tiles on a
    # server, so CARTO is live only.
    "carto": {
        "label": i18n.lazy("msg.mapsources.carto"),
        "url": "https://basemaps.cartocdn.com/rastertiles/light_all/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors, © CARTO",
        "max_zoom": 20, "snapshot_ok": False, "live_ok": True, "key": "carto_api_key", "key_param": "key",
    },
    "carto_voyager": {
        "label": i18n.lazy("msg.mapsources.carto_voyager"),
        "url": "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors, © CARTO",
        "max_zoom": 20, "snapshot_ok": False, "live_ok": True, "key": "carto_api_key", "key_param": "key",
    },
    "osm": {
        "label": i18n.lazy("msg.mapsources.osm"),
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "attribution": "© OpenStreetMap contributors",
        "max_zoom": 19, "snapshot_ok": False, "live_ok": True,
    },
    "google": {
        "label": i18n.lazy("msg.mapsources.google"),
        # G-129: ToS-riktig Google = Maps Tiles API. Klienten henter med en session-token
        # som serveren lager (se maps.py google_tile_session). URL-malen fylles klient-side
        # med {session} + {key}. Kan ALDRI snapshottes (Googles vilkår).
        "url": "https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session={session}&key={key}",
        "attribution": "© Google",
        "max_zoom": 20, "snapshot_ok": False, "live_ok": True, "key": "google_maps_api_key", "session": True,
    },
}

DEFAULT = os.environ.get("MAP_SOURCE", "kartverket")
CUSTOM_URL = os.environ.get("MAP_SOURCE_URL", "").strip()
CUSTOM_ATTR = os.environ.get("MAP_SOURCE_ATTRIBUTION", "© egen kilde").strip()

# Norge-bbox (fastlandet) — fornuftig default-utsnitt.
NORWAY_BBOX = {"west": 4.0, "south": 57.5, "east": 31.5, "north": 71.5}

# User-Agent toward tile servers; their usage policies require one, OSM's in particular.
# app.py puts the app version in at start-up, so the version is defined in one place only.
USER_AGENT = "Bifrost-netmon (self-hosted network weathermap)"


def set_version(version):
    """Put the app version into USER_AGENT. Called once by app.py at start-up."""
    global USER_AGENT
    USER_AGENT = f"Bifrost-netmon/{version} (self-hosted network weathermap)"


def google_available():
    """G-129/G-133: Google er kun klar når API-nøkkelen er satt (DB eller .env)."""
    return bool(google_key())


def has_key(src):
    """True when the source needs no API key, or its key is set."""
    return not src.get("key") or bool(settings.get(src["key"]))


def tile_url(src, key=None):
    """The tile URL template for the browser or the stitcher. For a source with `key_param` the API
    key (`key`, or the configured one) is added to the URL; None when that key is not set. A session
    source (Google) keeps its template, which the browser fills in after getting a session token."""
    url = src.get("url")
    if not url or not src.get("key_param"):
        return url
    key = key if key is not None else settings.get(src["key"])
    if not key:
        return None
    return f"{url}{'&' if '?' in url else '?'}{src['key_param']}={quote(key, safe='')}"


def validate_google_key(key):
    """G-133: bekreft at en Google-nøkkel faktisk virker via et ekte kartoppslag
    (Maps Tiles API createSession). Returnerer (ok: bool, melding: str)."""
    import requests
    try:
        r = requests.post("https://tile.googleapis.com/v1/createSession",
                          params={"key": key},
                          json={"mapType": "roadmap", "language": "no-NO", "region": "NO"},
                          headers={"User-Agent": USER_AGENT}, timeout=8)
        if r.status_code == 200 and r.json().get("session"):
            return True, i18n.t("msg.mapsources.google_key_ok")
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")
        except Exception:                              # noqa: BLE001
            pass
        return False, i18n.t("msg.mapsources.google_key_rejected", status=r.status_code,
                             detail=(": " + detail if detail else ""))
    except Exception as e:                             # noqa: BLE001
        return False, i18n.t("msg.mapsources.google_unreachable", error=e)


def validate_carto_key(key, src=None):
    """Check a CARTO key with a tile request. Returns (ok, message). CARTO answers a missing or invalid
    key with status 200 and its «API KEY REQUIRED» tile, so the tile for the key is compared with the
    tile CARTO gives without a key: the same image, another status than 200, an answer that is not an
    image, or no answer means the key is not accepted."""
    import requests
    src = src or SOURCES["carto"]
    tile = {"z": 4, "x": 8, "y": 4}

    def fetch(url):
        return requests.get(url.format(**tile), headers={"User-Agent": USER_AGENT}, timeout=8)

    try:
        r = fetch(tile_url(src, key=key))
        if r.status_code != 200 or not r.headers.get("Content-Type", "").startswith("image/"):
            return False, i18n.t("msg.mapsources.carto_key_rejected", status=r.status_code)
        without_key = fetch(src["url"])
    except Exception as e:                             # noqa: BLE001
        return False, i18n.t("msg.mapsources.carto_unreachable", error=e)
    if without_key.status_code == 200 and without_key.content == r.content:
        return False, i18n.t("msg.mapsources.carto_key_not_valid")
    return True, i18n.t("msg.mapsources.carto_key_ok")


def get_source(name):
    """Slå opp en kilde. 'custom' bruker MAP_SOURCE_URL fra .env. None om ukjent."""
    if name == "custom" and CUSTOM_URL:
        return {"label": i18n.t("msg.mapsources.custom"), "url": CUSTOM_URL, "attribution": CUSTOM_ATTR,
                "max_zoom": 20, "snapshot_ok": True, "live_ok": True}
    return SOURCES.get(name)


def default_source_key():
    """Gyldig default-nøkkel (faller tilbake til kartverket om .env peker feil)."""
    if DEFAULT in SOURCES or (DEFAULT == "custom" and CUSTOM_URL):
        return DEFAULT
    return "kartverket"


def listing():
    """Sources for the editor's drop-down, the default first. Sources that need an API key are always
    listed; `key_ok` tells the editor whether the key is set. 'custom' is included when configured."""
    items = [{"key": k, "label": str(s["label"]), "snapshot_ok": s["snapshot_ok"],
              "live_ok": s.get("live_ok", False), "needs_key": bool(s.get("key")), "key_ok": has_key(s)}
             for k, s in SOURCES.items()]
    if CUSTOM_URL:
        items.append({"key": "custom", "label": i18n.t("msg.mapsources.custom_env"), "snapshot_ok": True,
                      "live_ok": True, "needs_key": False, "key_ok": True})
    d = default_source_key()
    items.sort(key=lambda i: (i["key"] != d, i["label"]))
    return items


def client_map():
    """URL, attribution and max zoom per source for the browser (Leaflet preview and live tiles).
    Leaflet fills {z}/{x}/{y} in any order (Kartverket uses {z}/{y}/{x}). Google is left out (it has
    its own session flow in maps.py). A source whose API key is not set gets url None."""
    out = {k: {"url": tile_url(v), "attribution": v["attribution"],
               "max_zoom": v["max_zoom"], "snapshot_ok": v["snapshot_ok"],
               "live_ok": v.get("live_ok", False), "key_ok": has_key(v), "label": str(v["label"])}
           for k, v in SOURCES.items() if not v.get("session")}
    if CUSTOM_URL:
        out["custom"] = {"url": CUSTOM_URL, "attribution": CUSTOM_ATTR,
                         "max_zoom": 20, "snapshot_ok": True, "live_ok": True, "key_ok": True,
                         "label": i18n.t("msg.mapsources.custom")}
    return out
