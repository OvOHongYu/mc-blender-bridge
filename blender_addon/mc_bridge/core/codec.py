# -*- coding: utf-8 -*-
"""二进制编解码：MCC1（原始区块）、MCM1（网格）、版本表。

所有多字节整数为小端。格式细节见 docs/协议规范.md。
压缩协商由 HTTP 层负责（X-MCB-Encoding: 2 = zlib），本模块只处理未压缩载荷。

MCC1:
  b"MCC1" | ver u8 = 1|2 | dim(u16len+utf8) | cx i32 | cz i32 | yBottom i16 |
  secCount u8 | present u32(LSB-first) |
  每个存在 Section（自 yBottom 起，仅计存在的）:
    palSize u16 | 每项: class u8 + name(u16len+utf8) |
    bits u8 (0 表示单元素调色板) | bits>0 时: ceil(4096*bits/8) 字节位压缩
    --- 以下仅 v2（群系，R8）---
    bioPalSize u16 | 每项: name(u16len+utf8) |
    bioIds 64×u8（bioPalSize>0 时存在；4×4×4 采样，索引序 idx=(y<<4)|(z<<2)|x）
  单元索引顺序 idx = (y<<8)|(z<<4)|x，逐元素、元素内 LSB-first。
  v2 段使控制模式「本地网格」(模式 A) 也能按群系染色；无群系数据的 Section 写
  bioPalSize=0，整体无群系时用 v1（旧客户端可照常解析 v1）。

MCM1:
  b"MCM1" | ver u8 | dim | cx i32 | cz i32 | yBottom i16 |
  palSize u16 | 每项: class u8 + name |
  flags u8 (bit0 含AO) | quadCount u32 |
  每 quad: 12×i16 顶点(xyz×4，区块局部坐标，顶点顺序为外向 CCW) |
           dir u8 (0..5 = +X,-X,+Y,-Y,+Z,-Z) | block u16 | ao u8×4 (顶点对齐)
"""
import struct
import zlib

import numpy as np

MCC1_MAGIC = b"MCC1"
MCM1_MAGIC = b"MCM1"
_ENC_ZLIB = 2


def compress(payload: bytes) -> bytes:
    return zlib.compress(payload, 6)


def decompress(data: bytes) -> bytes:
    return zlib.decompress(data)


def _pack_str(s: str) -> bytes:
    b = s.encode("utf-8")
    return struct.pack("<H", len(b)) + b


def _read_str(buf, off):
    (n,) = struct.unpack_from("<H", buf, off)
    off += 2
    return buf[off:off + n].decode("utf-8"), off + n


# ---------------------------------------------------------------- MCC1 ----

def _pack_indices(idx: np.ndarray, bits: int) -> bytes:
    if bits == 0:
        return b""
    a = ((idx.astype(np.uint64)[:, None] >> np.arange(bits, dtype=np.uint64)) & 1).astype(np.uint8)
    return np.packbits(a.reshape(-1), bitorder="little").tobytes()


def _unpack_indices(data: bytes, bits: int, n: int) -> np.ndarray:
    if bits == 0:
        return np.zeros(n, np.uint16)
    raw = np.frombuffer(data, np.uint8)
    bits2 = np.unpackbits(raw, bitorder="little")[: n * bits].reshape(n, bits)
    w = (1 << np.arange(bits, dtype=np.uint64))
    return (bits2.astype(np.uint64) * w).sum(axis=1).astype(np.uint16)


def encode_mcc1(payload: dict) -> bytes:
    """payload: {"dim", "cx", "cz", "yBottom", "sections"}；
    sections: list，长度 secCount；每项 None（跳过）或
    {"palette": [(class, name), ...], "indices": uint16(4096,),
     "biomes": (names, ids uint8(64))   # 可选（R8 群系）；给出即写 MCC1 v2}。"""
    dim, cx, cz = payload["dim"], payload["cx"], payload["cz"]
    y_bottom, sections = payload["yBottom"], payload["sections"]
    ver = 1
    for s in sections:                        # 任一分段带群系即全文升到 v2
        if s is not None:
            b = s.get("biomes")
            if b and b[1] is not None:
                ver = 2
                break
    out = [MCC1_MAGIC, struct.pack("<B", ver), _pack_str(dim),
           struct.pack("<iih", cx, cz, y_bottom), struct.pack("<B", len(sections))]
    mask = 0
    for i, sec in enumerate(sections):
        if sec is not None:
            mask |= 1 << i
    out.append(struct.pack("<I", mask))
    for sec in sections:
        if sec is None:
            continue
        pal, idx = sec["palette"], sec["indices"]
        out.append(struct.pack("<H", len(pal)))
        for cls, name in pal:
            out.append(struct.pack("<B", cls) + _pack_str(name))
        if len(pal) == 1:
            bits = 0
        else:
            need = (len(pal) - 1).bit_length()
            bits = max(4, need)
        out.append(struct.pack("<B", bits))
        out.append(_pack_indices(idx, bits))
        if ver == 2:
            bn, bi = sec.get("biomes") or ([], None)
            if bi is None:
                out.append(struct.pack("<H", 0))
            else:
                out.append(struct.pack("<H", len(bn)))
                for nm in bn:
                    out.append(_pack_str(nm if nm else ""))
                out.append(np.asarray(bi, np.uint8).reshape(-1).tobytes())
    return b"".join(out)


