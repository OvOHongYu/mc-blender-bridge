# -*- coding: utf-8 -*-
"""MC Bridge —— Blender × Minecraft 动态区块加载插件。

无 bpy 环境（测试/模拟器）下 import 不报错；有 bpy 时注册插件。
"""
bl_info = {
    "name": "MC Bridge 动态区块",
    "author": "zcube",
    "version": (1, 2, 1),
    "blender": (3, 6, 0),
    "location": "3D 视口 N 面板 > MC Bridge",
    "description": "借助本地 MC(Fabric 模组)实例或存档文件，按相机位置动态加载/卸载区块，"
                   "带面剔除、贪心合并、AO 与 LOD 减面；支持原版资产包（贴图/模型烘焙）",
    "category": "Import-Export",
}

try:
    import bpy
    _HAS_BPY = True
except ImportError:          # 测试 / 无 Blender 环境下的开发模式
    _HAS_BPY = False

if _HAS_BPY:
    from . import props, ui, ops, importer, mats   # noqa: F401

    _CLASSES = (props.MCB_Properties,) + ops.CLASSES + ui.CLASSES

    def register():
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
        bpy.types.Scene.mcb = bpy.props.PointerProperty(type=props.MCB_Properties)
        ops.register_handlers()

    def unregister():
        ops.unregister_handlers()
        try:
            bpy.app.timers.unregister(ops._timer)
        except Exception:
            pass
        for cls in reversed(_CLASSES):
            bpy.utils.unregister_class(cls)
        del bpy.types.Scene.mcb

else:
    def register():
        pass

    def unregister():
        pass
