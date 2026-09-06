# -*- coding: utf-8 -*-
"""方块分类表（共享数据：模拟服务器 / Blender 插件 / 一致性测试均使用）。

class 定义（与 docs/协议规范.md 一致）:
  0 AIR        空气，不发射面
  1 OPAQUE     实心不透明方块，遮挡一切
  2 TRANSPARENT 半透明自剔除（玻璃/冰）: 同类相邻面剔除
  3 LIQUID     液体（水/岩浆）: 同类相邻面剔除，材质带 alpha
  4 CUTOUT     镂空（树叶/花草）: 不遮挡任何面
  5 NONCUBE    非完整立方体（楼梯/台阶/栅栏）: 近似为完整立方体，但视觉上不遮挡

AO 遮挡判定集合: {1, 5}（实心与非完整立方体产生环境光遮蔽）
"""
import numpy as np

AIR, OPAQUE, TRANSPARENT, LIQUID, CUTOUT, NONCUBE = 0, 1, 2, 3, 4, 5
AO_OCCLUDERS = (OPAQUE, NONCUBE)

# name -> (class, tint(rgb 0..1 或 None), avg_color(rgb 0..1), texture_faces)
# texture_faces: (top, side, bottom) 的贴图名，None 表示用方块名
_ENTRIES = [
    ("minecraft:air",           AIR,         None,                     (0.0, 0.0, 0.0),        None),
    ("minecraft:stone",         OPAQUE,      None,                     (0.55, 0.55, 0.55),     None),
    ("minecraft:dirt",          OPAQUE,      None,                     (0.53, 0.36, 0.24),     None),
    ("minecraft:grass_block",   OPAQUE,      None,                     (0.57, 0.53, 0.36),     ("grass_block_top", "grass_block_side", "dirt")),
    ("minecraft:bedrock",       OPAQUE,      None,                     (0.30, 0.30, 0.30),     None),
    ("minecraft:sand",          OPAQUE,      None,                     (0.87, 0.82, 0.62),     None),
    ("minecraft:gravel",        OPAQUE,      None,                     (0.62, 0.58, 0.56),     None),
    ("minecraft:coal_ore",      OPAQUE,      None,                     (0.35, 0.35, 0.35),     None),
    ("minecraft:iron_ore",      OPAQUE,      None,                     (0.60, 0.50, 0.42),     None),
    ("minecraft:oak_log",       OPAQUE,      None,                     (0.43, 0.33, 0.18),     ("oak_log_top", "oak_log", "oak_log_top")),
    ("minecraft:oak_planks",    OPAQUE,      None,                     (0.62, 0.48, 0.28),     None),
    ("minecraft:stone_bricks",  OPAQUE,      None,                     (0.52, 0.52, 0.52),     None),
    ("minecraft:water",         LIQUID,      None,                     (0.24, 0.38, 0.90),     None),
    ("minecraft:glass",         TRANSPARENT, None,                     (0.75, 0.90, 0.95),     None),
    ("minecraft:oak_leaves",    CUTOUT,      (0.32, 0.52, 0.22),       (0.30, 0.46, 0.20),     None),
    ("minecraft:poppy",         CUTOUT,      None,                     (0.75, 0.20, 0.20),     None),
    ("minecraft:short_grass",   CUTOUT,      (0.57, 0.74, 0.35),       (0.45, 0.60, 0.28),     None),
    ("minecraft:grass",         CUTOUT,      (0.57, 0.74, 0.35),       (0.45, 0.60, 0.28),     None),
    ("minecraft:oak_stairs",    NONCUBE,     None,                     (0.62, 0.48, 0.28),     None),
]

INDEX = {}      # name -> gid
NAMES = []      # gid -> name
CLASS = np.zeros(0, np.uint8)
TINT = []
COLOR = []

def _init():
    global CLASS
    for gid, (name, cls, tint, color, faces) in enumerate(_ENTRIES):
        INDEX[name] = gid
        NAMES.append(name)
        TINT.append(tuple(tint) if tint else None)
        COLOR.append(tuple(color))
    CLASS = np.array([e[1] for e in _ENTRIES], np.uint8)

_init()

N_BLOCKS = len(NAMES)
AIR_ID = INDEX["minecraft:air"]
WATER_ID = INDEX["minecraft:water"]


def block_info(name):
    gid = INDEX[name]
    return gid, int(CLASS[gid]), TINT[gid], COLOR[gid]


def texture_name(name, face):
    """face ∈ top/side/bottom -> 贴图名（供 /api/texture 使用）。"""
    gid = INDEX[name]
    faces = _ENTRIES[gid][4]
    if faces is None:
        return name.split(":")[-1]
    idx = {"top": 0, "side": 1, "bottom": 2}[face]
    return faces[idx]


def blocks_json():
    """供 /api/blocks 返回的字典。"""
    out = {}
    for name in NAMES:
        gid = INDEX[name]
        out[name] = {
            "class": int(CLASS[gid]),
            "tint": list(TINT[gid]) if TINT[gid] else None,
            "color": list(COLOR[gid]),
        }
    return {"blocks": out, "nBlocks": N_BLOCKS}
