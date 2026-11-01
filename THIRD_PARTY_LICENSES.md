# Third-party licenses

Bifrost's own code is licensed under AGPL-3.0-or-later and under a commercial license — see
[License](README.md#license) in the README. This file lists everything Bifrost builds on that is not
its own, and the license each part is under. All of these can be used together with
AGPL-3.0-or-later and with the commercial license.

The license given for each part is the one the part itself declares: the package metadata for the
Python packages, and the upstream project's license file for the files in this repository.

## In this repository

The third-party files that ship with Bifrost are under `webapp/static/vendor/`, with the license text
next to them. Nothing else in the repository is third-party.

| Component | Version | License | Files | License text | Source |
|---|---|---|---|---|---|
| htmx | 2.0.4 | 0BSD | `htmx.min.js` | `htmx.LICENSE` | https://github.com/bigskysoftware/htmx |
| uPlot | 1.6.31 | MIT | `uPlot.iife.min.js`, `uPlot.min.css` | `uPlot.LICENSE` | https://github.com/leeoniya/uPlot |
| Leaflet | 1.9.4 | BSD-2-Clause | `leaflet/` (script, stylesheet and images) | `leaflet/LICENSE` | https://github.com/Leaflet/Leaflet |
| Inter (font) | 4.001 | OFL-1.1 | `fonts/inter-latin-*.woff2` | `fonts/LICENSE.txt` | https://github.com/rsms/inter |

## Python packages

These are not in the repository. pip installs them from PyPI when the images are built, and each
package brings its own license file into the image. The versions are pinned in
`webapp/requirements.txt` and `poller/requirements.txt`; the list below covers both files, including
the packages that are only pulled in by others.

| Package | License | Used by |
|---|---|---|
| argon2-cffi | MIT | webapp |
| argon2-cffi-bindings | MIT | webapp |
| blinker | MIT | webapp |
| cbor2 | MIT | webapp |
| certifi | MPL-2.0 | webapp |
| cffi | MIT-0 | webapp |
| charset-normalizer | MIT | webapp |
| click | BSD-3-Clause | webapp |
| cryptography | Apache-2.0 OR BSD-3-Clause | webapp |
| Flask | BSD-3-Clause | webapp |
| gunicorn | MIT | webapp |
| idna | BSD-3-Clause | webapp |
| itsdangerous | BSD-3-Clause | webapp |
| Jinja2 | BSD-3-Clause | webapp |
| MarkupSafe | BSD-3-Clause | webapp |
| packaging | Apache-2.0 OR BSD-2-Clause | webapp |
| Pillow | MIT-CMU | webapp |
| psycopg2-binary | LGPL-3.0-or-later, with an exception for linking with OpenSSL | webapp, poller |
| pyasn1 | BSD-2-Clause | webapp, poller |
| pyasn1_modules | BSD-2-Clause | webapp |
| pycparser | BSD-3-Clause | webapp |
| pyOpenSSL | Apache-2.0 | webapp |
| pysnmp | BSD-2-Clause | webapp, poller |
| requests | Apache-2.0 | webapp |
| typing_extensions | PSF-2.0 | webapp |
| tzdata | Apache-2.0 | webapp, poller |
| urllib3 | MIT | webapp |
| webauthn | BSD-3-Clause | webapp |
| Werkzeug | BSD-3-Clause | webapp |

Some packages are built with native libraries inside them, and their license file lists those
libraries and their licenses: Pillow (image libraries such as libjpeg, libpng and FreeType),
psycopg2-binary (libpq, OpenSSL, Kerberos, OpenLDAP and Cyrus SASL) and cryptography (OpenSSL).

## Container images

These are not in the repository either. Docker pulls them when Bifrost is built and started, and `alpine`
when a cold backup or a restore runs; the exact tags are pinned in `webapp/Dockerfile`,
`poller/Dockerfile`, `docker-compose.yml` and the backup and restore scripts.

| Image | Used for | License |
|---|---|---|
| `python` (slim, Debian) | the web app and the poller | Python: PSF-2.0. The Debian packages in the image keep their own licenses. |
| `timescale/timescaledb` | the database | PostgreSQL: PostgreSQL License. TimescaleDB: Apache-2.0 for the core and the [Timescale License](https://github.com/timescale/timescaledb/blob/main/tsl/LICENSE-TIMESCALE) for the rest, which includes the continuous aggregates and retention policies Bifrost relies on. The Alpine packages in the image keep their own licenses. |
| `alpine` | short-lived containers in `scripts/backup-cold.sh` and `scripts/restore.sh` that pack and unpack the data volume | Alpine Linux: the packages in the image keep their own licenses, among them BusyBox (GPL-2.0) and musl (MIT). |

Bifrost talks to the database over the network; it does not link with PostgreSQL or TimescaleDB.

## Map tile services

Map backgrounds are fetched from an external tile service, chosen per map, with the default set by
`MAP_SOURCE` in `.env`. These are services with their own terms of use, not code in Bifrost. Bifrost
shows each service's attribution on every map that uses it, physical and logical; following the
terms of the chosen service is up to whoever runs the installation. Sources whose terms do not allow
storing their tiles on a server can only be used live.

| Service | Attribution shown | Terms |
|---|---|---|
| Kartverket (grey, colour, topo raster and nautical chart) | © Kartverket | CC BY 4.0 — [terms of use](https://www.kartverket.no/api-og-data/vilkar-for-bruk) |
| OpenTopoMap | Map data: © OpenStreetMap contributors, SRTM \| Map style: © OpenTopoMap (CC-BY-SA) | CC-BY-SA — [about and terms](https://opentopomap.org/about); map data under the [ODbL](https://www.openstreetmap.org/copyright) |
| CARTO (Positron and Voyager) | © OpenStreetMap contributors, © CARTO | [CARTO basemap terms](https://carto.com/legal/basemap-terms/); map data under the [ODbL](https://www.openstreetmap.org/copyright). Needs the installation's own API key, and live only: the terms do not allow storing the tiles on a server |
| OpenStreetMap | © OpenStreetMap contributors | [Tile usage policy](https://operations.osmfoundation.org/policies/tiles/); map data under the [ODbL](https://www.openstreetmap.org/copyright) |
| Google (Map Tiles API) | © Google | [Google Maps Platform terms](https://cloud.google.com/maps-platform/terms); needs the installation's own API key |
| Own source (`MAP_SOURCE_URL`) | `MAP_SOURCE_ATTRIBUTION` | The terms of that source |
