# -*- coding: utf-8 -*-
"""区块调度器：以相机锚点为中心的迟滞加载/卸载、优先级队列、
版本轮询、LOD 分级、双预算分帧。不依赖 bpy（Blender 定时器在主线程调用）。

状态机:  MISSING -> QUEUED -> FETCHING -> READY -> LIVE
         LIVE --(超出 r_unload / LOD 变更 / 版本变更)--> QUEUED 或 删除
"""
import heapq
import math
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import Future, ThreadPoolExecutor

from . import mesher
from .codec import build_arrays, decode_mcc1

# 状态常量
QUEUED, FETCHING, READY, LIVE = "QUEUED", "FETCHING", "READY", "LIVE"


class Params:
    def __init__(self, **kw):
        self.dim = kw.get("dim", "overworld")
        self.r_load = kw.get("r_load", 8)            # 加载半径（区块，欧氏）
        self.r_unload = kw.get("r_unload", 11)       # 卸载半径（迟滞带 = r_unload - r_load）
        self.lod1_dist = kw.get("lod1_dist", 6)      # >= 此距离用 LOD1（关 AO）
        self.lod2_dist = kw.get("lod2_dist", 10)     # >= 此距离用 LOD2（壳网格）
        self.ymin = kw.get("ymin", -64)
        self.ymax = kw.get("ymax", 320)
        self.mode = kw.get("mode", "mesh")           # mesh=模式B / raw=模式A
        self.leaves_fast = kw.get("leaves_fast", False)
        self.inflight = kw.get("inflight", 3)
        self.store_cap = kw.get("store_cap", 256)
        self.version_interval = kw.get("version_interval", 5.0)
        self.max_live_tris = kw.get("max_live_tris", 60_000_000)

    def as_dict(self):
        return dict(dim=self.dim, r_load=self.r_load, r_unload=self.r_unload,
                    lod1_dist=self.lod1_dist, lod2_dist=self.lod2_dist,
                    ymin=self.ymin, ymax=self.ymax, mode=self.mode,
                    leaves_fast=self.leaves_fast, inflight=self.inflight)


class ChunkStore:
    """模式 A 的已解码区块 LRU 缓存 + 在途请求合并：
    多个工作线程网格化相邻区块时，共享的邻域 payload 只拉取一次，
    其他线程等待同一次结果（避免重复 HTTP/解码）。"""

    def __init__(self, cap=256):
        self.cap = cap
        self._d = OrderedDict()
        self._inflight = {}                # key -> Future
        self.lock = threading.Lock()

    def get_or_fetch(self, key, fetch_fn):
        """返回 payload；未缓存时调用 fetch_fn（保证每 key 只调用一次）。"""
        with self.lock:
            pl = self._d.get(key)
            if pl is not None:
                self._d.move_to_end(key)
                return pl
            fut = self._inflight.get(key)
            if fut is None:
                fut = Future()
                self._inflight[key] = fut
                owner = True
            else:
                owner = False
        if owner:
            try:
                pl = fetch_fn()
                with self.lock:
                    self._d[key] = pl
                    self._d.move_to_end(key)
                    while len(self._d) > self.cap:
                        self._d.popitem(last=False)
                fut.set_result(pl)
            except Exception as e:
                fut.set_exception(e)
                raise
            finally:
                with self.lock:
                    self._inflight.pop(key, None)
            return pl
        return fut.result()                # 等待持有线程完成

    def get(self, key):
        with self.lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
        return None

    def put(self, key, payload):
        with self.lock:
            self._d[key] = payload
            self._d.move_to_end(key)
            while len(self._d) > self.cap:
                self._d.popitem(last=False)

    def __len__(self):
        with self.lock:
            return len(self._d)


