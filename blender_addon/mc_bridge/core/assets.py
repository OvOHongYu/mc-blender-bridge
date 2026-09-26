# -*- coding: utf-8 -*-
"""MCBA1 资产包读取器（纯标准库，无 bpy 依赖）。

由 tools/bake_assets.py 生成，包含：
  - 贴图表（名称 -> RGBA 像素）
  - 烘焙变体（blockstate 变换后的四边形流，方块局部 0..16 坐标）
  - 变体级方块信息（v3：class / 染色掩码 / use_model / 默认面），
    使 slab 的 double 与 bottom 等不同状态各拿自己的分类与贴图
  - 方块状态表（属性 -> 变体索引）
  - 默认面贴图（方块 -> top/side/bottom 贴图 id，供完整方块材质管线）
  - 画变体表（v4：变体名 -> 宽/高/画面贴图 id，供存档模式实体渲染）

线程安全：加载后所有数据只读，可多线程共享。
"""
import json
import struct
import zlib

MAGIC = b"MCBA1"
VERSION = 6                  # 当前格式版本（读取端兼容 v1..v6）
_SUPPORTED = (1, 2, 3, 4, 5, 6)
_V2_SCALE = 16.0             # v2 顶点 -> 1/16 方块单位
_NONE_TEX = 0xFFFF           # 变体级信息里"该面无贴图"的哨兵

# quad = (verts tuple[(x,y,z)]*4, dir u8, tex u16, tint i8, cull u8, uvs tuple[(u,v)]*4)


