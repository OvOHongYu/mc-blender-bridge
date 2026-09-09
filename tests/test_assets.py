# -*- coding: utf-8 -*-
"""MCBA1 烘焙 / 加载 / 模型注入测试（合成资源包，不依赖真实 MC 资产）。"""
import io
import json
import os
import struct
import sys
import tempfile
import unittest
import zipfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

try:
    from PIL import Image
except ImportError:                       # pragma: no cover
    Image = None

from mc_bridge.core import assets as A
from mc_bridge.core import blocks as B
from mc_bridge.core import mesher

from test_codec_mesher import make_payload, block_idx


def _png(w=16, h=16, rgba=(200, 40, 40, 255)):
    img = Image.new("RGBA", (w, h), rgba)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _synthetic_pack():
    """构造最小资源包：test_stairs（变体）+ test_fence（multipart）。"""
    slab = {
        "textures": {"tex": "minecraft:block/test_tex"},
        "elements": [{
            "from": [0, 0, 0], "to": [16, 8, 16],
            "faces": {d: {"uv": [0, 0, 16, 16], "texture": "#tex"}
                      for d in ("up", "down", "north", "south", "east", "west")},
        }],
    }
    post = {
        "textures": {"tex": "minecraft:block/test_tex"},
        "elements": [{
            "from": [6, 0, 6], "to": [10, 16, 10],
            "faces": {d: {"uv": [6, 0, 10, 16], "texture": "#tex"}
                      for d in ("up", "down", "north", "south", "east", "west")},
        }],
    }
    cube = {
        "textures": {"tex": "minecraft:block/test_tex"},
        "elements": [{
            "from": [0, 0, 0], "to": [16, 16, 16],
            "faces": {d: {"uv": [0, 0, 16, 16], "texture": "#tex"}
                      for d in ("down", "north", "south", "east", "west")},
        }],
    }
    cube["elements"][0]["faces"]["up"] = {
        "uv": [0, 0, 16, 16], "texture": "#tex", "tintindex": 0}
    files = {
        "assets/minecraft/blockstates/test_stairs.json": {
            "variants": {
                "facing=east,half=bottom": {"model": "minecraft:block/test_slab"},
                "facing=west,half=bottom": {"model": "minecraft:block/test_slab",
                                            "y": 90},
            }
        },
        "assets/minecraft/blockstates/test_fence.json": {
            "multipart": [
                {"apply": {"model": "minecraft:block/test_post"}},
                {"when": {"north": "true"},
                 "apply": {"model": "minecraft:block/test_post", "y": 90}},
            ]
        },
        "assets/minecraft/blockstates/test_grass.json": {
            "variants": {"": {"model": "minecraft:block/test_cube"}}
        },
        "assets/minecraft/blockstates/chest.json": {
            "variants": {"": {"model": "minecraft:block/test_chest"}}
        },
        "assets/minecraft/models/block/test_slab.json": slab,
        "assets/minecraft/models/block/test_post.json": post,
        "assets/minecraft/models/block/test_cube.json": cube,
        # 方块实体：只有 particle、无 elements -> 走代理模型
        "assets/minecraft/models/block/test_chest.json": {
            "textures": {"particle": "minecraft:block/test_tex"}},
        "assets/minecraft/textures/block/test_tex.png": _png(),
    }
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            if isinstance(data, bytes):
                z.writestr(name, data)
            else:
                z.writestr(name, json.dumps(data))
    return path


