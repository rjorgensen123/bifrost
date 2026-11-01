# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.8
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G-091: Web Mercator-projeksjon + server-side snapshot-stitching for geografisk
bakgrunnskart.

Henter XYZ-fliser fra en kartkilde og syr dem til ETT bilde for et valgt
lat/long-utsnitt + zoom. Lagrer en minimal geo-referanse (zoom + global piksel-
origo for utsnittet + dimensjon) som lar lat/long projiseres til piksel senere
(brukt av G-092 NetBox-GPS). Web Mercator (EPSG:3857), 256-px fliser.

Den rene matematikken (deg2tile/tile2deg/latlon_to_px/px_to_latlon/geo_to_canvas)
har ingen avhengigheter. `stitch()` importerer Pillow + requests lokalt, så modulen
kan lastes selv om de mangler — kun snapshot-bygging trenger dem.
"""
import io
import math
import logging

import i18n

log = logging.getLogger("bifrost.geo")

TILE = 256                       # standard flis-størrelse (px)
MAX_TILES = 400                  # default-vern (kalleren sender Innstillinger → max_bg_tiles)
LAT_CLAMP = 85.0511287798066     # Web Mercator gyldig lat-grense


def deg2tile(lat, lon, z):
    """lat/long (grader) -> flis-koordinat (float) ved zoom z (slippy-map)."""
    lat = max(-LAT_CLAMP, min(LAT_CLAMP, lat))
    n = 2.0 ** z
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def tile2deg(x, y, z):
    """flis-koordinat (float) -> lat/long (grader) ved zoom z (invers av deg2tile)."""
    n = 2.0 ** z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def latlon_to_px(lat, lon, z):
    """lat/long -> global piksel (Web Mercator, TILE-px fliser) ved zoom z."""
    x, y = deg2tile(lat, lon, z)
    return x * TILE, y * TILE


def px_to_latlon(px, py, z):
    """global piksel -> lat/long ved zoom z (invers av latlon_to_px)."""
    return tile2deg(px / TILE, py / TILE, z)


def geo_to_canvas(lat, lon, geo):
    """Projiser lat/long til piksel i et lagret snapshot.
    `geo` = dict med zoom/origin_px/origin_py/w/h (som lagret på composer.map).
    Returnerer (x, y) i bildets piksler, eller None om punktet er utenfor utsnittet."""
    if not geo or geo.get("zoom") is None:
        return None
    px, py = latlon_to_px(lat, lon, geo["zoom"])
    x = px - geo["origin_px"]
    y = py - geo["origin_py"]
    if x < 0 or y < 0 or x > geo["w"] or y > geo["h"]:
        return None
    return x, y


def bg_cover_transform(geo, canvas_w, canvas_h):
    """G-235: cover-transformen som bakgrunnen FAKTISK vises med (bg_live.js / snapshot
    `preserveAspectRatio=slice`): bakgrunnsbildet (bg_w×bg_h) skaleres uniformt så det DEKKER
    lerretet (canvas_w×canvas_h) og sentreres. Returnerer (scale, offx, offy)."""
    bw = geo["w"] or canvas_w
    bh = geo["h"] or canvas_h
    scale = max(canvas_w / bw, canvas_h / bh)
    offx = (canvas_w - bw * scale) / 2.0
    offy = (canvas_h - bh * scale) / 2.0
    return scale, offx, offy


def geo_to_canvas_cover(lat, lon, geo, canvas_w, canvas_h):
    """G-235: lat/lon → LERRET-piksel gjennom cover-transformen, så plasseringen treffer der
    bakgrunnen faktisk tegner det punktet (i motsetning til `geo_to_canvas` som gir rå bilde-
    piksler i bg_w×bg_h-rommet). Klamper aldri — returnerer (x, y) i lerret-rommet, og en
    `on_map`-flagg om punktet er innenfor det viste utsnittet.
    Returnerer (x, y, on_map) eller None hvis geo mangler."""
    if not geo or geo.get("zoom") is None:
        return None
    px, py = latlon_to_px(lat, lon, geo["zoom"])
    gx = px - geo["origin_px"]
    gy = py - geo["origin_py"]
    on_map = (0 <= gx <= geo["w"] and 0 <= gy <= geo["h"])
    scale, offx, offy = bg_cover_transform(geo, canvas_w, canvas_h)
    return offx + gx * scale, offy + gy * scale, on_map


def relative_layout(points, width, height, margin=60):
    """G-201 (1.5.0): RELATIV geografisk plassering UTEN bakgrunnskart. `points` = {key: (lat, lon)}.
    Projiserer alle punktene (Web Mercator, fast zoom — kun relative forhold teller), sentrerer
    bbox-senteret på lerret-senter (0,0-prinsippet) og skalerer UNIFORMT til å passe innenfor margin.
    Bevarer nord/sør/øst/vest mellom enhetene (Mercator-y øker sørover = riktig skjerm-y, ingen flip).
    Returnerer {key: (x, y)} (heltalls-px). n==0 → {}; n==1 → senter."""
    pts = {k: v for k, v in (points or {}).items()
           if v and v[0] is not None and v[1] is not None}
    cx0, cy0 = width / 2.0, height / 2.0
    if not pts:
        return {}
    if len(pts) == 1:
        k = next(iter(pts))
        return {k: (int(round(cx0)), int(round(cy0)))}
    Z = 12
    proj = {k: latlon_to_px(lat, lon, Z) for k, (lat, lon) in pts.items()}
    xs = [p[0] for p in proj.values()]
    ys = [p[1] for p in proj.values()]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    bw, bh = max(1e-9, maxx - minx), max(1e-9, maxy - miny)
    s = min((width - 2 * margin) / bw, (height - 2 * margin) / bh)
    bcx, bcy = (minx + maxx) / 2.0, (miny + maxy) / 2.0
    out = {}
    for k, (px, py) in proj.items():
        x = max(margin, min(width - margin, cx0 + (px - bcx) * s))
        y = max(margin, min(height - margin, cy0 + (py - bcy) * s))
        out[k] = (int(round(x)), int(round(y)))
    return out


STITCH_BUDGET_S = 90             # total frist for én snapshot-henting (under gunicorns 120 s)


def stitch(url_template, west, south, east, north, zoom, headers=None, timeout=8, budget_s=STITCH_BUDGET_S,
           max_tiles=MAX_TILES):
    """Hent XYZ-fliser som dekker bbox (lat/long) ved `zoom` og sy dem til ett PNG.

    `url_template` har navngitte felt {z}/{x}/{y} (rekkefølgen i malen avgjør —
    Kartverket bruker f.eks. .../{z}/{y}/{x}.png). Returnerer (png_bytes, geo) der
    geo = {zoom, origin_px, origin_py, w, h}. Kaster ved nettverks-/flisfeil eller
    for stort utsnitt — kalleren oversetter det til en vennlig «ingen tilgang til
    kilden»-melding (graceful fallback). Alt-eller-ingenting: hele hentingen har en total frist
    (`budget_s`); nås den, avbrytes det uten at noe returneres (eksisterende bakgrunn beholdes)."""
    import time
    import requests
    from PIL import Image

    x0f, y0f = deg2tile(north, west, zoom)    # topp-venstre (nord = mindre y)
    x1f, y1f = deg2tile(south, east, zoom)    # bunn-høyre
    xmin, xmax = math.floor(x0f), math.floor(x1f)
    ymin, ymax = math.floor(y0f), math.floor(y1f)
    nx, ny = xmax - xmin + 1, ymax - ymin + 1
    if nx <= 0 or ny <= 0:
        raise ValueError(i18n.t("msg.geo.bad_extent"))
    if nx * ny > max_tiles:
        raise ValueError(i18n.t("msg.geo.too_many_tiles", tiles=nx * ny, max_tiles=max_tiles))
    nmax = 2 ** zoom
    deadline = time.monotonic() + budget_s
    mosaic = Image.new("RGB", (nx * TILE, ny * TILE), (235, 235, 235))
    with requests.Session() as sess:   # G-255: lukk sessionen (frigjør socket-pool) når mosaikken er hentet
        for tx in range(xmin, xmax + 1):
            for ty in range(ymin, ymax + 1):
                url = url_template.format(z=zoom, x=tx % nmax, y=ty % nmax)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(i18n.t("msg.geo.too_slow", budget=budget_s, tiles=nx * ny))
                try:
                    r = sess.get(url, headers=headers or {}, timeout=min(timeout, left))
                except requests.Timeout:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(i18n.t("msg.geo.too_slow", budget=budget_s, tiles=nx * ny))
                    raise
                r.raise_for_status()
                _tile = Image.open(io.BytesIO(r.content), formats=("PNG", "JPEG", "WEBP"))   # kun header, kun disse dekoderne
                # Bombe-vern: en ondsinnet/kompromittert flis-server kan sende et bilde med enorme
                # dimensjoner som OOM-er workeren ved dekoding. Fliser er per def. små (TILE-px);
                # avvis alt urimelig stort FØR .convert() (som dekoder hele bildet).
                if (_tile.width or 0) * (_tile.height or 0) > 40_000_000:
                    raise ValueError(i18n.t("msg.geo.tile_too_big"))
                # Samme format-allowlist som opplastingsveien (maps.upload_background/branding):
                # begrens dekoder-flaten til formatene flis-servere legitimt serverer, i stedet
                # for alt Pillow kan dekode (review 2026-08-09, defense-in-depth mot flis-server).
                if (_tile.format or "").upper() not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError(i18n.t("msg.geo.tile_bad_format",
                                            fmt=_tile.format or i18n.t("msg.geo.unknown_format")))
                tile_img = _tile.convert("RGB")
                mosaic.paste(tile_img, ((tx - xmin) * TILE, (ty - ymin) * TILE))
    left = round((x0f - xmin) * TILE)
    top = round((y0f - ymin) * TILE)
    right = round((x1f - xmin) * TILE)
    bottom = round((y1f - ymin) * TILE)
    img = mosaic.crop((left, top, right, bottom))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    geo = {"zoom": int(zoom), "origin_px": x0f * TILE, "origin_py": y0f * TILE,
           "w": img.width, "h": img.height}
    log.info("snapshot stitched: zoom %d, %dx%d fliser -> %dx%d px",
             zoom, nx, ny, img.width, img.height)
    return out.getvalue(), geo
