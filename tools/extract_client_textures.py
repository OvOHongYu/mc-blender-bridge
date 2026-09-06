# -*- coding: utf-8 -*-
"""从 Minecraft 客户端 jar 解包原版方块贴图到模组 TextureService 目录。

用法: blender -b -P tools/extract_client_textures.py -- <client_jar> [out_dir]
  client_jar: 版本目录下的 <version>.jar（含 assets/minecraft/textures/block）
  out_dir:    输出目录（默认 mcbridge/textures，即服务端游戏工作目录下）

特性:
  - 每个方块输出 <block>__top/__side/__bottom.png（无专用贴图时回退）
  - 草方块侧面合成 grass_block_side_overlay 并按平原群系染色
  - 草/树叶/水按平原群系颜色染色（原版贴图是灰度，运行时染色）
  - 动画条带贴图（如 water_still）裁剪第一帧
"""
import os
import sys
import zipfile

import bpy
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
jar = argv[0]
out = argv[1] if len(argv) > 1 else "mcbridge/textures"
os.makedirs(out, exist_ok=True)

zf = zipfile.ZipFile(jar)
PREFIX = "assets/minecraft/textures/block/"
names = set(n[len(PREFIX):][:-4] for n in zf.namelist()
            if n.startswith(PREFIX) and n.endswith(".png"))

TINT_GRASS = (145 / 255, 189 / 255, 89 / 255)     # 平原草色 0x91BD59
TINT_FOLIAGE = (119 / 255, 171 / 255, 47 / 255)   # 平原叶色 0x77AB2F
TINT_WATER = (63 / 255, 118 / 255, 228 / 255)     # 平原水色 0x3F76E4

tints = {
    "grass_block_top": TINT_GRASS, "short_grass": TINT_GRASS,
    "tall_grass": TINT_GRASS, "fern": TINT_GRASS, "large_fern": TINT_GRASS,
    "bush": TINT_GRASS, "sugar_cane": TINT_GRASS, "grass": TINT_GRASS,
    "water_still": TINT_WATER, "water_overlay": TINT_WATER,
    "water_flow": TINT_WATER,
}
for n in names:
    if n.endswith("_leaves") or n == "vine":
        tints[n] = TINT_FOLIAGE

_px_cache = {}
_tmp = os.path.join(bpy.app.tempdir, "mcbx_tex.png")


def load(name):
    if name in _px_cache:
        return _px_cache[name]
    data = zf.read(PREFIX + name + ".png")
    with open(_tmp, "wb") as f:
        f.write(data)
    img = bpy.data.images.load(_tmp)
    w, h = img.size
    px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)[::-1]
    bpy.data.images.remove(img)
    if h != w:                      # 动画条带 -> 取第一帧
        px = px[:w, :w]
    _px_cache[name] = px
    return px


def save(px, path):
    h, w = px.shape[:2]
    img = bpy.data.images.new("mcbx_out", w, h, alpha=True)
    img.pixels = px[::-1].astype(np.float32).ravel().tolist()
    img.filepath_raw = path
    img.file_format = 'PNG'
    img.save()
    bpy.data.images.remove(img)


def get(name, tint=None):
    if name not in names:
        return None
    px = load(name).copy()
    t = tint or tints.get(name)
    if t is not None:
        rgb = px[:, :, :3] * np.array(t, np.float32)
        px = np.dstack([rgb, px[:, :, 3]])
    return px


def composite(base, top):
    a = top[:, :, 3:4]
    return base * (1 - a) + top * a


def first_not_none(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


count = 0
blocks = 0
for block in sorted(names):
    src = {}
    src["side"] = first_not_none(get(block + "_side"), get(block))
    src["top"] = first_not_none(get(block + "_top"), src["side"])
    src["bottom"] = first_not_none(get(block + "_bottom"),
                                   get(block + "_top"), src["side"])
    if src["side"] is None:
        continue
    if block == "grass_block" and "grass_block_side_overlay" in names:
        ov = get("grass_block_side_overlay", tint=TINT_GRASS)
        src["side"] = composite(src["side"], ov)
    blocks += 1
    for face, px in src.items():
        if px is None:
            continue
        save(px, os.path.join(out, "%s__%s.png" % (block, face)))
        count += 1

print("EXTRACTED: %d face textures for %d blocks -> %s" % (count, blocks, out),
      flush=True)