class Scheduler:
    def __init__(self, client, params=None, time_fn=None, log=None):
        self.client = client
        self.p = params or Params()
        self.time = time_fn or time.monotonic
        self.log = log or (lambda *a: None)
        self.store = ChunkStore(self.p.store_cap)
        self.state = {}                 # key -> dict
        self.ready = deque()            # 应用队列 (key, payload)
        self.evict = deque()            # 删除队列 (key, reason)
        self.queue = []                 # heapq (priority, seq, key)
        self._seq = 0
        self.inflight_keys = set()
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=self.p.inflight,
                                           thread_name_prefix="mcb")
        self.anchor = (0.0, 0.0)
        self.anchor_chunk = None
        self.frozen = False             # 预热/烘焙期间禁止卸载
        self.last_version_poll = -1e9
        self._version_polling = False
        self.stats = {"applied": 0, "evicted": 0, "errors": 0, "tris": 0,
                      "last_error": None}

    # ------------------------------------------------------------ 锚点 ----
    def update_anchor(self, x, z, frame=0):
        self.anchor = (float(x), float(z))
        return self.tick()

    def _anchor_chunk(self):
        return (int(self.anchor[0]) >> 4, int(self.anchor[1]) >> 4)

    def _dist(self, cx, cz):
        acx, acz = self._anchor_chunk()
        return math.hypot(cx - acx, cz - acz)

    def _desired_lod(self, cx, cz):
        d = self._dist(cx, cz)
        if d >= self.p.lod2_dist:
            return 2
        if d >= self.p.lod1_dist:
            return 1
        return 0

    # ------------------------------------------------------------ 主循环 ----
    def tick(self):
        """主线程定期调用：计算需求集、入队缺失、标记卸载。"""
        acx, acz = self._anchor_chunk()
        need = {}
        R = self.p.r_load
        for cx in range(acx - R - 1, acx + R + 2):
            for cz in range(acz - R - 1, acz + R + 2):
                d = math.hypot(cx - acx, cz - acz)
                if d <= R:
                    need[(cx, cz)] = d
        with self.lock:
            key_of = lambda cx, cz: (self.p.dim, cx, cz)
            # 1. 缺失/变化入队
            for (cx, cz), d in need.items():
                key = key_of(cx, cz)
                st = self.state.get(key)
                want_lod = self._desired_lod(cx, cz)
                if st is None:
                    self.state[key] = {"status": QUEUED, "lod": want_lod,
                                       "gen": 0, "last_seen": self.time(),
                                       "version": None}
                    self._push(d, key)
                elif st["status"] == LIVE and st["lod"] != want_lod:
                    st["status"] = QUEUED
                    st["lod"] = want_lod
                    st["gen"] += 1
                    self._push(d, key)
            # 2. 迟滞卸载
            if not self.frozen:
                for key, st in list(self.state.items()):
                    if st["status"] is None:
                        continue
                    cx, cz = key[1], key[2]
                    d = self._dist(cx, cz)
                    if d > self.p.r_unload:
                        del self.state[key]
                        self.evict.append((key, "out_of_range"))
            # 3. 补位（工作线程空闲且队列为空时按需重扫由下次 tick 处理）
        # 4. 派发（把队列任务交给线程池，受 inflight 限制）
        self._drain()
        return len(need)

    def _push(self, priority, key):
        self._seq += 1
        heapq.heappush(self.queue, (priority, self._seq, key))

    # ------------------------------------------------------------ 工作线程 ----
    def _drain(self):
        """主线程定期调用：把队列任务派发给线程池（受 inflight 限制）。"""
        dispatched = 0
        with self.lock:
            while self.queue and len(self.inflight_keys) < self.p.inflight:
                _, _, key = heapq.heappop(self.queue)
                st = self.state.get(key)
                if st is None or st["status"] not in (QUEUED, FETCHING):
                    continue
                if key in self.inflight_keys:
                    continue
                st["status"] = FETCHING
                self.inflight_keys.add(key)
                gen = st["gen"]
                lod = st["lod"]
                self.executor.submit(self._fetch, key, lod, gen)
                dispatched += 1
        return dispatched

    def _fetch(self, key, lod, gen):
        dim, cx, cz = key
        try:
            payload = self._fetch_geo(dim, cx, cz, lod)
            with self.lock:
                st = self.state.get(key)
                self.inflight_keys.discard(key)
                if st is None or st["gen"] != gen:
                    return  # 已过期（被重新入队或卸载）
                self.ready.append((key, {"lod": lod, "geo": payload,
                                         "yBottom": payload.get("yBottom", self.p.ymin)}))
                st["status"] = READY
        except Exception as e:
            with self.lock:
                self.inflight_keys.discard(key)
                st = self.state.get(key)
                if st is not None and st["gen"] == gen:
                    st["status"] = QUEUED     # 失败退避: 回到队列，等下次派发
                    # 必须重新压回优先队列，否则该区块永久搁浅
                    self._push(self._dist(cx, cz) + 2.0, key)
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{key}: {e}"
            self.log("fetch error", key, e)

    def _fetch_geo(self, dim, cx, cz, lod):
        if self.p.mode == "raw" and lod < 2:
            return self._fetch_geo_raw(dim, cx, cz, lod)
        m = self.client.mesh(dim, cx, cz, self.p.ymin, self.p.ymax,
                             lod=lod, ao=(lod == 0),
                             leaves=("fast" if self.p.leaves_fast else "fancy"))
        from .codec import decode_mcm1  # noqa: F401
        from .mesher import geo_from_arrays
        return geo_from_arrays(m["verts"], m["dirs"], m["blocks"], m["aos"],
                               [n for _, n in m["palette"]])

    def _fetch_geo_raw(self, dim, cx, cz, lod):
        payloads = {}
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                nkey = (dim, cx + dx, cz + dz)
                payloads[(dx, dz)] = self.store.get_or_fetch(
                    nkey, lambda k=nkey: self.client.chunk(
                        k[0], k[1], k[2], self.p.ymin, self.p.ymax))
        quads, pal, _ = mesher.mesh_payload(
            payloads, with_ao=(lod == 0), leaves_fast=self.p.leaves_fast)
        import numpy as np
        verts = np.array([q[0] for q in quads], np.int16) if quads else np.zeros((0, 4, 3), np.int16)
        dirs = np.array([q[1] for q in quads], np.uint8)
        blks = np.array([q[2] for q in quads], np.uint16)
        aos = np.array([q[3] for q in quads], np.uint8).reshape(-1, 4)
        from .mesher import geo_from_arrays
        return geo_from_arrays(verts, dirs, blks, aos, [n for _, n in pal])

    # ------------------------------------------------------------ 应用 ----
    def poll_apply(self, max_n=2):
        """主线程取回已就绪的区块（blender 定时器分帧调用）。"""
        out = []
        with self.lock:
            while self.ready and len(out) < max_n:
                key, payload = self.ready.popleft()
                st = self.state.get(key)
                if st is None:
                    continue             # 已被卸载，丢弃
                st["status"] = LIVE
                st["last_seen"] = self.time()
                out.append((key, payload))
        self.stats["applied"] += len(out)
        return out

    def poll_evict(self, max_n=8):
        out = []
        with self.lock:
            while self.evict and len(out) < max_n:
                key, reason = self.evict.popleft()
                out.append((key, reason))
        self.stats["evicted"] += len(out)
        return out

    # ------------------------------------------------------------ 版本 ----
    def maybe_poll_versions(self, force=False):
        """主线程定期调用；异步检查 LIVE 区块版本。"""
        if self._version_polling:
            return False
        t = self.time()
        if not force and (t - self.last_version_poll) < self.p.version_interval:
            return False
        self.last_version_poll = t
        with self.lock:
            live = [k for k, st in self.state.items() if st["status"] == LIVE]
        if not live:
            return False
        self._version_polling = True
        self.executor.submit(self._poll_versions, live)
        return True

    def _poll_versions(self, keys):
        try:
            cxs = [k[1] for k in keys]
            czs = [k[2] for k in keys]
            vers = self.client.versions(self.p.dim, min(cxs), min(czs),
                                        max(cxs), max(czs))
            with self.lock:
                for key in keys:
                    st = self.state.get(key)
                    if st is None or st["status"] != LIVE:
                        continue
                    v = vers.get((key[1], key[2]))
                    if st["version"] is None:
                        st["version"] = v
                    elif v is not None and v != st["version"]:
                        st["version"] = v
                        st["gen"] += 1
                        st["status"] = QUEUED
                        self._push(self._dist(key[1], key[2]), key)
        except Exception as e:
            self.log("version poll error", e)
        finally:
            self._version_polling = False

    # ------------------------------------------------------------ 预热 ----
    def prewarm(self, positions, progress=None):
        """positions: [(x, z)...]（逐帧相机位置）。冻结卸载并加载并集。"""
        keys = set()
        for (x, z) in positions:
            cx, cz = int(x) >> 4, int(z) >> 4
            R = self.p.r_load
            for a in range(cx - R, cx + R + 1):
                for b in range(cz - R, cz + R + 1):
                    if math.hypot(a - cx, b - cz) <= R:
                        keys.add((a, b))
        with self.lock:
            self.frozen = True
            for (cx, cz) in keys:
                key = (self.p.dim, cx, cz)
                if key not in self.state or self.state[key]["status"] not in (LIVE, READY, FETCHING):
                    self.state[key] = {"status": QUEUED, "lod": 0, "gen": 0,
                                       "last_seen": self.time(), "version": None}
                    self._push(0.0, key)
        return len(keys)

    def unfreeze(self):
        with self.lock:
            self.frozen = False

    # ------------------------------------------------------------ 统计 ----
    def counts(self):
        with self.lock:
            c = {QUEUED: 0, FETCHING: 0, READY: 0, LIVE: 0}
            for st in self.state.values():
                c[st["status"]] = c.get(st["status"], 0) + 1
            c["queue"] = len(self.queue)
            c["ready"] = len(self.ready)
            c["store"] = len(self.store)
            return c

    def stop(self):
        with self.lock:
            self.queue.clear()
            self.ready.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)

    # 兼容别名（定时器用）
    def drain(self):
        return self._drain()
