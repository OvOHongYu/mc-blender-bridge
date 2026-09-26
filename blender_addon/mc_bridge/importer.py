# -*- coding: utf-8 -*-
"""几何导入器：将调度器的 geo 字典转为 Blender 对象（foreach_set 批量写入），
并负责卸载删除与孤儿清理。

结构约定：所有 MCB_ 对象与相机统一挂在根空物体 MCB_Root 下，
区块按根物体局部坐标放置，整体变换（移动/旋转/缩放）带动全部区块与相机。
加载判定使用相机相对根物体的位置，根物体自身变换不引起装卸。
"""
import bpy

COLL_NAME = "MC Bridge"
ROOT_NAME = "MCB_Root"
MATERIALIZED_ATTR = "mcb_materialized"

_DIM_SHORT = {"overworld": "ow", "the_nether": "ne", "the_end": "end"}


def obj_name(key):
    dim, cx, cz = key
    short = dim.split(":")[-1]
    return "MCB_%s_%d_%d" % (_DIM_SHORT.get(short, short[:3]), cx, cz)


def is_root(obj):
    return bool(getattr(obj, "name", "") == ROOT_NAME)


def collection():
    coll = bpy.data.collections.get(COLL_NAME)
    if coll is None:
        coll = bpy.data.collections.new(COLL_NAME)
        bpy.context.scene.collection.children.link(coll)
    return coll


def root() -> "bpy.types.Object | None":
    """返回根空物体；不存在返回 None（不自动创建）。"""
    return bpy.data.objects.get(ROOT_NAME)


def ensure_root():
    """创建（或复用）根空物体并链入 MC Bridge 集合。"""
    obj = root()
    if obj is None:
        obj = bpy.data.objects.new(ROOT_NAME, None)
        coll = collection()
        coll.objects.link(obj)
        obj["mcb_materialized"] = True
    return obj


def anchor_pos(pos) -> tuple:
    """把世界坐标转为根物体局部坐标（0 长度拖回，根为世界）。

    Root 只做平移时等价于世界坐标减根位置；带旋转/缩放时由 matrix_world
    逆变换给出精确相对坐标，保证整体变换不触发脚手架装卸。
    """
    rt = root()
    if rt is None:
        return float(pos.x), float(pos.y), float(pos.z)
    try:
        m = rt.matrix_world
        local = m.inverted() @ pos
        return float(local.x), float(local.y), float(local.z)
    except Exception:
        t = m.translation
        return float(pos.x - t.x), float(pos.y - t.y), float(pos.z - t.z)


def _build_mesh(name, geo):
    mesh = bpy.data.meshes.new(name)
    nq = geo["nq"]
    import numpy as np
    # MC 坐标 (x=东, y=上, z=南) -> Blender Z-up:
    # 局部顶点 (x, y, z)_mc -> (x, -z, y)_bl,绕 X +90° 的正交旋转,
    # 保持右手系与面朝向(否则法线朝内)。
    v = np.asarray(geo["verts"]).reshape(-1, 4, 3)[:, :, [0, 2, 1]].copy()
    v[:, :, 1] *= -1
    nv = nq * 4
    # 全 foreach_set 路径（比 from_pydata + tolist 快 5–10 倍）
    mesh.vertices.add(nv)
    if nv:
        co = np.ascontiguousarray(v.reshape(-1, 3), np.float32)
        mesh.vertices.foreach_set("co", co.ravel())
    mesh.loops.add(nv)
    if nv:
        mesh.loops.foreach_set("vertex_index", np.arange(nv, dtype=np.int32).ravel())
    mesh.polygons.add(nq)
    if nq:
        idx = np.arange(nq, dtype=np.int32)
        mesh.polygons.foreach_set("loop_start", idx * 4)
        mesh.polygons.foreach_set("loop_total", np.full(nq, 4, dtype=np.int32))
        mesh.polygons.foreach_set("material_index", geo["mat_idx"].astype(np.int64))
        mesh.polygons.foreach_set("use_smooth", np.zeros(nq, bool))
    uv = mesh.uv_layers.new(name="UVMap")
    if uv is not None and nq:
        # Blender 4.x: foreach_set 在 .data (MeshLoopUV 集合) 上
        uv.data.foreach_set("uv", np.ascontiguousarray(geo["uv"], np.float32).ravel())
    col = mesh.color_attributes.new("Col", 'BYTE_COLOR', 'CORNER')
    if col is not None and nq:
        # vcol 是按 sRGB 空间算出来的 uint8（tint × AO）；而 BYTE_COLOR 的
        # `.color` 是**场景线性**空间，直接写 byte/255 会让着色器把 sRGB 值当线性
        # 使用 —— 等于把染色与 AO 整体提亮（实测 sRGB 0.416 的沼泽草色被当成
        # 线性 0.416，亮约 2.9 倍，观感"偏灰"）。这里先做 sRGB->线性再写入。
        f = np.ascontiguousarray(geo["vcol"], np.float32) / 255.0
        lin = np.where(f <= 0.04045, f / 12.92, ((f + 0.055) / 1.055) ** 2.4)
        col.data.foreach_set("color", lin.ravel())
    mesh.validate()
    mesh.update()
    return mesh