@unittest.skipIf(Image is None, "需要 Pillow")
class TestBakeAndLoad(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import bake_assets
        cls.zip_path = _synthetic_pack()
        cls.mcba = tempfile.mktemp(suffix=".mcba")
        pack = bake_assets.ResourcePack([cls.zip_path])
        baker = bake_assets.Baker(pack, "test")
        baker.run(cls.mcba)
        cls.pack = A.AssetPack.load(cls.mcba)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.zip_path, cls.mcba):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_textures(self):
        tid = self.pack.tex_id("minecraft:block/test_tex")
        self.assertIsNotNone(tid)
        (w, h), rgba = self.pack.texture_rgba(tid)
        self.assertEqual((w, h), (16, 16))
        self.assertEqual(len(rgba), 16 * 16 * 4)
        png = self.pack.texture_png(tid)
        img = Image.open(io.BytesIO(png))
        self.assertEqual(img.size, (16, 16))

    def test_default_faces(self):
        faces = self.pack.default_faces("minecraft:test_stairs")
        self.assertIsNotNone(faces)
        self.assertEqual(len(faces), 3)
        self.assertEqual(self.pack.tex_names[faces[0]], "minecraft:block/test_tex")

    def test_variant_match(self):
        vi = self.pack.variant_indices(
            "minecraft:test_stairs", {"facing": "east", "half": "bottom"})
        self.assertEqual(len(vi), 1)
        quads = self.pack.variants[vi[0]]
        self.assertEqual(len(quads), 6)
        ys = [v[1] for verts, *_ in quads for v in verts]
        self.assertEqual(min(ys), 0)
        self.assertEqual(max(ys), 8)             # 半砖高度
        # 无匹配属性 -> 无变体
        self.assertEqual(
            self.pack.variant_indices("minecraft:test_stairs", {"facing": "up"}), [])

    def test_classify(self):
        # 半砖模型 -> NONCUBE（供存档模式兜底分类）
        self.assertEqual(self.pack.classify("minecraft:test_stairs"), 5)

    def test_proxy_model(self):
        # 方块实体（无 elements）-> 代理模型几何 + NONCUBE + use_model
        self.assertEqual(self.pack.classify("minecraft:chest"), 5)
        self.assertTrue(self.pack.use_model("minecraft:chest"))
        vi = self.pack.variant_indices("minecraft:chest", {})
        self.assertEqual(len(vi), 1)
        self.assertGreater(len(self.pack.variants[vi[0]]), 0)

    def test_tint_face(self):
        # 只有顶面声明 tintindex -> 掩码 bit0；顶面顶点色带绿色，侧面不染色
        self.assertEqual(self.pack.tint_mask("minecraft:test_grass"), 1)
        cls = int(self.pack.classify("minecraft:test_grass"))
        pal = [(0, "minecraft:air"), (cls, "minecraft:test_grass")]
        idx = np.zeros(4096, np.uint16)
        idx[0] = 1
        payloads = {(0, 0): make_payload(secs=[(pal, idx)])}
        quads, pal_out, _, models = mesher.mesh_payload(payloads, pack=self.pack)
        verts = np.array([q[0] for q in quads], np.int16)
        dirs = np.array([q[1] for q in quads], np.uint8)
        blks = np.array([q[2] for q in quads], np.uint16)
        aos = np.array([q[3] for q in quads], np.uint8).reshape(-1, 4)
        geo = mesher.geo_from_arrays(verts, dirs, blks, aos,
                                     [n for _, n in pal_out], models=models,
                                     pack=self.pack)
        top = geo["vcol"][int(np.nonzero(dirs == 2)[0][0]) * 4]
        side = geo["vcol"][int(np.nonzero(dirs == 4)[0][0]) * 4]
        self.assertGreater(int(top[1]), int(top[0]))
        self.assertEqual(int(side[0]), int(side[1]))

    def test_multipart(self):
        vi = self.pack.variant_indices(
            "minecraft:test_fence", {"north": "true", "east": "false"})
        self.assertEqual(len(vi), 2)             # 柱子 + 北侧横杆

    def test_model_injection(self):
        name = "minecraft:test_stairs[facing=east,half=bottom]"
        pal, idx = block_idx({(8, 0, 8): name}, "x")
        payloads = {(0, 0): make_payload(secs=[(pal, idx)])}
        quads, pal_out, _, models = mesher.mesh_payload(payloads, pack=self.pack)
        self.assertTrue(models)
        # 该方块的完整方块近似面已被移除
        gid = {n: i for i, (c, n) in enumerate(pal_out)}[name]
        self.assertFalse(any(q[2] == gid for q in quads))
        # 模型几何落在正确格子（padded 9 -> 局部 8 -> 1/16 单位 128）
        mv = np.array([m[0] for m in models], np.int64)
        self.assertGreaterEqual(mv[:, :, 0].min(), 128)
        self.assertLessEqual(mv[:, :, 0].max(), 144)
        self.assertLessEqual(mv[:, :, 1].max(), 8)

    def test_geo_models(self):
        name = "minecraft:test_stairs[facing=east,half=bottom]"
        pal, idx = block_idx({(8, 0, 8): name}, "x")
        payloads = {(0, 0): make_payload(secs=[(pal, idx)])}
        quads, pal_out, _, models = mesher.mesh_payload(payloads, pack=self.pack)
        verts = np.array([q[0] for q in quads], np.int16) if quads else np.zeros((0, 4, 3), np.int16)
        dirs = np.array([q[1] for q in quads], np.uint8)
        blks = np.array([q[2] for q in quads], np.uint16)
        aos = np.array([q[3] for q in quads], np.uint8).reshape(-1, 4)
        geo = mesher.geo_from_arrays(verts, dirs, blks,
                                     aos, [n for _, n in pal_out], models=models,
                                     pack=self.pack)
        kinds = {d[0] for d in geo["mats"]}
        self.assertIn("tex", kinds)              # 模型面用贴图材质
        self.assertEqual(geo["verts"].shape[0], geo["nq"] * 4)
        # 模型 UV 已归一化到 0..1 附近
        self.assertLessEqual(float(np.abs(geo["uv"]).max()), 1.0001)



