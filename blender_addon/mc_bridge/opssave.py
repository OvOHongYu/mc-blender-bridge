# -*- coding: utf-8 -*-
"""存档模式核心逻辑：直接解析 MC 存档文件，无需模组/运行实例。

连接、调度器装配与状态/统计与 ops.py 保持一致；核心逻辑放在普通函数里，
便于无 bpy 环境下测试。"""
import os
import threading

from . import importer, mats, state
from .core.anvil import AnvilWorld, DIMS, SaveClient, norm_dim
from .core.scheduler import Params, Scheduler

_store = {}
_store_lock = threading.Lock()


def _remember(client, world):
    with _store_lock:
        for old_client, old_world in _store.values():
            try:
                old_client.close()
                old_world.close()
            except Exception:
                pass
        _store.clear()
        _store["world"] = (client, world)


def connect_save(p):
    """p: MCB_Properties（load_mode=save）。返回 (ok, message)。"""
    state.ensure_assets(p)
    save_dir = getattr(p, "save_dir", "") or os.getcwd()
    if not os.path.isdir(save_dir):
        return False, "存档目录不存在: %s" % save_dir
    dim = norm_dim(getattr(p, "dim", "") or "minecraft:overworld")
    if dim not in DIMS:
        return False, "存档模式暂不支持维度: %s" % dim
    try:
        world = AnvilWorld(save_dir)
        client = SaveClient(world)
    except Exception as e:
        return False, "打开存档失败: %s" % e
    scheduler = Scheduler(client, Params(
        dim=dim, r_load=p.r_load, r_unload=p.r_unload,
        lod1_dist=p.lod1_dist, lod2_dist=p.lod2_dist,
        ymin=p.ymin, ymax=p.ymax, mode="raw",
        leaves_fast=(p.leaves == "fast"), group=int(p.group),
        inflight=p.inflight,
        use_models=getattr(p, "use_models", True),
        biome_tint=getattr(p, "biome_tint", True),
        version_interval=p.version_poll))
    state.set_runtime(client, scheduler,
                      client.ping(), client.blocks())
    _remember(client, world)
    p.status = "已连接存档 %s (MC DataVersion %s)" % (save_dir, world.mc_version)
    return True, p.status


def disconnect_save(p, keep_objects=True):
    scheduler = state.scheduler()
    if scheduler is not None:
        scheduler.stop()
    mate = state.client()
    if mate is not None:
        try:
            mate.close()
        except Exception:
            pass
    with _store_lock:
        _store.clear()
    state.clear()
    mats.reset()
    if not keep_objects:
        importer.unload_all()
    p.status = "未连接"
    return True