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
from mc_bridge.core import entities
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
        # 动态代码模型：blockstate 指向的模型 JSON 不存在（模型文件不提供）
        "assets/minecraft/blockstates/test_dyn.json": {
            "variants": {"": {"model": "minecraft:block/test_dyn_missing"}}
        },
        # 同一方块的不同状态映射到不同形状的模型（状态粒度信息的关键用例）
        "assets/minecraft/blockstates/test_slab_block.json": {
            "variants": {
                "type=bottom": {"model": "minecraft:block/test_slab"},
                "type=double": {"model": "minecraft:block/test_cube"},
            }
        },
        "assets/minecraft/models/block/test_slab.json": slab,
        "assets/minecraft/models/block/test_post.json": post,
        "assets/minecraft/models/block/test_cube.json": cube,
        # 方块实体：只有 particle、无 elements -> 走代理模型
        "assets/minecraft/models/block/test_chest.json": {
            "textures": {"particle": "minecraft:block/test_tex"}},
        "assets/minecraft/textures/block/test_tex.png": _png(),
        # 画变体（1.21+ 数据驱动）+ 画贴图；1×2 的画贴图是 16×32 竖条，
        # 不能按"高度是宽度整数倍即动画条带"裁剪（回归）
        "data/minecraft/painting_variant/test_tall.json": {
            "width": 1, "height": 2, "asset_id": "minecraft:test_painting"},
        "data/minecraft/painting_variant/test_broken.json": {
            "width": 1, "height": 1, "asset_id": "minecraft:missing"},
        "assets/minecraft/textures/painting/test_painting.png": _png(16, 32),
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

    def test_paintings(self):
        # 1.21+ 数据驱动画变体：width/height/asset_id -> (宽, 高, 贴图 id)
        self.assertEqual(list(self.pack.paintings), ["minecraft:test_tall"])
        w, h, tid = self.pack.paintings["minecraft:test_tall"]
        self.assertEqual((w, h), (1, 2))
        self.assertEqual(self.pack.tex_names[tid], "minecraft:painting/test_painting")
        # 1×2 的画贴图 16×32 必须保留整幅（不得按动画条带裁成 16×16）
        (tw, th), rgba = self.pack.texture_rgba(tid)
        self.assertEqual((tw, th), (16, 32))
        self.assertEqual(len(rgba), 16 * 32 * 4)

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



