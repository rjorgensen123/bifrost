# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
G-201 plasserings-motor — «hjernen i Bifrost», ÉN samlet fil (Stage 2 + GPS-seed fra Stage 1).

Realiserer intensjonen i INIT-63 (4-pass deterministisk auto-layout) TILPASSET kjørende kode/erfaring
(kjørende kode overstyrer INIT ved konflikt — se memory init-vs-running-code-governance). GPS er det
geografiske UTGANGSPUNKTET (seed), ikke bare et cardinal-hint.

ALT er TOGGLE-BART (Toggles) så hver bit kan testes av/på:
  gps_seed · cluster_by_site · cardinal_anchors · hub_center · redundancy_pairs · intra_cluster ·
  overlap_resolve · grid_snap

4 passeringer:
  Pass 1  clustering: grupper på site; cardinal-/hub-deteksjon; redundans-par (Jaccard).
  Pass 2  cluster-plassering: GPS-seed ELLER cardinal-ankre + hub-senter, så meta-FR + overlap-resolve.
  Pass 3  intra-cluster: radial (liten/mesh) vs. hierarkisk (rolle-tier) vs. enkel.
  Pass 4  finalisering: node-overlap-resolve, grid-snap, klamp + kvalitetsscore Q.

Determinisme (INIT-63 §10): én Random(seed), sortert iterasjon, faste iterasjons-tall.
Inndata uavhengig av karttype → samme motor for fysisk OG logisk (G-201 sentralisering).
"""
import math
import random as _random
import re as _re

import geo

# --- parametre (INIT-63 §5, tilpasset) -------------------------------------
DEFAULTS = dict(
    NODE_FOOTPRINT=80, CANVAS_MARGIN=60, GRID=10,
    FR_ITERATIONS_META=120, FR_TEMP_FRACTION=0.10, IDEAL_EDGE_C=1.0, W_GRAVITY=0.02,
    CARDINAL_MARGIN=100, W_CARDINAL=4.0, W_HUB=4.0,
    RADIAL_MAX_NODES=8, MESH_EDGE_DENSITY_THRESHOLD=1.2, MIN_RING_RADIUS=90,
    BARYCENTER_SWEEPS=4, JACCARD_REDUNDANCY=0.6, OVERLAP_RESOLVE_ITERS=200,
    HUB_GRAVITY_MULT=10.0, SCALE_FILL_TARGET=0.9, SWAP_PASSES=20,
)
ROLE_TIERS = ["core", "distribution", "dist", "access", "NNI", "edge", "IX", "PNI", "CDN",
              "transit", "peering", "customer", "other", "ukategorisert"]

# alle brytere PÅ som default (operatøren slår av for å isolere en bit ved testing)
DEFAULT_TOGGLES = dict(
    gps_seed=True, cluster_by_site=True, cardinal_anchors=True, hub_center=True,
    redundancy_pairs=True, intra_cluster=True, overlap_resolve=True, grid_snap=True,
)

_CARDINALS = {
    "north": ("n",), "south": ("s",), "east": ("e",), "west": ("w",),
    "northeast": ("n", "e"), "northwest": ("n", "w"),
    "southeast": ("s", "e"), "southwest": ("s", "w"),
}



def clamp_coord(v, hi):
    """Kart-koordinat fra klienten → heltall innenfor rammen [0, hi], eller None hvis verdien ikke er et
    endelig tall (NaN/Infinity/tekst/bool). Avrunder nedover som int() — gyldige verdier blir som før."""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return int(max(0, min(f, hi)))

def _toggles(t):
    out = dict(DEFAULT_TOGGLES)
    if t:
        out.update({k: bool(v) for k, v in t.items() if k in out})
    return out


def _cardinal_of(site):
    """Returner (dx, dy) i [-1,1] for et cardinal site-navn, ellers None. Strip site-/region-prefiks."""
    if not site:
        return None
    s = site.strip().lower()
    for pre in ("site-", "region-", "site_", "region_"):
        if s.startswith(pre):
            s = s[len(pre):]
    parts = _CARDINALS.get(s)
    if not parts:
        return None
    dx = (1 if "e" in parts else 0) - (1 if "w" in parts else 0)
    dy = (1 if "s" in parts else 0) - (1 if "n" in parts else 0)   # skjerm-y: sør = ned = +
    return dx, dy


# --- G-238: hostnavn-tolkning (lokasjon vs. funksjon) ----------------------
# G-256: DEFAULT TIER-TABELL (0 = viktigst / ryggrad … høyere = mindre viktig). Hostnavn-brøkstykker
# (funksjons-tokens) → tier; lokasjon er noe annet. ÉN KILDE: `_FUNC_TIER` bygges fra denne lista.
# Klar for å eksponeres som en per-kart, operatør-tunbar tabell på admin-siden senere (motoren konsumerer
# allerede en tier-liste; per-kart override + redigerings-UI legges på da). Auto-generator = parkert G-257.
# Format pr. rad: (tier, [navne-brøkstykker], etikett). Operatør-regelen «én match pr. tier» gjelder den
# tunbare tabellen; default-en grupperer synonymer (core/bb/backbone) på samme tier. core < re (Roger).
DEFAULT_TIER_TABLE = [
    (0, ["core", "bb", "backbone", "p"], "Kjerne / ryggrad"),
    (1, ["re"], "Ruter (RE)"),
    (2, ["aggr", "agg", "aggregation", "dist", "distribution", "pe",
         "r", "rtr", "router", "gw", "bdr", "border"], "Aggregering / distribusjon"),
    (3, ["acc", "access"], "Aksess"),
    (4, ["sw", "switch", "swith"], "Switch"),
    (5, ["edge", "cpe", "ce"], "Edge / kunde"),
    # tier 6–9: ledige for operatør-definerte typer (utstyrsmodeller o.l.)
]
_FUNC_TIER = {tok: tier for tier, toks, _lbl in DEFAULT_TIER_TABLE for tok in toks}
# sub-område-modifiers regnes som DEL av lokasjon (ikke funksjon): «bergen-ytre-sw01» → lokasjon «bergen-ytre»
_LOC_MODIFIERS = {"ytre", "indre", "nord", "sor", "sør", "ost", "øst", "vest",
                  "nordre", "sondre", "søndre", "ovre", "øvre", "nedre"}


def _func_token(tok):
    """Token (evt. m/ tall-suffiks, f.eks 're1','sw09') → tier hvis funksjons-token, ellers None."""
    m = _re.match(r"^([a-zæøå]+?)(\d*)$", tok)
    if not m:
        return None
    base = m.group(1)
    return _FUNC_TIER.get(base)


def _name_tier(name):
    """G-256: hostnavn → tier (0=viktigst/ryggrad … 9=minst viktig). Robust: minste funksjons-tier blant
    ALLE navne-tokens (så 'demo2-core-oslo' → 0 via 'core'); ingen funksjons-token → 9. Default-tabell
    er `_FUNC_TIER`; den per-kart operatør-tunbare tabellen legges på senere (input til motoren)."""
    if not name:
        return 9
    tiers = [t for t in (_func_token(tok) for tok in _re.split(r"[^a-z0-9æøå]+", name.lower())) if t is not None]
    return min(tiers) if tiers else 9


def _parse_name(name):
    """G-238: tolk hostnavn → (lokasjons-token | None, rolle-tier | None). Lokasjon = ledende
    token(er) som verken er funksjons-token eller rene tall (+ sub-område-modifiers beholdes).
    Rolle-tier = mest kjerne-nære (laveste) funksjons-token. Navn er HINT, ikke fasit.
    Eks: 'bergen-aggr-sw01' → ('bergen', 1); 'bergen-ytre-sw01' → ('bergen-ytre', 4); 're1' → (None, 2)."""
    if not name:
        return None, None
    toks = [t for t in _re.split(r"[-_.\s]+", str(name).lower()) if t]
    loc_parts = []
    tier = None
    for t in toks:
        ft = _func_token(t)
        if ft is not None and t not in _LOC_MODIFIERS:
            tier = ft if tier is None else min(tier, ft)
            continue
        if t.isdigit():
            continue
        loc_parts.append(t)
    loc = "-".join(loc_parts) if loc_parts else None
    return loc, tier


# ===========================================================================
# Velprøvde bygge­klosser fra den gamle layout.py (merget inn — G-220). Strukturtrygge:
# bevarer relative posisjoner / topologi, bryter ikke GPS/site/rolle.
# ===========================================================================
def _snap(v, g):
    return int(round(v / g) * g)


def _effective_grid(grid_size, P):
    """G-126: minste celle-avstand som garanterer ingen-overlapp (≥ footprint, multiplum av grid)."""
    g = max(1, int(grid_size))
    return g * max(1, math.ceil(P["NODE_FOOTPRINT"] / g))


def _grid_cells(width, height, grid_size, P):
    """G-126: gyldige celle-sentre på effektivt grid innenfor margin. Minst én (sentrum)."""
    E = _effective_grid(grid_size, P)
    m = P["CANVAS_MARGIN"]
    lo_x, hi_x, lo_y, hi_y = m, width - m, m, height - m
    xs = list(range(int(math.ceil(lo_x / E) * E), int(hi_x) + 1, E))
    ys = list(range(int(math.ceil(lo_y / E) * E), int(hi_y) + 1, E))
    if not xs:
        xs = [_snap(width / 2.0, E)]
    if not ys:
        ys = [_snap(height / 2.0, E)]
    return [(x, y) for x in xs for y in ys], E


def grid_capacity(width, height, grid_size, params=None):
    """G-126: hvor mange objekter får plass på kartets rutenett. None = grid av (ingen grense)."""
    if not grid_size or grid_size <= 0:
        return None
    P = dict(DEFAULTS); P.update(params or {})
    cells, _ = _grid_cells(width, height, grid_size, P)
    return len(cells)


def _assign_grid_cells(pos, pinned, width, height, grid_size, P):
    """G-126: hver ikke-pinnet node får DISTINKT nærmeste ledige celle (ingen overlapp garantert).
    Pinnede beholder eksakt posisjon og blokkerer celler innen footprint. Over kapasitet: degrader pent."""
    cells, E = _grid_cells(width, height, grid_size, P)
    out = {}
    occupied = set()
    f2 = P["NODE_FOOTPRINT"] ** 2
    for v in pos:
        if v in pinned:
            px, py = pinned[v]
            out[v] = (int(px), int(py))
            near = {c for c in cells if (c[0] - px) ** 2 + (c[1] - py) ** 2 < f2}
            occupied.update(near or {min(cells, key=lambda c: (c[0] - px) ** 2 + (c[1] - py) ** 2)})
    movable = sorted((v for v in pos if v not in pinned),
                     key=lambda v: (pos[v][1], pos[v][0], str(v)))   # topp-venstre først (determ.)
    for v in movable:
        x, y = pos[v]
        free = [c for c in cells if c not in occupied]
        pool = free or cells
        c = min(pool, key=lambda c: (c[0] - x) ** 2 + (c[1] - y) ** 2)
        occupied.add(c)
        out[v] = (int(c[0]), int(c[1]))
    return out


def _scale_to_fill(pos, ids, width, height, P):
    """G-031/036: sentrer bounding-boks på lerret + skaler UNIFORMT til å fylle ~90% (mot klumping).
    Muteres in-place. G-241: skaleringen er uniform → bevarer relativ geografi/form (også for GPS-seedede
    layouts), så den brukes når ingenting er MANUELT pinnet (kun manuell «lås posisjon» fryser layouten)."""
    if not ids:
        return             # tomt sett → min()/max() under ville kastet ValueError
    cx, cy = width / 2.0, height / 2.0
    m = P["CANVAS_MARGIN"]
    xs = [pos[v][0] for v in ids]; ys = [pos[v][1] for v in ids]
    bbcx = (min(xs) + max(xs)) / 2.0; bbcy = (min(ys) + max(ys)) / 2.0
    bw = max(1e-6, max(xs) - min(xs)); bh = max(1e-6, max(ys) - min(ys))
    s = min((width - 2 * m) * P["SCALE_FILL_TARGET"] / bw, (height - 2 * m) * P["SCALE_FILL_TARGET"] / bh)
    s = max(0.1, min(s, 12.0))
    for v in ids:
        pos[v] = (cx + (pos[v][0] - bbcx) * s, cy + (pos[v][1] - bbcy) * s)


def _swap_optimize(pos, elist, ids, fixed, passes=20):
    """G-036: behold (jevnt fordelte) posisjoner, men BYTT hvilke noder som sitter hvor for å
    minimere total kantlengde (færre kryssinger). Muteres in-place. KUN trygt på pure topologi
    (ingen site/rolle) — ellers ville en node byttet logisk/geografisk plass; derfor gates kallet på
    `not has_sites`. Rører ikke 'fixed' (manuelt pinnede). GPS er ingen lås (G-241)."""
    if not elist:
        return
    def edge_len():
        return sum(math.hypot(pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]) for a, b in elist)
    swappable = [v for v in ids if v not in fixed]
    for _ in range(passes):
        improved = False
        for i in range(len(swappable)):
            for j in range(i + 1, len(swappable)):
                a, b = swappable[i], swappable[j]
                before = edge_len()
                pos[a], pos[b] = pos[b], pos[a]
                if edge_len() < before - 1e-6:
                    improved = True
                else:
                    pos[a], pos[b] = pos[b], pos[a]
        if not improved:
            break


def _swap_reduce_crossings(pos, elist, ids, fixed, fp, passes=8):
    """G-239: bytt posisjoner mellom IKKE-faste noder for å kutte KRYSSINGER direkte — link-gjennom-node
    (verst, jf. Roger) vektes tungt + link-link-kryssinger. Muteres in-place. `fixed` (manuelt pinnede /
    kjente noder i inkrementell modus) rikkes ikke; GPS er ingen lås (G-241). Brukes bl.a. for å rydde
    ghost-plassering. O(passes·M²·(E·N+E²))."""
    if not elist:
        return
    def cost():
        return 10 * count_link_node_crossings(pos, elist, fp) + count_crossings(pos, elist)
    movable = [v for v in ids if v not in fixed]
    if len(movable) < 2:
        return
    cur = cost()
    for _ in range(passes):
        if cur == 0:
            break
        improved = False
        for i in range(len(movable)):
            for j in range(i + 1, len(movable)):
                a, b = movable[i], movable[j]
                pos[a], pos[b] = pos[b], pos[a]
                nc = cost()
                if nc < cur:
                    cur = nc; improved = True
                else:
                    pos[a], pos[b] = pos[b], pos[a]
        if not improved:
            break


def _closest_point_on_seg(p, a, b):
    """Nærmeste punkt på linjestykket a–b til punktet p."""
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        return (ax, ay)
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    return (ax + t * dx, ay + t * dy)


def _relax_global(pos, edges, ids, fixed, fp, W, H, iters=220, geo_target=None):
    """G-256 (UTKAST, neste-gen): GLOBAL kraft-relaksering som lager plass der det trengs ved at ALLE
    noder forskyves litt (en bølge) i stedet for å flytte én node langt. MYKE, vektede krefter (fuzzy —
    ingen hard-optimering av ett mål):
      • lenke-attraksjon — koblede noder trekkes mot hverandre,
      • node-node-frastøting — alle noder dytter hverandre fra seg (sprer, gir rom, hindrer tomme soner),
      • LENKE-GJENNOM-NODE-frastøting — en node under en FREMMED lenke dyttes vinkelrett av + lenkens
        ender skubbes litt unna → åpner rommet (Rogers «forskyv alt bitte litt»),
      • SVAK geo-kraft (geo_target) — hver node dras mykt mot sin GPS-posisjon → relativ geografi bevares
        OMTRENT (fuzzy hint), men vinner ikke; brytes lokalt der frastøtning må fylle et tomt hjørne.
    Resultatet er bevisst litt SKJEVT (ærlig mot et ekte geografi-/link-bundet nett), ikke matematisk
    perfekt. Manuelt pinnede ('fixed') står fast. Muteres in-place. Q-portes av KALLEREN."""
    movable = [v for v in ids if v not in fixed]
    if len(movable) < 2 or not edges:
        return
    W_GEO = 0.08                      # svak — geografi er et hint, ikke et mål
    W_GRAV = 0.09                     # senter-gravitasjon: balanserer frastøtning → fyller MIDTEN
                                      # (uten den presses noder ut mot kantene = tom midte; for mye = klump)
    # ideell node-avstand = FYLL-avstanden √(areal/antall): da sprer frastøtningen nodene utover HELE
    # lerretet (ikke en kompakt klump). Min fp·1.3 så de aldri overlapper. Faktor >1 → fyller mer mot
    # kantene/hjørnene (balanse), uten å bli et rutenett (frastøtning er organisk, ikke jevn-avstand).
    k = max(fp * 1.3, (float(W) * float(H) / max(2, len(ids))) ** 0.5 * 1.0)
    temp0 = min(W, H) * 0.08          # maks steg pr. iterasjon (avtar lineært → layouten setter seg)
    for it in range(iters):
        disp = {v: [0.0, 0.0] for v in movable}
        # node-node frastøting (alle par)
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = ids[i], ids[j]
                ddx = pos[a][0] - pos[b][0]; ddy = pos[a][1] - pos[b][1]
                d = math.hypot(ddx, ddy) or 0.01
                f = k * k / d
                ux, uy = ddx / d, ddy / d
                if a in disp: disp[a][0] += ux * f; disp[a][1] += uy * f
                if b in disp: disp[b][0] -= ux * f; disp[b][1] -= uy * f
        # lenke-attraksjon (dempet — for sterk attraksjon klumper koblede noder; vi vil spre mer)
        for a, b in edges:
            ddx = pos[a][0] - pos[b][0]; ddy = pos[a][1] - pos[b][1]
            d = math.hypot(ddx, ddy) or 0.01
            f = 0.7 * d * d / k
            ux, uy = ddx / d, ddy / d
            if a in disp: disp[a][0] -= ux * f; disp[a][1] -= uy * f
            if b in disp: disp[b][0] += ux * f; disp[b][1] += uy * f
        # lenke-gjennom-node frastøting (det nye leddet)
        for a, b in edges:
            for v in ids:
                if v == a or v == b:
                    continue
                cx, cy = _closest_point_on_seg(pos[v], pos[a], pos[b])
                ddx = pos[v][0] - cx; ddy = pos[v][1] - cy
                d = math.hypot(ddx, ddy)
                if d < fp:                       # v ligger på/under lenken
                    d = d or 0.01
                    f = (fp - d) * 6.0           # sterk: skill node og lenke
                    ux, uy = ddx / d, ddy / d
                    if v in disp: disp[v][0] += ux * f; disp[v][1] += uy * f
                    if a in disp: disp[a][0] -= ux * f * 0.5; disp[a][1] -= uy * f * 0.5
                    if b in disp: disp[b][0] -= ux * f * 0.5; disp[b][1] -= uy * f * 0.5
        # svak geo-kraft: dra noder mykt mot GPS-posisjon → relativ geografi bevares omtrent (fuzzy)
        if geo_target:
            for v in movable:
                g = geo_target.get(v)
                if g:
                    disp[v][0] += W_GEO * (g[0] - pos[v][0]); disp[v][1] += W_GEO * (g[1] - pos[v][1])
        # senter-gravitasjon: mild trekk mot lerret-senter → fyller MIDTEN og hindrer «ring»-fella
        # (ren frastøtning presser ellers alle noder ut mot kantene → tomrom i midten, jf. Roger).
        cgx, cgy = W / 2.0, H / 2.0
        for v in movable:
            disp[v][0] += W_GRAV * (cgx - pos[v][0]); disp[v][1] += W_GRAV * (cgy - pos[v][1])
        temp = temp0 * (1.0 - it / iters)
        for v in movable:
            dl = math.hypot(disp[v][0], disp[v][1]) or 1.0
            s = min(dl, temp)
            pos[v] = (min(float(W), max(0.0, pos[v][0] + disp[v][0] / dl * s)),
                      min(float(H), max(0.0, pos[v][1] + disp[v][1] / dl * s)))


def _orient_to_geography(pos, geo_target, ids):
    """G-256: velg den FLIPPEN (ingen/horisontal/vertikal/begge) som best matcher geografien (nord opp,
    øst høyre) og anvend den på HELE den presenterte layouten. KUN rene flipp brukes — de PERMUTERER
    kvadrantene og bevarer derfor BÅDE balansen og kryssings-tallet (en vilkårlig skrå rotasjon ville
    flyttet noder over kvadrant-grenser og brutt balansen). Geografi er her en svak TIE-BREAKER mellom
    likeverdige layouts (f.eks. Oslo oppe vs nede). Flipp om bbox-senter → blir værende på lerretet.
    Muteres in-place. KUN det vi presenterer — bakgrunnsbildet røres ALDRI."""
    gids = [i for i in ids if i in geo_target and geo_target[i]]
    if len(gids) < 2:
        return
    def norm(pts):   # senter + skala-normaliser → sammenlign kun ORIENTERING
        cx = sum(p[0] for p in pts) / len(pts); cy = sum(p[1] for p in pts) / len(pts)
        c = [(p[0] - cx, p[1] - cy) for p in pts]
        s = (sum(x * x + y * y for x, y in c) / len(c)) ** 0.5 or 1.0
        return [(x / s, y / s) for x, y in c]
    T = norm([geo_target[i] for i in gids])
    sx = sum(pos[i][0] for i in gids) / len(gids); sy = sum(pos[i][1] for i in gids) / len(gids)
    best = None
    for fx, fy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
        S = norm([((pos[i][0] - sx) * fx, (pos[i][1] - sy) * fy) for i in gids])
        err = sum((S[k][0] - T[k][0]) ** 2 + (S[k][1] - T[k][1]) ** 2 for k in range(len(gids)))
        if best is None or err < best[0]:
            best = (err, fx, fy)
    _, fx, fy = best
    if fx == 1 and fy == 1:
        return
    xs = [pos[i][0] for i in ids]; ys = [pos[i][1] for i in ids]
    mx = (min(xs) + max(xs)) / 2.0; my = (min(ys) + max(ys)) / 2.0
    for i in ids:
        pos[i] = (mx + (pos[i][0] - mx) * fx, my + (pos[i][1] - my) * fy)


def _crossing_nodes(pos, edges):
    """Settet av noder som er endepunkt i MINST én kryssing (link-link). O(E²)."""
    s = set()
    for i in range(len(edges)):
        a, b = edges[i]
        for j in range(i + 1, len(edges)):
            c, d = edges[j]
            if _segments_cross(pos[a], pos[b], pos[c], pos[d]):
                s.update((a, b, c, d))
    return s


def _push_off_links(pos, edges, ids, fixed, fp, W, H, iters=40):
    """G-256 (Roger, streng regel): en node skal ALDRI ligge OPPÅ en fremmed lenke (lenke-gjennom-node =
    verst). Dytt enhver node som er nærmere enn `fp` fra en lenke den ikke er endepunkt i, perpendikulært
    av til den har klaring — godta heller en link-link-kryssing i et ledig område. Muteres in-place."""
    edges = [(a, b) for a, b in edges if a != b]
    for _ in range(iters):
        moved = False
        for v in ids:
            if v in fixed:
                continue
            for a, b in edges:
                if v == a or v == b:
                    continue
                cx, cy = _closest_point_on_seg(pos[v], pos[a], pos[b])
                ddx = pos[v][0] - cx; ddy = pos[v][1] - cy
                d = math.hypot(ddx, ddy)
                if d < fp:
                    if d < 0.5:   # står på linja → dytt vinkelrett på lenke-retningen
                        ex = pos[b][0] - pos[a][0]; ey = pos[b][1] - pos[a][1]
                        nl = math.hypot(ex, ey) or 1.0
                        ux, uy = -ey / nl, ex / nl
                    else:
                        ux, uy = ddx / d, ddy / d
                    push = (fp - d) + 4.0
                    pos[v] = (min(W, max(0.0, pos[v][0] + ux * push)),
                              min(H, max(0.0, pos[v][1] + uy * push)))
                    moved = True
        if not moved:
            break


def _relocate_for_crossings(pos, edges, ids, fixed, fp, W, H, passes=2):
    """G-256: flytt nodene som FAKTISK er i en kryssing til en bedre plass (rutenett-søk rundt nabo-
    barycenteret), Q-portet — godta kun hvis kryssings-kost synker; ties brytes mot NÆRMEST opprinnelig
    posisjon (minst offer). To grep mot tidligere versjon (Roger «flere/andre steg er greit»):
      • søker KUN rundt noder som er i en kryssing → få noder → RASKT (var O(alle pendant-noder)),
      • UANSETT grad → tar også høygrads core-mesh-kryssinger (ikke bare pendant-nesting).
    Forutsetter spredt layout (rom finnes). Muteres in-place."""
    edges = sorted({(min(a, b), max(a, b)) for a, b in edges if a != b})
    if not edges:
        return
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b); adj.setdefault(b, []).append(a)
    def cost():
        return 10 * count_link_node_crossings(pos, edges, fp) + count_crossings(pos, edges)
    step = max(20.0, fp / 2.0)
    K = 3              # lett søk (±4 steg) — godt-nok-utgangspunkt, ikke jakt på perfeksjon (Roger)
    cur = cost()
    for _ in range(passes):
        if cur == 0:
            break
        inv = [v for v in _crossing_nodes(pos, edges) if v not in fixed and adj.get(v)]
        if not inv:
            break
        improved = False
        for v in sorted(inv):
            nb = adj[v]
            bx = sum(pos[n][0] for n in nb) / len(nb)
            by = sum(pos[n][1] for n in nb) / len(nb)
            old = pos[v]
            cands = []
            for ix in range(-K, K + 1):
                for iy in range(-K, K + 1):
                    cand = (max(0.0, min(float(W), bx + ix * step)),
                            max(0.0, min(float(H), by + iy * step)))
                    if abs(cand[0] - old[0]) < 1e-6 and abs(cand[1] - old[1]) < 1e-6:
                        continue
                    pos[v] = cand
                    c = cost()
                    if c < cur - 1e-9:
                        cands.append((c, (cand[0] - old[0]) ** 2 + (cand[1] - old[1]) ** 2, cand))
            pos[v] = old
            if cands:
                cands.sort(key=lambda t: (t[0], t[1]))
                cur, _, best = cands[0]
                pos[v] = best
                improved = True
        if not improved:
            break


def _segments_cross(p1, p2, p3, p4):
    """True hvis linjestykkene p1-p2 og p3-p4 krysser hverandre (ekte kryssing, ikke felles endepunkt)."""
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
    # del endepunkt → ikke en kryssing (lenker som møtes i en node)
    if p1 in (p3, p4) or p2 in (p3, p4):
        return False
    d1 = ccw(p3, p4, p1); d2 = ccw(p3, p4, p2)
    d3 = ccw(p1, p2, p3); d4 = ccw(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def count_crossings(positions, edges):
    """G-238/G-215: antall kryssende lenke-par i en plassering. Målet er færrest mulig (ideelt 0).
    O(E²) — greit for typiske kart-størrelser. Felles endepunkt teller ikke."""
    segs = [(positions[a], positions[b]) for a, b in edges if a in positions and b in positions]
    c = 0
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            if _segments_cross(segs[i][0], segs[i][1], segs[j][0], segs[j][1]):
                c += 1
    return c


def _point_seg_dist(p, a, b):
    """Korteste avstand fra punkt p til linjestykket a-b, + parameter t (0..1 langs segmentet)."""
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-9:
        return math.hypot(px - ax, py - ay), 0.0
    t = ((px - ax) * dx + (py - ay) * dy) / L2
    t = max(0.0, min(1.0, t))
    cx, cy = ax + t * dx, ay + t * dy
    return math.hypot(px - cx, py - cy), t


def count_link_node_crossings(positions, edges, footprint):
    """G-239: antall ganger en LENKE går gjennom (eller tett inntil) en NODE som ikke er endepunktet.
    Dette er VERRE enn at to lenker krysser hverandre (Roger) — en link gjennom et objekt ser ødelagt
    ut. Klarering = ~halve footprint. Ignorerer treff helt nær endene (t∈⟨0.06,0.94⟩) = der ligger
    nodens egne naboer. O(E·N)."""
    clear = footprint * 0.5
    c = 0
    for a, b in edges:
        if a not in positions or b not in positions:
            continue
        pa, pb = positions[a], positions[b]
        for nid, p in positions.items():
            if nid == a or nid == b:
                continue
            d, t = _point_seg_dist(p, pa, pb)
            if d < clear and 0.06 < t < 0.94:
                c += 1
    return c


def quality(positions, edges, width, height, params=None):
    """Kvalitetsmål (INIT-63 §4 / G-215+G-238, merget fra layout.py): overlaps, min_dist, util,
    edge_len_cv, **crossings**, og en samlet vektet **Q** (lavere = bedre). Q driver «Forbedre/Nytt»-
    seed-valg og måler hovedmålet: færrest kryssende lenker. Ingen sideeffekter."""
    P = dict(DEFAULTS); P.update(params or {})
    ids = sorted(positions, key=str)
    n = len(ids)
    if n < 2:
        return {"nodes": n, "min_dist": None, "overlaps": 0, "util": 0.0, "edge_len_cv": None,
                "crossings": 0, "link_node_crossings": 0, "Q": 0.0}
    fp = P["NODE_FOOTPRINT"]
    min_d = float("inf"); overlaps = 0
    for i in range(n):
        for j in range(i + 1, n):
            (x1, y1), (x2, y2) = positions[ids[i]], positions[ids[j]]
            d = math.hypot(x1 - x2, y1 - y2)
            min_d = min(min_d, d)
            if d < fp - 1:
                overlaps += 1
    xs = [positions[i][0] for i in ids]; ys = [positions[i][1] for i in ids]
    usable = max(1.0, (width - 2 * P["CANVAS_MARGIN"]) * (height - 2 * P["CANVAS_MARGIN"]))
    util = ((max(xs) - min(xs)) * (max(ys) - min(ys))) / usable
    lens = [math.hypot(positions[a][0] - positions[b][0], positions[a][1] - positions[b][1])
            for a, b in edges if a in positions and b in positions]
    cv = None
    if lens:
        mean = sum(lens) / len(lens)
        if mean:
            var = sum((l - mean) ** 2 for l in lens) / len(lens)
            cv = math.sqrt(var) / mean
    crossings = count_crossings(positions, edges)
    ln_cross = count_link_node_crossings(positions, edges, fp)
    n_edges = len(lens)
    cross_norm = crossings / max(1, n_edges * (n_edges - 1) / 2)   # 0..1
    ln_norm = ln_cross / max(1, n_edges)                            # snitt link-node-treff pr. lenke
    util_pen = max(0.0, 1.0 - min(util, 1.0))                       # mindre utnyttelse = verre
    # G-238/G-239: samlet Q (INIT §4-vekter + link-gjennom-node). Link-gjennom-node vektes TYNGRE enn
    # link-link-kryssing (Roger: viktigere) — 30 pr. snittlig treff vs 0.3 for link-link. Overlapp hardest.
    Q = ((1.0 * (cv or 0.0)) + (1000.0 * overlaps) + (30.0 * ln_norm)
         + (0.3 * cross_norm) + (0.5 * util_pen))
    return {"nodes": n, "min_dist": round(min_d, 1), "overlaps": overlaps,
            "util": round(min(util, 1.0), 3), "edge_len_cv": round(cv, 3) if cv is not None else None,
            "crossings": crossings, "link_node_crossings": ln_cross, "Q": round(Q, 4)}


def _finalize_positions(pos, ids, pinned, width, height, P, grid_size,
                        do_grid_snap=True, do_overlap=True):
    """Felles slutt-steg for ALLE plasserings-modi (bygg + forbedre): grid kollisjonsfri celle-
    tildeling når grid_size>0, ellers cosmetic grid-snap FØRST + overlap-resolve SIST + klamp.
    Pinned/låste står absolutt fast; bevegelige ryddes helt klar (også vekk fra et låst objekt).
    Returnerer {id:(int x,int y)}."""
    W, H = float(width), float(height)
    M = P["CANVAS_MARGIN"]
    if grid_size and grid_size > 0:
        # G-239: bruk grid KUN når det er nok celler for ALLE noder (inkl. kjente/pinnede som
        # spiser celler). Ellers gjenbrukte `_assign_grid_cells` celler → stablet noder (typisk
        # ghosts oppå kjente). For få celler → IKKE grid (ville stablet); fri frastøting fra de
        # allerede spredte Pass-3-posisjonene (aldri-oppå-hverandre vinner over grid-justering).
        cells, _ = _grid_cells(W, H, grid_size, P)
        if len(cells) >= len(ids):
            return _assign_grid_cells(pos, pinned, W, H, grid_size, P)
        # ellers: fall gjennom til overlap-resolve under
    if do_grid_snap and P["GRID"] > 0:
        g = P["GRID"]
        for i in ids:
            if i not in pinned:
                pos[i] = (round(pos[i][0] / g) * g, round(pos[i][1] / g) * g)
    if do_overlap:
        fp = P["NODE_FOOTPRINT"]
        lo_x, hi_x, lo_y, hi_y = M, W - M, M, H - M
        for _ in range(P["OVERLAP_RESOLVE_ITERS"]):
            moved = False
            for x in range(len(ids)):
                for y in range(x + 1, len(ids)):
                    a, b = ids[x], ids[y]
                    a_free = a not in pinned
                    b_free = b not in pinned
                    if not a_free and not b_free:
                        continue   # to låste som overlapper: lås respekteres absolutt, rør ingen
                    ax, ay = pos[a]; bx, by = pos[b]
                    ddx = ax - bx; ddy = ay - by
                    d = math.hypot(ddx, ddy)
                    if d < fp:
                        if d < 1e-6:   # helt oppå hverandre → deterministisk retning
                            ux, uy = (1.0, 0.0) if x % 2 == 0 else (0.0, 1.0)
                            d = 0.01
                        else:
                            ux, uy = ddx / d, ddy / d
                        need = (fp - d) + 1.0
                        pa = need / 2 if (a_free and b_free) else (need if a_free else 0.0)
                        pb = need / 2 if (a_free and b_free) else (need if b_free else 0.0)
                        if a_free:
                            pos[a] = (min(hi_x, max(lo_x, ax + ux * pa)),
                                      min(hi_y, max(lo_y, ay + uy * pa)))
                        if b_free:
                            pos[b] = (min(hi_x, max(lo_x, bx - ux * pb)),
                                      min(hi_y, max(lo_y, by - uy * pb)))
                        moved = True
            if not moved:
                break
    for i in ids:
        if i not in pinned:
            pos[i] = (max(M, min(W - M, pos[i][0])), max(M, min(H - M, pos[i][1])))
    return {i: (int(round(pos[i][0])), int(round(pos[i][1]))) for i in ids}


def enforce_no_overlap(positions, pinned, width, height, grid_size=0, params=None):
    """INVARIANT (carry-over fra gammel kode): to objekter lander ALDRI oppå hverandre — overlappende
    noder frastøtes (eller får distinkt grid-celle når grid>0). Pinnede/låste står fast. Brukes der
    plasseringen IKKE allerede har gått gjennom `_finalize_positions` (f.eks. eksakt-GPS-grenen), så
    invarianten gjelder i ALLE plasserings-veier. `positions`: {id:(x,y)}. Returnerer {id:(int x,int y)}."""
    P = dict(DEFAULTS); P.update(params or {})
    work = {k: [float(v[0]), float(v[1])] for k, v in positions.items()}
    ids = sorted(work, key=str)
    return _finalize_positions(work, ids, dict(pinned or {}), float(width), float(height),
                               P, grid_size, do_grid_snap=bool(grid_size and grid_size > 0), do_overlap=True)


def improve_layout(node_ids, links, width, height, start_pos, pinned=None, params=None, grid_size=0):
    """FORBEDRE-modus (G-227): foredle DAGENS plassering (`start_pos`) uten å bygge på nytt.
    Node-nivå FR-relaksering fra gjeldende posisjoner → objektene plasseres bedre relativt hverandre
    (færre kryssende lenker, jevnere avstand), så swap-optimering (færre kryssinger) + fyll-plassen.
    Bruker IKKE GPS, re-clustrer IKKE. Låste/pinnede står absolutt fast. Returnerer ({id:(x,y)}, quality)."""
    P = dict(DEFAULTS); P.update(params or {})
    pinned = {k: v for k, v in (pinned or {}).items()}
    ids = sorted(node_ids, key=str)
    n = len(ids)
    W, H = float(width), float(height)
    M = P["CANVAS_MARGIN"]
    lo_x, hi_x, lo_y, hi_y = M, W - M, M, H - M
    cx, cy = W / 2.0, H / 2.0
    if n == 0:
        return {}, {"nodes": 0, "overlap": 0}
    adj = {i: set() for i in ids}
    elist = []; seen_e = set()
    for a, b in links:
        if a in adj and b in adj and a != b:
            adj[a].add(b); adj[b].add(a)
            key = (a, b) if str(a) <= str(b) else (b, a)
            if key not in seen_e:
                seen_e.add(key); elist.append(key)
    pos = {i: [float(start_pos.get(i, (cx, cy))[0]), float(start_pos.get(i, (cx, cy))[1])] for i in ids}
    for i in pinned:
        if i in pos:
            pos[i] = [float(pinned[i][0]), float(pinned[i][1])]
    if n >= 2:
        # node-nivå Fruchterman-Reingold-relaksering FRA gjeldende posisjoner (ikke tilfeldig start)
        k = P["IDEAL_EDGE_C"] * math.sqrt(max(1.0, (hi_x - lo_x) * (hi_y - lo_y) / max(1, n)))
        iters = P["FR_ITERATIONS_META"]
        temp0 = P["FR_TEMP_FRACTION"] * min(W, H)
        for it in range(iters):
            temp = temp0 * (1 - it / iters)
            disp = {i: [0.0, 0.0] for i in ids}
            for x in range(n):
                for y in range(n):
                    if x == y:
                        continue
                    a, b = ids[x], ids[y]
                    dx = pos[a][0] - pos[b][0]; dy = pos[a][1] - pos[b][1]
                    d = math.hypot(dx, dy) or 1.0
                    f = k * k / d
                    disp[a][0] += dx / d * f; disp[a][1] += dy / d * f
            for a, b in elist:
                dx = pos[a][0] - pos[b][0]; dy = pos[a][1] - pos[b][1]
                d = math.hypot(dx, dy) or 1.0
                f = d * d / k
                disp[a][0] -= dx / d * f; disp[a][1] -= dy / d * f
                disp[b][0] += dx / d * f; disp[b][1] += dy / d * f
            for i in ids:
                disp[i][0] += P["W_GRAVITY"] * (cx - pos[i][0])
                disp[i][1] += P["W_GRAVITY"] * (cy - pos[i][1])
            for i in ids:
                if i in pinned:
                    continue
                dl = math.hypot(disp[i][0], disp[i][1]) or 1.0
                step = min(dl, temp)
                pos[i][0] = min(hi_x, max(lo_x, pos[i][0] + disp[i][0] / dl * step))
                pos[i][1] = min(hi_y, max(lo_y, pos[i][1] + disp[i][1] / dl * step))
    post = {i: (pos[i][0], pos[i][1]) for i in ids}
    # færre kryssinger (bytt node-plasser) + bruk plassen bedre (kun når ingenting er pinnet)
    # Same node cap as the passes below: the swap pass is O(passes·N²·E) and runs in the request.
    if n - len(pinned) >= 2 and n <= 70:
        _swap_optimize(post, elist, ids, set(pinned), passes=P["SWAP_PASSES"])
    # G-256: SAMME oppskrift som Auto — jevn fordeling (frastøtning) + spre til skjerm FØRST, så flytt
    # pendant-noder inn i den nå-åpne rene plassen. Uten dette klumpet «Forbedre» og lot dist-east stå.
    _uedges = sorted({(min(a, b), max(a, b)) for a, b in elist if a != b})
    _fp = P["NODE_FOOTPRINT"]
    if n - len(pinned) >= 2 and n <= 70:
        _relax_global(post, _uedges, ids, set(pinned), _fp, W, H)
    if not pinned and n >= 2:
        _scale_to_fill(post, ids, W, H, P)
    if n - len(pinned) >= 2 and n <= 70:
        _relocate_for_crossings(post, _uedges, ids, set(pinned), _fp, W, H)
    out = _finalize_positions(post, ids, pinned, W, H, P, grid_size)
    # G-256: garanti — ingen node oppå en lenke (Forbedre respekterer låste; de står fast som ankre).
    if _uedges:
        work = {k: [float(v[0]), float(v[1])] for k, v in out.items()}
        _push_off_links(work, _uedges, ids, set(pinned), _fp, W, H)
        out = {k: (int(v[0]), int(v[1])) for k, v in work.items()}
    q = quality(out, elist, W, H, P)
    q.update({"overlap": q["overlaps"], "placed": len(out), "mode": "improve",
              "grid": bool(grid_size and grid_size > 0)})
    return out, q


def smart_layout(nodes, links, width, height, seed=1, pinned=None, gps=None, toggles=None,
                 params=None, grid_size=0, ghost_ids=None, do_relocate=True):
    """nodes: [{id, site, role}] · links: [(a_id, b_id)] · gps: {id:(lat,lon)} · pinned: {id:(x,y)}.
    ghost_ids: noder uten egen identitet hos oss (G-231) — KJENTE noder plasseres autoritativt først,
    ghosts settler rundt dem. Returnerer ({id:(x,y)}, quality_dict). Deterministisk for samme input."""
    P = dict(DEFAULTS); P.update(params or {})
    T = _toggles(toggles)
    pinned = pinned or {}
    gps = gps or {}
    rng = _random.Random(seed)
    W, H = float(width), float(height)
    M = P["CANVAS_MARGIN"]
    ids = sorted(n["id"] for n in nodes)
    if not ids:
        return {}, {"overlap": 0, "nodes": 0}
    ninfo = {n["id"]: n for n in nodes}
    adj = {i: set() for i in ids}
    elist = []
    seen_e = set()
    for a, b in links:
        if a in adj and b in adj and a != b:
            adj[a].add(b); adj[b].add(a)
            key = (a, b) if str(a) <= str(b) else (b, a)
            if key not in seen_e:
                seen_e.add(key); elist.append(key)
    # G-238: tolk hostnavn én gang → (lokasjon, rolle-tier). Hint til gruppering + rekkefølge.
    nmeta = {i: _parse_name(ninfo[i].get("name")) for i in ids}
    has_sites = any(ninfo[i].get("site") for i in ids)

    # --- G-231: plasserings-forrang — kjente noder ankres FØRST, ghosts settler rundt dem ----
    gh = set(ghost_ids or []) & set(ids)
    known_ids = [i for i in ids if i not in gh]
    if gh and known_ids:
        known_nodes = [ninfo[i] for i in known_ids]
        kset = set(known_ids)
        known_links = [(a, b) for a, b in links if a in kset and b in kset]
        # Fase A: plasser KJENTE autoritativt via full motor (samme grid)
        kpos, _ = smart_layout(known_nodes, known_links, width, height, seed=seed,
                               pinned={k: v for k, v in pinned.items() if k in kset},
                               gps=gps, toggles=toggles, params=params, grid_size=grid_size)
        # Fase B: plasser ghosts TETT rundt sin kjente anker (ring), ikke FR-spredt utover.
        # Anker = sentroide av tilkoblede KJENTE naboer (ellers senter). Flere ghosts på samme
        # anker fordeles i en ring; overlap-resolve rydder med kjente FASTE (ghosts viker).
        ring = max(P["MIN_RING_RADIUS"], P["NODE_FOOTPRINT"] + 10)
        pos = {k: (float(v[0]), float(v[1])) for k, v in kpos.items()}
        by_anchor = {}
        for gi in sorted(gh, key=str):
            kn = [k for k in adj[gi] if k in kpos]
            if kn:
                ax = sum(kpos[k][0] for k in kn) / len(kn); ay = sum(kpos[k][1] for k in kn) / len(kn)
            else:
                ax, ay = W / 2.0, H / 2.0
            by_anchor.setdefault((round(ax, 1), round(ay, 1)), []).append(gi)
        for (ax, ay), gl in sorted(by_anchor.items()):
            for idx, gi in enumerate(sorted(gl, key=str)):
                ang = 2 * math.pi * idx / max(1, len(gl))
                pos[gi] = (ax + ring * math.cos(ang), ay + ring * math.sin(ang))
        # G-239: ghosts ble klumpet i tette ringer + lenkene deres overlappet (oversett tidligere).
        # Spre HELE layouten til å fylle lerretet (uniformt → bevarer arrangement). Kjente «pinnes»
        # på sin (evt. skalerte) posisjon; ghosts viker.
        kset_fixed = set(kset)
        if not pinned and len(ids) >= 2:
            _scale_to_fill(pos, ids, W, H, P)
        pin_b = dict(pinned)
        pin_b.update({k: pos[k] for k in kset_fixed})
        out = _finalize_positions(pos, ids, pin_b, W, H, P, grid_size)
        # Kryssings-reduksjon PÅ de endelige posisjonene (etter finalize) — bytte ghost-posisjoner
        # (kjente FASTE) bevarer grid/ingen-overlapp og kutter kryssende/overlappende ghost-lenker.
        if len(gh) >= 2 and len(ids) <= 70:
            work = {k: list(v) for k, v in out.items()}
            _swap_reduce_crossings(work, elist, ids, set(pinned) | kset_fixed, P["NODE_FOOTPRINT"])
            out = {k: (int(v[0]), int(v[1])) for k, v in work.items()}
        q = quality(out, elist, W, H, P)
        q.update({"overlap": q["overlaps"], "ghosts": len(gh), "known": len(known_ids),
                  "placed": len(out), "grid": bool(grid_size and grid_size > 0),
                  "crossings": q.get("crossings"), "link_node": q.get("link_node_crossings")})
        return out, q

    # --- Pass 1: clustering (G-238) ----------------------------------------
    # «Hører sammen»-signal, i prioritert rekkefølge: (1) NetBox-site (autoritativ),
    # (2) navn-lokasjons-token ('bergen-...'), (3) KONNEKTIVITET — en node uten site/navn-lokasjon
    # arver gruppen til sin DOMINANTE grupperte nabo (høyest grad), så f.eks. mange 'sw' som henger
    # på samme 'reX' havner i samme gruppe (Roger 2026-06-21). Rester → solo.
    if T["cluster_by_site"]:
        gkey = {}
        for i in ids:
            site = ninfo[i].get("site")
            if site:
                gkey[i] = ("site", str(site).strip().lower())
                continue
            loc = nmeta[i][0]
            gkey[i] = ("name", loc) if loc else None
        # konnektivitets-propagasjon (deterministisk, sortert) — flere runder så kjeder sw→reX→site fester
        for _ in range(6):
            changed = False
            for i in sorted(ids, key=str):
                if gkey[i] is not None:
                    continue
                cands = [j for j in adj[i] if gkey.get(j) is not None]
                if cands:
                    gkey[i] = gkey[max(cands, key=lambda j: (len(adj[j]), str(j)))]
                    changed = True
            if not changed:
                break
        clusters = {}
        for i in ids:
            k = gkey[i]
            ckey = ("%s:%s" % k) if k else f"__solo_{i}"
            clusters.setdefault(ckey, []).append(i)
    else:
        clusters = {f"__solo_{i}": [i] for i in ids}
    for c in clusters:
        clusters[c].sort()
    ckeys = sorted(clusters)

    # cardinal pr. cluster (kun ekte site/navn-lokasjon, ikke solo). Strip «site:»/«name:»-prefiks
    # før cardinal-tolkning (nøkkelen er nå «site:<navn>» / «name:<lokasjon>»).
    cardinal = {}
    if T["cardinal_anchors"]:
        for c in ckeys:
            if c.startswith("__solo_"):
                continue
            cd = _cardinal_of(c.split(":", 1)[1] if ":" in c else c)
            if cd:
                cardinal[c] = cd

    # hub = ikke-cardinal cluster med flest distinkte nabo-clustre
    hub = None
    if T["hub_center"]:
        node_cluster = {i: c for c in ckeys for i in clusters[c]}
        meta_deg = {}
        for c in ckeys:
            if c in cardinal:
                continue
            others = set()
            for i in clusters[c]:
                for j in adj[i]:
                    if node_cluster[j] != c:
                        others.add(node_cluster[j])
            meta_deg[c] = len(others)
        if meta_deg:
            hub = max(sorted(meta_deg), key=lambda c: (meta_deg[c], c))

    # redundans-par (Jaccard på full nabo-mengde) innen cluster
    redundancy = {}
    if T["redundancy_pairs"]:
        for c in ckeys:
            mem = clusters[c]
            used = set()
            for x in range(len(mem)):
                for y in range(x + 1, len(mem)):
                    a, b = mem[x], mem[y]
                    if a in used or b in used:
                        continue
                    ua, ub = adj[a], adj[b]
                    uni = ua | ub
                    j = (len(ua & ub) / len(uni)) if uni else 0.0
                    if j >= P["JACCARD_REDUNDANCY"]:
                        redundancy[a] = b; redundancy[b] = a
                        used.add(a); used.add(b)

    # --- Pass 2: cluster-sentre -------------------------------------------
    ccenter = {}
    # G-241: GPS-seed = INITIELL plassering av cluster-sentre fra GPS (steg 1). IKKE en lås og IKKE
    # en egen modus: de påfølgende passene (meta-FR, skalering, swap) kjører like fullt og står fritt
    # til å flytte. `seeded` brukes nå KUN til informasjon i kvalitets-rapporten, ikke til å greine
    # algoritmen. Eneste lås er manuell pinning (`pinned` = operatørens «lås posisjon»).
    seeded = False
    if T["gps_seed"] and gps:
        cl_gps = {}
        for c in ckeys:
            pts = [gps[i] for i in clusters[c] if i in gps and gps[i][0] is not None]
            if pts:
                cl_gps[c] = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        if cl_gps:
            ccenter = geo.relative_layout(cl_gps, int(W), int(H), margin=max(M, 80))
            seeded = True
    # ankre / tilfeldig start for clustre uten GPS-seed
    for c in ckeys:
        if c in ccenter:
            continue
        if c in cardinal:
            dx, dy = cardinal[c]
            cx = W / 2 + dx * (W / 2 - P["CARDINAL_MARGIN"])
            cy = H / 2 + dy * (H / 2 - P["CARDINAL_MARGIN"])
            ccenter[c] = (cx, cy)
        elif c == hub:
            ccenter[c] = (W / 2, H / 2)
        elif clusters[c] and all(i in pinned for i in clusters[c]):
            xs = [pinned[i][0] for i in clusters[c]]; ys = [pinned[i][1] for i in clusters[c]]
            ccenter[c] = (sum(xs) / len(xs), sum(ys) / len(ys))
        else:
            ccenter[c] = (rng.uniform(M, W - M), rng.uniform(M, H - M))

    # meta-FR (G-241: kjøres ALLTID — GPS-seedet er kun startposisjon, ikke en lås på geografien;
    # FR starter fra GPS-sentrene og finpusser, og blir gjerne værende i nærheten av starten)
    if len(ckeys) > 1:
        # anker-sett (cardinal+hub) holdes mykt på plass
        anchors = set(cardinal) | ({hub} if hub else set())
        meta_edges = {}
        node_cluster = {i: c for c in ckeys for i in clusters[c]}
        for a, b in links:
            if a in node_cluster and b in node_cluster:
                ca, cb = node_cluster[a], node_cluster[b]
                if ca != cb:
                    meta_edges[(min(ca, cb), max(ca, cb))] = meta_edges.get((min(ca, cb), max(ca, cb)), 0) + 1
        k = P["IDEAL_EDGE_C"] * math.sqrt(max(1.0, (W - 2 * M) * (H - 2 * M) / max(1, len(ckeys))))
        temp = P["FR_TEMP_FRACTION"] * min(W, H)
        for it in range(P["FR_ITERATIONS_META"]):
            disp = {c: [0.0, 0.0] for c in ckeys}
            for a in ckeys:
                for b in ckeys:
                    if a == b:
                        continue
                    ddx = ccenter[a][0] - ccenter[b][0]; ddy = ccenter[a][1] - ccenter[b][1]
                    d = math.hypot(ddx, ddy) or 0.01
                    f = k * k / d
                    disp[a][0] += ddx / d * f; disp[a][1] += ddy / d * f
            for (ca, cb), w in sorted(meta_edges.items()):
                ddx = ccenter[ca][0] - ccenter[cb][0]; ddy = ccenter[ca][1] - ccenter[cb][1]
                d = math.hypot(ddx, ddy) or 0.01
                f = w * d * d / k
                disp[ca][0] -= ddx / d * f; disp[ca][1] -= ddy / d * f
                disp[cb][0] += ddx / d * f; disp[cb][1] += ddy / d * f
            for c in ckeys:
                gx = W / 2 - ccenter[c][0]; gy = H / 2 - ccenter[c][1]
                disp[c][0] += gx * P["W_GRAVITY"]; disp[c][1] += gy * P["W_GRAVITY"]
                if c in anchors:
                    ax, ay = (W / 2, H / 2) if c == hub else (
                        W / 2 + cardinal[c][0] * (W / 2 - P["CARDINAL_MARGIN"]),
                        H / 2 + cardinal[c][1] * (H / 2 - P["CARDINAL_MARGIN"]))
                    disp[c][0] += (ax - ccenter[c][0]) * P["W_CARDINAL"] * 0.05
                    disp[c][1] += (ay - ccenter[c][1]) * P["W_CARDINAL"] * 0.05
            for c in ckeys:
                dx, dy = disp[c]; dd = math.hypot(dx, dy) or 0.01
                step = min(dd, temp)
                x = ccenter[c][0] + dx / dd * step
                y = ccenter[c][1] + dy / dd * step
                ccenter[c] = (max(M, min(W - M, x)), max(M, min(H - M, y)))
            temp = max(0.0, temp - (P["FR_TEMP_FRACTION"] * min(W, H)) / P["FR_ITERATIONS_META"])

    # Låste noder er autoritative ankre (Roger): har et cluster låste medlemmer, overstyres
    # cluster-senteret til de låstes centroide — uansett GPS-seed/FR — så de bevegelige medlemmene
    # arrangeres rundt der låsen FAKTISK står. (Selve de låste settes til eksakt posisjon i l.~290.)
    for c in ckeys:
        locked = [pinned[i] for i in clusters[c] if i in pinned]
        if locked:
            ccenter[c] = (sum(p[0] for p in locked) / len(locked),
                          sum(p[1] for p in locked) / len(locked))

    # --- Pass 3: intra-cluster --------------------------------------------
    node_cluster = {i: c for c in ckeys for i in clusters[c]}   # G-238: sikret (brukes i bæring-sort)
    pos = {}
    for c in ckeys:
        mem = clusters[c]
        cx, cy = ccenter[c]
        if len(mem) == 1:
            pos[mem[0]] = (cx, cy); continue
        if not T["intra_cluster"]:
            # enkel sirkel rundt senteret
            R = max(P["MIN_RING_RADIUS"], P["NODE_FOOTPRINT"] * len(mem) / (2 * math.pi))
            for idx, i in enumerate(mem):
                ang = 2 * math.pi * idx / len(mem)
                pos[i] = (cx + R * math.cos(ang), cy + R * math.sin(ang))
            continue
        internal = sum(1 for i in mem for j in adj[i] if j in mem) / 2
        density = internal / max(1, len(mem) - 1)
        use_radial = len(mem) <= P["RADIAL_MAX_NODES"] or density > P["MESH_EDGE_DENSITY_THRESHOLD"]
        if use_radial:
            memset = set(mem)
            deg = {i: len(adj[i] & memset) for i in mem}
            center_node = max(sorted(mem), key=lambda i: (deg[i], -ids.index(i)))
            ring = [i for i in mem if i != center_node]
            # G-238: bæring-sortering (INIT §8.3.3) — ring-node settes mot retningen til sine EKSTERNE
            # naboer (andre clustres sentre), så inter-cluster-lenker peker «utover» og krysser mindre.
            def _bearing(i):
                vx = vy = 0.0
                for j in adj[i]:
                    if j not in memset and j in node_cluster:
                        cc = ccenter[node_cluster[j]]
                        vx += cc[0] - cx; vy += cc[1] - cy
                return math.atan2(vy, vx) if (vx or vy) else None
            bears = {i: _bearing(i) for i in ring}
            # noder uten ekstern nabo plasseres etter id (stabilt); ellers etter bæring
            ring_sorted = sorted(ring, key=lambda i: (0, bears[i], str(i)) if bears[i] is not None
                                  else (1, 0.0, str(i)))
            # redundans-par holdes ved siden av hverandre (bevarer bæring-rekkefølgen ellers)
            ordered = []
            seen = set()
            for i in ring_sorted:
                if i in seen:
                    continue
                ordered.append(i); seen.add(i)
                p = redundancy.get(i)
                if p and p in memset and p != center_node and p not in seen:
                    ordered.append(p); seen.add(p)
            R = max(P["MIN_RING_RADIUS"], P["NODE_FOOTPRINT"] * len(ring) / (2 * math.pi))
            pos[center_node] = (cx, cy)
            for idx, i in enumerate(ordered):
                ang = 2 * math.pi * idx / max(1, len(ordered))
                pos[i] = (cx + R * math.cos(ang), cy + R * math.sin(ang))
        else:
            # hierarkisk: rolle-tier ovenfra-ned + BARYCENTER-sweeps for x-rekkefølge (INIT §8.4 —
            # G-238: faktisk implementert nå; kutter kryssinger mellom nivåer). Rolle-tier fra HOSTNAVN
            # (core<dist/aggr<re<sw<edge) som primær, ellers interface-rolle — kun MYKT hint.
            def tier(i):
                t = nmeta[i][1]
                if t is not None:
                    return t
                r = (ninfo[i].get("role") or "other")
                return ROLE_TIERS.index(r) if r in ROLE_TIERS else len(ROLE_TIERS)
            tiers = {}
            for i in mem:
                tiers.setdefault(tier(i), []).append(i)
            tk = sorted(tiers)
            order = {t: sorted(tiers[t]) for t in tk}
            box_w = max(P["NODE_FOOTPRINT"] * 2, P["NODE_FOOTPRINT"] * max(len(v) for v in tiers.values()))
            # G-239: HALVERT rad-avstand (Roger) — tettere nivåer, mindre «alt på linje».
            row_gap = P["NODE_FOOTPRINT"] * 0.5
            box_h = max(row_gap, row_gap * (len(tk) - 1))

            def _xs(row):
                n_in = len(row)
                return {node: (cx - box_w / 2 + box_w * xi / max(1, n_in - 1)) if n_in > 1 else cx
                        for xi, node in enumerate(row)}
            tier_of = {i: tier(i) for i in mem}
            # Barycenter-sweeps: reordne hvert nivå etter snitt-x av naboer i ANDRE nivåer (kryssings-kutt)
            for sweep in range(P["BARYCENTER_SWEEPS"]):
                xof = {}
                for t in tk:
                    xof.update(_xs(order[t]))
                for t in (tk if sweep % 2 == 0 else list(reversed(tk))):
                    def bary(node):
                        nb = [xof[j] for j in adj[node] if j in mem and tier_of[j] != t and j in xof]
                        return (sum(nb) / len(nb)) if nb else xof[node]
                    order[t] = sorted(order[t], key=lambda node: (bary(node), str(node)))
                    xof.update(_xs(order[t]))
            # x pr. rad + start-y pr. nivå
            memset = set(mem)
            xpos_all = {}
            for t in tk:
                xpos_all.update(_xs(order[t]))
            tk_rank = {t: r for r, t in enumerate(tk)}
            ypos = {i: cy - box_h / 2 + box_h * tk_rank[tier_of[i]] / max(1, len(tk) - 1) for i in mem}
            # G-239: LA noder gli vertikalt mot naboenes snitt-y (kortere lenker, ikke stivt på raden —
            # en node m/ kort uplink kan havne alene lavere). Deterministisk, noen runder, dempet.
            for _ in range(3):
                ny = {}
                for i in mem:
                    nb = [ypos[j] for j in adj[i] if j in memset]
                    ny[i] = ypos[i] + (sum(nb) / len(nb) - ypos[i]) * 0.4 if nb else ypos[i]
                ypos = ny
            for i in mem:
                pos[i] = (xpos_all[i], ypos[i])

    # pinnede noder overstyrer (absolutte ankre — settes til eksakt låst posisjon)
    for i in ids:
        if i in pinned:
            pos[i] = (float(pinned[i][0]), float(pinned[i][1]))

    # G-256 — REKKEFØLGE (Roger): LØS ROM-PROBLEMET FØRST, så blir den rene plassen for en pendant-node
    # åpenbar, og DA flytter vi den dit. (Tung kraft-relaksering droppet — den klumpet + var treg; den
    # uniforme spre-til-skjerm gir rommet billigere, og relokeringen gjør jobben med kryssingene.)
    _uedges = sorted({(min(a, b), max(a, b)) for a, b in elist if a != b})
    _fp = P["NODE_FOOTPRINT"]
    # geografisk mål-orientering (nord opp) — brukes som svakt hint (relax) + tie-breaker (orient).
    _geo_t = None
    if gps:
        _gp = {i: gps[i] for i in ids if i in gps and gps[i] and gps[i][0] is not None}
        if len(_gp) >= 2:
            _geo_t = geo.relative_layout(_gp, int(W), int(H))
    # Hele blokka under (jevn-fordeling + relokering + orientering) kjøres KUN på vinner-seeden → raskt.
    if do_relocate and len(ids) - len(pinned) >= 2 and len(ids) <= 70:
        # (a) JEVN FORDELING: ren balanse-spредning (INGEN geografi her — den kommer kun inn via flipp-
        #     orienteringen i steg (d), så kreftene ikke motarbeider hverandre).
        _relax_global(pos, _uedges, ids, set(pinned), _fp, W, H, geo_target=None)
    # (b) fit til lerret — uniform skalering (kryssings-invariant), sentrert.
    if not pinned and len(ids) >= 2:
        _scale_to_fill(pos, ids, W, H, P)
        if not has_sites and len(ids) <= 70:   # swap bytter geografisk plass → KUN trygt på pure topologi
            _swap_optimize(pos, elist, ids, set(pinned), passes=P["SWAP_PASSES"])
    # (c) flytt pendant-noder (f.eks. dist-east) inn i den nå-åpenbare rene plassen — KUN på vinneren.
    if do_relocate and len(ids) - len(pinned) >= 2 and len(ids) <= 70:
        _relocate_for_crossings(pos, _uedges, ids, set(pinned), _fp, W, H)
    # (d) ORIENTER: stiv flipp/rotasjon så orienteringen matcher geografien (Tromsø nord osv.). Gratis —
    #     bevarer kryssinger OG balanse (flipp permuterer kun kvadranter). Velger blant likeverdige
    #     layouts (Oslo oppe vs nede) den som matcher GPS best. KUN presentasjonen, aldri bakgrunn.
    if do_relocate and _geo_t and not pinned and len(ids) >= 2:
        _orient_to_geography(pos, _geo_t, ids)
        _scale_to_fill(pos, ids, W, H, P)   # refit lerret etter rotasjon

    # --- Pass 4: finalisering (delt helper — samme overlap/grid-slutt for alle modi) -----
    out = _finalize_positions(pos, ids, pinned, W, H, P, grid_size,
                              do_grid_snap=T["grid_snap"], do_overlap=T["overlap_resolve"])

    # (G-256-forenkling: fjernet det gamle, dyre _swap_reduce_crossings-passet (G-239) — det kjørte i
    # ALLE smart_layout-kall (også de 4 seed-utvelgelsene) og var O(passes·M²·E·N) ⇒ ~80% av tiden.
    # Den nye relokeringen (c) håndterer kryssinger raskere og kun på vinner-seeden.)

    # kvalitet: merget quality() (overlaps/min_dist/util/edge_len_cv) + struktur-felt
    q = quality(out, elist, W, H, P)
    q.update({"overlap": q["overlaps"], "clusters": len(ckeys), "seeded_gps": seeded,
              "hub": hub, "placed": len(out), "grid": bool(grid_size and grid_size > 0)})
    return out, q


def best_smart_layout(nodes, links, width, height, seeds, pinned=None, gps=None, toggles=None,
                      params=None, grid_size=0, ghost_ids=None):
    """G-239 (INIT-63 §13): kjør smart_layout for flere seeds og returner (pos, q) med LAVEST Q.
    Slik UNNGÅR motoren aktivt det Q straffer — særlig **link-gjennom-node** (tyngst vektet) og
    link-link-kryssinger. Deterministisk for en fast seed-liste (Auto), variert for tilfeldig (Nytt).
    `seeds`: iterable av frø. Returnerer beste (pos, q)."""
    # Velg beste seed på det BILLIGE kartet (uten den dyre relokeringen), kjør så relokeringen ÉN gang
    # på vinneren (Roger: raskeste vei til målet trenger ikke være den teoretisk optimale).
    best_seed, best_q = None, None
    for s in seeds:
        _, q = smart_layout(nodes, links, width, height, seed=s, pinned=pinned, gps=gps,
                            toggles=toggles, params=params, grid_size=grid_size, ghost_ids=ghost_ids,
                            do_relocate=False)
        if best_q is None or q.get("Q", 1e18) < best_q.get("Q", 1e18):
            best_seed, best_q = s, q
    if best_seed is None:
        return ({}, {"nodes": 0})
    return smart_layout(nodes, links, width, height, seed=best_seed, pinned=pinned, gps=gps,
                        toggles=toggles, params=params, grid_size=grid_size, ghost_ids=ghost_ids,
                        do_relocate=True)


def tier_layout(ids, edges, names, width, height, gps=None, seed=1, pinned=None):
    """G-256 — TIER-BASERT INKREMENTELL PLASSERING (Roger 2026-06-22). Motoren kan ingenting om nett:
    den får en tabell med routere og sprer dem PENT på et kart. Ingen gravitasjon — alt SPRES utover.
    Rekkefølge: (1) tier fra navn, (2) GPS-seed, (3) spre tier0/1-ryggrad + sett SENTER-ANKER (mest-koblede
    backbone-node soft-låst i midtruten), (4) nest resten tier-for-tier (flest lenker først) ved naboenes
    barycenter + lokal frastøting, (5) løs kryssinger (flytt kryssings-noder), (6) flipp mot geografi,
    (7) spre-for-luft + re-relokér, (8) overlapp-resolve + klamp. Eneste klumping = GPS site-nivå.
    Returnerer {id:(x,y)}. UTKAST — kjøres ved siden av smart_layout til den er validert."""
    if not ids:
        return {}          # tomt kart («Prøv på nytt» etter at alle noder er fjernet) — speiler smart_layout
    W, H = float(width), float(height)
    rng = _random.Random(seed)
    P = dict(DEFAULTS)
    fp = P["NODE_FOOTPRINT"]
    uedges = sorted({(min(a, b), max(a, b)) for a, b in edges if a != b})
    adj = {i: [] for i in ids}
    for a, b in uedges:
        adj[a].append(b); adj[b].append(a)
    deg = {i: len(adj[i]) for i in ids}
    tier = {i: _name_tier(names.get(i)) for i in ids}
    gps = {i: gps[i] for i in (gps or {}) if gps[i] and gps[i][0] is not None}
    gpx = geo.relative_layout(gps, int(W), int(H)) if len(gps) >= 2 else {}
    cores = [i for i in ids if tier[i] <= 1]
    coreset = set(cores)
    # LÅSTE noder (manuell «lås posisjon») = absolutte ankre: plasseres på sin låste posisjon og holdes
    # FAST gjennom alle pass (fixed). Da respekterer Nytt/tier_layout låser nativt (Roger 2026-06-22).
    idset = set(ids)
    pin = {i: (float(pinned[i][0]), float(pinned[i][1])) for i in (pinned or {})
           if i in idset and pinned[i] and pinned[i][0] is not None}
    pinset = set(pin)
    pos = dict(pin)

    def repel(group, iters, X, fixed=()):
        """Roger: «bruk kun frastøting og pass på at alt er minst X fra hverandre». Dytt par som er
        NÆRMERE enn X fra hverandre; X stor (~fyll-avstand) → de tvinges til å fordele seg utover HELE
        lerretet. Senter-ankeret holdes fast. Bundet til lerret."""
        for _ in range(iters):
            for v in group:
                if v in fixed:
                    continue
                dx = dy = 0.0
                for u in group:
                    if u == v:
                        continue
                    ddx = pos[v][0] - pos[u][0]; ddy = pos[v][1] - pos[u][1]
                    d = math.hypot(ddx, ddy) or 0.01
                    if d < X:
                        f = (X - d) * 0.5
                        dx += ddx / d * f; dy += ddy / d * f
                pos[v] = (min(W, max(0.0, pos[v][0] + dx * 0.35)),
                          min(H, max(0.0, pos[v][1] + dy * 0.35)))

    # (3) ryggrad: senter-anker (mest core-koblede) i midten, resten spредt rundt (GPS-seed), ingen gravitasjon.
    # Senter-anker forses KUN når ingenting er låst (er noe låst, gir låsene allerede ankringen).
    anchor = None
    if cores:
        if not pin:
            cdeg = {i: sum(1 for j in adj[i] if j in coreset) for i in cores}
            anchor = max(cores, key=lambda i: (cdeg[i], deg[i]))
            pos[anchor] = (W / 2, H / 2)
        for i in cores:
            if i in pos:   # låst eller anker — alt plassert
                continue
            pos[i] = gpx.get(i, (W / 2 + rng.uniform(-250, 250), H / 2 + rng.uniform(-180, 180)))
        ctarget = max(fp * 2.0, (W * H / max(2, len(cores))) ** 0.5)   # ryggrad spres VIDT
        repel(cores, 120, ctarget, fixed=pinset | ({anchor} if anchor else set()))

    # (4) nest resten tier-for-tier (flest lenker først) ved naboenes barycenter + lokal frastøting
    rest = sorted([i for i in ids if i not in pos], key=lambda i: (tier[i], -deg[i]))
    k = (W * H / max(2, len(ids))) ** 0.5
    for v in rest:
        nb = [n for n in adj[v] if n in pos]
        if nb:
            pos[v] = (sum(pos[n][0] for n in nb) / len(nb), sum(pos[n][1] for n in nb) / len(nb))
        else:
            pos[v] = gpx.get(v, (W / 2, H / 2))
        # lokal frastøting mot alle alt-plasserte + svak trekk mot egne naboer (hold sammen, ikke graviter)
        for _ in range(40):
            dx = dy = 0.0
            for u in pos:
                if u == v:
                    continue
                ddx = pos[v][0] - pos[u][0]; ddy = pos[v][1] - pos[u][1]
                d = math.hypot(ddx, ddy) or 0.01
                if d < k:
                    f = (k - d) * 0.6; dx += ddx / d * f; dy += ddy / d * f
            if nb:
                bx = sum(pos[n][0] for n in nb) / len(nb); by = sum(pos[n][1] for n in nb) / len(nb)
                dx += 0.15 * (bx - pos[v][0]); dy += 0.15 * (by - pos[v][1])
            pos[v] = (min(W, max(0.0, pos[v][0] + dx * 0.3)), min(H, max(0.0, pos[v][1] + dy * 0.3)))

    # (5) løs kryssinger (låste står fast)
    _relocate_for_crossings(pos, uedges, ids, pinset, fp, W, H)
    # (6) flipp mot geografi (stiv — kryssinger invariante). HOPPES OVER hvis noe er låst (en flipp ville
    # flyttet de låste nodene fra sin posisjon).
    if gpx and not pin:
        _orient_to_geography(pos, gpx, ids)
    # (7) SPRE UTOVER for å FYLLE arealet (Roger: ikke klump → fordel utover → rom → flytt flere noder →
    # vekk med kryssinger). Mål = FYLL-avstanden √(areal/antall). Re-relokér mellom rundene. Låste fast.
    fill = (W * H / max(2, len(ids))) ** 0.5
    for _ in range(4):
        repel(ids, 50, fill, fixed=pinset)
        _relocate_for_crossings(pos, uedges, ids, pinset, fp, W, H)
    # (8) fyll + overlapp-resolve + klamp. Skalér-til-fyll hoppes over ved låsing (ville flyttet de låste).
    if not pin:
        _scale_to_fill(pos, ids, W, H, P)
    out = _finalize_positions(pos, ids, pin, W, H, P, 0)
    # (9) GARANTI: ingen node oppå en lenke (lenke-gjennom-node = forbudt; heller link-link-kryssing). Låste fast.
    work = {k: [float(v[0]), float(v[1])] for k, v in out.items()}
    _push_off_links(work, uedges, ids, pinset, fp, W, H)
    return {k: (int(v[0]), int(v[1])) for k, v in work.items()}
