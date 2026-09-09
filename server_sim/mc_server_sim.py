# -*- coding: utf-8 -*-
"""MC 桥接模拟服务器：不依赖 Minecraft，实现与 Fabric 模组完全一致的 HTTP API。

用途:
  1. 在没有 MC 环境时开发/测试 Blender 插件（数据链路 100% 同构）；
  2. 作为协议参考实现（Java 模组的行为基准）。

运行:  python3 server_sim/mc_server_sim.py [--port 8788] [--seed 7]
"""
import argparse
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import numpy as np

sys.path.insert(0, "blender_addon")

from mc_bridge.core import blocks as B
from mc_bridge.core import codec, mesher

WORLD_MIN_Y = -64
WORLD_HEIGHT = 384
SEA = 63
DIM = "overworld"


def _hash2(a, b, seed=0):
    """确定性 0..1 哈希。"""
    h = (a * 374761393 + b * 668265263 + seed * 2147483647) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((h ^ (h >> 16)) & 0xFFFFFFFF) / 0xFFFFFFFF


def _smooth(t):
    return t * t * (3 - 2 * t)


def _vnoise(x, z, scale, seed):
    """平滑值噪声（标量参考实现）。"""
    fx, fz = x / scale, z / scale
    x0, z0 = int(np.floor(fx)), int(np.floor(fz))
    tx, tz = _smooth(fx - x0), _smooth(fz - z0)
    c00 = _hash2(x0, z0, seed)
    c10 = _hash2(x0 + 1, z0, seed)
    c01 = _hash2(x0, z0 + 1, seed)
    c11 = _hash2(x0 + 1, z0 + 1, seed)
    return (c00 * (1 - tx) + c10 * tx) * (1 - tz) + (c01 * (1 - tx) + c11 * tx) * tz


def _hash2_arr(a, b, seed=0):
    """_hash2 的数组版。"""
    a = np.asarray(a, np.int64)
    b = np.asarray(b, np.int64)
    h = (a * 374761393 + b * 668265263 + np.int64(seed) * 2147483647) & np.int64(0xFFFFFFFF)
    h = ((h ^ (h >> 13)) * 1274126177) & np.int64(0xFFFFFFFF)
    return ((h ^ (h >> 16)) & np.int64(0xFFFFFFFF)) / float(0xFFFFFFFF)


def _vnoise_arr(x, z, scale, seed):
    fx = np.asarray(x, np.float64) / scale
    fz = np.asarray(z, np.float64) / scale
    x0 = np.floor(fx).astype(np.int64)
    z0 = np.floor(fz).astype(np.int64)
    tx = _smooth(fx - x0)
    tz = _smooth(fz - z0)
    c00 = _hash2_arr(x0, z0, seed)
    c10 = _hash2_arr(x0 + 1, z0, seed)
    c01 = _hash2_arr(x0, z0 + 1, seed)
    c11 = _hash2_arr(x0 + 1, z0 + 1, seed)
    return (c00 * (1 - tx) + c10 * tx) * (1 - tz) + (c01 * (1 - tx) + c11 * tx) * tz


def _height(x, z):
    h = 66 + (_vnoise(x, z, 24, 11) - 0.5) * 26 + (_vnoise(x, z, 8, 5) - 0.5) * 8
    return int(h)


def _height_arr(x, z):
    h = 66 + (_vnoise_arr(x, z, 24, 11) - 0.5) * 26 + (_vnoise_arr(x, z, 8, 5) - 0.5) * 8
    return np.floor(h).astype(np.int64)


def _shift(mat, dx, dz):
    out = np.zeros_like(mat)
    n0, n1 = mat.shape
    src0 = slice(max(0, -dx), n0 - max(0, dx))
    dst0 = slice(max(0, dx), n0 - max(0, -dx))
    src1 = slice(max(0, -dz), n1 - max(0, dz))
    dst1 = slice(max(0, dz), n1 - max(0, -dz))
    out[dst0, dst1] = mat[src0, src1]
    return out


