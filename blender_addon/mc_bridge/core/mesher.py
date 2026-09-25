# -*- coding: utf-8 -*-
"""贪心网格器（模式 A：Blender/模拟器本地网格化；Java 模组按本实现移植）。

数据约定:
  - 体积数组索引 [x, y, z]，形状 (16*G+2, H+2, 16*G+2)（含 1 格 air 边框，
    由 (G+2)×(G+2) 区块邻域拼装，见 assemble_padded；G = 区块组边长）。
    G=1 即单区块网格，G>1 时贪心矩形可跨区块边界合并。
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
DIR_VEC = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))

# 轴 d 上，升序 (u,v) 角点顺序给出的法向: d=0 -> +X, d=1 -> -Y, d=2 -> +Z
_FLIP_POS = (False, True, False)


# ------------------------------------------------------------ 体积拼装 ----

def assemble_padded(payloads, group=1):
    """payloads: dict[(dx,dz)] -> MCC1 payload 或 None（缺失按 air）。

    group = 区块组边长（1 = 单区块，即旧行为）。键的取值 [0, group-1] 是组本体，
    -1 与 group 各是外圈一格（邻居区块），供组边界剔除面子。返回
    (cls, gid, H, palette)，数组形状 (16*group+2, H+2, 16*group+2)；
    palette 以 (0,0) 区块的区块级调色板为基，其余按名字重映射并入
    （与 Java fillVolume 一致）。"""
    cp = payloads[(0, 0)]
    h_secs = len(cp["sections"])
    H = h_secs * 16
    n = 16 * group
    span = n + 2
    big_cls = np.zeros((span, H, span), np.uint8)
    big_gid = np.zeros((span, H, span), np.uint16)

    _, _, pal0, _ = build_arrays(cp)
    name2gid = {name: i for i, (c, name) in enumerate(pal0)}
    ext_pal = list(pal0)

    for dx in range(-1, group + 1):
        for dz in range(-1, group + 1):
            payload = payloads.get((dx, dz))
            if payload is None:
                continue
            assert len(payload["sections"]) == h_secs, "height mismatch"
            cls, gid, npal, _ = build_arrays(payload)   # (H,16,16) [y,z,x]
            if payload is not cp:
                # 邻居调色板 -> 组（可扩展）调色板
                remap = np.zeros(len(npal), np.uint16)
                for i, (c, name) in enumerate(npal):
                    if name in name2gid:
                        remap[i] = name2gid[name]
                    else:
                        name2gid[name] = len(ext_pal)
                        ext_pal.append((c, name))
                        remap[i] = len(ext_pal) - 1
                gid = remap[gid]
            # 该区块在 padded 坐标里的区间 [x0, x1) × [z0, z1)；组外圈只落进 1 格
            x0, x1 = 16 * dx + 1, 16 * dx + 17
            z0, z1 = 16 * dz + 1, 16 * dz + 17
            tx0, tx1 = max(0, x0), min(span, x1)
            tz0, tz1 = max(0, z0), min(span, z1)
            if tx0 >= tx1 or tz0 >= tz1:
                continue
            sx, sz = tx0 - x0, tz0 - z0
            sub_cls = cls[:, sz:sz + tz1 - tz0, sx:sx + tx1 - tx0]
            sub_gid = gid[:, sz:sz + tz1 - tz0, sx:sx + tx1 - tx0]
            big_cls[tx0:tx1, :, tz0:tz1] = np.transpose(sub_cls, (2, 0, 1))
            big_gid[tx0:tx1, :, tz0:tz1] = np.transpose(sub_gid, (2, 0, 1))

    cls = np.zeros((span, H + 2, span), np.uint8)
    gid = np.zeros((span, H + 2, span), np.uint16)
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


def mesh_padded(cls, gid, with_ao=True, leaves_fast=False, cross=None,
                palette=None, pack=None):
    """返回 (quads, models)。

    quads: list[(verts int16 (4,3), dir u8, block u16, ao u8×4)]（完整方块面）。
    models: list[(verts int16 (4,3), dir, tex u16, tint i8, cull u8, uv f32(4,2),
                  block u16, ao u8×4)]，顶点为 1/16 方块单位（烘焙模型几何）。

    cross: 按 gid 的 bool 数组（True = 交叉面片植物）。
    pack:  AssetPack；提供时对 class5 方块用烘焙模型替代完整方块近似。
    """
    if leaves_fast:
        if cross is None:
            cls = np.where(cls == B.CUTOUT, B.OPAQUE, cls).astype(np.uint8)
        else:
            cross_cell = np.asarray(cross)[gid]
            cls = np.where((cls == B.CUTOUT) & ~cross_cell, B.OPAQUE,
                           cls).astype(np.uint8)
    quads = []
    modeled = {}
    if pack is not None and palette is not None:
        for gi, ent in enumerate(palette):
            ecls = int(ent[0])
            if ecls == B.AIR or ecls == B.OPAQUE:
                continue        # 整立方体（含草方块等带叠加层）走贪心路径
            name = ent[1]
            if not pack.use_model(name):     # 传方块状态全名：v3 包按状态解析
                continue
            vis = pack.variant_indices(B.base_name(name), B.props_of(name))
            if vis:
                modeled[gi] = vis
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

    # 交叉面片植物（CUTOUT 非树叶）：对角两个面片，dir=0(+X)->side 贴图
    if cross is not None:
        cross_cell = np.asarray(cross)[gid]
        mask = (cls != 0) & cross_cell
        if modeled:                       # 已注入模型的方块不再叠加交叉面片
            mask &= ~np.isin(gid, np.array(sorted(modeled), np.uint16))
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
            # 每个对角面片只发射一次：Blender 默认双面渲染（材质未开背面剔除），
            # 再补一层反向绕序会与原面共面重叠 -> Z-Fighting。
            for verts in (diag_a, diag_b):
                flat = tuple(c for pt in verts for c in pt)
                quads.append((flat, 0, g, (3, 3, 3, 3)))

    # 一次性把 (12-int tuple) 顶点转为 numpy 视图（下游接口不变）
    if modeled:
        quads = [q for q in quads if q[2] not in modeled]
    if quads:
        va = np.array([q[0] for q in quads], np.int16).reshape(-1, 4, 3)
        quads = [(va[i], q[1], q[2], q[3]) for i, q in enumerate(quads)]
    models = _emit_models(cls, gid, modeled, pack) if modeled else []
    return quads, models


def _emit_models(cls, gid, modeled, pack):
    """对 modeled 调色板索引的格子发射烘焙模型几何（1/16 方块单位）。"""
    ids = np.array(sorted(modeled), np.uint16)
    mask = np.isin(gid, ids)
    mask[0, :, :] = mask[-1, :, :] = False
    mask[:, 0, :] = mask[:, -1, :] = False
    mask[:, :, 0] = mask[:, :, -1] = False
    out = []
    for x, y, z in zip(*np.nonzero(mask)):
        gi = int(gid[x, y, z])
        ox, oy, oz = (int(x) - 1) * 16, (int(y) - 1) * 16, (int(z) - 1) * 16
        for vi in modeled[gi]:
            for verts, d, tex, tint, cull, uv in pack.variants[vi]:
                if cull:
                    dv = DIR_VEC[cull - 1]
                    if cls[x + dv[0], y + dv[1], z + dv[2]] == B.OPAQUE:
                        continue
                wv = tuple((ox + v[0], oy + v[1], oz + v[2]) for v in verts)
                out.append((wv, d, tex, tint, cull, uv, gi, (3, 3, 3, 3)))
    return out


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

def geo_from_arrays(verts, dirs, blocks_, aos, palette, models=None, pack=None):
    """完整方块数组 + 可选烘焙模型 -> 导入器几何字典。

    mats 为材质描述符列表:
      ("block", 方块名, facegrp) —— 完整方块按面组取贴图
      ("tex",   texId)          —— 烘焙模型按贴图 id 取贴图
    """
    from . import blocks as B
    nq = verts.shape[0]
    nm = len(models) if models else 0
    if nq == 0 and nm == 0:
        return {"nq": 0, "verts": np.zeros((0, 3), np.float32),
                "uv": np.zeros((0, 2), np.float32), "vcol": np.zeros((0, 4), np.uint8),
                "mat_idx": np.zeros(0, np.uint16), "mats": [], "tris": 0}

    parts = []          # (verts, uv, vcol, inv, mats)
    if nq:
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

        # vcol: tint * AO。资产包声明染色面时按面组分别染色，否则沿用方块整体染色。
        shade = np.take(np.array(AO_CURVE, np.float32), aos)      # (nq,4)
        facegrp = np.where(dirs == 2, 0, np.where(dirs == 3, 1, 2)).astype(np.uint8)
        if pack is None:
            try:
                from . import assets
                pack = assets.current()
            except Exception:
                pack = None
        tints = np.ones((len(palette), 3, 3), np.float32)         # [pal][facegrp][rgb]
        for i, name in enumerate(palette):
            mask = pack.tint_mask(name) if pack is not None else None
            for fg in range(3):
                if mask is None:
                    t = B.tint_of(name)
                elif (mask >> fg) & 1:
                    t = B.default_tint(name)
                else:
                    t = None
                if t:
                    tints[i, fg] = t
        rgb = tints[blocks_, facegrp]                             # (nq,3)
        vcol = np.empty((nq, 4, 4), np.uint8)
        vcol[:, :, :3] = np.clip(rgb[:, None, :] * shade[:, :, None] * 255.0,
                                 0, 255).astype(np.uint8)
        vcol[:, :, 3] = 255

        # 材质槽: (name, facegrp) —— 组合键向量化求 unique
        key = blocks_.astype(np.int32) * 4 + facegrp
        uniq, inv = np.unique(key, return_inverse=True)
        mats = [("block", palette[int(k) >> 2], ("top", "bottom", "side")[int(k) & 3])
                for k in uniq]
        parts.append((vf.reshape(-1, 3), uv.reshape(-1, 2), vcol.reshape(-1, 4),
                      inv.astype(np.uint16), mats))

    if nm:
        mv = np.array([m[0] for m in models], np.float32).reshape(-1, 3) / 16.0
        # UV 已在烘焙期按 texture_size 归一化并翻转为 Blender 约定
        muv = np.array([m[5] for m in models], np.float32).reshape(-1, 2)
        maos = np.array([m[7] for m in models], np.uint8).reshape(-1, 4)
        mshade = np.take(np.array(AO_CURVE, np.float32), maos)
        mtex = np.array([m[2] for m in models], np.int32)
        mtint = np.array([m[3] for m in models], np.int8)
        mblk = np.array([m[6] for m in models], np.uint16)
        mcol = np.ones((nm, 3), np.float32)
        for i, ti in enumerate(mtint):
            if ti >= 0:
                mcol[i] = B.default_tint(palette[mblk[i]])
        vcol = np.empty((nm, 4, 4), np.uint8)
        vcol[:, :, :3] = np.clip(mcol[:, None, :] * mshade[:, :, None] * 255.0,
                                 0, 255).astype(np.uint8)
        vcol[:, :, 3] = 255
        uniq, inv = np.unique(mtex, return_inverse=True)
        mats = [("tex", int(t)) for t in uniq]
        parts.append((mv, muv, vcol.reshape(-1, 4), inv.astype(np.uint16), mats))

    verts_all = np.concatenate([p[0] for p in parts])
    uv_all = np.concatenate([p[1] for p in parts])
    vcol_all = np.concatenate([p[2] for p in parts])
    mats, idx_parts = [], []
    for _v, _u, _c, inv, mm in parts:
        base = len(mats)
        mats.extend(mm)
        idx_parts.append(inv + base)
    mat_idx = np.concatenate(idx_parts).astype(np.uint16)
    total = verts_all.shape[0] // 4
    return {"nq": total, "verts": verts_all, "uv": uv_all,
            "vcol": vcol_all, "mat_idx": mat_idx, "mats": mats,
            "tris": total * 2}


def entity_geo(quads):
    """实体四边形流 -> geo 片段（与 geo_from_arrays / merge_geos 同构）。

    quads: list[(verts (4,3) float32 方块单位, uv (4,2) float32 0..1, tex_id int)]；
    顶点色恒为 1（实体几何不参与 AO / 方块染色）。"""
    quads = [q for q in quads if len(q[0]) == 4]
    if not quads:
        return None
    verts = np.array([q[0] for q in quads], np.float32).reshape(-1, 3)
    uv = np.array([q[1] for q in quads], np.float32).reshape(-1, 2)
    tex = np.array([q[2] for q in quads], np.int32)
    uniq, inv = np.unique(tex, return_inverse=True)
    return {"nq": len(quads), "verts": verts, "uv": uv,
            "vcol": np.full((verts.shape[0], 4), 255, np.uint8),
            "mat_idx": inv.astype(np.uint16),
            "mats": [("tex", int(t)) for t in uniq],
            "tris": len(quads) * 2}


def merge_geos(geos, offsets):
    """把多个 geo_from_arrays 结果拼成一个（区块组 / 实体几何叠加用）。

    offsets: 与 geos 等长的 (dx, dy, dz) 平移量（方块单位，浮点）；
    材质描述符按内容去重成一个公共材质表，mat_idx 相应重映射。"""
    empty = {"nq": 0, "verts": np.zeros((0, 3), np.float32),
             "uv": np.zeros((0, 2), np.float32),
             "vcol": np.zeros((0, 4), np.uint8),
             "mat_idx": np.zeros(0, np.uint16), "mats": [], "tris": 0}
    parts, mats, idx_parts = [], [], []
    slot = {}
    for geo, off in zip(geos, offsets):
        if not geo or not geo["nq"]:
            continue
        remap = np.empty(len(geo["mats"]), np.uint16)
        for i, desc in enumerate(geo["mats"]):
            j = slot.get(desc)
            if j is None:
                j = len(mats)
                slot[desc] = j
                mats.append(desc)
            remap[i] = j
        idx_parts.append(remap[geo["mat_idx"]])
        verts = np.array(geo["verts"], np.float32, copy=True)
        if off[0] or off[1] or off[2]:
            verts += np.array(off, np.float32)
        parts.append((verts, geo["uv"], geo["vcol"]))
    if not parts:
        return dict(empty, mats=mats)
    verts = np.concatenate([p[0] for p in parts])
    uv = np.concatenate([p[1] for p in parts])
    vcol = np.concatenate([p[2] for p in parts])
    nq = verts.shape[0] // 4
    return {"nq": nq, "verts": verts, "uv": uv, "vcol": vcol,
            "mat_idx": np.concatenate(idx_parts).astype(np.uint16),
            "mats": mats, "tris": nq * 2}


# ------------------------------------------------------------ 流体 ----
# 类原版流体几何（对照 MC 1.21.1 FluidRenderer，逐指令核对 javap 反汇编）：
#   单列高度 h: 同种流体时「上方仍是同种流体 ? 1.0 : getHeight()」（getHeight = 流体 level/9，
#               水源 8/9；注意方块状态 level 与流体 level 相反，见 _fluid_height）；
#               非同种流体时「solid ? -1 : 0」，-1 权重为 0。
#   四角高度（getFluidHeight + calculateFluidHeight）:
#     ① 本列 h >= 1.0（上方是同种流体，整列满格）-> 四角直接 1.0，**不参与加权平均**；
#     ② 否则两个正交邻居任一 >= 1.0 -> 四角 1.0；
#     ③ 否则加权平均：本列必参与，两个正交邻居必参与，对角邻居仅在正交邻居任一 > 0 时
#        参与；权重 = 高度 >= 0.8 ? 10 : 1（高度 < 0 不参与）；对角块高度 >= 1.0 时直接 1.0。
# 仅本地网格路径（存档模式 / 模式 A）支持：服务端 MCM1 顶点是整型块坐标，
# 表示不了流体高度，因此默认关闭（fluids=True 才启用）。
# isSolid()（原版按碰撞箱判定，与"是否遮挡"不同）：树叶虽有 CUTOUT 渲染层但碰撞箱满格，
# 故与水面的四角平滑中视为实心（-1），否则水边会被反常地拉低。
_FLUID_SOLID = (B.OPAQUE, B.TRANSPARENT, B.NONCUBE)


def _fluid_height(name):
    """方块状态名 -> 单列流体高度（方块单位）。

    **方块状态的 level 与流体 level 相反**（MC 1.21.1 FluidBlock.statesByLevel =
    [getStill(), getFlowing(8-1), …, getFlowing(8-7), getFlowing(8, falling)]，
    且 FlowableFluid.getBlockStateLevel = 8 - getLevel()）：

      level=0 -> 静止水源（流体 level 8）-> 8/9
      level=N（1..7）-> 流体 level 8-N：1 最靠近水源最厚（7/9）… 7 最远最薄（1/9）
      level=8 -> 下落水（流体 level 8）-> 8/9

    高度 = 流体 level / 9（FlowableFluid.getHeight）。按方块 level 原样取 level/9 会把
    水面坡度整个反过来（远处反而更高），与游戏观感相反。"""
    lvl = 8
    if "[level=" in name:
        try:
            v = int(name.split("[level=", 1)[1].split("]", 1)[0].split(",")[0])
        except ValueError:
            v = 0
        lvl = 8 if (v == 0 or v >= 8) else 8 - v
    return lvl / 9.0


def _fluid_quads(cls, gid, palette):
    """液体方块的类原版几何；顶点为区块局部方块单位（可为小数）。

    cls/gid 是 assemble_padded 的填充数组（y 维 = 区块层数 + 2 层边框，
    x/z 维 = 16*组边长 + 2），因此层数 = shape[1] - 2，方块格位于 [1, E) ×
    [1, H+1) × [1, E)，E = shape[0] - 1。"""
    H = cls.shape[1] - 2
    E = cls.shape[0] - 1
    n = len(palette)
    is_liq = np.zeros(n, bool)
    is_solid = np.zeros(n, bool)
    frac = np.zeros(n, np.float32)
    base_id = np.zeros(n, np.int32)
    base_of = {}
    for i, (c, name) in enumerate(palette):
        is_liq[i] = (c == B.LIQUID)
        is_solid[i] = (c in _FLUID_SOLID) or (c == B.CUTOUT and "leaves" in name)
        frac[i] = _fluid_height(name)
        b = B.base_name(name)
        if b not in base_of:
            base_of[b] = len(base_of)
        base_id[i] = base_of[b]

    liq = is_liq[gid]
    if not liq.any():
        return []
    bid = base_id[gid]
    sol = is_solid[gid]
    above_same = np.zeros_like(liq)
    above_same[:, :-1, :] = (liq[:, 1:, :] & liq[:, :-1, :]
                             & (bid[:, 1:, :] == bid[:, :-1, :]))
    h = np.where(liq, np.where(above_same, 1.0, frac[gid]),
                 np.where(sol, -1.0, 0.0)).astype(np.float32)

    own = h[1:E, 1:H + 1, 1:E]
    liq_c = liq[1:E, 1:H + 1, 1:E]
    bid_c = bid[1:E, 1:H + 1, 1:E]

    def _nb(sx, sz):
        """邻居列高度。

        原版邻居高度 = getFluidHeight(world, **当前渲染的流体**, 邻居pos)：邻居必须是
        「与当前渲染流体同种」才按流体高度计入，否则走 solid ? -1 : 0 —— 水/岩浆相邻时
        彼此按 0 处理，不能直接借用邻居自己的列高度。"""
        sl = (slice(1 + sx, E + sx), slice(1, H + 1), slice(1 + sz, E + sz))
        return np.where(liq[sl] & (bid[sl] != bid_c), 0.0, h[sl])

    corners = {}
    for dx, dz in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        ax = _nb(dx, 0)
        bz = _nb(0, dz)
        dg = _nb(dx, dz)
        big = (ax >= 1.0) | (bz >= 1.0)
        diag_ok = (ax > 0.0) | (bz > 0.0)
        s = np.zeros_like(own)
        w = np.zeros_like(own)
        for v, inc in ((own, None), (ax, None), (bz, None), (dg, diag_ok)):
            heavy = v >= 0.8
            c = np.where(heavy, v * 10.0, np.where(v >= 0.0, v, 0.0))
            wt = np.where(heavy, 10.0, np.where(v >= 0.0, 1.0, 0.0))
            if inc is not None:
                c = np.where(inc, c, 0.0)
                wt = np.where(inc, wt, 0.0)
            s = s + c
            w = w + wt
        # ① 本列满格（上方同种流体）时原版直接返回 1.0：瀑布/多层水体的侧面必须
        #    是整格竖直面，若参与平均会收到 0.83 而出现锯齿状斜边。
        full = own >= 1.0
        corners[(dx, dz)] = np.where(full | big | (diag_ok & (dg >= 1.0)), 1.0,
                                     s / np.maximum(w, 1e-6))

    opa = (cls == B.OPAQUE)
    out = []

    def _blk(i, j, k):
        return int(gid[1 + i, 1 + j, 1 + k])

    def _corners(i, j, k):
        return (float(corners[(-1, -1)][i, j, k]), float(corners[(-1, 1)][i, j, k]),
                float(corners[(1, 1)][i, j, k]), float(corners[(1, -1)][i, j, k]))

    # 顶面：上方不是同种流体且未被不透明方块遮挡
    top = liq_c & ~above_same[1:E, 1:H + 1, 1:E] & ~opa[1:E, 2:H + 2, 1:E]
    for i, j, k in zip(*np.nonzero(top)):
        X, Y, Z = int(i), int(j), int(k)
        c00, c01, c11, c10 = _corners(i, j, k)
        out.append((((X, Y + c00, Z), (X, Y + c01, Z + 1),
                     (X + 1, Y + c11, Z + 1), (X + 1, Y + c10, Z)),
                    2, _blk(i, j, k), (3, 3, 3, 3)))

    # 底面：下方不是同种流体且未被遮挡
    below_same = np.zeros_like(liq)
    below_same[:, 1:, :] = (liq[:, :-1, :] & liq[:, 1:, :]
                            & (bid[:, :-1, :] == bid[:, 1:, :]))
    bot = liq_c & ~below_same[1:E, 1:H + 1, 1:E] & ~opa[1:E, 0:H, 1:E]
    for i, j, k in zip(*np.nonzero(bot)):
        X, Y, Z = int(i), int(j), int(k)
        out.append((((X, Y, Z), (X + 1, Y, Z), (X + 1, Y, Z + 1), (X, Y, Z + 1)),
                    3, _blk(i, j, k), (3, 3, 3, 3)))

    # 四个侧面：邻居不是同种流体且未被遮挡（顶边沿该侧两个角高度）
    for sx, sz in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        nb = (slice(1 + sx, E + sx), slice(1, H + 1), slice(1 + sz, E + sz))
        nb_same = liq[nb] & (bid[nb] == bid[1:E, 1:H + 1, 1:E])
        mask = liq_c & ~nb_same & ~opa[nb]
        for i, j, k in zip(*np.nonzero(mask)):
            X, Y, Z = int(i), int(j), int(k)
            c00, c01, c11, c10 = _corners(i, j, k)
            if sx < 0:
                v = ((X, Y, Z), (X, Y, Z + 1), (X, Y + c01, Z + 1), (X, Y + c00, Z))
                d = 1
            elif sx > 0:
                v = ((X + 1, Y, Z + 1), (X + 1, Y, Z),
                     (X + 1, Y + c10, Z), (X + 1, Y + c11, Z + 1))
                d = 0
            elif sz < 0:
                v = ((X, Y, Z), (X, Y + c00, Z), (X + 1, Y + c10, Z), (X + 1, Y, Z))
                d = 5
            else:
                v = ((X, Y, Z + 1), (X + 1, Y, Z + 1),
                     (X + 1, Y + c11, Z + 1), (X, Y + c01, Z + 1))
                d = 4
            out.append((v, d, _blk(i, j, k), (3, 3, 3, 3)))
    return out


def mesh_payload(payloads, group=1, with_ao=True, leaves_fast=False,
                 pack=None, fluids=False):
    """(group+2)×(group+2) payload 字典 -> (quads, palette, y_bottom, models)。

    group = 区块组边长：键 (dx,dz) ∈ [-1, group]，内圈 [0, group-1] 是组本体，
    外圈一格作剔除面用。group>1 时贪心矩形可跨区块边界合并（对象数按 group² 下降）。

    fluids=True 时把液体的整方块面换成类原版流体几何（见上），并剔除资产包为
    液体注入的整方块烘焙模型（use_model 只表示"模型不是简单整立方体"、与
    class 无关，水/岩浆同样会命中），否则整方块与流体几何会在同一格重叠。"""
    cls, gid, H, pal = assemble_padded(payloads, group)
    # 交叉面片: CUTOUT 且非树叶（与 Java 侧 BlockClassifier 规则一致）
    cross = np.zeros(len(pal), bool)
    for i, (c, name) in enumerate(pal):
        cross[i] = (c == B.CUTOUT) and ("leaves" not in name)
    quads, models = mesh_padded(cls, gid, with_ao=with_ao, leaves_fast=leaves_fast,
                                cross=cross, palette=pal, pack=pack)
    if fluids:
        liq_ids = {i for i, (c, _) in enumerate(pal) if c == B.LIQUID}
        if liq_ids:
            quads = [q for q in quads if int(q[2]) not in liq_ids]
            # 资产包对液体也会注入整方块烘焙模型（use_model 只看模型是否为
            # 简单整立方体，与 class 无关）——必须一并剔除，否则与流体几何重叠
            models = [m for m in models if int(m[6]) not in liq_ids]
            quads.extend(_fluid_quads(cls, gid, pal))
    return quads, pal, None, models


def shell_payload(payloads, center=(0, 0)):
    """3×3 payload 字典 -> LOD2 壳 quads（只用中心区块 + 邻居高度边界按 air）。"""
    cp = payloads[center]
    cls, gid, pal, _ = build_arrays(cp)
    cx_ = np.transpose(cls, (2, 0, 1))
    gx_ = np.transpose(gid, (2, 0, 1))
    quads = shell_lod(cx_, gx_)
    return quads, pal, None, []


# 延迟导入避免循环
def build_arrays(payload):
    from .codec import build_arrays as _ba
    return _ba(payload)
