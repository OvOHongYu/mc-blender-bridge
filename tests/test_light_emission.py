# -*- coding: utf-8 -*-
"""R6 发光方块：原版亮度表 light_of、材质描述符携带 glow、
Emission 节点接线与全局倍率热更新。"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))
sys.path.insert(0, HERE)

import numpy as np  # noqa: E402
import fake_bpy  # noqa: E402

bpy = fake_bpy.install()

from mc_bridge.core import blocks as B  # noqa: E402
from mc_bridge.core import mesher  # noqa: E402
from mc_bridge import mats  # noqa: E402


class TestVanillaLight(unittest.TestCase):
    def test_static_table(self):
        self.assertEqual(B.light_of("minecraft:glowstone"), 15)
        self.assertEqual(B.light_of("minecraft:lava"), 15)
        self.assertEqual(B.light_of("minecraft:torch"), 14)
        self.assertEqual(B.light_of("minecraft:magma_block"), 3)
        self.assertEqual(B.light_of("minecraft:stone"), 0)
        self.assertEqual(B.light_of("minecraft:water"), 0)

    def test_stateful_rules(self):
        self.assertEqual(B.light_of("minecraft:redstone_lamp[lit=true]"), 15)
        self.assertEqual(B.light_of("minecraft:redstone_lamp[lit=false]"), 0)
        self.assertEqual(
            B.light_of("minecraft:candle[candles=4,lit=true,waterlogged=false]"), 12)
        self.assertEqual(
            B.light_of("minecraft:candle[candles=2,lit=false,waterlogged=false]"), 0)
        self.assertEqual(
            B.light_of("minecraft:sea_pickle[pickles=2,waterlogged=true]"), 9)
        self.assertEqual(
            B.light_of("minecraft:sea_pickle[pickles=2,waterlogged=false]"), 6)
        self.assertEqual(B.light_of("minecraft:respawn_anchor[charges=5]"), 15)
        self.assertEqual(B.light_of("minecraft:respawn_anchor[charges=0]"), 0)
        self.assertEqual(B.light_of("minecraft:light[level=7,waterlogged=false]"), 7)
        self.assertEqual(B.light_of(
            "minecraft:campfire[lit=true,signal_fire=false,waterlogged=false]"), 15)

    def test_light_map_priority(self):
        lm = {"mymod:lamp[lit=true]": 12, "minecraft:glowstone": 15}
        self.assertEqual(B.light_of("mymod:lamp[lit=true]", lm), 12)
        self.assertEqual(B.light_of("mymod:lamp[lit=false]", lm), 0)
        self.assertEqual(B.light_of("minecraft:glowstone", lm), 15)
        # 精确状态未命中时回退基础名
        self.assertEqual(B.light_of("mymod:lamp[lit=true]",
                                    {"mymod:lamp": 9}), 9)

    def test_light_map_bounds_and_garbage(self):
        self.assertEqual(B.light_of("minecraft:torch", {"minecraft:torch": 99}), 15)
        self.assertEqual(B.light_of("minecraft:torch", {"minecraft:torch": "x"}), 14)
        self.assertEqual(B.light_of("minecraft:torch", {}), 14)


class TestGeoGlowDesc(unittest.TestCase):
    def _quad_geo(self, palette, blk_idx, light=None):
        verts = np.array([[[0, 1, 0], [1, 1, 0], [1, 1, 1], [0, 1, 1]]],
                         np.float32)
        dirs = np.array([2], np.uint8)
        blocks_ = np.array([blk_idx], np.uint16)
        aos = np.zeros((1, 4), np.uint8)
        return mesher.geo_from_arrays(verts, dirs, blocks_, aos, palette,
                                      light=light)

    def test_block_desc_carries_glow(self):
        d = self._quad_geo(["minecraft:air", "minecraft:glowstone"], 1)["mats"][0]
        self.assertEqual(d[1], "minecraft:glowstone")
        self.assertEqual(d[4], 15)
        d = self._quad_geo(["minecraft:air", "minecraft:stone"], 1)["mats"][0]
        self.assertEqual(len(d), 4, "不发光的方块不应带 glow 位")
        self.assertEqual(d[1], "minecraft:stone")

    def test_light_map_reaches_desc(self):
        d = self._quad_geo(["minecraft:air", "minecraft:stone"], 1,
                           light={"minecraft:stone": 7})["mats"][0]
        self.assertEqual(d[4], 7)

    def test_model_desc_glow_by_texture(self):
        palette = ["minecraft:air", "minecraft:torch"]
        m = [((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),   # verts (4,3)
             0,                                              # dir
             5,                                              # tex id
             -1,                                             # tint
             0,                                              # cull
             ((0, 0), (1, 0), (1, 1), (0, 1)),               # uv
             1,                                              # palette idx
             (3, 3, 3, 3)]                                   # ao
        geo = mesher.geo_from_arrays(
            np.zeros((0, 4, 3), np.float32), np.zeros(0, np.uint8),
            np.zeros(0, np.uint16), np.zeros((0, 4), np.uint8),
            palette, models=[m])
        self.assertIn(("tex", 5, 14), geo["mats"])

    def test_model_desc_no_glow(self):
        palette = ["minecraft:air", "minecraft:oak_stairs"]
        m = [((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)),
             0, 5, -1, 0, ((0, 0), (1, 0), (1, 1), (0, 1)), 1, (3, 3, 3, 3)]
        geo = mesher.geo_from_arrays(
            np.zeros((0, 4, 3), np.float32), np.zeros(0, np.uint8),
            np.zeros(0, np.uint16), np.zeros((0, 4), np.uint8),
            palette, models=[m])
        self.assertIn(("tex", 5), geo["mats"])


class TestEmissionNodes(unittest.TestCase):
    def _mk(self, block, glow, name):
        mat = bpy.data.materials.new(name)
        img = bpy.data.images.new("MCBT_" + name, 4, 4)
        mats._wire(mat, img, block, transparent=False, glow=glow)
        return mat

    def setUp(self):
        for m in list(bpy.data.materials):
            if m.name.startswith("MCB_"):
                bpy.data.materials.remove(m)
        mats.set_light_map(None)
        mats.set_emission_scale(4.0)

    def test_glowstone_has_emission(self):
        mat = self._mk("minecraft:glowstone", 15, "MCB_glow__top")
        emis = [n for n in mat.node_tree.nodes
                if n.type == "ShaderNodeEmission"]
        self.assertEqual(len(emis), 1)
        self.assertAlmostEqual(emis[0].inputs["Strength"].default_value, 4.0)
        self.assertTrue([n for n in mat.node_tree.nodes
                         if n.type == "ShaderNodeAddShader"])

    def test_plain_block_has_no_emission(self):
        mat = self._mk("minecraft:stone", 0, "MCB_stone__top")
        self.assertFalse([n for n in mat.node_tree.nodes
                          if n.type == "ShaderNodeEmission"])

    def test_scale_hot_update(self):
        mat = self._mk("minecraft:glowstone", 15, "MCB_glow__top")
        mats.set_emission_scale(8.0)
        emis = next(n for n in mat.node_tree.nodes
                    if n.type == "ShaderNodeEmission")
        self.assertAlmostEqual(emis.inputs["Strength"].default_value, 8.0)
        mats.set_emission_scale(0.0)
        self.assertAlmostEqual(emis.inputs["Strength"].default_value, 0.0)
        # 倍率从 0 再升：旧材质没有有效发光强度，整体重建
        mats.set_emission_scale(4.0)
        self.assertIsNone(bpy.data.materials.get("MCB_glow__top"))

    def test_glow_of_uses_light_map(self):
        mats.set_light_map({"mymod:lamp[lit=true]": 12})
        self.assertEqual(mats._glow_of("mymod:lamp[lit=true]"), 12)
        self.assertEqual(mats._glow_of("mymod:lamp[lit=false]"), 0)
        mats.set_light_map(None)
        self.assertEqual(mats._glow_of("minecraft:torch"), 14)

    def test_model_material_glow_name(self):
        # 无资产包时只建占位材质，这里仅验证命名分发不炸
        mat = mats.get_material_for(("tex", 42, 15))
        self.assertEqual(mat.name, "MCB_tex_42_g15")
        mat = mats.get_material_for(("tex", 42))
        self.assertEqual(mat.name, "MCB_tex_42")


if __name__ == "__main__":
    unittest.main(verbosity=2)