class TestPaintingGeometry(unittest.TestCase):
    """R5：画实体 NBT -> 平面四边形（尺寸 / 朝向 / UV / 区块局部坐标）。"""

    class _Pack:
        paintings = {"minecraft:test_wide": (2, 1, 7),
                     "minecraft:test_tall": (1, 3, 9)}

    def _quad(self, **kw):
        ent = {"id": "minecraft:painting", "variant": "minecraft:test_wide",
               "facing": 0, "Pos": [8.5, 70.0, 8.0]}
        ent.update(kw)
        quads = entities.painting_quads(ent, self._Pack())
        self.assertIsNotNone(quads)
        self.assertEqual(len(quads), 1)
        return quads[0]

    @staticmethod
    def _normal(verts):
        a, b, c = (np.asarray(v, float) for v in verts[:3])
        n = np.cross(b - a, c - a)
        return n / np.linalg.norm(n)

    def test_facing_normals_and_plane(self):
        # 画面法向 = facing（0=南 1=西 2=北 3=东），平面沿 facing 外移 1/32
        for facing, fwd, plane in ((0, (0, 0, 1), ("z", 8.0 + 1.0 / 32.0)),
                                   (1, (-1, 0, 0), ("x", 8.5 - 1.0 / 32.0)),
                                   (2, (0, 0, -1), ("z", 8.0 - 1.0 / 32.0)),
                                   (3, (1, 0, 0), ("x", 8.5 + 1.0 / 32.0))):
            verts, uv, _tex = self._quad(facing=facing)
            np.testing.assert_allclose(self._normal(verts), fwd, atol=1e-6)
            axis, value = plane
            col = verts[:, 0] if axis == "x" else verts[:, 2]
            np.testing.assert_allclose(col, value, atol=1e-6)
            np.testing.assert_allclose(uv, [(0, 0), (1, 0), (1, 1), (0, 1)],
                                       atol=1e-6)

    def test_size_and_bottom_center(self):
        # 2×1 画：宽沿墙面水平 2 格（x 8.5 为中点），高 1 格，底边在 Pos.y
        verts, _uv, tex = self._quad()
        self.assertEqual(tex, 7)
        self.assertAlmostEqual(float(verts[:, 0].min()), 7.5)
        self.assertAlmostEqual(float(verts[:, 0].max()), 9.5)
        self.assertAlmostEqual(float(verts[:, 1].min()), 70.0)
        self.assertAlmostEqual(float(verts[:, 1].max()), 71.0)
        # 1×3 竖画
        verts, _uv, tex = self._quad(variant="minecraft:test_tall")
        self.assertEqual(tex, 9)
        self.assertAlmostEqual(float(verts[:, 0].min()), 8.0)
        self.assertAlmostEqual(float(verts[:, 0].max()), 9.0)
        self.assertAlmostEqual(float(verts[:, 1].max()), 73.0)

    def test_skips_unknown(self):
        ent = {"id": "minecraft:painting", "variant": "minecraft:nope",
               "facing": 0, "Pos": [8.5, 70.0, 8.0]}
        self.assertIsNone(entities.painting_quads(ent, self._Pack()))
        # 非画实体
        ent["variant"] = "minecraft:test_wide"
        ent["id"] = "minecraft:armor_stand"
        self.assertIsNone(entities.painting_quads(ent, self._Pack()))
        # 缺 Pos / variant
        ent["id"] = "minecraft:painting"
        ent.pop("Pos")
        self.assertIsNone(entities.painting_quads(ent, self._Pack()))

    def test_build_geo_local_coords(self):
        ents = [{"id": "minecraft:painting", "variant": "minecraft:test_wide",
                 "facing": 0, "Pos": [8.5, 70.0, 8.0]},
                {"id": "minecraft:pig", "Pos": [1.0, 2.0, 3.0]}]
        geo = entities.build_geo(ents, self._Pack(), 0, 0, -64)
        self.assertEqual(geo["nq"], 1)
        self.assertEqual(geo["mats"], [("tex", 7)])
        self.assertEqual(geo["tris"], 2)
        # 世界 -> 区块局部：y 相对 y_bottom
        self.assertAlmostEqual(float(geo["verts"][:, 1].min()), 70.0 + 64)
        self.assertEqual(geo["vcol"].min(), 255)          # 实体不上色
        self.assertIsNone(entities.build_geo([], self._Pack(), 0, 0, -64))