def _be_pack():
    """合成资源包：只含原版箱子方块状态 + 方块实体贴图（走原版几何路径）。"""
    files = {
        "assets/minecraft/blockstates/chest.json": {
            "variants": {"": {"model": "minecraft:block/chest"}}},
        "assets/minecraft/models/block/chest.json": {
            "textures": {"particle": "minecraft:block/test_tex"}},
        "assets/minecraft/textures/block/test_tex.png": _png(),
        "assets/minecraft/textures/entity/chest/normal.png": _png(64, 64),
    }
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            if isinstance(data, bytes):
                z.writestr(name, data)
            else:
                z.writestr(name, json.dumps(data))
    return path


@unittest.skipIf(Image is None, "需要 Pillow")
class TestVanillaBlockEntity(unittest.TestCase):
    """原版方块实体几何（tools/vanilla_be_models.json + 渲染器矩阵变换）。"""

    @classmethod
    def setUpClass(cls):
        import bake_assets
        cls.zip_path = _be_pack()
        cls.mcba = tempfile.mktemp(suffix=".mcba")
        pack = bake_assets.ResourcePack([cls.zip_path])
        baker = bake_assets.Baker(pack, "test")
        baker.run(cls.mcba)
        cls.pack = A.AssetPack.load(cls.mcba)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.zip_path, cls.mcba):
            try:
                os.remove(p)
            except OSError:
                pass

    def _quads(self, props):
        vi = self.pack.variant_indices("minecraft:chest", props)
        self.assertEqual(len(vi), 1)
        return self.pack.variants[vi[0]]

    def test_uses_vanilla_geometry(self):
        # 原版单箱：箱体 1..15 × 0..10、箱盖 9..14、锁扣 7..11，共 3×6 面
        quads = self._quads({"facing": "south", "type": "single"})
        self.assertEqual(len(quads), 18)
        xs = [v[0] for q in quads for v in q[0]]
        ys = [v[1] for q in quads for v in q[0]]
        self.assertEqual((min(xs), max(xs)), (1, 15))
        self.assertEqual((min(ys), max(ys)), (0, 14))
        texs = {self.pack.tex_names[q[2]] for q in quads}
        self.assertEqual(texs, {"minecraft:entity/chest/normal"})
        self.assertEqual({q[4] for q in quads}, {0})       # 方块实体不做面剔除

    def test_facing_rotation(self):
        # facing=north 时绕方块中心旋转 180°，锁扣转到北面（z 0..1）
        north = self._quads({"facing": "north", "type": "single"})
        zs = [v[2] for q in north for v in q[0]]
        self.assertEqual((min(zs), max(zs)), (0, 15))
        south = self._quads({"facing": "south", "type": "single"})
        zs = [v[2] for q in south for v in q[0]]
        self.assertEqual((min(zs), max(zs)), (1, 16))

    def test_missing_props_falls_back(self):
        # 无属性时仍给出兜底变体（不会导致方块消失）
        self.assertTrue(self.pack.variant_indices("minecraft:chest", {}))

    def test_be_specs_table(self):
        import bake_assets as bk
        for name in ("minecraft:chest", "minecraft:white_bed", "minecraft:oak_sign",
                     "minecraft:oak_wall_sign", "minecraft:oak_hanging_sign",
                     "minecraft:white_banner", "minecraft:white_wall_banner",
                     "minecraft:skeleton_skull", "minecraft:player_head",
                     "minecraft:white_shulker_box", "minecraft:conduit",
                     "minecraft:decorated_pot"):
            self.assertTrue(bk._be_specs(name), name)
        for name in ("minecraft:stone", "minecraft:oak_planks",
                     "minecraft:piston_head", "minecraft:bell", "minecraft:lectern"):
            self.assertIsNone(bk._be_specs(name), name)

    def test_be_model_quads_transform(self):
        import bake_assets as bk
        saved = bk._BE_DATA
        try:
            bk._BE_DATA = {"unit": {"texW": 16, "texH": 16, "parts": [
                {"name": "root", "cuboids": [], "children": [
                    {"name": "box", "cuboids": [{"faces": [
                        {"n": [0, 0, 1],
                         "v": [[0, 0, 16, 0.0, 1.0], [16, 0, 16, 1.0, 1.0],
                               [16, 16, 16, 1.0, 0.0], [0, 16, 16, 0.0, 0.0]]},
                    ]}]}]}]}}
            # 平移 0.5 方块 = 8/16；UV 的 v 翻转为 Blender 约定
            q = bk.be_model_quads("unit", [("t", (0.5, 0.0, 0.0))],
                                  lambda n: 0, tex="t")
            verts, d, _tid, _tint, _cull, uvs = q[0]
            self.assertEqual(d, 4)
            self.assertEqual(verts[0], (8.0, 0.0, 16.0))
            self.assertEqual(uvs[0], (0.0, 0.0))
            self.assertEqual(uvs[2], (1.0, 1.0))
            # 镜像（负行列式）-> 反转绕序，保持外向 CCW
            q = bk.be_model_quads("unit", [("s", (-1.0, 1.0, 1.0))],
                                  lambda n: 0, tex="t")
            xs = [v[0] for v in q[0][0]]
            self.assertEqual((min(xs), max(xs)), (-16.0, 0.0))
            self.assertEqual(q[0][0][0], (0.0, 16.0, 16.0))   # 绕序已反转
            # 退化面（零面积）被丢弃
            bk._BE_DATA["unit"]["parts"][0]["children"][0]["cuboids"][0]["faces"].append(
                {"n": [1, 0, 0],
                 "v": [[8, 8, 8, 0.0, 0.0], [8, 8, 8, 1.0, 0.0],
                       [8, 8, 8, 1.0, 1.0], [8, 8, 8, 0.0, 1.0]]})
            q = bk.be_model_quads("unit", [], lambda n: 0, tex="t")
            self.assertEqual(len(q), 1)
        finally:
            bk._BE_DATA = saved


