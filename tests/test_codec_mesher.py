# -*- coding: utf-8 -*-
"""codec / mesher 单元测试（不依赖网络与 bpy）。"""
import os
import random
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
    quads, _ = mesher.mesh_padded(cls, gid, with_ao=with_ao, leaves_fast=leaves_fast)
    return quads, pal_out


def mesh_single_cross(idx3, with_ao=True, leaves_fast=False):
    """同 mesh_single，但按 mesh_payload 规则传入 cross 标记（CUTOUT 且非树叶）。"""
    pal, idx = block_idx(idx3, "x")
    p = make_payload(secs=[(pal, idx)])
    cls, gid, _H, pal_out = mesher.assemble_padded({(0, 0): p})
    cross = np.zeros(len(pal_out), bool)
    for i, (c, name) in enumerate(pal_out):
        cross[i] = (c == B.CUTOUT) and ("leaves" not in name)
    quads, _ = mesher.mesh_padded(cls, gid, with_ao=with_ao,
                                  leaves_fast=leaves_fast, cross=cross)
    return quads, pal_out


def quad_normal(verts):
    p0, p1, p2, p3 = [np.asarray(v, float) for v in verts]
    n = np.cross(p1 - p0, p2 - p0)
    if np.allclose(n, 0):
        n = np.cross(p1 - p0, p3 - p0)
    return n / np.linalg.norm(n)


def _is_diagonal(n):
    """法向在水平面内且不与坐标轴平行 = 交叉面片的对角面片。"""
    return abs(n[1]) < 1e-6 and abs(n[0]) > 1e-6 and abs(n[2]) > 1e-6


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

    def test_cross_plant_two_diagonal_faces(self):
        # 交叉面片植物只发射 2 个对角面片：Blender 默认双面渲染，够用。
        # 回归：曾对每个对角面片再补一层反向绕序（共 4 面）——两者共面重叠 -> Z-Fighting。
        quads, _pal = mesh_single_cross({(0, 1, 0): "minecraft:poppy"})
        diag = [q for q in quads if _is_diagonal(quad_normal(q[0]))]
        self.assertEqual(len(diag), 2)
        # 两个对角面片必须是两条不同的对角线（而非同一条被画两次）
        keys = {frozenset(tuple(int(c) for c in v) for v in q[0]) for q in diag}
        self.assertEqual(len(keys), 2)

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
        # 石头无 tint: vcol 灰度仅来自 AO；材质描述符 = ("block", 名, 面组)
        self.assertEqual(geo["mats"][0][0], "block")
        self.assertIn("minecraft:stone", geo["mats"][0][1])

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


# -------------------------------------------- 区块组（R4 跨区块贪心合并） ----

def _plate_payload():
    """16×16×1 石头平板（y=0），用于观察跨区块合并。"""
    pal, idx = block_idx({(x, 0, z): "minecraft:stone"
                          for x in range(16) for z in range(16)}, "x")
    return make_payload(secs=[(pal, idx)])


