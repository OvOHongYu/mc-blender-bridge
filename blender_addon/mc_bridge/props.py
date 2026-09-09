# -*- coding: utf-8 -*-
"""场景属性（PropertyGroup）。_defaults 字典供 fake-bpy 测试使用，
与 bpy.props 定义保持一致（修改时两处同步）。"""
import bpy


_defaults = {
    "load_mode": "control",
    "host": "127.0.0.1",
    "port": 8788,
    "save_dir": "",
    "assets_path": "",
    "use_models": True,
    "auto_bind_camera": True,
    "dim": "minecraft:overworld",
    "r_load": 8,
    "r_unload": 11,
    "lod1_dist": 6,
    "lod2_dist": 10,
    "ymin": -64,
    "ymax": 320,
    "mode": "mesh",
    "leaves": "fancy",
    "inflight": 8,
    "apply_per_tick": 8,
    "evict_per_tick": 16,
    "poll_interval": 0.05,
    "version_poll": 5.0,
    "auto_frame": True,
    "auto_prewarm": False,
    "sync_player": False,
    "status": "未连接",
    "stats": "",
}


class MCB_Properties(bpy.types.PropertyGroup):
    load_mode: bpy.props.EnumProperty(name="加载模式",
                                      items=[("control", "控制模式", "连接本地 MC 模组/模拟服务器，实时读取世界"),
                                             ("save", "存档模式", "直接解析 MC 存档文件，无需模组/运行实例")],
                                      default=_defaults["load_mode"])
    host: bpy.props.StringProperty(name="主机", default=_defaults["host"],
                                    description="MC 模组监听地址（本机即 127.0.0.1）")
    port: bpy.props.IntProperty(name="端口", default=_defaults["port"], min=1, max=65535)
    save_dir: bpy.props.StringProperty(name="存档目录", default=_defaults["save_dir"],
                                       subtype='DIR_PATH',
                                       description="Minecraft 存档根目录（含 region/ 与 level.dat）")
    assets_path: bpy.props.StringProperty(name="资产包", default=_defaults["assets_path"],
                                         subtype='FILE_PATH',
                                         description="MCBA1 资产包（tools/bake_assets.py 生成）；"
                                                     "提供后使用原版贴图与烘焙模型")
    use_models: bpy.props.BoolProperty(name="烘焙模型", default=_defaults["use_models"],
                                       description="LOD0 近处区块用资产包模型（楼梯/栅栏真实形状）。"
                                                   "关闭后控制模式全部走服务端网格，加载更快但非完整方块近似为立方体")
    auto_bind_camera: bpy.props.BoolProperty(name="相机自动挂载", default=_defaults["auto_bind_camera"],
                                             description="连接时把当前场景相机设为 MCB_Root 的子物体，"
                                                         "整体变换时区块与相机同步移动")
    dim: bpy.props.StringProperty(name="维度", default=_defaults["dim"])
    r_load: bpy.props.IntProperty(name="加载半径", default=_defaults["r_load"], min=1, max=64,
                                  description="以锚点为中心的加载半径（区块）")
    r_unload: bpy.props.IntProperty(name="卸载半径", default=_defaults["r_unload"], min=2, max=96,
                                    description="迟滞卸载半径，应大于加载半径")
    lod1_dist: bpy.props.IntProperty(name="LOD1 距离", default=_defaults["lod1_dist"], min=1, max=64,
                                     description="超过该距离的区块关闭 AO")
    lod2_dist: bpy.props.IntProperty(name="LOD2 距离", default=_defaults["lod2_dist"], min=2, max=96,
                                     description="超过该距离的区块使用壳网格（大幅减面）")
    ymin: bpy.props.IntProperty(name="Y 下限", default=_defaults["ymin"])
    ymax: bpy.props.IntProperty(name="Y 上限", default=_defaults["ymax"])
    mode: bpy.props.EnumProperty(name="网格模式",
                                 items=[("mesh", "服务端网格 (B)", "MC/模组侧贪心网格化，推荐"),
                                        ("raw", "本地网格 (A)", "拉取原始区块，Blender 侧 numpy 网格化")],
                                 default=_defaults["mode"])
    leaves: bpy.props.EnumProperty(name="树叶",
                                   items=[("fancy", "Fancy（不遮挡）", ""),
                                          ("fast", "Fast（按实心处理）", "")],
                                   default=_defaults["leaves"])
    inflight: bpy.props.IntProperty(name="并发数", default=_defaults["inflight"], min=1, max=16)
    apply_per_tick: bpy.props.IntProperty(name="每帧应用", default=_defaults["apply_per_tick"], min=1, max=16)
    evict_per_tick: bpy.props.IntProperty(name="每帧卸载", default=_defaults["evict_per_tick"], min=1, max=64)
    poll_interval: bpy.props.FloatProperty(name="轮询间隔", default=_defaults["poll_interval"], min=0.02, max=1.0)
    version_poll: bpy.props.FloatProperty(name="版本轮询间隔", default=_defaults["version_poll"], min=1.0, max=60.0)
    auto_frame: bpy.props.BoolProperty(name="播放时跟随帧", default=_defaults["auto_frame"])
    sync_player: bpy.props.BoolProperty(name="玩家跟随相机", default=_defaults["sync_player"],
                                        description="把 MC 玩家实时传送到 Blender 相机的位置与朝向"
                                                    "（需要模组 /api/player 支持）")
    auto_prewarm: bpy.props.BoolProperty(name="渲染前自动预热", default=_defaults["auto_prewarm"])
    anchor_object: bpy.props.PointerProperty(name="锚点对象", type=bpy.types.Object,
                                             description="锚点（默认跟随相机，也可选空物体/角色）")
    status: bpy.props.StringProperty(name="状态", default=_defaults["status"])
    stats: bpy.props.StringProperty(name="统计", default=_defaults["stats"])
