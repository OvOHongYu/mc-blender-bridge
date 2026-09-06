# -*- coding: utf-8 -*-
"""codec / mesher 单元测试（不依赖网络与 bpy）。"""
import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))

from mc_bridge.core import blocks as B
from mc_bridge.core import codec
from mc_bridge.core import mesher


def make_payload(dim="overworld", cx=0, cz=0, y_bottom=0, secs=None, n_secs=1):
    """secs: list[(palette [(cls,name)...], indices list/ndarray)] 或 None。"""
    if secs is None:
        secs = [([(0, "minecraft:air"), (1, "minecraft:stone")],
                 np.zeros(4096, np.uint16))]
    sections = []
    for pal, idx in secs:
        idx = np.asarray(idx, np.uint16)
        sections.append({"palette": list(pal), "indices": idx})
    while len(sections) < n_secs:
        sections.append(None)
    return {"dim": dim, "cx": cx, "cz": cz, "yBottom": y_bottom, "sections": sections}


def block_idx(idx3, name):
    """便捷构造 4096 索引: idx3 dict {(x,y,z): name} -> palette 按需。"""
    names = sorted(set(idx3.values()) | {"minecraft:air"})
    pal = [(int(B.CLASS[B.INDEX[n]]), n) if n in B.INDEX else (5, n) for n in names]
    pmap = {n: i for i, (c, n) in enumerate(pal)}
    arr = np.zeros(4096, np.uint16)
    for (x, y, z), n in idx3.items():
        arr[(y << 8) | (z << 4) | x] = pmap[n]
    return pal, arr


class TestCodec(unittest.TestCase):
    def test_mcc1_roundtrip(self):
        pal, idx = block_idx({(0, 0, 0): "minecraft:stone",
                               (5, 0, 3): "minecraft:glass",
                               (1, 1, 1): "minecraft:water"}, "x")
        p = make_payload(secs=[(pal, idx)])
        buf = codec.encode_mcc1(p)
        p2 = codec.decode_mcc1(buf)
        self.assertEqual(p2["cx"], 0)
        self.assertEqual(p2["yBottom"], 0)
        self.assertEqual(len(p2["sections"]), 1)
        np.testing.assert_array_equal(p2["sections"][0]["indices"], idx)
        self.assertEqual(p2["sections"][0]["palette"], pal)

    def test_mcc1_single_palette_bits0(self):
        pal = [(1, "minecraft:stone")]
        idx = np.zeros(4096, np.uint16)
        p = make_payload(secs=[(pal, idx)])
        buf = codec.encode_mcc1(p)
        p2 = codec.decode_mcc1(buf)
        self.assertEqual(p2["sections"][0]["palette"], pal)
        np.testing.assert_array_equal(p2["sections"][0]["indices"], idx)

    def test_mcc1_absent_sections(self):
        p = make_payload(n_secs=3)
        p["sections"][0] = None
        p["sections"][1] = {"palette": [(1, "minecraft:stone")],
                            "indices": np.zeros(4096, np.uint16)}
        p["sections"][2] = None
        p2 = codec.decode_mcc1(codec.encode_mcc1(p))
        self.assertIsNone(p2["sections"][0])
        self.assertIsNotNone(p2["sections"][1])
        self.assertIsNone(p2["sections"][2])

    def test_bitpacking_wide_palette(self):
        n = min(40, B.N_BLOCKS)
        names = [B.NAMES[i] for i in range(n)]
        pal = [(int(B.CLASS[i]), nm) for i, nm in enumerate(names)]
        rng = np.random.default_rng(7)
        idx = rng.integers(0, len(pal), 4096).astype(np.uint16)
        p = make_payload(secs=[(pal, idx)])
        p2 = codec.decode_mcc1(codec.encode_mcc1(p))
        np.testing.assert_array_equal(p2["sections"][0]["indices"], idx)

    def test_mcm1_roundtrip(self):
        quads = [(np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], np.int16),
                  2, 1, (3, 3, 2, 3))]
        pal = [(0, "minecraft:air"), (1, "minecraft:stone")]
        buf = codec.encode_mcm1("overworld", 3, -2, 0, pal, quads)
        m = codec.decode_mcm1(buf)
        self.assertEqual((m["cx"], m["cz"]), (3, -2))
        self.assertEqual(m["palette"], pal)
        np.testing.assert_array_equal(m["verts"][0], quads[0][0])
        self.assertEqual(m["dirs"][0], 2)
        self.assertEqual(m["blocks"][0], 1)
        self.assertEqual(tuple(m["aos"][0]), (3, 3, 2, 3))

    def test_versions_roundtrip(self):
        items = [(1, 2, 99), (-5, 0, 2**40)]
        buf = codec.encode_versions(items)
        d = codec.decode_versions(buf)
        self.assertEqual(d[(1, 2)], 99)
        self.assertEqual(d[(-5, 0)], 2**40)

    def test_build_arrays(self):
        pal, idx = block_idx({(0, 0, 0): "minecraft:stone"}, "x")
        p = make_payload(secs=[(pal, idx)])
        cls, gid, pal2, yb = codec.build_arrays(p)
        self.assertEqual(cls.shape, (16, 16, 16))
        self.assertEqual(cls[0, 0, 0], 1)
        self.assertEqual(gid[0, 0, 0], pal_index(pal2, "minecraft:stone"))
        self.assertEqual(pal2[0], (0, "minecraft:air"))