class TestChunkGroup(unittest.TestCase):
    """R4：区块组内跨区块贪心合并 + 多区块网格拼接。"""

    def test_group_merges_across_chunk_border(self):
        ks = {(dx, dz): _plate_payload() for dx in (0, 1) for dz in (0, 1)}
        quads, _pal, _, _ = mesher.mesh_payload(ks, group=2)
        # 32×32 平板：顶 / 底 / 四侧各 1 个矩形（组内跨区块边界合并）
        self.assertEqual(len(quads), 6)
        top = [q for q in quads if q[1] == 2][0][0]
        self.assertAlmostEqual(float(top[:, 0].max()), 32.0)
        self.assertAlmostEqual(float(top[:, 2].max()), 32.0)
        # 逐区块单独网格化：每块 6 面 → 24 面，且边界处各自留面
        single = sum(len(mesher.mesh_payload({(0, 0): ks[(dx, dz)]})[0])
                     for dx in (0, 1) for dz in (0, 1))
        self.assertEqual(single, 24)

    def test_group_ring_culls_group_border_faces(self):
        """组外圈一格参与剔除：组边界与邻居同高时不再生成侧面。"""
        ks = {(dx, dz): _plate_payload() for dx in (0, 1) for dz in (0, 1)}
        self.assertEqual(len(mesher.mesh_payload(ks, group=2)[0]), 6)
        ring = dict(ks)
        for key in ((2, 0), (2, 1), (0, 2), (1, 2)):
            ring[key] = _plate_payload()
        quads, _pal, _, _ = mesher.mesh_payload(ring, group=2)
        # 组外同高地板剔除 +X / +Z 两个组边界侧面 -> 顶 + 底 + 两个侧面
        self.assertEqual(len(quads), 4)

    def test_merge_geos_offsets_and_materials(self):
        quads, pal = mesh_single({(0, 0, 0): "minecraft:stone"})
        verts = np.array([q[0] for q in quads], np.float32)
        dirs = np.array([q[1] for q in quads], np.uint8)
        blks = np.array([q[2] for q in quads], np.uint16)
        aos = np.array([q[3] for q in quads], np.uint8)
        geo = mesher.geo_from_arrays(verts, dirs, blks, aos, [n for _, n in pal])
        merged = mesher.merge_geos([geo, geo, geo],
                                   [(0, 0, 0), (16, 0, 0), (0, 0, 16)])
        self.assertEqual(merged["nq"], geo["nq"] * 3)
        self.assertEqual(merged["tris"], merged["nq"] * 2)
        self.assertEqual(len(merged["mats"]), len(geo["mats"]))      # 材质去重
        self.assertEqual(int(merged["mat_idx"].max()), len(geo["mats"]) - 1)
        self.assertAlmostEqual(float(merged["verts"][:, 0].max()),
                               float(geo["verts"][:, 0].max()) + 16.0)
        self.assertEqual(merged["vcol"].shape, (merged["nq"] * 4, 4))



def fluid_block_idx(cells):
    """{(x,y,z): 状态名} -> (palette, 4096 索引)；class 按 base 名查共享表。"""
    names = ["minecraft:air"] + sorted(set(cells.values()))
    pal = []
    for n in names:
        b = B.base_name(n)
        cls = int(B.CLASS[B.INDEX[b]]) if b in B.INDEX else 1
        pal.append((cls, n))
    pmap = {n: i for i, (c, n) in enumerate(pal)}
    arr = np.zeros(4096, np.uint16)
    for (x, y, z), n in cells.items():
        arr[(y << 8) | (z << 4) | x] = pmap[n]
    return pal, arr


def mesh_fluid(cells, fluids=True):
    pal, idx = fluid_block_idx(cells)
    payloads = {(0, 0): make_payload(secs=[(pal, idx)])}
    quads, pal_out, _, _ = mesher.mesh_payload(payloads, fluids=fluids)
    water = [q for q in quads if "water" in pal_out[int(q[2])][1]]
    return quads, water, pal_out


