#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""烘焙 MCBA1 资产包：从 Minecraft 客户端 jar / 资源包提取贴图与方块模型。

用法:
  python tools/bake_assets.py <client.jar|resourcepack.zip|mod.jar> [...] -o dist/assets.mcba
    --mc-version 1.21.1     写入包的版本标记
    --only oak_stairs,...   只烘焙指定方块（调试用，可带命名空间）

模组方块：把 mods/*.jar 一并作为输入即可（自动扫描其 assets/<ns>/ 下资源）。
  通配符由本脚本自己展开，因此在 PowerShell / cmd（不会替原生命令展开 *）下
  同样可用：python tools/bake_assets.py client.jar "mods/*.jar" -o dist/assets.mcba

方块实体（箱子/床/告示牌/旗帜/潜影盒/头颅/陶罐/传送门框架/钟…）没有 JSON 几何，
其原版形状取自 tools/vanilla_be_models.json —— 由 tools/dump_be_models.ps1 从客户端
jar 直接调用原版模型工厂导出（缺失时退回简化代理模型）。换 MC 版本后需重跑该脚本。

输出 MCBA1（小端）:
  magic "MCBA1" | ver u8 = 2 | mcVersion str
  --- 贴图 ---  nTex u32；每项: name str | w u16 | h u16 | len u32 | RGBA 字节
  --- 变体 ---  nVar u32；每项: nQuads u16；
                每 quad: 12×i16 顶点 | dir u8 | tex u16 | tint i8 | cull u8 | 8×f32 UV
  v2 顶点单位为 1/256 方块（= 1/16 像素），模组模型（Blockbench 常用 0.25/0.5
  像素等亚像素坐标）不再被取整压扁；读取端 v1/v2 均兼容。
  --- 方块状态 --- nBlock u32；每项: name str | mode u8 | nRules u16；
                每 rule: nPairs u8；(key str, val str)×nPairs | variant u16
  --- 默认面贴图 --- nBlockTex u32；每项: name str | top u16 | side u16 | bottom u16

依赖: Pillow（`pip install pillow`）。工具仅构建期使用，不随插件分发。
"""
import argparse
import glob
import io
import json
import math
import os
import struct
import sys
import zipfile

try:
    from PIL import Image
except ImportError:                                  # pragma: no cover
    Image = None

MAGIC = b"MCBA1"
VERSION = 2                    # v1: 顶点 1/16 方块单位；v2: 1/256（保留亚像素精度）
_VERT_SCALE = 16               # v2 写入倍数：1/16 单位 × 16 = 1/256 方块单位
_I16_MIN, _I16_MAX = -32768, 32767

# 烘焙期统计（诊断「导出失效」：贴图缺失会让面被丢弃）
_STATS = {"faces": 0, "tex_miss": 0}

# 面方向编码（与 core/mesher.py 一致）: 0=+X 1=-X 2=+Y 3=-Y 4=+Z 5=-Z
FACE_DIR = {"east": 0, "west": 1, "up": 2, "down": 3, "south": 4, "north": 5}
DIR_VEC = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))

# 方块分类（与 core/blocks.py 一致）
AIR, OPAQUE, TRANSPARENT, LIQUID, CUTOUT, NONCUBE = 0, 1, 2, 3, 4, 5

_LIQUID_HINT = ("water", "lava")
_TRANSPARENT_HINT = ("glass", "ice", "pane", "portal", "beacon_beam")

_CENTER = (8.0, 8.0, 8.0)


# ------------------------------------------------------------ 资源包 ----
class ResourcePack:
    """多 zip 叠加（后者覆盖前者），提供文件读取与清单查询。"""

    def __init__(self, paths):
        self.zips = []
        for p in paths:
            try:
                self.zips.append(zipfile.ZipFile(p))
            except (OSError, zipfile.BadZipFile) as e:
                raise SystemExit(
                    "无法打开资源包 %s: %s\n"
                    "提示：通配符加引号传入（mods/*.jar），脚本会自行展开。" % (p, e))
        self._names = None

    def read(self, name):
        for z in self.zips:
            try:
                return z.read(name)
            except KeyError:
                continue
        return None

    def json(self, name):
        b = self.read(name)
        if b is None:
            return None
        try:
            return json.loads(b.decode("utf-8"))
        except ValueError:
            return None

    def names(self):
        if self._names is None:
            s = set()
            for z in self.zips:
                s.update(z.namelist())
            self._names = s
        return self._names


# ------------------------------------------------------------ 贴图 ----
def _decode_png(data):
    """PNG -> (w, h, RGBA bytes)；动画条带只取第一帧。"""
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGBA")
    w, h = img.size
    if h != w and w > 0 and h % w == 0:              # 动画条带：裁剪第一帧
        img = img.crop((0, 0, w, w))
        h = w
    return w, h, img.tobytes()


def tex_resource_path(name):
    """'minecraft:block/oak_planks' -> 'assets/minecraft/textures/block/oak_planks.png'"""
    if ":" in name:
        ns, path = name.split(":", 1)
    else:
        ns, path = "minecraft", name
    return "assets/%s/textures/%s.png" % (ns, path)


def model_resource_path(name):
    if ":" in name:
        ns, path = name.split(":", 1)
    else:
        ns, path = "minecraft", name
    return "assets/%s/models/%s.json" % (ns, path)


def blockstate_resource_path(block):
    ns, path = (block.split(":", 1) + [None])[:2] if ":" in block else ("minecraft", block)
    return "assets/%s/blockstates/%s.json" % (ns, path)


# ------------------------------------------------------------ 模型 ----
class ModelLoader:
    def __init__(self, pack):
        self.pack = pack
        self._cache = {}

    def load(self, name, depth=0):
        """返回 {'textures': {var: ref}, 'elements': [...], 'tex_size': (w,h)}
        （父链已合并；tex_size 缺省继承父模型，默认 16×16）。"""
        if name in self._cache:
            return self._cache[name]
        if depth > 32:
            return None
        raw = self.pack.json(model_resource_path(name))
        if raw is None:
            self._cache[name] = None
            return None
        textures = {}
        elements = raw.get("elements")
        tex_size = (16.0, 16.0)
        parent = raw.get("parent")
        if parent:
            base = self.load(parent, depth + 1)
            if base is not None:
                textures.update(base["textures"])
                if elements is None:
                    elements = base["elements"]
                tex_size = base["tex_size"]
        textures.update(raw.get("textures") or {})
        ts = raw.get("texture_size")
        if ts is not None:
            if isinstance(ts, (int, float)):
                tex_size = (float(ts), float(ts))
            else:
                tex_size = (float(ts[0]), float(ts[1]))
        out = {"textures": textures, "elements": elements, "tex_size": tex_size}
        self._cache[name] = out
        return out

    @staticmethod
    def resolve_texture(model, ref, depth=0):
        """'#all' -> ... -> 'minecraft:block/oak_planks'；失败返回 None。"""
        if ref is None or depth > 16:
            return None
        if isinstance(ref, str) and ref.startswith("#"):
            nxt = model["textures"].get(ref[1:])
            return ModelLoader.resolve_texture(model, nxt, depth + 1)
        return ref


# ------------------------------------------------------------ 几何 ----
def _default_uv(d, x0, y0, z0, x1, y1, z1, sx=1.0, sy=1.0):
    if d == 2:      # +Y up
        uv = [x0, z0, x1, z1]
    elif d == 3:    # -Y down
        uv = [x0, 16 - z1, x1, 16 - z0]
    elif d == 4:    # +Z south
        uv = [x0, 16 - y1, x1, 16 - y0]
    elif d == 5:    # -Z north
        uv = [16 - x1, 16 - y1, 16 - x0, 16 - y0]
    elif d == 1:    # -X west
        uv = [z0, 16 - y1, z1, 16 - y0]
    else:           # +X east
        uv = [16 - z1, 16 - y1, 16 - z0, 16 - y0]
    return [uv[0] * sx, uv[1] * sy, uv[2] * sx, uv[3] * sy]


def _face_corners(d, x0, y0, z0, x1, y1, z1):
    """外向 CCW 的 4 角点（MC 坐标，1/16 单位）。"""
    if d == 0:
        return [(x1, y0, z1), (x1, y0, z0), (x1, y1, z0), (x1, y1, z1)]
    if d == 1:
        return [(x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)]
    if d == 2:
        return [(x0, y1, z0), (x0, y1, z1), (x1, y1, z1), (x1, y1, z0)]
    if d == 3:
        return [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)]
    if d == 4:
        return [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    return [(x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)]


def _uv_frac(d, p, x0, y0, z0, x1, y1, z1):
    """点 p 在面内的归一化 (uu, vv)，与 MC 默认 UV 轴一致。"""
    x, y, z = p
    dx = (x1 - x0) or 1.0
    dy = (y1 - y0) or 1.0
    dz = (z1 - z0) or 1.0
    if d == 2:
        return (x - x0) / dx, (z - z0) / dz
    if d == 3:
        return (x - x0) / dx, (z1 - z) / dz
    if d == 4:
        return (x - x0) / dx, (y1 - y) / dy
    if d == 5:
        return (x1 - x) / dx, (y1 - y) / dy
    if d == 1:
        return (z - z0) / dz, (y1 - y) / dy
    return (z1 - z) / dz, (y1 - y) / dy


def _rot_uv(uu, vv, deg):
    deg = int(deg) % 360
    if deg == 90:
        return vv, 1.0 - uu
    if deg == 180:
        return 1.0 - uu, 1.0 - vv
    if deg == 270:
        return 1.0 - vv, uu
    return uu, vv


def _rotate_point(p, origin, axis, deg):
    """绕 origin 的 axis 轴旋转 deg 度（MC 约定：右手系负角）。"""
    import math
    if not deg:
        return p
    a = math.radians(-deg)
    ca, sa = math.cos(a), math.sin(a)
    x, y, z = (p[0] - origin[0], p[1] - origin[1], p[2] - origin[2])
    if axis == 0:
        y, z = y * ca - z * sa, y * sa + z * ca
    elif axis == 1:
        x, z = x * ca + z * sa, -x * sa + z * ca
    else:
        x, y = x * ca - y * sa, x * sa + y * ca
    return (x + origin[0], y + origin[1], z + origin[2])


def build_model_quads(model, texid_of, xr=0, yr=0, uvlock=False):
    """model -> [(verts(4,3), dir, texId, tint, cull, uvs(4,2))]（未应用 blockstate
    变换）。UV 归一化到 0..1 并翻转为 Blender 约定（v 自底向上）。

    xr/yr/uvlock 仅在 blockstate 声明 uvlock=true 时用于把 UV 矩形锁到世界方向
    （几何仍由 apply_variant_transform 旋转）。"""
    out = []
    # Java 模型 UV 始终以 0..16 为单位（texture_size 仅声明纹理像素尺寸，
    # 按 texture_size/16 缩放后归一化会抵消），因此统一除以 16。
    tw = th = 16.0
    sx = sy = 1.0
    for elem in (model.get("elements") or []):
        frm, to = elem.get("from"), elem.get("to")
        if not frm or not to:
            continue
        x0, y0, z0 = float(frm[0]), float(frm[1]), float(frm[2])
        x1, y1, z1 = float(to[0]), float(to[1]), float(to[2])
        rot = elem.get("rotation")
        rot_o = rot_a = None
        rot_deg = 0
        if rot:
            rot_o = tuple(float(v) for v in rot.get("origin", (8, 8, 8)))
            rot_a = {"x": 0, "y": 1, "z": 2}.get(rot.get("axis"), 1)
            rot_deg = int(round(float(rot.get("angle", 0))))
        for fname, face in (elem.get("faces") or {}).items():
            d = FACE_DIR.get(fname)
            if d is None:
                continue
            _STATS["faces"] += 1
            tex = ModelLoader.resolve_texture(model, face.get("texture"))
            if tex is None:
                continue
            texid = texid_of(tex)
            if texid is None:
                _STATS["tex_miss"] += 1
                continue
            uv = face.get("uv") or _default_uv(d, x0, y0, z0, x1, y1, z1, sx, sy)
            u1, v1, u2, v2 = (float(uv[0]), float(uv[1]), float(uv[2]), float(uv[3]))
            if uvlock and (xr or yr):
                u1, v1, u2, v2 = _uvlock_rect(u1, v1, u2, v2, xr, yr, d)
            frot = int(face.get("rotation", 0))
            corners = _face_corners(d, x0, y0, z0, x1, y1, z1)
            verts, uvs = [], []
            for p in corners:
                uu, vv = _uv_frac(d, p, x0, y0, z0, x1, y1, z1)
                uu, vv = _rot_uv(uu, vv, frot)
                up = (u1 + uu * (u2 - u1)) / tw
                vp = (v1 + vv * (v2 - v1)) / th
                uvs.append((up, 1.0 - vp))
                if rot_deg:
                    p = _rotate_point(p, rot_o, rot_a, rot_deg)
                verts.append(p)
            cull = FACE_DIR.get(face.get("cullface"), -1)
            out.append((verts, d, texid, int(face.get("tintindex", -1)),
                        cull + 1, uvs))
    return out


def apply_variant_transform(quads, xr, yr, uvlock):
    """按 blockstate 的 x/y 旋转（绕方块中心）变换几何。

    **次序：先 x 后 y**（与 MC 一致）。MC 的 ModelRotation.get(x, y) 生成的
    线性映射等价于 R_y ∘ R_x（已用 MC 1.21.1 本体探针逐组核对 16 种组合）：
    例如 chain 的 axis=x 用 x=90,y=90，up 轴先被 x 转到 -Z，再被 y 转到 +X；
    若反过来先 y 后 x，则仍停在 -Z（锁链横躺成南北向、末地烛朝向偏 90°，
    楼梯 half=top 的 x=180 与 y 组合也会整体错向）。
    UV 已在 build_model_quads 归一化并按 Blender 约定翻转；uvlock 的
    UV 锁定暂未实现（只影响贴图方向，不影响几何朝向）。"""
    if not xr and not yr:
        return quads
    out = []
    for verts, d, texid, tint, cull, uvs in quads:
        nv = list(verts)
        if xr:
            nv = [_rotate_point(p, _CENTER, 0, xr) for p in nv]
        if yr:
            nv = [_rotate_point(p, _CENTER, 1, yr) for p in nv]
        nd = _rotate_dir(d, xr, yr)
        ncull = _rotate_dir(cull - 1, xr, yr) + 1 if cull else 0
        out.append((nv, nd, texid, tint, ncull, uvs))
    return out


_DIR_BY_VEC = {(1, 0, 0): 0, (-1, 0, 0): 1, (0, 1, 0): 2,
               (0, -1, 0): 3, (0, 0, 1): 4, (0, 0, -1): 5}


# ------------------------------------------------------------ uvlock ----
# blockstate 的 "uvlock": 贴图锁在世界方向、不随模型旋转（否则倒置楼梯的
# 贴图会整体转 180°）。公式（用 MC 1.21.1 本体 96 组 (x,y,面) 实测矩阵核对）：
#     uvLock = D(d') · R · D(d)^T
#   R   = 模型 x/y 旋转矩阵（列 = 基向量像，先 x 后 y）
#   d   = 模型空间的面方向，d' = R·d
#   D(f)= MC AffineTransformations.DIRECTION_ROTATIONS（把标准面帧转到方向 f）
_D_DIR_MAT = {
    "down":  ((1, 0, 0), (0, 0, 1), (0, -1, 0)),
    "up":    ((1, 0, 0), (0, 0, -1), (0, 1, 0)),
    "north": ((-1, 0, 0), (0, 1, 0), (0, 0, -1)),
    "south": ((1, 0, 0), (0, 1, 0), (0, 0, 1)),
    "west":  ((0, 0, 1), (0, 1, 0), (-1, 0, 0)),
    "east":  ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
}
_DIR_NAME = {v: k for k, v in FACE_DIR.items()}


def _mat3_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
                 for i in range(3))


def _mat3_t(a):
    return tuple(tuple(a[j][i] for j in range(3)) for i in range(3))


def _mat3_vec(a, v):
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


def _rot_mat3(xr, yr):
    """blockstate x/y 旋转的 3×3 矩阵（列 = 基向量像；先 x 后 y）。"""
    cols = []
    for e in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)):
        p = e
        if xr:
            p = _rotate_point(p, (0.0, 0.0, 0.0), 0, xr)
        if yr:
            p = _rotate_point(p, (0.0, 0.0, 0.0), 1, yr)
        cols.append(tuple(int(round(c)) for c in p))
    return tuple(tuple(cols[j][i] for j in range(3)) for i in range(3))


def _uvlock_mat(xr, yr, d):
    """uvlock 的 3×3 变换矩阵（作用于 (u/16, v/16, 0)）。"""
    g = _rot_mat3(xr, yr)
    dv = _mat3_vec(g, DIR_VEC[d])
    d2 = _DIR_BY_VEC.get(tuple(int(round(c)) for c in dv), d)
    return _mat3_mul(_mat3_mul(_D_DIR_MAT[_DIR_NAME[d2]], g),
                     _mat3_t(_D_DIR_MAT[_DIR_NAME[d]]))


def _uvlock_rect(u1, v1, u2, v2, xr, yr, d):
    """按 uvlock 变换 UV 矩形（0..16 单位），返回 (u1, v1, u2, v2)。

    与 MC BakedQuadFactory.uvLock 一致：变换两个对角点，方向翻转时交换。"""
    m = _uvlock_mat(xr, yr, d)

    def tp(u, v):
        return (m[0][0] * (u / 16.0) + m[0][1] * (v / 16.0)) * 16.0, \
               (m[1][0] * (u / 16.0) + m[1][1] * (v / 16.0)) * 16.0

    nu1, nv1 = tp(u1, v1)
    nu2, nv2 = tp(u2, v2)

    def sgn(x):
        return (x > 0) - (x < 0)

    if sgn(nu2 - nu1) != sgn(u2 - u1):
        nu1, nu2 = nu2, nu1
    if sgn(nv2 - nv1) != sgn(v2 - v1):
        nv1, nv2 = nv2, nv1
    return nu1, nv1, nu2, nv2


def _rotate_dir(d, xr, yr):
    """面方向随模型旋转（次序同几何：先 x 后 y）。"""
    if d < 0:
        return d
    v = DIR_VEC[d]
    if xr:
        p = _rotate_point(v, (0, 0, 0), 0, xr)
        v = (round(p[0]), round(p[1]), round(p[2]))
    if yr:
        p = _rotate_point(v, (0, 0, 0), 1, yr)
        v = (round(p[0]), round(p[1]), round(p[2]))
    return _DIR_BY_VEC.get(v, d)


# ------------------------------------------------------------ 状态解析 ----
def _split_props(s):
    pairs = []
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            pairs.append((k.strip(), v.strip()))
    return pairs


def _norm_cond(when):
    """multipart 的 when -> [(key, frozenset(values)), ...]（已展开 OR）。"""
    if not when:
        return [[]]
    if "OR" in when:
        out = []
        for sub in when["OR"]:
            out.extend(_norm_cond(sub))
        return out
    if "AND" in when:
        acc = [[]]
        for sub in when["AND"]:
            sub_rules = _norm_cond(sub)
            acc = [a + b for a in acc for b in sub_rules]
        return acc
    pairs = []
    for k, v in when.items():
        vals = v if isinstance(v, list) else [v]
        pairs.append((k, frozenset(str(x) for x in vals)))
    return [pairs]


def _apply_variants(apply_node):
    """apply 可能是 dict 或 list（加权随机）；取第一个。"""
    if isinstance(apply_node, list):
        return apply_node[0] if apply_node else None
    return apply_node


# ------------------------------------------------------------ 原版方块实体 ----
# 方块实体（箱子/床/告示牌/旗帜/潜影盒/头颅/传送门框架…）没有 JSON 几何，形状由
# Java 渲染器绘制。tools/dump_be_models.ps1 直接调用原版模型工厂，把烘焙后的
# 顶点/UV/面法线导出为 vanilla_be_models.json；这里按原版渲染器的矩阵变换与
# 贴图规则还原为 MCBA 变体，因此与游戏内一致。
# 未收录的方块（含模组方块实体）才退回下面的简化代理模型。

_BE_DATA = None
_BE_JSON = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "vanilla_be_models.json")
_IDENT = (1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
          0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)
_NORMAL_DIR = {(1, 0, 0): 0, (-1, 0, 0): 1, (0, 1, 0): 2,
               (0, -1, 0): 3, (0, 0, 1): 4, (0, 0, -1): 5}
# Direction.asRotation()：南 0 / 西 90 / 北 180 / 东 270
_AS_ROT = {"south": 0.0, "west": 90.0, "north": 180.0, "east": 270.0}
# RotationPropertyHelper：0=北 4=东 8=南 12=西，每格 22.5°
_ROT_OF_DIR = {"north": 0, "east": 4, "south": 8, "west": 12}
_ROT_STEP = 22.5
_OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east"}

_COLORS = ("white", "orange", "magenta", "light_blue", "yellow", "lime", "pink",
           "gray", "light_gray", "cyan", "purple", "blue", "brown", "green",
           "red", "black")
# DyeColor 的实体染色值（原版 getEntityColor）
_DYE_RGB = {
    "white": (1.0, 1.0, 1.0), "orange": (0.976, 0.502, 0.114),
    "magenta": (0.780, 0.306, 0.741), "light_blue": (0.227, 0.702, 0.855),
    "yellow": (0.996, 0.847, 0.239), "lime": (0.502, 0.780, 0.122),
    "pink": (0.953, 0.545, 0.667), "gray": (0.278, 0.310, 0.322),
    "light_gray": (0.616, 0.616, 0.592), "cyan": (0.086, 0.612, 0.612),
    "purple": (0.537, 0.196, 0.722), "blue": (0.235, 0.267, 0.667),
    "brown": (0.514, 0.329, 0.196), "green": (0.369, 0.486, 0.086),
    "red": (0.690, 0.180, 0.149), "black": (0.114, 0.114, 0.129),
}
# 头颅贴图（原版 SkullBlockEntityRenderer 的 SkullType -> 贴图）
_SKULL_TEX = {
    "skeleton": "minecraft:entity/skeleton/skeleton",
    "wither_skeleton": "minecraft:entity/skeleton/wither_skeleton",
    "zombie": "minecraft:entity/zombie/zombie",
    "creeper": "minecraft:entity/creeper/creeper",
    "dragon": "minecraft:entity/enderdragon/dragon",
    "piglin": "minecraft:entity/piglin/piglin",
    "player": "minecraft:entity/player/wide/steve",
}


def _be_models():
    """加载原版方块实体几何表（缺失时返回空表，退回简化代理模型）。"""
    global _BE_DATA
    if _BE_DATA is None:
        try:
            with open(_BE_JSON, encoding="utf-8") as f:
                _BE_DATA = json.load(f).get("models", {}) or {}
        except (OSError, ValueError):
            _BE_DATA = {}
    return _BE_DATA


# ---- 4×4 矩阵（行主序）；与 MatrixStack 一致：M = M · op（后乘） ----
def _mat_mul(a, b):
    return tuple(sum(a[i * 4 + k] * b[k * 4 + j] for k in range(4))
                 for i in range(4) for j in range(4))


def _mat_t(x, y, z):
    return (1.0, 0.0, 0.0, x, 0.0, 1.0, 0.0, y,
            0.0, 0.0, 1.0, z, 0.0, 0.0, 0.0, 1.0)


def _mat_s(x, y, z):
    return (x, 0.0, 0.0, 0.0, 0.0, y, 0.0, 0.0,
            0.0, 0.0, z, 0.0, 0.0, 0.0, 0.0, 1.0)


def _mat_r(axis, deg):
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    if axis == 0:
        return (1.0, 0.0, 0.0, 0.0, 0.0, c, -s, 0.0,
                0.0, s, c, 0.0, 0.0, 0.0, 0.0, 1.0)
    if axis == 1:
        return (c, 0.0, s, 0.0, 0.0, 1.0, 0.0, 0.0,
                -s, 0.0, c, 0.0, 0.0, 0.0, 0.0, 1.0)
    return (c, -s, 0.0, 0.0, s, c, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _mat_ops(ops):
    m = _IDENT
    for op in ops:
        if op[0] == "t":
            m = _mat_mul(m, _mat_t(*op[1]))
        elif op[0] == "r":
            m = _mat_mul(m, _mat_r(op[1], op[2]))
        else:
            m = _mat_mul(m, _mat_s(*op[1]))
    return m


def _mat_point(m, p):
    x, y, z = p
    return (m[0] * x + m[1] * y + m[2] * z + m[3],
            m[4] * x + m[5] * y + m[6] * z + m[7],
            m[8] * x + m[9] * y + m[10] * z + m[11])


def _mat_dir(m, n):
    x, y, z = n
    return (m[0] * x + m[1] * y + m[2] * z,
            m[4] * x + m[5] * y + m[6] * z,
            m[8] * x + m[9] * y + m[10] * z)


def _mat_det3(m):
    return (m[0] * (m[5] * m[10] - m[6] * m[9])
            - m[1] * (m[4] * m[10] - m[6] * m[8])
            + m[2] * (m[4] * m[9] - m[5] * m[8]))


def _poly_area(verts):
    """四边形面积（叉积法；用于剔除退化面）。"""
    ax = ay = az = 0.0
    n = len(verts)
    for i in range(n):
        p, q = verts[i], verts[(i + 1) % n]
        ax += p[1] * q[2] - p[2] * q[1]
        ay += p[2] * q[0] - p[0] * q[2]
        az += p[0] * q[1] - p[1] * q[0]
    return 0.5 * math.sqrt(ax * ax + ay * ay + az * az)


def _nearest_axis(v):
    ax, ay, az = abs(v[0]), abs(v[1]), abs(v[2])
    if ax >= ay and ax >= az:
        return (1, 0, 0) if v[0] >= 0 else (-1, 0, 0)
    if ay >= az:
        return (0, 1, 0) if v[1] >= 0 else (0, -1, 0)
    return (0, 0, 1) if v[2] >= 0 else (0, 0, -1)


def be_model_quads(model_key, ops, texid_of, tex=None, part_tex=None,
                   skip=(), part_offset=None):
    """原版方块实体模型 -> MCBA quads（1/16 单位；UV 已翻转为 Blender 约定）。"""
    data = _be_models().get(model_key)
    if data is None:
        return []
    m = _mat_ops(ops)
    flip = _mat_det3(m) < 0          # 含镜像时反转绕序，保持外向 CCW
    out = []
    stack = list(data["parts"])
    while stack:
        part = stack.pop()
        name = part["name"]
        short = name.rsplit("/", 1)[-1]
        stack.extend(part.get("children") or ())
        if name in skip or short in skip:
            continue
        tname = (part_tex or {}).get(name, (part_tex or {}).get(short, tex))
        tid = texid_of(tname) if tname else None
        if tid is None:
            continue
        off = (part_offset or {}).get(short, (0.0, 0.0, 0.0))
        for cub in part["cuboids"]:
            for face in cub["faces"]:
                d = _NORMAL_DIR.get(_nearest_axis(_mat_dir(m, face["n"])))
                if d is None:
                    continue
                verts, uvs = [], []
                for v in face["v"]:
                    # 模型顶点为 1/16 单位，而渲染器的 translate/scale 为方块
                    # 单位（原版在渲染时才除以 16），故先换算再套矩阵。
                    p = _mat_point(m, ((v[0] + off[0]) / 16.0,
                                       (v[1] + off[1]) / 16.0,
                                       (v[2] + off[2]) / 16.0))
                    verts.append((p[0] * 16.0, p[1] * 16.0, p[2] * 16.0))
                    uvs.append((v[3], 1.0 - v[4]))
                if _poly_area(verts) < 1e-6:     # 退化面（零面积）丢弃
                    continue
                if flip:
                    verts.reverse()
                    uvs.reverse()
                out.append((verts, d, tid, -1, 0, uvs))
    return out


def _be_layer(model, tex, part_tex=None, skip=(), part_offset=None,
              model_pick=None, dye_parts=None):
    return {"model": model, "tex": tex, "part_tex": part_tex, "skip": skip,
            "part_offset": part_offset, "model_pick": model_pick,
            "dye_parts": dye_parts}


def _be_spec(props, ops, layers, dye=None):
    return {"props": props, "ops": ops, "layers": layers, "dye": dye}


def _wood_of(base, suffix):
    if base.endswith(suffix):
        return base[:-len(suffix)]
    return None


def _skull_kind(base):
    """'skeleton_wall_skull' -> ('skeleton', True) / 'player_head' -> ..."""
    for suffix, wall in (("_wall_skull", True), ("_skull", False),
                         ("_wall_head", True), ("_head", False)):
        if base.endswith(suffix):
            kind = base[:-len(suffix)]
            if kind in _SKULL_TEX:
                return kind, wall
    return None, False


def _be_specs(block):
    """方块 -> 原版方块实体变体规格列表；非方块实体返回 None。"""
    base = block.split(":")[-1]
    color = _color_of(base)
    rot_ops = [("t", (0.5, 0.5, 0.5)), ("r", 1, 0.0), ("t", (-0.5, -0.5, -0.5))]

    if base in ("chest", "trapped_chest", "ender_chest"):
        tex = {"chest": "minecraft:entity/chest/normal",
               "trapped_chest": "minecraft:entity/chest/trapped",
               "ender_chest": "minecraft:entity/chest/ender"}[base]
        out = []
        for ctype, mkey in (("single", "chest"), ("left", "chest_left"),
                            ("right", "chest_right")):
            for facing, rot in _AS_ROT.items():
                ops = [("t", (0.5, 0.5, 0.5)), ("r", 1, -rot),
                       ("t", (-0.5, -0.5, -0.5))]
                out.append(_be_spec([("facing", facing), ("type", ctype)], ops,
                                    [_be_layer(mkey, tex)]))
        return out

    if base == "bed" or base.endswith("_bed"):
        tex = "minecraft:entity/bed/%s" % (color or "white")
        out = []
        for facing, rot in _AS_ROT.items():
            ops = [("t", (0.0, 0.5625, -1.0)), ("r", 0, 90.0),
                   ("t", (0.5, 0.5, 0.5)), ("r", 2, 180.0 + rot),
                   ("t", (-0.5, -0.5, -0.5))]
            for part, mkey in (("head", "bed_head"), ("foot", "bed_foot")):
                out.append(_be_spec([("facing", facing), ("part", part)], ops,
                                    [_be_layer(mkey, tex)]))
        return out

    for suffix, kind in (("_wall_hanging_sign", "wall_hanging"),
                         ("_hanging_sign", "hanging"),
                         ("_wall_sign", "wall"), ("_sign", "standing")):
        wood = _wood_of(base, suffix)
        if wood is None:
            continue
        if kind in ("hanging", "wall_hanging"):
            tex = "minecraft:entity/signs/hanging/%s" % wood
            model, scale = "hanging_sign", (1.0, -1.0, -1.0)
            head = (0.0, 0.9375, 0.0)
        else:
            tex = "minecraft:entity/signs/%s" % wood
            model, scale = "sign", (2.0 / 3.0, -2.0 / 3.0, -2.0 / 3.0)
            head = (0.5, 0.5, 0.5)
        out = []
        if kind in ("standing", "hanging"):
            for i in range(16):
                ops = [("t", head), ("r", 1, -i * _ROT_STEP), ("s", scale)]
                out.append(_be_spec([("rotation", str(i))], ops,
                                    [_be_layer(model, tex)]))
        else:
            for facing, rot in _AS_ROT.items():
                ops = [("t", head), ("r", 1, -rot)]
                if kind == "wall":
                    ops.append(("t", (0.0, -0.3125, -0.4375)))
                    skip = ("stick",)
                else:
                    ops.append(("t", (0.0, -0.3125, 0.0)))
                    skip = ()
                ops.append(("s", scale))
                out.append(_be_spec([("facing", facing)], ops,
                                    [_be_layer(model, tex, skip=skip)]))
        return out

    if base == "banner" or base.endswith("_banner"):
        wall = base.endswith("_wall_banner")
        if base == "banner":
            name = "banner"
        else:
            name = base[:-len("_wall_banner" if wall else "_banner")]
        dye = name if name in _DYE_RGB else "white"
        flag_tex = "minecraft:entity/banner_base"
        layer = _be_layer("banner", flag_tex,
                          part_offset={"flag": (0.0, -32.0, 0.0)},
                          skip=("pole",) if wall else (),
                          dye_parts=("flag",))
        out = []
        if wall:
            for facing, rot in _AS_ROT.items():
                ops = [("t", (0.5, -1.0 / 6.0, 0.5)), ("r", 1, -rot),
                       ("t", (0.0, -0.3125, -0.4375)),
                       ("s", (2.0 / 3.0, -2.0 / 3.0, -2.0 / 3.0))]
                out.append(_be_spec([("facing", facing)], ops, [layer],
                                    dye=dye))
        else:
            for i in range(16):
                ops = [("t", (0.5, 0.5, 0.5)), ("r", 1, -i * _ROT_STEP),
                       ("s", (2.0 / 3.0, -2.0 / 3.0, -2.0 / 3.0))]
                out.append(_be_spec([("rotation", str(i))], ops, [layer],
                                    dye=dye))
        return out

    if base == "shulker_box" or base.endswith("_shulker_box"):
        tex = ("minecraft:entity/shulker/shulker" if color is None
               else "minecraft:entity/shulker/shulker_%s" % color)
        quat = {"up": [], "down": [("r", 0, 180.0)],
                "south": [("r", 0, 90.0)],
                "north": [("r", 0, 90.0), ("r", 2, 180.0)],
                "west": [("r", 0, 90.0), ("r", 2, 90.0)],
                "east": [("r", 0, 90.0), ("r", 2, -90.0)]}
        out = []
        for facing, qops in quat.items():
            ops = ([("t", (0.5, 0.5, 0.5)),
                    ("s", (0.9995, 0.9995, 0.9995))] + qops +
                   [("s", (1.0, -1.0, -1.0)), ("t", (0.0, -1.0, 0.0))])
            out.append(_be_spec([("facing", facing)], ops,
                                [_be_layer("shulker_box", tex, skip=("head",))]))
        return out

    kind, wall = _skull_kind(base)
    if kind:
        tex = _SKULL_TEX[kind]
        model = "dragon_head" if kind == "dragon" else None
        pick = None if model else ("skull_head", "skull_floor")
        out = []
        if wall:
            for facing in _AS_ROT:
                opp = _OPPOSITE[facing]
                deg = _ROT_OF_DIR[opp] * _ROT_STEP
                ox = {"east": 1, "west": -1}.get(facing, 0)
                oz = {"south": 1, "north": -1}.get(facing, 0)
                ops = [("t", (0.5 - ox * 0.25, 0.25, 0.5 - oz * 0.25)),
                       ("s", (-1.0, -1.0, 1.0)), ("r", 1, deg)]
                out.append(_be_spec([("facing", facing)], ops,
                                    [_be_layer(model, tex, model_pick=pick)]))
        else:
            for i in range(16):
                ops = [("t", (0.5, 0.0, 0.5)), ("s", (-1.0, -1.0, 1.0)),
                       ("r", 1, i * _ROT_STEP)]
                out.append(_be_spec([("rotation", str(i))], ops,
                                    [_be_layer(model, tex, model_pick=pick)]))
        return out

    if base == "conduit":
        return [_be_spec([], [("t", (0.5, 0.5, 0.5))],
                         [_be_layer("conduit_shell",
                                    "minecraft:entity/conduit/base")])]

    if base == "decorated_pot":
        out = []
        for facing, rot in _AS_ROT.items():
            ops = [("t", (0.5, 0.0, 0.5)), ("r", 1, 180.0 - rot),
                   ("t", (-0.5, 0.0, -0.5))]
            out.append(_be_spec(
                [("facing", facing)], ops,
                [_be_layer("decorated_pot_body",
                           "minecraft:entity/decorated_pot/decorated_pot_base"),
                 _be_layer("decorated_pot_sides",
                           "minecraft:entity/decorated_pot/decorated_pot_side")]))
        return out

    return None


def _box(frm, to, tex="#tex", uv=(0, 0, 16, 16)):
    faces = {d: {"uv": list(uv), "texture": tex}
             for d in ("down", "up", "north", "south", "west", "east")}
    return {"from": list(frm), "to": list(to), "faces": faces}


def _color_of(base):
    for c in _COLORS:
        if base.startswith(c + "_"):
            return c
    return None


def proxy_model(block, particle):
    """兜底简化代理模型（仅用于未收录的方块实体）：(elements, textures) 或 None。"""
    base = block.split(":")[-1]
    wood = particle or "minecraft:block/oak_planks"
    color = _color_of(base)
    if base == "bed" or base.endswith("_bed"):
        wool = "minecraft:block/%s_wool" % (color or "white")
        return ([_box((0, 0, 0), (16, 3, 16), "#wood"),        # 床架
                 _box((0, 3, 0), (16, 9, 16), "#mattress"),    # 床垫
                 _box((1, 9, 1), (15, 11, 6), "#mattress")],   # 枕头
                {"wood": "minecraft:block/oak_planks", "mattress": wool})
    if base in ("chest", "trapped_chest", "ender_chest"):
        return ([_box((1, 0, 1), (15, 10, 15), "#tex"),        # 箱体
                 _box((1, 9, 1), (15, 14, 15), "#tex"),        # 箱盖
                 _box((7, 7, 15), (9, 11, 16), "#tex")],       # 锁扣
                {"tex": wood})
    if "sign" in base:
        els = []
        if "hanging" not in base and "wall" not in base:
            els.append(_box((7, 0, 7), (9, 8, 9), "#tex"))     # 立柱
        if "wall" in base:
            els.append(_box((1, 4, 7), (15, 12, 9), "#tex"))   # 墙挂板
        else:
            els.append(_box((1, 8, 7), (15, 16, 9), "#tex"))   # 挂牌
        return (els, {"tex": wood})
    if "banner" in base:
        return ([_box((1, 0, 7), (15, 16, 9), "#cloth"),       # 旗面
                 _box((7, 0, 6), (9, 16, 10), "#tex")],        # 旗杆
                {"cloth": "minecraft:block/%s_wool" % (color or "white"),
                 "tex": wood})
    if "shulker_box" in base:
        return ([_box((2, 0, 2), (14, 13, 14), "#tex"),
                 _box((2, 13, 2), (14, 14, 14), "#tex")],
                {"tex": particle or "minecraft:block/shulker_box"})
    if "head" in base or "skull" in base:
        return ([_box((4, 0, 4), (12, 8, 12), "#tex")], {"tex": wood})
    if base == "conduit":
        return ([_box((5, 5, 5), (11, 11, 11), "#tex")], {"tex": wood})
    if base == "decorated_pot":
        return ([_box((2, 0, 2), (14, 14, 14), "#tex")], {"tex": wood})
    return None


class Baker:
    def __init__(self, pack, mc_version, only=None):
        self.pack = pack
        self.mc_version = mc_version
        self.only = set(only) if only else None
        self.models = ModelLoader(pack)
        self.tex_ids = {}
        self.tex_blobs = []          # (name, w, h, rgba)
        self.variants = {}           # key (model,xr,yr,uvlock) -> idx
        self.be_variants = {}        # 原版方块实体：spec repr -> idx
        self.variant_list = []       # quads
        self.blockstates = []        # (block, mode, rules)
        self.block_tex = []          # (block, top, side, bottom)

    # -------------------------------------------------- 贴图 ----
    def _ensure_texture(self, name):
        if name in self.tex_ids:
            return self.tex_ids[name]
        data = self.pack.read(tex_resource_path(name))
        if data is None:
            return None
        try:
            w, h, rgba = _decode_png(data)
        except Exception:
            return None
        idx = len(self.tex_blobs)
        self.tex_ids[name] = idx
        self.tex_blobs.append((name, w, h, rgba))
        return idx

    def _preload_textures(self):
        """预登记所有命名空间下 textures/block(s) 的贴图 id。"""
        for n in sorted(self.pack.names()):
            parts = n.split("/")
            if (len(parts) < 5 or parts[0] != "assets" or parts[2] != "textures"
                    or parts[3] not in ("block", "blocks") or not n.endswith(".png")):
                continue
            ns = parts[1]
            rest = "/".join(parts[3:])[:-4]
            self._ensure_texture("%s:%s" % (ns, rest))

    # -------------------------------------------------- 变体 ----
    def _first_texture(self, model):
        """模型可用的第一个贴图 id（用于无 elements 的方块实体兜底）。"""
        texs = model.get("textures") or {}
        for key in ("particle", "all", "texture", "side", "top", "front",
                    "bottom", "end", "back"):
            ref = texs.get(key)
            if not ref:
                continue
            name = ModelLoader.resolve_texture(model, ref)
            tid = self._ensure_texture(name) if name else None
            if tid is not None:
                return tid
        for ref in texs.values():
            name = ModelLoader.resolve_texture(model, ref)
            tid = self._ensure_texture(name) if name else None
            if tid is not None:
                return tid
        return None

    @staticmethod
    def _cube_quads(texid):
        """整立方体 6 面（UV 归一化到 0..1 并翻转为 Blender 约定）。"""
        x0 = y0 = z0 = 0.0
        x1 = y1 = z1 = 16.0
        quads = []
        for d in range(6):
            corners = _face_corners(d, x0, y0, z0, x1, y1, z1)
            u1, v1, u2, v2 = _default_uv(d, x0, y0, z0, x1, y1, z1)
            uvs = []
            for p in corners:
                uu, vv = _uv_frac(d, p, x0, y0, z0, x1, y1, z1)
                uvs.append(((u1 + uu * (u2 - u1)) / 16.0,
                            1.0 - (v1 + vv * (v2 - v1)) / 16.0))
            quads.append((corners, d, texid, -1, 0, uvs))
        return quads

    # JSON 模型之外仍需叠加的原版方块实体几何（如钟的钟体）
    _BE_EXTRA = {
        "minecraft:bell": ("bell", "minecraft:entity/bell/bell_body"),
    }

    def _variant(self, model_name, xr, yr, uvlock, block=None):
        key = (model_name, xr, yr, uvlock, block)
        if key in self.variants:
            return self.variants[key]
        model = self.models.load(model_name)
        raw = []
        if model is not None:
            raw = build_model_quads(model, self._ensure_texture,
                                    xr, yr, uvlock)
            if not raw:                      # 方块实体：原版几何 / 代理模型
                specs = _be_specs(block) if block else None
                if specs:
                    raw = self._be_spec_quads(specs[0])
                if not raw:
                    raw = self._proxy_quads(block, model)
        quads = apply_variant_transform(raw, xr, yr, uvlock)
        extra = self._BE_EXTRA.get(block) if block else None
        if extra and quads:
            quads = quads + be_model_quads(extra[0], (), self._ensure_texture,
                                           tex=extra[1])
        idx = len(self.variant_list)
        self.variant_list.append(quads)
        self.variants[key] = idx
        return idx

    @staticmethod
    def _particle_name(model):
        ref = (model.get("textures") or {}).get("particle")
        return ModelLoader.resolve_texture(model, ref) if ref else None

    def _proxy_quads(self, block, model):
        """无 JSON 几何 -> 简化代理模型（失败则 particle 兜底立方体）。"""
        pm = proxy_model(block, self._particle_name(model)) if block else None
        if pm is not None:
            els, texs = pm
            synth = {"elements": els, "textures": texs, "tex_size": (16.0, 16.0)}
            q = build_model_quads(synth, self._ensure_texture)
            if q:
                return q
        texid = self._first_texture(model)
        return self._cube_quads(texid) if texid is not None else []

    # -------------------------------------------------- 原版方块实体 ----
    def _dyed(self, name, dye):
        """按染料色预乘贴图（原版用顶点色染色）。"""
        rgb = _DYE_RGB.get(dye)
        if not rgb or rgb == (1.0, 1.0, 1.0):
            return name
        key = "%s#dye=%s" % (name, dye)
        if key in self.tex_ids:
            return key
        tid = self._ensure_texture(name)
        if tid is None:
            return name
        _n, w, h, rgba = self.tex_blobs[tid]
        r, g, b = (int(round(c * 255.0)) for c in rgb)
        buf = bytearray(rgba)
        for i in range(0, len(buf), 4):
            buf[i] = buf[i] * r // 255
            buf[i + 1] = buf[i + 1] * g // 255
            buf[i + 2] = buf[i + 2] * b // 255
        idx = len(self.tex_blobs)
        self.tex_ids[key] = idx
        self.tex_blobs.append((key, w, h, bytes(buf)))
        return key

    def _pick_model(self, cands, tex_name):
        """按贴图实际尺寸挑选模型（原版头颅 64×64 / 64×32 两套布局）。"""
        tid = self._ensure_texture(tex_name)
        models = _be_models()
        if tid is None:
            return cands[0]
        _n, w, h, _rgba = self.tex_blobs[tid]
        for k in cands:
            d = models.get(k)
            if d and int(d["texW"]) == w and int(d["texH"]) == h:
                return k
        return cands[0]

    def _be_spec_quads(self, spec):
        """一个原版方块实体规格 -> quads。"""
        out = []
        for layer in spec["layers"]:
            model = layer["model"]
            if model is None and layer.get("model_pick"):
                model = self._pick_model(layer["model_pick"], layer["tex"])
            tex = layer["tex"]
            part_tex = dict(layer.get("part_tex") or {})
            dye = spec.get("dye")
            if dye:
                # 原版用顶点色染色；这里把染色预乘进贴图，只作用于指定部件
                targets = layer.get("dye_parts")
                if targets is None:
                    tex = self._dyed(tex, dye)
                else:
                    dtex = self._dyed(tex, dye)
                    for name in targets:
                        part_tex[name] = dtex
            out.extend(be_model_quads(
                model, spec["ops"], self._ensure_texture, tex=tex,
                part_tex=part_tex, skip=layer.get("skip") or (),
                part_offset=layer.get("part_offset")))
        return out

    def _be_variant(self, spec):
        """原版方块实体规格 -> 变体索引（相同规格复用）。"""
        key = repr(spec)
        if key in self.be_variants:
            return self.be_variants[key]
        quads = self._be_spec_quads(spec)
        if not quads:
            return None
        idx = len(self.variant_list)
        self.variant_list.append(quads)
        self.be_variants[key] = idx
        return idx

    # -------------------------------------------------- 方块状态 ----
    def _block_rule(self, apply_node, block=None):
        a = _apply_variants(apply_node)
        if not a or not a.get("model"):
            return None
        model = a["model"]
        if not model.startswith("minecraft:") and ":" not in model:
            model = "minecraft:" + model
        return self._variant(model, int(a.get("x", 0)), int(a.get("y", 0)),
                             bool(a.get("uvlock", False)), block)

    def bake_blockstate(self, block):
        raw = self.pack.json(blockstate_resource_path(block))
        if raw is None:
            return None
        specs = _be_specs(block)
        if specs:
            rules = []
            for spec in specs:
                vi = self._be_variant(spec)
                if vi is None:
                    continue
                rules.append(([(k, frozenset([v])) for k, v in spec["props"]],
                              vi))
            if rules:
                if rules[0][0]:                      # 缺属性时的兜底
                    rules.append(([], rules[0][1]))
                return (block, 0, rules)
        if "variants" in raw:
            rules = []
            for key, node in raw["variants"].items():
                vi = self._block_rule(node, block)
                if vi is None:
                    continue
                rules.append((_split_props(key), vi))
            if not rules:
                return None
            return (block, 0, rules)
        if "multipart" in raw:
            rules = []
            for part in raw["multipart"]:
                vi = self._block_rule(part.get("apply"), block)
                if vi is None:
                    continue
                for cond in _norm_cond(part.get("when")):
                    rules.append((cond, vi))
            if not rules:
                return None
            return (block, 1, rules)
        return None

    # -------------------------------------------------- 方块信息 ----
    @staticmethod
    def _first_model(raw):
        if not raw:
            return None
        if "variants" in raw and raw["variants"]:
            a = _apply_variants(next(iter(raw["variants"].values())))
            return a.get("model") if a else None
        if "multipart" in raw:
            for part in raw["multipart"]:
                a = _apply_variants(part.get("apply"))
                if a and a.get("model"):
                    return a["model"]
        return None

    @staticmethod
    def _is_full_cube(model):
        els = model.get("elements") or []
        if len(els) != 1:
            return False
        e = els[0]
        return (list(e.get("from") or []) == [0, 0, 0]
                and list(e.get("to") or []) == [16, 16, 16]
                and len(e.get("faces") or {}) >= 6)

    @staticmethod
    def _tint_mask(model):
        """首个含面的元素 -> 染色位掩码（bit0=top, bit1=bottom, bit2=side）。"""
        for elem in (model.get("elements") or []):
            faces = elem.get("faces") or {}
            if not faces:
                continue
            mask = 0
            for fname, f in faces.items():
                if int(f.get("tintindex", -1)) < 0:
                    continue
                if fname == "up":
                    mask |= 1
                elif fname == "down":
                    mask |= 2
                else:
                    mask |= 4
            return mask
        return 0

    def _classify(self, block, model, quads):
        """从模型形状与贴图透明度推断方块分类（供存档模式兜底）。"""
        base = block.split(":")[-1].lower()
        if not self._is_full_cube(model):
            return NONCUBE
        if any(h in base for h in _LIQUID_HINT):
            return LIQUID
        if any(h in base for h in _TRANSPARENT_HINT):
            return TRANSPARENT
        for _v, _d, texid, _t, _c, _uv in quads:
            _name, _w, _h, rgba = self.tex_blobs[texid]
            if min(rgba[3::4]) < 255:
                return CUTOUT
        return OPAQUE

    def _block_info(self, block):
        """(block, class, tint_mask, use_model, top, side, bottom)；无模型返回 None。

        use_model=1 表示模型不是简单整立方体（交叉植物/楼梯/栅栏等），
        网格器应对该方块注入烘焙模型几何而非整方块近似。"""
        raw = self.pack.json(blockstate_resource_path(block))
        model_name = self._first_model(raw)
        if not model_name:
            return None
        if not model_name.startswith("minecraft:") and ":" not in model_name:
            model_name = "minecraft:" + model_name
        model = self.models.load(model_name)
        if model is None:
            return None
        quads = build_model_quads(model, self._ensure_texture)
        if quads:
            cls = self._classify(block, model, quads)
            mask = self._tint_mask(model)
            use_model = 0 if self._is_full_cube(model) else 1
        else:                            # 方块实体（无 JSON 几何）
            quads = None
            specs = _be_specs(block)
            if specs:
                quads = self._be_spec_quads(specs[0])
            if not quads:                # 未收录 -> 简化代理模型
                quads = self._proxy_quads(block, model)
            if not quads:
                return None
            cls = NONCUBE
            mask = 0
            use_model = 1
        faces = {}
        for _verts, d, texid, _tint, _cull, _uv in quads:
            faces.setdefault(d, texid)
        top = faces.get(2)
        bottom = faces.get(3)
        side = faces.get(5, faces.get(4, faces.get(0, faces.get(1))))
        if side is None:
            side = top if top is not None else bottom
        if top is None:
            top = side
        if bottom is None:
            bottom = side
        if top is None:
            return None
        return (block, cls, mask, use_model, top, side, bottom)

    # -------------------------------------------------- 主流程 ----
    def run(self, out_path):
        _STATS["faces"] = _STATS["tex_miss"] = 0
        self._preload_textures()
        blocks = []
        for n in sorted(self.pack.names()):
            parts = n.split("/")
            if (len(parts) < 4 or parts[0] != "assets" or parts[2] != "blockstates"
                    or not n.endswith(".json")):
                continue
            ns = parts[1]
            name = "/".join(parts[3:])[:-5]
            full = "%s:%s" % (ns, name)
            if self.only and name not in self.only and full not in self.only:
                continue
            blocks.append(full)
        for b in blocks:
            bs = self.bake_blockstate(b)
            if bs:
                self.blockstates.append(bs)
            ft = self._block_info(b)
            if ft:
                self.block_tex.append(ft)
        self._write(out_path)
        return len(self.blockstates), len(self.variant_list), len(self.tex_blobs)

    def _write(self, path):
        # 变体索引与贴图 id 均为 u16：超限直接报错，避免写出静默损坏的包
        for what, n in (("变体", len(self.variant_list)),
                        ("贴图", len(self.tex_blobs))):
            if n > 0xFFFF:
                raise SystemExit("MCBA1 上限：%s数 %d 超过 65535，请用 --only 分批烘焙" % (what, n))
        out = [MAGIC, struct.pack("<B", VERSION), _pstr(self.mc_version)]
        # 贴图
        out.append(struct.pack("<I", len(self.tex_blobs)))
        for name, w, h, rgba in self.tex_blobs:
            out.append(_pstr(name) + struct.pack("<HHI", w, h, len(rgba)) + rgba)
        # 变体
        out.append(struct.pack("<I", len(self.variant_list)))
        for quads in self.variant_list:
            out.append(struct.pack("<H", len(quads)))
            for verts, d, texid, tint, cull, uvs in quads:
                rec = b"".join(_pack_vert(c) for c in verts)
                rec += struct.pack("<BHbB", d, texid & 0xFFFF, tint, cull)
                rec += struct.pack("<8f", *[v for uv in uvs for v in uv])
                out.append(rec)
        # 方块状态
        out.append(struct.pack("<I", len(self.blockstates)))
        for block, mode, rules in self.blockstates:
            out.append(_pstr(block) + struct.pack("<BH", mode, len(rules)))
            for pairs, vi in rules:
                out.append(struct.pack("<B", len(pairs)))
                for k, v in pairs:
                    # 多值用 '|' 连接（MC 属性值不含 '|'）
                    vs = v if isinstance(v, str) else "|".join(sorted(v))
                    out.append(_pstr(k) + _pstr(vs))
                out.append(struct.pack("<H", vi))
        # 默认面贴图
        out.append(struct.pack("<I", len(self.block_tex)))
        for block, cls, mask, use_model, top, side, bottom in self.block_tex:
            out.append(_pstr(block) + struct.pack("<BBBHHH", cls, mask, use_model,
                                                  top, side, bottom))
        data = b"".join(out)
        with open(path, "wb") as f:
            f.write(data)
        return len(data)


def _pack_vert(c):
    """顶点（1/16 方块单位）-> v2 的 1/256 单位 int16。

    模组模型（Blockbench）大量使用 0.25 / 0.5 像素等亚像素坐标与零厚度面；
    按 1/16 单位取整会把它们压成零面积面（整片几何消失），故 v2 放大 16 倍。
    方块局部几何量级 ≤ 32 像素，1/256 单位下远未触及 i16 上限。
    """
    q = []
    for v in c:
        n = int(round(float(v) * _VERT_SCALE))
        q.append(_I16_MIN if n < _I16_MIN else (_I16_MAX if n > _I16_MAX else n))
    return struct.pack("<3h", *q)


def _pstr(s):
    b = s.encode("utf-8")
    return struct.pack("<H", len(b)) + b


def _expand_inputs(inputs):
    """展开通配符（PowerShell / cmd 不会替原生命令展开 *）。

    无匹配时保留原样，交由 ResourcePack 给出可读报错。"""
    out = []
    for p in inputs:
        if any(ch in p for ch in "*?["):
            hit = sorted(glob.glob(p, recursive=True))
            if not hit:
                raise SystemExit("输入匹配不到文件: %s" % p)
            out.extend(hit)
        else:
            out.append(p)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="烘焙 MCBA1 资产包")
    ap.add_argument("inputs", nargs="+", help="客户端 jar / 资源包 zip（支持通配符）")
    ap.add_argument("-o", "--out", default="dist/assets.mcba")
    ap.add_argument("--mc-version", default="?")
    ap.add_argument("--only", default="", help="逗号分隔的方块名（不含命名空间）")
    args = ap.parse_args(argv)
    if Image is None:
        print("需要 Pillow：pip install pillow", file=sys.stderr)
        return 2
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    inputs = _expand_inputs(args.inputs)
    pack = ResourcePack(inputs)
    baker = Baker(pack, args.mc_version, only or None)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    nb, nv, nt = baker.run(args.out)
    size = os.path.getsize(args.out)
    ns = {}
    for name in baker.blockstates:
        ns[name[0].split(":")[0]] = ns.get(name[0].split(":")[0], 0) + 1
    print("BAKED: %d blocks, %d variants, %d textures -> %s (%.1f MB)"
          % (nb, nv, nt, args.out, size / 1048576.0), flush=True)
    print("  命名空间: %s" % ", ".join("%s=%d" % kv for kv in
                                       sorted(ns.items(), key=lambda t: -t[1])), flush=True)
    if _STATS["tex_miss"]:
        print("  警告: %d/%d 面因贴图缺失被丢弃（检查资源包是否包含对应 textures/**）"
              % (_STATS["tex_miss"], _STATS["faces"]), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
