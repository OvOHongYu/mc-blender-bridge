# -*- coding: utf-8 -*-
"""MCBA1 资产包读取器（纯标准库，无 bpy 依赖）。

由 tools/bake_assets.py 生成，包含：
  - 贴图表（名称 -> RGBA 像素）
  - 烘焙变体（blockstate 变换后的四边形流，方块局部 0..16 坐标）
  - 方块状态表（属性 -> 变体索引）
  - 默认面贴图（方块 -> top/side/bottom 贴图 id，供完整方块材质管线）

线程安全：加载后所有数据只读，可多线程共享。
"""
import struct
import zlib

MAGIC = b"MCBA1"
VERSION = 2                  # v1 顶点为 1/16 方块单位；v2 为 1/256（亚像素）
_SUPPORTED = (1, 2)
_V2_SCALE = 16.0             # v2 顶点 -> 1/16 方块单位

# quad = (verts tuple[(x,y,z)]*4, dir u8, tex u16, tint i8, cull u8, uvs tuple[(u,v)]*4)


class AssetPack:
    def __init__(self):
        self.mc_version = "?"
        self.tex_names = []
        self.tex_wh = []
        self.tex_rgba = []
        self._tex_by_name = {}
        self.variants = []
        self.blockstates = {}       # name -> (mode, rules)
        self.block_faces = {}       # name -> (top, side, bottom)
        self.block_classes = {}     # name -> class u8
        self.block_tints = {}       # name -> 染色位掩码（bit0 top / bit1 bottom / bit2 side）
        self.block_use_model = {}   # name -> 1 表示应注入烘焙模型几何
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

    def default_faces(self, block):
        """(top, side, bottom) 贴图 id 或 None。"""
        return self.block_faces.get(block)

    def classify(self, block):
        """烘焙期推断的方块分类（无则 None）。"""
        return self.block_classes.get(block)

    def tint_mask(self, block):
        """染色位掩码（bit0 top / bit1 bottom / bit2 side）；无则 None。"""
        return self.block_tints.get(block)

    def use_model(self, block):
        """该方块是否应注入烘焙模型几何（非简单整立方体）。"""
        return bool(self.block_use_model.get(block, 0))

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
