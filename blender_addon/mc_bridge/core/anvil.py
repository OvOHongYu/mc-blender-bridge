# -*- coding: utf-8 -*-
"""存档模式：直接解析 Minecraft Java 版存档（Anvil 格式），无需模组 / 运行实例。

- NBT 解析（大端；兼容 1.18+ 区块结构，sections 直接挂根；兼容旧版 Level 包裹）
- Region (.mca) 读取：偏移表 + zlib/gzip/无压缩块，整文件头部缓存 + 惰性扇区读取
- AnvilWorld：按维度提供 MCC1 同构 payload；区块解析 LRU，降低重复 NBT 解析
- SaveClient：与 ApiClient 同构的接口，Scheduler 可直接使用（本地网格化）

效率要点：
  - Region 文件头一次读取，区块按扇区偏移惰性读，避免整文件驻留内存；
  - 区块 payload（含 numpy 解码结果）LRU 缓存（默认 512）；
  - 调色板容器位压缩用 numpy 向量化解码，单区块毫秒级；
  - 未知方块按 base 名查共享分类表，兜底 class=1（不透明）。
"""
import gzip
import os
import struct
import threading
import zlib
from collections import OrderedDict

import numpy as np

from . import blocks as B
from . import mesher
from . import util

# 维度 -> (region 子目录, min_y, height)
DIMS = {
    "minecraft:overworld": ("", -64, 384),
    "minecraft:the_nether": ("DIM-1", 0, 256),
    "minecraft:the_end": ("DIM1", 0, 256),
}

_SHORT2FULL = {"overworld": "minecraft:overworld",
               "the_nether": "minecraft:the_nether", "nether": "minecraft:the_nether",
               "the_end": "minecraft:the_end", "end": "minecraft:the_end"}


def norm_dim(dim):
    """维度名归一化（接受短名 overworld / the_nether / the_end）。"""
    return _SHORT2FULL.get(dim, dim)

# 区块压缩类型
_COMPRESS_NONE, _COMPRESS_GZIP, _COMPRESS_ZLIB = 0, 1, 2

# NBT tag id
_TAG_END = 0


# ---------------------------------------------------------------- NBT ----

class NBTReader:
    """NBT 大端读取器。返回 dict/list/int/float/str/bytes/np.uint64 数组。"""

    def __init__(self, data: bytes):
        self.d = data
        self.o = 0

    def read(self, n):
        v = self.d[self.o:self.o + n]
        self.o += n
        return v

    def u8(self):
        v = self.d[self.o]
        self.o += 1
        return v

    def signed(self, n):
        fmt = {1: ">b", 2: ">h", 4: ">i", 8: ">q"}[n]
        v = struct.unpack_from(fmt, self.d, self.o)[0]
        self.o += n
        return v

    def f32(self):
        v = struct.unpack_from(">f", self.d, self.o)[0]
        self.o += 4
        return v

    def f64(self):
        v = struct.unpack_from(">d", self.d, self.o)[0]
        self.o += 8
        return v

    def str(self):
        n = self.signed(2)
        s = self.d[self.o:self.o + n].decode("utf-8", "replace")
        self.o += n
        return s

    def read_named(self):
        """返回 (name, payload) 或 None（TAG_End）。"""
        tag = self.u8()
        if tag == _TAG_END:
            return None
        name = self.str()
        return name, self.read_payload(tag)

    def read_payload(self, tag):
        if tag == 1:
            return self.signed(1)
        if tag == 2:
            return self.signed(2)
        if tag == 3:
            return self.signed(4)
        if tag == 4:
            return self.signed(8)
        if tag == 5:
            return self.f32()
        if tag == 6:
            return self.f64()
        if tag == 7:
            n = self.signed(4)
            return self.read(n)
        if tag == 8:
            return self.str()
        if tag == 9:
            etag = self.u8()
            n = self.signed(4)
            return [self.read_payload(etag) for _ in range(n)]
        if tag == 10:
            d = {}
            while True:
                named = self.read_named()
                if named is None:
                    break
                k, v = named
                d[k] = v
            return d
        if tag == 11:
            n = self.signed(4)
            return np.frombuffer(self.read(n * 4), ">i4").copy().tolist()
        if tag == 12:
            n = self.signed(4)
            return np.frombuffer(self.read(n * 8), ">u8").copy()
        raise ValueError("unknown NBT tag %d" % tag)