def _subpixel_pack():
    """合成资源包：模组模型常见的亚像素坐标（0.25/0.75 像素）与零厚度面。"""
    model = {
        "textures": {"tex": "minecraft:block/test_tex"},
        "elements": [
            {   # 零厚度平面（y 恒为 0.25 像素）
                "from": [0.25, 0.25, 0.5], "to": [15.75, 0.25, 12.5],
                "faces": {d: {"uv": [0, 0, 16, 16], "texture": "#tex"}
                          for d in ("up", "down")},
            },
            {   # 0.5 像素薄板：按 1/16 单位取整会塌成零体积
                "from": [0.25, 0.5, 0.25], "to": [15.75, 15.5, 0.75],
                "faces": {d: {"uv": [0, 0, 16, 16], "texture": "#tex"}
                          for d in ("up", "down", "north", "south", "east", "west")},
            },
        ],
    }
    files = {
        "assets/minecraft/blockstates/test_subpixel.json": {
            "variants": {"": {"model": "minecraft:block/test_subpixel"}}},
        "assets/minecraft/models/block/test_subpixel.json": model,
        "assets/minecraft/textures/block/test_tex.png": _png(),
    }
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data if isinstance(data, bytes) else json.dumps(data))
    return path


def _v1_pack_bytes():
    """手工构造 v1 资产包（顶点 1/16 单位整数），验证旧包向后兼容。"""
    out = [b"MCBA1", struct.pack("<B", 1), struct.pack("<H", 6) + b"1.21.1"]
    rgba = bytes([255, 0, 0, 255]) * 256
    name = b"minecraft:block/t"
    out.append(struct.pack("<I", 1))
    out.append(struct.pack("<H", len(name)) + name
               + struct.pack("<HHI", 16, 16, len(rgba)) + rgba)
    rec = b"".join(struct.pack("<3h", *v) for v in
                   ((0, 0, 0), (16, 0, 0), (16, 0, 16), (0, 0, 16)))
    rec += struct.pack("<BHbB", 2, 0, -1, 0)
    rec += struct.pack("<8f", 0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0)
    out.append(struct.pack("<I", 1) + struct.pack("<H", 1) + rec)
    out.append(struct.pack("<I", 0))          # blockstates
    out.append(struct.pack("<I", 0))          # 方块信息
    return b"".join(out)