class TestFluidGeometry(unittest.TestCase):
    """类原版流体几何（对照 MC FluidRenderer 的列高度 / 四角平滑）。"""

    def test_pool_surface_is_8_9(self):
        cells = {}
        for x in range(5):
            for z in range(5):
                cells[(x, 0, z)] = "minecraft:stone"
        for x in range(1, 4):
            for z in range(1, 4):
                cells[(x, 1, z)] = "minecraft:water[level=0]"
        quads, water, pal = mesh_fluid(cells)
        # 水池中心格 (2,1,2) 的顶面四角都在 y = 1 + 8/9（水源高度）
        top = [q for q in water if q[1] == 2
               and min(v[0] for v in q[0]) >= 2 and max(v[0] for v in q[0]) <= 3
               and min(v[2] for v in q[0]) >= 2 and max(v[2] for v in q[0]) <= 3]
        self.assertEqual(len(top), 1)
        for v in top[0][0]:
            self.assertAlmostEqual(v[1], 1.0 + 8.0 / 9.0, places=5)

    def test_isolated_source_corner_droop(self):
        # 孤立水源四周皆空气：角高度 = (8/9 × 10) / (10 + 1 + 1) = 20/27
        quads, water, pal = mesh_fluid({(0, 0, 0): "minecraft:water[level=0]"})
        top = [q for q in water if q[1] == 2]
        self.assertEqual(len(top), 1)
        for v in top[0][0]:
            self.assertAlmostEqual(v[1], 20.0 / 27.0, places=5)

    def test_flowing_level_height(self):
        # 方块 level=7 = 流体 level 1（最远离水源、最薄）：own = 1/9 -> 四周空气 -> (1/9)/3
        quads, water, pal = mesh_fluid({(0, 0, 0): "minecraft:water[level=7]"})
        top = [q for q in water if q[1] == 2]
        self.assertEqual(len(top), 1)
        for v in top[0][0]:
            self.assertAlmostEqual(v[1], (1.0 / 9.0) / 3.0, places=5)

    def test_level_is_inverted_vs_fluid_level(self):
        # 回归：方块状态 level 与流体 level 相反（FluidBlock.statesByLevel = getFlowing(8-level)）
        # 靠近水源的 level=1 必须比远处的 level=7 高
        near = [q for q in mesh_fluid({(0, 0, 0): "minecraft:water[level=1]"})[1]
                if q[1] == 2][0]
        far = [q for q in mesh_fluid({(0, 0, 0): "minecraft:water[level=7]"})[1]
               if q[1] == 2][0]
        self.assertAlmostEqual(max(v[1] for v in near[0]), (7.0 / 9.0) / 3.0, places=5)
        self.assertAlmostEqual(max(v[1] for v in far[0]), (1.0 / 9.0) / 3.0, places=5)
        self.assertGreater(max(v[1] for v in near[0]), max(v[1] for v in far[0]))

    def test_no_full_cube_water(self):
        cells = {(0, 0, 0): "minecraft:water[level=0]",
                 (1, 0, 0): "minecraft:water[level=0]"}
        quads, water, pal = mesh_fluid(cells)
        self.assertTrue(water)
        for q in water:
            for v in q[0]:
                self.assertLess(v[1], 1.0 - 1e-6)     # 水面必须低于方块顶面

    def test_same_fluid_face_culled(self):
        cells = {(0, 0, 0): "minecraft:water[level=0]",
                 (1, 0, 0): "minecraft:water[level=0]"}
        quads, water, pal = mesh_fluid(cells)
        for verts, d, blk, ao in water:
            xs = [v[0] for v in verts]
            if min(xs) == 1 and max(xs) == 1:
                self.fail("水-水界面未剔除: %s" % (verts,))

    def test_fluid_winding(self):
        cells = {(0, 0, 0): "minecraft:water[level=0]",
                 (0, 0, 1): "minecraft:water[level=7]"}
        quads, water, pal = mesh_fluid(cells)
        self.assertTrue(water)
        # 流体面是斜坡（相邻角高度差可达半格），法向会明显偏离面方向，
        # 因此这里只要求「法向与面方向同号且占主导」（斜率上限 45° -> 0.707）
        axis = {0: (0, 1), 1: (0, -1), 2: (1, 1), 3: (1, -1), 4: (2, 1), 5: (2, -1)}
        for verts, d, blk, ao in water:
            p = [np.asarray(v, float) for v in verts]
            n = np.cross(p[1] - p[0], p[2] - p[0])
            if np.allclose(n, 0):
                n = np.cross(p[1] - p[0], p[3] - p[0])
            self.assertGreater(float(np.linalg.norm(n)), 1e-9)
            n = n / np.linalg.norm(n)
            ax, sign = axis[d]
            self.assertGreater(float(n[ax]) * sign, 0.5,
                               "winding fail dir=%d n=%s" % (d, n))

    def test_fluids_off_keeps_full_cube(self):
        # 默认关闭（服务端网格 MCM1 为整型块坐标）-> 仍是整方块，与 Java 侧一致
        cells = {(0, 0, 0): "minecraft:water[level=0]"}
        quads, water, pal = mesh_fluid(cells, fluids=False)
        self.assertTrue(any(max(v[1] for v in q[0]) == 1.0 for q in water))



# -------------------------------------------- 流体几何 vs 原版（对拍参考实现） ----
# 参考实现逐指令还原 MC 1.21.1 net/minecraft/client/render/block/FluidRenderer
# （javap -c 反汇编 getFluidHeight / calculateFluidHeight / addHeight / render）：
#   getFluidHeight(world, fluid, pos, state, fluidState):
#       同种流体 -> 上方仍是同种流体 ? 1.0 : fluidState.getHeight()
#       异种     -> state.isSolid() ? -1.0 : 0.0
#   calculateFluidHeight(world, fluid, own, f4, f5, cornerPos):
#       f5>=1 或 f4>=1 -> 1.0；f5>0 或 f4>0 时先计入对角块（>=1 则直接 1.0）；
#       再按 addHeight 权重平均 own / f5 / f4（h>=0.8 权重 10，0<=h<0.8 权重 1，h<0 忽略）
#   render: own >= 1.0 时四角直接 1.0；否则 NE=calc(own,n,e) NW=calc(own,n,w)
#           SE=calc(own,s,e) SW=calc(own,s,w)（n/s/w/e 为同层邻居列高度）
_HINT_CLASS = (("water", B.LIQUID), ("lava", B.LIQUID), ("glass", B.TRANSPARENT),
               ("leaves", B.CUTOUT), ("grass", B.CUTOUT), ("air", B.AIR))


