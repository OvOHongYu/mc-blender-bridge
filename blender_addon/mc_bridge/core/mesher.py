# -*- coding: utf-8 -*-
"""贪心网格器（模式 A：Blender/模拟器本地网格化；Java 模组按本实现移植）。

数据约定:
  - 体积数组索引 [x, y, z]，形状 (18, H+2, 18)（含 1 格 air 边框，
    由 3×3 区块邻域拼装，见 assemble_padded）。
  - 面可见性矩阵（a 为面所属方块, b 为相邻方块）:
      a 是 air        -> 不可见
      b 是 class1     -> 不可见（不透明遮挡一切）
      a 是 class1     -> 可见
      a,b 均 class2 且同方块 -> 不可见（玻璃自剔除）
      a,b 均 class3   -> 不可见（液体互剔）
      其余            -> 可见
  - 贪心合并约束 = (方向, 方块, AO签名)；AO 遮挡集合 {class1, class5}。
  - 顶点绕序: 外向 CCW；dir 编码 0..5 = +X,-X,+Y,-Y,+Z,-Z。
  - 输出坐标为区块局部坐标（x,z ∈ 0..16，y ∈ 0..H，相对 yBottom）。
"""
import numpy as np

from . import blocks as B

AO_CURVE = (0.45, 0.65, 0.85, 1.0)
DIRS = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")

# 轴 d 上，升序 (u,v) 角点顺序给出的法向: d=0 -> +X, d=1 -> -Y, d=2 -> +Z
_FLIP_POS = (False, True, False)


# ------------------------------------------------------------ 体积拼装 ----

def assemble_padded(payloads, center=(0, 0)):
    """payloads: dict[(dx,dz)] -> MCC1 payload 或 None（缺失按 air）。
    返回 (cls, gid, H, palette)。palette 为中心区块的区块级调色板，
    邻居区块的调色板按名字重映射并入（与 Java fillVolume 一致）。"""
    cp = payloads[center]
    h_secs = len(cp["sections"])
    H = h_secs * 16
    big_cls = np.zeros((18, H, 18), np.uint8)
    big_gid = np.zeros((18, H, 18), np.uint16)

    _, _, pal0, _ = build_arrays(cp)
    name2gid = {name: i for i, (c, name) in enumerate(pal0)}
    ext_pal = list(pal0)

    def put(dx, dz, payload, region):
        if payload is None:
            return
        assert len(payload["sections"]) == h_secs, "height mismatch"
        cls, gid, npal, _ = build_arrays(payload)       # (H,16,16) [y,z,x]
        if payload is not cp:
            # 邻居调色板 -> 中心（可扩展）调色板
            remap = np.zeros(len(npal), np.uint16)
            for i, (c, name) in enumerate(npal):
                if name in name2gid:
                    remap[i] = name2gid[name]
                else:
                    name2gid[name] = len(ext_pal)
                    ext_pal.append((c, name))
                    remap[i] = len(ext_pal) - 1
            gid = remap[gid]
        cx_ = np.transpose(cls, (2, 0, 1))               # [x,y,z]
        gx_ = np.transpose(gid, (2, 0, 1))
        big_cls[region[0], :, region[1]] = cx_[region[2], :, region[3]]
        big_gid[region[0], :, region[1]] = gx_[region[2], :, region[3]]

    put(0, 0, cp, (slice(1, 17), slice(1, 17), slice(0, 16), slice(0, 16)))
    put(-1, 0, payloads.get((-1, 0)), (slice(0, 1), slice(1, 17), slice(15, 16), slice(0, 16)))
    put(1, 0, payloads.get((1, 0)), (slice(17, 18), slice(1, 17), slice(0, 1), slice(0, 16)))
    put(0, -1, payloads.get((0, -1)), (slice(1, 17), slice(0, 1), slice(0, 16), slice(15, 16)))
    put(0, 1, payloads.get((0, 1)), (slice(1, 17), slice(17, 18), slice(0, 16), slice(0, 1)))
    put(-1, -1, payloads.get((-1, -1)), (slice(0, 1), slice(0, 1), slice(15, 16), slice(15, 16)))
    put(-1, 1, payloads.get((-1, 1)), (slice(0, 1), slice(17, 18), slice(15, 16), slice(0, 1)))
    put(1, -1, payloads.get((1, -1)), (slice(17, 18), slice(0, 1), slice(0, 1), slice(15, 16)))
    put(1, 1, payloads.get((1, 1)), (slice(17, 18), slice(17, 18), slice(0, 1), slice(0, 1)))

    cls = np.zeros((18, H + 2, 18), np.uint8)
    gid = np.zeros((18, H + 2, 18), np.uint16)
    cls[:, 1:H + 1, :] = big_cls
    gid[:, 1:H + 1, :] = big_gid
    return cls, gid, H, ext_pal