class AssetPack:
    def __init__(self):
        self.mc_version = "?"
        self.tex_names = []
        self.tex_wh = []
        self.tex_rgba = []
        self._tex_by_name = {}
        self.variants = []
        self.variant_info = []      # 变体级 (class, tintMask, useModel, top, side, bottom)
        self.blockstates = {}       # name -> (mode, rules)
        self.block_faces = {}       # name -> (top, side, bottom)
        self.block_classes = {}     # name -> class u8
        self.block_tints = {}       # name -> 染色位掩码（bit0 top / bit1 bottom / bit2 side）
        self.block_use_model = {}   # name -> 1 表示应注入烘焙模型几何
        self.paintings = {}         # 画变体名 -> (w, h, tex_id)（v4 起；R5）
        self.entity_models = {}     # 实体模型名 -> {"texW","texH","tex","parts"}（v5；R5）
        self.biome_colors = {}      # 群系名 -> (grass, foliage, water) 0xRRGGBB（v6；R8）
        self._png_cache = {}

    # ------------------------------------------------------------ 加载 ----
    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls.from_bytes(f.read())

    @classmethod
    def from_bytes(cls, buf):
        assert buf[:5] == MAGIC, "bad MCBA1 magic"
        off = 5
        ver = buf[off]
        off += 1
        assert ver in _SUPPORTED, "unsupported MCBA1 version %d" % ver
        self = cls()
        self.mc_version, off = _rstr(buf, off)

        (n,) = struct.unpack_from("<I", buf, off)
        off += 4
        for i in range(n):
            name, off = _rstr(buf, off)
            w, h, ln = struct.unpack_from("<HHI", buf, off)
            off += 8
            rgba = buf[off:off + ln]
            off += ln
            self._tex_by_name[name] = i
            self.tex_names.append(name)
            self.tex_wh.append((w, h))
            self.tex_rgba.append(rgba)

        (nv,) = struct.unpack_from("<I", buf, off)
        off += 4
        for _ in range(nv):
            (nq,) = struct.unpack_from("<H", buf, off)
            off += 2
            quads = []
            for _ in range(nq):
                vs = struct.unpack_from("<12h", buf, off)
                off += 24
                d, tex, tint, cull = struct.unpack_from("<BHbB", buf, off)
                off += 5
                uv = struct.unpack_from("<8f", buf, off)
                off += 32
                if ver >= 2:
                    # v2：1/256 方块单位 -> 1/16（保留模组模型的亚像素坐标）
                    s = _V2_SCALE
                    verts = ((vs[0] / s, vs[1] / s, vs[2] / s),
                             (vs[3] / s, vs[4] / s, vs[5] / s),
                             (vs[6] / s, vs[7] / s, vs[8] / s),
                             (vs[9] / s, vs[10] / s, vs[11] / s))
                else:
                    verts = ((vs[0], vs[1], vs[2]), (vs[3], vs[4], vs[5]),
                             (vs[6], vs[7], vs[8]), (vs[9], vs[10], vs[11]))
                uvs = ((uv[0], uv[1]), (uv[2], uv[3]),
                       (uv[4], uv[5]), (uv[6], uv[7]))
                quads.append((verts, d, tex, tint, cull, uvs))
            self.variants.append(quads)
            if ver >= 3:
                cls, mask, use_model, top, side, bottom = struct.unpack_from(
                    "<BBBHHH", buf, off)
                off += 9
                self.variant_info.append(
                    (int(cls), int(mask), int(use_model), _tex_or_none(top),
                     _tex_or_none(side), _tex_or_none(bottom)))
            else:
                self.variant_info.append(None)

        (nb,) = struct.unpack_from("<I", buf, off)
        off += 4
        for _ in range(nb):
            name, off = _rstr(buf, off)
            mode = buf[off]
            off += 1
            (nr,) = struct.unpack_from("<H", buf, off)
            off += 2
            rules = []
            for _ in range(nr):
                (npair,) = struct.unpack_from("<B", buf, off)
                off += 1
                pairs = []
                for _ in range(npair):
                    k, off = _rstr(buf, off)
                    v, off = _rstr(buf, off)
                    pairs.append((k, frozenset(v.split("|"))))
                (vi,) = struct.unpack_from("<H", buf, off)
                off += 2
                rules.append((pairs, vi))
            self.blockstates[name] = (mode, rules)

        (nf,) = struct.unpack_from("<I", buf, off)
        off += 4
        for _ in range(nf):
            name, off = _rstr(buf, off)
            cls, mask, use_model, top, side, bottom = struct.unpack_from(
                "<BBBHHH", buf, off)
            off += 9
            self.block_faces[name] = (top, side, bottom)
            self.block_classes[name] = int(cls)
            self.block_tints[name] = int(mask)
            self.block_use_model[name] = int(use_model)

        if ver >= 4:
            # 画变体表（1.21+ painting_variant）：宽 / 高 / 画面贴图 id
            (npaint,) = struct.unpack_from("<I", buf, off)
            off += 4
            for _ in range(npaint):
                name, off = _rstr(buf, off)
                w, h, tex = struct.unpack_from("<HHH", buf, off)
                off += 6
                self.paintings[name] = (int(w), int(h), _tex_or_none(tex))

        if ver >= 5:
            # 实体模型段（分层实体模型，整体 JSON；R5 盔甲架等）
            (nem,) = struct.unpack_from("<I", buf, off)
            off += 4
            for _ in range(nem):
                name, off = _rstr(buf, off)
                (ln,) = struct.unpack_from("<I", buf, off)
                off += 4
                self.entity_models[name] = json.loads(buf[off:off + ln].decode("utf-8"))
                off += ln

        if ver >= 6:
            # 群系染色表（R8）：biome -> (grass, foliage, water)，均 0xRRGGBB
            (nbc,) = struct.unpack_from("<I", buf, off)
            off += 4
            for _ in range(nbc):
                name, off = _rstr(buf, off)
                g, f, wc = struct.unpack_from("<III", buf, off)
                off += 12
                self.biome_colors[name] = (int(g), int(f), int(wc))

        assert off == len(buf), "MCBA1 trailing bytes: %d" % (len(buf) - off)
        return self

    # ------------------------------------------------------------ 查询 ----
    def tex_id(self, name):
        return self._tex_by_name.get(name)

    def texture_rgba(self, tid):
        return self.tex_wh[tid], self.tex_rgba[tid]

    def texture_png(self, tid):
        """RGBA -> PNG 字节（缓存；纯标准库编码）。"""
        hit = self._png_cache.get(tid)
        if hit is not None:
            return hit
        (w, h), rgba = self.tex_wh[tid], self.tex_rgba[tid]
        raw = bytearray()
        stride = w * 4
        for y in range(h):
            raw.append(0)                       # filter type 0
            raw += rgba[y * stride:(y + 1) * stride]
        def chunk(tag, data):
            c = tag + data
            return (struct.pack(">I", len(data)) + c
                    + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF))
        png = b"\x89PNG\r\n\x1a\n"
        png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
        png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        png += chunk(b"IEND", b"")
        self._png_cache[tid] = png
        return png

    def biome_tint(self, biome):
        """群系 -> (grass, foliage, water)，每项 (r, g, b) 0..1；未知返回 None。"""
        v = self.biome_colors.get(biome)
        if v is None:
            return None
        return tuple(tuple(((x >> sh) & 0xFF) / 255.0 for sh in (16, 8, 0))
                     for x in v)

    def cube_overlay_extras(self, vis):
        """变体索引列表 -> 叠加层面片列表；不是"整立方体 + 叠加层"则返回 None。

        草方块这类方块的模型是「整立方体基底 + 与基底**完全共面**的叠加层元素」
        （`grass_block_side_overlay`，带 tintindex）。这类方块应当：
          - 基底面继续走贪心合并（草顶是最高频的面，合并能省大量几何）；
          - 只把叠加层单独注入（它只出现在暴露的侧面上，量很小）。
        否则要么丢叠加层（侧面草皮变成基底贴图里烘焙的平原绿），要么放弃合并
        （实测草密集区块几何 561 -> 1264）。

        判定：把面按顶点集分组，只有"存在共面重复"且"去重后的面方向恰好覆盖
        6 个方向（整立方体）"时才认定为叠加层结构；其余情况一律返回 None，
        由调用方整模型注入（避免误判）。"""
        quads = [q for vi in vis for q in self.variants[vi]]
        by_vs = {}
        order = []
        for q in quads:
            key = frozenset(tuple(round(c, 6) for c in v) for v in q[0])
            if key not in by_vs:
                by_vs[key] = []
                order.append(key)
            by_vs[key].append(q)
        extras = []
        base_dirs = []
        for key in order:
            gs = by_vs[key]
            base_dirs.append(int(gs[0][1]))
            extras.extend(gs[1:])
        if not extras:
            return None
        if sorted(base_dirs) != [0, 1, 2, 3, 4, 5]:
            return None            # 去重后不是整立方体 -> 不按叠加层处理
        return extras

    def default_faces(self, block):
        """(top, side, bottom) 贴图 id 或 None。传入方块状态名时按其状态解析。"""
        info = self._state_info(block)
        if info is not None:
            return (info[3], info[4], info[5])
        return _block_lookup(self.block_faces, block)

    def classify(self, block):
        """烘焙期推断的方块分类（无则 None）。传入方块状态名时按其状态解析。"""
        info = self._state_info(block)
        return info[0] if info is not None else _block_lookup(self.block_classes, block)

    def tint_mask(self, block):
        """染色位掩码（bit0 top / bit1 bottom / bit2 side）；无则 None。"""
        info = self._state_info(block)
        return info[1] if info is not None else _block_lookup(self.block_tints, block)

    def use_model(self, block):
        """该方块是否应注入烘焙模型几何（非简单整立方体）。"""
        info = self._state_info(block)
        if info is not None:
            return bool(info[2])
        return bool(_block_lookup(self.block_use_model, block))

    def _state_info(self, name):
        """方块状态名 -> 变体级信息；无按状态结果时返回 None。

        传 'ns:block[props]' 时用 blockstates 规则解析出该状态对应的变体，
        从而拿到该状态自己的 class/掩码/use_model/默认面（v3 包才有效）；
        传基础名或规则未命中时返回 None，调用方退回方块级信息。"""
        if not self.variant_info:
            return None
        block, props = _split_state(name)
        for vi in self.variant_indices(block, props):
            info = self.variant_info[vi] if vi < len(self.variant_info) else None
            if info is not None and info[3] is not None:   # 有默认面 = 有几何
                return info
        return None

    def variant_indices(self, block, props=None):
        """返回适用的变体索引列表（按方块状态属性匹配）。"""
        ent = self.blockstates.get(block)
        if ent is None:
            return []
        mode, rules = ent
        if props is None:
            props = {}
        if mode == 0:
            best, best_n = None, -1
            for pairs, vi in rules:
                if len(pairs) <= best_n:
                    continue
                if all(props.get(k) in vals for k, vals in pairs):
                    best, best_n = vi, len(pairs)
            return [best] if best is not None else []
        return [vi for pairs, vi in rules
                if all(props.get(k) in vals for k, vals in pairs)]

    def has_models(self, block):
        return block in self.blockstates


