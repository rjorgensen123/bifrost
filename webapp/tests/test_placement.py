# Bifrost. © Roger Jorgensen (rogerj@gmail.com). Last modified: v2.4.0
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CI-enhetstester for plasseringsmotorens OBJEKTIVE invarianter (`placement.tier_layout`, G-256).

Bedømmer IKKE estetikk — om kartet *ser* pent ut er felttest. Her låser vi bare ja/nei-invariantene
motoren MÅ holde, så en regresjon fanges automatisk:
  - alle noder plassert innenfor lerretet
  - ingen to noder oppå hverandre
  - ingen node oppå en lenke (G-239: «verre enn link-link-kryssing»)
  - determinisme: samme seed → samme svar, også i en annen prosess (tekst-id)

`placement` importerer kun `geo` + stdlib → testen kjører uten DB/Flask.

  cd webapp && python -m unittest discover -s tests -v
"""
import json
import math
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import placement  # noqa: E402

FP = placement.DEFAULTS["NODE_FOOTPRINT"]
W, H = 1200, 700

# Liten, spredt topologi (6 noder): 2 core + 2 dist + 2 acc, tre-aktig — rikelig plass på lerretet.
IDS = [1, 2, 3, 4, 5, 6]
NAMES = {1: "core-oslo", 2: "core-bergen", 3: "dist-1", 4: "dist-2", 5: "acc-1", 6: "acc-2"}
EDGES = [(1, 2), (1, 3), (2, 4), (3, 5), (4, 6)]


def _min_pairwise_distance(pos):
    pts = list(pos.values())
    m = float("inf")
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            m = min(m, math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]))
    return m


class TierLayoutInvariants(unittest.TestCase):
    def setUp(self):
        self.pos = placement.tier_layout(IDS, EDGES, NAMES, W, H, seed=1)

    def test_all_nodes_within_canvas(self):
        self.assertEqual(set(self.pos), set(IDS))
        for nid, (x, y) in self.pos.items():
            self.assertTrue(0 <= x <= W, f"node {nid} x={x} utenfor lerret")
            self.assertTrue(0 <= y <= H, f"node {nid} y={y} utenfor lerret")

    def test_no_two_nodes_overlap(self):
        self.assertGreaterEqual(_min_pairwise_distance(self.pos), FP * 0.5,
                                "to noder ligger for tett (overlapp)")

    def test_no_node_on_a_link(self):
        self.assertEqual(placement.count_link_node_crossings(self.pos, EDGES, FP), 0,
                         "en node ligger oppå en lenke")

    def test_deterministic_same_seed(self):
        self.assertEqual(self.pos, placement.tier_layout(IDS, EDGES, NAMES, W, H, seed=1))


# Logical maps use router ids (text) as node ids. Python randomises the hashing of text per
# process (PYTHONHASHSEED), so any set iteration over ids would make the result depend on the
# process. The same input must give the same layout in every process.
_HASHSEED_SCRIPT = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import placement
ids = ["10.0.0.%d" % i for i in range(1, 15)]
pairs = [(0,1),(1,2),(2,3),(3,0),(0,4),(4,5),(5,6),(6,1),(2,7),(7,8),(8,9),(9,3),(4,10),
         (10,11),(11,12),(12,13),(13,8),(5,11),(6,12)]
edges = [(ids[a], ids[b]) for a, b in pairs]
nodes = [{"id": i, "site": None, "role": "core"} for i in ids]
smart, _ = placement.smart_layout(nodes, edges, 1600, 1000, seed=1)
start = {i: (100 + (k % 5) * 250, 100 + (k // 5) * 250) for k, i in enumerate(ids)}
improve, _ = placement.improve_layout(ids, edges, 1600, 1000, start)
r = lambda p: sorted((k, round(v[0], 3), round(v[1], 3)) for k, v in p.items())
print(json.dumps([r(smart), r(improve)]))
"""


class SameLayoutInEveryProcess(unittest.TestCase):
    def _run(self, seed):
        here = os.path.join(os.path.dirname(__file__), "..")
        out = subprocess.run([sys.executable, "-c", _HASHSEED_SCRIPT, here], capture_output=True,
                             text=True, check=True, env=dict(os.environ, PYTHONHASHSEED=str(seed)))
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_text_ids_give_the_same_layout_under_different_hash_seeds(self):
        self.assertEqual(self._run(1), self._run(2))


if __name__ == "__main__":
    unittest.main()