def decode_mcc1(buf: bytes) -> dict:
    assert buf[:4] == MCC1_MAGIC, "bad MCC1 magic"
    (ver,) = struct.unpack_from("<B", buf, 4)
    assert ver in (1, 2), f"bad MCC1 version {ver}"
    off = 5
    dim, off = _read_str(buf, off)
    cx, cz, y_bottom, sec_count = struct.unpack_from("<iihB", buf, off)
    off += 11
    (present,) = struct.unpack_from("<I", buf, off)
    off += 4
    sections = [None] * sec_count
    for i in range(sec_count):
        if not (present >> i) & 1:
            continue
        (psz,) = struct.unpack_from("<H", buf, off)
        off += 2
        pal = []
        for _ in range(psz):
            (cls,) = struct.unpack_from("<B", buf, off)
            off += 1
            name, off = _read_str(buf, off)
            pal.append((int(cls), name))
        (bits,) = struct.unpack_from("<B", buf, off)
        off += 1
        nbits = 0 if bits == 0 else (4096 * bits + 7) // 8
        data = buf[off:off + nbits]
        off += nbits
        indices = _unpack_indices(data, bits, 4096)
        sections[i] = {"palette": pal, "indices": indices}
        if ver == 2:                          # 群系段（与存档 payload 同构）
            (bsz,) = struct.unpack_from("<H", buf, off)
            off += 2
            bnames = []
            for _ in range(bsz):
                nm, off = _read_str(buf, off)
                bnames.append(nm)
            if bsz:
                bids = np.frombuffer(buf, np.uint8, 64, off).copy()
                off += 64
                sections[i]["biomes"] = (bnames, bids)
            else:
                sections[i]["biomes"] = ([], None)
    assert off == len(buf), f"MCC1 trailing bytes: {len(buf) - off}"
    return {"dim": dim, "cx": cx, "cz": cz, "yBottom": y_bottom, "sections": sections}


def build_arrays(payload: dict):
    """MCC1 -> (cls (H,16,16) uint8 按 [y,z,x], gid (H,16,16) uint16,
    区块级调色板 list[(class, name)]，y_bottom)。

    gid 引用返回的区块级调色板（palette[0] 恒为 (0,"minecraft:air")）；
    已知方块的 class 以本地 blocks 表为准（与服务端一致），未知方块用
    Section 调色板随附的 class。
    """
    secs = payload["sections"]
    height = len(secs) * 16
    cls = np.zeros((height, 16, 16), np.uint8)
    gid = np.zeros((height, 16, 16), np.uint16)
    pal = [(0, "minecraft:air")]
    pal_index = {"minecraft:air": 0}
    from . import blocks as B
    for si, sec in enumerate(secs):
        if sec is None:
            continue
        gmap = np.zeros(len(sec["palette"]), np.uint16)
        for pi, (c, name) in enumerate(sec["palette"]):
            if name not in pal_index:
                cls_ = int(B.CLASS[B.INDEX[name]]) if name in B.INDEX else int(c)
                pal_index[name] = len(pal)
                pal.append((cls_, name))
            gmap[pi] = pal_index[name]
        y0 = si * 16
        blk = gmap[sec["indices"].astype(np.int64)].reshape(16, 16, 16)
        gid[y0:y0 + 16] = blk
        cls[y0:y0 + 16] = np.array([e[0] for e in pal], np.uint8)[blk]
    return cls, gid, pal, payload["yBottom"]


# ---------------------------------------------------------------- MCM1 ----

DIR_NAMES = ["+X", "-X", "+Y", "-Y", "+Z", "-Z"]


