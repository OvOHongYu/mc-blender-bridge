# -*- coding: utf-8 -*-
"""操作符与主循环。核心逻辑放在普通函数里（connect/step_tick），
操作符仅做包装——便于无 bpy 环境下测试。"""
import bpy

from . import importer, mats, opssave, state
from .core.net import ApiClient, ApiError
from .core.scheduler import Params, Scheduler
from .core.util import parse_group_list, serialize_group_list


# ------------------------------------------------------------- 核心逻辑 ----

def connect(p):
    """p: MCB_Properties。返回 (ok, message)。存档模式走独立数据源。"""
    state.set_leftover(0)               # 重新连接后残留对象由调度器接管
    state.ensure_assets(p)
    if getattr(p, "load_mode", "control") == "save":
        mats.set_light_map(None)        # 存档模式没有 /api/blocks，走内置原版表
        return opssave.connect_save(p)
    client = ApiClient(p.host, p.port)
    try:
        info = client.ping()
        blocks = client.blocks()
    except Exception as e:
        return False, "连接失败: %s" % e
    scheduler = Scheduler(client, Params(
        dim=p.dim, r_load=p.r_load, r_unload=p.r_unload,
        ymin=p.ymin, ymax=p.ymax, mode=p.mode,
        leaves_fast=(p.leaves == "fast"), group=int(p.group),
        inflight=p.inflight,
        use_models=getattr(p, "use_models", True),
        biome_tint=getattr(p, "biome_tint", True),
        version_interval=p.version_poll,
        light=(blocks.get("lights") if isinstance(blocks, dict) else None),
        emission_prop=getattr(p, "emission_prop", True),
        emission_keyword=getattr(p, "emission_keyword", True),
        distance_first=getattr(p, "distance_first", True)))
    state.set_runtime(client, scheduler, info, blocks)
    mats.set_light_map(blocks.get("lights") if isinstance(blocks, dict) else None)
    p.status = "已连接 %s (MC %s)" % (info.get("mod", "?"), info.get("mcVersion", "?"))
    return True, p.status


_last_push = {"key": None}


def sync_player_view(p, scene):
    """把 MC 玩家同步到 Blender 相机（位置 + 朝向，模组 POST /api/player）。

    坐标取相机相对根空物体的局部位置（与区块放置坐标系一致）。
    MC yaw: 0=南(+z) 90=西(-x) 180=北(-z) 270=东(+x)；pitch 正=低头。
    坐标映射: Blender(x,y,z) -> MC(x, -y, z)，相机眼高 -> 玩家脚位 -1.62。
    """
    import math
    from mathutils import Vector
    client = state.client()
    if client is None or scene.camera is None:
        return
    cam = scene.camera
    pos = importer.anchor_pos(cam.matrix_world.translation)
    if pos is None:
        return
    d = cam.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0))
    dx, dy_mc, dz = d.x, d.z, -d.y
    yaw = math.degrees(math.atan2(-dx, dz))
    pitch = math.degrees(-math.asin(max(-1.0, min(1.0, dy_mc))))
    key = (round(pos[0], 2), round(pos[1], 2), round(pos[2], 2),
           round(yaw, 1), round(pitch, 1))
    if key == _last_push["key"]:
        return                      # 相机没动，不打扰玩家
    _last_push["key"] = key
    try:
        client.set_player(pos[0], pos[2] - 1.62, -pos[1], yaw, pitch)
    except Exception:
        pass


