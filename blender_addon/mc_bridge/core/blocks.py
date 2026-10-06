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


def base_name(name):
    """去掉方块状态属性后缀: 'minecraft:oak_stairs[facing=east]' -> 'minecraft:oak_stairs'。"""
    i = name.find("[")
    return name[:i] if i >= 0 else name


def props_of(name):
    """解析属性后缀 -> dict；无属性返回 {}。"""
    i = name.find("[")
    if i < 0 or not name.endswith("]"):
        return {}
    out = {}
    for part in name[i + 1:-1].split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def block_info(name):
    gid = INDEX[base_name(name)]
    return gid, int(CLASS[gid]), TINT[gid], COLOR[gid]


def tint_of(name):
    """方块染色（无则 None）。"""
    gid = INDEX.get(base_name(name))
    return TINT[gid] if gid is not None else None


# 平原群系默认染色（0x91BD59 草 / 0x77AB2F 叶 / 0x3F76E4 水）
TINT_GRASS = (0.568, 0.741, 0.349)
TINT_FOLIAGE = (0.467, 0.671, 0.184)
TINT_WATER = (0.247, 0.463, 0.894)


def default_tint(name):
    """默认染色：共享表优先，其次按名字启发式（资产包声明该面染色时用）。"""
    base = base_name(name)
    gid = INDEX.get(base)
    if gid is not None and TINT[gid]:
        return TINT[gid]
    low = base.lower()
    if "water" in low:
        return TINT_WATER
    if ("leaves" in low or "vine" in low or "fern" in low
            or "azalea" in low or "roots" in low):
        return TINT_FOLIAGE
    if ("grass" in low or "lily" in low or "sapling" in low or "plant" in low
            or "crop" in low or "wheat" in low or "flower" in low
            or "cactus" in low or "sugar_cane" in low or "moss" in low):
        return TINT_GRASS
    return (1.0, 1.0, 1.0)


# 染色类别（R8）：与资产包 v6 群系染色的 grass/foliage/water 三个通道对应
TINT_KIND_NONE, TINT_KIND_GRASS, TINT_KIND_FOLIAGE, TINT_KIND_WATER = 0, 1, 2, 3


def tint_kind(name):
    """方块染色的类别（0 无 / 1 草 / 2 叶 / 3 水）。

    按名字判定（与 default_tint 的启发式同源）；共享表里有染色但名字未命中的
    按草处理（如 grass_block 的顶面 overlay）。具体哪些面参与染色仍由资产包的
    tint_mask 决定，本函数只回答"取哪个通道的群系颜色"。
    """
    base = base_name(name)
    low = base.lower()
    if "water" in low:
        return TINT_KIND_WATER
    if ("leaves" in low or "vine" in low or "fern" in low
            or "azalea" in low or "roots" in low):
        return TINT_KIND_FOLIAGE
    if ("grass" in low or "lily" in low or "sapling" in low or "plant" in low
            or "crop" in low or "wheat" in low or "flower" in low
            or "cactus" in low or "sugar_cane" in low or "moss" in low):
        return TINT_KIND_GRASS
    gid = INDEX.get(base)
    if gid is not None and TINT[gid]:
        return TINT_KIND_GRASS
    return TINT_KIND_NONE


def texture_name(name, face):
    """face ∈ top/side/bottom -> 贴图名（供 /api/texture 使用）。"""
    base = base_name(name)
    gid = INDEX[base]
    faces = _ENTRIES[gid][4]
    if faces is None:
        return base.split(":")[-1]
    idx = {"top": 0, "side": 1, "bottom": 2}[face]
    return faces[idx]