def _tex_or_none(t):
    """0xFFFF 哨兵 -> None（该面无烘焙贴图）。"""
    return None if t == _NONE_TEX else int(t)


def _block_lookup(table, name):
    """方块级信息查表：先按全名（含属性）查，再退回基础名。

    存档/服务端传来的都是带属性的方块状态名，而 v3 前（或 blockstates 未收录）
    的包只有方块级条目，字典键是基础名——不退回基础名会导致整块贴图与模型丢失。"""
    v = table.get(name)
    if v is None:
        v = table.get(name.split("[", 1)[0])
    return v


def _split_state(name):
    """'ns:block[a=1,b=2]' -> ('ns:block', {'a': '1', 'b': '2'})。"""
    i = name.find("[")
    if i < 0:
        return name, {}
    props = {}
    for kv in name[i + 1:].rstrip("]").split(","):
        k, sep, v = kv.partition("=")
        if sep:
            props[k.strip()] = v.strip()
    return name[:i], props


def _rstr(buf, off):
    (n,) = struct.unpack_from("<H", buf, off)
    off += 2
    return buf[off:off + n].decode("utf-8"), off + n


# ------------------------------------------------------------ 单例 ----
_PACK = None
_PACK_PATH = None


def current():
    return _PACK


def path():
    return _PACK_PATH


def load_global(path):
    """加载并设为全局资产包（插件与测试共用）。"""
    global _PACK, _PACK_PATH
    _PACK = AssetPack.load(path)
    _PACK_PATH = path
    return _PACK


def set_global(pack):
    global _PACK, _PACK_PATH
    _PACK = pack
    _PACK_PATH = None
    return pack


def clear():
    global _PACK, _PACK_PATH
    _PACK = None
    _PACK_PATH = None