def create_or_replace(key, payload, mats_resolver):
    """mats_resolver(desc) -> Material；desc 见 mesher.geo_from_arrays。"""
    name = obj_name(key)
    old = bpy.data.objects.get(name)
    if old is not None:
        _delete_object(old)
    geo = payload["geo"]
    mesh = _build_mesh(name, geo)
    for desc in geo["mats"]:
        mesh.materials.append(mats_resolver(desc))
    obj = bpy.data.objects.new(name, mesh)
    dim, cx, cz = key
    # 根物体局部坐标（MC 世界坐标）：Z-up: X=MC x, Y=-MC z, Z=MC y(高度)
    obj.location = (cx * 16.0, -cz * 16.0, payload.get("yBottom", 0.0))
    # ID 属性数组不允许字符串，用 dict 存
    obj["mcb_key"] = {"dim": dim, "cx": cx, "cz": cz}
    obj["mcb_lod"] = payload.get("lod", 0)
    obj.display_type = 'SOLID'
    obj.parent = ensure_root()          # 统一挂到根空物体
    coll = collection()
    coll.objects.link(obj)
    return obj


def _delete_object(obj):
    if is_root(obj):
        return                      # 根空物体永不删除（重连/卸载时保留结构）
    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def delete_key(key):
    obj = bpy.data.objects.get(obj_name(key))
    if obj is not None:
        _delete_object(obj)
        return True
    return False


def purge_orphans():
    """移除 0 用户的 MCB 网格/图像（定期调用，防止 .blend 膨胀）。"""
    for mesh in list(bpy.data.meshes):
        if mesh.name.startswith("MCB_") and mesh.users == 0:
            bpy.data.meshes.remove(mesh)
    for img in list(bpy.data.images):
        if getattr(img, "filepath", "").find("mcbridge_tex") >= 0 and img.users == 0:
            bpy.data.images.remove(img)


def count_live():
    return sum(1 for o in bpy.data.objects
               if o.name.startswith("MCB_") and not is_root(o))


def unload_all():
    n = 0
    for obj in list(bpy.data.objects):
        if obj.name.startswith("MCB_") and not is_root(obj):
            _delete_object(obj)
            n += 1
    purge_orphans()
    return n


def bind_camera(cam):
    """把相机设为根空物体的子物体（保持世界变换）。返回是否成功。"""
    if cam is None:
        return False
    rt = ensure_root()
    if getattr(cam, "parent", None) is rt:
        return True
    try:
        mw = cam.matrix_world.copy()
        cam.parent = rt
        cam.matrix_world = mw       # 保持原世界位姿，仅改父子关系
    except Exception:
        cam.parent = rt
    return True
