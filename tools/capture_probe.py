# -*- coding: utf-8 -*-
"""运行时几何捕获探针（R17）：驱动模组的 /api/capture 并汇总报表。

用法（游戏已进入世界、目标机器在视野内）：

    # 从玩家视线前方取目标点（最省事：站在机器前，准星对着它）
    python tools/capture_probe.py --look --dist 2.5 --r 3.0

    # 指定方块坐标
    python tools/capture_probe.py --x 133 --y -53 --z -397 --r 3.0

参数：
    --host/--port  模组 HTTP 地址（默认 127.0.0.1:8788）
    --look         用 /api/player 的坐标+视角算出视线前方 --dist 格处的目标点
    --dist         视线取点距离（默认 3.0 格）
    --x/--y/--z    显式目标点（方块中心会 +0.5）
    --r            目标判定半径（默认 1.6 格）
    --hand-guard   排除距相机这么近的四边形（默认 1.5 格，滤掉第一人称手/手持物）
    --ms           捕获窗口毫秒（默认 1500）
    --wait         等待结算的最长秒数（默认 5）

用法建议：站在机器前 3~4 格、准星对着它；手离目标足够远时 --hand-guard 自动生效。
"""
import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")


def get(url):
    with urllib.request.urlopen(url, timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))


def get_raw(url):
    try:
        with urllib.request.urlopen(url, timeout=6) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--look", action="store_true")
    ap.add_argument("--dist", type=float, default=3.0)
    ap.add_argument("--x", type=float)
    ap.add_argument("--y", type=float)
    ap.add_argument("--z", type=float)
    ap.add_argument("--r", type=float, default=1.6)
    ap.add_argument("--hand-guard", type=float, default=1.5)
    ap.add_argument("--ms", type=int, default=1500)
    ap.add_argument("--wait", type=float, default=5.0)
    args = ap.parse_args()

    base = "http://%s:%d" % (args.host, args.port)

    if args.look:
        p = get(base + "/api/player")
        if p.get("x") is None:
            print("世界里没有玩家：请先进入世界")
            return 2
        yaw = math.radians(float(p["yaw"]))
        pitch = math.radians(float(p["pitch"]))
        eye = (float(p["x"]), float(p["y"]) + 1.62, float(p["z"]))
        d = (-math.sin(yaw) * math.cos(pitch), -math.sin(pitch),
             math.cos(yaw) * math.cos(pitch))
        px = eye[0] + d[0] * args.dist
        py = eye[1] + d[1] * args.dist
        pz = eye[2] + d[2] * args.dist
        # 吸附到方块中心：机器部件都按"方块 0..16 像素"建，中心对不准会漏
        tx = math.floor(px) + 0.5
        ty = math.floor(py) + 0.5
        tz = math.floor(pz) + 0.5
        print("玩家 %.2f, %.2f, %.2f  yaw=%.1f pitch=%.1f"
              % (p["x"], p["y"], p["z"], p["yaw"], p["pitch"]))
        print("视线 %.1f 格 -> %.2f, %.2f, %.2f；吸附方块中心 -> %.2f, %.2f, %.2f"
              % (args.dist, px, py, pz, tx, ty, tz))
    else:
        if args.x is None or args.y is None or args.z is None:
            print("需要 --look 或 --x/--y/--z")
            return 2
        tx, ty, tz = args.x + 0.5, args.y + 0.5, args.z + 0.5
        print("目标点（方块中心）%.2f, %.2f, %.2f" % (tx, ty, tz))

    url = "%s/api/capture?x=%.3f&y=%.3f&z=%.3f&r=%.2f&ms=%d" % (
        base, tx, ty, tz, args.r, args.ms)
    print("arm:", get(url))

    time.sleep(args.ms / 1000.0 + 0.6)
    deadline = time.time() + args.wait
    rep = None
    while time.time() < deadline:
        code, body = get_raw(base + "/api/capture/result")
        if code == 200:
            rep = json.loads(body)
            break
        time.sleep(0.3)
    if rep is None:
        print("超时：没等到报表（窗口内是否在渲染？）")
        return 3

    cam = rep["camera"]
    types = rep.get("types", [])
    print("\n== 逐 RenderType（坐标为相机相对；世界 = 相机 + 相对）==")
    print("%-40s %8s %8s %8s %s" % ("renderType", "四边形", "命中盒", "含手", "世界 x 范围"))
    total_q = 0
    total_h = 0
    total_hand = 0
    interesting = []
    for t in types:
        qs = t.get("quadsInBox", [])
        n = t["quads"]
        total_q += n
        # 手部过滤：四边形四顶点都距相机 1.5 格内 -> 判为第一人称手/手持物
        keep = []
        hand = 0
        for q in qs:
            verts = q[:4]                       # 第 5 个元素是面朝向
            cxd = sum(v[0] for v in verts) / 4.0 + cam[0]
            cyd = sum(v[1] for v in verts) / 4.0 + cam[1]
            czd = sum(v[2] for v in verts) / 4.0 + cam[2]
            dist = math.dist((cxd, cyd, czd), tuple(cam))
            if dist <= args.hand_guard:
                hand += 1
            else:
                keep.append(q)
        total_h += len(keep)
        total_hand += hand
        wx = [v[0] + cam[0] for q in keep for v in q]
        rng = ("[%.1f .. %.1f]" % (min(wx), max(wx))) if wx else "-"
        print("%-40s %8d %8d %8d %s"
              % (t["renderType"].split(":")[0][9:], n, len(keep), hand, rng))
        if keep:
            interesting.append((t["renderType"].split(":")[0][9:], len(keep)))

    print("\n合计 %d 四边形；命中目标盒 %d 条（已剔出手部 %d 条）"
          % (total_q, total_h, total_hand))
    if interesting:
        print("\n>>> 目标方块处可捕获的层:")
        for name, h in sorted(interesting, key=lambda x: -x[1]):
            print("    %-36s %d" % (name, h))
        print("\n>>> 结论：以上层里就有运行期生成的几何，可导出。")
    else:
        print("\n>>> 目标盒内 0 命中：确认准星对着机器、距离 3~4 格，或放大 --r 重试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
