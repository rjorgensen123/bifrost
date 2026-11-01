# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Demo-datasett: Norge-backbone (9 enheter), avledet fra
Definisjon/.../integration-tests/fixtures/ (devices.yaml + interfaces.yaml).

Lar oss kjøre en komplett, fungerende demonstrasjon uten ekte SNMP-enheter:
- demo-discovery serverer disse hostenes interface-katalog
- load_demo() oppretter alle hostene + interfaces og backfiller ~36t historikk
- poller'en (DEMO_MODE=1) genererer videre syntetiske tellere

ifAlias navngir peer-host ("to core-bergen"), så backbone-linkene utledes
automatisk. Interne roller: core (core↔core 10G) / distribution (mot dist 1G).
"""
import math
import random
from datetime import datetime, timezone, timedelta

from db import cursor

# G-319: demo-hoster får en tydelig IKKE-ekte community (de polles uansett kun syntetisk via
# DEMO_MODE — og med DEMO_MODE av hopper pollerens valid_ip-vakt over lab-hostnavnene).
DEMO_COMMUNITY = "demo-mock-only"

# (navn, adresse, [(ifindex, ifname, alias, speed_mbps, up), ...])
_SPEC = [
    ("core-oslo", "core-oslo.lab.example.net", [
        (1, "Gi0/0/0", "to core-bergen", 10000, True),
        (2, "Gi0/0/1", "to core-trondheim", 10000, True),
        (3, "Gi0/0/2", "to core-stavanger", 10000, True),
        (4, "Gi0/0/3", "to core-tromso", 10000, True),
        (5, "Gi0/0/4", "to dist-east", 1000, True),
        (6, "Gi0/0/5", "to dist-south", 1000, True)]),
    ("core-bergen", "core-bergen.lab.example.net", [
        (1, "Gi0/0/0", "to core-oslo", 10000, True),
        (2, "Gi0/0/1", "to core-trondheim", 10000, True),
        (3, "Gi0/0/2", "to core-stavanger", 10000, True),
        (4, "Gi0/0/3", "to dist-west", 1000, True)]),
    ("core-trondheim", "core-trondheim.lab.example.net", [
        (1, "Gi0/0/0", "to core-oslo", 10000, True),
        (2, "Gi0/0/1", "to core-bergen", 10000, True),
        (3, "Gi0/0/2", "to core-tromso", 10000, True),
        (4, "Gi0/0/3", "to dist-east", 1000, True)]),
    ("core-stavanger", "core-stavanger.lab.example.net", [
        (1, "Gi0/0/0", "to core-oslo", 10000, True),
        (2, "Gi0/0/1", "to core-bergen", 10000, True),
        (3, "Gi0/0/2", "to dist-south", 1000, True),
        (4, "Gi0/0/3", "to dist-west", 1000, True)]),
    ("core-tromso", "core-tromso.lab.example.net", [
        (1, "Gi0/0/0", "to core-oslo", 10000, True),
        (2, "Gi0/0/1", "to core-trondheim", 10000, True),
        (3, "Gi0/0/2", "to dist-north", 1000, True)]),
    ("dist-north", "dist-north.lab.example.net", [
        (1, "Gi0/0/0", "to core-tromso", 1000, True),
        (2, "Gi0/0/1", "spare (unused)", 1000, False)]),
    ("dist-south", "dist-south.lab.example.net", [
        (1, "Gi0/0/0", "to core-oslo", 1000, True),
        (2, "Gi0/0/1", "to core-stavanger", 1000, True)]),
    ("dist-east", "dist-east.lab.example.net", [
        (1, "Gi0/0/0", "to core-oslo", 1000, True),
        (2, "Gi0/0/1", "to core-trondheim", 1000, True)]),
    ("dist-west", "dist-west.lab.example.net", [
        (1, "Gi0/0/0", "to core-bergen", 1000, True),
        (2, "Gi0/0/1", "to core-stavanger", 1000, True)]),
]

# GPS (WGS84 desimalgrader) pr. demo-host — gir test-settet realistisk geografi for
# GPS-plassering (G-201). De fem core-ene = sine byer; dist-* ligger forskjøvet mot sin
# himmelretning, nær uplink-byen (east = Østlandet mellom Oslo/Trondheim, ikke Finnmark).
_GPS = {
    "core-oslo":       (59.91, 10.75),   # Oslo
    "core-bergen":     (60.39,  5.32),   # Bergen (vest)
    "core-trondheim":  (63.43, 10.40),   # Trondheim
    "core-stavanger":  (58.97,  5.73),   # Stavanger (sør-vest)
    "core-tromso":     (69.65, 18.95),   # Tromsø (nord)
    "dist-north":      (69.97, 23.27),   # Alta (Finnmark, nord)
    "dist-south":      (58.15,  7.99),   # Kristiansand (sør)
    "dist-east":       (60.79, 11.07),   # Hamar (Østlandet, mellom Oslo og Trondheim)
    "dist-west":       (59.41,  5.27),   # Haugesund (vestkyst, mellom Bergen og Stavanger)
}

# site (by) pr. demo-host — clustering-nøkkelen i plasserings-motoren (NetBox setter dette i prod).
_SITE = {
    "core-oslo": "Oslo", "core-bergen": "Bergen", "core-trondheim": "Trondheim",
    "core-stavanger": "Stavanger", "core-tromso": "Tromsø", "dist-north": "Alta",
    "dist-south": "Kristiansand", "dist-east": "Hamar", "dist-west": "Haugesund",
}


def _role(hostname, alias):
    if hostname.startswith("core-") and alias.startswith("to core-"):
        return "core"
    return "distribution"


def hosts():
    out = []
    for name, addr, ifs in _SPEC:
        interfaces = []
        for (idx, ifn, alias, spd, up) in ifs:
            r = _role(name, alias)
            interfaces.append({"if_index": idx, "if_name": ifn, "alias": alias,
                               "speed_mbps": spd, "up": up, "physical": True,
                               "sugg_role": r, "sugg_cp": None, "sugg_asn": None})
        lat, lon = _GPS.get(name, (None, None))
        out.append({"name": name, "address": addr, "lat": lat, "lon": lon,
                    "site": _SITE.get(name), "interfaces": interfaces})
    return out


def discover(address):
    """Demo-discovery: finn host som matcher adressen, ellers generisk demo-enhet."""
    a = (address or "").lower()
    for h in hosts():
        if h["name"] == a or h["name"] in a or a in h["address"].lower():
            return {"sysname": h["name"], "interfaces": h["interfaces"]}
    base = hosts()[0]
    return {"sysname": "demo-" + (address or "device"), "interfaces": base["interfaces"]}


def load_demo():
    """Opprett alle demo-hoster + interfaces, backfill ~36t syntetisk historikk.
    Alt i ÉN transaksjon: feiler noe underveis, legges ingenting inn (så et nytt forsøk ikke blir
    stående bak «core-oslo finnes»-vakten med en halv demo). Idempotent: hopper over hvis demo alt er
    lastet. Returnerer (antall_if, antall_samples)."""
    from psycopg2.extras import execute_values
    with cursor() as cur:
        # G-319: demo lastes KUN i en ren/tom installasjon — aldri der det finnes ekte hoster.
        cur.execute("SELECT 1 FROM host WHERE is_demo = FALSE AND deleted_at IS NULL LIMIT 1")
        if cur.fetchone():
            raise RuntimeError("installasjonen har ekte (ikke-demo) hoster — "
                               "demo lastes kun i en tom installasjon")
        cur.execute("SELECT 1 FROM host WHERE name='core-oslo' LIMIT 1")
        if cur.fetchone():
            return 0, 0
        iface_ids = []
        for h in hosts():
            # G-347: site/GPS bor i site-tabellen — demo lager (eller gjenbruker) en site-rad og linker
            # via site_id, i stedet for de droppede legacy-kolonnene host.site/lat/lon.
            site_id = None
            if h.get("site"):
                cur.execute("INSERT INTO site (name,lat,lon) VALUES (%s,%s,%s) "
                            "ON CONFLICT (name) DO UPDATE SET lat=EXCLUDED.lat, lon=EXCLUDED.lon "
                            "RETURNING id", (h["site"], h["lat"], h["lon"]))
                site_id = cur.fetchone()["id"]
            cur.execute("INSERT INTO host (name,address,snmp_version,snmp_community,site_id,is_demo) "
                        "VALUES (%s,%s,'2c',%s,%s,TRUE) RETURNING id",
                        (h["name"], h["address"], DEMO_COMMUNITY, site_id))
            hid = cur.fetchone()["id"]
            for i in h["interfaces"]:
                cur.execute("""INSERT INTO interface
                    (host_id,if_index,if_name,ifalias,role,speed_mbps,enabled)
                    VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (hid, i["if_index"], i["if_name"], i["alias"], i["sugg_role"],
                     i["speed_mbps"], 1 if i["up"] else 0))
                row = cur.fetchone()
                if i["up"]:
                    iface_ids.append((row["id"], i["speed_mbps"]))
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        rows = []
        for iid, spd in iface_ids:
            cap = spd * 1_000_000
            cin, cout = random.randint(0, 10**9), random.randint(0, 10**9)
            for k in range(432, 0, -1):
                ts = now - timedelta(minutes=5 * k)
                hour = ts.hour + ts.minute / 60.0
                diurnal = 0.35 + 0.45 * (1 + math.sin((hour - 9) / 24 * 2 * math.pi))
                cin += int(cap * min(0.95, diurnal * random.uniform(0.4, 0.9)) / 8 * 300)
                cout += int(cap * min(0.95, diurnal * random.uniform(0.3, 0.8)) / 8 * 300)
                rows.append((ts, iid, cin, cout))
        execute_values(cur, "INSERT INTO if_sample (time,interface_id,in_octets,out_octets) VALUES %s "
                       "ON CONFLICT (interface_id, time) DO NOTHING", rows)   # G-310: idempotent re-seed
    return len(iface_ids), len(rows)
