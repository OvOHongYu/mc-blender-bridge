# -*- coding: utf-8 -*-
"""N 面板 UI。"""
import bpy

from . import state


class MCB_PT_panel(bpy.types.Panel):
    bl_label = "MC Bridge 动态区块"
    bl_idname = "MCB_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MC Bridge"

    def draw(self, context):
        lay = self.layout
        p = context.scene.mcb

        n = state.leftover()
        if n:
            box = lay.box()
            box.alert = True
            box.label(text="发现 %d 个残留区块对象" % n, icon='ERROR')
            row = box.row(align=True)
            row.operator("mcb.leftover_keep", icon='CHECKMARK')
            row.operator("mcb.leftover_clear", icon='TRASH')

        box = lay.box()
        box.label(text="加载模式")
        box.prop(p, "load_mode", expand=True)
        if p.load_mode == "save":
            row = box.row(align=True)
            row.prop(p, "save_dir", text="存档目录")
            row.operator("mcb.pick_save_dir", text="", icon='FILE_FOLDER')
        else:
            row = box.row(align=True)
            row.prop(p, "host")
            row.prop(p, "port")
        row = box.row(align=True)
        row.operator("mcb.connect", icon='LINKED')
        row.operator("mcb.disconnect", icon='UNLINKED')
        box.label(text=p.status)
        if p.progress:
            box = lay.box()
            col = box.column(align=True)
            prog = getattr(col, "progress", None)
            drawn = False
            if prog is not None:        # Blender 4.x 的进度条控件
                try:
                    prog(factor=min(max(p.progress_pct / 100.0, 0.0), 1.0),
                         type='BAR', text=p.progress)
                    drawn = True
                except Exception:
                    drawn = False
            if not drawn:
                col.label(text=p.progress, icon='TIME')
        if p.stats:
            if p.progress:
                box = lay.box()
            box.label(text=p.stats, icon='MESH_GRID')

        box = lay.box()
        box.label(text="相机 / 锚点")
        box.prop(p, "anchor_object", text="锚点对象")
        row = box.row()
        row.operator("mcb.use_selected_as_anchor", icon='OBJECT_DATA')
        box.prop(p, "auto_bind_camera", text="相机归入根节点", icon='CAMERA_DATA')
        row = box.row()
        row.operator("mcb.bind_camera", icon='OBJECT_DATA')
        if p.load_mode != "save":
            box.prop(p, "sync_player", text="玩家跟随相机", icon='CAMERA_DATA')
        box.prop(p, "auto_frame", text="播放时跟随帧")

        box = lay.box()
        box.label(text="加载策略")
        col = box.column(align=True)
        col.prop(p, "r_load")
        col.prop(p, "r_unload")
        col.prop(p, "mode")
        col.prop(p, "group")
        col.prop(p, "leaves")
        row = box.row(align=True)
        row.prop(p, "ymin")
        row.prop(p, "ymax")
        row = box.row(align=True)
        row.prop(p, "inflight")
        row.prop(p, "apply_per_tick")

        box = lay.box()
        box.label(text="资产包（原版贴图 / 模型）")
        row = box.row(align=True)
        row.prop(p, "assets_path", text="")
        row.operator("mcb.load_assets", text="", icon='FILE_IMAGE')
        box.prop(p, "use_models")
        box.prop(p, "biome_tint")

        box = lay.box()
        box.label(text="渲染 / 交付")
        box.prop(p, "emission_scale")
        row = box.row()
        row.operator("mcb.prewarm", icon='RENDER_ANIMATION')
        row.operator("mcb.bake", icon='FILE_BLEND')
        box.operator("mcb.refresh", icon='FILE_REFRESH')
        box.operator("mcb.unload_all", icon='TRASH')


CLASSES = (MCB_PT_panel,)