def hinted_class(name):
    """按名字启发式判定 class（与资产包烘焙 _LIQUID_HINT 等一致；lava 不在共享小表里）。"""
    base = B.base_name(name)
    if base in B.INDEX:
        return int(B.CLASS[B.INDEX[base]])
    low = base.lower()
    for hint, cls in _HINT_CLASS:
        if hint in low:
            return cls
    return B.OPAQUE


def hinted_payload(cells):
    """{(x,y,z): 状态名} -> ({...payload}, palette)。class 用 hinted_class。"""
    names = sorted(set(cells.values()) | {"minecraft:air"})
    pal = [(hinted_class(n), n) for n in names]
    pmap = {n: i for i, (c, n) in enumerate(pal)}
    arr = np.zeros(4096, np.uint16)
    for (x, y, z), n in cells.items():
        arr[(y << 8) | (z << 4) | x] = pmap[n]
    return make_payload(secs=[(pal, arr)]), pal


def _ref_solid(name):
    """原版 BlockState.isSolid()：按碰撞箱判定（树叶碰撞箱满格 -> 实心）。"""
    c = hinted_class(name)
    return c in (B.OPAQUE, B.TRANSPARENT, B.NONCUBE) or (c == B.CUTOUT and "leaves" in name)


def _ref_state_height(name):
    """FluidState.getHeight() = 流体 level/9。

    方块状态 level 与流体 level 相反（FluidBlock.statesByLevel[i] = getFlowing(8-i)）：
    0 -> 静止(8/9)；1..7 -> (8-level)/9；8 -> 下落(8/9)。"""
    v = B.props_of(name).get("level")
    if v is None:
        return 8.0 / 9.0
    v = int(v)
    if v == 0 or v >= 8:
        return 8.0 / 9.0
    return (8 - v) / 9.0


def _ref_col_height(world, fluid, pos):
    x, y, z = pos
    st = world.get(pos, "minecraft:air")
    if B.base_name(st) == fluid:
        if B.base_name(world.get((x, y + 1, z), "minecraft:air")) == fluid:
            return 1.0
        return _ref_state_height(st)
    return -1.0 if _ref_solid(st) else 0.0


def _ref_corner(world, fluid, own, fa, fb, cpos):
    if fa >= 1.0 or fb >= 1.0:
        return 1.0
    acc = [0.0, 0.0]

    def add(h):
        if h >= 0.8:
            acc[0] += h * 10.0
            acc[1] += 10.0
        elif h >= 0.0:
            acc[0] += h
            acc[1] += 1.0

    if fa > 0.0 or fb > 0.0:
        h = _ref_col_height(world, fluid, cpos)
        if h >= 1.0:
            return 1.0
        add(h)
    add(own)
    add(fa)
    add(fb)
    return acc[0] / acc[1]


def ref_fluid_corners(world, pos):
    """原版四角高度：NE=(+x,-z) NW=(-x,-z) SE=(+x,+z) SW=(-x,+z)。"""
    fluid = B.base_name(world[pos])
    x, y, z = pos
    own = _ref_col_height(world, fluid, pos)
    if own >= 1.0:
        return {"NE": 1.0, "NW": 1.0, "SE": 1.0, "SW": 1.0}
    n = _ref_col_height(world, fluid, (x, y, z - 1))
    s = _ref_col_height(world, fluid, (x, y, z + 1))
    w = _ref_col_height(world, fluid, (x - 1, y, z))
    e = _ref_col_height(world, fluid, (x + 1, y, z))
    return {
        "NE": _ref_corner(world, fluid, own, n, e, (x + 1, y, z - 1)),
        "NW": _ref_corner(world, fluid, own, n, w, (x - 1, y, z - 1)),
        "SE": _ref_corner(world, fluid, own, s, e, (x + 1, y, z + 1)),
        "SW": _ref_corner(world, fluid, own, s, w, (x - 1, y, z + 1)),
    }


