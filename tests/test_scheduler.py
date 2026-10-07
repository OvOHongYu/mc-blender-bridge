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


if __name__ == "__main__":
    unittest.main(verbosity=2)
