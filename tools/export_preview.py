# -*- coding: utf-8 -*-
"""从模拟服务器取一片区域，导出 OBJ 并用 matplotlib 渲染等轴预览图。

输出:
  docs/img/preview.obj   网格（每 quad 一个 f-face, usemtl = 方块|面组）
  docs/img/preview.png   等轴预览渲染

用法: python3 tools/export_preview.py [--out docs/img]
"""
import argparse
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))
sys.path.insert(0, os.path.join(ROOT, "server_sim"))

from mc_bridge.core import blocks as B          # noqa: E402
from mc_bridge.core import mesher               # noqa: E402
from mc_bridge.core.net import ApiClient        # noqa: E402


def fetch_region(client, cx0, cz0, cx1, cz1, ymin, ymax):
    payloads = {}
    for cx in range(cx0, cx1 + 1):
        for cz in range(cz0, cz1 + 1):
            payloads[(cx - cx0, cz - cz0)] = client.chunk(
                "overworld", cx, cz, ymin, ymax)
    return payloads


def region_quads(client, cx0, cz0, cx1, cz1, ymin, ymax):
    """对区域逐区块走 3×3 邻域网格化（与插件模式 A 相同），坐标转世界。"""
    all_quads = []
    for cx in range(cx0, cx1 + 1):
        for cz in range(cz0, cz1 + 1):
            payloads = {}
            for dx in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    payloads[(dx, dz)] = client.chunk(
                        "overworld", cx + dx, cz + dz, ymin, ymax)
            quads, pal, _ = mesher.mesh_payload(payloads, with_ao=True)
            name_of = {i: n for i, (c, n) in enumerate(pal)}
            ox, oz = (cx - cx0) * 16, (cz - cz0) * 16
            for v, d, b, ao in quads:
                vv = np.asarray(v, np.int64).copy()
                vv[:, 0] += ox
                vv[:, 2] += oz
                all_quads.append((vv, int(d), name_of[b], [int(a) for a in ao],
                                  ymin))
    return all_quads


def write_obj(path, quads, uv_from_world=True):
    lines = ["# mc-blender-bridge preview", "o MCBridgePreview"]
    voff = 1
    for vv, d, name, ao, yb in quads:
        lines.append("usemtl %s|f%d" % (name, d))
        for (x, y, z) in vv:
            lines.append("v %d %d %d" % (x, y + yb, z))
        for (x, y, z) in vv:
            lines.append("vt %d %d" % (x, z))
        lines.append("f %d/%d %d/%d %d/%d %d/%d"
                     % (voff, voff, voff + 1, voff + 1,
                        voff + 2, voff + 2, voff + 3, voff + 3))
        voff += 4
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return voff - 1


def render_png(path, quads):
    """等轴投影 + 画家算法 2D 光栅化（Blender Z-up 语义: 平面 XZ, 高度 Y）。"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection

    COS, SIN = 0.866, 0.5
    items = []
    for vv, d, name, ao, yb in quads:
        gid = B.INDEX.get(name, 0)
        c = np.array(B.COLOR[gid], float)
        shade = 0.5 + 0.5 * (sum(ao) / 12.0)
        col = np.clip(c * shade, 0, 1)
        pts = []
        depth = 0.0
        for (x, y, z) in vv:
            wy = y + yb
            sx = (x - z) * COS
            sy = (x + z) * SIN - wy
            pts.append((sx, sy))
            depth += (x + z) * 1000 + wy
        items.append((depth / len(vv), np.array(pts), col))
    items.sort(key=lambda t: t[0])

    fig, ax = plt.subplots(figsize=(11, 8), dpi=110)
    pc = PolyCollection([p for _, p, _ in items],
                        facecolors=[c for _, _, c in items],
                        edgecolors="none", antialiased=False)
    ax.add_collection(pc)
    allp = np.vstack([p for _, p, _ in items])
    ax.set_xlim(allp[:, 0].min() - 4, allp[:, 0].max() + 4)
    ax.set_ylim(allp[:, 1].min() - 4, allp[:, 1].max() + 4)
    ax.set_aspect("equal")
    ax.set_axis_off()
    ax.set_title("mc-blender-bridge · isometric preview (greedy-meshed sim world)")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "img"))
    ap.add_argument("--radius", type=int, default=2, help="区块半径(5x5)")
    ap.add_argument("--port", type=int, default=8788)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    client = ApiClient("127.0.0.1", args.port)
    client.ping()
    import time
    t0 = time.time()
    quads = region_quads(client, -args.radius, -args.radius,
                         args.radius, args.radius, 48, 96)
    dt = time.time() - t0
    n_v = write_obj(os.path.join(args.out, "preview.obj"), quads)
    render_png(os.path.join(args.out, "preview.png"), quads)
    print("region %dx%d chunks -> %d quads, %d verts, %.1fs"
          % (2 * args.radius + 1, 2 * args.radius + 1, len(quads), n_v, dt))


if __name__ == "__main__":
    main()