_ARMOR_MODEL = {
    "texW": 64, "texH": 64, "tex": "minecraft:entity/armorstand/wood",
    "parts": [{
        "name": "root", "pivot": [0, 0, 0], "rot": [0, 0, 0], "cuboids": [],
        "children": [
            {"name": "body", "pivot": [0, 0, 0], "rot": [0, 0, 0],
             "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                 [-1, 0, 0, 0, 0], [1, 0, 0, 1, 0], [1, 3, 0, 1, 1], [-1, 3, 0, 0, 1]]}]}]},
            {"name": "head", "pivot": [0, 1, 0], "rot": [0, 0, 0],
             "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                 [-1, -7, 0, 0, 0], [1, -7, 0, 1, 0], [1, 0, 0, 1, 1], [-1, 0, 0, 0, 1]]}]}]},
            {"name": "left_arm", "pivot": [5, 2, 0], "rot": [0, 0, 0],
             "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                 [-1, -2, 0, 0, 0], [1, -2, 0, 1, 0], [1, 10, 0, 1, 1], [-1, 10, 0, 0, 1]]}]}]},
            {"name": "right_arm", "pivot": [-5, 2, 0], "rot": [0, 0, 0],
             "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                 [-1, -2, 0, 0, 0], [1, -2, 0, 1, 0], [1, 10, 0, 1, 1], [-1, 10, 0, 0, 1]]}]}]},
            {"name": "base_plate", "pivot": [0, 12, 0], "rot": [0, 0, 0],
             "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                 [-6, 11, 0, 0, 0], [6, 11, 0, 1, 0], [6, 12, 0, 1, 1], [-6, 12, 0, 0, 1]]}]}]},
            {"name": "left_body_stick", "pivot": [0, 0, 0], "rot": [0, 0, 0],
             "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                 [-1, 3, 0, 0, 0], [1, 3, 0, 1, 0], [1, 10, 0, 1, 1], [-1, 10, 0, 0, 1]]}]}]},
            {"name": "right_body_stick", "pivot": [0, 0, 0], "rot": [0, 0, 0],
             "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                 [-1, 3, 0, 0, 0], [1, 3, 0, 1, 0], [1, 10, 0, 1, 1], [-1, 10, 0, 0, 1]]}]}]},
            {"name": "shoulder_stick", "pivot": [0, 0, 0], "rot": [0, 0, 0],
             "cuboids": [{"faces": [{"n": [0, 0, 1], "v": [
                 [-6, 10, 0, 0, 0], [6, 10, 0, 1, 0], [6, 12, 0, 1, 1], [-6, 12, 0, 0, 1]]}]}]},
        ],
    }],
}


class TestArmorStandGeometry(unittest.TestCase):
    """R5：盔甲架运行时装配（分层模型 + Pose + 部件显隐 + 全局变换）。"""

    class _Pack:
        entity_models = {"armor_stand": _ARMOR_MODEL}
        paintings = {}

        @staticmethod
        def tex_id(name):
            return {"minecraft:entity/armorstand/wood": 7}.get(name)

    def _quads(self, **kw):
        ent = {"id": "minecraft:armor_stand", "Pos": [8.5, 70.0, 8.5],
               "Rotation": [0.0, 0.0]}
        ent.update(kw)
        quads = entities.armor_stand_quads(ent, self._Pack())
        self.assertIsNotNone(quads)
        return quads

    def test_default_parts_and_bottom_align(self):
        quads = self._quads()
        # 无 ShowArms：躯干 + 头 + 两木杆 + 肩杆 + 底座 = 6 面
        self.assertEqual(len(quads), 6)
        v = np.concatenate([q[0] for q in quads])
        # 底边对齐 Pos.y；头在顶部（总高 = 30/16）
        self.assertAlmostEqual(float(v[:, 1].min()), 70.0, places=5)
        self.assertAlmostEqual(float(v[:, 1].max()) - float(v[:, 1].min()), 1.875, places=5)
        self.assertTrue(all(q[2] == 7 for q in quads))

    def test_pose_rotates_head(self):
        flat = self._quads()
        # Head pitch 90°：头向前倾 -> 头顶沿 +Z 前移
        tilted = self._quads(Pose={"Head": [90.0, 0.0, 0.0]})
        fz = np.concatenate([q[0] for q in flat])[:, 2].max()
        tz = np.concatenate([q[0] for q in tilted])[:, 2].max()
        self.assertGreater(tz, fz + 0.05)

    def test_show_arms_hides_sticks(self):
        quads = self._quads(ShowArms=1)
        # body + head + 双臂 + 底座 = 5 面（木杆/肩杆隐藏）
        self.assertEqual(len(quads), 5)

    def test_no_base_plate_and_small(self):
        self.assertEqual(len(self._quads(NoBasePlate=1)), 5)   # 底座隐藏
        q = self._quads(Small=1)
        v = np.concatenate([qq[0] for qq in q])
        self.assertAlmostEqual(float(v[:, 1].max()) - float(v[:, 1].min()),
                               0.9375, places=5)                # 高度减半

    def test_marker_invisible_skipped(self):
        self.assertIsNone(entities.armor_stand_quads(
            {"id": "minecraft:armor_stand", "Pos": [1, 1, 1], "Marker": 1}, self._Pack()))
        self.assertIsNone(entities.armor_stand_quads(
            {"id": "minecraft:armor_stand", "Pos": [1, 1, 1], "Invisible": 1}, self._Pack()))

    def test_uv_flipped_for_blender(self):
        # _assemble_model（绕序反转之前）已翻转 UV v 轴：v_mc=0 -> 1
        parts = entities._assemble_model(_ARMOR_MODEL, {}, {})
        uv = parts[0][1]                       # body 面（第一个部件）
        self.assertAlmostEqual(float(uv[0, 1]), 1.0)   # v_mc=0
        self.assertAlmostEqual(float(uv[2, 1]), 0.0)   # v_mc=1


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

    def test_bed_stays_in_its_own_block(self):
        # 原版 BedBlockEntityRenderer.render 的**世界路径**每半张床各画在自己那一格
        # （renderPart 的末参 isFoot 恒为 false），故不应有 z 平移；
        # 回归：曾把背包路径的 -1.0 也套到世界路径，整床沿 -Z（Blender +Y）偏 1 格。
        import bake_assets as bk
        specs = bk._be_specs("minecraft:white_bed")
        self.assertTrue(specs)
        seen = set()
        for spec in specs:
            props = dict(spec["props"])
            seen.add((props["facing"], props["part"]))
            for layer in spec["layers"]:
                quads = bk.be_model_quads(layer["model"], spec["ops"],
                                          lambda _n: 0, tex=layer["tex"])
                self.assertTrue(quads, props)
                for axis, name in enumerate("xyz"):
                    lo = min(v[axis] for q in quads for v in q[0])
                    hi = max(v[axis] for q in quads for v in q[0])
                    self.assertGreaterEqual(lo, -1e-6, "%s 轴%s 下界" % (props, name))
                    self.assertLessEqual(hi, 16 + 1e-6, "%s 轴%s 上界" % (props, name))
        self.assertEqual(len(seen), 8)          # 4 朝向 × 头/尾

    def test_bed_legs_at_outer_ends(self):
        # FACING 由 FOOT 指向 HEAD（BedBlock.getDirectionTowardsOtherPart）：
        # 头/尾的腿应各在远离另一半的外侧，两格拼合后长轴恰好 2 格。
        import bake_assets as bk
        dirvec = {"north": (0, -1), "south": (0, 1), "west": (-1, 0), "east": (1, 0)}
        specs = bk._be_specs("minecraft:white_bed")
        for spec in specs:
            props = dict(spec["props"])
            facing, part = props["facing"], props["part"]
            mkey = spec["layers"][0]["model"]
            legs = bk.be_model_quads(mkey, spec["ops"], lambda _n: 0,
                                     tex=spec["layers"][0]["tex"],
                                     skip=("main",))
            self.assertTrue(legs, props)
            cx = sum(v[0] for q in legs for v in q[0]) / (4 * len(legs)) - 8.0
            cz = sum(v[2] for q in legs for v in q[0]) / (4 * len(legs)) - 8.0
            # 头：外侧 = +facing；尾：外侧 = -facing
            sign = 1 if part == "head" else -1
            fx, fz = dirvec[facing]
            self.assertGreater(cx * sign * fx + cz * sign * fz, 0.0, props)

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
    """模组模型（Blockbench）亚像素坐标与零厚度面的烘焙保真（MCBA v2/v3）。"""

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

    def test_round_trip_is_v6(self):
        with open(self.mcba, "rb") as f:
            self.assertEqual(f.read(6), b"MCBA1\x06")

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
        # 真值来自 dist/_uvlock_rows.txt（本体 getRow 导出，非本项目实现）
        import bake_assets as bk
        cases = {
            (0, 0, 2): (1, 0, 0, 1),
            (0, 90, 2): (0, -1, 1, 0),     # y 旋转锁顶/底面：转 -90°
            (0, 90, 5): (1, 0, 0, 1),      # y 旋转不动侧面 UV
            (0, 90, 0): (1, 0, 0, 1),
            (90, 0, 2): (-1, 0, 0, -1),
            (90, 0, 3): (1, 0, 0, 1),
            (90, 0, 0): (0, -1, 1, 0),
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

    def test_uvlock_rect_and_rotation_match_minecraft(self):
        # 原版 BakedQuadFactory.uvLock 除矩形外还会改写面的 rotation 索引，
        # 索引决定角点↔矩形角对应；真值取自本体（dist/_uvlock_truth.txt）
        import bake_assets as bk
        # (x, y, 面索引, 原矩形, 面部 rotation) -> (锁定矩形, 新 rotation)
        cases = {
            (0, 90, 2, (0, 0, 16, 8), 0): ((8, 0, 16, 16), 270),
            (0, 90, 3, (0, 0, 16, 8), 0): ((0, 0, 8, 16), 90),
            (0, 180, 2, (0, 0, 16, 8), 0): ((0, 8, 16, 16), 180),
            (0, 270, 2, (0, 0, 16, 8), 0): ((0, 0, 8, 16), 90),
            (0, 90, 5, (0, 0, 16, 8), 0): ((0, 0, 16, 8), 0),   # 侧面不受 y 旋转影响
            (180, 0, 5, (0, 0, 16, 8), 0): ((0, 8, 16, 16), 180),
        }
        for (xr, yr, d, rect, frot), (want_rect, want_rot) in cases.items():
            got_rect, got_rot = bk._uvlock_rect(rect[0], rect[1], rect[2],
                                                rect[3], frot, xr, yr, d)
            self.assertEqual(tuple(round(v, 4) for v in got_rect), want_rect,
                             "rect x=%d y=%d dir=%d" % (xr, yr, d))
            self.assertEqual(got_rot, want_rot,
                             "rotation x=%d y=%d dir=%d" % (xr, yr, d))

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
        # 锁定后侧面 u 方向反转（贴图 180° 旋转后仍与世界方向一致）；
        # x=180 的 uvlock 含绕中心的平移，u∈[0.75,1.0]（未经 Repeat 折返）
        pu = [uv[0] for q in plain[5] for uv in q]
        lu = [uv[0] for q in lock[5] for uv in q]
        self.assertAlmostEqual(min(lu), 0.75, places=5)
        self.assertAlmostEqual(max(lu), 1.0, places=5)
        self.assertAlmostEqual(min(pu), 0.0, places=5)
        self.assertAlmostEqual(max(pu), 0.25, places=5)

    def test_zero_thickness_model_dedupes_coincident_faces(self):
        # 零厚度「纸」模型（cross.json 等）：同一片纸的 north+south 两面顶点集合
        # 相同、绕序相反，会共面重叠 Z-Fighting；烘焙时合并为 1（Blender 双面渲染够用）。
        import bake_assets as bk
        face = {"uv": [0, 0, 16, 16], "texture": "#tex"}
        model = {
            "textures": {"tex": "minecraft:block/t"},
            "elements": [
                {"from": [0.8, 0, 8], "to": [15.2, 16, 8],
                 "faces": {"north": dict(face), "south": dict(face)}},
                {"from": [8, 0, 0.8], "to": [8, 16, 15.2],
                 "faces": {"west": dict(face), "east": dict(face)}},
            ],
        }
        quads = bk.build_model_quads(model, lambda _n: 3)
        self.assertEqual(len(quads), 2)          # 2 片纸 × 1 面（原为每片 2 = 4 面）

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


@unittest.skipIf(Image is None, "需要 Pillow")
class TestClassOverrides(unittest.TestCase):
    """手工标注表（mcbridge-classes.json）：覆盖 class / use_model / 贴图。"""

    @classmethod
    def setUpClass(cls):
        import bake_assets
        cls.zip_path = _synthetic_pack()
        cls.mcba = tempfile.mktemp(suffix=".mcba")
        cls.json_path = tempfile.mktemp(suffix=".json")
        # test_grass 是整立方体（启发式判 OPAQUE）、test_fence 非整立方（NONCUBE）；
        # test_dyn 无模型 JSON，只有给出 tex 才能成条目
        raw = {"minecraft:test_grass": {"class": "cutout", "use_model": 1},
               "minecraft:test_fence": "transparent",
               "minecraft:barrel": {"class": "noncube", "use_model": 0},
               "minecraft:test_dyn": {"class": "cutout",
                                      "tex": "minecraft:block/test_tex"},
               "minecraft:no_such_block": "liquid"}
        with open(cls.json_path, "w", encoding="utf-8") as f:
            json.dump(raw, f)
        cls.overrides = bake_assets.load_overrides(cls.json_path)
        pack = bake_assets.ResourcePack([cls.zip_path])
        cls.baker = bake_assets.Baker(pack, "test", None, cls.overrides)
        cls.baker.run(cls.mcba)
        cls.pack = A.AssetPack.load(cls.mcba)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.zip_path, cls.mcba, cls.json_path):
            try:
                os.remove(p)
            except OSError:
                pass

    def test_override_replaces_heuristic(self):
        self.assertEqual(self.pack.classify("minecraft:test_grass"), B.CUTOUT)
        self.assertEqual(self.pack.classify("minecraft:test_fence"),
                         B.TRANSPARENT)
        self.assertEqual(self.baker.overrides_applied,
                         {"minecraft:test_grass", "minecraft:test_fence",
                          "minecraft:test_dyn"})

    def test_unlisted_block_keeps_heuristic(self):
        self.assertEqual(self.pack.classify("minecraft:test_stairs"), B.NONCUBE)

    def test_use_model_override(self):
        # test_grass 是整立方体（启发式 use_model=0），标注翻成 1
        self.assertTrue(self.pack.use_model("minecraft:test_grass"))

    def test_tex_override_unlocks_dynamic_model(self):
        # 无模型 JSON 的方块：给出 tex 后才有条目（贴图 = 标注值）
        tid = self.pack.tex_id("minecraft:block/test_tex")
        self.assertEqual(self.pack.default_faces("minecraft:test_dyn"),
                         (tid, tid, tid))
        self.assertEqual(self.pack.classify("minecraft:test_dyn"), B.CUTOUT)

    def test_parse_compact_and_full_forms(self):
        self.assertEqual(self.overrides["minecraft:test_grass"],
                         {"class": B.CUTOUT, "use_model": 1, "tex": None})
        self.assertEqual(self.overrides["minecraft:test_fence"],
                         {"class": B.TRANSPARENT, "use_model": None,
                          "tex": None})
        self.assertEqual(self.overrides["minecraft:barrel"],
                         {"class": B.NONCUBE, "use_model": 0, "tex": None})
        self.assertEqual(self.overrides["minecraft:test_dyn"]["tex"],
                         ("minecraft:block/test_tex", "minecraft:block/test_tex",
                          "minecraft:block/test_tex"))
        # 未生效的标注仍被记录（main 会据此告警）
        self.assertIn("minecraft:no_such_block", self.overrides)

    def test_rejects_invalid_entries(self):
        import bake_assets
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"a:bad": "nope", "a:range": 9, "a:bool": True,
                           "a:empty": {}, "a:um": {"use_model": 7},
                           "a:tex": {"tex": {"top": ""}}, "a:list": [1]}, f)
            ov = bake_assets.load_overrides(path)
        finally:
            os.remove(path)
        self.assertEqual(ov, {})

    def test_none_path_is_empty(self):
        import bake_assets
        self.assertEqual(bake_assets.load_overrides(None), {})

    def test_non_object_exits(self):
        import bake_assets
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("[1, 2]")
            with self.assertRaises(SystemExit):
                bake_assets.load_overrides(path)
        finally:
            os.remove(path)


@unittest.skipIf(Image is None, "需要 Pillow")
class TestAnimMeta(unittest.TestCase):
    """动画贴图帧高：.mcmeta 权威，缺失时退回尺寸启发式。"""

    def test_meta_frame_height(self):
        import bake_assets as B_  # noqa: N814
        eh = B_._anim_frame_height
        # 显式 height 优先（非方形帧）
        meta = json.dumps({"animation": {"height": 8}}).encode()
        self.assertEqual(eh(meta, 16, 64), 8)
        # 无 height：按 frames 数量均分
        meta = json.dumps({"animation": {"frames": [0, 1, 2, 3]}}).encode()
        self.assertEqual(eh(meta, 16, 64), 16)
        # 无 animation 段（仅 mipmap）-> 不是动画
        meta = json.dumps({"texture": {"blur": True}}).encode()
        self.assertIsNone(eh(meta, 16, 64))
        # 无 .mcmeta / 非法 JSON -> 交由尺寸启发式
        self.assertIsNone(eh(None, 16, 64))
        self.assertIsNone(eh(b"{ not json", 16, 64))
        # animation 存在但只有一帧 -> 返回 h（不裁剪）
        meta = json.dumps({"animation": {}}).encode()
        self.assertEqual(eh(meta, 16, 16), 16)

    def test_decode_crops_animated(self):
        import bake_assets as B_  # noqa: N814
        png = _png(w=16, h=64)
        # 有 animation 声明 -> 裁到首帧
        w, h, rgba = B_._decode_png(png, json.dumps({"animation": {}}).encode())
        self.assertEqual((w, h), (16, 16))
        self.assertEqual(len(rgba), 16 * 16 * 4)
        # 无 .mcmeta 的竖直条带 -> 尺寸启发式同样裁首帧
        w, h, _ = B_._decode_png(png)
        self.assertEqual((w, h), (16, 16))
        # 方形贴图不受影响
        w, h, _ = B_._decode_png(_png(w=16, h=16))
        self.assertEqual((w, h), (16, 16))

@unittest.skipIf(Image is None, "需要 Pillow")
class TestStateGranularity(unittest.TestCase):
    """MCBA v3：class / tintMask / useModel / 默认面按**方块状态**解析。

    同一方块的 type=bottom（半砖）与 type=double（整方块）必须各拿自己的
    分类与模型注入决策，而不是被"方块首个变体"一刀切。"""

    @classmethod
    def setUpClass(cls):
        import bake_assets
        cls.zip_path = _synthetic_pack()
        cls.mcba = tempfile.mktemp(suffix=".mcba")
        pack = bake_assets.ResourcePack([cls.zip_path])
        bake_assets.Baker(pack, "test").run(cls.mcba)
        cls.pack = A.AssetPack.load(cls.mcba)

    @classmethod
    def tearDownClass(cls):
        for p in (cls.zip_path, cls.mcba):
            try:
                os.remove(p)
            except OSError:
                pass

    HALF = "minecraft:test_slab_block[type=bottom]"
    FULL = "minecraft:test_slab_block[type=double]"

    def test_class_per_state(self):
        self.assertEqual(self.pack.classify(self.HALF), B.NONCUBE)
        self.assertEqual(self.pack.classify(self.FULL), B.OPAQUE)

    def test_use_model_per_state(self):
        self.assertTrue(self.pack.use_model(self.HALF))
        self.assertFalse(self.pack.use_model(self.FULL))

    def test_tint_mask_per_state(self):
        # test_cube 仅顶面带 tintindex -> 掩码 bit0；test_slab 完全不染色
        self.assertEqual(self.pack.tint_mask(self.FULL), 1)
        self.assertEqual(self.pack.tint_mask(self.HALF), 0)

    def test_block_level_fallback(self):
        # 规则都带条件：传基础名（无属性）时退回块级信息（=首个变体）
        self.assertEqual(self.pack.classify("minecraft:test_slab_block"),
                         B.NONCUBE)
        self.assertTrue(self.pack.use_model("minecraft:test_slab_block"))

    def test_variant_info_round_trip(self):
        vi = self.pack.variant_indices("minecraft:test_slab_block",
                                       {"type": "double"})
        self.assertEqual(len(vi), 1)
        self.assertEqual(self.pack.variant_info[vi[0]][:3], (B.OPAQUE, 1, 0))

    def test_mesher_injects_only_half_slab(self):
        # 端到端：半砖状态注入烘焙模型，整方块状态走贪心路径（不注入）
        def models_for(state):
            pal = [(0, "minecraft:air"),
                   (int(self.pack.classify(state)), state)]
            idx = np.zeros(4096, np.uint16)
            idx[(8 << 8) | (8 << 4) | 8] = 1
            payloads = {(0, 0): make_payload(secs=[(pal, idx)])}
            return mesher.mesh_payload(payloads, pack=self.pack)[3]

        self.assertTrue(models_for(self.HALF))
        self.assertEqual(models_for(self.FULL), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
