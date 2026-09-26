# -*- coding: utf-8 -*-
"""生成 Java 实现的一致性测试夹具。

合成一个 16×16×16 单 Section 区块（覆盖 class1/2/3/4/5 全部剔除路径），
输出:
  tests/fixtures/mcc1_sample.bin        MCC1 v1 原始字节（未压缩）
  tests/fixtures/mcc1_biome_sample.bin  MCC1 v2 原始字节（含 section 群系）
  tests/fixtures/mcm1_sample.bin        MCM1 原始字节（未压缩, lod0/AO/fancy）
  tests/fixtures/expected.json          调色板、quad 摘要（Java 逐字节比对用）

Java 侧 ConformanceTest 以相同方式重建合成区块，输出与夹具逐字节一致方可通过。
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))

from mc_bridge.core import blocks as B          # noqa: E402
from mc_bridge.core import codec, mesher        # noqa: E402

FIX_DIR = os.path.join(ROOT, "tests", "fixtures")


def build_world():
    """合成区块: (16,16,16) -> name 字典。坐标 (x,y,z)。"""
    w = {}
    # 地板: y=0..3 石头
    for x in range(16):
        for z in range(16):
            for y in range(4):
                w[(x, y, z)] = "minecraft:stone"
    # 水池: 角落 6x6, y=4 水覆盖在石头上
    for x in range(10, 16):
        for z in range(10, 16):
            w[(x, 4, z)] = "minecraft:water"
    # 玻璃墙: x=2, z=4..9, y=4..7（测 class2 自剔除）
    for z in range(4, 10):
        for y in range(4, 8):
            w[(2, y, z)] = "minecraft:glass"
    # 树叶团: (12..14, 8..10, 5..7)（测 class4 不遮挡）
    for x in range(12, 15):
        for z in range(8, 11):
            for y in range(5, 8):
                w[(x, y, z)] = "minecraft:oak_leaves"
    # 楼梯: (5,4,5) 与相邻 (6,4,5)（测 class5 近似与 AO）
    w[(5, 4, 5)] = "minecraft:oak_stairs"
    w[(6, 4, 5)] = "minecraft:oak_stairs"
    # 原木柱: (8,4..7,8)（测不同顶/侧贴图面）
    for y in range(4, 8):
        w[(8, y, 8)] = "minecraft:oak_log"
    # 交叉面片植物（测 CUTOUT 非树叶的 cross 模型）
    w[(3, 4, 3)] = "minecraft:poppy"
    w[(4, 4, 4)] = "minecraft:short_grass"
    return w


def to_payload(world):
    gid_by_name = {n: i for i, n in enumerate(B.NAMES)}
    idx = np.zeros(4096, np.uint16)
    pal = [(0, "minecraft:air")]
    name_pos = {"minecraft:air": 0}
    for (x, y, z), name in world.items():
        if name not in name_pos:
            cls = int(B.CLASS[B.INDEX[name]])
            name_pos[name] = len(pal)
            pal.append((cls, name))
        cell = (y << 8) | (z << 4) | x
        idx[cell] = name_pos[name]
    return {"dim": "overworld", "cx": 0, "cz": 0, "yBottom": 0,
            "sections": [{"palette": pal, "indices": idx}]}


def section_biomes():
    """Section 的 4×4×4 群系（供 MCC1 v2 夹具）：x4>=2 为沙漠，其余平原。

    名字表下标即该 Section 内的群系 id；Java ConformanceTest 需重建出完全相同的
    名字表与 64 个下标。"""
    names = ["minecraft:plains", "minecraft:desert"]
    ids = np.zeros((4, 4, 4), np.uint8)      # (y4, z4, x4)
    ids[:, :, 2:] = 1
    return names, ids


def main():
    os.makedirs(FIX_DIR, exist_ok=True)
    world = build_world()
    payload = to_payload(world)

    mcc1 = codec.encode_mcc1(payload)
    with open(os.path.join(FIX_DIR, "mcc1_sample.bin"), "wb") as f:
        f.write(mcc1)

    # MCC1 v2（section 群系段）
    bnames, bids = section_biomes()
    payload_bio = dict(payload, sections=[dict(payload["sections"][0],
                                               biomes=(bnames, bids))])
    mcc1_bio = codec.encode_mcc1(payload_bio)
    with open(os.path.join(FIX_DIR, "mcc1_biome_sample.bin"), "wb") as f:
        f.write(mcc1_bio)
    assert mcc1_bio[4] == 2, "v2 夹具版本号应为 2"

    # 3×3 邻域（邻居为空）-> 与 Java 实现相同的输入
    payloads = {(0, 0): codec.decode_mcc1(mcc1)}
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            payloads.setdefault((dx, dz), payloads[(0, 0)] if (dx, dz) == (0, 0) else None)
    # 显式 None（空气邻居）
    for k in payloads:
        if payloads[k] is payloads[(0, 0)] and k != (0, 0):
            payloads[k] = None
    cls, gid, H, pal = mesher.assemble_padded(payloads)
    # 交叉面片标记（与 mesher.mesh_payload 规则一致）
    cross = np.zeros(len(pal), bool)
    for i, (c, name) in enumerate(pal):
        cross[i] = (c == B.CUTOUT) and ("leaves" not in name)
    quads, _ = mesher.mesh_padded(cls, gid, with_ao=True, leaves_fast=False,
                                  cross=cross)

    mcm1 = codec.encode_mcm1("overworld", 0, 0, 0, pal, quads, with_ao=True)
    with open(os.path.join(FIX_DIR, "mcm1_sample.bin"), "wb") as f:
        f.write(mcm1)

    expected = {
        "dim": "overworld", "cx": 0, "cz": 0, "yBottom": 0,
        "palette": [[int(c), n] for c, n in pal],
        "quadCount": len(quads),
        "quads": [
            {"verts": np.asarray(v).astype(int).tolist(),
             "dir": int(d), "block": int(b), "ao": [int(a) for a in ao]}
            for v, d, b, ao in quads
        ],
    }
    with open(os.path.join(FIX_DIR, "expected.json"), "w", encoding="utf-8") as f:
        json.dump(expected, f, ensure_ascii=False, indent=1)

    # 自校验: 解码回读一致
    p2 = codec.decode_mcc1(mcc1)
    m2 = codec.decode_mcm1(mcm1)
    assert p2["sections"][0]["palette"] == payload["sections"][0]["palette"]
    assert len(m2["dirs"]) == len(quads)
    p3 = codec.decode_mcc1(mcc1_bio)
    bn2, bi2 = p3["sections"][0]["biomes"]
    assert bn2 == bnames and np.array_equal(bi2, bids.reshape(-1))
    print(f"fixtures ok: mcc1={len(mcc1)}B mcm1={len(mcm1)}B quads={len(quads)} "
          f"palette={len(pal)}")


if __name__ == "__main__":
    main()