class World:
    """确定性世界：区块按需生成（含跨区块一致的树/地形/结构）。"""

    def __init__(self, seed=7):
        self.seed = seed
        self.chunks = {}          # (cx,cz) -> list[None | ndarray(4096) uint16 全局id]
        self.versions = {}        # (cx,cz) -> u64
        self.lock = threading.Lock()
        from collections import OrderedDict
        self._mesh_cache = OrderedDict()
        self._mesh_lock = threading.Lock()

    # ------------------------------------------------------- 生成 ----

    def _structure(self, x, y, z):
        """出生点附近的测试结构: 玻璃屋 + 楼梯（覆盖 class2/class5 场景）。"""
        if not (2 <= x <= 11 and 2 <= z <= 11 and WORLD_MIN_Y + 80 <= y <= WORLD_MIN_Y + 90):
            return None
        ly = y - (WORLD_MIN_Y + 80)          # 0..10
        if ly == 0:
            return "minecraft:oak_planks"
        if 2 <= x <= 11 and 2 <= z <= 11 and x in (2, 11) or z in (2, 11):
            pass
        on_wall = x in (2, 11) or z in (2, 11)
        if 1 <= ly <= 4:
            if on_wall:
                door = (x == 6 or x == 7) and z == 2 and ly in (1, 2)
                return None if door else "minecraft:glass"
            return "minecraft:air"
        if ly == 5 and (x in (2, 11) or z in (2, 11) or (x == 3 and z == 3) or
                        (x == 3 and z == 10) or (x == 10 and z == 3) or (x == 10 and z == 10)):
            return "minecraft:oak_planks"     # 屋顶
        if ly == 5:
            return "minecraft:air"
        if ly == 6 and x == 6 and z == 6:
            return "minecraft:oak_stairs"     # 屋顶上的楼梯(class5)
        return None

    def _chunk_cells(self, cx, cz):
        """生成区块 (cx, cz) 的 24 个 section 单元数组（全局方块 id）。
        与旧逐格实现逻辑等价（结构 > 树 > 地层），但全程 numpy 向量化。"""
        id_ = {n: B.INDEX[n] for n in B.NAMES}
        G = np.zeros((WORLD_HEIGHT, 16, 16), np.uint16)     # [y, z, x]

        # ---- 高度场（-3..18 边距, 供树冠越界; 24×24 用于优先级邻居）
        lo = np.arange(-4, 20)
        LX, LZ = np.meshgrid(lo, lo, indexing="ij")          # (24,24) [lx, lz]
        XW, ZW = cx * 16 + LX, cz * 16 + LZ
        H = _height_arr(XW, ZW)                              # (24,24)
        Hc = H[1:23, 1:23]                                   # (22,22) = -3..18

        # ---- 树判定（与 _tree_at 标量逻辑等价）
        r90 = _hash2_arr(XW, ZW, self.seed + 90)
        p91 = _hash2_arr(XW, ZW, self.seed + 91)
        h92 = _hash2_arr(XW, ZW, self.seed + 92)
        is_cand = r90 < 1 / 48.0
        yield_t = is_cand.copy()
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nc = _shift(is_cand, dx, dz)
            npr = _shift(p91, dx, dz)
            yield_t &= ~(nc & (npr < p91))
        th_arr = np.where(yield_t, 4 + np.floor(h92 * 3), 0).astype(np.int64)
        th_arr = np.where(H > SEA + 1, th_arr, 0)

        ys = np.arange(WORLD_MIN_Y, WORLD_MIN_Y + WORLD_HEIGHT)[:, None, None]
        H3 = Hc[3:19, 3:19].T[None, :, :]                    # (1,16,16) [z,x] 列高
        Hg = np.broadcast_to(H3, G.shape)                    # (384,16,16)
        yf = np.broadcast_to(ys, G.shape)                    # (384,16,16)
        Xg = np.broadcast_to((cx * 16 + np.arange(16))[None, None, :], G.shape)
        Zg = np.broadcast_to((cz * 16 + np.arange(16))[None, :, None], G.shape)

        # ---- 地层（全部 3D 掩码）
        above = yf > Hg
        G[yf < WORLD_MIN_Y + 4] = id_["minecraft:bedrock"]
        G[above & (yf <= SEA)] = id_["minecraft:water"]
        surf = yf == Hg
        beach_r = _hash2_arr(Xg, Zg, self.seed) < 0.6
        beach = surf & (Hg < SEA + 1)
        G[surf & ~beach] = id_["minecraft:grass_block"]
        G[beach & beach_r] = id_["minecraft:sand"]
        G[beach & ~beach_r] = id_["minecraft:gravel"]
        deep = (~above) & (yf < Hg - 3) & (yf >= WORLD_MIN_Y + 4)
        ore = _hash2_arr(Xg * 3 + 1, yf * 7 + Zg, self.seed + 40) < 0.015
        G[deep] = id_["minecraft:stone"]
        G[deep & ore] = id_["minecraft:coal_ore"]
        dirt = (~above) & (yf >= Hg - 3) & (yf < Hg) & (yf >= WORLD_MIN_Y + 4)
        G[dirt] = id_["minecraft:dirt"]

        # ---- 树（干 + 冠，写入本区块范围内的部分）
        # 网格 lo = -4..19（24×24）; 树对区块有影响的是本地 -3..18（树冠 ±2）
        log, leaf = id_["minecraft:oak_log"], id_["minecraft:oak_leaves"]
        for i in range(24):
            for j in range(24):
                th = int(th_arr[i, j])
                if th <= 0:
                    continue
                lx, lz = int(lo[i]), int(lo[j])
                if not (-3 <= lx <= 18 and -3 <= lz <= 18):
                    continue
                base = int(H[i, j]) + 1
                top = base + th - 1
                # 树干
                if 0 <= lx < 16 and 0 <= lz < 16:
                    for y in range(base, base + th):
                        if 0 <= y - WORLD_MIN_Y < WORLD_HEIGHT:
                            G[y - WORLD_MIN_Y, lz, lx] = log
                # 树冠 4 层（与旧逐格实现逐条对应）
                def put(y, dxs, dzs, skip=None):
                    if not (0 <= y - WORLD_MIN_Y < WORLD_HEIGHT):
                        return
                    for dx in dxs:
                        for dz in dzs:
                            px, pz = lx + dx, lz + dz
                            if not (0 <= px < 16 and 0 <= pz < 16):
                                continue
                            if skip and skip(dx, dz, px, pz):
                                continue
                            G[y - WORLD_MIN_Y, pz, px] = leaf
                put(top - 1, range(-2, 3), range(-2, 3))     # 5×5
                put(top, range(-1, 2), range(-1, 2),
                    skip=lambda dx, dz, px, pz: abs(dx) == 1 and abs(dz) == 1
                    and _hash2(px, pz, 3) < 0.5)              # 3×3 去随机角
                put(top + 1, range(-1, 2), range(-1, 2),
                    skip=lambda dx, dz, px, pz: abs(dx) == 1 and abs(dz) == 1)  # 十字
                put(top + 2, (-1, 0, 1), (-1, 0, 1),
                    skip=lambda dx, dz, px, pz: abs(dx) + abs(dz) != 1)         # 四正交

        # ---- 出生结构（结构优先级最高）
        for y in range(WORLD_MIN_Y + 80, WORLD_MIN_Y + 91):
            for x in range(2, 12):
                for z in range(2, 12):
                    nm = self._structure(x, y, z)
                    if nm is not None:
                        G[y - WORLD_MIN_Y, z, x] = id_.get(nm, 0)

        secs = []
        for si in range(WORLD_HEIGHT // 16):
            sec = G[si * 16:(si + 1) * 16]
            secs.append(sec.ravel().copy() if sec.any() else None)
        return secs

    # ------------------------------------------------------- 访问 ----
    def get_chunk(self, cx, cz):
        with self.lock:
            if (cx, cz) not in self.chunks:
                self.chunks[(cx, cz)] = self._chunk_cells(cx, cz)
                self.versions[(cx, cz)] = 1
            return self.chunks[(cx, cz)]

    def payload(self, cx, cz, ymin, ymax):
        """MCC1 payload（按 ymin/ymax 对齐 16 裁剪）。"""
        secs_all = self.get_chunk(cx, cz)
        yb = max(WORLD_MIN_Y, (ymin - WORLD_MIN_Y) // 16 * 16 + WORLD_MIN_Y)
        yt = min(WORLD_MIN_Y + WORLD_HEIGHT, (ymax - WORLD_MIN_Y + 15) // 16 * 16 + WORLD_MIN_Y)
        si0, nsec = (yb - WORLD_MIN_Y) // 16, (yt - yb) // 16
        sections = []
        for si in range(si0, si0 + nsec):
            cells = secs_all[si]
            if cells is None:
                sections.append(None)
                continue
            # 局部调色板
            uniq = np.unique(cells)
            names = [B.NAMES[i] for i in uniq]
            pal = [(int(B.CLASS[B.INDEX[n]]), n) for n in names]
            remap = np.zeros(int(uniq.max()) + 1, np.uint16)
            for k, u in enumerate(uniq):
                remap[u] = k
            sections.append({"palette": pal, "indices": remap[cells]})
        return {"dim": DIM, "cx": cx, "cz": cz, "yBottom": yb, "sections": sections}

    def setblock(self, x, y, z, name):
        if name not in B.INDEX:
            raise ValueError(f"unknown block {name}")
        cx, cz = x >> 4, z >> 4
        secs = self.get_chunk(cx, cz)
        si = (y - WORLD_MIN_Y) // 16
        if not (0 <= si < len(secs)) or secs[si] is None:
            secs[si] = np.zeros(4096, np.uint16)
        lx, lz = x & 15, z & 15
        ly = y - (WORLD_MIN_Y + si * 16)
        secs[si][(ly << 8) | (lz << 4) | lx] = B.INDEX[name]
        with self.lock:
            self.versions[(cx, cz)] = self.versions.get((cx, cz), 1) + 1
            self._mesh_cache.clear()   # 世界变化，历史网格全部失效
        return self.versions[(cx, cz)]

    def mesh(self, cx, cz, ymin, ymax, lod=0, with_ao=True, leaves_fast=False):
        """服务端网格（与客户端模式 A 走完全相同的代码路径, 保证一致性）。

        结果 LRU 缓存：LOD 切换/相机回看/多客户端重放时避免重复 40ms+ 网格化。
        缓存失效由 setblock 清空（版本变更时世界内容变了）。"""
        key = (cx, cz, ymin, ymax, lod, with_ao, leaves_fast)
        with self._mesh_lock:
            hit = self._mesh_cache.get(key)
            if hit is not None:
                self._mesh_cache.move_to_end(key)
                return hit
        payloads = {}
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                payloads[(dx, dz)] = self.payload(cx + dx, cz + dz, ymin, ymax)
        if lod == 2:
            quads, pal, _, _ = mesher.shell_payload(payloads)
            r = {"quads": quads, "pal": pal, "yBottom": payloads[(0, 0)]["yBottom"]}
        else:
            cls, gid, H, pal = mesher.assemble_padded(payloads)
            quads, _ = mesher.mesh_padded(cls, gid, with_ao=with_ao, leaves_fast=leaves_fast)
            r = {"quads": quads, "pal": pal, "yBottom": payloads[(0, 0)]["yBottom"]}
        with self._mesh_lock:
            self._mesh_cache[key] = r
            self._mesh_cache.move_to_end(key)
            while len(self._mesh_cache) > 8192:
                self._mesh_cache.popitem(last=False)
        return r


# ------------------------------------------------------------ 贴图 ----

def _tex(block, face):
    """16×16 RGBA 程序化贴图。"""
    from PIL import Image
    W = H = 16
    _, _, tint, color = B.block_info(block)
    base = np.array(color, float)
    if block == "minecraft:grass_block":
        if face == "top":
            base = np.array([0.42, 0.62, 0.28])
        elif face == "bottom":
            base = np.array([0.53, 0.36, 0.24])
        else:
            base = np.array([0.53, 0.36, 0.24])
    if block == "minecraft:oak_log":
        if face in ("top", "bottom"):
            base = np.array([0.66, 0.52, 0.34])
        else:
            base = np.array([0.41, 0.29, 0.16])
    img = np.zeros((H, W, 4), np.uint8)
    name = block.split(":")[-1]
    for y in range(H):
        for x in range(W):
            n = _hash2(x + 17, y + 31, hash(name) & 0xFFFF)
            c = np.clip(base * (0.9 + 0.2 * n), 0, 1)
            alpha = 255
            if block == "minecraft:water":
                alpha, c = 170, np.array([0.24, 0.42, 0.75])
            elif block == "minecraft:glass":
                border = x in (0, 15) or y in (0, 15)
                streak = (x + y) in (7, 8, 9) and x > 3
                alpha = 255 if (border or streak) else 0
                c = np.array([0.8, 0.9, 0.95]) if alpha else c
            elif block == "minecraft:oak_leaves":
                alpha = 255 if _hash2(x, y, 77) < 0.82 else 0
                c = np.array([0.9, 0.9, 0.9])   # 灰度纹理 × 顶点色 tint
            elif block == "minecraft:oak_log" and face in ("top", "bottom"):
                r = max(abs(x - 7.5), abs(y - 7.5))
                c = c * (0.85 if int(r) % 2 == 0 else 1.0)
            elif name.endswith("planks"):
                if y % 4 == 3:
                    c = c * 0.72
            if block == "minecraft:grass_block" and face == "side" and y < 5:
                c = np.array([0.42, 0.62, 0.28]) * (0.9 + 0.2 * n)
            img[y, x, :3] = (c * 255).astype(np.uint8)
            img[y, x, 3] = alpha
    out = io.BytesIO()
    Image.fromarray(img).save(out, "PNG")
    return out.getvalue()


# ------------------------------------------------------------ HTTP ----

class Handler(BaseHTTPRequestHandler):
    world = None
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _binary(self, data, ctype="application/octet-stream", enc=2):
        if enc == 2:
            data = codec.compress(data)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("X-MCB-Encoding", str(enc) if enc else "none")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _q(self, qs):
        d = parse_qs(qs)
        return {k: v[0] for k, v in d.items()}

    def do_GET(self):
        u = urlparse(self.path)
        q = self._q(u.query)
        try:
            if u.path == "/api/ping":
                self._json({"mod": "mcbridge-sim", "modVersion": "1.0.0",
                            "mcVersion": "sim", "encodings": [2],
                            "modes": ["raw", "mesh"],
                            "dims": [{"id": DIM, "minY": WORLD_MIN_Y,
                                      "height": WORLD_HEIGHT}],
                            "maxQuads": 200000})
            elif u.path == "/api/blocks":
                self._json(B.blocks_json())
            elif u.path == "/api/chunk":
                p = self.world.payload(int(q["cx"]), int(q["cz"]),
                                       int(q.get("ymin", WORLD_MIN_Y)),
                                       int(q.get("ymax", WORLD_MIN_Y + WORLD_HEIGHT)))
                self._binary(codec.encode_mcc1(p))
            elif u.path == "/api/mesh":
                lod = int(q.get("lod", 0))
                with_ao = q.get("ao", "1" if lod == 0 else "0") != "0"
                leaves_fast = q.get("leaves", "fancy") == "fast"
                r = self.world.mesh(int(q["cx"]), int(q["cz"]),
                                    int(q.get("ymin", WORLD_MIN_Y)),
                                    int(q.get("ymax", WORLD_MIN_Y + WORLD_HEIGHT)),
                                    lod=lod, with_ao=with_ao, leaves_fast=leaves_fast)
                m = codec.encode_mcm1(DIM, int(q["cx"]), int(q["cz"]), r["yBottom"],
                                      r["pal"], r["quads"], with_ao=lod != 2)
                self._binary(m)
            elif u.path == "/api/versions":
                items = []
                for cx in range(int(q["cx0"]), int(q["cx1"]) + 1):
                    for cz in range(int(q["cz0"]), int(q["cz1"]) + 1):
                        self.world.get_chunk(cx, cz)   # 确保存在
                        items.append((cx, cz, self.world.versions[(cx, cz)]))
                self._binary(codec.encode_versions(items))
            elif u.path == "/api/texture":
                png = _tex(q["block"], q.get("face", "side"))
                self._binary(png, ctype="image/png", enc=0)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        q = self._q(u.query)
        try:
            if u.path == "/api/setblock":
                v = self.world.setblock(int(q["x"]), int(q["y"]), int(q["z"]), q["block"])
                self._json({"ok": True, "version": v})
            elif u.path == "/api/shutdown":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def serve(port=8788, seed=7, host="127.0.0.1"):
    world = World(seed)
    Handler.world = world
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[mcbridge-sim] http://{host}:{port}  seed={seed}  (Ctrl+C 退出)")
    httpd.serve_forever()
    return world


def serve_in_thread(port=8790, seed=7):
    """测试用：后台线程启动，返回 (httpd, world)。"""
    world = World(seed)
    Handler.world = world
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, world


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    serve(args.port, args.seed, args.host)
