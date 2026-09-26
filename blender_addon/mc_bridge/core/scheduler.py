# -*- coding: utf-8 -*-
"""区块调度器：以相机锚点为中心的迟滞加载/卸载、优先级队列、
版本轮询、LOD 分级、双预算分帧。不依赖 bpy（Blender 定时器在主线程调用）。

调度与装卸的最小单位是**区块组**（Params.group = 边长 1/2/4，见 R4）：
组的键是其原区块坐标 (dim, gx, gz)，组内一次网格化产出一个 Blender 对象，
本地网格路径下贪心矩形还能跨区块边界合并。group=1 时退化为单区块调度。

状态机:  MISSING -> QUEUED -> FETCHING -> READY -> LIVE
         LIVE --(超出 r_unload / LOD 变更 / 版本变更)--> QUEUED 或删除
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
        self.use_models = kw.get("use_models", True)  # LOD0 用资产包烘焙模型
        self.biome_tint = kw.get("biome_tint", True)  # 按群系调色（R8）
        self.leaves_fast = kw.get("leaves_fast", False)
        self.group = int(kw.get("group", 2))         # 区块组边长（1/2/4，R4 跨区块合并）
        self.inflight = kw.get("inflight", 3)
        self.store_cap = kw.get("store_cap", 256)
        self.version_interval = kw.get("version_interval", 5.0)
        self.max_live_tris = kw.get("max_live_tris", 60_000_000)

    def as_dict(self):
        return dict(dim=self.dim, r_load=self.r_load, r_unload=self.r_unload,
                    lod1_dist=self.lod1_dist, lod2_dist=self.lod2_dist,
                    ymin=self.ymin, ymax=self.ymax, mode=self.mode,
                    leaves_fast=self.leaves_fast, group=self.group,
                    inflight=self.inflight)


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
        # 数据源是否提供实体（R5）：HTTP 控制模式看 /api/ping 的能力声明
        # （旧版模组未声明 -> False，避免每次网格化 404）；
        # 存档模式等无 server_info 的数据源按是否实现 entities() 判定。
        si = getattr(client, "server_info", None)
        if si is not None:
            self.has_entities = bool(si.get("entities"))
        else:
            self.has_entities = hasattr(client, "entities")
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

    def _group_origin(self, cx, cz):
        """区块坐标 -> 所属区块组的原区块坐标（组键的 (cx, cz) 分量）。"""
        g = self.p.group
        return (cx // g * g, cz // g * g)

    def _group_dist(self, gx, gz):
        """区块组到锚点的距离 = 组内**最近**区块的欧氏距离。

        装卸与 LOD 都以整组为单位：用"最近区块"而不是组中心，锚点所在组
        必定是 LOD0，也不会因组中心落在半径外而漏掉锚点旁边的区块。"""
        acx, acz = self._anchor_chunk()
        g = self.p.group
        dx = max(gx - acx, acx - (gx + g - 1), 0)
        dz = max(gz - acz, acz - (gz + g - 1), 0)
        return math.hypot(dx, dz)

    def _desired_lod(self, dist):
        if dist >= self.p.lod2_dist:
            return 2
        if dist >= self.p.lod1_dist:
            return 1
        return 0

    # ------------------------------------------------------------ 主循环 ----
    def tick(self):
        """主线程定期调用：计算需求集、入队缺失、标记卸载。"""
        acx, acz = self._anchor_chunk()
        need = {}
        R = self.p.r_load
        for cx in range(acx - R, acx + R + 1):
            for cz in range(acz - R, acz + R + 1):
                if math.hypot(cx - acx, cz - acz) <= R:
                    need[self._group_origin(cx, cz)] = None
        with self.lock:
            # 1. 缺失/变化入队
            for (gx, gz) in need:
                key = (self.p.dim, gx, gz)
                d = self._group_dist(gx, gz)
                st = self.state.get(key)
                want_lod = self._desired_lod(d)
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
                    if self._group_dist(key[1], key[2]) > self.p.r_unload:
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
        dim, gx, gz = key
        try:
            payload = self._fetch_geo(dim, gx, gz, lod)
            with self.lock:
                st = self.state.get(key)
                self.inflight_keys.discard(key)
                if st is None or st["gen"] != gen:
                    return  # 已过期（被重新入队或卸载）
                self.ready.append((key, {"lod": lod, "geo": payload,
                                         "yBottom": self.p.ymin}))
                st["status"] = READY
        except Exception as e:
            with self.lock:
                self.inflight_keys.discard(key)
                st = self.state.get(key)
                if st is not None and st["gen"] == gen:
                    st["status"] = QUEUED     # 失败退避: 回到队列，等下次派发
                    # 必须重新压回优先队列，否则该区块永久搁浅
                    self._push(self._group_dist(gx, gz) + 2.0, key)
                self.stats["errors"] += 1
                self.stats["last_error"] = f"{key}: {e}"
            self.log("fetch error", key, e)

    def _fetch_geo(self, dim, gx, gz, lod):
        # LOD0 且已加载资产包时走本地网格：只有本地网格路径能注入烘焙模型
        # （近处楼梯/栅栏需要真实几何）；LOD2（及未开模型时的 LOD1）仍用服务端
        # 网格保吞吐。两条路径都按整组产出：本地路径在组内跨区块贪心合并，
        # 服务端路径把组内各区块网格拼成一个对象（R4）。
        if lod < 2 and (self.p.mode == "raw"
                        or (lod == 0 and self._models_on())):
            geo = self._fetch_geo_local(dim, gx, gz, lod)
        else:
            geo = self._fetch_geo_server(dim, gx, gz, lod)
        ent = self._entity_geo(dim, gx, gz, lod)
        if ent is None:
            return geo
        return mesher.merge_geos([geo, ent], [(0.0, 0.0, 0.0)] * 2)

    def _entity_geo(self, dim, gx, gz, lod):
        """区块组内实体（目前只有画）的几何；远处 LOD 与无实体的数据源直接跳过。"""
        if lod > 0 or not self.has_entities:
            return None
        from . import assets, entities
        pack = assets.current()
        if pack is None or not pack.paintings:
            return None
        ents = []
        for i in range(self.p.group):
            for j in range(self.p.group):
                try:
                    got = self.client.entities(dim, gx + i, gz + j)
                except Exception as e:      # 损坏的实体区域文件不该拖垮整块网格
                    self.log("entity error", (gx + i, gz + j), e)
                    got = None
                if got:
                    ents.extend(got)
        if not ents:
            return None
        return entities.build_geo(ents, pack, gx, gz, self.p.ymin)

    def _models_on(self):
        if not self.p.use_models:
            return False
        from . import assets
        pack = assets.current()
        return pack is not None and bool(pack.blockstates)

    def _group_cells(self, gx, gz):
        """组的 (dx, dz) 偏移序列（dx 主序），供 mesher 组装邻域用。"""
        g = self.p.group
        return [(dx, dz) for dx in range(-1, g + 1) for dz in range(-1, g + 1)]

    def _fetch_geo_local(self, dim, gx, gz, lod):
        """组 + 外圈一格 -> 一个填充体积 -> 一次网格化（贪心可跨区块）。"""
        g = self.p.group
        payloads = {}
        for dx, dz in self._group_cells(gx, gz):
            nkey = (dim, gx + dx, gz + dz)
            payloads[(dx, dz)] = self.store.get_or_fetch(
                nkey, lambda k=nkey: self.client.chunk(
                    k[0], k[1], k[2], self.p.ymin, self.p.ymax))
        from . import assets
        quads, pal, _, models, bio = mesher.mesh_payload_biome(
            payloads, group=g, with_ao=(lod == 0), leaves_fast=self.p.leaves_fast,
            pack=assets.current(), fluids=True, biome=self.p.biome_tint)
        import numpy as np
        # 流体几何顶点是小数块坐标 -> 用 float32（整型会截断水面高度）
        verts = np.array([q[0] for q in quads], np.float32) if quads \
            else np.zeros((0, 4, 3), np.float32)
        dirs = np.array([q[1] for q in quads], np.uint8)
        blks = np.array([q[2] for q in quads], np.uint16)
        aos = np.array([q[3] for q in quads], np.uint8).reshape(-1, 4)
        from .mesher import geo_from_arrays
        return geo_from_arrays(verts, dirs, blks, aos, [n for _, n in pal],
                               models=models, pack=assets.current(), biome=bio)

    def _fetch_geo_server(self, dim, gx, gz, lod):
        """组内逐区块取服务端网格，平移到组局部坐标后拼成一个 geo。

        区块网格顶点是"区块局部块坐标、y 相对该区块的 yBottom"，组内各区块
        yBottom 相同（同一 ymin/ymax 裁剪），故按 (yBottom - ymin) 对齐高度即可。"""
        from . import assets
        from .mesher import geo_from_arrays, merge_geos
        g = self.p.group
        pack = assets.current()
        geos, offs = [], []
        for dx, dz in self._group_cells(gx, gz):
            if dx < 0 or dz < 0 or dx >= g or dz >= g:
                continue
            # 存档后端可由调用方决定是否要群系数据（net 后端由服务端决定，
            # 返回后再按开关决定用不用）
            kw = ({"biome": self.p.biome_tint}
                  if getattr(self.client, "world", None) is not None else {})
            m = self.client.mesh(dim, gx + dx, gz + dz, self.p.ymin, self.p.ymax,
                                 lod=lod, ao=(lod == 0),
                                 leaves=("fast" if self.p.leaves_fast else "fancy"),
                                 **kw)
            geos.append(geo_from_arrays(
                m["verts"], m["dirs"], m["blocks"], m["aos"],
                [n for _, n in m["palette"]], models=m.get("models"), pack=pack,
                biome=(m.get("biome") if self.p.biome_tint else None)))
            offs.append((16.0 * dx, float(m.get("yBottom", self.p.ymin) - self.p.ymin),
                         16.0 * dz))
        if g == 1:
            return geos[0]
        return merge_geos(geos, offs)

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
            g = self.p.group
            cxs = [k[1] for k in keys]
            czs = [k[2] for k in keys]
            vers = self.client.versions(self.p.dim, min(cxs), min(czs),
                                        max(cxs) + g - 1, max(czs) + g - 1)
            with self.lock:
                for key in keys:
                    st = self.state.get(key)
                    if st is None or st["status"] != LIVE:
                        continue
                    # 组内逐区块版本按固定顺序取成元组：任何一个区块变了都重载整组
                    v = tuple(vers.get((key[1] + i, key[2] + j))
                              for i in range(g) for j in range(g))
                    if st["version"] is None:
                        st["version"] = v
                    elif v != st["version"]:
                        st["version"] = v
                        st["gen"] += 1
                        st["status"] = QUEUED
                        self._push(self._group_dist(key[1], key[2]), key)
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
                        keys.add(self._group_origin(a, b))
        with self.lock:
            self.frozen = True
            for (gx, gz) in keys:
                key = (self.p.dim, gx, gz)
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