# 面方向 -> [(顶点 x 偏移, 顶点 z 偏移, 角名)]（顶面 2 / 侧面 0,1,4,5；底面 3 无角高度）
_FACE_CORNERS = {
    2: [(0, 0, "NW"), (0, 1, "SW"), (1, 1, "SE"), (1, 0, "NE")],
    0: [(1, 0, "NE"), (1, 1, "SE")],
    1: [(0, 0, "NW"), (0, 1, "SW")],
    4: [(0, 1, "SW"), (1, 1, "SE")],
    5: [(0, 0, "NW"), (1, 0, "NE")],
}


def mesh_fluid_face_corners(cells):
    """流体四边形 -> [(pos, dir, {角名: 相对高度})]，用于与原版四角对拍。"""
    payload, pal = hinted_payload(cells)
    quads, pal_out, _, _ = mesher.mesh_payload({(0, 0): payload}, fluids=True)
    out = []
    for verts, d, blk, ao in quads:
        if B.base_name(pal_out[int(blk)][1]) not in ("minecraft:water", "minecraft:lava"):
            continue
        # 面在方块边界上：+x 面 / +z 面 的 min 坐标比方块自身大 1
        X = int(min(v[0] for v in verts)) - (1 if d == 0 else 0)
        Z = int(min(v[2] for v in verts)) - (1 if d == 4 else 0)
        # 侧面/底面底边恰在 Y；顶面四角可能全为 1.0（邻居满格），此时 min y = Y+1
        ymin = min(v[1] for v in verts)
        ymax = max(v[1] for v in verts)
        Y = (int(np.ceil(ymax - 1e-6)) - 1) if d == 2 else int(round(ymin))
        top = {}
        for v in verts:                      # 同一 (x,z) 列有底/顶两个顶点，取最高者
            k = (int(v[0]), int(v[2]))
            top[k] = max(top.get(k, -1e9), v[1])
        cs = {}
        for ox, oz, cn in _FACE_CORNERS.get(d, []):
            if (X + ox, Z + oz) in top:
                cs[cn] = top[(X + ox, Z + oz)] - Y
        out.append(((X, Y, Z), d, cs))
    return out


class TestFluidVanillaConformance(unittest.TestCase):
    """流体四角高度与原版游戏逐面一致（回归：曾出现瀑布/多层水体侧面收成斜边）。"""

    def assert_vanilla(self, cells):
        world = dict(cells)
        checked = 0
        for pos, d, cs in mesh_fluid_face_corners(cells):
            if pos not in world:             # 邻居区块填充进来的方块不在本 payload
                continue
            ref = ref_fluid_corners(world, pos)
            for cn, got in cs.items():
                checked += 1
                self.assertAlmostEqual(
                    got, ref[cn], places=5,
                    msg="pos=%s dir=%d 角%s 网格=%.4f 原版=%.4f" % (pos, d, cn, got, ref[cn]))
        self.assertGreater(checked, 0)
        return checked

    def test_pool_two_layers(self):
        cells = {}
        for x in range(4, 12):
            for z in range(4, 12):
                cells[(x, 0, z)] = "minecraft:water[level=0]"
                if x in (4, 11) or z in (4, 11):
                    cells[(x, 1, z)] = "minecraft:water[level=0]"
        self.assert_vanilla(cells)

    def test_waterfall_column(self):
        cells = {(5, y, 5): "minecraft:water[level=0]" for y in range(1, 5)}
        self.assert_vanilla(cells)
        # 每段侧面（下方仍有水）的两条顶边顶点都必须到格顶：原版四角 = 1.0
        quad_list = mesher.mesh_payload({(0, 0): hinted_payload(cells)[0]}, fluids=True)[0]
        checked = 0
        for verts, d, blk, ao in quad_list:
            if d not in (0, 1, 4, 5):
                continue
            base = min(float(v[1]) for v in verts)
            if base >= 4.0:
                continue                     # 顶端一块上方是空气 -> 原版 8/9
            tops = [float(v[1]) for v in verts if float(v[1]) > base + 1e-6]
            self.assertTrue(tops)
            for y in tops:
                self.assertAlmostEqual(y - base, 1.0, places=5)
            checked += 1
        self.assertGreater(checked, 3)

    def test_flowing_stair(self):
        cells = {(5, 1, 5): "minecraft:water[level=0]"}
        for x in range(6, 11):
            cells[(x, 1, 5)] = "minecraft:water[level=%d]" % (11 - x)
        self.assert_vanilla(cells)

    def test_lava_and_leaves_neighbours(self):
        cells = {(5, 2, 5): "minecraft:water[level=0]",
                 (4, 2, 5): "minecraft:lava[level=3]", (6, 2, 5): "minecraft:lava",
                 (5, 2, 4): "minecraft:oak_leaves[distance=1]",
                 (4, 2, 4): "minecraft:short_grass", (5, 2, 6): "minecraft:oak_stairs"}
        self.assert_vanilla(cells)

    def test_random_blobs(self):
        rnd = random.Random(20240607)
        pool = ["minecraft:water[level=0]", "minecraft:water[level=1]",
                "minecraft:water[level=4]", "minecraft:water[level=7]",
                "minecraft:stone", "minecraft:glass", "minecraft:oak_leaves",
                "minecraft:short_grass", "minecraft:oak_stairs", "minecraft:lava"]
        for _ in range(4):
            cells = {}
            for x in range(2, 14):
                for z in range(2, 14):
                    for y in range(1, 4):
                        if rnd.random() < 0.5:
                            cells[(x, y, z)] = rnd.choice(pool)
            self.assert_vanilla(cells)

    def test_water_above_keeps_full_column(self):
        # 回归：下方水块上方仍是水 -> 原版四角 = 1.0；曾错误地按 10:1:1 平均成 0.8333
        cells = {(5, 1, 5): "minecraft:water[level=0]", (5, 2, 5): "minecraft:water[level=0]"}
        payload, pal = hinted_payload(cells)
        quads = [q for q in mesher.mesh_payload({(0, 0): payload}, fluids=True)[0]
                 if B.base_name(pal[int(q[2])][1]) == "minecraft:water"]
        side = [q for q in quads if q[1] == 1 and abs(min(v[1] for v in q[0]) - 1.0) < 1e-6]
        self.assertEqual(len(side), 1)
        self.assertAlmostEqual(max(float(v[1]) for v in side[0][0]), 2.0, places=5)


