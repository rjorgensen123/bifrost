# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.3.4
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Isolert tune-verksted for den MERGE'DE plasserings-motoren (placement.py — G-220/G-238/G-239).
Kjør HELT UTEN app/DB/Docker:
    python layout_playground.py [utmappe]
Skriver én SVG per eksempel-topologi (åpne i nettleser for å SE spredningen) og printer
kvalitetsmål (crossings, link_node_crossings, Q m.fl.). For å tune: endre `params` nederst
(dict over placement.DEFAULTS) og kjør på nytt — ingenting annet i appen påvirkes.

Bruker `placement` direkte, så ALLE motor-parametrene
(BARYCENTER_SWEEPS, MIN_RING_RADIUS, OVERLAP_RESOLVE_ITERS, …) og 4-pass-strukturen kan finjusteres.
Node-navn (f.eks. «core-oslo») mates som `name` så navn-/site-gruppering (G-238) også testes.
"""
import sys
import os
import random
import placement as P

W, H = 1200, 700


def nordic():
    nodes = ["core-oslo", "core-bergen", "core-trondheim", "core-stavanger", "core-tromso",
             "dist-north", "dist-south", "dist-east", "dist-west"]
    edges = [("core-oslo", "core-bergen"), ("core-oslo", "core-trondheim"),
             ("core-oslo", "core-stavanger"), ("core-oslo", "core-tromso"),
             ("core-bergen", "core-trondheim"), ("core-bergen", "core-stavanger"),
             ("core-trondheim", "core-tromso"),
             ("core-oslo", "dist-east"), ("core-oslo", "dist-south"),
             ("core-bergen", "dist-west"), ("core-stavanger", "dist-south"),
             ("core-stavanger", "dist-west"), ("core-trondheim", "dist-east"),
             ("core-tromso", "dist-north")]
    return "nordic", nodes, edges


def star(n=12):
    return "star", ["hub"] + [f"n{i}" for i in range(n)], [("hub", f"n{i}") for i in range(n)]


def ring(n=14):
    nodes = [f"n{i}" for i in range(n)]
    return "ring", nodes, [(f"n{i}", f"n{(i + 1) % n}") for i in range(n)]


def mesh(n=14, seed=1):
    r = random.Random(seed)
    nodes = [f"n{i}" for i in range(n)]
    edges = [(f"n{i}", f"n{j}") for i in range(n) for j in range(i + 1, n) if r.random() < 0.3]
    return "mesh", nodes, edges


def tree(depth=3, fan=3):
    # "uplink-tre" — bergen-re/sw-aktig: tester hierarki + drift + link-gjennom-node
    nodes = ["bergen-core-1"]
    edges = []
    frontier = ["bergen-core-1"]
    cnt = 0
    for d in range(depth):
        nxt = []
        for p in frontier:
            for f in range(fan):
                cnt += 1
                child = f"bergen-{'dist' if d == 0 else 'sw'}-{cnt}"
                nodes.append(child); edges.append((p, child)); nxt.append(child)
        frontier = nxt
    return "tree", nodes, edges


def disconnected(n=16):
    return "disconnected", [f"n{i}" for i in range(n)], []


SAMPLES = [nordic(), star(), ring(), mesh(), tree(), disconnected()]


def render_svg(name, positions, edges, w, h, q, path):
    e = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
         f'<rect width="{w}" height="{h}" fill="#0d132d"/>']
    for a, b in edges:
        if str(a) in positions and str(b) in positions:
            (x1, y1), (x2, y2) = positions[str(a)], positions[str(b)]
            e.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#8181f1" stroke-opacity="0.6" stroke-width="2"/>')
    for nid, (x, y) in positions.items():
        e.append(f'<circle cx="{x}" cy="{y}" r="14" fill="#1484b6" stroke="#fff" stroke-width="2"/>')
        e.append(f'<text x="{x}" y="{y + 27}" text-anchor="middle" fill="#fff" font-size="11" font-family="sans-serif">{nid}</text>')
    e.append(f'<text x="12" y="22" fill="#dfe1fe" font-size="13" font-family="monospace">'
             f'{name}  crossings={q["crossings"]}  link_node={q.get("link_node_crossings")}  '
             f'Q={q["Q"]}  overlaps={q["overlaps"]}  util={q["util"]}</text>')
    e.append('</svg>')
    open(path, "w").write("\n".join(e))


def main():
    outdir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(outdir, exist_ok=True)
    # >>> TUNE HER: kopier motorens defaults og overstyr ønskede param, f.eks.:
    #     params = {**P.DEFAULTS, "BARYCENTER_SWEEPS": 6, "MIN_RING_RADIUS": 70}
    params = dict(P.DEFAULTS)
    print(f"params: {params}")
    for name, nodes, edges in SAMPLES:
        # navn mates som `name` så navn-/site-gruppering (G-238) også testes
        nodes_rich = [{"id": n, "name": n} for n in nodes]
        pos, q = P.smart_layout(nodes_rich, edges, W, H, seed=1, params=params)
        pos = {str(k): v for k, v in pos.items()}
        out = os.path.join(outdir, f"layout-{name}.svg")
        render_svg(name, pos, edges, W, H, q, out)
        print(f"  {name:14} crossings={q['crossings']} link_node={q.get('link_node_crossings')} "
              f"Q={q['Q']} overlaps={q['overlaps']} util={q['util']}  -> {out}")


if __name__ == "__main__":
    main()
