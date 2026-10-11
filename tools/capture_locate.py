# -*- coding: utf-8 -*-
"""运行时几何定位（R17）：大范围扫一次，把命中四边形按世界坐标聚簇，
用来找出「哪里有运行期生成的几何」——即机器方块的位置，无需人工报坐标。

配合 tools/capture_probe.py 使用：先用本脚本定位，再对目标做紧捕获。

    python tools/capture_locate.py --r 24 --ms 1200

输出：逐 RenderType 命中数（已剔除第一人称手）+ 命中方块聚类（世界坐标 Top 20）。
"""
import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")


def get(url, timeout=8):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_raw(url, timeout=8):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--r", type=float, default=24.0, help="以玩家为心的扫描半径")
    ap.add_argument("--ms", type=int, default=1200)
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    base = "http://%s:%d" % (args.host, args.port)
    if get(base + "/api/player").get("x") is None:
        print("世界里没有玩家：请先进入世界")
        return 2
    p = get(base + "/api/player")
    eye = (float(p["x"]), float(p["y"]) + 1.62, float(p["z"]))
    print("玩家 %.2f, %.2f, %.2f yaw=%.1f pitch=%.1f" % (
        eye[0], eye[1] - 1.62, eye[2], p["yaw"], p["pitch"]))

    print("arm:", get("%s/api/capture?x=%.2f&y=%.2f&z=%.2f&r=%.1f&ms=%d"
                      % (base, eye[0], eye[1], eye[2], args.r, args.ms)))
    time.sleep(args.ms / 1000.0 + 0.8)
    code, body = get_raw(base + "/api/capture/result")
    if code != 200:
        print("未取到报表：", code, body[:120])
        return 3
    rep = json.loads(body)
    cam = rep["camera"]

    buckets = Counter()
    per_type = Counter()
    for t in rep.get("types", []):
        nm = t["renderType"].split(":")[0][9:]
        for q in t.get("quadsInBox", []):
            verts = q[:4]                       # 第 5 个元素是面朝向
            c = tuple(sum(v[i] for v in verts) / 4.0 + cam[i] for i in range(3))
            if math.dist(c, tuple(cam)) <= 1.5:
                continue                       # 第一人称手 / 手持物
            per_type[nm] += 1
            buckets[tuple(math.floor(v) for v in c)] += 1

    print("\n== 逐层命中数（已剔手）==")
    for k, v in per_type.most_common():
        print("   %-32s %d" % (k, v))
    print("\n== 命中方块聚类（世界坐标）==")
    if not buckets:
        print("   （无：这些层在当前视野里没有几何）")
    for (x, y, z), n in buckets.most_common(args.top):
        print("   (%5d, %4d, %5d)  %5d 条   距相机 %.1f"
              % (x, y, z, n, math.dist((x + .5, y + .5, z + .5), tuple(cam))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