@unittest.skipIf(Image is None, "需要 Pillow")
class TestSubPixelModels(unittest.TestCase):
    """模组模型（Blockbench）亚像素坐标与零厚度面的烘焙保真（MCBA v2）。"""

    @classmethod
    def setUpClass(cls):
        import bake_assets
        cls.zip_path = _subpixel_pack()
        cls.mcba = tempfile.mktemp(suffix=".mcba")
        pack = bake_assets.ResourcePack([cls.zip_path])
        baker = bake_assets.Baker(pack, "test")
        baker.run(cls.mcba)
        cls.pack = A.AssetPack.load(cls.mcba)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.zip_path, cls.mcba):
            try:
                os.remove(p)
            except OSError:
                pass

    def _quads(self):
        vi = self.pack.variant_indices("minecraft:test_subpixel", {})
        self.assertEqual(len(vi), 1)
        return self.pack.variants[vi[0]]

    def test_round_trip_is_v2(self):
        with open(self.mcba, "rb") as f:
            self.assertEqual(f.read(6), b"MCBA1\x02")

    def test_fractional_coords_survive(self):
        quads = self._quads()
        xs = [v[0] for q in quads for v in q[0]]
        zs = [v[2] for q in quads for v in q[0]]
        # 0.25 / 0.5 / 15.75 / 12.5 像素必须保留（容差 = 1/256 方块 = 1/16 像素）
        self.assertAlmostEqual(min(xs), 0.25, delta=1 / 16.0)
        self.assertAlmostEqual(max(xs), 15.75, delta=1 / 16.0)
        self.assertAlmostEqual(min(zs), 0.25, delta=1 / 16.0)
        self.assertAlmostEqual(max(zs), 12.5, delta=1 / 16.0)

    def test_thin_geometry_not_collapsed(self):
        # 0.5 像素薄板的各面不能退化为零面积
        def area(v):
            v = np.asarray(v, np.float64)
            return 0.5 * (np.linalg.norm(np.cross(v[1] - v[0], v[2] - v[0]))
                          + np.linalg.norm(np.cross(v[2] - v[0], v[3] - v[0])))
        areas = [area(q[0]) for q in self._quads()]
        self.assertTrue(all(a > 1e-6 for a in areas), areas)
        ys = sorted({round(v[1], 6) for q in self._quads() for v in q[0]})
        self.assertIn(0.5, ys)
        self.assertIn(15.5, ys)

    def test_mesher_injection_keeps_precision(self):
        pal, idx = block_idx({(8, 8, 8): "minecraft:test_subpixel"}, "x")
        payloads = {(0, 0): make_payload(secs=[(pal, idx)])}
        quads, pal_out, _, models = mesher.mesh_payload(payloads, pack=self.pack)
        self.assertTrue(models)
        mv = np.array([m[0] for m in models], np.float64)
        # padded 局部格 8 -> 128（1/16 单位）；亚像素顶点再偏 0.25 像素
        self.assertAlmostEqual(mv[:, :, 0].min(), 128.25, delta=0.1)
        self.assertAlmostEqual(mv[:, :, 2].min(), 128.25, delta=0.1)

    def test_v1_pack_still_loads(self):
        old = A.AssetPack.from_bytes(_v1_pack_bytes())
        self.assertEqual(len(old.variants), 1)
        verts = old.variants[0][0][0]
        self.assertEqual(verts[0], (0, 0, 0))
        self.assertEqual(verts[2], (16, 0, 16))
        self.assertEqual(old.tex_names[0], "minecraft:block/t")