# ---------------------------------------------------------------- 发光（R6）----
# 原版亮度表（block id -> luminance 0..15，取自 1.21.1 客户端字节码
# Block.Settings.luminance）。状态相关的亮度（点亮的红石灯、蜡烛数等）
# 走 _LIGHT_STATEFUL 的公式；控制模式下 /api/blocks 的 "lights" 表
# （逐状态精确、含模组方块）优先于本表。
_LIGHT_STATIC = {
    # 15
    "minecraft:glowstone": 15, "minecraft:lava": 15,
    "minecraft:sea_lantern": 15, "minecraft:jack_o_lantern": 15,
    "minecraft:shroomlight": 15, "minecraft:lava_cauldron": 15,
    "minecraft:lantern": 15, "minecraft:beacon": 15, "minecraft:conduit": 15,
    "minecraft:end_gateway": 15, "minecraft:end_portal": 15,
    "minecraft:fire": 15, "minecraft:ochre_froglight": 15,
    "minecraft:verdant_froglight": 15, "minecraft:pearlescent_froglight": 15,
    # 14 / 11 / 10
    "minecraft:torch": 14, "minecraft:wall_torch": 14, "minecraft:end_rod": 14,
    "minecraft:nether_portal": 11,
    "minecraft:soul_torch": 10, "minecraft:soul_wall_torch": 10,
    "minecraft:soul_lantern": 10, "minecraft:soul_fire": 10,
    "minecraft:crying_obsidian": 10,
    # 7 / 6 / 5 / 4 / 2 / 1
    "minecraft:redstone_torch": 7, "minecraft:redstone_wall_torch": 7,
    "minecraft:glow_lichen": 7, "minecraft:enchanting_table": 7,
    "minecraft:ender_chest": 7,
    "minecraft:sculk_catalyst": 6,
    "minecraft:amethyst_cluster": 5,
    "minecraft:large_amethyst_bud": 4,
    "minecraft:medium_amethyst_bud": 2,
    "minecraft:small_amethyst_bud": 1, "minecraft:brown_mushroom": 1,
    "minecraft:red_mushroom": 1, "minecraft:dragon_egg": 1,
    "minecraft:end_portal_frame": 1, "minecraft:sculk_sensor": 1,
    "minecraft:calibrated_sculk_sensor": 1, "minecraft:sculk_shrieker": 1,
    "minecraft:magma_block": 3,
}

_LIGHT_STATEFUL = {
    # base id -> fn(props dict) -> luminance（props 为空 dict 时按默认状态）
    "minecraft:light": lambda p: int(p.get("level", 15) or 0),
    "minecraft:sea_pickle": lambda p: 3 * int(p.get("pickles", 1) or 1)
                                      + (3 if p.get("waterlogged") == "true" else 0),
    "minecraft:candle": lambda p: 3 * int(p.get("candles", 1) or 1)
                                  if p.get("lit") == "true" else 0,
    "minecraft:candle_cake": lambda p: 3 if p.get("lit") == "true" else 0,
    "minecraft:respawn_anchor": lambda p: 3 * int(p.get("charges", 0) or 0),
    "minecraft:redstone_lamp": lambda p: 15 if p.get("lit") == "true" else 0,
    "minecraft:campfire": lambda p: 15 if p.get("lit") == "true" else 0,
    "minecraft:soul_campfire": lambda p: 10 if p.get("lit") == "true" else 0,
    "minecraft:redstone_ore": lambda p: 9 if p.get("lit") == "true" else 0,
    "minecraft:deepslate_redstone_ore": lambda p: 9 if p.get("lit") == "true" else 0,
    "minecraft:cave_vines": lambda p: 14 if p.get("berries") == "true" else 0,
    "minecraft:cave_vines_plant": lambda p: 14 if p.get("berries") == "true" else 0,
}


def light_of(name, light_map=None):
    """方块状态的发光亮度（原版 luminance，0..15）。用于 Emission 强度。

    light_map: /api/blocks 下发的 "lights"（逐状态精确、含模组方块）优先；
    未命中回退内置原版表（状态相关的按 _LIGHT_STATEFUL 公式）。
    """
    if light_map:
        v = light_map.get(name)
        if v is None:
            v = light_map.get(base_name(name))
        if v is not None:
            try:
                return max(0, min(15, int(v)))
            except (TypeError, ValueError):
                pass
    base = base_name(name)
    fn = _LIGHT_STATEFUL.get(base)
    if fn is not None:
        try:
            return max(0, min(15, int(fn(props_of(name)))))
        except (TypeError, ValueError):
            return 0
    return _LIGHT_STATIC.get(base, 0)


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
