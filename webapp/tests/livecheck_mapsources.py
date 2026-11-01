# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.1
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Enkel levende-sjekk av de eksterne kartkildene i mapsources.SOURCES.

Henter ÉN flis (zoom 4, over Norge) per kilde — bevisst minimal last på tjenestene.
Fanger opp at en kilde ikke svarer eller har endret URL/API (feil status, HTML-feilside
i stedet for bilde). Sources that need an API key are checked only when it is set in the
environment: Google with one createSession, CARTO with a tile with and one without the key.

Kjøres i CI (Tier C). Plukkes IKKE opp av `unittest discover` (filnavnet starter ikke
med test_), så lokale enhetstester rører ikke nettet. Manuelt:

  # inne i webapp-containeren (WORKDIR /app):
  python tests/livecheck_mapsources.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests      # noqa: E402
import mapsources    # noqa: E402

# Kildene vi forventer at registeret har; en fjernet/omdøpt kilde skal merkes.
EXPECTED = {"kartverket", "kartverket_farge", "kartverket_toporaster", "kartverket_sjokart", "opentopomap",
            "carto", "carto_voyager", "osm", "google"}
TILE = {"z": 4, "x": 8, "y": 4}
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"RIFF")   # PNG, JPEG, WEBP


def check_tile(key, src):
    url = src["url"].format(**TILE)
    try:
        r = requests.get(url, headers={"User-Agent": mapsources.USER_AGENT}, timeout=10)
    except Exception as e:                             # noqa: BLE001
        return False, f"nådde ikke kilden: {e}"
    ctype = r.headers.get("Content-Type", "")
    if r.status_code != 200:
        return False, f"HTTP {r.status_code} ({url})"
    if not ctype.startswith("image/") or not r.content.startswith(IMAGE_MAGIC):
        return False, f"svarte ikke med bilde (Content-Type {ctype!r}) — API/URL endret? ({url})"
    return True, f"{ctype}, {len(r.content)} byte"


def main():
    results = []
    missing = EXPECTED - set(mapsources.SOURCES)
    results.append(("registeret har alle kildene", not missing,
                    f"mangler: {sorted(missing)}" if missing else ""))
    for key, src in mapsources.SOURCES.items():
        if src.get("key"):                             # sources that need an API key: only when it is set
            env = src["key"].upper()
            api_key = os.environ.get(env, "").strip()
            if not api_key:
                print(f"SKIP  {key}: {env} is not set")
                continue
            ok, detail = (mapsources.validate_google_key(api_key) if src.get("session")
                          else mapsources.validate_carto_key(api_key, src))
        else:
            ok, detail = check_tile(key, src)
        results.append((key, ok, detail))

    for name, ok, detail in results:
        print(f"{'OK  ' if ok else 'FEIL'}  {name}{': ' + detail if detail else ''}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} OK")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
