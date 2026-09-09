# -*- coding: utf-8 -*-
"""极简软件渲染：把 MCBA 变体画成 PNG（正交投影 + 平均色 + 方块网格），目视核对。"""
import math, sys
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, "blender_addon")
from PIL import Image, ImageDraw
from mc_bridge.core import assets as A

pack = A.AssetPack.load(sys.argv[1])
OUT = 320          # 输出边长
VIEW = 3.0         # 视野：3 个方块

def tex_avg(tid):
    (w, h), rgba = pack.texture_rgba(tid)
    n = w * h
    return (int(sum(rgba[0::4]) / n), int(sum(rgba[1::4]) / n),
            int(sum(rgba[2::4]) / n), int(sum(rgba[3::4]) / n))

_cache = {}
def avg(tid):
    if tid not in _cache:
        _cache[tid] = tex_avg(tid)
    return _cache[tid]

def render(block, props, path):
    quads = []
    for vi in pack.variant_indices(block, props):
        quads.extend(pack.variants[vi])
    if not quads:
        print("no quads", block); return
    # 视角：等轴测（x 向右下、z 向左下、y 向上）
    def proj(p):
        x, y, z = p
        u = (x - z) * 0.7071
        v = (x + z) * 0.4082 - y
        return (u, v)
    pts = [proj(v) for q in quads for v in q[0]]
    minu = min(p[0] for p in pts); maxu = max(p[0] for p in pts)
    minv = min(p[1] for p in pts); maxv = max(p[1] for p in pts)
    span = max(maxu - minu, maxv - minv, 1.0) * 1.15
    sc = OUT / span
    img = Image.new("RGB", (OUT, OUT), (28, 28, 36))
    dr = ImageDraw.Draw(img)
    def px(p):
        u, v = proj(p)
        return ((u - (minu + maxu) / 2) * sc + OUT / 2,
                (v - (minv + maxv) / 2) * sc + OUT / 2)
    # 方块网格（0..1 方块边框，投影等轴测的立方体线框）
    def cube_lines():
        for i in range(4):
            a, b = (i & 1), (i >> 1)
            for (x0, y0, z0, x1, y1, z1) in (
                (a, 0, b, a, 1, b), (0, a, b, 1, a, b), (a, b, 0, a, b, 1)):
                dr.line([px((x0, y0, z0)), px((x1, y1, z1))], fill=(90, 90, 110))
    def depth(q):
        return sum(v[0] + v[1] + v[2] for v in q[0]) / 4.0
    for q in sorted(quads, key=depth):
        verts, d, tid, tint, cull, uv = q
        col = avg(tid)
        shade = 1.0 if d == 2 else (0.78 if d in (0, 4) else 0.55)
        c = (int(col[0] * shade), int(col[1] * shade), int(col[2] * shade))
        dr.polygon([px(v) for v in verts], fill=c, outline=(15, 15, 20))
    cube_lines()
    img.save(path)
    xs = [v[0] for q in quads for v in q[0]]
    ys = [v[1] for q in quads for v in q[0]]
    zs = [v[2] for q in quads for v in q[0]]
    print("%-28s %2d quads x[%.1f..%.1f] y[%.1f..%.1f] z[%.1f..%.1f] -> %s" % (
        block, len(quads), min(xs), max(xs), min(ys), max(ys), min(zs), max(zs), path))

SPECS = [
    ("minecraft:chest", {"facing": "south", "type": "single"}, "chest"),
    ("minecraft:white_bed", {"facing": "north", "part": "head"}, "bed"),
    ("minecraft:oak_sign", {"rotation": "0"}, "sign"),
    ("minecraft:white_shulker_box", {"facing": "up"}, "shulker"),
    ("minecraft:decorated_pot", {"facing": "north"}, "pot"),
    ("minecraft:skeleton_skull", {"rotation": "0"}, "skull"),
    ("minecraft:white_banner", {"rotation": "0"}, "banner"),
    ("minecraft:bell", {"attachment": "floor", "facing": "north"}, "bell"),
    ("minecraft:conduit", {}, "conduit"),
    ("minecraft:lectern", {"facing": "north"}, "lectern"),
]
for b, p, name in SPECS:
    render(b, p, "dist/_prev_%s.png" % name)