# ------------------------------------------------------------- 贪心合并 ----

def _rects(bm):
    """bm: 2D bool 数组（行=u）。产出 (u0, v0, w, h) 矩形覆盖，行主序扫描。"""
    n_u, n_v = bm.shape
    out = []
    while bm.any():
        rows = np.nonzero(bm.any(axis=1))[0]
        u0 = int(rows[0])
        v0 = int(np.argmax(bm[u0]))
        w = 1
        while u0 + w < n_u and bm[u0 + w, v0]:
            w += 1
        h = 1
        while v0 + h < n_v and bm[u0:u0 + w, v0 + h].all():
            h += 1
        bm[u0:u0 + w, v0:v0 + h] = False
        out.append((u0, v0, w, h))
    return out


def _corner_ao(occl):
    """occl: (planes, n_u, n_v) uint8 0/1 -> 4 个角点 AO 数组 (c0..c3)。"""
    planes, n_u, n_v = occl.shape
    Op = np.zeros((planes, n_u + 2, n_v + 2), np.uint8)
    Op[:, 1:-1, 1:-1] = occl

    def f(s1, s2, c):
        # s1/s2/c 均为 uint8 0/1：直接算术，免 >0 布尔转换拷贝
        return np.where((s1 == 1) & (s2 == 1), 0,
                        3 - (s1 + s2 + c)).astype(np.uint8)

    a0 = f(Op[:, :-2, 1:-1], Op[:, 1:-1, :-2], Op[:, :-2, :-2])   # (u-, v-)
    a1 = f(Op[:, 2:, 1:-1], Op[:, 1:-1, :-2], Op[:, 2:, :-2])     # (u+, v-)
    a2 = f(Op[:, 2:, 1:-1], Op[:, 1:-1, 2:], Op[:, 2:, 2:])       # (u+, v+)
    a3 = f(Op[:, :-2, 1:-1], Op[:, 1:-1, 2:], Op[:, :-2, 2:])     # (u-, v+)
    return [a0, a1, a2, a3]


