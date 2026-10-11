# -*- coding: utf-8 -*-
"""运行时几何批量导出（R17）：把捕获到的几何按方块归属、还原源贴图与 UV，
产出可直接喂给烘焙端的中间格式 `dist/runtime_models.json`。

用法（游戏已进入世界、机器在视野内）：

    python tools/capture_export.py --center auto --radius 8
    python tools/capture_export.py --x 125 --y -60 --z -388 --radius 8

    --center auto   以玩家位置为心扫一次；也可给 --x/--y/--z
    --radius        捕获半径（格）
    --filter        只导出该前缀的方块（默认 create:，逗号分隔可多个）
    --out           输出文件（默认 dist/runtime_models.json）

原理：
  1. arm 一次捕获，取回逐 RenderType 的四边形（**相机相对坐标 + 图集 UV**）；
  2. 世界坐标 = 相机 + 相对；按 floor 归属到方块，转成方块局部 1/16 单位；
  3. 用 /api/capture/sprites 的 sprite 表把图集 UV 折算成
     「源贴图路径 + sprite 内 0..1」，v 翻转让其与烘焙端（Blender 约定）一致；
  4. 用 /api/block 取每个方块的**状态名**，按状态聚合。
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def get_raw(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, None


class Sprites:
    """图集 sprite 表 + 网格索引（UV -> 源贴图 + 矩形）。

    UV 是 0..1 的浮点，逐条线性扫几千个 sprite 太慢；按 1/512 网格建索引，
    查一次即命中。sprite 边界可能跨格，故命中后做一次精确包含判定。
    """

    N = 512

    def __init__(self, table):
        self.list = table["sprites"]
        self.grid = defaultdict(list)
        for s in self.list:
            u0, v0, u1, v1 = s[0], s[1], s[2], s[3]
            iu0 = max(0, min(self.N - 1, int(u0 * self.N)))
            iu1 = max(0, min(self.N - 1, int(u1 * self.N)))
            iv0 = max(0, min(self.N - 1, int(v0 * self.N)))
            iv1 = max(0, min(self.N - 1, int(v1 * self.N)))
            for iu in range(iu0, iu1 + 1):
                for iv in range(iv0, iv1 + 1):
                    self.grid[(iu, iv)].append(s)

    def lookup_point(self, u, v):
        iu = max(0, min(self.N - 1, int(u * self.N)))
        iv = max(0, min(self.N - 1, int(v * self.N)))
        best = None
        # 半开区间 [u0,u1) / [v0,v1)：sprite 紧密相邻时共享边界（如 u==邻居.u0），
        # 闭区间会把边界顶点判给左邻居（实测 mixer 杆被判成相邻的红色 indicator）。
        for s in self.grid.get((iu, iv), ()):
            if s[0] <= u < s[2] and s[1] <= v < s[3]:
                if best is None or (s[2] - s[0]) * (s[3] - s[1]) < \
                        (best[2] - best[0]) * (best[3] - best[1]):
                    best = s
        if best is not None:
            return best
        # 兜底：退化为闭区间（处理恰好落在最右/最上边界的情形）
        for s in self.grid.get((iu, iv), ()):
            if s[0] - 1e-6 <= u <= s[2] + 1e-6 and s[1] - 1e-6 <= v <= s[3] + 1e-6:
                if best is None or (s[2] - s[0]) * (s[3] - s[1]) < \
                        (best[2] - best[0]) * (best[3] - best[1]):
                    best = s
        return best

    def lookup(self, verts):
        """按四边形 UV 的**质心**选 sprite（质心法比逐顶点投票鲁棒得多）。

        图集里 sprite 紧密相邻，四边形 UV 常正好压在共享边界上（顶点落在邻居边内
        一点或外一点），逐顶点会因浮点/边界而选错。用四顶点 UV 的平均值判定，落在
        哪个 sprite 内就是哪个；质心失败再退回逐顶点投票。
        """
        mu = sum(verts[i][3] for i in range(4)) / 4.0
        mv = sum(verts[i][4] for i in range(4)) / 4.0
        s = self.lookup_point(mu, mv)
        if s is not None:
            return s
        votes = {}
        for i in range(4):
            s = self.lookup_point(verts[i][3], verts[i][4])
            if s is None:
                continue
            k = tuple(s)
            votes[k] = votes.get(k, 0) + 1
        if not votes:
            return None
        return min(votes.items(), key=lambda kv: (
            -kv[1], (kv[0][2] - kv[0][0]) * (kv[0][3] - kv[0][1])))[0]


def _candidates(x, y, z):
    """四边形质心 -> 候选方块坐标（可能多个）。

    方块模型的几何按其自身原点建 0..16，**正好贴在格边界上的面**（如 +X 面在
    x=16）质心会落在 x.0 —— floor 会把它判给邻格。故当某一轴贴近整数时，把
    两个相邻格都列为候选，稍后用 /api/block 的返回结果消歧。
    """
    axes = []
    for v in (x, y, z):
        f = math.floor(v)
        lo = [f]
        if abs(v - round(v)) < 1e-3:
            lo.append(f - 1)
        axes.append(lo)
    return [(a, b, c) for a in axes[0] for b in axes[1] for c in axes[2]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--center", default="auto")
    ap.add_argument("--x", type=float)
    ap.add_argument("--y", type=float)
    ap.add_argument("--z", type=float)
    ap.add_argument("--radius", type=float, default=8.0)
    ap.add_argument("--ms", type=int, default=1200)
    ap.add_argument("--filter", default="create:")
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "runtime_models.json"))
    args = ap.parse_args()
    base = "http://%s:%d" % (args.host, args.port)

    if args.x is not None and args.y is not None and args.z is not None:
        cx, cy, cz = args.x + 0.5, args.y + 0.5, args.z + 0.5
    else:
        p = get(base + "/api/player")
        if p.get("x") is None:
            print("世界里没有玩家：请先进入世界")
            return 2
        cx, cy, cz = float(p["x"]), float(p["y"]) + 1.62, float(p["z"])
        print("以玩家为心: %.1f, %.1f, %.1f" % (cx, cy, cz))

    print("arm:", get("%s/api/capture?x=%.3f&y=%.3f&z=%.3f&r=%.1f&ms=%d"
                      % (base, cx, cy, cz, args.radius, args.ms)))
    time.sleep(args.ms / 1000.0 + 0.8)
    # 采集在客户端某一 tick 上完成（写报表 + 建 sprite 表），耗时随世界/模型规模波动。
    # 原来只轮询一次，容易在采集完成前就放弃 → 改为轮询到超时。
    deadline = time.time() + 25.0
    code, rep = get_raw(base + "/api/capture/result")
    while (code != 200 or rep is None) and time.time() < deadline:
        time.sleep(0.7)
        code, rep = get_raw(base + "/api/capture/result")
    if code != 200 or rep is None:
        print("未取到报表:", code)
        return 3

    code, table = get_raw(base + "/api/capture/sprites")
    while (code != 200 or table is None) and time.time() < deadline:
        time.sleep(0.5)
        code, table = get_raw(base + "/api/capture/sprites")
    if code != 200 or table is None:
        print("未取到 sprite 表:", code)
        return 3
    print("sprite 表: %d 项" % len(table["sprites"]))
    sprites = Sprites(table)

    cam = rep["camera"]
    prefixes = tuple(x for x in args.filter.split(",") if x)

    # ---- 1. 逐四边形：反解世界坐标与 UV，并算候选方块 ----
    entries = []                            # [(candidates, payload)]
    unresolved = 0
    hands = 0
    for t in rep.get("types", []):
        rtn = t["renderType"]
        forced = None
        if rtn.startswith("flywheel@"):
            # Flywheel 几何按模组已知方块归属（机器会伸进相邻方块，不能按质心重判）
            try:
                fx, fy, fz = (int(v) for v in rtn[len("flywheel@"):].split(","))
                forced = (fx, fy, fz)
            except ValueError:
                forced = None
        rt = rtn.split(":")[0][11:]
        for q in t.get("quadsInBox", []):
            verts = q[:4]
            d = q[4]
            if all(abs(v[3]) < 1e-6 and abs(v[4]) < 1e-6 for v in verts):
                continue                            # 退化 UV（BER 无贴图几何/阴影），弃
            wx = [v[0] + cam[0] for v in verts]     # 世界 = 相机 + 相对
            wy = [v[1] + cam[1] for v in verts]
            wz = [v[2] + cam[2] for v in verts]
            gx, gy, gz = sum(wx) / 4.0, sum(wy) / 4.0, sum(wz) / 4.0
            if math.dist((gx, gy, gz), tuple(cam)) <= 1.5:
                hands += 1
                continue                            # 第一人称手 / 手持物
            sp = sprites.lookup(verts)
            if sp is None:
                unresolved += 1
                continue
            cands = (forced,) if forced else _candidates(gx, gy, gz)
            entries.append((cands, (rt, wx, wy, wz, verts, d, sp)))

    # ---- 2. 批量取候选方块的状态名（去重后逐个查） ----
    cands = sorted({c for cs, _ in entries for c in cs})
    block_of = {}
    for c in cands:
        code, info = get_raw("%s/api/block?dim=minecraft:overworld&x=%d&y=%d&z=%d"
                             % (base, c[0], c[1], c[2]))
        block_of[c] = (info or {}).get("block")
    print("四边形 %d 条；候选方块 %d 个；手部剔除 %d；UV 未解析 %d"
          % (len(entries), len(cands), hands, unresolved))

    # ---- 3. 归属：候选里命中 filter 者胜出；多个则取离捕获目标最近的那个 ----
    target = tuple(rep.get("target") or (cx, cy, cz))
    per_block = defaultdict(list)
    for cs, (rt, wx, wy, wz, verts, d, sp) in entries:
        hit = [c for c in cs if (block_of.get(c) or "").startswith(prefixes)]
        if not hit:
            continue
        key = min(hit, key=lambda c: math.dist(
            (c[0] + .5, c[1] + .5, c[2] + .5), target))
        u0, v0, u1, v1 = sp[0], sp[1], sp[2], sp[3]
        du = (u1 - u0) or 1e-9
        dv = (v1 - v0) or 1e-9
        out_v = []
        out_uv = []
        for i in range(4):
            out_v.append(round((wx[i] - key[0]) * 16.0, 4))
            out_v.append(round((wy[i] - key[1]) * 16.0, 4))
            out_v.append(round((wz[i] - key[2]) * 16.0, 4))
            lu = (verts[i][3] - u0) / du
            lv = (verts[i][4] - v0) / dv
            # 模型 UV 可能比 sprite 矩形略大（~0.5%），采到邻居/内边距 → 多余像素/缝隙。
            # 钳到 [0,1]，避免越界采样。
            lu = 0.0 if lu < 0.0 else (1.0 if lu > 1.0 else lu)
            lv = 0.0 if lv < 0.0 else (1.0 if lv > 1.0 else lv)
            # 烘焙端约定：v 自底向上（Blender），故 1 - v
            out_uv.append(round(lu, 5))
            out_uv.append(round(1.0 - lv, 5))
        per_block[key].append({"verts": out_v, "uv": out_uv, "dir": d,
                               "tex": sp[4], "rt": rt})

    # ---- 4. 按状态聚合（去重：同一面可能被多个 pass 提交） ----
    states = {}
    for key, quads in sorted(per_block.items()):
        name = block_of.get(key) or ""
        seen = set()
        uniq = []
        for q in quads:
            sig = (tuple(q["verts"]), q["tex"], tuple(q["uv"]), q["dir"])
            if sig in seen:
                continue
            seen.add(sig)
            uniq.append(q)
        if not uniq:
            continue
        ent = states.setdefault(name, {"block": name.split("[")[0],
                                       "pos": list(key), "quads": []})
        ent["quads"].extend(uniq)
    print("导出状态 %d 个" % len(states))

    data = {"source": "runtime-capture", "camera": cam, "radius": args.radius,
            "states": states}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print("已写出:", args.out, os.path.getsize(args.out), "字节")
    for name, ent in sorted(states.items()):
        texs = sorted({q["tex"] for q in ent["quads"]})
        print("   %-56s %3d 四边形  %d 贴图" % (name, len(ent["quads"]), len(texs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
