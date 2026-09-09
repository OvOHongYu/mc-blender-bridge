# -*- coding: utf-8 -*-
"""端到端集成测试：模拟服务器 + 客户端（net.py）+ 双模式网格一致性。"""
import os
import sys
import time
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))
sys.path.insert(0, os.path.join(ROOT, "server_sim"))

PORT = 8790


class TestIntegration(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        import mc_server_sim
        cls.httpd, cls.world = mc_server_sim.serve_in_thread(PORT, seed=7)
        from mc_bridge.core.net import ApiClient
        cls.client = ApiClient("127.0.0.1", PORT)
        cls.client.ping()
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_ping_and_blocks(self):
        info = self.client.ping()
        self.assertEqual(info["mod"], "mcbridge-sim")
        self.assertIn("mesh", info["modes"])
        b = self.client.blocks()
        self.assertIn("minecraft:stone", b["blocks"])
        self.assertEqual(b["blocks"]["minecraft:stone"]["class"], 1)
        self.assertEqual(b["blocks"]["minecraft:water"]["class"], 3)

    def test_chunk_fetch_and_decode(self):
        p = self.client.chunk("overworld", 0, 0, -64, 320)
        self.assertEqual(p["cx"], 0)
        self.assertEqual(p["yBottom"], -64)
        self.assertEqual(len(p["sections"]), 24)
        nonnull = [s for s in p["sections"] if s is not None]
        self.assertGreater(len(nonnull), 8)
        from mc_bridge.core.codec import build_arrays
        cls, gid, pal, yb = build_arrays(p)
        self.assertEqual(cls.shape, (384, 16, 16))
        # 地表附近应有草地
        names = [n for _, n in pal]
        self.assertIn("minecraft:grass_block", names)
        self.assertIn("minecraft:water", names)

    def test_mesh_mode_b(self):
        m = self.client.mesh("overworld", 0, 0, -64, 320)
        self.assertGreater(len(m["dirs"]), 100)
        # dir 合法
        self.assertTrue(np.all(m["dirs"] < 6))
        # 顶点范围: x,z ∈ 0..16；y ∈ 0..384
        self.assertGreaterEqual(m["verts"].min(), 0)
        self.assertLessEqual(m["verts"][:, :, 0].max(), 16)
        self.assertLessEqual(m["verts"][:, :, 2].max(), 16)
        self.assertLessEqual(m["verts"][:, :, 1].max(), 384)

    def test_mode_a_mode_b_parity(self):
        """客户端拉 3×3 原始数据本地网格(模式A) == 服务器网格(模式B)。"""
        from mc_bridge.core.mesher import assemble_padded, mesh_padded
        from mc_bridge.core.codec import build_arrays
        from mc_bridge.core import mesher
        ymin, ymax = -64, 320
        payloads = {}
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                payloads[(dx, dz)] = self.client.chunk("overworld", 0 + dx, 0 + dz, ymin, ymax)
        quads, pal, _, _ = mesher.mesh_payload(payloads, with_ao=True)
        m = self.client.mesh("overworld", 0, 0, ymin, ymax, lod=0, ao=True, leaves="fancy")
        self.assertEqual(len(quads), len(m["dirs"]))
        for i, (v, d, b, ao) in enumerate(quads):
            np.testing.assert_array_equal(v, m["verts"][i])
            self.assertEqual(d, m["dirs"][i])
            self.assertEqual(b, m["blocks"][i])
            np.testing.assert_array_equal(ao, m["aos"][i])

    def test_versions_and_setblock(self):
        v0 = self.client.versions("overworld", -1, -1, 1, 1)
        # 修改方块后版本递增
        r = self.client.setblock("overworld", 8, 90, 8, "minecraft:gold_block" if False else "minecraft:glass")
        v1 = self.client.versions("overworld", -1, -1, 1, 1)
        self.assertEqual(v1[(0, 0)], v0[(0, 0)] + 1)
        # 网格应包含该玻璃方块
        m = self.client.mesh("overworld", 0, 0, 64, 128)
        names = [n for _, n in m["palette"]]
        self.assertIn("minecraft:glass", names)

    def test_lod1_lod2(self):
        m1 = self.client.mesh("overworld", 0, 0, -64, 320, lod=1)
        m2 = self.client.mesh("overworld", 0, 0, -64, 320, lod=2)
        m0 = self.client.mesh("overworld", 0, 0, -64, 320, lod=0)
        self.assertLessEqual(len(m1["dirs"]), len(m0["dirs"]) + 8000)  # LOD1(AO off) 面数近似或更少
        self.assertLess(len(m2["dirs"]), len(m0["dirs"]))              # LOD2 显著更少

    def test_leaves_fast_reduces_faces(self):
        fancy = self.client.mesh("overworld", 0, 0, -64, 320, lod=0, leaves="fancy")
        fast = self.client.mesh("overworld", 0, 0, -64, 320, lod=0, leaves="fast")
        self.assertLess(len(fast["dirs"]), len(fancy["dirs"]))

    def test_y_subset(self):
        p = self.client.chunk("overworld", 0, 0, 0, 128)
        self.assertEqual(p["yBottom"], 0)
        self.assertEqual(len(p["sections"]), 8)

    def test_texture_png(self):
        png = self.client.texture_png("minecraft:stone", "side")
        self.assertEqual(png[:4], b"\x89PNG")
        png2 = self.client.texture_png("minecraft:grass_block", "top")
        self.assertEqual(png2[:4], b"\x89PNG")

    def test_terrain_consistency_across_chunks(self):
        """相邻区块边界一致（跨区块的地形/树无缝）。"""
        a = self.client.chunk("overworld", 0, 0, 0, 128)
        b = self.client.chunk("overworld", 1, 0, 0, 128)
        from mc_bridge.core.codec import build_arrays
        ca, ga, _, _ = build_arrays(a)   # [y,z,x]
        cb, gb, _, _ = build_arrays(b)
        # 区块(0,0)的 x=15 列 与 区块(1,0)的 x=0 列 应当相邻而非重复错位
        self.assertEqual(ca.shape, cb.shape)
        # 高度连续性: 表面高度差通常 <= 2（平滑噪声）
        def surf(arr):
            top = np.zeros((16, 16), int)
            for z in range(16):
                for x in range(16):
                    col = np.nonzero(arr[:, z, x])[0]
                    top[z, x] = col[-1] if len(col) else -1
            return top
        sa, sb = surf(ca), surf(cb)
        edge_a, edge_b = sa[:, 15], sb[:, 0]
        diff = np.abs(edge_a - edge_b)
        self.assertLessEqual(diff.max(), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