def mesh_padded(cls, gid, with_ao=True, leaves_fast=False, cross=None):
    """返回 quads: list[(verts int16 (4,3), dir u8, block u16, ao u8×4)]。

    cross: 按 gid 的 bool 数组（True = 交叉面片植物）。
    若为 None，则所有 CUTOUT 按原样处理（旧模式）。
    """
    if leaves_fast:
        if cross is None:
            cls = np.where(cls == B.CUTOUT, B.OPAQUE, cls).astype(np.uint8)
        else:
            cross_cell = np.asarray(cross)[gid]
            cls = np.where((cls == B.CUTOUT) & ~cross_cell, B.OPAQUE,
                           cls).astype(np.uint8)
    quads = []
    for d in range(3):
        C = np.moveaxis(cls, d, 0)
        G = np.moveaxis(gid, d, 0)
        if d == 1:
            # 长轴(y)顶部全空层裁剪：起点保持、plane 索引 i 语义不变，
            # 输出与未裁剪逐位一致（上方空层不产生任何面），省 ~40% numpy 张量运算
            occ = (C != 0).any(axis=(1, 2))
            nnz = np.nonzero(occ)[0]
            if nnz.size:
                cut = int(nnz[-1]) + 2             # 含 1 层边界 + 顶部边框
                cut = max(2, min(cut, C.shape[0]))
                C, G = np.ascontiguousarray(C[:cut]), np.ascontiguousarray(G[:cut])
        Ac, Bc = C[:-1], C[1:]           # cls 已是 uint8，切片即视图
        Aid, Bid = G[:-1], G[1:]
        for positive in (True, False):
            if positive:
                mask = ((Ac != 0) & (Bc != B.OPAQUE)
                        & ~((Ac == B.TRANSPARENT) & (Bc == B.TRANSPARENT) & (Aid == Bid))
                        & ~((Ac == B.LIQUID) & (Bc == B.LIQUID)))
                ids = Aid
                if with_ao:
                    # AO 遮挡集 {OPAQUE, NONCUBE}：isin→手写布尔（快一个数量级）
                    occl = ((Bc == B.OPAQUE) | (Bc == B.NONCUBE)).astype(np.uint8)
                else:
                    occl = None
            else:
                mask = ((Bc != 0) & (Ac != B.OPAQUE)
                        & ~((Bc == B.TRANSPARENT) & (Ac == B.TRANSPARENT) & (Bid == Aid))
                        & ~((Bc == B.LIQUID) & (Ac == B.LIQUID)))
                ids = Bid
                if with_ao:
                    occl = ((Ac == B.OPAQUE) | (Ac == B.NONCUBE)).astype(np.uint8)
                else:
                    occl = None

            if with_ao:
                ao = _corner_ao(occl)
                ao4 = np.stack(ao).astype(np.uint32)   # (4,P,n,n) 一次转换
                sig = (ao4[0] | (ao4[1] << 2) | (ao4[2] << 4) | (ao4[3] << 6))
            else:
                ao = None
                sig = np.zeros(mask.shape, np.uint32)
            key = ((ids.astype(np.uint32) + 1) << 8) | sig
            key[~mask] = 0
            # 只发射"面所属格子"位于中心区块的面:
            #   +dir 面属于切片 i 的格子 -> i∈[1, n_d-2]（padded 0 是邻区块边框）
            #   -dir 面属于切片 i+1 的格子 -> i∈[0, n_d-3]（最后一格是边框）
            #   u/v 方向同理只保留 padded [1, n-2] 的格子
            n_planes, n_u, n_v = key.shape
            if positive:
                key[0, :, :] = 0
            else:
                key[n_planes - 1, :, :] = 0
            key[:, 0, :] = 0
            key[:, n_u - 1, :] = 0
            key[:, :, 0] = 0
            key[:, :, n_v - 1] = 0

            # 预筛非空层：跳过大量空 plane 的循环开销（等价于原 continue，
            # 顺序不变，省 ~30ms/区块）
            for i in np.nonzero(key.any(axis=(1, 2)))[0]:
                kp = key[i]
                for k in np.unique(kp[kp > 0]):
                    bm = (kp == k)
                    blk = (int(k) >> 8) - 1
                    for (u0, v0, w, h) in _rects(bm):
                        _emit(quads, blk, i, u0, v0, w, h, d, positive,
                               ao, i_col=None, with_ao=with_ao)

    # 交叉面片植物（CUTOUT 非树叶）：对角双面片，dir=0(+X)->side 贴图
    if cross is not None:
        cross_cell = np.asarray(cross)[gid]
        mask = (cls != 0) & cross_cell
        mask[0, :, :] = mask[-1, :, :] = False
        mask[:, 0, :] = mask[:, -1, :] = False
        mask[:, :, 0] = mask[:, :, -1] = False
        for x, y, z in zip(*np.nonzero(mask)):
            bx, by, bz = int(x) - 1, int(y) - 1, int(z) - 1
            g = int(gid[x, y, z])
            diag_a = ((bx, by, bz), (bx + 1, by, bz + 1),
                      (bx + 1, by + 1, bz + 1), (bx, by + 1, bz))
            diag_b = ((bx, by, bz + 1), (bx + 1, by, bz),
                      (bx + 1, by + 1, bz), (bx, by + 1, bz + 1))
            for base in (diag_a, diag_b):
                # 反向绕序 = 交换角点 1/3（与 Java emitCrosses 一致）
                for verts in (base, (base[0], base[3], base[2], base[1])):
                    flat = tuple(c for pt in verts for c in pt)
                    quads.append((flat, 0, g, (3, 3, 3, 3)))

    # 一次性把 (12-int tuple) 顶点转为 numpy 视图（下游接口不变）
    if quads:
        va = np.array([q[0] for q in quads], np.int16).reshape(-1, 4, 3)
        quads = [(va[i], q[1], q[2], q[3]) for i, q in enumerate(quads)]
    return quads


