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
        base = dict(r_load=2, r_unload=3, lod1_dist=1, lod2_dist=2,
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

    def test_lod_by_distance(self):
        sch = self._scheduler(r_load=3, r_unload=4, lod1_dist=1, lod2_dist=2)
        imp = FakeImporter()
        sch.update_anchor(8.0, 8.0)
        self.assertTrue(imp.apply(sch))
        self.assertEqual(imp.objects[("overworld", 0, 0)]["lod"], 0)
        lods = [p["lod"] for k, p in imp.objects.items()]
        self.assertIn(1, lods)
        self.assertIn(2, lods)
        sch.stop()

    def test_priority_order(self):
        """近处区块应先于远处就绪。"""
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
        d = lambda k: math.hypot(k[1], k[2])
        # 单线程串行: 应按距离近似升序
        dists = [d(k) for k in order]
        self.assertEqual(dists, sorted(dists))
        sch.stop()

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