# (x, y) -> (east, up, south) 的像，取自 MC 1.21.1 本体探针：
#   ModelRotation.get(x, y).getRotation().getMatrix().transformDirection(...)
# （16 组组合全量核对，见 dist/RotProbe.java 临时探针）
MC_ROT_TABLE = {
    (0, 0):     ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    (0, 90):    ((0, 0, 1), (0, 1, 0), (-1, 0, 0)),
    (0, 180):   ((-1, 0, 0), (0, 1, 0), (0, 0, -1)),
    (0, 270):   ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
    (90, 0):    ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
    (90, 90):   ((0, 0, 1), (1, 0, 0), (0, 1, 0)),
    (90, 180):  ((-1, 0, 0), (0, 0, 1), (0, 1, 0)),
    (90, 270):  ((0, 0, -1), (-1, 0, 0), (0, 1, 0)),
    (180, 0):   ((1, 0, 0), (0, -1, 0), (0, 0, -1)),
    (180, 90):  ((0, 0, 1), (0, -1, 0), (1, 0, 0)),
    (180, 180): ((-1, 0, 0), (0, -1, 0), (0, 0, 1)),
    (180, 270): ((0, 0, -1), (0, -1, 0), (-1, 0, 0)),
    (270, 0):   ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
    (270, 90):  ((0, 0, 1), (-1, 0, 0), (0, -1, 0)),
    (270, 180): ((-1, 0, 0), (0, 0, -1), (0, -1, 0)),
    (270, 270): ((0, 0, -1), (1, 0, 0), (0, -1, 0)),
}
_BASIS = ((1, 0, 0), (0, 1, 0), (0, 0, 1))          # 表内分量顺序: east, up, south
_DIR_OF = {(1, 0, 0): 0, (-1, 0, 0): 1, (0, 1, 0): 2,
           (0, -1, 0): 3, (0, 0, 1): 4, (0, 0, -1): 5}


def _rod_pack():
    """合成资源包：细长竖直模型 + chain/end_rod 用的 x/y 旋转组合。"""
    model = {
        "textures": {"tex": "minecraft:block/test_tex"},
        "elements": [{
            "from": [6, 0, 6], "to": [10, 16, 10],
            "faces": {d: {"uv": [0, 0, 4, 16], "texture": "#tex"}
                      for d in ("up", "down", "north", "south", "east", "west")},
        }],
    }
    states = {"variants": {
        "axis=y": {"model": "minecraft:block/test_rod"},
        "axis=x": {"model": "minecraft:block/test_rod", "x": 90, "y": 90},
        "axis=z": {"model": "minecraft:block/test_rod", "x": 90},
    }}
    files = {
        "assets/minecraft/blockstates/test_rod.json": states,
        "assets/minecraft/models/block/test_rod.json": model,
        "assets/minecraft/textures/block/test_tex.png": _png(),
        # uvlock 对照：同一 x=180 旋转，一个锁 UV、一个不锁
        "assets/minecraft/blockstates/test_rod_lock.json": {"variants": {
            "": {"model": "minecraft:block/test_rod", "x": 180, "uvlock": True}}},
        "assets/minecraft/blockstates/test_rod_nolock.json": {"variants": {
            "": {"model": "minecraft:block/test_rod", "x": 180}}},
    }
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data if isinstance(data, bytes) else json.dumps(data))
    return path