def _unpack_indices(longs, bits, n=4096, contiguous=False):
    """MC PalettedContainer 位流解码（LSB-first、大端 long 存储）。

    longs: np.uint64 数组。
    contiguous=False（默认，MC 1.16+）: 条目不跨 long 边界——
        每 long 存 floor(64/bits) 个条目，高位不足 bits 的剩余位补零；
        条目 i 位于 long i//vpl 的 (i%vpl)*bits 偏移。
    contiguous=True（1.15- 旧格式）: 位流跨 long 连续，条目 i 占用位
        [i*bits, (i+1)*bits)。"""
    if bits <= 0 or longs is None or len(longs) == 0:
        return np.zeros(n, np.uint16)
    arr = np.asarray(longs, np.uint64)
    if contiguous:
        b = (arr[:, None] >> np.arange(64, dtype=np.uint64)) & 1
        flat = b.astype(np.uint8).ravel()
        total = n * bits
        if flat.size < total:
            flat = np.resize(flat, total)
        bitm = flat[:total].reshape(n, bits)
        w = (np.uint64(1) << np.arange(bits, dtype=np.uint64))
        vals = (bitm.astype(np.uint64) * w).sum(axis=1)
    else:
        vpl = max(1, 64 // bits)
        i = np.arange(n)
        li = np.minimum(i // vpl, len(arr) - 1)
        shifts = ((i % vpl) * bits).astype(np.uint64)
        vals = (arr[li] >> shifts) & np.uint64((1 << bits) - 1)
    return np.clip(vals, 0, 65535).astype(np.uint16)


def _decode_block_states(bs):
    """block_states 字典 -> (base 名列表, indices uint16(4096))。

    palette 元素兼容两种格式：
      - compound: {"Name": "minecraft:stone", "Properties": {...}}（真实存档）
      - 字符串:   "minecraft:stone[facing=north,...]"（模拟/简易格式）

    bits 由 palette 大小推导（max(4, bit_length(palSize-1))）；按 data 长度
    自动识别 1.16+（条目不跨 long）与 1.15-（连续位流）两种打包格式。"""
    pal = bs.get("palette") or []
    if not pal:
        return [], np.zeros(4096, np.uint16)
    names = []
    for p in pal:
        if isinstance(p, dict):
            names.append(str(p.get("Name", "minecraft:air")))
        else:
            names.append(str(p).split("[", 1)[0])
    data = bs.get("data")
    if len(pal) == 1 or data is None or len(data) == 0:
        return names, np.zeros(4096, np.uint16)
    arr = np.asarray(data, np.uint64)
    bits = max(4, (len(pal) - 1).bit_length())
    vpl = max(1, 64 // bits)
    need_span = (4096 + vpl - 1) // vpl        # 1.16+ 打包所需 long 数
    need_cont = (4096 * bits + 63) // 64       # 1.15- 连续打包所需 long 数
    if len(arr) == need_span:
        idx = _unpack_indices(arr, bits)
    elif len(arr) == need_cont:
        idx = _unpack_indices(arr, bits, contiguous=True)
    else:
        # 长度都对不上（异常数据）：按 1.16+ 尽力而为
        idx = _unpack_indices(arr, bits)
    idx = np.clip(idx, 0, len(pal) - 1)
    return names, idx.astype(np.uint16)


def _classify(name):
    """base 名 -> class 字节（未知方块按不透明处理）。"""
    base = name.split("[", 1)[0]
    gid = B.INDEX.get(base)
    if gid is not None:
        return int(B.CLASS[gid])
    return 1


# ---------------------------------------------------------------- Region ----

class RegionFile:
    """单个 .mca 文件：头部偏移表缓存 + 按扇区惰性读取。"""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        with open(path, "rb") as f:
            head = f.read(8192)
        loc = head[:4096]
        self._loc = [((loc[i * 4] << 16) | (loc[i * 4 + 1] << 8) | loc[i * 4 + 2],
                      loc[i * 4 + 3]) for i in range(1024)]
        self._fh = open(path, "rb")

    def chunk_bytes(self, lx, lz):
        """返回区块 NBT 原始字节；不存在返回 None。线程安全。"""
        with self.lock:
            off, nsec = self._loc[(lz & 31) * 32 + (lx & 31)]
            if off == 0 or nsec == 0:
                return None
            self._fh.seek(off * 4096)
            head = self._fh.read(5)
            if len(head) < 5:
                return None
            length, ctype = struct.unpack(">IB", head)
            payload = self._fh.read(length - 1)
        if ctype == _COMPRESS_ZLIB:
            return zlib.decompress(payload)
        if ctype == _COMPRESS_GZIP:
            return gzip.decompress(payload)
        if ctype == _COMPRESS_NONE:
            return payload
        raise ValueError("unsupported chunk compression %d" % ctype)

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


# ---------------------------------------------------------------- World ----

class AnvilWorld:
    """存档根目录只读访问：维度 region + 区块 payload LRU。"""

    def __init__(self, save_root):
        if not os.path.isdir(save_root):
            raise FileNotFoundError("存档目录不存在: %s" % save_root)
        region_dirs = [os.path.join(save_root, "region")]
        region_dirs += [os.path.join(save_root, d, "region") for d in ("DIM-1", "DIM1")]
        if not any(os.path.isdir(d) for d in region_dirs):
            raise FileNotFoundError(
                "不是有效的 MC 存档目录（缺少 region/）: %s" % save_root)
        self.save_root = save_root
        self._regions = OrderedDict()          # (dim, rx, rz) -> RegionFile
        self._region_cap = 64
        self._payloads = OrderedDict()         # (dim, cx, cz) -> MCC1 payload
        self._payload_cap = 512
        self.lock = threading.Lock()
        self.mc_version = self._read_level_dat()

    # ------------------------------------------------------------ 元数据 ----
    def _read_level_dat(self):
        try:
            path = os.path.join(self.save_root, "level.dat")
            if not os.path.exists(path):
                return "?"
            with open(path, "rb") as f:
                raw = f.read()
            data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
            named = NBTReader(data).read_named()
            root = named[1] if named else {}
            dv = root.get("DataVersion")
            return str(dv) if dv is not None else "?"
        except Exception:
            return "?"

    # ------------------------------------------------------------ Region ----
    def _dim_dir(self, dim):
        dim = norm_dim(dim)
        if dim not in DIMS:
            raise KeyError("不支持的维度: %s" % dim)
        sub = DIMS[dim][0]
        base = self.save_root if not sub else os.path.join(self.save_root, sub)
        return os.path.join(base, "region")

    def _region(self, dim, rx, rz):
        key = (dim, rx, rz)
        reg = self._regions.get(key)
        if reg is not None:
            self._regions.move_to_end(key)
            return reg
        path = os.path.join(self._dim_dir(dim), "r.%d.%d.mca" % (rx, rz))
        if not os.path.exists(path):
            return None
        reg = RegionFile(path)
        with self.lock:
            self._regions[key] = reg
            self._regions.move_to_end(key)
            while len(self._regions) > self._region_cap:
                _, old = self._regions.popitem(last=False)
                old.close()
        return reg

    def _find_chunk(self, dim, cx, cz):
        reg = self._region(dim, cx >> 5, cz >> 5)
        if reg is None:
            return None
        return reg.chunk_bytes(cx & 31, cz & 31)

    def close(self):
        with self.lock:
            for reg in self._regions.values():
                reg.close()
            self._regions.clear()
            self._payloads.clear()

    # ------------------------------------------------------------ 区块 ----
    def _clip(self, dim, ymin, ymax):
        dim = norm_dim(dim)
        min_y, height = DIMS[dim][1], DIMS[dim][2]
        yb = max(min_y, (ymin - min_y) // 16 * 16 + min_y)
        yt = min(min_y + height, (ymax - min_y + 15) // 16 * 16 + min_y)
        if yt <= yb:
            yt = yb + 16
        return yb, yt, (yt - yb) // 16

    def _empty_payload(self, dim, cx, cz, ymin, ymax):
        yb, _, nsec = self._clip(dim, ymin, ymax)
        return {"dim": dim, "cx": cx, "cz": cz, "yBottom": yb,
                "sections": [None] * nsec}

    def chunk_payload(self, dim, cx, cz, ymin, ymax):
        """MCC1 同构 payload（sections 含 None）。线程安全。"""
        dim = norm_dim(dim)
        key = (dim, cx, cz)
        with self.lock:
            pl = self._payloads.get(key)
            if pl is not None:
                self._payloads.move_to_end(key)
                return pl
        data = self._find_chunk(dim, cx, cz)
        if data is None:
            pl = self._empty_payload(dim, cx, cz, ymin, ymax)
        else:
            pl = self._parse_chunk(dim, cx, cz, data, ymin, ymax)
        with self.lock:
            self._payloads[key] = pl
            self._payloads.move_to_end(key)
            while len(self._payloads) > self._payload_cap:
                self._payloads.popitem(last=False)
        return pl

    def _parse_chunk(self, dim, cx, cz, data, ymin, ymax):
        named = NBTReader(data).read_named()
        root = named[1] if named else {}
        chunk = root.get("Level") if isinstance(root.get("Level"), dict) else root
        yb, _, nsec = self._clip(dim, ymin, ymax)
        sections = [None] * nsec
        for s in chunk.get("sections") or []:
            y = s.get("Y")
            if y is None:
                continue
            wy = y * 16
            if wy < yb or wy >= yb + nsec * 16:
                continue
            si = (wy - yb) // 16
            bs = s.get("block_states")
            if bs is None:
                continue
            names, idx = _decode_block_states(bs)
            if not names:
                continue
            if len(names) == 1 and names[0] == "minecraft:air":
                continue
            pal = [(_classify(n), n) for n in names]
            sections[si] = {"palette": pal, "indices": idx}
        return {"dim": dim, "cx": cx, "cz": cz, "yBottom": yb, "sections": sections}


# ------------------------------------------------------------ 程序化贴图 ----

def procedural_png(block, face="side"):
    """16×16 程序化贴图（无 MC 资产时保证材质管线一致）。"""
    name = block.split(":")[-1]
    try:
        _, _, tint, color = B.block_info(block)
    except KeyError:
        color = (0.6, 0.6, 0.6)
    base = np.array(color, float)
    if name == "grass_block":
        if face == "top":
            base = np.array([0.42, 0.62, 0.28])
        elif face == "bottom":
            base = np.array([0.53, 0.36, 0.24])
        else:
            base = np.array([0.53, 0.36, 0.24])
    elif name == "oak_log":
        base = np.array([0.66, 0.52, 0.34]) if face in ("top", "bottom") \
            else np.array([0.41, 0.29, 0.16])
    img = np.zeros((16, 16, 4), np.uint8)
    seed = sum(ord(c) for c in name)
    for y in range(16):
        for x in range(16):
            n = ((x * 73856093 ^ y * 19349663 ^ seed) & 0xFFFFFFF) / float(0xFFFFFFF)
            c = np.clip(base * (0.9 + 0.2 * n), 0, 1)
            a = 255
            if name in ("water", "lava"):
                a = 170
                c = np.array([0.24, 0.42, 0.75]) if name == "water" \
                    else np.array([0.85, 0.30, 0.12])
            elif name == "glass":
                border = x in (0, 15) or y in (0, 15)
                streak = (x + y) in (7, 8, 9) and x > 3
                a = 255 if (border or streak) else 0
                c = np.array([0.8, 0.9, 0.95]) if a else c
            elif "leaves" in name:
                a = 255 if ((x * 31 + y * 17 + seed) & 0x7F) < 0x66 else 0
                c = np.array([0.9, 0.9, 0.9])
            img[y, x, :3] = (c * 255).astype(np.uint8)
            img[y, x, 3] = a
    return util.png_bytes(16, 16, img.reshape(-1).tobytes())


# ---------------------------------------------------------------- Client ----

class SaveClient:
    """与 ApiClient 同构的存档读取客户端（Scheduler 可直接使用）。"""

    def __init__(self, world: AnvilWorld):
        self.world = world
        from collections import OrderedDict
        self._mesh_cache = OrderedDict()

    def close(self):
        self.world.close()

    def ping(self):
        dims = [{"id": d, "minY": m, "height": h} for d, (_, m, h) in DIMS.items()]
        return {"mod": "mcbridge-save", "modVersion": "1.0.0",
                "mcVersion": self.world.mc_version, "encodings": [],
                "modes": ["raw"], "dims": dims, "maxQuads": 0}

    def blocks(self):
        return B.blocks_json()

    def chunk(self, dim, cx, cz, ymin, ymax):
        return self.world.chunk_payload(dim, cx, cz, ymin, ymax)

    def mesh(self, dim, cx, cz, ymin, ymax, lod=0, ao=None, leaves=None):
        """本地网格（与模拟服务器同路径）；LOD2 走高度壳。

        网格产物 LRU 缓存：LOD 升降/相机回看时免重复 40ms+ 贪心合并。"""
        dim = norm_dim(dim)
        with_ao = True if ao is None else bool(ao)
        key = (dim, cx, cz, ymin, ymax, lod, with_ao, leaves)
        with self.world.lock:
            hit = self._mesh_cache.get(key)
            if hit is not None:
                self._mesh_cache.move_to_end(key)
                return hit
        payloads = {}
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                payloads[(dx, dz)] = self.world.chunk_payload(
                    dim, cx + dx, cz + dz, ymin, ymax)
        if lod == 2:
            quads, pal, _ = mesher.shell_payload(payloads)
        else:
            cls, gid, H, pal = mesher.assemble_padded(payloads)
            quads = mesher.mesh_padded(cls, gid, with_ao=with_ao,
                                       leaves_fast=(leaves == "fast"))
        nq = len(quads)
        verts = np.array([q[0] for q in quads], np.int16) if nq else np.zeros((0, 4, 3), np.int16)
        dirs = np.array([q[1] for q in quads], np.uint8)
        blocks_ = np.array([q[2] for q in quads], np.uint16)
        aos = np.array([q[3] for q in quads], np.uint8).reshape(-1, 4)
        out = {"verts": verts, "dirs": dirs, "blocks": blocks_, "aos": aos,
               "palette": [(c, n) for c, n in pal],
               "yBottom": payloads[(0, 0)]["yBottom"]}
        with self.world.lock:
            self._mesh_cache[key] = out
            self._mesh_cache.move_to_end(key)
            while len(self._mesh_cache) > 2048:
                self._mesh_cache.popitem(last=False)
        return out

    def versions(self, dim, cx0, cz0, cx1, cz1):
        # 存档为静态数据：版本恒 0，不触发重载
        return {(cx, cz): 0 for cx in range(cx0, cx1 + 1) for cz in range(cz0, cz1 + 1)}

    def texture_png(self, block, face):
        return procedural_png(block, face)

    def player(self):
        return {"player": None}

    def set_player(self, *a, **kw):
        return {"ok": True, "note": "存档模式无玩家"}

    def setblock(self, *a, **kw):
        return {"ok": False, "error": "存档模式只读"}