def step_tick(p, scene):
    """主循环一步（Blender 定时器 / 帧回调 / 测试均调用此函数）。"""
    scheduler = state.scheduler()
    if scheduler is None:
        return False
    # 0. 视角同步（开启时把 MC 玩家传送到 Blender 相机位姿）
    if getattr(p, "sync_player", False) and getattr(p, "load_mode", "control") != "save":
        sync_player_view(p, scene)
    # 1. 锚点（相机相对根物体的位置；根物体整体变换不影响装卸）
    anchor = getattr(p, "anchor_object", None)
    cam = scene.camera
    target = anchor if anchor is not None else cam
    if target is not None:
        try:
            pos = importer.anchor_pos(target.matrix_world.translation)
            # Blender: X=MC X, Y=-MC Z, Z=MC Y(高度)。
            # 导入时区块放 (cx*16, -cz*16, y)，故 MC z = -Blender y。
            scheduler.update_anchor(pos[0], -pos[1], frame=scene.frame_current)
        except Exception:
            pass
    # 2. 装卸预算
    scheduler.tick()
    applied = importer_apply(scheduler, p.apply_per_tick)
    evicted = 0
    for key, reason in scheduler.poll_evict(p.evict_per_tick):
        importer.delete_key(key)
        evicted += 1
    # 3. 贴图补全 / 版本轮询 / 发光开关与策略热更新（变更时重建材质）
    mats.flush()
    mats.set_emission_switch(getattr(p, "emission_on", True))
    mats.set_emission_scale(getattr(p, "emission_scale", 4.0))
    mats.set_emission_improved(getattr(p, "emission_improved", True))
    mats.set_emission_strategies(getattr(p, "emission_prop", True),
                                 getattr(p, "emission_keyword", True))
    # 3.5 名单同步（R11 常见区块 / R10 白黑名单）与自动预载
    _sync_lists(p)
    if getattr(p, "auto_preload", True):
        scheduler.ensure_pinned()
    scheduler.maybe_poll_versions()
    # 4. 进度条（预热 / 首载）：LIVE 占全部受管组的比例
    c = scheduler.counts()
    live = c.get("LIVE", 0)
    pending = c.get("QUEUED", 0) + c.get("FETCHING", 0) + c.get("READY", 0)
    total = live + pending
    if pending:
        frac = live / total if total else 0.0
        p.progress = "%s %d/%d (%d%%)" % (
            "预热中" if scheduler.frozen else "加载中", live, total,
            int(frac * 100.0))
        p.progress_pct = frac * 100.0
        _wm_progress(frac)
    else:
        p.progress = ""
        p.progress_pct = 0.0
        _wm_progress(None)
    # 5. 状态栏
    p.stats = "LIVE %d | 队列 %d | 在途 %d | 面数 %.1fM | 贴图 %d" % (
        c.get("LIVE", 0), c.get("queue", 0), c.get("FETCHING", 0),
        scheduler.stats.get("tris", 0) / 1e6, mats.pending_count())
    return applied or evicted > 0


_wm_prog = {"on": False}


def _wm_progress(frac=None):
    """Blender 状态栏原生进度条。frac=None 结束；无窗口/不支持的宿主静默跳过。"""
    try:
        wm = bpy.context.window_manager
        if frac is None:
            if _wm_prog["on"]:
                wm.progress_end()
                _wm_prog["on"] = False
            return
        if not _wm_prog["on"]:
            wm.progress_begin(0, 1000)
            _wm_prog["on"] = True
        wm.progress_update(int(max(0.0, min(1.0, frac)) * 1000))
    except Exception:
        _wm_prog["on"] = False


def importer_apply(scheduler, limit):
    n = 0
    for key, payload in scheduler.poll_apply(limit):
        tex_fetcher = None
        client = state.client()
        if client is not None:
            def tex_fetcher(block, facegrp, _c=client):
                return _c.texture_png(block, facegrp)
        importer.create_or_replace(
            key, payload,
            lambda desc: mats.get_material_for(desc, tex_fetcher))
        scheduler.stats["tris"] += payload["geo"]["tris"]
        n += 1
    return n


# ------------------------------------------------------------- 区块名单 ----
# R11 常见区块（钉选）/ R10 更新白黑名单：序列化在场景属性里随 .blend 保存，
# step_tick 同步进调度器（字符串变更才解析）。

PINNED_WARN = 64

_LIST_SYNC = {}