@unittest.skipIf(Image is None, "需要 Pillow")
class TestVariantRotation(unittest.TestCase):
    """blockstate 的 x/y 旋转必须与 MC 本体一致（先 x 后 y）。"""

    @classmethod
    def setUpClass(cls):
        import bake_assets
        cls.zip_path = _rod_pack()
        cls.mcba = tempfile.mktemp(suffix=".mcba")
        pack = bake_assets.ResourcePack([cls.zip_path])
        baker = bake_assets.Baker(pack, "test")
        baker.run(cls.mcba)
        cls.pack = A.AssetPack.load(cls.mcba)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.zip_path, cls.mcba):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_vertex_rotation_matches_minecraft(self):
        import bake_assets as bk
        for (xr, yr), expect in MC_ROT_TABLE.items():
            quads = []
            for arm, d in zip(((8.0, 0.0, 0.0), (0.0, 8.0, 0.0), (0.0, 0.0, 8.0)),
                              (0, 2, 4)):
                p0 = (8.0, 8.0, 8.0)
                p1 = (8.0 + arm[0], 8.0 + arm[1], 8.0 + arm[2])
                quads.append(((p0, p1, p1, p1), d, 0, -1, 0, ((0.0, 0.0),) * 4))
            out = bk.apply_variant_transform(quads, xr, yr, False)
            got = tuple(tuple(int(round((q[0][1][i] - 8.0) / 8.0)) for i in range(3))
                        for q in out)
            self.assertEqual(got, expect, "x=%d y=%d" % (xr, yr))

    def test_direction_rotation_matches_minecraft(self):
        import bake_assets as bk
        dirs = ((0, (1, 0, 0)), (2, (0, 1, 0)), (4, (0, 0, 1)),
                (1, (-1, 0, 0)), (3, (0, -1, 0)), (5, (0, 0, -1)))
        for (xr, yr), expect in MC_ROT_TABLE.items():
            for d, vec in dirs:
                if vec in _BASIS:
                    img = expect[_BASIS.index(vec)]
                else:
                    img = tuple(-c for c in
                                expect[_BASIS.index(tuple(-c for c in vec))])
                self.assertEqual(bk._rotate_dir(d, xr, yr), _DIR_OF[img],
                                 "x=%d y=%d dir=%d" % (xr, yr, d))

    def test_uvlock_matches_minecraft(self):
        # MC 1.21.1 AffineTransformations.uvLock 实测矩阵的 2×2 部分（行主序）
        import bake_assets as bk
        cases = {
            (0, 0, 2): (1, 0, 0, 1),
            (0, 90, 2): (0, 1, -1, 0),
            (0, 90, 5): (1, 0, 0, 1),      # y 旋转不动侧面 UV
            (0, 90, 0): (1, 0, 0, 1),
            (90, 0, 2): (-1, 0, 0, -1),
            (90, 0, 3): (1, 0, 0, 1),
            (90, 0, 0): (0, 1, -1, 0),
            (90, 90, 2): (-1, 0, 0, -1),
            (180, 0, 2): (1, 0, 0, 1),     # 倒置楼梯：顶/底面 UV 不变
            (180, 0, 3): (1, 0, 0, 1),
            (180, 0, 5): (-1, 0, 0, -1),   # 侧面 UV 转 180°
            (180, 0, 0): (-1, 0, 0, -1),
            (270, 0, 2): (1, 0, 0, 1),
            (270, 90, 2): (1, 0, 0, 1),
        }
        for (xr, yr, d), want in cases.items():
            m = bk._uvlock_mat(xr, yr, d)
            self.assertEqual((m[0][0], m[0][1], m[1][0], m[1][1]), want,
                             "x=%d y=%d dir=%d" % (xr, yr, d))

    def test_uvlock_locks_side_uvs_only(self):
        # 同一模型 + x=180：uvlock=true 时顶/底面 UV 不变、侧面 UV 反转
        def by_dir(name):
            vi = self.pack.variant_indices(name, {})
            self.assertEqual(len(vi), 1, name)
            out = {}
            for q in self.pack.variants[vi[0]]:
                out.setdefault(q[1], []).append(q[5])
            return out

        plain = by_dir("minecraft:test_rod_nolock")
        lock = by_dir("minecraft:test_rod_lock")
        self.assertEqual(plain[2], lock[2])          # 顶面 UV 一致
        self.assertNotEqual(plain[5], lock[5])       # 侧面 UV 被锁定（不再随模型翻转）
        # 锁定后侧面 u 方向反转（贴图 180° 旋转后仍与世界方向一致）
        pu = [uv[0] for q in plain[5] for uv in q]
        lu = [uv[0] for q in lock[5] for uv in q]
        self.assertAlmostEqual(min(lu), -0.25, places=5)
        self.assertAlmostEqual(max(pu), 0.25, places=5)

    def test_rod_orientation(self):
        # 细长模型：axis=y 竖直；axis=x 躺向 X；axis=z 躺向 Z
        for prop, axis in (("y", 1), ("x", 0), ("z", 2)):
            vi = self.pack.variant_indices("minecraft:test_rod", {"axis": prop})
            self.assertEqual(len(vi), 1, prop)
            quads = self.pack.variants[vi[0]]
            spans = [max(v[i] for q in quads for v in q[0])
                     - min(v[i] for q in quads for v in q[0]) for i in range(3)]
            self.assertEqual(spans.index(max(spans)), axis,
                             "axis=%s spans=%s" % (prop, spans))

if __name__ == "__main__":
    unittest.main(verbosity=2)