def pal_index(pal, name):
    for i, (c, n) in enumerate(pal):
        if n == name:
            return i
    raise KeyError(name)


def mesh_single(idx3, with_ao=True, leaves_fast=False):
    """对单区块（无邻居=air 边框）跑网格，返回 quads。"""
    pal, idx = block_idx(idx3, "x")
    p = make_payload(secs=[(pal, idx)])
    payloads = {(0, 0): p}
    cls, gid, H, pal_out = mesher.assemble_padded(payloads)
    return mesher.mesh_padded(cls, gid, with_ao=with_ao, leaves_fast=leaves_fast), pal_out


def quad_face(verts, d):
    """校验 4 顶点共面且法向与 dir 一致（外向 CCW）。"""
    p0, p1, p2, p3 = [np.asarray(v, float) for v in verts]
    n = np.cross(p1 - p0, p2 - p0)
    if np.allclose(n, 0):
        n = np.cross(p1 - p0, p3 - p0)
    expect = [np.array(v) for v in [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]][d]
    cos = np.dot(n / np.linalg.norm(n), expect)
    return cos > 0.99


class TestMesher(unittest.TestCase):
    def test_single_block_6_faces(self):
        quads, pal = mesh_single({(0, 0, 0): "minecraft:stone"})
        self.assertEqual(len(quads), 6)
        for verts, d, blk, ao in quads:
            self.assertTrue(quad_face(verts, d), f"winding fail dir={d}")
        # 顶点局部坐标应在 [0,1]
        for verts, *_ in quads:
            self.assertTrue(verts.min() >= 0 and verts.max() <= 1)

    def test_two_stones_merge_x(self):
        quads, pal = mesh_single({(0, 0, 0): "minecraft:stone", (1, 0, 0): "minecraft:stone"})
        # 4 侧面 + 顶/底 = 6 面（x 方向的 ±X 面因相邻同类消除）
        self.assertEqual(len(quads), 6)
        # ±Y 与 ±Z 面应被合并成 2×1
        big = [q for q in quads if q[1] in (2, 3, 4, 5)]
        for verts, d, blk, ao in big:
            self.assertTrue(quad_face(verts, d))
        spans = [np.ptp(verts[:, a].astype(float)) for verts, d, *_ in quads
                 if d in (4, 5) for a in [0]]
        self.assertIn(2.0, spans)  # z 面横向合并成宽 2

    def test_glass_self_cull(self):
        quads, pal = mesh_single({(0, 0, 0): "minecraft:glass", (1, 0, 0): "minecraft:glass"})
        # 6 个外表面 + 4 个侧面? 玻璃相邻面剔除: ±Y ±Z 各 2, ±X 各 1 -> 6
        self.assertEqual(len(quads), 6)

    def test_water_cull_and_top(self):
        quads, pal = mesh_single({(0, 0, 0): "minecraft:water", (1, 0, 0): "minecraft:water"})
        self.assertEqual(len(quads), 6)

    def test_opaque_neighbor_occludes(self):
        # 石头紧贴玻璃: 玻璃朝向石头的面被剔除；石头隔着玻璃可见, 6 面全保留
        quads, pal = mesh_single({(0, 0, 0): "minecraft:glass", (1, 0, 0): "minecraft:stone"})
        # 石头 6 面 + 玻璃 5 面(朝石头面剔除) = 11
        self.assertEqual(len(quads), 11)

    def test_leaves_never_occlude(self):
        quads, pal = mesh_single({(0, 0, 0): "minecraft:stone", (1, 0, 0): "minecraft:oak_leaves"})
        # 石头 6 面 + 树叶 5 面（树叶贴着石头的那一面被不透明方块剔除——与 MC 一致）
        self.assertEqual(len(quads), 11)

    def test_leaves_fast_mode(self):
        quads, pal = mesh_single({(0, 0, 0): "minecraft:stone",
                                  (1, 0, 0): "minecraft:oak_leaves"}, leaves_fast=True)
        # fast 模式树叶视作不透明: 石头 5 面 + 树叶 5 面
        self.assertEqual(len(quads), 10)

    def test_wall_merge_16(self):
        idx3 = {(x, 0, z): "minecraft:stone" for x in range(16) for z in range(16)}
        quads, pal = mesh_single(idx3)
        # 完整 16x16 平板: 顶 1 + 底 1 + 4 侧各 1 = 6
        self.assertEqual(len(quads), 6)

    def test_ao_on_corner(self):
        # 2×2 石板地面 + 其一角上方再放一块石头：
        # 地板顶面在贴角处被压暗（AO=2），且 AO 差异阻止 2×2 合并成一块
        idx3 = {}
        for x in range(2):
            for z in range(2):
                idx3[(x, 0, z)] = "minecraft:stone"
        idx3[(1, 1, 1)] = "minecraft:stone"
        quads, pal = mesh_single(idx3)
        top = [q for q in quads if q[1] == 2]
        self.assertGreaterEqual(len(top), 2)           # AO 差异阻止完全合并
        self.assertEqual(min(min(q[3]) for q in top), 2)
        for verts, d, blk, ao in top:
            self.assertTrue(quad_face(verts, d))

    def test_all_dirs_winding(self):
        rng = np.random.default_rng(3)
        idx3 = {}
        for _ in range(30):
            idx3[(int(rng.integers(0, 5)), int(rng.integers(0, 5)), int(rng.integers(0, 5)))] = \
                B.NAMES[int(rng.integers(1, B.N_BLOCKS))]
        quads, pal = mesh_single(idx3)
        self.assertGreater(len(quads), 0)
        for verts, d, blk, ao in quads:
            self.assertTrue(quad_face(verts, d), f"winding fail dir={d} verts={verts}")

    def test_shell_lod(self):
        idx3 = {}
        for x in range(16):
            for z in range(16):
                h = (x * 7 + z * 3) % 5
                for y in range(h + 1):
                    idx3[(x, y, z)] = "minecraft:stone"
        pal, idx = block_idx(idx3, "x")
        p = make_payload(secs=[(pal, idx)])
        cls, gid, _, _ = codec.build_arrays(p)
        quads = mesher.shell_lod(np.transpose(cls, (2, 0, 1)), np.transpose(gid, (2, 0, 1)))
        self.assertGreater(len(quads), 16)
        for verts, d, blk, ao in quads:
            self.assertTrue(quad_face(verts, d), f"shell winding fail dir={d}")
            self.assertTrue(verts.min() >= 0)

    def test_geo_from_arrays(self):
        quads, pal = mesh_single({(0, 0, 0): "minecraft:stone"})
        verts = np.array([q[0] for q in quads])
        dirs = np.array([q[1] for q in quads])
        blks = np.array([q[2] for q in quads])
        aos = np.array([q[3] for q in quads])
        geo = mesher.geo_from_arrays(verts, dirs, blks, aos, [n for _, n in pal])
        self.assertEqual(geo["nq"], len(quads))
        self.assertEqual(geo["verts"].shape, (len(quads) * 4, 3))
        self.assertEqual(geo["uv"].shape, (len(quads) * 4, 2))
        self.assertEqual(geo["vcol"].shape, (len(quads) * 4, 4))
        self.assertEqual(geo["tris"], len(quads) * 2)
        # 石头无 tint: vcol 灰度仅来自 AO
        self.assertIn("minecraft:stone", geo["mats"][0][0])

    def test_geo_tint(self):
        quads, pal = mesh_single({(0, 0, 0): "minecraft:oak_leaves"})
        verts = np.array([q[0] for q in quads])
        dirs = np.array([q[1] for q in quads])
        blks = np.array([q[2] for q in quads])
        aos = np.array([q[3] for q in quads])
        geo = mesher.geo_from_arrays(verts, dirs, blks, aos, [n for _, n in pal])
        g = geo["vcol"][:, 1].astype(float)  # G 通道
        r = geo["vcol"][:, 0].astype(float)
        self.assertGreater(g.max(), r.max())  # 叶子绿色 tint


if __name__ == "__main__":
    unittest.main(verbosity=2)