def _obj_group_key(obj, group):
    """MCB_ 对象上的 mcb_key 自定义属性 -> 区块组键；非区块对象返回 None。"""
    try:
        mk = obj["mcb_key"]
    except Exception:
        return None
    if not isinstance(mk, dict):
        return None
    try:
        cx, cz = int(mk.get("cx")), int(mk.get("cz"))
    except (TypeError, ValueError):
        return None
    g = max(1, int(group or 1))
    return (str(mk.get("dim", "")), cx // g * g, cz // g * g)


def _selected_group_keys(context, p):
    g = int(getattr(p, "group", 2) or 2)
    keys = []
    for obj in context.selected_objects or ():
        k = _obj_group_key(obj, g)
        if k is not None and k not in keys:
            keys.append(k)
    return keys


def _sync_lists(p):
    """把场景属性里的名单字符串同步进调度器（按 (调度器id, 字符串) 缓存）。"""
    sch = state.scheduler()
    if sch is None:
        _LIST_SYNC.clear()
        return
    for prop, setter in (("pinned_chunks", sch.set_pinned),):
        s = getattr(p, prop, "") or ""
        if _LIST_SYNC.get(prop) != (id(sch), s):
            _LIST_SYNC[prop] = (id(sch), s)
            setter(parse_group_list(s))


class MCB_OT_list_manage(bpy.types.Operator):
    bl_idname = "mcb.list_manage"
    bl_label = "区块名单管理"
    bl_description = "把选中的区块对象加入/移出名单（常见区块、更新白/黑名单）"

    list_kind: bpy.props.EnumProperty(name="名单", items=[
        ("pinned", "常见区块", "不被主动卸载"),
        ("whitelist", "更新白名单", "总是自动更新"),
        ("blacklist", "更新黑名单", "永不自动更新")])
    action: bpy.props.EnumProperty(name="操作", items=[
        ("add", "加入", ""), ("remove", "移出", ""), ("clear", "清空", "")])

    def execute(self, context):
        p = context.scene.mcb
        prop = {"pinned": "pinned_chunks", "whitelist": "update_whitelist",
                "blacklist": "update_blacklist"}[self.list_kind]
        kind_name = {"pinned": "常见区块", "whitelist": "更新白名单",
                     "blacklist": "更新黑名单"}[self.list_kind]
        keys = parse_group_list(getattr(p, prop, ""))
        if self.action == "clear":
            n = len(keys)
            keys = []
        else:
            sel = _selected_group_keys(context, p)
            if not sel:
                self.report({'WARNING'}, "先选中要操作的 MCB_ 区块对象")
                return {'CANCELLED'}
            if self.action == "add":
                new = [k for k in sel if k not in keys]
                keys.extend(new)
                n = len(new)
                if self.list_kind == "pinned" and len(keys) > PINNED_WARN:
                    self.report({'WARNING'},
                                "常见区块超过 %d 组，注意内存占用" % PINNED_WARN)
            else:
                drop = set(sel)
                n = len([k for k in keys if k in drop])
                keys = [k for k in keys if k not in drop]
        setattr(p, prop, serialize_group_list(keys))
        self.report({'INFO'}, "%s %s %d 组" % (
            kind_name, {"add": "加入", "remove": "移出",
                        "clear": "清空"}[self.action], n))
        return {'FINISHED'}


def disconnect(p, keep_objects=True):
    if getattr(p, "load_mode", "control") == "save":
        ret = opssave.disconnect_save(p, keep_objects=keep_objects)
    else:
        scheduler = state.scheduler()
        if scheduler is not None:
            scheduler.stop()
        state.clear()
        mats.reset()
        mats.set_light_map(None)
        if not keep_objects:
            importer.unload_all()
        ret = True
    p.progress = ""
    _wm_progress(None)
    p.status = "未连接"
    return ret


def prewarm_camera(p, scene):
    """渲染前预热：按帧范围取相机位置并集，冻结卸载并全部入队。
    返回 (帧数, 区块数)。"""
    scheduler = state.scheduler()
    if scheduler is None:
        return 0, 0
    cam = scene.camera
    if cam is None:
        return 0, 0
    frames = []
    f0, f1 = scene.frame_preview_start, scene.frame_preview_end
    f0, f1 = int(f0), int(f1)
    for f in range(f0, f1 + 1, 2):
        scene.frame_set(f)
        pos = importer.anchor_pos(cam.matrix_world.translation)
        if pos is None:
            continue
        frames.append((pos[0], -pos[1]))      # MC z = -Blender y
    scene.frame_set(f0)
    n = scheduler.prewarm(frames)
    return len(frames), n


def bake_static(filepath):
    """把当前 LIVE 的 MCB 对象烘进 .blend 库（渲染农场友好）。"""
    objs = [o for o in bpy.data.objects if o.name.startswith("MCB_")]
    if not objs:
        return False, "没有可烘焙的区块对象"
    meshes = {o.data for o in objs}
    materials = {m for me in meshes for m in (me.materials or []) if m is not None}
    images = set()
    for m in materials:
        if m.node_tree is None:
            continue
        for n in m.node_tree.nodes:
            if getattr(n, "image", None) is not None:
                images.add(n.image)
    data_blocks = list(objs) + list(meshes) + list(materials) + list(images)
    bpy.data.libraries.write(filepath, set(data_blocks))
    return True, "已写入 %d 对象 -> %s" % (len(objs), filepath)


# ------------------------------------------------------------- load_post ----

_load_state = {"path": None}


def _on_load_post(*_args):
    """load_post：打开 .blend 后清理陈旧运行时并检测残留动态对象。

    memfile 撤销/重做也会触发 load_post——用文件路径是否变化区分：
    同一文件的 undo 只重建材质缓存（旧 ID 引用已失效），不重置连接。"""
    try:
        path = bpy.data.filepath
    except Exception:
        return
    if path == _load_state["path"]:
        mats.reset()
        return
    _load_state["path"] = path
    _handle_file_switched()


def _handle_file_switched():
    p = getattr(bpy.context.scene, "mcb", None)
    if p is None:
        return
    # 1. 运行中的连接作废：对象体系已随旧文件释放，锚点指针也已失效
    if state.scheduler() is not None:
        try:
            bpy.app.timers.unregister(_timer)
        except Exception:
            pass
        state.set_timers(timer_on=False)
        disconnect(p, keep_objects=True)
        p.status = "已打开新文件，连接已重置"
    # 2. 残留动态对象（随旧文件保存的 MCB_ 区块）：提示转静态或清除
    n = importer.count_live()
    state.set_leftover(n)
    if n:
        p.status = "发现 %d 个残留区块对象（转为静态保留或全部清除）" % n
    mats.reset()
    _wm_progress(None)


# ------------------------------------------------------------- 操作符 ----

class MCB_OT_connect(bpy.types.Operator):
    bl_idname = "mcb.connect"
    bl_label = "连接"
    bl_description = "连接运行中的 MC 桥接模组并启动调度器"

    def execute(self, context):
        p = context.scene.mcb
        ok, msg = connect(p)
        if not ok:
            self.report({'ERROR'}, msg)
            return {'CANCELLED'}
        # 相机统一挂到根空物体（整体变换时同步移动）
        if getattr(p, "auto_bind_camera", False):
            importer.bind_camera(context.scene.camera)
        # 启动主循环定时器（已注册则跳过）
        if not state.timers()[0]:
            try:
                bpy.app.timers.register(_timer, persistent=True)
                state.set_timers(timer_on=True)
            except Exception:
                pass
        return {'FINISHED'}


class MCB_OT_disconnect(bpy.types.Operator):
    bl_idname = "mcb.disconnect"
    bl_label = "断开"
    keep: bpy.props.BoolProperty(name="保留已加载对象", default=True)

    def execute(self, context):
        p = context.scene.mcb
        try:
            bpy.app.timers.unregister(_timer)
        except Exception:
            pass
        state.set_timers(timer_on=False)
        disconnect(p, keep_objects=self.keep)
        return {'FINISHED'}


class MCB_OT_unload_all(bpy.types.Operator):
    bl_idname = "mcb.unload_all"
    bl_label = "卸载全部区块"

    def execute(self, context):
        n = importer.unload_all()
        sch = state.scheduler()
        if sch is not None:
            # 清空调度器状态：范围内组由下个 tick 重新入队，常见区块由
            # 自动预载拉回（R11）；在途抓取会因状态缺失被丢弃（安全）
            with sch.lock:
                sch.state.clear()
                sch.ready.clear()
                sch.queue.clear()
                sch.inflight_keys.clear()
        self.report({'INFO'}, "已卸载 %d 个对象" % n)
        return {'FINISHED'}


class MCB_OT_leftover_keep(bpy.types.Operator):
    bl_idname = "mcb.leftover_keep"
    bl_label = "转为静态保留"
    bl_description = ("把残留的 MCB_ 区块对象当作普通静态几何保留"
                      "（重新连接后同名区块会被调度器重新生成替换）")

    def execute(self, context):
        n = state.leftover()
        state.set_leftover(0)
        p = context.scene.mcb
        if n:
            p.status = "已保留 %d 个静态区块对象" % n
            self.report({'INFO'}, p.status)
        return {'FINISHED'}


class MCB_OT_leftover_clear(bpy.types.Operator):
    bl_idname = "mcb.leftover_clear"
    bl_label = "清除残留对象"
    bl_description = "删除场景中全部 MCB_ 区块对象（含随旧文件保存的残留）"

    def execute(self, context):
        n = importer.unload_all()
        state.set_leftover(0)
        context.scene.mcb.status = "已清除 %d 个残留对象" % n
        self.report({'INFO'}, context.scene.mcb.status)
        return {'FINISHED'}


class MCB_OT_use_selected_as_anchor(bpy.types.Operator):
    bl_idname = "mcb.use_selected_as_anchor"
    bl_label = "选定对象为锚点"

    @classmethod
    def poll(cls, context):
        return context.active_object is not None

    def execute(self, context):
        p = context.scene.mcb
        p.anchor_object = context.active_object
        return {'FINISHED'}


class MCB_OT_prewarm(bpy.types.Operator):
    bl_idname = "mcb.prewarm"
    bl_label = "预热相机动画"
    bl_description = "扫描帧范围内的相机路径，预加载全部需要的区块并冻结卸载"

    _timer = None

    def execute(self, context):
        p = context.scene.mcb
        if state.scheduler() is None:
            self.report({'ERROR'}, "先连接")
            return {'CANCELLED'}
        frames, n = prewarm_camera(p, context.scene)
        self.report({'INFO'}, "预热 %d 帧 / %d 区块（渲染时保持冻结）" % (frames, n))
        return {'FINISHED'}


class MCB_OT_bake(bpy.types.Operator):
    bl_idname = "mcb.bake"
    bl_label = "烘焙静态库"
    bl_description = "把当前已加载的区块与材质写入独立 .blend（用于无 MC 环境的渲染农场）"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')

    def execute(self, context):
        ok, msg = bake_static(self.filepath or "//mc_baked.blend")
        if not ok:
            self.report({'ERROR'}, msg)
            return {'CANCELLED'}
        self.report({'INFO'}, msg)
        return {'FINISHED'}

    def invoke(self, context, event):
        self.filepath = "mc_baked.blend"
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class MCB_OT_refresh(bpy.types.Operator):
    bl_idname = "mcb.refresh"
    bl_label = "立即刷新"
    bl_description = "强制重新入队当前 LIVE 区块（世界有改动时用）"

    def execute(self, context):
        sch = state.scheduler()
        if sch is None:
            self.report({'ERROR'}, "先连接")
            return {'CANCELLED'}
        with sch.lock:
            keys = [k for k, st in sch.state.items() if st["status"] in ("LIVE", "READY")]
            for k in keys:
                st = sch.state[k]
                st["status"] = "QUEUED"
                st["gen"] = st.get("gen", 0) + 1
                sch._push(0.0, k)
        return {'FINISHED'}


class MCB_OT_bind_camera(bpy.types.Operator):
    bl_idname = "mcb.bind_camera"
    bl_label = "绑定相机到根节点"
    bl_description = "把当前场景相机设为根空物体 MCB_Root 的子物体（保持世界位姿）"

    def execute(self, context):
        cam = context.scene.camera
        if cam is None:
            self.report({'ERROR'}, "场景没有相机")
            return {'CANCELLED'}
        if importer.bind_camera(cam):
            self.report({'INFO'}, "相机已挂载到 %s" % importer.ROOT_NAME)
            return {'FINISHED'}
        self.report({'ERROR'}, "绑定失败")
        return {'CANCELLED'}


class MCB_OT_pick_save_dir(bpy.types.Operator):
    bl_idname = "mcb.pick_save_dir"
    bl_label = "选择存档目录"
    bl_description = "选择 Minecraft 存档根目录（含 region/ 与 level.dat）"

    directory: bpy.props.StringProperty(subtype='DIR_PATH')

    def execute(self, context):
        if self.directory:
            context.scene.mcb.save_dir = self.directory
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


class MCB_OT_load_assets(bpy.types.Operator):
    bl_idname = "mcb.load_assets"
    bl_label = "加载资产包"
    bl_description = "加载 MCBA1 资产包（原版贴图 + 烘焙模型，由 tools/bake_assets.py 生成）"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')

    def execute(self, context):
        from .core import assets
        p = context.scene.mcb
        path = self.filepath or p.assets_path
        if not path:
            self.report({'ERROR'}, "请选择 .mcba 资产包")
            return {'CANCELLED'}
        try:
            pack = assets.load_global(path)
        except Exception as e:
            self.report({'ERROR'}, "加载失败: %s" % e)
            return {'CANCELLED'}
        p.assets_path = path
        mats.reset()
        self.report({'INFO'}, "已加载 %d 方块 / %d 贴图 / %d 模型变体" % (
            len(pack.blockstates), len(pack.tex_names), len(pack.variants)))
        return {'FINISHED'}

    def invoke(self, context, event):
        if context.scene.mcb.assets_path:
            self.filepath = context.scene.mcb.assets_path
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


# ------------------------------------------------------------- 定时器 ----

def _timer():
    try:
        p = bpy.context.scene.mcb
        if state.scheduler() is None:
            state.set_timers(timer_on=False)
            return None
        step_tick(p, bpy.context.scene)
        return min(max(p.poll_interval, 0.02), 1.0)
    except Exception:
        return 0.2


def _frame_change(scene):
    p = scene.mcb
    if not p.auto_frame:
        return
    if state.scheduler() is None:
        return
    step_tick(p, scene)


def register_handlers():
    if _frame_change not in bpy.app.handlers.frame_change_post:
        bpy.app.handlers.frame_change_post.append(_frame_change)
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)


def unregister_handlers():
    try:
        bpy.app.handlers.frame_change_post.remove(_frame_change)
    except ValueError:
        pass
    try:
        bpy.app.handlers.load_post.remove(_on_load_post)
    except ValueError:
        pass


CLASSES = (MCB_OT_connect, MCB_OT_disconnect, MCB_OT_unload_all,
           MCB_OT_list_manage, MCB_OT_leftover_keep, MCB_OT_leftover_clear,
           MCB_OT_use_selected_as_anchor, MCB_OT_prewarm, MCB_OT_bake,
           MCB_OT_refresh, MCB_OT_bind_camera, MCB_OT_pick_save_dir,
           MCB_OT_load_assets)
