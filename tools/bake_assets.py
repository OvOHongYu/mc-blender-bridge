#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""烘焙 MCBA1 资产包：从 Minecraft 客户端 jar / 资源包提取贴图与方块模型。

用法:
  python tools/bake_assets.py <client.jar|resourcepack.zip|mod.jar> [...] -o dist/assets.mcba
    --mc-version 1.21.1     写入包的版本标记
    --only oak_stairs,...   只烘焙指定方块（调试用，可带命名空间）
    --classes <path>        手工分类标注表（JSON）；未指定时取脚本目录下的
                            mcbridge-classes.json（存在才加载）

模组方块：把 mods/*.jar 一并作为输入即可（自动扫描其 assets/<ns>/ 下资源）。
  通配符由本脚本自己展开，因此在 PowerShell / cmd（不会替原生命令展开 *）下
  同样可用：python tools/bake_assets.py client.jar "mods/*.jar" -o dist/assets.mcba

手工分类标注表（mcbridge-classes.json）：对启发式判定不准或无 JSON 模型的方块
  （多为模组方块）手工指定 class / use_model / 贴图：
    {"mymod:weird_glass": "transparent",          # 紧凑写法：只覆盖 class
     "mymod:barrel": {"class": "noncube", "use_model": 0},
     "mymod:lamp": {"tex": {"top": "mymod:block/lamp_top",
                            "side": "mymod:block/lamp"}}}
  class = 类名或 0..5；use_model = true/false 或 0/1；tex = 贴图名（字符串表示
  六面相同）。无 JSON 模型的方块只有在给出 tex 时才能成条目（合成整立方体兜底）。
  未生效的条目在烘焙结束时告警。共享分类表（core/blocks.py）内的原版方块优先。

方块实体（箱子/床/告示牌/旗帜/潜影盒/头颅/陶罐/传送门框架/钟…）没有 JSON 几何，
其原版形状取自 tools/vanilla_be_models.json —— 由 tools/dump_be_models.ps1 从客户端
jar 直接调用原版模型工厂导出（缺失时退回简化代理模型）。换 MC 版本后需重跑该脚本。

输出 MCBA1（小端）:
  magic "MCBA1" | ver u8 = 5 | mcVersion str
  --- 贴图 ---  nTex u32；每项: name str | w u16 | h u16 | len u32 | RGBA 字节
  --- 变体 ---  nVar u32；每项: nQuads u16；
                每 quad: 12×i16 顶点 | dir u8 | tex u16 | tint i8 | cull u8 | 8×f32 UV
                变体级信息: class u8 | tintMask u8 | useModel u8
                           | top u16 | side u16 | bottom u16（0xFFFF = 无）
  v2 顶点单位为 1/256 方块（= 1/16 像素），模组模型（Blockbench 常用 0.25/0.5
  像素等亚像素坐标）不再被取整压扁；读取端 v1..v5 均兼容。
  v3 把 class/tintMask/useModel/默认面从"方块级"下沉到"变体级"：blockstate
  的不同状态各自解析到自己的变体，因此 slab 的 double 与 bottom、snowy 草方块
  等能拿到各自正确的分类与贴图（块级信息保留为兜底）。
  v4 新增**画变体表**（1.21+ 数据驱动 painting_variant）：
  --- 画 --- nPainting u32；每项: name str | w u16 | h u16 | tex u16
                （tex = 画面贴图 id；无该表的旧包读取端按"无画"处理）
  v5 新增**实体模型段**（运行时装配的**分层**实体模型，如盔甲架——部件旋转
  Pose 是运行时任意角度，必须保留每个部件的 pivot / 默认旋转 / 本地顶点）：
  --- 实体模型 --- nEntityModel u32；每项: name str | jsonLen u32 | JSON UTF-8
                JSON = {"texW","texH","tex":"<贴图名>","parts":[分层部件树]}，
                顶点为部件局部坐标（1/16 方块单位，以该部件 pivot 为原点）。
  --- 方块状态 --- nBlock u32；每项: name str | mode u8 | nRules u16；
                每 rule: nPairs u8；(key str, val str)×nPairs | variant u16
  --- 方块信息 --- nBlockTex u32；每项: name str | class u8 | tintMask u8
                | useModel u8 | top u16 | side u16 | bottom u16
                （tintMask bit0=top bit1=bottom bit2=side；useModel=1 表示应
                  注入烘焙模型几何而非整方块近似）

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
VERSION = 6                    # v1: 顶点 1/16；v2: 1/256（亚像素）；v3: 变体级方块信息；
                               # v4: 画变体表；v5: 实体模型段（分层，运行时装配）；
                               # v6: 群系染色表（biome -> grass/foliage/water RGB）
_VERT_SCALE = 16               # v2 写入倍数：1/16 单位 × 16 = 1/256 方块单位
_I16_MIN, _I16_MAX = -32768, 32767

# 烘焙期统计（诊断「导出失效」：贴图缺失会让面被丢弃；近似几何方块另出清单）
_STATS = {"faces": 0, "tex_miss": 0, "approx_blocks": []}


def _reset_stats():
    _STATS["faces"] = _STATS["tex_miss"] = 0
    del _STATS["approx_blocks"][:]

# 面方向编码（与 core/mesher.py 一致）: 0=+X 1=-X 2=+Y 3=-Y 4=+Z 5=-Z
FACE_DIR = {"east": 0, "west": 1, "up": 2, "down": 3, "south": 4, "north": 5}
DIR_VEC = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))

# 方块分类（与 core/blocks.py 一致）
AIR, OPAQUE, TRANSPARENT, LIQUID, CUTOUT, NONCUBE = 0, 1, 2, 3, 4, 5

# 手工标注表里可用的类名（大小写不敏感）
_CLASS_NAMES = {"air": AIR, "opaque": OPAQUE, "transparent": TRANSPARENT,
                "liquid": LIQUID, "cutout": CUTOUT, "noncube": NONCUBE}

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
def _anim_frame_height(meta, w, h):
    """从 .mcmeta 求动画单帧高度；无 animation 段或无法判定时返回 None。

    帧高优先取显式 "height"（非方形帧的条带），否则按 frames 列表长度均分；
    都没给则视为竖直方形条带（帧高 = 宽）。返回 h 表示"不是动画/只有一帧"。"""
    if not meta:
        return None
    try:
        obj = json.loads(meta.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    anim = obj.get("animation") if isinstance(obj, dict) else None
    if not isinstance(anim, dict):
        return None                              # 非动画（仅 mipmap/blur 等段）
    fh = anim.get("height")
    if isinstance(fh, int) and 0 < fh <= h:
        return fh
    frames = anim.get("frames")
    if isinstance(frames, list) and frames and h % len(frames) == 0:
        return h // len(frames)
    return w if (w > 0 and h != w and h % w == 0) else h


def _decode_png(data, meta=None, strip=True):
    """PNG -> (w, h, RGBA bytes)；动画贴图只取首帧。

    meta 为同名 .mcmeta 内容：声明了 animation 时按其帧高裁剪（权威），
    否则退回"高度是宽度整数倍即条带"的尺寸启发式（兼容无 .mcmeta 的包）。
    strip=False 关闭该启发式：画贴图是 16×宽 × 16×高（1×2 的画就是 16×32），
    同样满足整数倍关系但不是动画，误裁会丢掉下半幅。"""
    img = Image.open(io.BytesIO(data))
    img = img.convert("RGBA")
    w, h = img.size
    fh = _anim_frame_height(meta, w, h)
    if fh is None and strip and h != w and w > 0 and h % w == 0:
        fh = w                                   # 尺寸启发式：竖直动画条带
    if fh is not None and 0 < fh < h:
        img = img.crop((0, 0, w, fh))
        h = fh
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
            frot = int(face.get("rotation", 0))
            if uvlock and (xr or yr):
                # 原版 uvLock 同时改矩形与"面的旋转索引"（索引决定角点↔矩形角对应）
                (u1, v1, u2, v2), frot = _uvlock_rect(u1, v1, u2, v2, frot,
                                                       xr, yr, d)
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
    return _dedupe_coincident(out)


def _dedupe_coincident(quads):
    """丢掉与原面共面同形、只差绕序的面片。

    MC 的零厚度模型（`block/cross.json` 等）会给同一片"纸"正反两面各写一个 face
    （如 north + south），两者顶点集合相同、绕序相反。原版渲染剔除背面所以两面
    各画一次是对的；Blender 默认双面渲染（材质未开背面剔除），两者会**共面重叠**
    产生 Z-Fighting，只需留一个。同顶点集且贴图/染色一致才合并，避免误删。

    注意：**同位置但贴图/染色不同**的面（草方块侧面的基底 + side_overlay）两份都
    保留，且这里**不改动顶点**——叠加层与基底是否共面，是运行时（Blender 缺
    MC 那样的分层绘制顺序）才需要沿法向微推的事，见 mesher._OVERLAY_PUSH。
    资产包保持原版模型几何的精确值。"""
    seen = set()
    out = []
    for q in quads:
        verts, _d, texid, tint, _cull, _uvs = q
        key = (frozenset(tuple(round(c, 6) for c in v) for v in verts),
               texid, tint)
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
    return out


def apply_variant_transform(quads, xr, yr, uvlock):
    """按 blockstate 的 x/y 旋转（绕方块中心）变换几何。

    **次序：先 x 后 y**（与 MC 一致）。MC 的 ModelRotation.get(x, y) 生成的
    线性映射等价于 R_y ∘ R_x（已用 MC 1.21.1 本体探针逐组核对 16 种组合）：
    例如 chain 的 axis=x 用 x=90,y=90，up 轴先被 x 转到 -Z，再被 y 转到 +X；
    若反过来先 y 后 x，则仍停在 -Z（锁链横躺成南北向、末地烛朝向偏 90°，
    楼梯 half=top 的 x=180 与 y 组合也会整体错向）。
    UV 已在 build_model_quads 归一化并按 Blender 约定翻转，uvlock 的 UV
    锁定也在那里完成（本函数只转几何，不重复处理 UV）。"""
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
    """uvlock 的 3×3 线性部分（作用于 (u/16, v/16, 0)）。

    M = T(0.5) · D(d) · R^-1 · D(d')^T · T(-0.5)（d' = R·d，绕 (0.5,0.5) 取心），
    与 MC AffineTransformations.uvLock 逐元素一致（已用本体 96 组矩阵核对）。"""
    g = _rot_mat3(xr, yr)
    dv = _mat3_vec(g, DIR_VEC[d])
    d2 = _DIR_BY_VEC.get(tuple(int(round(c)) for c in dv), d)
    return _mat3_mul(_mat3_mul(_D_DIR_MAT[_DIR_NAME[d]], _mat3_t(g)),
                     _mat3_t(_D_DIR_MAT[_DIR_NAME[d2]]))


def _uvlock_rect(u1, v1, u2, v2, frot, xr, yr, d):
    """原版 BakedQuadFactory.uvLock：返回 (锁定后的 UV 矩形, 锁定后的 face rotation)。

    与之前只锁矩形不同，原版会**一并改动面的 rotation**：角点 i 对应的矩形角是
    (i + rot/90) % 4，因此 90°/270° 旋转下 u/v 轴互换——只锁矩形会让贴图转 90°。
    取对角点时用 getDirectionIndex 选的矩形角，新 rotation 由 M 作用于
    (cosθ, sinθ) 的 atan2 取整得到。已用本体 288 组 (x,y,面,矩形) 真值核对。"""
    m = _uvlock_mat(xr, yr, d)
    # T(.5)·M·T(-.5) 的平移：c - M·c（c = 方块中心 0.5）
    cx, cy, cz = 0.5, 0.5, 0.5
    gx = m[0][0] * cx + m[0][1] * cy + m[0][2] * cz
    gy = m[1][0] * cx + m[1][1] * cy + m[1][2] * cz
    tx, ty = cx - gx, cy - gy
    j0 = (frot // 90) % 4
    j2 = (2 + frot // 90) % 4

    def corner(j):
        return (u1 if j in (0, 1) else u2), (v1 if j in (0, 3) else v2)

    def tp(u, v):
        return (m[0][0] * (u / 16.0) + m[0][1] * (v / 16.0) + tx) * 16.0, \
               (m[1][0] * (u / 16.0) + m[1][1] * (v / 16.0) + ty) * 16.0

    cu0, cv0 = corner(j0)
    cu2, cv2 = corner(j2)
    nu0, nv0 = tp(cu0, cv0)
    nu2, nv2 = tp(cu2, cv2)

    def sgn(x):
        return (x > 0) - (x < 0)

    if sgn(nu2 - nu0) == sgn(cu2 - cu0):
        ou1, ou2 = nu0, nu2
    else:
        ou1, ou2 = nu2, nu0
    if sgn(nv2 - nv0) == sgn(cv2 - cv0):
        ov1, ov2 = nv0, nv2
    else:
        ov1, ov2 = nv2, nv0
    th = math.radians(frot)
    vx = m[0][0] * math.cos(th) + m[0][1] * math.sin(th)
    vy = m[1][0] * math.cos(th) + m[1][1] * math.sin(th)
    newrot = int((-round(math.degrees(math.atan2(vy, vx)) / 90.0) * 90) % 360)
    return (ou1, ov1, ou2, ov2), newrot


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
# 分层实体模型（tools/vanilla_entity_models.json）的贴图（原版渲染器的 getTexture）
_ENTITY_TEX = {
    "armor_stand": "minecraft:entity/armorstand/wood",
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
            # 原版 renderPart 的 z 平移是 translate(0, 0.5625, isFoot ? -1.0 : 0)：
            # **世界路径**（BedBlockEntityRenderer.render 的 getWorld() != null 分支）
            # 每个半张床各画在自己那一格、isFoot 恒为 false，故无 z 平移；
            # 只有 getWorld() == null（背包里同时画头脚两半）才用 -1.0 把另半推开一格。
            ops = [("t", (0.0, 0.5625, 0.0)), ("r", 0, 90.0),
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


# ------------------------------------------------------------ 群系染色 ----

def _biome_colormap(pack, name):
    """colormap png -> (w, h, pixel)；缺失返回 None。"""
    raw = pack.read("assets/minecraft/textures/colormap/%s.png" % name)
    if raw is None:
        return None
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    return img.size[0], img.size[1], img.load()


def _cm_color(cm, t, d):
    """等价原版 GrassColors/FoliageColors.getColor(temperature, downfall)。

    取自 1.21.1 字节码:
      i = (int)((1 - t) * 255); j = (int)((1 - d * t) * 255); k = (j << 8) | i
      return k < 65536 ? colorMap[k] : 0xFF00FF
    colorMap 按 png 行主序填充（colorMap[y * 256 + x] = ARGB(png(x, y))）。
    """
    if cm is None:
        return None
    w, h, px = cm
    i = int((1.0 - t) * 255.0)
    j = int((1.0 - d * t) * 255.0)
    k = (j << 8) | i
    if k < 0 or k >= w * h:
        return 0xFF00FF
    y, x = divmod(k, w)
    r, g, b = px[x, y]
    return (r << 16) | (g << 8) | b


def build_biome_colors(pack):
    """烘焙「群系 -> (grass, foliage, water) 0xRRGGBB」。

    取值路径按原版 Biome 的字节码，不做近似推理:
      grass   = effects.grass_color，否则 colormap(clamp(t), clamp(d))，
                再按 effects.grass_color_modifier 修正（SWAMP / DARK_FOREST）
      foliage = effects.foliage_color，否则 colormap(clamp(t), clamp(d))
      water   = effects.water_color（不走 colormap；原版默认 4159204）
    """
    grass_cm = _biome_colormap(pack, "grass")
    fol_cm = _biome_colormap(pack, "foliage")
    prefix = "data/minecraft/worldgen/biome/"
    out = {}
    for name in sorted(pack.names()):
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        try:
            b = pack.json(name)
        except Exception:
            continue
        bid = "minecraft:" + name[len(prefix):-5]
        eff = b.get("effects") or {}
        t = min(1.0, max(0.0, float(b.get("temperature", 0.5))))
        d = min(1.0, max(0.0, float(b.get("downfall", 0.5))))
        g = eff.get("grass_color")
        if g is None:
            g = _cm_color(grass_cm, t, d)
            if g is None:
                g = 0x91BD59                    # 无 colormap 时退回平原色
        mod = str(eff.get("grass_color_modifier") or "").lower()
        if mod == "dark_forest":
            # 原版: ((c & 0xFEFEFE) + 0x28340A) >> 1
            g = ((int(g) & 0xFEFEFE) + 0x28340A) >> 1
        elif mod == "swamp":
            # 原版: OctaveSimplexNoise(x*0.0225, z*0.0225) < -0.1 ? 5011004 : 6975545
            # 噪声需按坐标采样，此处取后者（#6A7039，沼泽黄绿）
            g = 6975545
        f = eff.get("foliage_color")
        if f is None:
            f = _cm_color(fol_cm, t, d)
            if f is None:
                f = 0x77AB2F
        wc = eff.get("water_color", 4159204)
        out[bid] = (int(g) & 0xFFFFFF, int(f) & 0xFFFFFF, int(wc) & 0xFFFFFF)
    return out


class Baker:
    def __init__(self, pack, mc_version, only=None, overrides=None):
        self.pack = pack
        self.mc_version = mc_version
        self.only = set(only) if only else None
        self.overrides = dict(overrides or {})   # 手工分类标注：block -> class
        self.overrides_applied = set()
        self.models = ModelLoader(pack)
        self.tex_ids = {}
        self.tex_blobs = []          # (name, w, h, rgba)
        self.variants = {}           # key (model,xr,yr,uvlock) -> idx
        self.be_variants = {}        # 原版方块实体：spec repr -> idx
        self.variant_list = []       # quads
        self.variant_info = []       # (class, tintMask, useModel, top, side, bottom)
        self.blockstates = []        # (block, mode, rules)
        self.block_tex = []          # (block, top, side, bottom)
        self.paintings = []          # (name, w, h, tex)（v4 画变体表）
        self.entity_models = {}      # name -> {"texW","texH","tex","parts"}（v5）
        self.biome_colors = {}       # biome -> (grass, foliage, water) 0xRRGGBB（v6）

    # -------------------------------------------------- 贴图 ----
    def _ensure_texture(self, name, strip=True):
        if name in self.tex_ids:
            return self.tex_ids[name]
        path = tex_resource_path(name)
        data = self.pack.read(path)
        if data is None:
            return None
        # 同名 .mcmeta：动画贴图按其帧高取首帧（见 _decode_png）
        meta = self.pack.read(path + ".mcmeta")
        try:
            w, h, rgba = _decode_png(data, meta, strip)
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
    def _cube_quads(texid, tids=None):
        """整立方体 6 面（UV 归一化到 0..1 并翻转为 Blender 约定）。

        tids=(top, side, bottom) 时按面组取贴图（缺的面用其余面兜底）；
        否则六面统一用 texid。"""
        x0 = y0 = z0 = 0.0
        x1 = y1 = z1 = 16.0
        quads = []
        for d in range(6):
            if tids is None:
                tid = texid
            else:
                g = 0 if d == 2 else (2 if d == 3 else 1)
                tid = tids[g] if tids[g] is not None else texid
            corners = _face_corners(d, x0, y0, z0, x1, y1, z1)
            u1, v1, u2, v2 = _default_uv(d, x0, y0, z0, x1, y1, z1)
            uvs = []
            for p in corners:
                uu, vv = _uv_frac(d, p, x0, y0, z0, x1, y1, z1)
                uvs.append(((u1 + uu * (u2 - u1)) / 16.0,
                            1.0 - (v1 + vv * (v2 - v1)) / 16.0))
            quads.append((corners, d, tid, -1, 0, uvs))
        return quads

    def _face_tids(self, tex):
        """标注贴图 (top, side, bottom) 名 -> id 元组；全缺或全缺贴图返回 None。"""
        if not tex:
            return None
        tids = tuple(self._ensure_texture(n) if n else None for n in tex)
        if all(t is None for t in tids):
            return None
        fallback = next(t for t in tids if t is not None)
        return tuple(fallback if t is None else t for t in tids)

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
        self.variant_info.append(self._variant_meta(block, model, raw, quads))
        self.variants[key] = idx
        return idx

    def _meta_of(self, block, model, json_quads):
        """(class, tintMask, useModel)；json_quads 非空 = 有 JSON 几何。"""
        if json_quads:
            return (self._classify(block, model, json_quads),
                    self._tint_mask(model),
                    0 if self._is_full_cube(model) else 1)
        return NONCUBE, 0, 1

    def _variant_meta(self, block, model, json_quads, quads):
        """变体级方块信息 (class, tintMask, useModel, top, side, bottom)。

        与块级 _block_info 同判定，但按**该变体自己的模型**计算——这样
        blockstate 的不同状态（如 slab 的 double 与 bottom）各自拿到正确的
        分类与默认面，而不是被方块级信息一刀切。手工标注同样作用于变体级。"""
        cls, mask, use_model = self._meta_of(block, model, json_quads)
        top, side, bottom = self._pick_faces(quads)
        ov = self.overrides.get(block) or {}
        if ov.get("class") is not None:
            cls = ov["class"]
        if ov.get("use_model") is not None:
            use_model = ov["use_model"]
        ov_tids = self._face_tids(ov.get("tex"))
        if ov_tids is not None:
            top, side, bottom = ov_tids
        return (cls, mask, use_model, top, side, bottom)

    @staticmethod
    def _pick_faces(quads):
        """quads -> (top, side, bottom) 贴图 id（缺的面互相兜底，全缺为 None）。"""
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
        return top, side, bottom

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

    def _be_variant(self, spec, block=None):
        """原版方块实体规格 -> 变体索引（相同规格复用）。"""
        key = repr(spec)
        if key in self.be_variants:
            return self.be_variants[key]
        quads = self._be_spec_quads(spec)
        if not quads:
            return None
        idx = len(self.variant_list)
        self.variant_list.append(quads)
        self.variant_info.append(self._variant_meta(block, None, [], quads))
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
                vi = self._be_variant(spec, block)
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
        """(block, class, tint_mask, use_model, top, side, bottom)；无法成条目返回 None。

        use_model=1 表示模型不是简单整立方体（交叉植物/楼梯/栅栏等），
        网格器应对该方块注入烘焙模型几何而非整方块近似。
        无 JSON 模型（动态代码模型/内建模型）时，仅当手工标注给出贴图，
        才用该贴图合成整立方体兜底——否则无法产出贴图信息。"""
        ov = self.overrides.get(block) or {}
        ov_tids = self._face_tids(ov.get("tex"))
        raw = self.pack.json(blockstate_resource_path(block))
        model_name = self._first_model(raw)
        model = None
        if model_name:
            if not model_name.startswith("minecraft:") and ":" not in model_name:
                model_name = "minecraft:" + model_name
            model = self.models.load(model_name)
        if model is None:
            # 无 JSON 模型（动态代码模型/内建模型）：仅当标注给出贴图才能成条目
            if ov_tids is None:
                return None
            quads = self._cube_quads(None, ov_tids)
            json_quads = []
        else:
            quads = build_model_quads(model, self._ensure_texture)
            json_quads = quads
            if not quads:                # 方块实体（无 JSON 几何）
                specs = _be_specs(block)
                if specs:
                    quads = self._be_spec_quads(specs[0])   # 原版方块实体：保真几何
                if not quads:            # 未收录 -> 简化代理模型
                    quads = self._proxy_quads(block, model)
                    if quads:
                        _STATS["approx_blocks"].append(block)
                if not quads:
                    return None
        cls, mask, use_model = self._meta_of(block, model, json_quads)
        if ov:                           # 手工标注覆盖启发式结果
            if ov.get("class") is not None:
                cls = ov["class"]
            if ov.get("use_model") is not None:
                use_model = ov["use_model"]
            self.overrides_applied.add(block)
        top, side, bottom = self._pick_faces(quads)
        if ov_tids is not None:          # 标注贴图优先于模型推出的默认面
            top, side, bottom = ov_tids
        if top is None:
            return None
        return (block, cls, mask, use_model, top, side, bottom)

    # -------------------------------------------------- 画（R5） ----
    def _painting_tex(self, asset_id):
        """asset_id -> 贴图 id。

        1.21 画贴图在 textures/painting/ 下；命名习惯上 asset_id 是"画画"名
        （如 minecraft:kebab），但也有包写成完整贴图名，两种都试。"""
        ns, path = (asset_id.split(":", 1) if ":" in asset_id
                    else ("minecraft", asset_id))
        for name in ("%s:painting/%s" % (ns, path), "%s:%s" % (ns, path)):
            tid = self._ensure_texture(name, strip=False)
            if tid is not None:
                return tid
        return None

    def bake_paintings(self):
        """烘焙画变体表：data/<ns>/painting_variant/<id>.json -> (name, w, h, tex)。

        width/height/asset_id 为 1.21+ 的数据驱动字段（24w18a 起）；
        ≤1.20 的版本把变体写死在代码里、没有该表，此处返回空表，
        读取端按"无画数据"退化（不影响其它功能）。"""
        out = []
        for n in sorted(self.pack.names()):
            parts = n.split("/")
            if (len(parts) < 4 or parts[0] != "data" or parts[2] != "painting_variant"
                    or not n.endswith(".json")):
                continue
            raw = self.pack.json(n)
            if not isinstance(raw, dict):
                continue
            ns = parts[1]
            name = "/".join(parts[3:])[:-5]
            w = int(raw.get("width") or 1)
            h = int(raw.get("height") or 1)
            asset = str(raw.get("asset_id") or ("%s:%s" % (ns, name)))
            tid = self._painting_tex(asset)
            if tid is None or not 1 <= w <= 16 or not 1 <= h <= 16:
                print("警告: 画变体 %s:%s 贴图缺失或尺寸非法，已跳过" % (ns, name),
                      file=sys.stderr)
                continue
            out.append(("%s:%s" % (ns, name), w, h, tid))
        return out

    # -------------------------------------------------- 实体模型（v5） ----
    def _load_entity_models(self):
        """加载 tools/vanilla_entity_models.json（分层实体模型）。

        该文件由 tools/be_models/DumpEntityModels.java 从客户端 jar 的原版模型
        工厂导出（保留每部件的 pivot / 默认旋转 / 本地顶点，供运行时按实体
        Pose 装配）；缺失时本段为空（不影响其它功能）。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "vanilla_entity_models.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        models = data.get("models") or {}
        for name, model in sorted(models.items()):
            tex = _ENTITY_TEX.get(name)
            if tex:
                self._ensure_texture(tex)        # 实体贴图进包
                model = dict(model, tex=tex)
            self.entity_models[name] = model

    # -------------------------------------------------- 主流程 ----
    def run(self, out_path):
        _reset_stats()
        self._preload_textures()
        self.paintings = self.bake_paintings()
        self._load_entity_models()
        self.biome_colors = build_biome_colors(self.pack)
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
        # 变体（v3 起附带变体级方块信息：class/tintMask/useModel/默认面）
        if len(self.variant_info) != len(self.variant_list):
            raise SystemExit("内部错误：变体信息与变体数不一致（%d != %d）"
                             % (len(self.variant_info), len(self.variant_list)))
        out.append(struct.pack("<I", len(self.variant_list)))
        for quads, info in zip(self.variant_list, self.variant_info):
            out.append(struct.pack("<H", len(quads)))
            for verts, d, texid, tint, cull, uvs in quads:
                rec = b"".join(_pack_vert(c) for c in verts)
                rec += struct.pack("<BHbB", d, texid & 0xFFFF, tint, cull)
                rec += struct.pack("<8f", *[v for uv in uvs for v in uv])
                out.append(rec)
            cls, mask, use_model, top, side, bottom = info
            out.append(struct.pack("<BBBHHH", cls, mask, use_model,
                                   _u16_or_none(top), _u16_or_none(side),
                                   _u16_or_none(bottom)))
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
        # 画变体表（v4）
        out.append(struct.pack("<I", len(self.paintings)))
        for name, w, h, tex in self.paintings:
            out.append(_pstr(name) + struct.pack("<HHH", w, h, tex & 0xFFFF))
        # 实体模型段（v5）：分层实体模型，整体以 JSON 存（运行时装配）
        out.append(struct.pack("<I", len(self.entity_models)))
        for name, model in self.entity_models.items():
            blob = json.dumps(model, separators=(",", ":")).encode("utf-8")
            out.append(_pstr(name) + struct.pack("<I", len(blob)) + blob)
        # 群系染色表（v6）：name | grass u32 | foliage u32 | water u32（均 0xRRGGBB）
        out.append(struct.pack("<I", len(self.biome_colors)))
        for name in sorted(self.biome_colors):
            g, f, wc = self.biome_colors[name]
            out.append(_pstr(name) + struct.pack("<III", g, f, wc))
        data = b"".join(out)
        with open(path, "wb") as f:
            f.write(data)
        return len(data)


_NONE_TEX = 0xFFFF             # 变体级信息里"该面无贴图"的哨兵


def _u16_or_none(t):
    """贴图 id -> u16；None 写成 0xFFFF（贴图 id 上限 65534，不会冲突）。"""
    return _NONE_TEX if t is None else int(t) & 0xFFFF


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


def _warn_override(name, val, field):
    print("警告: 分类标注 %s 的 %s=%r 无法识别，已忽略" % (name, field, val),
          file=sys.stderr)


def _class_value(v):
    """标注里的 class 值 -> 0..5 整数；无法识别返回 None。"""
    if isinstance(v, str):
        v = _CLASS_NAMES.get(v.strip().lower())
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 5:
        return None
    return int(v)


def _tex_value(v):
    """标注里的 tex 值 -> (top, side, bottom) 贴图名；无法识别返回 None。

    字符串 = 六面同一贴图；对象给 top/side/bottom（缺的面由其余面兜底）。"""
    if isinstance(v, str):
        n = v.strip()
        return (n, n, n) if n else None
    if isinstance(v, dict):
        out = tuple((v.get(k) or None) for k in ("top", "side", "bottom"))
        return out if any(out) else None
    return None


def _norm_override(name, val):
    """标注值 -> {'class','use_model','tex'}（未覆盖项为 None）；全无效返回 None。

    紧凑写法：值直接是类名/整数，等价于只覆盖 class。
    完整写法：{"class": ..., "use_model": ..., "tex": ...}。"""
    if isinstance(val, (str, int)) and not isinstance(val, bool):
        cls = _class_value(val)
        if cls is None:
            _warn_override(name, val, "class")
            return None
        return {"class": cls, "use_model": None, "tex": None}
    if not isinstance(val, dict):
        _warn_override(name, val, "class")
        return None
    out = {"class": None, "use_model": None, "tex": None}
    if "class" in val:
        out["class"] = _class_value(val["class"])
        if out["class"] is None:
            _warn_override(name, val["class"], "class")
    if "use_model" in val:
        um = val["use_model"]
        if isinstance(um, bool):
            out["use_model"] = 1 if um else 0
        elif isinstance(um, int) and um in (0, 1):
            out["use_model"] = um
        else:
            _warn_override(name, um, "use_model")
    if "tex" in val:
        out["tex"] = _tex_value(val["tex"])
        if out["tex"] is None:
            _warn_override(name, val["tex"], "tex")
    if all(v is None for v in out.values()):
        return None
    return out


def load_overrides(path):
    """读取手工标注表 -> {block: {'class','use_model','tex'}}。

    值可为紧凑的类名/整数（只覆盖 class），或完整对象：
      {"mymod:weird_glass": "transparent",
       "mymod:barrel": {"class": "noncube", "use_model": 0},
       "mymod:lamp": {"tex": {"top": "mymod:block/lamp_top",
                              "side": "mymod:block/lamp"}}}
    class 取类名（air/opaque/transparent/liquid/cutout/noncube）或 0..5 整数；
    use_model 取 true/false 或 0/1；tex 取贴图名（字符串 = 六面相同）。
    非法项警告后跳过，不中断烘焙。path 为 None 时返回空表。
    """
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as e:
        raise SystemExit("无法读取分类标注表 %s: %s" % (path, e))
    except ValueError as e:
        raise SystemExit("分类标注表 %s 不是合法 JSON: %s" % (path, e))
    if not isinstance(raw, dict):
        raise SystemExit("分类标注表 %s 顶层应为对象 {方块名: 标注}" % path)
    out = {}
    for name, val in raw.items():
        ov = _norm_override(name, val)
        if ov is not None:
            out[name] = ov
    return out


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
    ap.add_argument("--classes", default=None,
                    help="手工分类标注表 JSON（默认: 脚本目录下 mcbridge-classes.json）")
    args = ap.parse_args(argv)
    if Image is None:
        print("需要 Pillow：pip install pillow", file=sys.stderr)
        return 2
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    cls_path = args.classes
    if cls_path is None:                    # 约定位置存在才自动加载
        cand = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "mcbridge-classes.json")
        cls_path = cand if os.path.exists(cand) else None
    overrides = load_overrides(cls_path)
    if overrides:
        print("分类标注: %s（%d 条）" % (cls_path, len(overrides)), flush=True)
    inputs = _expand_inputs(args.inputs)
    pack = ResourcePack(inputs)
    baker = Baker(pack, args.mc_version, only or None, overrides)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    nb, nv, nt = baker.run(args.out)
    size = os.path.getsize(args.out)
    ns = {}
    for name in baker.blockstates:
        ns[name[0].split(":")[0]] = ns.get(name[0].split(":")[0], 0) + 1
    print("BAKED: %d blocks, %d variants, %d textures -> %s (%.1f MB)"
          % (nb, nv, nt, args.out, size / 1048576.0), flush=True)
    if baker.paintings:
        print("  画变体: %d（painting_variant）" % len(baker.paintings), flush=True)
    print("  命名空间: %s" % ", ".join("%s=%d" % kv for kv in
                                       sorted(ns.items(), key=lambda t: -t[1])), flush=True)
    if _STATS["tex_miss"]:
        print("  警告: %d/%d 面因贴图缺失被丢弃（检查资源包是否包含对应 textures/**）"
              % (_STATS["tex_miss"], _STATS["faces"]), file=sys.stderr, flush=True)
    approx = _STATS["approx_blocks"]
    if approx:
        listed = ", ".join(approx[:20])
        tail = "" if len(approx) <= 20 else " …（共 %d 个）" % len(approx)
        print("  近似几何: %d 个方块为代理模型/整立方兜底: %s%s"
              % (len(approx), listed, tail), file=sys.stderr, flush=True)
    missed = sorted(set(overrides) - baker.overrides_applied)
    if missed:
        print("  警告: %d 条分类标注未生效（方块无可用模型或未烘焙）: %s"
              % (len(missed), ", ".join(missed[:20])), file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