def encode_mcm1(dim, cx, cz, y_bottom, palette, quads, with_ao=True,
                biome_names=None, biome_ids=None):
    """quads: list[(verts int16 (4,3), dir int, block_gid, ao uint8(4,))]，
    顶点顺序必须已是外向 CCW（与 ao 对齐）。palette: list[(class, name)]。

    biome_names/biome_ids（MCM1 v2）：群系名表 + **每面**一个群系下标。服务端贪心
    合并键包含群系（与客户端一致），因此一个合并面片必然整体属于同一群系，每面一个
    id 就够（1B/面），无需下发整块群系数组。"""
    ver = 2 if biome_names is not None else 1
    out = [MCM1_MAGIC, struct.pack("<B", ver), _pack_str(dim),
           struct.pack("<iih", cx, cz, y_bottom),
           struct.pack("<H", len(palette))]
    for cls, name in palette:
        out.append(struct.pack("<B", cls) + _pack_str(name))
    flags = 1 if with_ao else 0
    out.append(struct.pack("<B", flags))
    out.append(struct.pack("<I", len(quads)))
    for verts, d, blk, ao in quads:
        rec = struct.pack("<12h", *[int(v) for v in verts.ravel()])
        rec += struct.pack("<B", int(d))
        rec += struct.pack("<H", int(blk))
        if with_ao:
            rec += struct.pack("<4B", int(ao[0]), int(ao[1]), int(ao[2]), int(ao[3]))
        out.append(rec)
    if biome_names is not None:
        # v2 追加段：群系名表 + 每面 1B 下标（面记录保持定长，便于批量解析）
        qb = list(biome_ids) if biome_ids is not None else [0] * len(quads)
        if len(qb) != len(quads):
            raise ValueError("biome_ids 长度必须等于 quads 数")
        out.append(struct.pack("<H", len(biome_names)))
        for nm in biome_names:
            out.append(_pack_str(nm))
        out.append(bytes(int(x) & 0xFF for x in qb))
    return b"".join(out)


def decode_mcm1(buf: bytes) -> dict:
    """MCM1 v1（无群系）/ v2（追加 群系名表 + 每面 1B 群系下标）。

    v2 的 "biomeNames"/"biomeIds" 供控制模式按群系调色（R8）；v1 两者为 None。"""
    assert buf[:4] == MCM1_MAGIC, "bad MCM1 magic"
    (ver,) = struct.unpack_from("<B", buf, 4)
    assert ver in (1, 2), "unsupported MCM1 version %d" % ver
    off = 5
    dim, off = _read_str(buf, off)
    cx, cz, y_bottom = struct.unpack_from("<iih", buf, off)
    off += 10
    (psz,) = struct.unpack_from("<H", buf, off)
    off += 2
    palette = []
    for _ in range(psz):
        (cls,) = struct.unpack_from("<B", buf, off)
        off += 1
        name, off = _read_str(buf, off)
        palette.append((int(cls), name))
    (flags,) = struct.unpack_from("<B", buf, off)
    off += 1
    with_ao = bool(flags & 1)
    (nq,) = struct.unpack_from("<I", buf, off)
    off += 4
    # 固定记录长度：24B 顶点 + 1B dir + 2B block + 4B ao（with_ao=否则 3B）
    rec = 31 if with_ao else 27
    end = off + nq * rec
    # 整块一次性读取，再按 31/27B 记录切片（避免逐 quad frombuffer 的 Python 循环）
    q = np.frombuffer(buf, np.uint8, nq * rec, off).reshape(nq, rec)
    verts = q[:, :24].copy().view(np.int16).reshape(nq, 4, 3)
    dirs = q[:, 24].copy()
    b8 = q[:, 25:27]
    blocks = (b8[:, 0].astype(np.uint16) | (b8[:, 1].astype(np.uint16) << 8))
    if with_ao:
        aos = q[:, 27:31].copy()
        off += nq * rec
    else:
        aos = np.full((nq, 4), 3, np.uint8)
        off += nq * rec
    if ver == 1:
        assert off == end and end == len(buf), f"MCM1 trailing bytes: {len(buf) - end}"
        bnames, bids = None, None
    else:
        (bn,) = struct.unpack_from("<H", buf, off)
        off += 2
        bnames = []
        for _ in range(bn):
            nm, off = _read_str(buf, off)
            bnames.append(nm)
        bids = np.frombuffer(buf, np.uint8, nq, off).copy()
        off += nq
        assert off == len(buf), f"MCM1 v2 trailing bytes: {len(buf) - off}"
    return {"dim": dim, "cx": cx, "cz": cz, "yBottom": y_bottom,
            "palette": palette, "verts": verts, "dirs": dirs,
            "blocks": blocks, "aos": aos, "withAo": with_ao,
            "biomeNames": bnames, "biomeIds": bids}


# ------------------------------------------------------------- versions ----

def encode_versions(items):
    """items: iterable[(cx, cz, version u64)] -> bytes"""
    out = [struct.pack("<I", len(items))]
    for cx, cz, v in items:
        out.append(struct.pack("<iiQ", cx, cz, v))
    return b"".join(out)


def decode_versions(buf: bytes):
    (n,) = struct.unpack_from("<I", buf, 0)
    off = 4
    items = {}
    for _ in range(n):
        cx, cz, v = struct.unpack_from("<iiQ", buf, off)
        off += 16
        items[(cx, cz)] = v
    assert off == len(buf)
    return items
