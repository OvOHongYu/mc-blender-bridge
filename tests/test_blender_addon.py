# -*- coding: utf-8 -*-
"""Blender 插件冒烟测试（fake-bpy）：注册、连接、主循环、导入器、材质、卸载、
操作符。全部在无 Blender 的环境中验证插件代码路径。"""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "blender_addon"))
sys.path.insert(0, os.path.join(ROOT, "server_sim"))
sys.path.insert(0, HERE)

import fake_bpy  # noqa: E402
bpy = fake_bpy.install()

PORT = 8798


class TestAddonSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import mc_server_sim
        cls.httpd, cls.world = mc_server_sim.serve_in_thread(PORT, seed=7)
        # 清理可能的上次导入
        for m in [k for k in sys.modules if k == "mc_bridge" or k.startswith("mc_bridge.")]:
            del sys.modules[m]
        import mc_bridge
        mc_bridge.register()
        cls.mc_bridge = mc_bridge
        from mc_bridge import ops, importer, mats, state
        cls.ops, cls.importer, cls.mats, cls.state = ops, importer, mats, state

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        try:
            self = cls
            self.mc_bridge.unregister()
        except Exception:
            pass

    def _scene(self):
        return bpy.context.scene

    def _p(self):
        return self._scene().mcb

    def _set_props(self, p):
        p.host, p.port = "127.0.0.1", PORT
        p.dim = "overworld"
        p.r_load, p.r_unload = 2, 3
        p.ymin, p.ymax = -64, 320
        p.mode = "mesh"
        p.leaves = "fancy"
        p.inflight = 4
        p.apply_per_tick, p.evict_per_tick = 8, 32
        p.poll_interval = 0.02
        p.version_poll = 5.0
        p.auto_frame = True

    # ------------------------------------------------------------ 注册 ----
    def test_register(self):
        self.assertIn("MCB_PT_panel", fake_bpy._registered_classes)
        self.assertIn("MCB_OT_connect", fake_bpy._registered_classes)
        self.assertIn("MCB_Properties", fake_bpy._registered_classes)
        p = self._p()
        self.assertIsNotNone(p)
        self.assertEqual(p.host, "127.0.0.1")

    def test_props_roundtrip(self):
        p = self._p()
        self._set_props(p)
        self.assertEqual(p.r_load, 2)
        p.status = "测试状态"
        self.assertEqual(p.status, "测试状态")

    # ------------------------------------------------------------ 连接 ----
    def test_connect_disconnect(self):
        p = self._p()
        self._set_props(p)
        ok, msg = self.ops.connect(p)
        self.assertTrue(ok, msg)
        self.assertIn("已连接", p.status)
        self.assertIsNotNone(self.state.scheduler())
        cam = bpy.data.objects.new("Camera")
        import fake_bpy as _fb
        cam._matrix_world.translation = _fb._Vec3(8.0, 8.0, 8.0)
        self._scene().camera = cam
        # 主循环推进
        sch = self.state.scheduler()
        for _ in range(200):
            self.ops.step_tick(p, self._scene())
            if not sch.ready and not sch.queue and not sch.inflight_keys:
                break
            time.sleep(0.02)
        self.assertEqual(sch.stats["errors"], 0)
        n = self.importer.count_live()
        self.assertGreaterEqual(n, 6)       # r_load=2 -> 13 区块，2×2 组 -> 6 个对象
        obj = bpy.data.objects.get("MCB_ow_0_0")
        self.assertIsNotNone(obj)
        self.assertGreater(len(obj.data.vertices.co), 0)
        self.assertGreater(len(obj.data.materials), 0)
        self.assertEqual(obj.location[2], -64)   # Z-up: 高度在 Z 轴
        # 断开
        self.assertTrue(self.ops.disconnect(p))
        self.assertIsNone(self.state.scheduler())
        # 断开后 unload_all
        n = self.importer.unload_all()
        self.assertEqual(self.importer.count_live(), 0)

    # ------------------------------------------------------------ 装卸 ----
    def test_move_anchor_evicts(self):
        p = self._p()
        self._set_props(p)
        ok, msg = self.ops.connect(p)
        self.assertTrue(ok)
        # 相机锚点
        cam = bpy.data.objects.new("Camera")
        import fake_bpy as _fb
        cam._matrix_world.translation = _fb._Vec3(8.0, 8.0, 8.0)
        self._scene().camera = cam
        sch = self.state.scheduler()
        for _ in range(250):
            self.ops.step_tick(p, self._scene())
            if not sch.ready and not sch.queue and not sch.inflight_keys:
                break
            time.sleep(0.02)
        self.assertIn("MCB_ow_0_0", [o.name for o in bpy.data.objects])
        # 锚点远跳: (600, 8, 600) -> 区块 (37,37), 旧区块应卸载
        # （Blender Z 为竖直轴，水平面在 XZ；MC 的 (x,z) 对应 Blender 的 (x,z)）
        import fake_bpy as _fb
        cam._matrix_world.translation = _fb._Vec3(600.0, 8.0, 600.0)
        for _ in range(250):
            self.ops.step_tick(p, self._scene())
            if not sch.ready and not sch.queue and not sch.inflight_keys:
                break
            time.sleep(0.02)
        self.assertIsNone(bpy.data.objects.get("MCB_ow_0_0"),
                          "远跳后旧区块应已卸载")
        new_names = [o.name for o in bpy.data.objects if o.name.startswith("MCB_")]
        self.assertGreater(len(new_names), 0)
        self.ops.disconnect(p)
        self.importer.unload_all()

    def test_eviction_purges_orphan_meshes(self):
        p = self._p()
        self._set_props(p)
        ok, _ = self.ops.connect(p)
        self.assertTrue(ok)
        cam = bpy.data.objects.new("Camera2")
        self._scene().camera = cam
        import fake_bpy as _fb
        cam._matrix_world.translation = _fb._Vec3(8.0, 100.0, 8.0)
        sch = self.state.scheduler()
        for _ in range(250):
            self.ops.step_tick(p, self._scene())
            if not sch.ready and not sch.queue and not sch.inflight_keys:
                break
            time.sleep(0.02)

        def chunk_objs():
            # 根空物体 MCB_Root 不算区块对象
            return [o for o in bpy.data.objects
                    if o.name.startswith("MCB_") and o.name != self.importer.ROOT_NAME]

        n_objs = len(chunk_objs())
        n_meshes = len([m for m in bpy.data.meshes if m.name.startswith("MCB_")])
        self.assertEqual(n_objs, n_meshes)
        # 手工删除 object 不清 mesh -> purge 应清掉
        victim = next(iter(chunk_objs()))
        bpy.data.objects.remove(victim)
        self.importer.purge_orphans()
        self.assertEqual(len(chunk_objs()), n_objs - 1)
        self.assertEqual(len([m for m in bpy.data.meshes if m.name.startswith("MCB_")]),
                         n_objs - 1)
        self.ops.disconnect(p)
        self.importer.unload_all()

    # ------------------------------------------------------------ 进度 ----
    def test_progress_ui_first_load(self):
        import fake_bpy as _fb
        p = self._p()
        self._set_props(p)
        ok, _ = self.ops.connect(p)
        self.assertTrue(ok)
        sch = self.state.scheduler()
        self.ops.step_tick(p, self._scene())
        self.assertTrue(p.progress, "首载应显示进度文本")
        self.assertIn("加载中", p.progress)
        self.assertGreaterEqual(p.progress_pct, 0.0)
        wm = bpy.context.window_manager
        self.assertTrue(wm._progress["on"], "应启动状态栏原生进度条")
        for _ in range(250):
            self.ops.step_tick(p, self._scene())
            if not sch.ready and not sch.queue and not sch.inflight_keys:
                break
            time.sleep(0.02)
        self.ops.step_tick(p, self._scene())
        self.assertEqual(p.progress, "", "全部加载完成后进度文本应清空")
        self.assertFalse(wm._progress["on"], "加载完成后应关闭原生进度条")
        self.ops.disconnect(p)
        self.importer.unload_all()

    # ------------------------------------------------------------ load_post ----
    def test_load_post_leftover_and_reset(self):
        import fake_bpy as _fb
        p = self._p()
        self._set_props(p)
        ok, _ = self.ops.connect(p)
        self.assertTrue(ok)
        # 模拟打开新文件（路径变化）-> 连接应重置
        bpy.data.filepath = "//fake_a.blend"
        self.ops._on_load_post()
        self.assertIsNone(self.state.scheduler(), "切换文件后连接应重置")
        self.assertEqual(self.state.leftover(), 0)
        # 造两个残留对象，再模拟打开含残留对象的文件
        coll = self.importer.collection()
        for nm in ("MCB_ow_0_0", "MCB_ow_2_0"):
            coll.objects.link(bpy.data.objects.new(nm))
        bpy.data.filepath = "//fake_b.blend"
        self.ops._on_load_post()
        self.assertEqual(self.state.leftover(), 2)
        self.assertIn("残留", p.status)
        # 同一路径再次触发（模拟 undo/redo）-> 不重复重置/不弹残留
        self.ops._on_load_post()
        self.assertEqual(self.state.leftover(), 2)
        # 选择全部清除
        ret = fake_bpy.call_operator("mcb.leftover_clear", bpy.context)
        self.assertEqual(ret, {'FINISHED'})
        self.assertEqual(self.importer.count_live(), 0)
        self.assertEqual(self.state.leftover(), 0)
        # 转为静态保留
        coll.objects.link(bpy.data.objects.new("MCB_ow_4_4"))
        bpy.data.filepath = "//fake_c.blend"
        self.ops._on_load_post()
        self.assertEqual(self.state.leftover(), 1)
        ret = fake_bpy.call_operator("mcb.leftover_keep", bpy.context)
        self.assertEqual(ret, {'FINISHED'})
        self.assertEqual(self.importer.count_live(), 1, "转静态应保留对象")
        self.assertEqual(self.state.leftover(), 0)
        self.importer.unload_all()
        bpy.data.filepath = ""

    # ------------------------------------------------------------ R11 ----
    def test_list_manage_pinned_preload(self):
        """R11：名单操作符 + 同步 + 卸载全部后常见区块自动拉回。"""
        p = self._p()
        self._set_props(p)
        ok, _ = self.ops.connect(p)
        self.assertTrue(ok)
        sch = self.state.scheduler()
        import fake_bpy as _fb
        cam = bpy.data.objects.new("CameraP")
        self._scene().camera = cam
        cam._matrix_world.translation = _fb._Vec3(8.0, 8.0, 8.0)
        for _ in range(250):
            self.ops.step_tick(p, self._scene())
            if not sch.ready and not sch.queue and not sch.inflight_keys:
                break
            time.sleep(0.02)
        obj = bpy.data.objects.get("MCB_ow_0_0")
        self.assertIsNotNone(obj)
        # 选中 -> 加入常见区块
        bpy.context.selected_objects = [obj]
        ret = fake_bpy.call_operator("mcb.list_manage", bpy.context,
                                     list_kind='pinned', action='add')
        self.assertEqual(ret, {'FINISHED'})
        self.assertIn("overworld:0,0", p.pinned_chunks)
        # 卸载全部（状态重置）-> 常见区块被自动预载拉回
        fake_bpy.call_operator("mcb.unload_all", bpy.context)
        self.assertIsNone(bpy.data.objects.get("MCB_ow_0_0"))
        for _ in range(250):
            self.ops.step_tick(p, self._scene())
            if bpy.data.objects.get("MCB_ow_0_0") and not sch.ready \
                    and not sch.queue and not sch.inflight_keys:
                break
            time.sleep(0.02)
        self.assertIsNotNone(bpy.data.objects.get("MCB_ow_0_0"),
                             "常见区块应被自动预载拉回")
        # 选中 -> 移出名单；清空
        bpy.context.selected_objects = [obj]
        fake_bpy.call_operator("mcb.list_manage", bpy.context,
                               list_kind='pinned', action='remove')
        self.assertEqual(p.pinned_chunks, "")
        # 无选中时加入应被拒绝
        bpy.context.selected_objects = []
        ret = fake_bpy.call_operator("mcb.list_manage", bpy.context,
                                     list_kind='pinned', action='add')
        self.assertEqual(ret, {'CANCELLED'})
        self.ops.disconnect(p)
        self.importer.unload_all()

    def test_list_manage_update_lists(self):
        """R10：更新白/黑名单操作符 + 同步进调度器。"""
        p = self._p()
        self._set_props(p)
        ok, _ = self.ops.connect(p)
        self.assertTrue(ok)
        o = bpy.data.objects.new("MCB_ow_0_0")
        o["mcb_key"] = {"dim": "overworld", "cx": 0, "cz": 0}
        bpy.context.selected_objects = [o]
        fake_bpy.call_operator("mcb.list_manage", bpy.context,
                               list_kind='whitelist', action='add')
        self.assertIn("overworld:0,0", p.update_whitelist)
        fake_bpy.call_operator("mcb.list_manage", bpy.context,
                               list_kind='blacklist', action='add')
        self.assertIn("overworld:0,0", p.update_blacklist)
        self.ops._sync_lists(p)
        sch = self.state.scheduler()
        self.assertIn(("overworld", 0, 0), sch.update_whitelist)
        self.assertIn(("overworld", 0, 0), sch.update_blacklist)
        fake_bpy.call_operator("mcb.list_manage", bpy.context,
                               list_kind='whitelist', action='remove')
        self.assertEqual(p.update_whitelist, "")
        fake_bpy.call_operator("mcb.list_manage", bpy.context,
                               list_kind='blacklist', action='clear')
        self.assertEqual(p.update_blacklist, "")
        self.ops._sync_lists(p)
        self.assertEqual(sch.update_whitelist, set())
        self.assertEqual(sch.update_blacklist, set())
        self.ops.disconnect(p)
        self.importer.unload_all()

    # ------------------------------------------------------------ 材质 ----
    def test_material_placeholder_and_texture(self):
        client = None
        # 清理前序测试遗留的材质/贴图，确保从占位状态开始
        for m in list(bpy.data.materials):
            if m.name.startswith("MCB_"):
                bpy.data.materials.remove(m)
        for im in list(bpy.data.images):
            if im.name.startswith("MCBT_"):
                bpy.data.images.remove(im)
        self.mats.reset()
        p = self._p()
        self._set_props(p)
        ok, _ = self.ops.connect(p)
        self.assertTrue(ok)
        from mc_bridge.core.net import ApiClient
        client = ApiClient("127.0.0.1", PORT)
        self.state.client()  # noqa
        fetcher = client.texture_png
        mat = self.mats.get_material("minecraft:stone", "side", fetcher)
        self.assertEqual(mat.name, "MCB_minecraft_stone__side")
        self.assertTrue(mat.use_nodes)
        # 等待后台贴图线程
        for _ in range(100):
            self.mats.flush(64)
            if self.mats.pending_count() == 0 and not self.mats._task_q.unfinished_tasks:
                break
            time.sleep(0.05)
        img = bpy.data.images.get("MCBT_minecraft_stone__side")
        self.assertIsNotNone(img, "贴图应已加载为 Image")
        mat2 = self.mats.get_material("minecraft:stone", "side", fetcher)
        self.assertIs(mat, mat2)   # 复用
        self.ops.disconnect(p)

    # ------------------------------------------------------------ 操作符 ----
    def test_operator_connect_fail(self):
        p = self._p()
        self._set_props(p)
        p.port = 59999          # 无人监听
        ok, msg = self.ops.connect(p)
        self.assertFalse(ok)
        self.assertIn("连接失败", msg)
        p.status = "未连接"

    def test_ops_call_connect(self):
        p = self._p()
        self._set_props(p)
        ret = fake_bpy.call_operator("mcb.connect", bpy.context)
        self.assertEqual(ret, {'FINISHED'})
        self.assertIn("已连接", p.status)
        ret = fake_bpy.call_operator("mcb.disconnect", bpy.context)
        self.assertEqual(ret, {'FINISHED'})
        fake_bpy.call_operator("mcb.unload_all", bpy.context)


if __name__ == "__main__":
    unittest.main(verbosity=2)
