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
    "biome_tint": True,
    "auto_bind_camera": True,
    "dim": "minecraft:overworld",
    "r_load": 8,
    "r_unload": 11,
    "ymin": -64,
    "ymax": 320,
    "mode": "mesh",
    "group": "2",
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
    "progress": "",
    "progress_pct": 0.0,
    "emission_on": True,
    "emission_scale": 4.0,
    "emission_improved": True,
    "emission_prop": True,
    "emission_keyword": True,
    "distance_first": True,
    "pinned_chunks": "",
    "auto_preload": True,
    "auto_update": True,
    "update_event": True,
    "event_interval": 0.5,
    "update_whitelist": "",
    "update_blacklist": "",
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
    biome_tint: bpy.props.BoolProperty(name="按群系调色", default=_defaults["biome_tint"],
                                       description="草/树叶/水按所在生物群系取原版染色（R8）。"
                                                   "关闭后统一用平原常量色，且不再按群系拆分贪心矩形"
                                                   "（面数更少、生成更快）。变更后需重新加载区块")
    r_load: bpy.props.IntProperty(name="加载半径", default=_defaults["r_load"], min=1, max=64,
                                  description="以锚点为中心的加载半径（区块）")
    distance_first: bpy.props.BoolProperty(name="按摄像机距离优先",
                                           default=_defaults["distance_first"],
                                           description="锚点移动时按新位置重排加载队列，"
                                                       "就绪区块按距离应用——近处先上屏"
                                                       "（网络抖动时远组先完成也不插队）。"
                                                       "派发保持并行，吞吐无损")
    r_unload: bpy.props.IntProperty(name="卸载半径", default=_defaults["r_unload"], min=2, max=96,
                                    description="迟滞卸载半径，应大于加载半径")
    ymin: bpy.props.IntProperty(name="Y 下限", default=_defaults["ymin"])
    ymax: bpy.props.IntProperty(name="Y 上限", default=_defaults["ymax"])
    mode: bpy.props.EnumProperty(name="网格模式",
                                 items=[("mesh", "服务端网格 (B)", "MC/模组侧贪心网格化，推荐"),
                                        ("raw", "本地网格 (A)", "拉取原始区块，Blender 侧 numpy 网格化")],
                                 default=_defaults["mode"])
    group: bpy.props.EnumProperty(name="区块组",
                                  items=[("1", "1×1", "单区块对象；对象数最多，帧内负担最均匀"),
                                         ("2", "2×2", "4 区块合并为一个对象（对象数 /4）"),
                                         ("4", "4×4", "16 区块合并为一个对象（对象数 /16，单帧负担更集中）")],
                                  default=_defaults["group"],
                                  description="跨区块贪心合并的组边长：本地网格路径下"
                                              "贪心矩形可跨区块边界，两条路径都按组合并对象。"
                                              "组越大对象数越少（draw call 下降），"
                                              "但单个对象更大、LOD 分级更粗")
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
    pinned_chunks: bpy.props.StringProperty(name="常见区块", default=_defaults["pinned_chunks"],
                                            description="钉选的区块组（序列化存储），不被主动卸载。"
                                                        "用「选中设为常见」从视口添加")
    auto_preload: bpy.props.BoolProperty(name="自动预载常见区块",
                                         default=_defaults["auto_preload"],
                                         description="连接后（以及「卸载全部」后）自动加载全部"
                                                     "常见区块，无论距离")
    auto_update: bpy.props.BoolProperty(name="自动更新",
                                        default=_defaults["auto_update"],
                                        description="游戏/存档里的方块改动自动重载受影响区块"
                                                    "（R10）。关闭后仅更新白名单区块")
    update_event: bpy.props.BoolProperty(name="事件驱动更新",
                                         default=_defaults["update_event"],
                                         description="以亚秒级间隔轮询模组的全局世界修订号"
                                                     "（任何方块变化 +1，只取一个整数、开销≈0），"
                                                     "一有变化立即重载受影响区块——覆盖挖掘、放置、"
                                                     "爆炸/活塞、作物生长、水与岩浆流动、命令 setblock "
                                                     "等一切改变方块状态的操作（不含箱子内容之类的"
                                                     "方块实体数据）。需要新版模组（/api/worldrev），"
                                                     "旧模组自动回退常规轮询")
    event_interval: bpy.props.FloatProperty(name="事件轮询间隔",
                                            default=_defaults["event_interval"],
                                            min=0.1, max=10.0,
                                            description="事件驱动开启时的修订号轮询间隔（秒）。"
                                                        "每次只请求一个整数，可设得很小以获得准实时"
                                                        "更新；关闭事件驱动后改用「版本轮询间隔」")
    update_whitelist: bpy.props.StringProperty(name="更新白名单",
                                               default=_defaults["update_whitelist"],
                                               description="强制更新的区块组：自动更新关闭也照常更新"
                                                           "（边拍边改的舞台区）。用「选中加入」添加")
    update_blacklist: bpy.props.StringProperty(name="更新黑名单",
                                               default=_defaults["update_blacklist"],
                                               description="永不自动更新的区块组：防止大背景区被无关"
                                                           "改动（掉落沙/水流等）反复重载。冲突时黑名单"
                                                           "优先于白名单")
    sync_player: bpy.props.BoolProperty(name="玩家跟随相机", default=_defaults["sync_player"],
                                        description="把 MC 玩家实时传送到 Blender 相机的位置与朝向"
                                                    "（需要模组 /api/player 支持）")
    auto_prewarm: bpy.props.BoolProperty(name="渲染前自动预热", default=_defaults["auto_prewarm"])
    anchor_object: bpy.props.PointerProperty(name="锚点对象", type=bpy.types.Object,
                                             description="锚点（默认跟随相机，也可选空物体/角色）")
    status: bpy.props.StringProperty(name="状态", default=_defaults["status"])
    stats: bpy.props.StringProperty(name="统计", default=_defaults["stats"])
    # 进度（预热/首载）：面板与状态栏原生进度条共用；不直接暴露给用户编辑
    progress: bpy.props.StringProperty(name="加载进度", default=_defaults["progress"])
    progress_pct: bpy.props.FloatProperty(name="进度%", default=_defaults["progress_pct"],
                                          min=0.0, max=100.0)
    emission_on: bpy.props.BoolProperty(name="自发光",
                                        default=_defaults["emission_on"],
                                        description="为发光方块生成 Emission 自发光节点"
                                                    "（Cycles 下可真实照亮场景）。关闭后不生成"
                                                    "任何发光节点，材质自动重建")
    emission_scale: bpy.props.FloatProperty(name="自发光强度", default=_defaults["emission_scale"],
                                            min=0.0, max=100.0,
                                            description="发光方块（萤石/岩浆/海晶灯/火把等）的 "
                                                        "Emission 强度倍率：实际强度 = 原版亮度"
                                                        "(0..15)/15 × 本值。0 关闭自发光；"
                                                        "Cycles 下自发光面可真实照亮场景。"
                                                        "改动即时生效（含已加载区块）")
    emission_improved: bpy.props.BoolProperty(name="改进自发光",
                                              default=_defaults["emission_improved"],
                                              description="开启后发光颜色先经对比度(2.1)与饱和度(0.9)"
                                                          "处理，只让贴图高光部分主导发光，强度系数"
                                                          "自动 ×0.175（默认 4.0 → 0.7），避免原版式"
                                                          "整块均匀泛光的观感；关闭则整块按亮度发光。"
                                                          "变更后材质自动重建")
    emission_prop: bpy.props.BoolProperty(name="亮度属性筛选",
                                          default=_defaults["emission_prop"],
                                          description="按方块亮度属性筛选发光方块（逐状态精确，"
                                                      "含模组方块）。与 ID 关键词筛选可同时开启，"
                                                      "取较大亮度；关闭对应策略后材质自动重建")
    emission_keyword: bpy.props.BoolProperty(name="ID关键词筛选",
                                             default=_defaults["emission_keyword"],
                                             description="按英文 ID 关键词筛选发光方块（lamp/candle/"
                                                         "lantern/torch/glow/fire/lava 等），命中按满"
                                                         "亮度发光——用于模组里看起来该发光但未声明"
                                                         "亮度属性的方块；可能误伤（如未点亮的红石灯、"
                                                         "火珊瑚），不想要就关掉")