class TestFluidModelOverlap(unittest.TestCase):
    """回归：资产包给液体注入的整方块烘焙模型必须被剔除（否则与流体几何重叠）。

    pack.use_model 只表示"模型不是简单整立方体"、与 class 无关，水/岩浆在真实
    资产包里同样是 use_model=True（实测 dist/assets.mcba：water/lava
    classify=5 use_model=True）。"""

    class _FakePack(object):
        """最小资产包替身：仅水有烘焙模型（一整格立方体）。"""

        def __init__(self):
            cube = ((0, 0, 0), (0, 0, 16), (0, 16, 16), (0, 16, 0))
            self.variants = [[(cube, 0, 0, -1, 0, ((0.0, 0.0),) * 4)]]

        def use_model(self, block):
            # mesher 传方块状态全名（v3 按状态解析），真实包会按基础名兜底
            return B.base_name(block) == "minecraft:water"

        def variant_indices(self, block, props=None):
            return [0] if B.base_name(block) == "minecraft:water" else []

    def _payload(self):
        pal = [(0, "minecraft:air"), (3, "minecraft:water[level=0]"), (1, "minecraft:stone")]
        idx = np.zeros(4096, np.uint16)
        for z in range(16):
            for x in range(16):
                idx[(0 << 8) | (z << 4) | x] = 2          # 石地板
        idx[(1 << 8) | (3 << 4) | 3] = 1                  # 一格水
        return make_payload(secs=[(pal, idx)])

    def test_fluid_geometry_replaces_pack_model(self):
        pack = self._FakePack()
        payload = self._payload()
        quads, pal, _, models = mesher.mesh_payload({(0, 0): payload},
                                                    fluids=True, pack=pack)
        liq = {i for i, (c, _) in enumerate(pal) if c == B.LIQUID}
        self.assertTrue(liq)
        self.assertTrue(any(int(q[2]) in liq for q in quads))            # 流体几何在
        self.assertFalse([m for m in models if int(m[6]) in liq],        # 液体模型没了
                         "液体的整方块烘焙模型未被剔除，会与流体几何重叠")

    def test_pack_model_kept_when_fluids_off(self):
        # fluids=False（服务端网格路径）时仍走资产包/整方块，行为不变
        pack = self._FakePack()
        quads, pal, _, models = mesher.mesh_payload({(0, 0): self._payload()},
                                                    fluids=False, pack=pack)
        liq = {i for i, (c, _) in enumerate(pal) if c == B.LIQUID}
        self.assertTrue([m for m in models if int(m[6]) in liq])


if __name__ == "__main__":
    unittest.main(verbosity=2)