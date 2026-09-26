# -*- coding: utf-8 -*-
"""R8 群系染色：烘焙侧算法与网格侧接线的回归测试（不依赖外部 jar）。

覆盖点：
  - colormap 采样的索引公式与颜色格式（对齐原版 GrassColors.getColor 字节码）
  - 群系 modifier 的常量与位运算（dark_forest / swamp）
  - 方块染色类别（草/叶/水）
  - 完整方块面 / 烘焙模型面 -> padded 格子的反推（含贴边界薄面的 off-by-one）
  - 合并键只在"声明染色"的方块上引入群系维度
  - geo_from_arrays 按格取群系颜色
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import bake_assets                                            # noqa: E402
from mc_bridge.core import assets as AS                       # noqa: E402
from mc_bridge.core import blocks as B                        # noqa: E402
from mc_bridge.core import mesher as M                        # noqa: E402


class _FakePx:
    """模拟 PIL PixelAccess：px[x, y] -> (r, g, b)。"""

    def __getitem__(self, k):
        x, y = k
        return (x, y, 0)


def _fake_colormap():
    """256×256 合成 colormap：pixel(x, y) = (x, y, 0)，便于反查索引。"""
    return (256, 256, _FakePx())


class _TintAllPack:
    """假资产包：所有面都声明染色（真实包由模型的 tintindex 决定）。"""

    def tint_mask(self, name):
        return 7


class TestColormapSampling:
    """_cm_color 必须复刻 GrassColors.getColor（colorMap[y*256+x]，行主序）。"""

    def test_index_formula(self):
        # t=0.5, d=0.5 -> i=(1-0.5)*255=127, j=(1-0.5*0.5)*255=191（无浮点歧义）
        cm = _fake_colormap()
        assert bake_assets._cm_color(cm, 0.5, 0.5) == (127 << 16) | (191 << 8)

    def test_float_truncation_matches_java(self):
        # 1.0-0.8 在 IEEE754 下是 0.19999999999999996，×255 后截断得 50 而非 51。
        # Java 的 (int) 同样是向零截断；若这里改成 round 或加 epsilon，就会与
        # 原版差一整列 colormap，表现为草色整体偏一档。
        cm = _fake_colormap()
        assert bake_assets._cm_color(cm, 0.8, 0.4) == (50 << 16) | (173 << 8)

    def test_corners(self):
        cm = _fake_colormap()
        assert bake_assets._cm_color(cm, 1.0, 0.0) == (0 << 16) | (255 << 8)
        assert bake_assets._cm_color(cm, 0.0, 0.0) == (255 << 16) | (255 << 8)

    def test_out_of_range_is_magenta(self):
        # 原版对 k 越界返回 0xFF00FF；温度 >1 由 Biome.getDefaultGrassColor 的
        # MathHelper.clamp 兜住，这里单独验证越界分支本身
        assert bake_assets._cm_color(_fake_colormap(), 2.0, 0.0) == 0xFF00FF

    def test_missing_colormap(self):
        assert bake_assets._cm_color(None, 0.5, 0.5) is None


class TestBiomeModifiers:
    """modifier 常量与公式取自 1.21.1 字节码。"""

    def test_dark_forest_formula(self):
        # 原版: ((c & 0xFEFEFE) + 0x28340A) >> 1
        # dark_forest 的 colormap 底色 0x78C05A -> 0x507A32（与 wiki 真值一致）
        assert ((0x78C05A & 0xFEFEFE) + 0x28340A) >> 1 == 0x507A32

    def test_swamp_constant(self):
        # 原版 SWAMP: noise < -0.1 ? 5011004 : 6975545
        assert 6975545 == 0x6A7039

    def test_desert_temperature_is_clamped(self):
        # desert 的 temperature=2.0 必须被 clamp，否则 _cm_color 会越界
        assert min(1.0, max(0.0, 2.0)) == 1.0
        assert bake_assets._cm_color(_fake_colormap(), 1.0, 0.0) != 0xFF00FF


class TestTintKind:
    def test_grass(self):
        assert B.tint_kind("minecraft:grass_block[snowy=false]") == B.TINT_KIND_GRASS
        assert B.tint_kind("minecraft:short_grass") == B.TINT_KIND_GRASS

    def test_foliage(self):
        assert B.tint_kind("minecraft:oak_leaves") == B.TINT_KIND_FOLIAGE
        assert B.tint_kind("minecraft:vine") == B.TINT_KIND_FOLIAGE

    def test_water(self):
        assert B.tint_kind("minecraft:water[level=0]") == B.TINT_KIND_WATER

    def test_none(self):
        assert B.tint_kind("minecraft:stone") == B.TINT_KIND_NONE
        assert B.tint_kind("minecraft:oak_planks") == B.TINT_KIND_NONE


class TestQuadCells:
    """完整方块面 -> 所属格子（与 _emit 的坐标约定对应）。

    _emit 里 u/v 轴写的是 `u0 - 1`（格子索引 - 1），法向轴直接写平面索引 p，
    因此：u/v 格子 = 顶点 + 1；法向 +dir 格子 = 顶点、-dir 格子 = 顶点 + 1。

    下面用「padded 格子 (5,3,4) 的 +X 面」构造：该面位于 x 高边（顶点 x=5），
    y/z 方向覆盖格子 3..3、4..4（顶点 2..3、3..4）。"""

    # +X 面：(5,2,3) (5,2,4) (5,3,4) (5,3,3)
    V = np.array([[[5, 2, 3], [5, 2, 4], [5, 3, 4], [5, 3, 3]]], np.int16)

    def test_positive_face_is_own_cell(self):
        c = M._quad_cells(self.V, np.array([0], np.uint8))       # +X
        assert tuple(int(v) for v in c[0]) == (5, 3, 4)

    def test_negative_face_belongs_to_next_cell(self):
        # 同样这组顶点若是 -X 面，则它属于东侧邻格 (6,3,4)
        c = M._quad_cells(self.V, np.array([1], np.uint8))       # -X
        assert tuple(int(v) for v in c[0]) == (6, 3, 4)

    def test_uv_axes_shift_by_one(self):
        # 回归：u/v 轴漏 +1 会让群系查找整体斜移 (-1,-1)
        c = M._quad_cells(self.V, np.array([0], np.uint8))[0]
        assert int(c[1]) == 3 and int(c[2]) == 4, "u/v 轴必须 +1"


class TestModelCells:
    """烘焙模型面（模型自身局部像素 0..16）-> padded 格子。

    面心落在格边界时归属由**面法向**决定：+dir 面在格上边界、-dir 面在下边界，
    两者都属于本方块。只按 floor/ceil 会把其中一半判到隔壁格。"""

    def test_positive_face_at_own_upper_boundary(self):
        # 本方块（chunk 首格）的 +X 面：面心 px=16 -> padded 1
        v = np.array([[[16, 0, 0], [16, 0, 16], [16, 16, 16], [16, 16, 0]]], np.float32)
        assert int(M._model_cells(v, np.array([0]))[0][0]) == 1

    def test_negative_face_at_own_lower_boundary(self):
        # 同一方块的 -X 面：面心 px=0 -> 仍属本格
        v = np.array([[[0, 0, 0], [0, 0, 16], [0, 16, 16], [0, 16, 0]]], np.float32)
        assert int(M._model_cells(v, np.array([1]))[0][0]) == 1

    def test_negative_face_at_non_origin_cell(self):
        # 关键回归：方块在局部第 5 格的 -X 面（面心 px=80）
        # 旧实现 ceil(80/16)=5 -> padded 5（取到 -X 邻格群系色）；正确应为 padded 6
        v = np.array([[[80, 0, 80], [80, 0, 96], [80, 16, 96], [80, 16, 80]]], np.float32)
        c = M._model_cells(v, np.array([1]))       # -X
        assert tuple(int(x) for x in c[0]) == (6, 1, 6)

    def test_positive_face_at_non_origin_cell(self):
        # 同一方块的 +X 面在 px=96 -> 也是 padded 6
        v = np.array([[[96, 0, 80], [96, 0, 96], [96, 16, 96], [96, 16, 80]]], np.float32)
        c = M._model_cells(v, np.array([0]))       # +X
        assert tuple(int(x) for x in c[0]) == (6, 1, 6)

    def test_negative_z_face_at_non_origin_cell(self):
        v = np.array([[[80, 0, 80], [96, 0, 80], [96, 16, 80], [80, 16, 80]]], np.float32)
        c = M._model_cells(v, np.array([5]))       # -Z
        assert tuple(int(x) for x in c[0]) == (6, 1, 6)

    def test_inner_face(self):
        # 薄板顶面 y=8 像素 -> 格 0 -> padded 1
        v = np.array([[[0, 8, 0], [16, 8, 0], [16, 8, 16], [0, 8, 16]]], np.float32)
        c = M._model_cells(v, np.array([2]))       # +Y
        assert tuple(int(x) for x in c[0]) == (1, 1, 1)


class TestVertexColorColorSpace:
    """F9：BYTE_COLOR 的 `.color` 是场景线性空间，写入前必须做 sRGB->线性。

    否则把 sRGB 值当线性用，染色与 AO 会被整体提亮（实测约 2.9 倍），观感偏灰。
    标定实验见 dist/_vcol_calib.py（已知灰贴图 × 已知顶点色，顶光 π）。"""

    def _written(self, rgb):
        import fake_bpy                                   # noqa: E402
        fake_bpy.install()
        from mc_bridge import importer                    # noqa: E402
        geo = {
            "nq": 1,
            "verts": np.zeros((1, 4, 3), np.float32),
            "uv": np.zeros((1, 4, 2), np.float32),
            "vcol": np.array([[list(rgb) + [255]] * 4], np.uint8).reshape(4, 4),
            "mat_idx": np.zeros(1, np.int32),
            "mats": [("block", "minecraft:stone", "top")],
        }
        mesh = importer._build_mesh("T", geo)
        col = mesh.color_attributes.get("Col")
        assert col is not None
        return list(col.data.color)

    def test_byte_is_converted_from_srgb(self):
        got = self._written((128, 128, 128))
        # sRGB 128/255 = 0.502 -> 线性 0.2158
        assert abs(got[0] - 0.2158) < 0.005, got[0]

    def test_not_raw_srgb(self):
        # 回归：退回"直接写 byte/255"时会是 0.502（明显偏亮）
        got = self._written((128, 128, 128))
        assert got[0] < 0.30, got[0]

    def test_white_and_alpha_unchanged(self):
        got = self._written((255, 255, 255))
        assert abs(got[0] - 1.0) < 1e-6
        assert abs(got[3] - 1.0) < 1e-6


class TestOverlayPush:
    """F10：同位置不同贴图的面（草方块侧面 overlay）要沿法向推出，避免 Z-Fighting。"""

    _V = ((0, 0, 0), (0, 0, 16), (0, 16, 16), (0, 16, 0))     # -X 面
    _UV = ((0, 0), (1, 0), (1, 1), (0, 1))

    def _q(self, tex, tint):
        return (self._V, 1, tex, tint, 2, self._UV)

    def test_coincident_different_tex_pushed_along_normal(self):
        out = bake_assets._dedupe_coincident([self._q(10, -1), self._q(11, 0)])
        assert len(out) == 2, "贴图不同 -> 两份都保留"
        assert out[0][0][0][0] == 0.0, "第一个（基底）不动"
        assert abs(out[1][0][0][0] + bake_assets._OVERLAY_PUSH) < 1e-6, \
            "第二个（overlay）应沿 -X 法向推出"

    def test_identical_faces_still_deduped(self):
        out = bake_assets._dedupe_coincident([self._q(10, -1), self._q(10, -1)])
        assert len(out) == 1, "完全重复（零厚度模型正反面）仍应合并"

    def test_push_is_subpixel(self):
        # 单位是 1/16 方块（= 模型像素）：推出量须远小于 1 格（此处 ~1/50 格）
        blk = bake_assets._OVERLAY_PUSH / 16.0
        assert 0 < blk < 0.05, blk
        # 且要能被 1/256 方块精度表达（非零整数单位）
        assert round(bake_assets._OVERLAY_PUSH * 16) >= 1


class TestMeshBiomeKey:
    """合并键只在声明染色的方块上引入群系维度。"""

    def _volume(self):
        cls = np.zeros((18, 6, 18), np.uint8)
        gid = np.zeros((18, 6, 18), np.uint16)
        cls[1:17, 2, 1:17] = 1                      # 中心区块一整层实心
        return cls, gid

    def _biome(self):
        bio = np.zeros((18, 6, 18), np.uint8)
        for x in range(18):
            bio[x, :, :] = (x - 1) // 4 + 1         # 每 4 格换一个群系
        return bio

    def test_not_needed_matches_no_biome(self):
        cls, gid = self._volume()
        q0, _ = M.mesh_padded(cls, gid, with_ao=False, palette=["minecraft:stone"])
        q1, _ = M.mesh_padded(cls, gid, with_ao=False, palette=["minecraft:stone"],
                              biome=self._biome(), bio_need=np.zeros(1, bool))
        assert len(q0) == len(q1)

    def test_needed_splits_across_biomes(self):
        cls, gid = self._volume()
        q0, _ = M.mesh_padded(cls, gid, with_ao=False, palette=["minecraft:stone"])
        q2, _ = M.mesh_padded(cls, gid, with_ao=False, palette=["minecraft:stone"],
                              biome=self._biome(), bio_need=np.ones(1, bool))
        assert len(q2) > len(q0)


class TestGeoPerCellTint:
    """顶点色按"面所属格子"的群系取色，而不是整块统一。"""

    def test_two_cells_different_biome(self):
        AS.clear()
        verts = np.array([
            [[1, 1, 1], [1, 1, 2], [1, 2, 2], [1, 2, 1]],      # 格 (1,1,1)
            [[3, 1, 1], [3, 1, 2], [3, 2, 2], [3, 2, 1]],      # 格 (3,1,1)
        ], np.int16)
        dirs = np.array([0, 0], np.uint8)
        blks = np.array([0, 0], np.uint16)
        aos = np.array([[3, 3, 3, 3], [3, 3, 3, 3]], np.uint8)
        bio = np.zeros((18, 6, 18), np.uint8)
        bio[1, :, :] = 1
        bio[3, :, :] = 2
        lut = np.ones((3, 4, 3), np.float32)
        lut[1, B.TINT_KIND_GRASS] = (1.0, 0.0, 0.0)
        lut[2, B.TINT_KIND_GRASS] = (0.0, 0.0, 1.0)
        geo = M.geo_from_arrays(verts, dirs, blks, aos, ["minecraft:grass_block"],
                                pack=_TintAllPack(), biome=(bio, lut))
        vc = np.asarray(geo["vcol"]).reshape(-1, 4, 4)
        assert tuple(int(v) for v in vc[0, 0, :3]) == (255, 0, 0)
        assert tuple(int(v) for v in vc[1, 0, :3]) == (0, 0, 255)