def _emit(quads, blk, p, u0, v0, w, h, d, positive, ao, i_col, with_ao):
    a1 = {0: 1, 1: 0, 2: 0}[d]        # 除 d 外两个轴（升序）
    a2 = {0: 2, 1: 2, 2: 1}[d]
    lu, lv = u0 - 1, v0 - 1
    corners = ((lu, lv), (lu + w, lv), (lu + w, lv + h), (lu, lv + h))
    flip = _FLIP_POS[d] if positive else (not _FLIP_POS[d])
    order = (0, 3, 2, 1) if flip else (0, 1, 2, 3)
    # 纯 tuple 累积（mesh_padded 末尾一次性转 numpy），省每 quad 的小数组开销
    v12 = [0] * 12
    for vi, ci in enumerate(order):
        cu, cv = corners[ci]
        v12[vi * 3 + d] = p
        v12[vi * 3 + a1] = cu
        v12[vi * 3 + a2] = cv
    if with_ao:
        ao4 = tuple(int(ao[j][p, u0, v0]) for j in order)
    else:
        ao4 = (3, 3, 3, 3)
    dirn = d * 2 + (0 if positive else 1)
    quads.append((tuple(v12), dirn, blk, ao4))


# ------------------------------------------------------------ LOD2 壳 ----

def shell_lod(cls, gid):
    """高度场壳网格：顶面矩形合并 + 每列裙边。cls/gid 为未加边框的
    (16,H,16) [x,y,z] 中心区块数组。"""
    H = cls.shape[1]
    nonair = cls != 0
    has = nonair.any(axis=1)                                        # (16,16) [x,z]
    rev = nonair[:, ::-1, :]                                        # y 反转
    top = np.where(has, H - 1 - np.argmax(rev, axis=1), -1)         # (16,16) [x,z]
    quads = []

    # 顶面: 合并相等 (top, gid)
    key = ((top.astype(np.int64) + 2) << 17) | (gid_top := np.where(top >= 0,
            _gather_top_gid(gid, top), 0).astype(np.int64) + 1)
    key = np.where(has, key, 0).astype(np.int64)
    for k in np.unique(key[key > 0]):
        bm = key == k
        blk = int((k & 0x1FFFF) - 1)
        t = (int(k) >> 17) - 2
        for (x0, z0, w, h) in _rects(bm):
            _emit_flat(quads, blk, t + 1, x0, z0, w, h, d=1, positive=True)

    # 裙边（每列 1×(h-hn)，不合并）
    gid_topv = (gid_top - 1).astype(np.int64)   # gid_top 含 +1 偏移，还原
    for (dx, dz) in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        d = 0 if dz == 0 else 2
        positive = (dx + dz) > 0
        for x in range(16):
            for z in range(16):
                t = int(top[x, z])
                if t < 0:
                    continue
                nx, nz = x + dx, z + dz
                hn = int(top[nx, nz]) if 0 <= nx < 16 and 0 <= nz < 16 else -1
                if t <= hn:
                    continue
                blk = int(gid_topv[x, z])
                y0w, y1w = hn + 1, t + 1          # 裙边 y 范围 [hn+1, t+1]
                if d == 0:
                    p = x + 1 if dx > 0 else x
                    u0, v0, w, hgt = y0w, z, y1w - y0w, 1
                else:
                    p = z + 1 if dz > 0 else z
                    u0, v0, w, hgt = x, y0w, 1, y1w - y0w
                _emit_flat(quads, blk, p, u0, v0, w, hgt, d=d, positive=positive)
    return quads


def _gather_top_gid(gid, top):
    """gid: (16,H,16), top: (16,16) -> 每列顶面方块的 gid。"""
    H = gid.shape[1]
    flat = gid.reshape(16, H, 16)
    yy = np.clip(top, 0, H - 1)
    return flat[np.arange(16)[:, None], yy, np.arange(16)[None, :]]


def _emit_flat(quads, blk, p, u0, v0, w, h, d, positive):
    """与 _emit 相同的角点/绕序规则，AO 恒满。"""
    a1 = {0: 1, 1: 0, 2: 0}[d]
    a2 = {0: 2, 1: 2, 2: 1}[d]
    corners = [(u0, v0), (u0 + w, v0), (u0 + w, v0 + h), (u0, v0 + h)]
    flip = _FLIP_POS[d] if positive else (not _FLIP_POS[d])
    order = (0, 3, 2, 1) if flip else (0, 1, 2, 3)
    verts = np.zeros((4, 3), np.int16)
    for vi, ci in enumerate(order):
        cu, cv = corners[ci]
        vec = [0, 0, 0]
        vec[d] = p
        vec[a1] = cu
        vec[a2] = cv
        verts[vi] = vec
    quads.append((verts, d * 2 + (0 if positive else 1), blk, (3, 3, 3, 3)))


