# -*- coding: utf-8 -*-
"""调度器测试：迟滞装卸 / 优先级 / LOD / 版本重载 / 预热。"""
import math
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))
sys.path.insert(0, os.path.join(ROOT, "server_sim"))

PORT = 8793


class FakeImporter:
    """收集调度器输出，模拟 Blender 主线程。"""

    def __init__(self):
        self.objects = {}    # key -> payload
        self.dead = []

    def apply(self, scheduler, max_loops=400):
        for _ in range(max_loops):
            scheduler.drain()
            applied = scheduler.poll_apply(64)
            for key, payload in applied:
                self.objects[key] = payload
            evicted = scheduler.poll_evict(64)
            for key, reason in evicted:
                self.objects.pop(key, None)
                self.dead.append((key, reason))
            if not applied and not evicted:
                # 队列、就绪、在途全部清空才算收敛
                if not scheduler.ready and not scheduler.queue \
                        and not scheduler.inflight_keys:
                    return True
            time.sleep(0.02)
        return False


class TestScheduler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import mc_server_sim
        cls.httpd, cls.world = mc_server_sim.serve_in_thread(PORT, seed=11)
        from mc_bridge.core.net import ApiClient
        from mc_bridge.core.scheduler import Params, Scheduler
        cls.ApiClient, cls.Params, cls.Scheduler = ApiClient, Params, Scheduler

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _scheduler(self, **kw):
        base = dict(r_load=2, r_unload=3,
                    ymin=-64, ymax=320, inflight=4)
        base.update(kw)
        return self.Scheduler(self.ApiClient("127.0.0.1", PORT), self.Params(**base))

    def test_load_and_evict_hysteresis(self):
        sch = self._scheduler()
        imp = FakeImporter()
        sch.update_anchor(8.0, 8.0)      # 锚点在区块(0,0)
        self.assertTrue(imp.apply(sch))
        self.assertIn(("overworld", 0, 0), imp.objects)
        self.assertIn(("overworld", 2, 0), imp.objects)
        n0 = len(imp.objects)
        # 迟滞: 移动 1 区块（仍在 r_unload 内）不卸载
        sch.update_anchor(24.0, 8.0)     # 锚点区块(1,0)
        self.assertTrue(imp.apply(sch))
        self.assertGreaterEqual(len(imp.objects), n0)
        # 移出卸载半径 -> 卸载发生
        sch.update_anchor(8.0 + 16 * 8, 8.0)
        self.assertTrue(imp.apply(sch))
        self.assertNotIn(("overworld", 0, 0), imp.objects)
        sch.stop()

    def test_priority_order(self):
        """近处区块组应先于远处就绪。"""
        sch = self._scheduler(r_load=2, r_unload=3, inflight=1)
        order = []
        orig = sch.poll_apply

        def wrapped(max_n=2):
            out = orig(max_n)
            order.extend(k for k, _ in out)
            return out

        sch.poll_apply = wrapped
        sch.update_anchor(8.0, 8.0)
        imp = FakeImporter()
        self.assertTrue(imp.apply(sch))
        G = sch.p.group

        def d(k):
            """组的调度距离 = 组内最近区块到锚点（锚点区块 (0,0)）。"""
            gx, gz = k[1], k[2]
            return math.hypot(max(gx, -gx - G + 1, 0), max(gz, -gz - G + 1, 0))

        # 单线程串行: 应按组的调度距离近似升序
        dists = [d(k) for k in order]
        self.assertEqual(dists, sorted(dists))
        sch.stop()

    def test_group_reduces_objects(self):
        """R4：区块组把对象数成倍减少，且几何覆盖整组范围。

        组按绝对区块坐标对齐，锚点附近必然有"只用到 1~2 个区块"的边界组，
        故收益低于理论 group²（r_load=4 实测 49 -> 17，约 2.9 倍）。"""
        counts = {}
        for g in (1, 2, 4):
            sch = self._scheduler(r_load=4, r_unload=5, mode="raw", group=g)
            imp = FakeImporter()
            sch.update_anchor(8.0, 8.0)
            self.assertTrue(imp.apply(sch))
            counts[g] = len(imp.objects)
            for key in imp.objects:
                self.assertEqual(key[1] % g, 0)
                self.assertEqual(key[2] % g, 0)
            if g > 1:
                geo = imp.objects[("overworld", 0, 0)]["geo"]
                # 组局部坐标：顶点跨到组内最后一个区块（x/z 最大 16*group）
                self.assertAlmostEqual(float(geo["verts"][:, 0].max()), 16.0 * g)
                self.assertAlmostEqual(float(geo["verts"][:, 2].max()), 16.0 * g)
            sch.stop()
        self.assertGreater(counts[1], counts[2] * 2, counts)
        self.assertGreater(counts[2], counts[4] * 1.5, counts)

    def test_version_reload(self):
        sch = self._scheduler()
        imp = FakeImporter()
        sch.update_anchor(8.0, 8.0)
        self.assertTrue(imp.apply(sch))
        key = ("overworld", 0, 0)
        v0 = sch.state[key]["version"] if sch.state[key]["version"] is None else None
        # 强制版本轮询建立基线
        sch.maybe_poll_versions(force=True)
        deadline = time.time() + 5
        while sch._version_polling and time.time() < deadline:
            time.sleep(0.02)
        base = sch.state[key]["version"]
        self.assertIsNotNone(base)
        # 世界变化 -> setblock 版本+1
        self.world.setblock(8, 80, 8, "minecraft:stone")
        sch.maybe_poll_versions(force=True)
        deadline = time.time() + 5
        while sch._version_polling and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(sch.state[key]["status"], "QUEUED")
        imp.apply(sch)
        self.assertEqual(sch.state[key]["status"], "LIVE")
        sch.stop()

    def test_prewarm_freezes_eviction(self):
        sch = self._scheduler()
        imp = FakeImporter()
        sch.update_anchor(8.0, 8.0)
        self.assertTrue(imp.apply(sch))
        n0 = len(imp.objects)
        sch.prewarm([(8.0, 8.0), (200.0, 200.0)])
        self.assertTrue(imp.apply(sch))
        # 冻结期间不卸载
        self.assertGreaterEqual(len(imp.objects), n0)
        self.assertIn(("overworld", 12, 12), imp.objects)
        sch.unfreeze()
        sch.update_anchor(8.0, 8.0)
        imp.apply(sch)
        sch.stop()

    def test_raw_mode(self):
        """模式 A（客户端本地网格）也能完成调度。"""
        sch = self._scheduler(mode="raw")
        imp = FakeImporter()
        sch.update_anchor(8.0, 8.0)
        self.assertTrue(imp.apply(sch))
        self.assertIn(("overworld", 0, 0), imp.objects)
        geo = imp.objects[("overworld", 0, 0)]["geo"]
        self.assertGreater(geo["nq"], 50)
        sch.stop()

    # ------------------------------------------------------------ R9 ----
    def _stage_ready(self, sch, order):
        """按 order（组 x 坐标序列）把远/近组塞进 ready（模拟乱序完成）。"""
        with sch.lock:
            for gx in order:
                key = ("overworld", gx, 0)
                sch.state[key] = {"status": "READY", "gen": 0,
                                  "last_seen": sch.time(), "version": None}
                sch.ready.append((key, {"geo": {}}))

    def test_distance_first_apply_order(self):
        """R9：远组先完成时，应用仍按距离（近组先上屏）。"""
        sch = self._scheduler(distance_first=True)
        self._stage_ready(sch, (4, 0))       # 远组先 ready
        out = sch.poll_apply(10)
        self.assertEqual([k[1] for k, _ in out], [0, 4])
        sch.stop()

    def test_distance_first_off_keeps_ready_order(self):
        """R9：关闭开关保持完成序（不做重排）。"""
        sch = self._scheduler(distance_first=False)
        self._stage_ready(sch, (4, 0))
        out = sch.poll_apply(10)
        self.assertEqual([k[1] for k, _ in out], [4, 0])
        sch.stop()

    def test_queue_reorder_on_anchor_move(self):
        """R9：锚点跨组移动后，队列按新锚点距离重排（旧入队序作废）。"""
        import heapq
        sch = self._scheduler(r_load=2, r_unload=3, distance_first=True)
        sch._drain = lambda: None            # 只查队列序，不派发
        sch.update_anchor(8.0, 8.0)          # 锚点区块 (0,0)
        self.assertTrue(sch.queue)
        # 在旧队列前面压一个"新锚点附近"的组，模拟旧排序残留
        far_key = ("overworld", -6, 0)
        with sch.lock:
            sch.state[far_key] = {"status": "QUEUED", "gen": 0,
                                  "last_seen": sch.time(), "version": None}
            heapq.heappush(sch.queue, (0.0, sch._seq, far_key))
            sch._seq += 1
        sch.update_anchor(8.0 + 16 * 2, 8.0)  # 锚点区块 (2,0)：跨组移动
        popped = []
        with sch.lock:
            while sch.queue:
                popped.append(heapq.heappop(sch.queue)[2])
        G = sch.p.group
        dists = []
        for _dim, gx, gz in popped:
            dx = max(gx - 2, 2 - (gx + G - 1), 0)
            dz = max(gz, -gz - G + 1, 0)
            dists.append(math.hypot(dx, dz))
        self.assertEqual(dists, sorted(dists),
                         "队列弹出序应按新锚点距离非降")
        sch.stop()

    # ------------------------------------------------------------ R11 ----
    def test_pinned_no_evict(self):
        """R11：钉选的常见区块移出卸载半径后仍在场景。"""
        sch = self._scheduler(r_load=2, r_unload=3)
        imp = FakeImporter()
        sch.update_anchor(8.0, 8.0)
        self.assertTrue(imp.apply(sch))
        key = ("overworld", 0, 0)
        self.assertIn(key, imp.objects)
        sch.set_pinned([key])
        sch.update_anchor(8.0 + 16 * 8, 8.0)   # 远超 r_unload
        self.assertTrue(imp.apply(sch))
        self.assertIn(key, imp.objects, "钉选组不应被卸载")
        self.assertNotIn(("overworld", 2, 0), imp.objects,
                         "未钉选的邻居组应照常卸载")
        sch.stop()

    def test_pinned_preload(self):
        """R11：ensure_pinned 把远距离钉选组补进调度（自动预载）。"""
        sch = self._scheduler(r_load=2, r_unload=3)
        key = ("overworld", 10, 10)
        sch.set_pinned([key])
        sch.update_anchor(8.0, 8.0)
        self.assertEqual(sch.ensure_pinned(), 1)
        imp = FakeImporter()
        self.assertTrue(imp.apply(sch))
        self.assertIn(key, imp.objects, "远距离钉选组应被预载")
        # 幂等：再次 ensure 不重复入队
        self.assertEqual(sch.ensure_pinned(), 0)
        sch.stop()

    def test_pinned_dim_mismatch_ignored(self):
        """R11：其它维度的钉选组不在当前维度预载。"""
        sch = self._scheduler()
        sch.set_pinned([("the_nether", 0, 0)])
        sch.update_anchor(8.0, 8.0)
        self.assertEqual(sch.ensure_pinned(), 0)
        sch.stop()

    # ------------------------------------------------------------ R10 ----
    class _RevClient:
        """带世界修订号的假客户端：验证事件驱动门控。"""

        def __init__(self, rev=5):
            self.rev = rev
            self.versions_calls = 0

        def world_rev(self):
            return self.rev

        def versions(self, dim, x0, z0, x1, z1):
            self.versions_calls += 1
            return {(cx, cz): 1 for cx in range(x0, x1 + 1)
                    for cz in range(z0, z1 + 1)}

    def _live(self, sch, gxs=(0,), version=None):
        with sch.lock:
            for gx in gxs:
                key = ("overworld", gx, 0)
                sch.state[key] = {"status": "LIVE", "gen": 0,
                                  "last_seen": sch.time(),
                                  "version": version}

    def _wait_poll(self, sch, timeout=5):
        deadline = time.time() + timeout
        while sch._version_polling and time.time() < deadline:
            time.sleep(0.02)

    def test_world_rev_gate(self):
        """R10：修订号未变跳过逐组扫描，变化后扫描并重载改动组。"""
        client = self._RevClient()
        sch = self.Scheduler(client, self.Params(r_load=2, r_unload=3))
        self._live(sch, version=(1,) * (sch.p.group ** 2))
        sch._poll_versions(list(sch.state))      # 首查建立基线
        self.assertEqual(client.versions_calls, 1)
        sch._poll_versions(list(sch.state))      # rev 未变 -> 门控跳过
        self.assertEqual(client.versions_calls, 1)
        client.rev += 1                          # 世界变了
        sch._poll_versions(list(sch.state))
        self.assertEqual(client.versions_calls, 2)
        sch.stop()

    def test_rev_fallback_on_old_mod(self):
        """R10：旧模组无 /api/worldrev -> 404 后回退常规轮询，不抛异常。"""
        class OldClient:
            def versions(self, dim, x0, z0, x1, z1):
                return {(cx, cz): 1 for cx in range(x0, x1 + 1)
                        for cz in range(z0, z1 + 1)}
        sch = self.Scheduler(OldClient(), self.Params())
        self._live(sch, version=(1,) * (sch.p.group ** 2))
        sch._poll_versions(list(sch.state))
        self.assertFalse(sch._rev_ok)
        self.assertEqual(sch.state[("overworld", 0, 0)]["status"], "LIVE")
        sch.stop()

    def test_update_lists_semantics(self):
        """R10：总开关 / 白名单 / 黑名单（黑优先）的过滤语义。"""
        sch = self._scheduler(auto_update=False)
        k0, k4 = ("overworld", 0, 0), ("overworld", 4, 0)
        # 总开关关：全部不更新
        self.assertFalse(sch._updatable(k0))
        self.assertFalse(sch._updatable(k4))
        # 白名单：强制更新
        sch.set_update_whitelist([k0])
        self.assertTrue(sch._updatable(k0))
        self.assertFalse(sch._updatable(k4))
        # 黑名单优先于白名单
        sch.set_update_blacklist([k0])
        self.assertFalse(sch._updatable(k0))
        # 总开关开时黑名单仍拦截
        sch.p.auto_update = True
        self.assertFalse(sch._updatable(k0))
        self.assertTrue(sch._updatable(k4))
        sch.stop()

    def test_save_stamp_reload(self):
        """R10 存档：变更戳变化 -> 清缓存 + LIVE 重扫（首查只建基线）。"""
        stamps = {"v": 1.0}
        invalidated = {"n": 0}
        sch = self.Scheduler(self._RevClient(), self.Params(
            world_stamp_fn=lambda: stamps["v"],
            invalidate_fn=lambda: invalidated.__setitem__(
                "n", invalidated["n"] + 1)))
        self._live(sch, (0,))
        self.assertTrue(sch.maybe_poll_versions())
        self._wait_poll(sch)
        self.assertEqual(invalidated["n"], 0)
        self.assertEqual(sch.state[("overworld", 0, 0)]["status"], "LIVE")
        stamps["v"] = 2.0                         # 存档被外部更新
        self.assertTrue(sch.maybe_poll_versions(force=True))
        self._wait_poll(sch)
        self.assertEqual(invalidated["n"], 1, "变更后应先失效缓存")
        self.assertEqual(sch.state[("overworld", 0, 0)]["status"], "QUEUED")
        sch.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