# ------------------------------------------------------- 几何装配(BI 数据) ----

def geo_from_arrays(verts, dirs, blocks_, aos, palette):
    """(nq,4,3)i16 + dirs + blocks + aos + palette -> 导入器几何字典。"""
    from . import blocks as B
    nq = verts.shape[0]
    if nq == 0:
        return {"nq": 0, "verts": np.zeros((0, 3), np.float32),
                "uv": np.zeros((0, 2), np.float32), "vcol": np.zeros((0, 4), np.uint8),
                "mat_idx": np.zeros(0, np.uint16), "mats": [], "tris": 0}
    vf = verts.astype(np.float32)
    # UV 规则: ±X -> (z,y); ±Y -> (x,z); ±Z -> (x,y)
    d = dirs // 2
    uv = np.zeros((nq, 4, 2), np.float32)
    mx = (d == 0)
    uv[mx] = vf[mx][:, :, [2, 1]]
    my = (d == 1)
    uv[my] = vf[my][:, :, [0, 2]]
    mz = (d == 2)
    uv[mz] = vf[mz][:, :, [0, 1]]

    # vcol: tint * AO（tint 表按 palette 索引预取，向量化）
    shade = np.take(np.array(AO_CURVE, np.float32), aos)      # (nq,4)
    tints = np.ones((len(palette), 3), np.float32)
    for i, name in enumerate(palette):
        g = B.INDEX.get(name)
        if g is not None and B.TINT[g]:
            tints[i] = B.TINT[g]
    rgb = tints[blocks_][:, None, :]                          # (nq,1,3)
    vcol = np.empty((nq, 4, 4), np.uint8)
    vcol[:, :, :3] = np.clip(rgb * shade[:, :, None] * 255.0, 0, 255).astype(np.uint8)
    vcol[:, :, 3] = 255

    # 材质槽: (name, facegrp) —— 组合键向量化求 unique
    facegrp = np.where(dirs == 2, 0, np.where(dirs == 3, 1, 2)).astype(np.uint8)  # 0 top 1 bottom 2 side
    key = blocks_.astype(np.int32) * 4 + facegrp
    uniq, inv = np.unique(key, return_inverse=True)
    mats = [(palette[int(k) >> 2], ("top", "bottom", "side")[int(k) & 3]) for k in uniq]
    mat_idx = inv.astype(np.uint16)

    return {"nq": nq, "verts": vf.reshape(-1, 3), "uv": uv.reshape(-1, 2),
            "vcol": vcol.reshape(-1, 4), "mat_idx": mat_idx, "mats": mats,
            "tris": nq * 2}


def mesh_payload(payloads, center=(0, 0), with_ao=True, leaves_fast=False):
    """3×3 payload 字典 -> (quads, palette, y_bottom)。"""
    cls, gid, H, pal = assemble_padded(payloads, center)
    # 交叉面片: CUTOUT 且非树叶（与 Java 侧 BlockClassifier 规则一致）
    cross = np.zeros(len(pal), bool)
    for i, (c, name) in enumerate(pal):
        cross[i] = (c == B.CUTOUT) and ("leaves" not in name)
    quads = mesh_padded(cls, gid, with_ao=with_ao, leaves_fast=leaves_fast,
                        cross=cross)
    return quads, pal, None


def shell_payload(payloads, center=(0, 0)):
    """3×3 payload 字典 -> LOD2 壳 quads（只用中心区块 + 邻居高度边界按 air）。"""
    cp = payloads[center]
    cls, gid, pal, _ = build_arrays(cp)
    cx_ = np.transpose(cls, (2, 0, 1))
    gx_ = np.transpose(gid, (2, 0, 1))
    quads = shell_lod(cx_, gx_)
    return quads, pal, None


# 延迟导入避免循环
def build_arrays(payload):
    from .codec import build_arrays as _ba
    return _ba(payload)
